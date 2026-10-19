from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_SH = REPO_ROOT / "scripts" / "report.sh"


def test_report_sh_delegates_to_report_py(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_log = tmp_path / "uv.log"
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "{uv_log}"
""",
        encoding="utf-8",
    )
    fake_uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = subprocess.run(
        ["bash", str(REPORT_SH), "data/runs/example", "--time-window-days", "60"],
        check=False,
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
    )

    assert result.returncode == 0
    uv_call = uv_log.read_text(encoding="utf-8")
    assert "run python" in uv_call
    assert "scripts/report.py data/runs/example --time-window-days 60" in uv_call
