# План реализации — Слой 24. Фотографии деталей и вложения

**Статус:** УТВЕРЖДЁН (2026-06-30) · все решения зафиксированы жёстко (§ниже) · реализация строго в границах §22. **Ключевой инвариант приёмки: фото — информационный слой, НЕ складское действие. Слой может добавлять записи `PartTypeImage`/`PartItemImage` и файлы в media, но НЕ создаёт `StockMovement`, НЕ меняет `StockBalance`/количества/статусы, продажи/движения и НЕ трогает scanner/barcodes.**

**Усиление при утверждении:** dev-serving `/media/` в `DEBUG` — **обязательная** часть этого слоя (§6). Docker/Caddy media-том — future-note (§21).

---

## 1. Цель слоя

Дать возможность **прикреплять фотографии** к деталям, чтобы Денис и сотрудники быстро
видели внешний вид, состояние, маркировку, повреждения и комплектацию — при звонке
клиенту и в работе склада. Это **информационный** слой поверх каталога/экземпляров:

- фото **не меняют** остатки, движения, продажи, ремонт, возвраты, инвентаризацию;
- хранение — **локальная файловая система** (`MEDIA_ROOT`), без облака/S3;
- **без тяжёлой обработки** изображений (ни thumbnails, ни resize, ни EXIF, ни OCR/AI);
- только: загрузка, просмотр, выбор «главного» фото, мягкое удаление.

### Что уже есть (переиспользуем)

| Факт в коде | Значение для слоя |
|---|---|
| `MEDIA_URL = "/media/"`, `MEDIA_ROOT = BASE_DIR/"mediafiles"` ([base.py](config/settings/base.py)) | хранилище уже настроено — добавим только dev-serving |
| `STORAGES.default = FileSystemStorage` | локальное файловое хранилище из коробки |
| `mediafiles/` и `media/` в `.gitignore` | загруженные файлы не попадут в git |
| Возможности вычисляются в коде (`roles.ROLE_CAPABILITIES`); группы сидятся по имени (`0002_roles_groups`) | `MANAGE_IMAGES` — **код-онли** в `roles.py`, без миграции прав |
| `PartTypeDetailView` (любой авторизованный), `PartItemDetailView` (`can_manage_inventory`/viewer) | просмотр фото наследует доступ к карточке |
| Pillow **не установлен** | см. §7 — берём `FileField` без Pillow |

### Что нового

- Две **модели-изображения** (`PartTypeImage`, `PartItemImage`) → миграции в `catalog`/`inventory`.
- Абстрактная база + валидатор файлов в `apps/core` (без таблицы, без зависимостей).
- Возможность `MANAGE_IMAGES`; загрузка/удаление/primary; галерея на карточках.
- Dev-serving `/media/` в `DEBUG`. **Без новых зависимостей** (FileField, не ImageField).

---

## 2. К каким объектам прикрепляем — **рекомендация: PartType + PartItem; остальное — future**

| Объект | Слой 24 | Обоснование |
|---|---|---|
| **PartType** | ✅ | типовое фото вида детали — нужно всегда (звонок клиенту, «как выглядит») |
| **PartItem** | ✅ | состояние конкретного экземпляра, дефекты, маркировка, комплектация |
| StockLot | ⏸ future | bulk-лот обезличен; фото экземпляра/вида закрывают потребность; не плодим сущности |
| RepairOrder / WriteOffDocument / ReturnDocument | ⏸ future | фото-доказательства к документам — отдельная ценность и отдельный слой; здесь раздуло бы объём и затронуло бы доменные приложения |

**Обоснование.** Слой даёт максимум практической пользы двумя целями (вид + экземпляр)
при минимуме поверхности. Документальные фото (ремонт/возврат/списание) — самостоятельная
тема (юридический след, привязка к статусам документов), её выносим в будущий слой, чтобы
не размывать «фото детали» и не лезть в доменную логику документов.

---

## 3. Разница PartType photo vs PartItem photo

- **PartType photo** — *типовая* иллюстрация вида детали (каталожный вид, «эталон»).
  Показываем на карточке вида (`/parts/<pk>/`) и как «лицо» детали.
