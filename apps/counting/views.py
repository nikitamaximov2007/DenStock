"""Layer 32 — экраны пересчёта ячейки. View — оркестратор.

Все мутации через apps.counting.services. Скан идёт POST + redirect (PRG),
поэтому фокус в поле сканера и Enter работают без всякого JS. Доступ —
can_manage_inventory (роли склада).
"""
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, DecimalField, F, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.catalog.models import PartBarcode, PartNumber, PartType, normalize_number
from apps.polaris.services import find_polaris_by_number

from .forms import CountingStartForm
from .models import InventoryCountingLine, InventoryCountingSession
from .services import (
    DEFAULT_VALUE_SORT,
    VALUE_SORTS,
    CountingError,
    can_delete_session,
    cancel_session,
    convert_to_receipt,
    delete_session,
    find_brp_by_number,
    get_session_value_breakdown,
    post_session,
    record_scan,
    refresh_draft_prices,
    remove_line,
    resolve_unknown_to_brp,
    resolve_unknown_to_part,
    resolve_unknown_to_polaris,
    set_line_quantity,
    start_session,
    undo_last_scan,
)


def _require_manage(request) -> None:
    if not request.user.can_manage_inventory:
        raise PermissionDenied


@login_required
def counting_list(request):
    _require_manage(request)
    status = request.GET.get("status", "")
    qs = InventoryCountingSession.objects.select_related(
        "storage_location", "created_by", "converted_receipt"
    ).annotate(
        # Агрегаты по ОДНОЙ связи lines: без дублирования строк в join.
        unique_total=Count("lines"),
        scans_total=Sum("lines__scan_count"),
        quantity_total=Sum("lines__quantity_counted"),
        value_total=Sum(
            F("lines__quantity_counted") * F("lines__final_customer_price_rub"),
            output_field=DecimalField(max_digits=32, decimal_places=9),
        ),
    )
    if status:
        qs = qs.filter(status=status)
    sessions = list(qs[:100])
    for session in sessions:
        session.can_be_deleted = can_delete_session(session)
    return render(
        request,
        "counting/list.html",
        {
            "sessions": sessions,
            "status": status,
            "statuses": InventoryCountingSession.Status.choices,
        },
    )


@login_required
def counting_new(request):
    _require_manage(request)
    if request.method == "POST":
        form = CountingStartForm(request.POST)
        if form.is_valid():
            location = form.resolve_location()
            session = start_session(
                location=location, comment=form.cleaned_data["comment"], by=request.user
            )
            messages.success(
                request, f"Пересчёт начат для адреса {session.full_address}."
            )
            return redirect("counting_detail", pk=session.pk)
    else:
        form = CountingStartForm()
    return render(request, "counting/new.html", {"form": form})


@login_required
def counting_detail(request, pk):
    """Главная страница сканера: сгруппированная таблица + счётчики."""
    _require_manage(request)
    session = get_object_or_404(
        InventoryCountingSession.objects.select_related("storage_location", "converted_receipt"),
        pk=pk,
    )
    # Черновик освежает снимки цен по текущим каталогам: после реимпорта
    # прайса исправленные цены видны без повторного сканирования ячейки.
    refresh_draft_prices(session)
    lines = session.lines.select_related(
        "warehouse_part", "brp_catalog_part", "polaris_catalog_part"
    )
    return render(
        request,
        "counting/detail.html",
        {
            "session": session,
            "lines": lines,
            "counters": session.counters(),
            "breakdown": get_session_value_breakdown(
                session, sort=request.GET.get("value_sort", DEFAULT_VALUE_SORT)
            ),
            "value_sorts": VALUE_SORTS,
            "is_draft": session.is_draft,
        },
    )


@login_required
@require_POST
def counting_scan(request, pk):
    session = get_object_or_404(InventoryCountingSession, pk=pk)
    _require_manage(request)
    code = request.POST.get("code", "")
    try:
        line = record_scan(session, code, by=request.user)
    except CountingError as exc:
        messages.error(request, str(exc))
    else:
        label = {
            "warehouse": "склад",
            "brp_catalog": "BRP",
            "polaris_catalog": "Polaris",
            "unknown": "неизвестно",
        }.get(line.source, line.source)
        qty = format(line.quantity_counted.normalize(), "f")
        messages.success(
            request,
            f"{line.scanned_value}: {line.display_name} ({label}), количество {qty}",
        )
    # PRG: возврат на страницу с якорем на поле сканера (autofocus вернёт фокус).
    return redirect(reverse("counting_detail", args=[pk]) + "#scan")


@login_required
@require_POST
def counting_comment(request, pk):
    """Сохранить описание ячейки. Только метаданные: склад и сканы не трогает.

    Разрешено в любом статусе: полезное описание («Роллеры вариатора»)
    появляется уже после разбора ячейки, в том числе после проведения.
    """
    session = get_object_or_404(InventoryCountingSession, pk=pk)
    _require_manage(request)
    session.comment = (request.POST.get("comment") or "").strip()
    session.save(update_fields=["comment", "updated_at"])
    messages.success(request, "Описание ячейки сохранено.")
    return redirect("counting_detail", pk=pk)


@login_required
@require_POST
def counting_undo(request, pk):
    session = get_object_or_404(InventoryCountingSession, pk=pk)
    _require_manage(request)
    try:
        undone = undo_last_scan(session)
    except CountingError as exc:
        messages.error(request, str(exc))
    else:
        messages.info(request, "Последний скан отменён." if undone else "Отменять нечего.")
    return redirect("counting_detail", pk=pk)


