"""Очистить старый хардкод application_area = «МОТО ЗАПЧАСТИ» (Layer 33.1).

Раньше это был default модели, а компания мотоциклы не обслуживает: значение
никогда не выгружалось в таможенную форму (services.LEGACY_APPLICATION), но
оставалось в базе как «заполнено», хотя фактически область применения не
определена. Меняем ТОЛЬКО точное совпадение "МОТО ЗАПЧАСТИ" на пустую строку
(= «не заполнено» — экспорт попробует автоопределение по совместимости).
Любые другие значения (в т.ч. неизвестные) не трогаются и не удаляются.
"""
from django.db import migrations

LEGACY_VALUE = "МОТО ЗАПЧАСТИ"


def clear_legacy(apps, schema_editor):
    PartCustomsInfo = apps.get_model("actions", "PartCustomsInfo")
    updated = PartCustomsInfo.objects.filter(application_area=LEGACY_VALUE).update(
        application_area=""
    )
    print(
        f"  0006_clear_legacy_application_area: очищено {updated} "
        f"строк(и) с легаси-значением {LEGACY_VALUE!r}"
    )


def restore_legacy(apps, schema_editor):
    """Обратная миграция намеренно ничего не делает.

    Восстановить, какие строки были легаси-значением, а какие изначально
    пустыми, невозможно: откат схемы (0005) сам по себе данные не портит.
    """


class Migration(migrations.Migration):

    dependencies = [
        ("actions", "0005_alter_partcustomsinfo_application_area"),
    ]

    operations = [
        migrations.RunPython(clear_legacy, restore_legacy),
    ]
