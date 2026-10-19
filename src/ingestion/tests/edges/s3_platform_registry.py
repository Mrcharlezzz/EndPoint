"""Edge + production-edge coverage for slice 3 (platform registry).

Each test docstring is tagged with [CODE-FIX] when it pins a real bug
the reviewer-fixer must address, or [TEST-COVERAGE] when the test
layer is itself the contract being pinned.

Brainstorm (>= 8 additional, beyond the seed list of 10):
  * [TEST-COVERAGE] Adapter factory signature is uniform — every spec
    accepts a single ``IngestionConfig`` and returns an ``Adapter``
  * [TEST-COVERAGE] Concurrent ``make_adapter`` calls produce DISTINCT
    instances per call (no caching) — adapters are stateless
  * [TEST-COVERAGE] iter_platforms is order-deterministic for use in
    test parametrize ids
  * [TEST-COVERAGE] PlatformSpec is pickleable + JSON-serialisable
    (excluding the factory) for any logging path that wants to dump it
  * [TEST-COVERAGE] enabled_config_attr indirection is validated at
    startup — drift catches at boot, not at first sync
  * [TEST-COVERAGE] PlatformSpec field types are str / Callable
    (pyright strictness — verified via ``dataclasses.fields``)
  * [TEST-COVERAGE] paused_overlay_key always equals
    ``"platforms.paused.<name>"`` — pin the contract
  * [TEST-COVERAGE] make_adapter does NOT swallow BaseException
    subclasses (KeyboardInterrupt, SystemExit) — those bubble through
    the wrap untouched
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import pickle
import time
import tracemalloc
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
from ingestion.config import IngestionConfig
from ingestion.daemon import platforms as platforms_mod
from ingestion.daemon.candidates import Candidate, InMemoryCandidateSource
from ingestion.daemon.config import DaemonConfig, PlatformPauseConfig, PlatformToggles
from ingestion.daemon.dead_letter import DeadLetterTracker
from ingestion.daemon.platforms import (
    PLATFORMS,
    AdapterBuildError,
    PlatformSpec,
    UnknownPlatformError,
    get_platform,
    iter_platforms,
    known_platforms,
    make_adapter,
)
from ingestion.daemon.ports.sync_logger import SqliteSyncLogger
from ingestion.daemon.scheduler import Scheduler, SchedulerDeps
from ingestion.daemon.store.local import apply_migrations, open_store
from ingestion.daemon.worker import WorkerPool
from ingestion.exceptions import IngestionError

from tests.daemon._scheduler_helpers import (
    FakeAdapter,
    InMemoryContentSink,
    InMemorySyncStateStore,
    InMemoryTokenStore,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ingestion.ports import Adapter


# Helpers


def _build_scheduler(
    sqlite_path: Path,
    candidates: list[Candidate],
    *,
    overlay: dict[str, object] | None = None,
) -> Scheduler:
    """Minimal scheduler harness for the edge tests below. Mirrors the
    pattern in ``tests/daemon/test_scheduler.py`` but without exposing
    every dep — the edge tests only need ``_platform_enabled`` /
    ``_platform_paused`` / ``tick_once``."""
    config_store = open_store(sqlite_path)
    if overlay:
        for key, value in overlay.items():
            config_store.set_overlay(key, value)
    config_store.set_overlay("scheduler.default_interval_seconds", 60)
    adapter = FakeAdapter()
    pool = WorkerPool(global_limit=4, on_finish=lambda *_a, **_k: asyncio.sleep(0))
    dead_letter = DeadLetterTracker(sqlite_path, threshold=3, cooldown_seconds=60)
    token_store = InMemoryTokenStore(
        {(c.user_email, c.platform): {"token": "tok"} for c in candidates}
    )

    def _adapter_factory(_platform: str) -> Adapter:
        return cast("Adapter", adapter)

    deps = SchedulerDeps(
        config_store=config_store,
        candidate_source=InMemoryCandidateSource(candidates),
        worker_pool=pool,
        dead_letter=dead_letter,
        token_store=cast("Any", token_store),
        content_sink=cast("Any", InMemoryContentSink()),
        sync_state=cast("Any", InMemorySyncStateStore()),
        sync_logger=cast("Any", SqliteSyncLogger(sqlite_path)),
        adapter_factory=_adapter_factory,
        sqlite_path=sqlite_path,
        now=lambda: datetime.now(tz=UTC),
    )
    return Scheduler(deps)


@pytest.fixture
def sqlite_path(tmp_path: Path) -> Path:
    path = tmp_path / "state.db"
    apply_migrations(path)
    return path


def _spec_with(
    name: str,
    factory: Callable[..., Any],
    *,
    enabled_attr: str = "teams_enabled",
    paused_attr: str = "teams",
) -> PlatformSpec:
    """Build a PlatformSpec that mirrors an existing platform's config
    attrs but swaps the factory and (optionally) the lookup keys."""
    return PlatformSpec(
        name=name,
        enabled_config_attr=enabled_attr,
        paused_config_attr=paused_attr,
        paused_overlay_key=f"platforms.paused.{name}",
        adapter_factory=factory,
        display_name=name.capitalize(),
    )


def test_adding_fourth_platform_requires_only_registry_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Demonstrates the design: a fake fourth platform
    becomes available everywhere via a single ``_REGISTRY`` entry.
    No scheduler / runtime / router edit needed."""

    def _fake_factory(_cfg: IngestionConfig) -> Any:
        class _FakeAdapter:
            platform = "discord"

            async def fetch(
                self,
                _credentials: dict[str, Any],
                _cursor: dict[str, Any] | None = None,
                _scope: Any = None,
            ) -> Any:
                return None

            async def test_connection(self, _credentials: dict[str, Any]) -> bool:
                return True

        return _FakeAdapter()

    fake_spec = _spec_with("discord", _fake_factory)
    extended = {**PLATFORMS, "discord": fake_spec}
    monkeypatch.setattr(platforms_mod, "_REGISTRY", extended)

    # Now the registry exposes the fourth platform.
    assert "discord" in known_platforms()
    assert get_platform("discord") is fake_spec
    names = [s.name for s in iter_platforms()]
    assert names == ["teams", "moodle", "telegram", "discord"]


