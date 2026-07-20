# ИИ-поддержка через MAXINIK: изоляция сети и launcher

## Статус

Документ описывает интегрированный host-side и Django external launcher layer.
Feature по умолчанию выключена. Production activation разрешена только после
backup, установки отдельного MAXINIK client, device login, runtime verification,
PostgreSQL gate и pre-activation smoke tests из этого runbook.

Зафиксированные версии:

- sing-box `1.13.14`, стабильный release от 25 июня 2026 года;
- официальный Linux amd64 DEB `sing-box_1.13.14_linux_amd64.deb`;
- SHA-256 DEB: `320523f9586877c4cb244df753d848356787e15f2f4e23a00908af2422206542`;
- Codex CLI `0.142.5`;
- официальный native asset `codex-x86_64-unknown-linux-musl.tar.gz`;
- SHA-256 Codex asset:
  `cb933ec3cb61bf4b5fc88eecf5e6149829faa6172535b6ef0afb0154beb4aab8`;
- SHA-256 распакованного Codex binary:
  `ac06f492f3ded7a8e2f36dc961e3cc5276a3c4841a2695d4681d0557c5b30e41`;
- launcher protocol `1`;
- launcher `1.0.0`.

Официальные источники:

- `https://github.com/SagerNet/sing-box/releases/tag/v1.13.14`;
- `https://sing-box.sagernet.org/installation/package-manager/`;
- `https://sing-box.sagernet.org/configuration/inbound/mixed/`;
- `https://sing-box.sagernet.org/configuration/outbound/vless/`;
- `https://sing-box.sagernet.org/configuration/shared/tls/`;
- `https://github.com/openai/codex/releases/tag/rust-v0.142.5`.

Installer скачивает только зафиксированный официальный DEB и сравнивает полный
SHA-256. Затем `dpkg-deb --extract` извлекает только проверенный binary в
`/usr/local/lib/denstock-ai/bin/sing-box`. Package maintainer scripts не
выполняются, vendor service не устанавливается и непривязанный install script не
используется. Тем же способом installer скачивает официальный native Codex
archive, проверяет полный SHA-256, принимает ровно один regular member с
фиксированным именем и после установки требует точный вывод
`codex-cli 0.142.5`.

## Архитектура

```text
Django container
  | framed protocol v1 over Unix socket
root-owned systemd launcher instance
  | validates request, checks health, drops UID
denstock-ai + Codex CLI 0.142.5
  | HTTP CONNECT variables, direct egress blocked
127.0.0.1:2080 mixed proxy
  | VLESS + Reality, xtls-rprx-vision
denstock-ai-proxy + sing-box 1.13.14
  |
MAXINIK server
  |
OpenAI
```

Только процесс с UID `denstock-ai` получает proxy environment. Caddy, Django,
PostgreSQL, Docker, SSH, Git, backups, обновления ОС и остальные процессы не
меняют route, DNS или environment. TUN не создаётся. Default route и global DNS
не меняются. Proxy слушает только IPv4 loopback.

## Threat model

Защита рассчитана на следующие ошибки и атаки:

- Codex игнорирует одну или все proxy variables;
- пользовательский prompt пытается передать shell, model, cwd, `-c` или path;
- request ID содержит traversal;
- schema, screenshot или request directory заменены symlink;
- Django process пытается подменить файлы между проверкой и запуском;
- proxy остановился, subscription истёк или route MAXINIK не работает;
- launcher вызван повторно для активного UUID;
- другой локальный пользователь пытается использовать proxy;
- Codex или дочерний процесс пытается открыть прямой IPv4, IPv6, UDP или DNS;
- stderr, prompt или credentials могут попасть в journal.

Не рассматриваются как доверенные данные: prompt, screenshot, request ID и
содержимое request directory. Доверенными являются только root-owned launcher,
launcher config, systemd units и числовой UID request creator, заданный во время
установки.

## Почему не используется VPN для всего сервера

