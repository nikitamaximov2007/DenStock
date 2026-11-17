# Подготовка собственного Telegram egress через Turkey reverse SSH

Статус: Stage 1 установлен; технический Stage 2 выполнен. Ожидается ручная
приёмка двустороннего общения.

Документ описывает изолированный маршрут для `telegram-bot` через Turkey VPS.
Германия не используется. Токены, приватные ключи и клиентские данные в документ
не включаются.

## Подтверждённые исходные данные

- Production: `185.250.44.206`, проект `/opt/denstock`.
- Turkey: `45.74.158.103`.
- Turkey -> Telegram API проходит DNS, проверку TLS и HTTPS; ранее прошли 5
  последовательных и 5 параллельных запросов без bot token.
- Turkey -> Production: отдельная SSH-аутентификация ключом и reverse-forward
  проходят. Ограничения учётной записи и ключа проверены положительными и
  отрицательными тестами.
- Production -> Turkey не проходит TLS/HTTPS payload на проверенных портах.
- Production `sshd`: `AllowTcpForwarding yes`, `GatewayPorts no`, активен
  `Include /etc/ssh/sshd_config.d/*.conf`.
- Фактический Production `denstock-telegram-egress.service` запускает Xray
  `26.11.9` с Xray JSON runtime config. Он не запускает sing-box.
- Фактическая версия Turkey Xray: `26.10.21`.
- Кандидат Turkey Xray SOCKS inbound проверен установленной версией командой
  `xray run -format=json -test`; результат `Configuration OK`.
- Turkey proxy слушает только `127.0.0.1:23081`; reverse listener Production
  слушает только `127.0.0.1:23082`.
- Turkey Dante слушает публичный IP на `:1080`; в схему его не включать.
- Существующий production egress слушает `10.231.0.1:2081` на host-системе.
  Docker network `br-tg-egress` содержит Telegram bot с адресом `10.231.0.2`;
  существующая nftables политика допускает именно этот адрес. Остальные
  приложения и сети этим proxy не пользуются.
- Ранее измеренная RAM: Production `MemAvailable` около 681 MiB, Turkey около
  522 MiB. Во время последнего снимка Production egress Xray использовал около
  19 MiB cgroup memory; Turkey Xray processes суммарно около 41 MiB RSS.

## Целевая схема

```text
telegram-bot container (10.231.0.2)
  -> существующий HTTP CONNECT proxy 10.231.0.1:2081
  -> существующий Xray egress на Production
  -> SOCKS5 upstream 127.0.0.1:23082
  -> reverse SSH listener только на loopback Production
  -> Turkey Xray SOCKS5 127.0.0.1:23081
  -> прямой Turkey egress
  -> api.telegram.org:443
```

TLS остаётся сквозным между ботом и Telegram. Proxy не расшифровывает запрос,
не получает bot token и не меняет TLS-проверку. Германия, глобальные routes,
Docker-сети других приложений и Telegram client setting не меняются.

### Доступность с учётом network namespaces

Production Xray egress запущен как host systemd service, поэтому видит Production
`127.0.0.1:23082`. Обратный listener не обязан и не должен быть доступен из
контейнера. Контейнер обращается к прежнему host bridge адресу `10.231.0.1:2081`;
Xray сам соединяется с loopback listener. Это сохраняет текущую границу
Docker и не публикует новый порт.

## Проверенные конфигурационные фрагменты

Порты ниже подтверждены свободными на момент проверки. Перед установкой повторно
проверить их владельца. Если порт занят, остановиться и согласовать другой.

### Turkey: отдельный Xray SOCKS listener

Новый экземпляр Xray, не изменение текущего `xray.service`:

