"""Red-team: замороженная аварийная сессия обязана реально блокировать запись.

Почему этот файл существует отдельно. Существующая проверка полноты списка
бизнес-приложений опрашивает `_is_business_mutation`, то есть регулярное
выражение по тексту SQL. Это КОСВЕННОЕ доказательство: оно показывает, что
таблица распознана, но не то, что запись действительно не пройдёт.

Прямой проверки мешало то, что защита намеренно отключена при
`DENSTOCK_MODE == "test"`, а весь набор тестов идёт именно в этом режиме.
Здесь режим переключается явно, поэтому проверяется настоящее поведение
защиты, а не её намерение.

Ожидание контракта: в состоянии EMERGENCY_FROZEN бизнес-запись обязана
падать с BusinessWriteBlocked на уровне SQL, независимо от того, каким путём
её выполнили: сервисом, ORM, bulk-операцией или напрямую.
"""
from decimal import Decimal

import pytest

from apps.brp.models import BrpCatalogPart
from apps.catalog_import.models import CatalogImportBatch
from apps.customers.models import Customer
from apps.operations.models import DeploymentState
from apps.operations.write_guard import BusinessWriteBlocked
from apps.suppliers.models import Supplier


def _set_state(write_state):
    state = DeploymentState.get_solo()
    state.write_state = write_state
    state.save(update_fields=["write_state", "updated_at"])


@pytest.fixture
def frozen_emergency(db, settings):
    """Локальная аварийная сессия в состоянии «заморожено»."""
    _set_state(DeploymentState.WriteState.EMERGENCY_FROZEN)
    settings.DENSTOCK_MODE = "emergency-local"
    yield
    settings.DENSTOCK_MODE = "test"


@pytest.fixture
def active_emergency(db, settings):
    _set_state(DeploymentState.WriteState.EMERGENCY_ACTIVE)
    settings.DENSTOCK_MODE = "emergency-local"
    yield
    settings.DENSTOCK_MODE = "test"


# --- Заморозка действительно блокирует ---------------------------------------------------


def test_frozen_blocks_customer_create(frozen_emergency):
    with pytest.raises(BusinessWriteBlocked):
        Customer.objects.create(name="Иванов", phone="+79121234567")


def test_frozen_blocks_customer_update(db, settings):
    customer = Customer.objects.create(name="Иванов")
    _set_state(DeploymentState.WriteState.EMERGENCY_FROZEN)
    settings.DENSTOCK_MODE = "emergency-local"
    try:
        customer.name = "Переименован"
        with pytest.raises(BusinessWriteBlocked):
            customer.save(update_fields=["name"])
    finally:
        settings.DENSTOCK_MODE = "test"


def test_frozen_blocks_queryset_update(db, settings):
    Customer.objects.create(name="Иванов")
    _set_state(DeploymentState.WriteState.EMERGENCY_FROZEN)
    settings.DENSTOCK_MODE = "emergency-local"
    try:
        with pytest.raises(BusinessWriteBlocked):
            Customer.objects.all().update(name="Массово")
    finally:
        settings.DENSTOCK_MODE = "test"


def test_frozen_blocks_bulk_create(frozen_emergency):
    with pytest.raises(BusinessWriteBlocked):
        Customer.objects.bulk_create([Customer(name="A"), Customer(name="B")])


def test_frozen_blocks_delete(db, settings):
    Customer.objects.create(name="Иванов")
    _set_state(DeploymentState.WriteState.EMERGENCY_FROZEN)
    settings.DENSTOCK_MODE = "emergency-local"
    try:
        with pytest.raises(BusinessWriteBlocked):
            Customer.objects.all().delete()
    finally:
        settings.DENSTOCK_MODE = "test"


def test_frozen_blocks_catalog_import_batch(frozen_emergency):
    with pytest.raises(BusinessWriteBlocked):
        CatalogImportBatch.objects.create(
            catalog=CatalogImportBatch.Catalog.BRP,
            source_filename="price.xlsx",
            source_sha256="0" * 64,
        )


def test_frozen_blocks_catalog_part(frozen_emergency):
    with pytest.raises(BusinessWriteBlocked):
        BrpCatalogPart.objects.create(
            material_no="900000001", wholesale_price_usd=Decimal("10")
        )


def test_frozen_blocks_known_business_entity(frozen_emergency):
    """Контроль: уже классифицированное приложение ведёт себя так же."""
    with pytest.raises(BusinessWriteBlocked):
        Supplier.objects.create(name="Должен быть заблокирован")


# --- Активная сессия писать разрешает -----------------------------------------------------


def test_active_emergency_allows_customer_write(active_emergency):
    customer = Customer.objects.create(name="Иванов", phone="+79121234567")
    assert customer.pk is not None


def test_active_emergency_allows_catalog_batch(active_emergency):
    batch = CatalogImportBatch.objects.create(
        catalog=CatalogImportBatch.Catalog.BRP,
        source_filename="price.xlsx",
        source_sha256="1" * 64,
    )
    assert batch.pk is not None


# --- Служебные записи заморозка не ломает --------------------------------------------------


def test_frozen_still_allows_control_plane_write(frozen_emergency):
    """Сам аварийный контур обязан оставаться управляемым в заморозке."""
    state = DeploymentState.get_solo()
    state.state_reason = "red-team probe"
    state.save(update_fields=["state_reason", "updated_at"])
    state.refresh_from_db()
    assert state.state_reason == "red-team probe"
