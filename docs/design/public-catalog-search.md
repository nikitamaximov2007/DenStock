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
8. PostgreSQL word-similarity name match.

The normalized article path reuses `catalog.normalize_number`; there is no
second article normalization scheme. Only OEM and ARTICLE numbers are public
identifiers. Auxiliary, analog, internal, barcode, and supplier numbers are
not candidates.

Russian matching reads only `PartCustomsInfo.customs_name_ru` where
`customs_name_ru_confirmed` is true. A generated, unconfirmed, or blank name
is never treated as public search content. Search does not filter current
stock, so a zero-stock part remains findable.

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

Migration `catalog.0007_search_trigram` installs `pg_trgm` and creates GIN
trigram indexes for normalized articles and the uppercase English name.
Migration `actions.0013_customs_name_ru_trigram` adds the corresponding
uppercase Russian-name index. PostgreSQL qualification tests use `EXPLAIN`
with sequential and ordinary index scans disabled, leaving bitmap scans to
prove that the predicate-serving trigram index occurs in the actual plan.
