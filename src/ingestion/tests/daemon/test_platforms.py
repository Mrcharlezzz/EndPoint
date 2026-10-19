"""Unit tests for ``ingestion.daemon.platforms`` — the slice-3 registry.

Targets 100% line + branch coverage on ``platforms.py``. Every public
symbol gets at least one happy-path and one edge-path assertion;
``MappingProxyType`` immutability and the ``make_adapter`` error wrap
are pinned because both are load-bearing for the rest of the daemon.

Adapter modules under ``ingestion/adapters/`` are off-limits to the
slice-3 effort — these tests CALL the registered factories but never
modify them. Where an adapter constructor's behaviour is observed, it
is observed via the public ``Adapter`` Protocol surface.
"""

from __future__ import annotations

import dataclasses
from types import MappingProxyType
from typing import TYPE_CHECKING

import pytest
from ingestion.config import IngestionConfig
from ingestion.daemon import platforms as platforms_mod
from ingestion.daemon.platforms import (
    PLATFORMS,
    AdapterBuildError,
    PlatformSpec,
    UnknownPlatformError,
    _wrapped_exc_types,
    get_platform,
    iter_platforms,
    known_platforms,
    make_adapter,
    redact_credentials,
    validate_registry,
)
from ingestion.exceptions import IngestionError

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# PlatformSpec dataclass: frozen, slots, hashable, field shape
# ---------------------------------------------------------------------------


def test_platform_spec_is_frozen_dataclass() -> None:
    """Mutation must raise FrozenInstanceError so the registry's
    callers can treat specs as values, not bags of state."""
    spec = get_platform("teams")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "moodle"  # type: ignore[misc]


def test_platform_spec_uses_slots() -> None:
    """slots=True keeps each spec small. A frozen dataclass with
    slots has no ``__dict__``: introspection-friendly without paying
    the per-instance dict cost. Ad-hoc additions raise — Python
    ``frozen+slots`` in 3.14 surfaces a TypeError out of the
    super() chain, in 3.13 a FrozenInstanceError; we accept either
    so the test isn't tied to one CPython version."""
    assert "__slots__" in PlatformSpec.__dict__
    spec = get_platform("teams")
    assert not hasattr(spec, "__dict__")
    with pytest.raises((dataclasses.FrozenInstanceError, TypeError)):
        spec.surprise_field = 1  # type: ignore[attr-defined]


def test_platform_spec_is_hashable() -> None:
    """Hashable so a spec can sit in a dict or set — used by the test
    suite below to build a set of factories per registered platform."""
    seen = {get_platform(name) for name in ("teams", "moodle", "telegram")}
    assert len(seen) == 3


def test_platform_spec_field_set_is_pinned() -> None:
    """Pin the field surface so a future field add (or removal) is
    visible in code review. The runtime depends on every name listed
    here; new ones need new tests below."""
    expected = {
        "name",
        "enabled_config_attr",
        "paused_config_attr",
        "paused_overlay_key",
        "adapter_factory",
        "display_name",
        "refresh_strategy",
    }
    actual = {f.name for f in dataclasses.fields(PlatformSpec)}
    assert actual == expected


# ---------------------------------------------------------------------------
# Registry contents — exact rows
# ---------------------------------------------------------------------------


def test_registry_contains_exactly_three_platforms() -> None:
    """Adding a fourth platform must be a deliberate two-line edit
    (this assertion plus the per-platform row test below)."""
    assert set(PLATFORMS.keys()) == {"teams", "moodle", "telegram"}


