"""The account's database-side guarantees (PostgreSQL only).

The public process runs as a restricted role that must not be able to list
other people's requests or sales even if it is fully compromised. So:

* ``customer_account_session_account(hash)`` resolves a live session. The public
  role cannot read the sessions table; it can only ask this question for a
  token it already holds.
* ``customer_account_current()`` answers it for the session digest the request
  put into ``prostor.session_hash`` — the only thing the views and policies
  trust.
* ``customer_account_complete_attempt(...)`` is the ONLY way the public role can
  create a session or attach an identity. It checks the one-time code inside
  the database, so a compromised process cannot mint a session for an account
  whose messenger code it never saw.
* ``customer_account_logout(hash)`` revokes one session.
* ``customer_account_*`` views expose the signed-in account's own requests and
  completed purchases. Views run with their owner's rights, so the public role
  reads them without any grant on the underlying business tables — and without
  cost, profit, supplier or employee columns, which the views do not select.

Grants, RLS policies and the role itself live in
``scripts/operations/create_public_catalog_role.sql``: only that script knows
the role's name. Every function here is revoked from PUBLIC; the script grants
EXECUTE to the public role explicitly.

The same rules are implemented in Python (``services``) for SQLite and the
internal role; ``tests/test_customer_account_postgresql.py`` runs the database
version.
"""

from django.db import migrations

FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION customer_account_session_account(p_session_hash text)
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT s.account_id
    FROM customer_accounts_customersession s
    JOIN customer_accounts_customeraccount a ON a.id = s.account_id
    WHERE s.token_hash = p_session_hash
      AND p_session_hash <> ''
      AND s.revoked_at IS NULL
      AND s.expires_at > now()
      AND a.status = 'active'
$$;

CREATE OR REPLACE FUNCTION customer_account_current()
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT customer_account_session_account(
        coalesce(current_setting('prostor.session_hash', true), '')
    )
$$;

CREATE OR REPLACE FUNCTION customer_account_current_customer()
RETURNS bigint
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT l.customer_id
    FROM customer_accounts_customeraccountcustomerlink l
    WHERE l.account_id = customer_account_current()
      AND l.unlinked_at IS NULL
$$;

