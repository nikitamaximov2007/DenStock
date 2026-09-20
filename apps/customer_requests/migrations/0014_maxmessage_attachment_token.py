from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("customer_requests", "0013_private_message_attachment_storage"),
    ]

    operations = [
        migrations.AddField(
            model_name="maxmessage",
            name="max_attachment_token",
            field=models.CharField(
                blank=True, max_length=512, verbose_name="Токен вложения MAX"
            ),
        ),
    ]
