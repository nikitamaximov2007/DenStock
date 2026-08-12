from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.core.part_lookup import resolve_part_lookup
from apps.warehouse.models import StorageLocation

from .models import SectionRecount, SectionRecountLine
from .section_recount import (
    SECTION_CODE,
    SectionRecountError,
    _allocation_statuses,
    _candidate_batch_lines,
    allocate_section_line,
    apply_section_recount,
    build_section_dry_run,
    cancel_section_recount,
    cell_recount_preview,
    complete_section_cell,
    create_cell_recount,
    create_section_recount,
    mark_section_ready,
    record_section_part,
    record_section_scan,
    remove_section_line,
    set_section_line_quantity,
    start_section_recount,
)


def _require_section_recount(request) -> None:
    if not request.user.can_manage_stocktaking:
        raise PermissionDenied


def _detail_context(doc, *, query=""):
    doc = (
        SectionRecount.objects.select_related("created_by")
        .prefetch_related("cells__location", "lines__cell__location", "lines__part_type")
        .get(pk=doc.pk)
    )
    lines = list(doc.lines.all())
    for line in lines:
        line.batch_candidates = _candidate_batch_lines(line)
        for batch_line in line.batch_candidates:
            batch_line.section_statuses = _allocation_statuses(doc, batch_line.pk)
        line.show_allocation_form = any(
            len(batch_line.section_statuses) > 0 for batch_line in line.batch_candidates
        ) and (
            len(line.batch_candidates) > 1
            or any(len(batch_line.section_statuses) > 1 for batch_line in line.batch_candidates)
        )
    search_result = (
        resolve_part_lookup(query, allow_partial=True, allow_name=True)
        if query.strip()
        else None
    )
    return {
        "doc": doc,
        "cells": list(doc.cells.all()),
        "lines": lines,
        "dry_run": (
            build_section_dry_run(doc)
            if doc.status != SectionRecount.Status.DRAFT
            else None
        ),
        "can_edit": doc.is_mutable,
        "counted_positions": len([line for line in lines if line.quantity > 0]),
        "counted_units": sum((line.quantity for line in lines), 0),
        "query": query,
        "search_result": search_result,
    }


@login_required
def section_recount_list(request):
    _require_section_recount(request)
    return render(
        request,
        "stocktaking/section_recount_list.html",
        {
            "documents": SectionRecount.objects.select_related("created_by")[:100],
            "new_url": reverse("section_recount_new"),
        },
    )


@login_required
def section_recount_new(request):
    _require_section_recount(request)
    drawers = StorageLocation.objects.filter(
        level=StorageLocation.Level.DRAWER,
        is_active=True,
    ).order_by("code", "pk")
    if request.method == "POST":
        try:
            drawer_id = request.POST.get("drawer_id")
            section_code = None
            if drawer_id:
                section_code = get_object_or_404(drawers, pk=drawer_id).code
            doc = create_section_recount(
                section_code=section_code or SECTION_CODE,
                by=request.user,
            )
        except SectionRecountError as exc:
            messages.error(request, str(exc))
        else:
            return redirect("section_recount_detail", pk=doc.pk)
    return render(
        request,
        "stocktaking/section_recount_new.html",
        {"drawers": drawers},
    )


@login_required
def cell_recount_new(request, location_pk):
    _require_section_recount(request)
    location = get_object_or_404(
        StorageLocation,
        pk=location_pk,
        level=StorageLocation.Level.CELL,
    )
    preview = cell_recount_preview(location)
    if request.method == "POST":
        try:
            doc = create_cell_recount(location=location, by=request.user)
        except SectionRecountError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(
                request,
                f"Ячейка {location.code} заблокирована. Можно начинать физический пересчёт.",
            )
            return redirect("section_recount_detail", pk=doc.pk)
    return render(
        request,
        "stocktaking/cell_recount_new.html",
        {"location": location, "preview": preview},
    )


@login_required
def section_recount_detail(request, pk):
    _require_section_recount(request)
    doc = get_object_or_404(SectionRecount, pk=pk)
    template = (
        "stocktaking/cell_recount_detail.html"
        if doc.is_cell_recount
        else "stocktaking/section_recount_detail.html"
    )
    return render(request, template, _detail_context(doc, query=request.GET.get("q", "")))