@pytest.mark.parametrize(
    ("name", "enabled_attr", "paused_attr", "overlay_key", "display"),
    [
        ("teams", "teams_enabled", "teams", "platforms.paused.teams", "Microsoft Teams"),
        ("moodle", "moodle_enabled", "moodle", "platforms.paused.moodle", "Moodle"),
        (
            "telegram",
            "telegram_enabled",
            "telegram",
            "platforms.paused.telegram",
            "Telegram",
        ),
    ],
)
def test_each_platform_row_matches_expected_shape(
    name: str,
    enabled_attr: str,
    paused_attr: str,
    overlay_key: str,
    display: str,
) -> None:
    """Pin every field on every registered spec. The scheduler reads
    enabled_config_attr / paused_config_attr; the schedule router
    reads paused_overlay_key; the dashboard reads display_name."""
    spec = get_platform(name)
    assert spec.name == name
    assert spec.enabled_config_attr == enabled_attr
    assert spec.paused_config_attr == paused_attr
    assert spec.paused_overlay_key == overlay_key
    assert spec.display_name == display
    assert callable(spec.adapter_factory)


# ---------------------------------------------------------------------------
# get_platform — happy path, miss, case sensitivity, empty string
# ---------------------------------------------------------------------------


def test_get_platform_returns_registered_spec() -> None:
    spec = get_platform("teams")
    assert spec is PLATFORMS["teams"]


def test_get_platform_is_case_sensitive_and_rejects_uppercase() -> None:
    """Pinned: lookup is case-SENSITIVE. Callers must lowercase
    upstream — the daemon API stores platform identifiers lowercase
    in Postgres, so accepting "Teams" here would silently allow
    bypass paths."""
    with pytest.raises(UnknownPlatformError):
        get_platform("Teams")


def test_get_platform_empty_string_raises() -> None:
    """Empty string is treated like any other miss. Pin the message
    shape because operator dashboards grep on it."""
    with pytest.raises(UnknownPlatformError) as ei:
        get_platform("")
    assert "''" in str(ei.value)


def test_get_platform_unknown_name_raises_with_helpful_message() -> None:
    with pytest.raises(UnknownPlatformError) as ei:
        get_platform("discord")
    msg = str(ei.value)
    assert "discord" in msg
    assert "unknown platform" in msg


def test_unknown_platform_error_inherits_keyerror() -> None:
    """KeyError inheritance keeps callers that ``except KeyError``
    around the legacy literal-dict lookup pattern from breaking when
    they migrate to the registry."""
    err = UnknownPlatformError("foo")
    assert isinstance(err, KeyError)


# ---------------------------------------------------------------------------
# iter_platforms / known_platforms / immutability
# ---------------------------------------------------------------------------


def test_iter_platforms_returns_specs_in_insertion_order() -> None:
    """The dashboard renders platform rows in iteration order — a
    shuffle would visibly reorder rows across redeploys."""
    names = [spec.name for spec in iter_platforms()]
    assert names == ["teams", "moodle", "telegram"]


def test_iter_platforms_is_a_fresh_iterator_each_call() -> None:
    """Each call hands back a NEW iterator so two concurrent loops
    don't share progress state. ``Iterator`` (not ``Iterable``) by
    type — exhausting one must not exhaust the other."""
    a: Iterator[PlatformSpec] = iter_platforms()
    b: Iterator[PlatformSpec] = iter_platforms()
    list(a)  # exhaust a
    assert list(b) == [PLATFORMS[k] for k in ("teams", "moodle", "telegram")]


def test_known_platforms_is_frozen_set() -> None:
    """Frozen so callers can pass it around without defensive copies."""
    names = known_platforms()
    assert isinstance(names, frozenset)
    assert names == frozenset({"teams", "moodle", "telegram"})


def test_platforms_view_is_mappingproxytype_and_immutable() -> None:
    """Accidental writes (a buggy caller doing ``PLATFORMS["x"] = ...``)
    must raise rather than corrupt global state."""
    assert isinstance(PLATFORMS, MappingProxyType)
    with pytest.raises(TypeError):
        PLATFORMS["new"] = get_platform("teams")  # type: ignore[index]
    with pytest.raises(TypeError):
        del PLATFORMS["teams"]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# validate_registry — happy path + drift detection
# ---------------------------------------------------------------------------


def test_validate_registry_passes_with_current_config() -> None:
    """Sanity check: the live registry's enabled_config_attr /
    paused_config_attr names must all resolve to fields on the
    current ``PlatformToggles`` / ``PlatformPauseConfig``."""
    validate_registry()  # must not raise


