from django.db import migrations


def seed(apps, schema_editor):
    NumberSequence = apps.get_model("inventory", "NumberSequence")
    NumberSequence.objects.get_or_create(
        key="repair_order", defaults={"prefix": "R-", "last_value": 0}
    )


def unseed(apps, schema_editor):
    NumberSequence = apps.get_model("inventory", "NumberSequence")
    NumberSequence.objects.filter(key="repair_order").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("repairs", "0001_initial"),
        ("inventory", "0002_seed_number_sequence"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
