"""Layer 33 — экраны «Действий со склада». View — оркестратор.

Сканер работает как клавиатура: большое поле + Enter (GET-поиск), действие
проводится POST + redirect (PRG). Доступ: любое из прав продаж/резервов/
ремонта; каждый тип действия дополнительно проверяется по своему праву.
"""

import datetime
import secrets

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode

from apps.catalog.models import PartType
from apps.core.part_lookup import MatchSource, resolve_part_lookup
from apps.core.templatetags.number_format import quantity_int
from apps.customers.models import Customer
from apps.inventory.presentation import identity_for_part_ids
from apps.warehouse.models import StorageLocation

from .cart import (
    CART_KINDS,
    KIND_REPAIR,
    KIND_SALE,
    add_scan,
    cart_rows,
    cart_total,
    clear_cart,
    complete_cart,
    discard_cart,
    load_cart,
    open_cart,
    parse_row_key,
    remove_row,
    set_row_quantity,
)
from .models import PartCustomsInfo, WarehouseAction
from .services import (
    IDENTITY_MISMATCH_MESSAGE,
    MANUAL_WEIGHT_NOTE,
    MULTI_LOCATION_MESSAGE,
    NOT_FOUND_MESSAGE,
    ActionError,
    actions_report,
    cancel_warehouse_action,
    get_or_create_customs,
    historical_customs_rows,
    parse_customs_usd,
    parse_weight_kg,
    perform_action,
    stock_overview,
    validate_weight_pair,
)

# Подписи источников для UI (см. part_export_data.application_source/weight_source).
APPLICATION_SOURCE_LABELS = {
    "manual": "Указано вручную",
    "compatibility": "Определено по совместимости",
    "none": "Не заполнено",
}
WEIGHT_SOURCE_LABELS = {
    "manual": "Указано вручную",
    "sourced": "Получено из источника",
    "none": "Не заполнено",
}

# Сентинел «поле не передано в POST» — отличаем от «передано пустым» (сброс).
_UNSET = object()

ACTION_PERMISSIONS = {
    WarehouseAction.Type.SALE: "can_manage_sales",
    WarehouseAction.Type.RESERVE: "can_manage_reservations",
    WarehouseAction.Type.REPAIR: "can_manage_repairs",
}


# Корзина живёт в сессии пользователя: там только id черновика документа,
# сам состав — в БД, поэтому корзина переживает перезагрузку страницы.
CART_SESSION_KEYS = {
    KIND_SALE: "actions_cart_sale",
    KIND_REPAIR: "actions_cart_repair",
}
CART_TITLES = {KIND_SALE: "Продажа", KIND_REPAIR: "Выдача в ремонт"}
# Что именно отсканировали по каждой позиции: снимок номера в отчёте должен
# остаться таким же точным, как при одиночном проведении. Первый скан позиции
# и определяет номер: дальше это уже та же canonical деталь.
CART_SCANS_SESSION_KEY = "actions_cart_scans"


def _allowed_actions(user) -> list:
    return [
        (value, label)
        for value, label in WarehouseAction.Type.choices
        if value in ACTION_PERMISSIONS and getattr(user, ACTION_PERMISSIONS[value])
    ]


def _require_access(request) -> None:
    if not _allowed_actions(request.user):
        raise PermissionDenied


def _parse_date(value):
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


