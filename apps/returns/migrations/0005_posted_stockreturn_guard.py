"""PostgreSQL guard: a completed/canceled StockReturn is immutable posted history.

Below the ORM (``_base_manager``, ``QuerySet._update``, raw SQL from the
application role) the database itself enforces:

* a return is inserted only as ``draft``;
* ``draft -> completed`` only when exactly one RETURN_ITEM/RETURN_LOT movement
  per line exists for this document, and no cancel movement exists (movements
  created before the document are ignored: production has two such legacy
  movements whose ``document_id`` was reused);
* ``draft -> canceled`` only without any posting movement;
* ``completed -> canceled`` only when exactly one compensating movement per line
  exists; nothing else about the document may change in that step;
* ``completed`` and ``canceled`` never change status again, so a posted return
  can never be posted twice;
* columns of a posted document do not change, except ``updated_at``, a user
  reference being cleared by ON DELETE SET NULL, and the cost columns inside the
  transaction-local ``denstock.returns_cost_correction`` scope used by the
  receipt-proven historical cost remediation;
* lines of a posted document are never inserted, moved, deleted or edited
  (cost columns excepted in the same scope); the parent row is share-locked,
  so a line write and a completion serialize.

It does not touch existing rows: the checks run only on new writes.  A database
superuser (or the table owner disabling triggers) can still bypass it; that is
the documented boundary.  SQLite (development/tests) relies on the ORM guards.
"""

from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION denstock_return_cost_scope() RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT coalesce(current_setting('denstock.returns_cost_correction', true), '') = 'on'
$$;

-- Only movements created at/after the document count: production holds two
-- older RETURN_LOT movements whose document_id was later reused by new returns.
CREATE OR REPLACE FUNCTION denstock_return_movement_counts(
    doc_id bigint, since timestamptz,
    OUT posted bigint, OUT canceled bigint, OUT lines bigint
) LANGUAGE sql STABLE AS $$
    SELECT
        (SELECT count(*) FROM inventory_stockmovement
          WHERE document_type = 'stock_return' AND document_id = doc_id
            AND movement_type IN ('return_item', 'return_lot') AND created_at >= since),
        (SELECT count(*) FROM inventory_stockmovement
          WHERE document_type = 'stock_return_cancel' AND document_id = doc_id
            AND created_at >= since),
        (SELECT count(*) FROM returns_stockreturnline WHERE stock_return_id = doc_id)
$$;

