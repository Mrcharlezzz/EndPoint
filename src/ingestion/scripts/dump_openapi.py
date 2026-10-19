"""Dump the daemon's OpenAPI schema as JSON.

Usage:
    uv run python scripts/dump_openapi.py [--out PATH]

The dashboard calls this via ``pnpm codegen`` to regenerate its typed
client after any backend API change.

The script constructs a temporary DaemonConfig + SQLite + cipher, builds
the FastAPI app, and serialises ``app.openapi()``. No network bind, no
admin password required beyond a placeholder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write schema to this path (default: stdout).",
    )
    args = parser.parse_args()

    # Placeholder secrets so create_app doesn't refuse to boot.
    os.environ.setdefault("ENDPOINT_FERNET_KEY", Fernet.generate_key().decode())
    os.environ.setdefault("ENDPOINT_ADMIN_PASSWORD", "codegen-placeholder")
    os.environ.setdefault(
        "ENDPOINT_SESSION_SECRET",
        "codegen-placeholder-session-secret-0123456789",
    )

    from core.bootstrap import init

    init()

    from ingestion.daemon.api import create_app
    from ingestion.daemon.config import DaemonConfig
    from ingestion.daemon.crypto import CredentialCipher
    from ingestion.daemon.store.local import open_store

    with tempfile.TemporaryDirectory() as tmp:
        config = DaemonConfig(sqlite_path=Path(tmp) / "codegen.sqlite")
        cipher = CredentialCipher.from_env()
        store = open_store(config.sqlite_path)
        try:
            app = create_app(config=config, store=store, cipher=cipher)
            schema = app.openapi()
        finally:
            store.close()

    payload = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload, encoding="utf-8")
    else:
        sys.stdout.write(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
