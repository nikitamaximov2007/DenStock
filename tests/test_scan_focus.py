"""Регрессии фокуса поля сканера (feature/counting-scanner-focus).

JS-раннера в проекте нет, поэтому логику фокуса покрываем:
- render/response-тестами Django (селектор, autofocus, подключение модуля,
  сохранённая серверная валидация «Пустой скан»);
- статическим анализом самого модуля scan_focus.js (preventScroll, единая
  функция, защита от пустого submit, отсутствие агрессивного setInterval).
Полный сценарий 20-30 сканов проверяется отдельным browser smoke.
"""
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.counting.models import InventoryCountingSession
from apps.warehouse.addresses import get_or_create_location

PASSWORD = "parol-12345"
JS = (Path(settings.BASE_DIR) / "static" / "js" / "scan_focus.js").read_text(encoding="utf-8")


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, is_superuser=False):
        if is_superuser:
            return django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        return django_user_model.objects.create_user(username=username, password=PASSWORD)

    return _make


@pytest.fixture
def boss(make_user, client):
    make_user("boss", is_superuser=True)
    client.login(username="boss", password=PASSWORD)
    return "boss"


@pytest.fixture
def part(db):
    cat = Category.objects.create(name="Крепёж")
    wh = PartType.objects.create(
        name="Болт складской", category=cat,
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(part=wh, value="700700", kind=PartNumber.Kind.OEM)
    return wh


@pytest.fixture
def session(db):
    loc = get_or_create_location("B-S01-L02-D03-C08", name="Ящик")
    return InventoryCountingSession.objects.create(
        storage_location=loc, full_address=loc.code, title="t",
    )


def _detail(client, session):
    return client.get(reverse("counting_detail", args=[session.pk])).content.decode()


# --- Разметка страницы инвентаризации ------------------------------------------------


def test_scan_input_has_stable_selector(client, boss, session):
    html = _detail(client, session)
    assert "data-scan-input" in html


def test_scan_input_has_autofocus(client, boss, session):
    html = _detail(client, session)
    # autofocus и data-scan-input на одном и том же поле #scan
    field = html[html.index('id="scan"'): html.index('id="scan"') + 400]
    assert "autofocus" in field
    assert "data-scan-input" in field


def test_scan_focus_module_included(client, boss, session):
    html = _detail(client, session)
    assert "js/scan_focus.js" in html


def test_readiness_indicator_present_without_emoji(client, boss, session):
    html = _detail(client, session)
    assert "data-scan-indicator" in html
    assert "Сканер готов" in html
    # индикатор — не замена автофокусу и без эмодзи/разработческих формулировок
    indicator = html[html.index("data-scan-indicator"): html.index("data-scan-indicator") + 300]
    assert not any(ord(ch) > 0x2600 for ch in indicator)  # нет эмодзи-символов


def test_scan_input_value_not_run_through_formatters(client, boss, session):
    """Значение поля скана — сырой ввод, без money/quantity форматтеров."""
    field = _detail(client, session)
    seg = field[field.index('id="scan"'): field.index('id="scan"') + 400]
    assert "money_int" not in seg and "quantity_int" not in seg and "floatformat" not in seg


# --- Серверная валидация сохранена ----------------------------------------------------


def test_empty_scan_still_rejected_by_server(client, boss, session, part):
    """Клиент блокирует пустой submit, но серверная защита остаётся."""
    resp = client.post(reverse("counting_scan", args=[session.pk]), {"code": ""}, follow=True)
    assert resp.status_code == 200
    assert "Пустой скан" in resp.content.decode()
    assert session.lines.count() == 0  # пустой скан не создаёт строку


def test_valid_scan_still_works(client, boss, session, part):
    resp = client.post(reverse("counting_scan", args=[session.pk]), {"code": "700700"})
    assert resp.status_code == 302
    assert session.lines.count() == 1


def test_whitespace_only_scan_rejected(client, boss, session, part):
    resp = client.post(reverse("counting_scan", args=[session.pk]), {"code": "   "}, follow=True)
    assert "Пустой скан" in resp.content.decode()
    assert session.lines.count() == 0


# --- Родственные экраны непрерывного скана -------------------------------------------


def test_sibling_scanner_screens_use_selector(client, boss):
    for url in ("scanner_receiving", "scanner_move", "actions_scan"):
        html = client.get(reverse(url)).content.decode()
        assert "data-scan-input" in html, url


# --- Статический анализ модуля scan_focus.js -----------------------------------------


def test_module_defines_single_focus_function():
    assert "function focusScanInput" in JS
    assert "querySelector(\"[data-scan-input]\")" in JS


def test_module_uses_prevent_scroll():
    assert "preventScroll: true" in JS


def test_module_blocks_empty_submit():
    assert "preventDefault()" in JS
    assert ".trim() === \"\"" in JS or 'trim() === ""' in JS


def test_module_guards_double_submit():
    assert "submitting" in JS


def test_module_does_not_use_aggressive_interval():
    # Постоянный setInterval, отбирающий фокус, запрещён.
    assert "setInterval" not in JS


def test_module_guards_against_duplicate_listeners():
    assert "scanFocusBound" in JS


def test_module_respects_manual_editing():
    assert "userIsEditingElsewhere" in JS


def test_module_rebinds_focus_events():
    for event in ("DOMContentLoaded", "pageshow", "visibilitychange", "hashchange"):
        assert event in JS


def test_scanner_js_no_longer_double_focuses_reload_inputs():
    scanner_js = (Path(settings.BASE_DIR) / "static" / "js" / "scanner.js").read_text(
        encoding="utf-8"
    )
    # Приёмку теперь держит scan_focus.js — старого дублирующего фокуса нет.
    assert "receiving-scan-input" not in scanner_js
