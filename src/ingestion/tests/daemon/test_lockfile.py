"""Affected-components tests for ``ingestion.daemon.lockfile``.

Branch coverage target: 100% line + branch on ``lockfile.py``. Each test
states the branch it pins. Real lockfiles live under ``tmp_path`` so
state never leaks across tests; PID-based assertions use
``os.getpid()`` (guaranteed alive) and ``99999999`` (almost certainly
dead, ``os.kill`` raises ``ProcessLookupError``).
"""

from __future__ import annotations

import contextlib
import os
import socket
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from ingestion.daemon import lockfile, runtime
from ingestion.daemon.crypto import ENV_FERNET_KEY
from ingestion.daemon.lockfile import LockAcquisitionError

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_payload(path: Path, pid: int, hostname: str, started: str | None = None) -> None:
    """Pre-create a lockfile with chosen contents to simulate a holder."""
    started = started or datetime.now().isoformat()
    path.write_text(f"{pid}\n{hostname}\n{started}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# acquire — happy paths
# ---------------------------------------------------------------------------


def test_acquire_creates_lockfile_with_expected_payload(tmp_path: Path) -> None:
    """Happy path: directory exists, no holder. Lockfile appears with our PID."""
    sqlite_path = tmp_path / "state.db"
    lock_path = lockfile.acquire(sqlite_path)
    assert lock_path is not None
    assert lock_path == sqlite_path.parent / "state.db.lock"
    assert lock_path.exists()
    contents = lock_path.read_text(encoding="utf-8").splitlines()
    assert int(contents[0]) == os.getpid()
    assert contents[1] == socket.gethostname()
    # Round-trip the timestamp through fromisoformat — pins the wire format.
    parsed = datetime.fromisoformat(contents[2])
    assert parsed.tzinfo is not None
    lockfile.release(lock_path)


def test_acquire_creates_parent_directory_when_missing(tmp_path: Path) -> None:
    """``parents=True`` mkdir creates a deeply nested SQLite parent dir."""
    nested = tmp_path / "a" / "b" / "c" / "state.db"
    assert not nested.parent.exists()
    lock_path = lockfile.acquire(nested)
    assert lock_path is not None
    assert lock_path.exists()
    assert nested.parent.is_dir()
    lockfile.release(lock_path)


def test_acquire_lockfile_mode_is_0o600(tmp_path: Path) -> None:
    """Pin the file permission mode used by ``os.open`` (0o600 by const)."""
    sqlite_path = tmp_path / "state.db"
    lock_path = lockfile.acquire(sqlite_path)
    assert lock_path is not None
    mode = lock_path.stat().st_mode & 0o777
    # Process umask may strip group/world bits further, but the user-bit
    # must stay readable+writable.
    assert mode & 0o600 == 0o600
    lockfile.release(lock_path)


# ---------------------------------------------------------------------------
# acquire — collision (live holder)
# ---------------------------------------------------------------------------


def test_acquire_rejects_when_current_pid_holds_lock(tmp_path: Path) -> None:
    """Pre-create a lockfile claiming our PID — must abort, ``os.kill(pid, 0)``
    on our own PID succeeds → live → reject."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, os.getpid(), socket.gethostname())
    with pytest.raises(LockAcquisitionError) as exc:
        lockfile.acquire(sqlite_path)
    msg = str(exc.value)
    assert str(os.getpid()) in msg
    assert socket.gethostname() in msg


def test_acquire_rejects_when_pid1_holds_lock_same_host(tmp_path: Path) -> None:
    """PID 1 (init) is always alive in containers — same host, alive → reject.

    The kernel may also surface ``PermissionError`` for non-root signal-0;
    the lockfile treats both as alive. Either way → ``LockAcquisitionError``.
    """
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 1, socket.gethostname())
    with pytest.raises(LockAcquisitionError):
        lockfile.acquire(sqlite_path)


# ---------------------------------------------------------------------------
# acquire — stale recovery
# ---------------------------------------------------------------------------


def test_acquire_recovers_when_holder_hostname_differs(tmp_path: Path) -> None:
    """Foreign hostname → treat as stale (different machine, different
    PID space) and recover the lockfile in our name."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 1, "other-host-zz9")
    fresh = lockfile.acquire(sqlite_path)
    assert fresh == lock_path
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    assert fresh.read_text(encoding="utf-8").splitlines()[1] == socket.gethostname()
    lockfile.release(fresh)


def test_acquire_recovers_when_holder_pid_dead(tmp_path: Path) -> None:
    """Same hostname, PID dead → ``ProcessLookupError`` → stale → recover."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 99999999, socket.gethostname())
    fresh = lockfile.acquire(sqlite_path)
    assert fresh == lock_path
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


def test_acquire_recovers_from_empty_lockfile(tmp_path: Path) -> None:
    """Empty file → ``len(parts) < 2`` → malformed → stale → recover.

    Simulates the partial-write window between O_EXCL create and ``os.write``
    if the predecessor crashes between those two syscalls.
    """
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    lock_path.write_text("", encoding="utf-8")
    fresh = lockfile.acquire(sqlite_path)
    assert fresh == lock_path
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


def test_acquire_recovers_from_corrupted_pid(tmp_path: Path) -> None:
    """Non-numeric PID → ``int(...)`` raises ValueError → malformed → stale."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    lock_path.write_text("not-a-number\nfedora\n2026-04-26T00:00:00+00:00\n", encoding="utf-8")
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


def test_acquire_recovers_from_two_line_payload(tmp_path: Path) -> None:
    """Old-format payload missing start_time line — ``parts[2]`` falls back to
    ``"unknown"`` and parsing still succeeds. Pins the ``len(parts) >= 3``
    branch in ``_holder_is_live``."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    # Two-line payload with a dead PID — should be treated as stale and recovered.
    lock_path.write_text(f"99999999\n{socket.gethostname()}\n", encoding="utf-8")
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


# ---------------------------------------------------------------------------
# acquire — in-memory bypass
# ---------------------------------------------------------------------------


def test_acquire_returns_none_for_memory_path(tmp_path: Path) -> None:
    """``:memory:`` → no lockfile → return None. Each in-memory DB is private."""
    sentinel_dir = tmp_path / "sentinel"
    sentinel_dir.mkdir()
    result = lockfile.acquire(Path(":memory:"))
    assert result is None
    # No file should have been created in CWD or anywhere visible.
    assert not (Path(".") / ":memory:.lock").exists()


def test_acquire_returns_none_for_file_memory_uri() -> None:
    """``file::memory:?cache=shared`` → bypass."""
    result = lockfile.acquire(Path("file::memory:?cache=shared"))
    assert result is None


# ---------------------------------------------------------------------------
# release semantics
# ---------------------------------------------------------------------------


def test_release_none_is_noop() -> None:
    """``release(None)`` returns silently — covers the in-memory bypass path."""
    lockfile.release(None)  # No exception expected.


def test_release_removes_owned_lockfile(tmp_path: Path) -> None:
    """Happy release: file disappears."""
    sqlite_path = tmp_path / "state.db"
    lock_path = lockfile.acquire(sqlite_path)
    assert lock_path is not None
    assert lock_path.exists()
    lockfile.release(lock_path)
    assert not lock_path.exists()


def test_release_does_not_unlink_foreign_pid(tmp_path: Path) -> None:
    """If the lockfile names another PID, ``release`` must not unlink — a
    successor process took over after we crashed and shouldn't be evicted."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 99999999, socket.gethostname())
    lockfile.release(lock_path)
    assert lock_path.exists(), "release must leave a foreign-PID lockfile in place"


def test_release_missing_file_is_noop(tmp_path: Path) -> None:
    """``release`` of a vanished lockfile is silent (FileNotFoundError swallowed)."""
    lock_path = tmp_path / "absent.lock"
    lockfile.release(lock_path)  # No exception expected.
    assert not lock_path.exists()


def test_release_corrupted_pid_is_noop(tmp_path: Path) -> None:
    """If the lockfile's first line is non-numeric, ``release`` returns
    early without touching the file (defensive against operator edits)."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    lock_path.write_text("garbage\nfedora\n", encoding="utf-8")
    lockfile.release(lock_path)
    assert lock_path.exists(), "release must not unlink when PID line is corrupt"


def test_release_unlinks_after_external_unlink_race(tmp_path: Path) -> None:
    """``unlink`` losing a race with an external delete → no exception
    (FileNotFoundError suppressed in ``contextlib.suppress``).
    Pins the second ``contextlib.suppress(FileNotFoundError)`` branch."""
    sqlite_path = tmp_path / "state.db"
    lock_path = lockfile.acquire(sqlite_path)
    assert lock_path is not None

    real_unlink = Path.unlink

    def race_unlink(self: Path, missing_ok: bool = False) -> None:
        # Simulate an external process deleting the file between read_text and unlink.
        if self == lock_path:
            real_unlink(self, missing_ok=missing_ok)
            raise FileNotFoundError(2, "vanished", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    with patch.object(Path, "unlink", race_unlink):
        lockfile.release(lock_path)


# ---------------------------------------------------------------------------
# LockAcquisitionError — class semantics
# ---------------------------------------------------------------------------


def test_lock_acquisition_error_is_runtime_error_subclass() -> None:
    """Pin the inheritance: any ``except RuntimeError`` or ``except Exception``
    catch site upstream still works."""
    exc = LockAcquisitionError("boom")
    assert isinstance(exc, RuntimeError)
    assert isinstance(exc, Exception)
    assert str(exc) == "boom"


def test_lock_acquisition_error_message_includes_holder_details(tmp_path: Path) -> None:
    """Operator-facing requirement: the error must include the holding PID,
    hostname, and start_time so an admin can ``ps -p <pid>`` immediately."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    started = "2026-04-26T12:34:56+00:00"
    _write_payload(lock_path, os.getpid(), socket.gethostname(), started)
    with pytest.raises(LockAcquisitionError) as exc:
        lockfile.acquire(sqlite_path)
    msg = str(exc.value)
    assert str(os.getpid()) in msg
    assert socket.gethostname() in msg
    assert started in msg


# ---------------------------------------------------------------------------
# Bootstrap smoke test (in-memory bypass)
# ---------------------------------------------------------------------------


def test_bootstrap_in_memory_yields_none_lock_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the daemon is bootstrapped against ``:memory:``, the lockfile is
    skipped and ``Runtime.lock_path`` is None.

    The full bootstrap path opens a SQLite store and runs migrations against
    it; ``:memory:`` makes per-connection migrations invisible to the store's
    write/read connections. Mock ``open_store`` so the smoke test isolates
    the lock-bypass branch in ``_bootstrap`` itself.
    """
    monkeypatch.setenv(ENV_FERNET_KEY, Fernet.generate_key().decode())
    fake_store = MagicMock()
    fake_store.all_overlay.return_value = {}

    def fake_open_store(_path: Path) -> MagicMock:
        return fake_store

    monkeypatch.setattr(runtime, "open_store", fake_open_store)

    rt = runtime._bootstrap(sqlite_path=Path(":memory:"))  # pyright: ignore[reportPrivateUsage]
    try:
        assert rt.lock_path is None
        # Sanity: the store was reachable and the runtime was assembled.
        assert rt.store is fake_store
    finally:
        with contextlib.suppress(Exception):
            rt.store.close()
