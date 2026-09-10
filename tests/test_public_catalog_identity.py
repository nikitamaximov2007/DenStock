import uuid

import pytest
from django.core.exceptions import ValidationError

from apps.catalog.models import Category, PartType, Unit


@pytest.fixture
def public_part(db):
    category = Category.objects.create(name="Public identity category")
    unit = Unit.objects.create(name="Public identity unit", short_name="шт")
    return PartType.objects.create(name="Public identity part", category=category, unit=unit)


def test_public_identity_is_opaque_unique_and_created_automatically(public_part):
    assert isinstance(public_part.public_id, uuid.UUID)
    assert public_part.is_public is True


def test_public_identity_cannot_change_through_model_validation(public_part):
    public_part.public_id = uuid.uuid4()
    with pytest.raises(ValidationError, match="Публичный ID"):
        public_part.full_clean()
