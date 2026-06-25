from django.db import migrations


def seed(apps, schema_editor):
    NumberSequence = apps.get_model("inventory", "NumberSequence")
    NumberSequence.objects.get_or_create(
        key="sale", defaults={"prefix": "S-", "last_value": 0}
    )


def unseed(apps, schema_editor):
    NumberSequence = apps.get_model("inventory", "NumberSequence")
    NumberSequence.objects.filter(key="sale").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0003_sale_saleline"),
        ("inventory", "0002_seed_number_sequence"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
