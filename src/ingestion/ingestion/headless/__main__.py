"""Run the headless auth server: uv run python -m ingestion.headless"""

from __future__ import annotations

import logging

import uvicorn

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

from ingestion.headless.api import app  # noqa: E402

uvicorn.run(app, host="0.0.0.0", port=8000)
