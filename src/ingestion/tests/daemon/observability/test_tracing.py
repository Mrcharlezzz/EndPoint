"""Slice 13 part C: optional OpenTelemetry tracing shim.

:func:`traced_span` is a no-op when ``opentelemetry-sdk`` isn't
installed and yields a real span when it is. The tests exercise both
paths via the cache + import-resolver seam so the shim's behaviour is
pinned regardless of the operator's pip environment.

* Without the SDK importable → ``traced_span`` is a nullcontext (yields
  None and accepts any kwargs without raising).
* With a fake tracer module installed → ``traced_span`` opens a span
  and records the kwargs as attributes.
* Tracer resolution is cached: a second invocation does NOT re-import.
* Misconfigured tracer (raises during ``get_tracer``) falls back to
  the no-op + logs a warning.
* ``set_attribute`` failures are caught and logged so a span error
  does not propagate.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest
from ingestion.daemon.observability.tracing import (
    reset_tracer_cache_for_tests,
    traced_span,
)


@pytest.fixture(autouse=True)
def reset_cache_each_test() -> None:
    """Tracer lookup is cached at module level. Reset before AND after
    each test so a previous test's monkeypatched tracer doesn't leak."""
    reset_tracer_cache_for_tests()
    yield
    reset_tracer_cache_for_tests()


# ---------------------------------------------------------------------------
# no-op fallback when SDK is not installed
# ---------------------------------------------------------------------------


def test_no_otel_installed_returns_nullcontext(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the import to fail and assert the context manager yields
    None and accepts arbitrary kwargs without raising."""
    # Block ``opentelemetry`` so the import inside ``_import_tracer`` raises.
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    with traced_span("sync.run", sync_id="abc", platform="teams") as span:
        # nullcontext yields None.
        assert span is None


def test_no_otel_runs_body_without_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The body of the ``with`` block executes normally with the no-op
    fallback. Pin the contract that operators without the SDK don't
    pay any extra runtime cost or surface any error."""
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    side_effects: list[int] = []
    with traced_span("foo"):
        side_effects.append(1)
    assert side_effects == [1]


# ---------------------------------------------------------------------------
# OTel SDK installed (faked): real span + attributes
# ---------------------------------------------------------------------------


class _FakeSpan:
    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class _FakeSpanContext:
    def __init__(self, span: _FakeSpan) -> None:
        self._span = span

    def __enter__(self) -> _FakeSpan:
        return self._span

    def __exit__(self, *_exc: object) -> None:
        return None


class _FakeTracer:
    def __init__(self) -> None:
        self.spans: list[_FakeSpan] = []
        self.span_names: list[str] = []

    def start_as_current_span(self, name: str) -> _FakeSpanContext:
        self.span_names.append(name)
        span = _FakeSpan()
        self.spans.append(span)
        return _FakeSpanContext(span)


def _install_fake_otel(monkeypatch: pytest.MonkeyPatch, tracer: _FakeTracer) -> None:
    """Inject a fake ``opentelemetry.trace`` module into ``sys.modules``
    so :func:`_import_tracer` resolves to our tracer. The real SDK is
    not required."""
    fake_otel = types.ModuleType("opentelemetry")
    fake_trace = types.ModuleType("opentelemetry.trace")

    def _get_tracer(_name: str) -> _FakeTracer:
        return tracer

    fake_trace.get_tracer = _get_tracer  # type: ignore[attr-defined]
    fake_otel.trace = fake_trace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)


def test_otel_installed_opens_span_with_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the SDK is importable, ``traced_span("foo")`` invokes
    ``tracer.start_as_current_span("foo")``."""
    tracer = _FakeTracer()
    _install_fake_otel(monkeypatch, tracer)
    with traced_span("sync.run"):
        pass
    assert tracer.span_names == ["sync.run"]


