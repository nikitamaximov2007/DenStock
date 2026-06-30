from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.catalog.models import PartType
from apps.inventory.models import PartItem, StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    move_part_item,
    move_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.reports.services import (
    get_low_stock_report,
    get_sales_report,
    get_stock_report,
    resolve_period,
)
from apps.sales.models import Reservation
from apps.warehouse.models import StorageLocation

from .models import UnresolvedScan
from .scanner import resolve_scan
from .search import search_parts


def _dashboard_actions(user) -> list[dict]:
    """Быстрые действия — только те, на которые у пользователя есть право."""
    actions = [{"label": "Поиск детали", "url": reverse("part_search"), "primary": True}]
    if user.can_manage_inventory:
        actions.append({"label": "Приёмка сканером", "url": reverse("scanner_receiving")})
        actions.append({"label": "Перемещение", "url": reverse("scanner_move")})
    if user.can_manage_sales:
        actions.append({"label": "Новая продажа", "url": reverse("sale_create")})
    if user.can_manage_reservations:
        actions.append({"label": "Новый резерв", "url": reverse("reservation_create")})
    if user.can_manage_parts:
        actions.append({"label": "Добавить деталь", "url": reverse("part_create")})
    if user.can_manage_batches:
        actions.append({"label": "Создать партию", "url": reverse("batch_create")})
    if user.can_view_reports:
        actions.append({"label": "Отчёты", "url": reverse("reports_dashboard")})
    return actions


@login_required
def dashboard(request: HttpRequest) -> HttpResponse:
    """Главная панель: read-only сводка из сервисов отчётов + быстрые действия.

    Ничего не пишет — только читает `get_*_report()` Слоя 21 и простые счётчики.
    Финансы показываются лишь под `can_view_purchase_cost`; KPI/«требует внимания» —
    под `can_view_reports`; действия — по capability пользователя.
    """
    user = request.user
    ctx = {"actions": _dashboard_actions(user)}
    if user.can_view_reports:
        stock = get_stock_report()
        low = get_low_stock_report()
        part_types = PartType.objects.count()
        ctx.update(
            {
                "has_reports": True,
                "kpi_part_types": part_types,
                "kpi_part_types_stock": stock.part_types_with_stock,
                "kpi_available": stock.total_available,
                "kpi_quarantine": stock.total_quarantine,
                "kpi_low_stock": len(low),
                "kpi_active_reservations": Reservation.objects.filter(
                    status=Reservation.Status.ACTIVE
                ).count(),
                "low_rows": low[:8],
                "catalog_empty": part_types == 0,
                "stock_empty": stock.part_types_with_stock == 0,
            }
        )
        if user.can_view_purchase_cost:
            ctx["sales_month"] = get_sales_report(resolve_period({"preset": "month"}))
    return render(request, "core/dashboard.html", ctx)


# --- Слой 11: единый резолв сканера ------------------------------------------

_EMPTY_PAYLOAD = {
    "found": False, "status": "error", "type": None, "id": None,
    "label": "", "url": None, "message": "Пустой код.", "candidates": [],
}


def _record_unresolved(request: HttpRequest, code: str) -> None:
    """Журналировать нераспознанный скан. Анти-спам: тот же код тем же
    пользователем в пределах ~5 с новой строки не плодит."""
    recent = timezone.now() - timedelta(seconds=5)
    user = request.user if request.user.is_authenticated else None
    dup = UnresolvedScan.objects.filter(
        raw_value=code, user=user, created_at__gte=recent
    ).exists()
    if dup:
        return
    UnresolvedScan.objects.create(
        raw_value=code, user=user, context=request.POST.get("context", "")[:60]
    )


@login_required
@require_POST
def scanner_resolve(request: HttpRequest) -> JsonResponse:
    """Endpoint резолва: возвращает JSON-локатор. Только распознаёт, не действует.

    На реальном unknown пишет `UnresolvedScan` (сам резолвер — чистый).
    """
    code = (request.POST.get("code") or "").strip()
    if not code:
        return JsonResponse(_EMPTY_PAYLOAD, status=400)
    result = resolve_scan(code, user=request.user)
    if result.status == "unknown":
        _record_unresolved(request, code)
    return JsonResponse(result.to_dict())


