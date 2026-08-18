"""Adversarial A/B: две синтетические рабочие станции против одного production.

Стенд не подменяет проверяющий код. Ключи настоящие Ed25519, manifest подписан
тем же `sign_manifest`, что и в production, а проверка идёт через настоящий
`validate_manifest(..., expected_source="production")` в режиме emergency-local,
то есть через ту же ветку, что выполняется на складском компьютере.

Машина A и машина B получают РАЗДЕЛЬНЫЕ: корень аварийных данных, защищённый
файл идентичности, настройки и standby. Общий у них только синтетический
production, который держит приватный ключ и назначает Primary.

Проверяется главное свойство: подписанная резервная копия, выданная одной
станции, не должна активировать другую, и ни одна подделка не должна пройти.
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from apps.operations.emergency_environment import (
    EmergencySafetyError,
    configured_workstation_id,
)
from apps.operations.emergency_manifest import SCHEMA_VERSION, validate_manifest
from apps.operations.manifest_signing import (
    ManifestSignatureError,
    canonical_manifest_payload,
    sign_manifest,
    verify_manifest,
)

UUID_A = uuid.UUID("aaaaaaaa-0000-4000-8000-00000000000a")
UUID_B = uuid.UUID("bbbbbbbb-0000-4000-8000-00000000000b")
KEY_ID = "test-production-1"
APP_COMMIT = "a" * 40
MIGRATION_FINGERPRINT = hashlib.sha256(b"[]").hexdigest()
DATABASE_IDENTITY = "52347a14-d939-45e6-a397-06c79ef257f2"


# --- Синтетический production -----------------------------------------------------------


@dataclass
class Authority:
    """Единственный источник доверия: держит приватный ключ и назначает Primary."""

    private: Ed25519PrivateKey
    private_path: Path
    public_path: Path
    key_id: str = KEY_ID
    authorized_primary: uuid.UUID | None = None
    epoch: int = 0

    def authorize(self, workstation_id: uuid.UUID) -> None:
        if self.authorized_primary == workstation_id:
            return
        self.authorized_primary = workstation_id
        self.epoch += 1

    def revoke(self) -> None:
        if self.authorized_primary is None:
            return
        self.authorized_primary = None
        self.epoch += 1


@dataclass
class Machine:
    """Рабочая станция со своей идентичностью и своим корнем данных."""

    workstation_id: uuid.UUID
    root: Path
    identity_path: Path
    role: str = "primary"
    extra: dict = field(default_factory=dict)


@pytest.fixture
def authority(tmp_path, settings) -> Authority:
    private = Ed25519PrivateKey.generate()
    private_path = tmp_path / "production-signing-private.pem"
    public_path = tmp_path / "pinned-production-public.pem"
    private_path.write_bytes(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    settings.DENSTOCK_MANIFEST_SIGNING_KEY_PATH = str(private_path)
    settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH = str(public_path)
    settings.DENSTOCK_MANIFEST_SIGNING_KEY_ID = KEY_ID
    return Authority(private=private, private_path=private_path, public_path=public_path)


def _make_machine(tmp_path, name, workstation_id) -> Machine:
    root = tmp_path / name
    (root / "standbys").mkdir(parents=True, exist_ok=True)
    identity_path = root / "workstation-id.txt"
    identity_path.write_text(str(workstation_id), encoding="utf-8")
    return Machine(workstation_id=workstation_id, root=root, identity_path=identity_path)


@pytest.fixture
def machine_a(tmp_path) -> Machine:
    return _make_machine(tmp_path, "machine-a", UUID_A)


@pytest.fixture
def machine_b(tmp_path) -> Machine:
    return _make_machine(tmp_path, "machine-b", UUID_B)


# --- Настоящие manifest и проверка ------------------------------------------------------


def build_manifest(authority: Authority, **overrides) -> dict:
    """Полный production manifest в том же виде, что создаёт backup_all."""
    now = datetime.now().astimezone()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "backup_run_id": str(uuid.uuid4()),
        "created_at": now.isoformat(timespec="seconds"),
        "source_environment": "production",
        "source_instance_id": "production",
        "authorized_emergency_primary_id": (
            str(authority.authorized_primary) if authority.authorized_primary else None
        ),
        "primary_authorization_epoch": authority.epoch,
        "app_commit": APP_COMMIT,
        "database_name": "denstock",
        "database_identity": DATABASE_IDENTITY,
        "database_dump_filename": "db.dump",
        "database_sha256": hashlib.sha256(b"dump").hexdigest(),
        "media_filename": None,
        "media_sha256": None,
        "media_tree_sha256": "e" * 64,
        "migration_fingerprint": MIGRATION_FINGERPRINT,
        "migration_state": [],
        "data_state": {
            "database_identity": DATABASE_IDENTITY,
            "business_generation": 7,
            "business_sha256": "d" * 64,
            "tables": {},
        },
        "storage_origin": "yandex-object-storage",
        "verification_status": "verified",
        "verified_at": now.isoformat(timespec="seconds"),
        "consistency": "single_writer_locked",
    }
    manifest.update(overrides)
    return manifest


def sign_as_production(manifest: dict, settings) -> dict:
    """Подписать настоящим production-путём: sign_manifest требует production mode."""
    mode = settings.DENSTOCK_MODE
    settings.DENSTOCK_MODE = "production"
    try:
        sign_manifest(manifest)
    finally:
        settings.DENSTOCK_MODE = mode
    return manifest


def publish(machine: Machine, manifest: dict, *, slot="2026-08-18_10-00-00") -> Path:
    """Разложить копию на диск станции ровно так, как это делает standby."""
    run = machine.root / "standbys" / slot
    run.mkdir(parents=True, exist_ok=True)
    (run / "db.dump").write_bytes(b"dump")
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run


def use_machine(settings, machine: Machine) -> None:
    settings.DENSTOCK_EMERGENCY_ROOT = str(machine.root)
    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID = str(machine.workstation_id)
    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID_PATH = str(machine.identity_path)
    settings.DENSTOCK_EMERGENCY_ROLE = machine.role
    for key, value in machine.extra.items():
        setattr(settings, key, value)


@dataclass
class Decision:
    allowed: bool
    reasons: list[str]

    def __bool__(self) -> bool:
        return self.allowed


def activation_decision(settings, machine: Machine, run: Path) -> Decision:
    """Решение об активации через настоящие продуктовые примитивы.

    Порядок и предикаты повторяют `_start_offline_session_database`: сначала
    подлинность копии, затем идентичность станции, затем совпадение назначения и
    ненулевая эпоха. Никакой проверяющий код не подменяется: подпись проверяет
    `validate_manifest` в режиме emergency-local, идентичность возвращает
    `configured_workstation_id`.
    """
    use_machine(settings, machine)
    reasons: list[str] = []

    mode = settings.DENSTOCK_MODE
    settings.DENSTOCK_MODE = "emergency-local"
    try:
        report = validate_manifest(run, expected_source="production")
    finally:
        settings.DENSTOCK_MODE = mode
    if not report.ok:
        return Decision(False, ["manifest: " + "; ".join(report.errors)])
    manifest = report.manifest

    if settings.DENSTOCK_EMERGENCY_ROLE != "primary":
        reasons.append("role: не primary")

    try:
        workstation = configured_workstation_id()
    except EmergencySafetyError as exc:
        return Decision(False, [f"identity: {exc}"])

    try:
        authorized = uuid.UUID(str(manifest.get("authorized_emergency_primary_id")))
    except (TypeError, ValueError):
        return Decision(False, ["authorization: Primary не назначен"])
    if authorized != workstation:
        reasons.append("authorization: копия выдана другой станции")

    epoch = manifest.get("primary_authorization_epoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        reasons.append("authorization: эпоха отсутствует или нулевая")

    return Decision(not reasons, reasons)


# --- 1. Primary не назначен ---------------------------------------------------------------


def test_no_primary_denies_both_machines(settings, authority, machine_a, machine_b):
    """Fail closed: пока Primary не назначен, активироваться не может никто."""
    assert authority.authorized_primary is None and authority.epoch == 0
    manifest = sign_as_production(build_manifest(authority), settings)

    for machine in (machine_a, machine_b):
        run = publish(machine, manifest)
        decision = activation_decision(settings, machine, run)
        assert not decision, f"{machine.workstation_id} активировалась без назначенного Primary"


# --- 2. Назначение A ----------------------------------------------------------------------


@pytest.fixture
def authorized_a(settings, authority, machine_a, machine_b):
    authority.authorize(UUID_A)
    assert authority.epoch >= 1
    manifest = sign_as_production(build_manifest(authority), settings)
    return manifest


def test_authorized_machine_activates(settings, authority, machine_a, authorized_a):
    run = publish(machine_a, authorized_a)
    decision = activation_decision(settings, machine_a, run)
    assert decision, f"назначенная станция не активировалась: {decision.reasons}"


def test_other_machine_is_denied_with_the_same_genuine_manifest(
    settings, authority, machine_b, authorized_a
):
    """Та же подлинная подписанная копия на чужой станции: отказ."""
    run = publish(machine_b, authorized_a)
    decision = activation_decision(settings, machine_b, run)
    assert not decision
    assert any("другой станции" in reason for reason in decision.reasons)


# --- 3. Локальная подмена роли ------------------------------------------------------------


def test_local_role_escalation_does_not_authorize(
    settings, authority, machine_b, authorized_a
):
    """B локально объявляет себя primary: назначение всё равно у A."""
    machine_b.role = "primary"
    run = publish(machine_b, authorized_a)
    assert not activation_decision(settings, machine_b, run)


def test_secondary_role_is_denied_even_when_authorized(
    settings, authority, machine_a, authorized_a
):
    machine_a.role = "secondary"
    run = publish(machine_a, authorized_a)
    decision = activation_decision(settings, machine_a, run)
    assert not decision
    assert any("не primary" in reason for reason in decision.reasons)


# --- 4. Копирование конфигурации и standby ------------------------------------------------


def test_copying_config_without_identity_file_does_not_clone_a_primary(
    settings, authority, machine_a, machine_b, authorized_a
):
    """B получает конфигурацию A, но свой защищённый файл идентичности.

    Именно ради этого случая идентичность вынесена в отдельный файл: настройка
    подделывается, а файл остаётся своим, и расхождение ловится.
    """
    run = publish(machine_b, authorized_a)
    # Скопирована конфигурация A, но защищённый файл идентичности остался своим.
    machine_b.workstation_id = UUID_A

    use_machine(settings, machine_b)
    with pytest.raises(EmergencySafetyError):
        configured_workstation_id()

    decision = activation_decision(settings, machine_b, run)
    assert not decision
    assert any("identity" in reason for reason in decision.reasons)


def test_full_standby_copy_alone_does_not_authorize(
    settings, authority, machine_a, machine_b, authorized_a
):
    """Полная копия standby A на B без клонирования идентичности: отказ."""
    source = publish(machine_a, authorized_a)
    target = machine_b.root / "standbys" / source.name
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        (target / item.name).write_bytes(item.read_bytes())

    assert not activation_decision(settings, machine_b, target)


def test_identity_file_overwrite_is_the_documented_trust_boundary(
    settings, authority, machine_b, authorized_a
):
    """Замена защищённого файла идентичности значением A даёт активацию.

    Это зафиксированная граница доверия, а не дефект: файл лежит вне доступа
    обычного складского пользователя, и его подмена требует прав, при которых
    можно подменить и сам проверяющий код.
    """
    run = publish(machine_b, authorized_a)
    machine_b.identity_path.write_text(str(UUID_A), encoding="utf-8")
    machine_b.workstation_id = UUID_A

    decision = activation_decision(settings, machine_b, run)
    assert decision, (
        "ожидалось, что подмена и конфигурации, и защищённого файла даёт активацию: "
        "это и есть заявленная граница threat model"
    )


def test_missing_identity_file_fails_closed(settings, authority, machine_a, authorized_a):
    run = publish(machine_a, authorized_a)
    machine_a.identity_path.unlink()
    assert not activation_decision(settings, machine_a, run)


def test_corrupt_identity_file_fails_closed(settings, authority, machine_a, authorized_a):
    run = publish(machine_a, authorized_a)
    machine_a.identity_path.write_text("не-uuid", encoding="utf-8")
    assert not activation_decision(settings, machine_a, run)


# --- 5. Подделка полей подписанного manifest ----------------------------------------------


TAMPER_CASES = [
    ("authorized_emergency_primary_id", str(UUID_B)),
    ("primary_authorization_epoch", 99),
    ("app_commit", "b" * 40),
    ("migration_fingerprint", hashlib.sha256(b"other").hexdigest()),
    ("database_sha256", hashlib.sha256(b"other").hexdigest()),
    ("media_tree_sha256", "f" * 64),
    ("database_identity", "11111111-1111-4111-8111-111111111111"),
    ("source_instance_id", "attacker"),
    ("consistency", "database_snapshot"),
    ("verification_status", "failed"),
]


@pytest.mark.parametrize(("field_name", "value"), TAMPER_CASES)
def test_field_tampering_after_signing_fails_verification(
    settings, authority, machine_a, field_name, value
):
    authority.authorize(UUID_A)
    manifest = sign_as_production(build_manifest(authority), settings)
    manifest[field_name] = value
    run = publish(machine_a, manifest)

    decision = activation_decision(settings, machine_a, run)
    assert not decision, f"подделка поля {field_name} прошла проверку"
    assert any("подпись" in r or "signature" in r.lower() for r in decision.reasons), (
        f"подделка {field_name} отклонена не подписью: {decision.reasons}"
    )


def test_business_state_tampering_fails_verification(settings, authority, machine_a):
    authority.authorize(UUID_A)
    manifest = sign_as_production(build_manifest(authority), settings)
    manifest["data_state"]["business_generation"] = 999
    manifest["data_state"]["business_sha256"] = "0" * 64
    run = publish(machine_a, manifest)
    assert not activation_decision(settings, machine_a, run)


# --- 6. Атаки на подпись ------------------------------------------------------------------


def _signed(settings, authority):
    authority.authorize(UUID_A)
    return sign_as_production(build_manifest(authority), settings)


def test_missing_signature_is_rejected(settings, authority, machine_a):
    manifest = _signed(settings, authority)
    manifest.pop("signature")
    assert not activation_decision(settings, machine_a, publish(machine_a, manifest))


def test_unsigned_legacy_manifest_never_becomes_trusted(settings, authority, machine_a):
    """Downgrade: подписанную копию превращают в «старую» без подписи."""
    authority.authorize(UUID_A)
    manifest = build_manifest(authority)  # намеренно НЕ подписываем
    assert "signature" not in manifest
    assert not activation_decision(settings, machine_a, publish(machine_a, manifest))


@pytest.mark.parametrize(
    "signature",
    [
        None,
        "",
        [],
        {"algorithm": "ed25519"},
        {"algorithm": "rsa", "key_id": KEY_ID, "value": "AAAA"},
        {"algorithm": "ed25519", "key_id": KEY_ID, "value": "не-base64!!"},
        {"algorithm": "ed25519", "key_id": KEY_ID, "value": base64.b64encode(b"x" * 64).decode()},
        {
            "algorithm": "ed25519",
            "key_id": "unknown-key",
            "value": base64.b64encode(b"x" * 64).decode(),
        },
    ],
)
def test_malformed_or_forged_signatures_are_rejected(settings, authority, machine_a, signature):
    manifest = _signed(settings, authority)
    manifest["signature"] = signature
    assert not activation_decision(settings, machine_a, publish(machine_a, manifest))


def test_signature_from_a_different_private_key_is_rejected(settings, authority, machine_a):
    """Злоумышленник подписывает своим ключом, key_id оставляет доверенный."""
    authority.authorize(UUID_A)
    manifest = build_manifest(authority)
    rogue = Ed25519PrivateKey.generate()
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": KEY_ID,
        "value": base64.b64encode(
            rogue.sign(canonical_manifest_payload(manifest))
        ).decode("ascii"),
    }
    assert not activation_decision(settings, machine_a, publish(machine_a, manifest))


def test_public_key_supplied_beside_the_manifest_is_ignored(
    settings, authority, machine_a, tmp_path
):
    """Ключ проверки берётся только из закреплённого файла, не из копии."""
    authority.authorize(UUID_A)
    manifest = build_manifest(authority)
    rogue = Ed25519PrivateKey.generate()
    manifest["public_key"] = rogue.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode("ascii")
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": KEY_ID,
        "value": base64.b64encode(
            rogue.sign(canonical_manifest_payload(manifest))
        ).decode("ascii"),
    }
    run = publish(machine_a, manifest)
    rogue_pem = run / "public_key.pem"
    rogue_pem.write_bytes(
        rogue.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    assert not activation_decision(settings, machine_a, run)


def test_missing_pinned_public_key_fails_closed(settings, authority, machine_a):
    manifest = _signed(settings, authority)
    run = publish(machine_a, manifest)
    Path(settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH).unlink()
    assert not activation_decision(settings, machine_a, run)


def test_wrong_pinned_public_key_fails_closed(settings, authority, machine_a):
    manifest = _signed(settings, authority)
    run = publish(machine_a, manifest)
    other = Ed25519PrivateKey.generate()
    Path(settings.DENSTOCK_MANIFEST_PUBLIC_KEY_PATH).write_bytes(
        other.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    assert not activation_decision(settings, machine_a, run)


def test_production_signing_requires_production_mode(settings, authority):
    authority.authorize(UUID_A)
    manifest = build_manifest(authority)
    settings.DENSTOCK_MODE = "emergency-local"
    with pytest.raises(ManifestSignatureError):
        sign_manifest(manifest)


# --- 7. Канонизация ------------------------------------------------------------------------


def test_key_order_and_whitespace_do_not_affect_verification(settings, authority):
    manifest = _signed(settings, authority)
    reordered = json.loads(json.dumps(manifest, sort_keys=False, indent=4))
    shuffled = dict(reversed(list(reordered.items())))
    verify_manifest(shuffled)  # не должно бросить


def test_signature_envelope_is_excluded_from_the_signed_payload(settings, authority):
    manifest = _signed(settings, authority)
    without = dict(manifest)
    without.pop("signature")
    assert canonical_manifest_payload(manifest) == canonical_manifest_payload(without)


def test_every_security_field_is_inside_the_signed_payload(settings, authority):
    manifest = _signed(settings, authority)
    payload = canonical_manifest_payload(manifest).decode("ascii")
    for name in (
        "authorized_emergency_primary_id",
        "primary_authorization_epoch",
        "app_commit",
        "database_sha256",
        "media_tree_sha256",
        "migration_fingerprint",
        "database_identity",
        "data_state",
        "consistency",
        "verification_status",
    ):
        assert f'"{name}"' in payload, f"поле {name} не входит в подписываемый payload"


# --- 8. Отзыв и передача Primary ------------------------------------------------------------


def test_revocation_denies_the_former_primary(settings, authority, machine_a):
    authority.authorize(UUID_A)
    first = sign_as_production(build_manifest(authority), settings)
    assert activation_decision(settings, machine_a, publish(machine_a, first, slot="run-1"))

    authority.revoke()
    after = sign_as_production(build_manifest(authority), settings)
    assert authority.authorized_primary is None
    decision = activation_decision(settings, machine_a, publish(machine_a, after, slot="run-2"))
    assert not decision, "после отзыва станция всё ещё активируется по новой копии"


def test_promotion_moves_authorization_from_a_to_b(
    settings, authority, machine_a, machine_b
):
    authority.authorize(UUID_A)
    epoch_a = authority.epoch
    manifest_a = sign_as_production(build_manifest(authority), settings)

    authority.authorize(UUID_B)
    assert authority.epoch == epoch_a + 1
    manifest_b = sign_as_production(build_manifest(authority), settings)

    assert activation_decision(settings, machine_b, publish(machine_b, manifest_b, slot="run-b"))
    assert not activation_decision(
        settings, machine_a, publish(machine_a, manifest_b, slot="run-new")
    ), "прежний Primary активировался по новой копии"
    # Контроль: по своей старой копии A всё ещё активируется, см. отдельный тест
    # про устаревшего прежнего Primary.
    assert activation_decision(settings, machine_a, publish(machine_a, manifest_a, slot="run-a"))


def test_repeated_authorization_of_the_same_primary_keeps_the_epoch(settings, authority):
    authority.authorize(UUID_A)
    epoch = authority.epoch
    authority.authorize(UUID_A)
    assert authority.epoch == epoch, "повторное назначение того же Primary сдвинуло эпоху"


# --- 9. Устаревший прежний Primary ----------------------------------------------------------


def test_stale_former_primary_can_still_activate_offline(
    settings, authority, machine_a, machine_b
):
    """Ключевой сценарий: A отрезан от production, назначение ушло к B.

    A хранит подлинную подписанную копию с эпохой N. Production позже назначает B
    и увеличивает эпоху до N+1, но A этого никогда не видит. Затем production
    недоступен, и A запускается со своей старой копии.

    Активация ПРОХОДИТ. Криптографически копия подлинна, а сравнить эпоху не с чем:
    единственный источник актуального назначения это сам production. Это физика
    автономной работы, а не дефект проверки: закрыть её можно только организационно.
    """
    authority.authorize(UUID_A)
    stale_epoch = authority.epoch
    stale_manifest = sign_as_production(build_manifest(authority), settings)
    stale_run = publish(machine_a, stale_manifest, slot="epoch-old")

    authority.authorize(UUID_B)
    fresh_manifest = sign_as_production(build_manifest(authority), settings)
    assert authority.epoch == stale_epoch + 1
    assert activation_decision(
        settings, machine_b, publish(machine_b, fresh_manifest, slot="epoch-new")
    )

    decision = activation_decision(settings, machine_a, stale_run)
    assert decision, (
        "ожидалось, что устаревший прежний Primary активируется автономно: "
        "это зафиксированное ограничение, а не проверка"
    )


def test_stale_standby_age_is_visible_for_an_operational_guard(settings, authority, machine_a):
    """Свежесть отделена от подлинности: возраст копии доступен для порога."""
    authority.authorize(UUID_A)
    old = datetime.now().astimezone() - timedelta(days=30)
    manifest = sign_as_production(
        build_manifest(
            authority,
            created_at=old.isoformat(timespec="seconds"),
            verified_at=old.isoformat(timespec="seconds"),
        ),
        settings,
    )
    run = publish(machine_a, manifest)

    decision = activation_decision(settings, machine_a, run)
    assert decision, "подлинность не должна зависеть от возраста"

    created = datetime.fromisoformat(manifest["created_at"])
    age_hours = (datetime.now().astimezone() - created).total_seconds() / 3600
    assert age_hours > settings.DENSTOCK_EMERGENCY_STALE_WARNING_HOURS, (
        "порог предупреждения о несвежести не срабатывает на копии месячной давности"
    )
