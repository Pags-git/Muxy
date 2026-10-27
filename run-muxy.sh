#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python_bin="/home/mikepaglia/Software/Development/video/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
    printf 'Could not find the Python environment at %s\n' "$python_bin" >&2
    exit 1
fi
export PYTHONPATH="$project_root/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$python_bin" -m video_editor.app "$@"
