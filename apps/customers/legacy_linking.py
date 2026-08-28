"""Ручная привязка исторических документов к карточке клиента.

Автоматический backfill на этих данных бессилен: у семидесяти старых продаж и
ремонтов есть имя и почти нигде нет отдельного снимка телефона, а связывать по
одному имени нельзя - тёзки это норма. Поэтому решение принимает человек.

Здесь ровно то, что нужно этому решению: собрать группу документов по её
историческому имени, подсказать оператору имя и телефон, если телефон явно
записан прямо в строке, и связать документы после подтверждения.

Чего здесь нет и не будет: угадывания по имени, массового создания карточек и
доверия к присланным браузером номерам документов. Группа заново определяется
на сервере внутри транзакции, снимки имени и телефона в документах не
меняются - меняется только ссылка на карточку.
"""
import re

from django.db import transaction
from django.db.models.functions import Trim

from apps.core.phones import normalize_phone
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale

# Телефон распознаём только в очевидном виде: 11 цифр с 8 или +7 либо 10 цифр,
# начинающихся с девятки, с любыми разделителями между ними. Всё прочее
# оставляем оператору: выдуманный телефон хуже пустого.
_PHONE = re.compile(
    r"(?<![0-9])(?:\+?7|8)?[\s(-]*(?:9\d{2})[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?![0-9])"
)


def _trimmed(model, name):
    return (
        model.objects.filter(customer__isnull=True, status=model.Status.COMPLETED)
        .annotate(legacy_name=Trim("customer_name"))
        .filter(legacy_name=name)
    )


def legacy_group(name: str):
    """Документы одной исторической группы: продажи и ремонты без карточки."""
    name = (name or "").strip()
    if not name:
        return Sale.objects.none(), RepairOrder.objects.none()
    return _trimmed(Sale, name), _trimmed(RepairOrder, name)


def legacy_group_summary(name: str) -> dict:
    """Что именно будет связано: счётчики для экрана подтверждения."""
    sales, repairs = legacy_group(name)
    return {
        "name": (name or "").strip(),
        "sales": sales.count(),
        "repairs": repairs.count(),
    }


def suggest_identity(raw: str) -> dict:
    """Разобрать историческую строку на имя и телефон. Только подсказка.

    «Петр 89120707078» превращается в имя «Петр» и телефон «89120707078».
    Если телефон не опознан однозначно, имя остаётся строкой целиком, а
    телефон пустым: правит оператор, а не догадка.
    """
    raw = (raw or "").strip()
    match = _PHONE.search(raw)
    if match is None:
        return {"name": raw, "phone": ""}
    phone = match.group(0).strip()
    if not normalize_phone(phone):
        return {"name": raw, "phone": ""}
    name = (raw[: match.start()] + " " + raw[match.end():]).strip()
    name = re.sub(r"[\s,;]+$", "", name).strip(" ,;")
    return {"name": name or raw, "phone": phone}


@transaction.atomic
def link_legacy_group(*, legacy_name: str, customer, by=None) -> dict:
    """Связать документы исторической группы с карточкой.

    Группа пересобирается здесь же, из имени: списку документов из браузера
    доверять нельзя. Обновляется только внешний ключ, поэтому снимки имени,
    телефона, цены и суммы документа остаются нетронутыми, а повторный вызов
    ничего не удваивает.
    """
    legacy_name = (legacy_name or "").strip()
    if not legacy_name:
        raise ValueError("Историческая группа не указана.")
    if customer is None or not customer.pk:
        raise ValueError("Карточка клиента не указана.")
    sales, repairs = legacy_group(legacy_name)
    linked_sales = sales.select_for_update().update(customer=customer)
    linked_repairs = repairs.select_for_update().update(customer=customer)
    return {
        "customer": customer,
        "sales_linked": linked_sales,
        "repairs_linked": linked_repairs,
    }
