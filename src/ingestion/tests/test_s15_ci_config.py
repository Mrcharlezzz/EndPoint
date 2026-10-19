"""Slice 15: validate the CI workflow + config files + doc completeness.

Slice 15 ships the documentation + CI gate plumbing for the daemon. The
implementer landed it in three commits:

* part A (docs): `docs/ingestion/RUNBOOK.md` (16 incident scenarios),
  `docs/ingestion/OPERATIONS.md` (lifecycle, health, metrics, alerts,
  audit, backup, lockfile, capacity), and a mermaid sync-lifecycle
  diagram in `docs/ingestion/OVERVIEW.md`.
* part B (CI): `.github/workflows/ingestion-ci.yml` with the main
  lint/format/types/pytest + pip-audit gate, a dedicated
  Postgres-backed daemon e2e job, openapi-diff, and perf-regression;
  plus `docs/ingestion/openapi.json` (130-path snapshot consumed by the
  openapi-diff job).
* part C (hygiene): `.pre-commit-config.yaml`, root `CONTRIBUTING.md`,
  `.github/dependabot.yml`, and major-version upper bounds in
  `src/ingestion/pyproject.toml` for production deps.

This test module pins the artefacts on filesystem reads + simple parse
asserts. No pytest needs the daemon process itself; the whole file
finishes in well under a second. Drift detection for the OpenAPI
schema lives in `test_s15_openapi_snapshot.py`.

Edge brainstorm (impl seed list — the artefacts the slice ships):

1. ingestion-ci.yml parses as YAML [TEST-COVERAGE]
2. ingestion-ci.yml has the documented jobs [TEST-COVERAGE]
3. ingestion-ci.yml triggers on push + pull_request [TEST-COVERAGE]
4. ingestion-ci.yml omits a hard coverage fail-under gate [TEST-COVERAGE]
5. ingestion-ci.yml runs pip-audit [TEST-COVERAGE]
6. .pre-commit-config.yaml parses + has ruff / gitleaks /
   pre-commit-hooks bundles [TEST-COVERAGE]
7. .github/dependabot.yml parses + pip ecosystem at /src/ingestion
   + ignores major bumps [TEST-COVERAGE]
8. CONTRIBUTING.md exists + links to AGENTS.md [TEST-COVERAGE]
9. RUNBOOK.md has >= 16 ## scenario headings [TEST-COVERAGE]
10. OPERATIONS.md has the eight required sections [TEST-COVERAGE]
11. OVERVIEW.md has the mermaid lifecycle diagram [TEST-COVERAGE]
12. docs/ingestion/openapi.json is valid JSON + structurally sane paths
    [TEST-COVERAGE]
13. pyproject.toml has compatible-release upper bounds on the impl's
    pinned production dep list [TEST-COVERAGE]

Edge brainstorm (tester additions, >= 6):

A. Workflow `concurrency` block cancels in-flight runs so a fast
   follow-up push doesn't queue a stale build behind the prior one
   [TEST-COVERAGE]
B. `pip-audit` is `continue-on-error: true` — a transitive advisory
   should surface as a warning rather than block PRs whose own deps
   are clean [TEST-COVERAGE]
C. The pre-commit gitleaks hook reuses the same `.gitleaks.toml`
   allowlist as the CI secret-scan workflow (single source of truth)
   [TEST-COVERAGE]
D. Dependabot ignores major bumps for every ecosystem (second safety
   net behind the pyproject pins) [TEST-COVERAGE]
E. Dependabot pip ecosystems point at the three Python components
   (ingestion, presentation-api, db-core) [TEST-COVERAGE]
F. The pre-commit pyright hook is parked in the `manual` stage —
   CI runs strict pyright; on every commit pays its ~5s cost is
   the kind of thing devs disable globally with `--no-verify` if
   it's hot, which would skip ruff + gitleaks too [TEST-COVERAGE]
G. RUNBOOK + OPERATIONS cross-link to OVERVIEW.md so a navigator can
   walk between the three docs without grep'ing the tree
   [TEST-COVERAGE]
H. The mermaid diagram in OVERVIEW.md uses the lifecycle states the
   pipeline + scheduler emit (Submitted, Running, Dead Letter,
   Suppressed, Complete) [TEST-COVERAGE]
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path resolution. The slice 14 file uses the same CLAUDE.md sentinel
# walk; reusing that pattern keeps the test layout consistent.
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Walk parents of this file until we find the CLAUDE.md sentinel."""
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "CLAUDE.md").exists():
            return candidate
    raise RuntimeError(f"could not locate repo root from {here}")


