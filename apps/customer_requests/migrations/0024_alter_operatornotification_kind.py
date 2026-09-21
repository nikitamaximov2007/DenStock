from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("customer_requests", "0023_owner_panel_notification"),
    ]

    operations = [
        migrations.AlterField(
            model_name="operatornotification",
            name="kind",
            field=models.CharField(
                choices=[
                    ("new_request", "Новая заявка"),
                    ("customer_message", "Сообщение клиента"),
                    ("owner_panel", "Панель владельца"),
                ],
                max_length=24,
                verbose_name="Событие",
            ),
        ),
    ]
