"""PostgreSQL: cap how many deep links a request can collect from the public role.

The customer may retry the Telegram handoff when the browser did not leave the
page, so the public role can now insert more than one link token per request.
The readable session cookie must not be the only limit: this replaces the
guard function of 0006 with the same checks plus a hard per-request cap.
"""

from django.db import migrations

TABLES = (
    "customer_requests_telegramconversation",
    "customer_requests_telegramoutboxevent",
    "customer_requests_customerrequestmessengerlinktoken",
)
FUNCTION = "denstock_telegram_public_insert_guard"
MAX_LINKS_PER_REQUEST = 3

CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {FUNCTION}() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
DECLARE
    proof text;
    expected text;
    issued integer;
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
        SELECT count(*) INTO issued
          FROM customer_requests_customerrequestmessengerlinktoken
         WHERE request_id = NEW.request_id;
        IF issued >= {MAX_LINKS_PER_REQUEST} THEN
            RAISE EXCEPTION 'telegram link limit reached' USING ERRCODE = '42501';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""

PREVIOUS_FUNCTION = CREATE_FUNCTION.replace(
    """        SELECT count(*) INTO issued
          FROM customer_requests_customerrequestmessengerlinktoken
         WHERE request_id = NEW.request_id;
        IF issued >= 3 THEN
            RAISE EXCEPTION 'telegram link limit reached' USING ERRCODE = '42501';
        END IF;
""",
    "",
).replace("    issued integer;\n", "")


def forward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CREATE_FUNCTION)


def backward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(PREVIOUS_FUNCTION)


class Migration(migrations.Migration):
    dependencies = [("customer_requests", "0006_telegram_public_insert_guard")]

    operations = [migrations.RunPython(forward, backward)]
