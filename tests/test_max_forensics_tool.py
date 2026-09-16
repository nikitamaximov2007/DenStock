"""The MAX acceptance forensics script must run and judge correctly at release time.

A returning customer with two requests, an operator reply and the Telegram
runtime are built through the real webhook and worker; then the script file is
executed exactly as Stage C will pipe it into ``manage.py shell``.
"""

import io
import json
import pathlib
import runpy
from contextlib import redirect_stdout
from unittest import mock

from django.utils import timezone

from apps.customer_requests import max_service
from apps.customer_requests.messengers import issue_max_link
from apps.customer_requests.models import MaxConversation, MaxMessage
from apps.operations.models import TelegramBotRuntime

from .max_fake import bot_started, deliver, message_callback
from .test_max_messaging import (  # noqa: F401 - fixtures
    CUSTOMER,
    CUSTOMER_CHAT,
    _request,
    drain,
    max_settings,
    operator_bot,
    operators,
    part,
    say,
    server,
    sleeps,
    worker,
)

SCRIPT = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "operations" / "max_forensics.py"
)


def _run(references, employee_names=""):
    out = io.StringIO()
    env = {"MAX_FORENSICS_REFERENCES": references, "MAX_FORENSICS_OPERATORS": employee_names}
    with mock.patch.dict("os.environ", env), redirect_stdout(out):
        runpy.run_path(str(SCRIPT), run_name="__main__")
    return out.getvalue()


def _acceptance(client, worker, part, operators, operator_bot):  # noqa: F811
    bot, _tg = operator_bot
    first = _request(part, key="F" * 32)
    deliver(client, bot_started(CUSTOMER, CUSTOMER_CHAT, issue_max_link(request_id=first.pk).token))
    drain(worker)
    say(client, worker, "первое тестовое сообщение")
    say(client, worker, "второе тестовое сообщение")
    seller = operators[0].user
    max_service.submit_operator_reply(
        request_id=first.pk, user=seller, text="тестовый ответ менеджера", submission_key="c" * 32
    )
    drain(worker)
    second = _request(part, key="G" * 32)
    say(client, worker, f"/start {issue_max_link(request_id=second.pk).token}")
    b = MaxConversation.objects.get(request=second)
    deliver(client, message_callback(CUSTOMER, CUSTOMER_CHAT, f"s:{b.public_id.hex}"))
    drain(worker)
    say(client, worker, "сообщение для B")
    for _ in range(2):
        drain(worker)
        bot.iterate(poll_timeout=0)
    TelegramBotRuntime.objects.update(heartbeat_at=timezone.now())
    return first, second, seller


def test_forensics_pass_on_a_clean_acceptance(
    client, worker, part, operators, operator_bot  # noqa: F811
):
    first, second, seller = _acceptance(client, worker, part, operators, operator_bot)
    output = _run(f"{first.reference},{second.reference}", employee_names=seller.username)
    assert output.strip().endswith("MAX FORENSICS PASS"), output
    assert "FAIL" not in output
    assert f"PASS {first.reference} acknowledgements=1" in output
    assert "returning customer used one MAX account" in output
    counts = json.loads(output.split("business_counts=", 1)[1].splitlines()[0])
    assert set(counts) >= {"sales", "sale_lines", "reservations", "stock_movements", "receipts",
                           "repairs", "repair_issues", "writeoffs", "stock_counts", "payments"}
    assert all(value == 0 for value in counts.values())
    for text in ("первое тестовое сообщение", "тестовый ответ менеджера", "сообщение для B"):
        assert text not in output  # facts only, never message bodies


def test_forensics_fail_on_stuck_rows_and_unknown_references(
    client, worker, part, operators, operator_bot  # noqa: F811
):
    first, second, _seller_user = _acceptance(client, worker, part, operators, operator_bot)
    MaxMessage.objects.filter(dedupe_key__startswith="ack:").update(delivery_status="uncertain")
    output = _run(f"{first.reference},ZZZZZZZZ")
    assert "FAIL ZZZZZZZZ not found exactly once" in output
    assert f"FAIL {first.reference} acknowledgement sent" in output
    assert output.strip().splitlines()[-1].startswith("MAX FORENSICS FAIL")