- **PartItem photo** — *конкретный* экземпляр: фактическое состояние, царапины/дефекты,
  серийная табличка, комплектность. Показываем на карточке экземпляра (`/inventory/<pk>/`).

Правило отображения: на карточке экземпляра показываем **его** фото; если у экземпляра
фото нет — можно показать типовое фото его `part_type` как запасной визуал (с пометкой
«типовое фото вида»). На карточке вида — только фото вида.

---

## 4. Где размещаем домен — **рекомендация: модели в catalog/inventory + абстрактная база в core**

| Вариант | Вердикт |
|---|---|
| `PartTypeImage` в `apps/catalog`, `PartItemImage` в `apps/inventory` | ✅ **выбран**: конкретные FK, доменная принадлежность, простые миграции |
| Новое `apps/attachments` с `GenericForeignKey` (contenttypes) | ❌ для двух целей избыточно: generic-FK усложняет запросы, ослабляет целостность, тяжелее в админке |
| Новое `apps/media` | ❌ лишнее приложение ради двух моделей |

**Чтобы не дублировать поля/логику**, общее выносим в **абстрактную** модель
`BaseImage` (без таблицы) в `apps/core/models.py`, плюс валидатор и `upload_to`-хелперы в
`apps/core/files.py`. Конкретные модели наследуют базу и добавляют свой FK на владельца.
Так получаем DRY без generic-FK машинерии. Миграции появятся в `catalog` и `inventory`
(в `core` — нет, база абстрактна).

---

## 5. Модели (минимальные)

Абстрактная база (в `apps/core/models.py`, без таблицы):

```python
class BaseImage(models.Model):
    image = models.FileField("Файл", upload_to=...)        # FileField, не ImageField (§7)
    caption = models.CharField("Подпись", max_length=255, blank=True)
    is_primary = models.BooleanField("Главное фото", default=False)
    sort_order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активно", default=True)   # soft-delete (§14)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="%(app_label)s_%(class)s_uploads",       # обязателен для abstract
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["sort_order", "uploaded_at"]
```

Конкретные:

```python
# apps/catalog/models.py
class PartTypeImage(BaseImage):
    part = models.ForeignKey(PartType, on_delete=models.CASCADE, related_name="images")

# apps/inventory/models.py
class PartItemImage(BaseImage):
    part_item = models.ForeignKey(PartItem, on_delete=models.CASCADE, related_name="images")
```

- FK-владелец — `CASCADE` (фото живёт ровно пока жив объект; запись чистится вместе с ним).
- `is_active=False` — **мягкое удаление** (§14); запись и файл остаются.
- Частичный индекс/constraint на «одно primary среди активных» — см. §13.

---

## 6. Хранение файлов

- **Уже есть:** `MEDIA_URL=/media/`, `MEDIA_ROOT=BASE_DIR/mediafiles`, `FileSystemStorage`,
  `mediafiles/` в `.gitignore`.
- **Dev-serving — ОБЯЗАТЕЛЬНО в этом слое:** в `config/urls.py` при `DEBUG` добавляем
  `+ static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)`, чтобы загруженные фото
  реально открывались локально по `/media/...`. Без этого слой не имеет смысла (фото
  загрузилось бы, но не отображалось). В проде статику/медиа раздаёт фронт-прокси (§21).
- **Без облака/S3** на этом слое (рекомендация заказчика соблюдена).

---

## 7. FileField vs ImageField — **рекомендация: FileField без Pillow**

`ImageField` **требует Pillow** (валидация/размеры), а Pillow в проекте **нет**. Ценность
`ImageField` — извлечение размеров и Pillow-проверка — нам на этом слое не нужна (thumbnails
и resize вне объёма, §9). Поэтому:

- берём **`FileField`** + **собственную валидацию** (расширение + размер + сигнатура файла);
- **Pillow не добавляем** (принцип минимальных зависимостей — как CSV вместо openpyxl в
  Слое 22, как Code128-SVG вместо python-barcode в Слое 23);
- thumbnails/resize/EXIF-поворот — **будущий слой** (тогда и появится Pillow, осознанно).

---

## 8. Ограничения файлов (валидация, §15)

Валидатор `validate_image_upload(file)` в `apps/core/files.py`:

