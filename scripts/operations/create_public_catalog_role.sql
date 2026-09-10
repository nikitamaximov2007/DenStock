-- Run as the database owner during deployment, never as catalog-web.
-- Replace the password through a secure psql variable or deployment secret.
-- catalog-web receives only the resulting PUBLIC_DATABASE_URL.

CREATE ROLE denstock_public LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
REVOKE ALL ON DATABASE denstock FROM denstock_public;
GRANT CONNECT ON DATABASE denstock TO denstock_public;
REVOKE ALL ON SCHEMA public FROM denstock_public;
GRANT USAGE ON SCHEMA public TO denstock_public;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM denstock_public;
GRANT SELECT ON TABLE
    catalog_parttype,
    catalog_partnumber,
    catalog_unit,
    catalog_manufacturer,
    actions_partcustomsinfo,
    inventory_stocklot,
    inventory_partitem,
    sales_reservation,
    sales_reservationline
TO denstock_public;

REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM denstock_public;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public
    REVOKE ALL ON TABLES FROM denstock_public;
