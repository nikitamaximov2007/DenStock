-- Restricted database role for the public catalog runtime (catalog-web).
--
-- Run as the database owner during deployment, never as catalog-web, and
-- only AFTER the privileged release job has applied migrations. It is
-- idempotent: re-running it after an upgrade re-derives the exact grant set.
-- catalog-web receives only the resulting PUBLIC_DATABASE_URL.
--
--   psql -v ON_ERROR_STOP=1 -v public_role=denstock_public -f create_public_catalog_role.sql
--
-- public_role defaults to denstock_public; the preview uses its own role name.
-- Set the password separately through a secure channel, for example
-- ALTER ROLE ... PASSWORD entered interactively with \password.
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
        EXECUTE format(
            'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION',
            role_name
        );
    END IF;
    EXECUTE format('ALTER ROLE %I NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION', role_name);

    -- Defence in depth for a read-only web process on a shared database:
    -- every session starts read-only, a runaway query is cut off, an idle
    -- transaction cannot hold locks, and the role cannot exhaust connections.
    -- The request stack integration, which needs exact request-domain
    -- INSERTs, must replace the read-only default with its own narrow grants.
    EXECUTE format('ALTER ROLE %I SET default_transaction_read_only = on', role_name);
    EXECUTE format('ALTER ROLE %I SET statement_timeout = %L', role_name, '5s');
    EXECUTE format('ALTER ROLE %I SET idle_in_transaction_session_timeout = %L', role_name, '30s');
    EXECUTE format('ALTER ROLE %I CONNECTION LIMIT 20', role_name);

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

    -- Photos: the role can see a row only while it is published. The owner
    -- (internal runtime, migrations, backups) is unaffected; RLS applies to
    -- other roles only.
    EXECUTE 'ALTER TABLE catalog_publicpartphoto ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE catalog_publicpartphotorendition ENABLE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS public_catalog_published_photos ON catalog_publicpartphoto';
    EXECUTE format(
        'CREATE POLICY public_catalog_published_photos ON catalog_publicpartphoto '
        'FOR SELECT TO %I USING (status = %L)',
        role_name, 'published'
    );
    EXECUTE 'DROP POLICY IF EXISTS public_catalog_published_renditions ON catalog_publicpartphotorendition';
    EXECUTE format(
        'CREATE POLICY public_catalog_published_renditions ON catalog_publicpartphotorendition '
        'FOR SELECT TO %I USING (EXISTS (SELECT 1 FROM catalog_publicpartphoto photo '
        'WHERE photo.id = catalog_publicpartphotorendition.photo_id AND photo.status = %L))',
        role_name, 'published'
    );

    EXECUTE format(
        'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public REVOKE ALL ON TABLES FROM %I',
        current_user, role_name
    );
END
$$;
