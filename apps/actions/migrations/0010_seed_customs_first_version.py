"""Перенести уже введённые таможенные карточки в версию 1.

Исторический экспорт берёт факты из версий, а не из текущей карточки. Без
этого переноса всё, что пользователь заполнил ДО появления версий, молча
исчезло бы из таможенного Excel. Данные карточки — это тот же ручной ввод
пользователя, поэтому перенос ничего не выдумывает: значения копируются как
есть, один в один.

Версия 1 намеренно покрывает и более ранние движения: карточка заполнялась
после того, как деталь начала расходоваться.
"""
from django.db import migrations


def seed_first_version(apps, schema_editor):
    PartCustomsInfo = apps.get_model("actions", "PartCustomsInfo")
    PartCustomsDataVersion = apps.get_model("actions", "PartCustomsDataVersion")
    fields = (
        "customs_name_ru", "customs_name_en", "manufacturer", "country_of_origin",
        "gross_weight_kg", "net_weight_kg", "customs_unit_price_usd",
        "application_area", "source_reference",
    )
    existing = set(
        PartCustomsDataVersion.objects.values_list("part_type_id", flat=True)
    )
    versions = [
        PartCustomsDataVersion(
            part_type_id=card.part_type_id,
            version=1,
            effective_from=card.updated_at,
            created_by_id=card.updated_by_id,
            **{field: getattr(card, field) for field in fields},
        )
        for card in PartCustomsInfo.objects.all()
        if card.part_type_id not in existing
    ]
    PartCustomsDataVersion.objects.bulk_create(versions, batch_size=500)


def drop_seeded_versions(apps, schema_editor):
    # Откат снимает ТОЛЬКО первую версию: правки, сделанные после миграции,
    # остаются историей и удалению не подлежат.
    PartCustomsDataVersion = apps.get_model("actions", "PartCustomsDataVersion")
    PartCustomsDataVersion.objects.filter(version=1).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("actions", "0009_customs_historical_profiles"),
    ]

    operations = [
        migrations.RunPython(seed_first_version, drop_seeded_versions),
    ]
