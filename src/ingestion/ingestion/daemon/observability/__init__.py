"""Observability scaffolding: optional tracing, future Prometheus, etc.

Slice 13 ships :mod:`tracing`, an optional OpenTelemetry shim. The
default behaviour is a no-op so the daemon doesn't add an opentelemetry
dependency; operators who care opt in by installing
``opentelemetry-sdk`` (and configuring ``OTEL_EXPORTER_OTLP_ENDPOINT``)
themselves. See the module docstring for the contract.
"""

from __future__ import annotations

__all__: list[str] = []