def test_otel_installed_records_kwargs_as_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kwargs passed to ``traced_span`` are forwarded to
    ``span.set_attribute(key, value)`` so distributed-trace tooling can
    pivot on sync_id / user_email / platform."""
    tracer = _FakeTracer()
    _install_fake_otel(monkeypatch, tracer)
    with traced_span(
        "sync.run",
        sync_id="abc-123",
        user_email="alice@x",
        platform="teams",
    ):
        pass
    assert len(tracer.spans) == 1
    assert tracer.spans[0].attributes == {
        "sync_id": "abc-123",
        "user_email": "alice@x",
        "platform": "teams",
    }


# ---------------------------------------------------------------------------
# caching: tracer is resolved exactly once per process
# ---------------------------------------------------------------------------


def test_tracer_resolution_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    """The first call to ``traced_span`` resolves the tracer; subsequent
    calls reuse the cached one. Pin via a counter on ``get_tracer``
    that must be incremented at most once for two ``traced_span`` calls."""
    tracer = _FakeTracer()
    fake_otel = types.ModuleType("opentelemetry")
    fake_trace = types.ModuleType("opentelemetry.trace")
    counter = {"n": 0}

    def _get_tracer(_name: str) -> _FakeTracer:
        counter["n"] += 1
        return tracer

    fake_trace.get_tracer = _get_tracer  # type: ignore[attr-defined]
    fake_otel.trace = fake_trace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)

    with traced_span("a"):
        pass
    with traced_span("b"):
        pass
    # get_tracer is called once; the second traced_span reuses the cache.
    assert counter["n"] == 1


# ---------------------------------------------------------------------------
# misconfigured tracer (get_tracer raises) -> no-op fallback
# ---------------------------------------------------------------------------


def test_misconfigured_tracer_falls_back_to_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A global tracer provider that raises on ``get_tracer`` must NOT
    take down the daemon. The shim catches the exception, logs once,
    and falls back to a nullcontext."""
    fake_otel = types.ModuleType("opentelemetry")
    fake_trace = types.ModuleType("opentelemetry.trace")

    def _get_tracer(_name: str) -> Any:
        raise RuntimeError("misconfigured provider")

    fake_trace.get_tracer = _get_tracer  # type: ignore[attr-defined]
    fake_otel.trace = fake_trace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)
    # Should not raise — fallback to nullcontext.
    with traced_span("foo", x=1) as span:
        assert span is None


# ---------------------------------------------------------------------------
# set_attribute failure is caught
# ---------------------------------------------------------------------------


class _BadSpan:
    def set_attribute(self, _key: str, _value: Any) -> None:
        raise RuntimeError("attribute backend down")


class _BadSpanContext:
    def __enter__(self) -> _BadSpan:
        return _BadSpan()

    def __exit__(self, *_exc: object) -> None:
        return None


class _BadTracer:
    def start_as_current_span(self, _name: str) -> _BadSpanContext:
        return _BadSpanContext()


def test_set_attribute_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the span backend raises during ``set_attribute`` (e.g.
    a remote OTLP collector hangs up mid-write), the shim catches and
    logs but does NOT propagate — the wrapped code keeps running."""
    fake_otel = types.ModuleType("opentelemetry")
    fake_trace = types.ModuleType("opentelemetry.trace")

    def _get_tracer(_name: str) -> _BadTracer:
        return _BadTracer()

    fake_trace.get_tracer = _get_tracer  # type: ignore[attr-defined]
    fake_otel.trace = fake_trace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)
    # Must not raise.
    with traced_span("foo", bad_attr="will-raise"):
        pass


# ---------------------------------------------------------------------------
# reset_tracer_cache_for_tests
# ---------------------------------------------------------------------------


def test_reset_tracer_cache_forces_re_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reset helper flips the cache flag so the next ``traced_span``
    call re-runs ``_import_tracer``. Pin via a counter on get_tracer
    that increments twice across a reset."""
    tracer = _FakeTracer()
    fake_otel = types.ModuleType("opentelemetry")
    fake_trace = types.ModuleType("opentelemetry.trace")
    counter = {"n": 0}

    def _get_tracer(_name: str) -> _FakeTracer:
        counter["n"] += 1
        return tracer

    fake_trace.get_tracer = _get_tracer  # type: ignore[attr-defined]
    fake_otel.trace = fake_trace  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "opentelemetry", fake_otel)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", fake_trace)

    with traced_span("a"):
        pass
    reset_tracer_cache_for_tests()
    with traced_span("b"):
        pass
    assert counter["n"] == 2
