"""Endpoint ingestion pipeline — fetches and normalizes content from Teams, Moodle, and Telegram."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("endpoint-ingestion")
except PackageNotFoundError:
    __version__ = "0.0.0"
