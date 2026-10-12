from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.contrib.auth import get_user_model
from django.db import close_old_connections, connection

from apps.catalog.models import Category, PartType, Unit
from apps.counting.models import InventoryCountingSession, InventoryScanEvent
from apps.counting.services import record_part_pick
from apps.warehouse.addresses import get_or_create_location

pytestmark = [
    pytest.mark.postgresql,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="Requires PostgreSQL row locks"),
]


def test_concurrent_duplicate_name_pick_token_counts_one_unit():
    unit, _ = Unit.objects.get_or_create(name="Штука")
    category = Category.objects.create(name="Concurrent picker")
    part = PartType.objects.create(
        name="Сальник конкурентный",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    location = get_or_create_location("S01-D03-C08", name="Concurrent picker")
    session = InventoryCountingSession.objects.create(
        storage_location=location,
        full_address=location.code,
        title="Concurrent picker",
    )
    operator = get_user_model().objects.create_user("picker-race")
    barrier = Barrier(2)

    def submit():
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            current_session = InventoryCountingSession.objects.get(pk=session.pk)
            current_part = PartType.objects.get(pk=part.pk)
            return record_part_pick(
                current_session,
                current_part,
                by=operator,
                request_token="same-picker-submit-token",
            ).pk
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        line_ids = list(pool.map(lambda _index: submit(), range(2)))

    session.refresh_from_db()
    assert line_ids[0] == line_ids[1]
    assert session.lines.count() == 1
    line = session.lines.get()
    assert line.warehouse_part_id == part.pk
    assert line.quantity_counted == 1
    assert InventoryScanEvent.objects.filter(session=session).count() == 1
