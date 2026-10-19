"""Enumerates candidate (user, platform) rows the scheduler might sync.

The scheduler orchestrator does NOT know that the source of truth is
``PlatformCredential`` — only that some async callable yields a list of
:class:`Candidate` rows. This keeps pure-logic scheduler tests runnable
without Postgres by injecting an in-memory source. The Postgres-backed
implementation lives in :mod:`ingestion.daemon.ports.candidate_source` so it
inherits the same pyright relaxation as the other ORM ports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Candidate:
    user_email: str
    platform: str
    is_paused: bool = False


class CandidateSource(Protocol):
    """Return the non-expired (user, platform) rows to consider this tick."""

    async def list_candidates(self) -> list[Candidate]: ...


class InMemoryCandidateSource:
    """Useful for scheduler unit tests. Mutate ``candidates`` between ticks."""

    def __init__(self, candidates: list[Candidate] | None = None) -> None:
        self.candidates: list[Candidate] = list(candidates or [])

    async def list_candidates(self) -> list[Candidate]:
        return list(self.candidates)
