-- Lot provenance: read-only production check (PostgreSQL 16).
--
-- Run inside a read-only transaction, for example from /opt/denstock:
--   ( echo 'BEGIN TRANSACTION READ ONLY;'; cat lot-provenance-readonly.sql;
--     echo 'ROLLBACK;' ) | docker compose exec -T db psql -U <user> -d <db> \
--     -v ON_ERROR_STOP=1 -P pager=off
-- Every statement is a SELECT. No customer data: ids, statuses, quantities, times.
--
-- "Receipt evidence" = a RECEIVE_LOT that the old backfill_opening_movements did
-- NOT write (that command wrote comment 'Открывающий остаток' and no document).
-- The full classification (classes, UNKNOWN, closed lines, capacity changes) is
-- `python manage.py audit_lot_provenance` from the candidate code; these queries
-- reproduce its evidence rules and must agree with it
-- (tests/test_lot_provenance_sql_postgresql.py).

-- name: lot_inventory
-- Lots by status and by the evidence they carry.
WITH m AS (
    SELECT stock_lot_id,
           count(*) AS movements,
           count(*) FILTER (
               WHERE movement_type = 'receive_lot'
                 AND NOT (comment = 'Открывающий остаток' AND document_type = '')
           ) AS receipts,
           count(*) FILTER (
               WHERE movement_type = 'receive_lot'
                 AND comment = 'Открывающий остаток' AND document_type = ''
           ) AS old_backfill
    FROM inventory_stockmovement
    WHERE stock_lot_id IS NOT NULL
    GROUP BY stock_lot_id
)
SELECT l.status,
       coalesce(m.receipts, 0) > 0 AS has_receipt,
       coalesce(m.old_backfill, 0) > 0 AS has_old_backfill_receipt,
       coalesce(m.movements, 0) = 0 AS no_movement,
       count(*) AS lots
FROM inventory_stocklot l
LEFT JOIN m ON m.stock_lot_id = l.id
GROUP BY 1, 2, 3, 4
ORDER BY 1, 2, 3, 4;

-- name: supplier_receipt_evidence
-- Positive supplier evidence, as distinct from raw RECEIVE_LOT rows counted
-- in lot_inventory and receipts_over_line. Reject retyped or appended rows.
WITH first_whole_move AS (
    SELECT DISTINCT ON (stock_lot_id) stock_lot_id, from_location_id
    FROM inventory_stockmovement
    WHERE movement_type = 'move_lot' AND document_type = '' AND stock_lot_id IS NOT NULL
    ORDER BY stock_lot_id, created_at, id
), first_real_movement AS (
    SELECT DISTINCT ON (stock_lot_id) stock_lot_id, id
    FROM inventory_stockmovement
    WHERE stock_lot_id IS NOT NULL
      AND NOT (movement_type = 'receive_lot'
               AND comment = 'Открывающий остаток' AND document_type = '')
    ORDER BY stock_lot_id, created_at, id
)
SELECT l.id AS lot_id, m.id AS movement_id, m.batch_line_id AS receipt_line_id,
       l.batch_line_id AS current_line_id, m.quantity
