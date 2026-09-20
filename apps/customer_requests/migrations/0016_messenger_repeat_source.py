from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("customer_requests", "0015_customerrequest_customer_account")]

    operations = [
        migrations.AlterField(
            model_name="customerrequest",
            name="source",
            field=models.CharField(
                choices=[
                    ("public_catalog", "Публичный каталог"),
                    ("messenger_repeat", "Повтор покупки через мессенджер"),
                ],
                default="public_catalog",
                max_length=30,
                verbose_name="Источник",
            ),
        ),
    ]