- **расширение** в allowlist: `jpg`, `jpeg`, `png`, `webp` (lowercase);
- **размер** ≤ **10 MB** (`file.size`);
- **сигнатура (magic bytes)** соответствует jpg/png/webp — проверяем **первые байты**, а не
  доверяем браузерному `content_type` (он подделывается). Подпись-сниффинг **без
  зависимостей** (чистая проверка префикса);
- **явный запрет** `svg`/`html`/`js`/прочего: SVG — это XML и может нести скрипт, поэтому
  не входит в allowlist ни по расширению, ни по сигнатуре;
- **имя файла не доверяем**: на диск пишем сгенерированное имя (§20), исходное имя
  пользователя не используется в пути;
- файлы **не исполняются** — только хранятся и отдаются как media.

> Примечание: у Django нет глобального лимита размера загрузки файла (есть
> `DATA_UPLOAD_MAX_MEMORY_SIZE` — про не-файловые поля). Поэтому размер режем **в
> валидаторе формы** явно.

---

## 9. Обработка изображений — НЕ делаем

На Слое 24 **нет**: thumbnails, сжатия, авто-поворота по EXIF, OCR, удаления фона, AI-анализа.
Только: **загрузка → просмотр → primary → мягкое удаление**.

---

## 10. Права — **рекомендация: новая capability `MANAGE_IMAGES`**

`MANAGE_IMAGES` в `apps/accounts/roles.py` (код-онли, как `PRINT_LABELS`; в
`ALL_CAPABILITIES` и в `ROLE_CAPABILITIES`).

| Роль | Загрузка/удаление/primary | Просмотр |
|---|---|---|
| Администратор | ✅ | ✅ |
| Руководитель | ✅ | ✅ |
| Кладовщик | ✅ | ✅ |
| Продавец/Мастер | ❌ | ✅ (там, где видит карточку) |
| Наблюдатель | ❌ | ✅ |

**Обоснование.** Маркировка/фотофиксация состояния — складская работа (приёмка/хранение),
поэтому управление фото у Админа/Руководителя/Кладовщика. Продавец/Мастер и Наблюдатель —
только смотрят. Добавление возможности — **без миграции** (вычисляется в коде).

---

## 11. Просмотр

- Фото видит тот, кто **уже** видит карточку владельца: `PartType` — любой авторизованный;
  `PartItem` — у кого `can_manage_inventory` или роль Наблюдатель (как сейчас в
  `PartItemDetailView`). Отдельного гейта на просмотр фото **не вводим** — наследуем доступ
  к странице.
- Загрузка/удаление/primary — **только** `MANAGE_IMAGES` (POST, 403 иначе).

---

## 12. UI

- **Карточка PartType** (`templates/catalog/part_detail.html`): блок «Фотографии» — primary
  крупно + миниатюрная (через CSS, не серверный thumbnail) галерея активных фото; при
  `MANAGE_IMAGES` — форма «Добавить фото» (`<input type=file>` + `caption`), кнопки
  «Сделать главным» и «Удалить» у каждого фото.
- **Карточка PartItem** (`templates/inventory/item_detail.html`): то же для экземпляра; если
  своих фото нет — опц. показать типовое фото вида (с подписью).
- Без drag-and-drop редактора, без массовой загрузки. Простые `<form enctype="multipart/
  form-data" method="post">` + POST-экшены.

---

## 13. Primary image (правила)

- У объекта **не более одного** активного `is_primary=True`.
- **Первое** загруженное активное фото → автоматически primary.
- При установке нового primary — остальные primary этого владельца **сбрасываются** (в одной
  транзакции).
- При удалении/деактивации текущего primary — **назначаем следующим primary** ближайшее
  активное фото (по `sort_order`, затем по `uploaded_at`); если активных не осталось —
  владелец без primary. *(Карточка всегда показывает что-то, если фото есть.)*
- Целостность: `UniqueConstraint(fields=[<owner>], condition=Q(is_primary=True, is_active=True))`
  — гарантирует один primary среди активных на уровне БД.

---

## 14. Удаление — **рекомендация: soft-delete (`is_active=False`), файл оставляем**