FROM inventory_stocklot l
JOIN inventory_stockmovement m ON m.stock_lot_id = l.id
JOIN procurement_batchline receipt_line ON receipt_line.id = m.batch_line_id
JOIN first_real_movement first_m ON first_m.stock_lot_id = l.id AND first_m.id = m.id
LEFT JOIN first_whole_move first_move ON first_move.stock_lot_id = l.id
WHERE m.movement_type = 'receive_lot'
  AND NOT (m.comment = 'Открывающий остаток' AND m.document_type = '')
  AND m.document_type = '' AND m.document_id IS NULL
  AND m.batch_id = l.batch_id AND m.part_type_id = l.part_type_id
  AND receipt_line.batch_id = m.batch_id AND receipt_line.part_type_id = m.part_type_id
  AND m.to_location_id = coalesce(first_move.from_location_id, l.location_id)
  AND m.quantity = l.initial_quantity AND l.initial_quantity > 0
  AND nullif(to_jsonb(l)->>'origin_transfer_id', '') IS NULL
  AND nullif(to_jsonb(l)->>'origin_return_line_id', '') IS NULL
  AND (nullif(to_jsonb(l)->>'creation_origin', '') IS NULL
       OR to_jsonb(l)->>'creation_origin' = 'supplier_received')
  AND l.note NOT LIKE 'Перемещение #%'
  AND NOT EXISTS (
      SELECT 1 FROM inventory_stockmovement another
      WHERE another.stock_lot_id = l.id AND another.id <> m.id
        AND another.movement_type = 'receive_lot'
        AND NOT (another.comment = 'Открывающий остаток' AND another.document_type = '')
  )
  AND NOT EXISTS (
      SELECT 1 FROM inventory_stockmovement transfer_m
      WHERE transfer_m.batch_id = l.batch_id
        AND transfer_m.batch_line_id = l.batch_line_id
        AND transfer_m.part_type_id = l.part_type_id
        AND transfer_m.to_location_id = coalesce(first_move.from_location_id, l.location_id)
        AND transfer_m.quantity = l.initial_quantity
        AND transfer_m.movement_type = 'move_lot'
        AND transfer_m.document_type = 'stock_transfer'
        AND transfer_m.created_at < l.created_at
  )
  AND (
      to_jsonb(l)->>'creation_origin' = 'supplier_received'
      OR NOT EXISTS (
          SELECT 1 FROM returns_stockreturnline return_line
          JOIN returns_stockreturn ret ON ret.id = return_line.stock_return_id
          WHERE return_line.returned_lot_id = l.id AND ret.completed_at IS NOT NULL
            AND abs(extract(epoch FROM (ret.completed_at - l.created_at))) <= 1
      )
  )
ORDER BY l.id;

-- name: transfer_evidence
-- Lots without receipt evidence that a stock transfer opened. The matching
-- MOVE_LOT is journaled on its source lot, so do not require its current
-- BatchLine FK to equal the target's current BatchLine. Prove part identity on
-- the source lot, movement, transfer, target lot and both BatchLines, and prove
-- the complete transfer movement group agrees with its document.
WITH first_whole_move AS (
    SELECT DISTINCT ON (stock_lot_id) stock_lot_id, from_location_id
    FROM inventory_stockmovement
    WHERE movement_type = 'move_lot' AND document_type = '' AND stock_lot_id IS NOT NULL
    ORDER BY stock_lot_id, created_at, id
),
transfer_groups AS (
    SELECT t.id AS transfer_id,
           sum(m.quantity) AS moved_quantity,
           bool_and(coalesce((
               m.movement_type = 'move_lot'
               AND m.document_type = 'stock_transfer'
               AND m.part_type_id = t.part_type_id
               AND m.from_location_id = t.from_location_id
               AND m.to_location_id = t.to_location_id
               AND s.part_type_id = t.part_type_id
               AND m.batch_id = s.batch_id
               AND m.batch_line_id = s.batch_line_id
               AND origin_line.part_type_id = t.part_type_id
               AND origin_line.batch_id = m.batch_id
               AND NOT EXISTS (
                   SELECT 1 FROM inventory_stockmovement source_history
                   WHERE source_history.stock_lot_id = s.id
                     AND source_history.batch_line_id IS NOT NULL
                     AND source_history.batch_line_id <> s.batch_line_id
               )
           ), false)) AS rows_consistent
    FROM inventory_stocktransfer t
    JOIN inventory_stockmovement m
      ON m.document_id = t.id AND m.document_type = 'stock_transfer'
    LEFT JOIN inventory_stocklot s ON s.id = m.stock_lot_id
    LEFT JOIN procurement_batchline origin_line ON origin_line.id = m.batch_line_id
    GROUP BY t.id
),
candidates AS (
    SELECT l.id, l.batch_id, l.batch_line_id, l.status, l.part_type_id, l.initial_quantity,
           l.created_at, l.note,
           nullif(to_jsonb(l)->>'origin_return_line_id', '')::bigint AS origin_return_line_id,
           coalesce(f.from_location_id, l.location_id) AS original_location_id,
           nullif(to_jsonb(l)->>'origin_transfer_id', '')::bigint AS origin_transfer_id,
           nullif(to_jsonb(l)->>'creation_origin', '') AS creation_origin
    FROM inventory_stocklot l
    LEFT JOIN first_whole_move f ON f.stock_lot_id = l.id
    WHERE l.status <> 'receiving'
      AND NOT EXISTS (
          SELECT 1 FROM inventory_stockmovement r
          WHERE r.stock_lot_id = l.id AND r.movement_type = 'receive_lot'
            AND NOT (r.comment = 'Открывающий остаток' AND r.document_type = '')
      )
)
SELECT DISTINCT ON (c.id)
       c.id AS lot_id, c.batch_line_id, c.status, c.initial_quantity,
       m.id AS movement_id, m.stock_lot_id AS source_lot_id, t.id AS transfer_id