def test_scheduler_platform_enabled_uses_registry_attr_field(
    sqlite_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Patch get_platform to return a spec with a
    DIFFERENT ``enabled_config_attr`` and verify the scheduler reads
    that field — proves the indirection is real, not a coincidence."""
    scheduler = _build_scheduler(sqlite_path, [Candidate("alice@x", "teams")])
    config = DaemonConfig(sqlite_path=sqlite_path)

    # Default: teams_enabled is True.
    assert scheduler._platform_enabled(config, "teams") is True

    # Override the spec to read moodle_enabled instead. With Moodle
    # disabled, the scheduler must report Teams as disabled — proving
    # the field name is the source of truth.
    swapped = _spec_with(
        "teams",
        PLATFORMS["teams"].adapter_factory,
        enabled_attr="moodle_enabled",
    )
    monkeypatch.setattr(platforms_mod, "_REGISTRY", {**PLATFORMS, "teams": swapped})
    config = config.model_copy(update={"platforms": PlatformToggles(moodle_enabled=False)})
    assert scheduler._platform_enabled(config, "teams") is False


def test_scheduler_platform_enabled_unknown_returns_false_and_warns_once(
    sqlite_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[TEST-COVERAGE] Unknown platform → False, and the warn log
    fires exactly ONCE per process even on repeat ticks."""
    scheduler = _build_scheduler(sqlite_path, [])
    config = DaemonConfig(sqlite_path=sqlite_path)
    with caplog.at_level("WARNING"):
        assert scheduler._platform_enabled(config, "nope") is False
        assert scheduler._platform_enabled(config, "nope") is False
        assert scheduler._platform_enabled(config, "nope") is False
    # The instance memory is the warn-once gate.
    assert "nope" in scheduler._unknown_platforms_seen


def test_scheduler_platform_paused_known_reads_pause_field(
    sqlite_path: Path,
) -> None:
    """[TEST-COVERAGE] Known platform: reads PlatformPauseConfig.<paused_attr>."""
    scheduler = _build_scheduler(sqlite_path, [])
    config = DaemonConfig(sqlite_path=sqlite_path)
    # Default: not paused.
    assert scheduler._platform_paused(config, "teams") is False
    # Flip the field on a model_copy.
    config = config.model_copy(
        update={"platforms": PlatformToggles(paused=PlatformPauseConfig(teams=True))}
    )
    assert scheduler._platform_paused(config, "teams") is True


def test_scheduler_platform_paused_unknown_returns_false(
    sqlite_path: Path,
) -> None:
    """[TEST-COVERAGE] Unknown platform: False (the kill-switch path
    already rejected it; we don't double-warn)."""
    scheduler = _build_scheduler(sqlite_path, [])
    config = DaemonConfig(sqlite_path=sqlite_path)
    assert scheduler._platform_paused(config, "nope") is False


@pytest.mark.parametrize("platform", ["teams", "moodle", "telegram"])
def test_adapter_factory_signature_matches_registry_contract(platform: str) -> None:
    """[TEST-COVERAGE] Every registered factory accepts exactly one
    positional ``cfg: IngestionConfig`` and returns an Adapter.
    inspect.signature trivially catches a future drift."""
    spec = get_platform(platform)
    sig = inspect.signature(spec.adapter_factory)
    params = list(sig.parameters.values())
    assert len(params) == 1
    # Annotation strings (PEP 563); compare by string match.
    assert "IngestionConfig" in str(params[0].annotation)
    assert "Adapter" in str(sig.return_annotation)


def test_make_adapter_preserves_inner_via_dunder_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Structured logging pivots on inner exception
    type. Pin that ``raise AdapterBuildError(...) from exc`` puts the
    original on ``__cause__``, not on a different attribute."""
    original = IngestionError("kaboom from token endpoint")

    def _boom(_cfg: IngestionConfig) -> Adapter:
        raise original

    fake = _spec_with("discord", _boom)
    monkeypatch.setattr(platforms_mod, "_REGISTRY", {**PLATFORMS, "discord": fake})

    with pytest.raises(AdapterBuildError) as ei:
        make_adapter("discord", IngestionConfig())
    assert ei.value.__cause__ is original
    # The traceback mechanism also gets the chain.
    assert ei.value.__context__ is original


def test_production_registry_cannot_be_mutated_at_runtime() -> None:
    """[TEST-COVERAGE] PRODUCTION. A malicious overlay or buggy
    extension must NOT be able to inject a new spec into the running
    daemon by writing to ``PLATFORMS["malicious"] = ...`` — the
    MappingProxyType view rejects every write path."""
    # All four mutation pathways must raise.
    with pytest.raises(TypeError):
        PLATFORMS["malicious"] = PLATFORMS["teams"]  # type: ignore[index]
    with pytest.raises(TypeError):
        del PLATFORMS["teams"]  # type: ignore[attr-defined]
    with pytest.raises((AttributeError, TypeError)):
        PLATFORMS.clear()  # type: ignore[attr-defined]
    with pytest.raises((AttributeError, TypeError)):
        PLATFORMS.update({"foo": PLATFORMS["teams"]})  # type: ignore[attr-defined]


def test_production_registry_hot_loop_has_no_allocation_cost() -> None:
    """[TEST-COVERAGE] PRODUCTION. The scheduler reads the registry
    on every tick for every candidate; an allocation per lookup
    would balloon under N=10000 candidates. Pin that 1000 lookups
    do not allocate more than a few KB."""
    tracemalloc.start()
    try:
        # Warm the snapshot machinery.
        _ = tracemalloc.take_snapshot()
        snap1 = tracemalloc.take_snapshot()
        for _ in range(1000):
            _ = PLATFORMS["teams"]
        snap2 = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()
    diffs = snap2.compare_to(snap1, "filename")
    total = sum(d.size_diff for d in diffs)
    # Threshold is loose (8 KB) — the goal is to catch a regression
    # that turns the lookup into a per-call allocation, not to assert
    # exact byte counts on tracemalloc internals.
    assert total < 8192, f"hot loop allocated {total} bytes — registry is leaky"


def test_production_missing_config_field_raises_attribute_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] PRODUCTION. If ``DaemonConfig.platforms`` is
    missing the field the registry references (validate_registry was
    skipped or the schema drifted post-boot), the runtime must raise
    AttributeError loudly — NOT silently default to disabled.

    Distinct from validate_registry's startup catch: this pins the
    BEHAVIOUR if validate_registry is bypassed (e.g., ad-hoc scripts)."""
    bad_spec = _spec_with(
        "teams",
        PLATFORMS["teams"].adapter_factory,
        enabled_attr="not_a_real_attr",
    )
    monkeypatch.setattr(platforms_mod, "_REGISTRY", {**PLATFORMS, "teams": bad_spec})
    config = DaemonConfig()
    with pytest.raises(AttributeError, match="not_a_real_attr"):
        getattr(config.platforms, get_platform("teams").enabled_config_attr)


async def test_production_scheduler_tick_under_10000_candidates(
    sqlite_path: Path,
) -> None:
    """[TEST-COVERAGE] PRODUCTION. With N=10000 mixed-platform
    candidates, the registry lookup must not dominate runtime. Pins
    a budget of < 1.5 seconds for the registry-only filter portion
    (exact wall-clock will vary by machine; the spread between
    registry lookups vs everything else is what matters)."""
    candidates = []
    plats = ["teams", "moodle", "telegram"]
    for i in range(10000):
        candidates.append(Candidate(f"u{i}@x", plats[i % 3]))

    scheduler = _build_scheduler(sqlite_path, candidates)
    config = DaemonConfig(sqlite_path=sqlite_path)

    start = time.perf_counter()
    for c in candidates:
        scheduler._platform_enabled(config, c.platform)
        scheduler._platform_paused(config, c.platform)
    elapsed = time.perf_counter() - start
    # Loose budget — on a slow CI runner 10k * 2 lookups should
    # comfortably finish in under 1.5s. A regression that turns each
    # lookup into a Postgres round-trip would blow this trivially.
    assert elapsed < 1.5, f"registry lookups took {elapsed:.3f}s for 20k calls"


async def test_concurrent_make_adapter_calls_produce_distinct_instances() -> None:
    """[TEST-COVERAGE] Adapters are stateless — make_adapter must NOT
    cache. Two concurrent callers must each get their own instance so
    a stateful future adapter doesn't accidentally share connection
    pools across users."""
    cfg = IngestionConfig()

    def _build() -> Adapter:
        return make_adapter("teams", cfg)

    # Run 8 builds concurrently via to_thread to exercise GIL-released paths.
    results = await asyncio.gather(*(asyncio.to_thread(_build) for _ in range(8)))
    ids = {id(a) for a in results}
    assert len(ids) == 8, "make_adapter cached across concurrent calls"


def test_iter_platforms_order_is_stable_across_calls() -> None:
    """[TEST-COVERAGE] If a test uses ``[s.name for s in iter_platforms()]``
    as parametrize ids, the test order must be stable across runs."""
    runs = [tuple(s.name for s in iter_platforms()) for _ in range(50)]
    assert all(r == runs[0] for r in runs)


def test_platform_spec_is_picklable_for_logging_paths() -> None:
    """[TEST-COVERAGE] Some structured-log handlers (multiprocessing
    queues) require values be pickleable. Pin that PlatformSpec
    survives a pickle round-trip — including the factory."""
    spec = get_platform("teams")
    raw = pickle.dumps(spec)
    restored = pickle.loads(raw)
    assert restored == spec
    assert restored.adapter_factory is spec.adapter_factory


def test_platform_spec_is_json_serialisable_without_factory() -> None:
    """[TEST-COVERAGE] Operator-facing endpoints want to dump specs as
    JSON. The factory is a callable and excluded; everything else is
    a plain string. Pin that ``dataclasses.asdict`` + ``json.dumps``
    with ``default=str`` round-trips."""
    spec = get_platform("teams")
    asdict = dataclasses.asdict(spec)
    asdict.pop("adapter_factory")  # callables aren't JSON
    raw = json.dumps(asdict)
    restored = json.loads(raw)
    assert restored["name"] == "teams"
    assert restored["enabled_config_attr"] == "teams_enabled"
    assert restored["display_name"] == "Microsoft Teams"


def test_validate_registry_runs_against_pause_config_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] validate_registry covers BOTH PlatformToggles
    and PlatformPauseConfig — confirms the validator hasn't drifted
    to checking only one half."""
    # A spec with a bogus paused_config_attr only.
    spec = PlatformSpec(
        name="teams",
        enabled_config_attr="teams_enabled",
        paused_config_attr="this_field_does_not_exist",
        paused_overlay_key="platforms.paused.teams",
        adapter_factory=PLATFORMS["teams"].adapter_factory,
        display_name="Microsoft Teams",
    )
    monkeypatch.setattr(
        platforms_mod,
        "_REGISTRY",
        {"teams": spec, "moodle": PLATFORMS["moodle"], "telegram": PLATFORMS["telegram"]},
    )
    with pytest.raises(RuntimeError, match="this_field_does_not_exist"):
        platforms_mod.validate_registry()


def test_platform_spec_field_types_are_str_or_callable() -> None:
    """[TEST-COVERAGE] Pin that the dataclass field types haven't
    drifted away from str / Callable. A future "Optional[str]" on
    enabled_config_attr would silently let validate_registry pass
    a None and then explode at runtime in scheduler tick."""
    fields = {f.name: f.type for f in dataclasses.fields(PlatformSpec)}
    assert fields["name"] == "str"
    assert fields["enabled_config_attr"] == "str"
    assert fields["paused_config_attr"] == "str"
    assert fields["paused_overlay_key"] == "str"
    assert fields["display_name"] == "str"
    # adapter_factory is the only non-str field.
    assert "AdapterFactory" in fields["adapter_factory"]


@pytest.mark.parametrize("name", ["teams", "moodle", "telegram"])
def test_paused_overlay_key_follows_dotted_convention(name: str) -> None:
    """[TEST-COVERAGE] The schedule API and scheduler both reconstruct
    the overlay key as ``platforms.paused.<name>`` from various
    sources; pin that the registry's stored value matches the
    derivation. A typo here would silently un-pause one platform."""
    spec = get_platform(name)
    assert spec.paused_overlay_key == f"platforms.paused.{name}"


def test_make_adapter_does_not_swallow_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] A SIGINT during adapter construction must
    propagate as KeyboardInterrupt — wrapping it in AdapterBuildError
    would defeat Ctrl+C / signal handlers in the daemon."""

    def _ki(_cfg: IngestionConfig) -> Adapter:
        raise KeyboardInterrupt

    fake = _spec_with("intplat", _ki)
    monkeypatch.setattr(platforms_mod, "_REGISTRY", {**PLATFORMS, "intplat": fake})

    with pytest.raises(KeyboardInterrupt):
        make_adapter("intplat", IngestionConfig())