- Удаление = `is_active=False`; запись и файл **остаются** на диске.
- **Обоснование:** мгновенно убирает фото из интерфейса без рисков гонок и «висящих»
  ссылок; не усложняет storage-cleanup; сохраняет историю/возможность восстановления.
  Физическую очистку осиротевших файлов выносим в **будущий maintenance-слой** (команда
  `prune_media`). Это согласуется с принципом «не плодить сложность файловой системы».

---

## 15. Безопасность загрузки

- **POST-only** + **CSRF** (Django-формы).
- **Серверная** валидация (§8): размер, allowlist расширений, сигнатура; не доверяем
  `content_type` и имени файла от клиента.
- **Уникальный путь** файла (UUID, §20) — нет перезаписи/коллизий и предсказуемых URL.
- Гард `MANAGE_IMAGES` на всех мутациях (upload/primary/delete) → иначе **403**.
- Несуществующий владелец/фото → **404** (читаем объект по pk на сервере).

---

## 16. Read-only гарантия относительно склада (ключевой инвариант)

Загрузка/правка/удаление фото **не**: создаёт `StockMovement`; меняет `StockBalance`/
`quantity`/статусы `PartItem`/`StockLot`; создаёт `Sale`/`Repair`/`Return`/`WriteOff`/
`InventoryCount`; трогает `barcode`/scanner. Гарантируется тем, что image-сервисы пишут
**только** строки image + файлы. Закрепляется тестами (§18).

---

## 17. Интеграция с поиском — **рекомендация: НЕ трогать `/search/` в этом слое**

Полезный поиск-thumbnail требует серверных миниатюр (вне объёма, §9), а показ полноразмерных
фото в списке тяжёл. Поэтому `/search/` (`part_search`) **не меняем**; фокус — карточки
PartType/PartItem. Thumbnail-в-поиске — будущий слой (после слоя миниатюр). Placeholder в
поиске тоже не добавляем, чтобы не раздувать.

---

## 18. Тесты (`tests/test_images.py`)

Используем `SimpleUploadedFile` с валидными magic-байтами и `override_settings(MEDIA_ROOT=
tmp_path)` (изоляция файлов теста).

1. `MANAGE_IMAGES` может загрузить фото PartType (POST → 302/200, запись создана).
2. Без `MANAGE_IMAGES` (Продавец) → **403** на загрузку фото PartType.
3. `MANAGE_IMAGES` может загрузить фото PartItem.
4. Без `MANAGE_IMAGES` → **403** на загрузку фото PartItem.
5. Первое активное фото становится **primary** автоматически.
6. Установка нового primary **сбрасывает** старый (один primary среди активных).
7. Удаление primary (`is_active=False`) → **следующее активное** становится primary.
8. Soft-deleted фото **не показывается** в активной галерее.
9. Валидные расширения (`jpg/jpeg/png/webp`) с корректной сигнатурой — проходят.
10. `svg`/`html`/`js` — **отклонены** (allowlist расширений).
11. Файл с **неверными magic bytes** (напр. `.png` с текстом внутри / фейковый
    `content_type`) — **отклонён** (сигнатура-проверка, не доверяем browser content-type).
12. Слишком большой файл (> 10 MB) — отклонён (юнит-тест валидатора со stub `.size`).
13. Карточка PartType показывает primary image (URL `/media/...` в HTML).
14. Карточка PartItem показывает primary image.
15. **Read-only:** загрузка фото не создаёт `StockMovement`.
16. **Read-only:** загрузка не меняет `StockBalance`/`quantity`/статус `PartItem`/`StockLot`.
17. **Read-only:** scanner-резолв и `barcode` не изменились.
18. Роли: Админ/Руководитель/Кладовщик имеют `MANAGE_IMAGES`; Продавец/Мастер и Наблюдатель — нет.
19. Просмотр: пользователь, видящий карточку, видит фото; гость → редирект на логин.
20. Dev-serving: при `DEBUG=True` URL `/media/<path>` обслуживается (если тестируемо —
    через `override_settings(DEBUG=True)` и проверку резолва media-маршрута).
21. `makemigrations --check` — после добавления миграций **чисто** (нет «забытых» миграций).

---

## 19. Миграции

- **Будут** миграции: `catalog` (`PartTypeImage`) и `inventory` (`PartItemImage`) — обычные
  `CreateModel` + constraint primary (§13). Это ожидаемо (в отличие от Слоёв 22/23).
