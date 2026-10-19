from datetime import datetime, timezone

import pytest
from core.models import (
    Course,
    CoursePlatformMapping,
    IngestedItem,
    BaseClassifiedItem,
    Assignment,
    Announcement,
    Event,
    Material,
    Link,
    CanonicalEntity,
    ClassificationLog,
    DedupLog,
    Platform,
    PlatformCredential,
    EntityType,
    ClassificationMethod,
)


@pytest.mark.django_db
def test_create_course():
    course = Course.objects.create(
        name="Software Engineering", code="CS-301", major="CS"
    )
    assert Course.objects.count() == 1
    assert course.code == "CS-301"


@pytest.mark.django_db
def test_create_course_platform_mapping():
    course = Course.objects.create(name="Software Engineering", code="CS-301")
    mapping = CoursePlatformMapping.objects.create(
        course=course,
        platform=Platform.MOODLE,
        external_course_id="8740",
        external_course_name="F25_SAT_02_SAT Training - English",
    )

    assert mapping.course_id == course.id
    assert mapping.platform == Platform.MOODLE
    assert mapping.external_course_id == "8740"


def _make_ingested(external_id, platform=Platform.MOODLE, **kwargs):
    defaults = dict(
        schema_version="4.0",
        adapter_version="moodle-v1",
        payload={"content_type": "assignment"},
    )
    defaults.update(kwargs)
    return IngestedItem.objects.create(
        external_id=external_id, platform=platform, **defaults
    )


@pytest.mark.django_db
def test_ingested_item_external_id_unique():
    _make_ingested("moodle_assignment_1")
    with pytest.raises(Exception):
        _make_ingested("moodle_assignment_1")


@pytest.mark.django_db
def test_classified_item_chain():
    ingested = _make_ingested("moodle_assignment_42")
    classified = Assignment.objects.create(
        entity_type=EntityType.ASSIGNMENT,
        ingested_item=ingested,
        title="Lab 3",
    )
    assert ingested.processing_status == "pending"
    assert classified.ingested_item.platform == "moodle"
    assert Assignment.objects.count() == 1


@pytest.mark.django_db
def test_material_item_chain():
    ingested = _make_ingested(
        "moodle_material_42",
        payload={"content_type": "material"},
    )
    classified = Material.objects.create(
        entity_type=EntityType.MATERIAL,
        ingested_item=ingested,
        title="Lecture 1 slides",
    )

    assert classified.ingested_item.platform == "moodle"
    assert classified.title == "Lecture 1 slides"
    assert Material.objects.count() == 1


@pytest.mark.django_db
def test_ingested_item_stores_error_message():
    ingested = _make_ingested(
        "teams_failed_1",
        platform=Platform.TEAMS,
        processing_status="failed",
        error_message="OpenAI request timed out while extracting fields.",
    )

    assert ingested.error_message == "OpenAI request timed out while extracting fields."


@pytest.mark.django_db
def test_ingested_item_stores_top_level_source_url():
    ingested = _make_ingested(
        "moodle_assignment_with_source_url",
        source_url="https://moodle.test/mod/assign/view.php?id=41249",
        payload={"content_type": "assignment", "title": "Homework 1"},
    )

    assert ingested.source_url == "https://moodle.test/mod/assign/view.php?id=41249"
    assert "source_url" not in ingested.payload


@pytest.mark.django_db
def test_link_fk_to_classified():
    ingested = _make_ingested("teams_msg_1", platform=Platform.TEAMS)
    classified = BaseClassifiedItem.objects.create(
        entity_type=EntityType.ANNOUNCEMENT,
        ingested_item=ingested,
    )
    link = Link.objects.create(classified_item=classified, url="https://example.com")
    assert link.classified_item_id == classified.id


@pytest.mark.django_db
def test_canonical_entity():
    course = Course.objects.create(name="DB Systems", code="CS-401")
    ingested = _make_ingested("moodle_ann_1")
    classified = BaseClassifiedItem.objects.create(
        entity_type=EntityType.ANNOUNCEMENT,
        ingested_item=ingested,
        course=course,
    )
    canonical = CanonicalEntity.objects.create(
        entity_type=EntityType.ANNOUNCEMENT,
        primary_item=classified,
        course=course,
    )
    assert canonical.course_id == course.id
    assert canonical.primary_item_id == classified.id