@login_required
def actions_scan(request):
    """Сканер действий: scan добавляет в draft, проведение отдельным submit."""
    _require_access(request)
    q = (request.GET.get("q") or "").strip()
    allowed_actions = _allowed_actions(request.user)
    selected_action_kind = request.GET.get("kind") or allowed_actions[0][0]
    if selected_action_kind not in {value for value, _label in allowed_actions}:
        selected_action_kind = allowed_actions[0][0]
    ctx = {
        "q": q,
        "searched": bool(q),
        "allowed_actions": allowed_actions,
        "not_found_message": NOT_FOUND_MESSAGE,
        "multi_location_message": MULTI_LOCATION_MESSAGE,
        "cart_panels": _cart_panels(request),
        "cart_token": secrets.token_urlsafe(32),
        "selected_action_kind": selected_action_kind,
        "customers": Customer.objects.order_by("name", "pk")[:500],
        "selected_customer_id": request.GET.get("customer_id", ""),
    }
    if q:
        lookup = resolve_part_lookup(q, include_price=request.user.can_view_purchase_cost)
        selected_part = None
        if lookup.ambiguous:
            selected_part_id = request.GET.get("part_id")
            selected_part = next(
                (
                    candidate.part
                    for candidate in lookup.candidates
                    if str(candidate.part.pk) == selected_part_id
                    and candidate.match_source
                    in {
                        MatchSource.EXACT,
                        MatchSource.BARCODE,
                    }
                ),
                None,
            )
            if selected_part is None:
                ctx["lookup_candidates"] = lookup.candidates
                ctx["lookup_message"] = lookup.message
        part = selected_part or (lookup.candidate.part if lookup.found else None)
        overview = stock_overview(part) if part else None
        has_no_stock = overview and not overview["locations"] and not overview["unit_items"]
        unresolved_ambiguity = lookup.ambiguous and selected_part is None
        if not unresolved_ambiguity and (part is None or has_no_stock):
            ctx["not_found"] = True
        elif not unresolved_ambiguity:
            ctx["overview"] = overview
            ctx["request_token"] = secrets.token_urlsafe(32)
    return render(request, "actions/scan.html", ctx)


@login_required
def actions_cart_scan(request):
    """Resolve one scan and add it to the selected draft when location is unambiguous."""
    _require_access(request)
    if request.method != "POST":
        return redirect("actions_scan")
    q = (request.POST.get("q") or "").strip()
    kind_value = request.POST.get("kind", "")
    permission = ACTION_PERMISSIONS.get(kind_value)
    if permission is None or not getattr(request.user, permission, False):
        raise PermissionDenied
    kind = kind_value
    back = reverse("actions_scan") + f"?{urlencode({'kind': kind, 'q': q})}"
    if not q:
        messages.error(request, "Отсканируйте номер детали.")
        return redirect(back)
    lookup = resolve_part_lookup(q, include_price=request.user.can_view_purchase_cost)
    if lookup.ambiguous or not lookup.found:
        messages.error(request, lookup.message or NOT_FOUND_MESSAGE)
        return redirect(back)
    if kind not in CART_KINDS:
        # Резерв — отдельная немедленная операция, а не тип многострочной
        # корзины. Сохраняем прежнюю возможность, но не списываем товар при
        # сканировании.
        return redirect(reverse("actions_scan") + f"?{urlencode({'q': q, 'kind': kind})}")
    part = lookup.candidate.part
    overview = stock_overview(part)
    if not overview["locations"]:
        messages.error(request, NOT_FOUND_MESSAGE)
        return redirect(back)
    if len(overview["locations"]) != 1:
        messages.warning(request, MULTI_LOCATION_MESSAGE)
        return redirect(reverse("actions_scan") + f"?{urlencode({'q': q, 'kind': kind})}")
    location = overview["locations"][0]["location"]
    cart = _cart_for(request, kind, create=True)
    try:
        row = add_scan(cart, part, location, by=request.user)
    except ActionError as exc:
        messages.error(request, str(exc))
        if not cart_rows(cart):
            discard_cart(cart, by=request.user)
            _forget_cart(request, kind)
        return redirect(back)
    _remember_scan(request, kind, row.key, q)
    messages.success(
        request, f"Добавлено: {part.name}, {quantity_int(row.quantity)} шт, {location.code}."
    )
    return redirect(back)


@login_required
def actions_perform(request):
    """Провести действие (POST): Продажа / Резерв / Ремонт из выбранной ячейки."""
    _require_access(request)
    if request.method != "POST":
        return redirect("actions_scan")
    q = (request.POST.get("q") or "").strip()
    action_kind = request.POST.get("action_type", "")
    back_params = {"q": q, "kind": action_kind}
    back = reverse("actions_scan") + f"?{urlencode(back_params)}"
    part = get_object_or_404(PartType, pk=request.POST.get("part_id"))
    location_id = request.POST.get("location_id")
    if not location_id:
        messages.error(request, "Выберите ячейку списания.")
        return redirect(back)
    location = get_object_or_404(StorageLocation, pk=location_id)
    action_type = request.POST.get("action_type", "")
    permission = ACTION_PERMISSIONS.get(action_type)
    if permission is None or not getattr(request.user, permission):
        raise PermissionDenied
    if not q:
        messages.error(request, IDENTITY_MISMATCH_MESSAGE)
        return redirect(back)
    try:
        action = perform_action(
            part=part,
            location=location,
            action_type=action_type,
            quantity=request.POST.get("quantity", ""),
            customer_comment=request.POST.get("customer_comment", ""),
            scanned_number=q,
            by=request.user,
            request_token=request.POST.get("request_token"),
        )
    except ActionError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    qty = quantity_int(action.quantity)
    # Идентификация детали в подтверждении: название + exact-артикул из
    # снимка действия — не внутренний лот.
    identity = action.part_name or str(part)
    if action.part_number:
        identity += f", артикул {action.part_number}"
    messages.success(
        request,
        f"Действие проведено: {action.get_action_type_display()} — {identity}, "
        f"{qty} шт, {location.code}",
    )
    return redirect(back)