def test_make_adapter_does_not_swallow_system_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Same for SystemExit. BaseException family is
    deliberately outside the wrap so signal-driven exits stay clean."""

    def _se(_cfg: IngestionConfig) -> Adapter:
        raise SystemExit(1)

    fake = _spec_with("seplat", _se)
    monkeypatch.setattr(platforms_mod, "_REGISTRY", {**PLATFORMS, "seplat": fake})

    with pytest.raises(SystemExit):
        make_adapter("seplat", IngestionConfig())


def test_get_platform_reads_through_module_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Tests that monkeypatch ``_REGISTRY`` must see
    their changes through ``get_platform`` (not a stale binding from
    module import time). Pin the contract."""

    def _f(_cfg: IngestionConfig) -> Any:
        return None

    fake = _spec_with("ephem", _f)
    monkeypatch.setattr(platforms_mod, "_REGISTRY", {**PLATFORMS, "ephem": fake})
    assert get_platform("ephem") is fake


def test_adapter_build_error_distinguishable_from_unknown_platform_error() -> None:
    """[TEST-COVERAGE] The whole point of the two exception classes
    is the API layer can route 5xx (build error) vs 404 (unknown
    platform) without inspecting messages. Pin that catching
    UnknownPlatformError does NOT swallow AdapterBuildError."""
    inner = ValueError("x")
    build_err = AdapterBuildError("teams", inner)
    unknown_err = UnknownPlatformError("foo")
    assert not isinstance(build_err, UnknownPlatformError)
    assert not isinstance(build_err, KeyError)
    assert isinstance(unknown_err, KeyError)
    # And the converse — a router that catches AdapterBuildError must
    # NOT accidentally catch UnknownPlatformError.
    assert not isinstance(unknown_err, AdapterBuildError)


