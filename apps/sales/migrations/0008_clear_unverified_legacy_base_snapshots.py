from django.db import migrations


def clear_unverified_legacy_base_snapshots(apps, schema_editor):
    SaleLine = apps.get_model("sales", "SaleLine")
    SaleLine.objects.filter(
        unmarked_price_snapshot_note__startswith="legacy_reconstruction_"
    ).update(
        unmarked_unit_price_rub_snapshot=None,
        unmarked_dealer_unit_usd_snapshot=None,
        unmarked_usd_rate_snapshot=None,
        unmarked_price_source="",
        unmarked_price_snapshot_note="historical_base_unknown",
    )


class Migration(migrations.Migration):
    dependencies = [("sales", "0007_saleline_unmarked_price_snapshot")]

    operations = [
        migrations.RunPython(clear_unverified_legacy_base_snapshots, migrations.RunPython.noop),
    ]
