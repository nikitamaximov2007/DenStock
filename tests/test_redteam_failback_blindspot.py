"""Red-team: может ли изменение на production остаться невидимым для failback.

Сценарий, который надо исключить: пока склад работает автономно, на production
меняют роль сотрудника; при возврате система говорит «расхождений нет» и
локальная база затирает это изменение.

Раньше здесь был пробел. Отпечаток бизнес-состояния строился по моделям с
`include_auto_created=False`, а защита записи по `include_auto_created=True`.
Из-за этой асимметрии промежуточные таблицы связей «многие ко многим» в
отпечаток не попадали, а в DenisStock одна из них хранит членство пользователя
в группах, то есть фактические РОЛИ. Ложного «безопасно» это не давало, потому
что изменение всё равно двигало счётчик поколений, но эшелонированная защита
держалась на одном признаке вместо двух.

Пробел закрыт: набор моделей отпечатка приведён к тому же, что охраняет защита
записи. Теперь оба признака видят изменение роли независимо. Файл остаётся как
регрессионная защита от возврата асимметрии.
"""
import pytest
from django.contrib.auth.models import Group

from apps.operations.emergency_state import business_state_marker
from apps.operations.models import DeploymentState


def _set_state(write_state):
    state = DeploymentState.get_solo()
    state.write_state = write_state
    state.save(update_fields=["write_state", "updated_at"])


@pytest.fixture
def production_mode(db, settings):
    """Обычный production: защита записи включена, запись разрешена."""
    _set_state(DeploymentState.WriteState.NORMAL)
    settings.DENSTOCK_MODE = "production"
    yield
    settings.DENSTOCK_MODE = "test"


def _generation():
    return DeploymentState.objects.get(pk=DeploymentState.SINGLETON_PK).business_generation


def test_role_change_is_visible_to_data_fingerprint(db, django_user_model):
    """Отпечаток данных обязан видеть смену роли.

    Именно этот случай раньше был слепым пятном: связь пользователь-группа живёт
    в промежуточной таблице, которую отпечаток не обходил.
    """
    from apps.accounts import roles

    user = django_user_model.objects.create_user(username="worker", password="x")
    before = business_state_marker()["business_sha256"]
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    after = business_state_marker()["business_sha256"]
    assert before != after, (
        "Смена роли не изменила отпечаток бизнес-состояния: промежуточная таблица "
        "членства в группах снова выпала из набора моделей"
    )


def test_role_change_is_caught_by_business_generation(production_mode, django_user_model):
    """Компенсирующий контроль: счётчик поколений изменение всё-таки видит.

    Именно он не даёт failback сказать «безопасно» там, где production ушёл
    вперёд.
    """
    from apps.accounts import roles

    user = django_user_model.objects.create_user(username="worker2", password="x")
    before = _generation()
    user.groups.add(Group.objects.get(name=roles.SELLER))
    after = _generation()
    assert after > before, (
        "Изменение роли на production не сдвинуло счётчик поколений: "
        "failback мог бы ошибочно признать состояние неизменившимся"
    )


def test_ordinary_business_change_moves_both_signals(production_mode):
    """Контроль: обычная бизнес-запись двигает и отпечаток, и счётчик."""
    from apps.customers.models import Customer

    sha_before = business_state_marker()["business_sha256"]
    generation_before = _generation()
    Customer.objects.create(name="Иванов", phone="+79121234567")
    assert business_state_marker()["business_sha256"] != sha_before
    assert _generation() > generation_before


def test_permission_change_is_also_caught_by_generation(production_mode, django_user_model):
    from django.contrib.auth.models import Permission

    user = django_user_model.objects.create_user(username="worker3", password="x")
    permission = Permission.objects.first()
    before = _generation()
    user.user_permissions.add(permission)
    assert _generation() > before
