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
        "+ Новая деталь", "Экспорт бэкапа",
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
    assert "Восстановления/загрузки/импорта в веб-интерфейсе НЕТ" in text
    assert "Не выдумывай функции" in text
    # Карта ролей присутствует.
    for role in ("Администратор", "Руководитель", "Кладовщик", "Продавец", "Наблюдатель"):
        assert role in text
