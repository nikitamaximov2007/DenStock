"""Гарантия: у сохранённой таможенной карточки всегда есть текущая версия.

Историческое списание ссылается не на карточку, а на версию, действовавшую в
момент движения. Если версию писать только из вьюхи, то карточка, сохранённая
из админки, импорта или консоли, останется без исторического следа, и её данные
молча исчезнут из таможенного экспорта. Инвариант дешевле держать здесь, чем
повторять в каждом месте записи.

Сама запись идемпотентна: сохранение без изменений новой версии не создаёт.
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import PartCustomsInfo


@receiver(post_save, sender=PartCustomsInfo)
def record_version_for_saved_customs(sender, instance: PartCustomsInfo, **kwargs) -> None:
    from .services import record_customs_data_version

    record_customs_data_version(instance, by=instance.updated_by)
