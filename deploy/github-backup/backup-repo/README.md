# DenisStock: зашифрованные резервные копии

Этот приватный репозиторий хранит только код наблюдателя и зашифрованные
резервные копии в GitHub Releases. Архивы базы и файлов не коммитятся в Git.
Каждый выпуск `denstock-backup-*` содержит `*.signed-backup.tar.age` и
`*.backup.json`. Внутри шифртекста находятся `db.dump`, `media.tar.gz`,
`private_media.tar.gz` и подписанный Ed25519 `manifest.json`.

## Что хранить отдельно

- Файл закрытого ключа age `recovery-identity.age` и его парольную фразу.
  Нужна независимая копия вне VPS, GitHub и основного Mac.
- Закреплённый публичный ключ проверки подписи production manifest и его
  отпечаток. Ключ из только что скачанного архива не является подтверждением
  подлинности самого архива.
- Доступ к репозиторию исходников DenisStock. Полный Git clone получает все
  опубликованные ветки и позволяет открыть точный `app_commit` из manifest.

Ни закрытый ключ age, ни токены GitHub, ни `.env`, ни данные клиентов здесь не
публикуются. Потеря ключа age или парольной фразы делает копии нечитаемыми.

## Скачать и проверить копию без VPS и Yandex

На новой машине установите `git`, `age`, Python с `cryptography`, Docker и
Docker Compose. Создайте файл с GitHub-токеном **только для чтения** этого
репозитория и правами 0600. Токен передаётся через путь в `GHB_TOKEN_FILE`,
никогда через аргумент команды.

```bash
git clone https://github.com/nikitamaximov2007/DenStock.git DenisStock-recovery
git -C DenisStock-recovery switch --detach origin/codex/github-release-backups-prod-67b

GHB_CONFIG=/nonexistent \
GHB_REPO=nikitamaximov2007/datamax \
GHB_TOKEN_FILE=/private/path/github-read.token \
python3 DenisStock-recovery/scripts/operations/github_release_backup.py fetch \
  --out /private/path/github-kit

bash DenisStock-recovery/scripts/dr/prepare_kit_restore.sh \
  --kit /private/path/github-kit \
  --identity /private/path/recovery-identity.age \
  --work /private/path/new-restore-work \
  --fresh-secrets \
  --pubkey /private/path/pinned-production-ed25519-public.pem \
  --source-git https://github.com/nikitamaximov2007/DenStock.git
```

`prepare_kit_restore.sh` запросит парольную фразу и загрузит исходники по
**полному SHA** из manifest. Не используйте shallow или single-branch clone
как источник: в нём может не оказаться развёрнутого production-коммита.
Скрипт остановится с ошибкой, если SHA отсутствует. Он создаст новые секреты
для изолированного восстановления; прежний production `.env` ему не нужен.

Затем выполните напечатанную команду `offline_restore.sh --mode test`. Она
проверит Ed25519 и SHA-256, запустит новый PostgreSQL 16, восстановит базу,
публичные и приватные файлы, сверит данные и HTTP в отдельном Docker-проекте
`dr-*`. После проверки удалите только созданные тестовые ресурсы командой
`scripts/dr/cleanup_dr.sh <имя dr-проекта>`. Не переключайте рабочий сайт
на восстановленную копию без отдельного плана аварийного восстановления.

## Права и ротация

Для ежедневной отправки нужен отдельный fine-grained GitHub token: владелец
`nikitamaximov2007`, доступ только к `datamax`, `Contents: Read and write` и
обязательное `Metadata: Read`. Права `Administration`, `Actions` и `Workflows`
ему не нужны. Токен Mac для скачивания и контроля: только `Contents: Read`.
Токены хранятся в отдельных файлах 0600, а не в конфигурации Git или логах.

Процедура считает размеры **всех** Release-assets, включая чужие выпуски и
черновики, через постраничный GitHub API. Жёсткий предел составляет 1 ГиБ,
рабочая цель 850 МиБ. Удаляются только распознанные старые backup Releases,
при этом остаются как минимум две проверенные копии. Если новая копия не
помещается вместе с ними, отправка останавливается. Immutable releases
защищают файлы после публикации, но токен записи всё ещё способен удалить
выпуск целиком. Поэтому нужна отдельная холодная копия и независимый
наблюдатель за свежестью выпусков.

Подробная процедура: `docs/operations/github-release-backups.md` в
репозитории исходников DenisStock.
