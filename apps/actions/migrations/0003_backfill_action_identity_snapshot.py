"""Backfill снимков личности для уже созданных действий (hotfix identity).

Существующие продажи показывали номер-замену (ANALOG) из-за сортировки
PartNumber. Здесь snapshot part_number берётся из ОСНОВНОГО номера детали
(is_primary, затем pk) — это OEM/material_no, а не аналог; part_name и
location_code — из связанных объектов. Склад не трогается.
"""
from django.db import migrations


def backfill(apps, schema_editor):
    WarehouseAction = apps.get_model("actions", "WarehouseAction")
    PartNumber = apps.get_model("catalog", "PartNumber")
    for action in WarehouseAction.objects.select_related("part_type", "location"):
        changed = []
        if not action.part_number:
            primary = (
                PartNumber.objects.filter(part_id=action.part_type_id)
                .order_by("-is_primary", "pk")
                .first()
            )
            action.part_number = primary.value if primary else ""
            changed.append("part_number")
        if not action.part_name:
            action.part_name = action.part_type.name
            changed.append("part_name")
        if not action.location_code:
            action.location_code = action.location.code
            changed.append("location_code")
        if changed:
            action.save(update_fields=changed)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("actions", "0002_warehouseaction_cancel_reason_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, noop),
    ]
