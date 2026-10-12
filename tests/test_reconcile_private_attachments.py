"""Legacy messenger attachments: public media -> private volume (synthetic files).

Regression: the previous command saved through ``storage.save()``. The storage
reports legacy files as existing, so Django chose an alternative name that no
row referenced; ``--remove-legacy`` then deleted the only copy the row could
open.
"""

import hashlib

import pytest
from django.core.management import CommandError, call_command
from django.test import override_settings
from django.utils import timezone

from apps.customer_requests.models import (
    CustomerRequest,
    MaxMessage,
    TelegramConversation,
    TelegramMessage,
)
from apps.customer_requests.storage import PrivateAttachmentStorage

NAME = "customer_requests/Снимок экрана 2026-09-19 в 12.34.56.png"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-attachment"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    public = tmp_path / "media"
    private = tmp_path / "private_media"
    public.mkdir()
    private.mkdir()
    with override_settings(MEDIA_ROOT=public, PRIVATE_MEDIA_ROOT=private):
        storage = PrivateAttachmentStorage()
    for model in (TelegramMessage, MaxMessage):
        monkeypatch.setattr(model._meta.get_field("attachment"), "storage", storage)
    return {"public": public, "private": private, "storage": storage}


def _telegram_row(name):
    request = CustomerRequest.objects.create(
        customer_name="Synthetic", customer_phone="0", preferred_messenger="telegram",
        privacy_policy_version="v", personal_data_consent_version="v",
        consent_purpose="synthetic", consent_accepted_at=timezone.now(),
        submission_key_hash="b" * 64,
    )
    conversation = TelegramConversation.objects.create(request=request)
    return TelegramMessage.objects.create(
        conversation=conversation, direction=TelegramMessage.Direction.OPERATOR,
        attachment=name,
    )


