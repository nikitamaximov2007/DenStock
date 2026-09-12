# Public Catalog Search 2.0

`apps.catalog.search` is a read-only, ranked identity search service for the
future public catalog. It does not return stock, prices, locations, supplier
data, or ORM objects. Its result is a bounded sequence of `PartSearchHit`
values (`part_id`, rank, score, and match type). A consumer that is allowed to
show public data hydrates those identities separately through the Stage 1
public contract.

## Matching and ranking

The engine uses ordered, limited tiers. A part can occur only once, in its
highest matching tier. This is the ranking contract, with `part_id` as the
stable tie-breaker inside a tier:

1. exact article as typed;
2. exact article after canonical `normalize_number` normalization;
3. article prefix;
4. article substring;
5. exact English name or confirmed Russian customs name;
6. name prefix;
7. name substring;
8. every word of a multiword query, in any order;
9. PostgreSQL word-similarity name match.

The normalized article path reuses `catalog.normalize_number`; there is no
second article normalization scheme. Only OEM and ARTICLE numbers are public
identifiers. Auxiliary, analog, internal, barcode, and supplier numbers are
not candidates.

Russian matching reads only `PartCustomsInfo.customs_name_ru` where
`customs_name_ru_confirmed` is true. A generated, unconfirmed, or blank name
is never treated as public search content. Search does not filter current
stock, so a zero-stock part remains findable.

### Case folding for Russian

Russian names are matched on `PartCustomsInfo.search_name_ru`, a folded copy of
the confirmed name, never through the database's own `UPPER()`. `UPPER` follows
the cluster locale: a database created with locale `C` leaves Cyrillic
untouched, which silently made every Russian tier case-sensitive and made the
Russian fuzzy tier match nothing at all. Measured on PostgreSQL 16:

    locale en_US.utf8  upper('прокладка') = 'ПРОКЛАДКА'
    locale C           upper('прокладка') = 'прокладка'

The one folding rule is `apps.core.search_text.fold_search_text`: Unicode case
folding in Python, `ё` folded to `е`, whitespace collapsed. It is applied to
the stored name (on save, and by migration `actions.0015_customs_search_name_ru`
for existing rows) and to the query, so both sides are comparable with plain
`=`, `LIKE` and trigrams on any backend. The internal operator lookup
(`apps.core.part_lookup`) uses the same column and the same rule, so DenisStock
and PRO-STOR answer a Russian query identically.

English names keep the database's `UPPER()`: they are ASCII, where every locale
folds correctly, and their existing expression index stays in use.

### Multiword queries

Word order in a Russian name is free, and a customer is not required to guess
how the operator wrote it. Tier 8 therefore matches parts whose name contains
EVERY word of the query in any order, so `масляный фильтр` finds
`Фильтр масляный`. It ranks below the substring tier, where the words are
adjacent and the match is stronger, and above typo tolerance, because it is an
exact word match rather than a guess. The tier runs only for two to five words
of at least three characters each: dropping a short word silently would answer
a different question than the one asked.

## Bounds and portability

Input whitespace is collapsed and input is limited to 64 characters. Exact
article equality stays available for short input. Prefix search starts at three
characters, while substring and fuzzy search start at four characters. Results
are capped at 300 identities and service pagination is capped at 50 rows.

SQLite supports all non-fuzzy tiers for portable tests. PostgreSQL 16 adds
`pg_trgm` indexes and typo tolerance using `word_similarity`, for example
`bearng` to `bearing` and `проклатка` to `прокладка`. Fuzzy SQL is never run
for an input without letters.

## PostgreSQL settings and indexes

The fuzzy tier sets `pg_trgm.word_similarity_threshold` locally to its search
threshold. In autocommit this setting ends with the search transaction. If the
caller has an outer transaction, the previous local value is restored before
the nested block ends, so the setting cannot leak to later work on the same
connection.

Reading that previous value relies on tier order. `pg_trgm` registers the
setting only when its library is loaded into the backend, so on a brand-new
connection `current_setting('pg_trgm.word_similarity_threshold')` raises
"unrecognized configuration parameter". The public entry point never hits
this: the name-partial tier has the same four-character floor as the fuzzy
tier, always runs first, and loads `pg_trgm` while planning against its GIN
index. `_fuzzy_rows` is therefore not safe as a standalone entry point inside
an outer transaction. Keep a trigram-planned tier ahead of it, or read the
setting with `missing_ok`, when changing the tiers.
`test_fresh_backend_fuzzy_search_in_autocommit_and_inside_atomic` pins this.

Fuzzy cost grows with the number of names that genuinely resemble the typo.
A GIN trigram index yields candidates but no order, so every candidate is
scored, grouped and sorted before the cap applies. On the 125k qualification
corpus a typo with 1 candidate took about 6 ms, with 12,500 about 41 ms and
with 112,494 about 130 ms (English). Bounding that further, for example with
a candidate cap or an ordered GiST index, would change result semantics and is
a product decision rather than a tuning detail.

Migration `catalog.0007_search_trigram` installs `pg_trgm` and creates GIN
trigram indexes for normalized articles and the uppercase English name.
Migration `actions.0015_customs_search_name_ru` moves the Russian GIN index
from `UPPER(customs_name_ru)` to the folded `search_name_ru` column, keeping the
partial predicate `customs_name_ru_confirmed`. Therefore unconfirmed generated
translations are not even in the fuzzy candidate index.

Typo tolerance for Cyrillic additionally needs a cluster whose CTYPE treats
Cyrillic as letters. `pg_trgm` splits a string into trigrams by letters and
digits only, so in a `C` database `show_trgm('проба')` is empty and the Russian
fuzzy tier finds nothing - silently, without an error. Every other Russian tier
works in any locale. `manage.py ops_check` reports this as a warning
(`Опечатки в русском поиске`) instead of leaving it invisible. PostgreSQL qualification uses ordinary
planner settings and `EXPLAIN (ANALYZE, BUFFERS)`; a separate restricted-scan
test remains only as an expression/index compatibility check.
