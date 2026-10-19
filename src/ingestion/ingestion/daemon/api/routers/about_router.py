"""``GET /v1/about`` — daemon build + runtime introspection.

Public (never auth-gated, like ``/v1/health``) so on-call operators can
remotely answer "what version is this pod?" without a session cookie.
The endpoint pairs with the slice-14 image-tagging guidance: every
release ships an image tagged ``endpoint/ingestor:<git-sha>`` and
``endpoint/ingestor:<semver>``, and ``/v1/about`` echoes the same
identifiers so a one-line ``curl`` answers the question.

Build metadata comes from environment variables set at image build
time (``ENDPOINT_VERSION``, ``ENDPOINT_GIT_SHA``, ``ENDPOINT_BUILT_AT``)
populated by the Dockerfile's ``ARG``/``ENV`` chain. When run outside
a CI-tagged build (local ``docker build`` with no args, or a non-Docker
``python -m ingestion.daemon`` invocation) the values fall back to
``"unknown"`` rather than the literal ``"$ENV_VAR"`` string — operators
seeing ``"unknown"`` know the build pipeline missed the tagging step.

The response also surfaces:

* ``python_version`` — useful when triaging cpython-specific bugs.
* ``uptime_seconds`` — reuses slice-6's ``_PROCESS_START_MONOTONIC``
  so the same value is reported here and in ``/metrics``.

Wire size is ~200 bytes. No DB calls — the endpoint must answer fast
on a cold pod and never depend on SQLite reachability (that's what
``/v1/ready`` is for).
"""

from __future__ import annotations

import os
import platform
import time
from typing import Final

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

# Reuse the slice-6 process-start anchor so /v1/about and /metrics
# report identical uptime values. Importing it (rather than capturing
# again) means the field reflects "since the API stack was imported,"
# not "since this router was queried for the first time." The
# underscore-prefixed name is module-private by convention but
# treated as the canonical anchor for "process uptime" across the
# API stack — pyright's reportPrivateUsage rule is intentionally
# suppressed here.
from ingestion.daemon.api.routers.metrics_router import (
    _PROCESS_START_MONOTONIC,  # pyright: ignore[reportPrivateUsage]
)

router = APIRouter(tags=["about"])


# Environment variable names. Hard-coded as a Final tuple so the lookup
# logic in :func:`_lookup_build_meta` and the docstrings stay in sync;
# adding a new build-time meta field means changing both at once.
_ENV_VERSION: Final = "ENDPOINT_VERSION"
_ENV_GIT_SHA: Final = "ENDPOINT_GIT_SHA"
_ENV_BUILT_AT: Final = "ENDPOINT_BUILT_AT"

# Sentinel returned when a build-time variable is missing or empty.
# Keeps the wire shape consistent (always a string) and gives operators
# a clear signal that CI tagging didn't run.
_UNKNOWN: Final = "unknown"


class AboutResponse(BaseModel):
    """Build + runtime metadata payload.

    All fields are required strings/ints so the wire shape is stable —
    a missing build-time env var resolves to ``"unknown"`` rather than
    omitting the key. This keeps client codegen (TypeScript) simple:
    no nullable fields to unwrap.
    """

    model_config = ConfigDict(extra="forbid")

    version: str
    git_sha: str
    built_at: str
    python_version: str
    uptime_seconds: int


def _lookup_build_meta(env_var: str) -> str:
    """Resolve a build-time env var, returning ``"unknown"`` when absent.

    Empty string is treated as "absent" so an accidental ``ARG=""`` in
    a Dockerfile doesn't leak the empty value through. Whitespace-only
    values fold the same way — operators see ``"unknown"`` and know
    the tagging step misfired rather than seeing a meaningless
    placeholder.
    """
    raw = os.environ.get(env_var, "")
    stripped = raw.strip()
    return stripped if stripped else _UNKNOWN


@router.get("/about", response_model=AboutResponse)
async def about() -> AboutResponse:
    """Return version + uptime introspection. No auth, no DB."""
    uptime_seconds = max(0, int(time.monotonic() - _PROCESS_START_MONOTONIC))
    return AboutResponse(
        version=_lookup_build_meta(_ENV_VERSION),
        git_sha=_lookup_build_meta(_ENV_GIT_SHA),
        built_at=_lookup_build_meta(_ENV_BUILT_AT),
        python_version=platform.python_version(),
        uptime_seconds=uptime_seconds,
    )
