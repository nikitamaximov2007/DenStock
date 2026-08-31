"""Подтверждение русского таможенного названия.

Оба поля добавляются со значением False. Старые строки подтверждёнными не
объявляются: доказательства подтверждения в данных нет, а выпускать
декларацию по неподтверждённому названию нельзя. Миграция обратима -
откат просто удаляет колонки.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("actions", "0010_seed_customs_first_version"),
    ]

    operations = [
        migrations.AddField(
            model_name="partcustomsinfo",
            name="customs_name_ru_confirmed",
            field=models.BooleanField(default=False, verbose_name="Русское название подтверждено"),
        ),
        migrations.AddField(
            model_name="partcustomsdataversion",
            name="customs_name_ru_confirmed",
            field=models.BooleanField(default=False, verbose_name="Русское название подтверждено"),
        ),
    ]