def _public(roots, name=NAME, data=PNG):
    path = roots["public"] / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _run(*args):
    from io import StringIO

    out = StringIO()
    call_command("reconcile_message_attachments_private", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db
def test_default_is_a_read_only_audit(roots):
    _telegram_row(NAME)
    public = _public(roots)

    output = _run()

    assert "только в публичном media: 1" in output
    assert "Режим проверки" in output
    assert public.read_bytes() == PNG
    assert not (roots["private"] / "customer_requests").exists()
    assert "Снимок" not in output  # customer file names never reach the log


@pytest.mark.django_db
def test_apply_copies_to_the_same_name_and_the_row_reads_the_private_copy(roots):
    row = _telegram_row(NAME)
    public = _public(roots)

    output = _run("--apply")

    private = roots["private"] / NAME
    assert private.read_bytes() == PNG
    assert sorted(p.name for p in private.parent.iterdir()) == [private.name]
    assert public.read_bytes() == PNG  # public copy kept without --remove-legacy
    assert "скопировано: 1" in output
    row.refresh_from_db()
    assert row.attachment.name == NAME  # the database is not changed
    public.write_bytes(b"public copy changed later")
    with roots["storage"].open(row.attachment.name) as handle:
        assert handle.read() == PNG  # resolved from the private volume


@pytest.mark.django_db
def test_remove_legacy_deletes_public_copy_only_after_identical_private_copy(roots):
    row = MaxMessage.objects.create(
        direction=MaxMessage.Direction.OPERATOR, recipient_chat_id=7, attachment=NAME
    )
    public = _public(roots)

    _run("--apply", "--remove-legacy")

    assert not public.exists()
    assert (roots["private"] / NAME).read_bytes() == PNG
    with roots["storage"].open(row.attachment.name) as handle:
        assert handle.read() == PNG


@pytest.mark.django_db
def test_shared_attachment_path_is_removed_only_once(roots):
    _telegram_row(NAME)
    MaxMessage.objects.create(
        direction=MaxMessage.Direction.OPERATOR, recipient_chat_id=7, attachment=NAME
    )
    public = _public(roots)

    output = _run("--apply", "--remove-legacy")

    assert not public.exists()
    assert (roots["private"] / NAME).read_bytes() == PNG
    assert "удалено публичных копий: 1" in output


@pytest.mark.django_db
def test_remove_legacy_without_apply_changes_nothing(roots):
    _telegram_row(NAME)
    public = _public(roots)

    output = _run("--remove-legacy")

    assert public.exists()
    assert "только вместе с --apply" in output
    assert not (roots["private"] / NAME).exists()


@pytest.mark.django_db
def test_conflicting_private_copy_is_never_overwritten_or_used_to_delete(roots):
    _telegram_row(NAME)
    public = _public(roots)
    private = roots["private"] / NAME
    private.parent.mkdir(parents=True)
    private.write_bytes(b"different bytes")

    with pytest.raises(CommandError, match="различаются"):
        _run("--apply", "--remove-legacy")

    assert public.read_bytes() == PNG
    assert private.read_bytes() == b"different bytes"


@pytest.mark.django_db
def test_later_conflict_keeps_every_public_original(roots):
    first_name = "customer_requests/first.png"
    second_name = "customer_requests/second.png"
    _telegram_row(first_name)
    MaxMessage.objects.create(
        direction=MaxMessage.Direction.OPERATOR, recipient_chat_id=7,
        attachment=second_name,
    )
    first_public = _public(roots, first_name, b"first original")
    second_public = _public(roots, second_name, b"second original")
    second_private = roots["private"] / second_name
    second_private.parent.mkdir(parents=True, exist_ok=True)
    second_private.write_bytes(b"conflicting private copy")

    with pytest.raises(CommandError, match="публичные исходники сохранены"):
        _run("--apply", "--remove-legacy")

    assert first_public.read_bytes() == b"first original"
    assert (roots["private"] / first_name).read_bytes() == b"first original"
    assert second_public.read_bytes() == b"second original"
    assert second_private.read_bytes() == b"conflicting private copy"


@pytest.mark.django_db
def test_rerun_is_idempotent_and_reports_earlier_alternative_copies(roots):
    _telegram_row(NAME)
    _public(roots)
    stray = roots["private"] / "customer_requests/Снимок экрана 2026-09-19 в 12_AbC1234.34.56.png"
    stray.parent.mkdir(parents=True)
    stray.write_bytes(PNG)

    first = _run("--apply")
    second = _run("--apply")

    assert "вероятных дублей прошлых запусков: 1" in first
    assert "в обоих местах и совпадают: 1" in second
    assert "скопировано: 0" in second
    assert stray.exists()  # duplicates are reported, never deleted
    files = sorted(p.name for p in stray.parent.iterdir())
    assert len(files) == 2 and not any(name.startswith(".reconcile-") for name in files)


@pytest.mark.django_db
def test_private_only_and_missing_rows_are_counted_and_left_alone(roots):
    _telegram_row("customer_requests/only-private.pdf")
    MaxMessage.objects.create(
        direction=MaxMessage.Direction.OPERATOR, recipient_chat_id=7,
        attachment="customer_requests/gone.pdf",
    )
    private = roots["private"] / "customer_requests/only-private.pdf"
    private.parent.mkdir(parents=True)
    private.write_bytes(b"%PDF-private")

    with pytest.raises(CommandError, match="отсутствующие вложения"):
        _run("--apply", "--remove-legacy")

    assert hashlib.sha256(private.read_bytes()).hexdigest() == hashlib.sha256(
        b"%PDF-private"
    ).hexdigest()


@pytest.mark.django_db
def test_name_escaping_either_root_is_refused(roots):
    MaxMessage.objects.create(
        direction=MaxMessage.Direction.OPERATOR, recipient_chat_id=7,
        attachment="customer_requests/../../escape.png",
    )
    (roots["public"].parent / "escape.png").write_bytes(PNG)

    with pytest.raises(CommandError, match="Небезопасные"):
        _run("--apply", "--remove-legacy")

    assert (roots["public"].parent / "escape.png").exists()