@login_required
def scanner_page(request: HttpRequest) -> HttpResponse:
    """Страница «Сканер» (4.5). No-JS fallback: POST резолвится сервером."""
    result = None
    code = ""
    if request.method == "POST":
        code = (request.POST.get("code") or "").strip()
        if code:
            result = resolve_scan(code, user=request.user)
            if result.status == "unknown":
                _record_unresolved(request, code)
    return render(request, "core/scanner.html", {"result": result, "code": code})


@login_required
def unresolved_list(request: HttpRequest) -> HttpResponse:
    """История нераспознанных сканов — только Админ/Руководитель."""
    if not (request.user.is_admin or request.user.is_manager):
        raise PermissionDenied
    scans = UnresolvedScan.objects.select_related("user", "resolved_part")[:200]
    return render(request, "core/unresolved_list.html", {"scans": scans})


def healthz(request: HttpRequest) -> JsonResponse:
    """Проверка доступности приложения и (lightweight) базы данных.

    Приложение отвечает → status=ok. Доступность БД проверяется простым
    запросом; при ошибке возвращается 503, чтобы Docker/мониторинг это видел.
    """
    db_ok = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 — для healthcheck достаточно факта недоступности
        db_ok = False

    payload = {"status": "ok", "db": "ok" if db_ok else "down"}
    return JsonResponse(payload, status=200 if db_ok else 503)


# --- Слой 13: быстрый поиск детали (read-only) -------------------------------


@login_required
def search_page(request: HttpRequest) -> HttpResponse:
    """Быстрый read-only поиск детали с наличием. Ничего не пишет.

    Разворот экземпляров/лотов (со ссылками на item/lot_detail) показываем только
    инвентарь-видящим ролям; у Продавца/Мастера эти карточки дали бы 403.
    """
    q = request.GET.get("q", "").strip()
    rows = search_parts(q)
    can_view_inventory = request.user.can_manage_inventory or request.user.is_viewer
    if can_view_inventory:
        for row in rows:
            part = row.part
            if part.tracking_mode == part.TrackingMode.SERIAL:
                row.items = list(
                    PartItem.objects.filter(part_type=part)
                    .select_related("current_location", "batch")
                    .order_by("internal_number")[:20]
                )
            else:
                row.lots = list(
                    StockLot.objects.filter(part_type=part)
                    .select_related("location", "batch", "batch_line")
                    .order_by("-created_at")[:20]
                )
    ctx = {
        "q": q,
        "rows": rows,
        "show_costs": request.user.can_view_purchase_cost,
        "can_view_inventory": can_view_inventory,
        "can_sell": request.user.can_manage_sales,
        "can_repair": request.user.can_manage_repairs,
        "can_write_off": request.user.can_manage_write_offs,
        "too_short": 0 < len(q) < 2,
    }
    return render(request, "core/search.html", ctx)


# --- Слой 12: приёмка и размещение через сканер ------------------------------

_HISTORY_TYPES = [
    StockMovement.MovementType.RECEIVE_ITEM,
    StockMovement.MovementType.RECEIVE_LOT,
    StockMovement.MovementType.MOVE_ITEM,
    StockMovement.MovementType.MOVE_LOT,
]


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_operation(request: HttpRequest):
    """Перечитать объект/ячейку из hidden-полей по ID.

    Hidden-полям доверять нельзя: объект и ячейка ВСЕГДА перечитываются из БД,
    статус/тип/доступность проверяются позже (`_confirm_operation`).
    """
    kind = request.POST.get("object_kind", "")
    obj = None
    obj_id = _int(request.POST.get("object_id"))
    if obj_id is not None:
        if kind == "part_item":
            obj = (
                PartItem.objects.filter(pk=obj_id)
                .select_related("part_type", "current_location").first()
            )
        elif kind == "stock_lot":
            obj = (
                StockLot.objects.filter(pk=obj_id)
                .select_related("part_type", "location").first()
            )
    if obj is None:
        kind = ""
    loc_id = _int(request.POST.get("location_id"))
    location = (
        StorageLocation.objects.filter(pk=loc_id).first() if loc_id is not None else None
    )
    return kind, obj, location


def _wrong_object_message(result) -> str:
    if result.type == "location":
        return "Сначала отсканируйте деталь, потом ячейку."
    if result.type == "part_type":
        return (
            "Это вид детали, а не конкретный экземпляр. Отсканируйте экземпляр "
            "(ITEM:/DS-…) или серийный номер."
        )
    if result.type == "batch":
        return "Это партия, а не экземпляр."
    return "Ожидается экземпляр детали."