Global TUN или смена default route связали бы доступность склада, SSH, backups,
Docker registry, package updates и PostgreSQL с личной VPN-подпиской. Ошибка VPN
могла бы отрезать сервер или изменить исходящий IP всех сервисов. Здесь sing-box
работает как обычный local proxy без route/DNS mutations, а owner-based firewall
касается только UID `denstock-ai`.

## Linux users и permissions

| Объект | Owner/group | Mode | Назначение |
| --- | --- | --- | --- |
| `denstock-ai` | отдельный system user | nologin | Только Codex CLI |
| `denstock-ai-proxy` | отдельный system user | nologin | Только sing-box |
| `denstock-ai-client` | system group | без shell | Доступ к launcher socket и создание requests |
| `/etc/denstock-ai` | `root:denstock-ai-proxy` | `0750` | Конфигурация host-side слоя |
| `maxinik.env` | `root:root` | `0600` | Исходные VPN secrets, renderer only |
| `sing-box.json` | `denstock-ai-proxy:denstock-ai-proxy` | `0600` | Rendered client config |
| `launcher.json` | `root:root` | `0600` | Fixed model, limits, UIDs и paths |
| `CODEX_HOME` | `denstock-ai:denstock-ai` | `0700` | Device auth и Codex state |
| requests root | `root:denstock-ai-client` | `1731` | Client создаёт UUID; AI имеет только traverse |
| request directory | request creator | `0700` | Один UUID до передачи launcher |
| schema/image | request creator | `0600` | Только фиксированные имена |
| launcher code | `root:root` | `0755/0644` | Не writable service users |
| launcher socket | `root:denstock-ai-client` | `0660` | Local Unix IPC |

Ни один service user не должен входить в `docker`, `sudo` или
`denstock-ai-client`. Installer останавливается, если существующий service user
находится в `docker`/client group, имеет другой shell/home или `.ssh` directory.
`denstock-ai` не получает repository, `.env`, PostgreSQL socket, backups,
production media или SSH keys. Requests root даёт ему только execute для
доступа к уже переданному directory `0700`, но не list/write. Launcher unit
скрывает `/opt/denstock`, Docker/PostgreSQL paths и backup paths через
`InaccessiblePaths`. `denstock-ai-proxy` не получает `CODEX_HOME`, repository,
DB или Docker.

Sudoers не используется. Django имеет только Unix socket и request root.

## MAXINIK secrets и renderer

Git содержит только `deploy/ai-support/maxinik.env.example` с `.invalid` hosts и
фиктивными значениями. Реальные UUID, server, Reality key, short ID, SNI,
subscription/VLESS URI и tokens нельзя помещать в Git, Django DB, tickets,
backups или logs.

Создание source file на staging:

```bash
sudo install -d -o root -g denstock-ai-proxy -m 0750 /etc/denstock-ai
sudo install -o root -g root -m 0600 /dev/null /etc/denstock-ai/maxinik.env
sudoedit /etc/denstock-ai/maxinik.env
```

Проверка без записи:

```bash
sudo /usr/local/sbin/denstock-ai-render-maxinik \
  --env-file /etc/denstock-ai/maxinik.env \
  --check --show-redacted
```

Dry-run без записи:

```bash
sudo /usr/local/sbin/denstock-ai-render-maxinik \
  --env-file /etc/denstock-ai/maxinik.env \
  --dry-run --show-redacted
```

Atomic rendering и проверка sing-box:

```bash
sudo /usr/local/sbin/denstock-ai-render-maxinik \
  --env-file /etc/denstock-ai/maxinik.env \
  --output /etc/denstock-ai/sing-box.json
sudo -u denstock-ai-proxy /usr/local/lib/denstock-ai/bin/sing-box check \
  --config /etc/denstock-ai/sing-box.json
sudo stat -c '%U:%G %a %n' /etc/denstock-ai/maxinik.env \
  /etc/denstock-ai/sing-box.json /etc/denstock-ai/launcher.json
```

