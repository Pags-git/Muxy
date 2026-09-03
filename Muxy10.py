#!/usr/bin/env python3
"""
MKV <-> MP4 Converter (single-file build)

A dark-mode PyQt6 app for converting between .mkv and .mp4, with a random
frame preview and live brightness/contrast/saturation adjustment.

Requirements:
    - ffmpeg / ffprobe on PATH
    - pip install PyQt6 Pillow

Run:
    python3 main.py
"""

import os
import re
import sys
import json
import random
import shutil
import platform
import tempfile
import subprocess
from collections import deque
from functools import lru_cache

from PIL import Image, ImageEnhance, ImageFilter
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QFileDialog, QFrame, QProgressBar, QMessageBox,
    QStatusBar, QSlider, QSizePolicy, QListWidget, QListWidgetItem,
    QAbstractItemView, QTabWidget, QComboBox, QDialog, QDialogButtonBox,
    QScrollArea
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QImage, QPixmap


# On Windows, subprocess calls spawn their own console window by default -
# even with --noconsole applied to the main app. This flag suppresses that
# for ffmpeg/ffprobe calls. It's a no-op (0) on Linux/Mac.
if platform.system() == "Windows":
    SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    SUBPROCESS_FLAGS = 0


def _app_directory() -> str:
    """Folder the running app lives in - the frozen .exe's folder when
    built with PyInstaller, otherwise the folder this script is in."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


@lru_cache(maxsize=None)
def resolve_executable(name: str) -> str:
    """
    Find the full path to an ffmpeg/ffprobe executable.

    Looks in this order:
      1. Next to the running app (same folder as the .exe, or this
         script when run directly) - lets ffmpeg.exe/ffprobe.exe be
         bundled or dropped alongside the app without touching PATH.
      2. The system PATH.

    Raises MediaProbeError with a clear message if neither has it.
    """
    exe_name = f"{name}.exe" if platform.system() == "Windows" else name
    app_dir = _app_directory()

    local_path = os.path.join(app_dir, exe_name)
    if os.path.isfile(local_path):
        return local_path

    found_on_path = shutil.which(name)
    if found_on_path:
        return found_on_path

    raise MediaProbeError(
        f"Could not find '{exe_name}'. Checked the app folder "
        f"({app_dir}) and your system PATH. Install ffmpeg, or place "
        f"'{exe_name}' in the same folder as this app."
    )


# =============================================================================
# Dark theme stylesheet
# =============================================================================

ACCENT = "#3ba7ff"
ACCENT_HOVER = "#5cb8ff"
BG_DARK = "#1e1f22"
BG_PANEL = "#26282c"
BG_INPUT = "#2f3136"
BORDER = "#3a3c42"
TEXT = "#e6e6e6"
TEXT_DIM = "#9a9ca1"
DANGER = "#e05a5a"

DARK_STYLESHEET = f"""
QWidget {{
    background-color: {BG_DARK};
    color: {TEXT};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 13px;
}}
QMainWindow {{ background-color: {BG_DARK}; }}
QFrame#panel {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QLabel {{ color: {TEXT}; }}
QLabel#heading {{ font-size: 15px; font-weight: 600; color: {TEXT}; }}
QLabel#dim {{ color: {TEXT_DIM}; font-size: 12px; }}
QLineEdit {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 6px 8px;
    color: {TEXT};
}}
QLineEdit:focus {{ border: 1px solid {ACCENT}; }}
QPushButton {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 8px 16px;
    color: {TEXT};
}}
QPushButton:hover {{ background-color: #383b40; border: 1px solid {ACCENT}; }}
QPushButton:disabled {{ color: {TEXT_DIM}; background-color: {BG_INPUT}; border: 1px solid {BORDER}; }}
QPushButton#primary {{ background-color: {ACCENT}; color: #0d0e10; font-weight: 600; border: none; }}
QPushButton#primary:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#primary:disabled {{ background-color: #2c3e4d; color: #6b7580; }}
QPushButton#danger {{ background-color: transparent; color: {DANGER}; border: 1px solid {DANGER}; }}
QPushButton#danger:hover {{ background-color: {DANGER}; color: #0d0e10; }}
QPushButton#danger:disabled {{ color: {TEXT_DIM}; border: 1px solid {BORDER}; }}
QPushButton#toggle {{ background-color: {BG_INPUT}; border: 1px solid {BORDER}; border-radius: 6px; padding: 8px 16px; }}
QPushButton#toggle:checked {{ background-color: {ACCENT}; color: #0d0e10; font-weight: 600; border: none; }}
QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {ACCENT}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px; }}
QSlider::handle:horizontal:disabled {{ background: {TEXT_DIM}; }}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 2px; }}
QSlider::sub-page:horizontal:disabled {{ background: {BORDER}; }}
QProgressBar {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 6px;
    text-align: center;
    color: {TEXT};
    height: 20px;
}}
QProgressBar::chunk {{ background-color: {ACCENT}; border-radius: 5px; }}
QStatusBar {{ background-color: {BG_PANEL}; color: {TEXT_DIM}; }}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    top: -1px;
}}
QTabBar::tab {{
    background-color: {BG_INPUT};
    color: {TEXT_DIM};
    padding: 6px 14px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}}
