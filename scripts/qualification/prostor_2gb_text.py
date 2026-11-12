"""Exercise synthetic Telegram/MAX text handlers against an isolated QA database.

No provider API is called. MAX replies remain in the QA outbox and are
checked for idempotency; Telegram replies are in-memory handler results.
"""

import argparse
import json
import os
import time
import uuid

import django


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=100)
    args = parser.parse_args()
    if args.count < 1:
        parser.error("count must be positive")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    django.setup()

    from django.db import transaction

    from apps.customer_requests import max_bot, telegram_bot
    from apps.customer_requests.models import MaxMessage

    prefix = f"qa-{uuid.uuid4().hex[:12]}"
    begun = time.monotonic()
    for index in range(args.count):
        telegram = {
            "update_id": 800_000_000 + index,
            "message": {
                "chat": {"id": 800_000_001, "type": "private"},
                "from": {"id": 800_000_001, "is_bot": False},
                "text": "/start",
            },
        }
        assert telegram_bot.handle_update(telegram)
        update = {
            "update_type": "message_created",
            "message": {
                "sender": {"user_id": 800_000_002, "is_bot": False},
                "recipient": {"chat_id": 800_000_002, "chat_type": "dialog"},
                "body": {"mid": f"{prefix}-{index}", "text": "/start"},
            },
        }
        with transaction.atomic():
            assert max_bot.handle_update(update) == "greeting"
        # A redelivery of the same MAX event must not add another reply.
        with transaction.atomic():
            max_bot.handle_update(update)
    queued = MaxMessage.objects.filter(dedupe_key__startswith=f"reply:{prefix}").count()
    report = {
        "telegram_handler_replies": args.count,
        "max_unique_queued_replies": queued,
        "max_redeliveries": args.count,
        "elapsed_seconds": round(time.monotonic() - begun, 2),
    }
    print(json.dumps(report, sort_keys=True))
    return 0 if queued == args.count else 1


if __name__ == "__main__":
    raise SystemExit(main())