```json
{
  "log": {"loglevel": "warning"},
  "inbounds": [{
    "listen": "127.0.0.1",
    "port": 23081,
    "protocol": "socks",
    "settings": {"auth": "noauth", "udp": false},
    "tag": "telegram-only"
  }],
  "outbounds": [
    {"protocol": "freedom", "tag": "direct"},
    {"protocol": "blackhole", "tag": "blocked"}
  ],
  "routing": {
    "domainStrategy": "AsIs",
    "rules": [
      {
        "type": "field",
        "inboundTag": ["telegram-only"],
        "domain": ["full:api.telegram.org"],
        "port": "443",
        "outboundTag": "direct"
      },
      {
        "type": "field",
        "inboundTag": ["telegram-only"],
        "outboundTag": "blocked"
      }
    ]
  }
}
```

Listener доступен только loopback. Правило пропускает только `api.telegram.org`
на TCP 443; всё остальное блокируется. Static IP allowlist для Telegram не
используется, потому что адреса API могут меняться. Конфигурация уже принята
установленным Xray в режиме test.

Путь при внедрении: `/etc/denstock-telegram-egress/xray.json`, владелец отдельная
systemd-служебная учётная запись, режим `0600`.

Xray unit запускает `/usr/local/bin/xray run -config
/etc/denstock-telegram-egress/xray.json`; стартовать его можно только после
повторной проверки точного файла командой `xray run -test -config ...`.

Подготовленный unit:

```ini
[Unit]
Description=DenisStock Telegram-only local proxy on Turkey
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=denstock-telegram-egress
Group=denstock-telegram-egress
UMask=0077
ExecStartPre=/usr/local/bin/xray run -format=json -test -config /etc/denstock-telegram-egress/xray.json
ExecStart=/usr/local/bin/xray run -format=json -config /etc/denstock-telegram-egress/xray.json
Restart=on-failure
RestartSec=15s
KillMode=control-group
TimeoutStopSec=15s
MemoryMax=96M
TasksMax=32
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectKernelLogs=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
AmbientCapabilities=
ReadOnlyPaths=/etc/denstock-telegram-egress/xray.json

[Install]
WantedBy=multi-user.target
```

### Production egress: не менять на Stage 1

Фактическая служба запускает `/usr/local/lib/denstock-ai/bin/xray` с конфигом
`/etc/denstock-ai/telegram-egress.json`, формат runtime файла соответствует Xray
(ключи `protocol`, `streamSettings`, `routing`). В старом варианте этого runbook
ошибочно была описана конфигурация sing-box. Не применять sing-box JSON к текущей
службе.

Stage 1 не меняет этот runtime config, Xray service, слушатель `10.231.0.1:2081`,
firewall или Telegram container. Для последующего переключения понадобится
отдельно подготовить Xray SOCKS outbound на `127.0.0.1:23082`, проверить его
точным бинарником Production Xray в режиме `-test` и обновить local generator,
tests и operations docs. Это отдельный Stage 2 и требует отдельного разрешения.

### Production: ограничение SSH

Создать отдельного пользователя `denstock-egress-tunnel`, без sudo и членства в
группах приложений, с shell `/usr/sbin/nologin`. На текущем Production основной
`sshd_config` включает drop-ins до последующих глобальных директив, поэтому
`Match User` нужно безопасно добавить в конец `/etc/ssh/sshd_config`, после всех
существующих глобальных параметров. Не помещать `Match` в ранний drop-in.

```sshconfig
Match User denstock-egress-tunnel
    AuthenticationMethods publickey
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    AllowTcpForwarding remote
    AllowStreamLocalForwarding no
    PermitListen 127.0.0.1:23082
    PermitOpen none
    GatewayPorts no
    PermitTTY no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    PermitUserRC no
    MaxSessions 0
```

Ключ создаётся на Turkey для отдельной службы. На Production добавляется только
его публичная часть в
`/var/lib/denstock-egress-tunnel/.ssh/authorized_keys` с владельцем туннельной
учётной записи и режимами каталога `0700`, файла `0600`:

```text
restrict,port-forwarding,permitlisten="127.0.0.1:23082" ssh-ed25519 <public-key> denstock-turkey-egress
```

