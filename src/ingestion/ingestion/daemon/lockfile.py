"""PID lockfile guarding the daemon's SQLite state from a second instance.

Two daemons sharing one ``state.db`` race on cursor writes, dead-letter
counters, and ``force_sync_requests`` consumption. ``O_EXCL`` create plus
a stored ``<pid>\\n<hostname>\\n<iso8601>`` payload lets a stale lockfile
from a crashed predecessor recover automatically while a live sibling on
the same host aborts startup.

After writing the payload ``acquire`` always re-reads the file and
asserts the PID matches our own. The verification closes a same-host
race window between ``O_EXCL`` create and ``os.write`` where a peer
could have observed the empty file as "malformed", treated it as stale,
unlinked it, and recreated under its own PID. Without the post-write
check both peers would return success and both would believe they hold
the lock.
"""

from __future__ import annotations

import contextlib
import os
import socket
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_MODE = 0o600


class LockAcquisitionError(RuntimeError):
    """Raised when a live peer holds the lock or a race blocks recovery."""


def _is_in_memory(sqlite_path: Path) -> bool:
    raw = str(sqlite_path)
    return raw == ":memory:" or raw.startswith("file::memory:")


def _write_lock(path: Path, hostname: str) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _MODE)
    try:
        payload = f"{os.getpid()}\n{hostname}\n{datetime.now(tz=UTC).isoformat()}\n"
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)


def _verify_we_hold(path: Path) -> bool:
    """Confirm the file at ``path`` still names our PID.

    Closes the empty-file race: if a concurrent peer observed our
    ``O_EXCL``-created file before ``os.write`` populated it, the peer
    may have treated it as malformed, unlinked it, and recreated it
    under its own PID. Without this check both threads would return
    successfully from ``acquire``. The check is on the public lockfile
    contents; a TOCTOU window remains (another peer could replace it
    immediately after we read), but a peer that observed our PID has
    correctly aborted, so winning here is equivalent to "no other peer
    is currently inside acquire()" — the strongest guarantee a PID
    lockfile can offer.
    """
    try:
        head = path.read_text(encoding="utf-8").split("\n", 1)[0]
    except FileNotFoundError:
        return False
    try:
        return int(head) == os.getpid()
    except ValueError:
        return False


def _holder_is_live(path: Path, hostname: str) -> tuple[bool, str]:
    try:
        parts = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return False, "lockfile vanished during inspection"
    if len(parts) < 2:
        return False, f"malformed lockfile (lines={len(parts)})"
    try:
        pid = int(parts[0])
    except ValueError:
        return False, f"malformed pid: {parts[0]!r}"
    holder_host, started = parts[1], parts[2] if len(parts) >= 3 else "unknown"
    if pid <= 0 or holder_host != hostname:
        return False, f"foreign or invalid holder (pid={pid}, host={holder_host!r})"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False, f"pid {pid} no longer running"
    except PermissionError:
        return True, f"pid {pid} alive (cannot verify, treating as live)"
    return True, f"pid {pid} on {holder_host} (started {started})"


def acquire(sqlite_path: Path) -> Path | None:
    """Acquire the lockfile. Returns its path, or None for in-memory SQLite."""
    if _is_in_memory(sqlite_path):
        return None
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    path = sqlite_path.parent / f"{sqlite_path.name}.lock"
    hostname = socket.gethostname()
    try:
        _write_lock(path, hostname)
        # Re-read to make sure a peer didn't unlink+replace us during the
        # O_EXCL→write window. If verification fails we lost the race —
        # raise so the caller doesn't double-occupy the SQLite file.
        if not _verify_we_hold(path):
            raise LockAcquisitionError("another daemon claimed lock during O_EXCL→write window")
        return path
    except FileExistsError:
        pass
    except OSError as exc:
        # ENOSPC, EROFS, EACCES on the create syscall etc. — wrap so the
        # operator sees a single ``LockAcquisitionError`` envelope instead
        # of a raw OSError leaking out of acquire.
        raise LockAcquisitionError(f"could not create lockfile {path}: {exc}") from exc
    live, detail = _holder_is_live(path, hostname)
    if live:
        raise LockAcquisitionError(f"another daemon is running: {detail}")
    try:
        path.unlink()
    except FileNotFoundError:
        # Stale lockfile vanished between inspection and unlink — fine,
        # the recovery ``_write_lock`` below races for it normally.
        pass
    except OSError as exc:
        raise LockAcquisitionError(f"could not remove stale lockfile {path}: {exc}") from exc
    try:
        _write_lock(path, hostname)
    except FileExistsError as exc:
        raise LockAcquisitionError("another daemon claimed lock during stale recovery") from exc
    except OSError as exc:
        raise LockAcquisitionError(
            f"could not create lockfile during stale recovery {path}: {exc}"
        ) from exc
    if not _verify_we_hold(path):
        raise LockAcquisitionError("another daemon claimed lock during stale-recovery write window")
    return path


def release(path: Path | None) -> None:
    """Remove the lockfile only if it still names our PID."""
    if path is None:
        return
    try:
        head = path.read_text(encoding="utf-8").split("\n", 1)[0]
    except FileNotFoundError:
        return
    try:
        if int(head) != os.getpid():
            return
    except ValueError:
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
