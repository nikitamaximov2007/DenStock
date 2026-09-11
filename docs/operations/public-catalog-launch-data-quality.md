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

Measured read-only with `manage.py public_catalog_coverage_report` on a
local copy of the production catalog from 2026-09-06, migrated to the launch
candidate (125,981 cards):

| Measure | Count | Share | What the customer sees when missing |
| --- | ---: | ---: | --- |
| Public parts | 125,979 | | 2 retired cards are hidden |
| Confirmed Russian name | 0 | 0.0% | the English catalog name as the title |
| Published photo | 0 | 0.0% | a quiet placeholder "Фото появится после проверки" |
| Part with a confirmed analog relation | 0 | 0.0% | "Подтверждённых аналогов для этой детали пока нет" |
| In stock now | 826 | 0.7% | "Нет на складе" and "Узнать о поставке" |
| Known price | 125,896 | 99.9% | "Уточнить цену" for the other 83 |
| Article | 125,965 | 100.0% | 14 cards are findable by name only |
| Manufacturer | 125,961 | 100.0% | 18 cards show no manufacturer |
| Explicit application area | 0 | 0.0% | the "Техника" filter stays hidden until there is data |

Most of the catalog is the imported aftermarket range without stock: those
parts are the "Узнать о поставке" offer, which is intended. The table is a
work plan, not a gate. Recommended order, by customer value:

1. **Russian names for parts that sell.** Confirm the Russian name on the
   826 in-stock parts first (internal part card, customs data: "Русское
   название подтверждено"). Search finds confirmed Russian names at once.
2. **Analogs for in-stock originals.** Confirm the analog links that
   operators already use ("Подтвердить" in the part card). Each one adds a
   label, a filter value and a cross-link in public.
3. **Photos for the top sellers.** Publish photos with a recorded source;
   start with the BRP parts that have stock.
4. **Application area.** Set it where operators already know it; the filter
   appears automatically when results contain the data.

## Re-measure

```
docker compose exec -T web python manage.py public_catalog_coverage_report
docker compose exec -T web python manage.py public_catalog_coverage_report --json
```

Both run in a read-only transaction and take a few seconds on the full
catalog. Run them on production only in an approved window; the numbers
above came from an isolated local copy.