@pytest.mark.django_db
def test_classification_log():
    ingested = _make_ingested("telegram_msg_5", platform=Platform.TELEGRAM)
    log = ClassificationLog.objects.create(
        ingested_item=ingested,
        entity_type=EntityType.ASSIGNMENT,
        classification_method=ClassificationMethod.LLM,
        llm_model="claude-sonnet-4-6",
        confidence=0.92,
        processor_version="processor-v1",
        prompt_version="classification-prompts-v1",
    )
    assert log.ingested_item_id == ingested.id
    assert log.confidence == 0.92
    assert log.processor_version == "processor-v1"
    assert log.prompt_version == "classification-prompts-v1"


@pytest.mark.django_db
def test_classification_log_stores_token_fields():
    ingested = _make_ingested("teams_msg_tokens", platform=Platform.TEAMS)
    log = ClassificationLog.objects.create(
        ingested_item=ingested,
        entity_type=EntityType.ASSIGNMENT,
        classification_method=ClassificationMethod.LLM,
        llm_model="gpt-5",
        input_tokens=1200,
        output_tokens=180,
    )

    assert log.input_tokens == 1200
    assert log.output_tokens == 180


def test_classification_log_has_processor_metadata_fields():
    field_names = {field.name for field in ClassificationLog._meta.fields}

    assert "processor_version" in field_names
    assert "prompt_version" in field_names


@pytest.mark.django_db
def test_ingested_item_stores_processing_completed_at():
    item = _make_ingested("teams_msg_done", platform=Platform.TEAMS)
    completed_at = datetime.now(timezone.utc)

    item.processing_completed_at = completed_at
    item.save(update_fields=["processing_completed_at"])

    assert IngestedItem.objects.get(pk=item.id).processing_completed_at == completed_at


def test_processor_observability_fields_exist():
    classification_fields = {field.name for field in ClassificationLog._meta.fields}
    ingested_fields = {field.name for field in IngestedItem._meta.fields}

    assert "input_tokens" in classification_fields
    assert "output_tokens" in classification_fields
    assert "processing_completed_at" in ingested_fields


@pytest.mark.django_db
def test_platform_credential_creation_defaults():
    cred = PlatformCredential.objects.create(
        user_email="student@example.edu",
        platform=Platform.MOODLE,
        encrypted_payload=b"ciphertext-bytes",
    )

    assert cred.user_email == "student@example.edu"
    assert cred.platform == Platform.MOODLE
    assert cred.encrypted_payload == b"ciphertext-bytes"
    assert cred.display_name == ""
    assert cred.is_paused is False
    assert cred.is_expired is False
    assert cred.refresh_url == ""
    assert cred.last_refreshed_at is None
    assert cred.expired_at is None


@pytest.mark.django_db
def test_platform_credential_unique_per_user_and_platform():
    PlatformCredential.objects.create(
        user_email="student@example.edu",
        platform=Platform.TEAMS,
        encrypted_payload=b"teams-cipher",
    )

    with pytest.raises(Exception):
        PlatformCredential.objects.create(
            user_email="student@example.edu",
            platform=Platform.TEAMS,
            encrypted_payload=b"teams-cipher-2",
        )


@pytest.mark.django_db
def test_platform_credential_same_user_multiple_platforms():
    email = "student@example.edu"
    PlatformCredential.objects.create(
        user_email=email, platform=Platform.TEAMS, encrypted_payload=b"t"
    )
    PlatformCredential.objects.create(
        user_email=email, platform=Platform.MOODLE, encrypted_payload=b"m"
    )
    PlatformCredential.objects.create(
        user_email=email, platform=Platform.TELEGRAM, encrypted_payload=b"tg"
    )

    assert PlatformCredential.objects.filter(user_email=email).count() == 3


@pytest.mark.django_db
def test_platform_credential_bulk_pause_by_email():
    email = "student@example.edu"
    for platform in (Platform.TEAMS, Platform.MOODLE, Platform.TELEGRAM):
        PlatformCredential.objects.create(
            user_email=email, platform=platform, encrypted_payload=b"x"
        )

    PlatformCredential.objects.filter(user_email=email).update(is_paused=True)

    assert (
        PlatformCredential.objects.filter(user_email=email, is_paused=True).count() == 3
    )


