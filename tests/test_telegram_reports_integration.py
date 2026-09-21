"""Telegram V1 and the Reports rework stay independent of each other.

Telegram customer messaging is optional reference data next to a
CustomerRequest. It must never create business documents, move stock, change
the canonical profit (actual sale price minus the immutable dealer-base
snapshot) or depend on profit pricing. The profit snapshot migration, in turn,
must not touch Telegram.
"""

import ast
from datetime import timedelta
from pathlib import Path

from django.apps import apps as registry
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.customer_requests.messengers import issue_telegram_link
from apps.customer_requests.models import (
    CustomerRequest,
    TelegramConversation,
    TelegramMessage,
    TelegramOutboxEvent,
)
from apps.customer_requests.services import RequestLineInput, create_customer_request
from apps.customer_requests.telegram_bot import TelegramBotWorker
from apps.reports.services import (
    Period,
    get_dashboard_report,
    get_sales_report,
    get_writeoffs_report,
)
from tests.test_sale_price_profit_snapshots import _complete, priced_sale  # noqa: F401
from tests.test_telegram_customer_messaging import (
    CUSTOMER,
    OPERATOR_A,
    FakeBotApi,
    _operator,
    message_update,
)

from .test_customer_requests import POLICY

ROOT = Path(__file__).resolve().parents[1]
BUSINESS_APPS = (
    "sales",
    "inventory",
    "repairs",
    "receipts",
    "writeoffs",
    "returns",
    "procurement",
    "stocktaking",
    "counting",
)
TELEGRAM_MIGRATIONS = (
    ("customer_requests", "0005_telegram_messaging"),
    ("customer_requests", "0006_telegram_public_insert_guard"),
    ("operations", "0005_telegram_messaging"),
)
PROFIT_SNAPSHOT_MIGRATION = ("sales", "0007_saleline_unmarked_price_snapshot")


def _business_counts() -> dict:
    return {
        model._meta.label: model.objects.count()
        for model in registry.get_models()
        if model._meta.app_label in BUSINESS_APPS
    }


def _period() -> Period:
    today = timezone.localdate()
    return Period(today - timedelta(days=1), today, "")


def _report_values():
    sales = get_sales_report(_period())
    writeoffs = get_writeoffs_report(_period())
    return (
        sales.revenue,
        sales.profit,
        sales.profit_unavailable_lines,
        writeoffs.count,
        writeoffs.cost,
        repr(get_dashboard_report(_period())),
    )


def test_telegram_request_linking_and_messages_leave_business_data_and_profit_untouched(
    priced_sale, django_user_model  # noqa: F811
):
    user, part, lot, _location = priced_sale
    _complete(user, lot, unit_price="16000")
    counts = _business_counts()
    reports = _report_values()
    assert reports[1] > 0  # a real, non-trivial profit is being protected

    request, created = create_customer_request(
        customer_name="Клиент Telegram",
        customer_phone="+7 (912) 123-45-67",
        preferred_messenger=CustomerRequest.Messenger.TELEGRAM,
        comment="Нужна та же деталь.",
        lines=[RequestLineInput(part_id=part.pk, quantity="2", supply_inquiry=False)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key="t" * 32,
    )
    assert created
    _operator(django_user_model, OPERATOR_A, username="telegram-seller")
    api = FakeBotApi()
    worker = TelegramBotWorker(api, worker_id="reports-check", poll_timeout=0, heartbeat_file="")
    worker.start()
    token = issue_telegram_link(request_id=request.pk).token
    api.updates.extend(
        [
            message_update(CUSTOMER, f"/start {token}"),
            message_update(CUSTOMER, "Деталь есть в наличии?"),
        ]
    )
    worker.iterate(poll_timeout=0)
    worker.iterate(poll_timeout=0)

    assert TelegramConversation.objects.get(request=request).is_linked
    assert TelegramMessage.objects.filter(conversation__request=request).exists()
    assert TelegramOutboxEvent.objects.filter(request=request).count() >= 2
    assert _business_counts() == counts
    with CaptureQueriesContext(connection) as queries:
        after = _report_values()
    assert after == reports
    assert not [q["sql"] for q in queries.captured_queries if "telegram" in q["sql"].lower()]


def test_telegram_and_profit_snapshot_migrations_are_independent():
    loader = MigrationLoader(None, ignore_no_migrations=True)
    graph = loader.graph
    assert loader.detect_conflicts() == {}
    assert PROFIT_SNAPSHOT_MIGRATION in graph.nodes
    profit_plan = set(graph.forwards_plan(PROFIT_SNAPSHOT_MIGRATION))
    for key in TELEGRAM_MIGRATIONS:
        assert key in graph.nodes
        assert PROFIT_SNAPSHOT_MIGRATION not in graph.forwards_plan(key)
        assert key not in profit_plan
        migration = loader.get_migration(*key)
        touched = {
            str(getattr(operation, "model_name", "") or getattr(operation, "name", "")).lower()
            for operation in migration.operations
        }
        assert not {name for name in touched if "sale" in name}, (key, touched)
    profit = loader.get_migration(*PROFIT_SNAPSHOT_MIGRATION)
    assert not [
        operation
        for operation in profit.operations
        if "telegram" in str(getattr(operation, "model_name", "")).lower()
    ]


def _imports(package: Path):
    for path in package.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                yield path, node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    yield path, alias.name


def test_telegram_code_does_not_depend_on_profit_pricing_and_reports_ignore_telegram():
    forbidden_for_telegram = ("apps.reports", "apps.sales.pricing_snapshots")
    for path, module in _imports(ROOT / "apps" / "customer_requests"):
        assert not module.startswith(forbidden_for_telegram), (path, module)
    for path, module in _imports(ROOT / "apps" / "reports"):
        assert not module.startswith("apps.customer_requests"), (path, module)
