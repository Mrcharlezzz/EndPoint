from __future__ import annotations

from uuid import uuid4

from processing.adapters.llm.prompts import (
    build_extraction_system_prompt,
    build_user_prompt,
)
from processing.models.envelope import IngestedItemContext, ProcessingEnvelope
from processing.models.entities import RawMessage


def _make_envelope() -> ProcessingEnvelope:
    ingested = IngestedItemContext(
        id=uuid4(),
        external_id="teams_msg_1",
        platform="teams",
        course_external_id="thread-123",
        schema_version="4.0",
        adapter_version="v0",
        ingestion_mode="server_poll",
        processing_status="pending",
        source_url="https://teams.microsoft.com/l/message/1",
        payload={
            "content_type": "raw_message",
            "title": "Reminder",
            "author": "Example",
            "body": "The homework is due next Tuesday.",
            "course_name": "SYNTHETIC EXAMPLE COURSE",
            "published_at": "2025-10-02T07:47:19+00:00",
            "deadline": None,
            "submission_format": None,
            "start_time": None,
            "end_time": None,
            "location": None,
            "attachments": [],
            "links": [],
        },
    )
    return ProcessingEnvelope(
        id=ingested.id,
        ingested_item=ingested,
        entity=RawMessage(
            title="Reminder", body="The homework is due next Tuesday.", author="Example"
        ),
    )


def test_classification_prompt_lists_raw_message_with_rubric():
    from processing.adapters.llm.prompts import get_prompt_set

    prompt = get_prompt_set("entity-inference-v1").classification_system_prompt

    assert "raw_message" in prompt
    assert "greetings" in prompt or "chit-chat" in prompt


def test_build_user_prompt_includes_entity_and_ingested_context():
    prompt = build_user_prompt(_make_envelope())

    assert "Platform: teams" in prompt
    assert "Adapter Version: v0" in prompt
    assert "Course Name: SYNTHETIC EXAMPLE COURSE" in prompt
    assert "Source URL: https://teams.microsoft.com/l/message/1" in prompt
    assert "Author: Example" in prompt
    assert "Title: Reminder" in prompt
    assert "Body:\nThe homework is due next Tuesday." in prompt


def test_extraction_prompt_field_isolation():
    """Each entity type's prompt contains exactly its own type-specific fields
    and excludes fields belonging to other types."""
    assignment = build_extraction_system_prompt("assignment")
    event = build_extraction_system_prompt("event")
    announcement = build_extraction_system_prompt("announcement")
    material = build_extraction_system_prompt("material")

    # assignment-specific fields present in assignment, absent from event
    for field in ("deadline", "submission_format"):
        assert f"{field}\n- Return:" in assignment, f"assignment prompt missing {field}"
        assert f"{field}\n- Return:" not in event, f"event prompt leaked {field}"

    for field in ("max_points", "points", "status", "dates"):
        assert f"{field}\n- Return:" not in assignment, (
            f"assignment prompt leaked removed field {field}"
        )

    # event-specific fields present in event, absent from assignment
    for field in ("start_time", "end_time", "location"):
        assert f"{field}\n- Return:" in event, f"event prompt missing {field}"
        assert f"{field}\n- Return:" not in assignment, (
            f"assignment prompt leaked {field}"
        )

    # announcement and material have no type-specific fields beyond the base set
    for field in (
        "deadline",
        "submission_format",
        "max_points",
        "points",
        "status",
        "dates",
        "start_time",
        "end_time",
        "location",
    ):
        assert f"{field}\n- Return:" not in announcement, (
            f"announcement prompt leaked {field}"
        )
        assert f"{field}\n- Return:" not in material, f"material prompt leaked {field}"

    for prompt in (assignment, event, announcement, material):
        assert "links\n- Return:" not in prompt
        assert "_source_kind\n- Return:" not in prompt
        assert "published_at_offset_utc" not in prompt


def test_prompt_sets_are_versioned_and_fingerprinted():
    from processing.adapters.llm.prompts import get_prompt_set

    current = get_prompt_set("entity-inference-v1")
    experimental = get_prompt_set("entity-inference-v2")
    v3 = get_prompt_set("entity-inference-v3")

    assert current.version == "entity-inference-v1"
    assert experimental.version == "entity-inference-v2"
    assert v3.version == "entity-inference-v3"
    assert current.fingerprint != experimental.fingerprint
    assert experimental.fingerprint != v3.fingerprint
    assert len(experimental.fingerprint) == 12
    assert len(v3.fingerprint) == 12