Renderer не принимает secrets через argv, не раскрывает значения в error/output,
не следует symlink, пишет temporary file `0600`, делает `fsync` и atomic replace.
Поддерживается stdin, но staging runbook предпочитает root-only file. VLESS URI
import намеренно не добавлен: это уменьшает parser surface и риск утечки URI.

## sing-box и local proxy

Mixed inbound sing-box поддерживает HTTP и SOCKS на одном порту. Конфигурация:

- bind строго `127.0.0.1`;
- default port `2080`, порт задаётся server config;
- `set_system_proxy=false`;
- VLESS + Reality;
- flow строго `xtls-rprx-vision`;
- network `tcp`;
- packet encoding `xudp`, `packetaddr` или empty;
- optional ALPN только `h2` и `http/1.1`;
- TUN, redirect, tproxy и route mutation отсутствуют.

uTLS fingerprint поддерживается только ради совместимости с параметрами клиента
MAXINIK. Документация sing-box не рекомендует uTLS как универсальную защиту от
fingerprinting. Значение должно совпадать с выданной конфигурацией и отдельно
проверяться на staging.

## Proxy environment и отсутствие fallback

Launcher создаёт environment с нуля. Передаются только:

```text
HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
http_proxy https_proxy all_proxy no_proxy
CODEX_HOME HOME PATH LANG LC_ALL
```

Все proxy variables используют `http://127.0.0.1:<port>`. Mixed inbound
обрабатывает HTTP CONNECT. Для Codex CLI `0.142.5` нельзя считать SOCKS
`socks5h://` универсально подтверждённым для каждого внутреннего HTTP client,
поэтому launcher не зависит от него. `ALL_PROXY` указывает на тот же HTTP proxy.
Даже если HTTP stack проигнорирует variables, nftables и systemd cgroup policy
не позволят direct egress. Direct fallback отсутствует.

## nftables fail-closed

Отдельная table `inet denstock_ai` содержит output chain с общей `policy accept`.
Правила в порядке обработки:

1. UID `denstock-ai` может открыть TCP только к `127.0.0.1:<proxy-port>`.
2. Root может диагностировать local proxy; другие local users на этот порт не
   допускаются.
3. Весь остальной output UID `denstock-ai` получает reject.

Последнее правило не зависит от family/protocol, поэтому закрывает direct IPv4,
IPv6, TCP, UDP и DNS, включая уже established direct connections. Остальные UID
сохраняют обычный output. Общий firewall, default policy и другие tables не
меняются.

Static dry-run после установки config/users:

```bash
sudo env PYTHONPATH=/usr/local/lib/denstock-ai \
  python3 -m denstock_ai_network.firewall install --dry-run
```

Проверка и применение выполняются systemd unit. Для ручной диагностики:

```bash
sudo nft -nn list table inet denstock_ai
sudo nft --check --file /run/denstock-ai/nftables.conf
```

Удаление только собственной table:

```bash
sudo env PYTHONPATH=/usr/local/lib/denstock-ai \
  python3 -m denstock_ai_network.firewall remove
```

Remove идемпотентен. При reload сначала остановить launcher socket, затем
применить новую policy и только после успешной проверки вернуть socket.

## Safe launcher contract

Root-owned `/usr/local/sbin/denstock-ai-launcher` принимает только:

```text
denstock-ai-launcher version
denstock-ai-launcher login-status
denstock-ai-launcher capabilities --json
denstock-ai-launcher exec-support-request <canonical-uuid>
```

`socket-serve` является внутренним systemd transport mode. Launcher не принимает
binary, model, cwd, image path, environment, Codex subcommand или config override
от клиента. Запрещены resume, search, MCP, plugins, apps, hooks, shell, write
sandbox и произвольный `-c`.

Codex binary является отдельным regular file `/usr/local/bin/codex`,
`root:root`, без group/other write. Symlink и npm shim launcher отвергает.
Installer устанавливает проверенный native artifact `0.142.5` и сверяет также
SHA-256 распакованного binary.

