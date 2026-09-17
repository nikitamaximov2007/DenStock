"""Operator workspace: replies to either messenger, and idempotent form replies.

Additive only. ``TelegramOperator.reply_request`` names the request an
employee answers in the operators' bot, so a MAX customer can be answered
there too; the old Telegram-only ``reply_conversation`` stays in place, unused,
so the previous release still runs against this schema. An operator already in
reply mode keeps that target. ``TelegramMessage.dedupe_key`` lets one DenisStock
reply form submission store exactly one Telegram reply, like ``MaxMessage``; its
database default keeps inserts by the previous release valid, so this migration
survives a rollback of the code.
Neither table is granted to the public database role.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def carry_reply_targets(apps, schema_editor):
    TelegramOperator = apps.get_model("customer_requests", "TelegramOperator")
    for operator in TelegramOperator.objects.filter(reply_conversation__isnull=False).select_related(
        "reply_conversation"
    ):
        operator.reply_request_id = operator.reply_conversation.request_id
        operator.save(update_fields=["reply_request"])


class Migration(migrations.Migration):

    dependencies = [
        ("customer_requests", "0009_max_public_link_guard"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="telegrammessage",
            name="dedupe_key",
            field=models.CharField(
                blank=True,
                db_default=models.Value(""),
                default="",
                max_length=160,
                verbose_name="Ключ сообщения",
            ),
        ),
        migrations.AddField(
            model_name="telegramoperator",
            name="reply_request",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="customer_requests.customerrequest",
                verbose_name="Отвечает на заявку",
            ),
        ),
        migrations.AlterField(
            model_name="telegramoperator",
            name="reply_conversation",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="customer_requests.telegramconversation",
                verbose_name="Отвечает на (Telegram, прежнее поле)",
            ),
        ),
        migrations.AddConstraint(
            model_name="telegrammessage",
            constraint=models.UniqueConstraint(
                condition=models.Q(("dedupe_key", ""), _negated=True),
                fields=("dedupe_key",),
                name="tg_message_dedupe_unique",
            ),
        ),
        migrations.RunPython(carry_reply_targets, migrations.RunPython.noop),
    ]