`restrict` включает ограничения ключа, `port-forwarding` разрешает forwarding,
а серверные `AllowTcpForwarding remote` и `PermitListen` допускают только один
remote TCP listener. `MaxSessions 0` запрещает shell/login/subsystem, сохраняя
forwarding согласно OpenSSH manual. `GatewayPorts no` не позволяет выставить
listener на публичный интерфейс. [OpenSSH sshd_config](https://man.openbsd.org/sshd_config.5)

Текущие общие параметры Production forwarding шире, чем нужно для туннеля.
Они не изменяются глобально; права ограничиваются завершающим `Match User` и
options ключа.
Фактические настройки нового пользователя после подготовки проверяются через
`sshd -T -C user=denstock-egress-tunnel,host=<production-host>,addr=45.74.158.103`.

Loopback исключает доступ извне и из Docker namespaces, но host-local процессы
технически смогут подключиться к `127.0.0.1:23082`. Практический доступ ограничен:
Turkey Xray допускает только `api.telegram.org:443`, а Telegram egress сохраняет
свой allowlist. Текущий egress и AI proxy используют общий системный user, поэтому
UID rule не отделит их. Если требуется изоляция именно по локальному процессу,
нужны отдельный egress user/config directory и отдельная nftables output policy;
это дополнительное изменение, не входящее в минимальную схему. До разработки и
проверки этой изоляции считать host-local процессы доверенной границей.

### Turkey: systemd-службы

Новые файлы:

- `/etc/systemd/system/denstock-telegram-egress-proxy.service`
- `/etc/systemd/system/denstock-telegram-egress-tunnel.service`
- `/etc/denstock-telegram-egress/xray.json`
- `/var/lib/denstock-telegram-egress/ssh/id_ed25519`
- `/var/lib/denstock-telegram-egress/ssh/known_hosts`

Обе службы работают от отдельного системного пользователя, не от root. Xray unit
использует `Type=simple`, `Restart=on-failure`, `RestartSec=15s`, `NoNewPrivileges`,
`ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `CapabilityBoundingSet=` и
`MemoryMax=96M`. Он запускает только отдельный Xray config и не меняет текущий
Xray unit.

Туннель unit запускает ровно один OpenSSH процесс:

```ini
[Unit]
Description=DenisStock Telegram reverse SSH tunnel to Production
Wants=network-online.target
Requires=denstock-telegram-egress-proxy.service
After=network-online.target denstock-telegram-egress-proxy.service

[Service]
Type=simple
User=denstock-telegram-egress
Group=denstock-telegram-egress
UMask=0077
ExecStart=/usr/bin/ssh -F /dev/null -NT -i /var/lib/denstock-telegram-egress/ssh/id_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/var/lib/denstock-telegram-egress/ssh/known_hosts -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ConnectTimeout=10 -o ConnectionAttempts=1 -o LogLevel=ERROR -R 127.0.0.1:23082:127.0.0.1:23081 denstock-egress-tunnel@185.250.44.206
Restart=always
RestartSec=30s
StartLimitIntervalSec=0
KillMode=control-group
TimeoutStopSec=15s
MemoryMax=48M
TasksMax=8
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
```

`ServerAliveInterval=30` и count `3` обнаруживают мёртвый канал примерно за
90 секунд; затем systemd ждёт 30 секунд между одиночными попытками. `-N` не
создаёт shell, `-T` запрещает PTY. `-F /dev/null` исключает внешние SSH options.
Не задавать `ClearAllForwardings=yes`: OpenSSH тогда очищает и явный `-R`.
`KillMode=control-group` убирает все процессы службы при остановке. Журнал
содержит только ошибки SSH уровня `ERROR`, без ключей, config и Telegram данных.

## Порядок контролируемого внедрения

Пункты 1-5 ниже выполнены в Stage 1. В Stage 2 переключение egress и техническая
проверка выполнены отдельно разрешённым изменением. Ручная приёмка реального
двустороннего диалога ещё не выполнялась.

Каждый следующий этап блокируется при неудаче. Этап 1 выполнен 2026-11-16/17;
изменений runtime egress, Telegram-бота или складских данных не было.

1. **Ограниченная учётная запись на Production.** Создать пользователя и
   authorized key; дописать per-user блок в конец `/etc/ssh/sshd_config`,
   проверить `sshd -t`, затем `sshd -T -C ...`. Держать текущую
   административную SSH-сессию открытой. Только после корректного вывода
   выполнить `systemctl reload ssh`. Критерий: параметры совпадают с ограничениями
   выше. При любой ошибке не reload и удалить только новые draft-файлы.
2. **Положительный и отрицательные SSH тесты.** С Turkey запустить один `-R`
   с точным loopback адресом/портом; Production `ss` должен показывать только
   `127.0.0.1:23082`. Проверить отдельные запросы на второй порт, wildcard bind,
   `-L` и shell: все запрещены. `MaxSessions 0` должен при этом позволять `-N -R`.
   Это live-тест нового пользователя, не текущей root-сессии.
3. **Turkey proxy.** Установить только новые Xray config/unit. До старта повторно
   проверить свободный порт и выполнить
   `xray run -format=json -test -config /etc/denstock-telegram-egress/xray.json`.
   Критерий: Xray слушает только `127.0.0.1:23081`; SOCKS запрос к
   `api.telegram.org:443` проходит, к другому домену/порту блокируется. Не трогать
   `xray.service`, Dante, HAProxy, Caddy, Reality или WireGuard. После успешной
   проверки включить и запустить только новую proxy unit.
4. **Reverse tunnel.** Запустить новый tunnel unit. Критерий: tunnel service
   active, `127.0.0.1:23082` слушает только на Production loopback, SOCKS handshake
   через этот порт успешен. После успешной проверки включить новую tunnel unit,
   чтобы она стартовала после reboot. При неудаче остановить только новую Turkey
   unit.
5. **Telegram API через туннель до переключения egress.** С Production выполнить
   без токена запрос через тестовый SOCKS путь; ожидается корректный HTTPS ответ
   Telegram (например HTTP 302 для `/`). Проверить certificate verification и
   убедиться, что посторонний domain/port блокируется на обоих концах. До успеха
   runtime egress config не менять.
6. **Стабильность и восстановление.** Проверить несколько часов умеренными
   интервалами: service active, loopback listener, один tokenless Telegram HTTPS
   запрос. Затем отдельно остановить/запустить только tunnel unit и проверить
   восстановление. Не перезагружать VPS как часть этого теста: enabled unit и
   `network-online.target` проверяются статически, полный reboot требует отдельного
   согласования, чтобы не прервать остальные VPN-сервисы.
7. **Подготовка переключения egress.** В `/opt/denstock` сначала сохранить
   root-only backup текущего `/etc/denstock-ai/telegram-egress.json`. Подготовить
   новый Xray JSON; проверить тем же бинарником
   `/usr/local/lib/denstock-ai/bin/xray run -test -config <candidate>`. Не писать
   кандидат в runtime path до отдельного разрешения на Stage 2.
8. **Переключение.** Установить прошедший check JSON атомарно с mode `0600`, затем
   перезапустить только `denstock-telegram-egress.service`. Telegram container
   сохраняет `TELEGRAM_API_PROXY_URL=http://10.231.0.1:2081` и не требует
   перезапуска. Критерий: unit active; из Telegram container GET
   `https://api.telegram.org/` через прежний proxy получает валидный TLS и
   корректный ответ. При проблеме сразу перейти к откату.
9. **Ручная Telegram приёмка.** Только с заранее согласованным служебным
   тестовым чатом/аккаунтом: одно новое входящее сообщение и один штатный ответ
   сотрудника. Подтвердить update обработан, ответ получил `message_id`, статус
   `sent`, дублей нет. Не использовать старые pending/failed/uncertain отправки и
   не обращаться к клиентам.
10. **Итоговая проверка.** Убедиться, что бот healthy, site/catalog/DB/MAX
    здоровы, default route прежний, другие Telegram clients не получили proxy,
    обе новые units active, loopback port не публичен, ресурсы стабильны. Только
    после этого решать, удалять ли старый fallback.

### Мониторинг

- Turkey: `systemctl is-active` для обоих новых units, `NRestarts`, RAM и
  отсутствие повторяющихся ошибок SSH/Xray. Достаточно интервала 60 секунд для
  пассивного наблюдения; не запускать частые HTTP loops.
- Production: egress unit active; listener только на `127.0.0.1:23082`; Xray
  listener прежний `10.231.0.1:2081`; tokenless HTTPS probe из bot container с
  TLS validation. Probe не должен содержать Telegram token и не создаёт сообщение.
- Критерий восстановления: после потери SSH канал исчезает и создаётся заново
  без ручного запуска; egress proxy вновь проходит tokenless Telegram HTTPS.

## Память и влияние

- Production egress Xray сейчас около 19 MiB cgroup memory. На Stage 1 он не
  меняется; новый SSH channel ожидаемо добавит около 5-15 MiB. Это оценка,
  фактическое значение измерить после старта.
- Turkey текущий набор Xray процессов около 41 MiB RSS на четыре процесса.
  Отдельный Xray instance ожидаемо добавит 10-25 MiB, OpenSSH tunnel до 10 MiB.
  Оценка общего дополнительного расхода Turkey: 20-40 MiB, лимит Xray 96 MiB и
  tunnel 48 MiB. Эти лимиты требуют проверки по фактическому RSS.
- Docker, PostgreSQL, web, catalog, MAX, складские данные и default route не
  меняются. Дополнительный публичный порт не открывается. Диск: несколько файлов
  конфигурации и журналы systemd; лимит журналов задаётся текущим journald.

## Откат

1. Атомарно вернуть сохранённый исходный
   `/etc/denstock-ai/telegram-egress.json` с исходными owner/mode `0600`.
2. Перезапустить только `denstock-telegram-egress.service` и проверить, что он
   active. Docker proxy endpoint у бота остаётся тем же; контейнер, база и данные
   заявок не затрагиваются.
3. Остановить и disable только
   `denstock-telegram-egress-tunnel.service` на Turkey, затем только новую
   `denstock-telegram-egress-proxy.service`.
4. Удалять SSH account, key или config только отдельным согласованным cleanup;
   сначала убедиться, что tunnel не используется.

Старая MAXINIK/VPN подписка истекла. Поэтому возврат старого JSON восстановит
прежнюю конфигурацию, но не гарантирует связь с Telegram. До acceptance новый
туннель остаётся отдельным; после отказа возможна временная недоступность
Telegram, но складские записи и очередь не удаляются и не сбрасываются.

## Результат Stage 1

**STAGE 1 PASSED - READY FOR TELEGRAM SWITCH**. Выполнено только отдельное
туннельное подключение; Stage 2 не запускался.

- Production: добавлена непривилегированная учётная запись
  `denstock-egress-tunnel`, отдельный `authorized_keys` и завершающий блок
  `Match User` в `/etc/ssh/sshd_config`. Снимок для отката сохранён в
  `/root/denstock-stage1-turkey-reverse-20261116-pre`.
- Turkey: добавлены отдельная учётная запись и файлы
  `/etc/denstock-telegram-egress/xray.json`,
  `/etc/systemd/system/denstock-telegram-egress-proxy.service`,
  `/etc/systemd/system/denstock-telegram-egress-tunnel.service`, а также
  приватный ключ и pinned `known_hosts` под `/var/lib/denstock-telegram-egress/ssh/`.
  Ключ и конфиги не копируются в репозиторий или этот документ.
- SSH ограничения проверены через `sshd -t` и `sshd -T -C`: только public-key
  auth, только remote TCP forwarding, только `127.0.0.1:23082`, без shell,
  PTY, agent/X11 forwarding, tunnel device, user RC и stream-local forwarding.
  Production SSH был безопасно reloaded; существующая admin-сессия сохранилась,
  независимый новый admin login прошёл.
- Неверный remote listener `127.0.0.1:23083` отклонён, попытка shell отклонена.
  Production `ss` показывает только `127.0.0.1:23082`; публичного listener нет.
- На Turkey Xray принимает только `api.telegram.org:443`; тесты
  `example.com:443` и `api.telegram.org:80` заблокированы. SOCKS listener
  доступен только на `127.0.0.1:23081`.
- С Production выполнен tokenless запрос к `https://api.telegram.org/` через
  `127.0.0.1:23082`: HTTP `302`, TLS verification result `0` (успешная проверка
  сертификата). TLS-трафик к Telegram наблюдался на Turkey `eth0`; source
  `2a01:f900:159:3f::1` совпадает с назначенным Turkey IPv6-адресом. Поэтому
  маршрут фактически выходит через Turkey.
- Контролируемо завершён только процесс новой tunnel unit. Listener пропал,
  systemd автоматически восстановил SSH-сессию примерно через 31 секунду;
  listener появился снова, `NRestarts=1`, после восстановления Telegram HTTPS
  снова вернул HTTP 302.
- Текущая память новых Turkey unit: proxy около 5.4 MiB и SSH около 1.6 MiB,
  суммарно около 7 MiB. Production `MemAvailable` около 773 MiB; Turkey около
  527 MiB. Production egress Xray остался active, PID не изменился; его
  измеренная память около 19 MiB.
- Проверка сервисов: Production web, catalog, DB и MAX healthy; старый
  `denstock-telegram-bot-1` остаётся unhealthy по ранее подтверждённой причине
  истёкшего старого VPN и не перезапускался. На Turkey текущие Xray, HAProxy,
  Caddy, Dante, Squid и WireGuard активны; их конфигурации не менялись.
- Default routes обоих VPS не менялись. Docker, PostgreSQL, web, catalog, MAX,
  Telegram egress config и сам Telegram-бот не изменялись. Bot token не
  использовался; сообщений не отправляли.

### Откат только Stage 1

Если потребуется отменить Stage 1, выполнять по отдельному согласованию:

1. На Turkey остановить и disable только
   `denstock-telegram-egress-tunnel.service`, затем только
   `denstock-telegram-egress-proxy.service`; удалить только их unit/config и
   отдельную служебную учётную запись/ключи после проверки, что они не нужны.
2. На Production удалить только завершающий `Match User
   denstock-egress-tunnel` блок и отдельный tunnel user's `authorized_keys`;
   удалить отдельную учётную запись. До reload проверить `sshd -t`, держать
   административную сессию открытой, затем выполнить только `systemctl reload
   ssh` и проверить новый admin login.
3. Сверить итоговые sshd effective settings с сохранённым снимком. Не менять
   `/etc/denstock-ai/telegram-egress.json`, egress unit, контейнеры, маршруты,
   VPN, БД или данные склада. Снимок отката читать только если требуется
   восстановить именно исходное состояние SSH.

## Результат Stage 2

**Технический транспорт восстановлен; статус для владельца:
READY FOR TELEGRAM MANUAL ACCEPTANCE.** Реальное входящее сообщение клиента и
ответ сотрудника вручную ещё не проверялись, поэтому полный бизнес-путь не
объявляется завершённым.

- До переключения сохранена root-only копия runtime config, nft policy, unit,
  активной nft table и service properties в
  `/root/denstock-telegram-egress-stage2-pre-20261117` (каталог `0700`, файлы
  `0600`). Суммы копий совпали с оригиналами; старый Xray JSON и nft policy
  прошли `xray run -test`, `nft --check`, unit прошёл `systemd-analyze verify`.
- Production Xray `26.11.9` принял новую конфигурацию с SOCKS outbound на
  `127.0.0.1:23082`; inbound `10.231.0.1:2081`, адрес Docker и nft policy
  сохранены. Маршрут Xray: `telegram-bot` → `turkey-reverse` → Turkey proxy →
  `api.telegram.org:443`. Остальные назначения блокируются.
- Исходный генератор в `/opt/denstock` на SHA
  `0394171cebccd02625e946ba08c9bf6ba4810a5d` всё ещё содержит старый sing-box
  renderer. Active systemd unit вызывает установленный Xray напрямую и не
  запускает generator при start/restart; Telegram timers, которые бы его
  вызывали, не найдены. Исправленные renderer, unit template и тесты находятся
  в этом локальном коммите. Не запускать старый renderer из `/opt/denstock`;
  обновлённый источник нужно включить в будущий релиз проекта.
- До изменения очереди: `TelegramMessage` — 2 `uncertain`, 11 `received`, 19
  `sent`; `TelegramDelivery` — 19 `sent`; событий Telegram `pending` не было.
  MAX-to-Telegram имел 2 due `pending` уведомления сотрудникам, ещё не
  отправлявшихся (`attempts=0`), созданных менее суток назад; интерфейсные
  Telegram staff notifications имели 3 таких же due first-attempt записи.
  Клиентских ожидающих отправок не было.
- После восстановления эти пять служебных уведомлений были отправлены штатным
  worker один раз: 2/2 и 3/3 получили внешний message ID, `attempts=1`; теперь
  очереди пусты. Две `uncertain` Telegram-записи остались `uncertain` и не
  повторялись. Число сообщений в `TelegramMessage` не изменилось; реальных
  клиентских тестовых сообщений не отправляли.
- Через фактический proxy из `denstock-telegram-bot-1` tokenless HTTPS к
  `api.telegram.org` вернул HTTP 302 без перехода по редиректу, с обычной
  проверкой TLS. Redirected `core.telegram.org`, `example.com:443` и
  `api.telegram.org:80` не разрешаются маршрутом.
- Production Xray логировал запросы от `10.231.0.2` через `turkey-reverse`;
  Turkey Xray подтвердил `api.telegram.org:443` через direct outbound.
- `denstock-telegram-egress.service` active, `NRestarts=0`, Xray RSS около
  30 MiB. `denstock-telegram-bot-1` восстановился самостоятельно до healthy,
  `RestartCount=0`, heartbeat свежий. При контролируемом обрыве только нового
  reverse-туннеля возникли временные сетевые ошибки polling; systemd поднял
  туннель примерно за 25 секунд, после чего tokenless HTTPS снова прошёл.
  Bot остался healthy и не перезапускался.
- После проверки: Production `MemAvailable` около 751 MiB, swap 414 MiB из
  511 MiB занят, свободно 4.0 GiB на диске; Turkey `MemAvailable` около
  525 MiB, новые proxy/tunnel services используют примерно 7.3 MiB суммарно.
  Web, catalog, DB и MAX healthy, default route не изменился. Production Git SHA
  остался прежним, его существующий untracked `docker-compose.signing.yml` не
  менялся.

### Откат Stage 2

Если новый маршрут откажет, восстановить только старый egress JSON из root-only
копии и перезапустить только `denstock-telegram-egress.service`:

```sh
install -o denstock-ai-proxy -g denstock-ai-proxy -m 0600 \
  /root/denstock-telegram-egress-stage2-pre-20261117/telegram-egress.json \
  /etc/denstock-ai/.telegram-egress-rollback.json
mv -f /etc/denstock-ai/.telegram-egress-rollback.json \
  /etc/denstock-ai/telegram-egress.json
systemctl restart denstock-telegram-egress.service
systemctl is-active denstock-telegram-egress.service
```

Это вернёт прежний истёкший VLESS маршрут и, вероятно, снова лишит Telegram
связи. Контейнер бота не перезапускать; его очередь/offset и данные не менять.
Turkey reverse tunnel оставить включённым до отдельного согласованного cleanup.

## Условия готовности к контролируемому внедрению

- Stage 1 и технический Stage 2 подтверждены выше. Следующее действие - ручная
  приёмка владельцем: одно новое входящее сообщение и один штатный ответ
  сотрудника в соответствующем диалоге; подтвердить Telegram `message_id`,
  `sent` и отсутствие дубля. Это не должно затрагивать исторические или
  `uncertain` сообщения.
