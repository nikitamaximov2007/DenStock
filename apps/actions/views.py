"""Layer 33 — экраны «Действий со склада». View — оркестратор.

Сканер работает как клавиатура: большое поле + Enter (GET-поиск), действие
проводится POST + redirect (PRG). Доступ: любое из прав продаж/резервов/
ремонта; каждый тип действия дополнительно проверяется по своему праву.
"""
import datetime
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse, HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import urlencode

from apps.catalog.models import PartType
from apps.warehouse.models import StorageLocation

from .models import PartCustomsInfo, WarehouseAction
from .services import (
    MULTI_LOCATION_MESSAGE,
    NOT_FOUND_MESSAGE,
    ActionError,
    actions_report,
    build_export_rows,
    cancel_warehouse_action,
    get_or_create_customs,
    perform_action,
    resolve_part,
    stock_overview,
)

# Подпись источника области применения для UI (см. part_export_data.application_source).
APPLICATION_SOURCE_LABELS = {
    "manual": "Указано вручную",
    "compatibility": "Определено по совместимости",
    "none": "Не заполнено",
}

ACTION_PERMISSIONS = {
    WarehouseAction.Type.SALE: "can_manage_sales",
    WarehouseAction.Type.RESERVE: "can_manage_reservations",
    WarehouseAction.Type.REPAIR: "can_manage_repairs",
}


def _allowed_actions(user) -> list:
    return [
        (value, label)
        for value, label in WarehouseAction.Type.choices
        if getattr(user, ACTION_PERMISSIONS[value])
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
    """Сканер действий: поиск остатков по скану + форма проведения."""
    _require_access(request)
    q = (request.GET.get("q") or "").strip()
    ctx = {
        "q": q,
        "searched": bool(q),
        "allowed_actions": _allowed_actions(request.user),
        "not_found_message": NOT_FOUND_MESSAGE,
        "multi_location_message": MULTI_LOCATION_MESSAGE,
    }
    if q:
        part = resolve_part(q)
        overview = stock_overview(part) if part else None
        if part is None or (overview and not overview["locations"] and not overview["unit_items"]):
            ctx["not_found"] = True
        else:
            ctx["overview"] = overview
    return render(request, "actions/scan.html", ctx)


@login_required
def actions_perform(request):
    """Провести действие (POST): Продажа / Резерв / Ремонт из выбранной ячейки."""
    _require_access(request)
    if request.method != "POST":
        return redirect("actions_scan")
    q = (request.POST.get("q") or "").strip()
    back = reverse("actions_scan") + (f"?{urlencode({'q': q})}" if q else "")
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
    try:
        action = perform_action(
            part=part,
            location=location,
            action_type=action_type,
            quantity=request.POST.get("quantity", ""),
            customer_comment=request.POST.get("customer_comment", ""),
            scanned_number=q,
            by=request.user,
        )
    except ActionError as exc:
        messages.error(request, str(exc))
        return redirect(back)
    qty = format(action.quantity.normalize(), "f")
    messages.success(
        request,
        f"Действие проведено: {action.get_action_type_display()}, "
        f"{qty} шт, {location.code}",
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
    # Таможня и Excel — только по активным действиям (без отменённых).
    active_actions = [a for a in actions if not a.is_cancelled]
    export_rows = build_export_rows(active_actions)
    ready = [r for r in export_rows if not r["warnings"]]
    # Готовность именно области применения — отдельно от прочих предупреждений
    # (название/вес/цена): «готово» значит явно заполнено вручную ИЛИ уверенно
    # определено по совместимости, «не заполнено» — ни того, ни другого.
    application_missing = [r for r in export_rows if not r["application_area"]]
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
            "application_ready_count": len(export_rows) - len(application_missing),
            "application_missing_count": len(application_missing),
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
            cancel_warehouse_action(
                action, by=request.user, reason=request.POST.get("reason", "")
            )
        except ActionError as exc:
            messages.error(request, str(exc))
            return redirect("actions_cancel", pk=pk)
        messages.success(
            request,
            f"Продажа отменена, остаток {action.quantity.normalize():f} шт "
            f"возвращён в ячейку {action.location_code or action.location.code}.",
        )
        return redirect("actions_report")
    return render(
        request,
        "actions/cancel.html",
        {"action": action, "can_cancel": action.status == WarehouseAction.Status.ACTIVE
         and action.action_type == WarehouseAction.Type.SALE},
    )


@login_required
def actions_export(request):
    """Скачать «Форму для заказа» (xlsx) по текущим фильтрам отчёта.

    Read-only: тот же набор действий, что показывает отчёт, и только активные
    (отменённые в таможенный экспорт не попадают, как и в блоке готовности).
    """
    _require_access(request)
    from .services import export_customs_xlsx

    filters = _report_filters(request)
    actions, _totals = actions_report(**filters)  # include_cancelled=False
    buffer = export_customs_xlsx(actions)
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
        customs.customs_name_source = (
            customs.NameSource.MANUAL if name_ru else customs.NameSource.AUTO
        )
        for field in ("gross_weight_kg", "net_weight_kg"):
            raw = (request.POST.get(field) or "").strip().replace(",", ".")
            if raw:
                try:
                    setattr(customs, field, Decimal(raw))
                except InvalidOperation:
                    messages.error(request, "Некорректный вес: используйте число в кг.")
                    return redirect("actions_customs_edit", part_id=part.pk)
            else:
                setattr(customs, field, None)
        customs.weight_source_url = (request.POST.get("weight_source_url") or "").strip()
        customs.weight_source_note = (request.POST.get("weight_source_note") or "").strip()
        customs.weight_verified = bool(request.POST.get("weight_verified"))
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
            "next": request.GET.get("next", ""),
        },
    )


@login_required
def actions_customs_application(request, part_id):
    """Быстрое построчное сохранение области применения (готовность к экспорту).

    Только POST — просмотр страницы готовности НЕ создаёт PartCustomsInfo
    (список строится через read_customs, см. build_export_rows). Строка
    создаётся здесь, только когда пользователь явно сохраняет выбор. Ту же
    точную PartType, что показана в строке экспорта — BRP и Polaris с
    одинаковым номером здесь не смешиваются, потому что part_id указывает на
    конкретную складскую карточку, а не на каталожный номер.
    """
    _require_access(request)
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    part = get_object_or_404(PartType, pk=part_id)
    next_url = request.POST.get("next") or reverse("actions_report")
    value = (request.POST.get("application_area") or "").strip().upper()
    if value and value not in PartCustomsInfo.ApplicationArea.values:
        messages.error(request, "Недопустимая область применения.")
        return redirect(next_url)
    with transaction.atomic():
        customs, _created = PartCustomsInfo.objects.get_or_create(part_type=part)
        customs.application_area = value
        customs.updated_by = request.user
        customs.save(update_fields=["application_area", "updated_by", "updated_at"])
    messages.success(request, "Область применения сохранена.")
    return redirect(next_url)
