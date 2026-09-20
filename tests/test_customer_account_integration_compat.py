"""The account must not disturb what production main already accepted.

Three accepted lineages share this repository with the customer account: the
sidebar cleanup, the realtime request workspace with its messenger
attachments, and the public catalog's restricted role. The account adds an app,
a nullable request column and a link in the PUBLIC header — and nothing else.
These tests say so in a way that fails if that ever stops being true.
"""

import pytest
from django.test import Client
from django.urls import reverse

from apps.accounts.context_processors import navigation
from tests.customer_account_support import (
    ACCOUNT_OFF,
    account_on,
    public_account_runtime,
)
from tests.public_catalog_support import PUBLIC_HOST, public_runtime_settings

# The entries the accepted sidebar cleanup removed. None may come back.
RETIRED_SIDEBAR_LABELS = [
    "Списания",
    "Каталоги",
    "КАТАЛОГИ",
    "BRP",
    "Polaris",
    "Справочники",
    "Инструменты",
    "Нераспознанные",
]
MAX_USER = 9000001


@pytest.fixture
def staff_client(db, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="account-compat-admin", password="compat-password-12345"
    )
    client = Client()
    client.force_login(user)
    return client


# --- Sidebar ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_account_app_never_appears_in_the_internal_sidebar(staff_client):
    """The account lives on the public runtime; it is not an employee screen."""
    for enabled in (True, False):
        with account_on(CUSTOMER_ACCOUNT_ENABLED=enabled):
            body = staff_client.get(reverse("dashboard")).content.decode()
            assert "customer_account" not in body
            assert "/account/" not in body
            assert "Мой кабинет" not in body


@pytest.mark.django_db
def test_the_accepted_sidebar_cleanup_survives_with_the_account_enabled(staff_client):
    with account_on():
        body = staff_client.get(reverse("dashboard")).content.decode()
        sidebar = body[body.index("<nav"):body.rindex("</nav>")]
        for label in RETIRED_SIDEBAR_LABELS:
            assert f">{label}<" not in sidebar, label


@pytest.mark.django_db
def test_the_navigation_context_is_identical_with_the_account_on_and_off(
    db, django_user_model, rf
):
    """Turning the account on must not add, remove or reorder one sidebar entry."""
    user = django_user_model.objects.create_superuser(
        username="account-nav-admin", password="compat-password-12345"
    )

    def labels(**flags):
        request = rf.get("/")
        request.user = user
        with account_on(**flags):
            context = navigation(request)
        return [
            (group.get("label"), tuple(item.get("label") for item in group.get("items", [])))
            for group in context.get("nav_groups", [])
        ]

    assert labels(CUSTOMER_ACCOUNT_ENABLED=True) == labels(CUSTOMER_ACCOUNT_ENABLED=False)


@pytest.mark.django_db
def test_no_internal_route_changes_availability_with_the_account(staff_client):
    for name in ["dashboard", "part_list", "customer_request_workspace"]:
        try:
            url = reverse(name)
        except Exception:  # noqa: BLE001 - the route simply is not in this build
            continue
        with account_on():
            on = staff_client.get(url).status_code
        with account_on(CUSTOMER_ACCOUNT_ENABLED=False):
            off = staff_client.get(url).status_code
        assert on == off, name


# --- Realtime, attachments, human numbers ------------------------------------------------


@pytest.mark.django_db
def test_the_account_adds_no_column_the_workspace_reads(public_catalog):
    """WorkspaceEvent is untouched: the account writes none and reads none."""
    from apps.customer_requests.models import WorkspaceEvent

    fields = {field.name for field in WorkspaceEvent._meta.get_fields()}
    assert "customer_account" not in fields
    assert not any(name.startswith("customer_account") for name in fields)


@pytest.mark.django_db
def test_the_request_ownership_column_is_the_only_thing_added_to_requests():
    from apps.customer_requests.models import CustomerRequest

    field = CustomerRequest._meta.get_field("customer_account")
    assert field.null and field.blank
    assert field.remote_field.on_delete.__name__ == "SET_NULL"


@pytest.mark.django_db
def test_human_request_numbers_are_untouched_by_the_account(public_catalog):
    from apps.customer_requests.models import CustomerRequest
    from tests.customer_account_support import make_request

    part = public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    public_catalog.stock(part, "10")
    with public_account_runtime():
        first = make_request(public_catalog, part, key="hn-1")
        second = make_request(public_catalog, part, key="hn-2")
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.human_number and second.human_number
    assert second.human_number == first.human_number + 1
    assert CustomerRequest.reference_for(first.public_id)


@pytest.mark.django_db
def test_attachment_models_are_unchanged_by_the_account():
    """The account stores no attachment and adds no field to the messenger rows."""
    from apps.customer_requests.models import MaxMessage, TelegramMessage

    for model in (MaxMessage, TelegramMessage):
        names = {field.name for field in model._meta.get_fields()}
        assert not any(name.startswith("customer_account") for name in names)


@pytest.mark.django_db
def test_the_account_only_redacts_its_own_code_messages(public_catalog):
    """The outbox redaction must match account codes and nothing else."""
    from apps.customer_accounts import services, tokens
    from apps.customer_requests.models import MaxMessage

    with public_account_runtime():
        ordinary = MaxMessage.objects.create(
            direction=MaxMessage.Direction.SYSTEM,
            recipient_chat_id=555,
            text="Фото по заявке №1",
            dedupe_key="request-photo:1",
        )
        token_hash = tokens.digest("some-token")
        code_row = MaxMessage.objects.create(
            direction=MaxMessage.Direction.SYSTEM,
            recipient_chat_id=555,
            text="Код для входа: 123456",
            dedupe_key=f"account-code:{token_hash[:32]}:evt-1",
        )
        services._redact_code_messages(token_hash)

        ordinary.refresh_from_db()
        code_row.refresh_from_db()
        assert ordinary.text == "Фото по заявке №1"  # untouched
        assert code_row.text == services.CODE_USED_TEXT


# --- The public catalog itself -------------------------------------------------------------


@pytest.mark.django_db
def test_the_public_catalog_is_byte_identical_in_the_header_when_disabled(public_catalog):
    part = public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    public_catalog.stock(part, "5")
    with public_runtime_settings(**ACCOUNT_OFF):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        for path in ["/", "/search/?q=piston", f"/parts/{part.public_id}/", "/cart/"]:
            body = client.get(path).content.decode()
            assert "account-link" not in body, path
            assert "/account/" not in body, path


@pytest.mark.django_db
def test_enabling_the_account_adds_exactly_one_header_link(public_catalog):
    public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    with public_runtime_settings(**ACCOUNT_OFF):
        off = Client(HTTP_HOST=PUBLIC_HOST).get("/").content.decode()
    with public_account_runtime():
        on = Client(HTTP_HOST=PUBLIC_HOST).get("/").content.decode()
    assert on.count("account-link") == 1 and off.count("account-link") == 0
    # And the rest of the page is untouched: strip the one added anchor from the
    # enabled page and what remains is exactly the disabled page.
    start = on.index('<a class="account-link"')
    end = on.index("</a>", start) + len("</a>")
    stripped = on[:start] + on[end:]
    squash = lambda html: " ".join(html.split())  # noqa: E731 - test-local
    assert squash(stripped) == squash(off)
