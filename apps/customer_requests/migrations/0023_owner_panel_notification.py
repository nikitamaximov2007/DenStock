from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("customer_requests", "0022_operator_identity_keys"),
    ]

    operations = [
        migrations.AlterField(
            model_name="operatornotification",
            name="request",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="operator_notifications",
                to="customer_requests.customerrequest",
                verbose_name="Заявка",
            ),
        ),
    ]
