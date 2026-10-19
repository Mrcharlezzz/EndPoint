from __future__ import annotations

import subprocess
from types import SimpleNamespace

from evaluation.run_eval import _git_sha, _update_run_timing_metrics


def test_git_sha_short_includes_head_revision(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args, check, capture_output, text):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="abc123\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _git_sha(short=True) == "abc123"
    assert calls == [["git", "rev-parse", "--short", "HEAD"]]


def test_update_run_timing_metrics_adds_wall_clock_and_throughput() -> None:
    metrics = {"run": {"total_rows": 20}}

    _update_run_timing_metrics(metrics, wall_clock_seconds=10.0)

    assert metrics["run"]["wall_clock_seconds"] == 10.0
    assert metrics["run"]["throughput_rows_per_second"] == 2.0


def test_prompt_metadata_reads_active_classifier_prompt_version() -> None:
    from evaluation.run_eval import _prompt_metadata

    classifier = SimpleNamespace(
        prompt_version="entity-inference-v2",
        prompt_fingerprint="abc123def456",
    )

    assert _prompt_metadata(classifier) == {
        "prompt_version": "entity-inference-v2",
        "prompt_fingerprint": "abc123def456",
    }
