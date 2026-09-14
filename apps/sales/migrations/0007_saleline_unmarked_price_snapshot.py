from decimal import Decimal, ROUND_HALF_UP

from django.db import migrations, models
from django.db.models import Q


RATE = Decimal("105")


def _whole_rub(value):
    return (value * RATE).quantize(Decimal("1"), rounding=ROUND_HALF_UP)


def backfill_owner_approved_unmarked_prices(apps, schema_editor):
    """Owner-approved cutover reconstruction.  Never changes sale prices/totals."""
    SaleLine = apps.get_model("sales", "SaleLine")
    BrpLink = apps.get_model("brp", "BrpPartLink")
    BrpCatalogPart = apps.get_model("brp", "BrpCatalogPart")
    PolarisLink = apps.get_model("polaris", "PolarisPartLink")
    Aftermarket = apps.get_model("catalog_import", "AftermarketCatalogPart")
    Arctic = apps.get_model("catalog_import", "ArcticCatCatalogPart")
    lines = SaleLine.objects.filter(sale__status="completed")
    for line in lines.iterator():
        part_id = line.part_type_id
        source = usd = None
        note = "no_authoritative_link"
        brp = BrpLink.objects.filter(part_id=part_id).select_related("brp_part").first()
        if brp:
            selected = brp.brp_part
            raw = selected.wholesale_price_usd
            # Keep the historical migration self-contained, but use the same
            # direct/replacement source rule as the live BRP price resolver.
            if (raw is None or raw <= 0) and selected.is_current:
                related = (
                    Q(material_no_norm=selected.material_no_norm)
                    | Q(replacement_no_1_norm=selected.material_no_norm)
                    | Q(replacement_no_2_norm=selected.material_no_norm)
                )
                if selected.replacement_no_1_norm:
                    related |= Q(material_no_norm=selected.replacement_no_1_norm)
                if selected.replacement_no_2_norm:
                    related |= Q(material_no_norm=selected.replacement_no_2_norm)
                replacement = BrpCatalogPart.objects.filter(
                    is_current=True, wholesale_price_usd__gt=0
                ).filter(related).order_by("pk").first()
                raw = replacement.wholesale_price_usd if replacement else raw
                status = replacement.brp_status if replacement else selected.brp_status
            else:
                status = selected.brp_status
            if raw is not None and raw > 0 and selected.is_current:
                usd = raw + (Decimal("25") if status == "VIN" else Decimal("0"))
                source, note = "brp", "legacy_reconstruction_105"
            else:
                note = "brp_wholesale_missing"
        else:
            polaris = (
                PolarisLink.objects.filter(part_id=part_id).select_related("polaris_part").first()
            )
            if polaris:
                raw = polaris.polaris_part.wholesale_price_usd
                if raw is not None and raw > 0:
                    usd, source, note = raw, "polaris", "legacy_reconstruction_105"
                else:
                    note = "polaris_wholesale_missing"
            else:
                aftermarket = Aftermarket.objects.filter(part_id=part_id).first()
                if aftermarket:
                    raw = aftermarket.dealer_cost_usd
                    if raw is not None and raw > 0:
                        usd, source, note = raw, "aftermarket", "legacy_reconstruction_105"
                    else:
                        note = "aftermarket_dealer_cost_missing"
                else:
                    arctic = Arctic.objects.filter(part_id=part_id).first()
                    if arctic:
                        raw = arctic.dealer_price_usd
                        if raw is not None and raw > 0:
                            usd, source, note = raw, "arctic_cat", "legacy_reconstruction_105"
                        else:
                            note = "arctic_dealer_price_missing"
        if usd is not None:
            line.unmarked_unit_price_rub_snapshot = _whole_rub(usd)
            line.unmarked_dealer_unit_usd_snapshot = usd
            line.unmarked_usd_rate_snapshot = RATE
            line.unmarked_price_source = source
        line.unmarked_price_snapshot_note = note
        line.save(update_fields=[
            "unmarked_unit_price_rub_snapshot", "unmarked_dealer_unit_usd_snapshot",
            "unmarked_usd_rate_snapshot", "unmarked_price_source", "unmarked_price_snapshot_note",
        ])


class Migration(migrations.Migration):
    dependencies = [
        ("sales", "0006_sale_canceled_by_sale_cancellation_author_and_more"),
        ("brp", "0001_initial"),
        ("polaris", "0001_initial"),
        ("catalog_import", "0004_alter_catalogimportbatch_catalog_and_more"),
    ]
    operations = [
        migrations.AddField(
            model_name="saleline", name="unmarked_unit_price_rub_snapshot",
            field=models.DecimalField(blank=True, decimal_places=2, editable=False, max_digits=14,
                                      null=True, verbose_name="Немаркированная цена за ед. (снимок, ₽)"),
        ),
        migrations.AddField(
            model_name="saleline", name="unmarked_dealer_unit_usd_snapshot",
            field=models.DecimalField(blank=True, decimal_places=4, editable=False, max_digits=14,
                                      null=True, verbose_name="Дилерская цена за ед. (снимок, USD)"),
        ),
        migrations.AddField(
            model_name="saleline", name="unmarked_usd_rate_snapshot",
            field=models.DecimalField(blank=True, decimal_places=4, editable=False, max_digits=10,
                                      null=True, verbose_name="Курс для немаркированной цены (снимок)"),
        ),
        migrations.AddField(
            model_name="saleline", name="unmarked_price_source",
            field=models.CharField(blank=True, editable=False, max_length=20),
        ),
        migrations.AddField(
            model_name="saleline", name="unmarked_price_snapshot_note",
            field=models.CharField(blank=True, editable=False, max_length=80),
        ),
        migrations.RunPython(backfill_owner_approved_unmarked_prices, migrations.RunPython.noop),
    ]
