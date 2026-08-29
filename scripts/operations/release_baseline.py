"""Снимок состояния перед выпуском и после него. Только чтение.

Запускается ДО развёртывания и ПОСЛЕ, вывод сравнивается построчно. Ничего не
пишет в базу и не трогает файлы, поэтому безопасен на production.

Использование:

    python manage.py shell < scripts/operations/release_baseline.py > before.txt
    # ... развёртывание ...
    python manage.py shell < scripts/operations/release_baseline.py > after.txt
    diff before.txt after.txt

Строки, которые ОБЯЗАНЫ совпасть: всё, кроме `deployed_sha`, `runtime_sha` и
`migrations_total`. Любое другое расхождение означает, что выпуск изменил данные,
а не только код, и это повод остановиться.
"""

from decimal import Decimal

from django.conf import settings
from django.db import connection

from apps.actions.models import WarehouseAction
from apps.brp.models import BrpCatalogPart
from apps.catalog.models import PartType
from apps.catalog_import.models import CatalogImportBatch
from apps.customers.models import Customer, CustomerPeriodPaymentAcknowledgement
from apps.inventory.models import StockLot, StockMovement
from apps.operations.models import DeploymentState, OfflineSession
from apps.repairs.models import RepairOrder
from apps.sales.models import Reservation, Sale

ZERO = Decimal("0")


def _sum(queryset, field="quantity"):
    total = ZERO
    for value in queryset.values_list(field, flat=True):
        total += value or ZERO
    return total


def _line(key, value):
    print(f"{key}={value}")


def _git_sha():
    from apps.operations import backup

    return backup._git_commit() or "unavailable"


def main():
    print("# DenisStock release baseline (read-only)")

    # --- Версия и здоровье ---------------------------------------------------
    _line("deployed_sha", _git_sha())
    _line("runtime_sha", settings.DENSTOCK_APP_COMMIT or "EMPTY")
    _line("mode", settings.DENSTOCK_MODE)
    _line("instance_id", settings.DENSTOCK_INSTANCE_ID)
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    _line("database", "reachable")
    _line("migrations_total", _migrations())

    # --- Управляющее состояние ------------------------------------------------
    state = DeploymentState.objects.filter(pk=DeploymentState.SINGLETON_PK).first()
    if state is None:
        _line("deployment_state", "MISSING")
    else:
        _line("deployment_state", "present")
        _line("write_state", state.write_state)
        _line("business_generation", state.business_generation)
        _line("database_identity", state.database_identity)
        _line(
            "authorized_emergency_primary",
            getattr(state, "authorized_emergency_primary_id", None) or "none",
        )
        _line("primary_authorization_epoch", getattr(state, "primary_authorization_epoch", "n/a"))

    # Незавершённой считается любая сессия, кроме подтверждённого возврата.
    # Наличие такой сессии на production означает, что склад где-то работает
    # автономно, и выпускать поверх этого нельзя.
    unfinished = OfflineSession.objects.exclude(
        status=OfflineSession.Status.COMPLETED
    ).count()
    _line("offline_sessions_unfinished", unfinished)
    _line("offline_sessions_total", OfflineSession.objects.count())

    # --- Складские блокировки -------------------------------------------------
    try:
        from apps.inventory.models import StockLocationLock

        _line(
            "recount_locks_open",
            StockLocationLock.objects.filter(released_at__isnull=True).count(),
        )
    except Exception:  # noqa: BLE001 - модель может отсутствовать в старой версии
        _line("recount_locks_open", "n/a")

    # --- Каталог --------------------------------------------------------------
    _line("parts_total", PartType.objects.count())
    _line("brp_total", BrpCatalogPart.objects.count())
    try:
        _line("brp_current", BrpCatalogPart.objects.filter(is_current=True).count())
        _line("brp_inactive", BrpCatalogPart.objects.filter(is_current=False).count())
    except Exception:  # noqa: BLE001 - до brp.0003 поля нет
        _line("brp_current", "n/a")
        _line("brp_inactive", "n/a")
    _line("catalog_import_batches", CatalogImportBatch.objects.count())

    # --- Склад ----------------------------------------------------------------
    lots = StockLot.objects.all()
    _line("lots_total", lots.count())
    _line("lots_available", lots.filter(status=StockLot.Status.AVAILABLE).count())
    _line("stock_available_qty", _sum(lots.filter(status=StockLot.Status.AVAILABLE)))
    _line("stock_physical_qty", _sum(lots))
    _line("lots_negative", lots.filter(quantity__lt=0).count())
    _line("lots_without_location", lots.filter(location__isnull=True).count())
    _line("lots_without_batch_line", lots.filter(batch_line__isnull=True).count())

    # --- Документы ------------------------------------------------------------
    _line("warehouse_actions_total", WarehouseAction.objects.count())
    _line(
        "warehouse_actions_active",
        WarehouseAction.objects.filter(status=WarehouseAction.Status.ACTIVE).count(),
    )
    _line("sales_total", Sale.objects.count())
    _line("sales_completed", Sale.objects.filter(status=Sale.Status.COMPLETED).count())
    _line("repairs_total", RepairOrder.objects.count())
    _line(
        "repairs_completed",
        RepairOrder.objects.filter(status=RepairOrder.Status.COMPLETED).count(),
    )
    _line("reservations_total", Reservation.objects.count())
    _line("customers_total", Customer.objects.count())
    # Журнал движений и подтверждения оплаты: выпуск кода не имеет права
    # менять ни то, ни другое, поэтому оба числа сверяются до и после.
    _line("stock_movements_total", StockMovement.objects.count())
    _line("payment_acknowledgements_total", CustomerPeriodPaymentAcknowledgement.objects.count())
    _line(
        "payment_acknowledgements_active",
        CustomerPeriodPaymentAcknowledgement.objects.filter(revoked_at__isnull=True).count(),
    )

    print("# end of baseline")


def _migrations():
    with connection.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM django_migrations")
        return cursor.fetchone()[0]


main()
