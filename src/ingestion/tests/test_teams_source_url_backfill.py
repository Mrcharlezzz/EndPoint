from __future__ import annotations

from ingestion.daemon.source_url_backfill import (
    build_teams_csa_source_url,
    teams_csa_message_id,
)


def test_teams_csa_message_id_parses_expected_external_id() -> None:
    assert teams_csa_message_id("teams_csa_1709234277523") == "1709234277523"


def test_teams_csa_message_id_rejects_other_external_ids() -> None:
    assert teams_csa_message_id("teams_assignment_abc") is None
    assert teams_csa_message_id("teams_csa_") is None
    assert teams_csa_message_id("") is None


def test_build_teams_csa_source_url_uses_chat_context_deep_link() -> None:
    url = build_teams_csa_source_url(
        external_id="teams_csa_1709234277523",
        chat_id="19:QgXaSfEp0OpsGmBIU-K6cxF09s0u7_Qvp-TlKlzbV041@thread.tacv2",
    )

    assert url == (
        "https://teams.microsoft.com/l/message/"
        "19%3AQgXaSfEp0OpsGmBIU-K6cxF09s0u7_Qvp-TlKlzbV041%40thread.tacv2/"
        "1709234277523?context=%7B%22contextType%22%3A%22chat%22%7D"
    )


def test_build_teams_csa_source_url_requires_chat_id() -> None:
    assert build_teams_csa_source_url("teams_csa_1709234277523", None) is None
    assert build_teams_csa_source_url("teams_csa_1709234277523", "") is None
