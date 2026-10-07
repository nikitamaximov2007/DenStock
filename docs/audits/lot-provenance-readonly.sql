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
-- final_provenance below classifies every lot. The preceding queries expose
-- supporting evidence for investigation; the final result is the parity gate
-- against `python manage.py audit_lot_provenance`.

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
  AND (to_jsonb(l)->>'creation_origin' IS NULL
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
           to_jsonb(l)->>'creation_origin' AS creation_origin
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
  AND (c.origin_transfer_id = t.id OR
       (c.origin_transfer_id IS NULL AND t.created_at <= c.created_at
        AND c.note = 'Перемещение #' || t.id::text || ' из ' || t.from_location_code))
  AND (c.creation_origin IS NULL OR c.creation_origin = 'transfer')
  AND (c.creation_origin IS DISTINCT FROM 'transfer' OR c.origin_transfer_id IS NOT NULL)
  AND (c.origin_transfer_id IS NULL OR c.creation_origin = 'transfer')
  AND c.origin_return_line_id IS NULL
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
           to_jsonb(l)->>'creation_origin' AS creation_origin
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
  AND (to_jsonb(l)->>'creation_origin' IS NULL
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

-- name: final_provenance
-- One final, fail-closed class per lot. Run this SELECT in a read-only snapshot.
-- Keep the CASE order aligned with classify_lot in apps/inventory/lot_provenance.py.
-- An explicit origin_transfer is proved by structured evidence, never by note.
-- Historical transfer inference requires the exact machine-written note;
-- nearby documents never replace a missing or malformed note.
WITH lot_base AS (
    SELECT l.id, l.batch_id, l.batch_line_id, l.part_type_id, l.location_id,
           l.quantity, l.initial_quantity, l.status, l.created_at, l.note,
           nullif(to_jsonb(l)->>'origin_transfer_id', '')::bigint AS origin_transfer_id,
           nullif(to_jsonb(l)->>'origin_return_line_id', '')::bigint AS origin_return_line_id,
           to_jsonb(l)->>'creation_origin' AS marker,
           coalesce(first_move.from_location_id, l.location_id) AS original_location_id,
           note_match.id AS note_transfer_id,
           b.batch_id AS current_line_batch_id,
           b.part_type_id AS current_line_part_type_id
    FROM inventory_stocklot l
    JOIN procurement_batchline b ON b.id = l.batch_line_id
    LEFT JOIN LATERAL (
        SELECT t.id FROM inventory_stocktransfer t
        WHERE l.note LIKE 'Перемещение #%'
          AND l.note = 'Перемещение #' || t.id::text || ' из ' || t.from_location_code
        LIMIT 1
    ) note_match ON true
    LEFT JOIN LATERAL (
        SELECT m.from_location_id
        FROM inventory_stockmovement m
        WHERE m.stock_lot_id = l.id AND m.movement_type = 'move_lot'
          AND m.document_type = '' AND m.from_location_id IS NOT NULL
          AND m.to_location_id IS NOT NULL
        ORDER BY m.created_at, m.id LIMIT 1
    ) first_move ON true
), own_stats AS (
    SELECT m.stock_lot_id AS lot_id,
           (array_agg(m.id ORDER BY m.created_at, m.id))[1] AS first_id,
           (array_agg(m.id ORDER BY m.created_at, m.id)
               FILTER (WHERE NOT (m.movement_type = 'receive_lot'
                   AND m.comment = 'Открывающий остаток' AND m.document_type = '')))[1]
               AS first_real_id,
           count(*) AS movement_count,
           count(*) FILTER (WHERE m.movement_type = 'receive_lot'
               AND NOT (m.comment = 'Открывающий остаток' AND m.document_type = ''))
               AS receipt_count,
           min(m.id) FILTER (WHERE m.movement_type = 'receive_lot'
               AND NOT (m.comment = 'Открывающий остаток' AND m.document_type = ''))
               AS receipt_id,
           count(*) FILTER (WHERE m.movement_type = 'receive_lot'
               AND m.comment = 'Открывающий остаток' AND m.document_type = '')
               AS backfill_count,
           count(*) FILTER (WHERE m.batch_line_id IS NOT NULL
               AND m.batch_line_id <> l.batch_line_id) AS other_line_count,
           coalesce(sum(CASE
               WHEN m.movement_type IN ('adjust_in', 'return_lot', 'writeoff_cancel_lot')
                   THEN m.quantity
               WHEN m.movement_type IN ('adjust_out', 'sale_lot', 'issue_lot',
                                        'write_off_lot')
                   OR (m.movement_type = 'move_lot'
                       AND m.document_type = 'stock_transfer') THEN -m.quantity
               ELSE 0 END), 0) AS own_net
    FROM inventory_stockmovement m
    JOIN inventory_stocklot l ON l.id = m.stock_lot_id
    GROUP BY m.stock_lot_id
), lot_history AS (
    SELECT l.*, coalesce(s.movement_count, 0) AS movement_count,
           coalesce(s.receipt_count, 0) AS receipt_count,
           coalesce(s.backfill_count, 0) AS backfill_count,
           coalesce(s.other_line_count, 0) AS other_line_count,
           coalesce(s.own_net, 0) AS own_net,
           first_m.id AS first_id, first_m.movement_type AS first_type,
           first_m.document_type AS first_document_type,
           first_m.document_id AS first_document_id,
           first_m.created_at AS first_created_at,
           s.first_real_id,
           receipt.id AS receipt_id, receipt.batch_line_id AS receipt_line_id,
           receipt.batch_id AS receipt_batch_id,
           receipt.part_type_id AS receipt_part_type_id,
           receipt.to_location_id AS receipt_location_id,
           receipt.quantity AS receipt_quantity,
           receipt.document_type AS receipt_document_type,
           receipt.document_id AS receipt_document_id,
           receipt_line.batch_id AS receipt_line_batch_id,
           receipt_line.part_type_id AS receipt_line_part_type_id
    FROM lot_base l
    LEFT JOIN own_stats s ON s.lot_id = l.id
    LEFT JOIN inventory_stockmovement first_m ON first_m.id = s.first_id
    LEFT JOIN inventory_stockmovement receipt ON receipt.id = s.receipt_id
    LEFT JOIN procurement_batchline receipt_line ON receipt_line.id = receipt.batch_line_id
), source_conflicts AS (
    SELECT DISTINCT m.stock_lot_id AS lot_id
    FROM inventory_stockmovement m
    JOIN inventory_stocklot l ON l.id = m.stock_lot_id
    WHERE m.batch_line_id IS NOT NULL AND m.batch_line_id <> l.batch_line_id
), transfer_groups AS (
    SELECT t.id AS transfer_id, sum(m.quantity) AS moved_quantity,
           bool_and(coalesce(
               m.movement_type = 'move_lot' AND m.part_type_id = t.part_type_id
               AND m.from_location_id = t.from_location_id
               AND m.to_location_id = t.to_location_id
               AND source.id IS NOT NULL AND source.part_type_id = t.part_type_id
               AND source.batch_line_id = m.batch_line_id
               AND m.batch_id = source.batch_id
               AND source_line.id IS NOT NULL
               AND source_line.part_type_id = t.part_type_id
               AND source_line.batch_id = m.batch_id
               AND conflict.lot_id IS NULL, false)) AS rows_consistent
    FROM inventory_stocktransfer t
    JOIN inventory_stockmovement m ON m.document_type = 'stock_transfer'
        AND m.document_id = t.id
    LEFT JOIN inventory_stocklot source ON source.id = m.stock_lot_id
    LEFT JOIN procurement_batchline source_line ON source_line.id = m.batch_line_id
    LEFT JOIN source_conflicts conflict ON conflict.lot_id = source.id
    GROUP BY t.id
), transfer_candidates AS (
    SELECT l.id AS lot_id, count(DISTINCT t.id) AS doc_count, min(t.id) AS transfer_id
    FROM lot_history l
    JOIN inventory_stockmovement m ON m.stock_lot_id IS DISTINCT FROM l.id
        AND m.movement_type = 'move_lot' AND m.document_type = 'stock_transfer'
        AND m.batch_id = l.batch_id AND m.batch_line_id = l.batch_line_id
        AND m.part_type_id = l.part_type_id
        AND m.to_location_id = l.original_location_id
        AND m.quantity = l.initial_quantity
        AND abs(extract(epoch FROM (m.created_at - l.created_at))) <= 1
    JOIN inventory_stocktransfer t ON t.id = m.document_id
        AND t.part_type_id = l.part_type_id
        AND t.to_location_id = l.original_location_id
    WHERE l.status <> 'receiving'
    GROUP BY l.id
), transfer_choice AS (
    SELECT l.*, coalesce(c.doc_count, 0) AS candidate_doc_count,
           c.transfer_id AS candidate_transfer_id,
           CASE WHEN l.origin_transfer_id IS NOT NULL THEN l.origin_transfer_id
                WHEN l.note LIKE 'Перемещение #%' THEN l.note_transfer_id
                ELSE NULL::bigint END AS chosen_transfer_id
    FROM lot_history l
    LEFT JOIN transfer_candidates c ON c.lot_id = l.id
), transfer_proof AS (
    SELECT l.*,
           coalesce(t.part_item_id IS NULL
               AND t.stock_state IN ('available', 'quarantine')
               AND t.part_type_id = l.part_type_id
               AND t.to_location_id = l.original_location_id
               AND l.current_line_part_type_id = l.part_type_id
               AND g.moved_quantity = t.quantity AND g.rows_consistent
               AND coalesce(target.moved_quantity, 0) = l.initial_quantity, false)
               AS transfer_valid,
           t.created_at AS transfer_created_at,
           coalesce(target.has_near_target_movement, false) AS has_near_target_movement
    FROM transfer_choice l
    LEFT JOIN inventory_stocktransfer t ON t.id = l.chosen_transfer_id
    LEFT JOIN transfer_groups g ON g.transfer_id = t.id
    LEFT JOIN LATERAL (
        SELECT sum(m.quantity) AS moved_quantity,
               bool_or(m.created_at >= l.created_at
                   AND m.created_at <= l.created_at + interval '1 second')
                   AS has_near_target_movement
        FROM inventory_stockmovement m
        WHERE m.document_type = 'stock_transfer' AND m.document_id = t.id
          AND m.batch_line_id = l.batch_line_id
          AND m.to_location_id = l.original_location_id
    ) target ON true
), return_document_proof AS (
    SELECT r.id AS return_id,
           (SELECT count(*) FROM returns_stockreturnline rl
               WHERE rl.stock_return_id = r.id) > 0 AS has_lines,
           (SELECT count(*) FROM returns_stockreturnline rl
               WHERE rl.stock_return_id = r.id) =
           (SELECT count(*) FROM inventory_stockmovement m
               WHERE m.document_type = 'stock_return' AND m.document_id = r.id)
               AS counts_match,
           NOT EXISTS (
               SELECT 1 FROM inventory_stockmovement m
               WHERE m.document_type = 'stock_return' AND m.document_id = r.id
                 AND (SELECT count(*) FROM returns_stockreturnline rl
                     WHERE rl.stock_return_id = r.id
                       AND ((m.movement_type = 'return_lot'
                             AND rl.returned_lot_id = m.stock_lot_id
                             AND m.stock_lot_id IS NOT NULL AND rl.part_item_id IS NULL)
                         OR (m.movement_type = 'return_item'
                             AND rl.part_item_id = m.part_item_id
                             AND m.part_item_id IS NOT NULL))
                       AND rl.batch_id = m.batch_id
                       AND rl.batch_line_id = m.batch_line_id
                       AND rl.part_type_id = m.part_type_id
                       AND rl.to_location_id = m.to_location_id
                       AND rl.quantity = m.quantity) <> 1
           ) AS every_movement_matches,
           NOT EXISTS (
               SELECT 1 FROM returns_stockreturnline rl
               WHERE rl.stock_return_id = r.id
                 AND (SELECT count(*) FROM inventory_stockmovement m
                     WHERE m.document_type = 'stock_return' AND m.document_id = r.id
                       AND ((m.movement_type = 'return_lot'
                             AND rl.returned_lot_id = m.stock_lot_id
                             AND m.stock_lot_id IS NOT NULL AND rl.part_item_id IS NULL)
                         OR (m.movement_type = 'return_item'
                             AND rl.part_item_id = m.part_item_id
                             AND m.part_item_id IS NOT NULL))
                       AND rl.batch_id = m.batch_id
                       AND rl.batch_line_id = m.batch_line_id
                       AND rl.part_type_id = m.part_type_id
                       AND rl.to_location_id = m.to_location_id
                       AND rl.quantity = m.quantity) <> 1
           ) AS every_line_matches
    FROM returns_stockreturn r
), valid_return_lines AS (
    SELECT l.id AS lot_id, rl.id AS return_line_id,
           m.id AS movement_id,
           abs(extract(epoch FROM (m.created_at - l.created_at))) <= 1
               AND abs(extract(epoch FROM (r.completed_at - l.created_at))) <= 1
               AS historical_time_valid
    FROM lot_history l
    JOIN returns_stockreturnline rl ON rl.returned_lot_id = l.id
    JOIN returns_stockreturn r ON r.id = rl.stock_return_id
        AND r.status IN ('completed', 'canceled') AND r.completed_at IS NOT NULL
    JOIN return_document_proof doc ON doc.return_id = r.id
        AND doc.has_lines AND doc.counts_match
        AND doc.every_movement_matches AND doc.every_line_matches
    JOIN inventory_stockmovement m ON m.id = l.first_id
        AND m.movement_type = 'return_lot' AND m.document_type = 'stock_return'
        AND m.document_id = r.id
        AND m.batch_id = rl.batch_id AND m.batch_line_id = rl.batch_line_id
        AND m.part_type_id = rl.part_type_id
        AND m.to_location_id = rl.to_location_id AND m.quantity = rl.quantity
    WHERE rl.batch_id = l.batch_id AND rl.batch_line_id = l.batch_line_id
      AND rl.part_type_id = l.part_type_id
      AND rl.to_location_id = l.original_location_id
      AND rl.quantity = l.initial_quantity
      AND (SELECT count(*) FROM inventory_stockmovement own
           WHERE own.stock_lot_id = l.id AND own.document_type = 'stock_return'
             AND own.document_id = r.id) = 1
      AND (SELECT count(*) FROM returns_stockreturnline same_lot
           WHERE same_lot.stock_return_id = r.id AND same_lot.returned_lot_id = l.id) = 1
), return_stats AS (
    SELECT l.id AS lot_id,
           count(v.return_line_id) FILTER (WHERE v.historical_time_valid)
               AS historical_valid_count,
           bool_or(v.return_line_id = l.origin_return_line_id)
               AS explicit_valid,
           (l.first_id IS NOT NULL
               AND abs(extract(epoch FROM (l.first_created_at - l.created_at))) <= 1
               AND (l.first_type = 'return_lot'
                    OR l.first_document_type = 'stock_return'))
               OR EXISTS (
                   SELECT 1 FROM returns_stockreturnline rl
                   JOIN returns_stockreturn r ON r.id = rl.stock_return_id
                   WHERE rl.returned_lot_id = l.id AND r.completed_at IS NOT NULL
                     AND abs(extract(epoch FROM (r.completed_at - l.created_at))) <= 1
               ) AS has_return_creation_evidence
    FROM lot_history l
    LEFT JOIN valid_return_lines v ON v.lot_id = l.id
    GROUP BY l.id, l.first_id, l.first_created_at, l.created_at, l.first_type,
             l.first_document_type
), return_proof AS (
    SELECT l.*,
           CASE WHEN l.origin_return_line_id IS NOT NULL
                THEN coalesce(rs.explicit_valid, false)
                WHEN l.first_id IS NOT NULL
                     AND abs(extract(epoch FROM (l.first_created_at - l.created_at))) <= 1
                     AND l.first_type = 'adjust_in'
                     AND l.first_document_type IN ('found_addition', 'section_recount')
                    THEN false
                WHEN NOT rs.has_return_creation_evidence THEN false
                WHEN rs.historical_valid_count = 1 THEN true
                ELSE NULL::boolean END AS return_origin
    FROM transfer_proof l
    JOIN return_stats rs ON rs.lot_id = l.id
), final_flags AS (
    SELECT l.*,
           EXISTS (
               SELECT 1 FROM inventory_stockmovement m
               WHERE l.status <> 'receiving'
                 AND m.movement_type = 'move_lot' AND m.document_type = 'stock_transfer'
                 AND m.batch_id = l.batch_id AND m.batch_line_id = l.batch_line_id
                 AND m.part_type_id = l.part_type_id
                 AND m.to_location_id = l.original_location_id
                 AND m.quantity = l.initial_quantity AND m.created_at < l.created_at
           ) AS unanchored_transfer,
           coalesce(merge_net.quantity, 0) AS merge_net,
           (SELECT count(*) FROM procurement_batchline b
               WHERE b.batch_id = l.batch_id AND b.part_type_id = l.part_type_id)
               AS same_part_line_count
    FROM return_proof l
    LEFT JOIN LATERAL (
        SELECT sum(m.quantity) AS quantity
        FROM inventory_stockmovement m
        WHERE m.batch_line_id = l.batch_line_id
          AND m.stock_lot_id IS DISTINCT FROM l.id
          AND m.movement_type = 'move_lot' AND m.document_type = 'stock_transfer'
          AND m.created_at > l.created_at
          AND m.to_location_id = coalesce((
              SELECT whole_move.to_location_id
              FROM inventory_stockmovement whole_move
              WHERE whole_move.stock_lot_id = l.id
                AND whole_move.movement_type = 'move_lot'
                AND whole_move.document_type = ''
                AND whole_move.from_location_id IS NOT NULL
                AND whole_move.to_location_id IS NOT NULL
                AND whole_move.created_at <= m.created_at
              ORDER BY whole_move.created_at DESC, whole_move.id DESC LIMIT 1
          ), l.original_location_id)
    ) merge_net ON true
)
SELECT l.id AS lot_id, l.batch_line_id, l.status,
       CASE
           WHEN l.marker IS NOT NULL AND l.marker NOT IN (
               'supplier_pending', 'supplier_received', 'transfer', 'return',
               'found', 'recount'
           ) THEN 'unknown'
           -- Claimed derived origin wins the ordering. A broken claim is UNKNOWN.
           WHEN l.origin_transfer_id IS NOT NULL AND l.origin_return_line_id IS NOT NULL
               THEN 'unknown'
           WHEN l.origin_transfer_id IS NOT NULL AND l.marker IS DISTINCT FROM 'transfer'
               THEN 'unknown'
           WHEN l.origin_return_line_id IS NOT NULL AND l.marker IS DISTINCT FROM 'return'
               THEN 'unknown'
           WHEN l.marker = 'transfer' AND l.origin_transfer_id IS NULL THEN 'unknown'
           WHEN l.marker = 'return' AND l.origin_return_line_id IS NULL THEN 'unknown'
           WHEN l.origin_transfer_id IS NOT NULL THEN
               CASE WHEN l.receipt_count = 0 AND l.transfer_valid
                    THEN 'transfer_derived' ELSE 'unknown' END
           WHEN l.origin_return_line_id IS NOT NULL THEN
               CASE WHEN l.receipt_count = 0 AND l.return_origin IS TRUE
                    THEN 'return_derived' ELSE 'unknown' END
           -- Damaged return creation evidence blocks weaker supplier inference.
           WHEN l.marker IS DISTINCT FROM 'supplier_received'
                AND (l.return_origin IS NULL
                     OR (l.return_origin IS TRUE AND l.receipt_count > 0))
               THEN 'unknown'
           WHEN l.return_origin IS TRUE THEN 'return_derived'
           WHEN l.receipt_count > 0 THEN
               CASE WHEN l.marker IN ('supplier_received') OR l.marker IS NULL THEN
                   CASE WHEN l.note NOT LIKE 'Перемещение #%'
                             AND NOT l.unanchored_transfer
                             AND l.receipt_count = 1 AND l.first_real_id = l.receipt_id
                             AND l.receipt_document_type = ''
                             AND l.receipt_document_id IS NULL
                             AND l.receipt_batch_id = l.batch_id
                             AND l.receipt_part_type_id = l.part_type_id
                             AND l.receipt_line_id IS NOT NULL
                             AND l.receipt_line_batch_id = l.receipt_batch_id
                             AND l.receipt_line_part_type_id = l.receipt_part_type_id
                             AND l.receipt_location_id = l.original_location_id
                             AND l.receipt_quantity = l.initial_quantity
                             AND l.initial_quantity > 0
                        THEN CASE WHEN l.receipt_line_id = l.batch_line_id
                             THEN 'primary_receipt' ELSE 'received_on_another_line' END
                        ELSE 'unknown' END
                   ELSE 'unknown' END
           WHEN l.note LIKE 'Перемещение #%' THEN
               CASE WHEN l.chosen_transfer_id IS NOT NULL
                         AND (l.candidate_doc_count = 0 OR
                              (l.candidate_doc_count = 1 AND
                               l.candidate_transfer_id = l.chosen_transfer_id))
                         AND l.transfer_created_at <= l.created_at
                         AND l.transfer_created_at >= l.created_at - interval '1 second'
                         AND l.has_near_target_movement AND l.transfer_valid
                    THEN 'transfer_derived' ELSE 'unknown' END
           WHEN l.status = 'receiving' THEN
               CASE WHEN l.movement_count = l.backfill_count
                    THEN 'pending_receipt' ELSE 'unknown' END
           WHEN l.candidate_doc_count > 0 THEN 'unknown'
           WHEN l.unanchored_transfer THEN 'unknown'
           WHEN l.initial_quantity = 0 AND l.first_type = 'adjust_in'
                AND l.first_document_type = 'section_recount' THEN 'recount_derived'
           WHEN l.initial_quantity = 0 AND l.first_type = 'adjust_in'
                AND l.first_document_type = 'found_addition' THEN 'found_stock'
           WHEN l.backfill_count > 0 THEN 'unknown'
           -- The persisted supplier_pending marker is positive legacy evidence;
           -- a NULL historical marker never acquires supplier provenance here.
           WHEN l.initial_quantity > 0 AND l.marker = 'supplier_pending'
                AND l.batch_id = l.current_line_batch_id
                AND l.current_line_part_type_id = l.part_type_id
                AND l.same_part_line_count = 1
                AND l.quantity - l.own_net - l.merge_net = l.initial_quantity
                AND l.other_line_count = 0 THEN 'legacy_primary_receipt'
           -- Absent, ambiguous and contradictory evidence all fail closed.
           ELSE 'unknown'
       END AS provenance
FROM final_flags l
ORDER BY l.id;
