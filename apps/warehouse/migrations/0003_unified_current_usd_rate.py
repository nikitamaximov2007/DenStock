from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def _decimal(value):
    return Decimal(str(value))


def _unify_current_rate(apps, schema_editor):
    ValuationSettings = apps.get_model("warehouse", "ValuationSettings")
    BrpPricingSettings = apps.get_model("brp", "BrpPricingSettings")
    PolarisPricingSettings = apps.get_model("polaris", "PolarisPricingSettings")

    valuation = ValuationSettings.objects.filter(pk=1).first()
    base_rate = valuation.current_usd_rate if valuation else Decimal("105")
    rates = [("warehouse", base_rate)]

    brp = BrpPricingSettings.objects.filter(pk=1).first()
    if brp is not None:
        rates.append(("brp", brp.brp_usd_rate))

    polaris = PolarisPricingSettings.objects.filter(pk=1).first()
    if polaris is not None:
        rates.append(("polaris", polaris.polaris_usd_rate))

    conflicts = [(name, value) for name, value in rates if _decimal(value) != _decimal(base_rate)]
    if conflicts:
        formatted = ", ".join(f"{name}={value}" for name, value in rates)
        raise RuntimeError(
            "Cannot unify current USD rate automatically because existing rates differ: "
            f"{formatted}. Set them equal before applying this migration."
        )

    if valuation is None:
        ValuationSettings.objects.create(pk=1, current_usd_rate=base_rate)


def _noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("warehouse", "0002_valuationsettings"),
        ("brp", "0001_initial"),
        ("polaris", "0001_initial"),
    ]

    operations = [
        migrations.RenameField(
            model_name="valuationsettings",
            old_name="purchase_usd_rate",
            new_name="current_usd_rate",
        ),
        migrations.AlterModelOptions(
            name="valuationsettings",
            options={"verbose_name": "Настройки цен", "verbose_name_plural": "Настройки цен"},
        ),
        migrations.AlterField(
            model_name="valuationsettings",
            name="current_usd_rate",
            field=models.DecimalField(
                decimal_places=4,
                default=Decimal("105"),
                max_digits=10,
                verbose_name="Текущий курс доллара (₽ за $)",
            ),
        ),
        migrations.AddField(
            model_name="valuationsettings",
            name="updated_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Кто изменил",
            ),
        ),
        migrations.RunPython(_unify_current_rate, _noop),
    ]
