from django.db import migrations

UNITS = [
    ("Штука", "шт"),
    ("Комплект", "компл"),
    ("Метр", "м"),
    ("Килограмм", "кг"),
    ("Литр", "л"),
    ("Упаковка", "упак"),
]

VEHICLE_TYPES = [
    ("Автомобиль", 1),
    ("Снегоход", 2),
    ("Квадроцикл", 3),
    ("Катер", 4),
    ("Яхта", 5),
]


def seed(apps, schema_editor):
    Unit = apps.get_model("catalog", "Unit")
    VehicleType = apps.get_model("catalog", "VehicleType")
    for name, short in UNITS:
        Unit.objects.get_or_create(name=name, defaults={"short_name": short})
    for name, order in VEHICLE_TYPES:
        VehicleType.objects.get_or_create(name=name, defaults={"sort_order": order})


def unseed(apps, schema_editor):
    Unit = apps.get_model("catalog", "Unit")
    VehicleType = apps.get_model("catalog", "VehicleType")
    Unit.objects.filter(name__in=[u[0] for u in UNITS]).delete()
    VehicleType.objects.filter(name__in=[t[0] for t in VEHICLE_TYPES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("catalog", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
