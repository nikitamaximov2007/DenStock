# PRO-STOR: SEO / search engine indexing

Operational reference for the public catalog's indexability. This is a
narrower, SEO-specific companion to
[`docs/operations/public-catalog-domain-readiness.md`](operations/public-catalog-domain-readiness.md),
which owns the full cutover runbook (DNS, Caddy blocks, `.env.public`,
release checklist). Read that document for the deployment steps; this one
explains what is indexable, why, and how to verify it.

## Domain name note

`docs/operations/public-catalog-domain-readiness.md` and the test suite use
`pro-stor.ru` as the canonical example domain. The committed production Caddy
config (`deploy/caddy/Caddyfile.production`) instead serves the real site at
`pro-brp.ru`. Everything below is domain-agnostic (the code never hardcodes
either name - see "Canonical domain" below), but **the owner should confirm
which domain is actually live** and make sure `PUBLIC_CATALOG_BASE_URL`,
`DJANGO_PUBLIC_ALLOWED_HOSTS` and the Caddy blocks all agree before flipping
the indexing switch. This report and the domain-readiness doc were not
reconciled as part of this task - do not assume `pro-stor.ru` without
checking the deployed Caddy config first.

## Canonical domain

Nothing in the codebase hardcodes a production hostname. The canonical origin
for every canonical link, sitemap URL and JSON-LD `url` is
`apps.catalog.public_seo.base_url()`:

- `PUBLIC_CATALOG_BASE_URL` (env var), if set - always used in production.
- Otherwise `f"{request.scheme}://{request.get_host()}"` - a same-origin
  fallback only meant for local/staging runs where the base URL was not
  configured.

Set `PUBLIC_CATALOG_BASE_URL=https://<the real domain>` in production. This
is what makes `www`, the preview host, and any alias collapse onto one
canonical URL instead of creating duplicate indexable copies.

## Indexable route policy

| Route | Indexable | Mechanism |
| --- | --- | --- |
| `/` (home) | yes, once indexing is on | default (no per-template override) |
| `/parts/<public_id>/` (product page) | yes, once indexing is on | default; canonical, title, description, JSON-LD |
| `/search/` (any `?q=`/filter combination) | no | `noindex, follow` - crawlers may still follow links to product pages |
| `/cart/` | no | `noindex, nofollow` |
| `/request/`, `/request/submit/`, `/request/success/<id>/` | no | `noindex, nofollow` |
| `/robots.txt`, `/sitemap.xml`, `/sitemaps/parts-<n>.xml`, `/healthz/` | n/a | not HTML pages; not linked for crawling as content |
| Internal DenisStock (admin, `/sales/`, `/repairs/`, reports, Telegram/MAX webhooks, etc.) | never | served by a completely separate process/URLConf (`config.urls`, `web`/`admin.<domain>`) - not reachable from the public host at all, not merely hidden by robots |

Whether ANY of the above indexes at all is gated by one deployment switch,
`PUBLIC_CATALOG_INDEXING` (default `false`): when off, every page - including
product pages - carries `X-Robots-Tag: noindex, nofollow` and a matching
`<meta name="robots">`, and `robots.txt` is `Disallow: /` for everything. This
makes a forgotten env var on a new host fail closed, never open.

Non-public or inactive `PartType`s (`is_public=False` or `is_active=False`)
404 exactly like a nonexistent article - they never render a public product
page, indexable or not (`apps.catalog.public_catalog.public_parts()` is the
single visibility rule, reused by the detail view, search and the sitemap).

## Product page metadata

Built once, in `apps/catalog/public_seo.py`, and reused by every surface
(HTML head, JSON-LD, Open Graph) - there is no second title/description
generator:

- **Title** (`part_title`): `{article} {name}, {manufacturer} · купить в
  PRO-STOR`. Deterministic and unique per part because it is built from the
  part's own facts, not a template with filled-in blanks.
- **H1**: the product's display name, exactly once per page
  (`<h1 class="part__title">`), with the article shown in the same visible
  block (not only in metadata).
- **Meta description** (`part_description`): name, manufacturer, "артикул
  <article>", and a fixed closing sentence about price/stock/analogs at
  PRO-STOR. No price is stated in the description text itself (price lives in
  the JSON-LD `offers` and the visible page).
- **Canonical**: `<link rel="canonical" href="{base_url}/parts/<public_id>/">`
  - always absolute, always the configured host, never the request's own host
  when `PUBLIC_CATALOG_BASE_URL` is set (so a request that arrives via an
  unexpected `Host` header cannot self-canonicalize).
- **Price**: the exact same current-customer-price authority as the rest of
  DenisStock (`apps.inventory.pricing`) - metadata never computes or shows a
  different price than the page. No positive price → "Уточнить цену", never
  `0 ₽`.

## Structured data (JSON-LD)

One `schema.org/Product` block per product page
(`apps.catalog.public_seo.product_json_ld`), truthful by construction:

- `name`, `description`, `url` always present.
- `sku`/`mpn` only when an article is known; `brand` only when a manufacturer
  is known. No invented brand/dealer claims.
- `image` only with real `PublicPartPhoto` renditions of *that* part - never
  another part's photo, never a placeholder.
