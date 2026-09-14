# Public catalog availability-first ranking

Base: `44a310e2c2d39fee0af0402463e2ff42af179c8e`.

## Contract

The public service preserves Search 2.0 relevance inside each group while
ordering results as follows:

1. exact and normalized-exact article matches;
2. non-exact cards with canonical available quantity greater than zero;
3. non-exact cards with canonical available quantity zero or below.

Exact duplicates remain in the protected article group. Availability may sort
those duplicates, but an unavailable exact article remains above every
available partial or name match. The sort is stable, so the original Search
2.0 order is the deterministic final tie-breaker.

Canonical availability comes from `available_totals`: it aggregates all lots
and available serial items and subtracts active reservations. Price provenance
does not participate in the key.

## Public route matrix

| Route | Mode | Old order | New order | Shared service | Test |
| --- | --- | --- | --- | --- | --- |
| `/search/` | text, article, EN, RU, fuzzy, multiword | Search 2.0 relevance | protected exact article, then availability, then relevance | `search_catalog` | availability ranking tests |
| `/search/` | manufacturer, application, relation, in-stock filters | relevance, then filters | ranking before filters and paging; in-stock removes zero only | `search_catalog` | public catalog search filters |
| `/search/` | pagination | relevance page | full bounded ranked list before page window | `search_catalog` | pagination boundary test |

The deployed public UI is intentionally search-first. It exposes no separate
catalog/category browse endpoint without a query, so there is no hidden browse
ordering path to change in this candidate.

## PostgreSQL 16 qualification

An isolated PostgreSQL 16 container with the standard 125,000-row Stage 2
corpus was migrated from the base and cloned before candidate measurement.
Each value is a seven-sample median in milliseconds for `search_catalog`.

| Case | Base | Candidate |
| --- | ---: | ---: |
| Exact article | 11.036 | 12.051 |
| Partial article | 13.978 | 14.902 |
| Common EN | 92.184 | 98.127 |
| Confirmed RU | 20.784 | 22.517 |
| Fuzzy typo | 208.447 | 200.532 |
| EN with in-stock and manufacturer filters | 90.161 | 97.120 |

The candidate adds no query per card. It reuses the one batched availability
read already used for filters and cards, then stably ranks at most 300 Search
2.0 identities before pagination. No database migration is required.
