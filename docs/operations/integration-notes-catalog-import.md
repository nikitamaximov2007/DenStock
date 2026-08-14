# Как объединить импорт каталога с текущей integration-веткой

Заметка для следующего агента. Сам merge здесь НЕ выполнен намеренно.

## Что объединяем

- `integration/roadmap-client360-emergency` на `4910fd4`: аварийный режим,
  мультискан-корзина, справочник клиентов, отчёты по клиентам;
- `feature/catalog-excel-import`: импорт каталога из Excel и надбавка VIN.

Обе ветки отходят от `main` (`ff6048b`) независимо.

## Почему конфликтов почти не ожидается

Ветки трогают разные области:

- импорт каталога живёт в новом приложении `apps/catalog_import`, меняет
  `apps/brp/*`, `apps/catalog/services.py` и добавляет пункт в группу
  «Каталог» навигации;
- integration-ветка меняет `apps/operations`, `apps/actions`, `apps/customers`,
  `apps/sales`, `apps/repairs`, `apps/reports` и группы «Продажи» и «Отчёты».

Ожидаемые точки пересечения ровно две.

**1. `apps/accounts/navigation.py`.** Обе ветки добавляют пункты меню, но в
разные группы: импорт в `_catalog_tabs`, клиенты в `_sales_tabs`, отчёты в
блок отчётов. Конфликт, если возникнет, будет соседними строками и решается
объединением обоих пунктов.

**2. `tests/test_navigation_simplification.py`.** Тест фиксирует точный состав
меню. После объединения ожидаемые списки нужно дополнить И пунктом «Импорт
каталога» в группе каталога, И пунктами клиентов и отчётов из
integration-ветки. Это не конфликт логики, а обновление ожиданий.

## Порядок

1. Новый worktree от `main`, новая ветка интеграции.
2. Сначала `integration/roadmap-client360-emergency` (она крупнее).
3. Затем `feature/catalog-excel-import`.
4. `manage.py makemigrations --check` и `showmigrations`: миграции обеих веток
   независимы (`catalog_import/0001_initial` против `customers/0001_initial`,
   `sales/0005`, `repairs/0003`), пересечений по таблицам нет.
5. Полный pytest.

## На что посмотреть отдельно

Настройки тестов. Возможность гонять тесты на реальном PostgreSQL
(`DENSTOCK_TEST_DATABASE_URL` в `config/settings/test.py`) пришла из ветки
аварийного режима и в ветке импорта каталога отсутствует. После объединения
она появится, и тесты импорта можно будет прогонять на PostgreSQL штатно, без
временной правки настроек.

Известный пробел оттуда же: `test_private_attachment_ownership_headers_and_missing_file`
падает только на PostgreSQL, причина и попытка исправления описаны в
`docs/operations/emergency-local-mode.md`.
