from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0022_parttype_parttype_public_active_idx"),
        ("customer_requests", "0027_customerrequeststatusevent_reason"),
    ]

    operations = [
        migrations.AddField(
            model_name="ownerphotouploadcontext",
            name="mode",
            field=models.CharField(
                choices=[
                    ("upload", "Ожидает фото"),
                    ("manage", "Управление фото"),
                    ("replace_confirm", "Подтверждение замены"),
                    ("replace_select", "Выбор фото"),
                    ("replace_upload", "Ожидает замену"),
                ],
                default="upload",
                max_length=20,
                verbose_name="Режим",
            ),
        ),
        migrations.AddField(
            model_name="ownerphotouploadcontext",
            name="target_image",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="owner_photo_replace_contexts",
                to="catalog.parttypeimage",
                verbose_name="Заменяемое фото",
            ),
        ),
    ]