QTabBar::tab:selected {{
    background-color: {BG_PANEL};
    color: {TEXT};
    border-bottom: 1px solid {BG_PANEL};
}}
QListWidget {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 5px;
}}
QListView::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background-color: {BG_INPUT};
}}
QListView::indicator:checked {{
    background-color: {DANGER};
    border: 1px solid {DANGER};
}}
QComboBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 6px 8px;
}}
QComboBox:hover {{
    border: 1px solid {ACCENT};
}}
QComboBox:disabled {{
    color: {TEXT_DIM};
}}
QComboBox::drop-down {{
    border: none;
}}
QComboBox QAbstractItemView {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    selection-color: #0d0e10;
    color: {TEXT};
}}
"""


# =============================================================================
# Frame extraction / adjustment utilities
# =============================================================================

class MediaProbeError(Exception):
    pass


def probe_duration(path: str) -> float:
    """Return the duration of a media file in seconds using ffprobe."""
    ffprobe_path = resolve_executable("ffprobe")
    cmd = [
        ffprobe_path, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                 creationflags=SUBPROCESS_FLAGS)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise MediaProbeError(f"Could not read file info: {exc}") from exc

    data = json.loads(result.stdout)
    duration = data.get("format", {}).get("duration")
    if duration is None:
        raise MediaProbeError("Duration not found in file metadata.")
    return float(duration)


def probe_subtitle_streams(path: str) -> list:
    """
    Return a list of subtitle tracks in the file, in the same order ffmpeg
    uses for its 's:N' stream specifiers. Each entry is a dict with keys:
    relative_index, codec, language, title, label.
    """
    ffprobe_path = resolve_executable("ffprobe")
    cmd = [
        ffprobe_path, "-v", "error",
        "-select_streams", "s",
        "-show_entries", "stream=codec_name:stream_tags=language,title",
        "-of", "json", path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                 creationflags=SUBPROCESS_FLAGS)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise MediaProbeError(f"Could not read subtitle streams: {exc}") from exc

    data = json.loads(result.stdout)
    streams = data.get("streams", [])

    tracks = []
    for i, stream in enumerate(streams):
        tags = stream.get("tags", {})
        language = tags.get("language", "und")
        title = tags.get("title", "")
        codec = stream.get("codec_name", "unknown")

        label = f"Track {i}: {language}"
        if title:
            label += f" - {title}"
        label += f" ({codec})"

        tracks.append({
            "relative_index": i,
            "codec": codec,
            "language": language,
            "title": title,
            "label": label,
        })
    return tracks


def probe_audio_streams(path: str) -> list:
    """
    Return a list of audio tracks in the file, in the same order ffmpeg
    uses for its 'a:N' stream specifiers. Each entry is a dict with keys:
    relative_index, codec, language, channels, label.
    """
    ffprobe_path = resolve_executable("ffprobe")
    cmd = [
        ffprobe_path, "-v", "error",
        "-select_streams", "a",
        "-show_entries", "stream=codec_name,channels:stream_tags=language,title",
        "-of", "json", path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True,
                                 creationflags=SUBPROCESS_FLAGS)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        raise MediaProbeError(f"Could not read audio streams: {exc}") from exc

    data = json.loads(result.stdout)
    streams = data.get("streams", [])

    tracks = []
    for i, stream in enumerate(streams):
        tags = stream.get("tags", {})
        language = tags.get("language", "und")
        title = tags.get("title", "")
        codec = stream.get("codec_name", "unknown")
        channels = stream.get("channels", "?")

        label = f"Track {i}: {language} - {codec}, {channels}ch"
        if title:
            label += f" ({title})"

        tracks.append({
            "relative_index": i,
            "codec": codec,
            "language": language,
            "channels": channels,
            "label": label,
        })
    return tracks


def audio_bitrate_for_channels(channels) -> int:
    """
    Pick a sensible AAC bitrate (kbps) based on channel count, so
    multichannel audio (5.1/7.1) gets enough headroom rather than being
    squeezed into a bitrate sized for stereo.
    """
    try:
        channels = int(channels)
    except (TypeError, ValueError):
        return 192

    if channels <= 2:
        return 192
    elif channels <= 6:
        return 384
    else:
        return 512


def extract_random_frame(path: str) -> Image.Image:
    """Extract a single random frame from the video as a Pillow Image."""
    duration = probe_duration(path)
    safe_start = min(1.0, duration * 0.05)
    safe_end = max(safe_start + 0.1, duration - 1.0)
    timestamp = random.uniform(safe_start, safe_end)

    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = os.path.join(tmpdir, "frame.jpg")
        ffmpeg_path = resolve_executable("ffmpeg")
        cmd = [
            ffmpeg_path, "-y",
            "-ss", f"{timestamp:.3f}",
            "-i", path,
            "-frames:v", "1",
            "-q:v", "2",
            out_path,
        ]
        try:
            subprocess.run(cmd, capture_output=True, check=True,
                            creationflags=SUBPROCESS_FLAGS)
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise MediaProbeError(f"Could not extract preview frame: {exc}") from exc

        if not os.path.exists(out_path):
            raise MediaProbeError("Frame extraction produced no output.")

        image = Image.open(out_path)
        image.load()
        return image.convert("RGB")


def apply_adjustments(image, brightness=0.0, contrast=0.0, saturation=0.0,
                       denoise=0.0, sharpen=0.0):
    """
    Apply brightness/contrast/saturation (-100..100, 0 = no change), plus
    denoise and sharpen (0..100, 0 = no change). This is a Pillow-based
    approximation used only for the live preview - the actual conversion
    uses ffmpeg's own filters for full quality.
    """
    def to_factor(value):
        return 1.0 + (value / 100.0)

    result = image
    if brightness != 0.0:
        result = ImageEnhance.Brightness(result).enhance(to_factor(brightness))
    if contrast != 0.0:
        result = ImageEnhance.Contrast(result).enhance(to_factor(contrast))
    if saturation != 0.0:
        result = ImageEnhance.Color(result).enhance(to_factor(saturation))
    if denoise > 0.0:
        radius = (denoise / 100.0) * 3.0
        result = result.filter(ImageFilter.GaussianBlur(radius=radius))
    if sharpen > 0.0:
        percent = int((sharpen / 100.0) * 150)
        result = result.filter(ImageFilter.UnsharpMask(radius=2, percent=percent, threshold=3))
    return result


def eq_filter_string(brightness: float, contrast: float, saturation: float) -> str:
    """Build an ffmpeg `eq` filter string from -100..100 slider values."""
    ff_brightness = brightness / 100.0
    ff_contrast = 1.0 + (contrast / 100.0)
    ff_saturation = 1.0 + (saturation / 100.0)
    return f"eq=brightness={ff_brightness:.3f}:contrast={ff_contrast:.3f}:saturation={ff_saturation:.3f}"


def denoise_filter_string(denoise: float) -> str:
    """Build an ffmpeg `hqdn3d` filter string from a 0..100 slider value."""
    amount = denoise / 100.0
    luma_spatial = amount * 8.0
    chroma_spatial = amount * 6.0
    luma_tmp = amount * 6.0
    chroma_tmp = amount * 4.0
    return f"hqdn3d={luma_spatial:.2f}:{chroma_spatial:.2f}:{luma_tmp:.2f}:{chroma_tmp:.2f}"


def sharpen_filter_string(sharpen: float) -> str:
    """Build an ffmpeg `unsharp` filter string from a 0..100 slider value."""
    amount = (sharpen / 100.0) * 2.0  # unsharp amount range: -2..2, 0 = no change
    return f"unsharp=5:5:{amount:.2f}:5:5:{amount:.2f}"


def pil_to_pixmap(image) -> QPixmap:
    """Convert a Pillow RGB image into a QPixmap for display."""
    image = image.convert("RGB")
    data = image.tobytes("raw", "RGB")
    qimage = QImage(data, image.width, image.height, image.width * 3,
                     QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimage)


# =============================================================================
# Background conversion worker
# =============================================================================

TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+)\.(\d+)")
SPEED_RE = re.compile(r"speed=\s*([\d.]+)x")


def format_eta(seconds: float) -> str:
    """Format a seconds count as a short human-readable ETA string."""
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    if minutes > 0:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


class ConversionWorker(QThread):
    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    eta_updated = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    failed = pyqtSignal(str)
    canceled = pyqtSignal()

    def __init__(self, input_path, output_path, remux_only,
                 brightness=0.0, contrast=0.0, saturation=0.0,
                 denoise=0.0, sharpen=0.0, remove_subtitle_indices=None,
                 convert_audio_aac=False, rf=22, parent=None):
        super().__init__(parent)
        self.input_path = input_path
        self.output_path = output_path
        self.remux_only = remux_only
        self.brightness = brightness
        self.contrast = contrast
        self.saturation = saturation
        self.denoise = denoise
        self.sharpen = sharpen
        self.remove_subtitle_indices = remove_subtitle_indices or []
        self.convert_audio_aac = convert_audio_aac
        self.rf = rf

        self._process = None
        self._cancel_requested = False

    def build_command(self) -> list:
        ffmpeg_path = resolve_executable("ffmpeg")
        cmd = [ffmpeg_path, "-y", "-i", self.input_path]

        # Map every stream explicitly so we can selectively drop specific
        # subtitle tracks while keeping everything else intact.
        cmd += ["-map", "0"]
        for idx in sorted(self.remove_subtitle_indices):
            cmd += ["-map", f"-0:s:{idx}"]

        if self.remux_only:
            cmd += ["-c", "copy"]
            if self.convert_audio_aac:
                # Everything else stays a straight stream copy; only audio
                # gets transcoded - much faster than a full re-encode, and
                # fixes MP4-incompatible audio codecs (DTS, TrueHD, FLAC...).
                cmd += ["-c:a", "aac"]
                cmd += self._audio_bitrate_args()
        else:
            filter_chain = []

            # Denoise runs before sharpen so sharpening doesn't amplify grain.
            if self.denoise > 0:
                filter_chain.append(denoise_filter_string(self.denoise))
            if any([self.brightness, self.contrast, self.saturation]):
                filter_chain.append(eq_filter_string(self.brightness, self.contrast, self.saturation))
            if self.sharpen > 0:
                filter_chain.append(sharpen_filter_string(self.sharpen))

            if filter_chain:
                cmd += ["-vf", ",".join(filter_chain)]
            cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", str(self.rf)]
            # Full Convert always re-encodes audio to AAC regardless of the
            # Audio tab choice, since everything is being re-encoded anyway.
            cmd += ["-c:a", "aac"]
            cmd += self._audio_bitrate_args()

            # Subtitle streams need an explicit codec here too - without one,
            # ffmpeg tries to pick a default that MP4 often can't hold (e.g.
            # ASS/SSA), and fails outright instead of just dropping them.
            output_ext = os.path.splitext(self.output_path)[1].lower()
            if output_ext == ".mp4":
                # MP4 only supports text-based subtitles; mov_text converts
                # SRT/ASS text subs. Bitmap formats (PGS, VobSub) can't be
                # converted this way and will still fail - remove those
                # tracks in the Subtitles tab first if you hit that.
                cmd += ["-c:s", "mov_text"]
            else:
                # MKV can hold subtitle streams in their original codec.
                cmd += ["-c:s", "copy"]

        cmd += [self.output_path]
        return cmd

    def _audio_bitrate_args(self) -> list:
        """
        Return per-stream '-b:a:N' flags scaled to each audio track's
        channel count (stereo vs 5.1 vs 7.1), so multichannel audio isn't
        squeezed into a bitrate sized for stereo. Falls back to a flat
        192k for every track if the audio streams can't be probed.
        """
        try:
            tracks = probe_audio_streams(self.input_path)
        except MediaProbeError:
            return ["-b:a", "192k"]

        if not tracks:
            return ["-b:a", "192k"]

        args = []
        for track in tracks:
            idx = track["relative_index"]
            bitrate = audio_bitrate_for_channels(track.get("channels"))
            args += [f"-b:a:{idx}", f"{bitrate}k"]
        return args

    def cancel(self):
        self._cancel_requested = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def _cleanup_partial_output(self):
        """Best-effort removal of a partial output file after a cancel or
        failure, so a half-written file doesn't get mistaken for a real
        previous output on the next run."""
        try:
            if os.path.exists(self.output_path):
                os.remove(self.output_path)
        except OSError:
            pass  # not critical - leave it if it can't be removed

    def run(self):
        try:
            duration = probe_duration(self.input_path)
        except MediaProbeError as exc:
            self.failed.emit(str(exc))
            return

        try:
            cmd = self.build_command()
        except MediaProbeError as exc:
            self.failed.emit(str(exc))
            return

        self.status.emit("Converting..." if not self.remux_only else "Remuxing...")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                bufsize=1,
                creationflags=SUBPROCESS_FLAGS,
            )
        except FileNotFoundError:
            self.failed.emit("ffmpeg was not found.")
            return

        # Keep the last handful of output lines so a failure can show
        # ffmpeg's actual error text, not just a bare exit code.
        recent_lines = deque(maxlen=15)

        for line in self._process.stdout:
            if self._cancel_requested:
                break

            stripped = line.strip()
            if stripped:
                recent_lines.append(stripped)

            match = TIME_RE.search(line)
            if match and duration > 0:
                hours, minutes, seconds, hundredths = match.groups()
                elapsed = (
                    int(hours) * 3600
                    + int(minutes) * 60
                    + int(seconds)
                    + int(hundredths) / 100.0
                )
                percent = max(0, min(100, int((elapsed / duration) * 100)))
                self.progress.emit(percent)

                speed_match = SPEED_RE.search(line)
                if speed_match:
                    speed = float(speed_match.group(1))
                    if speed > 0:
                        remaining = max(0.0, duration - elapsed)
                        self.eta_updated.emit(format_eta(remaining / speed))

        self._process.wait()

        if self._cancel_requested:
            self._cleanup_partial_output()
            self.canceled.emit()
            return

        if self._process.returncode == 0:
            self.progress.emit(100)
            self.finished_ok.emit(self.output_path)
        else:
            self._cleanup_partial_output()
            detail = "\n".join(recent_lines) if recent_lines else "(no output captured)"
            self.failed.emit(
                f"ffmpeg exited with code {self._process.returncode}:\n{detail}"
            )


# =============================================================================
# Preview widget (frame image + sliders)
# =============================================================================

class SliderRow(QWidget):
    """A labeled slider with a live value readout."""

    valueChanged = pyqtSignal(int)

    def __init__(self, label_text: str, min_val: int = -100, max_val: int = 100, parent=None):
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QHBoxLayout()
        self.label = QLabel(label_text)
        self.value_label = QLabel("0")
        self.value_label.setObjectName("dim")
        self.value_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        header.addWidget(self.label)
        header.addStretch()
        header.addWidget(self.value_label)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(min_val, max_val)
        self.slider.setValue(0)
        self.slider.valueChanged.connect(self._on_change)

        layout.addLayout(header)
        layout.addWidget(self.slider)

    def _on_change(self, value: int):
        self.value_label.setText(str(value))
        self.valueChanged.emit(value)

    def value(self) -> int:
        return self.slider.value()

    def reset(self):
        self.slider.setValue(0)
        self.value_label.setText("0")

    def set_enabled(self, enabled: bool):
        self.slider.setEnabled(enabled)


class _PopoutPreviewDialog(QDialog):
    """A plain floating window that hosts just the preview image while
    it's popped out. Notifies the owner on resize (to rescale the image)
    and on close (so the image can be docked back into the main window)."""

    def __init__(self, on_resize, on_close, parent=None):
        super().__init__(parent)
        self._on_resize = on_resize
        self._on_close = on_close
        self.setWindowTitle("Preview")
        self.resize(640, 480)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._on_resize:
            self._on_resize()

    def closeEvent(self, event):
        super().closeEvent(event)
        if self._on_close:
            self._on_close()


class PreviewWidget(QFrame):
    """Frame preview image plus adjustment sliders."""

    adjustmentsChanged = pyqtSignal(float, float, float, float, float)
    newFrameRequested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("panel")

        self._source_image = None
        self._popout_dialog = None
        self._placeholder_label = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        heading_row = QHBoxLayout()
        heading = QLabel("Preview")
        heading.setObjectName("heading")
        heading_row.addWidget(heading)
        heading_row.addStretch()
        self.popout_btn = QPushButton("Pop Out")
        self.popout_btn.setFixedWidth(90)
        self.popout_btn.clicked.connect(self._toggle_popout)
        heading_row.addWidget(self.popout_btn)
        layout.addLayout(heading_row)

        # Index in `layout` where the image (or its placeholder while
        # popped out) lives - used to put it back in the same spot.
        self._image_slot_index = layout.count()

        self.image_label = QLabel("No file loaded")
        self.image_label.setObjectName("dim")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setMinimumHeight(260)
        self.image_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._image_style = (
            "background-color: #17181a; border: 1px solid #3a3c42; border-radius: 6px;"
        )
        self.image_label.setStyleSheet(self._image_style)
        layout.addWidget(self.image_label)

        new_frame_row = QHBoxLayout()
        self.new_frame_btn = QPushButton("New Frame")
        self.new_frame_btn.setFixedWidth(100)
        self.new_frame_btn.clicked.connect(self.newFrameRequested.emit)
        new_frame_row.addWidget(self.new_frame_btn)
        new_frame_row.addStretch()
        layout.addLayout(new_frame_row)

        self.brightness_row = SliderRow("Brightness")
        self.contrast_row = SliderRow("Contrast")
        self.saturation_row = SliderRow("Saturation")
        self.denoise_row = SliderRow("Denoise", min_val=0, max_val=100)
        self.sharpen_row = SliderRow("Sharpen", min_val=0, max_val=100)

        self._all_rows = (
            self.brightness_row, self.contrast_row, self.saturation_row,
            self.denoise_row, self.sharpen_row,
        )
        for row in self._all_rows:
            row.valueChanged.connect(self._on_slider_changed)
            layout.addWidget(row)

        reset_row = QHBoxLayout()
        self.reset_btn = QPushButton("Reset")
        self.reset_btn.setFixedWidth(80)
        self.reset_btn.clicked.connect(self._on_reset_clicked)
        reset_row.addWidget(self.reset_btn)
        reset_row.addStretch()
        layout.addLayout(reset_row)

    def _on_reset_clicked(self):
        self.reset_adjustments()
        self._render()
        b, c, s, d, sh = self.current_values()
        self.adjustmentsChanged.emit(b, c, s, d, sh)

    def set_source_image(self, pil_image, keep_adjustments: bool = False):
        self._source_image = pil_image
        if not keep_adjustments:
            self.reset_adjustments()
        self._render()

    def clear(self):
        self._source_image = None
        self.image_label.setText("No file loaded")
        self.image_label.setPixmap(QPixmap())

    def reset_adjustments(self):
        for row in self._all_rows:
            row.slider.blockSignals(True)
            row.reset()
            row.slider.blockSignals(False)

    def current_values(self):
        """Return (brightness, contrast, saturation, denoise, sharpen)."""
        return (
            float(self.brightness_row.value()),
            float(self.contrast_row.value()),
            float(self.saturation_row.value()),
            float(self.denoise_row.value()),
            float(self.sharpen_row.value()),
        )

    def set_controls_enabled(self, enabled: bool):
        for row in self._all_rows:
            row.set_enabled(enabled)
        self.reset_btn.setEnabled(enabled)

    def set_new_frame_enabled(self, enabled: bool):
        self.new_frame_btn.setEnabled(enabled)

    def _on_slider_changed(self, _value: int):
        self._render()
        b, c, s, d, sh = self.current_values()
        self.adjustmentsChanged.emit(b, c, s, d, sh)

    def _render(self):
        if self._source_image is None:
            return
        b, c, s, d, sh = self.current_values()
        adjusted = apply_adjustments(self._source_image, b, c, s, d, sh)
        pixmap = pil_to_pixmap(adjusted)
        scaled = pixmap.scaled(
            self.image_label.width(),
            self.image_label.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.image_label.setPixmap(scaled)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    # -- pop-out / dock ---------------------------------------------------

    def _toggle_popout(self):
        if self._popout_dialog is None:
            self._popout_preview()
        else:
            self._popout_dialog.close()  # triggers _dock_preview via closeEvent

    def _popout_preview(self):
        main_layout = self.layout()
        main_layout.removeWidget(self.image_label)

        self._placeholder_label = QLabel("Preview popped out")
        self._placeholder_label.setObjectName("dim")
        self._placeholder_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder_label.setMinimumHeight(260)
        self._placeholder_label.setStyleSheet(self._image_style)
        main_layout.insertWidget(self._image_slot_index, self._placeholder_label)

        self._popout_dialog = _PopoutPreviewDialog(
            on_resize=self._render, on_close=self._dock_preview, parent=self
        )
        dlg_layout = QVBoxLayout(self._popout_dialog)
        dlg_layout.setContentsMargins(8, 8, 8, 8)
        dlg_layout.addWidget(self.image_label)
        self._popout_dialog.show()

        self.popout_btn.setText("Dock")
        # Let the dialog finish laying out before rescaling into it.
        QTimer.singleShot(0, self._render)

    def _dock_preview(self):
        main_layout = self.layout()

        if self._placeholder_label is not None:
            main_layout.removeWidget(self._placeholder_label)
            self._placeholder_label.deleteLater()
            self._placeholder_label = None

        self.image_label.setParent(None)
        main_layout.insertWidget(self._image_slot_index, self.image_label)

        if self._popout_dialog is not None:
            self._popout_dialog.deleteLater()
            self._popout_dialog = None

        self.popout_btn.setText("Pop Out")
        QTimer.singleShot(0, self._render)


# =============================================================================
# Main window
# =============================================================================

VIDEO_FILTER = "Video Files (*.mkv *.mp4);;All Files (*)"


def build_output_path(input_path: str, output_format: str) -> str:
    """
    Build the output path for a file, using the chosen output format
    ('mp4' or 'mkv') rather than automatically flipping to the opposite
    of the source extension. '-out' is appended so it never collides
    with the source, even when input and output formats match.
    """
    root, _ext = os.path.splitext(input_path)
    ext = ".mkv" if output_format.lower() == "mkv" else ".mp4"
    return root + "-out" + ext


def resolve_collision_rename(path: str) -> str:
    """
    If `path` already exists, return the next available '(2)', '(3)', ...
    variant. Returns `path` unchanged if it doesn't exist.
    """
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    n = 2
    while True:
        candidate = f"{root} ({n}){ext}"
        if not os.path.exists(candidate):
            return candidate
        n += 1


class ConflictResolutionDialog(QDialog):
    """
    Shown before a batch starts if one or more planned output files
    already exist. Lets the user choose Rename / Overwrite / Skip for
    each conflicting file, with bulk 'apply to all' shortcuts.
    """

    def __init__(self, conflicts, parent=None):
        # conflicts: list of (input_path, output_path) tuples
        super().__init__(parent)
        self.conflicts = conflicts
        self.combos = []

        self.setWindowTitle("Output File Already Exists")
        self.resize(460, 360)

        layout = QVBoxLayout(self)

        info = QLabel(
            f"{len(conflicts)} output file(s) already exist. "
            f"Choose what to do for each before converting:"
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        list_container = QWidget()
        list_layout = QVBoxLayout(list_container)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(8)

        for input_path, output_path in conflicts:
            row = QHBoxLayout()
            name_label = QLabel(os.path.basename(output_path))
            name_label.setWordWrap(True)
            combo = QComboBox()
            combo.addItems(["Rename", "Overwrite", "Skip"])
            row.addWidget(name_label, 1)
            row.addWidget(combo)
            list_layout.addLayout(row)
            self.combos.append(combo)
        list_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(list_container)
        layout.addWidget(scroll)

        bulk_row = QHBoxLayout()
        rename_all_btn = QPushButton("Rename All")
        rename_all_btn.clicked.connect(lambda: self._set_all("Rename"))
        overwrite_all_btn = QPushButton("Overwrite All")
        overwrite_all_btn.clicked.connect(lambda: self._set_all("Overwrite"))
        skip_all_btn = QPushButton("Skip All")
        skip_all_btn.clicked.connect(lambda: self._set_all("Skip"))
        bulk_row.addWidget(rename_all_btn)
        bulk_row.addWidget(overwrite_all_btn)
        bulk_row.addWidget(skip_all_btn)
        layout.addLayout(bulk_row)

        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _set_all(self, choice: str):
        for combo in self.combos:
            combo.setCurrentText(choice)

    def get_resolutions(self) -> dict:
        """Return {input_path: 'rename' | 'overwrite' | 'skip'}."""
        result = {}
        for (input_path, _output_path), combo in zip(self.conflicts, self.combos):
            result[input_path] = combo.currentText().lower()
        return result


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MKV / MP4 Converter")
        self.resize(960, 680)

        self.queue = []          # list of input file paths
        self.queue_index = 0     # index of file currently being processed
        self.batch_results = []  # list of (path, status) tuples: status in "ok"/"failed"/"canceled"/"skipped"
        self.worker = None
        self._batch_canceled = False
        self.planned_outputs = {}  # input_path -> resolved output_path for this run
        self.skip_paths = set()    # input_paths the user chose to skip due to conflicts
        self.current_preview_path = None
        self.subtitle_removals = {}      # path -> set of relative subtitle indices to remove
        self.current_subtitle_tracks = []  # subtitle tracks for the currently displayed file
        self._loading_subtitle_list = False  # guards itemChanged while populating
        self.audio_convert_choices = {}  # path -> bool (True = convert audio to AAC)
        self.current_audio_tracks = []   # audio tracks for the currently displayed file
        self._loading_audio_tab = False  # guards currentIndexChanged while populating

        self._build_ui()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)

        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(16)

        left_panel = QFrame()
        left_panel.setObjectName("panel")
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(16, 16, 16, 16)
        left_layout.setSpacing(14)
        left_panel.setFixedWidth(340)

        self.tabs = QTabWidget()

        # --- Queue tab ---
        queue_tab = QWidget()
        queue_tab_layout = QVBoxLayout(queue_tab)
        queue_tab_layout.setContentsMargins(8, 8, 8, 8)
        queue_tab_layout.setSpacing(10)

        self.queue_list = QListWidget()
        self.queue_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.queue_list.setFixedHeight(140)
        self.queue_list.itemClicked.connect(self.on_queue_item_clicked)
        queue_tab_layout.addWidget(self.queue_list)

        queue_btn_row = QHBoxLayout()
        add_btn = QPushButton("Add Files...")
        add_btn.clicked.connect(self.on_add_files)
        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self.on_remove_selected)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.on_clear_queue)
        queue_btn_row.addWidget(add_btn)
        queue_btn_row.addWidget(remove_btn)
        queue_btn_row.addWidget(clear_btn)
        queue_tab_layout.addLayout(queue_btn_row)

        format_row = QHBoxLayout()
        format_label = QLabel("Output format")
        self.output_format_combo = QComboBox()
        self.output_format_combo.addItem("MP4")
        self.output_format_combo.addItem("MKV")
        format_row.addWidget(format_label)
        format_row.addStretch()
        format_row.addWidget(self.output_format_combo)
        queue_tab_layout.addLayout(format_row)

        self.output_label = QLabel("")
        self.output_label.setObjectName("dim")
        self.output_label.setWordWrap(True)
        queue_tab_layout.addWidget(self.output_label)
        queue_tab_layout.addStretch()

        self.tabs.addTab(queue_tab, "Queue")

        # --- Subtitles tab ---
        subs_tab = QWidget()
        subs_tab_layout = QVBoxLayout(subs_tab)
        subs_tab_layout.setContentsMargins(8, 8, 8, 8)
        subs_tab_layout.setSpacing(10)

        self.subs_hint_label = QLabel("Select a file in the Queue tab to view its subtitle tracks.")
        self.subs_hint_label.setObjectName("dim")
        self.subs_hint_label.setWordWrap(True)
        subs_tab_layout.addWidget(self.subs_hint_label)

        self.subtitle_list = QListWidget()
        self.subtitle_list.setFixedHeight(140)
        self.subtitle_list.itemChanged.connect(self.on_subtitle_item_changed)
        subs_tab_layout.addWidget(self.subtitle_list)

        subs_note = QLabel("Tick a track to remove it from the output.")
        subs_note.setObjectName("dim")
        subs_note.setWordWrap(True)
        subs_tab_layout.addWidget(subs_note)
        subs_tab_layout.addStretch()

        self.tabs.addTab(subs_tab, "Subtitles")

        # --- Audio tab ---
        audio_tab = QWidget()
        audio_tab_layout = QVBoxLayout(audio_tab)
        audio_tab_layout.setContentsMargins(8, 8, 8, 8)
        audio_tab_layout.setSpacing(10)

        self.audio_hint_label = QLabel("Select a file in the Queue tab to view its audio tracks.")
        self.audio_hint_label.setObjectName("dim")
        self.audio_hint_label.setWordWrap(True)
        audio_tab_layout.addWidget(self.audio_hint_label)

        self.audio_list = QListWidget()
        self.audio_list.setFixedHeight(110)
        audio_tab_layout.addWidget(self.audio_list)

        audio_combo_label = QLabel("Audio handling")
        audio_combo_label.setObjectName("heading")
        audio_tab_layout.addWidget(audio_combo_label)

        self.audio_codec_combo = QComboBox()
        self.audio_codec_combo.addItem("Keep original codec")
        self.audio_codec_combo.addItem("Convert to AAC")
        self.audio_codec_combo.currentIndexChanged.connect(self.on_audio_codec_changed)
        audio_tab_layout.addWidget(self.audio_codec_combo)

        audio_note = QLabel(
            "In Remux mode, choosing AAC transcodes only the audio - video "
            "stays a fast stream copy. In Full Convert mode audio is always "
            "encoded to AAC, so this choice has no effect there."
        )
        audio_note.setObjectName("dim")
        audio_note.setWordWrap(True)
        audio_tab_layout.addWidget(audio_note)
        audio_tab_layout.addStretch()

        self.tabs.addTab(audio_tab, "Audio")

        # --- Quality (RF) tab ---
        quality_tab = QWidget()
        quality_tab_layout = QVBoxLayout(quality_tab)
        quality_tab_layout.setContentsMargins(8, 8, 8, 8)
        quality_tab_layout.setSpacing(10)

        quality_heading = QLabel("Video Quality (RF)")
        quality_heading.setObjectName("heading")
        quality_tab_layout.addWidget(quality_heading)

        rf_header = QHBoxLayout()
        rf_label = QLabel("RF")
        self.rf_value_label = QLabel("22")
        self.rf_value_label.setObjectName("dim")
        self.rf_value_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        rf_header.addWidget(rf_label)
        rf_header.addStretch()
        rf_header.addWidget(self.rf_value_label)
        quality_tab_layout.addLayout(rf_header)

        self.rf_slider = QSlider(Qt.Orientation.Horizontal)
        self.rf_slider.setRange(0, 51)
        self.rf_slider.setValue(22)
        self.rf_slider.valueChanged.connect(self.on_rf_changed)
        quality_tab_layout.addWidget(self.rf_slider)

        scale_row = QHBoxLayout()
        scale_left = QLabel("0: Lossless")
        scale_left.setObjectName("dim")
        scale_right = QLabel("51: Worst")
        scale_right.setObjectName("dim")
        scale_right.setAlignment(Qt.AlignmentFlag.AlignRight)
        scale_row.addWidget(scale_left)
        scale_row.addWidget(scale_right)
        quality_tab_layout.addLayout(scale_row)

        rf_note = QLabel(
            "Lower RF = higher quality and a larger file; higher RF = "
            "smaller file, lower quality. 22 is a good default (18-22 is "
            "typically visually lossless). Only applies in Full Convert "
            "mode - Remux copies the original video untouched."
        )
        rf_note.setObjectName("dim")
        rf_note.setWordWrap(True)
        quality_tab_layout.addWidget(rf_note)
        quality_tab_layout.addStretch()

        self.tabs.addTab(quality_tab, "Quality")

        left_layout.addWidget(self.tabs)
        left_layout.addSpacing(10)

        mode_heading = QLabel("Mode")
        mode_heading.setObjectName("heading")
        left_layout.addWidget(mode_heading)

        mode_row = QHBoxLayout()
        self.remux_btn = QPushButton("Remux (copy)")
        self.remux_btn.setObjectName("toggle")
        self.remux_btn.setCheckable(True)
        self.remux_btn.setChecked(True)

        self.convert_mode_btn = QPushButton("Full Convert")
        self.convert_mode_btn.setObjectName("toggle")
        self.convert_mode_btn.setCheckable(True)

        self.remux_btn.clicked.connect(lambda: self._set_mode(remux=True))
        self.convert_mode_btn.clicked.connect(lambda: self._set_mode(remux=False))

        mode_row.addWidget(self.remux_btn)
        mode_row.addWidget(self.convert_mode_btn)
        left_layout.addLayout(mode_row)

        mode_hint = QLabel(
            "Remux only re-packages the streams (fast, no quality loss, "
            "filters disabled). Full Convert re-encodes and applies the "
            "adjustments below. Files convert one after another; the "
            "current sliders apply to the whole queue."
        )
        mode_hint.setObjectName("dim")
        mode_hint.setWordWrap(True)
        left_layout.addWidget(mode_hint)

        left_layout.addStretch()

        self.convert_btn = QPushButton("Convert")
        self.convert_btn.setObjectName("primary")
        self.convert_btn.setEnabled(False)
        self.convert_btn.clicked.connect(self.on_convert)
        left_layout.addWidget(self.convert_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("danger")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.on_cancel)
        left_layout.addWidget(self.cancel_btn)

        self.queue_progress_label = QLabel("")
        self.queue_progress_label.setObjectName("dim")
        left_layout.addWidget(self.queue_progress_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        left_layout.addWidget(self.progress_bar)

        self.eta_label = QLabel("")
        self.eta_label.setObjectName("dim")
        self.eta_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        left_layout.addWidget(self.eta_label)

        root_layout.addWidget(left_panel)

        self.preview = PreviewWidget()
        self.preview.newFrameRequested.connect(self.on_new_frame_requested)
        self.preview.set_new_frame_enabled(False)
        root_layout.addWidget(self.preview, stretch=1)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        self._set_mode(remux=True)

    def _set_mode(self, remux: bool):
        self.remux_btn.setChecked(remux)
        self.convert_mode_btn.setChecked(not remux)
        self.preview.set_controls_enabled(not remux)

    def is_remux_mode(self) -> bool:
        return self.remux_btn.isChecked()

    # ------------------------------------------------------------------
    # Queue management
    # ------------------------------------------------------------------

    def on_add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select video files", "", VIDEO_FILTER)
        if not paths:
            return

        for path in paths:
            if path in self.queue:
                continue
            self.queue.append(path)
            self.queue_list.addItem(QListWidgetItem(os.path.basename(path)))

        self._update_queue_label()

        # Preview the first file added if nothing is previewed yet.
        if self.queue_list.count() == len(paths):
            self.queue_list.setCurrentRow(0)
            self._load_preview(self.queue[0])

        self.convert_btn.setEnabled(len(self.queue) > 0)

    def on_remove_selected(self):
        rows = sorted((idx.row() for idx in self.queue_list.selectedIndexes()), reverse=True)
        for row in rows:
            self.queue_list.takeItem(row)
            del self.queue[row]

        self._update_queue_label()
        self.convert_btn.setEnabled(len(self.queue) > 0)
        if not self.queue:
            self.preview.clear()
            self.current_preview_path = None
            self.preview.set_new_frame_enabled(False)
            self.subtitle_list.clear()
            self.current_subtitle_tracks = []
            self.subs_hint_label.setText("Select a file in the Queue tab to view its subtitle tracks.")
            self.audio_list.clear()
            self.current_audio_tracks = []
            self.audio_hint_label.setText("Select a file in the Queue tab to view its audio tracks.")
            self._loading_audio_tab = True
            self.audio_codec_combo.setCurrentIndex(0)
            self._loading_audio_tab = False

    def on_clear_queue(self):
        self.queue_list.clear()
        self.queue.clear()
        self.preview.clear()
        self.current_preview_path = None
        self.preview.set_new_frame_enabled(False)
        self.subtitle_list.clear()
        self.current_subtitle_tracks = []
        self.subs_hint_label.setText("Select a file in the Queue tab to view its subtitle tracks.")
        self.audio_list.clear()
        self.current_audio_tracks = []
        self.audio_hint_label.setText("Select a file in the Queue tab to view its audio tracks.")
        self._loading_audio_tab = True
        self.audio_codec_combo.setCurrentIndex(0)
        self._loading_audio_tab = False
        self._update_queue_label()
        self.convert_btn.setEnabled(False)

    def on_queue_item_clicked(self, item: QListWidgetItem):
        row = self.queue_list.row(item)
        if 0 <= row < len(self.queue):
            self._load_preview(self.queue[row])

    def on_new_frame_requested(self):
        if self.current_preview_path:
            self._load_preview(self.current_preview_path, keep_adjustments=True)

    def _load_preview(self, path: str, keep_adjustments: bool = False):
        self.current_preview_path = path
        self.status_bar.showMessage("Sampling preview frame...")
        try:
            frame = extract_random_frame(path)
        except MediaProbeError as exc:
            QMessageBox.warning(self, "Preview unavailable", str(exc))
            self.preview.clear()
        else:
            self.preview.set_source_image(frame, keep_adjustments=keep_adjustments)
            self.preview.set_new_frame_enabled(True)
        self.status_bar.showMessage("Ready")
        self._load_subtitle_tracks(path)
        self._load_audio_tracks(path)

    def _load_subtitle_tracks(self, path: str):
        try:
            tracks = probe_subtitle_streams(path)
        except MediaProbeError as exc:
            self.current_subtitle_tracks = []
            self.subs_hint_label.setText(f"Could not read subtitle tracks: {exc}")
            self.subtitle_list.clear()
            return

        self.current_subtitle_tracks = tracks
        removed = self.subtitle_removals.get(path, set())

        self._loading_subtitle_list = True
        self.subtitle_list.clear()
        if not tracks:
            self.subs_hint_label.setText(f"{os.path.basename(path)}: no subtitle tracks found.")
        else:
            self.subs_hint_label.setText(
                f"{os.path.basename(path)}: {len(tracks)} subtitle track(s) found."
            )
            for track in tracks:
                item = QListWidgetItem(track["label"])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.CheckState.Checked if track["relative_index"] in removed
                    else Qt.CheckState.Unchecked
                )
                self.subtitle_list.addItem(item)
        self._loading_subtitle_list = False

    def on_subtitle_item_changed(self, item: QListWidgetItem):
        if self._loading_subtitle_list or not self.current_preview_path:
            return
        row = self.subtitle_list.row(item)
        if row < 0 or row >= len(self.current_subtitle_tracks):
            return

        rel_index = self.current_subtitle_tracks[row]["relative_index"]
        removed = self.subtitle_removals.setdefault(self.current_preview_path, set())
        if item.checkState() == Qt.CheckState.Checked:
            removed.add(rel_index)
        else:
            removed.discard(rel_index)

    def _load_audio_tracks(self, path: str):
        try:
            tracks = probe_audio_streams(path)
        except MediaProbeError as exc:
            self.current_audio_tracks = []
            self.audio_hint_label.setText(f"Could not read audio tracks: {exc}")
            self.audio_list.clear()
            return

        self.current_audio_tracks = tracks

        self.audio_list.clear()
        if not tracks:
            self.audio_hint_label.setText(f"{os.path.basename(path)}: no audio tracks found.")
        else:
            self.audio_hint_label.setText(
                f"{os.path.basename(path)}: {len(tracks)} audio track(s) found."
            )
            for track in tracks:
                self.audio_list.addItem(QListWidgetItem(track["label"]))

        convert_to_aac = self.audio_convert_choices.get(path, False)
        self._loading_audio_tab = True
        self.audio_codec_combo.setCurrentIndex(1 if convert_to_aac else 0)
        self._loading_audio_tab = False

    def on_audio_codec_changed(self, index: int):
        if self._loading_audio_tab or not self.current_preview_path:
            return
        self.audio_convert_choices[self.current_preview_path] = (index == 1)

    def on_rf_changed(self, value: int):
        self.rf_value_label.setText(str(value))

    def _update_queue_label(self):
        n = len(self.queue)
        self.output_label.setText(f"{n} file(s) queued" if n else "")

    # ------------------------------------------------------------------
    # Batch conversion
    # ------------------------------------------------------------------

    def on_convert(self):
        if not self.queue:
            return

        output_format = self.output_format_combo.currentText()
        planned_outputs = {}
        conflicts = []
        for input_path in self.queue:
            output_path = build_output_path(input_path, output_format)
            planned_outputs[input_path] = output_path
            if os.path.exists(output_path):
                conflicts.append((input_path, output_path))

        skip_paths = set()

        if conflicts:
            dialog = ConflictResolutionDialog(conflicts, parent=self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return  # user canceled - don't start the batch at all

            resolutions = dialog.get_resolutions()
            for input_path, action in resolutions.items():
                if action == "rename":
                    planned_outputs[input_path] = resolve_collision_rename(planned_outputs[input_path])
                elif action == "skip":
                    skip_paths.add(input_path)
                # "overwrite" needs no change - planned path is used as-is

        self.planned_outputs = planned_outputs
        self.skip_paths = skip_paths
        self.queue_index = 0
        self.batch_results = []
        self._batch_canceled = False

        self._set_busy(True)
        self._process_next_in_queue()

    def _process_next_in_queue(self):
        if self.queue_index >= len(self.queue):
            self._finish_batch()
            return

        input_path = self.queue[self.queue_index]

        if input_path in self.skip_paths:
            self.batch_results.append((input_path, "skipped", ""))
            self.queue_index += 1
            self._process_next_in_queue()
            return

        output_path = self.planned_outputs.get(
            input_path, build_output_path(input_path, self.output_format_combo.currentText())
        )
        remux_only = self.is_remux_mode()
        brightness, contrast, saturation, denoise, sharpen = self.preview.current_values()

        self.queue_progress_label.setText(
            f"File {self.queue_index + 1} of {len(self.queue)}: {os.path.basename(input_path)}"
        )
        self.progress_bar.setValue(0)
        self.eta_label.setText("")
        self.queue_list.setCurrentRow(self.queue_index)

        self.worker = ConversionWorker(
            input_path=input_path,
            output_path=output_path,
            remux_only=remux_only,
            brightness=brightness,
            contrast=contrast,
            saturation=saturation,
            denoise=denoise,
            sharpen=sharpen,
            remove_subtitle_indices=self.subtitle_removals.get(input_path, set()),
            convert_audio_aac=self.audio_convert_choices.get(input_path, False),
            rf=self.rf_slider.value(),
        )
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.status.connect(self.status_bar.showMessage)
        self.worker.eta_updated.connect(self._on_eta_updated)
        self.worker.finished_ok.connect(self._on_file_finished_ok)
        self.worker.failed.connect(self._on_file_failed)
        self.worker.canceled.connect(self._on_file_canceled)
        self.worker.start()

    def _on_eta_updated(self, eta_text: str):
        self.eta_label.setText(f"Est. time remaining: {eta_text}")

    def on_cancel(self):
        if self.worker:
            self._batch_canceled = True
            self.status_bar.showMessage("Cancelling...")
            self.worker.cancel()

    def _on_file_finished_ok(self, output_path: str):
        self.batch_results.append((self.queue[self.queue_index], "ok", output_path))
        self.queue_index += 1
        self._process_next_in_queue()

    def _on_file_failed(self, message: str):
        self.batch_results.append((self.queue[self.queue_index], "failed", message))
        self.queue_index += 1
        self._process_next_in_queue()

    def _on_file_canceled(self):
        self.batch_results.append((self.queue[self.queue_index], "canceled", ""))
        # Mark every remaining queued file as canceled too, then stop.
        for path in self.queue[self.queue_index + 1:]:
            self.batch_results.append((path, "canceled", ""))
        self._finish_batch()

    def _finish_batch(self):
        self._set_busy(False)
        self.progress_bar.setValue(0)
        self.queue_progress_label.setText("")
        self.eta_label.setText("")

        ok_count = sum(1 for _, status, _ in self.batch_results if status == "ok")
        failed = [(p, msg) for p, status, msg in self.batch_results if status == "failed"]
        canceled_count = sum(1 for _, status, _ in self.batch_results if status == "canceled")
        skipped_count = sum(1 for _, status, _ in self.batch_results if status == "skipped")

        lines = [f"Completed: {ok_count} of {len(self.batch_results)} file(s) converted."]
        if failed:
            lines.append("")
            lines.append("Failed:")
            for path, msg in failed:
                lines.append(f"  - {os.path.basename(path)}:")
                # Long ffmpeg error dumps: show just the tail, indented,
                # so one failure doesn't swamp the whole summary dialog.
                for detail_line in msg.splitlines()[-6:]:
                    lines.append(f"      {detail_line}")
        if skipped_count:
            lines.append("")
            lines.append(f"{skipped_count} file(s) skipped (already existed).")
        if canceled_count:
            lines.append("")
            lines.append(f"{canceled_count} file(s) canceled/skipped.")

        self.status_bar.showMessage("Batch complete")

        title = "Batch canceled" if self._batch_canceled else "Batch conversion complete"
        # Modal dialog: execution blocks here until the user clicks OK.
        if failed:
            QMessageBox.warning(self, title, "\n".join(lines))
        else:
            QMessageBox.information(self, title, "\n".join(lines))

    def _set_busy(self, busy: bool):
        self.convert_btn.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.remux_btn.setEnabled(not busy)
        self.convert_mode_btn.setEnabled(not busy)
        self.subtitle_list.setEnabled(not busy)
        self.audio_list.setEnabled(not busy)
        self.audio_codec_combo.setEnabled(not busy)
        self.rf_slider.setEnabled(not busy)
        self.output_format_combo.setEnabled(not busy)
        self.preview.set_controls_enabled(False if busy else not self.is_remux_mode())
        self.preview.set_new_frame_enabled(not busy)
        for btn in self.findChildren(QPushButton):
            if btn.text() in ("Add Files...", "Remove", "Clear"):
                btn.setEnabled(not busy)


# =============================================================================
# Entry point
# =============================================================================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Muxy Editor")
    app.setStyleSheet(DARK_STYLESHEET)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()