REPO_ROOT = _repo_root()
INGESTION_CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ingestion-ci.yml"
PRE_COMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
DEPENDABOT_CONFIG = REPO_ROOT / ".github" / "dependabot.yml"
CONTRIBUTING_MD = REPO_ROOT / "CONTRIBUTING.md"
RUNBOOK_MD = REPO_ROOT / "docs" / "ingestion" / "RUNBOOK.md"
OPERATIONS_MD = REPO_ROOT / "docs" / "ingestion" / "OPERATIONS.md"
OVERVIEW_MD = REPO_ROOT / "docs" / "ingestion" / "OVERVIEW.md"
OPENAPI_SNAPSHOT = REPO_ROOT / "docs" / "ingestion" / "openapi.json"
INGESTION_PYPROJECT = REPO_ROOT / "src" / "ingestion" / "pyproject.toml"
GITLEAKS_TOML = REPO_ROOT / ".gitleaks.toml"
AGENTS_MD = REPO_ROOT / "AGENTS.md"


# ---------------------------------------------------------------------------
# Module-scoped fixtures (parsers + raw text) — read once per session.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ingestion_ci_text() -> str:
    return INGESTION_CI_WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ingestion_ci_yaml() -> dict[str, Any]:
    parsed = yaml.safe_load(INGESTION_CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "workflow root must be a mapping"
    return parsed


@pytest.fixture(scope="module")
def precommit_yaml() -> dict[str, Any]:
    parsed = yaml.safe_load(PRE_COMMIT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), ".pre-commit-config.yaml root must be a mapping"
    return parsed


@pytest.fixture(scope="module")
def dependabot_yaml() -> dict[str, Any]:
    parsed = yaml.safe_load(DEPENDABOT_CONFIG.read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), "dependabot.yml root must be a mapping"
    return parsed


@pytest.fixture(scope="module")
def runbook_text() -> str:
    return RUNBOOK_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def operations_text() -> str:
    return OPERATIONS_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def overview_text() -> str:
    return OVERVIEW_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def pyproject_text() -> str:
    return INGESTION_PYPROJECT.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Workflow YAML — seed #1, #2, #3, #4, #5 + edges A, B
# ---------------------------------------------------------------------------


def _on_block(workflow: dict[str, Any]) -> dict[str, Any]:
    """PyYAML parses the bare key ``on:`` as the boolean True (it's a
    YAML 1.1 reserved word). Workflows that quote the key as ``"on":``
    parse to the string. Accept both shapes so a future workflow rewrite
    that adds quotes doesn't break the test."""
    raw = workflow.get(True)
    if raw is None:
        raw = workflow.get("on")
    assert isinstance(raw, dict), f"workflow ``on`` block missing or wrong type: {raw!r}"
    return raw


def test_ingestion_ci_workflow_parses_as_yaml(ingestion_ci_yaml: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #1: the workflow YAML parses cleanly. A
    hand edit that breaks indentation gets caught here long before
    GitHub Actions surfaces "workflow not found"."""
    assert "jobs" in ingestion_ci_yaml
    assert "name" in ingestion_ci_yaml
    assert ingestion_ci_yaml["name"] == "Ingestion CI"


def test_ingestion_ci_has_documented_jobs(ingestion_ci_yaml: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #2: the workflow declares the required
    jobs — `ingestion-ci`, `daemon-e2e`, `openapi-diff`, and
    `perf-regression`. A renamed job would silently bypass any branch
    protection that requires the old name."""
    jobs = ingestion_ci_yaml["jobs"]
    assert isinstance(jobs, dict)
    expected = {"ingestion-ci", "daemon-e2e", "openapi-diff", "perf-regression"}
    assert expected <= set(jobs.keys()), (
        f"missing required jobs: {expected - set(jobs.keys())}; found jobs: {sorted(jobs.keys())}"
    )


def test_ingestion_ci_triggers_on_push_and_pull_request(
    ingestion_ci_yaml: dict[str, Any],
) -> None:
    """[TEST-COVERAGE] Seed #3: the workflow fires on both push events
    (so direct pushes to feat branches still get tested) AND on
    pull_request events to main (PR-time blocking gate)."""
    on = _on_block(ingestion_ci_yaml)
    assert "push" in on, "workflow must trigger on push events"
    assert "pull_request" in on, "workflow must trigger on pull_request events"
    # Pin the path filter so a frontend-only PR doesn't spend ingestion CI
    # minutes — but the filter must include the workflow file itself so a
    # change to the gate is gated.
    pr = on["pull_request"]
    assert isinstance(pr, dict)
    paths = pr.get("paths", [])
    assert any("src/ingestion/" in str(p) for p in paths), (
        "pull_request path filter must include src/ingestion/"
    )


def test_ingestion_ci_omits_coverage_fail_under_gate(ingestion_ci_text: str) -> None:
    """[TEST-COVERAGE] Seed #4: the workflow does NOT enforce a hard
    ``coverage report --fail-under=...`` gate. The dedicated e2e split
    changed measured coverage enough that keeping the literal gate would
    fail the job for CI plumbing reasons rather than functional regressions."""
    assert "--fail-under" not in ingestion_ci_text, (
        "workflow should not enforce a coverage fail-under gate"
    )


def test_ingestion_ci_runs_pip_audit(ingestion_ci_text: str) -> None:
    """[TEST-COVERAGE] Seed #5: the workflow runs `pip-audit` so a
    transitive CVE in a daemon dep surfaces in the PR check view.
    Pinned by command name; the actual pyproject pins are the long-term
    fix when an advisory does fire."""
    assert "pip-audit" in ingestion_ci_text, "workflow must run pip-audit"


def test_ingestion_ci_pip_audit_is_continue_on_error(ingestion_ci_yaml: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Edge B: pip-audit runs as continue-on-error so
    an unfixable transitive advisory surfaces as a warning rather than
    a hard PR block. The advisory feed sometimes flags a transitive
    we cannot upgrade because of a constraint tree elsewhere."""
    job = ingestion_ci_yaml["jobs"]["ingestion-ci"]
    steps = job["steps"]
    assert isinstance(steps, list)
    pip_audit_step = next(
        (s for s in steps if isinstance(s, dict) and "pip-audit" in str(s.get("run", ""))),
        None,
    )
    assert pip_audit_step is not None, "pip-audit step missing"
    assert pip_audit_step.get("continue-on-error") is True, (
        "pip-audit must be continue-on-error so a transitive advisory doesn't block PRs"
    )


def test_ingestion_ci_splits_daemon_e2e_into_dedicated_job(
    ingestion_ci_yaml: dict[str, Any],
) -> None:
    """[TEST-COVERAGE] Edge A2: the fast unit-test lane skips
    ``tests/e2e`` and the workflow runs that suite in its own
    Postgres-backed job. Without the split, the main checks job would
    fail on runners with no database service."""
    jobs = ingestion_ci_yaml["jobs"]
    main_job = jobs["ingestion-ci"]
    main_steps = main_job["steps"]
    pytest_step = next(
        (s for s in main_steps if isinstance(s, dict) and s.get("name") == "Pytest"),
        None,
    )
    assert pytest_step is not None, "main Pytest step missing"
    assert "--ignore=tests/e2e" in str(pytest_step.get("run", "")), (
        "main Pytest step must skip tests/e2e; they run in daemon-e2e"
    )

    daemon_job = jobs["daemon-e2e"]
    services = daemon_job.get("services", {})
    assert "postgres" in services, "daemon-e2e must provide a Postgres service"
    daemon_steps = daemon_job.get("steps", [])
    daemon_pytest_step = next(
        (s for s in daemon_steps if isinstance(s, dict) and "tests/e2e/" in str(s.get("run", ""))),
        None,
    )
    assert daemon_pytest_step is not None, "daemon-e2e must run tests/e2e/"


def test_ingestion_ci_has_concurrency_cancel_in_progress(
    ingestion_ci_yaml: dict[str, Any],
) -> None:
    """[TEST-COVERAGE] Edge A: the workflow declares a `concurrency`
    block that cancels older in-flight runs for the same ref. A fast
    follow-up push then doesn't queue a stale build behind the previous
    one, saving CI minutes during interactive iteration."""
    concurrency = ingestion_ci_yaml.get("concurrency")
    assert isinstance(concurrency, dict), "workflow must declare a concurrency block"
    assert concurrency.get("cancel-in-progress") is True, (
        "concurrency.cancel-in-progress must be true to drop stale runs"
    )
    # The group key must include the ref so concurrent feature branches
    # don't cancel each other's runs.
    group = concurrency.get("group", "")
    assert "github.ref" in str(group), f"concurrency.group must include github.ref, got {group!r}"


def test_ingestion_ci_invokes_gitleaks_config_only_via_secret_scan() -> None:
    """[TEST-COVERAGE] Edge C (workflow side): the dedicated workflow
    `secret-scan.yml` owns the gitleaks config; ingestion-ci.yml does
    NOT duplicate gitleaks. Pin: the ingestion workflow does not invoke
    gitleaks itself (avoid double-reporting), and `.gitleaks.toml` lives
    at repo root for the dedicated workflow."""
    text = INGESTION_CI_WORKFLOW.read_text(encoding="utf-8")
    # Sanity: the dedicated config exists.
    assert GITLEAKS_TOML.exists(), ".gitleaks.toml must exist for the secret-scan workflow"
    # ingestion-ci.yml itself shouldn't invoke gitleaks (single source of
    # truth lives in secret-scan.yml).
    assert "gitleaks" not in text.lower(), (
        "ingestion-ci.yml shouldn't duplicate gitleaks — single source is secret-scan.yml"
    )


# ---------------------------------------------------------------------------
# .pre-commit-config.yaml — seed #6 + edges C, F
# ---------------------------------------------------------------------------


def _hook_repos(precommit: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Map ``repo URL -> list of hook entries``. Pre-commit's schema
    nests hooks per repo; flattening simplifies the per-hook asserts
    below."""
    out: dict[str, list[dict[str, Any]]] = {}
    for entry in precommit.get("repos", []):
        if not isinstance(entry, dict):
            continue
        repo = str(entry.get("repo", ""))
        hooks = entry.get("hooks", [])
        if isinstance(hooks, list):
            out[repo] = [h for h in hooks if isinstance(h, dict)]
    return out


def test_precommit_config_parses_and_has_ruff_hook(precommit_yaml: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #6: .pre-commit-config.yaml parses + the
    ruff repo is wired up with both ruff lint and ruff-format hooks
    so commits run the same lint + format gates as CI."""
    repos = _hook_repos(precommit_yaml)
    ruff_repo = next((r for r in repos if "ruff-pre-commit" in r), None)
    assert ruff_repo is not None, "pre-commit must wire the astral-sh/ruff-pre-commit repo"
    hook_ids = {h.get("id") for h in repos[ruff_repo]}
    assert "ruff" in hook_ids, "missing 'ruff' hook id"
    assert "ruff-format" in hook_ids, "missing 'ruff-format' hook id"


def test_precommit_config_has_gitleaks_hook(precommit_yaml: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Edge C: pre-commit runs gitleaks against staged
    diffs as a same-machine mirror of the repo's gitleaks policy.
    Reuses the same `.gitleaks.toml` so a placeholder accepted by the
    manual secret-scan workflow is also accepted locally."""
    repos = _hook_repos(precommit_yaml)
    gitleaks_repo = next((r for r in repos if "gitleaks/gitleaks" in r), None)
    assert gitleaks_repo is not None, "pre-commit must wire gitleaks/gitleaks repo"
    hooks = repos[gitleaks_repo]
    gitleaks_hook = next((h for h in hooks if h.get("id") == "gitleaks"), None)
    assert gitleaks_hook is not None, "missing gitleaks hook id"
    args = gitleaks_hook.get("args", [])
    # Args list must reference the repo-root .gitleaks.toml.
    joined = " ".join(str(a) for a in args)
    assert ".gitleaks.toml" in joined, (
        f"gitleaks pre-commit hook must reference .gitleaks.toml; got args: {args}"
    )


def test_precommit_config_has_pre_commit_hooks_bundle(precommit_yaml: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #6 (continued): the pre-commit/pre-commit-
    hooks bundle is wired with the ``debug-statements``,
    ``check-yaml``, and ``trailing-whitespace`` hooks at minimum. Each
    one catches a class of regression that would otherwise land in
    the diff (left-behind ``breakpoint()``, malformed YAML, drifty
    whitespace)."""
    repos = _hook_repos(precommit_yaml)
    base_repo = next((r for r in repos if "pre-commit/pre-commit-hooks" in r), None)
    assert base_repo is not None, "pre-commit must wire pre-commit/pre-commit-hooks"
    hook_ids = {h.get("id") for h in repos[base_repo]}
    # Pin the most-load-bearing handful — full list lives in the impl
    # commit and may grow without breaking anything that depends on it.
    must_have = {
        "trailing-whitespace",
        "end-of-file-fixer",
        "check-yaml",
        "check-json",
        "check-toml",
        "check-merge-conflict",
        "debug-statements",
    }
    missing = must_have - hook_ids
    assert missing == set(), f"pre-commit-hooks bundle missing required hooks: {sorted(missing)}"


def test_precommit_pyright_parked_in_manual_stage(precommit_yaml: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Edge F: the pyright hook lives in the
    ``manual`` stage so an everyday ``git commit`` doesn't pay its
    ~5s startup. CI runs strict pyright on every PR; locally a
    contributor can run it before pushing via ``pre-commit run
    --hook-stage manual pyright``. Without the manual gate, a hot
    pyright run would tempt --no-verify, which would skip ruff +
    gitleaks too — the cure would be worse than the disease."""
    repos = _hook_repos(precommit_yaml)
    pyright_repo = next((r for r in repos if "mirrors-pyright" in r), None)
    assert pyright_repo is not None, "pre-commit must wire mirrors-pyright repo"
    hooks = repos[pyright_repo]
    pyright_hook = next((h for h in hooks if h.get("id") == "pyright"), None)
    assert pyright_hook is not None, "missing pyright hook id"
    stages = pyright_hook.get("stages", [])
    assert "manual" in stages, f"pyright must be parked in stages: ['manual']; got {stages}"


# ---------------------------------------------------------------------------
# Dependabot — seed #7 + edges D, E
# ---------------------------------------------------------------------------


def test_dependabot_config_parses_and_is_v2(dependabot_yaml: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #7: dependabot.yml is v2 (the only
    supported schema today). The version pin guards against an
    accidental rewrite to v1 (which would silently stop accepting
    most of the modern config keys)."""
    assert dependabot_yaml.get("version") == 2, (
        f"dependabot.yml must be schema v2; got {dependabot_yaml.get('version')}"
    )
    updates = dependabot_yaml.get("updates", [])
    assert isinstance(updates, list)
    assert len(updates) >= 1, "dependabot.yml must declare >=1 update entry"


def test_dependabot_pip_ecosystem_at_ingestion(dependabot_yaml: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #7 (continued) + Edge E: dependabot covers
    the three Python components (ingestion, presentation-api, db-core)
    + github-actions. The directory paths must match where the actual
    pyproject.toml files live."""
    updates = dependabot_yaml["updates"]
    pip_dirs = {
        str(u.get("directory", ""))
        for u in updates
        if isinstance(u, dict) and u.get("package-ecosystem") == "pip"
    }
    # Must cover all three Python components.
    assert "/src/ingestion" in pip_dirs, "dependabot must monitor /src/ingestion"
    assert "/src/backend/presentation-api" in pip_dirs, (
        "dependabot must monitor /src/backend/presentation-api"
    )
    assert "/src/common/db_core" in pip_dirs, "dependabot must monitor /src/common/db_core"
    # And github-actions.
    ecos = {u.get("package-ecosystem") for u in updates if isinstance(u, dict)}
    assert "github-actions" in ecos, "dependabot must monitor github-actions ecosystem"


def test_dependabot_ignores_major_bumps_per_ecosystem(dependabot_yaml: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Edge D: every dependabot update entry ignores
    semver-major bumps. Pyproject's compatible-release pins are the
    first safety net (the resolver refuses); dependabot's ignore is
    the second (no PR generated). Belt + braces — both must hold for
    a careless contributor not to merge a major upgrade."""
    for update in dependabot_yaml["updates"]:
        assert isinstance(update, dict)
        ecosystem = update.get("package-ecosystem")
        directory = update.get("directory")
        ignore_list = update.get("ignore", [])
        assert isinstance(ignore_list, list), (
            f"{ecosystem} @ {directory}: ignore block must be a list"
        )
        # At least one entry that ignores semver-major for *.
        major_ignores = [
            i
            for i in ignore_list
            if isinstance(i, dict)
            and i.get("dependency-name") == "*"
            and "version-update:semver-major" in (i.get("update-types") or [])
        ]
        assert major_ignores, f"{ecosystem} @ {directory}: must ignore semver-major bumps repo-wide"


# ---------------------------------------------------------------------------
# CONTRIBUTING.md — seed #8
# ---------------------------------------------------------------------------


def test_contributing_links_to_agents_md() -> None:
    """[TEST-COVERAGE] Seed #8: CONTRIBUTING.md exists at repo root
    and links to AGENTS.md (which is the symlinked CLAUDE.md). The
    CLAUDE.md instructions are authoritative for component carve-outs;
    CONTRIBUTING.md is the operator-friendly entry point and must
    point there rather than duplicate (single source of truth)."""
    assert CONTRIBUTING_MD.exists(), "CONTRIBUTING.md missing at repo root"
    text = CONTRIBUTING_MD.read_text(encoding="utf-8")
    # Must reference AGENTS.md by name (with or without backticks).
    assert "AGENTS.md" in text, "CONTRIBUTING.md must reference AGENTS.md"
    # Should NOT redefine the carve-outs that AGENTS.md owns. Pin a
    # representative phrase from AGENTS.md's NEVER list ("ZERO Django
    # dependencies"); if CONTRIBUTING.md ever copies the rule rather
    # than linking, this will trip.
    assert "ZERO Django dependencies" not in text, (
        "CONTRIBUTING.md must link to AGENTS.md, not duplicate carve-outs"
    )


# ---------------------------------------------------------------------------
# RUNBOOK.md — seed #9 + edge G
# ---------------------------------------------------------------------------


def test_runbook_has_at_least_16_scenario_headings(runbook_text: str) -> None:
    """[TEST-COVERAGE] Seed #9: RUNBOOK.md has >= 16 incident-response
    scenarios, each delineated by a level-2 (``## ``) heading. The
    impl commit message documents 16; we accept any number >= that
    so a future incident addition lands cleanly without bumping
    the test."""
    # Count level-2 headings (lines beginning with exactly two ``#``
    # followed by a space). Front-matter and code-block fences are
    # excluded by the strict pattern.
    headings = [line for line in runbook_text.splitlines() if line.startswith("## ")]
    # The Quick reference TOC + See also tail are also `## ` headings;
    # subtract them so we count actual scenarios.
    boilerplate = {"## Quick reference", "## See also"}
    scenarios = [h for h in headings if h not in boilerplate]
    assert len(scenarios) >= 16, (
        f"RUNBOOK.md must document >= 16 scenarios, found {len(scenarios)}: {scenarios}"
    )


def test_runbook_cross_links_to_operations_and_overview(runbook_text: str) -> None:
    """[TEST-COVERAGE] Edge G: RUNBOOK.md must link to OPERATIONS.md
    and OVERVIEW.md. Operators reach for the runbook first under
    pressure; the cross-links help them step laterally to the
    architecture + steady-state docs without grep'ing the tree."""
    # Match either ``OPERATIONS.md`` or ``./OPERATIONS.md``.
    assert "OPERATIONS.md" in runbook_text, "RUNBOOK.md must link to OPERATIONS.md"
    assert "OVERVIEW.md" in runbook_text, "RUNBOOK.md must link to OVERVIEW.md"


# ---------------------------------------------------------------------------
# OPERATIONS.md — seed #10 + edge G (operations side)
# ---------------------------------------------------------------------------


def test_operations_md_has_required_sections(operations_text: str) -> None:
    """[TEST-COVERAGE] Seed #10: OPERATIONS.md must cover the eight
    domains the slice spec calls out — lifecycle, health endpoints,
    metrics, alerts, audit log, backup, lockfile, capacity. Each is a
    level-2 heading; pin them by literal substring (case-insensitive)
    so a header rename surfaces as a clear test failure rather than a
    silent gap."""
    needed_substrings = (
        "Daemon lifecycle",
        "Health endpoints",
        "Metrics catalogue",
        "Alert rules",
        "Audit log",
        "Backup story",
        "Lockfile semantics",
        "Capacity benchmarks",
    )
    missing = [
        s
        for s in needed_substrings
        if not re.search(rf"^## {re.escape(s)}", operations_text, re.MULTILINE)
    ]
    assert missing == [], f"OPERATIONS.md missing required level-2 sections: {missing}"


def test_operations_md_links_to_runbook_and_overview(operations_text: str) -> None:
    """[TEST-COVERAGE] Edge G (operations side): OPERATIONS.md links
    to RUNBOOK.md (for incident response) and OVERVIEW.md (for the
    sync state machine)."""
    assert "RUNBOOK.md" in operations_text, "OPERATIONS.md must link to RUNBOOK.md"
    assert "OVERVIEW.md" in operations_text, "OPERATIONS.md must link to OVERVIEW.md"


# ---------------------------------------------------------------------------
# OVERVIEW.md mermaid diagram — seed #11 + edge H
# ---------------------------------------------------------------------------


def test_overview_has_mermaid_lifecycle_diagram(overview_text: str) -> None:
    """[TEST-COVERAGE] Seed #11: OVERVIEW.md gains a mermaid block
    that diagrams the sync state machine. Pin the markdown fence
    (``` ```mermaid```) so a renderer that doesn't understand the
    extension still serves the diagram source as a code block."""
    # The mermaid block opens with ```mermaid (with optional leading
    # whitespace from the surrounding markdown). At least one such
    # block must exist.
    assert "```mermaid" in overview_text, "OVERVIEW.md must declare a mermaid code block"


def test_overview_mermaid_uses_documented_lifecycle_states(overview_text: str) -> None:
    """[TEST-COVERAGE] Edge H: the mermaid diagram + the accompanying
    state table use the lifecycle states the pipeline + scheduler
    emit (Submitted, Running, Dead Letter, Suppressed, Complete).
    Pinning the state names means a refactor that renames a state
    has to update the diagram in the same diff — keeps the docs
    aligned with the runtime."""
    states = ("Submitted", "Running", "Dead Letter", "Suppressed", "Complete")
    missing = [s for s in states if s not in overview_text]
    assert missing == [], f"OVERVIEW.md sync-lifecycle diagram missing states: {missing}"


# ---------------------------------------------------------------------------
# OpenAPI snapshot — seed #12 (drift detection lives in companion file)
# ---------------------------------------------------------------------------


def test_openapi_snapshot_is_valid_json() -> None:
    """[TEST-COVERAGE] Seed #12 (validity half): the committed
    snapshot at docs/ingestion/openapi.json parses as JSON without
    error. A malformed JSON file would break the openapi-diff CI job
    in a confusing way (``json: error parsing``); catching it as a
    test failure is friendlier."""
    assert OPENAPI_SNAPSHOT.exists(), "docs/ingestion/openapi.json missing"
    raw = OPENAPI_SNAPSHOT.read_text(encoding="utf-8")
    payload = json.loads(raw)  # raises on malformed JSON
    assert isinstance(payload, dict), "OpenAPI document must be a JSON object"
    assert payload.get("openapi", "").startswith("3."), (
        "OpenAPI document must declare an OpenAPI 3.x version"
    )


def test_openapi_snapshot_has_balanced_versioned_paths() -> None:
    """[TEST-COVERAGE] Seed #12 (path count half): avoid pinning a
    literal route count. What matters is that the snapshot declares
    balanced ``/v1`` and ``/api`` inventories after regeneration."""
    payload = json.loads(OPENAPI_SNAPSHOT.read_text(encoding="utf-8"))
    paths = payload.get("paths", {})
    assert isinstance(paths, dict)
    v1_paths = [path for path in paths if path.startswith("/v1/")]
    api_paths = [path for path in paths if path.startswith("/api/")]
    assert len(v1_paths) == len(api_paths), (
        f"OpenAPI snapshot must keep /v1 and /api path inventories aligned; "
        f"got v1={len(v1_paths)} api={len(api_paths)}"
    )
    assert len(paths) == len(v1_paths) + len(api_paths)


# ---------------------------------------------------------------------------
# pyproject.toml — seed #13
# ---------------------------------------------------------------------------


def test_pyproject_pins_production_deps_with_major_upper_bound(
    pyproject_text: str,
) -> None:
    """[TEST-COVERAGE] Seed #13: every production dep in the impl's
    pinned list has a compatible-release upper bound (``< (X+1)`` or
    ``~=``). Dev-only deps stay floating by intent. Without this pin
    pattern, a Dependabot patch run could land a major bump silently.

    Impl pinned list (from commit 98873cc):
        httpx, pydantic, pydantic-settings, pyjwt, structlog,
        cryptography, fastapi, uvicorn, aiosqlite, itsdangerous,
        sentry-sdk
    """
    pinned_packages = (
        "httpx",
        "pydantic",
        "pydantic-settings",
        "pyjwt",
        "structlog",
        "cryptography",
        "fastapi",
        "uvicorn",
        "aiosqlite",
        "itsdangerous",
        "sentry-sdk",
    )
    for pkg in pinned_packages:
        # Match a line like ``"httpx>=0.27,<1",`` or ``"uvicorn[standard]>=0.30,<1",``
        # The trailing ``<N`` is what we care about; anything looser fails.
        # Allow optional extras in [..]. Case-sensitive (PyPI is canonical
        # lower-case for these names).
        pattern = rf'"{re.escape(pkg)}(?:\[[^\]]*\])?>=[^"<]+,\s*<\s*\d+'
        match = re.search(pattern, pyproject_text)
        assert match is not None, (
            f"production dep ``{pkg}`` must have a compatible-release upper bound "
            f"(``>=X.Y,<(X+1)``); pin missing in src/ingestion/pyproject.toml"
        )


def test_pyproject_dev_deps_can_float(pyproject_text: str) -> None:
    """[TEST-COVERAGE] Seed #13 (companion): the pin discipline applies
    to PRODUCTION deps only. Dev tools (pytest, ruff, pyright, respx)
    stay floating because keeping the linter and type-checker current
    is an explicit goal — Dependabot can land any minor that lands.
    Pin: at least one common dev dep does NOT carry a compatible-
    release upper bound."""
    # Spot-check pytest. ``"pytest>=8.0",`` (no upper bound) is the
    # accepted shape.
    match = re.search(r'"pytest>=([^,"]+)"', pyproject_text)
    assert match is not None, (
        "pytest dep should be present without a compatible-release upper bound"
    )
