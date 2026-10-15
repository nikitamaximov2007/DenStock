# Выпуск private_media в ночных копиях: пошаговый runbook

Приложение меняется в `web`, `telegram-bot` и `max-bot`: оба бота используют
обновлённый `cleanup_attachment` из `apps/customer_requests/attachments.py`.
Оставить их на старом образе означает сохранить гонку удаления файла во время
бэкапа. Миграций нет. `catalog-web`, PostgreSQL, Caddy, обёртка
`/usr/local/sbin/denstock-backup-capped` и таймер НЕ меняются.
Точный SHA нужно подтвердить перед выпуском. Выполняется только с отдельного
разрешения владельца. В production и emergency-local web-restore запрещён
даже при включённом `DENSTOCK_ENABLE_WEB_RESTORE`.

Подробности и обоснования: `private-media-backup-only-release.md`.

## 0. Доказано до выпуска (2026-10-13, только чтение и изолированные стенды)

- Обёртка выгружает каталог рана целиком (`rclone copy "$RUN_PATH"`), сверяет
  размер и число файлов; `verify_backup` не вызывает. Новый файл
  `private_media.tar.gz` уйдет в Yandex без изменения обёртки.
- Ключ подписи в `web` есть; 16 приватных файлов (51 253 509 байт) читаются
  UID 2001; ссылок и спецфайлов нет; свободно 8,5 GB.
- Хранение в Yandex: моделирование правил обёртки на реальном списке ранов
  (11 ранов, 511 558 566 байт) с PRE и POST в день выпуска и 21 ночью по
  ~98 МБ: максимум 9 копий и 470 МБ в день выпуска, затем 7 копий и около
  700 МБ при мягком лимите 838 860 800. Цикл удаления по размеру не
  срабатывает ни разу. Блокера нет.
- Особенность действующей обёртки (было и раньше): недельный уровень хранения
  не накапливается, копия старше 6 суток удаляется, то есть история в Yandex
  около 7 дней.
- Тесты кандидата: 172 SQLite, 168 PostgreSQL 16 (полнота архива, порча,
  отсутствующие файлы, ссылки, изменения во время упаковки, прерванный
  бэкап). Полное восстановление из нового формата с проверкой каждого
  приватного файла и всех ссылок на файлы: прогон `GITHUB=1 synthetic_drill.sh`.

## 1. Предпроверка (только чтение)

```bash
ssh -i ~/.ssh/id_ed25519 -o IdentitiesOnly=yes root@185.250.44.206
cd /opt/denstock && git rev-parse HEAD                      # ожидается f7b80861...
sha256sum /usr/local/sbin/denstock-backup-capped             # 5b5f8a93...
python3 -c "import json;print(json.load(open('backups/offsite_status.json'))['status'])"   # ok
df -h /opt/denstock                                          # свободно не меньше 2 GB
docker compose exec -T -u 2001:2001 web find /app/private_media ! -readable </dev/null   # пусто
```

## 2. PRE: копия старым кодом через обычную обёртку

```bash
systemctl start denstock-backup.service
python3 -c "import json;d=json.load(open('backups/offsite_status.json'));print(d['status'],d['uploaded_run'])"
```

Ожидается `ok` и новый ран. Если не `ok`: остановиться, выпуск не делать.

## 3. Выпуск web и обоих ботов

```bash
docker tag denstock-web:latest denstock-web:rollback-f7b8086
git fetch origin
git checkout --detach <отдельно утверждённый SHA>
sed -i "s/^DENSTOCK_APP_COMMIT=.*/DENSTOCK_APP_COMMIT=$(git rev-parse HEAD)/" .env
docker compose build web telegram-bot max-bot
docker compose up -d --no-deps --wait web telegram-bot max-bot
docker compose logs --tail 50 web telegram-bot max-bot </dev/null | grep -E "No migrations to apply|Error"
```

Не выполнять `docker compose up` без имени сервиса и без `--no-deps`: иначе
могут быть пересозданы `catalog-web` и PostgreSQL. До команды проверить
отсутствие неожиданных миграций и сохранить отдельные образы отката ботов.

## 4. POST: первая полная копия новым кодом

```bash
systemctl start denstock-backup.service
RUN=$(python3 -c "import json;print(json.load(open('backups/offsite_status.json'))['uploaded_run'])"); echo $RUN
ls -la backups/$RUN                                          # есть private_media.tar.gz
python3 -c "import json;m=json.load(open('backups/$RUN/manifest.json'));print(m['private_media_status'],m['private_media_file_count'])"   # included 16
docker compose exec -T web python manage.py verify_backup $RUN </dev/null | grep -E "Подпись|private_media"
set -a; . ./.env.backup; set +a; rclone check backups/$RUN "$BACKUP_OFFSITE_TARGET/$RUN"   # 0 differences
```

Ожидается: `[OK] Подпись manifest (Ed25519)`,
`[OK] Содержимое private_media совпадает с manifest`, `0 differences`.

## 5. Ночью и утром

Следующая ночная копия (03:00): `offsite_status.json` = `ok`, в новом ране
есть `private_media.tar.gz`. Потом копия этого рана попадает в комплект на
Mac или в GitHub, и восстановление проверяется вне сервера
(`scripts/dr/offline_restore.sh`, ветка `claude/offline-full-service-restore`).

## 6. Откат (если шаг 3 или 4 не прошел)

```bash
git checkout --detach f7b80861fc1d81f6be70c86e78be7f06418a82d2
sed -i "s/^DENSTOCK_APP_COMMIT=.*/DENSTOCK_APP_COMMIT=f7b80861fc1d81f6be70c86e78be7f06418a82d2/" .env
docker tag denstock-web:rollback-f7b8086 denstock-web:latest
```

Это не полная команда отката: исходные образы `telegram-bot` и `max-bot`
тоже нужно заранее сохранить и вернуть, затем пересоздать те же три сервиса
без сборки. Если исходный образ любого сервиса не сохранён, остановиться и
составить отдельный план по фактическим image ID. PostgreSQL не трогать.

Данных откат не меняет (миграций нет). Раны, созданные новым кодом, остаются
обычными ранами: база и public media в них прежнего формата, лишний файл
`private_media.tar.gz` старый код игнорирует. Простой при выпуске и откате:
перезапуск трёх сервисов; перед откатом сохранить их исходные образы.
