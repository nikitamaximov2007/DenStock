# Claude recovery matrix, 2026-09-13

Основание: Git refs, ancestry, рабочие копии и production read-only preflight.
Этот документ не заменяет release qualification.

| Commit / branch | Фактическое назначение | Статус для release |
| --- | --- | --- |
| `7d708e4` `claude/prostor-launch-integration` | Сводный production source: public catalog, CustomerRequest pipeline, публичная runtime boundary, rate limit/idempotency, customer phone, price provenance gate и RU search | Уже deployed source и base интеграции |
| `891e6fd` `codex/public-catalog-request-stack-integration` | Сборка cart -> CustomerRequest, privacy, Telegram и MAX boundaries | Предок `7d708e4`, повторно не переносится |
| `468e3f6` `claude/public-catalog-launch-readiness` | Final launch readiness fixes и evidence | Предок `7d708e4`, повторно не переносится |
| `cf493a2` `claude/public-catalog-launch-candidate` | Финальный candidate request stack | Предок `7d708e4`, повторно не переносится |
| `74e2e15` `codex/public-catalog-launch-candidate-remediation` | Исправление startup grants public runtime | Предок `7d708e4`, повторно не переносится |
| `55db932` `claude/public-catalog-launch-final-remediation` | Документация preview review и content plan | Не требуется для runtime release |
| `832046e` `claude/prostor-phase-b-data-readiness` | Документация Phase B data readiness | Справочный материал, не нужен в code integration |
| `0e2e38b` `claude/prostor-phase-b-data-readiness` | RU approval tooling | Не переносится: в этом release нет массового RU approval |

## Production preflight

На момент проверки production был на `7d708e4`, рабочее дерево содержало одну
локальную модификацию, активных процессов deploy/migrate не было. Работали
`db`, `web` и `proxy`; отдельный `catalog-web-preview` обслуживал catalog
hostname. Роль `denstock_public` и `.env.public` отсутствовали, CustomerRequest
в production было 0. Цена `421000667` была 45 000 ₽ с provenance `unverified`.

Публичный hostname уже возвращал `X-Robots-Tag: noindex, nofollow`; `/admin/`
на нём возвращал 404. Это preview routing, не production-connected public
runtime. Caddy routing остаётся production-only конфигурацией и не должен
копироваться поверх интеграционной ветки.
