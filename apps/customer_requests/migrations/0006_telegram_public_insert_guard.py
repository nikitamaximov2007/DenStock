"""PostgreSQL guard: the public role may attach Telegram rows only to its own request.

The restricted public catalog role needs INSERT on the Telegram conversation,
outbox and link-token tables for the request it has just created. A plain
INSERT grant would also let a compromised public process attach a linked
conversation or a known link token to somebody else's request and receive the
operator replies meant for that customer.

This trigger applies only to roles without UPDATE on the table (the public
role); the internal runtime, the bot and migrations are unaffected. Such a role
must prove, inside the same transaction, the raw submission key of the target
request (``denstock.telegram_request_proof``), and may insert only the harmless
initial shape of each row. Additive and non-destructive: no existing row is
touched. SQLite (development and tests) has no roles and gets nothing.
"""

from django.db import migrations

TABLES = (
    "customer_requests_telegramconversation",
    "customer_requests_telegramoutboxevent",
    "customer_requests_customerrequestmessengerlinktoken",
)
FUNCTION = "denstock_telegram_public_insert_guard"

CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {FUNCTION}() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    proof text;
    expected text;
BEGIN
    IF has_table_privilege(current_user, TG_RELID, 'UPDATE') THEN
        RETURN NEW;
    END IF;
    proof := coalesce(current_setting('denstock.telegram_request_proof', true), '');
    IF proof = '' THEN
        RAISE EXCEPTION 'telegram row refused' USING ERRCODE = '42501';
    END IF;
    SELECT submission_key_hash INTO expected
      FROM customer_requests_customerrequest WHERE id = NEW.request_id;
    IF expected IS NULL
       OR expected <> encode(sha256(convert_to(proof, 'UTF8')), 'hex') THEN
        RAISE EXCEPTION 'telegram row refused' USING ERRCODE = '42501';
    END IF;
    IF TG_TABLE_NAME = 'customer_requests_telegramconversation' THEN
        IF NEW.status <> 'awaiting_link'
           OR NEW.customer_chat_id IS NOT NULL
           OR NEW.customer_user_id IS NOT NULL
           OR NEW.customer_username <> ''
           OR NEW.linked_at IS NOT NULL
           OR NEW.last_message_at IS NOT NULL THEN
            RAISE EXCEPTION 'telegram row refused' USING ERRCODE = '42501';
        END IF;
    ELSIF TG_TABLE_NAME = 'customer_requests_telegramoutboxevent' THEN
        IF NEW.kind <> 'new_request'
           OR NEW.message_id IS NOT NULL
           OR NEW.exclude_operator_id IS NOT NULL
           OR NEW.dedupe_key <> ('new_request:' || NEW.request_id)
           OR NEW.status <> 'pending'
           OR NEW.attempts <> 0
           OR NEW.dispatched_at IS NOT NULL THEN
            RAISE EXCEPTION 'telegram row refused' USING ERRCODE = '42501';
        END IF;
    ELSIF TG_TABLE_NAME = 'customer_requests_customerrequestmessengerlinktoken' THEN
        IF NEW.channel <> 'telegram'
           OR NEW.used_at IS NOT NULL
           OR NEW.revoked_at IS NOT NULL
           OR NEW.created_by_id IS NOT NULL
           OR NEW.expires_at > now() + interval '7 days 1 hour' THEN
            RAISE EXCEPTION 'telegram row refused' USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""


def forward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CREATE_FUNCTION)
    for table in TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {FUNCTION} ON {table}")
        schema_editor.execute(
            f"CREATE TRIGGER {FUNCTION} BEFORE INSERT ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {FUNCTION}()"
        )


def backward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for table in TABLES:
        schema_editor.execute(f"DROP TRIGGER IF EXISTS {FUNCTION} ON {table}")
    schema_editor.execute(f"DROP FUNCTION IF EXISTS {FUNCTION}()")


class Migration(migrations.Migration):
    dependencies = [("customer_requests", "0005_telegram_messaging")]

    operations = [migrations.RunPython(forward, backward)]
