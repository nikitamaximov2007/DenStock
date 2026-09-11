from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("catalog", "0008_parttype_public_identity")]
    operations = [
        migrations.AddField(
            model_name="partanalog",
            name="source",
            field=models.CharField(default="internal", max_length=120, verbose_name="Источник"),
        ),
        migrations.AddField(
            model_name="partanalog",
            name="is_confirmed",
            field=models.BooleanField(
                default=False, verbose_name="Подтверждена для публичного каталога"
            ),
        ),
        migrations.AddField(
            model_name="partanalog",
            name="confirmed_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Подтверждена"),
        ),
        migrations.AddField(
            model_name="partanalog",
            name="confirmed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Кто подтвердил",
            ),
        ),
    ]
