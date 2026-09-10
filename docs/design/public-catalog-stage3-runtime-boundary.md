# Public Catalog Stage 3: runtime boundary

Stage 3 adds no catalog page or public DTO endpoint. It establishes the
deployment boundary that a later browse UI will use.

## Process and route boundary

`catalog-web` is a separate compose service, launched only with the
`public-catalog` profile. It runs `config.settings.public` and
`config.public_urls`; the latter exposes only `/` and `/healthz/`. Internal
paths, including admin, login, stock, sales, repairs, reports, customs and
media, are absent from the resolver and return 404 instead of an internal
login redirect. Caddy routes only `CADDY_PUBLIC_CATALOG_HOST` to this process.

The public image has no media, private-media, backup or signing-key mount, and
uses `.env.public`, never the internal `.env`. Its allowed variables are the
public Django secret, public host list and `PUBLIC_DATABASE_URL`; it must not
contain backup, manifest-signing, AI or staff-integration settings.

`docker/public-entrypoint.sh` only waits for the database and starts Gunicorn.
It never migrates, collects internal media or creates a superuser. The normal
internal release job remains the migration owner.

## Database least privilege

The runtime role is SELECT-only. Its exact required read graph is:

| Table/model | Reason | Stage 3 grant |
| --- | --- | --- |
| `catalog_parttype`, `catalog_partnumber`, `catalog_unit`, `catalog_manufacturer` | Search identities and public facts | SELECT |
| `brp_brppartlink`, `brp_brpcatalogpart`, `polaris_polarispartlink`, `polaris_polariscatalogpart` | Canonical source article selected by the established identity helper | SELECT |
| `actions_partcustomsinfo` | confirmed Russian name only | SELECT |
| `inventory_stocklot`, `inventory_partitem`, `procurement_batch`, `warehouse_storagelocation` | canonical physical availability; the existing read model joins batch and location before aggregating | SELECT |
| `sales_reservation`, `sales_reservationline` | subtract active reservations | SELECT |

`resolve_current_customer_price` reads `PartType.recommended_price` and needs
no price-table write grant. `available_totals` delegates to `live_stock_rows`,
which reads the inventory and reservation rows above. `build_public_part_facts`
combines these facades. Search 2.0 reads the catalog and confirmed customs rows.
No public operation requires INSERT, UPDATE, DELETE, sequence usage, schema
ownership or migration privileges.

The deploy owner must grant SELECT only on the listed tables and revoke all
write/schema privileges from `denstock_public`. A restricted-role acceptance
must prove the three read facades work and catalogue, price, stock,
reservation, customer, sale and repair writes are rejected by PostgreSQL.

## Deferred work

Public browse/search UI, part DTO HTTP endpoints, public identifiers, SEO,
photos, cart and customer requests remain Stage 4+ product work. The runtime
foundation deliberately exposes none of them.