- `offers` only when the current price is known (`price`, `priceCurrency:
  "RUB"`, `availability: InStock/OutOfStock` from real stock). No price known
  → no `offers` block at all, never a fabricated `0`.
- No `aggregateRating`, `review`, or any other unsupported claim.
- Oil parts: nothing in the schema states a quantity or unit, so there is
  nothing that could misrepresent litres as a piece count. The package price
  stays the only price shown (see "Oil" below).
- The script content is escaped against `</script>` and `<`/`>`/`&`
  injection from part names (`json_for_script`).

## robots.txt

`GET /robots.txt` is computed (`apps.catalog.public_seo.robots_txt`), never a
static file:

```
User-agent: *
Allow: /
Disallow: /search/
Disallow: /cart/
Disallow: /request/
Sitemap: https://<base_url>/sitemap.xml
```

or, whenever `PUBLIC_CATALOG_INDEXING` is off:

```
User-agent: *
Disallow: /
```

Robots.txt is a courtesy for well-behaved crawlers, not a security boundary:
internal DenisStock routes are unreachable from the public host at the
process/URLConf level (see the route table above), not merely disallowed.

## Sitemap architecture

- `/sitemap.xml` is a **sitemap index** (`<sitemapindex>`), listing one
  `<sitemap>` entry per shard file.
- Each shard, `/sitemaps/parts-<n>.xml`, holds at most `SITEMAP_PAGE_SIZE`
  (10,000) product URLs - safely under the 50,000-URL sitemap protocol limit.
  Shard 1 also carries the catalog root URL.
- Only `public_parts()` (`is_public=True, is_active=True`) is ever included -
  the identical rule the product page and search use.
- Each `<url>` carries `<lastmod>` from `PartType.updated_at` (`auto_now`) -
  a real signal, not `now()` on every request. It is not scoped to
  public-visible fields only (any save bumps it), which is an accepted,
  documented trade-off: "omitting is better than fabricating", and this is
  not fabricated. The catalog root entry omits `<lastmod>` - there is no
  single trustworthy "changed" timestamp for it.
- Both files are cached for one hour (`Cache-Control: max-age=3600`).

**Performance**: sitemap generation never materializes the catalog in Python.
Each shard is one bounded `.values_list("public_id", "updated_at")[start:
start+10000]` query ordered by `pk` - a `LIMIT`/`OFFSET` slice, not a full
table scan or `.iterator()` over everything. A composite index,
`parttype_public_active_idx` on `(is_public, is_active, id)`, backs the one
filter+order every one of these queries shares. Proven query-count-flat by
test regardless of catalog size in `tests/test_public_catalog_seo_gaps.py`
(50 vs. 2,000 rows: identical query count; a 12,000-row
`@pytest.mark.slow` run stays at ≤6 queries total for both the index and a
shard).

## Redirects / canonical host

- **HTTP → HTTPS**: handled entirely by Caddy's automatic HTTPS at the edge;
  Django has no `SECURE_SSL_REDIRECT` (it trusts `X-Forwarded-Proto` from
  Caddy instead - `SECURE_PROXY_SSL_HEADER` in `config/settings/prod.py`).
- **www → apex**: a permanent Caddy redirect
  (`www.<domain> { redir https://<domain>{uri} permanent }`), one hop, no
  chain.
- The application itself never redirects between hosts or schemes - that is
  entirely the edge's job, so there is exactly one redirect (www→apex) in the
  whole path, never a chain.

## Oil compatibility

Oil `PartType`s (`is_oil=True`) keep their existing public semantics
unchanged by this task:

- Availability is shown in **litres** on the page (`facts.available_quantity`
  with an "л" label), not as a piece count.
- Price stays the **package** price (`facts.price`), never a derived
  per-litre price.
- JSON-LD carries no quantity/unit field at all (see "Structured data"
  above), so there is no schema field that could state litres as pieces in
  the first place.

## Analog compatibility

Trusted (`verification_state=VERIFIED`) analog relations already show on a
product page as ordinary related-product cards, each linking to that
analog's own canonical `/parts/<public_id>/` page
(`apps.catalog.public_catalog.confirmed_links`/`part_relations`). No separate
page is generated for the relation itself, and no description, photo, or
manufacturer is ever copied between an original and its analog - each
product page's metadata comes only from that `PartType`'s own facts.

## What is intentionally noindex (and why)

| Surface | Why |
| --- | --- |
| `/search/*` | Unbounded query/filter combinations would otherwise create enormous thin/duplicate URL space; `follow` keeps the catalog crawlable through links to product pages without indexing the search results themselves. |
| `/cart/` | Per-session state, not shared content. |
| `/request/*` | Contains a form and a specific customer's submission; nothing here is content to rank, and nothing customer-identifying belongs in a crawlable page. |
| Everything, while `PUBLIC_CATALOG_INDEXING=false` | Fail-closed default - a preview or misconfigured host never gets indexed by accident. |

## Search engine verification (Google Search Console / Yandex Webmaster)

Two optional environment variables render a verification `<meta>` tag on
every public page when set (empty by default - no token is ever committed):

