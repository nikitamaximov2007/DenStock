-- Privileges of the database role used by the public catalog runtime (catalog-web).
--
-- Run as the database owner during deployment, never as catalog-web, and
-- only AFTER the privileged release job has applied migrations. It is
-- idempotent: re-running it after an upgrade re-derives the exact grant set.
-- catalog-web receives only the resulting PUBLIC_DATABASE_URL.
--
--   psql -v ON_ERROR_STOP=1 -v public_role=denstock_public -f create_public_catalog_role.sql
--
-- public_role defaults to denstock_public; the preview uses its own role name.
-- The deployment identity provisions the LOGIN role and its password
-- separately, through a secure channel (for example \password). This file
-- creates no role or database: it only narrows the role's attributes and
-- sets its privileges and session defaults in the connected database.
--
-- The command is database-name independent: acceptance and disaster recovery
-- use isolated database names, while production uses ``denstock``. psql must be
-- connected to the database being configured.

\if :{?public_role}
\else
\set public_role denstock_public
\endif
SELECT set_config('denstock.public_role', :'public_role', false);

DO $$
DECLARE
    role_name text := current_setting('denstock.public_role');
BEGIN
    IF role_name !~ '^[a-z_][a-z0-9_]*$' THEN
        RAISE EXCEPTION 'unsafe role name %', role_name;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = role_name) THEN
        RAISE EXCEPTION 'role % does not exist; the deployment identity creates it first', role_name;
    END IF;
    -- Only ever narrows: the web role is never privileged.
    EXECUTE format('ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION', role_name);
    EXECUTE format('ALTER ROLE %I CONNECTION LIMIT 20', role_name);

    -- Session defaults in this database only. Every transaction starts
    -- read-only; the single write path (a new customer request) opens its
    -- transaction with an explicit SET TRANSACTION READ WRITE, so no page read
    -- can write by accident. A runaway query is cut off and an idle
    -- transaction cannot hold locks.
    EXECUTE format('ALTER ROLE %I IN DATABASE %I SET default_transaction_read_only = on',
        role_name, current_database());
    EXECUTE format('ALTER ROLE %I IN DATABASE %I SET statement_timeout = %L',
        role_name, current_database(), '5s');
    EXECUTE format('ALTER ROLE %I IN DATABASE %I SET idle_in_transaction_session_timeout = %L',
        role_name, current_database(), '30s');

    EXECUTE format('REVOKE ALL ON DATABASE %I FROM %I', current_database(), role_name);
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), role_name);
    EXECUTE format('REVOKE ALL ON SCHEMA public FROM %I', role_name);
    EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', role_name);
    EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM %I', role_name);
    EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM %I', role_name);
    EXECUTE format('REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM %I', role_name);

    -- The exact read graph of the public pages. Anything not listed here is
    -- unreadable, including customers, sales, repairs, costs and users.
    EXECUTE format(
        'GRANT SELECT ON TABLE '
        -- Search 2.0 identities and public facts
        'catalog_parttype, catalog_partnumber, catalog_unit, catalog_manufacturer, '
        -- canonical source article (BRP / Polaris link)
        'brp_brppartlink, brp_brpcatalogpart, polaris_polarispartlink, polaris_polariscatalogpart, '
        -- confirmed Russian name and the explicit application area
        'actions_partcustomsinfo, '
        -- canonical availability (lots, serial items, reservations)
        'inventory_stocklot, inventory_partitem, procurement_batch, warehouse_storagelocation, '
        'sales_reservation, sales_reservationline, '
        -- confirmed analog relations (Stage 7)
        'catalog_partanalog, '
        -- application filter: explicit vehicle compatibility
        'catalog_partcompatibility, catalog_vehiclemodel, catalog_vehiclemake, catalog_vehicletype, '
        -- published photo metadata and re-encoded renditions (Stage 14, RLS below)
        'catalog_publicpartphoto, catalog_publicpartphotorendition '
        'TO %I',
        role_name
    );

    -- The one write: a new customer request and its line snapshots. INSERT
    -- only, no UPDATE or DELETE. Column-level SELECT covers exactly what the
    -- write needs (INSERT ... RETURNING id and the idempotency lookup by the
    -- submission key hash); names, phones and comments stay unreadable, so a
    -- compromised public process cannot list earlier requests. Identity
    -- columns need no sequence privilege.
    EXECUTE format(
        'GRANT INSERT ON TABLE customer_requests_customerrequest, '
        'customer_requests_customerrequestline TO %I',
        role_name
    );
    EXECUTE format(
        'GRANT SELECT (id, public_id, submission_key_hash) '
        'ON TABLE customer_requests_customerrequest TO %I',
        role_name
    );
    EXECUTE format('GRANT SELECT (id) ON TABLE customer_requests_customerrequestline TO %I', role_name);

    -- Customer requests are business data, so the global write guard wraps
    -- that INSERT: it checks the deployment write state (a frozen or failed-
    -- over database refuses requests) and bumps the business generation that
    -- backup and failback compare. Nothing else in that row is writable.
    EXECUTE format(
        'GRANT SELECT (id, write_state, business_generation) '
        'ON TABLE operations_deploymentstate TO %I',
        role_name
    );
    EXECUTE format(
        'GRANT UPDATE (business_generation) ON TABLE operations_deploymentstate TO %I',
        role_name
    );

    -- Photos: the public role can see a row only while it is published, even
    -- through direct SQL. A permissive policy keeps every other role (the
    -- internal runtime, backups, migrations) seeing all rows whether or not
    -- it owns the table; the restrictive policy narrows only the public role.
    -- Table GRANTs still decide who may read the tables at all.
    EXECUTE 'ALTER TABLE catalog_publicpartphoto ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE catalog_publicpartphotorendition ENABLE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS public_catalog_all_rows ON catalog_publicpartphoto';
    EXECUTE 'CREATE POLICY public_catalog_all_rows ON catalog_publicpartphoto '
        'AS PERMISSIVE FOR ALL TO PUBLIC USING (true) WITH CHECK (true)';
    EXECUTE 'DROP POLICY IF EXISTS public_catalog_all_rows ON catalog_publicpartphotorendition';
    EXECUTE 'CREATE POLICY public_catalog_all_rows ON catalog_publicpartphotorendition '
        'AS PERMISSIVE FOR ALL TO PUBLIC USING (true) WITH CHECK (true)';
    EXECUTE 'DROP POLICY IF EXISTS public_catalog_published_photos ON catalog_publicpartphoto';
    EXECUTE format(
        'CREATE POLICY public_catalog_published_photos ON catalog_publicpartphoto '
        'AS RESTRICTIVE FOR SELECT TO %I USING (status = %L)',
        role_name, 'published'
    );
    EXECUTE 'DROP POLICY IF EXISTS public_catalog_published_renditions ON catalog_publicpartphotorendition';
    EXECUTE format(
        'CREATE POLICY public_catalog_published_renditions ON catalog_publicpartphotorendition '
        'AS RESTRICTIVE FOR SELECT TO %I USING (EXISTS (SELECT 1 FROM catalog_publicpartphoto photo '
        'WHERE photo.id = catalog_publicpartphotorendition.photo_id AND photo.status = %L))',
        role_name, 'published'
    );

    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM %I',
        current_user, role_name
    );
END
$$;
