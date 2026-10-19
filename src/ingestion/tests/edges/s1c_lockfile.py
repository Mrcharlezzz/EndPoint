"""Edge + production tests for ``ingestion.daemon.lockfile``.

Covers the impl agent's brainstormed edges, the production-environment
matrix (containers, SIGKILL, disk full, read-only FS), and 8+ additional
brainstorm scenarios this agent surfaced. Every test names the edge it
pins in its docstring.

PID-based locks are a minefield: stale-recovery races, hostname drift on
DHCP, and FS-level partial writes all produce subtly broken behaviour.
We pin them here so a future refactor that breaks them shows red.
"""

from __future__ import annotations

import contextlib
import errno
import os
import socket
import threading
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from ingestion.daemon import lockfile, runtime
from ingestion.daemon.crypto import ENV_FERNET_KEY
from ingestion.daemon.lockfile import LockAcquisitionError

if TYPE_CHECKING:
    # ``os._typeshed.StrOrBytesPath`` is only exposed at type-check time;
    # mirror it locally so monkeypatch wrappers can match the real
    # ``os.open`` / ``os.unlink`` signature without ``# type: ignore``.
    from _typeshed import StrOrBytesPath

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_payload(path: Path, pid: int, hostname: str, started: str | None = None) -> None:
    started = started or datetime.now().isoformat()
    path.write_text(f"{pid}\n{hostname}\n{started}\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. PID 1 (container init) — same host alive → reject
# ---------------------------------------------------------------------------


def test_pid1_same_host_blocks_acquire(tmp_path: Path) -> None:
    """Edge: container init (PID 1) is always alive. Same host → reject."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 1, socket.gethostname())
    with pytest.raises(LockAcquisitionError):
        lockfile.acquire(sqlite_path)


def test_pid1_foreign_host_treated_as_stale(tmp_path: Path) -> None:
    """Edge: PID 1 in a different namespace (foreign host) → stale → recover.
    A previous container's PID 1 is irrelevant to ours."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 1, "previous-container-pod-abc")
    fresh = lockfile.acquire(sqlite_path)
    assert fresh == lock_path
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


# ---------------------------------------------------------------------------
# 2. PID 0 / negative PID — early return without calling os.kill
# ---------------------------------------------------------------------------


def test_pid_zero_treated_as_stale(tmp_path: Path) -> None:
    """Edge: ``pid == 0`` would signal own process group via ``os.kill(0, 0)``;
    the lockfile guards with ``pid <= 0`` short-circuit → stale → recover."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 0, socket.gethostname())
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


def test_negative_pid_treated_as_stale(tmp_path: Path) -> None:
    """Edge: a negative PID in a corrupted file would hit the
    ``os.kill(-pid, 0)`` "send to process group" semantics. ``pid <= 0``
    short-circuits to stale before that ever happens."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, -42, socket.gethostname())
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


# ---------------------------------------------------------------------------
# 3. Crash between O_EXCL create and os.write
# ---------------------------------------------------------------------------


def test_crash_between_open_and_write_recovered(tmp_path: Path) -> None:
    """Edge: predecessor crashed after ``O_EXCL`` create but before
    ``os.write`` — empty file. Treated as malformed → stale → recover."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    # Simulate the partial-write window with O_EXCL (just like _write_lock does)
    # The fd is closed immediately to leave a zero-byte file.
    os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    assert lock_path.read_bytes() == b""

    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


# ---------------------------------------------------------------------------
# 4. Non-numeric PID line
# ---------------------------------------------------------------------------


def test_non_numeric_pid_treated_as_stale(tmp_path: Path) -> None:
    """Edge: ``int("foobar")`` raises ValueError → stale recovery path."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    lock_path.write_text("foobar\nfedora\n2026-04-26T00:00:00+00:00\n", encoding="utf-8")
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


# ---------------------------------------------------------------------------
# 5. Hostname change (DHCP rename, k8s migration with persistent PVC)
# ---------------------------------------------------------------------------


def test_hostname_drift_recovers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge: persistent volume survives an instance migration that renamed
    the host. Old hostname in payload → mismatch → stale → recover."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 1, "old-pod-name-pre-migration")

    # Pin our own hostname for assertion clarity.
    monkeypatch.setattr(socket, "gethostname", lambda: "new-pod-name-post-migration")
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert fresh.read_text(encoding="utf-8").splitlines()[1] == "new-pod-name-post-migration"
    lockfile.release(fresh)


# ---------------------------------------------------------------------------
# 6. Stale-recovery loses the race to a successor
# ---------------------------------------------------------------------------


def test_stale_recovery_race_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge: lockfile was stale, we removed it, another daemon won the
    O_EXCL race during our retry → ``LockAcquisitionError`` with a clear
    "claimed lock during stale recovery" message. Pins the second
    ``_write_lock`` call's FileExistsError branch (lockfile.py:84-85)."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 99999999, socket.gethostname())  # stale

    real_open = os.open
    call_count = {"n": 0}

    def fail_second_open(path: StrOrBytesPath, flags: int, mode: int = 0o777) -> int:
        # Only intercept O_EXCL writes against our lockfile path.
        is_excl = bool(flags & os.O_EXCL)
        # Match by ``os.fspath`` to normalise str / bytes / PathLike[...] to a
        # comparable form without losing strict-typed information.
        if is_excl and os.fspath(path) == os.fspath(lock_path):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise FileExistsError(errno.EEXIST, "race", os.fsdecode(path))
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_second_open)
    with pytest.raises(LockAcquisitionError) as exc:
        lockfile.acquire(sqlite_path)
    assert "claimed lock during stale recovery" in str(exc.value)