def test_validate_registry_detects_drifted_enabled_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Pin the loud-fail behaviour. Any future schema
    rename (``teams_enabled`` -> ``ms_teams_enabled``) without a
    coordinated registry edit must crash the daemon at startup."""
    bad = PlatformSpec(
        name="teams",
        enabled_config_attr="does_not_exist_attr",
        paused_config_attr="teams",
        paused_overlay_key="platforms.paused.teams",
        adapter_factory=PLATFORMS["teams"].adapter_factory,
        display_name="Microsoft Teams",
    )
    fake = {"teams": bad, "moodle": PLATFORMS["moodle"], "telegram": PLATFORMS["telegram"]}
    monkeypatch.setattr(platforms_mod, "_REGISTRY", fake)
    with pytest.raises(RuntimeError, match="does_not_exist_attr"):
        validate_registry()


def test_validate_registry_detects_drifted_paused_attr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """[TEST-COVERAGE] Same loud-fail for the pause field."""
    bad = PlatformSpec(
        name="moodle",
        enabled_config_attr="moodle_enabled",
        paused_config_attr="ghost_field",
        paused_overlay_key="platforms.paused.moodle",
        adapter_factory=PLATFORMS["moodle"].adapter_factory,
        display_name="Moodle",
    )
    fake = {"teams": PLATFORMS["teams"], "moodle": bad, "telegram": PLATFORMS["telegram"]}
    monkeypatch.setattr(platforms_mod, "_REGISTRY", fake)
    with pytest.raises(RuntimeError, match="ghost_field"):
        validate_registry()


# ---------------------------------------------------------------------------
# make_adapter — happy path and unknown-platform propagation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["teams", "moodle", "telegram"])
def test_make_adapter_returns_correct_adapter_for_each_platform(platform: str) -> None:
    """Each registered platform yields an adapter with a matching
    ``platform`` attribute. Pin the lazy-import path so a future
    move of an adapter module surfaces here, not at first run."""
    cfg = IngestionConfig()
    adapter = make_adapter(platform, cfg)
    assert getattr(adapter, "platform", None) == platform


def test_make_adapter_unknown_platform_propagates_unknown_error() -> None:
    """UnknownPlatformError is NOT wrapped — callers translate it to
    HTTP 404 / ``ValueError`` upstream (see settings router and
    sync_service.``_build_adapter``)."""
    cfg = IngestionConfig()
    with pytest.raises(UnknownPlatformError):
        make_adapter("discord", cfg)


# ---------------------------------------------------------------------------
# make_adapter — error wrap, __cause__ preservation, secret redaction
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> dict[str, PlatformSpec]:
    """Replace ``_REGISTRY`` with a fresh copy so a test that adds a
    fake spec doesn't pollute the live registry."""
    copy = dict(PLATFORMS)
    monkeypatch.setattr(platforms_mod, "_REGISTRY", copy)
    return copy


def _spec_with_factory(name: str, factory: object) -> PlatformSpec:
    """Build a spec that mirrors an existing platform's config attrs
    but swaps the factory. Used to inject failure factories without
    monkey-patching adapter constructors."""
    base = PLATFORMS["teams"]
    return PlatformSpec(
        name=name,
        enabled_config_attr=base.enabled_config_attr,
        paused_config_attr=base.paused_config_attr,
        paused_overlay_key=f"platforms.paused.{name}",
        adapter_factory=factory,  # type: ignore[arg-type]
        display_name=name.capitalize(),
    )


def test_make_adapter_wraps_ingestion_error_with_platform_context(
    fake_registry: dict[str, PlatformSpec],
) -> None:
    """[TEST-COVERAGE] IngestionError raised inside the factory is
    re-raised as AdapterBuildError. The wrap message names the
    platform; the original is preserved on ``__cause__``."""

    def _boom(_cfg: IngestionConfig) -> object:
        raise IngestionError("inner cause")

    fake_registry["fakeplat"] = _spec_with_factory("fakeplat", _boom)
    cfg = IngestionConfig()
    with pytest.raises(AdapterBuildError) as ei:
        make_adapter("fakeplat", cfg)
    assert "fakeplat" in str(ei.value)
    assert isinstance(ei.value.__cause__, IngestionError)
    assert ei.value.platform == "fakeplat"
    assert isinstance(ei.value.inner, IngestionError)


