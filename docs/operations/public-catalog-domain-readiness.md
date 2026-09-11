# PRO-STOR public catalog: final domain readiness

Design for moving from the preview hostname to `pro-stor.ru` (customers)
and `admin.pro-stor.ru` (DenisStock staff). Nothing here assumes the domain
is bought or that DNS exists; nothing here changes DNS, Caddy or any server.
Each step is a deliberate, separately approved release action.

## Target map

| Hostname | Runtime | Database role | Indexing |
| --- | --- | --- | --- |
| `pro-stor.ru` | `catalog-web` (`config.settings.public`) | `denstock_public` (catalog reads, RLS on photos, INSERT of new requests only) | on, after launch |
| `www.pro-stor.ru` | Caddy redirect to `https://pro-stor.ru{uri}` | none | follows the apex |
| `admin.pro-stor.ru` | `web` (internal DenisStock) | owner role | never (`X-Robots-Tag` at the edge) |
| preview (`catalog.<ip>.sslip.io`) | `catalog-web-preview` | `denstock_public_preview` on the preview database | never |

The public process has no admin, login, internal route, media or private
media, so the split is by process and database role, not by URL prefix.

## Prerequisites (external, owner)

1. Register `pro-stor.ru`; create DNS `A` records for `pro-stor.ru`,
   `www.pro-stor.ru` and `admin.pro-stor.ru` pointing to the VPS
   (`185.250.44.206`). Optional `AAAA` only if the VPS has working IPv6.
2. Ports 80 and 443 reachable from the internet (already true for the
   preview). Let's Encrypt HTTP-01 needs port 80.
3. Keep the old internal hostname working until staff move to
   `admin.pro-stor.ru`, then decide whether to keep it as an alias.

## Caddy (example for the release step, not applied)

```
{$CADDY_ADMIN_HOST} {
	encode gzip
	header X-Robots-Tag "noindex, nofollow"
	header -Server
	handle_path /media/* {
		root * /srv/media
		file_server
	}
	handle {
		reverse_proxy web:8000
	}
}

{$CADDY_PUBLIC_CATALOG_HOST} {
	encode gzip
	header -Server
	# No /media handler here: public photos come only from catalog-web.
	reverse_proxy catalog-web:8000
}

www.{$CADDY_PUBLIC_CATALOG_HOST} {
	redir https://{$CADDY_PUBLIC_CATALOG_HOST}{uri} permanent
}
```

* TLS is automatic per site block once DNS resolves to the VPS.
* The preview block keeps `header X-Robots-Tag "noindex, nofollow"`.
  The application also sends it while `PUBLIC_CATALOG_INDEXING` is off;
  both say the same thing, so there is no conflict.
* `/media/*` exists only on the admin host. Internal media stay reachable
  there exactly as today (unguessable UUID names, no login): a pre-existing
  property of the internal host, unchanged by this work.
* `header -Server` hides `Server: gunicorn`.
* Do not add HSTS at the edge and in Django at the same time; pick
  `PUBLIC_HSTS_SECONDS` for the public host (see below).

## catalog-web environment (`.env.public`)

| Variable | Launch value | Notes |
| --- | --- | --- |
| `DJANGO_SECRET_KEY` | new random value | never the internal key; it signs the cart cookie |
| `DJANGO_PUBLIC_ALLOWED_HOSTS` | `pro-stor.ru` | add `www.pro-stor.ru` only if the redirect is not at the edge |
| `DJANGO_ALLOWED_HOSTS` | same as above | read by `prod.py`, overridden by the public settings |
| `PUBLIC_DATABASE_URL` | `postgres://denstock_public:<secret>@db:5432/denstock` | see the cutover design |
| `DJANGO_SECURE_COOKIES` | `true` | Secure flag on `prostor_cart` and `prostor_csrf` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://pro-stor.ru` | comma-separated; parsed as a list (checked). Same-origin POSTs already pass behind Caddy; explicit is clearer |
| `PUBLIC_CATALOG_BASE_URL` | `https://pro-stor.ru` | canonical links, sitemap and JSON-LD URLs |
| `PUBLIC_CATALOG_INDEXING` | `false` until the launch switch | fails closed when absent |
| `PUBLIC_HSTS_SECONDS` | `0`, later `31536000` | raise only after a week of stable TLS |
| `CATALOG_WEB_WORKERS` / `CATALOG_WEB_THREADS` | `3` / `2` | compose interpolation; tune to VPS cores |
| `PUBLIC_DB_CONN_MAX_AGE` | `60` | persistent connections |
| `PUBLIC_REQUEST_PRIVACY_POLICY_VERSION` / `PUBLIC_REQUEST_PERSONAL_DATA_CONSENT_VERSION` | identifiers of the approved texts | see `public-catalog-legal-privacy-pack.md`; default `draft-legal-review-1` |
| `PUBLIC_REQUEST_RATE_LIMIT` / `PUBLIC_REQUEST_RATE_WINDOW_SECONDS` | `5` / `600` | new requests per client address and process |

## Internal DenisStock on `admin.pro-stor.ru`

