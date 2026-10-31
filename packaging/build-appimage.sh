#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="$project_root/build/ffmpeg"
archive="$build_dir/ffmpeg-static.tar.xz"
asset_url="https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n9.0-latest-linux64-gpl-9.0.tar.xz"
asset_sha256="844421ab0f5a2ae227a0d7a5e43fd12bb18270b4fd5b26b3085f09260c34744f"

if [[ ! -x "$build_dir/bin/ffmpeg" || ! -x "$build_dir/bin/ffprobe" || ! -f "$build_dir/LICENSE.txt" ]]; then
    mkdir -p "$build_dir"
    curl -fL --retry 2 "$asset_url" -o "$archive"
    printf '%s  %s\n' "$asset_sha256" "$archive" | sha256sum --check --status
    tar -xJf "$archive" -C "$build_dir" --strip-components=1 \
        ffmpeg-n9.0-latest-linux64-gpl-9.0/bin/ffmpeg \
        ffmpeg-n9.0-latest-linux64-gpl-9.0/bin/ffprobe \
        ffmpeg-n9.0-latest-linux64-gpl-9.0/LICENSE.txt
fi

chmod +x "$build_dir/bin/ffmpeg" "$build_dir/bin/ffprobe"
cd "$project_root"
python_bin="${MUXY_PYTHON:-$project_root/.venv/bin/python}"
"$python_bin" -m appimage.ctl build
