# PRO-STOR public catalog: what data completeness means for launch

The public catalog shows only facts a person or a canonical process
confirmed. Where a fact is missing, the page says so calmly and still lets
the customer act. That design is what makes it possible to launch before
the content is complete, and to improve content afterwards without code.

## Blocking technical issues (must be zero before launch)

* A wrong price: anything other than the current `PartType.recommended_price`,
  or a price shown where the canonical price is unknown.
* A wrong availability: anything other than `available_totals` (physical
  stock minus active, unexpired reservations).
* An unconfirmed Russian name, an unconfirmed analog relation or an
  unpublished photo visible in public.
* An internal route, internal media, internal ID, warehouse cell, cost,
  supplier or customer data reachable from the public host.
* The public database role able to write, or able to read non-catalog
  tables.
* A failing item of the automated acceptance
  (`public-catalog-acceptance-checklist.md`).

## Content coverage (improves after launch, never blocks it)

Measured read-only with `manage.py public_catalog_coverage_report` on the
preview database (a copy of production data, migrated to the launch
candidate) on 2026-09-12; 125,987 cards:

| Measure | Count | Share | What the customer sees when missing |
| --- | ---: | ---: | --- |
| Public parts | 125,985 | | 2 retired cards are hidden |
| Confirmed Russian name | 0 | 0.0% | the English catalog name as the title |
| Published photo | 0 | 0.0% | a quiet placeholder "Фото появится после проверки" |
| Part with a confirmed analog relation | 0 | 0.0% | "Подтверждённых аналогов для этой детали пока нет" |
| Explicit application area | 12 | 0.01% | the "Техника" filter appears only on results that have the data |
| In stock now | 825 | 0.7% | "Нет на складе" and "Узнать о поставке" (a supply inquiry) |
| Known price | 125,902 | 99.9% | "Уточнить цену" for the other 83 |
| Article | 125,970 | 100.0% | 15 cards are findable by name only |
| Manufacturer | 125,964 | 100.0% | 21 cards show no manufacturer |

Candidates already waiting inside DenisStock: 4 internal photos on 1 part
(never published automatically), 1 unconfirmed analog link, and no
unconfirmed Russian names. Of the 825 in-stock parts, 748 are BRP.

### Classification

* Technical blockers: none. Every gap above is handled by the pages and
  proved by the acceptance run on the preview.
* Content gaps: Russian names, photos, analogs, application areas.
* External requirements: legal texts, DNS and TLS for `pro-stor.ru`,
  messenger credentials (see the legal pack, the domain readiness and the
  messenger runbook).

### Content priorities

Before launch (small, high value, a few hours of operator time):

1. Russian names confirmed for the in-stock parts customers ask about
   most, starting with the 748 in-stock BRP parts (internal part card,
   customs data, "Русское название подтверждено"). Search finds confirmed
   Russian names immediately.
2. At least one published photo with a recorded source and one confirmed
   analog, so the owner can show both on the live site.
3. A look at the 83 cards without a price that have stock: either a price
   or a deliberate "Уточнить цену".

After launch (continuous, no deadline):

4. Russian names for the rest of the in-stock range, then for parts that
   receive supply inquiries.
5. Analog confirmations where operators already rely on the links.
6. Photos for the best sellers, BRP first.
7. Application areas where operators know them; the filter follows.

## Re-measure

```
docker compose exec -T web python manage.py public_catalog_coverage_report
docker compose exec -T web python manage.py public_catalog_coverage_report --json
```

Both run in a read-only transaction and take a few seconds on the full
catalog. Run them on production only in an approved window; the numbers
above came from the preview database. On the preview itself (no internal
runtime there), run the command in a one-off owner container, as in the
preview runbook step 6, with `public_catalog_coverage_report --json`.