CREATE OR REPLACE FUNCTION denstock_guard_stockreturn() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    c record;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'draft' THEN
            RAISE EXCEPTION 'StockReturn % must be created as draft', NEW.number
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'draft' THEN
            RAISE EXCEPTION 'Posted StockReturn % cannot be deleted', OLD.number
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;

    -- UPDATE
    IF OLD.status = 'draft' THEN
        IF NEW.status = 'draft' THEN
            RETURN NEW;
        END IF;
        c := denstock_return_movement_counts(NEW.id, OLD.created_at);
        IF NEW.status = 'completed'
           AND c.lines > 0 AND c.posted = c.lines AND c.canceled = 0 THEN
            RETURN NEW;
        END IF;
        IF NEW.status = 'canceled' AND c.posted = 0 AND c.canceled = 0 THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'StockReturn %: % -> % without matching stock movements',
            OLD.number, OLD.status, NEW.status USING ERRCODE = 'check_violation';
    END IF;

    -- OLD is posted (completed or canceled).  Identity and history are frozen.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.number IS DISTINCT FROM OLD.number
       OR NEW.source_type IS DISTINCT FROM OLD.source_type
       OR NEW.source_id IS DISTINCT FROM OLD.source_id
       OR NEW.reason IS DISTINCT FROM OLD.reason
       OR NEW.comment IS DISTINCT FROM OLD.comment
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.completed_at IS DISTINCT FROM OLD.completed_at
       OR (NEW.created_by_id IS DISTINCT FROM OLD.created_by_id
           AND NEW.created_by_id IS NOT NULL)
       OR (NEW.completed_by_id IS DISTINCT FROM OLD.completed_by_id
           AND NEW.completed_by_id IS NOT NULL)
       OR (NEW.cost_total IS DISTINCT FROM OLD.cost_total
           AND NOT denstock_return_cost_scope())
    THEN
        RAISE EXCEPTION 'Posted StockReturn % is immutable', OLD.number
            USING ERRCODE = 'check_violation';
    END IF;

    IF OLD.status = 'completed' AND NEW.status = 'canceled' THEN
        c := denstock_return_movement_counts(NEW.id, OLD.created_at);
        IF c.lines > 0 AND c.canceled = c.lines THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'StockReturn % cannot be canceled without compensating movements',
            OLD.number USING ERRCODE = 'check_violation';
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status
       OR NEW.canceled_at IS DISTINCT FROM OLD.canceled_at
       OR NEW.cancel_reason IS DISTINCT FROM OLD.cancel_reason
       OR (NEW.canceled_by_id IS DISTINCT FROM OLD.canceled_by_id
           AND NEW.canceled_by_id IS NOT NULL)
    THEN
        RAISE EXCEPTION 'Posted StockReturn % is immutable (% -> %)',
            OLD.number, OLD.status, NEW.status USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION denstock_guard_stockreturnline() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    parent_status text;
    other_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO parent_status FROM returns_stockreturn
         WHERE id = OLD.stock_return_id FOR SHARE;
        -- A missing parent means the document itself is being deleted (draft only).
        IF parent_status IS NOT NULL AND parent_status <> 'draft' THEN
            RAISE EXCEPTION 'Lines of a posted StockReturn cannot be deleted'
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;

    SELECT status INTO parent_status FROM returns_stockreturn
     WHERE id = NEW.stock_return_id FOR SHARE;
    IF TG_OP = 'UPDATE' AND NEW.stock_return_id IS DISTINCT FROM OLD.stock_return_id THEN
        SELECT status INTO other_status FROM returns_stockreturn
         WHERE id = OLD.stock_return_id FOR SHARE;
        IF parent_status <> 'draft' OR other_status <> 'draft' THEN
            RAISE EXCEPTION 'A posted StockReturn line cannot move between documents'
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    IF parent_status = 'draft' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'UPDATE' AND denstock_return_cost_scope()
       AND (to_jsonb(NEW) - 'unit_cost_rub' - 'total_cost_rub')
           = (to_jsonb(OLD) - 'unit_cost_rub' - 'total_cost_rub')
    THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'Lines of a posted StockReturn are immutable'
        USING ERRCODE = 'check_violation';
END
$$;

DROP TRIGGER IF EXISTS denstock_guard_stockreturn ON returns_stockreturn;
CREATE TRIGGER denstock_guard_stockreturn
    BEFORE INSERT OR UPDATE OR DELETE ON returns_stockreturn
    FOR EACH ROW EXECUTE FUNCTION denstock_guard_stockreturn();

DROP TRIGGER IF EXISTS denstock_guard_stockreturnline ON returns_stockreturnline;
CREATE TRIGGER denstock_guard_stockreturnline
    BEFORE INSERT OR UPDATE OR DELETE ON returns_stockreturnline
    FOR EACH ROW EXECUTE FUNCTION denstock_guard_stockreturnline();
"""

REVERSE = r"""
DROP TRIGGER IF EXISTS denstock_guard_stockreturnline ON returns_stockreturnline;
DROP TRIGGER IF EXISTS denstock_guard_stockreturn ON returns_stockreturn;
DROP FUNCTION IF EXISTS denstock_guard_stockreturnline();
DROP FUNCTION IF EXISTS denstock_guard_stockreturn();
DROP FUNCTION IF EXISTS denstock_return_movement_counts(bigint, timestamptz);
DROP FUNCTION IF EXISTS denstock_return_cost_scope();
"""


def forward(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(FORWARD, params=None)


def reverse(apps, schema_editor):
    if schema_editor.connection.vendor == "postgresql":
        schema_editor.execute(REVERSE, params=None)


class Migration(migrations.Migration):

    dependencies = [
        ("returns", "0004_stockreturn_cancel_reason_stockreturn_canceled_at_and_more"),
        ("inventory", "0015_receipt_customer_price_snapshot"),
    ]

    operations = [migrations.RunPython(forward, reverse)]