def test_make_adapter_wraps_value_error(
    fake_registry: dict[str, PlatformSpec],
) -> None:
    """ValueError out of an adapter constructor is wrapped (the
    pydantic-settings re-validation path)."""

    def _boom(_cfg: IngestionConfig) -> object:
        raise ValueError("bad config field")

    fake_registry["fakeplat"] = _spec_with_factory("fakeplat", _boom)
    with pytest.raises(AdapterBuildError) as ei:
        make_adapter("fakeplat", IngestionConfig())
    assert isinstance(ei.value.__cause__, ValueError)


def test_make_adapter_wraps_type_error(
    fake_registry: dict[str, PlatformSpec],
) -> None:
    """TypeError likewise wrapped — the third member of the
    ``_wrapped_exc_types`` tuple."""

    def _boom(_cfg: IngestionConfig) -> object:
        raise TypeError("wrong arg type")

    fake_registry["fakeplat"] = _spec_with_factory("fakeplat", _boom)
    with pytest.raises(AdapterBuildError) as ei:
        make_adapter("fakeplat", IngestionConfig())
    assert isinstance(ei.value.__cause__, TypeError)


def test_make_adapter_does_not_wrap_unrelated_exceptions(
    fake_registry: dict[str, PlatformSpec],
) -> None:
    """[TEST-COVERAGE] KeyError / AttributeError / RuntimeError out of
    a factory propagate raw — wrapping them would mask the real
    fault. Pin all three to keep the carve-out from regressing."""

    def _key_boom(_cfg: IngestionConfig) -> object:
        raise KeyError("missing")

    def _attr_boom(_cfg: IngestionConfig) -> object:
        raise AttributeError("nope")

    def _rt_boom(_cfg: IngestionConfig) -> object:
        raise RuntimeError("internal")

    for tag, factory, exc_cls in (
        ("k", _key_boom, KeyError),
        ("a", _attr_boom, AttributeError),
        ("r", _rt_boom, RuntimeError),
    ):
        fake_registry[f"fake_{tag}"] = _spec_with_factory(f"fake_{tag}", factory)
        with pytest.raises(exc_cls):
            make_adapter(f"fake_{tag}", IngestionConfig())


def test_make_adapter_error_message_does_not_leak_credentials(
    fake_registry: dict[str, PlatformSpec],
) -> None:
    """The wrap message NEVER echoes raw credential values. This is
    a CRITICAL guard — operators paste these messages into bug
    reports."""

    def _boom(_cfg: IngestionConfig) -> object:
        raise IngestionError("boom")

    fake_registry["fakeplat"] = _spec_with_factory("fakeplat", _boom)
    secret = "super-secret-bearer-token-deadbeef"
    creds = {"access_token": secret, "refresh_token": "another-secret"}
    with pytest.raises(AdapterBuildError) as ei:
        make_adapter("fakeplat", IngestionConfig(), credentials=creds)
    msg = str(ei.value)
    assert secret not in msg
    assert "another-secret" not in msg


def test_make_adapter_attaches_redacted_credentials_summary(
    fake_registry: dict[str, PlatformSpec],
) -> None:
    """When credentials are passed, the summary attribute carries
    keys + value-type metadata only — never the values themselves."""

    def _boom(_cfg: IngestionConfig) -> object:
        raise IngestionError("boom")

    fake_registry["fakeplat"] = _spec_with_factory("fakeplat", _boom)
    creds = {"access_token": "x" * 40, "scopes": ["a", "b"], "expires_in": 3600}
    with pytest.raises(AdapterBuildError) as ei:
        make_adapter("fakeplat", IngestionConfig(), credentials=creds)
    summary = ei.value.credentials_summary  # type: ignore[attr-defined]
    assert summary == {
        "access_token": {"type": "str", "length": 40},
        "scopes": {"type": "list", "items": 2},
        "expires_in": {"type": "int"},
    }
    assert "x" * 40 not in str(summary)