def _confirm_operation(request: HttpRequest, kind: str, obj, location) -> str:
    """Жёсткая проверка перед вызовом сервиса. Возвращает текст ошибки или "".

    Сервер не доверяет hidden-полям: объект/ячейка уже перечитаны из БД
    (`_load_operation`); здесь проверяем существование, тип, статус, ячейку и
    `can_hold_stock()`, и только потом вызываем сервис Слоя 10. View сам ledger
    не трогает — ни `StockMovement`, ни `StockBalance`.
    """
    if obj is None:
        return "Не выбран объект для приёмки."
    if location is None:
        return "Не отсканирована ячейка."
    if not location.can_hold_stock():
        return "Ячейка не предназначена для хранения остатка (неактивна или запрещена)."
    try:
        if kind == "part_item":
            if obj.status == PartItem.Status.RECEIVING:
                receive_part_item(obj, to_location=location, by=request.user)
                messages.success(
                    request, f"Экземпляр {obj.internal_number} принят в {location.code}."
                )
            elif obj.status == PartItem.Status.AVAILABLE:
                move_part_item(obj, location, by=request.user)
                messages.success(
                    request, f"Экземпляр {obj.internal_number} перемещён в {location.code}."
                )
            else:
                return "Экземпляр в недопустимом статусе для приёмки/перемещения."
        elif kind == "stock_lot":
            if obj.status == StockLot.Status.RECEIVING:
                if obj.location_id != location.pk:
                    return (
                        f"Лот создан для ячейки {obj.location.code}; "
                        f"отсканирована {location.code}."
                    )
                receive_stock_lot(obj, by=request.user)
                messages.success(request, f"Лот #{obj.pk} принят в {location.code}.")
            elif obj.status == StockLot.Status.AVAILABLE:
                move_stock_lot(obj, location, by=request.user)
                messages.success(request, f"Лот #{obj.pk} перемещён в {location.code}.")
            else:
                return "Лот в недопустимом статусе."
        else:
            return "Не выбран объект для приёмки."
    except InventoryError as exc:
        return str(exc)
    return ""


@login_required
def scanner_receiving(request: HttpRequest) -> HttpResponse:
    """Экран приёмки/размещения сканером. View только оркеструет; складские
    изменения идут исключительно через сервисы Слоя 10."""
    if not request.user.can_manage_inventory:
        raise PermissionDenied

    kind, obj, location = "", None, None
    candidates: list = []
    error = ""

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "reset":
            return redirect("scanner_receiving")

        kind, obj, location = _load_operation(request)

        if action == "select_lot":
            lot = (
                StockLot.objects.filter(
                    pk=_int(request.POST.get("lot_id")), status=StockLot.Status.RECEIVING
                ).select_related("part_type", "location").first()
            )
            if lot is None:
                error = "Лот не найден или уже не на приёмке."
                kind, obj, location = "", None, None
            else:
                kind, obj, location = "stock_lot", lot, None

        elif action == "select_candidate":
            cand_id = _int(request.POST.get("candidate_id"))
            picked = (
                PartItem.objects.filter(pk=cand_id).select_related("part_type").first()
                if cand_id is not None else None
            )
            if picked is None:
                error = "Экземпляр не найден."
            else:
                kind, obj, location = "part_item", picked, None

        elif action == "scan":
            code = (request.POST.get("code") or "").strip()
            if not code:
                error = "Пустой код."
            elif obj is None:
                result = resolve_scan(code, user=request.user)
                if result.status == "unknown":
                    _record_unresolved(request, code)
                    error = "Код не распознан."
                elif result.status == "ambiguous":
                    candidates = result.candidates
                    error = "Уточните, какой экземпляр разместить."
                elif result.type == "part_item":
                    kind = "part_item"
                    obj = PartItem.objects.select_related(
                        "part_type", "current_location"
                    ).get(pk=result.id)
                else:
                    error = _wrong_object_message(result)
            else:
                result = resolve_scan(code, user=request.user)
                if result.status == "unknown":
                    _record_unresolved(request, code)
                    error = "Код не распознан."
                elif result.type == "location":
                    location = StorageLocation.objects.get(pk=result.id)
                else:
                    error = "Ожидается ячейка (LOC:/код)."

        elif action == "confirm":
            error = _confirm_operation(request, kind, obj, location)
            if not error:
                return redirect("scanner_receiving")  # успех → messages + PRG

    if obj is None:
        step = "scan_object"
    elif location is None:
        step = "scan_location"
    else:
        step = "confirm"

    ctx = {
        "object": obj,
        "object_kind": kind,
        "location": location,
        "step": step,
        "candidates": candidates,
        "error": error,
        "receiving_lots": (
            StockLot.objects.filter(status=StockLot.Status.RECEIVING)
            .select_related("part_type", "batch", "location")
            .order_by("-created_at")[:50]
        ),
        "history": (
            StockMovement.objects.filter(
                created_by=request.user, movement_type__in=_HISTORY_TYPES
            ).select_related("part_type", "to_location")[:10]
        ),
        "show_costs": request.user.can_view_purchase_cost,
    }
    return render(request, "core/receiving.html", ctx)


