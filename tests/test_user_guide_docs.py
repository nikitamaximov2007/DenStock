"""Layer 29 — пользовательская документация (docs/user-guide/).

Лёгкие тесты: файлы существуют, без длинных тире, ключевые факты не разошлись
с системой (названия разделов/кнопок), ссылки README резолвятся (это проверяет
существующий test_readme_doc_links_resolve в test_ops_docs.py).
"""
from pathlib import Path

from django.conf import settings

GUIDE_DIR = Path(settings.BASE_DIR) / "docs" / "user-guide"
MANUAL = GUIDE_DIR / "denstock-user-manual.md"
CHATGPT = GUIDE_DIR / "denstock-chatgpt-context.md"
QUICK = GUIDE_DIR / "quick-start.md"
CHECKLIST = GUIDE_DIR / "launch-checklist.md"

ALL_DOCS = (MANUAL, CHATGPT, QUICK, CHECKLIST)


def test_user_guide_docs_exist():
    for path in ALL_DOCS:
        assert path.exists(), path


def test_no_em_dash_in_user_guide():
    for path in ALL_DOCS:
        assert "—" not in path.read_text(encoding="utf-8"), path


def test_manual_covers_all_sections():
    text = MANUAL.read_text(encoding="utf-8")
    for section in (
        "Поиск детали", "Сканер", "Детали", "Партии", "Поступление",
        "Приёмка сканером", "Перемещение", "Остатки", "Движения",
        "Экземпляры", "Лоты", "Резервы", "Продажи", "Ремонтные заказы",
        "Возвраты", "Списания", "Инвентаризация", "Отчёты", "Статистика",
        "Пользователи", "Бэкапы", "Нераспознанные",
    ):
        assert section in text, f"в инструкции нет раздела: {section}"


def test_manual_uses_real_button_labels():
    text = MANUAL.read_text(encoding="utf-8")
    for label in (
        "Провести поступление", "Провести продажу", "Провести (выдать в ремонт)",
        "Провести возврат", "Провести (списать)", "Провести инвентаризацию",
        "Активировать", "Продать из резерва", "Оформить возврат",
        "Завести деталь", "Экспорт бэкапа",
    ):
        assert label in text, f"в инструкции нет кнопки: {label}"


def test_manual_states_core_rules():
    text = MANUAL.read_text(encoding="utf-8")
    assert "Карточка детали не равна остатку" in text
    assert "Черновик" in text and "read-only" in text


def test_chatgpt_context_is_honest():
    text = CHATGPT.read_text(encoding="utf-8")
    # Ключевые ограничения, которые ассистент не должен «изобретать».
    assert "Отмены проведённой продажи нет" in text
    # Layer 30: web-restore есть ТОЛЬКО у allowlist-владельца; upload нет ни у кого.
    assert "Аварийное восстановление" in text
    assert "ПОДТВЕРЖДАЮ" in text
    assert "pre-restore" in text
    assert "Не выдумывай функции" in text
    # Карта ролей присутствует.
    for role in ("Администратор", "Руководитель", "Кладовщик", "Продавец", "Наблюдатель"):
        assert role in text


def test_manual_describes_the_short_part_form():
    """Инструкция вела по прежней форме создания детали.

    Она называла категорию, режим учёта и минимальный остаток. Этих полей при
    создании больше нет, зато появился артикул, и оператор по такому описанию
    искал бы на экране то, чего там нет.
    """
    text = MANUAL.read_text(encoding="utf-8")
    scenario = text.split("### Сценарий 1.")[1].split("### Сценарий 2.")[0]
    # Проверяется первый абзац: это сам путь по экранам. Ниже речь идёт о том,
    # что дозаполняется потом, и упоминать те же поля там уместно.
    path = scenario.split("\n\n")[0].lower()
    assert "артикул" in path, "в пути нет артикула, а форма его спрашивает"
    # Название поля берётся из самой формы: раньше здесь стояла строка «цена
    # продажи», поле переименовали, и инструкция разошлась с экраном молча.
    from apps.catalog.forms import ManualPartForm

    price_label = ManualPartForm().fields["price"].label.split(",")[0].strip().lower()
    assert price_label in path, f"в пути нет поля «{price_label}»"
    assert "режим учёта" not in path, "описана прежняя форма создания"
    assert "минимальный остаток" not in path, "описана прежняя форма создания"