- **Capabilities — без миграции:** `MANAGE_IMAGES` вычисляется в коде (`roles.py`); группы
  уже сидятся по имени (`0002_roles_groups`). Новую возможность сидить в БД не нужно.
- Критерий: **после** создания миграций `makemigrations --check` — «изменений нет».

---

## 20. Файловая структура (`upload_to`)

- `upload_to`-callable → **`part-types/<owner_id>/<uuid4>.<ext>`** и
  **`part-items/<owner_id>/<uuid4>.<ext>`** под `MEDIA_ROOT` (`mediafiles/`).
- Имя файла — **UUID4** (не имя пользователя); расширение — из **валидированного** набора.
- **Обоснование:** группировка по владельцу — порядок и удобство ручного просмотра; UUID —
  отсутствие коллизий, непредсказуемость URL, отсутствие утечки исходного имени.

---

## 21. Docker / локально

- **Локально/dev:** файлы пишутся в `mediafiles/` (gitignored), раздаются Django при `DEBUG`
  (§6). Этого достаточно для работы Дениса локально.
- **Docker (future-note, не усложняем):** в `docker-compose.yml` у сервиса `web` **нет**
  тома для `mediafiles/` и Caddy не раздаёт `/media/` — значит в текущем compose загрузки не
  переживут пересборку контейнера. Рекомендация (отдельно, не в этом слое): добавить
  именованный том `media:` на `MEDIA_ROOT` и маршрут `/media/` в Caddy. **Production/cloud
  storage не трогаем.** В рамках Слоя 24 цель — рабочая локальная загрузка.

---

## 22. Границы Слоя 24 (чего НЕ делаем)

Не реализуем: thumbnails/resize; OCR; AI-анализ; PDF; облачное хранилище/S3; drag-and-drop
редактор; массовую загрузку; фото документов ремонта/возврата/списания; изменение складской
физики; `StockMovement`; правку scanner/barcodes; thumbnail-в-поиске; физическую очистку
файлов (soft-delete only); Pillow и тяжёлую обработку изображений.

---

## 23. Ручная проверка

1. Кладовщиком открыть `/parts/<pk>/` → «Добавить фото» (jpg) → фото появилось и стало
   primary; подпись отображается.
2. Загрузить второе фото, нажать «Сделать главным» → primary переключился, старое перестало
   быть главным.
3. «Удалить» главное → следующее активное стало главным; удалённое исчезло из галереи.
4. Открыть `/inventory/<pk>/` → загрузить фото экземпляра; если фото нет — видно типовое фото
   вида (с подписью).
5. Продавцом/Мастером и Наблюдателем: фото **видно**, кнопок загрузки/удаления **нет**; прямой
   POST на загрузку → **403**.
6. Попытка загрузить `.svg`/`.txt`/файл > 10 MB → отклонено с понятной ошибкой.
7. После серии загрузок: `StockMovement`/`StockBalance`/статусы/`barcode` не изменились;
   scanner-резолв прежний.

---

## 24. Критерии готовности

1. Фото грузятся/просматриваются на карточках PartType и PartItem; primary работает по §13.
2. `FileField` без Pillow; валидация: allowlist `jpg/jpeg/png/webp`, ≤10 MB, проверка
   сигнатуры; SVG/прочее отклоняется; имя файла — UUID.
3. Доступ: загрузка/удаление/primary — `MANAGE_IMAGES` (Продавец/Мастер, Наблюдатель → 403);
   просмотр наследует доступ к карточке.
4. Удаление — soft (`is_active=False`), файл остаётся; primary переназначается.
5. **Read-only:** фото не создают движений и не меняют склад/статусы/scanner/barcode (тесты).
6. Dev-serving `/media/` в `DEBUG`; файлы в `mediafiles/` (gitignored).
7. Границы §22 соблюдены; новых зависимостей нет.
8. `pytest`/`ruff`/`djlint --check`/`manage.py check` зелёные; `makemigrations --check` —
   после создания миграций «изменений нет».

---

## 25. Файлы (создаются/изменяются)

