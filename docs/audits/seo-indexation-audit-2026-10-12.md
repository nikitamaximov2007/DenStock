# pro-brp.ru technical SEO audit: 126,084 non-indexed URLs

Date: 2026-10-12. Production code: `bc53d89` (= `origin/main`; `catalog-web`
SEO files verified byte-identical by sha256 inside the running container).
Production was only read: HTTP GET/HEAD at low rate, read-only Django shell
sessions inside `SET TRANSACTION READ ONLY`, and container logs. Nothing was
deployed, restarted, configured or submitted. Search Console was NOT accessed
(see Q).

## A. Executive summary

Google knows about ~126k URLs because the sitemap lists exactly 126,116 URLs,
and the public catalog has exactly 126,114 visible parts. There is no URL
explosion, no duplicate URL space, no robots or noindex block, no canonical
error and no server-error problem. Every check of the technical plumbing
passed.

Three confirmed facts explain why almost none of it is indexed:

1. **Every product page is an orphan.** No public HTML page links to any
   product page: the home page has only a search form, product pages link only
   to confirmed analogs (0 exist), and the breadcrumb links to `/search/`,
   which robots.txt disallows. Discovery is sitemap-only.
2. **99.3% of the URLs are one bulk aftermarket import with almost no unique
   content.** 125,181 of 126,114 parts were created on 2026-08-27 in category
   "Aftermarket" (All Balls, ProX, EBC, Wiseco, Vertex, ...). Of all public
   parts, 884 (0.7%) are in stock, 1 has a photo and 1 has a confirmed Russian
   name. A typical page is an English part name, an article, a brand, a price
   and "Сейчас нет на складе" inside Russian boilerplate.
3. **The property is new.** Indexing opened 2026-09-25, Search Console was
   verified and the sitemap submitted 2026-09-27, and the reported snapshot is
   dated 2026-10-09: 126k URLs submitted at once to a site about two weeks old,
   with no internal links pointing to them.

"Discovered, currently not indexed" with no last crawl is the expected Search
Console state for that combination. Which of the three weighs most cannot be
measured from here: Google does not publish its crawl-scheduling inputs.

A tested fix candidate adds a crawlable `/in-stock/` listing (linked from the
home page and every footer) and a separate `sitemaps/in-stock.xml` segment, so
the 884 parts that are really sellable now have an HTML link path and a
segment that Search Console can measure on its own.

## B. Actual product counts (production, 2026-10-12)

