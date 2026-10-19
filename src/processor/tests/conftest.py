import sys
from pathlib import Path

import core.bootstrap

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

core.bootstrap.init()
