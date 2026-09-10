from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventory", "0014_alter_stockmovement_movement_type")]

    operations = [
        migrations.AddField(
            model_name="partitem", name="receipt_customer_price_rub",
            field=models.DecimalField(blank=True, decimal_places=2, editable=False, max_digits=12, null=True, verbose_name="Рекомендованная цена при приёмке (₽)"),
        ),
        migrations.AddField(
            model_name="stocklot", name="receipt_customer_price_rub",
            field=models.DecimalField(blank=True, decimal_places=2, editable=False, max_digits=12, null=True, verbose_name="Рекомендованная цена при приёмке (₽)"),
        ),
    ]
