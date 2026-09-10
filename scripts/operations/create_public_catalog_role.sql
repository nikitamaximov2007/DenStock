-- Run as the database owner during deployment, never as catalog-web.
-- Replace the password through a secure psql variable or deployment secret.
-- catalog-web receives only the resulting PUBLIC_DATABASE_URL.

-- The command is intentionally database-name independent: acceptance and
-- disaster recovery use isolated database names, while production uses
-- ``denstock``.  psql must be connected to the database being configured.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'denstock_public') THEN
        CREATE ROLE denstock_public LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
    EXECUTE format('REVOKE ALL ON DATABASE %I FROM denstock_public', current_database());
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO denstock_public', current_database());
END
$$;
REVOKE ALL ON SCHEMA public FROM denstock_public;
GRANT USAGE ON SCHEMA public TO denstock_public;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM denstock_public;
GRANT SELECT ON TABLE
    catalog_parttype,
    catalog_partnumber,
    catalog_unit,
    catalog_manufacturer,
    brp_brppartlink,
    brp_brpcatalogpart,
    polaris_polarispartlink,
    polaris_polariscatalogpart,
    actions_partcustomsinfo,
    inventory_stocklot,
    inventory_partitem,
    procurement_batch,
    warehouse_storagelocation,
    sales_reservation,
    sales_reservationline
TO denstock_public;

REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM denstock_public;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public
    REVOKE ALL ON TABLES FROM denstock_public;
