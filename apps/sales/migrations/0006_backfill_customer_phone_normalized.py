"""Заполнить поисковую форму телефона у уже существующих броней и продаж.

Видимый телефон не трогаем: пересчитываем только служебную колонку поиска,
чтобы старые документы находились по номеру так же, как новые.
"""
from django.db import migrations

from apps.core.phones import normalize_phone

BATCH = 500


def _backfill(model):
    updated = []
    for obj in model.objects.exclude(customer_phone="").only("pk", "customer_phone"):
        normalized = normalize_phone(obj.customer_phone)
        if normalized:
            obj.customer_phone_normalized = normalized
            updated.append(obj)
    if updated:
        model.objects.bulk_update(updated, ["customer_phone_normalized"], batch_size=BATCH)
    return len(updated)


def forwards(apps, schema_editor):
    for label in ("Reservation", "Sale"):
        model = apps.get_model("sales", label)
        count = _backfill(model)
        print(f"  0006_backfill_customer_phone_normalized: {label} обновлено {count}")


def backwards(apps, schema_editor):
    # Служебная колонка вычисляется из видимого телефона: чистим без потерь.
    for label in ("Reservation", "Sale"):
        apps.get_model("sales", label).objects.update(customer_phone_normalized="")


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0005_reservation_customer_phone_normalized_and_more"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