def _check_cart_kind(request, kind: str) -> str:
    """Корзина есть только у продажи и ремонта; резерв проводится сразу."""
    if kind not in CART_KINDS:
        raise ActionError("Корзина доступна для продажи и выдачи в ремонт.")
    permission = ACTION_PERMISSIONS.get(kind)
    if permission is None or not getattr(request.user, permission):
        raise PermissionDenied
    return kind


def _cart_for(request, kind: str, *, create=False):
    key = CART_SESSION_KEYS[kind]
    cart = load_cart(kind, request.session.get(key))
    if cart is not None:
        return cart
    # Черновик проведён/удалён в другой вкладке — забываем его.
    request.session.pop(key, None)
    if not create:
        return None
    cart = open_cart(kind, by=request.user)
    request.session[key] = cart.pk
    return cart


def _forget_cart(request, kind: str) -> None:
    request.session.pop(CART_SESSION_KEYS[kind], None)
    _drop_scans(request, kind)


def _scan_key(kind: str, row_key: str) -> str:
    return f"{kind}:{row_key}"


def _remember_scan(request, kind: str, row_key: str, scanned: str) -> None:
    scanned = (scanned or "").strip()
    if not scanned:
        return
    scans = request.session.get(CART_SCANS_SESSION_KEY) or {}
    scans.setdefault(_scan_key(kind, row_key), scanned)
    request.session[CART_SCANS_SESSION_KEY] = scans


def _forget_scan(request, kind: str, row_key: str) -> None:
    scans = request.session.get(CART_SCANS_SESSION_KEY) or {}
    if scans.pop(_scan_key(kind, row_key), None) is not None:
        request.session[CART_SCANS_SESSION_KEY] = scans


def _drop_scans(request, kind: str) -> None:
    scans = request.session.get(CART_SCANS_SESSION_KEY) or {}
    kept = {key: value for key, value in scans.items() if not key.startswith(f"{kind}:")}
    if kept != scans:
        request.session[CART_SCANS_SESSION_KEY] = kept


def _scans_for(request, kind: str) -> dict:
    prefix = f"{kind}:"
    scans = request.session.get(CART_SCANS_SESSION_KEY) or {}
    return {key[len(prefix) :]: value for key, value in scans.items() if key.startswith(prefix)}


def _cart_panels(request) -> list:
    """Непустые корзины пользователя для показа на странице сканера."""
    panels = []
    for kind in CART_KINDS:
        permission = ACTION_PERMISSIONS.get(kind)
        if not getattr(request.user, permission, False):
            continue
        cart = _cart_for(request, kind)
        if cart is None:
            continue
        rows = cart_rows(cart)
        if not rows:
            continue
        identities = identity_for_part_ids([row.part.pk for row in rows])
        q = (request.GET.get("q") or "").strip()
        clear_url = reverse("actions_cart_clear", args=[kind])
        panels.append(
            {
                "kind": kind,
                "title": CART_TITLES[kind],
                "clear_url": clear_url + (f"?{urlencode({'q': q})}" if q else ""),
                "document": cart,
                "is_sale": kind == KIND_SALE,
                "rows": [
                    {
                        "row": row,
                        "identity": identities.get(row.part.pk),
                    }
                    for row in rows
                ],
                "positions": len(rows),
                "total": cart_total(cart) if kind == KIND_SALE else None,
            }
        )
    return panels


def _scan_back(request) -> str:
    q = (request.POST.get("q") or "").strip()
    kind = request.POST.get("kind") or request.POST.get("action_type") or ""
    params = {"q": q, "kind": kind}
    return reverse("actions_scan") + f"?{urlencode(params)}"


