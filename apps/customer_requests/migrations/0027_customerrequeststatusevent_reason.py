from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("customer_requests", "0026_customerrequest_customer_customerrequest_sale_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="customerrequeststatusevent",
            name="reason",
            field=models.CharField(blank=True, max_length=500, verbose_name="Причина"),
        ),
    ]