Для `exec-support-request` prompt приходит только через bounded stdin/framed
socket. Directory вычисляется как `<runtime-root>/<canonical-uuid>`. Допустимы:

- `support-response.schema.json` с точным audited schema;
- максимум один `attachment.png|jpg|jpeg|webp`;
- directory `0700`, files `0600`, ожидаемый numeric owner;
- regular files с link count 1, без symlink;
- screenshot не больше configured limit.

Launcher создаёт exclusive lock. Затем через `O_DIRECTORY` и `O_NOFOLLOW`
повторно проверяет directory inode, точный набор имён, owner/mode/link count,
размер и schema через открытые file descriptors. После этого делает `fchown` к
`denstock-ai`. Sticky runtime root не позволяет прежнему owner переименовать
directory. По завершении request directory удаляется root launcher.

Codex argv, model, read-only sandbox, approval `never`, disabled web/MCP/apps/
plugins/tools/features и output limits зафиксированы в launcher. Version check
требует ровно `codex-cli 0.142.5`. Login check принимает только ChatGPT device
auth. Process запускается без shell, в новом process group, с bounded nonblocking
stdin/stdout/stderr и общим timeout; timeout/overflow завершает process group.
Prompt и screenshot path не логируются. Systemd направляет launcher stderr в
`null`; framed response получает только вызывающий Django adapter.

## Handshake и health

Успешный `capabilities --json` возвращает только:

```json
{
  "codex_cli_version": "0.142.5",
  "direct_network_blocked": true,
  "launcher_version": "1.0.0",
  "network_mode": "maxinik-proxy-only",
  "protocol_version": 1,
  "proxy_health": "ok"
}
```

Допустимые health statuses:

```text
ok
proxy_unavailable
direct_network_not_blocked
unexpected_egress
configuration_error
```

Health проверяет nftables до proxy process. Затем проверяет active systemd unit,
ровно один listener `127.0.0.1:<port>` и HTTP CONNECT к fixed ChatGPT endpoint
через local proxy. Optional staging egress validator возвращает только status,
а не IP. При любом не-`ok` Codex не запускается. Остановленный proxy при
установленном firewall даёт `proxy_unavailable` и
`direct_network_blocked=true`.

## systemd units

- `denstock-ai-proxy.service`: sing-box под `denstock-ai-proxy`;
- `denstock-ai-firewall.service`: одна root oneshot policy с `CAP_NET_ADMIN`;
- `denstock-ai-launcher.socket`: local Unix socket `0660`;
- `denstock-ai-launcher@.service`: один root launcher на connection с drop UID.

Launcher service дополнительно использует `IPAddressDeny=any` и
`IPAddressAllow=localhost`. Это cgroup defense поверх nftables. `PrivateNetwork`
не используется, так как отдельный namespace не видел бы host loopback proxy.
Proxy service не получает `IPAddressDeny`, потому что ему нужен VLESS egress.
Оба сервиса имеют `ProtectSystem`, `ProtectHome`, kernel protections, empty
ambient capabilities и ограниченный address-family set. Proxy не получает
network/admin capabilities и не может создавать TUN.

Root launcher сохраняет `CAP_NET_ADMIN` только для фиксированного read-only
вызова `nft -nn list chain inet denstock_ai output` в health-check. Клиент не
может менять argv этого вызова. Codex запускается после drop UID/GID, с пустыми
supplementary groups и без ambient capabilities, поэтому capability ему не
передаётся. Изменение firewall выполняет только отдельный firewall unit.
`AF_NETLINK` разрешён launcher unit для read-only health inspection через `nft`
и `ss`. Proxy unit использует его без network capabilities, потому что sing-box
подписывается на route updates при запуске даже без TUN и изменения маршрутов.

## Host installation

Dry-run из trusted checkout:

