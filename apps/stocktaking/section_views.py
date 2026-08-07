from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .models import SectionRecount, SectionRecountLine
from .section_recount import (
    SectionRecountError,
    _candidate_batch_lines,
    allocate_section_line,
    apply_section_recount,
    build_section_dry_run,
    cancel_section_recount,
    complete_section_cell,
    create_section_recount,
    mark_section_ready,
    record_section_scan,
    remove_section_line,
    set_section_line_quantity,
    start_section_recount,
)


def _require_section_recount(request) -> None:
    if not request.user.can_manage_stocktaking:
        raise PermissionDenied


def _detail_context(doc):
    doc = (
        SectionRecount.objects.select_related("created_by")
        .prefetch_related("cells__location", "lines__cell__location", "lines__part_type")
        .get(pk=doc.pk)
    )
    lines = list(doc.lines.all())
    for line in lines:
        line.batch_candidates = _candidate_batch_lines(line)
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
    if request.method == "POST":
        try:
            doc = create_section_recount(by=request.user)
        except SectionRecountError as exc:
            messages.error(request, str(exc))
        else:
            return redirect("section_recount_detail", pk=doc.pk)
    return render(request, "stocktaking/section_recount_new.html")


@login_required
def section_recount_detail(request, pk):
    _require_section_recount(request)
    doc = get_object_or_404(SectionRecount, pk=pk)
    return render(request, "stocktaking/section_recount_detail.html", _detail_context(doc))


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
        record_section_scan(
            get_object_or_404(SectionRecount, pk=pk),
            cell_number=int(request.POST.get("cell_number", "0")),
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
    try:
        complete_section_cell(
            get_object_or_404(SectionRecount, pk=pk), cell_number=cell_number, by=request.user
        )
    except SectionRecountError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"C{cell_number:02d} отмечена как пересчитанная.")
    return redirect("section_recount_detail", pk=pk)


@login_required
@require_POST
def section_recount_set_quantity(request, pk, line_pk):
    _require_section_recount(request)
    try:
        set_section_line_quantity(
            get_object_or_404(SectionRecountLine, pk=line_pk),
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
        remove_section_line(get_object_or_404(SectionRecountLine, pk=line_pk), by=request.user)
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
        allocate_section_line(
            get_object_or_404(SectionRecountLine, pk=line_pk),
            batch_line_id=int(request.POST.get("batch_line_id", "0")),
            quantity=request.POST.get("quantity", ""),
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
    if request.POST.get("confirm") != "APPLY":
        messages.error(request, "Для применения введите подтверждение APPLY.")
    else:
        try:
            apply_section_recount(get_object_or_404(SectionRecount, pk=pk))
        except SectionRecountError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "Пересчёт участка применён атомарно.")
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