def test_make_adapter_omits_summary_when_no_credentials(
    fake_registry: dict[str, PlatformSpec],
) -> None:
    """The ``credentials=None`` branch must NOT attach a summary
    attribute — distinguishes the with/without-creds wrap path."""

    def _boom(_cfg: IngestionConfig) -> object:
        raise IngestionError("boom")

    fake_registry["fakeplat"] = _spec_with_factory("fakeplat", _boom)
    with pytest.raises(AdapterBuildError) as ei:
        make_adapter("fakeplat", IngestionConfig())
    assert not hasattr(ei.value, "credentials_summary")


# ---------------------------------------------------------------------------
# redact_credentials — keys, value types, length, non-mapping inputs
# ---------------------------------------------------------------------------


def test_redact_credentials_returns_empty_for_non_mapping() -> None:
    """None, str, list, etc. all produce an empty dict — there's
    nothing legitimate to log."""
    assert redact_credentials(None) == {}
    assert redact_credentials("abc") == {}
    assert redact_credentials([1, 2]) == {}
    assert redact_credentials(42) == {}


def test_redact_credentials_emits_metadata_only() -> None:
    creds = {
        "token": "abcdef",
        "scopes": ["read", "write", "admin"],
        "tuple_field": (1, 2, 3, 4),
        "expires_at": 1700000000,
        "is_admin": True,
    }
    out = redact_credentials(creds)
    assert out == {
        "token": {"type": "str", "length": 6},
        "scopes": {"type": "list", "items": 3},
        "tuple_field": {"type": "tuple", "items": 4},
        "expires_at": {"type": "int"},
        "is_admin": {"type": "bool"},
    }


def test_redact_credentials_stringifies_non_string_keys() -> None:
    """Defensive: a numeric key (impossible in practice but cheap to
    pin) gets coerced to ``str`` before it's used as an output key."""
    creds = {1: "v"}
    out = redact_credentials(creds)
    assert out == {"1": {"type": "str", "length": 1}}


# ---------------------------------------------------------------------------
# AdapterBuildError shape
# ---------------------------------------------------------------------------


def test_adapter_build_error_carries_platform_and_inner_attrs() -> None:
    """The two attrs ``platform`` + ``inner`` are part of the public
    contract — the structured-log handler reads them by name."""
    inner = ValueError("x")
    err = AdapterBuildError("teams", inner)
    assert err.platform == "teams"
    assert err.inner is inner
    assert "teams" in str(err)
    assert "ValueError" in str(err)


def test_adapter_build_error_is_runtime_error_subclass() -> None:
    """Distinct from UnknownPlatformError (which is a KeyError) so
    the API layer can branch 5xx vs 404."""
    err = AdapterBuildError("teams", ValueError("x"))
    assert isinstance(err, RuntimeError)
    assert not isinstance(err, KeyError)


# ---------------------------------------------------------------------------
# _wrapped_exc_types caching behaviour
# ---------------------------------------------------------------------------


def test_wrapped_exc_types_returns_cached_tuple_after_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cache exists so repeat ``make_adapter`` calls don't pay
    the ``ingestion.exceptions`` import cost on every invocation."""
    # Trigger a populated cache.
    first = _wrapped_exc_types()
    assert first == (IngestionError, ValueError, TypeError)
    # Force a second call; should return the SAME tuple object.
    second = _wrapped_exc_types()
    assert second is first


def test_wrapped_exc_types_resolves_lazily_on_cold_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reset the cache and re-resolve to exercise the cold path."""
    monkeypatch.setattr(platforms_mod, "_wrapped_exc_cache", None)
    out = _wrapped_exc_types()
    assert out == (IngestionError, ValueError, TypeError)
    # Cache must be populated now.
    assert platforms_mod._wrapped_exc_cache is out
