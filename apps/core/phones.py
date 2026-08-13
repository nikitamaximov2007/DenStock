"""Нормализация телефона клиента для поиска.

Введённый телефон остаётся свободным текстом: как записали, так и показываем в
документе. Рядом хранится нормализованная форма только для поиска, чтобы
«+7 916 123-45-67», «8 (916) 1234567» и «79161234567» находили одного и того же
клиента и не заставляли работника вспоминать формат ввода.

Правила нормализации сознательно узкие и российские:

* остаются только цифры;
* 11 цифр, начинающихся с 8, приводятся к 7 (та же самая российская запись);
* 10 цифр, начинающихся с 9, считаются российским мобильным без кода страны и
  дополняются семёркой.

Всё остальное остаётся как есть: чужие форматы мы не угадываем и не портим.
Правило про 10 цифр намеренно узкое. Номер «4930123456» это немецкий номер, а
не московский без восьмёрки, и приписать ему российскую семёрку значило бы
выдумать факт. Городской номер, записанный без кода страны, останется своими
цифрами и найдётся по этой же записи.

Уникальности у телефона нет и быть не должно: один номер законно встречается в
разных документах, у клиента бывает несколько номеров, а у номера несколько
владельцев за время жизни. Телефон здесь помогает найти документ, а не
идентифицирует клиента.
"""

import re

from django.db.models import Q

_NON_DIGITS = re.compile(r"\D+")

RU_COUNTRY_CODE = "7"
RU_MOBILE_PREFIX = "9"
RU_FULL_LENGTH = 11
RU_LOCAL_LENGTH = 10


def normalize_phone(value: str) -> str:
    """Нормализованная форма телефона для поиска. Пусто, если цифр нет."""
    digits = _NON_DIGITS.sub("", value or "")
    if not digits:
        return ""
    if len(digits) == RU_FULL_LENGTH and digits.startswith("8"):
        return RU_COUNTRY_CODE + digits[1:]
    if len(digits) == RU_LOCAL_LENGTH and digits.startswith(RU_MOBILE_PREFIX):
        return RU_COUNTRY_CODE + digits
    return digits


def sync_normalized_phone(instance, kwargs) -> None:
    """Пересчитать поисковую форму телефона перед сохранением документа.

    Отдельно чинит частый случай: если документ сохраняют с `update_fields`, где
    есть `customer_phone`, поисковая форма должна попасть в тот же список, иначе
    она молча отстанет от видимого телефона.
    """
    instance.customer_phone_normalized = normalize_phone(instance.customer_phone)
    update_fields = kwargs.get("update_fields")
    if update_fields is not None:
        fields = set(update_fields)
        if "customer_phone" in fields:
            fields.add("customer_phone_normalized")
            kwargs["update_fields"] = sorted(fields)


def looks_like_phone(value: str) -> bool:
    """Похоже ли на телефон: достаточно цифр и нет букв.

    Нужно поиску, который принимает и имя клиента, и номер: по «Иванов» искать
    как по телефону бессмысленно.
    """
    value = (value or "").strip()
    if not value:
        return False
    digits = _NON_DIGITS.sub("", value)
    if len(digits) < 5:
        return False
    return not any(char.isalpha() for char in value)


def customer_search_q(query: str) -> Q:
    """Условие поиска документа по клиенту: имя подстрокой или телефон.

    Телефон ищется по нормализованной колонке, поэтому «+7 916 123-45-67»,
    «8 (916) 123-45-67» и «9161234567» находят одни и те же документы. Хвост
    номера тоже работает: по последним цифрам находится полный номер.
    """
    query = (query or "").strip()
    if not query:
        return Q()
    condition = Q(customer_name__icontains=query)
    if looks_like_phone(query):
        normalized = normalize_phone(query)
        if normalized:
            condition |= Q(customer_phone_normalized__contains=normalized)
    return condition
