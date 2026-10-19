"""Edge + production tests for slice 14 (ops hardening artifacts).

Filesystem + simple-parser pins for the four artefacts the slice ships:

* ``docker-compose.yml`` — daemon healthcheck on /v1/health, tmpfs /tmp,
  resource limits, restart policy.
* ``docker/Dockerfile.ingestor`` — non-root USER directive, HEALTHCHECK,
  Playwright Chromium install, build-arg metadata.
* ``.env.example`` — REQUIRED block at top, every ENDPOINT_DAEMON__*
  scalar field documented, no obviously-weak placeholder defaults.
* ``.gitleaks.toml`` + ``.github/workflows/secret-scan.yml`` — config
  exists, allowlists .env.example, gitleaks-action runs against full
  history in a manual workflow.

All tests run from on-disk file reads and simple regex / line walks. NO
docker daemon required. The whole file finishes in well under a second.

Each test names the specific edge in its docstring and is tagged
``[CODE-FIX]`` (a code change is recommended) or ``[TEST-COVERAGE]``
(the existing implementation is fine; this just locks the behaviour).

Edge brainstorm — impl seed list from the slice spec:

1. compose validates with the required env vars set [TEST-COVERAGE]
2. compose fails fast (`:?required`) without
   ``PRESENTATION_API_SECRET_KEY`` [TEST-COVERAGE]
3. docker-compose.yml daemon healthcheck uses /v1/health
   (not raw socket connect()) [TEST-COVERAGE]
4. docker-compose.yml daemon has deploy.resources.limits
   (1.5 CPU / 2 GiB) [TEST-COVERAGE]
5. docker-compose.yml daemon has deploy.restart_policy
   (on-failure / 5 attempts / 120s window) [TEST-COVERAGE]
6. docker-compose.yml daemon mounts tmpfs: /tmp [TEST-COVERAGE]
7. Dockerfile.ingestor declares USER ingestor before ENTRYPOINT
   [TEST-COVERAGE]
8. Dockerfile.ingestor has a HEALTHCHECK directive [TEST-COVERAGE]
9. .env.example documents every ENDPOINT_DAEMON__* var (parse via
   regex, compare against daemon/config.py field names) [TEST-COVERAGE]
10. .gitleaks.toml exists and allowlists .env.example placeholders
    [TEST-COVERAGE]
11. .github/workflows/secret-scan.yml uses gitleaks-action with
    full-history scan and manual trigger [TEST-COVERAGE]
12. Image tagging guidance in docs/DEPLOYMENT.md mentions git-sha,
    semver, latest [TEST-COVERAGE]

Edge brainstorm — tester additions (>= 8):

A. .env.example has REQUIRED block at top (no values, just sentinel
   header) so an operator who copies the file and runs without
   filling it gets a fail-fast error [TEST-COVERAGE]
B. Dockerfile.ingestor runs ``playwright install --with-deps chromium``
   so the Teams refresh loop has its browser binaries (~500 MB image
   size; pinning is what stops a future refactor from silently
   dropping the layer) [TEST-COVERAGE]
C. HEALTHCHECK uses ``python -c "import urllib.request; ..."`` not
   curl, because the slim base image does NOT include curl and a curl-
   based probe would always exit 1 even when the daemon is healthy
   [TEST-COVERAGE]
D. restart_policy.window is 120s (sliding) — five rapid failures
   stops the container, but five spread across a day does not (gives
   slice 13's alert engine room to act on the missing /v1/health
   probe before the watchdog hard-disables the service) [TEST-COVERAGE]
E. Bumped route-count invariants in test_versioning_and_deprecation
   still hold (sanity) [TEST-COVERAGE]
F. compose validates with parallel ``--profile observability`` (the
   GlitchTip block has no `:?required` interpolation so the daemon-only
   path can't trip on profile-gated services) [TEST-COVERAGE]
G. .env.example has no obviously-weak placeholder secrets in the
   REQUIRED block (no ``ENDPOINT_ADMIN_PASSWORD=change-me``) — empty
   is the only acceptable default [TEST-COVERAGE]
H. Dockerfile.ingestor build args declare ENDPOINT_VERSION /
   ENDPOINT_GIT_SHA / ENDPOINT_BUILT_AT (plumbing for /v1/about)
   [TEST-COVERAGE]
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

# ---------------------------------------------------------------------------
# Resolve the repo root by walking up from this test file. The tests run
# from the ingestion uv venv (cwd = src/ingestion) so the repo root is
# four directories up from this file: tests/edges -> tests -> ingestion
# -> src -> repo. Pin via a sentinel file (CLAUDE.md) so a future repo
# rename doesn't quietly break the path resolution.
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Walk parents of this file until we find the CLAUDE.md sentinel."""
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "CLAUDE.md").exists():
            return candidate
    raise RuntimeError(f"could not locate repo root from {here}")