@login_required
def actions_cart_add(request):
    """Добавить отсканированную деталь в корзину (склад не меняется)."""
    _require_access(request)
    if request.method != "POST":
        return redirect("actions_scan")
    back = _scan_back(request)
    try:
        kind = _check_cart_kind(request, request.POST.get("action_type", ""))
    except ActionError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    part = get_object_or_404(PartType, pk=request.POST.get("part_id"))
    location_id = request.POST.get("location_id")
    if not location_id:
        messages.error(request, "Выберите ячейку списания.")
        return redirect(back)
    location = get_object_or_404(StorageLocation, pk=location_id)
    cart = _cart_for(request, kind, create=True)
    try:
        row = add_scan(
            cart, part, location, quantity=request.POST.get("quantity", "1"), by=request.user
        )
    except ActionError as exc:
        messages.error(request, str(exc))
        # Не оставляем пустой черновик, если самая первая позиция не прошла.
        if not cart_rows(cart):
            discard_cart(cart, by=request.user)
            _forget_cart(request, kind)
        return redirect(back)
    _remember_scan(request, kind, row.key, request.POST.get("q", ""))
    messages.success(
        request,
        f"В корзину «{CART_TITLES[kind]}»: {part.name}, "
        f"{quantity_int(row.quantity)} шт, {location.code}. Склад не изменён.",
    )
    return redirect(back)


@login_required
def actions_cart_update(request):
    """Изменить количество позиции, удалить её или очистить корзину."""
    _require_access(request)
    if request.method != "POST":
        return redirect("actions_scan")
    back = _scan_back(request)
    try:
        kind = _check_cart_kind(request, request.POST.get("kind", ""))
    except ActionError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    cart = _cart_for(request, kind)
    if cart is None:
        messages.error(request, "Корзина уже пуста.")
        return redirect(back)

    operation = request.POST.get("operation", "")
    try:
        part_id, location_id = parse_row_key(request.POST.get("row_key", ""))
    except ActionError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    part = get_object_or_404(PartType, pk=part_id)
    location = get_object_or_404(StorageLocation, pk=location_id)

    row_key = f"{part.pk}:{location.pk}"
    if operation == "remove":
        remove_row(cart, part, location, by=request.user)
        _forget_scan(request, kind, row_key)
        messages.success(request, f"Позиция убрана из корзины: {part.name}.")
    elif operation == "set":
        try:
            raw_unit_price = request.POST.get("unit_price")
            row = set_row_quantity(
                cart,
                part,
                location,
                request.POST.get("quantity", ""),
                unit_price=(raw_unit_price or None) if kind == KIND_REPAIR else None,
                by=request.user,
            )
        except ActionError as exc:
            messages.error(request, str(exc))
            return redirect(back)
        if row is None:
            _forget_scan(request, kind, row_key)
            messages.success(request, f"Позиция убрана из корзины: {part.name}.")
        else:
            messages.success(
                request, f"Количество обновлено: {part.name}, {quantity_int(row.quantity)} шт."
            )
    else:
        messages.error(request, "Неизвестная операция с корзиной.")
        return redirect(back)

    if not cart_rows(cart):
        discard_cart(cart, by=request.user)
        _forget_cart(request, kind)
    return redirect(back)


@login_required
def actions_cart_clear(request, kind):
    """Очистка корзины: GET — подтверждение с составом, POST — очистка.

    Корзина ничего не списывала, поэтому очистка не трогает остатки — удаляются
    только набранные позиции и сам черновик.
    """
    _require_access(request)
    try:
        kind = _check_cart_kind(request, kind)
    except ActionError as exc:
        messages.error(request, str(exc))
        return redirect("actions_scan")
    q = (request.GET.get("q") or request.POST.get("q") or "").strip()
    back = reverse("actions_scan") + (f"?{urlencode({'q': q})}" if q else "")
    cart = _cart_for(request, kind)
    if request.method == "POST":
        if cart is not None:
            clear_cart(cart, by=request.user)
            discard_cart(cart, by=request.user)
        _forget_cart(request, kind)
        messages.success(request, f"Корзина «{CART_TITLES[kind]}» очищена. Склад не изменён.")
        return redirect(back)
    return render(
        request,
        "actions/cart_clear.html",
        {
            "kind": kind,
            "title": CART_TITLES[kind],
            "rows": cart_rows(cart) if cart is not None else [],
            "back": back,
            "q": q,
        },
    )


