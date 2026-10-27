# Muxy

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

Clone the public Muxy desktop branch and enter the project directory:

```bash
git clone --branch muxy-desktop-10.5.0.5 https://github.com/Pags-git/Muxy.git muxy
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
- Select video, audio, and subtitle streams and configure export options.
- Save, load, and update presets spanning the editing tabs.
- Remux compatible streams or re-encode with available FFmpeg encoders.
- Export MP4/MOV with web-optimized faststart when supported.

