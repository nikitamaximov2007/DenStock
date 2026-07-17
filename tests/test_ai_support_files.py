import io
import uuid

import pytest
from django.contrib.auth.models import Group
from django.core.checks import Tags, run_checks
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from PIL import Image, ImageDraw

from apps.accounts import roles
from apps.ai_support.files import normalize_image, private_path, save_normalized_image
from apps.ai_support.models import (
    DeveloperTicket,
    SupportAttachment,
    SupportConversation,
    SupportMessage,
)
from apps.ai_support.services import send_message

PASSWORD = "parol-12345"


def image_upload(
    *, fmt="PNG", name="screen.png", content_type="image/png", size=(20, 20), exif=False
):
    output = io.BytesIO()
    image = Image.new("RGB", size, "white")
    ImageDraw.Draw(image).text((1, 1), "https://185.250.44.206/", fill="black")
    kwargs = {}
    if exif:
        metadata = Image.Exif()
        metadata[0x010E] = "private metadata"
        kwargs["exif"] = metadata
    image.save(output, fmt, **kwargs)
    return SimpleUploadedFile(name, output.getvalue(), content_type=content_type)


@pytest.fixture
def file_settings(settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "private"
    settings.MEDIA_ROOT = tmp_path / "public"
    settings.AI_SUPPORT_MAX_IMAGE_BYTES = 5 * 1024 * 1024
    settings.AI_SUPPORT_ENABLED = True
    settings.AI_SUPPORT_PROVIDER = "fake"
    settings.AI_SUPPORT_ALLOW_FAKE_PROVIDER = True
    settings.DENSTOCK_PUBLIC_BASE_URL = "https://185-250-44-206.sslip.io/"
    return settings


@pytest.fixture
def users(db, django_user_model):
    owner = django_user_model.objects.create_user(username="image-owner", password=PASSWORD)
    owner.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    other = django_user_model.objects.create_user(username="image-other", password=PASSWORD)
    other.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    manager = django_user_model.objects.create_user(username="image-manager", password=PASSWORD)
    manager.groups.add(Group.objects.get(name=roles.MANAGER))
    return owner, other, manager


@pytest.mark.parametrize(
    ("fmt", "name", "mime"),
    [
        ("JPEG", "a.jpg", "image/jpeg"),
        ("PNG", "a.png", "image/png"),
        ("WEBP", "a.webp", "image/webp"),
    ],
)
def test_allowed_images_are_fully_normalized(file_settings, fmt, name, mime):
    normalized = normalize_image(image_upload(fmt=fmt, name=name, content_type=mime))
    assert normalized.mime_type == mime
    assert len(normalized.sha256) == 64
    assert normalized.width == 20 and normalized.height == 20


@pytest.mark.parametrize(
    "upload",
    [
        SimpleUploadedFile("evil.svg", b"<svg/>", content_type="image/svg+xml"),
        SimpleUploadedFile("broken.png", b"not png", content_type="image/png"),
        image_upload(name="wrong.jpg", content_type="image/jpeg"),
        image_upload(name="ok.png", content_type="image/jpeg"),
    ],
)
def test_extension_mime_magic_and_corrupt_files_are_rejected(file_settings, upload):
    with pytest.raises(ValidationError):
        normalize_image(upload)


def test_oversized_image_is_rejected(file_settings):
    file_settings.AI_SUPPORT_MAX_IMAGE_BYTES = 10
    with pytest.raises(ValidationError):
        normalize_image(image_upload())


def test_pixel_limit_and_decompression_guard(file_settings, monkeypatch):
    monkeypatch.setattr("apps.ai_support.files.MAX_PIXELS", 4)
    with pytest.raises(ValidationError):
        normalize_image(image_upload(size=(3, 3)))


def test_animated_webp_is_rejected(file_settings):
    output = io.BytesIO()
    frames = [Image.new("RGB", (10, 10), color) for color in ("red", "blue")]
    frames[0].save(output, "WEBP", save_all=True, append_images=frames[1:], duration=100, loop=0)
    upload = SimpleUploadedFile("animated.webp", output.getvalue(), content_type="image/webp")
    with pytest.raises(ValidationError):
        normalize_image(upload)


def test_image_limit_cannot_be_configured_above_hard_cap(file_settings):
    file_settings.AI_SUPPORT_MAX_IMAGE_BYTES = 10 * 1024 * 1024
    upload = SimpleUploadedFile(
        "large.png", b"x" * (5 * 1024 * 1024 + 1), content_type="image/png"
    )
    with pytest.raises(ValidationError, match="5 МБ"):
        normalize_image(upload)


def test_decompression_bomb_warning_is_rejected(file_settings, monkeypatch):
    upload = SimpleUploadedFile("large.png", b"png", content_type="image/png")

    def raise_warning(_upload):
        raise Image.DecompressionBombWarning("unsafe dimensions")

    monkeypatch.setattr(Image, "open", raise_warning)
    with pytest.raises(ValidationError, match="повреждён"):
        normalize_image(upload)


def test_exif_is_removed_by_reencoding(file_settings):
    normalized = normalize_image(
        image_upload(fmt="JPEG", name="photo.jpg", content_type="image/jpeg", exif=True)
    )
    clean = Image.open(io.BytesIO(normalized.content))
    assert not clean.getexif()


def test_private_storage_is_outside_public_media(file_settings):
    normalized = normalize_image(image_upload())
    relative = save_normalized_image(normalized)
    path = private_path(relative)
    assert path.is_file()
    assert file_settings.PRIVATE_MEDIA_ROOT.resolve() in path.parents
    assert file_settings.MEDIA_ROOT.resolve() not in path.parents


def test_django_check_rejects_private_storage_inside_public_media(file_settings):
    file_settings.PRIVATE_MEDIA_ROOT = file_settings.MEDIA_ROOT / "private"
    errors = run_checks(tags=[Tags.security])
    assert any(error.id == "ai_support.E001" for error in errors)


def _create_attachment(owner, file_settings):
    conversation = SupportConversation.objects.create(owner=owner)
    result = send_message(
        conversation=conversation,
        user=owner,
        text="После проведения продажи появилось ERR_SSL_PROTOCOL_ERROR",
        token=uuid.uuid4(),
        upload=image_upload(),
        image_consent=True,
        route_path=reverse("sale_list"),
    )
    return result.user_message.attachment


def test_private_attachment_ownership_headers_and_missing_file(
    client, users, file_settings
):
    owner, other, _ = users
    attachment = _create_attachment(owner, file_settings)
    client.force_login(other)
    assert client.get(reverse("ai_support:attachment", args=[attachment.id])).status_code == 404
    client.force_login(owner)
    response = client.get(reverse("ai_support:attachment", args=[attachment.id]))
    assert response.status_code == 200
    assert response["Cache-Control"] == "private, no-store"
    assert response["X-Content-Type-Options"] == "nosniff"
    response.close()
    private_path(attachment.relative_path).unlink()
    assert client.get(reverse("ai_support:attachment", args=[attachment.id])).status_code == 404


def test_manager_reads_only_attachment_explicitly_shared_with_ticket(
    client, users, file_settings
):
    owner, _, manager = users
    shared = _create_attachment(owner, file_settings)
    unshared_message = SupportMessage.objects.create(
        conversation=shared.message.conversation,
        role="user",
        text="Другой",
        sequence=3,
    )
    normalized = normalize_image(image_upload())
    relative = save_normalized_image(normalized)
    unshared = SupportAttachment.objects.create(
        message=unshared_message,
        relative_path=relative,
        sha256=normalized.sha256,
        size=len(normalized.content),
        mime_type=normalized.mime_type,
        width=normalized.width,
        height=normalized.height,
    )
    DeveloperTicket.objects.create(
        conversation=shared.message.conversation,
        author=owner,
        attachment=shared,
        description="Передано явно",
    )
    client.force_login(manager)
    assert client.get(reverse("ai_support:attachment", args=[shared.id])).status_code == 200
    assert client.get(reverse("ai_support:attachment", args=[unshared.id])).status_code == 404


def test_image_requires_explicit_consent(users, file_settings):
    owner, _, _ = users
    conversation = SupportConversation.objects.create(owner=owner)
    from apps.ai_support.services import ImageRejected

    with pytest.raises(ImageRejected):
        send_message(
            conversation=conversation,
            user=owner,
            text="Ошибка",
            token=uuid.uuid4(),
            upload=image_upload(),
            image_consent=False,
        )