FROM candidates c
JOIN inventory_stockmovement m
  ON m.stock_lot_id <> c.id
 AND m.movement_type = 'move_lot' AND m.document_type = 'stock_transfer'
 AND m.to_location_id = c.original_location_id
 AND m.quantity = c.initial_quantity
 AND (
      c.origin_transfer_id = m.document_id
      OR (
          m.created_at >= c.created_at
          AND m.created_at - c.created_at <= interval '1 second'
      )
 )
JOIN inventory_stocktransfer t
  ON t.id = m.document_id
JOIN inventory_stocklot source_lot ON source_lot.id = m.stock_lot_id
JOIN procurement_batchline origin_line ON origin_line.id = m.batch_line_id
JOIN procurement_batchline current_line ON current_line.id = c.batch_line_id
JOIN transfer_groups g ON g.transfer_id = t.id
WHERE t.part_item_id IS NULL
  AND t.stock_state IN ('available', 'quarantine')
  AND t.part_type_id = c.part_type_id
  AND m.part_type_id = c.part_type_id
  AND source_lot.part_type_id = c.part_type_id
  AND source_lot.batch_line_id = m.batch_line_id
  AND m.batch_id = c.batch_id
  AND c.batch_line_id = m.batch_line_id
  AND current_line.part_type_id = c.part_type_id
  AND origin_line.part_type_id = c.part_type_id
  AND m.batch_id = source_lot.batch_id
  AND origin_line.batch_id = m.batch_id
  AND m.from_location_id = t.from_location_id
  AND t.to_location_id = c.original_location_id
  AND (c.origin_transfer_id = t.id OR t.created_at <= c.created_at)
  AND (c.creation_origin IS NULL OR c.creation_origin = 'transfer')
  AND (c.creation_origin IS DISTINCT FROM 'transfer' OR c.origin_transfer_id IS NOT NULL)
  AND (c.origin_transfer_id IS NULL OR c.creation_origin = 'transfer')
  AND c.origin_return_line_id IS NULL
  AND (c.origin_transfer_id IS NULL OR c.note !~ '^Перемещение #[0-9]+ из '
       OR substring(c.note from '^Перемещение #([0-9]+) из ')::bigint = c.origin_transfer_id)
  AND (c.origin_transfer_id IS NOT NULL OR c.note NOT LIKE 'Перемещение #%'
       OR (c.note ~ '^Перемещение #[0-9]+ из '
           AND substring(c.note from '^Перемещение #([0-9]+) из ')::bigint = t.id))
  AND g.moved_quantity = t.quantity
  AND g.rows_consistent
ORDER BY c.id, m.id;

