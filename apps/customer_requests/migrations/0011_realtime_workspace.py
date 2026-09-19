from django.db import migrations, models
import django.db.models.deletion


def backfill_numbers(apps, schema_editor):
    Request = apps.get_model("customer_requests", "CustomerRequest")
    Counter = apps.get_model("customer_requests", "CustomerRequestNumberSequence")
    number = 1
    for request in Request.objects.order_by("created_at", "pk").iterator():
        request.human_number = number
        request.save(update_fields=["human_number"])
        number += 1
    Counter.objects.create(singleton=True, next_number=number)


class Migration(migrations.Migration):
    dependencies = [("customer_requests", "0010_operator_workspace")]

    operations = [
        migrations.AddField(
            model_name="customerrequest",
            name="human_number",
            field=models.PositiveBigIntegerField(
                editable=False, null=True, unique=True, verbose_name="Номер заявки"
            ),
        ),
        migrations.CreateModel(
            name="CustomerRequestNumberSequence",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("singleton", models.BooleanField(default=True, editable=False, unique=True)),
                ("next_number", models.PositiveBigIntegerField(default=1)),
            ],
            options={"verbose_name": "Последовательность номеров заявок"},
        ),
        migrations.CreateModel(
            name="WorkspaceEvent",
            fields=[
                ("event_id", models.BigAutoField(primary_key=True, serialize=False)),
                ("event_type", models.CharField(max_length=64, verbose_name="Тип")),
                ("entity_type", models.CharField(max_length=64, verbose_name="Тип объекта")),
                ("entity_id", models.CharField(max_length=128, verbose_name="Идентификатор объекта")),
                ("payload", models.JSONField(default=dict, verbose_name="Данные")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
                (
                    "request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="workspace_events",
                        to="customer_requests.customerrequest",
                    ),
                ),
            ],
            options={"ordering": ["event_id"]},
        ),
        migrations.AddIndex(
            model_name="workspaceevent",
            index=models.Index(fields=["event_id"], name="workspace_event_cursor_idx"),
        ),
        migrations.RunPython(backfill_numbers, migrations.RunPython.noop),
    ]