# --- Слой 14: перемещение деталей и лотов через сканер ------------------------

_MOVE_HISTORY_TYPES = [
    StockMovement.MovementType.MOVE_ITEM,
    StockMovement.MovementType.MOVE_LOT,
]

# Что можно перемещать этим экраном (receiving идёт через приёмку; терминальные/
# depleted — нельзя). Сервисы Слоя 10 допускают physical-статусы; экран дополнительно
# отсекает receiving, разделяя receive- и move-сценарии.
_MOVABLE_ITEM_STATUSES = (PartItem.Status.AVAILABLE, PartItem.Status.QUARANTINE)
_MOVABLE_LOT_STATUSES = (StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE)


def _move_block_reason(kind: str, obj) -> str:
    """Почему объект нельзя переместить этим экраном (или "" если можно).

    Гард уровня экрана: `receiving` → на приёмку; терминальные/`depleted` →
    недоступно. Сервис не меняем — это политика workflow, а не ledger.
    """
    if kind == "part_item":
        if obj.status == PartItem.Status.RECEIVING:
            return "Экземпляр ещё не принят — используйте «Приёмку сканером»."
        if obj.status not in _MOVABLE_ITEM_STATUSES:
            return f"Экземпляр в статусе «{obj.get_status_display()}» — перемещение недоступно."
    elif kind == "stock_lot":
        if obj.status == StockLot.Status.RECEIVING:
            return "Лот ещё не принят — используйте «Приёмку сканером»."
        if obj.status not in _MOVABLE_LOT_STATUSES:
            return f"Лот в статусе «{obj.get_status_display()}» — перемещение недоступно."
    return ""


def _current_location_id(kind: str, obj):
    return obj.current_location_id if kind == "part_item" else obj.location_id


def _confirm_move(request: HttpRequest, kind: str, obj, location) -> tuple[str, str]:
    """Проверить состояние и переместить через сервис Слоя 10.

    Возвращает (level, text): "" — успех (messages + redirect); "info" —
    нейтральный no-op (та же ячейка, движение НЕ создаётся, сервис не вызывается);
    "error" — ошибка. View сам ledger не трогает: ни `StockMovement`, ни
    `StockBalance` — только `move_part_item`/`move_stock_lot`.
    """
    if obj is None:
        return "error", "Не выбран объект для перемещения."
    if location is None:
        return "error", "Не отсканирована ячейка."
    reason = _move_block_reason(kind, obj)
    if reason:
        return "error", reason
    # Та же ячейка — нейтральный no-op: сервис не вызываем, движение не создаётся.
    if _current_location_id(kind, obj) == location.pk:
        return "info", "Объект уже в этой ячейке — перемещение не требуется."
    if not location.can_hold_stock():
        return "error", "Ячейка не предназначена для хранения остатка (неактивна или запрещена)."
    try:
        if kind == "part_item":
            move_part_item(obj, location, by=request.user)
            messages.success(
                request, f"Экземпляр {obj.internal_number} перемещён в {location.code}."
            )
        elif kind == "stock_lot":
            move_stock_lot(obj, location, by=request.user)
            messages.success(request, f"Лот #{obj.pk} перемещён в {location.code}.")
        else:
            return "error", "Не выбран объект для перемещения."
    except InventoryError as exc:
        return "error", str(exc)
    return "", ""


