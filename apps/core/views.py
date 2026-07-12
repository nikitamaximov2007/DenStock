from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.brp.models import BrpCatalogPart
from apps.catalog.models import PartType, normalize_number
from apps.core.templatetags.number_format import quantity_int
from apps.inventory.models import FoundStockPosting, PartItem, StockLot, StockMovement
from apps.inventory.presentation import (
    attach_movement_identity,
    attach_part_identity,
    identity_numbers_prefetch,
    with_part_identity,
)
from apps.inventory.services import (
    FOUND_ADDITION_DOC,
    FoundStockAlreadyPosted,
    InventoryError,
    move_part_item,
    move_stock_lot,
    post_found_stock_group,
    receive_part_item,
    receive_stock_lot,
)
from apps.polaris.models import PolarisCatalogPart
from apps.reports.services import (
    get_low_stock_report,
    get_sales_report,
    get_stock_report,
    resolve_period,
)
from apps.sales.models import Reservation
from apps.warehouse.models import StorageLocation

from .models import UnresolvedScan
from .receiving_queue import (
    ReceivingQueueError,
    add_candidate,
    assign_location,
    clear_pending,
    clear_queue,
    find_receiving_candidates,
    group_for_post,
    pending_context,
    pop_pending_candidate,
    queue_context,
    remove_line,
    remove_posted_group,
    store_pending_candidates,
    unassign_location,
    update_quantity,
)
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
    if can_view_inventory and rows:
        # Разворот одним запросом на все результаты (не по запросу на строку);
        # лимит 20 на деталь сохраняется при группировке.
        serial_ids = [
            r.part.pk for r in rows
            if r.part.tracking_mode == PartType.TrackingMode.SERIAL
        ]
        bulk_ids = [
            r.part.pk for r in rows
            if r.part.tracking_mode != PartType.TrackingMode.SERIAL
        ]
        items_by_part: dict[int, list] = defaultdict(list)
        if serial_ids:
            for item in (
                PartItem.objects.filter(part_type_id__in=serial_ids)
                .select_related("current_location", "batch")
                .order_by("internal_number")
            ):
                if len(items_by_part[item.part_type_id]) < 20:
                    items_by_part[item.part_type_id].append(item)
        lots_by_part: dict[int, list] = defaultdict(list)
        if bulk_ids:
            for lot in (
                StockLot.objects.filter(part_type_id__in=bulk_ids)
                .select_related("location", "batch", "batch_line")
                .order_by("-created_at")
            ):
                if len(lots_by_part[lot.part_type_id]) < 20:
                    lots_by_part[lot.part_type_id].append(lot)
        for row in rows:
            if row.part.tracking_mode == PartType.TrackingMode.SERIAL:
                row.items = items_by_part.get(row.part.pk, [])
            else:
                row.lots = lots_by_part.get(row.part.pk, [])
    # Catalog hints are reference data, not stock. If the same number exists in
    # several catalogs, show every catalog hit instead of silently choosing one.
    brp_hits = []
    polaris_hits = []
    if len(q) >= 2:
        norm = normalize_number(q)
        number_q = Q(pk=None)
        if norm:
            number_q = (
                Q(material_no_norm=norm)
                | Q(replacement_no_1_norm=norm)
                | Q(replacement_no_2_norm=norm)
            )
        if not rows and len(q) >= 3:
            number_q = number_q | Q(part_desc__icontains=q)
        brp_hits = list(BrpCatalogPart.objects.filter(number_q)[:5])
        polaris_q = Q(pk=None)
        if norm:
            polaris_q = Q(part_number_norm=norm) | Q(superseded_number_norm=norm)
        if not rows and len(q) >= 3:
            polaris_q = polaris_q | Q(part_name__icontains=q)
        polaris_hits = list(PolarisCatalogPart.objects.filter(polaris_q)[:5])
    ctx = {
        "q": q,
        "rows": rows,
        "brp_hits": brp_hits,
        "polaris_hits": polaris_hits,
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
    if result.type == "batch":
        return "Это партия, а не экземпляр."
    return "Ожидается экземпляр детали."


def _queue_candidate_scan(request: HttpRequest, code: str, *, warehouse_part_id=None):
    """Add an unambiguous exact identity, or persist a manufacturer choice."""
    candidates = find_receiving_candidates(code, warehouse_part_id=warehouse_part_id)
    if not candidates:
        return False
    if len(candidates) > 1:
        store_pending_candidates(request.session, candidates)
        messages.info(
            request,
            "Артикул найден у нескольких производителей. Выберите точную позицию.",
        )
        return True
    line, added_new = add_candidate(request.session, candidates[0])
    verb = "добавлен в очередь" if added_new else "увеличен в очереди"
    messages.success(
        request,
        f"{line['exact_number']}: {verb}, количество {quantity_int(line['quantity'])}.",
    )
    return True


def _post_queue_group(request: HttpRequest) -> str:
    location_id = _int(request.POST.get("location_id"))
    token = (request.POST.get("token") or "").strip()
    if location_id is None:
        return "Группа ячейки не найдена."
    location = StorageLocation.objects.filter(pk=location_id).first()
    if location is None or not location.can_hold_stock():
        return "Ячейка неактивна или не предназначена для хранения."

    already = FoundStockPosting.objects.filter(token=token, created_by=request.user).exists()
    if already:
        remove_posted_group(request.session, location_id)
        messages.info(request, "Эта группа уже была проведена.")
        return ""
    try:
        entries, _fingerprint = group_for_post(request.session, location_id, token)
        results = post_found_stock_group(
            entries=entries, location=location, token=token, by=request.user
        )
    except FoundStockAlreadyPosted:
        remove_posted_group(request.session, location_id)
        messages.info(request, "Эта группа уже была проведена.")
        return ""
    except (InventoryError, ReceivingQueueError) as exc:
        return str(exc)

    remove_posted_group(request.session, location_id)
    quantity = sum(row["quantity"] for row in results)
    balances = ", ".join(
        f"{row['exact_number']}: {quantity_int(row['lot'].quantity)}"
        for row in results
    )
    messages.success(
        request,
        f"Добавлено {quantity} деталей в ячейку {location.code}. "
        f"Новые остатки: {balances}.",
    )
    return ""


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

        try:
            if action == "queue_clear":
                clear_queue(request.session)
                messages.success(request, "Список приёмки очищен. Остатки не изменялись.")
                return redirect("scanner_receiving")
            if action == "queue_remove":
                remove_line(request.session, request.POST.get("line_id", ""))
                messages.success(request, "Строка удалена из очереди. Остатки не изменялись.")
                return redirect("scanner_receiving")
            if action == "queue_update":
                update_quantity(
                    request.session,
                    request.POST.get("line_id", ""),
                    request.POST.get("quantity"),
                )
                messages.success(request, "Количество в очереди обновлено.")
                return redirect("scanner_receiving")
            if action == "queue_assign":
                code = assign_location(
                    request.session,
                    request.POST.get("line_id", ""),
                    location_id=_int(request.POST.get("location_id")),
                    location_code=request.POST.get("location_code", ""),
                )
                messages.success(request, f"Деталь будет добавлена в {code}.")
                return redirect("scanner_receiving")
            if action == "queue_unassign":
                unassign_location(request.session, request.POST.get("line_id", ""))
                messages.success(request, "Выберите другую ячейку до проведения группы.")
                return redirect("scanner_receiving")
            if action == "queue_select_candidate":
                candidate = pop_pending_candidate(
                    request.session,
                    request.POST.get("candidate_token", ""),
                    request.POST.get("candidate_key", ""),
                )
                line, _added_new = add_candidate(request.session, candidate)
                messages.success(
                    request,
                    f"{line['manufacturer']} {line['exact_number']} добавлен в очередь.",
                )
                return redirect("scanner_receiving")
            if action == "queue_post":
                error = _post_queue_group(request)
                if not error:
                    return redirect("scanner_receiving")
        except ReceivingQueueError as exc:
            error = str(exc)

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
                clear_pending(request.session)
                result = resolve_scan(code, user=request.user)
                if result.type == "part_item" and result.status == "found":
                    kind = "part_item"
                    obj = PartItem.objects.select_related(
                        "part_type", "current_location"
                    ).get(pk=result.id)
                elif result.status == "ambiguous" and any(
                    candidate.get("type") == "part_item" for candidate in result.candidates
                ):
                    candidates = result.candidates
                    error = "Уточните, какой экземпляр разместить."
                elif result.type == "part_type" and result.status == "found":
                    try:
                        queued = _queue_candidate_scan(
                            request, code, warehouse_part_id=result.id
                        )
                    except ReceivingQueueError as exc:
                        error = str(exc)
                    else:
                        if queued:
                            return redirect("scanner_receiving")
                        error = "Карточка детали не подходит для пакетной приёмки."
                elif result.status in {"unknown", "ambiguous"}:
                    try:
                        queued = _queue_candidate_scan(request, code)
                    except ReceivingQueueError as exc:
                        error = str(exc)
                    else:
                        if queued:
                            return redirect("scanner_receiving")
                        _record_unresolved(request, code)
                        error = (
                            "Код не распознан: он не найден в каталогах BRP, Polaris "
                            "или в складском "
                            "справочнике. Сначала создайте карточку детали или проверьте номер."
                        )
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

    history = list(
        StockMovement.objects.filter(
            created_by=request.user, movement_type__in=_HISTORY_TYPES
        )
        .select_related(
            "part_type", "part_type__manufacturer", "to_location",
            "part_type__brp_link__brp_part", "part_type__polaris_link__polaris_part",
        )
        .prefetch_related(identity_numbers_prefetch())[:10]
    )
    found_history = list(
        StockMovement.objects.filter(
            movement_type=StockMovement.MovementType.ADJUST_IN,
            document_type=FOUND_ADDITION_DOC,
        )
        .select_related(
            "part_type", "part_type__manufacturer", "to_location", "created_by",
            "part_type__brp_link__brp_part", "part_type__polaris_link__polaris_part",
        )
        .prefetch_related(identity_numbers_prefetch())[:10]
    )
    attach_movement_identity(history)
    attach_movement_identity(found_history)
    for movement in found_history:
        movement.customer_unit_price = movement.part_type.recommended_price or Decimal("0")
        movement.customer_total_value = movement.customer_unit_price * movement.quantity
    receiving_lots = list(
        with_part_identity(
            StockLot.objects.filter(status=StockLot.Status.RECEIVING)
            .select_related("part_type", "batch", "location")
            .order_by("-created_at")
        )[:50]
    )
    attach_part_identity(receiving_lots)  # exact-артикул отдельной колонкой
    ctx = {
        "object": obj,
        "object_kind": kind,
        "location": location,
        "step": step,
        "candidates": candidates,
        "catalog_choice": pending_context(request.session),
        "receiving_queue": queue_context(request.session),
        "error": error,
        "receiving_lots": receiving_lots,
        "history": history,
        "found_history": found_history,
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

    placed_lots = list(
        with_part_identity(
            StockLot.objects.filter(status__in=_MOVABLE_LOT_STATUSES)
            .select_related("part_type", "batch", "location")
            .order_by("part_type__name", "location__code")
        )[:50]
    )
    attach_part_identity(placed_lots)  # exact-артикул отдельной колонкой
    ctx = {
        "object": obj,
        "object_kind": kind,
        "location": location,
        "step": step,
        "candidates": candidates,
        "error": error,
        "info": info,
        "placed_lots": placed_lots,
        "history": (
            StockMovement.objects.filter(
                created_by=request.user, movement_type__in=_MOVE_HISTORY_TYPES
            ).select_related("part_type", "from_location", "to_location")[:10]
        ),
        "show_costs": request.user.can_view_purchase_cost,
    }
    return render(request, "core/move.html", ctx)
