"""Qt Widgets video editor prototype."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter
from PySide6.QtCore import (
    QEvent,
    QObject,
    QProcess,
    QSize,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QKeySequence, QPainter, QPalette, QPen, QPixmap
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStyle,
    QSlider,
    QStackedWidget,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# Current product version shown in About and application metadata.
APP_VERSION = "10.6.0.8"


def probe_media(path: str) -> dict:
    """Return ffprobe stream and duration metadata for a media file."""
    ffprobe = media_tool("ffprobe")
    if not ffprobe:
        raise FileNotFoundError("ffprobe is not available")
    result = subprocess.run(
        [
            ffprobe, "-v", "error", "-show_streams", "-show_format",
            "-of", "json", path,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def media_tool(name: str) -> str | None:
    """Prefer the host media tool for hardware-driver compatibility, then bundle fallback."""
    system_tool = shutil.which(name)
    if system_tool:
        return system_tool
    appdir = os.environ.get("APPDIR")
    if appdir:
        bundled = Path(appdir) / "usr" / "bin" / name
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    return None


def ffmpeg_encoders() -> tuple[list[str], list[str]]:
    """Read audio/video encoders supported by the installed FFmpeg build."""
    ffmpeg = media_tool("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError("ffmpeg is not available")
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"],
        check=True,
        capture_output=True,
        text=True,
    )
    video: list[str] = []
    audio: list[str] = []
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*([A-Z\.]{6})\s+(\S+)", line)
        if not match:
            continue
        flags, name = match.groups()
        if flags.startswith("V"):
            video.append(name)
        elif flags.startswith("A"):
            audio.append(name)
    return video, audio


class PreviewLabel(QLabel):
    """Video preview surface that scales its frame to the available space."""

    def __init__(self) -> None:
        super().__init__("Drop a video here, or choose File → Open Video")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(480, 270)
        self.setStyleSheet("border: 1px solid palette(mid)")
        self._frame: QPixmap | None = None

    def set_theme(self, dark: bool) -> None:
        background, foreground, border = (
            ("#111315", "#c7c9cc", "#45474d")
            if dark
            else ("#e7e9ed", "#30343a", "#b8bdc5")
        )
        self.setStyleSheet(
            f"background: {background}; color: {foreground}; border: 1px solid {border}"
        )

    def set_frame(self, image: QImage) -> None:
        self._frame = QPixmap.fromImage(image)
        self._update_frame()

    def set_placeholder(self, text: str) -> None:
        self._frame = None
        self.setPixmap(QPixmap())
        self.setText(text)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._update_frame()

    def _update_frame(self) -> None:
        if self._frame:
            self.setPixmap(
                self._frame.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )


class TrimRangeSlider(QWidget):
    """Compact dual-handle timeline for selecting the exported video range."""

    rangeChanged = Signal(int, int)
    previewPositionChanged = Signal(int)
    dragStarted = Signal()
    dragFinished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.duration_ms = 0
        self.start_ms = 0
        self.end_ms = 0
        self._dragging: str | None = None
        self._active_handle = "start"
        self._handle_radius = 7
        self.setMinimumHeight(24)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(
            "Drag the handles to choose the exported section. Frame-accurate cuts "
            "require re-encoding; video copy may trim at nearby keyframes. "
            "Trim points snap to one-second increments. Focus the timeline and "
            "use the left and right arrow keys for precise one-second adjustments."
        )

    def sizeHint(self):  # noqa: N802 - Qt override
        from PySide6.QtCore import QSize

        return QSize(320, 26)

    def set_duration(self, duration_ms: int, reset: bool = False) -> None:
        duration_ms = max(0, int(duration_ms))
        was_full_range = self.end_ms == self.duration_ms
        self.duration_ms = duration_ms
        if duration_ms <= 0:
            self.start_ms = self.end_ms = 0
        elif reset or was_full_range:
            self.start_ms, self.end_ms = 0, duration_ms
        else:
            self.start_ms = min(self.start_ms, duration_ms - 1)
            self.end_ms = min(max(self.end_ms, self.start_ms + 1), duration_ms)
        self.update()
        self.rangeChanged.emit(self.start_ms, self.end_ms)

    def reset(self) -> None:
        self.start_ms = 0
        self.end_ms = self.duration_ms
        self.update()
        self.rangeChanged.emit(self.start_ms, self.end_ms)

    def set_boundary(self, boundary: str, position_ms: int) -> None:
        if self.duration_ms <= 0:
            return
        minimum_gap = min(1000, self.duration_ms)
        value = min(self.duration_ms, max(0, round(position_ms / 1000) * 1000))
        if boundary == "start":
            self.start_ms = min(value, max(0, self.end_ms - minimum_gap))
            position = self.start_ms
        else:
            self.end_ms = max(value, min(self.duration_ms, self.start_ms + minimum_gap))
            position = self.end_ms
        self._active_handle = boundary
        self.update()
        self.rangeChanged.emit(self.start_ms, self.end_ms)
        self.previewPositionChanged.emit(position)

    def _track_bounds(self) -> tuple[float, float]:
        margin = self._handle_radius + 2
        return float(margin), float(max(margin, self.width() - margin))

    def _value_to_x(self, value: int) -> float:
        left, right = self._track_bounds()
        if self.duration_ms <= 0:
            return left
        return left + (right - left) * value / self.duration_ms

    def _x_to_value(self, x: float) -> int:
        left, right = self._track_bounds()
        if self.duration_ms <= 0 or right <= left:
            return 0
        proportion = min(1.0, max(0.0, (x - left) / (right - left)))
        return round(proportion * self.duration_ms)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, right = self._track_bounds()
        center_y = self.height() / 2
        track_height = 5
        painter.setPen(Qt.PenStyle.NoPen)
        palette = self.palette()
        painter.setBrush(palette.color(QPalette.ColorRole.Mid))
        painter.drawRoundedRect(left, center_y - track_height / 2, right - left, track_height, 2.5, 2.5)

        start_x = self._value_to_x(self.start_ms)
        end_x = self._value_to_x(self.end_ms)
        painter.setBrush(palette.color(QPalette.ColorRole.Highlight))
        painter.drawRoundedRect(start_x, center_y - track_height / 2, max(0.0, end_x - start_x), track_height, 2.5, 2.5)

        painter.setPen(QPen(palette.color(QPalette.ColorRole.WindowText), 1.5))
        painter.setBrush(palette.color(QPalette.ColorRole.Base))
        for x in (start_x, end_x):
            painter.drawEllipse(x - self._handle_radius, center_y - self._handle_radius,
                                self._handle_radius * 2, self._handle_radius * 2)

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self.isEnabled() or self.duration_ms <= 0 or event.button() != Qt.MouseButton.LeftButton:
            return
        x = event.position().x()
        start_x, end_x = self._value_to_x(self.start_ms), self._value_to_x(self.end_ms)
        if abs(x - start_x) <= abs(x - end_x):
            self._dragging = "start"
        else:
            self._dragging = "end"
        self._active_handle = self._dragging
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.dragStarted.emit()
        self._set_drag_value(x)
        self.setCursor(Qt.CursorShape.SizeHorCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self._dragging:
            self._set_drag_value(event.position().x())
        elif self.isEnabled():
            self.setCursor(Qt.CursorShape.SizeHorCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            was_dragging = self._dragging is not None
            self._dragging = None
            if was_dragging:
                self.dragFinished.emit()

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if not self._dragging:
            self.unsetCursor()
        super().leaveEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right) and self.duration_ms > 0:
            delta = 1000 if event.key() == Qt.Key.Key_Right else -1000
            minimum_gap = min(1000, self.duration_ms)
            self.dragStarted.emit()
            if self._active_handle == "start":
                self.start_ms = min(
                    max(0, self.start_ms + delta),
                    max(0, self.end_ms - minimum_gap),
                )
                position = self.start_ms
            else:
                self.end_ms = min(
                    self.duration_ms,
                    max(self.start_ms + minimum_gap, self.end_ms + delta),
                )
                position = self.end_ms
            self.update()
            self.rangeChanged.emit(self.start_ms, self.end_ms)
            self.previewPositionChanged.emit(position)
            self.dragFinished.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def _set_drag_value(self, x: float) -> None:
        value = round(self._x_to_value(x) / 1000) * 1000
        value = min(self.duration_ms, max(0, value))
        minimum_gap = min(1000, self.duration_ms)
        if self._dragging == "start":
            value = min(value, max(0, self.end_ms - minimum_gap))
            self.start_ms = (value // 1000) * 1000
        elif self._dragging == "end":
            self.end_ms = max(value, min(self.duration_ms, self.start_ms + minimum_gap))
        else:
            return
        self.update()
        self.rangeChanged.emit(self.start_ms, self.end_ms)
        self.previewPositionChanged.emit(
            self.start_ms if self._dragging == "start" else self.end_ms
        )


class TrimTimeSpinBox(QSpinBox):
    """Integer-second time editor displayed as MM:SS."""

    def textFromValue(self, value: int) -> str:  # noqa: N802 - Qt override
        return f"{value // 60:02d}:{value % 60:02d}"

    def valueFromText(self, text: str) -> int:  # noqa: N802 - Qt override
        try:
            minutes, seconds = text.strip().split(":", 1)
            return max(0, int(minutes) * 60 + int(seconds))
        except (ValueError, TypeError):
            return 0


class PreviewWindow(QMainWindow):
    """Separate resizable window for the live video preview."""

    closed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Muxy Preview")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(960, 540)
        self.preview = PreviewLabel()
        self.preview.setMinimumSize(320, 180)
        self.before_preview = PreviewLabel()
        self.before_preview.setMinimumSize(320, 180)
        self.comparison_after_preview = PreviewLabel()
        self.comparison_after_preview.setMinimumSize(320, 180)
        self.preview_stack = QStackedWidget()
        self.preview_stack.addWidget(self.preview)
        comparison_panel = QWidget()
        comparison_layout = QHBoxLayout(comparison_panel)
        before_panel = QWidget()
        before_layout = QVBoxLayout(before_panel)
        before_layout.addWidget(QLabel("Before"))
        before_layout.addWidget(self.before_preview, stretch=1)
        after_panel = QWidget()
        after_layout = QVBoxLayout(after_panel)
        after_layout.addWidget(QLabel("After"))
        after_layout.addWidget(self.comparison_after_preview, stretch=1)
        comparison_layout.addWidget(before_panel, stretch=1)
        comparison_layout.addWidget(after_panel, stretch=1)
        self.preview_stack.addWidget(comparison_panel)
        self.setCentralWidget(self.preview_stack)

    def set_comparison_view(self, enabled: bool) -> None:
        self.preview_stack.setCurrentIndex(1 if enabled else 0)

    def set_frames(self, original: QImage, adjusted: QImage) -> None:
        self.preview.set_frame(adjusted)
        self.before_preview.set_frame(original)
        self.comparison_after_preview.set_frame(adjusted)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.hide()
        self.closed.emit()
        event.accept()


class ExportWorker(QObject):
    progress = Signal(int)
    estimate = Signal(str)
    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, command: list[str], duration_ms: int) -> None:
        super().__init__()
        self.command = command
        self.duration_ms = max(duration_ms, 1)
        self.process: subprocess.Popen | None = None
        self.cancel_requested = False

    def stop(self) -> None:
        """Request cancellation and interrupt FFmpeg if it is still running."""
        self.cancel_requested = True
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass

    def run(self) -> None:
        try:
            started_at = time.monotonic()
            self.process = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            if self.cancel_requested:
                self.stop()
            tail: list[str] = []
            assert self.process.stdout is not None
            for raw_line in self.process.stdout:
                line = raw_line.strip()
                if line:
                    tail.append(line)
                    tail = tail[-30:]
                if line.startswith("out_time="):
                    try:
                        hours, minutes, seconds = line.split("=", 1)[1].split(":")
                        elapsed_ms = (
                            (int(hours) * 3600 + int(minutes) * 60 + float(seconds))
                            * 1000
                        )
                        percent = min(99, int(elapsed_ms * 100 / self.duration_ms))
                        self.progress.emit(percent)
                        if 0 < elapsed_ms < self.duration_ms:
                            wall_elapsed = time.monotonic() - started_at
                            remaining = wall_elapsed * (self.duration_ms - elapsed_ms) / elapsed_ms
                            self.estimate.emit(
                                f"Estimated time remaining: {self.format_eta(remaining)}"
                            )
                    except ValueError:
                        pass
            code = self.process.wait()
            if self.cancel_requested:
                self.cancelled.emit()
            elif code:
                self.failed.emit("\n".join(tail) or f"FFmpeg exited with code {code}.")
            else:
                self.progress.emit(100)
                self.estimate.emit("Encoding complete")
                self.finished.emit(self.command[-1])
        except OSError as exc:
            self.failed.emit(str(exc))

    @staticmethod
    def format_eta(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:d}:{seconds:02d}"

class VideoEditorWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Muxy")
        screen = QApplication.primaryScreen()
        available = screen.availableGeometry() if screen else None
        available_width = available.width() if available else 1600
        available_height = available.height() if available else 1000
        startup_width = min(1280, available_width)
        startup_height = min(650, available_height)
        self.resize(startup_width, startup_height)
        self.setMinimumSize(startup_width, startup_height)
        self.setAcceptDrops(True)
        self.current_path: str | None = None
        self.media_info: dict = {}
        self.settings_at_load: dict | None = None
        self.encoding_active = False
        self.audio_checks: list[tuple[int, QCheckBox]] = []
        self.subtitle_checks: list[tuple[int, QCheckBox]] = []
        self.export_thread: QThread | None = None
        self.export_worker: ExportWorker | None = None
        self.app = QApplication.instance()
        config_path = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppConfigLocation
        )
        self.presets_file = Path(config_path) / "presets.json"
        self.presets = self.load_presets()
        if self.app:
            self.app.installEventFilter(self)

        self.player = QMediaPlayer(self)
        self.audio_output = QAudioOutput(self)
        self.player.setAudioOutput(self.audio_output)
        self.video_sink = QVideoSink(self)
        self.player.setVideoSink(self.video_sink)
        self.video_sink.videoFrameChanged.connect(self.on_video_frame)
        self.player.durationChanged.connect(self.set_duration)
        self.player.positionChanged.connect(self.set_position)
        self.player.playbackStateChanged.connect(self.update_play_button)
        self._trim_frame_process: QProcess | None = None
        self._trim_frame_source_generation = 0
        self._trim_preview_pending_position = 0
        self.trim_frame_timer = QTimer(self)
        self.trim_frame_timer.setSingleShot(True)
        self.trim_frame_timer.setInterval(35)
        self.trim_frame_timer.timeout.connect(self.decode_trim_preview_frame)

        self.preview = PreviewLabel()
        self.preview_window = PreviewWindow()
        self.before_preview = PreviewLabel()
        self.before_preview.setMinimumSize(320, 180)
        self.preview.setMinimumSize(320, 180)
        self.comparison_active = False
        self.position_slider = QSlider(Qt.Orientation.Horizontal)
        self.position_slider.setRange(0, 0)
        self.position_slider.sliderMoved.connect(self.player.setPosition)
        self.time_label = QLabel("00:00 / 00:00")
        self.trim_range_slider = TrimRangeSlider()
        self.trim_range_slider.setEnabled(False)
        self.trim_range_slider.rangeChanged.connect(self.update_trim_label)
        self.trim_range_slider.previewPositionChanged.connect(self.preview_trim_position)
        self.trim_range_slider.dragStarted.connect(self.begin_trim_handle_drag)
        self.trim_range_slider.dragFinished.connect(self.finish_trim_handle_drag)
        self._trim_handle_dragging = False
        self.trim_start_spin = TrimTimeSpinBox()
        self.trim_end_spin = TrimTimeSpinBox()
        for spin in (self.trim_start_spin, self.trim_end_spin):
            spin.setRange(0, 0)
            spin.setSingleStep(1)
            spin.setAccelerated(True)
            spin.setKeyboardTracking(False)
            spin.setFixedWidth(82)
            spin.setEnabled(False)
        self.trim_start_spin.setToolTip("Trim start time; use the arrows for one-second steps.")
        self.trim_end_spin.setToolTip("Trim end time; use the arrows for one-second steps.")
        self.trim_start_spin.valueChanged.connect(
            lambda seconds: self.trim_range_slider.set_boundary("start", seconds * 1000)
        )
        self.trim_end_spin.valueChanged.connect(
            lambda seconds: self.trim_range_slider.set_boundary("end", seconds * 1000)
        )
        self.trim_range_label = QLabel("Selected: 00:00")
        self.trim_preview_active = False
        self.trim_preview_button = QPushButton("Preview trim")
        self.trim_preview_button.setEnabled(False)
        self.trim_preview_button.clicked.connect(self.preview_trim)
        self.reset_trim_button = QPushButton("Reset trim")
        self.reset_trim_button.setEnabled(False)
        self.reset_trim_button.clicked.connect(self.trim_range_slider.reset)
        self.summary_file_label: QLabel | None = None
        self.summary_video_label: QLabel | None = None
        self.summary_audio_label: QLabel | None = None
        self.summary_subtitle_label: QLabel | None = None

        self.play_button = QPushButton()
        self.play_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.play_button.setIconSize(QSize(20, 20))
        self.play_button.setFixedSize(44, 36)
        self.play_button.setToolTip("Play video")
        self.play_button.setAccessibleName("Play video")
        self.play_button.clicked.connect(self.toggle_playback)
        self.seek_back_button = QPushButton()
        self.seek_back_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSeekBackward)
        )
        self.seek_back_button.setIconSize(QSize(20, 20))
        self.seek_back_button.setFixedSize(44, 36)
        self.seek_back_button.setToolTip("Rewind 5 seconds")
        self.seek_back_button.setAccessibleName("Rewind 5 seconds")
        self.seek_back_button.clicked.connect(lambda: self.seek_by(-5000))
        self.seek_forward_button = QPushButton()
        self.seek_forward_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSeekForward)
        )
        self.seek_forward_button.setIconSize(QSize(20, 20))
        self.seek_forward_button.setFixedSize(44, 36)
        self.seek_forward_button.setToolTip("Forward 5 seconds")
        self.seek_forward_button.setAccessibleName("Forward 5 seconds")
        self.seek_forward_button.clicked.connect(lambda: self.seek_by(5000))
        transport_widget = QWidget()
        transport = QGridLayout(transport_widget)
        transport.setContentsMargins(0, 0, 0, 0)
        transport.setHorizontalSpacing(6)
        transport.setVerticalSpacing(4)
        transport.setColumnStretch(0, 1)
        transport.setColumnStretch(5, 1)
        transport.addWidget(self.seek_back_button, 0, 1, alignment=Qt.AlignmentFlag.AlignCenter)
        transport.addWidget(self.play_button, 0, 2, alignment=Qt.AlignmentFlag.AlignCenter)
        transport.addWidget(self.seek_forward_button, 0, 3, alignment=Qt.AlignmentFlag.AlignCenter)
        transport.addWidget(self.time_label, 0, 4)
        self.preview_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.preview_volume_slider.setRange(0, 100)
        self.preview_volume_slider.setValue(100)
        self.preview_volume_slider.setMaximumWidth(180)
        self.preview_volume_slider.setToolTip("Preview playback volume")
        self.preview_volume_slider.valueChanged.connect(self.set_preview_volume)
        preview_volume_icon = QLabel()
        preview_volume_icon.setPixmap(
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
            .pixmap(QSize(18, 18))
        )
        preview_volume_icon.setToolTip("Preview playback volume")
        preview_volume_icon.setAccessibleName("Preview playback volume")
        preview_volume_row_widget = QWidget()
        preview_volume_row = QHBoxLayout(preview_volume_row_widget)
        preview_volume_row.setContentsMargins(0, 0, 0, 0)
        preview_volume_row.setSpacing(2)
        preview_volume_row.addSpacing(
            (self.seek_back_button.width() - preview_volume_icon.sizeHint().width()) // 2
        )
        preview_volume_row.addWidget(preview_volume_icon)
        preview_volume_row.addWidget(self.preview_volume_slider)
        preview_volume_row.addStretch(1)
        transport.addWidget(preview_volume_row_widget, 1, 1, 1, 4)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        self.preview_stack = QStackedWidget()
        single_preview_panel = QWidget()
        single_preview_layout = QVBoxLayout(single_preview_panel)
        single_preview_layout.setContentsMargins(0, 0, 0, 0)
        single_preview_layout.addWidget(self.preview)
        self.preview_stack.addWidget(single_preview_panel)
        comparison_panel = QWidget()
        comparison_layout = QHBoxLayout(comparison_panel)
        comparison_layout.setContentsMargins(0, 0, 0, 0)
        before_panel = QWidget()
        before_layout = QVBoxLayout(before_panel)
        before_layout.setContentsMargins(0, 0, 0, 0)
        before_layout.addWidget(QLabel("Before"))
        before_layout.addWidget(self.before_preview, stretch=1)
        after_panel = QWidget()
        after_layout = QVBoxLayout(after_panel)
        after_layout.setContentsMargins(0, 0, 0, 0)
        after_layout.addWidget(QLabel("After"))
        self.comparison_after_preview = PreviewLabel()
        self.comparison_after_preview.setMinimumSize(320, 180)
        after_layout.addWidget(self.comparison_after_preview, stretch=1)
        comparison_layout.addWidget(before_panel, stretch=1)
        comparison_layout.addWidget(after_panel, stretch=1)
        self.preview_stack.addWidget(comparison_panel)
        self.popout_message = QLabel("Preview is playing in the separate window")
        self.popout_message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_stack.addWidget(self.popout_message)
        preview_layout.addWidget(self.preview_stack, stretch=1)
        self.view_mode_button = QPushButton("Split view")
        self.view_mode_button.clicked.connect(self.toggle_preview_mode)
        preview_buttons = QHBoxLayout()
        preview_buttons.addWidget(self.view_mode_button)
        self.pop_out_button = QPushButton("Float")
        self.pop_out_button.clicked.connect(self.toggle_preview_window)
        preview_buttons.addWidget(self.pop_out_button)
        preview_layout.addLayout(preview_buttons)
        preview_layout.addWidget(self.position_slider)
        trim_row = QHBoxLayout()
        trim_row.addWidget(QLabel("Start"))
        trim_row.addWidget(self.trim_start_spin)
        trim_row.addWidget(QLabel("End"))
        trim_row.addWidget(self.trim_end_spin)
        trim_row.addWidget(self.trim_range_label)
        trim_row.addStretch(1)
        trim_row.addWidget(self.trim_preview_button)
        trim_row.addWidget(self.reset_trim_button)
        preview_layout.addLayout(trim_row)
        preview_layout.addWidget(self.trim_range_slider)
        preview_layout.addWidget(transport_widget)
        preview_layout.addWidget(self.make_media_details())
        self.preview_window.closed.connect(self.on_preview_window_closed)

        controls = self.make_controls_panel()
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(preview_panel)
        self.splitter.addWidget(controls)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([960, 620])
        self.setCentralWidget(self.splitter)

        self.file_menu = self.menuBar().addMenu("&File")
        open_action = QAction("Open Video…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.open_dialog)
        self.file_menu.addAction(open_action)
        self.file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.setShortcut(QKeySequence.StandardKey.Quit)
        exit_action.triggered.connect(self.close)
        self.file_menu.addAction(exit_action)
        presets_menu = self.menuBar().addMenu("&Presets")
        save_preset_action = QAction("Save current settings…", self)
        save_preset_action.triggered.connect(self.save_current_preset)
        presets_menu.addAction(save_preset_action)
        update_preset_action = QAction("Update preset…", self)
        update_preset_action.triggered.connect(self.update_saved_preset)
        presets_menu.addAction(update_preset_action)
        apply_preset_action = QAction("Load preset…", self)
        apply_preset_action.triggered.connect(self.choose_preset_to_apply)
        presets_menu.addAction(apply_preset_action)
        manage_presets_action = QAction("Manage presets…", self)
        manage_presets_action.triggered.connect(self.manage_presets)
        presets_menu.addAction(manage_presets_action)
        about_action = QAction("About Muxy", self)
        about_action.triggered.connect(self.show_about)
        self.menuBar().addAction(about_action)

        self.statusBar().showMessage("Drop a video file anywhere in the window to open it")
        if self.app:
            self.app.styleHints().colorSchemeChanged.connect(self.apply_theme)
        self.apply_theme()

    def system_theme_is_dark(self) -> bool:
        if shutil.which("gsettings"):
            try:
                scheme = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                    capture_output=True,
                    text=True,
                    timeout=0.5,
                    check=False,
                ).stdout.strip().strip("'").lower()
                if scheme == "prefer-dark":
                    return True
                if scheme == "prefer-light":
                    return False

                theme = subprocess.run(
                    ["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                    capture_output=True,
                    text=True,
                    timeout=0.5,
                    check=False,
                ).stdout.strip().strip("'").lower()
                if "dark" in theme:
                    return True
                if "light" in theme:
                    return False
            except (OSError, subprocess.TimeoutExpired):
                pass

        scheme = self.app.styleHints().colorScheme
        if callable(scheme):
            scheme = scheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
        return self.app.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def apply_theme(self, *_args) -> None:
        if not self.app:
            return
        dark = self.system_theme_is_dark()

        if dark:
            window, foreground = "#202124", "#eeeeee"
            field, menu, border = "#303238", "#25272d", "#666970"
            selected, selected_text, highlight = "#8d5a22", "#ffffff", "#bd792b"
            slider_highlight = "#78b7ee"
            track, handle_edge, handle = "#45474d", "#17181b", "#f0f0f0"
            credit = "#a5a5a5"
        else:
            window, foreground = "#f4f5f7", "#202124"
            field, menu, border = "#ffffff", "#ffffff", "#aeb4bd"
            selected, selected_text, highlight = "#d6e5fa", "#17365d", "#bd792b"
            slider_highlight = "#4c91cf"
            track, handle_edge, handle = "#c4c9d1", "#60656d", "#ffffff"
            credit = "#62666d"

        app_palette = QPalette(self.app.palette())
        palette_colors = {
            QPalette.ColorRole.Window: window,
            QPalette.ColorRole.WindowText: foreground,
            QPalette.ColorRole.Base: field,
            QPalette.ColorRole.AlternateBase: menu,
            QPalette.ColorRole.ToolTipBase: field,
            QPalette.ColorRole.ToolTipText: foreground,
            QPalette.ColorRole.Text: foreground,
            QPalette.ColorRole.Button: field,
            QPalette.ColorRole.ButtonText: foreground,
            QPalette.ColorRole.BrightText: selected_text,
            QPalette.ColorRole.Highlight: slider_highlight,
            QPalette.ColorRole.HighlightedText: selected_text,
            QPalette.ColorRole.Link: highlight,
            QPalette.ColorRole.Mid: track,
            QPalette.ColorRole.Dark: border,
        }
        for role, color in palette_colors.items():
            app_palette.setColor(role, QColor(color))
        self.app.setPalette(app_palette)

        stylesheet = (
            f"QMainWindow {{ background: {window}; }} QWidget {{ color: {foreground}; }}"
            f"QGroupBox {{ border: 1px solid {border}; border-radius: 5px; margin-top: 10px;"
            " padding: 8px; font-weight: 600; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }"
            "QPushButton { padding: 6px; }"
            f"QComboBox {{ color: {foreground}; background-color: {field};"
            f" border: 1px solid {border}; border-radius: 4px; padding: 6px; }}"
            f"QComboBox:focus {{ border: 1px solid {highlight}; }}"
            f"QComboBox#containerCombo:focus {{ border: 1px solid {border}; }}"
            f"QComboBox#containerCombo QLineEdit {{ color: {foreground}; background-color: {field};"
            f" selection-color: {selected_text}; selection-background-color: {selected}; border: none; }}"
            f"QComboBox QAbstractItemView {{ color: {foreground}; background-color: {menu};"
            f" selection-color: {selected_text}; selection-background-color: {selected};"
            f" border: 1px solid {border}; outline: 0; }}"
            f"QComboBox QAbstractItemView::item:selected {{ color: {selected_text}; background-color: {selected}; }}"
            f"QProgressBar {{ color: {foreground}; background-color: {field};"
            f" border: 1px solid {border}; border-radius: 4px; text-align: center; }}"
            f"QProgressBar::chunk {{ background-color: {highlight}; border-radius: 3px; }}"
        )
        for window_widget in (self, self.preview_window, self.picture_float_window):
            window_widget.setStyleSheet(stylesheet)

        preview_labels = [
            self.preview,
            self.before_preview,
            self.comparison_after_preview,
            self.preview_window.preview,
            self.preview_window.before_preview,
            self.preview_window.comparison_after_preview,
        ]
        for label in preview_labels:
            label.set_theme(dark)

        slider_palette = QPalette(self.app.palette())
        slider_palette.setColor(QPalette.ColorRole.Mid, QColor(track))
        slider_palette.setColor(QPalette.ColorRole.Highlight, QColor(slider_highlight))
        slider_palette.setColor(QPalette.ColorRole.WindowText, QColor(handle_edge))
        slider_palette.setColor(QPalette.ColorRole.Base, QColor(handle))
        self.trim_range_slider.setPalette(slider_palette)
        self.trim_range_slider.update()

        if hasattr(self, "app_credit"):
            self.app_credit.setStyleSheet(f"color: {credit}; font-size: 10pt;")

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "About Muxy",
            "Muxy is a desktop video conversion and editing tool powered by FFmpeg. "
            "It lets you preview videos, adjust picture settings, choose video and "
            "audio options, manage embedded subtitle tracks, and export to a range "
            "of formats.\n\n"
            "Muxy is designed to make common video tasks easier while giving you "
            "control over the settings that matter.\n\n"
            f"Version {APP_VERSION}",
        )

    def prepare_confirmation_dialog(self, dialog: QMessageBox) -> None:
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

    def confirm_action(self, title: str, text: str) -> bool:
        dialog = QMessageBox(
            QMessageBox.Icon.Question,
            title,
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            self,
        )
        dialog.setDefaultButton(QMessageBox.StandardButton.No)
        self.prepare_confirmation_dialog(dialog)
        return dialog.exec() == QMessageBox.StandardButton.Yes

    def load_presets(self) -> dict[str, dict]:
        try:
            if not self.presets_file.is_file():
                return {}
            stored = json.loads(self.presets_file.read_text(encoding="utf-8"))
            presets = stored.get("presets", {}) if isinstance(stored, dict) else {}
            if isinstance(presets, dict):
                return {
                    str(name): value
                    for name, value in presets.items()
                    if isinstance(name, str) and isinstance(value, dict)
                }
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def write_presets(self) -> bool:
        temporary = self.presets_file.with_suffix(".tmp")
        try:
            self.presets_file.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps({"schema_version": 1, "presets": self.presets}, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.presets_file)
            return True
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            QMessageBox.critical(self, "Could not save presets", str(exc))
            return False

    def stream_descriptors(self, kind: str) -> list[dict]:
        streams = [
            stream for stream in self.media_info.get("streams", [])
            if stream.get("codec_type") == kind
        ]
        descriptors = []
        for ordinal, stream in enumerate(streams):
            tags = stream.get("tags", {})
            descriptors.append({
                "index": int(stream["index"]),
                "ordinal": ordinal,
                "codec": str(stream.get("codec_name", "")).casefold(),
                "language": str(tags.get("language", "und")).casefold(),
                "title": str(tags.get("title", "")).casefold(),
                "channels": stream.get("channels"),
                "channel_layout": str(stream.get("channel_layout", "")).casefold(),
            })
        return descriptors

    def capture_track_policy(self, kind: str, checks: list[tuple[int, QCheckBox]]) -> dict:
        descriptors = self.stream_descriptors(kind)
        selected_indices = {index for index, check in checks if check.isChecked()}
        if not checks or len(selected_indices) == len(checks):
            return {"mode": "all", "tracks": []}
        if not selected_indices:
            return {"mode": "none", "tracks": []}
        return {
            "mode": "match",
            "tracks": [
                {key: value for key, value in descriptor.items() if key != "index"}
                for descriptor in descriptors
                if descriptor["index"] in selected_indices
            ],
        }

    def current_preset_data(self) -> dict:
        return {
            "picture": {
                key: slider.value() for key, slider in self.adjustment_sliders.items()
            },
            "video": {
                "container": self.format_combo.currentText(),
                "codec": self.video_codec_combo.currentData(),
                "quality": self.rf_slider.value(),
                "frame_rate": self.frame_rate_combo.currentData(),
                "color_range": self.color_range_combo.currentData(),
            },
            "audio": {
                "codec": self.audio_codec_combo.currentData(),
                "channels": self.audio_channels_combo.currentData(),
                "bitrate": self.audio_bitrate_combo.currentData(),
                "tracks": self.capture_track_policy("audio", self.audio_checks),
            },
            "subtitles": {
                "tracks": self.capture_track_policy("subtitle", self.subtitle_checks),
            },
            "export": {"web_optimize": self.web_optimize_check.isChecked()},
        }

    def save_current_preset(self) -> None:
        if not self.current_path:
            QMessageBox.information(
                self, "Load a video", "Load a video before saving a preset so its track choices can be recorded."
            )
            return
        name, accepted = QInputDialog.getText(self, "Save preset", "Preset name:")
        name = name.strip()
        if not accepted or not name:
            return
        if name in self.presets and not self.confirm_action(
            "Replace preset?", f"A preset named ‘{name}’ already exists. Replace it?"
        ):
            return
        self.presets[name] = self.current_preset_data()
        if self.write_presets():
            self.statusBar().showMessage(f"Saved preset ‘{name}’", 5000)

    def update_saved_preset(self) -> None:
        if not self.current_path:
            QMessageBox.information(self, "Load a video", "Load a video before updating a preset.")
            return
        if not self.presets:
            QMessageBox.information(self, "No presets", "Save a preset first.")
            return
        name, accepted = QInputDialog.getItem(
            self, "Update preset", "Choose the preset to overwrite:", sorted(self.presets), 0, False
        )
        if not accepted or not name:
            return
        if not self.confirm_action(
            "Update preset?",
            f"Replace ‘{name}’ with the settings currently shown across all tabs?",
        ):
            return
        self.presets[name] = self.current_preset_data()
        if self.write_presets():
            self.statusBar().showMessage(f"Updated preset ‘{name}’", 5000)

    def choose_preset_to_apply(self) -> None:
        if not self.presets:
            QMessageBox.information(self, "No presets", "Save a preset first.")
            return
        if not self.current_path:
            QMessageBox.information(self, "Load a video", "Load a video before applying a preset.")
            return
        name, accepted = QInputDialog.getItem(
            self, "Apply preset", "Choose a preset:", sorted(self.presets), 0, False
        )
        if accepted and name:
            self.apply_preset(name)

    @staticmethod
    def track_match_score(saved: dict, current: dict) -> int:
        title = saved.get("title", "")
        language = saved.get("language", "und")
        score = 0
        if title and title == current.get("title", ""):
            score += 100
        if language not in ("", "und", "unknown") and language == current.get("language"):
            score += 20
        if saved.get("codec") and saved.get("codec") == current.get("codec"):
            score += 10
        if (
            saved.get("channel_layout")
            and saved.get("channel_layout") == current.get("channel_layout")
        ):
            score += 3
        if saved.get("channels") and saved.get("channels") == current.get("channels"):
            score += 2
        if saved.get("ordinal") == current.get("ordinal"):
            score += 1
        if score >= 10 or (language in ("", "und", "unknown") and score >= 1):
            return score
        return 0

    def apply_track_policy(
        self,
        policy: dict,
        kind: str,
        checks: list[tuple[int, QCheckBox]],
        warnings: list[str],
    ) -> None:
        mode = policy.get("mode", "all")
        if mode == "all":
            for _, check in checks:
                check.setChecked(True)
            return
        if mode == "none":
            for _, check in checks:
                check.setChecked(False)
            return
        saved_tracks = policy.get("tracks", [])
        current_tracks = self.stream_descriptors(kind)
        selected: set[int] = set()
        unused = list(current_tracks)
        for saved in saved_tracks:
            if not isinstance(saved, dict) or not unused:
                continue
            best = max(unused, key=lambda current: self.track_match_score(saved, current))
            if self.track_match_score(saved, best) > 0:
                selected.add(best["index"])
                unused.remove(best)
        if saved_tracks and not selected and checks:
            selected = {index for index, _ in checks}
            warnings.append(
                f"No matching {kind} tracks were found; all available {kind} tracks were kept."
            )
        for index, check in checks:
            check.setChecked(index in selected)

    def apply_preset(self, name: str) -> None:
        if not self.current_path:
            QMessageBox.information(self, "Load a video", "Load a video before applying a preset.")
            return
        preset = self.presets.get(name)
        if not preset:
            return
        warnings: list[str] = []
        picture = preset.get("picture", {})
        for key, value in picture.items():
            if key in self.adjustment_sliders:
                self.adjustment_sliders[key].setValue(int(value))

        video = preset.get("video", {})
        container_index = self.format_combo.findText(str(video.get("container", "")))
        if container_index >= 0:
            self.format_combo.setCurrentIndex(container_index)
        else:
            warnings.append("The saved container is unavailable; the current container was kept.")
        codec_index = self.video_codec_combo.findData(video.get("codec"))
        if codec_index >= 0:
            self.video_codec_combo.setCurrentIndex(codec_index)
        else:
            warnings.append("The saved video encoder is unavailable; the current encoder was kept.")
        self.update_video_controls_enabled()
        self.rf_slider.setValue(int(video.get("quality", self.rf_slider.value())))
        frame_rate_index = self.frame_rate_combo.findData(video.get("frame_rate"))
        if frame_rate_index >= 0:
            self.frame_rate_combo.setCurrentIndex(frame_rate_index)
        color_range_index = self.color_range_combo.findData(video.get("color_range"))
        if color_range_index >= 0:
            self.color_range_combo.setCurrentIndex(color_range_index)

        audio = preset.get("audio", {})
        audio_codec_index = self.audio_codec_combo.findData(audio.get("codec"))
        if audio_codec_index >= 0:
            self.audio_codec_combo.setCurrentIndex(audio_codec_index)
        else:
            warnings.append("The saved audio encoder is unavailable; the current encoder was kept.")
        saved_channels = audio.get("channels")
        if saved_channels == 0:
            saved_channels = 2
            warnings.append(
                "The saved ‘Match source track layout’ option is no longer available; Stereo was selected."
            )
        channels_index = self.audio_channels_combo.findData(saved_channels)
        if channels_index >= 0:
            self.audio_channels_combo.setCurrentIndex(channels_index)
        bitrate_index = self.audio_bitrate_combo.findData(audio.get("bitrate"))
        if bitrate_index >= 0:
            self.audio_bitrate_combo.setCurrentIndex(bitrate_index)
        self.apply_track_policy(
            audio.get("tracks", {}), "audio", self.audio_checks, warnings
        )
        subtitles = preset.get("subtitles", {})
        self.apply_track_policy(
            subtitles.get("tracks", {}), "subtitle", self.subtitle_checks, warnings
        )

        self.update_audio_layout_enabled()
        self.update_web_optimize_enabled()
        requested_web_optimize = bool(
            preset.get("export", {}).get("web_optimize", False)
        )
        if requested_web_optimize and self.web_optimize_check.isEnabled():
            self.web_optimize_check.setChecked(True)
        else:
            self.web_optimize_check.setChecked(False)
            if requested_web_optimize:
                warnings.append("Web optimisation only applies to MP4 and MOV; it was turned off.")
        self.output_path = None
        self.statusBar().showMessage(f"Applied preset ‘{name}’", 5000)
        if warnings:
            QMessageBox.information(self, "Preset applied with adjustments", "\n".join(warnings))

    def manage_presets(self) -> None:
        while self.presets:
            name, accepted = QInputDialog.getItem(
                self, "Manage presets", "Choose a preset:", sorted(self.presets), 0, False
            )
            if not accepted or not name:
                return
            action, accepted = QInputDialog.getItem(
                self,
                "Manage presets",
                f"What would you like to do with ‘{name}’?",
                ["Rename", "Delete", "Done"],
                0,
                False,
            )
            if not accepted or action == "Done":
                return
            if action == "Delete":
                if self.confirm_action("Delete preset?", f"Delete ‘{name}’?"):
                    del self.presets[name]
                    if self.write_presets():
                        self.statusBar().showMessage(f"Deleted preset ‘{name}’", 5000)
                continue
            new_name, accepted = QInputDialog.getText(
                self, "Rename preset", "New preset name:", text=name
            )
            new_name = new_name.strip()
            if not accepted or not new_name or new_name == name:
                continue
            if new_name in self.presets and not self.confirm_action(
                "Replace preset?",
                f"A preset named ‘{new_name}’ already exists. Replace it?",
            ):
                continue
            self.presets[new_name] = self.presets.pop(name)
            if self.write_presets():
                self.statusBar().showMessage(f"Renamed preset to ‘{new_name}’", 5000)
        QMessageBox.information(self, "No presets", "There are no presets to manage.")

    def make_media_details(self) -> QWidget:
        box = QGroupBox("Loaded media")
        layout = QVBoxLayout(box)
        self.media_label = QLabel("No file loaded")
        self.media_label.setWordWrap(True)
        layout.addWidget(self.media_label)
        return box

    def make_controls_panel(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(580)
        panel = QWidget()
        panel.setMinimumWidth(550)
        layout = QVBoxLayout(panel)
        self.export_tabs = QTabWidget()

        summary_tab = QWidget()
        summary_layout = QVBoxLayout(summary_tab)
        summary_file_group = QGroupBox("File")
        summary_file_layout = QVBoxLayout(summary_file_group)
        self.summary_file_label = QLabel("Load a video to see its file details.")
        self.summary_file_label.setWordWrap(True)
        self.summary_file_label.setTextFormat(Qt.TextFormat.PlainText)
        summary_file_layout.addWidget(self.summary_file_label)
        summary_video_group = QGroupBox("Video")
        summary_video_layout = QVBoxLayout(summary_video_group)
        self.summary_video_label = QLabel("No video information available.")
        self.summary_video_label.setWordWrap(True)
        self.summary_video_label.setTextFormat(Qt.TextFormat.PlainText)
        summary_video_layout.addWidget(self.summary_video_label)
        summary_audio_group = QGroupBox("Audio tracks")
        summary_audio_layout = QVBoxLayout(summary_audio_group)
        self.summary_audio_label = QLabel("No audio tracks found.")
        self.summary_audio_label.setWordWrap(True)
        self.summary_audio_label.setTextFormat(Qt.TextFormat.PlainText)
        summary_audio_layout.addWidget(self.summary_audio_label)
        summary_subtitle_group = QGroupBox("Subtitle tracks")
        summary_subtitle_layout = QVBoxLayout(summary_subtitle_group)
        self.summary_subtitle_label = QLabel("No subtitle tracks found.")
        self.summary_subtitle_label.setWordWrap(True)
        self.summary_subtitle_label.setTextFormat(Qt.TextFormat.PlainText)
        summary_subtitle_layout.addWidget(self.summary_subtitle_label)
        for group in (summary_file_group, summary_video_group, summary_audio_group, summary_subtitle_group):
            summary_layout.addWidget(group)
        summary_layout.addStretch(1)
        self.app_credit = QLabel("An App by Pags")
        self.app_credit.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        summary_layout.addWidget(self.app_credit)

        picture_tab = QWidget()
        picture_layout = QVBoxLayout(picture_tab)
        self.adjustments_group = QGroupBox("Picture adjustments")
        grid = QGridLayout(self.adjustments_group)
        self.adjustment_sliders: dict[str, QSlider] = {}
        self.adjustment_values: dict[str, QLabel] = {}
        for row, (key, title, low, high, initial) in enumerate(
            [
                ("brightness", "Brightness", -100, 100, 0),
                ("contrast", "Contrast", -100, 100, 0),
                ("saturation", "Saturation", -100, 100, 0),
                ("sharpness", "Sharpness", 0, 100, 0),
                ("denoise", "Denoise", 0, 100, 0),
            ]
        ):
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(low, high)
            slider.setValue(initial)
            value = QLabel(str(initial))
            slider.valueChanged.connect(lambda n, label=value: label.setText(str(n)))
            slider.valueChanged.connect(self.redraw_current_frame)
            self.adjustment_sliders[key] = slider
            self.adjustment_values[key] = value
            grid.addWidget(QLabel(title), row, 0)
            grid.addWidget(slider, row, 1)
            grid.addWidget(value, row, 2)
        reset_button = QPushButton("Reset all")
        reset_button.setFixedWidth(96)
        reset_button.clicked.connect(self.reset_adjustments)
        self.float_picture_button = QPushButton("Float")
        self.float_picture_button.setFixedWidth(84)
        self.float_picture_button.clicked.connect(self.toggle_picture_controls)
        self.picture_view_mode_button = QPushButton("Split view")
        self.picture_view_mode_button.clicked.connect(self.toggle_preview_mode)
        denoise_note = QLabel("It is recommended to pause the video when using Denoise.")
        denoise_note.setWordWrap(True)
        denoise_note.setStyleSheet("font-style: italic;")
        grid.addWidget(denoise_note, 5, 0, 1, 3)
        picture_actions = QHBoxLayout()
        picture_actions.addWidget(self.picture_view_mode_button)
        picture_actions.addStretch(1)
        picture_actions.addWidget(reset_button)
        picture_actions.addWidget(self.float_picture_button)
        grid.addLayout(picture_actions, 6, 0, 1, 3)
        picture_transport_widget = QWidget()
        picture_transport = QGridLayout(picture_transport_widget)
        picture_transport.setContentsMargins(0, 0, 0, 0)
        picture_transport.setHorizontalSpacing(6)
        picture_transport.setVerticalSpacing(4)
        picture_transport.setColumnStretch(0, 1)
        picture_transport.setColumnStretch(4, 1)
        self.picture_seek_back_button = QPushButton()
        self.picture_seek_back_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSeekBackward)
        )
        self.picture_seek_back_button.setIconSize(QSize(20, 20))
        self.picture_seek_back_button.setFixedSize(44, 36)
        self.picture_seek_back_button.setToolTip("Rewind 5 seconds")
        self.picture_seek_back_button.setAccessibleName("Rewind 5 seconds")
        self.picture_seek_back_button.clicked.connect(lambda: self.seek_by(-5000))
        self.picture_play_button = QPushButton()
        self.picture_play_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self.picture_play_button.setIconSize(QSize(20, 20))
        self.picture_play_button.setFixedSize(44, 36)
        self.picture_play_button.setToolTip("Play video")
        self.picture_play_button.setAccessibleName("Play video")
        self.picture_play_button.clicked.connect(self.toggle_playback)
        self.picture_seek_forward_button = QPushButton()
        self.picture_seek_forward_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaSeekForward)
        )
        self.picture_seek_forward_button.setIconSize(QSize(20, 20))
        self.picture_seek_forward_button.setFixedSize(44, 36)
        self.picture_seek_forward_button.setToolTip("Forward 5 seconds")
        self.picture_seek_forward_button.setAccessibleName("Forward 5 seconds")
        self.picture_seek_forward_button.clicked.connect(lambda: self.seek_by(5000))
        picture_transport.addWidget(
            self.picture_seek_back_button, 0, 1, alignment=Qt.AlignmentFlag.AlignCenter
        )
        picture_transport.addWidget(
            self.picture_play_button, 0, 2, alignment=Qt.AlignmentFlag.AlignCenter
        )
        picture_transport.addWidget(
            self.picture_seek_forward_button, 0, 3, alignment=Qt.AlignmentFlag.AlignCenter
        )
        self.picture_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.picture_volume_slider.setRange(0, 100)
        self.picture_volume_slider.setValue(100)
        self.picture_volume_slider.setToolTip("Preview playback volume")
        self.picture_volume_slider.valueChanged.connect(self.set_preview_volume)
        picture_volume_icon = QLabel()
        picture_volume_icon.setPixmap(
            self.style()
            .standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
            .pixmap(QSize(18, 18))
        )
        picture_volume_icon.setToolTip("Preview playback volume")
        picture_volume_icon.setAccessibleName("Preview playback volume")
        picture_volume_row_widget = QWidget()
        picture_volume_row = QHBoxLayout(picture_volume_row_widget)
        picture_volume_row.setContentsMargins(0, 0, 0, 0)
        picture_volume_row.setSpacing(2)
        picture_volume_row.addSpacing(
            (self.picture_seek_back_button.width() - picture_volume_icon.sizeHint().width()) // 2
        )
        picture_volume_row.addWidget(picture_volume_icon)
        picture_volume_row.addWidget(self.picture_volume_slider)
        picture_volume_row.addStretch(1)
        picture_transport.addWidget(picture_volume_row_widget, 1, 1, 1, 3)
        grid.addWidget(picture_transport_widget, 7, 0, 2, 3)
        self.picture_position_slider = QSlider(Qt.Orientation.Horizontal)
        self.picture_position_slider.setRange(0, 0)
        self.picture_position_slider.setToolTip("Scrub through the video")
        self.picture_position_slider.sliderMoved.connect(self.player.setPosition)
        grid.addWidget(self.picture_position_slider, 9, 0, 1, 3)
        grid.setColumnMinimumWidth(2, 38)
        grid.setColumnStretch(1, 1)
        picture_layout.addWidget(self.adjustments_group)
        picture_layout.addStretch(1)
        self.picture_tab = picture_tab
        self.picture_layout = picture_layout
        self.picture_float_window = QDialog(self, Qt.WindowType.Window)
        self.picture_float_window.setWindowTitle("Muxy Picture Controls")
        self.picture_float_window.setWindowFlag(
            Qt.WindowType.WindowStaysOnTopHint, True
        )
        self.picture_float_window.setFixedSize(520, 350)
        self.picture_float_layout = QVBoxLayout(self.picture_float_window)
        self.picture_float_window.finished.connect(self.restore_picture_controls)

        video_tab = QWidget()
        video_tab_layout = QGridLayout(video_tab)
        self.format_combo = QComboBox()
        self.format_combo.setObjectName("containerCombo")
        self.format_combo.addItems(
            ["mp4", "mkv", "mov", "webm", "avi", "mpeg", "ts", "flv", "ogv"]
        )
        self.format_combo.view().setMouseTracking(True)
        self.format_combo.view().viewport().setMouseTracking(True)
        self.video_codec_combo = QComboBox()
        self.video_codec_combo.setToolTip(
            "Passthrough copies the video stream without re-encoding. Selecting it also switches "
            "audio to passthrough; you can choose an audio encoder separately on the Audio tab. "
            "H.264 NVENC uses 8-bit output, so 10-bit input is converted and its full bit depth "
            "is not retained."
        )
        self.video_codec_combo.currentIndexChanged.connect(
            self.update_video_controls_enabled
        )

        self.rf_slider = QSlider(Qt.Orientation.Horizontal)
        self.rf_slider.setRange(0, 51)
        self.rf_slider.setValue(22)
        self.rf_value_label = QLabel("22")
        self.rf_slider.setToolTip(
            "Quality scale: lower values produce higher quality and larger files. "
            "Software H.264/H.265 use CRF; NVIDIA encoders use constant-quality CQ. "
            "The exact quality represented by a value varies by encoder; NVENC uses CQ 1 "
            "for RF 0 because CQ 0 means automatic."
        )
        self.rf_slider.valueChanged.connect(
            lambda value: self.rf_value_label.setText(str(value))
        )

        self.frame_rate_combo = QComboBox()
        self.frame_rate_combo.addItem("Same as source", None)
        for label, value in (
            ("15 fps", "15"),
            ("23.976 fps", "24000/1001"),
            ("24 fps", "24"),
            ("25 fps", "25"),
            ("29.97 fps", "30000/1001"),
            ("30 fps", "30"),
            ("48 fps", "48"),
            ("50 fps", "50"),
            ("59.94 fps", "60000/1001"),
            ("60 fps", "60"),
            ("100 fps", "100"),
            ("120 fps", "120"),
        ):
            self.frame_rate_combo.addItem(label, value)

        self.color_range_combo = QComboBox()
        self.color_range_combo.addItem("Same as source", None)
        self.color_range_combo.addItem("Limited (TV)", "tv")

        video_tab_layout.addWidget(QLabel("Container"), 0, 0)
        video_tab_layout.addWidget(self.format_combo, 0, 1)
        video_tab_layout.addWidget(QLabel("Video encoder"), 1, 0)
        video_tab_layout.addWidget(self.video_codec_combo, 1, 1)
        video_tab_layout.addWidget(QLabel("RF / quality"), 2, 0)
        video_tab_layout.addWidget(self.rf_slider, 2, 1)
        video_tab_layout.addWidget(self.rf_value_label, 2, 2)
        video_tab_layout.addWidget(QLabel("Frame rate"), 3, 0)
        video_tab_layout.addWidget(self.frame_rate_combo, 3, 1, 1, 2)
        video_tab_layout.addWidget(QLabel("Color range"), 4, 0)
        video_tab_layout.addWidget(self.color_range_combo, 4, 1, 1, 2)
        video_tab_layout.setColumnMinimumWidth(2, 38)
        video_tab_layout.setColumnStretch(1, 1)
        video_tab_layout.setRowStretch(5, 1)

        audio_tab = QWidget()
        audio_tab_layout = QVBoxLayout(audio_tab)
        source_audio_group = QGroupBox("Audio in the original file")
        source_audio_layout = QVBoxLayout(source_audio_group)
        self.source_audio_info = QLabel("Load a video to inspect its audio tracks.")
        self.source_audio_info.setWordWrap(True)
        source_audio_layout.addWidget(self.source_audio_info)
        audio_tab_layout.addWidget(source_audio_group)

        audio_controls = QGroupBox("Export audio")
        audio_controls_layout = QGridLayout(audio_controls)
        self.audio_codec_combo = QComboBox()
        self.audio_channels_combo = QComboBox()
        self.audio_channels_combo.addItem("Mono (1.0)", 1)
        self.audio_channels_combo.addItem("Stereo (2.0)", 2)
        self.audio_channels_combo.addItem("5.1 surround", 6)
        self.audio_channels_combo.addItem("7.1 surround", 8)
        self.audio_channels_combo.setCurrentIndex(self.audio_channels_combo.findData(2))
        self.audio_bitrate_combo = QComboBox()
        for bitrate in (64, 96, 128, 160, 192, 224, 256, 320, 384):
            self.audio_bitrate_combo.addItem(f"{bitrate} kb/s", bitrate)
        self.audio_bitrate_combo.setCurrentIndex(
            self.audio_bitrate_combo.findData(256)
        )
        self.audio_codec_combo.currentIndexChanged.connect(self.update_audio_layout_enabled)
        self.load_encoder_choices()

        audio_controls_layout.addWidget(QLabel("Encoder"), 0, 0)
        audio_controls_layout.addWidget(self.audio_codec_combo, 0, 1)
        audio_controls_layout.addWidget(QLabel("Output layout"), 1, 0)
        audio_controls_layout.addWidget(self.audio_channels_combo, 1, 1)
        audio_controls_layout.addWidget(QLabel("AAC bitrate per track"), 2, 0)
        audio_controls_layout.addWidget(self.audio_bitrate_combo, 2, 1)
        audio_tab_layout.addWidget(audio_controls)

        audio_tracks_group = QGroupBox("Audio tracks (checked = keep)")
        self.audio_tracks_layout = QVBoxLayout(audio_tracks_group)
        self.audio_tracks_layout.addWidget(QLabel("Load a video to inspect its tracks."))
        audio_tab_layout.addWidget(audio_tracks_group)
        audio_tab_layout.addStretch(1)

        subtitles_tab = QWidget()
        subtitles_tab_layout = QVBoxLayout(subtitles_tab)
        subtitles_tab_layout.addWidget(
            QLabel("Choose which embedded subtitle tracks to include in the export.")
        )
        subtitle_tracks_group = QGroupBox("Subtitle tracks (checked = keep)")
        self.subtitle_tracks_layout = QVBoxLayout(subtitle_tracks_group)
        self.subtitle_tracks_layout.addWidget(
            QLabel("Load a video to inspect its subtitle tracks.")
        )
        subtitles_tab_layout.addWidget(subtitle_tracks_group)
        subtitles_tab_layout.addStretch(1)

        export_tab = QWidget()
        export_layout = QVBoxLayout(export_tab)
        self.output_path: str | None = None
        self.export_button = QPushButton("Encode video")
        self.export_button.clicked.connect(self.toggle_export)
        self.export_progress = QProgressBar()
        self.export_progress.setRange(0, 100)
        self.export_progress.setValue(0)
        self.export_estimate = QLabel("Estimated time remaining: —")
        video_review_group = QGroupBox("Video")
        video_review_layout = QVBoxLayout(video_review_group)
        video_review_layout.setContentsMargins(16, 6, 6, 6)
        video_review_layout.setSpacing(2)
        self.video_review_rows: dict[str, QLabel] = {}
        for setting in ("Container", "Video", "RF", "Frame rate", "Color"):
            row = QLabel()
            self.video_review_rows[setting] = row
            video_review_layout.addWidget(row)
        audio_review_group = QGroupBox("Audio")
        audio_review_layout = QVBoxLayout(audio_review_group)
        audio_review_layout.setContentsMargins(16, 6, 6, 6)
        audio_review_layout.setSpacing(2)
        self.audio_review_rows: dict[str, QLabel] = {}
        for setting in ("Encoder", "Output layout", "Bit rate", "Tracks"):
            row = QLabel()
            self.audio_review_rows[setting] = row
            audio_review_layout.addWidget(row)
        subtitle_review_group = QGroupBox("Subtitles")
        subtitle_review_layout = QVBoxLayout(subtitle_review_group)
        subtitle_review_layout.setContentsMargins(16, 6, 6, 6)
        subtitle_review_layout.setSpacing(2)
        self.subtitle_review_tracks_heading = QLabel("Tracks:")
        subtitle_review_layout.addWidget(self.subtitle_review_tracks_heading)
        self.subtitle_review_tracks_scroll = QScrollArea()
        self.subtitle_review_tracks_scroll.setWidgetResizable(True)
        self.subtitle_review_tracks_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.subtitle_review_tracks_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.subtitle_review_tracks_scroll.setMinimumHeight(0)
        self.subtitle_review_tracks_scroll.setMaximumHeight(140)
        self.subtitle_review_tracks_widget = QWidget()
        self.subtitle_review_tracks_layout = QVBoxLayout(self.subtitle_review_tracks_widget)
        self.subtitle_review_tracks_layout.setContentsMargins(10, 0, 0, 0)
        self.subtitle_review_tracks_layout.setSpacing(2)
        self.subtitle_review_tracks_scroll.setWidget(self.subtitle_review_tracks_widget)
        subtitle_review_layout.addWidget(self.subtitle_review_tracks_scroll)
        self.format_combo.currentTextChanged.connect(self.update_export_video_summary)
        self.video_codec_combo.currentIndexChanged.connect(self.update_export_video_summary)
        self.rf_slider.valueChanged.connect(self.update_export_video_summary)
        self.frame_rate_combo.currentIndexChanged.connect(self.update_export_video_summary)
        self.color_range_combo.currentIndexChanged.connect(self.update_export_video_summary)
        self.audio_codec_combo.currentIndexChanged.connect(self.update_export_audio_summary)
        self.audio_channels_combo.currentIndexChanged.connect(self.update_export_audio_summary)
        self.audio_bitrate_combo.currentIndexChanged.connect(self.update_export_audio_summary)
        self.web_optimize_check = QCheckBox("Web optimised (faststart)")
        self.web_optimize_check.setToolTip(
            "For MP4 and MOV, move the movie index to the beginning of the file "
            "so playback can start before the whole file downloads. FFmpeg performs a second pass."
        )
        self.format_combo.currentTextChanged.connect(self.update_web_optimize_enabled)
        self.update_web_optimize_enabled()
        self.update_export_video_summary()
        self.update_export_audio_summary()
        self.update_export_subtitle_summary()
        output_row = QHBoxLayout()
        output_row.addWidget(self.export_button)
        export_layout.addWidget(video_review_group)
        export_layout.addWidget(audio_review_group)
        export_layout.addWidget(subtitle_review_group)
        export_layout.addWidget(self.web_optimize_check)
        export_layout.addSpacing(5 * self.web_optimize_check.fontMetrics().lineSpacing())
        export_layout.addLayout(output_row)
        export_layout.addWidget(self.export_progress)
        export_layout.addWidget(self.export_estimate)
        export_layout.addStretch(1)

        presets_tab = QWidget()
        presets_layout = QVBoxLayout(presets_tab)
        preset_actions = QGridLayout()
        save_preset_button = QPushButton("Save current settings")
        save_preset_button.clicked.connect(self.save_current_preset)
        update_preset_button = QPushButton("Update saved preset")
        update_preset_button.clicked.connect(self.update_saved_preset)
        load_preset_button = QPushButton("Load preset")
        load_preset_button.clicked.connect(self.choose_preset_to_apply)
        manage_presets_button = QPushButton("Rename or delete presets")
        manage_presets_button.clicked.connect(self.manage_presets)
        for row, button in enumerate(
            (save_preset_button, update_preset_button, load_preset_button, manage_presets_button)
        ):
            preset_actions.addWidget(button, row // 2, row % 2)
        presets_layout.addSpacing(2 * save_preset_button.fontMetrics().lineSpacing())
        presets_layout.addLayout(preset_actions)
        presets_layout.addStretch(1)

        self.export_tabs.addTab(summary_tab, "Summary")
        self.export_tabs.addTab(picture_tab, "Picture")
        self.export_tabs.addTab(video_tab, "Video")
        self.export_tabs.addTab(audio_tab, "Audio")
        self.export_tabs.addTab(subtitles_tab, "Subtitles")
        self.export_tab_index = self.export_tabs.addTab(export_tab, "Export")
        self.export_tabs.addTab(presets_tab, "Presets")
        layout.addWidget(self.export_tabs)

        scroll.setWidget(panel)
        return scroll

    def reset_adjustments(self) -> None:
        for slider in self.adjustment_sliders.values():
            slider.setValue(0)

    def toggle_picture_controls(self) -> None:
        if self.adjustments_group.parentWidget() is self.picture_float_window:
            self.picture_float_window.accept()
            return
        self.picture_layout.removeWidget(self.adjustments_group)
        self.adjustments_group.setParent(self.picture_float_window)
        self.picture_float_layout.addWidget(self.adjustments_group)
        self.float_picture_button.setText("Restore")
        self.picture_float_window.show()
        self.picture_float_window.raise_()
        self.picture_float_window.activateWindow()

    def restore_picture_controls(self, _result: int = 0) -> None:
        if self.adjustments_group.parentWidget() is not self.picture_float_window:
            return
        self.picture_float_layout.removeWidget(self.adjustments_group)
        self.adjustments_group.setParent(self.picture_tab)
        self.picture_layout.insertWidget(0, self.adjustments_group)
        self.float_picture_button.setText("Float")

    def load_encoder_choices(self) -> None:
        try:
            videos, audios = ffmpeg_encoders()
            if not videos or not audios:
                raise RuntimeError("FFmpeg did not report audio and video encoders")
            video_choices = [
                ("H.264 (software)", "libx264"),
                ("H.265 / HEVC (software)", "libx265"),
                ("VP9 (software)", "libvpx-vp9"),
                ("NVIDIA NVENC H.264", "h264_nvenc"),
                ("NVIDIA NVENC H.265 / HEVC", "hevc_nvenc"),
                ("Passthrough (copy video)", "copy"),
            ]
            for label, encoder in video_choices:
                if encoder == "copy" or encoder in videos:
                    self.video_codec_combo.addItem(label, encoder)
            self.audio_codec_combo.addItem("AAC", "aac")
            for label, encoder in (("Opus", "libopus"), ("Vorbis", "libvorbis")):
                if encoder in audios:
                    self.audio_codec_combo.addItem(label, encoder)
            self.audio_codec_combo.addItem("Passthrough (copy audio)", "copy")
            if "aac" not in audios:
                self.audio_codec_combo.model().item(0).setEnabled(False)
            self.video_codec_combo.setCurrentIndex(
                max(0, self.video_codec_combo.findData("libx264"))
            )
            self.audio_codec_combo.setCurrentIndex(
                max(0, self.audio_codec_combo.findData("aac"))
            )
            self.update_audio_layout_enabled()
            self.update_video_controls_enabled()
        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
            self.statusBar().showMessage(f"Could not list FFmpeg encoders: {exc}")

    def update_audio_layout_enabled(self, _index: int | None = None) -> None:
        encoding_aac = self.audio_codec_combo.currentData() != "copy"
        self.audio_channels_combo.setEnabled(encoding_aac)
        self.audio_bitrate_combo.setEnabled(encoding_aac)
        self.update_container_options()

    def update_video_controls_enabled(self, _index: int | None = None) -> None:
        video_codec = self.video_codec_combo.currentData()
        if video_codec in {"h264_nvenc", "hevc_nvenc"}:
            self.rf_slider.setValue(27)
        elif video_codec in {"libx264", "libx265", "libvpx-vp9"}:
            self.rf_slider.setValue(22)
        self.rf_slider.setEnabled(video_codec != "copy")
        encoding_video = video_codec != "copy"
        self.frame_rate_combo.setEnabled(encoding_video)
        self.color_range_combo.setEnabled(encoding_video)
        if video_codec == "copy" and self.audio_codec_combo.currentData() != "copy":
            copy_index = self.audio_codec_combo.findData("copy")
            if copy_index >= 0:
                self.audio_codec_combo.setCurrentIndex(copy_index)
        self.update_container_options()

    @staticmethod
    def codec_supported_by_container(container: str, kind: str, codec: str) -> bool:
        """Conservative codec support check for stream-copy container choices."""
        container = container.lower()
        codec = codec.lower()
        video_codecs = {
            "mp4": {"h264", "hevc", "mpeg4", "mpeg2video", "mjpeg", "av1", "vp9"},
            "mkv": None,
            "mov": {"h264", "hevc", "mpeg4", "mpeg2video", "mjpeg", "prores", "av1", "dvvideo"},
            "webm": {"vp8", "vp9", "av1"},
            "avi": {"h264", "mpeg4", "mpeg1video", "mpeg2video", "mjpeg", "dvvideo", "rawvideo"},
            "mpeg": {"mpeg1video", "mpeg2video"},
            "ts": {"h264", "hevc", "mpeg1video", "mpeg2video", "av1"},
            "flv": {"h264", "flv1", "vp6", "vp6f"},
            "ogv": {"theora"},
        }
        audio_codecs = {
            "mp4": {"aac", "mp3", "ac3", "eac3", "alac"},
            "mkv": None,
            "mov": {"aac", "mp3", "ac3", "eac3", "alac"},
            "webm": {"opus", "vorbis"},
            "avi": {"mp3", "mp2", "ac3"},
            "mpeg": {"mp2", "mp3", "ac3"},
            "ts": {"aac", "ac3", "eac3", "mp2", "mp3"},
            "flv": {"aac", "mp3"},
            "ogv": {"vorbis", "opus"},
        }
        mp4_text_subtitle_inputs = {
            "mov_text",
            "webvtt",
            "subrip",
            "ass",
            "ssa",
            "text",
            "sami",
            "microdvd",
            "mpl2",
            "jacosub",
            "realtext",
            "subviewer",
            "subviewer1",
            "vplayer",
            "pjs",
        }
        subtitle_codecs = {
            "mp4": mp4_text_subtitle_inputs,
            "mkv": None,
            "mov": mp4_text_subtitle_inputs,
            "webm": {"webvtt"},
            "avi": set(),
            "mpeg": set(),
            "ts": {"dvb_subtitle", "dvb_teletext"},
            "flv": set(),
            "ogv": set(),
        }
        support = {
            "video": video_codecs,
            "audio": audio_codecs,
            "subtitle": subtitle_codecs,
        }.get(kind, {})
        supported = support.get(container, set())
        if supported is None:
            return True
        if kind == "audio" and codec.startswith("pcm_"):
            return container in {"mkv", "mov", "avi", "ts"}
        return codec in supported

    def update_container_options(self) -> None:
        """Disable containers that cannot accept the selected output streams."""
        if not hasattr(self, "format_combo"):
            return
        model = self.format_combo.model()
        if not hasattr(model, "item"):
            return
        have_video = bool(self.current_path and self.media_info.get("streams"))
        selected_codecs: list[tuple[str, str]] = []
        streams = self.media_info.get("streams", [])
        if have_video:
            video_stream = next(
                (stream for stream in streams if stream.get("codec_type") == "video"), None
            )
            video_encoder = self.video_codec_combo.currentData()
            if video_encoder == "copy":
                if video_stream:
                    selected_codecs.append(("video", str(video_stream.get("codec_name", ""))))
            else:
                output_video_codec = {
                    "libx264": "h264", "h264_nvenc": "h264",
                    "libx265": "hevc", "hevc_nvenc": "hevc",
                    "libvpx-vp9": "vp9",
                }.get(video_encoder, "")
                selected_codecs.append(("video", output_video_codec))
            if self.audio_codec_combo.currentData() == "copy":
                selected_audio = {index for index, check in self.audio_checks if check.isChecked()}
                selected_codecs.extend(
                    ("audio", str(stream.get("codec_name", "")))
                    for stream in streams
                    if stream.get("codec_type") == "audio" and stream.get("index") in selected_audio
                )
            else:
                output_audio_codec = {
                    "aac": "aac", "libopus": "opus", "libvorbis": "vorbis"
                }.get(self.audio_codec_combo.currentData(), "")
                if self.audio_checks:
                    selected_codecs.append(("audio", output_audio_codec))
            selected_subtitles = {
                index for index, check in self.subtitle_checks if check.isChecked()
            }
            selected_codecs.extend(
                ("subtitle", str(stream.get("codec_name", "")))
                for stream in streams
                if stream.get("codec_type") == "subtitle"
                and stream.get("index") in selected_subtitles
            )

        valid_containers: list[int] = []
        for index in range(self.format_combo.count()):
            container = self.format_combo.itemText(index).lower()
            compatible = not have_video or all(
                self.codec_supported_by_container(container, kind, codec)
                for kind, codec in selected_codecs
            )
            item = model.item(index)
            if item is not None:
                item.setEnabled(compatible)
                item.setToolTip(
                    "Container not available: the selected codecs or tracks are not supported."
                    if not compatible else ""
                )
            if compatible:
                valid_containers.append(index)

        if valid_containers and self.format_combo.currentIndex() not in valid_containers:
            self.format_combo.setCurrentIndex(valid_containers[0])
            self.statusBar().showMessage(
                "Selected container is not compatible with the selected output codecs; "
                f"switched to {self.format_combo.currentText()}.", 7000
            )

    def update_web_optimize_enabled(self, _text: str | None = None) -> None:
        supported = self.format_combo.currentText().strip().lstrip(".").lower() in {"mp4", "mov"}
        self.web_optimize_check.setEnabled(supported)
        if not supported:
            self.web_optimize_check.setChecked(False)

    def update_export_video_summary(self, *_args) -> None:
        """Show the chosen video export settings in the Export tab."""
        values = {
            "Container": self.format_combo.currentText(),
            "Video": self.video_codec_combo.currentText() or "Encoder unavailable",
            "RF": str(self.rf_slider.value()),
            "Frame rate": self.frame_rate_combo.currentText(),
            "Color": self.color_range_combo.currentText(),
        }
        for setting, value in values.items():
            row = self.video_review_rows.get(setting)
            if row is not None:
                row.setText(f"{setting}: {value}")

    def update_export_audio_summary(self, *_args) -> None:
        """Show the chosen audio export settings in the Export tab."""
        selected_tracks = [
            check.text() for _, check in self.audio_checks if check.isChecked()
        ]
        if selected_tracks:
            track_summary = "; ".join(selected_tracks)
        else:
            track_summary = ""

        copying_audio = self.audio_codec_combo.currentData() == "copy"
        values = {
            "Encoder": self.audio_codec_combo.currentText() or "Encoder unavailable",
            "Tracks": track_summary,
            "Output layout": (
                "Copied from source" if copying_audio
                else self.audio_channels_combo.currentText()
            ),
            "Bit rate": (
                "Copied from source" if copying_audio
                else f"{self.audio_bitrate_combo.currentText()} per track"
            ),
        }
        for setting, value in values.items():
            row = self.audio_review_rows.get(setting)
            if row is not None:
                row.setText(f"{setting}: {value}")
        tracks_row = self.audio_review_rows.get("Tracks")
        if tracks_row is not None:
            tracks_row.setVisible(bool(selected_tracks))

    def update_export_subtitle_summary(self, *_args) -> None:
        """List the selected subtitle tracks in the Export tab."""
        layout = getattr(self, "subtitle_review_tracks_layout", None)
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        selected_tracks = [
            check.text() for _, check in self.subtitle_checks if check.isChecked()
        ]
        if selected_tracks:
            track_labels = selected_tracks
        elif self.subtitle_checks:
            track_labels = ["None selected"]
        elif self.current_path:
            track_labels = ["No subtitle tracks in the source"]
        else:
            track_labels = []

        self.subtitle_review_tracks_heading.setVisible(bool(self.current_path))

        for text in track_labels:
            row = QLabel(text)
            row.setIndent(8)
            row.setWordWrap(True)
            layout.addWidget(row)

    def open_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open video",
            str(Path.home()),
            "Video files (*.*)",
        )
        if path:
            self.load_video(path)

    def eventFilter(self, watched: QObject, event) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.WindowActivate:
            modal_dialog_open = bool(self.app and self.app.activeModalWidget())
            if watched is self and self.preview_window.isVisible() and not modal_dialog_open:
                QTimer.singleShot(0, self.raise_preview_and_picture)
            elif (
                watched is self.preview_window
                and self.picture_float_window.isVisible()
                and not modal_dialog_open
            ):
                QTimer.singleShot(0, self.picture_float_window.raise_)
        if event.type() in (
            QEvent.Type.DragEnter,
            QEvent.Type.DragMove,
            QEvent.Type.Drop,
        ):
            if self.encoding_active:
                event.ignore()
                return True
            mime = event.mimeData()
            if mime and mime.hasUrls():
                paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
                if paths:
                    if event.type() == QEvent.Type.Drop:
                        self.load_video(paths[0])
                    event.acceptProposedAction()
                    return True
        return super().eventFilter(watched, event)

    def raise_preview_and_picture(self) -> None:
        if self.app and self.app.activeModalWidget():
            return
        if self.preview_window.isVisible():
            self.preview_window.raise_()
            if self.picture_float_window.isVisible():
                self.picture_float_window.raise_()

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        if paths:
            self.load_video(paths[0])
            event.acceptProposedAction()

    def load_video(self, path: str) -> None:
        if not Path(path).is_file():
            return
        try:
            info = probe_media(path)
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "Could not open media", str(exc))
            return
        streams = info.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video is None:
            QMessageBox.warning(self, "No video stream", "The selected file has no video stream.")
            return

        is_different_video = (
            self.current_path is None
            or Path(path).resolve() != Path(self.current_path).resolve()
        )
        if is_different_video:
            self.reset_settings_for_new_video()
        self._trim_frame_source_generation += 1
        self.trim_frame_timer.stop()
        if self._trim_frame_process and self._trim_frame_process.state() != QProcess.ProcessState.NotRunning:
            self._trim_frame_process.kill()
        self._trim_frame_process = None
        self._trim_preview_pending_position = 0
        self.current_path = path
        self.media_info = info
        duration = float(info.get("format", {}).get("duration", 0) or 0)
        self.duration_ms = int(duration * 1000)
        self.trim_range_slider.set_duration(self.duration_ms, reset=is_different_video)
        self.trim_range_slider.setEnabled(self.duration_ms > 0)
        self.trim_start_spin.setEnabled(self.duration_ms > 0)
        self.trim_end_spin.setEnabled(self.duration_ms > 0)
        self.trim_preview_active = False
        self.trim_preview_button.setText("Preview trim")
        self.trim_preview_button.setEnabled(self.duration_ms > 0)
        self.reset_trim_button.setEnabled(self.duration_ms > 0)
        self.update_summary(path, info)
        self.position_slider.setRange(0, self.duration_ms)
        self.media_label.setText(
            f"{Path(path).name}\n{video.get('width', '?')} × {video.get('height', '?')} · "
            f"{video.get('codec_name', 'unknown')} · {self.format_time(self.duration_ms)}\n"
            f"Source: {self.format_frame_rate(video.get('avg_frame_rate') or video.get('r_frame_rate'))} · "
            f"{self.format_color_range(video.get('color_range'))} color range"
        )
        self.populate_track_checks(streams)
        self.settings_at_load = self.current_unsaved_state()
        self.player.stop()
        self._last_source_frame = None
        self._current_preview_image = None
        placeholder = "Ready to preview — press Play"
        for preview in (
            self.preview,
            self.before_preview,
            self.comparison_after_preview,
            self.preview_window.preview,
            self.preview_window.before_preview,
            self.preview_window.comparison_after_preview,
        ):
            preview.set_placeholder(placeholder)
        self.player.setSource(QUrl.fromLocalFile(path))
        self.load_first_preview_frame(path)
        self.statusBar().showMessage(f"Loaded {path}")

    def reset_settings_for_new_video(self) -> None:
        """Restore per-file editing and export options to their defaults."""
        self.reset_adjustments()

        container_index = self.format_combo.findText("mp4")
        if container_index >= 0:
            self.format_combo.setCurrentIndex(container_index)
        video_codec_index = self.video_codec_combo.findData("libx264")
        if video_codec_index < 0:
            video_codec_index = self.video_codec_combo.findData("copy")
        if video_codec_index >= 0:
            self.video_codec_combo.setCurrentIndex(video_codec_index)
        self.rf_slider.setValue(22)
        frame_rate_index = self.frame_rate_combo.findData(None)
        if frame_rate_index >= 0:
            self.frame_rate_combo.setCurrentIndex(frame_rate_index)
        color_range_index = self.color_range_combo.findData(None)
        if color_range_index >= 0:
            self.color_range_combo.setCurrentIndex(color_range_index)

        audio_codec_index = self.audio_codec_combo.findData("aac")
        if audio_codec_index >= 0:
            self.audio_codec_combo.setCurrentIndex(audio_codec_index)
        channels_index = self.audio_channels_combo.findData(2)
        if channels_index >= 0:
            self.audio_channels_combo.setCurrentIndex(channels_index)
        bitrate_index = self.audio_bitrate_combo.findData(256)
        if bitrate_index >= 0:
            self.audio_bitrate_combo.setCurrentIndex(bitrate_index)

        self.web_optimize_check.setChecked(False)
        self.update_video_controls_enabled()
        self.update_audio_layout_enabled()
        self.update_web_optimize_enabled()
        self.output_path = None

    def load_first_preview_frame(self, path: str) -> None:
        """Decode one small frame for the still preview without starting playback."""
        ffmpeg = media_tool("ffmpeg")
        if not ffmpeg:
            return
        try:
            result = subprocess.run(
                [
                    ffmpeg,
                    "-v", "error",
                    "-ss", "0",
                    "-i", path,
                    "-map", "0:v:0",
                    "-frames:v", "1",
                    "-vf", "scale=960:540:force_original_aspect_ratio=decrease",
                    "-f", "image2pipe",
                    "-vcodec", "png",
                    "pipe:1",
                ],
                check=True,
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return
        image = QImage.fromData(result.stdout, "PNG")
        if image.isNull():
            return
        self._last_source_frame = image.convertToFormat(QImage.Format.Format_RGB888)
        self.on_video_frame_from_image(self._last_source_frame)

    def populate_track_checks(self, streams: list[dict]) -> None:
        for target_layout in (self.audio_tracks_layout, self.subtitle_tracks_layout):
            while target_layout.count():
                item = target_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
        self.audio_checks.clear()
        self.subtitle_checks.clear()
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        if audio_streams:
            details = ["Source tracks:"]
            for number, stream in enumerate(audio_streams, start=1):
                layout = stream.get("channel_layout") or "unknown layout"
                channels = stream.get("channels")
                channel_count = f"{channels} ch" if channels else "channel count unknown"
                sample_rate = stream.get("sample_rate")
                rate = f"{int(sample_rate) / 1000:g} kHz" if sample_rate else "sample rate unknown"
                bit_rate = stream.get("bit_rate")
                bitrate = f"{int(round(int(bit_rate) / 1000)):,} kb/s" if bit_rate else "bitrate not reported"
                language = stream.get("tags", {}).get("language")
                language_text = f" · {language}" if language else ""
                details.append(
                    f"Track {number}: {stream.get('codec_name', 'unknown codec')} · "
                    f"{layout} ({channel_count}) · {rate} · {bitrate}{language_text}"
                )
            self.source_audio_info.setText("\n".join(details))
        else:
            self.source_audio_info.setText("The original file has no audio tracks.")

        for stream in streams:
            kind = stream.get("codec_type")
            if kind not in ("audio", "subtitle"):
                continue
            index = int(stream["index"])
            codec = stream.get("codec_name", "unknown")
            language = stream.get("tags", {}).get("language", "und")
            label = f"{kind.title()} {index} · {codec} · {language}"
            check = QCheckBox(label)
            check.setChecked(True)
            check.toggled.connect(self.update_container_options)
            if kind == "audio":
                check.toggled.connect(self.update_export_audio_summary)
            else:
                check.toggled.connect(self.update_export_subtitle_summary)
            target_layout = (
                self.audio_tracks_layout if kind == "audio" else self.subtitle_tracks_layout
            )
            target_layout.addWidget(check)
            (self.audio_checks if kind == "audio" else self.subtitle_checks).append(
                (index, check)
            )
        if not self.audio_checks:
            self.audio_tracks_layout.addWidget(QLabel("No audio tracks found."))
        if not self.subtitle_checks:
            self.subtitle_tracks_layout.addWidget(QLabel("No subtitle tracks found."))
        self.update_container_options()
        self.update_export_audio_summary()
        self.update_export_subtitle_summary()

    def on_video_frame(self, frame) -> None:
        image = frame.toImage()
        if image.isNull():
            return
        image = image.scaled(
            960,
            540,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ).convertToFormat(QImage.Format.Format_RGB888)
        width, height = image.width(), image.height()
        bits = image.constBits()
        raw = bits.tobytes()
        pil = Image.frombytes(
            "RGB", (width, height), raw, "raw", "RGB", image.bytesPerLine(), 1
        )
        values = {key: slider.value() for key, slider in self.adjustment_sliders.items()}
        if values["denoise"] > 0:
            pil = pil.filter(ImageFilter.MedianFilter(3 if values["denoise"] < 65 else 5))
        pil = ImageEnhance.Brightness(pil).enhance(1 + values["brightness"] / 100)
        pil = self.apply_ffmpeg_eq_to_preview(pil, values["contrast"], values["saturation"])
        if values["sharpness"]:
            pil = ImageEnhance.Sharpness(pil).enhance(1 + values["sharpness"] / 25)
        output = pil.convert("RGB")
        preview_image = QImage(
            output.tobytes(),
            output.width,
            output.height,
            output.width * 3,
            QImage.Format.Format_RGB888,
        ).copy()
        self._last_source_frame = image
        self._current_preview_image = preview_image
        self.before_preview.set_frame(image)
        self.comparison_after_preview.set_frame(preview_image)
        if not self.preview_window.isVisible():
            self.preview.set_frame(preview_image)
        self.preview_window.set_frames(image, preview_image)

    def redraw_current_frame(self, _value: int | None = None) -> None:
        frame = getattr(self, "_last_source_frame", None)
        if frame is not None:
            self.on_video_frame_from_image(frame)

    def on_video_frame_from_image(self, image: QImage) -> None:
        # Reuse the same filter path without waiting for another decoded frame.
        width, height = image.width(), image.height()
        pil = Image.frombytes(
            "RGB", (width, height), image.constBits().tobytes(),
            "raw", "RGB", image.bytesPerLine(), 1,
        )
        values = {key: slider.value() for key, slider in self.adjustment_sliders.items()}
        if values["denoise"] > 0:
            pil = pil.filter(ImageFilter.MedianFilter(3 if values["denoise"] < 65 else 5))
        pil = ImageEnhance.Brightness(pil).enhance(1 + values["brightness"] / 100)
        pil = self.apply_ffmpeg_eq_to_preview(pil, values["contrast"], values["saturation"])
        if values["sharpness"]:
            pil = ImageEnhance.Sharpness(pil).enhance(1 + values["sharpness"] / 25)
        output = pil.convert("RGB")
        preview_image = QImage(
            output.tobytes(), output.width, output.height, output.width * 3,
            QImage.Format.Format_RGB888,
        ).copy()
        self._current_preview_image = preview_image
        self.before_preview.set_frame(image)
        self.comparison_after_preview.set_frame(preview_image)
        if self.preview_window.isVisible():
            self.preview_window.set_frames(image, preview_image)
        else:
            self.preview.set_frame(preview_image)

    @staticmethod
    def apply_ffmpeg_eq_to_preview(
        image: Image.Image, contrast_value: int, saturation_value: int
    ) -> Image.Image:
        """Apply FFmpeg eq's luma contrast and chroma saturation curves to the preview."""
        contrast = 1 + contrast_value / 100
        saturation = 1 + saturation_value / 100
        if contrast == 1 and saturation == 1:
            return image
        y_channel, cb_channel, cr_channel = image.convert("YCbCr").split()

        def contrast_lut(factor: float) -> list[int]:
            lut: list[int] = []
            for sample in range(256):
                value = factor * (sample / 255 - 0.5) + 0.5
                if value <= 0:
                    lut.append(0)
                elif value >= 1:
                    lut.append(255)
                else:
                    lut.append(int(256 * value))
            return lut

        if contrast != 1:
            y_channel = y_channel.point(contrast_lut(contrast))
        if saturation != 1:
            chroma_lut = contrast_lut(saturation)
            cb_channel = cb_channel.point(chroma_lut)
            cr_channel = cr_channel.point(chroma_lut)
        return Image.merge("YCbCr", (y_channel, cb_channel, cr_channel)).convert("RGB")

    def toggle_preview_window(self) -> None:
        if self.preview_window.isVisible():
            self.preview_window.hide()
            if getattr(self, "_current_preview_image", None) is not None:
                self.preview.set_frame(self._current_preview_image)
            self.preview_stack.setCurrentIndex(1 if self.comparison_active else 0)
            self.pop_out_button.setText("Float")
        else:
            self.preview_stack.setCurrentIndex(2)
            self.preview_window.set_comparison_view(self.comparison_active)
            source_frame = getattr(self, "_last_source_frame", None)
            preview_image = getattr(self, "_current_preview_image", None)
            if source_frame is not None and preview_image is not None:
                self.preview_window.set_frames(source_frame, preview_image)
            self.preview_window.show()
            self.preview_window.raise_()
            self.preview_window.activateWindow()
            if self.picture_float_window.isVisible():
                self.picture_float_window.raise_()
            self.pop_out_button.setText("Restore")

    def on_preview_window_closed(self) -> None:
        if getattr(self, "_current_preview_image", None) is not None:
            self.preview.set_frame(self._current_preview_image)
        self.preview_stack.setCurrentIndex(1 if self.comparison_active else 0)
        self.pop_out_button.setText("Float")

    def toggle_preview_mode(self) -> None:
        self.comparison_active = not self.comparison_active
        button_text = "Original view" if self.comparison_active else "Split view"
        self.view_mode_button.setText(button_text)
        self.picture_view_mode_button.setText(button_text)
        if not self.preview_window.isVisible():
            self.preview_stack.setCurrentIndex(1 if self.comparison_active else 0)
        self.preview_window.set_comparison_view(self.comparison_active)

    def set_duration(self, duration: int) -> None:
        self.position_slider.setRange(0, duration)
        self.picture_position_slider.setRange(0, duration)
        self.duration_ms = duration
        self.trim_range_slider.set_duration(duration)

    def update_trim_label(self, start_ms: int, end_ms: int) -> None:
        selected_ms = max(0, end_ms - start_ms)
        start_seconds = round(start_ms / 1000)
        end_seconds = round(end_ms / 1000)
        duration_seconds = round(self.duration_ms / 1000)
        self.trim_start_spin.blockSignals(True)
        self.trim_end_spin.blockSignals(True)
        self.trim_start_spin.setRange(0, max(0, end_seconds - 1))
        self.trim_end_spin.setRange(min(duration_seconds, start_seconds + 1), duration_seconds)
        self.trim_start_spin.setValue(start_seconds)
        self.trim_end_spin.setValue(end_seconds)
        self.trim_start_spin.blockSignals(False)
        self.trim_end_spin.blockSignals(False)
        self.trim_range_label.setText(f"Selected: {self.format_time(selected_ms)}")

    def preview_trim_position(self, position_ms: int) -> None:
        if not self._trim_handle_dragging:
            if self.player.isPlaying():
                self.player.pause()
            self.trim_preview_active = False
            self.trim_preview_button.setText("Preview trim")
        self.player.setPosition(position_ms)
        self._trim_preview_pending_position = position_ms
        if not self._trim_frame_process:
            self.trim_frame_timer.start()

    def begin_trim_handle_drag(self) -> None:
        self._trim_handle_dragging = True
        self.trim_preview_active = False
        self.trim_preview_button.setText("Preview trim")
        if self.player.isPlaying():
            self.player.pause()

    def finish_trim_handle_drag(self) -> None:
        self._trim_handle_dragging = False
        if self.player.isPlaying():
            self.player.pause()

    def decode_trim_preview_frame(self) -> None:
        if self._trim_frame_process or not self.current_path:
            return
        ffmpeg = media_tool("ffmpeg")
        if not ffmpeg:
            return
        position_ms = self._trim_preview_pending_position
        source_generation = self._trim_frame_source_generation
        process = QProcess(self)
        self._trim_frame_process = process
        process.setProgram(ffmpeg)
        process.setArguments([
            "-v", "error",
            "-ss", f"{position_ms / 1000:.3f}",
            "-i", self.current_path,
            "-map", "0:v:0",
            "-frames:v", "1",
            "-vf", "scale=960:540:force_original_aspect_ratio=decrease",
            "-f", "image2pipe",
            "-vcodec", "png",
            "pipe:1",
        ])
        process.finished.connect(
            lambda _code, _status, p=process, g=source_generation, pos=position_ms:
                self.trim_preview_frame_finished(p, g, pos)
        )
        process.errorOccurred.connect(
            lambda _error, p=process, g=source_generation, pos=position_ms:
                self.trim_preview_frame_failed(p, g, pos)
        )
        process.start()

    def trim_preview_frame_finished(
        self, process: QProcess, source_generation: int, requested_position: int
    ) -> None:
        image_data = bytes(process.readAllStandardOutput())
        if self._trim_frame_process is process:
            self._trim_frame_process = None
        process.deleteLater()
        if source_generation == self._trim_frame_source_generation and image_data:
            image = QImage.fromData(image_data, "PNG")
            if not image.isNull():
                self._last_source_frame = image.convertToFormat(QImage.Format.Format_RGB888)
                self.on_video_frame_from_image(self._last_source_frame)
        if (
            source_generation == self._trim_frame_source_generation
            and self._trim_preview_pending_position != requested_position
        ):
            self.trim_frame_timer.start()

    def trim_preview_frame_failed(
        self, process: QProcess, source_generation: int, requested_position: int
    ) -> None:
        if self._trim_frame_process is process:
            self._trim_frame_process = None
        process.deleteLater()
        if (
            source_generation == self._trim_frame_source_generation
            and self._trim_preview_pending_position != requested_position
        ):
            self.trim_frame_timer.start()

    def preview_trim(self) -> None:
        if self.trim_preview_active:
            self.player.pause()
            self.trim_preview_active = False
            self.trim_preview_button.setText("Preview trim")
            return
        start_ms = self.trim_range_slider.start_ms
        end_ms = self.trim_range_slider.end_ms
        if not self.current_path or end_ms <= start_ms:
            return
        if self.player.isPlaying() and not self._trim_handle_dragging:
            self.player.pause()
        self.trim_preview_active = True
        self.trim_preview_button.setText("Stop preview")
        self.player.setPosition(start_ms)
        self.player.play()

    def current_unsaved_state(self) -> dict:
        state = self.current_preset_data()
        state["_trim_range_ms"] = [
            self.trim_range_slider.start_ms,
            self.trim_range_slider.end_ms,
        ]
        return state

    def set_position(self, position: int) -> None:
        if self.player.isPlaying() and not self._trim_handle_dragging:
            trim_start = self.trim_range_slider.start_ms
            trim_end = self.trim_range_slider.end_ms
            if position < trim_start:
                self.player.setPosition(trim_start)
                position = trim_start
            elif trim_end > trim_start and position >= trim_end:
                self.player.pause()
                if self.trim_preview_active:
                    self.trim_preview_active = False
                    self.trim_preview_button.setText("Preview trim")
                    self.player.setPosition(trim_end)
                    position = trim_end
                else:
                    self.player.setPosition(trim_start)
                    position = trim_start
        if not self.position_slider.isSliderDown():
            self.position_slider.setValue(position)
        if not self.picture_position_slider.isSliderDown():
            self.picture_position_slider.setValue(position)
        self.time_label.setText(
            f"{self.format_time(position)} / {self.format_time(getattr(self, 'duration_ms', 0))}"
        )

    @staticmethod
    def format_time(milliseconds: int) -> str:
        seconds = max(0, milliseconds // 1000)
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    @staticmethod
    def format_duration(milliseconds: int) -> str:
        seconds = max(0, milliseconds // 1000)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def format_file_size(value: object) -> str:
        try:
            size = float(value)
        except (TypeError, ValueError):
            return "not reported"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
            size /= 1024
        return "not reported"

    @staticmethod
    def format_bitrate(value: object) -> str:
        try:
            bitrate = float(value)
        except (TypeError, ValueError):
            return "not reported"
        return f"{bitrate / 1000:,.0f} kb/s" if bitrate >= 1000 else f"{bitrate:.0f} b/s"

    def update_summary(self, path: str, info: dict) -> None:
        fmt = info.get("format", {})
        duration_ms = int(float(fmt.get("duration", 0) or 0) * 1000)
        format_name = fmt.get("format_long_name") or fmt.get("format_name") or Path(path).suffix.lstrip(".").upper()
        format_short = fmt.get("format_name")
        if format_short and format_short not in format_name:
            format_name = f"{format_name} ({format_short})"
        self.summary_file_label.setText(
            f"Name: {Path(path).name}\n"
            f"Container: {format_name}\n"
            f"Duration: {self.format_duration(duration_ms)}\n"
            f"File size: {self.format_file_size(fmt.get('size'))}\n"
            f"Overall bitrate: {self.format_bitrate(fmt.get('bit_rate'))}"
        )

        streams = info.get("streams", [])
        video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
        if video:
            average_fps = self.format_frame_rate(video.get("avg_frame_rate"))
            nominal_fps = self.format_frame_rate(video.get("r_frame_rate"))
            fps_text = average_fps
            if average_fps == "unknown frame rate":
                fps_text = nominal_fps
            elif nominal_fps != "unknown frame rate" and nominal_fps != average_fps:
                fps_text = f"{average_fps} average (nominal {nominal_fps})"
            codec = video.get("codec_name", "unknown")
            if video.get("profile"):
                codec = f"{codec} ({video['profile']})"
            color_details = [
                f"pixel format {video['pix_fmt']}" if video.get("pix_fmt") else None,
                f"color space {video['color_space']}" if video.get("color_space") else None,
                f"{self.format_color_range(video.get('color_range'))} range" if video.get("color_range") else None,
            ]
            self.summary_video_label.setText(
                f"Codec: {codec}\n"
                f"Resolution: {video.get('width', '?')} × {video.get('height', '?')}\n"
                f"Frame rate: {fps_text}\n"
                f"Bitrate: {self.format_bitrate(video.get('bit_rate'))}\n"
                f"Format: {', '.join(detail for detail in color_details if detail) or 'not reported'}"
            )
        else:
            self.summary_video_label.setText("No video stream found.")

        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        audio_lines = []
        for number, stream in enumerate(audio_streams, start=1):
            tags = stream.get("tags", {})
            language = tags.get("language")
            title = tags.get("title")
            channels = stream.get("channels")
            channel_layout = stream.get("channel_layout")
            channel_text = f"{channels} channels" if channels else "channels not reported"
            if channel_layout:
                channel_text += f" ({channel_layout})"
            sample_rate = stream.get("sample_rate")
            try:
                sample_text = f"{int(sample_rate) / 1000:g} kHz" if sample_rate else "sample rate not reported"
            except (TypeError, ValueError):
                sample_text = "sample rate not reported"
            labels = [f"Audio {number}: {stream.get('codec_name', 'unknown codec')}"]
            labels.extend(value for value in (title, language) if value and value != "und")
            labels.extend((channel_text, sample_text, self.format_bitrate(stream.get("bit_rate"))))
            audio_lines.append(" · ".join(labels))
        self.summary_audio_label.setText("\n".join(audio_lines) if audio_lines else "No audio tracks found.")

        subtitle_streams = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
        subtitle_lines = []
        for number, stream in enumerate(subtitle_streams, start=1):
            tags = stream.get("tags", {})
            language = tags.get("language")
            title = tags.get("title")
            labels = [f"Subtitle {number}: {stream.get('codec_name', 'unknown format')}"]
            labels.extend(value for value in (title, language) if value and value != "und")
            subtitle_lines.append(" · ".join(labels))
        self.summary_subtitle_label.setText(
            "\n".join(subtitle_lines) if subtitle_lines else "No subtitle tracks found."
        )

    @staticmethod
    def format_frame_rate(value: str | None) -> str:
        if not value or value in ("0/0", "N/A"):
            return "unknown frame rate"
        try:
            numerator, denominator = value.split("/", 1)
            rate = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return "unknown frame rate"
        return f"{rate:.3f}".rstrip("0").rstrip(".") + " fps"

    @staticmethod
    def format_color_range(value: str | None) -> str:
        return {"tv": "limited", "pc": "full"}.get(value, "unspecified")

    def seek_by(self, offset: int) -> None:
        start = self.trim_range_slider.start_ms
        end = self.trim_range_slider.end_ms or self.duration_ms
        self.player.setPosition(min(end, max(start, self.player.position() + offset)))

    def set_preview_volume(self, value: int) -> None:
        self.audio_output.setVolume(value / 100.0)
        for slider in (self.preview_volume_slider, self.picture_volume_slider):
            if slider.value() != value:
                slider.blockSignals(True)
                slider.setValue(value)
                slider.blockSignals(False)

    def toggle_playback(self) -> None:
        if self.player.isPlaying():
            self.player.pause()
            self.trim_preview_active = False
            self.trim_preview_button.setText("Preview trim")
        else:
            trim_start = self.trim_range_slider.start_ms
            trim_end = self.trim_range_slider.end_ms
            if self.player.position() < trim_start or self.player.position() >= trim_end:
                self.player.setPosition(trim_start)
            self.player.play()

    def update_play_button(self, state: QMediaPlayer.PlaybackState) -> None:
        is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.play_button.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_MediaPause
                if is_playing
                else QStyle.StandardPixmap.SP_MediaPlay
            )
        )
        action = "Pause" if is_playing else "Play"
        self.play_button.setToolTip(f"{action} video")
        self.play_button.setAccessibleName(f"{action} video")
        self.picture_play_button.setIcon(
            self.style().standardIcon(
                QStyle.StandardPixmap.SP_MediaPause
                if is_playing
                else QStyle.StandardPixmap.SP_MediaPlay
            )
        )
        self.picture_play_button.setToolTip(f"{action} video")
        self.picture_play_button.setAccessibleName(f"{action} video")

    def ffmpeg_filters(self) -> str | None:
        values = {key: slider.value() for key, slider in self.adjustment_sliders.items()}
        filters: list[str] = []
        if values["denoise"]:
            amount = values["denoise"] / 20
            filters.append(f"hqdn3d={amount:.2f}:{amount / 2:.2f}:{amount:.2f}:{amount / 2:.2f}")
        brightness_factor = 1 + values["brightness"] / 100
        if brightness_factor != 1:
            filters.append(
                "lutrgb="
                f"r='clip(val*{brightness_factor:.4f},0,255)':"
                f"g='clip(val*{brightness_factor:.4f},0,255)':"
                f"b='clip(val*{brightness_factor:.4f},0,255)'"
            )
        if values["contrast"] or values["saturation"]:
            filters.append(
                "eq="
                f"contrast={1 + values['contrast'] / 100:.3f}:"
                f"saturation={1 + values['saturation'] / 100:.3f}"
            )
        if values["sharpness"]:
            filters.append(f"unsharp=5:5:{values['sharpness'] / 25:.2f}:5:5:0")
        return ",".join(filters) if filters else None

    def choose_output(self) -> bool:
        if not self.current_path:
            QMessageBox.information(self, "Open a video", "Load a video before choosing an output.")
            return False
        ext = self.format_combo.currentText().strip().lstrip(".") or "mp4"
        default = str(Path(self.current_path).with_name(Path(self.current_path).stem + f"_edited.{ext}"))
        path, _ = QFileDialog.getSaveFileName(self, "Export video", default)
        if path:
            self.output_path = path
            self.statusBar().showMessage(f"Output: {path}")
            return True
        self.output_path = None
        return False

    def start_export(self) -> None:
        if not self.current_path:
            QMessageBox.information(self, "Open a video", "Load a video before exporting.")
            return
        if not self.choose_output():
            return
        output = self.output_path
        if output is None:
            return
        output_path = Path(output)
        ext = self.format_combo.currentText().strip().lstrip(".") or output_path.suffix.lstrip(".") or "mp4"
        if output_path.suffix.lower() != f".{ext.lower()}":
            output_path = output_path.with_suffix(f".{ext}")
            self.output_path = str(output_path)
            self.statusBar().showMessage(f"Output: {output_path}")

        video_codec = self.video_codec_combo.currentData()
        filters = self.ffmpeg_filters()
        if video_codec == "copy" and filters:
            QMessageBox.warning(
                self, "Choose a video encoder",
                "Picture adjustments require re-encoding. Select an encoder instead of copy.",
            )
            return

        trim_start_ms = self.trim_range_slider.start_ms
        trim_end_ms = self.trim_range_slider.end_ms
        trim_duration_ms = trim_end_ms - trim_start_ms
        if trim_duration_ms <= 0:
            QMessageBox.warning(self, "Invalid trim range", "Choose an export range longer than zero.")
            return

        ffmpeg = media_tool("ffmpeg")
        if not ffmpeg:
            QMessageBox.critical(self, "FFmpeg is required", "The FFmpeg executable could not be found.")
            return
        command = [ffmpeg, "-hide_banner", "-y"]
        if trim_start_ms > 0:
            # Input-side seeking is fast. FFmpeg seeks accurately while re-encoding;
            # stream copy is limited by the media's keyframes.
            command += ["-ss", f"{trim_start_ms / 1000:.3f}"]
        command += ["-i", self.current_path]
        if trim_start_ms > 0 or trim_end_ms < self.duration_ms:
            command += ["-t", f"{trim_duration_ms / 1000:.3f}"]
        video_stream = next(
            s for s in self.media_info["streams"] if s.get("codec_type") == "video"
        )
        command += ["-map", f"0:{video_stream['index']}"]
        for index, check in self.audio_checks:
            if check.isChecked():
                command += ["-map", f"0:{index}"]
        for index, check in self.subtitle_checks:
            if check.isChecked():
                command += ["-map", f"0:{index}"]
        command += ["-c:v", video_codec]
        if filters:
            command += ["-vf", filters]
        if video_codec in ("libx264", "libx265"):
            command += ["-crf", str(self.rf_slider.value())]
        elif video_codec == "libvpx-vp9":
            command += ["-crf", str(self.rf_slider.value()), "-b:v", "0"]
        elif video_codec in ("h264_nvenc", "hevc_nvenc"):
            cq_value = max(1, self.rf_slider.value())
            command += ["-rc", "vbr", "-cq", str(cq_value), "-b:v", "0"]
        if video_codec == "h264_nvenc":
            command += ["-pix_fmt", "yuv420p"]
        if video_codec != "copy":
            frame_rate = self.frame_rate_combo.currentData()
            if frame_rate is None:
                command += ["-fps_mode", "passthrough"]
            else:
                command += ["-r", frame_rate, "-fps_mode", "cfr"]
            color_range = self.color_range_combo.currentData()
            if color_range:
                command += ["-color_range", color_range]
        audio_codec = self.audio_codec_combo.currentData()
        if any(check.isChecked() for _, check in self.audio_checks):
            command += ["-c:a", audio_codec]
            channel_count = self.audio_channels_combo.currentData()
            if audio_codec != "copy" and channel_count:
                command += ["-ac", str(channel_count)]
            if audio_codec != "copy":
                command += ["-b:a", f"{self.audio_bitrate_combo.currentData()}k"]
        if any(check.isChecked() for _, check in self.subtitle_checks):
            subtitle_codec = (
                "mov_text"
                if self.format_combo.currentText().strip().lower() in {"mp4", "mov"}
                else "copy"
            )
            command += ["-c:s", subtitle_codec]
        if self.web_optimize_check.isChecked() and ext.lower() in {"mp4", "mov"}:
            command += ["-movflags", "+faststart"]
        command += ["-progress", "pipe:1", "-nostats", str(output_path)]

        duration_ms = trim_duration_ms
        self.export_progress.setValue(0)
        self.export_estimate.setText("Estimated time remaining: calculating…")
        self.statusBar().showMessage("Exporting…")
        self.player.pause()
        self.set_encode_lock(True)
        self.export_thread = QThread(self)
        self.export_worker = ExportWorker(command, duration_ms)
        self.export_worker.moveToThread(self.export_thread)
        self.export_thread.started.connect(self.export_worker.run)
        self.export_worker.progress.connect(self.export_progress.setValue)
        self.export_worker.estimate.connect(self.export_estimate.setText)
        self.export_worker.finished.connect(self.export_succeeded)
        self.export_worker.failed.connect(self.export_failed)
        self.export_worker.cancelled.connect(self.export_cancelled)
        self.export_worker.finished.connect(self.export_thread.quit)
        self.export_worker.failed.connect(self.export_thread.quit)
        self.export_worker.cancelled.connect(self.export_thread.quit)
        self.export_thread.finished.connect(self.export_thread.deleteLater)
        self.export_thread.start()

    def toggle_export(self) -> None:
        if self.encoding_active:
            self.stop_export()
        else:
            self.start_export()

    def stop_export(self) -> None:
        if not self.export_worker:
            return
        confirmation = QMessageBox(self)
        confirmation.setIcon(QMessageBox.Icon.Question)
        confirmation.setWindowTitle("Stop encoding?")
        confirmation.setText(
            "Stop this encode and delete its incomplete output file?"
        )
        stop_button = confirmation.addButton(
            "Stop and delete file", QMessageBox.ButtonRole.DestructiveRole
        )
        continue_button = confirmation.addButton(
            "Continue encoding", QMessageBox.ButtonRole.RejectRole
        )
        confirmation.setDefaultButton(continue_button)
        self.prepare_confirmation_dialog(confirmation)
        confirmation.exec()
        if confirmation.clickedButton() is not stop_button:
            return
        self.statusBar().showMessage("Stopping encode and removing partial file…")
        self.export_estimate.setText("Stopping encode…")
        self.export_button.setText("Stopping…")
        self.export_button.setEnabled(False)
        self.export_worker.stop()

    def set_encode_lock(self, locked: bool) -> None:
        self.encoding_active = locked
        self.file_menu.setEnabled(not locked)
        self.adjustments_group.setEnabled(not locked)
        self.position_slider.setEnabled(not locked)
        self.trim_range_slider.setEnabled(not locked and self.duration_ms > 0)
        self.trim_start_spin.setEnabled(not locked and self.duration_ms > 0)
        self.trim_end_spin.setEnabled(not locked and self.duration_ms > 0)
        self.trim_preview_button.setEnabled(not locked and self.duration_ms > 0)
        self.reset_trim_button.setEnabled(not locked and self.duration_ms > 0)
        self.seek_back_button.setEnabled(not locked)
        self.play_button.setEnabled(not locked)
        self.seek_forward_button.setEnabled(not locked)
        for index in range(self.export_tabs.count()):
            self.export_tabs.setTabEnabled(index, not locked or index == self.export_tab_index)
        self.export_button.setEnabled(True)
        self.export_button.setText("Stop encode" if locked else "Encode video")
        web_faststart_supported = self.format_combo.currentText().strip().lstrip(".").lower() in {"mp4", "mov"}
        self.web_optimize_check.setEnabled(not locked and web_faststart_supported)
        self.setAcceptDrops(not locked)
        self.splitter.handle(1).setEnabled(not locked)

    def export_succeeded(self, path: str) -> None:
        self.set_encode_lock(False)
        self.export_button.setText("Stop encode")
        self.export_button.setEnabled(False)
        QMessageBox.information(
            self,
            "Export complete",
            f"Encoding finished successfully:\n{path}",
        )
        self.export_progress.setValue(0)
        self.export_estimate.setText("Estimated time remaining: —")
        self.export_button.setText("Encode video")
        self.export_button.setEnabled(True)
        self.statusBar().showMessage(f"Export complete: {path}", 10000)

    def export_failed(self, message: str) -> None:
        self.set_encode_lock(False)
        self.export_estimate.setText("Encoding failed")
        self.statusBar().showMessage("Export failed")
        QMessageBox.critical(self, "Export failed", message)

    def export_cancelled(self) -> None:
        self.set_encode_lock(False)
        output_file = Path(self.export_worker.command[-1]) if self.export_worker else None
        try:
            if output_file:
                output_file.unlink(missing_ok=True)
        except OSError as exc:
            self.export_estimate.setText("Encoding stopped; output cleanup failed")
            self.statusBar().showMessage("Encoding stopped; could not remove partial output")
            QMessageBox.warning(
                self,
                "Could not remove partial output",
                f"Encoding stopped, but Muxy could not delete the incomplete file:\n"
                f"{output_file}\n\n{exc}",
            )
            return
        self.export_estimate.setText("Encoding stopped; partial output deleted")
        self.statusBar().showMessage("Encoding stopped and partial output deleted")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.encoding_active:
            self.statusBar().showMessage("Stop or wait for the encode before closing the app")
            event.ignore()
            return
        if (
            self.current_path
            and self.settings_at_load is not None
            and self.current_unsaved_state() != self.settings_at_load
        ):
            dialog = QMessageBox(self)
            dialog.setIcon(QMessageBox.Icon.Warning)
            dialog.setWindowTitle("Unsaved changes")
            dialog.setText("The current video has settings changes that have not been saved.")
            dialog.setInformativeText("Exit and discard these changes?")
            exit_button = dialog.addButton(
                "Exit without saving", QMessageBox.ButtonRole.DestructiveRole
            )
            dialog.addButton(QMessageBox.StandardButton.Cancel)
            self.prepare_confirmation_dialog(dialog)
            dialog.exec()
            if dialog.clickedButton() is not exit_button:
                event.ignore()
                return
        if self.preview_window.isVisible():
            self.preview_window.close()
        if self.picture_float_window.isVisible():
            self.picture_float_window.close()
        if self.app:
            self.app.removeEventFilter(self)
        super().closeEvent(event)


def main() -> int:
    if not media_tool("ffmpeg") or not media_tool("ffprobe"):
        app = QApplication(sys.argv)
        QMessageBox.critical(
            None,
            "FFmpeg is required",
            "Install FFmpeg and make both ffmpeg and ffprobe available on PATH.",
        )
        return 1
    app = QApplication(sys.argv)
    app.setApplicationName("Muxy")
    app.setApplicationVersion(APP_VERSION)
    icon_path = Path(__file__).resolve().parents[2] / "packaging" / "muxy.png"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    window = VideoEditorWindow()
    if icon_path.is_file():
        window.setWindowIcon(QIcon(str(icon_path)))
    window.show()
    video_path = next(
        (
            argument
            for argument in sys.argv[1:]
            if not argument.startswith("-") and Path(argument).is_file()
        ),
        None,
    )
    if video_path:
        QTimer.singleShot(0, lambda: window.load_video(video_path))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
