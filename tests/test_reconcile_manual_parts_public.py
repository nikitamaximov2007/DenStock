from decimal import Decimal
from io import StringIO

from django.core.management import call_command

from apps.catalog.models import PartType
from apps.catalog.services import create_manual_part


def test_reconcile_manual_parts_is_dry_run_by_default(db):
    part = create_manual_part(name="Топливный фильтр", price=Decimal("2350"))
    part.price_provenance = PartType.PriceProvenance.UNVERIFIED
    part.save(update_fields=["price_provenance"])
    output = StringIO()

    call_command("reconcile_manual_parts_public", stdout=output)

    part.refresh_from_db()
    assert part.price_provenance == PartType.PriceProvenance.UNVERIFIED
    assert "изменений нет" in output.getvalue()


def test_reconcile_manual_parts_apply_preserves_hidden_parts(db):
    part = create_manual_part(name="Скрытая ручная деталь", price=Decimal("2350"))
    part.is_public = False
    part.price_provenance = PartType.PriceProvenance.UNVERIFIED
    part.save(update_fields=["is_public", "price_provenance"])
    output = StringIO()

    call_command("reconcile_manual_parts_public", "--apply", stdout=output)

    part.refresh_from_db()
    assert part.price_provenance == PartType.PriceProvenance.VALID_MANUAL_EXCEPTION
    assert part.is_public is False
    assert "не опубликованы" in output.getvalue()

