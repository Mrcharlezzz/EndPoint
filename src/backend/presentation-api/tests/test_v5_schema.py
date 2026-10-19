import pytest
from django.core.exceptions import FieldDoesNotExist

from api.models import mirrors


def test_ingested_item_mirror_reads_source_url_column():
    field = mirrors.IngestedItem._meta.get_field("source_url")

    assert field.max_length == 2048
    assert field.null is True


def test_base_classified_item_mirror_reads_v5_dedup_and_time_fields():
    assert mirrors.BaseClassifiedItem._meta.get_field("published_at").null is True
    assert mirrors.BaseClassifiedItem._meta.get_field("dedup_status").default == "skipped"
    canonical_field = mirrors.BaseClassifiedItem._meta.get_field("canonical_entity")
    assert canonical_field.null is True
    assert canonical_field.related_model is mirrors.CanonicalEntity


def test_canonical_entity_mirror_points_to_primary_classified_item():
    primary_field = mirrors.CanonicalEntity._meta.get_field("primary_item")
    course_field = mirrors.CanonicalEntity._meta.get_field("course")

    assert primary_field.related_model is mirrors.BaseClassifiedItem
    assert course_field.related_model is mirrors.Course


@pytest.mark.parametrize("removed_field", ["max_points", "points", "status"])
def test_assignment_mirror_does_not_expose_removed_v5_fields(removed_field):
    with pytest.raises(FieldDoesNotExist):
        mirrors.Assignment._meta.get_field(removed_field)
