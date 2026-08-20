"""Когда именно нужна новая копия после назначения станции.

Это главный практический вопрос дня установки, и ответ на него не очевиден:
назначение станции живёт в базе production, но до станции оно доезжает не
через базу, а через манифест резервной копии. Манифест заполняется в момент
создания копии.

Отсюда следует разделение, которое легко перепутать:

* обновление копии до состояния «готово» назначения НЕ требует;
* переход в автономный режим требует, и берёт его из манифеста.

Значит копия, созданная до назначения станции, доведёт её до готовности, но
активироваться по такой копии станция не сможет никогда. После назначения
нужна НОВАЯ подписанная копия.

Тесты закрепляют именно это, чтобы порядок дня установки не поехал молча.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPERATIONS = ROOT / "apps" / "operations"

BACKUP = (OPERATIONS / "backup.py").read_text(encoding="utf-8")
LIFECYCLE = (OPERATIONS / "emergency_lifecycle.py").read_text(encoding="utf-8")
STANDBY = (OPERATIONS / "standby.py").read_text(encoding="utf-8")
AUTHORIZE = (
    OPERATIONS / "management" / "commands" / "authorize_emergency_primary.py"
).read_text(encoding="utf-8")


def test_the_authorization_travels_inside_the_backup_manifest():
    """Не через базу: манифест заполняется на момент создания копии."""
    assert '"authorized_emergency_primary_id"' in BACKUP
    assert '"primary_authorization_epoch": state.primary_authorization_epoch' in BACKUP


def test_activation_requires_the_authorization_from_the_manifest():
    assert 'manifest.get("authorized_emergency_primary_id")' in LIFECYCLE
    assert 'manifest.get("primary_authorization_epoch")' in LIFECYCLE
    assert "Этот компьютер не назначен аварийным основным компьютером." in LIFECYCLE
    assert "Резервная копия создана для другого аварийного компьютера." in LIFECYCLE


def test_activation_rejects_a_backup_made_before_any_authorization():
    """У копии до назначения счётчик равен нулю, и это должно остановить активацию."""
    assert "epoch < 1" in LIFECYCLE
    assert "Авторизация аварийного компьютера отсутствует или устарела." in LIFECYCLE


def test_the_standby_refresh_does_not_require_authorization():
    """Станция должна доходить до готовности и до назначения.

    Иначе день установки упирался бы в лишний круг: назначить, снять копию,
    и только потом впервые проверить, что станция вообще работает.
    """
    assert "authorized_emergency_primary_id" not in STANDBY
    assert "primary_authorization_epoch" not in STANDBY


def test_the_standby_refresh_still_pins_the_release_and_migrations():
    """То, что refresh проверяет вместо назначения."""
    assert "Backup migration state несовместим с local application." in STANDBY
    assert "Backup application commit не совпадает с local application." in STANDBY
    assert 'validate_manifest(fetched, expected_source="production")' in STANDBY


def test_the_authorization_command_bumps_the_epoch():
    """Счётчик отличает свежее назначение от старого."""
    primary = (OPERATIONS / "emergency_primary.py").read_text(encoding="utf-8")
    assert "state.primary_authorization_epoch += 1" in primary
    assert "НАЗНАЧИТЬ-EMERGENCY-PRIMARY" in AUTHORIZE


def test_the_checklist_states_the_ordering():
    """Порядок должен быть написан там, где его читают, а не только в коде."""
    checklist = (
        ROOT / "docs" / "operations" / "emergency-physical-install-checklist.md"
    ).read_text(encoding="utf-8")
    lowered = checklist.lower()
    assert "после назначения" in lowered, "в листе нет порядка назначения и копии"
    assert any(
        phrase in lowered
        for phrase in ("новая подписанная копия", "новую подписанную копию")
    ), "в листе не сказано, что после назначения нужна новая копия"
