import apps.customer_requests.storage
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("customer_requests", "0012_maxmessage_attachment_and_more")]

    operations = [
        migrations.AlterField(
            model_name="maxmessage",
            name="attachment",
            field=models.FileField(
                blank=True,
                storage=apps.customer_requests.storage.PrivateAttachmentStorage(),
                upload_to="customer_requests/",
                verbose_name="Вложение",
            ),
        ),
        migrations.AlterField(
            model_name="telegrammessage",
            name="attachment",
            field=models.FileField(
                blank=True,
                storage=apps.customer_requests.storage.PrivateAttachmentStorage(),
                upload_to="customer_requests/",
                verbose_name="Вложение",
            ),
        ),
    ]
