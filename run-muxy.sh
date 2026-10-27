#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="${MUXY_PYTHON:-$project_root/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
    printf 'Muxy Python environment not found at %s\n' "$python_bin" >&2
    printf 'Follow the setup steps in README.md, or set MUXY_PYTHON to a Python environment with Muxy installed.\n' >&2
    exit 1
fi
export PYTHONPATH="$project_root/src"
exec "$python_bin" -m video_editor.app "$@"
