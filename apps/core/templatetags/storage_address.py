"""Операторский формат складского адреса в шаблонах.

Живая ячейка знает его сама (``location.short_code``). Этот фильтр нужен для
исторических СНИМКОВ адреса - строк вроде ``location_code`` в журнале действий:
переписывать снимок нельзя, а показать его сотруднику нужно в том же виде, в
каком он видит склад.
"""
from django import template

from apps.warehouse.addresses import short_address

register = template.Library()


@register.filter(name="short_address")
def short_address_filter(value) -> str:
    return short_address(value or "")
