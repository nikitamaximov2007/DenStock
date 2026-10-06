from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0017_stocklot_origin_return_line"),
    ]

    operations = [
        migrations.AddField(
            model_name="stocklot",
            name="creation_origin",
            field=models.CharField(
                blank=True,
                choices=[
                    ("supplier_pending", "Создан для приёмки"),
                    ("supplier_received", "Принят от поставщика"),
                    ("transfer", "Создан перемещением"),
                    ("return", "Создан возвратом"),
                    ("found", "Создан найденным остатком"),
                    ("recount", "Создан пересчётом"),
                ],
                editable=False,
                max_length=20,
                null=True,
                verbose_name="Путь создания лота",
            ),
        ),
    ]