```bash
cd /path/to/trusted/denstock-checkout
sudo env PYTHONPATH="$PWD/scripts/ai-support" python3 \
  -m denstock_ai_network.installer \
  --dry-run \
  --model SET_AUDITED_MODEL \
  --request-creator-uid 2001 \
  --proxy-port 2080
```

Apply не включает и не запускает units:

```bash
sudo env PYTHONPATH="$PWD/scripts/ai-support" python3 \
  -m denstock_ai_network.installer \
  --apply \
  --model SET_AUDITED_MODEL \
  --request-creator-uid 2001 \
  --proxy-port 2080
```

После первой установки доступны явные lifecycle entrypoints:

```bash
sudo /usr/local/sbin/denstock-ai-update --dry-run \
  --model SET_AUDITED_MODEL --request-creator-uid 2001 --proxy-port 2080
sudo /usr/local/sbin/denstock-ai-verify --installed
sudo /usr/local/sbin/denstock-ai-verify --runtime
sudo /usr/local/sbin/denstock-ai-rollback --dry-run
```

Install и update используют один идемпотентный код и отказываются заменять
активные units. `--installed` проверяет root ownership, отсутствие symlink и
точные версии. `--runtime` дополнительно требует точный launcher handshake и
ChatGPT login status; API key login не принимается.

Installer отказывается менять файлы, если любой из собственных proxy/firewall/
socket units или launcher instance уже active. Активный staging слой сначала
останавливают и проверяют отдельно; installer не делает скрытый restart.

`request-creator-uid` должен совпадать с `DENSTOCK_WEB_UID` non-root Django
container. После host installation узнайте numeric GID client group и задайте
его в production `.env`:

```bash
getent group denstock-ai-client
```

Production web запускается с override:

```bash
docker compose -f docker-compose.yml \
  -f deploy/ai-support/docker-compose.external.yml config
docker compose -f docker-compose.yml \
  -f deploy/ai-support/docker-compose.external.yml up -d --build
```

Оба bind mount используют `create_host_path: false`: отсутствующие launcher
socket или requests root не создаются Docker-ом с неверным типом/режимом, а
activation завершается fail closed до исправления host installation.

До первого non-root запуска host bind `backups/` и существующие Docker volumes
должны быть доступны numeric UID/GID web process. Не менять ownership database
volume или host project checkout.

После secrets, renderer, `sing-box check`, Codex install/login и firewall proof:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now denstock-ai-firewall.service
sudo systemctl enable --now denstock-ai-proxy.service
sudo systemctl enable --now denstock-ai-launcher.socket
sudo systemctl is-active denstock-ai-firewall.service
sudo systemctl is-active denstock-ai-proxy.service
sudo systemctl is-active denstock-ai-launcher.socket
sudo /usr/local/sbin/denstock-ai-launcher capabilities --json
```

Local journal inspection:

```bash
sudo journalctl -u denstock-ai-firewall.service -n 30 --no-pager
sudo journalctl -u denstock-ai-proxy.service -n 30 --no-pager
```

Не копировать proxy journal в tickets: connection errors могут содержать private
MAXINIK endpoint. Launcher journal не содержит prompt/stderr.

Stop/disable:

```bash
sudo systemctl disable --now denstock-ai-launcher.socket
sudo systemctl disable --now denstock-ai-proxy.service
sudo systemctl disable --now denstock-ai-firewall.service
```

## Device login

Device login выполняется вручную только на staging, после firewall proof. Команда
должна запускаться под `denstock-ai` с очищенным environment и local proxy. Не
вводить token в shell argv, Django, ticket или log. Полученный auth остаётся в
`/var/lib/denstock-ai/codex-home`, mode `0700`, и не входит в backups.

Пример transient unit для ручного staging login:

```bash
sudo systemd-run --wait --collect --pty \
  --unit=denstock-ai-device-login \
  --uid=denstock-ai --gid=denstock-ai \
  --working-directory=/var/lib/denstock-ai/codex-home \
  --setenv=CODEX_HOME=/var/lib/denstock-ai/codex-home \
  --setenv=HOME=/var/lib/denstock-ai/codex-home \
  --setenv=PATH=/usr/local/bin:/usr/bin:/bin \
  --setenv=HTTP_PROXY=http://127.0.0.1:2080 \
  --setenv=HTTPS_PROXY=http://127.0.0.1:2080 \
  --setenv=ALL_PROXY=http://127.0.0.1:2080 \
  --setenv=NO_PROXY=127.0.0.1,localhost \
  --property=NoNewPrivileges=yes \
  --property=PrivateTmp=yes --property=PrivateDevices=yes \
  --property=ProtectSystem=strict --property=ProtectHome=yes \
  --property=ReadWritePaths=/var/lib/denstock-ai/codex-home \
  --property=IPAddressDeny=any --property=IPAddressAllow=localhost \
  --property='InaccessiblePaths=-/opt/denstock -/var/backups -/var/lib/docker -/run/docker.sock -/run/postgresql' \
  /usr/local/bin/codex -c 'forced_login_method="chatgpt"' login --device-auth
