-- Run as the database owner during deployment, never as catalog-web.
-- The deployment identity must provision ``denstock_public`` separately.
-- This file deliberately creates no roles, databases, schemas, or privileges
-- outside the currently connected database.

-- The command is intentionally database-name independent: acceptance and
-- disaster recovery use isolated database names, while production uses
-- ``denstock``.  psql must be connected to the database being configured.
DO $$
BEGIN
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
GRANT SELECT, INSERT ON TABLE
    customer_requests_customerrequest,
    customer_requests_customerrequestline
TO denstock_public;
GRANT USAGE, SELECT ON SEQUENCE
    customer_requests_customerrequest_id_seq,
    customer_requests_customerrequestline_id_seq
TO denstock_public;
ALTER DEFAULT PRIVILEGES FOR ROLE CURRENT_USER IN SCHEMA public
    REVOKE ALL ON TABLES FROM denstock_public;
