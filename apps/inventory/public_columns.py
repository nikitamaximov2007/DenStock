"""Columns the public catalog database role may NOT read (least privilege).

``scripts/operations/create_public_catalog_role.sql`` grants SELECT on every
column of these tables EXCEPT the ones listed here; a test keeps both lists
equal. Code that runs under the public role and loads rows of these tables
defers exactly these fields, so a compromised catalog-web cannot read
purchase costs, supplier prices, customs values, staff ids, internal notes or
the reserving customer's name and phone.
"""

HIDDEN_COLUMNS: dict[str, tuple[str, ...]] = {
    "inventory_stocklot": ("landed_unit_cost_rub", "receipt_customer_price_snapshot_rub", "note"),
    "inventory_partitem": ("landed_cost_rub", "receipt_customer_price_snapshot_rub", "note"),
    "procurement_batch": (
        "supplier_id", "invoice_number", "notes", "shipping_cost", "customs_cost",
        "commission_cost", "other_cost", "total_extra_cost", "cost_allocation_method",
        "cost_finalized_at", "cost_finalized_by_id", "created_by_id",
    ),
    "sales_reservation": ("customer_id", "customer_name", "customer_phone", "comment",
                          "created_by_id"),
    "brp_brpcatalogpart": ("wholesale_price_usd",),
    "brp_brppartlink": ("brp_wholesale_price_usd", "usd_rate_used", "markup_percent_used",
                        "created_by_id"),
    "polaris_polariscatalogpart": ("wholesale_price_usd",),
    "polaris_polarispartlink": ("polaris_wholesale_price_usd", "usd_rate_used",
                                "markup_percent_used", "created_by_id"),
    "actions_partcustomsinfo": ("customs_unit_price_usd",),
}


def deferred_fields(model, *, prefix: str = "") -> list[str]:
    """ORM field names (for ``defer``) of the hidden columns of ``model``."""
    hidden = set(HIDDEN_COLUMNS.get(model._meta.db_table, ()))
    return [prefix + field.name for field in model._meta.concrete_fields
            if field.column in hidden]


def identity_deferred_fields(prefix: str = "") -> list[str]:
    """Hidden columns of the BRP/Polaris rows that ``with_part_identity`` joins."""
    from apps.brp.models import BrpCatalogPart, BrpPartLink
    from apps.polaris.models import PolarisCatalogPart, PolarisPartLink

    return [
        *deferred_fields(BrpPartLink, prefix=f"{prefix}brp_link__"),
        *deferred_fields(BrpCatalogPart, prefix=f"{prefix}brp_link__brp_part__"),
        *deferred_fields(PolarisPartLink, prefix=f"{prefix}polaris_link__"),
        *deferred_fields(PolarisCatalogPart, prefix=f"{prefix}polaris_link__polaris_part__"),
    ]