```

Login не разрешает временно снимать firewall. Если device flow не работает через
proxy, staging acceptance считается failed.

## Staging acceptance plan

1. Создать отдельный MAXINIK client `denstock-ai`, не переиспользовать личный URI.
2. Проверить trusted checkout и выполнить installer `--dry-run`.
3. Выполнить installer `--apply`; убедиться, что units disabled/stopped.
4. Проверить users, groups, nologin, отсутствие `.ssh`, Docker и repository access.
5. Создать root-only `maxinik.env`, render и выполнить `sing-box check`.
6. Запустить только proxy и доказать ровно один bind `127.0.0.1:2080`.
7. Применить nftables policy для UID `denstock-ai`.
8. Под `denstock-ai` доказать отказ direct TCP к fixed public IP.
9. Под `denstock-ai` доказать отказ direct UDP/DNS и IPv6.
10. Через proxy проверить route; сравнить только SHA-256 direct/proxy egress IP.
11. Проверить установленный installer-ом Codex CLI `0.142.5` командой
    `denstock-ai-verify --installed`.
12. Выполнить `codex login --device-auth` под `denstock-ai` через local proxy.
13. Проверить launcher `version`, `login-status`, затем handshake JSON.
14. Выполнить один read-only smoke `exec-support-request` без private данных.
15. Проверить общий timeout и process-group cleanup.
16. Остановить proxy; ожидать `proxy_unavailable` без Codex process.
17. Повторно доказать отсутствие direct fallback при остановленном proxy.
18. Проверить screenshot request, symlink/traversal/mode/owner refusals и cleanup.
19. После rebase проверить PostgreSQL global gate и concurrency=1.
20. Только после независимого review рассматривать снятие production guard.

OpenAI endpoint smoke и real login разрешены только пунктами 12-14 на staging.
Они не выполнялись при подготовке этой ветки.

## Production activation checklist

1. Подтвердить backup, rollback commit, обычный web health и свободное место.
2. Повторить PostgreSQL gate и полный test suite.
3. Сверить production UID/GID, regular Codex binary, mounts и отсутствие secrets
   в repository, Compose environment, logs и backups.
4. Сначала применить firewall, затем proxy, затем проверить fail-closed health.
5. Включать launcher socket только после exact `capabilities --json`.
6. Выполнить text/screenshot smoke без private данных, cleanup и proxy-stop test.
7. Включить env flags только после `denstock-ai-verify --runtime`.
8. Выполнить `python manage.py check --deploy` внутри non-root web container.
9. При любой ошибке оставить `AI_SUPPORT_ENABLED=false`.

## Egress proof без раскрытия IP

На staging можно сравнить hashes, не записывая IP:

```bash
curl --silent --show-error https://api.ipify.org | sha256sum
sudo -u denstock-ai env -i \
  HTTPS_PROXY=http://127.0.0.1:2080 \
  HTTP_PROXY=http://127.0.0.1:2080 \
  ALL_PROXY=http://127.0.0.1:2080 \
  NO_PROXY=127.0.0.1,localhost \
  PATH=/usr/bin:/bin \
  curl --silent --show-error https://api.ipify.org | sha256sum