-- name: return_origin_evidence
-- A later return into an existing lot is stock flow, not lot origin. New lots
-- carry an explicit immutable origin line. For historical NULL-origin lots,
-- the return draft line's created_at is deliberately irrelevant: inference
-- requires the exact returned_lot relation, completed return document, exact
-- typed first movement, and movement time matching lot creation.
WITH first_lot_movement AS (
    SELECT DISTINCT ON (stock_lot_id) stock_lot_id, id, created_at
    FROM inventory_stockmovement
    WHERE stock_lot_id IS NOT NULL
    ORDER BY stock_lot_id, created_at, id
), first_whole_move AS (
    SELECT DISTINCT ON (stock_lot_id) stock_lot_id, from_location_id
    FROM inventory_stockmovement
    WHERE movement_type = 'move_lot' AND document_type = '' AND stock_lot_id IS NOT NULL
    ORDER BY stock_lot_id, created_at, id
), candidates AS (
    SELECT l.id AS lot_id, l.batch_id, l.batch_line_id, l.part_type_id,
           coalesce(f.from_location_id, l.location_id) AS original_location_id,
           l.initial_quantity, l.created_at,
           nullif(to_jsonb(l)->>'origin_transfer_id', '')::bigint AS origin_transfer_id,
           nullif(to_jsonb(l)->>'origin_return_line_id', '')::bigint AS origin_return_line_id,
           nullif(to_jsonb(l)->>'creation_origin', '') AS creation_origin
    FROM inventory_stocklot l
    LEFT JOIN first_whole_move f ON f.stock_lot_id = l.id
    WHERE NOT EXISTS (
        SELECT 1 FROM inventory_stockmovement receipt
        WHERE receipt.stock_lot_id = l.id
          AND receipt.movement_type = 'receive_lot'
          AND NOT (receipt.comment = 'Открывающий остаток' AND receipt.document_type = '')
    )
), matching_return_lines AS (
    SELECT c.lot_id, rl.id AS return_line_id
    FROM candidates c
    JOIN returns_stockreturnline rl
  ON rl.returned_lot_id = c.lot_id
 AND (c.origin_return_line_id IS NULL OR c.origin_return_line_id = rl.id)
JOIN returns_stockreturn r ON r.id = rl.stock_return_id
 AND r.status IN ('completed', 'canceled') AND r.completed_at IS NOT NULL
JOIN inventory_stockmovement m
  ON m.stock_lot_id = c.lot_id
 AND m.movement_type = 'return_lot'
 AND m.document_type = 'stock_return'
 AND m.document_id = r.id
 AND m.batch_id = c.batch_id
 AND m.batch_line_id = c.batch_line_id
 AND m.part_type_id = c.part_type_id
 AND m.to_location_id = rl.to_location_id
 AND m.quantity = rl.quantity
 AND m.quantity = c.initial_quantity
JOIN first_lot_movement first_m ON first_m.stock_lot_id = c.lot_id
WHERE rl.batch_id = c.batch_id
  AND rl.batch_line_id = c.batch_line_id
  AND rl.part_type_id = c.part_type_id
  AND rl.to_location_id = c.original_location_id
  AND rl.quantity = c.initial_quantity
  AND (c.creation_origin IS NULL OR c.creation_origin = 'return')
  AND (c.creation_origin IS DISTINCT FROM 'return' OR c.origin_return_line_id IS NOT NULL)
  AND (c.origin_return_line_id IS NULL OR c.creation_origin = 'return')
  AND c.origin_transfer_id IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM returns_stockreturnline extra_line
      WHERE extra_line.stock_return_id = r.id
        AND extra_line.returned_lot_id = c.lot_id AND extra_line.id <> rl.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM inventory_stockmovement extra_movement
      WHERE extra_movement.stock_lot_id = c.lot_id
        AND extra_movement.document_type = 'stock_return'
        AND extra_movement.document_id = r.id AND extra_movement.id <> m.id
  )
  AND NOT EXISTS (
      SELECT 1 FROM inventory_stockmovement doc_movement
      WHERE doc_movement.document_type = 'stock_return'
        AND doc_movement.document_id = r.id
        AND 1 <> (
            SELECT count(*) FROM returns_stockreturnline doc_line
            WHERE doc_line.stock_return_id = r.id
              AND (
                  (doc_movement.movement_type = 'return_lot'
                   AND doc_line.returned_lot_id = doc_movement.stock_lot_id
                   AND doc_movement.stock_lot_id IS NOT NULL
                   AND doc_line.part_item_id IS NULL)
                  OR
                  (doc_movement.movement_type = 'return_item'
                   AND doc_line.part_item_id = doc_movement.part_item_id
                   AND doc_movement.part_item_id IS NOT NULL)
              )
              AND doc_line.batch_id = doc_movement.batch_id
              AND doc_line.batch_line_id = doc_movement.batch_line_id
              AND doc_line.part_type_id = doc_movement.part_type_id
              AND doc_line.to_location_id = doc_movement.to_location_id
              AND doc_line.quantity = doc_movement.quantity
        )
  )
  AND NOT EXISTS (
      SELECT 1 FROM returns_stockreturnline doc_line
      WHERE doc_line.stock_return_id = r.id
        AND 1 <> (
            SELECT count(*) FROM inventory_stockmovement doc_movement
            WHERE doc_movement.document_type = 'stock_return'
              AND doc_movement.document_id = r.id
              AND (
                  (doc_movement.movement_type = 'return_lot'
                   AND doc_line.returned_lot_id = doc_movement.stock_lot_id
                   AND doc_movement.stock_lot_id IS NOT NULL
                   AND doc_line.part_item_id IS NULL)
                  OR
                  (doc_movement.movement_type = 'return_item'
                   AND doc_line.part_item_id = doc_movement.part_item_id
                   AND doc_movement.part_item_id IS NOT NULL)
              )
              AND doc_line.batch_id = doc_movement.batch_id
              AND doc_line.batch_line_id = doc_movement.batch_line_id
              AND doc_line.part_type_id = doc_movement.part_type_id
              AND doc_line.to_location_id = doc_movement.to_location_id
              AND doc_line.quantity = doc_movement.quantity
        )
  )
  AND (
      (c.origin_return_line_id = rl.id AND first_m.id = m.id)
      OR (
          first_m.id = m.id
          AND abs(extract(epoch FROM (m.created_at - c.created_at))) <= 1
          AND abs(extract(epoch FROM (r.completed_at - c.created_at))) <= 1
      )
  )