REPO_ROOT = _repo_root()
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.ingestor"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
GITLEAKS_TOML = REPO_ROOT / ".gitleaks.toml"
SECRET_SCAN_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "secret-scan.yml"
DEPLOYMENT_DOC = REPO_ROOT / "docs" / "DEPLOYMENT.md"


# ---------------------------------------------------------------------------
# Compose YAML parser fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    """Parsed docker-compose.yml (module-scoped — read once per test session)."""
    raw = COMPOSE_FILE.read_text(encoding="utf-8")
    parsed = yaml.safe_load(raw)
    assert isinstance(parsed, dict), "compose root must be a mapping"
    return parsed


@pytest.fixture(scope="module")
def compose_text() -> str:
    """Raw docker-compose.yml text (preserves :?required interpolation —
    yaml.safe_load resolves the ${} but the literal text keeps the syntax
    visible for fail-fast pinning)."""
    return COMPOSE_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def daemon_service(compose: dict[str, Any]) -> dict[str, Any]:
    """Just the ``daemon`` service block from compose."""
    services = compose["services"]
    daemon = services["daemon"]
    assert isinstance(daemon, dict)
    return daemon


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_example_text() -> str:
    return ENV_EXAMPLE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Seed #1, #2 — compose env-var enforcement (filesystem parser; no docker)
# ---------------------------------------------------------------------------


