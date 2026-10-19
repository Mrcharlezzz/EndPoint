"""Lifecycle hooks for daemon-side state: cascade-delete, TTL prune,
per-scope cursor cleanup, and Fernet rotation.

Where Slice 10 introduced reliability primitives (worker pool, dead
letter, idempotency cache), Slice 12 wires up the matching cleanup
paths so daemon-side rows don't outlive the user, the schedule, or
the encryption key that produced them.

Public surface:

* :func:`cleanup.cascade_delete_user` — single-transaction wipe of
  daemon-side rows for one email when they leave the source of truth.
* :func:`cleanup.prune_sync_history` / :func:`cleanup.prune_dead_letter`
  / :func:`cleanup.prune_audit_log` — TTL set-DELETE helpers for the
  observability + history tables.
* :func:`loops.run_lifecycle_prune_loop` — wraps the prune helpers in
  the same shutdown-aware loop pattern the observability loops use.
* :func:`cleanup.cleanup_stale_cursors` — drops sync_cursors rows for
  scopes no longer in the active config.
* :func:`fernet_rotation.rotate_fernet_in_place` — re-encrypts every
  PlatformCredential row from one key to another (interruptible,
  idempotent).

Module split keeps each file under the 400-line ceiling — the TTL
loops live separately from the one-shot ``cascade_delete_user`` so a
bug in the loop driver doesn't risk the destructive single-call path.
"""