@login_required
def actions_cart_complete(request):
    """Провести корзину одним документом: здесь и только здесь меняется склад."""
    _require_access(request)
    if request.method != "POST":
        return redirect("actions_scan")
    back = _scan_back(request)
    try:
        kind = _check_cart_kind(request, request.POST.get("kind", ""))
    except ActionError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    cart = _cart_for(request, kind)
    if cart is None:
        messages.error(request, "Корзина пуста: отсканируйте хотя бы одну деталь.")
        return redirect(back)
    try:
        customer = get_object_or_404(Customer, pk=request.POST.get("customer_id"))
        actions = complete_cart(
            cart,
            customer=customer,
            by=request.user,
            scanned_numbers=_scans_for(request, kind),
            request_token=request.POST.get("request_token"),
        )
    except ActionError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    _forget_cart(request, kind)
    total_qty = sum(action.quantity for action in actions)
    messages.success(
        request,
        f"{CART_TITLES[kind]} проведена: позиций {len(actions)}, {quantity_int(total_qty)} шт.",
    )
    return redirect(back)


def _report_filters(request) -> dict:
    """Фильтры отчёта из GET. Общий парсер для HTML-отчёта и Excel-экспорта."""
    return {
        "date_from": _parse_date(request.GET.get("date_from", "")),
        "date_to": _parse_date(request.GET.get("date_to", "")),
        "action_type": request.GET.get("action_type", ""),
        "q": (request.GET.get("q") or "").strip(),
        "part_number": (request.GET.get("part_number") or "").strip(),
        "location_code": (request.GET.get("location_code") or "").strip(),
    }


@login_required
def actions_report_view(request):
    """Единый отчёт действий со склада + подготовка таможенного экспорта."""
    _require_access(request)
    show_cancelled = request.GET.get("cancelled") == "1"
    filters = _report_filters(request)
    actions, totals = actions_report(include_cancelled=show_cancelled, **filters)
    actions = list(actions[:500])
    export_rows = historical_customs_rows(**filters)
    ready = [r for r in export_rows if not r["warnings"]]
    # Готовность к таможенному экспорту (Layer 33.1): область применения +
    # оба веса одной штуки. Цена и название сюда не входят - у них своя
    # генерическая таблица предупреждений выше (ready_count/warning_count).
    for row in export_rows:
        row["gross_weight_total_kg"] = (
            row["gross_weight_kg"] * row["quantity"] if row["gross_weight_kg"] is not None else None
        )  # только для отображения: quantity меняется от экспорта к экспорту,
        # само значение никогда не сохраняется обратно в PartCustomsInfo.
    customs_missing = [r for r in export_rows if not r["customs_ready"]]
    return render(
        request,
        "actions/report.html",
        {
            "actions": actions,
            "totals": totals,
            "filters": filters,
            "show_cancelled": show_cancelled,
            "types": WarehouseAction.Type.choices,
            "export_rows": export_rows,
            "ready_count": len(ready),
            "warning_count": len(export_rows) - len(ready),
            "customs_ready_count": len(export_rows) - len(customs_missing),
            "customs_missing_count": len(customs_missing),
            "customs_absent_count": sum(1 for r in export_rows if not r["customs_entered"]),
            "application_choices": PartCustomsInfo.ApplicationArea.choices,
            "export_query": request.GET.urlencode(),
            "current_path_query": request.get_full_path(),
            "can_cancel": request.user.is_admin or request.user.is_manager,
        },
    )


@login_required
def actions_cancel(request, pk):
    """Отмена ошибочной продажи: GET — подтверждение, POST — возврат остатка.

    Доступ — администратор/руководитель. Возврат физического остатка и
    сторно делает сервис (транзакция); здесь только UI и причина.
    """
    if not (request.user.is_admin or request.user.is_manager):
        raise PermissionDenied
    action = get_object_or_404(
        WarehouseAction.objects.select_related("part_type", "location", "sale"), pk=pk
    )
    if request.method == "POST":
        try:
            cancel_warehouse_action(action, by=request.user, reason=request.POST.get("reason", ""))
        except ActionError as exc:
            messages.error(request, str(exc))
            return redirect("actions_cancel", pk=pk)
        messages.success(
            request,
            f"Продажа отменена, остаток {quantity_int(action.quantity)} шт "
            f"возвращён в ячейку {action.location_code or action.location.code}.",
        )
        return redirect("actions_report")
    return render(
        request,
        "actions/cancel.html",
        {
            "action": action,
            "can_cancel": action.status == WarehouseAction.Status.ACTIVE
            and action.action_type == WarehouseAction.Type.SALE,
        },
    )