CREATE OR REPLACE FUNCTION customer_account_logout(p_session_hash text)
RETURNS void
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_account bigint;
BEGIN
    UPDATE customer_accounts_customersession
       SET revoked_at = now()
     WHERE token_hash = p_session_hash AND revoked_at IS NULL
    RETURNING account_id INTO v_account;
    IF v_account IS NOT NULL THEN
        INSERT INTO customer_accounts_customeraccountevent
            (account_id, kind, detail, actor_user_id, created_at)
        VALUES (v_account, 'logout', '{}'::jsonb, NULL, now());
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION customer_account_claim(
    p_account bigint, p_provider text, p_user bigint
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_count integer := 0;
BEGIN
    IF p_provider = 'max' THEN
        UPDATE customer_requests_customerrequest r
           SET customer_account_id = p_account
          FROM customer_requests_maxconversation c
         WHERE c.request_id = r.id AND c.customer_user_id = p_user
           AND c.status = 'linked' AND r.customer_account_id IS NULL;
        GET DIAGNOSTICS v_count = ROW_COUNT;
    ELSIF p_provider = 'telegram' THEN
        UPDATE customer_requests_customerrequest r
           SET customer_account_id = p_account
          FROM customer_requests_telegramconversation c
         WHERE c.request_id = r.id AND c.customer_user_id = p_user
           AND c.status = 'linked' AND r.customer_account_id IS NULL;
        GET DIAGNOSTICS v_count = ROW_COUNT;
    END IF;
    IF v_count > 0 THEN
        INSERT INTO customer_accounts_customeraccountevent
            (account_id, kind, detail, actor_user_id, created_at)
        VALUES (p_account, 'requests_claimed',
                jsonb_build_object('provider', p_provider, 'count', v_count), NULL, now());
    END IF;
    RETURN v_count;
END
$$;

CREATE OR REPLACE FUNCTION customer_account_complete_attempt(
    p_browser_hash text,
    p_code text,
    p_new_session_hash text,
    p_current_session_hash text,
    p_session_days integer,
    p_max_tries integer
) RETURNS TABLE(outcome text, account_id bigint)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    a customer_accounts_customerloginattempt%ROWTYPE;
    v_account bigint;
    v_owner bigint;
    v_status text;
    v_identity_account bigint;
BEGIN
    SELECT * INTO a FROM customer_accounts_customerloginattempt
     WHERE browser_hash = p_browser_hash AND p_browser_hash <> ''
     FOR UPDATE;
    IF NOT FOUND OR a.status IN ('completed', 'failed') THEN
        RETURN QUERY SELECT 'invalid'::text, NULL::bigint; RETURN;
    END IF;
    IF a.expires_at <= now() THEN
        UPDATE customer_accounts_customerloginattempt
           SET status = 'failed', failure = 'expired' WHERE id = a.id;
        RETURN QUERY SELECT 'expired'::text, NULL::bigint; RETURN;
    END IF;
    IF a.status <> 'code_sent' THEN
        RETURN QUERY SELECT 'not_ready'::text, NULL::bigint; RETURN;
    END IF;
    IF a.code_tries >= p_max_tries THEN
        UPDATE customer_accounts_customerloginattempt
           SET status = 'failed', failure = 'locked' WHERE id = a.id;
        RETURN QUERY SELECT 'locked'::text, NULL::bigint; RETURN;
    END IF;
    IF p_code IS NULL OR p_code !~ '^[0-9]{6}$'
       OR encode(sha256(convert_to(a.token_hash || ':' || p_code, 'UTF8')), 'hex')
          IS DISTINCT FROM a.code_hash
    THEN
        IF a.code_tries + 1 >= p_max_tries THEN
            UPDATE customer_accounts_customerloginattempt
               SET code_tries = a.code_tries + 1, status = 'failed', failure = 'locked'
             WHERE id = a.id;
            RETURN QUERY SELECT 'locked'::text, NULL::bigint; RETURN;
        END IF;
        UPDATE customer_accounts_customerloginattempt
           SET code_tries = a.code_tries + 1 WHERE id = a.id;
        RETURN QUERY SELECT 'wrong_code'::text, NULL::bigint; RETURN;
    END IF;

    SELECT i.account_id INTO v_identity_account
      FROM customer_accounts_customeridentity i
     WHERE i.provider = a.provider AND i.provider_user_id = a.provider_user_id;

    IF a.purpose = 'link' THEN
        v_owner := customer_account_session_account(coalesce(p_current_session_hash, ''));
        IF v_owner IS NULL OR v_owner <> a.account_id THEN
            RETURN QUERY SELECT 'invalid'::text, NULL::bigint; RETURN;
        END IF;
        IF v_identity_account IS NOT NULL AND v_identity_account <> v_owner THEN
            UPDATE customer_accounts_customerloginattempt
               SET status = 'failed', failure = 'conflict' WHERE id = a.id;
            RETURN QUERY SELECT 'conflict'::text, NULL::bigint; RETURN;
        END IF;
        IF v_identity_account IS NULL THEN
            IF EXISTS (SELECT 1 FROM customer_accounts_customeridentity
                        WHERE customer_accounts_customeridentity.account_id = v_owner
                          AND provider = a.provider) THEN
                UPDATE customer_accounts_customerloginattempt
                   SET status = 'failed', failure = 'conflict' WHERE id = a.id;
                RETURN QUERY SELECT 'conflict'::text, NULL::bigint; RETURN;
            END IF;
            BEGIN
                INSERT INTO customer_accounts_customeridentity
                    (account_id, provider, provider_user_id, display_name, verified_at, created_at)
                VALUES (v_owner, a.provider, a.provider_user_id,
                        left(a.display_name, 160), now(), now());
            EXCEPTION WHEN unique_violation THEN
                UPDATE customer_accounts_customerloginattempt
                   SET status = 'failed', failure = 'conflict' WHERE id = a.id;
                RETURN QUERY SELECT 'conflict'::text, NULL::bigint; RETURN;
            END;
            INSERT INTO customer_accounts_customeraccountevent
                (account_id, kind, detail, actor_user_id, created_at)
            VALUES (v_owner, 'identity_linked',
                    jsonb_build_object('provider', a.provider), NULL, now());
            PERFORM customer_account_claim(v_owner, a.provider, a.provider_user_id);
        END IF;
        v_account := v_owner;
    ELSE
        IF v_identity_account IS NOT NULL THEN
            SELECT status INTO v_status FROM customer_accounts_customeraccount
             WHERE id = v_identity_account;
            IF v_status <> 'active' THEN
                UPDATE customer_accounts_customerloginattempt
                   SET status = 'failed', failure = 'deactivated' WHERE id = a.id;
                RETURN QUERY SELECT 'deactivated'::text, NULL::bigint; RETURN;
            END IF;
            v_account := v_identity_account;
        ELSE
            INSERT INTO customer_accounts_customeraccount
                (public_id, display_name, status, created_at, updated_at)
            VALUES (gen_random_uuid(), left(a.display_name, 120), 'active', now(), now())
            RETURNING id INTO v_account;
            INSERT INTO customer_accounts_customeraccountevent
                (account_id, kind, detail, actor_user_id, created_at)
            VALUES (v_account, 'created', jsonb_build_object('provider', a.provider), NULL, now());
            BEGIN
                INSERT INTO customer_accounts_customeridentity
                    (account_id, provider, provider_user_id, display_name, verified_at, created_at)
                VALUES (v_account, a.provider, a.provider_user_id,
                        left(a.display_name, 160), now(), now());
            EXCEPTION WHEN unique_violation THEN
                -- A concurrent completion created it first: use that account
                -- and drop the one this call made.
                DELETE FROM customer_accounts_customeraccountevent
                 WHERE customer_accounts_customeraccountevent.account_id = v_account;
                DELETE FROM customer_accounts_customeraccount WHERE id = v_account;
                SELECT i.account_id INTO v_account
                  FROM customer_accounts_customeridentity i
                 WHERE i.provider = a.provider AND i.provider_user_id = a.provider_user_id;
            END;
            IF NOT EXISTS (SELECT 1 FROM customer_accounts_customeraccountevent e
                            WHERE e.account_id = v_account AND e.kind = 'identity_linked') THEN
                INSERT INTO customer_accounts_customeraccountevent
                    (account_id, kind, detail, actor_user_id, created_at)
                VALUES (v_account, 'identity_linked',
                        jsonb_build_object('provider', a.provider), NULL, now());
            END IF;
            PERFORM customer_account_claim(v_account, a.provider, a.provider_user_id);
        END IF;
        INSERT INTO customer_accounts_customersession
            (account_id, token_hash, created_at, expires_at, revoked_at)
        VALUES (v_account, p_new_session_hash, now(),
                now() + make_interval(days => p_session_days), NULL);
        UPDATE customer_accounts_customeraccount
           SET last_login_at = now(), updated_at = now() WHERE id = v_account;
        INSERT INTO customer_accounts_customeraccountevent
            (account_id, kind, detail, actor_user_id, created_at)
        VALUES (v_account, 'login', jsonb_build_object('provider', a.provider), NULL, now());
    END IF;

    UPDATE customer_accounts_customerloginattempt
       SET status = 'completed', completed_at = now(), code_hash = ''
     WHERE id = a.id;
    -- The code already did its job; do not leave it readable in the outbox.
    UPDATE customer_requests_maxmessage
       SET text = 'Код для входа в кабинет PRO-STOR использован.'
     WHERE dedupe_key LIKE 'account-code:' || left(a.token_hash, 32) || ':%';
    RETURN QUERY SELECT 'ok'::text, v_account;
END
$$;
"""

VIEWS = r"""
CREATE OR REPLACE VIEW customer_account_requests WITH (security_barrier) AS
    SELECT r.id, r.public_id, r.human_number, r.status, r.preferred_messenger,
           r.created_at, r.customer_account_id
      FROM customer_requests_customerrequest r
     WHERE r.customer_account_id = customer_account_current();

CREATE OR REPLACE VIEW customer_account_request_lines WITH (security_barrier) AS
    SELECT l.id, l.request_id, l.article, l.part_name, l.quantity_requested,
           l.unit_short_name, l.price_seen, l.is_supply_inquiry
      FROM customer_requests_customerrequestline l
      JOIN customer_requests_customerrequest r ON r.id = l.request_id
     WHERE r.customer_account_id = customer_account_current();

CREATE OR REPLACE VIEW customer_account_sales WITH (security_barrier) AS
    SELECT s.id, s.number, s.sold_at
      FROM sales_sale s
     WHERE s.status = 'completed'
       AND s.customer_id = customer_account_current_customer();

CREATE OR REPLACE VIEW customer_account_sale_lines WITH (security_barrier) AS
    SELECT sl.id, sl.sale_id, sl.part_type_id, sl.quantity, sl.unit_price, sl.total_price
      FROM sales_saleline sl
      JOIN sales_sale s ON s.id = sl.sale_id
     WHERE s.status = 'completed'
       AND s.customer_id = customer_account_current_customer();
"""

REVOKE = r"""
REVOKE ALL ON FUNCTION customer_account_session_account(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION customer_account_current() FROM PUBLIC;
REVOKE ALL ON FUNCTION customer_account_current_customer() FROM PUBLIC;
REVOKE ALL ON FUNCTION customer_account_logout(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION customer_account_claim(bigint, text, bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION customer_account_complete_attempt(
    text, text, text, text, integer, integer) FROM PUBLIC;
"""

DROP = r"""
DROP VIEW IF EXISTS customer_account_sale_lines;
DROP VIEW IF EXISTS customer_account_sales;
DROP VIEW IF EXISTS customer_account_request_lines;
DROP VIEW IF EXISTS customer_account_requests;
DROP FUNCTION IF EXISTS customer_account_complete_attempt(text, text, text, text, integer, integer);
DROP FUNCTION IF EXISTS customer_account_claim(bigint, text, bigint);
DROP FUNCTION IF EXISTS customer_account_logout(text);
DROP FUNCTION IF EXISTS customer_account_current_customer();
DROP FUNCTION IF EXISTS customer_account_current();
DROP FUNCTION IF EXISTS customer_account_session_account(text);
"""


def create(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    # params=None: the SQL carries literal ``%`` (``%ROWTYPE``, ``LIKE ':%'``)
    # that must not be read as placeholders.
    schema_editor.execute(FUNCTIONS, params=None)
    schema_editor.execute(VIEWS, params=None)
    schema_editor.execute(REVOKE, params=None)


def drop(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(DROP, params=None)


class Migration(migrations.Migration):
    dependencies = [
        ("customer_accounts", "0001_initial"),
        ("customer_requests", "0015_customerrequest_customer_account"),
        ("sales", "0007_saleline_unmarked_price_snapshot"),
    ]
    operations = [migrations.RunPython(create, drop)]