@pytest.mark.django_db
def test_platform_credential_filter_expired_by_platform():
    PlatformCredential.objects.create(
        user_email="a@example.edu",
        platform=Platform.TEAMS,
        encrypted_payload=b"x",
        is_expired=True,
    )
    PlatformCredential.objects.create(
        user_email="b@example.edu",
        platform=Platform.TEAMS,
        encrypted_payload=b"x",
        is_expired=False,
    )
    PlatformCredential.objects.create(
        user_email="c@example.edu",
        platform=Platform.MOODLE,
        encrypted_payload=b"x",
        is_expired=True,
    )

    expired_teams = PlatformCredential.objects.filter(
        platform=Platform.TEAMS, is_expired=True
    )
    assert expired_teams.count() == 1
    assert expired_teams.first().user_email == "a@example.edu"


@pytest.mark.django_db
def test_announcement_published_at_and_dedup_status():
    ingested = _make_ingested(
        "moodle_ann_with_published_at",
        payload={"content_type": "announcement"},
    )
    classified = Announcement.objects.create(
        entity_type=EntityType.ANNOUNCEMENT,
        ingested_item=ingested,
        published_at=datetime(2025, 10, 2, 7, 47, 19, tzinfo=timezone.utc),
    )

    assert classified.published_at == datetime(
        2025, 10, 2, 7, 47, 19, tzinfo=timezone.utc
    )
    assert classified.dedup_status == "skipped"


@pytest.mark.django_db
def test_published_at_is_base_classified_field_for_all_entities():
    published_at = datetime(2025, 10, 2, 7, 47, 19, tzinfo=timezone.utc)
    ingested_assignment = _make_ingested(
        "moodle_assignment_with_published_at",
        payload={"content_type": "assignment"},
    )
    ingested_material = _make_ingested(
        "moodle_material_with_published_at",
        payload={"content_type": "material"},
    )
    ingested_event = _make_ingested(
        "moodle_event_with_published_at",
        payload={"content_type": "event"},
    )

    assignment = Assignment.objects.create(
        entity_type=EntityType.ASSIGNMENT,
        ingested_item=ingested_assignment,
        published_at=published_at,
    )
    material = Material.objects.create(
        entity_type=EntityType.MATERIAL,
        ingested_item=ingested_material,
        published_at=published_at,
    )
    event = Event.objects.create(
        entity_type=EntityType.EVENT,
        ingested_item=ingested_event,
        published_at=published_at,
    )

    assert assignment.published_at == published_at
    assert material.published_at == published_at
    assert event.published_at == published_at


def test_removed_schema_exports_are_absent():
    import core.models as models

    assert not hasattr(models, "AssignmentStatus")
    assert not hasattr(models, "EntityDate")
    assert "AssignmentStatus" not in models.__all__
    assert "EntityDate" not in models.__all__


def test_assignment_schema_does_not_expose_grading_or_status_fields():
    field_names = set(Assignment._meta.fields_map) | {
        field.name for field in Assignment._meta.fields
    }

    assert "max_points" not in field_names
    assert "points" not in field_names
    assert "status" not in field_names


@pytest.mark.django_db
def test_dedup_log():
    course = Course.objects.create(name="DB Systems", code="CS-401")
    ingested = _make_ingested("telegram_msg_6", platform=Platform.TELEGRAM)
    classified = BaseClassifiedItem.objects.create(
        entity_type=EntityType.ANNOUNCEMENT,
        ingested_item=ingested,
        course=course,
    )
    canonical = CanonicalEntity.objects.create(
        entity_type=EntityType.ANNOUNCEMENT,
        primary_item=classified,
        course=course,
    )

    log = DedupLog.objects.create(
        classified_item=classified,
        canonical_entity=canonical,
        status="linked",
        match_strategy="weighted",
        resolution_strategy="first_seen",
        score=0.93,
    )

    assert log.classified_item_id == classified.id
    assert log.canonical_entity_id == canonical.id
    assert log.status == "linked"
    assert log.score == 0.93
