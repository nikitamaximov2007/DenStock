"""Заполнить поисковую форму телефона у уже существующих ремонтных заказов.

Видимый телефон не трогаем: пересчитывается только служебная колонка поиска.
"""
from django.db import migrations

from apps.core.phones import normalize_phone

BATCH = 500


def forwards(apps, schema_editor):
    model = apps.get_model("repairs", "RepairOrder")
    updated = []
    for obj in model.objects.exclude(customer_phone="").only("pk", "customer_phone"):
        normalized = normalize_phone(obj.customer_phone)
        if normalized:
            obj.customer_phone_normalized = normalized
            updated.append(obj)
    if updated:
        model.objects.bulk_update(updated, ["customer_phone_normalized"], batch_size=BATCH)
    print(f"  0004_backfill_customer_phone_normalized: RepairOrder обновлено {len(updated)}")


def backwards(apps, schema_editor):
    apps.get_model("repairs", "RepairOrder").objects.update(customer_phone_normalized="")


class Migration(migrations.Migration):

    dependencies = [
        ("repairs", "0003_repairorder_customer_phone_normalized"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
