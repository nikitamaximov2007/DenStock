from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("customer_requests", "0020_operator_context_safety"),
    ]

    operations = [
        migrations.AddField(
            model_name="staffmessengerpairingtoken",
            name="telegram_consumed_at",
            field=models.DateTimeField(
                blank=True, null=True, verbose_name="Telegram подключён"
            ),
        ),
        migrations.AddField(
            model_name="staffmessengerpairingtoken",
            name="max_consumed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="MAX подключён"),
        ),
        migrations.AlterField(
            model_name="staffmessengerpairingtoken",
            name="provider",
            field=models.CharField(
                blank=True,
                choices=[("telegram", "Telegram"), ("max", "MAX")],
                default="",
                max_length=12,
                verbose_name="Мессенджер",
            ),
        ),
    ]