GROUP BY c.lot_id, rl.id
HAVING count(DISTINCT m.id) = 1
)
SELECT lot_id, min(return_line_id) AS return_line_id
FROM matching_return_lines
GROUP BY lot_id
HAVING count(*) = 1
ORDER BY lot_id;

-- name: reassigned_receipts
-- Receipts whose lot now sits on another batch line (admin re-assignment before
-- 2c64484). The receipt counts for the line it names, not the lot's current one.
WITH first_real_movement AS (
    SELECT DISTINCT ON (stock_lot_id) stock_lot_id, id
    FROM inventory_stockmovement
    WHERE stock_lot_id IS NOT NULL
      AND NOT (movement_type = 'receive_lot'
               AND comment = 'Открывающий остаток' AND document_type = '')
    ORDER BY stock_lot_id, created_at, id
), first_whole_move AS (
    SELECT DISTINCT ON (stock_lot_id) stock_lot_id, from_location_id
    FROM inventory_stockmovement
    WHERE movement_type = 'move_lot' AND document_type = '' AND stock_lot_id IS NOT NULL
    ORDER BY stock_lot_id, created_at, id
)
SELECT m.id AS movement_id, m.stock_lot_id AS lot_id, m.batch_line_id AS receipt_line_id,
       l.batch_line_id AS current_line_id, m.quantity
