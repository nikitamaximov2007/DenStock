"""v1.2.1 — русский формат дат/времени в UI (фильтры ru_date/ru_dt/ru_dts).

Гарантируем, что технический ISO-вид (2026-07-03T05:21:19) и run_id
(2026-07-03_05-21-19) не просачиваются в UI.
"""
from datetime import date, datetime

from django.template import Context, Template

from apps.core.templatetags.ru_dates import ru_date, ru_dt, ru_dts

DT = datetime(2026, 7, 3, 5, 21, 19)


def test_datetime_object_formats():
    assert ru_date(DT) == "03.07.2026"
    assert ru_dt(DT) == "03.07.2026 05:21"
    assert ru_dts(DT) == "03.07.2026 05:21:19"


def test_date_object():
    assert ru_date(date(2026, 7, 3)) == "03.07.2026"


def test_iso_string_is_parsed():
    assert ru_dt("2026-07-03T05:21:19") == "03.07.2026 05:21"
    assert ru_dts("2026-07-03T05:21:19") == "03.07.2026 05:21:19"


def test_run_id_string_is_parsed():
    assert ru_dts("2026-07-03_05-21-19") == "03.07.2026 05:21:19"
    assert ru_date("2026-07-03_05-21-19") == "03.07.2026"


def test_empty_returns_empty():
    assert ru_date(None) == ""
    assert ru_dt("") == ""


def test_unparseable_passes_through():
    assert ru_dts("legacy") == "legacy"


def test_filters_available_as_builtins_in_templates():
    # Зарегистрированы как builtins -> работают без {% load %}.
    out = Template("{{ v|ru_dt }}").render(Context({"v": "2026-07-03T05:21:19"}))
    assert out == "03.07.2026 05:21"
    assert "2026-07-03T05:21:19" not in out