| Measure | Count |
| --- | --- |
| `PartType` rows | 272,812 |
| active | 272,808 |
| active but not public (`is_public=False`) | 146,694 |
| public but retired (`is_active=False`, 404 by design) | 4 |
| **public and active (the catalog's one visibility rule)** | **126,114** |
| category "Aftermarket", all created 2026-08-27 | 125,181 (99.26%) |
| category "BRP" / BRP catalog link | 790 / 789 |
| category "Добавлено вручную" (manual) | 125 |
| Polaris | 4 |
| other categories | 14 |
| with an article (catalog identity) | 126,059 (55 without) |
| priced (current customer price > 0) | 125,990 (124 unpriced) |
| **in stock now** | **884** (BRP 755, manual 62, aftermarket 62, Polaris 4, other 1) |
| in stock and priced | 881 |
| with a published public photo | 1 |
| with a confirmed Russian name | 1 |
| with an explicit application area | 100 |
| with vehicle compatibility rows | 0 |
| confirmed analog links | 0 (`PartAnalog` table is empty) |
| distinct English names | 64,806 (top: "BRAKE LINE KIT" x1,471) |
| normalized articles shared by more than one public part | 1,265 articles, 2,584 parts |

`PartType.description` is non-blank for 125,976 parts but is not rendered on
the public page, correctly: for aftermarket rows it repeats the name, for BRP
rows it is internal provenance ("Из BRP-каталога, номер ... Статус BRP: нет.").

Shared articles are most likely different manufacturers using the same part
number; they were not verified as true duplicates and must not be merged or
excluded without review.

## C. Database vs sitemap vs GSC

| Source | URLs |
| --- | --- |
| public and active parts | 126,114 |
| sitemap product URLs (13 shards) | 126,114, all unique, all `/parts/<uuid>/` |
| sitemap total incl. `/` and `/about/` | 126,116 |
| GSC 2026-10-09: not indexed + indexed | 126,084 + 5 = 126,089 |

The sitemap equals the visible catalog exactly. GSC's known set is 27 URLs
short of today's sitemap, which is consistent with report lag or parts added
after the report's data date (not measured; hypothesis). Of the product URLs
crawled in the last 12 days, 100% are sitemap URLs. No other URL source exists
in the HTML.

## D. robots.txt

```
User-agent: *
Allow: /
Disallow: /search/
Disallow: /cart/
Disallow: /request/
Sitemap: https://pro-brp.ru/sitemap.xml
```

Correct for product pages. Side effect: the only onward link on a product page
(breadcrumb to `/search/?q=<article>`) is disallowed, so it is a dead end for
crawlers. Keeping `/search/` disallowed is right (unbounded query space); the
fix is to add real links, not to open search.

## E. sitemap.xml

- Index with 13 shards, 10,000 URLs each (last 6,114), max 1.16 MB
  uncompressed. All 13 pass `xmllint`; all HTTP 200 `application/xml`,
  `Cache-Control: public, max-age=3600`.
- Absolute `https://pro-brp.ru` URLs, no `www`, no query strings, no private,
  search, cart, request or redirecting URLs. Every product URL in it answered
  200 when crawled (0 product 404s in 12 days of logs).
- `<lastmod>` is real (`updated_at`) but 125,190 URLs share `2026-08-27`, so it
  carries no prioritization signal.
- Generation cost: server time per shard p50 1.23 s, p90 2.38 s, max 8.8 s
  (119 shard requests in the log window) on a 1 vCPU / 2 GB host. LIMIT/OFFSET
  plus 10,000 `reverse()` calls. Not a cause of non-indexing; worth keyset
  pagination or a cached render later.
- Not deserving equal weight: 99% of entries are out-of-stock aftermarket
  rows. Protocol-valid, but the sitemap gives Google no way to tell the 884
  sellable pages from the rest. The candidate adds that segment (N).

## F. Googlebot access

- Product pages: 200, no `X-Robots-Tag`, no meta robots, self-canonical. No
  auth, no WAF/Cloudflare (Caddy → gunicorn directly), no rate limiting: in
  128,395 logged requests there is no 429 and no 5xx at all.
- Redirects: `www` → apex 301; `http` → `https` 308; missing slash 301;
  unknown or upper-case UUID 404; `?utm=` variant 200 with the clean canonical.
- **Crawl volume:** 2026-09-30 to 2026-10-12, `catalog-web` served 117,794
  product-page requests covering 96,827 distinct URLs (77% of the sitemap),
  spread evenly across all 13 shards (~76% each): a sitemap-driven crawler
  sampling URLs, not a sequential scraper. Server time p50 49 ms, p90 109 ms,
  p99 238 ms.
- **Who crawled is NOT VERIFIED.** The `catalog-web` access log records route,
  status and time but no User-Agent or IP, and Caddy keeps no access log. Since
  Search Console reported no last crawl for many URLs on 2026-10-09, a large
  share was probably not Googlebot (Yandex was submitted the same day), but
  that is a hypothesis. Google's own Crawl Stats report answers it (Q).
- 8,852 404s are vulnerability scanners (`/wp-admin/`, `/.git/`, `*.php`).
- A crawl budget problem is not supported by evidence: the host answered
  every request quickly and without errors.

## G. Product HTML quality (6 production pages: BRP in stock, Polaris in stock, two aftermarket out of stock, two manual)

| Element | Finding |
| --- | --- |
| title | `{article} {name}, {brand} · купить в PRO-STOR`, unique, factual |
| H1 | the display name (English for 125k parts); manual part `84f668d3...` has a barcode as its name |
| meta description | `{name}, {brand}, артикул {article}. Цена, наличие на складе и подтверждённые аналоги в каталоге PRO-STOR.` The analog claim is boilerplate: 0 analogs exist |
| visible main text | 110-135 words, of which the part-specific facts are name, article, brand, unit, price, stock line |
| images | none (placeholder) on all 6; 1 photo in the whole catalog |
| internal links | 11 per page: home, about, cart, `/search/` (disallowed), 3 social; **0 to other products** |
| breadcrumbs | Главная › Поиск, no category/brand level |
| language | Russian chrome, English product names, no Russian product text |

The pages are honest and technically clean. They are thin and near-identical
in template: 126k pages that differ mostly in a part number and an English
name.

## H. Canonical / meta robots

Correct everywhere sampled: absolute `https://pro-brp.ru/parts/<uuid>/`
canonical, no meta robots on indexable pages, `X-Robots-Tag` only on `www`
(redirect host) and the private preview host. `PUBLIC_CATALOG_INDEXING=true`
and `PUBLIC_CATALOG_BASE_URL=https://pro-brp.ru` confirmed in the running
container. No canonical defect found.

## I. Structured data

One `schema.org/Product` per page: `name`, `description`, `url`, `sku`, `mpn`,
`brand`, `offers{price, priceCurrency RUB, availability}`.

- A. Validity: valid JSON-LD, required Product fields present.
- B. Google merchant listings: need `image`, so 126,113 of 126,114 pages are
  ineligible today. `shippingDetails` and `hasMerchantReturnPolicy` are
  recommended there; Search Console reports them as warnings.
- C. Product snippets: need offers, review or rating; offers exist for priced
  parts, so they are eligible.
- D. Not in the system: shipping terms, return policy, reviews, ratings. Do
  not fabricate any of them. Availability is honest (`OutOfStock` for 99%) and
  must stay so.

## J. Internal linking

The whole crawlable HTML graph was `/`, `/about/`, `/cart/` plus the
disallowed `/search/`. No category, brand or listing page existed, and
`/search/` without a query renders an empty prompt. Product discovery depended
entirely on `sitemap.xml`. Fixed for Tier 1 by the candidate (N); a brand or
category navigation for the rest is an owner decision (L, S).

## K. Performance

- Product page server time: p50 49 ms, p90 109 ms, p99 238 ms, 0 errors.
- From this Mac TTFB was ~1.1 s, but `connect` = 2 ms and TLS = 0.83 s show a
  local proxy path; server-side time for the same requests was 32-124 ms.
- Sitemap shards are the only slow responses (E).
- Host: 1 vCPU, 2 GB RAM, ~600 MB available; gunicorn gthread 3x2 for
  `catalog-web`.
- The candidate's stock read on production data: 913 stock rows, 884 in-stock
  public parts, 7 queries, 335-397 ms (two runs, read-only). An `/in-stock/`
  page is expected around 0.4-0.5 s server time.

## L. Confirmed root causes

| # | Finding | Evidence | Impact | Confidence |
| --- | --- | --- | --- | --- |
| L1 | Product pages have no HTML inlinks; discovery is sitemap-only | code (`home.html`, `part_detail.html`), production HTML of home/about/6 products, robots.txt, 0 analog rows | high: Google's crawl scheduling favours linked URLs; sitemap-only URLs on a new site commonly stay "Discovered" | fact: certain; causal weight: high but not measurable |
| L2 | 99.3% of URLs are a bulk out-of-stock aftermarket import with near-zero unique content | DB census (B), HTML sample (G) | high: low perceived value per URL across the whole site | fact: certain; causal weight: hypothesis |
| L3 | New property, 126k URLs submitted at once | release 2026-09-25, GSC verified 2026-09-27, report 2026-10-09 | medium: time alone explains part of the backlog | fact: certain |

Status: ROOT CAUSES PARTIALLY IDENTIFIED. The facts are confirmed; their
relative weight needs Search Console Crawl Stats and per-sitemap data (Q).

## M. Possible contributing factors (not confirmed)

- English-only names on a Russian site: weak relevance for Russian queries.
- Homepage H1 says "запчасти BRP" while 99% of the catalog is aftermarket
  motorcycle/ATV parts: mixed topical signal.
- Meta description claims "подтверждённые аналоги" on every page while none
  exist.
- Identical `lastmod` on 125,190 URLs.
- Slow sitemap shards (1-9 s): unlikely to matter, Google fetches them rarely.
- Ruled out by evidence: robots block, noindex, canonical errors, URL
  duplication, redirects in the sitemap, 5xx, 429/rate limiting, stale URLs.

## N. Minimal code fixes (candidate)

1. `/in-stock/` (`public_catalog_in_stock`): indexable listing of every public
   part with stock available now, 50 per page, stable order by ID, ordinary
   result cards. One URL per page: `?page=1` → 301 `/in-stock/`, a page past
   the end or a malformed page → 404, self-canonical, `rel=prev/next`. Linked
   from the home page and the footer of every public page.
2. `/sitemaps/in-stock.xml` (`public_catalog_sitemap_in_stock`): the in-stock
   product URLs as their own segment, listed first in the sitemap index. URLs
   also stay in their part shard. `/in-stock/` is added to shard 1.
3. Cart buttons on the listing return to it (`LOCAL_NEXT_PREFIXES`).

No migration, no model change, no price/stock/visibility logic change, no URL
change, nothing noindexed or removed. Availability comes from the canonical
`live_stock_rows` read already used by the coverage report; the restricted
public PostgreSQL role reads it (proved on PostgreSQL 16).

Deliberately NOT changed (owner decisions): the meta description wording, the
homepage H1, a brand/category navigation, sitemap membership of the
aftermarket rows, logging of User-Agent.

## O. Branch / commit

Branch `claude/seo-indexation-audit` from `origin/main` `bc53d89`; commit SHA
in the final chat report.

## P. Test results

| Run | Baseline `bc53d89` | Candidate |
| --- | --- | --- |
| SQLite, 37 public-catalog/SEO/account/edge files | 1 failed / 480 passed / 94 skipped | 1 failed / 491 passed / 94 skipped |
| PostgreSQL 16.15, role + pages + SEO + browse + boundary (+ new file) | 159 passed | 170 passed |
| slow 12,000-row sitemap test | n/a | passed |

The one failure in both is the inherited
`tests/test_max_edge_route.py::test_only_the_public_catalog_block_changes`.
Zero candidate-only failures. `ruff check .` clean, `djlint templates --check`
clean, `manage.py check` clean, `makemigrations --check` no changes,
`git diff --check` clean. The full `pytest` suite was NOT run: a parallel
Disaster Recovery session was using this machine and the instruction was not
to run heavy suites alongside it. The independent audit must run it.

Contract changes in existing tests (intentional, not weakened): the sitemap
index now has one more `<sitemap>` entry and shard 1 one more `<url>`; the
public route set gained two names. The PostgreSQL role test now also fetches
`/in-stock/` and the segment under the restricted role.

## Q. Google Search Console follow-up

Search Console was not accessed: the built-in browser has no Google session,
and signing in or using the owner's Chrome was not authorized. Ask the owner
for these exports or screenshots (all read-only, no submissions):

1. Settings → Crawl stats → Open report: total crawl requests per day since
   2026-09-27, by response, by file type, by purpose (Discovery/Refresh), by
   Googlebot type, and Host status. This answers who crawled the 96,827 URLs.
2. Indexing → Pages: chart and the "Why pages aren't indexed" table, with the
   filter set to "All submitted pages", then to each sitemap file.
3. Indexing → Sitemaps: per file status, last read, discovered pages.
4. URL Inspection (inspect only, do NOT request indexing in bulk) for:
   - `/parts/c23f2cbe-df8c-4f8c-be15-ef5d789d22e3/` (BRP, in stock)
   - `/parts/b416f890-9df4-4e38-91aa-0151045054ea/` (Polaris, in stock)
   - `/parts/02d5fb0c-b016-48a5-aefc-0f786484785b/` (aftermarket, out of stock)
   - `/parts/988fd403-2756-48d5-a6bc-b66bea3a824c/` (manual, out of stock)
   - the 5 indexed URLs (which are they?)
   Record: discovery (sitemaps, referring page), last crawl, crawl allowed,
   page fetch, indexing allowed, user-declared vs Google-selected canonical.
5. Performance → Search results, last 28 days: impressions, clicks, queries,
   pages.
6. Yandex Webmaster: Индексирование → Статистика обхода and Страницы в поиске
   (excluded reasons, especially "Малоценная или маловостребованная страница").

## R. Proposed deploy plan (not executed)

Code-only, no migration. Follow the standard release sequence in
`docs/SEO_INDEXING.md` and the production access notes:

1. Independent audit of this branch, including the full `pytest` on SQLite and
   PostgreSQL against a same-day `origin/main` baseline.
2. Fetch, confirm the qualified SHA, one writer only.
3. PRE backup (signed, offsite).
4. `git checkout <sha>`, update `DENSTOCK_APP_COMMIT`, rebuild `web` and
   `catalog-web` with `--no-deps` (the public catalog is the
   `public-catalog` profile service).
5. Verify inside `catalog-web` that the new files are present.
6. Read-only checks: `/in-stock/` 200 without `X-Robots-Tag`, canonical
   `https://pro-brp.ru/in-stock/`, `?page=1` 301, last page + 1 404,
   `/sitemap.xml` lists `sitemaps/in-stock.xml` first, the segment has ~884
   URLs, home and a product page link to `/in-stock/`, `/search/` still
   `noindex, follow`, server time of `/in-stock/` in the access log.
7. POST backup, fast-forward `main` to the deployed SHA.
8. Search Console: open Sitemaps and resubmit `sitemap.xml` once (single
   action) so the new segment is read. No bulk indexing requests.

## S. SEO strategy and measurement plan

Tiers (provisional, from data available today):

- **Tier 1, 884 parts:** public, in stock (881 also priced). Real, sellable,
  mostly BRP. Now linked from `/in-stock/` and segmented in the sitemap. Next
  factual work: photos (1 of 884 today) and confirmed Russian names (1 today),
  both existing operator workflows.
- **Tier 2, ~125,100 parts:** valid, priced, out of stock, with article and
  brand. Keep public and indexable. Do not generate AI descriptions. Add value
  only through real data (photos, Russian names, compatibility, analogs).
- **Tier 3, provisional, for review only:** 55 without an article, 121
  unpriced and out of stock, manual rows named by barcode, 2,584 parts in
  shared-article groups. Review; do not exclude or merge without evidence.

Owner decisions:

1. Whether the out-of-stock aftermarket import belongs in the sitemap at all.
   Option: keep the pages live and indexable but list only Tier 1 plus
   BRP/Polaris/manual in the sitemap. Trade-off: fewer discovered URLs and a
   clearer quality signal versus slower discovery of aftermarket long-tail
   pages. Decide after 4 weeks of segment data, not before.
2. A brand navigation (`/brands/<brand>/`, paginated) for brands with real
   demand, starting with BRP. Scales without millions of links.
3. Copy: drop "подтверждённые аналоги" from the meta description where none
   exist; align the homepage H1 with what the catalog actually holds.
4. Add User-Agent to the `catalog-web` access log (or a Caddy access log) so
   crawler identity can be verified next time.

Metrics and baseline (2026-10-12 unless noted):

| Metric | Baseline |
| --- | --- |
| GSC indexed pages | 5 (2026-10-09) |
| GSC discovered, not indexed | 126,084 (2026-10-09) |
| GSC crawled, not indexed | unknown (Q) |
| Googlebot crawl requests/day | unknown (Q) |
| Impressions / clicks | 0 clicks (2026-10-09); impressions unknown |
| sitemap URLs / valid | 126,116 / 126,116 |
| in-stock segment URLs | 0 (884 after deploy) |
| product pages with an HTML inlink | 0 (884 after deploy) |
| canonical errors / product 4xx-5xx in logs | 0 / 0 |
| product server time p50/p90/p99 | 49 / 109 / 238 ms |
| pages with a photo | 1 |
| pages with a confirmed Russian name | 1 |
| pages without an article | 55 |

Checkpoints at 7, 14, 30, 60 and 90 days after deploy: record each metric,
reported separately as crawlable (inlinks, sitemap), crawled (Crawl stats,
logs), indexed (Pages, per sitemap file), impressions and clicks
(Performance), and leads (customer requests from the public catalog in
DenisStock). No growth is projected: Google decides indexing.

## T. Remaining risks

- Crawler identity is unverified; part of the 117k fetches may be scrapers.
- `/in-stock/` costs ~0.4-0.5 s per page on a 1 vCPU host; a misbehaving
  crawler hammering it would load PostgreSQL. 18 pages today; acceptable, but
  add caching if stock grows a lot.
- The in-stock segment changes as stock moves; a sold-out part drops out of
  the segment and the listing but stays in its shard and stays indexable.
- Full `pytest` not run (P).
- Search Console data is from the owner's 2026-10-09 screenshots only.
