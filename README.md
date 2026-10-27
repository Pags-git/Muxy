# Muxy (Video-1 working copy)

This is a separate working copy of Muxy. Its source is in `src/video_editor`; it shares the existing Python environment from `/home/mikepaglia/Software/Development/video/.venv` without changing the original `Video` project.

## Run with the existing environment

```bash
./run-muxy.sh
```

The launcher loads code from this `Video-1` folder and dependencies from the existing `Video/.venv` environment. Edit files in `Video-1/src/video_editor/` to continue development here.

The first **Summary** tab displays the loaded file's container and file details, video codec and frame information, plus its audio and subtitle tracks. In **Export**, **Web optimized (faststart)** moves the MP4/MOV index to the beginning of the output so playback can start before the full file downloads. FFmpeg performs a second pass for this operation.

Selecting video passthrough automatically selects audio passthrough too, so changing containers defaults to remuxing instead of encoding. Audio can still be changed to AAC separately on the Audio tab.

To create an independent environment instead, follow the setup instructions in `pyproject.toml` with Python 3.12 or newer and install the project into a new `.venv` in this folder.

## Build a Linux AppImage

The packaging script can use the shared environment by setting `MUXY_PYTHON`:

```bash
MUXY_PYTHON=/home/mikepaglia/Software/Development/video/.venv/bin/python ./packaging/build-appimage.sh
```

The packaging script downloads FFmpeg and verifies its checksum before packaging.
