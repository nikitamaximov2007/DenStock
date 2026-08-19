from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("operations", "0003_add_completed_offline_session_status")]
    operations = [
        migrations.AddField(model_name="deploymentstate", name="authorized_emergency_primary_id", field=models.UUIDField(blank=True, null=True)),
        migrations.AddField(model_name="deploymentstate", name="primary_authorization_epoch", field=models.PositiveBigIntegerField(default=0)),
    ]