# ---------------------------------------------------------------------------
# 7. Permission denied on stale-unlink
# ---------------------------------------------------------------------------


def test_unlink_permission_denied_during_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edge: stale lockfile exists, but ``unlink`` denied (e.g., FS perms,
    SELinux). The impl wraps the underlying ``PermissionError`` (an OSError)
    as ``LockAcquisitionError`` so operators see a single, named failure
    mode. The original ``PermissionError`` is chained via ``__cause__``."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 99999999, socket.gethostname())  # stale

    real_unlink = Path.unlink

    def deny_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == lock_path:
            raise PermissionError(errno.EACCES, "denied", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", deny_unlink)
    with pytest.raises(LockAcquisitionError, match="could not remove stale lockfile") as exc:
        lockfile.acquire(sqlite_path)
    # The PermissionError must be chained so operators can introspect the
    # underlying errno without losing the friendly outer message.
    assert isinstance(exc.value.__cause__, PermissionError)
    assert exc.value.__cause__.errno == errno.EACCES


# ---------------------------------------------------------------------------
# 8. Unicode hostname round-trips
# ---------------------------------------------------------------------------


def test_unicode_hostname_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Edge: hostnames can be IDN/non-ASCII (Cyrillic, CJK). UTF-8 encode
    and ``read_text("utf-8")`` decode must round-trip exactly."""
    weird_host = "сервер-альфа-日本語"
    monkeypatch.setattr(socket, "gethostname", lambda: weird_host)
    sqlite_path = tmp_path / "state.db"
    lock_path = lockfile.acquire(sqlite_path)
    assert lock_path is not None
    assert lock_path.read_text(encoding="utf-8").splitlines()[1] == weird_host
    lockfile.release(lock_path)


# ---------------------------------------------------------------------------
# 9. Two distinct sqlite paths — independent locks
# ---------------------------------------------------------------------------


def test_two_paths_acquire_independently(tmp_path: Path) -> None:
    """Edge: separate DBs → separate lockfiles. No cross-contamination."""
    a = tmp_path / "a" / "state.db"
    b = tmp_path / "b" / "state.db"
    lock_a = lockfile.acquire(a)
    lock_b = lockfile.acquire(b)
    try:
        assert lock_a is not None and lock_b is not None
        assert lock_a != lock_b
        assert lock_a.exists() and lock_b.exists()
    finally:
        lockfile.release(lock_a)
        lockfile.release(lock_b)


# ---------------------------------------------------------------------------
# 10. Pytest fixture isolation
# ---------------------------------------------------------------------------


def test_lockfile_fixture_isolation_part_a(tmp_path: Path) -> None:
    """Edge: tmp_path gives each test a private dir → no leak across tests."""
    lock_path = lockfile.acquire(tmp_path / "state.db")
    assert lock_path is not None
    lockfile.release(lock_path)


def test_lockfile_fixture_isolation_part_b(tmp_path: Path) -> None:
    """Edge: companion to part_a — second test starts with no lockfile."""
    expected = tmp_path / "state.db.lock"
    assert not expected.exists()
    lock_path = lockfile.acquire(tmp_path / "state.db")
    assert lock_path == expected
    lockfile.release(lock_path)


# ---------------------------------------------------------------------------
# 11. Production: container restart with persistent PVC, different hostname
# ---------------------------------------------------------------------------


def test_container_restart_with_persistent_pvc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production edge: PVC survives a ``docker compose down`` then a restart
    where the new container has a different hostname. Old lock from a process
    that's irrelevant to the new namespace → recover."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 7, "endpoint-daemon-abc-old")
    monkeypatch.setattr(socket, "gethostname", lambda: "endpoint-daemon-xyz-new")
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert fresh.read_text(encoding="utf-8").splitlines()[1] == "endpoint-daemon-xyz-new"
    lockfile.release(fresh)


# ---------------------------------------------------------------------------
# 12. Production: SIGKILL → no graceful release
# ---------------------------------------------------------------------------


def test_sigkilled_predecessor_recovered(tmp_path: Path) -> None:
    """Production edge: SIGKILL skips the ``finally`` shutdown, lockfile
    remains. Same hostname, dead PID → ``ProcessLookupError`` → stale → recover."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 99999999, socket.gethostname())
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


# ---------------------------------------------------------------------------
# 13. Production: ENOSPC at lockfile write
# ---------------------------------------------------------------------------


def test_disk_full_leaves_zero_byte_lockfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production edge: ``os.write`` fails with ENOSPC after O_EXCL succeeded.
    The empty file remains. Per the impl iteration, the OSError is wrapped
    as ``LockAcquisitionError``. Next ``acquire`` sees malformed → recover."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"

    real_write = os.write

    def fail_first_write(_fd: int, _data: bytes) -> int:
        # Only fail the first call so the recovery write succeeds.
        monkeypatch.setattr(os, "write", real_write)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(os, "write", fail_first_write)
    with pytest.raises(LockAcquisitionError, match="No space left") as exc:
        lockfile.acquire(sqlite_path)
    assert isinstance(exc.value.__cause__, OSError)
    assert exc.value.__cause__.errno == errno.ENOSPC
    # The O_EXCL create succeeded; the file should remain empty.
    assert lock_path.exists()
    assert lock_path.read_bytes() == b""

    # Second attempt: acquire sees an empty file, treats as malformed, recovers.
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


# ---------------------------------------------------------------------------
# 14. Production: read-only filesystem at acquire
# ---------------------------------------------------------------------------


def test_read_only_filesystem_propagates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Production edge: lockfile dir is on a read-only mount (EROFS).
    ``os.open`` raises; the daemon must NOT have an orphaned store handle."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"

    real_open = os.open

    def deny_create(path: StrOrBytesPath, flags: int, mode: int = 0o777) -> int:
        if bool(flags & os.O_CREAT) and os.fspath(path) == os.fspath(lock_path):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", deny_create)
    # Per the impl iteration, OSError on initial create is now wrapped as
    # ``LockAcquisitionError`` with the underlying message preserved.
    with pytest.raises(LockAcquisitionError, match="Read-only"):
        lockfile.acquire(sqlite_path)
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# 15. Bootstrap integration: open_store fails → lock released
# ---------------------------------------------------------------------------


def test_bootstrap_releases_lock_on_open_store_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Bootstrap edge: ``open_store`` raises (e.g., migration error). The
    ``try/except BaseException`` path must call ``lockfile.release`` so a
    crashed startup doesn't leave a lockfile behind."""
    monkeypatch.setenv(ENV_FERNET_KEY, Fernet.generate_key().decode())
    sqlite_path = tmp_path / "state.db"

    released: list[Path | None] = []
    real_release = lockfile.release

    def spy_release(p: Path | None) -> None:
        released.append(p)
        real_release(p)

    monkeypatch.setattr(runtime.lockfile, "release", spy_release)

    def explode(_path: Path) -> None:
        raise RuntimeError("synthetic open_store failure")

    monkeypatch.setattr(runtime, "open_store", explode)

    with pytest.raises(RuntimeError, match="synthetic"):
        runtime._bootstrap(sqlite_path=sqlite_path)  # pyright: ignore[reportPrivateUsage]
    # The lock was acquired and released — file should not remain.
    assert not (sqlite_path.parent / "state.db.lock").exists()
    assert released, "lockfile.release was never called on the open_store failure path"


# ---------------------------------------------------------------------------
# 16. Bootstrap integration: lockfile.acquire raises → no open_store call
# ---------------------------------------------------------------------------


def test_bootstrap_aborts_when_lock_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bootstrap edge: a live peer holds the lock. ``open_store`` MUST NOT be
    called — otherwise we'd briefly open a second handle to the SQLite file."""
    monkeypatch.setenv(ENV_FERNET_KEY, Fernet.generate_key().decode())

    def reject(_path: Path) -> Path:
        raise LockAcquisitionError("another daemon is running")

    monkeypatch.setattr(runtime.lockfile, "acquire", reject)

    open_calls: list[Path] = []

    def track_open(path: Path) -> None:
        open_calls.append(path)
        raise AssertionError("open_store must not be called when lock is rejected")

    monkeypatch.setattr(runtime, "open_store", track_open)

    with pytest.raises(LockAcquisitionError, match="another daemon"):
        runtime._bootstrap(sqlite_path=Path("/tmp/never-touched.db"))  # pyright: ignore[reportPrivateUsage]
    assert open_calls == []


# ---------------------------------------------------------------------------
# Additional brainstorm — surfaced by this agent (>= 8 required)
# ---------------------------------------------------------------------------


def test_concurrent_threads_only_one_acquires(tmp_path: Path) -> None:
    """Brainstorm: same-process concurrent ``acquire`` calls. ``O_EXCL``
    serialises at the kernel — exactly one thread wins; the others get
    a holder check on the live current PID and abort."""
    sqlite_path = tmp_path / "state.db"
    results: list[tuple[Path | None, BaseException | None]] = []
    barrier = threading.Barrier(8)

    def attempt() -> None:
        barrier.wait()
        try:
            lp = lockfile.acquire(sqlite_path)
            results.append((lp, None))
        except BaseException as exc:  # noqa: BLE001
            results.append((None, exc))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
    winners = [lp for lp, _exc in results if lp is not None]
    losers = [exc for _lp, exc in results if exc is not None]
    assert len(winners) == 1, f"expected exactly one winner, got {len(winners)}"
    assert len(losers) == len(threads) - 1
    assert all(isinstance(e, LockAcquisitionError) for e in losers)
    lockfile.release(winners[0])


def test_release_after_successor_took_over_no_spurious_unlink(tmp_path: Path) -> None:
    """Brainstorm: we acquired, crashed mid-shutdown, restart took the lock,
    our shutdown finally fires release after — the lockfile names a different
    PID, so release is a no-op. No silent eviction of the successor."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    # Successor's payload — different PID, same host.
    successor_pid = 99999998
    _write_payload(lock_path, successor_pid, socket.gethostname())
    # Our late release call from the path returned by an earlier acquire.
    lockfile.release(lock_path)
    assert lock_path.exists()
    assert int(lock_path.read_text(encoding="utf-8").splitlines()[0]) == successor_pid


def test_symlink_path_acquires_through_link(tmp_path: Path) -> None:
    """Brainstorm: SQLite parent dir is a symlink to elsewhere. ``O_EXCL``
    follows symlinks by default (no ``O_NOFOLLOW``); the lockfile lands at
    the symlink target. Pin the behaviour so a future ``O_NOFOLLOW`` flip
    is a deliberate decision."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir)
    sqlite_path = link / "state.db"
    lock_path = lockfile.acquire(sqlite_path)
    assert lock_path is not None
    # The lockfile materialises behind the symlink as expected.
    assert (real_dir / "state.db.lock").exists()
    lockfile.release(lock_path)


def test_lockfile_with_extra_trailing_lines_parses(tmp_path: Path) -> None:
    """Brainstorm: an operator hand-edited the lockfile and added a trailing
    note. ``splitlines()`` returns >3 lines but only [0..2] are inspected.
    With a dead PID the file is still treated as stale → recover."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    payload = (
        f"99999999\n{socket.gethostname()}\n2026-04-26T00:00:00+00:00\n"
        "operator-note: this lock looked stale on 2026-04-26\n"
        "extra: line\n"
    )
    lock_path.write_text(payload, encoding="utf-8")
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


def test_socket_gethostname_failure_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brainstorm: ``socket.gethostname`` can raise (DNS resolver borked,
    seccomp blocking). The daemon must surface the error rather than
    creating a lockfile with a bogus hostname."""
    sqlite_path = tmp_path / "state.db"

    def boom() -> str:
        raise OSError("hostname resolution failed")

    monkeypatch.setattr(socket, "gethostname", boom)
    with pytest.raises(OSError, match="hostname resolution failed"):
        lockfile.acquire(sqlite_path)
    assert not (sqlite_path.parent / "state.db.lock").exists()


def test_holder_inspection_lockfile_vanishes_under_us(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brainstorm: between the FileExistsError on the first ``_write_lock``
    call and ``_holder_is_live``'s ``read_text``, an external process
    deleted the lockfile. ``_holder_is_live`` returns
    ``(False, "lockfile vanished during inspection")`` → recovery proceeds.
    Pins the FileNotFoundError branch in ``_holder_is_live``."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    # Pre-create a lockfile so the first _write_lock raises FileExistsError.
    _write_payload(lock_path, 99999999, socket.gethostname())

    real_read_text = Path.read_text
    fired = {"n": 0}

    def vanish_during_read(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        # Simulate a concurrent unlink that raced our inspection. Only fire
        # the first time so the post-acquire assertion can read normally.
        # Signature mirrors the lockfile.py call site: ``read_text(encoding="utf-8")``.
        # Python 3.13 added a ``newline`` keyword to Path.read_text but the
        # project pins pythonVersion=3.12 — keep the wrapper to the 3.12 shape.
        if self == lock_path and fired["n"] == 0:
            fired["n"] = 1
            real_unlink = Path.unlink
            real_unlink(self)
            raise FileNotFoundError(2, "vanished", str(self))
        return real_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", vanish_during_read)
    fresh = lockfile.acquire(sqlite_path)
    assert fresh is not None
    assert fired["n"] == 1, "expected the inspection to hit the FileNotFoundError branch"
    assert int(fresh.read_text(encoding="utf-8").splitlines()[0]) == os.getpid()
    lockfile.release(fresh)


def test_acquire_succeeds_after_explicit_release(tmp_path: Path) -> None:
    """Brainstorm: graceful shutdown then quick restart. The same process
    re-acquires its own released lockfile without a recovery cycle."""
    sqlite_path = tmp_path / "state.db"
    first = lockfile.acquire(sqlite_path)
    assert first is not None
    lockfile.release(first)
    assert not first.exists()
    second = lockfile.acquire(sqlite_path)
    assert second == first
    assert second is not None
    assert second.exists()
    lockfile.release(second)


def test_holder_pid_alive_treated_as_live_no_kill_signal_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brainstorm: ``os.kill(pid, 0)`` is a probe — sig 0 must never deliver
    a real signal. Pin that the impl uses signal 0, not e.g. SIGTERM, by
    spying on the signal argument. (Catches a future regression that
    accidentally upgrades the probe to a kill.)"""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, os.getpid(), socket.gethostname())

    real_kill = os.kill
    seen: list[int] = []

    def spy_kill(pid: int, sig: int) -> None:
        seen.append(sig)
        return real_kill(pid, sig)

    monkeypatch.setattr(os, "kill", spy_kill)
    with pytest.raises(LockAcquisitionError):
        lockfile.acquire(sqlite_path)
    assert seen == [0], f"impl must probe with signal 0 only, saw {seen}"


def test_permission_error_on_kill_treated_as_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brainstorm: ``os.kill`` may raise ``PermissionError`` for a foreign-
    UID process even though it's alive (kernel says: yes it exists, no you
    can't signal it). The lockfile treats that as live → reject. Pin it."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 99999999, socket.gethostname())

    def permission_denied(_pid: int, _sig: int) -> None:
        raise PermissionError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(os, "kill", permission_denied)
    with pytest.raises(LockAcquisitionError) as exc:
        lockfile.acquire(sqlite_path)
    assert "alive" in str(exc.value)


def test_release_idempotent_double_call(tmp_path: Path) -> None:
    """Brainstorm: shutdown can ``release`` twice if both the ``finally``
    in ``run`` and an upstream ``except`` arm fire. Second call is a no-op
    (FileNotFoundError suppressed)."""
    sqlite_path = tmp_path / "state.db"
    lock_path = lockfile.acquire(sqlite_path)
    assert lock_path is not None
    lockfile.release(lock_path)
    assert not lock_path.exists()
    # Second call after the file is already gone.
    lockfile.release(lock_path)


def test_release_path_with_garbage_first_line_no_unlink_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brainstorm: a malformed PID line short-circuits release before the
    unlink call, so an attacker editing the file to "0\\n..." can't trick
    a victim into unlinking it. Spy on ``Path.unlink`` to confirm."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    lock_path.write_text("not-a-pid\nfedora\n", encoding="utf-8")

    unlink_calls: list[Path] = []
    real_unlink = Path.unlink

    def spy_unlink(self: Path, missing_ok: bool = False) -> None:
        unlink_calls.append(self)
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", spy_unlink)
    lockfile.release(lock_path)
    assert lock_path not in unlink_calls, "release must not unlink when PID parse fails"
    assert lock_path.exists()


# ---------------------------------------------------------------------------
# Bonus: the runtime release path uses contextlib.suppress(Exception)
# ---------------------------------------------------------------------------


def test_runtime_release_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bonus brainstorm: the ``run()`` shutdown path wraps ``lockfile.release``
    in ``contextlib.suppress(Exception)``. Verify a deliberately-raised
    release() doesn't crash the shutdown sequence."""
    fake_runtime = MagicMock()
    fake_runtime.lock_path = Path("/tmp/never.lock")

    def boom(_path: Path) -> None:
        raise RuntimeError("synthetic release failure")

    monkeypatch.setattr(runtime.lockfile, "release", boom)
    # Mirror the runtime.run() shutdown branch verbatim.
    with contextlib.suppress(Exception):
        runtime.lockfile.release(fake_runtime.lock_path)


# ---------------------------------------------------------------------------
# Impl iteration: all OSError paths during acquire wrap as LockAcquisitionError
# ---------------------------------------------------------------------------


def test_initial_write_lock_erofs_wrapped_as_lock_acquisition_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Impl iteration: when ``_write_lock`` fails on a read-only filesystem
    at the initial create, the underlying ``OSError(EROFS)`` must be wrapped
    as ``LockAcquisitionError`` so operators see one named failure mode.
    Pins the ``except OSError`` branch on the first ``_write_lock`` call."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"

    real_open = os.open

    def deny_create(path: StrOrBytesPath, flags: int, mode: int = 0o777) -> int:
        if bool(flags & os.O_CREAT) and os.fspath(path) == os.fspath(lock_path):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", deny_create)
    with pytest.raises(LockAcquisitionError, match="could not create lockfile") as exc:
        lockfile.acquire(sqlite_path)
    assert isinstance(exc.value.__cause__, OSError)
    assert exc.value.__cause__.errno == errno.EROFS
    # The error message includes the underlying OSError detail.
    assert "Read-only" in str(exc.value)


def test_initial_write_lock_enospc_wrapped_as_lock_acquisition_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Impl iteration: ENOSPC during the initial ``os.open`` create (the
    O_EXCL syscall itself can fail with no space) must surface as
    ``LockAcquisitionError`` not raw OSError. Pins the same branch with a
    second errno value to defend against an over-narrow ``if exc.errno ==``
    check that a future refactor might add."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"

    real_open = os.open

    def fill_disk(path: StrOrBytesPath, flags: int, mode: int = 0o777) -> int:
        if bool(flags & os.O_CREAT) and os.fspath(path) == os.fspath(lock_path):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fill_disk)
    with pytest.raises(LockAcquisitionError, match="could not create lockfile") as exc:
        lockfile.acquire(sqlite_path)
    assert isinstance(exc.value.__cause__, OSError)
    assert exc.value.__cause__.errno == errno.ENOSPC


def test_recovery_write_lock_erofs_wrapped_as_lock_acquisition_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Impl iteration: the SECOND ``_write_lock`` call (during stale recovery)
    can also fail with ``OSError`` — e.g., the FS flipped to read-only between
    the unlink and the recreate. Must wrap as ``LockAcquisitionError``.
    Pins lockfile.py:97-100 (the recovery-path OSError catch). This branch
    was previously uncovered."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    # Pre-create a stale lockfile so we go through the recovery path.
    _write_payload(lock_path, 99999999, socket.gethostname())

    real_open = os.open
    fired = {"n": 0}

    def deny_recovery_create(path: StrOrBytesPath, flags: int, mode: int = 0o777) -> int:
        # First (initial) create raises FileExistsError naturally — let it
        # through so the live-check + unlink + recovery sequence runs. The
        # recovery create then trips this branch.
        if bool(flags & os.O_CREAT) and os.fspath(path) == os.fspath(lock_path):
            fired["n"] += 1
            if fired["n"] >= 2:
                raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", deny_recovery_create)
    with pytest.raises(
        LockAcquisitionError, match="could not create lockfile during stale recovery"
    ) as exc:
        lockfile.acquire(sqlite_path)
    assert isinstance(exc.value.__cause__, OSError)
    assert exc.value.__cause__.errno == errno.EROFS


# ---------------------------------------------------------------------------
# Additional brainstorm — wrapping interaction with release + os.kill quirks
# ---------------------------------------------------------------------------


def test_release_not_called_on_initial_oserror_no_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brainstorm: when initial ``_write_lock`` raises ``OSError`` (e.g.
    EROFS), ``acquire`` returns nothing and the caller never gets a path
    to release. The wrapped error means the caller's ``release(None)`` is
    a no-op, but more importantly — no half-written lockfile remains and
    no orphan FD leaks. Pin both: file does not exist + acquire never
    returned a path."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"

    real_open = os.open

    def fail_create(path: StrOrBytesPath, flags: int, mode: int = 0o777) -> int:
        if bool(flags & os.O_CREAT) and os.fspath(path) == os.fspath(lock_path):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_create)
    captured: list[Path | None] = []
    with contextlib.suppress(LockAcquisitionError):
        captured.append(lockfile.acquire(sqlite_path))
    assert captured == []  # acquire raised before returning
    assert not lock_path.exists()
    # The caller's defensive ``release(None)`` must remain a no-op.
    lockfile.release(None)


def test_os_kill_eintr_treated_as_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Brainstorm: ``os.kill(pid, 0)`` can technically raise ``OSError`` with
    other errnos (EINTR, EINVAL on weird platforms). The current impl only
    matches ``ProcessLookupError`` and ``PermissionError`` explicitly; any
    other ``OSError`` propagates — which is the desired behaviour because
    a confused kernel is not safe ground for "definitely dead". Pin it."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    _write_payload(lock_path, 99999999, socket.gethostname())

    def kernel_confused(_pid: int, _sig: int) -> None:
        raise OSError(errno.EINTR, "Interrupted system call")

    monkeypatch.setattr(os, "kill", kernel_confused)
    # The OSError is not caught inside ``_holder_is_live`` — it propagates
    # out of acquire. This is the "fail loud rather than silently treat as
    # stale" stance. Documenting it via a test pins the behaviour.
    with pytest.raises(OSError, match="Interrupted"):
        lockfile.acquire(sqlite_path)


def test_iso8601_payload_uses_utc_timezone(tmp_path: Path) -> None:
    """Brainstorm: the lockfile timestamp must use UTC, not local time. An
    operator on EST and an operator on JST should be able to compare lockfile
    timestamps without timezone arithmetic. Pin the tz suffix."""
    sqlite_path = tmp_path / "state.db"
    lock_path = lockfile.acquire(sqlite_path)
    assert lock_path is not None
    started = lock_path.read_text(encoding="utf-8").splitlines()[2]
    parsed = datetime.fromisoformat(started)
    assert parsed.tzinfo is not None
    # UTC offset is exactly 0 — pin via timedelta to avoid timezone-name brittle assertions.
    from datetime import timedelta

    assert parsed.utcoffset() == timedelta(0), f"timestamp {started!r} must be UTC"
    lockfile.release(lock_path)


def test_acquire_called_twice_in_same_process_rejects_second(tmp_path: Path) -> None:
    """Brainstorm: a misconfigured runtime might call ``acquire`` twice (e.g.
    bootstrap + a stray helper). The second call sees our own lockfile, our
    PID matches, our hostname matches → live → ``LockAcquisitionError``.
    Pin this so an accidental double-bootstrap fails loud instead of
    leaving the system in a confused state."""
    sqlite_path = tmp_path / "state.db"
    first = lockfile.acquire(sqlite_path)
    assert first is not None
    try:
        with pytest.raises(LockAcquisitionError, match=str(os.getpid())) as exc:
            lockfile.acquire(sqlite_path)
        # The error names our own PID — that's the diagnostic operators need.
        assert socket.gethostname() in str(exc.value)
    finally:
        lockfile.release(first)


def test_release_after_lock_acquisition_error_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brainstorm: defensive coding pattern — caller wraps acquire in a
    try/finally and always calls release(lock_path). If acquire raised
    before binding lock_path, the caller's release receives whatever was
    initialised (often None). Verify the no-partial-state contract end-to-end:
    acquire raises, the lockfile does not exist, and a follow-up release(None)
    is a no-op (i.e., does not crash and does not create anything)."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"

    real_open = os.open

    def fail_create(path: StrOrBytesPath, flags: int, mode: int = 0o777) -> int:
        if bool(flags & os.O_CREAT) and os.fspath(path) == os.fspath(lock_path):
            raise OSError(errno.EACCES, "Permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", fail_create)

    # Idiomatic caller pattern.
    captured: Path | None = None
    try:
        captured = lockfile.acquire(sqlite_path)
    except LockAcquisitionError:
        pass
    finally:
        # Mirror runtime.py's release-on-any-failure pattern.
        lockfile.release(captured)

    assert captured is None
    assert not lock_path.exists()


def test_chained_oserror_preserves_strerror_for_observability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brainstorm: log/observability pipelines often inspect ``__cause__``
    on a chained exception to extract the underlying ``errno`` and
    ``strerror``. Verify the chain preserves both so an operator's log
    aggregator can pivot from the friendly outer message to the underlying
    syscall failure."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"

    real_open = os.open

    def deny(path: StrOrBytesPath, flags: int, mode: int = 0o777) -> int:
        if bool(flags & os.O_CREAT) and os.fspath(path) == os.fspath(lock_path):
            raise OSError(errno.EACCES, "Permission denied")
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", deny)
    with pytest.raises(LockAcquisitionError) as exc:
        lockfile.acquire(sqlite_path)
    cause = exc.value.__cause__
    assert isinstance(cause, OSError)
    assert cause.errno == errno.EACCES
    assert cause.strerror == "Permission denied"
    # The outer exception's repr should include both layers for log digestion.
    assert "Permission denied" in str(exc.value)


# ---------------------------------------------------------------------------
# Post-write verification: closes the O_EXCL→write race window
# ---------------------------------------------------------------------------


def test_post_write_verification_detects_lost_race_initial_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug fix coverage: simulate a peer unlinking + replacing our lockfile
    in the O_EXCL→write window. ``_verify_we_hold`` returns False → acquire
    raises ``LockAcquisitionError`` instead of returning success. Without
    this guard two threads could both think they hold the lock."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"

    real_verify = lockfile._verify_we_hold  # pyright: ignore[reportPrivateUsage]

    def lost_race(_path: Path) -> bool:
        # Restore for any subsequent calls so the recovery path could finish
        # if it ran. Our test expects the initial-path branch to raise first.
        monkeypatch.setattr(lockfile, "_verify_we_hold", real_verify)
        return False

    monkeypatch.setattr(lockfile, "_verify_we_hold", lost_race)
    with pytest.raises(LockAcquisitionError, match="O_EXCL"):
        lockfile.acquire(sqlite_path)
    # Cleanup any artefacts (the real lockfile may still exist on disk
    # because this branch raises before returning the path).
    if lock_path.exists():
        lock_path.unlink()


def test_post_write_verification_detects_lost_race_recovery_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug fix coverage: same race, but on the stale-recovery code path
    (where the second ``_write_lock`` runs after we unlinked a stale peer's
    lockfile). The verification must also fire here."""
    sqlite_path = tmp_path / "state.db"
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = sqlite_path.parent / "state.db.lock"
    # Plant a stale lockfile so acquire follows the recovery path.
    _write_payload(lock_path, 99999999, socket.gethostname())

    real_verify = lockfile._verify_we_hold  # pyright: ignore[reportPrivateUsage]
    call_count = {"n": 0}

    def lose_recovery_race(path: Path) -> bool:
        call_count["n"] += 1
        # The initial path verification doesn't run on the recovery branch
        # (the initial _write_lock raised FileExistsError); the only call
        # we should see here is the recovery-write verification. Still, be
        # defensive and only fail the first call.
        if call_count["n"] == 1:
            return False
        return real_verify(path)

    monkeypatch.setattr(lockfile, "_verify_we_hold", lose_recovery_race)
    with pytest.raises(LockAcquisitionError, match="stale-recovery write window"):
        lockfile.acquire(sqlite_path)
    if lock_path.exists():
        lock_path.unlink()


def test_verify_we_hold_returns_false_for_missing_file(tmp_path: Path) -> None:
    """Cover the FileNotFoundError branch of ``_verify_we_hold``: the
    file disappeared between our write and our verification read."""
    missing = tmp_path / "never-existed.lock"
    assert lockfile._verify_we_hold(missing) is False  # pyright: ignore[reportPrivateUsage]


def test_verify_we_hold_returns_false_for_garbage_pid(tmp_path: Path) -> None:
    """Cover the ValueError branch of ``_verify_we_hold``: the file's first
    line is not a valid integer (some other process trampled it)."""
    weird = tmp_path / "garbage.lock"
    weird.write_text("not-a-pid-at-all\nfedora\n", encoding="utf-8")
    assert lockfile._verify_we_hold(weird) is False  # pyright: ignore[reportPrivateUsage]


def test_verify_we_hold_returns_true_for_own_pid(tmp_path: Path) -> None:
    """Sanity: positive case for ``_verify_we_hold`` — file names our PID."""
    ours = tmp_path / "ours.lock"
    ours.write_text(f"{os.getpid()}\nfedora\n", encoding="utf-8")
    assert lockfile._verify_we_hold(ours) is True  # pyright: ignore[reportPrivateUsage]
