"""Аварийный режим обязан видеть ВСЕ бизнес-приложения, включая новые.

Регрессия, найденная при сборке release candidate: справочник клиентов и
партии импорта каталога появились как отдельные приложения, но не попали в
список бизнес-приложений аварийного режима. Из-за этого:

* записи о клиентах и об импорте каталога проходили мимо SQL-уровня защиты
  записи, то есть в замороженной сессии их мог изменить любой путь помимо
  HTTP-слоя;
* отпечаток бизнес-состояния их не учитывал, поэтому решение о failback было
  слепым к изменениям клиентов на production во время автономной сессии.

Тест закрепляет полноту списка: любое новое приложение с бизнес-данными обязано
в него попасть, иначе аварийный режим даст ложное чувство безопасности.
"""
import pytest
from django.apps import apps as django_apps

from apps.operations.emergency_state import BUSINESS_APP_LABELS, business_state_marker
from apps.operations.write_guard import _is_business_mutation

# Приложения, которые НЕ являются бизнес-данными склада: инфраструктура,
# журналы доставки и служебные таблицы Django.
NON_BUSINESS_LABELS = frozenset(
    {
        "admin",
        "auth",
        "contenttypes",
        "labels",
        "messages",
        "operations",
        "reports",
        "sessions",
        "staticfiles",
    }
)


@pytest.mark.parametrize(
    "table",
    [
        "customers_customer",
        "catalog_import_catalogimportbatch",
        "sales_sale",
        "repairs_repairorder",
        "brp_brpcatalogpart",
        "inventory_stocklot",
    ],
)
def test_business_tables_are_guarded(table):
    assert _is_business_mutation(f'INSERT INTO "{table}" (x) VALUES (1)')
    assert _is_business_mutation(f'UPDATE "{table}" SET x = 1')
    assert _is_business_mutation(f'DELETE FROM "{table}"')


def test_service_tables_are_not_guarded():
    assert not _is_business_mutation('INSERT INTO "django_session" (x) VALUES (1)')
    assert not _is_business_mutation('SELECT * FROM "sales_sale"')


def test_new_business_apps_are_listed():
    assert "customers" in BUSINESS_APP_LABELS
    assert "catalog_import" in BUSINESS_APP_LABELS


def test_every_local_app_is_classified():
    """Ни одно приложение проекта не должно остаться незаклассифицированным.

    Если появится новое приложение, этот тест упадёт и заставит принять
    осознанное решение: бизнес-данные оно содержит или нет.
    """
    local = {
        config.label
        for config in django_apps.get_app_configs()
        if config.name.startswith("apps.")
    }
    unclassified = local - BUSINESS_APP_LABELS - NON_BUSINESS_LABELS
    assert not unclassified, f"Приложения не отнесены ни к одной группе: {sorted(unclassified)}"


def test_customer_data_is_part_of_business_fingerprint(db):
    """Изменение клиента обязано менять отпечаток бизнес-состояния."""
    from apps.customers.models import Customer

    before = business_state_marker()["business_sha256"]
    Customer.objects.create(name="Иванов", phone="+79121234567")
    after = business_state_marker()["business_sha256"]
    assert before != after


def test_catalog_import_batch_is_part_of_business_fingerprint(db, django_user_model):
    from apps.catalog_import.models import CatalogImportBatch

    before = business_state_marker()["business_sha256"]
    CatalogImportBatch.objects.create(
        catalog=CatalogImportBatch.Catalog.BRP,
        source_filename="price.xlsx",
        source_sha256="0" * 64,
    )
    after = business_state_marker()["business_sha256"]
    assert before != after


def test_marker_lists_new_tables(db):
    tables = business_state_marker()["tables"]
    assert any(name.startswith("customers.") for name in tables)
    assert any(name.startswith("catalog_import.") for name in tables)
