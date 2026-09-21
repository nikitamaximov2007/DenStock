from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("customer_requests", "0021_two_provider_pairing_slots")]

    operations = [
        migrations.AddField(
            model_name="staffmessengerbinding",
            name="operator_key",
            field=models.CharField(
                blank=True,
                default="",
                max_length=32,
                verbose_name="Ключ личности оператора",
            ),
        ),
        migrations.AddField(
            model_name="staffmessengerpairingtoken",
            name="operator_key",
            field=models.CharField(
                blank=True,
                default="",
                max_length=32,
                verbose_name="Ключ личности оператора",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="staffmessengerbinding",
            name="staff_binding_user_provider_unique",
        ),
        migrations.AddConstraint(
            model_name="staffmessengerbinding",
            constraint=models.UniqueConstraint(
                fields=("user", "provider", "operator_key"),
                name="staff_binding_user_provider_key_unique",
            ),
        ),
    ]
