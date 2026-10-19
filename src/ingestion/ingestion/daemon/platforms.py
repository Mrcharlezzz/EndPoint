"""Single source of truth for the set of supported platforms.

Before this module the same three-element list (``teams`` / ``moodle`` /
``telegram``) was open-coded as ``if/elif`` in five sites: the scheduler's
kill-switch + pause checks, the runtime's adapter factory, the schedule
router's overlay-read, the settings router's health-probe adapter
factory, and the sync_service's dry-run adapter factory. Adding a
fourth platform required touching every site; missing one silently
disabled the platform on whatever code path was forgotten.

Every site that needs platform-aware dispatch now consults the
:data:`PLATFORMS` registry via :func:`get_platform` or
:func:`iter_platforms`. Adding a fourth platform becomes one new entry
in :data:`PLATFORMS` plus the adapter implementation under
``ingestion/adapters/<name>/``.

Note on the carve-out: ``ingestion/adapters/`` is intentionally NOT
edited by this module. The registry's ``adapter_factory`` callables
reference the existing adapter constructors verbatim (lazy-imported
inside each factory so a Django-touching import path stays
request-scoped). The registry only solves the dispatch problem; the
adapters' internal credential validation (and its associated
``# type: ignore`` casts) is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Sized

    from ingestion.config import IngestionConfig
    from ingestion.daemon.auth.strategy import RefreshStrategy
    from ingestion.ports import Adapter


class UnknownPlatformError(KeyError):
    """Raised when :func:`get_platform` is asked for a platform that
    isn't registered.

    Inherits from :class:`KeyError` so call sites that previously
    bubbled a ``KeyError`` from a literal dict-lookup remain
    backwards-compatible. The string form is still operator-friendly
    (``"unknown platform: 'discord'"``).
    """

    def __init__(self, name: str) -> None:
        self._name = name
        super().__init__(name)

    def __str__(self) -> str:
        return f"unknown platform: {self._name!r}"


# Adapter factory contract pinned to the three existing adapter
# constructors: ``ConfigSection -> Adapter``. The ``IngestionConfig`` is
# passed in by the caller (so a single instance is shared across every
# adapter the registry hands out) and the factory selects the
# appropriate ``cfg.<platform>`` section.
AdapterFactory = Callable[["IngestionConfig"], "Adapter"]


def _build_teams_adapter(cfg: IngestionConfig) -> Adapter:
    """Lazy-imported Teams factory. Carve-out: adapter unchanged."""
    from typing import cast

    from ingestion.adapters.teams.adapter import TeamsAdapter

    return cast("Adapter", TeamsAdapter(cfg.teams))


def _build_moodle_adapter(cfg: IngestionConfig) -> Adapter:
    """Lazy-imported Moodle factory. Carve-out: adapter unchanged."""
    from typing import cast

    from ingestion.adapters.moodle.adapter import MoodleAdapter

    return cast("Adapter", MoodleAdapter(cfg.moodle))


def _build_telegram_adapter(cfg: IngestionConfig) -> Adapter:
    """Lazy-imported Telegram factory. Carve-out: adapter unchanged."""
    from typing import cast

    from ingestion.adapters.telegram.adapter import TelegramAdapter

    return cast("Adapter", TelegramAdapter(cfg.telegram))


@dataclass(frozen=True, slots=True)
class PlatformSpec:
    """Static metadata for one platform.

    Frozen + slots so instances are hashable (usable as dict keys in
    tests) and trivially immutable. Every field is consulted by at
    least one of the call sites this module replaces; nothing
    aspirational.
    """

    # Canonical lowercase identifier. Matches the value stored in
    # ``PlatformCredential.platform`` and used as a URL path segment in
    # the daemon API.
    name: str

    # Attribute name on ``DaemonConfig.platforms``. Pinned as a string
    # constant so the registry fails loud at startup (via
    # :func:`validate_registry`) if the config schema ever drops one
    # without a coordinated migration here.
    enabled_config_attr: str

    # Attribute name on ``DaemonConfig.platforms.paused``.
    paused_config_attr: str

    # Dotted overlay key the dashboard's V10 ops pane writes to flip
    # the runtime pause. Always ``"platforms.paused.<name>"``; stored
    # explicitly so callers don't have to reconstruct it.
    paused_overlay_key: str

    # Adapter constructor. Takes an ``IngestionConfig`` and returns an
    # ``Adapter`` Protocol-conformant instance. Must not capture
    # mutable state at import time — adapters are stateless and one
    # instance per (platform, IngestionConfig) is the contract every
    # call site relies on.
    adapter_factory: AdapterFactory

    # Operator-friendly label. Shown in the dashboard's settings page
    # and structured log fields where the canonical name reads
    # awkwardly to a human.
    display_name: str

    # Slice 8: optional credential-refresh strategy. ``None`` means the
    # platform doesn't expose a programmatic refresh path (yet) — the
    # bulk-refresh loop skips these platforms with one warn-once log
    # so an operator notices a future addition without redeploys.
    # Populated lazily via :func:`register_refresh_strategy` so the
    # registry module doesn't import ``daemon.auth`` at startup (the
    # auth modules pull in httpx / silent-OIDC machinery that should
    # only load when refresh is actually wired).
    refresh_strategy: RefreshStrategy | None = None


_REGISTRY: dict[str, PlatformSpec] = {
    "teams": PlatformSpec(
        name="teams",
        enabled_config_attr="teams_enabled",
        paused_config_attr="teams",
        paused_overlay_key="platforms.paused.teams",
        adapter_factory=_build_teams_adapter,
        display_name="Microsoft Teams",
    ),
    "moodle": PlatformSpec(
        name="moodle",
        enabled_config_attr="moodle_enabled",
        paused_config_attr="moodle",
        paused_overlay_key="platforms.paused.moodle",
        adapter_factory=_build_moodle_adapter,
        display_name="Moodle",
    ),
    "telegram": PlatformSpec(
        name="telegram",
        enabled_config_attr="telegram_enabled",
        paused_config_attr="telegram",
        paused_overlay_key="platforms.paused.telegram",
        adapter_factory=_build_telegram_adapter,
        display_name="Telegram",
    ),
}


# Read-only view: ``MappingProxyType`` rejects ``__setitem__`` /
# ``__delitem__``, so a buggy caller that tries to mutate the registry
# at runtime fails with ``TypeError`` instead of corrupting global
# state. Insertion order is preserved (Python dict guarantee).
PLATFORMS: Mapping[str, PlatformSpec] = MappingProxyType(_REGISTRY)


def get_platform(name: str) -> PlatformSpec:
    """Return the spec for ``name`` or raise :class:`UnknownPlatformError`.

    Lookup is case-sensitive: callers are expected to normalize
    upstream (the daemon API stores platform identifiers lowercase in
    Postgres). An empty string is treated like any other miss.
    """
    spec = _REGISTRY.get(name)
    if spec is None:
        raise UnknownPlatformError(name)
    return spec


def iter_platforms() -> Iterator[PlatformSpec]:
    """Iterate registered specs in insertion order.

    Stable order matters for the schedule API: the dashboard renders
    platforms in this order and a shuffle would visibly reorder rows
    on every redeploy.
    """
    return iter(_REGISTRY.values())


def known_platforms() -> frozenset[str]:
    """Return the set of registered platform names. Frozen so callers
    can pass it around without worrying about mutation."""
    return frozenset(_REGISTRY.keys())


def validate_registry() -> None:
    """Confirm every spec's config-attr names actually exist on the
    current ``DaemonConfig`` schema.

    Called by ``runtime._bootstrap`` so a missing attribute fails the
    daemon at startup rather than silently never-syncing. Imported
    lazily because ``DaemonConfig`` pulls in pydantic-settings, which
    is heavy enough that we don't want to pay for it at module-import
    time of every consumer.
    """
    from ingestion.daemon.config import (  # noqa: PLC0415 — see docstring
        PlatformPauseConfig,
        PlatformToggles,
    )

    toggle_fields = set(PlatformToggles.model_fields.keys())
    pause_fields = set(PlatformPauseConfig.model_fields.keys())
    for spec in _REGISTRY.values():
        if spec.enabled_config_attr not in toggle_fields:
            msg = (
                f"PlatformSpec {spec.name!r} references "
                f"PlatformToggles.{spec.enabled_config_attr} which does not exist. "
                f"Available: {sorted(toggle_fields)}"
            )
            raise RuntimeError(msg)
        if spec.paused_config_attr not in pause_fields:
            msg = (
                f"PlatformSpec {spec.name!r} references "
                f"PlatformPauseConfig.{spec.paused_config_attr} which does not exist. "
                f"Available: {sorted(pause_fields)}"
            )
            raise RuntimeError(msg)


class AdapterBuildError(RuntimeError):
    """Raised when :func:`make_adapter` cannot construct an adapter.

    Distinct from :class:`UnknownPlatformError` (the platform name was
    not registered) — this means the registered factory itself blew
    up. Callers that want to surface a 5xx vs a 404 in the API layer
    can branch on the type. The original exception is preserved on
    ``__cause__`` so the structured-log handler can still pivot on
    the inner exception type.

    ``credentials_summary`` is attached dynamically by :func:`make_adapter`
    when credentials are supplied (via ``setattr``). The attribute is
    deliberately NOT declared at class scope so ``hasattr`` cleanly
    distinguishes the with-credentials path from the without-credentials
    one — used by the structured-log handler to pivot.
    """

    def __init__(self, platform: str, inner: BaseException) -> None:
        self.platform = platform
        self.inner = inner
        super().__init__(
            f"failed to build adapter for platform {platform!r} ({type(inner).__name__})",
        )


# Exception types produced by adapter constructors that we wrap with
# AdapterBuildError so callers see a uniform façade. Unrelated bugs
# (KeyError on a typo, AttributeError on a stale config field) are
# allowed to propagate raw — wrapping them would hide the real fault.
# Resolved lazily because ``ingestion.exceptions`` imports the rest of
# the pipeline; deferring the import keeps this module cheap to load.
_wrapped_exc_cache: tuple[type[BaseException], ...] | None = None


def _wrapped_exc_types() -> tuple[type[BaseException], ...]:
    """Return the tuple of exception types ``make_adapter`` wraps.

    Cached after the first call. ``IngestionError`` is the family of
    typed pipeline errors; ``ValueError``/``TypeError`` cover the
    misconfiguration cases pydantic-settings can raise out of an
    adapter constructor that re-validates inputs.
    """
    global _wrapped_exc_cache
    if _wrapped_exc_cache is not None:
        return _wrapped_exc_cache
    from ingestion.exceptions import IngestionError  # noqa: PLC0415

    _wrapped_exc_cache = (IngestionError, ValueError, TypeError)
    return _wrapped_exc_cache


def redact_credentials(credentials: object | None) -> dict[str, dict[str, object]]:
    """Build a log-safe summary of a credential bundle.

    Only the keys (and value types / lengths for strings) are
    surfaced; values are never echoed. Safe to attach to error
    messages and structured logs.

    Returns ``{}`` when ``credentials`` is None or not a mapping —
    callers that pass anything else have nothing legitimate to log.
    """
    if not isinstance(credentials, dict):
        return {}
    out: dict[str, dict[str, object]] = {}
    # ``credentials`` is dict[Unknown, Unknown] from pyright's POV; the
    # explicit Mapping[object, object] cast lets the iteration produce
    # tuples we can name without losing precision on ``out``. ``len``
    # is well-defined on the post-isinstance-narrowed ``Sized`` types
    # below regardless of element type.
    typed: Mapping[object, object] = cast("Mapping[object, object]", credentials)
    for raw_key, raw_value in typed.items():
        key = str(raw_key)
        value: object = raw_value
        if isinstance(value, str):
            out[key] = {"type": "str", "length": len(value)}
        elif isinstance(value, list):
            out[key] = {"type": "list", "items": len(cast("Sized", value))}
        elif isinstance(value, tuple):
            out[key] = {"type": "tuple", "items": len(cast("Sized", value))}
        else:
            out[key] = {"type": type(value).__name__}
    return out


def register_refresh_strategy(name: str, strategy: RefreshStrategy) -> None:
    """Replace the registry entry for ``name`` with one that carries
    ``strategy`` on ``refresh_strategy``.

    The :class:`PlatformSpec` is frozen, so we use
    :func:`dataclasses.replace` to build a fresh spec and rebind the
    name in ``_REGISTRY``. Lazy registration keeps the import graph
    one-way: the strategy modules import ``platforms`` (this module),
    not the other way round.

    Idempotent: registering the same strategy twice is a no-op
    (same identity check). Registering a *different* strategy
    overwrites the previous one — used by tests, not production.
    """
    from dataclasses import replace as _replace  # noqa: PLC0415 — narrow import

    spec = _REGISTRY.get(name)
    if spec is None:
        raise UnknownPlatformError(name)
    if spec.refresh_strategy is strategy:
        return
    _REGISTRY[name] = _replace(spec, refresh_strategy=strategy)


def make_adapter(
    name: str,
    ingestion_config: IngestionConfig,
    credentials: object | None = None,
) -> Adapter:
    """Construct an adapter via the registry with consistent error wrap.

    The adapter modules under ``ingestion/adapters/`` raise
    :class:`ingestion.exceptions.IngestionError` (or :class:`ValueError`
    for bad configuration) at various points; construction itself
    rarely throws today but the single chokepoint exists so future
    error-paths get the wrap for free.

    ``credentials`` is accepted for forward-compat with adapters that
    move validation into the constructor (none today). When provided
    the wrap message includes a redacted summary
    (:func:`redact_credentials`) — keys + value-type metadata only,
    never the values themselves.

    Re-raising chain:
      * :class:`UnknownPlatformError` — name not registered (propagates
        straight through; callers usually translate to 404).
      * :class:`AdapterBuildError` — registered factory blew up. The
        original exception is preserved via ``__cause__``.
      * Anything else (KeyError, AttributeError on the registry
        itself) — propagates raw so the real fault isn't masked.
    """
    spec = get_platform(name)
    try:
        return spec.adapter_factory(ingestion_config)
    except _wrapped_exc_types() as exc:
        # Preserve the original via __cause__ so the structured-log
        # handler can still pivot on the inner exception type.
        if credentials is not None:
            # The summary is intentionally not embedded in the message
            # string — that would make log search noisy. It's an
            # attribute callers can attach to a structured log entry.
            err = AdapterBuildError(spec.name, exc)
            # Attribute is deliberately not declared at class scope — see
            # ``AdapterBuildError`` docstring for the hasattr contract.
            # ``setattr`` keeps pyright quiet without a type-ignore comment.
            setattr(err, "credentials_summary", redact_credentials(credentials))  # noqa: B010
            raise err from exc
        raise AdapterBuildError(spec.name, exc) from exc