@login_required
def actions_export(request):
    """Скачать «Форму для заказа» (xlsx) по текущим фильтрам отчёта.

    Read-only: тот же набор действий, что показывает отчёт, и только активные
    (отменённые в таможенный экспорт не попадают, как и в блоке готовности).
    Файл содержит оптовую цену в USD, поэтому одного права на проведение
    складских действий недостаточно: нужно также право просмотра закупочной
    стоимости.
    """
    _require_access(request)
    if not request.user.can_view_purchase_cost:
        raise PermissionDenied
    from .services import export_customs_xlsx

    filters = _report_filters(request)
    rows = historical_customs_rows(**filters)
    # Неполная историческая карточка не должна превратить экспорт в частично
    # заполненный XLSX. Предпросмотр уже показывает точные причины; скачивание
    # разрешается только когда каждая строка готова к декларации.
    missing = [row for row in rows if not row["customs_ready"]]
    if missing:
        messages.error(
            request,
            f"Нельзя сформировать Excel: у {len(missing)} строк не заполнены таможенные данные.",
        )
        return redirect(f"{reverse('actions_report')}?{urlencode(request.GET)}")
    buffer = export_customs_xlsx(rows=rows)
    date_from = filters["date_from"] or datetime.date.today()
    date_to = filters["date_to"] or datetime.date.today()
    filename = f"customs_order_{date_from}_{date_to}.xlsx"
    response = HttpResponse(
        buffer.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def actions_customs_edit(request, part_id):
    """Таможенные данные детали: RU-название, веса (только ручные), источник."""
    _require_access(request)
    part = get_object_or_404(PartType, pk=part_id)
    customs = get_or_create_customs(part)
    if request.method == "POST":
        name_ru = (request.POST.get("customs_name_ru") or "").strip()
        customs.customs_name_ru = name_ru
        customs.customs_name_en = (request.POST.get("customs_name_en") or "").strip()
        customs.manufacturer = (request.POST.get("manufacturer") or "").strip().upper()
        customs.country_of_origin = (request.POST.get("country_of_origin") or "").strip().upper()
        customs.source_reference = (request.POST.get("source_reference") or "").strip()
        customs.customs_name_source = (
            customs.NameSource.MANUAL if name_ru else customs.NameSource.AUTO
        )
        try:
            gross = parse_weight_kg(request.POST.get("gross_weight_kg"))
            net = parse_weight_kg(request.POST.get("net_weight_kg"))
            customs_price = parse_customs_usd(request.POST.get("customs_unit_price_usd"))
            validate_weight_pair(gross, net)
        except ValueError as exc:
            messages.error(request, str(exc))
            return redirect("actions_customs_edit", part_id=part.pk)
        customs.gross_weight_kg = gross
        customs.net_weight_kg = net
        customs.customs_unit_price_usd = customs_price
        customs.weight_source_url = (request.POST.get("weight_source_url") or "").strip()
        customs.weight_source_note = (request.POST.get("weight_source_note") or "").strip()
        # Чекбокс — явное решение пользователя (здесь есть поля источника:
        # вес мог быть записан с непроверенной страницы). Guard единый с
        # быстрым редактором: неполную пару весов подтвердить нельзя.
        customs.weight_verified = (
            bool(request.POST.get("weight_verified")) and gross is not None and net is not None
        )
        application_area = (request.POST.get("application_area") or "").strip().upper()
        if application_area and application_area not in PartCustomsInfo.ApplicationArea.values:
            messages.error(request, "Недопустимая область применения.")
            return redirect("actions_customs_edit", part_id=part.pk)
        customs.application_area = application_area  # "" = не заполнено, не легаси-хардкод
        customs.updated_by = request.user
        customs.save()
        messages.success(request, "Таможенные данные сохранены.")
        next_url = request.POST.get("next") or reverse("actions_report")
        return redirect(next_url)
    from .services import auto_customs_name_ru, part_export_data

    data = part_export_data(part)
    return render(
        request,
        "actions/customs_form.html",
        {
            "part": part,
            "customs": customs,
            "auto_name": auto_customs_name_ru(data["name_en"]),
            "data": data,
            "application_choices": PartCustomsInfo.ApplicationArea.choices,
            "application_source_label": APPLICATION_SOURCE_LABELS[data["application_source"]],
            "weight_source_label": WEIGHT_SOURCE_LABELS[data["weight_source"]],
            "next": request.GET.get("next", ""),
        },
    )


@login_required
def actions_customs_quick_save(request, part_id):
    """Быстрое построчное сохранение таможенных данных (готовность к экспорту).

    Одна форма/кнопка на строку сохраняет область применения и оба веса
    одной штуки вместе, но НЕЗАВИСИМО: если в POST нет ключа для какого-то
    поля, оно не трогается (пользователь мог поменять только одно поле —
    смена области применения не должна стирать сохранённые веса, и наоборот).

    Только POST — просмотр страницы готовности НЕ создаёт PartCustomsInfo
    (список строится через read_customs, см. build_export_rows). Строка
    создаётся здесь, только когда пользователь явно сохраняет. part_id —
    конкретная складская карточка (PartType): BRP и Polaris с одинаковым
    номером здесь не смешиваются, а replacement/superseded не может получить
    вес exact-детали — это физически другая карточка со своим PartCustomsInfo.

    weight_verified: ручной ввод здесь — само по себе подтверждение веса
    (полей источника в быстрой форме нет). Если веса затронуты в POST,
    флаг пересчитывается от итоговой пары: оба валидных веса -> True,
    неполная пара (в т.ч. после удаления одного из весов) -> False. Смена
    только области применения (ключей веса нет в POST) флаг не трогает.
    В weight_source_note пишется маркер «Указано вручную сотрудником» —
    только если URL и заметка пусты: реальный источник из детальной формы
    не затирается, фиктивный URL не выдумывается.
    """
    _require_access(request)
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    part = get_object_or_404(PartType, pk=part_id)
    next_url = request.POST.get("next") or reverse("actions_report")

    application_area = _UNSET
    if "application_area" in request.POST:
        application_area = (request.POST.get("application_area") or "").strip().upper()
        if application_area and application_area not in PartCustomsInfo.ApplicationArea.values:
            messages.error(request, "Недопустимая область применения.")
            return redirect(next_url)

    gross = net = _UNSET
    try:
        if "gross_weight_kg" in request.POST:
            gross = parse_weight_kg(request.POST.get("gross_weight_kg"))
        if "net_weight_kg" in request.POST:
            net = parse_weight_kg(request.POST.get("net_weight_kg"))
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(next_url)

    error_message = None
    with transaction.atomic():
        customs, _created = PartCustomsInfo.objects.get_or_create(part_type=part)
        update_fields = ["updated_by", "updated_at"]
        if application_area is not _UNSET:
            customs.application_area = application_area
            update_fields.append("application_area")
        if gross is not _UNSET:
            customs.gross_weight_kg = gross
            update_fields.append("gross_weight_kg")
        if net is not _UNSET:
            customs.net_weight_kg = net
            update_fields.append("net_weight_kg")
        try:
            validate_weight_pair(customs.gross_weight_kg, customs.net_weight_kg)
        except ValueError as exc:
            # Откатываем и вставку get_or_create для новой строки, и правки:
            # невалидная строка не должна оставлять после себя пустой ряд.
            transaction.set_rollback(True)
            error_message = str(exc)
        else:
            if gross is not _UNSET or net is not _UNSET:
                both_weights = (
                    customs.gross_weight_kg is not None and customs.net_weight_kg is not None
                )
                customs.weight_verified = both_weights
                update_fields.append("weight_verified")
                if (
                    both_weights
                    and not customs.weight_source_url.strip()
                    and (customs.weight_source_note.strip() in ("", MANUAL_WEIGHT_NOTE))
                ):
                    customs.weight_source_note = MANUAL_WEIGHT_NOTE
                    update_fields.append("weight_source_note")
            customs.updated_by = request.user
            customs.save(update_fields=update_fields)
    if error_message:
        messages.error(request, error_message)
        return redirect(next_url)
    messages.success(request, "Таможенные данные сохранены.")
    return redirect(next_url)
