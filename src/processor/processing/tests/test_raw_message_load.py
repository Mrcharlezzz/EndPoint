# pyright: reportAttributeAccessIssue=false, reportIndexIssue=false

"""Load 3 raw message ORM objects from fixture data and verify they are accessible."""

import pytest

from core.models.ingested import IngestedItem

from processing.tests.fixtures import create_pending_teams_raw_message


@pytest.mark.django_db
def test_load_three_raw_messages():
    # Record 1: grading reminder (default factory data)
    msg1 = create_pending_teams_raw_message()

    # Record 2: syllabus link
    msg2 = create_pending_teams_raw_message(
        payload_overrides={
            "body": "The (public) web page for this module with the syllabus and links to learning materials.",
            "author": None,
        }
    )

    # Record 3: online class reminder
    msg3 = create_pending_teams_raw_message(
        payload_overrides={
            "body": (
                "Dear Students, for today's online class please complete "
                "Module 2 of the mock test available in the Lecture Materials section."
            ),
            "author": None,
        }
    )

    # All three are distinct ORM objects
    ids = {msg1.pk, msg2.pk, msg3.pk}
    assert len(ids) == 3

    # All queryable as pending raw_messages
    loaded = list(
        IngestedItem.objects.filter(
            pk__in=ids,
            processing_status="pending",
            payload__content_type="raw_message",
        )
    )
    assert len(loaded) == 3

    # ORM objects have the right fields accessible
    by_pk = {obj.pk: obj for obj in loaded}

    assert "grading" in by_pk[msg1.pk].payload["body"]
    assert by_pk[msg1.pk].payload["author"] == "Example Teacher"
    assert by_pk[msg1.pk].platform == "teams"

    assert by_pk[msg2.pk].payload["body"].startswith("The (public) web page")
    assert by_pk[msg2.pk].payload["author"] is None

    assert "online class" in by_pk[msg3.pk].payload["body"]
    assert by_pk[msg3.pk].payload["author"] is None
