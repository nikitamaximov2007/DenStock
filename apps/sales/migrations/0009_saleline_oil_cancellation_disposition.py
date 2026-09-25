import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sales", "0008_saleline_oil_package_price_rub_snapshot_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="SaleOilCancellationDecision",
            fields=[
                (
                    "id",
                    models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
                ),
                (
                    "disposition",
                    models.CharField(
                        choices=[
                            ("return_to_stock", "Вернуть объём на склад"),
                            ("do_not_return", "Не возвращать - масло уже выдано/использовано"),
                        ],
                        max_length=20,
                        verbose_name="Решение по отмене масла",
                    ),
                ),
                ("decided_at", models.DateTimeField(auto_now_add=True, verbose_name="Решено (когда)")),
                (
                    "decided_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="sale_oil_cancellation_decisions",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Кто решил",
                    ),
                ),
                (
                    "sale_line",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="oil_cancellation_decision",
                        to="sales.saleline",
                        verbose_name="Строка продажи",
                    ),
                ),
            ],
            options={
                "verbose_name": "Решение по отмене масла",
                "verbose_name_plural": "Решения по отмене масла",
            },
        ),
    ]