def _move_preselect(request: HttpRequest):
    """GET-преселект объекта из карточки (?item=/?lot=). Query недоверенный —
    объект перечитывается из БД и проверяется гардом статуса."""
    item_pk = _int(request.GET.get("item"))
    lot_pk = _int(request.GET.get("lot"))
    if item_pk is not None:
        picked = (
            PartItem.objects.filter(pk=item_pk)
            .select_related("part_type", "current_location").first()
        )
        if picked is not None and not _move_block_reason("part_item", picked):
            return "part_item", picked
    elif lot_pk is not None:
        picked = (
            StockLot.objects.filter(pk=lot_pk)
            .select_related("part_type", "location").first()
        )
        if picked is not None and not _move_block_reason("stock_lot", picked):
            return "stock_lot", picked
    return "", None


@login_required
def scanner_move(request: HttpRequest) -> HttpResponse:
    """Экран перемещения деталей/лотов сканером. View только оркеструет:
    резолв → проверка → сервис Слоя 10. Прямой записи движений/баланса нет."""
    if not request.user.can_manage_inventory:
        raise PermissionDenied

    kind, obj, location = "", None, None
    candidates: list = []
    error = ""
    info = ""

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "reset":
            return redirect("scanner_move")

        kind, obj, location = _load_operation(request)

        if action == "select_lot":
            lot = (
                StockLot.objects.filter(
                    pk=_int(request.POST.get("lot_id")), status__in=_MOVABLE_LOT_STATUSES
                ).select_related("part_type", "location").first()
            )
            if lot is None:
                error = "Лот не найден или недоступен для перемещения."
                kind, obj, location = "", None, None
            else:
                kind, obj, location = "stock_lot", lot, None

        elif action == "select_candidate":
            cand_id = _int(request.POST.get("candidate_id"))
            picked = (
                PartItem.objects.filter(pk=cand_id)
                .select_related("part_type", "current_location").first()
                if cand_id is not None else None
            )
            if picked is None:
                error = "Экземпляр не найден."
            else:
                reason = _move_block_reason("part_item", picked)
                if reason:
                    error = reason
                else:
                    kind, obj, location = "part_item", picked, None

        elif action == "scan":
            code = (request.POST.get("code") or "").strip()
            if not code:
                error = "Пустой код."
            elif obj is None:
                result = resolve_scan(code, user=request.user)
                if result.status == "unknown":
                    _record_unresolved(request, code)
                    error = "Код не распознан."
                elif result.status == "ambiguous":
                    candidates = result.candidates
                    error = "Уточните, какой экземпляр переместить."
                elif result.type == "part_item":
                    picked = PartItem.objects.select_related(
                        "part_type", "current_location"
                    ).get(pk=result.id)
                    reason = _move_block_reason("part_item", picked)
                    if reason:
                        error = reason
                    else:
                        kind, obj = "part_item", picked
                else:
                    error = _wrong_object_message(result)
            else:
                result = resolve_scan(code, user=request.user)
                if result.status == "unknown":
                    _record_unresolved(request, code)
                    error = "Код не распознан."
                elif result.type == "location":
                    location = StorageLocation.objects.get(pk=result.id)
                else:
                    error = "Ожидается ячейка (LOC:/код)."

        elif action == "confirm":
            level, text = _confirm_move(request, kind, obj, location)
            if level == "":
                return redirect("scanner_move")  # успех → messages + PRG
            if level == "info":
                info = text
                location = None  # вернуть на шаг «скан ячейки», объект сохранить
            else:
                error = text
    else:
        kind, obj = _move_preselect(request)

    if obj is None:
        step = "scan_object"
    elif location is None:
        step = "scan_location"
    else:
        step = "confirm"

    ctx = {
        "object": obj,
        "object_kind": kind,
        "location": location,
        "step": step,
        "candidates": candidates,
        "error": error,
        "info": info,
        "placed_lots": (
            StockLot.objects.filter(status__in=_MOVABLE_LOT_STATUSES)
            .select_related("part_type", "batch", "location")
            .order_by("part_type__name", "location__code")[:50]
        ),
        "history": (
            StockMovement.objects.filter(
                created_by=request.user, movement_type__in=_MOVE_HISTORY_TYPES
            ).select_related("part_type", "from_location", "to_location")[:10]
        ),
        "show_costs": request.user.can_view_purchase_cost,
    }
    return render(request, "core/move.html", ctx)
