# Storage Address V2

## Архитектурный аудит

`StorageLocation` уже является стабильной identity физического места. Остатки,
лоты, движения, резервы, preferred location, документы и пересчёты ссылаются на
его primary key. Поэтому переход на V2 не создаёт новые ячейки вместо старых и
не переносит stock: меняются только code, auto-barcode и parent metadata.

До V2 адрес создавался в `apps.warehouse.addresses.compose_address` как
`S-L-D-C`. Реальный пользовательский ввод с отдельным L находился в форме
пересчёта поступления. Остальные складские workflows выбирают готовый
`StorageLocation` по FK, code или barcode и не зависят от количества сегментов.

Существующая модель допускает произвольное дерево и уже содержит
`StorageLocationRenameHistory`, но до V2 отсутствовали:

- canonical parser адреса;
- historical aliases старых code/barcode;
- анализ коллизий при удалении L;
- групповой atomic rename ящика и его ячеек;
- сохранение исторического адреса при показе старого `StockMovement`.

Переход `S-L-D -> S-D` физически неоднозначен. Например,
`S03-L01-D01` и `S03-L02-D01` не могут оба стать `S03-D01`. Значение L/D также
не доказывает фактический порядок ящиков снизу вверх. DenisStock не выводит
production mapping автоматически.

## Каноническая модель

Новые пользовательские адреса имеют только три уровня:

- `S03` - стеллаж;
- `S03-D02` - ящик;
- `S03-D02-C05` - ячейка.

Ящики нумеруются снизу вверх: нижний ящик имеет номер D01, следующий D02 и так
далее. Сортировка выполняется по нормализованному номеру D по возрастанию.

Legacy-типы и legacy-коды остаются читаемыми для истории, но новые формы их не
создают. Новый drawer использует отдельный внутренний тип `drawer`; старые
`section`, `shelf`, `zone` и адреса с L/B/K/X не переосмысливаются молча.

## Aliases и история

При migration или rename старые code и auto-barcode становятся aliases той же
`StorageLocation`. Scanner и точный ввод могут найти текущую ячейку по активному
старому адресу, но текущий canonical code всегда имеет приоритет.

Historical alias не резервирует namespace навсегда. Если освободившийся код
выдают другой текущей ячейке, прежний alias атомарно становится inactive и
больше не участвует в scanner/operator lookup. Сама строка alias и неизменяемая
`StorageLocationRenameHistory` сохраняются. Когда код освобождается снова,
старый inactive alias автоматически не оживает; новый redirect создаётся только
следующим фактическим rename владельца этого кода.

`StorageLocationRenameHistory` хранит old/new code, actor, reason и общий
operation key. Групповой rename ящика создаёт записи для ящика и каждой ячейки.
Для старого движения адрес на момент операции восстанавливается по этой цепочке;
текущий адрес остаётся доступен рядом.

## Migration workflow

Команда `migrate_storage_addresses_v2` по умолчанию выполняет только dry-run.
Mapping передаётся JSON-файлом в формате:

```json
{
  "S03-L01-D01": "S03-D01",
  "S03-L02-D01": "S03-D02"
}
```

Dry-run показывает каждое `OLD -> NEW`, создаваемых родителей, active/inactive
состояние, collisions code/barcode/alias и legacy-группы без mapping. Любая
неоднозначность блокирует apply. Применение требует одновременно `--apply` и
явное подтверждение, выполняется одной транзакцией с row locks и повторной
проверкой плана. Production apply в рамках разработки V2 не выполняется.

Apply сохраняет IDs конечных ячеек и не создаёт `StockMovement`, не меняет
`StockLot`, `StockBalance`, reservations или `PartPreferredLocation`. Активный
`StorageLocationLock` на любой затрагиваемой Location блокирует migration.

## Rename ящика

На странице canonical drawer доступно «Переименовать ящик». Preview показывает
новый drawer code и все child cell mappings. Существующий target drawer,
duplicate child, alias/barcode collision или active location lock блокируют
операцию. Auto-swap и overwrite отсутствуют.

Rename блокирует rack, drawer и children по стабильному порядку и обновляет их
атомарно. Stock сам по себе rename не запрещает, потому что все FK и IDs
сохраняются. Ошибка любой child-операции откатывает весь rename.
