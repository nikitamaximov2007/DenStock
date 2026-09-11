# PRO-STOR public catalog: Stage 15 launch acceptance

Two layers: automated checks that must pass before a person looks, and a
short manual pass for what automation cannot judge (layout on real phones,
photo quality, wording).

## 1. Automated

| Area | Evidence | Where |
| --- | --- | --- |
| Public host, TLS, headers, no internal routes or media, noindex policy, Search 2.0 article variants, detail SEO and JSON-LD, photos, sitemap, error pages, health | `scripts/qualification/public_catalog_acceptance.py --base-url <host> --article <article> --expect-indexing off` ends with `"failed": 0` | any running host, read-only |
| Cart round trip on an isolated stack | same script with `--exercise-cart --probe-post` | local or preview only |
| Route boundary under the real public middleware | `tests/test_public_runtime_boundary.py` | pytest |
| Search, filters, confirmed analog filter, paging, query budgets | `tests/test_public_catalog_search_filters.py` | pytest, SQLite and PG16 |
| Detail, SEO, JSON-LD, robots, sitemap, headers, errors, logs, health | `tests/test_public_catalog_pages.py` | pytest |
| Cart tampering, limits, no writes | `tests/test_public_catalog_cart.py` | pytest |
| Photos: publication, renditions, serving isolation | `tests/test_public_catalog_photos.py` | pytest |
| Live price and availability contracts | `tests/test_public_catalog_live_contract.py` | pytest |
| Restricted role grants, RLS, refused writes | `tests/test_public_catalog_role_postgresql.py` | pytest on PG16 |
| Public settings fail closed | `tests/test_public_settings_module.py` | pytest |
| Migrations: backfill, rollback defaults | `tests/test_public_catalog_migrations.py` | pytest, SQLite and PG16 |
| Performance on 125k | `scripts/qualification/public_catalog_launch_benchmark.py` | isolated PG16 corpus |
| Load | `scripts/qualification/public_catalog_load.py` | loopback only |
| Content coverage | `manage.py public_catalog_coverage_report` | read-only |

## 2. Manual pass (15 minutes)

Use the demo checklist (`public-catalog-demo-checklist.md`) on the target
host, then confirm:

- [ ] Phone widths 320, 375, 390 and 412 px: no horizontal scrolling on
      home, results (with filters open), part page and cart; long names
      and articles wrap.
- [ ] Tablet (768 px) and desktop (1280 px): filters sit beside results;
      the cart summary stays beside the lines.
- [ ] Keyboard only: skip link, search, filters, result links, quantity,
      add to cart, remove; focus is always visible.
- [ ] A part with a confirmed Russian name shows it as the title and the
      English name below; a part without one shows only English.
- [ ] Unknown price shows "Уточнить цену"; zero stock shows "Нет на складе"
      and "Узнать о поставке", and adding it gives a supply-inquiry line.
- [ ] A confirmed analog appears on both parts' pages with its own price and
      availability; an unconfirmed one appears nowhere.
- [ ] A published photo appears on the card and the part page; an internal
      photo that was never published does not; after "Снять с публикации"
      it disappears on reload.
- [ ] The cart total counts only in-stock lines with a known price and says
      what it leaves out.
- [ ] `https://<host>/admin/`, `/login/`, `/media/...` answer "Такой страницы
      нет", never a login form.
- [ ] Response headers on the part page: `X-Robots-Tag: noindex, nofollow`
      before the launch switch; none after it.

## 3. Launch blockers vs content work

A failed item in section 1, a leaked internal route, a writable public role,
a wrong price or availability, or an unconfirmed analog or photo in public
is a blocker. Missing Russian names, photos, analogs or application areas
are content work and do not block the launch; see
`public-catalog-launch-data-quality.md`.