def test_v2_uses_registry_derived_extraction_fields():
    from processing.adapters.llm.field_instructions import ENTITY_EXTRACTION_FIELDS
    from processing.adapters.llm.prompts import get_prompt_set

    current = get_prompt_set("entity-inference-v1")
    prompt_set = get_prompt_set("entity-inference-v2")

    assert prompt_set.extraction_fields is None
    assert (
        current.extraction_fields_for("assignment")
        == ENTITY_EXTRACTION_FIELDS["assignment"]
    )
    assert current.extraction_fields_for("event") == ENTITY_EXTRACTION_FIELDS["event"]
    assert prompt_set.extraction_fields_for("assignment") == (
        "title",
        "deadline",
        "deadline_source_kind",
        "submission_format",
    )
    assert prompt_set.extraction_fields_for("event") == (
        "title",
        "start_time",
        "start_time_source_kind",
        "end_time",
        "end_time_source_kind",
        "location",
    )


def test_unknown_prompt_set_fails_clearly():
    from processing.adapters.llm.prompts import get_prompt_set

    try:
        get_prompt_set("missing-prompt-set")
    except ValueError as exc:
        assert "Unknown prompt version" in str(exc)
        assert "entity-inference-v1" in str(exc)
        assert "entity-inference-v2" in str(exc)
        assert "entity-inference-v3" in str(exc)
    else:
        raise AssertionError("Expected unknown prompt version to fail")


def test_v2_classification_prompt_contains_decision_rubric_and_uncertainty_rules():
    from processing.adapters.llm.prompts import get_prompt_set

    prompt = get_prompt_set("entity-inference-v2").classification_system_prompt

    assert "Type criteria" in prompt
    assert "Tie-breakers" in prompt
    assert "If uncertain between raw_message and a publishable type" in prompt
    assert "default to raw_message" in prompt
    assert "assignment" in prompt
    assert "event" in prompt
    assert "material" in prompt
    assert "announcement" in prompt


def test_v2_classification_prompt_uses_publishable_signal_framing():
    from processing.adapters.llm.prompts import get_prompt_set

    prompt = get_prompt_set("entity-inference-v2").classification_system_prompt

    assert "publishable entity signals" in prompt
    assert "instruction, request, or directive" in prompt
    assert "addresses a group of students" in prompt
    assert "If the signals are ambiguous" in prompt
    assert "default to raw_message" in prompt
    assert "team reports" in prompt
    assert "replies" in prompt
    assert "personal/team retrospective" not in prompt
    assert "Length is not a signal" not in prompt


def test_v3_classification_prompt_mentions_non_durable_near_term_content():
    from processing.adapters.llm.prompts import get_prompt_set

    prompt = get_prompt_set("entity-inference-v3").classification_system_prompt

    assert "non-durable" in prompt
    assert "raw_message" in prompt
    assert "near-term reminders" in prompt


def test_v2_type_criteria_keep_general_signals_out_of_entity_rules():
    from processing.adapters.llm.prompts import get_prompt_set

    prompt = get_prompt_set("entity-inference-v2").classification_system_prompt
    type_criteria = prompt.split("Type-specific signals:", 1)[1].split(
        "Tie-breakers:", 1
    )[0]

    for term in ("homework", "lab", "contest", "task", "submission"):
        assert term in type_criteria
    assert "lecture, quiz session, lab, office hour" in type_criteria
    assert "slides, recording, reading" in type_criteria
    assert "standalone course information" in type_criteria
    assert "cohort" not in type_criteria
    assert "group of students" not in type_criteria


def test_v2_extraction_prompt_makes_deadline_time_policy_explicit():
    from processing.adapters.llm.prompts import get_prompt_set

    prompt = get_prompt_set("entity-inference-v2").build_extraction_system_prompt(
        "assignment"
    )

    assert "23:59 local academic time" in prompt


def test_v2_extraction_prompt_includes_temporal_source_kind_contract_once():
    from processing.adapters.llm.prompts import get_prompt_set

    event_prompt = get_prompt_set("entity-inference-v2").build_extraction_system_prompt(
        "event"
    )
    assignment_prompt = get_prompt_set(
        "entity-inference-v2"
    ).build_extraction_system_prompt("assignment")

    assert "start_time_source_kind" in event_prompt
    assert "end_time_source_kind" in event_prompt
    assert "deadline_source_kind" in assignment_prompt
    assert "explicit_local_time" in event_prompt
    assert "published_at_offset_utc" in event_prompt
    assert event_prompt.count("Use `published_at_offset_utc` only") == 1
    assert "Follow the Temporal date/time rules above." in event_prompt
    assert "Use `published_at_offset_utc` only" in assignment_prompt


def test_prompt_public_module_does_not_define_version_content():
    import processing.adapters.llm.prompts as prompts

    assert not hasattr(prompts, "CLASSIFICATION_SYSTEM_PROMPT_V2")
    assert not hasattr(prompts, "FIELD_INSTRUCTIONS_V2")
