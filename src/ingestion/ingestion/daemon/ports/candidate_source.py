"""Postgres-backed :class:`CandidateSource` against ``PlatformCredential``.

Reads every non-expired row. Paused users still surface in the list — it's the
scheduler's job to filter them — so callers can count pause state without a
second query.
"""

from __future__ import annotations

import random

from core.models import PlatformCredential

from ingestion.daemon.candidates import Candidate


class PostgresCandidateSource:
    async def list_candidates(self) -> list[Candidate]:
        rows: list[Candidate] = []
        # Deterministic DB order (user_email, platform) so explaining a
        # missing user in the logs isn't confounded by Postgres's physical-
        # page iteration order.
        async for row in (
            PlatformCredential.objects.filter(is_expired=False)
            .order_by("user_email", "platform")
            .aiterator()
        ):
            rows.append(
                Candidate(
                    user_email=row.user_email,
                    platform=row.platform,
                    is_paused=row.is_paused,
                )
            )
        # Shuffle in Python before returning so a hot prefix of the list
        # doesn't repeatedly win the global semaphore while tail users
        # wait. Probabilistic fairness; cheap for the expected 50-300
        # candidate count.
        random.shuffle(rows)
        return rows