@login_required
@require_POST
def section_recount_start(request, pk):
    _require_section_recount(request)
    try:
        start_section_recount(get_object_or_404(SectionRecount, pk=pk))
    except SectionRecountError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Участок создан и заблокирован для складских операций.")
    return redirect("section_recount_detail", pk=pk)


@login_required
@require_POST
def section_recount_scan(request, pk):
    _require_section_recount(request)
    try:
        doc = get_object_or_404(SectionRecount, pk=pk)
        cell_number = int(request.POST.get("cell_number", "0"))
        if request.POST.get("part_id"):
            record_section_part(
                doc,
                cell_number=cell_number,
                part_id=int(request.POST["part_id"]),
                by=request.user,
            )
        else:
            record_section_scan(
                doc,
                cell_number=cell_number,
                raw_value=request.POST.get("raw_value", ""),
                by=request.user,
            )
    except (SectionRecountError, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Скан добавлен в пересчёт.")
    return redirect("section_recount_detail", pk=pk)


@login_required
@require_POST
def section_recount_complete_cell(request, pk, cell_number):
    _require_section_recount(request)
    doc = get_object_or_404(SectionRecount, pk=pk)
    try:
        complete_section_cell(doc, cell_number=cell_number, by=request.user)
    except SectionRecountError as exc:
        messages.error(request, str(exc))
    else:
        message = (
            "Физический пересчёт ячейки завершён. Теперь проверьте расхождения."
            if doc.is_cell_recount
            else f"C{cell_number:02d} отмечена как пересчитанная."
        )
        messages.success(request, message)
    return redirect("section_recount_detail", pk=pk)


@login_required
@require_POST
def section_recount_set_quantity(request, pk, line_pk):
    _require_section_recount(request)
    try:
        set_section_line_quantity(
            get_object_or_404(SectionRecountLine, pk=line_pk, recount_id=pk),
            request.POST.get("quantity", ""), by=request.user,
        )
    except SectionRecountError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Количество обновлено.")
    return redirect("section_recount_detail", pk=pk)


@login_required
@require_POST
def section_recount_remove_line(request, pk, line_pk):
    _require_section_recount(request)
    try:
        remove_section_line(
            get_object_or_404(SectionRecountLine, pk=line_pk, recount_id=pk),
            by=request.user,
        )
    except SectionRecountError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Строка удалена.")
    return redirect("section_recount_detail", pk=pk)


@login_required
@require_POST
def section_recount_allocate(request, pk, line_pk):
    _require_section_recount(request)
    try:
        source = request.POST.get("allocation_source", "")
        batch_line_id, lot_status = source.split(":", 1)
        allocate_section_line(
            get_object_or_404(SectionRecountLine, pk=line_pk, recount_id=pk),
            batch_line_id=int(batch_line_id),
            quantity=request.POST.get("quantity", ""),
            lot_status=lot_status,
            by=request.user,
        )
    except (SectionRecountError, ValueError) as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Распределение по партии сохранено.")
    return redirect("section_recount_detail", pk=pk)


@login_required
@require_POST
def section_recount_ready(request, pk):
    _require_section_recount(request)
    try:
        mark_section_ready(get_object_or_404(SectionRecount, pk=pk))
    except SectionRecountError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Dry-run готов. Проверьте расхождения перед применением.")
    return redirect("section_recount_detail", pk=pk)


@login_required
@require_POST
def section_recount_apply_view(request, pk):
    _require_section_recount(request)
    doc = get_object_or_404(SectionRecount, pk=pk)
    if request.POST.get("confirm") != "APPLY":
        messages.error(request, "Для применения введите подтверждение APPLY.")
    else:
        try:
            apply_section_recount(doc, by=request.user)
        except SectionRecountError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, f"{doc.operation_label} применён атомарно.")
    return redirect("section_recount_detail", pk=pk)


@login_required
@require_POST
def section_recount_cancel(request, pk):
    _require_section_recount(request)
    try:
        cancel_section_recount(get_object_or_404(SectionRecount, pk=pk))
    except SectionRecountError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Пересчёт отменён, блокировка снята.")
    return redirect("section_recount_detail", pk=pk)
