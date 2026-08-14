"""Экраны импорта каталога. View - оркестратор, вся логика в services.

Доступ по праву на справочник деталей: импорт меняет номенклатуру и исходные
цены, поэтому обычный складской сотрудник сюда не попадает. Исходный прайс это
коммерческий документ, поэтому скачивание закрыто тем же правом.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from .adapters import ADAPTERS, CatalogAdapterError, get_adapter
from .models import CatalogImportBatch
from .services import (
    CatalogImportError,
    apply_batch,
    previous_applied,
    run_check,
    save_upload,
    stored_file_path,
)

PAGE_SIZE = 25

# Что показываем в сводке проверки. Порядок и подписи фиксированы здесь, чтобы
# шаблон не знал о внутренних именах счётчиков импортёра.
SUMMARY_ROWS = (
    ("data_rows", "Строк данных"),
    ("unique_materials", "Уникальных номеров"),
    ("created", "Новых позиций"),
    ("updated", "Изменённых позиций"),
    ("skipped_unchanged", "Без изменений"),
    ("skipped_empty", "Пропущено пустых"),
    ("duplicates", "Дубликатов номера"),
    ("with_wholesale_price", "С оптовой ценой"),
    ("with_retail_price", "С розничной ценой"),
    ("with_replacement", "С заменой номера"),
)


def _require_access(request) -> None:
    if not request.user.can_manage_parts:
        raise PermissionDenied


def _status_rows(summary: dict) -> list[dict]:
    """Статусы поставщика с расшифровкой. Неизвестные показываем как есть."""
    from apps.brp.models import BrpCatalogPart

    counts = (summary or {}).get("status_counts") or {}
    rows = []
    for code, count in sorted(counts.items()):
        hint = BrpCatalogPart.STATUS_LABELS.get(code, "")
        rows.append(
            {
                "code": code or "(пусто)",
                "count": count,
                "hint": hint,
                "known": bool(hint) or not code,
            }
        )
    return rows


@login_required
def import_list(request):
    _require_access(request)
    batches = CatalogImportBatch.objects.select_related("created_by", "applied_by")
    page_obj = Paginator(batches, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "catalog_import/import_list.html",
        {
            "page_obj": page_obj,
            "is_paginated": page_obj.paginator.num_pages > 1,
            "catalogs": [(key, adapter.label) for key, adapter in ADAPTERS.items()],
        },
    )


@login_required
@require_POST
def import_upload(request):
    """Загрузка файла. Сразу после загрузки выполняется ТОЛЬКО проверка."""
    _require_access(request)
    upload = request.FILES.get("workbook")
    catalog = (request.POST.get("catalog") or "").strip()
    if upload is None:
        messages.error(request, "Выберите файл Excel.")
        return redirect("catalog_import_list")
    try:
        batch = save_upload(upload, catalog=catalog, by=request.user)
    except (CatalogImportError, CatalogAdapterError) as exc:
        messages.error(request, str(exc))
        return redirect("catalog_import_list")
    try:
        run_check(batch)
    except CatalogImportError as exc:
        messages.error(request, f"Файл не прошёл проверку: {exc}")
        return redirect("catalog_import_detail", pk=batch.pk)
    messages.success(request, "Файл проверен. Изменения пока не применены.")
    return redirect("catalog_import_detail", pk=batch.pk)


@login_required
def import_detail(request, pk):
    _require_access(request)
    batch = get_object_or_404(
        CatalogImportBatch.objects.select_related("created_by", "applied_by"), pk=pk
    )
    summary = batch.apply_summary if batch.is_applied else batch.summary
    return render(
        request,
        "catalog_import/import_detail.html",
        {
            "batch": batch,
            "summary_rows": [
                (label, (summary or {}).get(key, 0)) for key, label in SUMMARY_ROWS
            ],
            "status_rows": _status_rows(summary),
            "already_applied": previous_applied(batch),
        },
    )


@login_required
@require_POST
def import_recheck(request, pk):
    _require_access(request)
    batch = get_object_or_404(CatalogImportBatch, pk=pk)
    if batch.is_applied:
        messages.error(request, "Партия уже применена, повторная проверка не нужна.")
        return redirect("catalog_import_detail", pk=pk)
    try:
        run_check(batch)
    except CatalogImportError as exc:
        messages.error(request, f"Файл не прошёл проверку: {exc}")
        return redirect("catalog_import_detail", pk=pk)
    messages.success(request, "Файл проверен заново.")
    return redirect("catalog_import_detail", pk=pk)


@login_required
@require_POST
def import_apply(request, pk):
    _require_access(request)
    batch = get_object_or_404(CatalogImportBatch, pk=pk)
    try:
        apply_batch(batch, by=request.user)
    except CatalogImportError as exc:
        messages.error(request, str(exc))
        return redirect("catalog_import_detail", pk=pk)
    messages.success(request, "Импорт применён к справочнику. Складские остатки не изменены.")
    return redirect("catalog_import_detail", pk=pk)


@login_required
def import_inspect(request, pk):
    """Структура книги: листы, заголовки, первые строки. Только чтение."""
    _require_access(request)
    batch = get_object_or_404(CatalogImportBatch, pk=pk)
    adapter = get_adapter(batch.catalog)
    inspect = getattr(adapter, "inspect", None)
    if inspect is None:
        raise Http404("Для этого каталога инспектор недоступен.")
    try:
        structure = inspect(stored_file_path(batch))
    except (CatalogAdapterError, CatalogImportError) as exc:
        messages.error(request, str(exc))
        return redirect("catalog_import_detail", pk=pk)
    return render(
        request,
        "catalog_import/import_inspect.html",
        {"batch": batch, "structure": structure},
    )


@login_required
def import_download(request, pk):
    """Исходный прайс это коммерческий документ: отдаём только по праву."""
    _require_access(request)
    batch = get_object_or_404(CatalogImportBatch, pk=pk)
    try:
        path = stored_file_path(batch)
    except CatalogImportError as exc:
        raise Http404(str(exc)) from exc
    if not path.exists():
        raise Http404("Файл недоступен.")
    response = FileResponse(
        path.open("rb"), as_attachment=True, filename=batch.source_filename
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response
