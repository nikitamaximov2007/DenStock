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
           bool_and(
               m.movement_type = 'move_lot'
               AND m.document_type = 'stock_transfer'
               AND m.part_type_id = t.part_type_id
               AND m.from_location_id = t.from_location_id
               AND m.to_location_id = t.to_location_id
               AND s.part_type_id = t.part_type_id
               AND m.batch_id = s.batch_id
               AND origin_line.part_type_id = t.part_type_id
               AND origin_line.batch_id = m.batch_id
           ) AS rows_consistent
    FROM inventory_stocktransfer t
    JOIN inventory_stockmovement m
      ON m.document_id = t.id AND m.document_type = 'stock_transfer'
     AND m.movement_type = 'move_lot'
    JOIN inventory_stocklot s ON s.id = m.stock_lot_id
    JOIN procurement_batchline origin_line ON origin_line.id = m.batch_line_id
    GROUP BY t.id
),
candidates AS (
    SELECT l.id, l.batch_id, l.batch_line_id, l.status, l.part_type_id, l.initial_quantity,
           l.created_at, coalesce(f.from_location_id, l.location_id) AS original_location_id
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
 AND m.created_at >= c.created_at AND m.created_at - c.created_at <= interval '1 second'
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
  AND m.batch_id = c.batch_id
  AND current_line.part_type_id = c.part_type_id
  AND origin_line.part_type_id = c.part_type_id
  AND m.batch_id = source_lot.batch_id
  AND origin_line.batch_id = m.batch_id
  AND m.from_location_id = t.from_location_id
  AND t.to_location_id = c.original_location_id
  AND t.created_at <= c.created_at
  AND g.moved_quantity = t.quantity
  AND g.rows_consistent
ORDER BY c.id, m.id;

-- name: reassigned_receipts
-- Receipts whose lot now sits on another batch line (admin re-assignment before
-- 2c64484). The receipt counts for the line it names, not the lot's current one.
SELECT m.id AS movement_id, m.stock_lot_id AS lot_id, m.batch_line_id AS receipt_line_id,
       l.batch_line_id AS current_line_id, m.quantity
FROM inventory_stockmovement m
JOIN inventory_stocklot l ON l.id = m.stock_lot_id
WHERE m.movement_type = 'receive_lot'
  AND NOT (m.comment = 'Открывающий остаток' AND m.document_type = '')
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
