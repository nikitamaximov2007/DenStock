# Выпуск private_media в ночных копиях: пошаговый runbook

Что меняется: только контейнер `web` (приложение, которое выполняет
`backup_all`). Миграций нет. `catalog-web`, боты, PostgreSQL, Caddy, обёртка
`/usr/local/sbin/denstock-backup-capped`, таймер и `.env*` НЕ меняются.
Код приложения равен квалифицированному `041a285`. Выполняется только с
отдельного разрешения владельца.

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

Результат этих проверок 2026-10-13 (повтор), все пройдены: HEAD `f7b80861`,
в рабочем дереве только неотслеживаемый `docker-compose.signing.yml`;
`748225d` доступен с сервера (`git ls-remote`); обёртка `5b5f8a93`,
Caddyfile `8ba9fb9e`; свободно 8,5 GB; proxy монтирует
`/etc/denstock/caddy/Caddyfile`, выпуск не меняет Caddy, compose, Docker и
`.env`-файлы.

### 1a. Ворота сохранности до изменения (только чтение)

- Текущие приватные файлы уже восстановимы независимо: дерево
  `/app/private_media` на сервере (16 файлов, `917447d1...`) совпадает с
  зашифрованным захватом на Mac `~/DenisStock-Recovery-Updates/2026-10-12/`.
- Последняя ночная копия проходит `verify_backup` и проверку подписи Ed25519
  закрепленным ключом (`production-1`).

## 2. PRE: копия старым кодом через обычную обёртку

```bash
systemctl start denstock-backup.service
python3 -c "import json;d=json.load(open('backups/offsite_status.json'));print(d['status'],d['uploaded_run'])"
```

Ожидается `ok` и новый ран. Если не `ok`: остановиться, выпуск не делать.

## 3. Выпуск только web

```bash
docker tag denstock-web:latest denstock-web:rollback-f7b8086
git fetch origin claude/private-media-backup-release
git checkout --detach 748225d934ae675edd2f9795f42ce2c8d43c4b22
sed -i "s/^DENSTOCK_APP_COMMIT=.*/DENSTOCK_APP_COMMIT=$(git rev-parse HEAD)/" .env
docker compose build web
docker compose up -d --no-deps --wait web
docker compose logs --tail 50 web </dev/null | grep -E "No migrations to apply|Error" 
```

Не выполнять `docker compose up` без имени сервиса и без `--no-deps`: иначе
пересоберутся `catalog-web` и боты из нового дерева.

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

## 4a. Что считается успехом и как виден сбой

`backup_all` нового кода в production-режиме перед выходом с кодом 0 сам
проверяет подпись Ed25519 закрепленным ключом, заново считает SHA-256 дампа,
public media и private_media, проверяет дамп и архив media и отказывает, если
private_media нет. Обёртка работает с `set -e`: при любой из этих ошибок
выгрузка не начинается. Статус-файл при этом не обновляется, но экран
«Резервные копии» помечает статус старше 26 часов как «запуск не завершился»;
второй независимый сигнал даст монитор GitHub-копий.

Чего действующая обёртка НЕ делает: не скачивает копию из Yandex обратно для
сверки содержимого (сравнивает только размер и число файлов; при выгрузке
rclone сверяет MD5). Это закрывает отдельно протестированная обёртка
`scripts/operations/denstock-backup-capped-safe.sh` (чтение назад
`rclone check --download`); ее установка это отдельное решение владельца и
не входит в этот выпуск.

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
docker compose up -d --no-deps --no-build --wait web
```

Данных откат не меняет (миграций нет). Раны, созданные новым кодом, остаются
обычными ранами: база и public media в них прежнего формата, лишний файл
`private_media.tar.gz` старый код игнорирует. Простой при выпуске и откате:
перезапуск `web`, около минуты.
