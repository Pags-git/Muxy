# Muxy

Current desktop version: **10.6.0.8** (Python 3.12 / PySide6).

Muxy is a desktop video editor and converter for Linux. It uses FFmpeg for video processing and PySide6 for its graphical interface.

## Requirements

- Python 3.12 or later
- FFmpeg and FFprobe available on your `PATH`
- Git

Check that both FFmpeg tools are available before setup:

```bash
ffmpeg -version
ffprobe -version
```

Install FFmpeg with your Linux distribution's package manager if either command is missing. Muxy uses the system FFmpeg build, so available encoders depend on how that build was packaged.

## Download and run

Clone the current desktop source and enter the project directory:

```bash
git clone https://github.com/Pags-git/Muxy.git muxy
cd muxy
```

Create a virtual environment, install Muxy and its Python dependencies, then launch it:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install .
./run-muxy.sh
```

The launcher uses the `.venv` inside the cloned folder. To use another Python environment, set `MUXY_PYTHON` when launching:

```bash
MUXY_PYTHON=/path/to/python ./run-muxy.sh
```

## Build a Linux AppImage

Install the optional packaging dependency and run the build script:

```bash
.venv/bin/python -m pip install '.[packaging]'
MUXY_PYTHON="$PWD/.venv/bin/python" ./packaging/build-appimage.sh
```

The build script downloads the FFmpeg bundle it needs and checks its SHA-256 checksum before packaging.

## Main features

- Inspect container, codecs, frame rate, colour information, and tracks in the Summary tab.
- Adjust picture levels and preview changes, including a split before-and-after view.
- Choose an export trim range by dragging the start and end handles below the preview timeline, then preview that section before encoding.
- Select video, audio, and subtitle streams and configure export options.
- Save, load, and update presets spanning the editing tabs.
- Remux compatible streams or re-encode with available FFmpeg encoders.
- Export MP4/MOV with web-optimized faststart when supported.


## AppImage downloads

Packaged Linux builds are distributed through [GitHub Releases](https://github.com/Pags-git/Muxy/releases). Download the AppImage and checksum from the matching release. Make the AppImage executable, then launch it. If FUSE is unavailable, use `--appimage-extract-and-run`.

The current source is under `src/video_editor/`; the older single-file PyQt6 implementation remains available in Git history. Earlier desktop branches are retained for reference. Build output, virtual environments, backups, snapshots and local media are excluded from Git.

The Linux AppImage bundles Python, PySide6 and FFmpeg/FFprobe. The host still provides baseline system libraries, display and graphics/audio services. Builds are for Linux x86_64; other platforms and Linux distributions require testing. FFmpeg provenance and license details are included by the packaging scripts.
