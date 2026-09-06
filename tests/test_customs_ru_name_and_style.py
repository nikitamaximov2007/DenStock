import openpyxl
import pytest

from apps.actions.services import auto_customs_name_ru, export_customs_xlsx


@pytest.mark.parametrize(
    "source,expected",
    [
        ("OIL HOSE 1500MM LONG", "МАСЛЯНЫЙ ШЛАНГ ДЛИНОЙ 1500 ММ"),
        ("PIN_SPRING 2.6MM X 20.5MM", "ПРУЖИННЫЙ ШТИФТ 2,6 × 20,5 ММ"),
        ("O-RING", "УПЛОТНИТЕЛЬНОЕ КОЛЬЦО"),
        ("BALL BEARING", "ШАРИКОВЫЙ ПОДШИПНИК"),
        ("OETIKER CLAMP", "ХОМУТ OETIKER"),
        ("MAINTENANCE CLUTCH KIT EDRIVE II", "КОМПЛЕКТ ОБСЛУЖИВАНИЯ СЦЕПЛЕНИЯ EDRIVE II"),
        ("SPARK PLUG NGK PZFR6F", "СВЕЧА ЗАЖИГАНИЯ NGK PZFR6F"),
        ("ROLLER_PULLER", "СЪЁМНИК РОЛИКА"),
        ("PIN_ROLLER", "ОСЬ РОЛИКА"),
        ("SLIDER_SHOE", "БАШМАК СКОЛЬЖЕНИЯ"),
        ("BELT_DRIVE", "ПРИВОДНОЙ РЕМЕНЬ"),
        ("OIL PUMP COVER", "КРЫШКА МАСЛЯНОГО НАСОСА"),
        ("VALVE STEM SEAL", "МАСЛОСЪЁМНЫЙ КОЛПАЧОК КЛАПАНА"),
        ("BUSHING", "ВТУЛКА"),
        ("BUSHING HALF", "ПОЛОВИНА ВТУЛКИ"),
    ],
)
def test_phrase_first_ru_translation(source, expected):
    assert auto_customs_name_ru(source) == expected
    assert "_" not in auto_customs_name_ru(source)


def test_customs_data_cells_use_one_font_and_nonshrinking_alignment():
    rows = [{
        "number": "A", "name_ru": auto_customs_name_ru("OIL HOSE 1500MM LONG"),
        "name_en": "OIL HOSE 1500MM LONG", "manufacturer": "BRP", "country": "КАНАДА",
        "gross_weight_kg": None, "net_weight_kg": None, "quantity": 1,
        "usd_price": 10, "application_area": "",
    }]
    sheet = openpyxl.load_workbook(export_customs_xlsx(rows=rows)).active
    assert (sheet["C10"].font.name, sheet["C10"].font.sz, sheet["C10"].font.bold,
            sheet["C10"].font.italic) == (sheet["D10"].font.name, sheet["D10"].font.sz,
            sheet["D10"].font.bold, sheet["D10"].font.italic)
    assert sheet["C10"].alignment.wrap_text is True
    assert sheet["C10"].alignment.shrink_to_fit in (None, False)
    assert sheet["C10"].alignment.horizontal == "center"
    assert sheet["C10"].alignment.vertical == "center"