```

Hashes должны отличаться. Full IP не сохранять в journal/report. Эта команда не
является регулярным production health check.

## Rollback

Сначала dry-run:

```bash
sudo env PYTHONPATH=/usr/local/lib/denstock-ai python3 \
  -m denstock_ai_network.rollback --dry-run
```

Apply:

```bash
sudo env PYTHONPATH=/usr/local/lib/denstock-ai python3 \
  -m denstock_ai_network.rollback --apply
```

Rollback останавливает/disables три units, удаляет только table
`inet denstock_ai`, exact pinned Codex binary, units, wrappers, private sing-box
binary, installed launcher code/docs и tmpfiles rule. Он намеренно сохраняет `/etc/denstock-ai`,
`CODEX_HOME` и users. Удаление credentials/auth/users является отдельной ручной
операцией после backup decision; никогда не использовать recursive wildcard.
Перед удалением firewall rollback отдельно доказывает, что socket, proxy и все
`denstock-ai-launcher@*.service` instances не active. При неудачной остановке он
прерывается и оставляет nftables policy на месте.

## Диагностика отказов

`proxy_unavailable`:

- проверить `systemctl is-active denstock-ai-proxy.service`;
- проверить `ss -H -ltn 'sport = :2080'`;
- локально проверить redacted proxy journal;
- проверить срок MAXINIK client/subscription;
- не разрешать direct route.

`direct_network_not_blocked`:

- немедленно оставить launcher socket stopped;
- проверить table/comments/UID/port;
- повторить direct IPv4/IPv6/UDP proof;
- не запускать Codex до `ok`.

`configuration_error`:

- проверить owner/mode/path launcher config, Codex binary и state directories;
- проверить точную version `0.142.5`;
- не менять model/overrides на месте без нового audit.

`unexpected_egress`:

- остановить launcher socket;
- сверить MAXINIK client и staging hashes;
- не печатать full external IP.

Если MAXINIK subscription/client истёк, launcher остаётся fail closed. Если
OpenAI account заблокирован или device auth истёк, login/exec завершается с
ошибкой, но direct fallback не появляется.

## Обновления

Обновление sing-box:

1. Выбрать новый stable release только из официального SagerNet repository.
2. Зафиксировать exact DEB name, URL и SHA-256 release asset.
3. Проверить VLESS/Reality/mixed schema и `sing-box check` на staging.
4. Повторить all network tests, direct-block proof и rollback.
5. Обновить pin отдельным reviewed commit, не использовать `latest`.

Обновление Codex CLI:

1. Не заменять `0.142.5` без отдельного security audit.
2. Сверить argv, config keys, disabled features, event schema и proxy behavior.
3. Обновить launcher/provider contract одновременно после rebase.
4. Повторить device login, timeout, output cap, screenshot и egress tests.

## Django integration

`apps.ai_support.providers.external_launcher` создаёт canonical UUID directory,
fixed schema/image и передаёт prompt только в base64 field bounded frame. Model,
cwd, executable, environment, Codex flags и image path клиент не передаёт.
Adapter использует один absolute monotonic deadline на capabilities, login и
exec, ограничивает frame/stdout/stderr и нормализует launcher errors.

Production system check принимает только Unix socket
`/run/denstock-ai/launcher.sock`, request root
`/var/lib/denstock-ai/requests`, protocol `1`, launcher `1.0.0`, Codex `0.142.5`,
`maxinik-proxy-only`, `direct_network_blocked=true`, `proxy_health=ok` и ChatGPT
login. Direct execution остаётся только для явного `DEBUG` opt-in.