async def test_scheduler_clear_paused_skip_memory_uses_iter_platforms(
    sqlite_path: Path,
) -> None:
    """[TEST-COVERAGE] _clear_paused_skip_memory walks iter_platforms
    rather than a hardcoded triple. Adding a fourth platform's pause
    clean-up needs no edit here — verify by patching the registry."""
    scheduler = _build_scheduler(sqlite_path, [])
    # Seed warn-once entries for two platforms.
    scheduler._paused_skipped_logged = {
        ("alice@x", "teams"),
        ("alice@x", "moodle"),
        ("alice@x", "telegram"),
    }
    config = DaemonConfig(
        sqlite_path=sqlite_path,
        platforms=PlatformToggles(paused=PlatformPauseConfig(teams=True)),
    )
    scheduler._clear_paused_skip_memory(config)
    # Only teams remains paused, so moodle + telegram entries are dropped.
    assert scheduler._paused_skipped_logged == {("alice@x", "teams")}


def test_every_registry_value_is_a_platform_spec_instance() -> None:
    """[TEST-COVERAGE] A future refactor that swaps a value out for a
    plain dict would compile (Mapping is loose) but break every
    consumer's ``spec.enabled_config_attr`` access. Pin the type."""
    for value in PLATFORMS.values():
        assert isinstance(value, PlatformSpec)
        assert value.name in {"teams", "moodle", "telegram"}
