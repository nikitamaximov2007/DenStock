"""Reject invalid lot-creation markers without changing historical NULL rows."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0018_stocklot_creation_origin"),
    ]

    operations = [
        migrations.AddConstraint(
            model_name="stocklot",
            constraint=models.CheckConstraint(
                condition=models.Q(creation_origin__isnull=True) | models.Q(
                    creation_origin__in=[
                        "supplier_pending", "supplier_received", "transfer", "return",
                        "found", "recount",
                    ]
                ),
                name="stocklot_creation_origin_valid",
            ),
        ),
    ]