FROM inventory_stockmovement m
JOIN inventory_stocklot l ON l.id = m.stock_lot_id
JOIN procurement_batchline receipt_line ON receipt_line.id = m.batch_line_id
JOIN first_real_movement first_m ON first_m.stock_lot_id = l.id AND first_m.id = m.id
LEFT JOIN first_whole_move first_move ON first_move.stock_lot_id = l.id
WHERE m.movement_type = 'receive_lot'
  AND NOT (m.comment = 'Открывающий остаток' AND m.document_type = '')
  AND m.document_type = '' AND m.document_id IS NULL
  AND m.batch_id = l.batch_id AND m.part_type_id = l.part_type_id
  AND receipt_line.batch_id = m.batch_id AND receipt_line.part_type_id = m.part_type_id
  AND m.to_location_id = coalesce(first_move.from_location_id, l.location_id)
  AND m.quantity = l.initial_quantity AND l.initial_quantity > 0
  AND nullif(to_jsonb(l)->>'origin_transfer_id', '') IS NULL
  AND nullif(to_jsonb(l)->>'origin_return_line_id', '') IS NULL
  AND (nullif(to_jsonb(l)->>'creation_origin', '') IS NULL
       OR to_jsonb(l)->>'creation_origin' = 'supplier_received')
  AND l.note NOT LIKE 'Перемещение #%'
  AND NOT EXISTS (
      SELECT 1 FROM inventory_stockmovement another
      WHERE another.stock_lot_id = l.id AND another.id <> m.id
        AND another.movement_type = 'receive_lot'
        AND NOT (another.comment = 'Открывающий остаток' AND another.document_type = '')
  )
  AND NOT EXISTS (
      SELECT 1 FROM inventory_stockmovement transfer_m
      WHERE transfer_m.batch_id = l.batch_id
        AND transfer_m.batch_line_id = l.batch_line_id
        AND transfer_m.part_type_id = l.part_type_id
        AND transfer_m.to_location_id = coalesce(first_move.from_location_id, l.location_id)
        AND transfer_m.quantity = l.initial_quantity
        AND transfer_m.movement_type = 'move_lot'
        AND transfer_m.document_type = 'stock_transfer'
        AND transfer_m.created_at < l.created_at
  )
  AND (
      to_jsonb(l)->>'creation_origin' = 'supplier_received'
      OR NOT EXISTS (
          SELECT 1 FROM returns_stockreturnline return_line
          JOIN returns_stockreturn ret ON ret.id = return_line.stock_return_id
          WHERE return_line.returned_lot_id = l.id AND ret.completed_at IS NOT NULL
            AND abs(extract(epoch FROM (ret.completed_at - l.created_at))) <= 1
      )
  )
  AND m.batch_line_id IS DISTINCT FROM l.batch_line_id
ORDER BY m.id;

-- name: old_backfill_receipts
-- RECEIVE_LOT written by the old backfill. Not receipt evidence.
SELECT m.id AS movement_id, m.stock_lot_id AS lot_id, m.batch_line_id, l.status,
       m.quantity, m.created_at,
       EXISTS (
           SELECT 1 FROM inventory_stockmovement r
           WHERE r.stock_lot_id = m.stock_lot_id AND r.movement_type = 'receive_lot'
             AND NOT (r.comment = 'Открывающий остаток' AND r.document_type = '')
       ) AS lot_also_has_real_receipt
FROM inventory_stockmovement m
JOIN inventory_stocklot l ON l.id = m.stock_lot_id
WHERE m.movement_type = 'receive_lot'
  AND m.comment = 'Открывающий остаток' AND m.document_type = ''
ORDER BY m.id;

-- name: receipts_over_line
-- Lines whose receipt movements alone already exceed the line quantity.
SELECT b.id AS batch_line_id, b.quantity, sum(m.quantity) AS received_by_receipts
FROM procurement_batchline b
JOIN inventory_stockmovement m ON m.batch_line_id = b.id
WHERE m.movement_type = 'receive_lot'
  AND NOT (m.comment = 'Открывающий остаток' AND m.document_type = '')
GROUP BY b.id, b.quantity
HAVING sum(m.quantity) > b.quantity
ORDER BY b.id;