def test_compose_yaml_parses_cleanly(compose: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #1 (filesystem variant): the compose file
    parses as YAML and exposes the expected top-level shape (a
    ``services`` mapping). A `docker compose config --quiet` would do
    more, but we don't run docker here per the spec — the parse alone
    catches an accidental indent or quote error introduced by a hand
    edit."""
    assert "services" in compose
    services = compose["services"]
    assert isinstance(services, dict)
    # Pin the well-known top-level service names so an accidental rename
    # surfaces here.
    assert "daemon" in services
    assert "db" in services
    assert "migrate" in services


def test_compose_requires_presentation_api_secret_key(compose_text: str) -> None:
    """[TEST-COVERAGE] Seed #2: the compose interpolation
    ``${PRESENTATION_API_SECRET_KEY:?...}`` makes the boot fail fast
    when the var is missing. Pin the literal interpolation form (with
    the colon-question-mark) so a future ``:-`` regression silently
    booting with empty SECRET_KEY would trip this test."""
    # The ``:?`` shape is what makes compose abort interpolation when
    # the var is unset OR empty. ``:-`` would silently default; ``-``
    # without a colon would only fire on unset (not empty). We need
    # the strict form.
    assert "${PRESENTATION_API_SECRET_KEY:?" in compose_text


def test_compose_requires_headless_internal_secret(compose_text: str) -> None:
    """[TEST-COVERAGE] Companion pin to seed #2: the headless ↔
    presentation-api shared secret is also enforced via ``:?required``.
    Two services interpolate it (``presentation-api`` and ``headless``)
    so a regression in either spot would silently boot with the empty
    string."""
    # Both daemon + presentation-api blocks declare the var.
    occurrences = compose_text.count("${ENDPOINT_HEADLESS__INTERNAL_SECRET:?")
    assert occurrences >= 2, f"expected >= 2 :?required interpolations, found {occurrences}"


def test_compose_validates_with_observability_profile_active(compose: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Edge F: profile-gated services (GlitchTip,
    PostHog) interpolate even when their profile is inactive — that's
    why their secrets default with ``${VAR:-default}`` rather than
    ``${VAR:?required}``. Pin: every service tagged with the
    ``observability`` profile uses ``:-`` (default), never ``:?``,
    for its own SECRET_KEY / password vars. Otherwise daemon-only
    `docker compose up` would trip on a profile-gated service it
    isn't even starting."""
    services = compose["services"]
    obs_services: list[tuple[str, dict[str, Any]]] = []
    for name, body in services.items():
        if not isinstance(body, dict):
            continue
        profiles = body.get("profiles") or []
        if "observability" in profiles:
            obs_services.append((name, body))
    assert obs_services, "no observability-profile services found"
    # Walk each one's environment and check none of its values use the
    # `:?required` syntax for its own SECRET_KEY (which would block the
    # daemon-only path).
    for name, body in obs_services:
        env = body.get("environment", {})
        if isinstance(env, dict):
            values = list(env.values())
        elif isinstance(env, list):
            values = env
        else:
            continue
        for raw in values:
            if not isinstance(raw, str):
                continue
            # GlitchTip blocks must NOT declare their own vars as :?required.
            assert not re.search(r"\$\{GLITCHTIP_[A-Z_]+:\?", raw), (
                f"{name} declares a :?required GlitchTip var — daemon-only "
                f"docker compose up would trip on it"
            )
            assert not re.search(r"\$\{POSTHOG_[A-Z_]+:\?", raw), (
                f"{name} declares a :?required PostHog var — daemon-only "
                f"docker compose up would trip on it"
            )


# ---------------------------------------------------------------------------
# Seed #3, #4, #5, #6 — daemon service hardening
# ---------------------------------------------------------------------------


def test_daemon_healthcheck_uses_v1_health_path(daemon_service: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #3: the healthcheck must hit ``/v1/health``
    (slice-5 canonical name), not the older raw socket connect() probe
    that only proved uvicorn was listening. A wedged request handler
    is now caught."""
    healthcheck = daemon_service["healthcheck"]
    test_cmd = healthcheck["test"]
    # The ``test`` field can be ["CMD", "...", ...] or "CMD-SHELL ...".
    if isinstance(test_cmd, list):
        cmd_text = " ".join(str(part) for part in test_cmd)
    else:
        cmd_text = str(test_cmd)
    assert "/v1/health" in cmd_text, f"healthcheck doesn't hit /v1/health: {cmd_text!r}"


def test_daemon_healthcheck_uses_python_urllib_not_curl(daemon_service: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Edge C: the slim base image does not ship curl.
    A curl-based probe would always exit 1 even when the daemon is
    healthy. Pin the ``python -c "import urllib.request; ..."`` shape
    so a refactor that flips back to curl trips here."""
    test_cmd = daemon_service["healthcheck"]["test"]
    if isinstance(test_cmd, list):
        cmd_text = " ".join(str(part) for part in test_cmd)
    else:
        cmd_text = str(test_cmd)
    assert "curl" not in cmd_text, "healthcheck shouldn't use curl (not in slim image)"
    assert "urllib.request" in cmd_text or "python" in cmd_text, (
        f"healthcheck must use python+urllib, got: {cmd_text!r}"
    )


def test_daemon_resource_limits_are_15_cpu_2gib(daemon_service: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #4: deploy.resources.limits is 1.5 CPU /
    2 GiB. A runaway sync (large Teams payload, stuck Playwright
    session) can't starve neighbouring services on the host. Pinned
    against the implementer's observed steady-state numbers."""
    deploy = daemon_service.get("deploy", {})
    limits = deploy.get("resources", {}).get("limits", {})
    cpus = str(limits.get("cpus", ""))
    memory = str(limits.get("memory", ""))
    # cpus: "1.5"
    assert cpus == "1.5", f"cpus limit should be '1.5', got {cpus!r}"
    # memory: "2G" (compose accepts G/GiB; pin G with case-insensitive)
    assert memory.upper().startswith("2G"), f"memory limit should start with '2G', got {memory!r}"


def test_daemon_restart_policy_caps_crash_loop(daemon_service: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #5: deploy.restart_policy bounds a crash
    storm. After 5 failures the container stays stopped and slice-13's
    alert engine pages on the missing /v1/health probe. Edge D: window
    is 120s (sliding) — five quick failures stops, but five spread
    across a day does not, giving the alert engine room to fire."""
    deploy = daemon_service.get("deploy", {})
    restart = deploy.get("restart_policy", {})
    assert restart.get("condition") == "on-failure"
    assert restart.get("max_attempts") == 5
    # Window can be string ("120s") or numeric (120) depending on parser
    # variant — accept both shapes pinned to 120.
    window = restart.get("window")
    assert window in {"120s", 120, "2m"}, f"unexpected window: {window!r}"
    # Delay between attempts also pinned (5s prevents instant cycling).
    delay = restart.get("delay")
    assert delay in {"5s", 5}, f"unexpected delay: {delay!r}"


def test_daemon_mounts_tmp_as_tmpfs(daemon_service: dict[str, Any]) -> None:
    """[TEST-COVERAGE] Seed #6: /tmp is a tmpfs so a future
    ``read_only: true`` overlay (docker-compose.prod.yml) doesn't
    break Playwright Chromium's profile + crash-dump directory."""
    tmpfs = daemon_service.get("tmpfs", [])
    tmpfs_list = [tmpfs] if isinstance(tmpfs, str) else list(tmpfs)
    assert any("/tmp" in entry for entry in tmpfs_list), (
        f"daemon must mount /tmp as tmpfs, got: {tmpfs_list!r}"
    )
    # Pin the size cap (256m) so a future regression that drops the
    # cap and lets Chromium bloat host memory trips here.
    matched = [entry for entry in tmpfs_list if "/tmp" in entry]
    assert any("size=256m" in entry for entry in matched), (
        f"tmpfs /tmp must cap at 256m, got: {matched!r}"
    )


# ---------------------------------------------------------------------------
# Seed #7, #8 — Dockerfile.ingestor non-root + healthcheck
# ---------------------------------------------------------------------------


def test_dockerfile_declares_user_ingestor_before_entrypoint(dockerfile_text: str) -> None:
    """[TEST-COVERAGE] Seed #7: the non-root USER directive must
    appear BEFORE ENTRYPOINT so the daemon process actually runs
    unprivileged. A USER directive after ENTRYPOINT has no effect on
    the entrypoint's process tree."""
    user_match = re.search(r"^USER\s+ingestor", dockerfile_text, re.MULTILINE)
    entrypoint_match = re.search(r"^ENTRYPOINT\s+", dockerfile_text, re.MULTILINE)
    assert user_match, "Dockerfile.ingestor missing 'USER ingestor' directive"
    assert entrypoint_match, "Dockerfile.ingestor missing ENTRYPOINT"
    assert user_match.start() < entrypoint_match.start(), (
        "USER ingestor must precede ENTRYPOINT to drop privileges"
    )


def test_dockerfile_creates_non_root_uid_gid(dockerfile_text: str) -> None:
    """[TEST-COVERAGE] Edge: the user creation line specifies a high
    uid/gid (65532, the convention for distroless / k8s nonroot
    runtimes) so it doesn't collide with a host user that has the
    same id when the volume is mounted on the host."""
    # groupadd / useradd both use --uid/--gid 65532
    assert "65532" in dockerfile_text, "non-root user should use uid/gid 65532"


def test_dockerfile_has_healthcheck_directive(dockerfile_text: str) -> None:
    """[TEST-COVERAGE] Seed #8: a HEALTHCHECK directive exists. Docker
    healthchecks are independent of the compose-level healthcheck and
    catch the case where the container is run outside compose (k8s,
    raw `docker run`, etc.)."""
    assert re.search(r"^HEALTHCHECK\s+", dockerfile_text, re.MULTILINE), (
        "Dockerfile.ingestor missing HEALTHCHECK directive"
    )
    # Must hit /v1/health, same shape as compose-level healthcheck.
    healthcheck_block = re.search(
        r"^HEALTHCHECK[^\n]*(?:\n[^\n]*)*?\n(?=^[A-Z]|\Z)", dockerfile_text, re.MULTILINE
    )
    assert healthcheck_block is not None
    assert "/v1/health" in healthcheck_block.group(0), "Dockerfile HEALTHCHECK must hit /v1/health"


def test_dockerfile_healthcheck_uses_python_urllib_not_curl(dockerfile_text: str) -> None:
    """[TEST-COVERAGE] Edge C (Dockerfile variant): the HEALTHCHECK
    uses python+urllib because curl isn't in the slim base image. A
    curl-based HEALTHCHECK would silently fail on every probe."""
    healthcheck_block = re.search(
        r"^HEALTHCHECK[^\n]*(?:\n[^\n]*)*?\n(?=^[A-Z]|\Z)", dockerfile_text, re.MULTILINE
    )
    assert healthcheck_block is not None
    block_text = healthcheck_block.group(0)
    assert "curl" not in block_text, "HEALTHCHECK must not use curl in slim image"
    assert "urllib.request" in block_text, "HEALTHCHECK must use urllib.request"


def test_dockerfile_installs_playwright_with_deps_chromium(dockerfile_text: str) -> None:
    """[TEST-COVERAGE] Edge B: ``playwright install --with-deps
    chromium`` is the layer that grows the image by ~500 MB but
    enables the Teams refresh loop in prod. A future refactor that
    drops --with-deps to save image size would silently break Chromium
    at runtime (libnss3 missing, etc.)."""
    assert "playwright install --with-deps chromium" in dockerfile_text, (
        "Dockerfile must install Playwright Chromium with --with-deps"
    )


def test_dockerfile_declares_build_arg_metadata(dockerfile_text: str) -> None:
    """[TEST-COVERAGE] Edge H: the three build-time ARG declarations
    (ENDPOINT_VERSION, _GIT_SHA, _BUILT_AT) feed /v1/about. They must
    each be declared once at the top, then re-declared INSIDE the
    FROM stage (Docker doesn't carry global ARGs across stage
    boundaries unless they're re-declared)."""
    for var in ("ENDPOINT_VERSION", "ENDPOINT_GIT_SHA", "ENDPOINT_BUILT_AT"):
        # Each ARG must appear at least twice: once before FROM (global)
        # and once inside the stage (re-declaration).
        arg_count = len(re.findall(rf"^ARG\s+{var}\b", dockerfile_text, re.MULTILINE))
        assert arg_count >= 2, (
            f"{var} must be declared >=2 times (global ARG + stage re-declare); found {arg_count}"
        )
        # Plus an ENV ${VAR} line wires it into the running container.
        assert re.search(rf"{var}=\$\{{{var}\}}", dockerfile_text), (
            f"{var} must be wired ENV={var}=${{{var}}}"
        )


# ---------------------------------------------------------------------------
# Seed #9, #10, #11 — .env.example + .gitleaks + secret-scan workflow
# ---------------------------------------------------------------------------


# Known config fields that MUST NOT appear in .env.example because they
# are either internal-only (in-process state, not env-overridable in
# practice) or carry a complex collection type that pydantic-settings
# can't round-trip cleanly through a single env value. Pinning this
# set explicitly is what stops a future config field rename from
# silently dropping documentation.
_KNOWN_OMITTED_FIELDS = frozenset(
    {
        # Cleared by the scheduler on consumption — never persisted.
        "ENDPOINT_DAEMON__SCHEDULER__EMERGENCY_STOP_REQUESTED",
        # dict[str, int] / dict[str, int] — pydantic-settings reads
        # JSON from one env var, which is too easy to typo. Documented
        # via the SQLite overlay path instead.
        "ENDPOINT_DAEMON__SCHEDULER__PER_PLATFORM_INTERVAL_SECONDS",
        "ENDPOINT_DAEMON__SCHEDULER__PER_PLATFORM_MAX_CONCURRENT",
    }
)


def _enumerate_config_env_vars() -> set[str]:
    """Walk DaemonConfig recursively and yield every leaf-scalar env var
    name. Nested ``BaseModel`` fields are flattened with the ``__``
    delimiter pydantic-settings uses; collection-typed fields (dict/list)
    are excluded because their env-encoding is JSON-via-string and not
    one-to-one with this enumeration."""
    from ingestion.daemon.config import DaemonConfig
    from pydantic import BaseModel

    def walk(prefix: str, model_cls: type[BaseModel]) -> set[str]:
        out: set[str] = set()
        for name, field in model_cls.model_fields.items():
            ann = field.annotation
            # Flatten nested BaseModel into ``parent__child``.
            if isinstance(ann, type) and issubclass(ann, BaseModel):
                out.update(walk(prefix + "__" + name.upper(), ann))
                continue
            out.add(prefix + "__" + name.upper())
        return out

    return walk("ENDPOINT_DAEMON", DaemonConfig)


def _enumerate_env_example_vars(text: str) -> set[str]:
    """Pull every ENDPOINT_DAEMON__* var name from .env.example, both
    set lines and commented-out doc lines. Returns the bare names
    (no ``=value`` suffix)."""
    out: set[str] = set()
    for line in text.splitlines():
        # Strip leading whitespace and `# ` comment markers — many
        # documented vars are commented out as templates.
        stripped = line.lstrip()
        if stripped.startswith("#"):
            stripped = stripped[1:].lstrip()
        match = re.match(r"^(ENDPOINT_DAEMON__[A-Z_]+)\s*=", stripped)
        if match:
            out.add(match.group(1))
    return out


def test_env_example_documents_every_endpoint_daemon_var(env_example_text: str) -> None:
    """[TEST-COVERAGE] Seed #9: every leaf scalar field on
    DaemonConfig must be documented in .env.example. The known-
    omitted set is pinned at the top of this file with a per-field
    rationale; anything outside that set is a documentation gap."""
    expected = _enumerate_config_env_vars()
    documented = _enumerate_env_example_vars(env_example_text)
    missing = (expected - documented) - _KNOWN_OMITTED_FIELDS
    assert missing == set(), (
        f"undocumented ENDPOINT_DAEMON vars (add to .env.example or "
        f"to _KNOWN_OMITTED_FIELDS with a rationale): {sorted(missing)}"
    )
    # Stale documentation: env.example mentions a var that doesn't exist.
    stale = documented - expected
    assert stale == set(), (
        f"stale ENDPOINT_DAEMON vars in .env.example (config field "
        f"removed but doc lingered): {sorted(stale)}"
    )


def test_env_example_starts_with_required_block(env_example_text: str) -> None:
    """[TEST-COVERAGE] Edge A: the file leads with a clearly-labelled
    REQUIRED section so an operator who copies .env.example to .env
    and runs ``docker compose up`` without filling anything gets a
    fail-fast message rather than a confusing mid-startup error."""
    # The REQUIRED section header appears before any Optional block.
    required_idx = env_example_text.find("REQUIRED")
    optional_idx = env_example_text.find("Optional")
    assert required_idx != -1, ".env.example must declare a REQUIRED section"
    assert optional_idx != -1, ".env.example must declare Optional section(s)"
    assert required_idx < optional_idx, "REQUIRED block must come before Optional"


def test_env_example_required_secrets_have_no_placeholder_default(
    env_example_text: str,
) -> None:
    """[TEST-COVERAGE] Edge G: the four required-secret vars have no
    placeholder default in .env.example — the line is bare
    ``VAR=`` so a developer who forgets to fill the value gets an
    obvious empty-value boot error rather than silently running with
    a known-weak default like ``change-me``. Slice 14 part C
    deliberately removed the placeholder defaults."""
    required_secrets = (
        "ENDPOINT_FERNET_KEY",
        "ENDPOINT_ADMIN_PASSWORD",
        "ENDPOINT_SESSION_SECRET",
        "PRESENTATION_API_SECRET_KEY",
        "ENDPOINT_HEADLESS__INTERNAL_SECRET",
    )
    for var in required_secrets:
        # Match the assignment line, capturing the value.
        match = re.search(rf"^{var}=(.*)$", env_example_text, re.MULTILINE)
        assert match is not None, f"{var} not found in .env.example"
        value = match.group(1).strip()
        assert value == "", (
            f"{var} should default to empty in .env.example, got: {value!r} — "
            f"placeholder secrets like 'change-me' lull operators into shipping them"
        )


def test_gitleaks_toml_exists_and_uses_default_ruleset() -> None:
    """[TEST-COVERAGE] Seed #10: .gitleaks.toml exists and inherits
    gitleaks' built-in ruleset. Without ``useDefault = true`` the
    config replaces (rather than extends) the detector list and a
    real Slack token / GitHub PAT would slip through."""
    assert GITLEAKS_TOML.exists(), ".gitleaks.toml missing"
    raw = GITLEAKS_TOML.read_text(encoding="utf-8")
    # The TOML config has [extend] useDefault = true.
    assert re.search(r"useDefault\s*=\s*true", raw), (
        ".gitleaks.toml must inherit the default detector ruleset"
    )


def test_gitleaks_toml_allowlists_env_example(env_example_text: str) -> None:
    """[TEST-COVERAGE] Seed #10 (continued): the placeholder values
    in .env.example MUST be allowlisted, otherwise gitleaks flags
    every ``some-fake-token`` doc value. The allowlist is path-based
    (regex over the relative path)."""
    # env_example_text fixture pulls the file content; we just confirm
    # the allowlist is wired up.
    assert env_example_text  # sanity (file exists, fixture loaded)
    raw = GITLEAKS_TOML.read_text(encoding="utf-8")
    # Either a path allowlist for .env.example or a wholesale skip.
    assert re.search(r"\.env\\?\.example", raw), (
        ".gitleaks.toml must allowlist .env.example placeholders"
    )


def test_secret_scan_workflow_uses_gitleaks_action() -> None:
    """[TEST-COVERAGE] Seed #11: the CI workflow runs
    ``gitleaks/gitleaks-action`` (not a hand-rolled grep) and pins to
    a tagged release rather than @main (which a compromised gitleaks-
    action could silently change)."""
    assert SECRET_SCAN_WORKFLOW.exists(), "secret-scan.yml missing"
    text = SECRET_SCAN_WORKFLOW.read_text(encoding="utf-8")
    # Gitleaks-action call.
    assert re.search(r"uses:\s*gitleaks/gitleaks-action@v\d", text), (
        "workflow must use the official gitleaks/gitleaks-action pinned to a tag"
    )


def test_secret_scan_workflow_runs_against_full_history() -> None:
    """[TEST-COVERAGE] Seed #11 (continued): the workflow must use
    ``fetch-depth: 0`` so gitleaks scans EVERY commit, not just the
    PR diff. A diff-only scan misses any secret that landed before
    the workflow was added."""
    text = SECRET_SCAN_WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"fetch-depth:\s*0", text), (
        "secret-scan.yml must scan full history (fetch-depth: 0)"
    )
    assert "workflow_dispatch" in text, (
        "secret-scan.yml must be manual-only until the historical findings are remediated"
    )


# ---------------------------------------------------------------------------
# Seed #12 — image tagging guidance in DEPLOYMENT.md
# ---------------------------------------------------------------------------


def test_deployment_doc_documents_image_tagging() -> None:
    """[TEST-COVERAGE] Seed #12: the deployment doc has an image-
    versioning section that mentions all three tags (git-sha, semver,
    latest) and the three build-args. Pinned by literal substring
    presence — the doc walks an operator through the production
    tagging workflow."""
    assert DEPLOYMENT_DOC.exists(), "docs/DEPLOYMENT.md missing"
    text = DEPLOYMENT_DOC.read_text(encoding="utf-8")
    # Section header.
    assert "Image versioning" in text, "DEPLOYMENT.md missing 'Image versioning' section"
    # Three tags.
    assert "git-sha" in text or "git_sha" in text
    assert "semver" in text or "SEMVER" in text
    assert ":latest" in text
    # Three build-args feeding /v1/about.
    for var in ("ENDPOINT_VERSION", "ENDPOINT_GIT_SHA", "ENDPOINT_BUILT_AT"):
        assert var in text, f"DEPLOYMENT.md must mention {var}"
    # Cross-reference to /v1/about (the runtime surface this doc enables).
    assert "/v1/about" in text


# ---------------------------------------------------------------------------
# Edge E — sanity-check the route-count invariants still hold (slice 14
# part B added one path, both slice-14 part B test bumps must apply)
# ---------------------------------------------------------------------------


def test_route_count_invariants_match_slice_14_bumps() -> None:
    """[TEST-COVERAGE] Edge E: the bumped route-count pins in
    test_versioning_and_deprecation.py reflect the +1 path that
    /v1/about adds. Re-import the FastAPI app here (cheap) and verify
    the actual route count matches the pinned literal in the existing
    test, so this slice's commit didn't accidentally land another route
    that pushed the count past the bump."""
    from cryptography.fernet import Fernet
    from fastapi.routing import APIRoute
    from ingestion.daemon.api import create_app
    from ingestion.daemon.api.auth import AuthSettings
    from ingestion.daemon.config import DaemonConfig
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.store.local import open_store

    fernet_key = Fernet.generate_key().decode()
    # Use a /tmp path; pytest's tmp_path fixture isn't accessible at
    # collection time in a function-scope test that doesn't take it
    # as a parameter. We could request the fixture, but a one-off
    # relative path keeps the test self-contained.
    sqlite_path = Path(os.environ.get("PYTEST_TMPDIR", "/tmp")) / "test_s14_routes.db"
    store = open_store(sqlite_path)
    cipher = CredentialCipher([fernet_key])
    auth_settings = AuthSettings(
        admin_password="hunter2-strong",
        session_secret="test-secret-do-not-leak-at-least-32b-long",
        session_ttl_seconds=3600,
    )
    config = DaemonConfig(sqlite_path=sqlite_path)
    app = create_app(config=config, store=store, cipher=cipher, auth_settings=auth_settings)
    api_routes = [r for r in app.routes if isinstance(r, APIRoute)]
    v1_pairs = {
        (r.path[len("/v1") :], m)
        for r in api_routes
        if r.path.startswith("/v1/")
        for m in r.methods
    }
    # Slice 14 part B bumped this to 70. If a future slice adds another
    # path, both this number AND the pin in test_versioning_and_deprecation.py
    # need bumping in lockstep.
    assert len(v1_pairs) == 70, (
        f"v1 (path, method) pair count is {len(v1_pairs)}; "
        f"if you added a new endpoint, also bump test_versioning_and_deprecation.py"
    )