* `DJANGO_ALLOWED_HOSTS` gains `admin.pro-stor.ru`;
  `DJANGO_CSRF_TRUSTED_ORIGINS` gains `https://admin.pro-stor.ru`.
* `CADDY_SITE_ADDRESS` becomes `admin.pro-stor.ru` (or a list with the old
  hostname during the transition).
* Leave `SESSION_COOKIE_DOMAIN` and `CSRF_COOKIE_DOMAIN` unset. Host-only
  cookies mean the staff session is never sent to `pro-stor.ru` and the cart
  cookie never reaches the admin host. Setting a `.pro-stor.ru` cookie domain
  would hand the staff session cookie to the public process: never do it.
* Cookie names already differ (`sessionid` vs `prostor_cart`), a second
  safeguard.

## Request writes on `pro-stor.ru`

* The public role creates requests and nothing else. It must exist before
  the role script runs, and the role script runs after every release's
  migrations (it re-derives the exact grants and revokes anything broader).
* Requests are business writes: during a maintenance freeze or an emergency
  state the form answers "Приём заявок временно приостановлен" (HTTP 503)
  and nothing is written. A long internal business transaction holds the
  write-generation row lock; a public request that waits longer than the
  role's 5 s statement timeout gets the temporary-failure page, not a
  partial write.
* The per-address limit trusts the right-most `X-Forwarded-For` entry,
  which Caddy sets to the address it saw. With Caddy directly on the
  internet (the plan) that is the customer. If a CDN or another proxy is
  ever put in front of Caddy, configure Caddy's `trusted_proxies` for it,
  otherwise every customer shares the CDN's address and the limit.
* Operators work in "Заявки клиентов" on `admin.pro-stor.ru`; the Telegram
  webhook, when enabled, also lives there
  (`public-catalog-messenger-runbook.md`).

## Capacity

The current VPS has 1 vCPU and 2 GB of RAM shared by production DenisStock,
its database, Caddy and the preview. The load figures in the qualification
documents come from a 10-core workstation. Before announcing the site, size
the host for the expected traffic (at least 2 vCPU recommended) or measure
on the VPS itself in a quiet window; the preview runs 2 threaded workers.

## Cutover checklist (in order, each step separately approved)

1. Owner: `pro-stor.ru` registered; DNS `A` records for `pro-stor.ru`,
   `www.pro-stor.ru`, `admin.pro-stor.ru` to `185.250.44.206`; wait until
   `dig +short pro-stor.ru` answers the VPS address from outside.
2. Legal gate passed (`public-catalog-legal-privacy-pack.md`, launch gate).
3. Release of the reviewed SHA to production per
   `public-catalog-release-runbook.md` (PRE backup, migrations, role
   creation and role script, `.env.public` as in the table above with
   `PUBLIC_CATALOG_INDEXING=false`, `catalog-web` up).
4. Caddy: the `pro-stor.ru` and `www` blocks above; the admin block with
   `admin.pro-stor.ru`. TLS certificates are issued on the first request.
5. Internal: `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS` gain
   `admin.pro-stor.ru`; cookie domains stay unset.
6. Acceptance against `https://pro-stor.ru` (read-only), then one request
   with `--exercise-cart --submit-request` agreed with the operators, who
   cancel it.
7. A week of stable TLS: `PUBLIC_HSTS_SECONDS=31536000`.
8. The indexing switch below, then Yandex Webmaster and Google Search
   Console with the sitemap.

## The launch switch (indexing)

The code is SEO-ready and noindex is purely a deployment setting:

1. In `.env.public`: `PUBLIC_CATALOG_INDEXING=true` and
   `PUBLIC_CATALOG_BASE_URL=https://pro-stor.ru`.
2. Make sure the `pro-stor.ru` Caddy block has no `X-Robots-Tag` header.
3. Recreate only `catalog-web`
   (`docker compose --profile public-catalog up -d --no-deps catalog-web`).
4. Verify: `curl -sI https://pro-stor.ru/parts/<id>/` has no `X-Robots-Tag`;
   `https://pro-stor.ru/robots.txt` lists `Disallow: /search/`,
   `Disallow: /cart/`, `Disallow: /request/` and
   `Sitemap: https://pro-stor.ru/sitemap.xml`; run
   `public_catalog_acceptance.py --base-url https://pro-stor.ru
   --expect-indexing on --article <article>`.
5. Owner: add the site and sitemap to Yandex Webmaster and Google Search
   Console (external accounts).

Rollback of the switch is the same edit with `false`. Search result pages
stay `noindex, follow` and the cart stays `noindex, nofollow` in both modes;
part pages are the only indexable content.

## Canonical URLs and the sitemap

Every part page links `rel=canonical` to
`PUBLIC_CATALOG_BASE_URL/parts/<public_id>/`, so `www`, the preview and any
alias collapse onto one URL. `/sitemap.xml` is an index of files with at
most 10,000 part URLs each (`/sitemaps/parts-<n>.xml`), built with the same
visibility rule as the pages and cached for an hour. Public IDs are
permanent: the identity survives price, stock, name and photo changes.