- `PUBLIC_CATALOG_GOOGLE_SITE_VERIFICATION` → `<meta
  name="google-site-verification" content="...">`
- `PUBLIC_CATALOG_YANDEX_VERIFICATION` → `<meta name="yandex-verification"
  content="...">`

### Owner steps, Google Search Console

1. Add the property (Domain or URL-prefix) for the production domain.
2. Choose the HTML tag verification method; Search Console gives a token.
3. Set `PUBLIC_CATALOG_GOOGLE_SITE_VERIFICATION=<token>` in `.env.public` and
   restart `catalog-web`.
4. Confirm verification in the Search Console UI.
5. Submit the sitemap index URL (`https://<domain>/sitemap.xml`).
6. Use URL Inspection on one representative product URL (see below); request
   indexing for it once it reports "URL is on Google" as eligible.
7. Monitor the Pages / indexing report over the following days - do not
   expect immediate ranking.

### Owner steps, Yandex Webmaster

1. Add the site.
2. Choose meta-tag verification; Yandex gives a token.
3. Set `PUBLIC_CATALOG_YANDEX_VERIFICATION=<token>` in `.env.public` and
   restart `catalog-web`.
4. Confirm verification in Yandex Webmaster.
5. Submit `https://<domain>/sitemap.xml` under Indexing → Sitemap files.
6. Use "Проверить страницу" on one representative product URL; request
   crawling/indexing for it.
7. Monitor Indexing → Pages in search and the crawl-error report.

(A static verification HTML file, e.g. `yandex_<token>.html`, is the other
option Yandex supports if the meta tag is ever inconvenient - not
implemented here since the meta tag alone covers both engines with one
mechanism; ask if the static-file route is specifically needed.)

## Representative URL inspection procedure

Use the existing acceptance script rather than manual `curl`, since it
already checks headers/robots/sitemap/JSON-LD together:

```
python scripts/qualification/public_catalog_acceptance.py \
  --base-url https://<domain> --article <a real public article> \
  --expect-indexing on
```

It checks, among other things: `X-Robots-Tag` absent on the home page,
`robots.txt` allows `/parts/` and lists the sitemap, the sitemap index and
first shard both respond, and a representative product page has canonical,
title, and JSON-LD.

## How to test SEO locally / staging

- `pytest tests/test_public_catalog_pages.py tests/test_public_catalog_seo_gaps.py`
  - the full metadata/JSON-LD/robots/sitemap/noindex contract, including the
    two additions from this task (JSON-LD description, sitemap `lastmod`).
- `pytest tests/test_public_catalog_seo_gaps.py -m slow` - the 12,000-row
  sitemap performance proof (excluded from the default run; run explicitly
  when touching sitemap/query code).
- Against a running `catalog-web` (local or preview), the acceptance script
  above with `--expect-indexing off` first (the safe default) and `on` only
  against a host that should actually be indexable.

## How to diagnose missing product indexing

In order - each step rules out one layer:

1. **Is `PUBLIC_CATALOG_INDEXING=true` on the actual running process?**
   `curl -sI https://<domain>/` - no `X-Robots-Tag` header means yes.
2. **Is the edge (Caddy) still forcing noindex regardless?** This is a real,
   separate gate from step 1 - `deploy/caddy/Caddyfile.production` sends
   `header X-Robots-Tag "noindex, nofollow"` unconditionally for the
   preview and admin hosts, and the *production* catalog host's block must
   have that header **removed**, not merely have the Django flag turned on.
   `curl -sI https://<domain>/parts/<id>/` - if `X-Robots-Tag` is present
   here even though step 1 passed, this is the cause.
3. **Is the part actually public?** `is_public=True` and `is_active=True` in
   DenisStock; otherwise the URL 404s (by design) and was never a candidate.
4. **Is the canonical host correct?** `PUBLIC_CATALOG_BASE_URL` must match
   the domain actually being crawled - a mismatch here creates a canonical
   pointing at the wrong host, which search engines treat as "index the
   other URL instead".
5. **Was the sitemap actually submitted, and does it 200?**
   `curl -sI https://<domain>/sitemap.xml` and inspect a shard directly.
6. **Search Console / Yandex Webmaster crawl errors** - check the property's
   own coverage/indexing report for a specific rejection reason (soft 404,
   duplicate without user-selected canonical, blocked by robots.txt, etc.).
7. Indexing takes time even once everything above is correct - a missing
   page a few hours after submission is not yet a bug.

## Not implemented in this task (deferred, see owner decisions)

- **Search result "verified analog" secondary listing** for an exact-article
  query (§14 of the task spec) - the existing search path only labels
  same-article duplicates, it does not fan out to related parts. Left alone
  deliberately; see the final RC report's owner-decisions section.
- **IndexNow** - investigated, not implemented. The catalog's own sitemap +
  standard crawl discovery is enough for an RC; IndexNow would need a
  dedicated key file/endpoint and a "which URLs actually changed" signal
  that does not exist yet outside of `updated_at`. Worth reconsidering later
  if Bing/Yandex freshness turns out to matter, but it is explicitly
  optional per the task and was not built to avoid scope creep.
