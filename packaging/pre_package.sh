#!/usr/bin/env bash
set -euo pipefail

python3 - "$APPDIR" <<'PY'
from pathlib import Path
import sys

appdir = Path(sys.argv[1])
for template in appdir.rglob("*.tmpl.py"):
    template.unlink()
PY
