"""PostgreSQL: the public role may issue a MAX deep link for its own request.

The success page hands a MAX customer over exactly like a Telegram one: after
the customer's local POST the public role inserts one link token. The guard of
0006/0007 accepted only ``telegram`` tokens. This keeps every other check (the
submission-key proof, the initial row shape, the lifetime and the per-request
cap) and admits the ``max`` channel. The public role gets no grant on any MAX
table: conversations, messages and events are written by the internal runtime.
"""

import importlib

from django.db import migrations

PREVIOUS = importlib.import_module(
    "apps.customer_requests.migrations.0007_telegram_link_attempt_cap"
).CREATE_FUNCTION
OLD_CHECK = "IF NEW.channel <> 'telegram'"
NEW_CHECK = "IF NEW.channel NOT IN ('telegram', 'max')"
assert PREVIOUS.count(OLD_CHECK) == 1
CREATE_FUNCTION = PREVIOUS.replace(OLD_CHECK, NEW_CHECK)


def forward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(CREATE_FUNCTION)


def backward(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute(PREVIOUS)


class Migration(migrations.Migration):
    dependencies = [("customer_requests", "0008_max_messaging")]

    operations = [migrations.RunPython(forward, backward)]
