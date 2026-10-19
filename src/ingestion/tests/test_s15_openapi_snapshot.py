"""Slice 15: OpenAPI snapshot drift detector.

The committed snapshot at ``docs/ingestion/openapi.json`` is the
source of truth the dashboard's ``pnpm codegen`` consumes for its
typed client. The CI ``openapi-diff`` job regenerates the schema via
``scripts/dump_openapi.py`` and ``diff -u``s it against the committed
file; any drift fails the job, forcing the contributor to bump the
snapshot in the same PR (audit trail for API-shape changes).

This test module mirrors that gate locally so a developer who breaks
the schema gets the same failure on their workstation as in CI. The
suite boots a fresh FastAPI app via the same factory the dump script
uses and asserts:

* Live ``app.openapi()`` payload matches the committed JSON byte-for-
  byte (modulo nothing — the impl pinned `json.dumps(indent=2,
  sort_keys=True) + "\\n"` so the output is reproducible).
* ``scripts/dump_openapi.py`` exists and is invocable as a script
  (the CI job calls it directly; if the script disappears, the gate
  silently breaks).
* Live route count matches the snapshot's path count (a subset check
  that catches the common case where a router was renamed but a path
  was kept — picks up the symptom even if the byte-equality check is
  ever loosened to a structural diff).

Tests are tagged with the ``snapshot`` marker so future contributors
know this is the drift detector and have a clear regenerate path:

    cd src/ingestion
    uv run python scripts/dump_openapi.py \\
        --out ../../docs/ingestion/openapi.json

If a router changes intentionally, regenerate, commit, and the test
passes again. If an unintended rename slipped in, the failure points
straight at it.

Edge brainstorm — tester additions for this slice (>= 4):

A. The dump script honours the same indent + sort_keys + trailing
   newline shape the committed snapshot uses [TEST-COVERAGE]
B. Live OpenAPI document declares a 3.x ``openapi`` version (catches
   a future FastAPI bump that flips to 3.2 or 4.0 and changes the
   document shape) [TEST-COVERAGE]
C. Each path in the snapshot resolves to >=1 method (an empty stub
   object is a sign of a dropped router) [TEST-COVERAGE]
D. The dump_openapi.py module text references ``app.openapi()`` —
   pin so a refactor doesn't accidentally call ``app.routes`` instead
   (the two return slightly different schemas) [TEST-COVERAGE]
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet


def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "CLAUDE.md").exists():
            return candidate
    raise RuntimeError(f"could not locate repo root from {here}")


REPO_ROOT = _repo_root()
SNAPSHOT_PATH = REPO_ROOT / "docs" / "ingestion" / "openapi.json"
DUMP_SCRIPT = REPO_ROOT / "src" / "ingestion" / "scripts" / "dump_openapi.py"


# ---------------------------------------------------------------------------
# Live FastAPI app fixture. Boots once per module: the create_app factory
# is cheap (no network bind, sqlite in tmp), and the snapshot doesn't
# depend on any per-test state.
# ---------------------------------------------------------------------------


def _build_live_schema() -> dict[str, object]:
    """Recreate exactly what ``scripts/dump_openapi.py`` does. The
    script's own ``main()`` is a CLI wrapper so we can't import-and-call
    it without mutating sys.argv; reproducing the (small) sequence here
    keeps the test self-contained and keeps the assertion target in one
    place."""
    # Placeholder secrets matching the dump script's env defaults — keep
    # in lockstep so a regenerate via either path produces the same bytes.
    os.environ.setdefault("ENDPOINT_FERNET_KEY", Fernet.generate_key().decode())
    os.environ.setdefault("ENDPOINT_ADMIN_PASSWORD", "codegen-placeholder")
    os.environ.setdefault(
        "ENDPOINT_SESSION_SECRET",
        "codegen-placeholder-session-secret-0123456789",
    )

    from core.bootstrap import init

    init()

    from ingestion.daemon.api import create_app
    from ingestion.daemon.config import DaemonConfig
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.store.local import open_store

    with tempfile.TemporaryDirectory() as tmp:
        config = DaemonConfig(sqlite_path=Path(tmp) / "codegen.sqlite")
        cipher = CredentialCipher.from_env()
        store = open_store(config.sqlite_path)
        try:
            app = create_app(config=config, store=store, cipher=cipher)
            schema = app.openapi()
        finally:
            store.close()
    return schema


@pytest.fixture(scope="module")
def live_schema() -> dict[str, object]:
    return _build_live_schema()


@pytest.fixture(scope="module")
def snapshot_text() -> str:
    return SNAPSHOT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def snapshot_payload(snapshot_text: str) -> dict[str, object]:
    return json.loads(snapshot_text)


# ---------------------------------------------------------------------------
# Drift detector — primary assertion
# ---------------------------------------------------------------------------


@pytest.mark.snapshot
def test_live_openapi_matches_committed_snapshot_byte_for_byte(
    live_schema: dict[str, object],
    snapshot_text: str,
) -> None:
    """[TEST-COVERAGE] Drift detector. The live app's OpenAPI document,
    serialised with the same ``json.dumps(indent=2, sort_keys=True) +
    "\\n"`` shape the dump script writes, must equal the committed
    snapshot byte-for-byte. Any drift means a router changed and the
    snapshot wasn't regenerated.

    To regenerate after an intentional API change:

        cd src/ingestion
        uv run python scripts/dump_openapi.py \\
            --out ../../docs/ingestion/openapi.json

    Then re-run this test. If the change was unintentional, the diff
    output below points at the exact rename / signature drift."""
    live_text = json.dumps(live_schema, indent=2, sort_keys=True) + "\n"
    if live_text == snapshot_text:
        return
    # The byte-equality check failed. Surface a useful diff snippet
    # rather than a wall of text — the full diff lives in CI output.
    import difflib

    diff_lines = list(
        difflib.unified_diff(
            snapshot_text.splitlines(keepends=True),
            live_text.splitlines(keepends=True),
            fromfile="docs/ingestion/openapi.json (committed)",
            tofile="live app.openapi() (regenerated)",
            n=2,
        )
    )
    # Cap the diff at a reasonable number of lines so the assert message
    # stays scannable. CI output will show the full diff via the
    # openapi-diff job; this is just for fast local iteration.
    snippet = "".join(diff_lines[:60])
    pytest.fail(
        "OpenAPI snapshot drift detected. Regenerate via:\n"
        "    cd src/ingestion\n"
        "    uv run python scripts/dump_openapi.py "
        "--out ../../docs/ingestion/openapi.json\n\n"
        f"First {min(60, len(diff_lines))} lines of diff:\n{snippet}"
    )


# ---------------------------------------------------------------------------
# Path count consistency — a structural sanity check
# ---------------------------------------------------------------------------


@pytest.mark.snapshot
def test_live_route_count_matches_snapshot_path_count(
    live_schema: dict[str, object],
    snapshot_payload: dict[str, object],
) -> None:
    """[TEST-COVERAGE] Subset check that catches a router restructure
    that changes the route table without changing the bytes. If the
    byte-equality check is ever loosened (e.g. to a structural diff
    that ignores reordering), this still trips on a path add/remove."""
    live_paths = live_schema.get("paths", {})
    snap_paths = snapshot_payload.get("paths", {})
    assert isinstance(live_paths, dict)
    assert isinstance(snap_paths, dict)
    assert len(live_paths) == len(snap_paths), (
        f"path-count drift: live={len(live_paths)}, snapshot={len(snap_paths)}. "
        f"Regenerate the snapshot if the API changed intentionally."
    )


@pytest.mark.snapshot
def test_each_snapshot_path_has_at_least_one_method(
    snapshot_payload: dict[str, object],
) -> None:
    """[TEST-COVERAGE] Edge C: every path in the snapshot maps to
    >= 1 HTTP method. An empty stub object would be the sign of a
    dropped router that left an orphaned ``"/v1/foo": {}`` behind —
    a real source of drift bugs in larger FastAPI apps."""
    paths = snapshot_payload.get("paths", {})
    assert isinstance(paths, dict)
    http_verbs = {"get", "post", "put", "patch", "delete", "head", "options"}
    orphans: list[str] = []
    for path, body in paths.items():
        if not isinstance(body, dict):
            orphans.append(path)
            continue
        if not (set(body.keys()) & http_verbs):
            orphans.append(path)
    assert orphans == [], f"snapshot has paths with no HTTP methods (dropped router?): {orphans}"


# ---------------------------------------------------------------------------
# Snapshot version pin — Edge B
# ---------------------------------------------------------------------------


@pytest.mark.snapshot
def test_snapshot_declares_openapi_3_x(
    snapshot_payload: dict[str, object],
) -> None:
    """[TEST-COVERAGE] Edge B: pin the OpenAPI document version to a
    3.x release. A future FastAPI bump that flips to OpenAPI 4 would
    change the document shape (different keys, different schema for
    requestBody, etc.) and we want the bump to be a deliberate event
    rather than a silent CI-pass — this test breaks first, the diff
    test breaks second, and the contributor decides whether the bump
    is accepted."""
    version = snapshot_payload.get("openapi", "")
    assert isinstance(version, str)
    assert version.startswith("3."), f"snapshot must declare OpenAPI 3.x; got {version!r}"


# ---------------------------------------------------------------------------
# Helper script — Edge D + the impl spec
# ---------------------------------------------------------------------------


@pytest.mark.snapshot
def test_dump_openapi_script_exists() -> None:
    """[TEST-COVERAGE] The CI ``openapi-diff`` job invokes
    ``scripts/dump_openapi.py`` directly. If the script ever moves or
    is renamed, the CI job silently fails. Pin: the script lives at
    the well-known path and is a real file."""
    assert DUMP_SCRIPT.exists(), f"scripts/dump_openapi.py missing at expected path {DUMP_SCRIPT}"
    assert DUMP_SCRIPT.is_file(), f"{DUMP_SCRIPT} must be a regular file"


@pytest.mark.snapshot
def test_dump_openapi_script_text_uses_app_openapi() -> None:
    """[TEST-COVERAGE] Edge D: the dump script must call
    ``app.openapi()`` (the FastAPI helper that walks the app + schema
    customisations) and not, say, ``app.routes`` (which returns the
    raw Starlette routes without the OpenAPI overlay). A refactor
    that swaps these would produce a different document and silently
    break the openapi-diff CI job."""
    text = DUMP_SCRIPT.read_text(encoding="utf-8")
    assert "app.openapi()" in text, (
        "scripts/dump_openapi.py must call app.openapi() to produce the schema"
    )
    # And it must use the same indent + sort_keys + trailing-newline
    # shape (Edge A) so a regenerate produces byte-stable output.
    assert "indent=2" in text, "dump_openapi.py must serialise with indent=2"
    assert "sort_keys=True" in text, "dump_openapi.py must serialise with sort_keys=True"


@pytest.mark.snapshot
def test_dump_openapi_script_runs_via_subprocess() -> None:
    """[TEST-COVERAGE] The dump script is invocable as ``python
    scripts/dump_openapi.py`` (no module-import-only escape hatch).
    Run it via subprocess to confirm exit 0 and that the stdout is
    valid JSON — the CI openapi-diff job uses ``--out``, but stdout
    mode is the public default and a regression there would surface
    in the dashboard's ``pnpm codegen`` workflow first.

    Slow-ish (~1s for app boot) but this is the drift-detector module
    and the heavyweight test suite already excludes itself from the
    every-commit hot path."""
    env = dict(os.environ)
    # The dump script's env defaults handle missing vars; setting
    # explicit ones here matches what CI provides in practice.
    env.setdefault("ENDPOINT_FERNET_KEY", Fernet.generate_key().decode())
    env.setdefault("ENDPOINT_ADMIN_PASSWORD", "codegen-placeholder")
    env.setdefault(
        "ENDPOINT_SESSION_SECRET",
        "codegen-placeholder-session-secret-0123456789",
    )
    # Run from src/ingestion so ``import core.bootstrap`` in the script
    # resolves against db_core's editable install.
    cwd = REPO_ROOT / "src" / "ingestion"
    result = subprocess.run(
        [sys.executable, str(DUMP_SCRIPT)],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, (
        f"dump_openapi.py exited {result.returncode}; stderr:\n{result.stderr[:500]}"
    )
    # Stdout must parse as JSON.
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    assert payload.get("openapi", "").startswith("3.")