**Создаются:**
- `apps/core/files.py` — валидатор `validate_image_upload` + `upload_to`-хелперы.
- `apps/catalog/migrations/000X_parttypeimage.py`, `apps/inventory/migrations/000X_partitemimage.py`.
- `apps/catalog/forms` (или расширение) — форма загрузки фото вида; аналогично для inventory.
- `tests/test_images.py`.
- (опц.) `templates/partials/_image_gallery.html` — переиспользуемый блок галереи.

**Изменяются:**
- `apps/core/models.py` — абстрактная `BaseImage`.
- `apps/catalog/models.py` — `PartTypeImage`; `apps/inventory/models.py` — `PartItemImage`.
- `apps/accounts/roles.py` — `MANAGE_IMAGES` (+`ALL_CAPABILITIES`, +Admin/Manager/Storekeeper);
  `apps/accounts/models.py` — свойство `can_manage_images`.
- `apps/catalog/part_views.py` + `apps/catalog/part_urls.py` — upload/primary/delete фото вида.
- `apps/inventory/views.py` + `apps/inventory/urls.py` — upload/primary/delete фото экземпляра.
- `templates/catalog/part_detail.html`, `templates/inventory/item_detail.html` — блок фото.
- `config/urls.py` — dev-serving media при `DEBUG`.
- `tests/test_roles.py` — учесть `MANAGE_IMAGES`.

**Без изменений:** scanner/ledger/складская физика; зависимости; `docker-compose.yml` (том —
future-note §21).

---

## 26. Что будет закоммичено

Два коммита (как в Слоях 5–23):
1. `План Слоя 24: фотографии деталей и вложения` — этот файл (push в `origin/main`).
2. `Слой 24: фотографии деталей и вложения` — реализация (после `pytest`, `ruff`,
   `djlint --check`, `makemigrations --check`, `manage.py check`), затем push в `origin/main`.

Останавливаемся перед **Слоем 25**.

---

## Решения (утверждены 2026-06-30)

Все границы зафиксированы заказчиком жёстко. Открытых вопросов нет.

1. **Цели:** PartType + PartItem. StockLot фото — **не делаем**; фото документов
   (RepairOrder/Return/WriteOff/InventoryCount) — **не делаем** (future) — ✅ принято.
2. **Архитектура:** `PartTypeImage`(catalog) + `PartItemImage`(inventory) + абстрактная
   `BaseImage`(core); **без** `apps/attachments` и `GenericForeignKey` — ✅ принято.
3. **Поле файла:** `FileField`; **Pillow не добавляем**; `ImageField` не используем (требует
   Pillow); thumbnails/resize/EXIF/OCR/AI — future — ✅ принято.
4. **Валидация:** allowlist `jpg/jpeg/png/webp`, ≤10 MB, extension + **magic-bytes**, запрет
   `svg/html/js`, не доверяем browser content-type, имя — UUID (исходное не используем) — ✅ принято.
5. **Право:** `MANAGE_IMAGES` для Админа/Руководителя/Кладовщика; Продавец/Мастер и
   Наблюдатель — только просмотр — ✅ принято.
6. **Просмотр:** наследует доступ к карточке PartType/PartItem; загрузка/primary/удаление —
   только `MANAGE_IMAGES` — ✅ принято.
7. **Удаление:** soft-delete (`is_active=False`), файл физически не удаляем; cleanup orphan —
   future maintenance — ✅ принято.
8. **Primary:** первое активное фото — primary; новый primary сбрасывает старый; одно active
   primary на объект (service + БД-constraint, где аккуратно); при soft-delete primary —
   следующее активное; нет активных → primary отсутствует — ✅ принято.
9. **Хранение:** локально `mediafiles/`; **dev-serving `/media/` в `DEBUG` — обязательно в
   этом слое**; Docker/Caddy media-том — future-note; без облака/S3 — ✅ принято.
10. **Поиск:** `/search/` в этом слое не трогаем (thumbnail — future) — ✅ принято.
11. **Миграции:** модели → миграции в catalog/inventory; `MANAGE_IMAGES` — код-онли без
    отдельной seed-миграции (возможности вычисляются в коде) — ✅ принято.
12. **Границы §22 и read-only §16** — фиксированы жёстко: фото не трогают складскую физику,
    scanner, barcodes, продажи, движения — ✅ принято.