@login_required
@require_POST
def counting_line_qty(request, pk):
    line = get_object_or_404(InventoryCountingLine, pk=pk)
    _require_manage(request)
    try:
        set_line_quantity(line, request.POST.get("quantity", ""))
    except CountingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Количество обновлено.")
    return redirect("counting_detail", pk=line.session_id)


@login_required
@require_POST
def counting_line_remove(request, pk):
    line = get_object_or_404(InventoryCountingLine, pk=pk)
    _require_manage(request)
    session_pk = line.session_id
    try:
        remove_line(line)
    except CountingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Строка удалена.")
    return redirect("counting_detail", pk=session_pk)


@login_required
@require_POST
def counting_line_resolve(request, pk):
    """Разобрать неизвестную строку: найти на складе, в BRP или Polaris."""
    line = get_object_or_404(InventoryCountingLine, pk=pk)
    _require_manage(request)
    code = (request.POST.get("code") or "").strip()
    norm = normalize_number(code)
    part_id = (
        PartNumber.objects.filter(normalized_value=norm).values_list("part_id", flat=True).first()
        or PartBarcode.objects.filter(value__iexact=code).values_list("part_id", flat=True).first()
    )
    try:
        if part_id:
            resolve_unknown_to_part(line, PartType.objects.get(pk=part_id))
            messages.success(request, "Строка привязана к складской карточке.")
        else:
            brp = find_brp_by_number(norm)
            polaris = find_polaris_by_number(norm)
            if brp and polaris:
                messages.error(
                    request,
                    "Этот номер есть в BRP и Polaris. Откройте общий поиск и выберите каталог.",
                )
            elif brp:
                resolve_unknown_to_brp(line, brp)
                messages.success(request, "Строка привязана к BRP-каталогу.")
            elif polaris:
                resolve_unknown_to_polaris(line, polaris)
                messages.success(request, "Строка привязана к Polaris-каталогу.")
            else:
                messages.error(request, "По этому номеру ничего не найдено.")
    except CountingError as exc:
        messages.error(request, str(exc))
    return redirect("counting_detail", pk=line.session_id)


@login_required
def counting_convert(request, pk):
    """Обзор сгруппированных результатов + создание черновика документа."""
    _require_manage(request)
    session = get_object_or_404(
        InventoryCountingSession.objects.select_related("storage_location", "converted_receipt"),
        pk=pk,
    )
    refresh_draft_prices(session)  # обзор и конвертация видят актуальные цены
    lines = session.lines.select_related(
        "warehouse_part", "brp_catalog_part", "polaris_catalog_part"
    )
    unknown_count = sum(1 for line in lines if line.needs_review)
    if request.method == "POST":
        try:
            convert_to_receipt(
                session, by=request.user, unit_cost=request.POST.get("unit_cost") or Decimal("0")
            )
        except CountingError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Черновик документа инвентаризации создан.")
            return redirect("counting_convert", pk=pk)
    return render(
        request,
        "counting/convert.html",
        {
            "session": session,
            "lines": lines,
            "counters": session.counters(),
            "unknown_count": unknown_count,
        },
    )


@login_required
@require_POST
def counting_post(request, pk):
    """Провести инвентаризацию: остаток пишется по адресу сессии.

    Layer 34: результат для пользователя — документ в разделе
    «Инвентаризация» (IC-номер), а не поступление: пересчёт — первичный
    ввод, внутренний документ проведения в «Поступлениях» не показывается.
    """
    _require_manage(request)
    session = get_object_or_404(InventoryCountingSession, pk=pk)
    try:
        post_session(session, by=request.user)
    except CountingError as exc:
        messages.error(request, str(exc))
        return redirect("counting_convert", pk=pk)
    session.refresh_from_db()
    messages.success(
        request,
        f"Пересчёт завершён. Создан документ инвентаризации "
        f"{session.inventory_number}: остаток записан по адресу "
        f"{session.full_address}.",
    )
    return redirect("initial_inventory_detail", pk=session.pk)


@login_required
def counting_delete(request, pk):
    """Удаление незавершённого черновика: GET — подтверждение, POST — удаление.

    Только черновик до «Завершить пересчёт»: завершённые, сконвертированные,
    проведённые и связанные с документом сессии не удаляются (проверка и в
    сервисе, не только в UI). Склад удаление не меняет.
    """
    _require_manage(request)
    session = get_object_or_404(
        InventoryCountingSession.objects.select_related("storage_location", "created_by"),
        pk=pk,
    )
    if request.method == "POST":
        try:
            address = delete_session(session)
        except CountingError as exc:
            messages.error(request, str(exc))
            return redirect("counting_list")
        messages.success(request, f"Черновик инвентаризации ячейки {address} удалён.")
        return redirect("counting_list")
    return render(
        request,
        "counting/delete.html",
        {
            "session": session,
            "counters": session.counters(),
            "can_delete": can_delete_session(session),
        },
    )


@login_required
@require_POST
def counting_cancel(request, pk):
    _require_manage(request)
    session = get_object_or_404(InventoryCountingSession, pk=pk)
    try:
        cancel_session(session)
    except CountingError as exc:
        messages.error(request, str(exc))
    else:
        messages.info(request, "Сессия отменена.")
    return redirect("counting_list")
