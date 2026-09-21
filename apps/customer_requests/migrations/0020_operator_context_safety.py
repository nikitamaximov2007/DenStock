from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("customer_requests", "0019_customerrequest_current_responder_control_source_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="staffmessengerbinding",
            name="delivery_chat_id",
            field=models.BigIntegerField(
                blank=True, null=True, verbose_name="Диалог для уведомлений"
            ),
        ),
        migrations.AddField(
            model_name="customerrequest",
            name="pending_responder_label",
            field=models.CharField(
                blank=True, max_length=80, verbose_name="Ожидаемый ответственный"
            ),
        ),
        migrations.AddField(
            model_name="customerrequest",
            name="pending_responder_control_source",
            field=models.CharField(
                blank=True, max_length=12, verbose_name="Канал ожидаемого ответственного"
            ),
        ),
    ]
