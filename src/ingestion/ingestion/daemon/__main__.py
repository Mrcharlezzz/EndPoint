"""Entry point for ``python -m ingestion.daemon``.

The daemon is the long-running service; the CLI in ``ingestion.cli`` is
invoked separately for ad-hoc syncs and debugging.
"""

from __future__ import annotations

from ingestion.daemon.runtime import main

if __name__ == "__main__":
    main()
