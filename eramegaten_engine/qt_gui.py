from __future__ import annotations

import os
import math
import threading
import traceback
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from PIL import Image as PILImage

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSettings, QSize, QTimer, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QIcon,
    QImage,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QResizeEvent,
    QTransform,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QLayout,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .builtins import _config_raw
from .frontend import FrontendSession


CLICK_VALUE_ROLE = 0xEA01
HOVER_TITLE_ROLE = 0xEA02

BACKGROUND = QColor("#090e17")
SURFACE = QColor("#111827")
SURFACE_ALT = QColor("#172033")
BORDER = QColor("#26344d")
TEXT = QColor("#e7edf8")
MUTED = QColor("#8ea0ba")
ACCENT = QColor("#7c8cff")
ACCENT_BRIGHT = QColor("#9aa6ff")
SUCCESS = QColor("#51d89a")
WARNING = QColor("#f5bd58")
DANGER = QColor("#ff7085")

# Authoritative values from eraMegaten's ``emuera.config``.  Keep these
# separate from the surrounding inspector shell: the game surface itself is
# deliberately rendered like Emuera 1.824 (MS Gothic 18px on an 18px row,
# gray text on black, and yellow focused PRINTBUTTON text).
EMUERA_FONT_SIZE = 18
EMUERA_LINE_HEIGHT = 18
EMUERA_TEXT = QColor("#c0c0c0")
EMUERA_SELECTED_TEXT = QColor("#ffff00")


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip() or default))
    except Exception:
        return default


def _qcolor(value: Any, default: QColor = TEXT) -> QColor:
    if isinstance(value, QColor):
        return QColor(value)
    if isinstance(value, str) and value.startswith("#"):
        color = QColor(value)
        return color if color.isValid() else QColor(default)
    try:
        return QColor.fromRgb(int(value) & 0xFFFFFF)
    except Exception:
        return QColor(default)


def _font_for_drawable(item: dict[str, Any], *, pixel_size: int = 18) -> QFont:
    family = str(item.get("font", "") or "MS Gothic")
    # Qt does not consistently resolve Emuera's localized/full-width family
    # spelling on Windows.  It can silently fall back to a proportional CJK
    # font, changing both character-art alignment and hit boxes.  The original
    # configured face is the English Windows family name below.
    family_aliases = {
        "ＭＳ ゴシック": "MS Gothic",
        "ＭＳ Ｐゴシック": "MS PGothic",
        "MS ゴシック": "MS Gothic",
        "MS Pゴシック": "MS PGothic",
    }
    family = family_aliases.get(family, family)
    style = _int(item.get("font_style"), 0)
    font = QFont(family)
    font.setPixelSize(max(9, int(pixel_size)))
    font.setKerning(False)
    # Prefer native-size hinted glyphs.  The old 1x tile path rasterized an
    # 18 px glyph and let Windows enlarge it to 27 device pixels at 150% DPI,
    # which blurred every stem.  Full hinting keeps MS Gothic aligned to the
    # physical pixel grid once the tile itself is rendered at native DPR.
    font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    if any(token in family.casefold() for token in ("gothic", "ゴシック", "mono", "consol", "courier")):
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setFixedPitch(True)
    font.setBold(bool(style & 1))
    font.setItalic(bool(style & 2))
    font.setUnderline(bool(style & 4))
    font.setStrikeOut(bool(style & 8))
    return font


def _qt_key_to_emuera(key: int) -> int:
    special = {
        int(Qt.Key.Key_Backspace): 8,
        int(Qt.Key.Key_Tab): 9,
        int(Qt.Key.Key_Return): 13,
        int(Qt.Key.Key_Enter): 13,
        int(Qt.Key.Key_Shift): 16,
        int(Qt.Key.Key_Control): 17,
        int(Qt.Key.Key_Alt): 18,
        int(Qt.Key.Key_Escape): 27,
        int(Qt.Key.Key_Space): 32,
        int(Qt.Key.Key_PageUp): 33,
        int(Qt.Key.Key_PageDown): 34,
        int(Qt.Key.Key_End): 35,
        int(Qt.Key.Key_Home): 36,
        int(Qt.Key.Key_Left): 37,
        int(Qt.Key.Key_Up): 38,
        int(Qt.Key.Key_Right): 39,
        int(Qt.Key.Key_Down): 40,
        int(Qt.Key.Key_Insert): 45,
        int(Qt.Key.Key_Delete): 46,
    }
    if key in special:
        return special[key]
    f1 = int(Qt.Key.Key_F1)
    f24 = int(Qt.Key.Key_F24)
    if f1 <= key <= f24:
        return 112 + key - f1
    if int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
        return ord("0") + key - int(Qt.Key.Key_0)
    if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
        return ord("A") + key - int(Qt.Key.Key_A)
    return max(0, int(key) & 0xFF)


class WorkerBridge(QObject):
    completed = Signal(object)
    failed = Signal(str)


class HistoryLineEdit(QLineEdit):
    """Input box with shell-like Up/Down history."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._history: list[str] = []
        self._history_index = 0

    def remember(self, value: str) -> None:
        if value and (not self._history or self._history[-1] != value):
            self._history.append(value)
            self._history = self._history[-100:]
        self._history_index = len(self._history)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        if event.key() == Qt.Key.Key_Up and self._history:
            self._history_index = max(0, self._history_index - 1)
            self.setText(self._history[self._history_index])
            self.selectAll()
            return
        if event.key() == Qt.Key.Key_Down and self._history:
            self._history_index = min(len(self._history), self._history_index + 1)
            self.setText(self._history[self._history_index] if self._history_index < len(self._history) else "")
            self.selectAll()
            return
        super().keyPressEvent(event)


class HitRegion(QGraphicsPathItem):
    """Rounded interactive scene region with lightweight hover feedback."""

    def __init__(
        self,
        rect: QRectF,
        *,
        value: str | None,
        title: str = "",
        visible_frame: bool = True,
        hover_frame: bool = False,
    ):
        path = QPainterPath()
        path.addRoundedRect(rect, 5.0, 5.0)
        super().__init__(path)
        self.setData(CLICK_VALUE_ROLE, value)
        self.setData(HOVER_TITLE_ROLE, title)
        self.setToolTip(title)
        # Pointer routing is owned by GameSceneView.  Letting the scene track
        # item hover/cursors causes broken partial repaints on some Windows
        # backing stores, where static text gets cleared and never redrawn.
        self.setAcceptHoverEvents(False)
        self._visible_frame = visible_frame
        self._hover_frame = visible_frame or hover_frame
        self._base_pen = QPen(ACCENT if value is not None else BORDER, 1.0)
        self._hover_pen = QPen(ACCENT_BRIGHT, 1.6)
        self._base_brush = QBrush(QColor(48, 60, 91, 105) if value is not None else QColor(0, 0, 0, 0))
        self._hover_brush = QBrush(QColor(78, 94, 150, 115) if visible_frame else QColor(124, 140, 255, 42))
        if visible_frame:
            self.setPen(self._base_pen)
            self.setBrush(self._base_brush)
        else:
            self.setPen(QPen(Qt.PenStyle.NoPen))
            self.setBrush(QBrush(Qt.BrushStyle.NoBrush))

    def hoverEnterEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._hover_frame:
            self.setPen(self._hover_pen)
            self.setBrush(self._hover_brush)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._visible_frame:
            self.setPen(self._base_pen)
            self.setBrush(self._base_brush)
        else:
            self.setPen(QPen(Qt.PenStyle.NoPen))
            self.setBrush(QBrush(Qt.BrushStyle.NoBrush))
        super().hoverLeaveEvent(event)


class _LegacySceneView(QGraphicsView):
    """Stable scene-coordinate game viewport with zoom, pan and hit locking."""

    activated = Signal(int, int, object)
    pointerMoved = Signal(int, int, object)
    keyStateChanged = Signal(int, bool, bool)
    quickInputRequested = Signal(str)
    advanceRequested = Signal()
    skipRequested = Signal()
    zoomChanged = Signal(float)
    userNavigated = Signal()
    viewportResized = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("gameView")
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._zoom = 1.0
        self._first_layout = True
        self._rebuilding = False
        self._panning = False
        self._pan_origin = QPoint()
        self._last_pointer = QPointF(0.0, 0.0)
        self._pointer_event_serial = 0
        self._last_drawable_count = 0
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        # Some Windows raster/backing-store combinations lose untouched scene
        # pixels during cursor-driven partial updates, making the hovered row
        # (or larger parts of the page) turn black.  Repaint the visible scene
        # as one frame so a dirty-region update can never erase prior text.
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setCacheMode(QGraphicsView.CacheModeFlag.CacheNone)
        self.setBackgroundBrush(QBrush(BACKGROUND))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.horizontalScrollBar().sliderPressed.connect(self.userNavigated.emit)
        self.verticalScrollBar().sliderPressed.connect(self.userNavigated.emit)
        self.horizontalScrollBar().valueChanged.connect(lambda _value: self._schedule_pointer_refresh())
        self.verticalScrollBar().valueChanged.connect(lambda _value: self._schedule_pointer_refresh())

    @property
    def zoom_factor(self) -> float:
        return self._zoom

    @property
    def drawable_count(self) -> int:
        return self._last_drawable_count

    def logical_viewport_width(self) -> int:
        return max(1, int(self.viewport().width()))

    def set_zoom(self, value: float) -> None:
        target = max(0.5, min(2.5, float(value)))
        if abs(target - self._zoom) < 0.001:
            return
        ratio = target / self._zoom
        self.scale(ratio, ratio)
        self._zoom = target
        self.zoomChanged.emit(self._zoom)
        self._schedule_pointer_refresh()

    def reset_zoom(self) -> None:
        self.resetTransform()
        self._zoom = 1.0
        self.zoomChanged.emit(self._zoom)
        self._schedule_pointer_refresh()

    def clickable_value_at(self, viewport_position: QPoint) -> str | None:
        # Search through the full item stack rather than stopping at a plain
        # text item above an invisible hit area.
        for item in self.items(viewport_position):
            current = item
            while current is not None:
                value = current.data(CLICK_VALUE_ROLE)
                if value is not None:
                    return str(value)
                current = current.parentItem()
        return None

    def tooltip_at(self, viewport_position: QPoint) -> str:
        for item in self.items(viewport_position):
            current = item
            while current is not None:
                value = current.data(HOVER_TITLE_ROLE)
                if value:
                    return str(value)
                current = current.parentItem()
        return ""

    def set_layout(
        self,
        layout: dict[str, Any],
        runtime: Any,
        *,
        follow_output: bool,
        max_drawables: int = 30_000,
    ) -> bool:
        """Rebuild the scene while preserving exact scrollbar coordinates."""

        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        old_h = hbar.value()
        old_v = vbar.value()
        was_at_bottom = vbar.maximum() <= 0 or old_v >= vbar.maximum() - 4
        self._rebuilding = True
        try:
            self._scene.clear()
            self._scene.setBackgroundBrush(QBrush(_qcolor(runtime.default_bgcolor, BACKGROUND)))
            drawables = [item for item in layout.get("drawables", []) if isinstance(item, dict)]
            truncated = len(drawables) > max_drawables
            if truncated:
                drawables = drawables[-max_drawables:]
            self._last_drawable_count = len(drawables)
            self._render_drawables(drawables, runtime)
            canvas = layout.get("canvas", {})
            width = max(self.logical_viewport_width(), _int(canvas.get("width"), 1) + 20)
            height = max(1, _int(canvas.get("height"), 1) + 20)
            self._scene.setSceneRect(QRectF(0.0, 0.0, float(width), float(height)))
            if follow_output and (self._first_layout or was_at_bottom):
                hbar.setValue(min(old_h, hbar.maximum()))
                vbar.setValue(vbar.maximum())
            else:
                hbar.setValue(min(old_h, hbar.maximum()))
                vbar.setValue(min(old_v, vbar.maximum()))
            self._first_layout = False
            return truncated
        finally:
            self._rebuilding = False
            QTimer.singleShot(0, self.refresh_pointer_state)

    def _render_drawables(self, drawables: list[dict[str, Any]], runtime: Any) -> None:
        default_bg = _qcolor(runtime.default_bgcolor, BACKGROUND)
        plain_text_items: dict[tuple[int, int], list[QGraphicsSimpleTextItem]] = {}
        for order, item in enumerate(drawables):
            kind = str(item.get("type", ""))
            if kind == "print_space":
                continue
            x = _int(item.get("x"))
            y = _int(item.get("y"))
            width = max(0, _int(item.get("width")))
            height = max(1, _int(item.get("height"), self.line_height))
            color = _qcolor(item.get("color"), TEXT)
            bgcolor = _qcolor(item.get("bgcolor"), default_bg)
            title = str(item.get("title", "") or item.get("parent_title", ""))
            button_value: str | None = None
            if kind in {"button", "print_button", "implicit_button"}:
                raw_value = str(item.get("value", ""))
                button_value = raw_value if raw_value != "" or item.get("activate_empty") else None
            elif kind == "image" and item.get("parent") == "button":
                raw_value = str(item.get("parent_value", ""))
                button_value = raw_value if raw_value != "" else None

            if bgcolor.rgb() != default_bg.rgb() and width > 0:
                bg_item = self._scene.addRect(
                    QRectF(float(x), float(y), float(width), float(height)),
                    QPen(Qt.PenStyle.NoPen),
                    QBrush(bgcolor),
                )
                bg_item.setZValue(float(order) / 10000.0)

            if kind in {"image", "print_image"}:
                pixmap = self._sprite_pixmap(runtime, str(item.get("src", "")), width, height)
                if pixmap is not None:
                    image_item = QGraphicsPixmapItem(pixmap)
                    image_item.setPos(float(x), float(y))
                    image_item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
                    image_item.setData(CLICK_VALUE_ROLE, button_value)
                    image_item.setData(HOVER_TITLE_ROLE, title)
                    image_item.setToolTip(title)
                    image_item.setZValue(4 + float(order) / 10000.0)
                    self._scene.addItem(image_item)
                else:
                    fallback = HitRegion(
                        QRectF(float(x), float(y), float(max(1, width)), float(max(1, height))),
                        value=button_value,
                        title=title or str(item.get("src", "")),
                        visible_frame=True,
                    )
                    fallback.setPen(QPen(QColor("#5a6780"), 1.0, Qt.PenStyle.DashLine))
                    fallback.setZValue(3)
                    self._scene.addItem(fallback)
                continue

            if kind == "implicit_button":
                # The transcript text is a separate drawable emitted just
                # before this active hit marker.  Make the visible item itself
                # clickable so it always stays above the fallback hit area.
                for text_item in reversed(plain_text_items.get((x, y), [])):
                    text_item.setData(CLICK_VALUE_ROLE, button_value)
                    text_item.setData(HOVER_TITLE_ROLE, title)
                    text_item.setToolTip(title)
                    break
                region = HitRegion(
                    QRectF(0.0, 0.0, float(max(1, width)), float(max(1, height))),
                    value=button_value,
                    title=title,
                    visible_frame=False,
                    hover_frame=False,
                )
                region.setPos(float(x), float(y))
                # Sit above plain transcript text for deterministic itemAt()
                # hit testing.  It must remain completely paint-free: drawing
                # a hover brush above a separate text item can make the whole
                # row disappear with some Windows graphics backends.
                region.setAcceptHoverEvents(False)
                region.unsetCursor()
                region.setZValue(2 + float(order) / 10000.0)
                self._scene.addItem(region)
                continue

            if kind in {"button", "print_button", "nonbutton", "print_rect"}:
                label = str(item.get("label", ""))
                visible = kind == "print_rect" or bool(label.strip())
                region = HitRegion(
                    QRectF(0.0, 0.0, float(max(1, width)), float(max(1, height))),
                    value=button_value,
                    title=title,
                    visible_frame=visible,
                )
                region.setPos(float(x), float(y))
                region.setZValue(2 + float(order) / 10000.0)
                if kind == "print_rect":
                    region.setPen(QPen(color, 1.0))
                self._scene.addItem(region)
                if kind != "print_rect" and label:
                    text_item = QGraphicsSimpleTextItem(label, region)
                    text_item.setBrush(QBrush(color))
                    text_item.setFont(_font_for_drawable(item, pixel_size=max(12, height - 5)))
                    text_item.setPos(0.0, -1.0)
                    text_item.setData(CLICK_VALUE_ROLE, button_value)
                    text_item.setData(HOVER_TITLE_ROLE, title)
                    text_item.setToolTip(title)
                continue

            if kind in {"text", "html_text"}:
                text = str(item.get("text", ""))
                if not text:
                    continue
                text_item = QGraphicsSimpleTextItem(text)
                text_item.setBrush(QBrush(color))
                text_item.setFont(_font_for_drawable(item, pixel_size=max(12, height - 5)))
                text_item.setPos(float(x), float(y - 1))
                text_item.setZValue(3 + float(order) / 10000.0)
                text_item.setToolTip(title)
                self._scene.addItem(text_item)
                plain_text_items.setdefault((x, y), []).append(text_item)

    @staticmethod
    def _sprite_pixmap(runtime: Any, source: str, width: int, height: int) -> QPixmap | None:
        if not source or width <= 0 or height <= 0:
            return None
        try:
            image = runtime.render_sprite_image(source).convert("RGBA")
            if image.size != (width, height):
                image = image.resize((width, height))
            raw = image.tobytes("raw", "RGBA")
            qimage = QImage(raw, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
            return QPixmap.fromImage(qimage)
        except Exception:
            return None

    def _schedule_pointer_refresh(self) -> None:
        if not self._rebuilding:
            QTimer.singleShot(0, self.refresh_pointer_state)

    def refresh_pointer_state(self) -> None:
        viewport_pos = self.viewport().mapFromGlobal(QCursor.pos())
        if not self.viewport().rect().contains(viewport_pos):
            self.pointerMoved.emit(round(self._last_pointer.x()), round(self._last_pointer.y()), None)
            return
        scene_pos = self.mapToScene(viewport_pos)
        self._last_pointer = scene_pos
        value = self.clickable_value_at(viewport_pos)
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor if value is not None else Qt.CursorShape.ArrowCursor
        )
        self.pointerMoved.emit(round(scene_pos.x()), round(scene_pos.y()), value)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if self._panning:
            delta = event.position().toPoint() - self._pan_origin
            self._pan_origin = event.position().toPoint()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        scene_pos = self.mapToScene(event.position().toPoint())
        self._last_pointer = scene_pos
        value = self.clickable_value_at(event.position().toPoint())
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor if value is not None else Qt.CursorShape.ArrowCursor
        )
        self.pointerMoved.emit(round(scene_pos.x()), round(scene_pos.y()), value)
        # Do not forward hover motion into QGraphicsScene.  Hit testing and
        # cursor selection are already complete above; forwarding only makes
        # Qt dirty individual items and is the trigger for the Windows blank
        # row/backing-store bug.  Request one complete viewport frame instead.
        event.accept()
        self.viewport().update()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_origin = event.position().toPoint()
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            self.viewport().grabMouse()
            self.userNavigated.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            scene_pos = self.mapToScene(event.position().toPoint())
            value = self.clickable_value_at(event.position().toPoint())
            self.activated.emit(round(scene_pos.x()), round(scene_pos.y()), value)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.viewport().releaseMouse()
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self._schedule_pointer_refresh()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt API
        self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
        self.pointerMoved.emit(round(self._last_pointer.x()), round(self._last_pointer.y()), None)
        event.accept()
        self.viewport().update()

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        # Bypass QGraphicsView's exposed-item/dirty-region paint path entirely.
        # On the affected Windows backend it can clear the backing store and
        # redraw only interactive items, leaving the rest of the title page
        # blank.  QGraphicsScene.render() deterministically paints every item
        # in the visible scene rectangle while the view still owns scrolling,
        # transforms, and hit testing.
        painter = QPainter(self.viewport())
        painter.setRenderHints(self.renderHints())
        painter.fillRect(self.viewport().rect(), self.backgroundBrush())
        source = self.mapToScene(self.viewport().rect()).boundingRect()
        target = QRectF(self.viewport().rect())
        self._scene.render(
            painter,
            target,
            source,
            Qt.AspectRatioMode.IgnoreAspectRatio,
        )
        painter.end()
        event.accept()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self._zoom * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15))
            event.accept()
            return
        self.userNavigated.emit()
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - event.angleDelta().y()
            )
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        code = _qt_key_to_emuera(event.key())
        self.keyStateChanged.emit(code, True, not event.isAutoRepeat())
        if event.key() in {
            Qt.Key.Key_PageUp,
            Qt.Key.Key_PageDown,
            Qt.Key.Key_Home,
            Qt.Key.Key_End,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
        }:
            self.userNavigated.emit()
        if not event.isAutoRepeat() and not (event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)):
            if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
                self.advanceRequested.emit()
                event.accept()
                return
            text = event.text()
            if len(text) == 1 and text.isdigit():
                self.quickInputRequested.emit(text)
                event.accept()
                return
            if len(text) == 1 and text.casefold() in {"y", "n"}:
                self.quickInputRequested.emit(text.casefold())
                event.accept()
                return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        self.keyStateChanged.emit(_qt_key_to_emuera(event.key()), False, False)
        super().keyReleaseEvent(event)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self.viewportResized.emit()


class GameSceneView(QAbstractScrollArea):
    """Immutable off-screen tile canvas with independent hit testing.

    Text, images and character art are first rasterized into bounded QImages;
    viewport paints only copy those immutable tiles.  Mouse movement never
    mutates a text item or its backing store.  Clickable areas live in a
    separate geometry list, so classic ``PRINTL [0]`` menus remain clickable
    without turning their visible rows into hover-sensitive widgets.
    """

    activated = Signal(int, int, object)
    pointerMoved = Signal(int, int, object)
    keyStateChanged = Signal(int, bool, bool)
    quickInputRequested = Signal(str)
    advanceRequested = Signal()
    skipRequested = Signal()
    zoomChanged = Signal(float)
    userNavigated = Signal()
    viewportResized = Signal()

    renderer_name = "offscreen-tile-raster"
    font_pixel_size = EMUERA_FONT_SIZE
    line_height = EMUERA_LINE_HEIGHT

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("gameView")
        self._zoom = 1.0
        self._first_layout = True
        self._panning = False
        self._pan_origin = QPoint()
        self._last_pointer = QPointF(0.0, 0.0)
        self._hovered_order: int | None = None
        self._pointer_event_serial = 0
        self._drawables: list[dict[str, Any]] = []
        self._paint_tiles: dict[int, list[int]] = {}
        self._paint_tile_height = 256
        self._raster_tile_width = 1024
        self._raster_tile_limit = 48
        self._raster_tiles: OrderedDict[tuple[int, int, int], QImage] = OrderedDict()
        self._last_paint_candidate_count = 0
        self._last_painted_count = 0
        self._hit_regions: list[dict[str, Any]] = []
        self._runtime: Any | None = None
        self._sprite_cache: OrderedDict[tuple[str, int, int, int], QPixmap | None] = OrderedDict()
        self._sprite_cache_limit = 256
        self._render_failures: set[str] = set()
        self._layout_issues: list[str] = []
        self._canvas_width = 1
        self._canvas_height = 1
        self._reference_width = 0
        self._reference_height = 0
        self._content_origin_y = 0
        # Instance values replace the class defaults when emuera.config is
        # loaded; keeping the class attributes preserves the lightweight view
        # API used by standalone tests and tools.
        self.font_pixel_size = EMUERA_FONT_SIZE
        self.line_height = EMUERA_LINE_HEIGHT
        self._last_drawable_count = 0
        self._background = QColor(BACKGROUND)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().setAutoFillBackground(False)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.horizontalScrollBar().sliderPressed.connect(self.userNavigated.emit)
        self.verticalScrollBar().sliderPressed.connect(self.userNavigated.emit)
        self.horizontalScrollBar().valueChanged.connect(lambda _value: self._schedule_pointer_refresh())
        self.verticalScrollBar().valueChanged.connect(lambda _value: self._schedule_pointer_refresh())

    @property
    def zoom_factor(self) -> float:
        return self._zoom

    @property
    def drawable_count(self) -> int:
        return self._last_drawable_count

    @property
    def hit_regions(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._hit_regions)

    @property
    def render_failures(self) -> tuple[str, ...]:
        return tuple(sorted(self._render_failures))

    @property
    def layout_issues(self) -> tuple[str, ...]:
        return tuple(self._layout_issues)

    @property
    def canvas_size(self) -> tuple[int, int]:
        return self._canvas_width, self._canvas_height

    @property
    def last_paint_candidate_count(self) -> int:
        return self._last_paint_candidate_count

    @property
    def last_painted_count(self) -> int:
        return self._last_painted_count

    def logical_viewport_width(self) -> int:
        return max(1, round(self.viewport().width() / self._scene_scale()))

    def _native_device_pixel_ratio(self) -> float:
        return max(1.0, float(self.viewport().devicePixelRatioF()))

    def _scene_scale(self) -> float:
        """Qt logical units per original Emuera physical pixel.

        Emuera's configured 1600x950 and 18 px font are physical-pixel
        measurements.  A DPI-aware Qt window uses device-independent units;
        dividing by DPR keeps 100% zoom physically identical to the original
        instead of inflating and blurring the canvas at Windows 150% scaling.
        """

        return max(0.01, float(self._zoom) / self._native_device_pixel_ratio())

    @property
    def reference_size(self) -> tuple[int, int]:
        return self._reference_width, self._reference_height

    @property
    def content_origin_y(self) -> int:
        return self._content_origin_y

    def configure_rendering(
        self,
        *,
        font_pixel_size: int,
        line_height: int,
        reference_width: int,
        reference_height: int,
    ) -> None:
        """Apply the active Emuera text/window metrics to the canvas.

        Layout is always calculated against the configured game viewport, not
        against the optional inspector's current width.  This keeps centered
        title/menu rows at the same coordinates when side panels are toggled.
        """

        values = (
            max(1, int(font_pixel_size)),
            max(1, int(line_height)),
            max(1, int(reference_width)),
            max(1, int(reference_height)),
        )
        previous = (
            self.font_pixel_size,
            self.line_height,
            self._reference_width,
            self._reference_height,
        )
        self.font_pixel_size, self.line_height, self._reference_width, self._reference_height = values
        if values != previous:
            self._raster_tiles.clear()
            self._sprite_cache.clear()

    def mapToScene(self, viewport_position: QPoint) -> QPointF:  # noqa: N802 - compatibility API
        scale = self._scene_scale()
        return QPointF(
            (float(viewport_position.x()) + self.horizontalScrollBar().value()) / scale,
            (float(viewport_position.y()) + self.verticalScrollBar().value()) / scale,
        )

    def mapFromScene(self, scene_position: QPointF) -> QPoint:  # noqa: N802 - compatibility API
        scale = self._scene_scale()
        return QPoint(
            round(float(scene_position.x()) * scale - self.horizontalScrollBar().value()),
            round(float(scene_position.y()) * scale - self.verticalScrollBar().value()),
        )

    def centerOn(self, scene_position: QPointF) -> None:  # noqa: N802 - compatibility API
        scale = self._scene_scale()
        self.horizontalScrollBar().setValue(
            round(float(scene_position.x()) * scale - self.viewport().width() / 2)
        )
        self.verticalScrollBar().setValue(
            round(float(scene_position.y()) * scale - self.viewport().height() / 2)
        )
        self.viewport().update()

    def set_zoom(self, value: float) -> None:
        target = max(0.5, min(2.5, float(value)))
        if abs(target - self._zoom) < 0.001:
            return
        anchor = QPoint(self.viewport().width() // 2, self.viewport().height() // 2)
        scene_anchor = self.mapToScene(anchor)
        self._zoom = target
        # Tile resolution includes zoom so enlarged text is rerasterized at
        # native output resolution instead of magnifying an older bitmap.
        self._raster_tiles.clear()
        self._sprite_cache.clear()
        self._update_scrollbars()
        scale = self._scene_scale()
        self.horizontalScrollBar().setValue(round(scene_anchor.x() * scale - anchor.x()))
        self.verticalScrollBar().setValue(round(scene_anchor.y() * scale - anchor.y()))
        self.zoomChanged.emit(self._zoom)
        self.viewport().update()
        self._schedule_pointer_refresh()

    def reset_zoom(self) -> None:
        self.set_zoom(1.0)

    def fit_width(self) -> None:
        """Fit the complete logical canvas width into the viewport."""

        if self._canvas_width <= 0:
            return
        available = max(1, self.viewport().width() - 4)
        self.set_zoom(available * self._native_device_pixel_ratio() / self._canvas_width)
        self.horizontalScrollBar().setValue(0)

    def jump_to_latest(self) -> None:
        self.verticalScrollBar().setValue(self._latest_scroll_value())
        self.viewport().update()

    def set_layout(
        self,
        layout: dict[str, Any],
        runtime: Any,
        *,
        follow_output: bool,
        max_drawables: int = 30_000,
    ) -> bool:
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        old_h = hbar.value()
        old_v = vbar.value()
        was_at_bottom = vbar.maximum() <= 0 or old_v >= vbar.maximum() - 4
        drawables = [dict(item) for item in layout.get("drawables", []) if isinstance(item, dict)]
        truncated = len(drawables) > max_drawables
        if truncated:
            drawables = drawables[-max_drawables:]
        canvas = layout.get("canvas", {})
        raw_content_bottom = max(
            (_int(item.get("y")) + max(1, _int(item.get("height"), self.line_height)) for item in drawables),
            default=0,
        )
        content_extent = max(1, _int(canvas.get("height"), 1), raw_content_bottom)
        output = getattr(runtime, "output", [])
        tail = "".join(str(value) for value in output[-4:]) if output else ""
        if not output or tail.endswith("\n"):
            # PRINTL leaves the cursor on a fresh row.  Emuera includes that
            # row when it pins a short page to the bottom of the 950 px view.
            content_extent += self.line_height
        self._content_origin_y = max(0, self._reference_height - content_extent)
        if self._content_origin_y:
            for item in drawables:
                item["y"] = _int(item.get("y")) + self._content_origin_y
        self._layout_issues = []
        self._drawables = self._prepare_drawables(drawables)
        self._hovered_order = None
        self._raster_tiles.clear()
        runtime_changed = runtime is not self._runtime
        self._runtime = runtime
        # Runtime-created graphics may change while retaining the same sprite
        # name, so never reuse those across frames.  Static resource sprites
        # keep their bounded LRU cache and avoid repeated PIL decoding.
        if runtime_changed or getattr(runtime, "graphics", None) or getattr(runtime, "sprites", None):
            self._sprite_cache.clear()
        self._render_failures.clear()
        self._background = _qcolor(runtime.default_bgcolor, BACKGROUND)
        content_right = max(
            (_int(item.get("x")) + max(0, _int(item.get("width"))) for item in self._drawables),
            default=0,
        )
        content_bottom = max(
            (_int(item.get("y")) + max(1, _int(item.get("height"), 1)) for item in self._drawables),
            default=0,
        )
        if self._reference_width > 0 or self._reference_height > 0:
            requested_width = max(1, _int(canvas.get("width"), 1), content_right)
            # A full-width DRAWLINE intentionally contains one final glyph
            # that is clipped by Emuera's fixed client area.  Do not create a
            # horizontal scrollbar for that sub-glyph overhang.
            if requested_width <= self._reference_width + self.font_pixel_size:
                requested_width = self._reference_width
            self._canvas_width = max(1, self._reference_width, requested_width)
            self._canvas_height = max(
                1,
                self._reference_height,
                self._content_origin_y + content_extent,
                content_bottom,
            )
        else:
            # Preserve the standalone view's historical breathing room when
            # no Emuera window profile has been supplied.
            self._canvas_width = max(1, _int(canvas.get("width"), 1), content_right) + 20
            self._canvas_height = max(1, _int(canvas.get("height"), 1), content_bottom) + 20
        self._last_drawable_count = len(self._drawables)
        missing_sources = {
            str(item.get("src", "") or "<empty>")
            for item in self._drawables
            if str(item.get("type", "")) in {"image", "print_image"} and item.get("asset_missing")
        }
        self._render_failures.update(missing_sources)
        image_items = [
            item
            for item in self._drawables
            if str(item.get("type", "")) in {"image", "print_image"}
            and not item.get("asset_missing")
            and str(item.get("src", ""))
            and _int(item.get("width")) > 0
            and _int(item.get("height")) > 0
        ]
        unique_images = {
            (str(item.get("src", "")), _int(item.get("width")), _int(item.get("height")))
            for item in image_items
        }
        if len(unique_images) <= 64:
            for source, width, height in unique_images:
                self._sprite_pixmap(source, width, height)
        else:
            self._layout_issues.append(f"当前页图片规格过多，延迟校验: {len(unique_images)}")
        self._rebuild_paint_index()
        self._rebuild_hit_regions()
        self._update_scrollbars()
        if follow_output:
            # New Emuera output starts from the left edge.  Keeping a previous
            # horizontal inspection offset can make a successful menu click
            # look like it did nothing because the next prompt is off-screen.
            hbar.setValue(0)
            vbar.setValue(self._latest_scroll_value())
        else:
            hbar.setValue(min(old_h, hbar.maximum()))
            vbar.setValue(min(old_v, vbar.maximum()))
        self._first_layout = False
        self.viewport().update()
        self._schedule_pointer_refresh()
        return truncated

    def _latest_scroll_value(self) -> int:
        """Return a scroll position that reveals the newest useful content.

        Some Era pages intentionally append hundreds of blank rows after their
        current map/command block.  Following the geometric canvas bottom then
        shows an apparently empty screen.  Prefer the latest active control,
        then the last drawable; only use the absolute bottom when the remaining
        gap is small.
        """

        bar = self.verticalScrollBar()
        if bar.maximum() <= 0:
            return 0
        if self._hit_regions:
            latest_bottom = max(float(region["rect"].bottom()) for region in self._hit_regions)
        else:
            latest_bottom = max(
                (
                    float(_int(item.get("y")) + max(1, _int(item.get("height"), 1)))
                    for item in self._drawables
                    if str(item.get("type", "")) not in {"", "print_space", "implicit_button"}
                ),
                default=float(self._canvas_height),
            )
        logical_gap = max(0.0, float(self._canvas_height) - latest_bottom)
        scale = self._scene_scale()
        logical_viewport = max(1.0, self.viewport().height() / scale)
        if logical_gap <= max(64.0, logical_viewport * 0.2):
            return bar.maximum()
        margin = 18.0
        target = round((latest_bottom + margin) * scale - self.viewport().height())
        return max(bar.minimum(), min(target, bar.maximum()))

    def _prepare_drawables(self, drawables: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize paint bounds against the actual Qt font in use.

        Era layout coordinates are cell based.  When Windows substitutes a
        missing Japanese font, the real glyph width can be wider than the
        model estimate; expanding the drawable and canvas bounds prevents the
        final characters and button labels from being clipped or unclickable.
        """

        known = {
            "text",
            "html_text",
            "button",
            "print_button",
            "implicit_button",
            "nonbutton",
            "print_rect",
            "print_space",
            "image",
            "print_image",
        }
        prepared: list[dict[str, Any]] = []
        for item in drawables:
            kind = str(item.get("type", ""))
            if kind not in known:
                self._layout_issues.append(f"未知绘制类型: {kind or '<empty>'}")
            x = _int(item.get("x"))
            y = _int(item.get("y"))
            width = max(0, _int(item.get("width")))
            height = max(1, _int(item.get("height"), self.line_height))
            if x < 0 or y < 0:
                self._layout_issues.append(f"负坐标 {kind}: ({x}, {y})")
            text = ""
            if kind in {"text", "html_text"}:
                text = str(item.get("text", ""))
            elif kind in {"button", "print_button", "nonbutton"}:
                text = str(item.get("label", ""))
            if text:
                font = _font_for_drawable(item, pixel_size=self.font_pixel_size)
                measured = max(0, QFontMetrics(font).horizontalAdvance(text))
                width = max(width, measured)
                item["measured_text_width"] = measured
            if kind in {"image", "print_image"} and width <= 0:
                self._layout_issues.append(f"零宽图片: {item.get('src', '')}")
            item["x"] = x
            item["y"] = y
            item["width"] = width
            item["height"] = height
            prepared.append(item)
        painted_text = {
            (
                _int(item.get("x")),
                _int(item.get("y")),
                str(item.get("text", "")),
            )
            for item in prepared
            if str(item.get("type", "")) in {"text", "html_text"}
            and str(item.get("text", ""))
        }
        for item in prepared:
            if str(item.get("type", "")) not in {"button", "print_button", "nonbutton"}:
                continue
            key = (
                _int(item.get("x")),
                _int(item.get("y")),
                str(item.get("label", "")),
            )
            if key in painted_text:
                item["label_already_painted"] = True
        # Keep the inspector concise when one malformed source repeats.
        self._layout_issues = list(dict.fromkeys(self._layout_issues))[:100]
        return prepared

    def _update_scrollbars(self) -> None:
        width = max(self.logical_viewport_width(), self._canvas_width)
        height = max(1, self._canvas_height)
        scale = self._scene_scale()
        scaled_width = max(1, round(width * scale))
        scaled_height = max(1, round(height * scale))
        viewport_width = max(1, self.viewport().width())
        viewport_height = max(1, self.viewport().height())
        hbar = self.horizontalScrollBar()
        vbar = self.verticalScrollBar()
        hbar.setPageStep(viewport_width)
        vbar.setPageStep(viewport_height)
        hbar.setSingleStep(max(16, viewport_width // 20))
        vbar.setSingleStep(max(16, viewport_height // 20))
        hbar.setRange(0, max(0, scaled_width - viewport_width))
        vbar.setRange(0, max(0, scaled_height - viewport_height))

    @staticmethod
    def _drawable_button_value(item: dict[str, Any], kind: str) -> str | None:
        if kind in {"button", "print_button", "implicit_button"}:
            raw = str(item.get("value", ""))
            return raw if raw != "" or item.get("activate_empty") else None
        if kind in {"image", "print_image"} and item.get("parent") == "button":
            raw = str(item.get("parent_value", ""))
            return raw if raw != "" else None
        return None

    def _rebuild_hit_regions(self) -> None:
        regions: list[dict[str, Any]] = []
        for order, item in enumerate(self._drawables):
            kind = str(item.get("type", ""))
            value = self._drawable_button_value(item, kind)
            if value is None:
                continue
            x = _int(item.get("x"))
            y = _int(item.get("y"))
            width = max(1, _int(item.get("width"), 1))
            height = max(1, _int(item.get("height"), self.line_height))
            regions.append(
                {
                    "rect": QRectF(float(x), float(y), float(width), float(height)),
                    "value": value,
                    "title": str(item.get("title", "") or item.get("parent_title", "")),
                    "order": order,
                    "kind": kind,
                    "implicit": kind == "implicit_button",
                }
            )
        # Plain PRINT/PRINTL menus are visually row-oriented.  Their original
        # marker-sized rectangles (for example 63x22 for ``[0] Yes``) are too
        # easy to miss with a real mouse, especially at 150% DPI.  Make a
        # single choice own its whole row; if several choices share a row,
        # divide the row at the next marker's x coordinate.
        implicit_by_row: dict[tuple[float, float], list[dict[str, Any]]] = {}
        for region in regions:
            if region.get("implicit"):
                rect = region["rect"]
                implicit_by_row.setdefault((rect.y(), rect.height()), []).append(region)
        for (_y, _height), row in implicit_by_row.items():
            row.sort(key=lambda region: region["rect"].x())
            for index, region in enumerate(row):
                rect = region["rect"]
                left = 0.0 if index == 0 else rect.x()
                right = (
                    row[index + 1]["rect"].x()
                    if index + 1 < len(row)
                    else float(
                        max(
                            self._canvas_width,
                            self.logical_viewport_width(),
                            round(rect.right()),
                        )
                    )
                )
                region["rect"] = QRectF(left, rect.y(), max(1.0, right - left), rect.height())
        self._hit_regions = regions

    def _rebuild_paint_index(self) -> None:
        tiles: dict[int, list[int]] = {}
        tile_height = self._paint_tile_height
        for index, item in enumerate(self._drawables):
            kind = str(item.get("type", ""))
            if kind in {"", "print_space", "implicit_button"}:
                continue
            y = _int(item.get("y"))
            height = max(1, _int(item.get("height"), self.line_height))
            first = max(0, y // tile_height)
            last = max(first, (y + height - 1) // tile_height)
            for tile in range(first, last + 1):
                tiles.setdefault(tile, []).append(index)
        self._paint_tiles = tiles

    def _hit_region_at(self, viewport_position: QPoint) -> dict[str, Any] | None:
        scene_position = self.mapToScene(viewport_position)
        for region in reversed(self._hit_regions):
            if region["rect"].contains(scene_position):
                return region
        return None

    def clickable_value_at(self, viewport_position: QPoint) -> str | None:
        region = self._hit_region_at(viewport_position)
        return None if region is None else str(region["value"])

    def tooltip_at(self, viewport_position: QPoint) -> str:
        region = self._hit_region_at(viewport_position)
        return "" if region is None else str(region.get("title", ""))

    def _paint_viewport(self, event: QEvent) -> None:
        painter = QPainter(self.viewport())
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
        )
        # Preserve crisp pixel sprites at integral zoom levels; fractional
        # zooms use smooth sampling to avoid uneven pixel columns.
        painter.setRenderHint(
            QPainter.RenderHint.SmoothPixmapTransform,
            abs(self._zoom - round(self._zoom)) > 0.001,
        )
        painter.fillRect(self.viewport().rect(), self._background)
        scale = self._scene_scale()
        transform = QTransform(
            scale,
            0.0,
            0.0,
            scale,
            -float(self.horizontalScrollBar().value()),
            -float(self.verticalScrollBar().value()),
        )
        painter.setTransform(transform)
        visible = QRectF(
            self.horizontalScrollBar().value() / scale,
            self.verticalScrollBar().value() / scale,
            self.viewport().width() / scale,
            self.viewport().height() / scale,
        )
        self._paint_cached_scene(painter, visible)
        self._paint_hover_overlay(painter, visible)
        painter.end()
        event.accept()

    def _candidate_indices_for(self, visible: QRectF) -> list[int]:
        first_tile = max(0, int(visible.top()) // self._paint_tile_height)
        last_tile = max(first_tile, int(max(visible.top(), visible.bottom())) // self._paint_tile_height)
        candidate_indices: set[int] = set()
        for tile in range(first_tile, last_tile + 1):
            candidate_indices.update(self._paint_tiles.get(tile, ()))
        return sorted(candidate_indices)

    def _paint_cached_scene(self, painter: QPainter, visible: QRectF) -> None:
        """Blit immutable off-screen tiles for the visible scene rectangle."""

        if self._runtime is None:
            self._last_paint_candidate_count = 0
            self._last_painted_count = 0
            return

        candidates = self._candidate_indices_for(visible)
        self._last_paint_candidate_count = len(candidates)
        self._last_painted_count = sum(
            1
            for index in candidates
            if QRectF(
                float(_int(self._drawables[index].get("x"))),
                float(_int(self._drawables[index].get("y"))),
                float(max(1, _int(self._drawables[index].get("width"), 1))),
                float(max(1, _int(self._drawables[index].get("height"), self.line_height))),
            ).intersects(visible)
        )

        tile_width = self._raster_tile_width
        tile_height = self._paint_tile_height
        max_tx = max(0, (self._canvas_width - 1) // tile_width)
        max_ty = max(0, (self._canvas_height - 1) // tile_height)
        first_tx = max(0, int(max(0.0, visible.left())) // tile_width)
        last_tx = min(max_tx, int(max(0.0, visible.right())) // tile_width)
        first_ty = max(0, int(max(0.0, visible.top())) // tile_height)
        last_ty = min(max_ty, int(max(0.0, visible.bottom())) // tile_height)
        if first_tx > last_tx or first_ty > last_ty:
            return
        for ty in range(first_ty, last_ty + 1):
            for tx in range(first_tx, last_tx + 1):
                tile = self._raster_tile(tx, ty)
                if tile is not None:
                    painter.drawImage(QPointF(float(tx * tile_width), float(ty * tile_height)), tile)

    def _raster_tile(self, tx: int, ty: int) -> QImage | None:
        raster_ratio = self._raster_device_pixel_ratio()
        ratio_key = max(1, round(raster_ratio * 1024.0))
        key = (int(tx), int(ty), ratio_key)
        if key in self._raster_tiles:
            self._raster_tiles.move_to_end(key)
            return self._raster_tiles[key]
        left = key[0] * self._raster_tile_width
        top = key[1] * self._paint_tile_height
        width = min(self._raster_tile_width, self._canvas_width - left)
        height = min(self._paint_tile_height, self._canvas_height - top)
        if width <= 0 or height <= 0:
            return None
        # QImage dimensions are physical pixels.  Marking a 1x image as a
        # logical tile and drawing it into a 150% widget forced Qt to enlarge
        # every already-rasterized glyph.  Allocate the backing store at the
        # actual device/zoom ratio instead, while keeping painter coordinates
        # in Emuera's logical 1600x950 space.
        physical_width = max(1, int(math.ceil(width * raster_ratio)))
        physical_height = max(1, int(math.ceil(height * raster_ratio)))
        tile = QImage(physical_width, physical_height, QImage.Format.Format_ARGB32_Premultiplied)
        tile.setDevicePixelRatio(raster_ratio)
        tile.fill(self._background.rgba())
        tile_painter = QPainter(tile)
        tile_painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        tile_painter.translate(-float(left), -float(top))
        self._paint_drawables(
            tile_painter,
            QRectF(float(left), float(top), float(width), float(height)),
            track_stats=False,
        )
        tile_painter.end()
        self._raster_tiles[key] = tile
        self._raster_tiles.move_to_end(key)
        effective_limit = max(4, int(self._raster_tile_limit / max(1.0, raster_ratio * raster_ratio)))
        while len(self._raster_tiles) > effective_limit:
            self._raster_tiles.popitem(last=False)
        return tile

    def _raster_device_pixel_ratio(self) -> float:
        """Resolution needed for a one-to-one tile blit at current DPI/zoom."""

        # Native DPR multiplied by the scene transform equals user zoom in
        # original physical pixels.  At 100% this is exactly 1x, so cached
        # glyphs are blitted one-to-one rather than scaled by Windows.
        return max(1.0, self._native_device_pixel_ratio() * self._scene_scale())

    def _paint_drawables(
        self,
        painter: QPainter,
        visible: QRectF,
        *,
        track_stats: bool = True,
    ) -> None:
        runtime = self._runtime
        if runtime is None:
            if track_stats:
                self._last_paint_candidate_count = 0
                self._last_painted_count = 0
            return
        default_bg = _qcolor(runtime.default_bgcolor, BACKGROUND)
        ordered_candidates = self._candidate_indices_for(visible)
        if track_stats:
            self._last_paint_candidate_count = len(ordered_candidates)
        painted = 0
        for index in ordered_candidates:
            item = self._drawables[index]
            kind = str(item.get("type", ""))
            if kind in {"", "print_space", "implicit_button"}:
                continue
            x = _int(item.get("x"))
            y = _int(item.get("y"))
            width = max(0, _int(item.get("width")))
            height = max(1, _int(item.get("height"), self.line_height))
            bounds = QRectF(float(x), float(y), float(max(1, width)), float(height))
            if not bounds.intersects(visible):
                continue
            painted += 1
            color = _qcolor(item.get("color"), EMUERA_TEXT)
            bgcolor = _qcolor(item.get("bgcolor"), default_bg)
            if bgcolor.rgb() != default_bg.rgb() and width > 0:
                painter.fillRect(bounds, bgcolor)

            if kind in {"image", "print_image"}:
                source = str(item.get("src", ""))
                if item.get("asset_missing"):
                    self._render_failures.add(source or "<empty>")
                    pixmap = None
                else:
                    pixmap = self._sprite_pixmap(source, width, height)
                if pixmap is not None:
                    painter.drawPixmap(x, y, pixmap)
                else:
                    # Match Emuera's unobtrusive PRINT_RECT-style fallback.
                    # Never paint a long ``[IMG:name]`` debug label into the
                    # game scene; repeated portrait slices otherwise become a
                    # block of overlapping diagnostic text.
                    painter.setPen(QPen(QColor("#606060"), 1.0))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    placeholder = bounds.adjusted(0.0, 0.0, -1.0, -1.0)
                    painter.drawRect(placeholder)
                    if width >= 8 and height >= 8:
                        painter.drawLine(placeholder.topLeft(), placeholder.bottomRight())
                        painter.drawLine(placeholder.topRight(), placeholder.bottomLeft())
                continue

            if kind in {"button", "print_button", "nonbutton", "print_rect"}:
                label = str(item.get("label", ""))
                if kind == "print_rect":
                    # Emuera's PRINT_RECT is a square placeholder, not a
                    # modern rounded card.
                    painter.setPen(QPen(color, 1.0))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawRect(bounds.adjusted(0.0, 0.0, -1.0, -1.0))
                elif label and not item.get("label_already_painted"):
                    # PRINTBUTTON/HTML button visuals are plain text in the
                    # original frontend.  Click geometry is maintained by the
                    # separate hit layer and is intentionally not painted.
                    self._draw_text(painter, label, x, y, height, color, item)
                continue

            if kind in {"text", "html_text"}:
                text = str(item.get("text", ""))
                if text:
                    self._draw_text(painter, text, x, y, height, color, item)
        if track_stats:
            self._last_painted_count = painted

    def _paint_hover_overlay(self, painter: QPainter, visible: QRectF) -> None:
        """Paint the original yellow focus text over an immutable base tile."""

        order = self._hovered_order
        if order is None or not (0 <= order < len(self._drawables)) or self._runtime is None:
            return
        item = self._drawables[order]
        kind = str(item.get("type", ""))
        label = str(item.get("label", ""))
        if kind not in {"button", "print_button"} or not label:
            return
        x = _int(item.get("x"))
        y = _int(item.get("y"))
        width = max(1, _int(item.get("width"), 1))
        height = max(1, _int(item.get("height"), self.line_height))
        bounds = QRectF(float(x), float(y), float(width), float(height))
        if not bounds.intersects(visible):
            return
        painter.fillRect(bounds, _qcolor(item.get("bgcolor"), _qcolor(self._runtime.default_bgcolor, BACKGROUND)))
        self._draw_text(painter, label, x, y, height, EMUERA_SELECTED_TEXT, item)

    def _draw_text(
        self,
        painter: QPainter,
        text: str,
        x: int,
        y: int,
        height: int,
        color: QColor,
        item: dict[str, Any],
    ) -> None:
        font = _font_for_drawable(item, pixel_size=self.font_pixel_size)
        metrics = QFontMetrics(font)
        padding = 3 if font.italic() else 1
        layer_width = max(1, metrics.horizontalAdvance(text) + padding * 2)
        layer_height = max(1, int(height), metrics.height())
        # Paint onto transparency first.  DirectWrite uses RGB subpixel
        # ClearType on an opaque QImage; when that bitmap is transformed it
        # leaves colored fringes that read as blur.  A transparent glyph mask
        # forces grayscale antialiasing, matching Emuera's TextRenderer while
        # retaining native-resolution edges.
        device = painter.deviceTransform()
        raster_ratio = max(1.0, abs(float(device.m11())), abs(float(device.m22())))
        layer = QImage(
            max(1, int(math.ceil(layer_width * raster_ratio))),
            max(1, int(math.ceil(layer_height * raster_ratio))),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        layer.setDevicePixelRatio(raster_ratio)
        layer.fill(0)
        layer_painter = QPainter(layer)
        layer_painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        layer_painter.setFont(font)
        layer_painter.setPen(QPen(color))
        baseline = max(0.0, (float(height) - metrics.height()) / 2.0) + metrics.ascent()
        layer_painter.drawText(QPointF(float(padding), baseline), text)
        layer_painter.end()
        painter.drawImage(QPointF(float(x - padding), float(y)), layer)

    def _sprite_pixmap(self, source: str, width: int, height: int) -> QPixmap | None:
        raster_ratio = self._raster_device_pixel_ratio()
        ratio_key = max(1, round(raster_ratio * 1024.0))
        key = (source, int(width), int(height), ratio_key)
        if key in self._sprite_cache:
            self._sprite_cache.move_to_end(key)
            cached = self._sprite_cache[key]
            if cached is None:
                self._render_failures.add(source or "<empty>")
            return cached
        pixmap: QPixmap | None = None
        if source and width > 0 and height > 0 and self._runtime is not None:
            try:
                image = self._runtime.render_sprite_image(source).convert("RGBA")
                physical_width = max(1, int(math.ceil(width * raster_ratio)))
                physical_height = max(1, int(math.ceil(height * raster_ratio)))
                if image.size != (physical_width, physical_height):
                    image = image.resize(
                        (physical_width, physical_height),
                        resample=PILImage.Resampling.NEAREST,
                    )
                raw = image.tobytes("raw", "RGBA")
                qimage = QImage(
                    raw,
                    physical_width,
                    physical_height,
                    physical_width * 4,
                    QImage.Format.Format_RGBA8888,
                ).copy()
                qimage.setDevicePixelRatio(raster_ratio)
                pixmap = QPixmap.fromImage(qimage)
            except Exception:
                pixmap = None
                self._render_failures.add(source or "<empty>")
        self._sprite_cache[key] = pixmap
        self._sprite_cache.move_to_end(key)
        while len(self._sprite_cache) > self._sprite_cache_limit:
            self._sprite_cache.popitem(last=False)
        return pixmap

    def _schedule_pointer_refresh(self) -> None:
        serial = self._pointer_event_serial
        QTimer.singleShot(0, lambda: self.refresh_pointer_state(serial))

    def _set_hover_region(self, region: dict[str, Any] | None) -> None:
        order: int | None = None
        if region is not None and not region.get("implicit"):
            candidate = _int(region.get("order"), -1)
            if 0 <= candidate < len(self._drawables):
                item = self._drawables[candidate]
                if str(item.get("type", "")) in {"button", "print_button"} and str(
                    item.get("label", "")
                ):
                    order = candidate
        if order != self._hovered_order:
            self._hovered_order = order
            # Only an immutable tile blit plus a tiny yellow text overlay is
            # repainted.  Plain PRINTL choice rows never invalidate at all.
            self.viewport().update()

    def refresh_pointer_state(self, expected_serial: int | None = None) -> None:
        if expected_serial is not None and expected_serial != self._pointer_event_serial:
            return
        viewport_position = self.viewport().mapFromGlobal(QCursor.pos())
        if not self.viewport().rect().contains(viewport_position):
            self._set_hover_region(None)
            self.pointerMoved.emit(round(self._last_pointer.x()), round(self._last_pointer.y()), None)
            return
        scene_position = self.mapToScene(viewport_position)
        self._last_pointer = scene_position
        region = self._hit_region_at(viewport_position)
        self._set_hover_region(region)
        value = None if region is None else str(region["value"])
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor if value is not None else Qt.CursorShape.ArrowCursor
        )
        self.pointerMoved.emit(round(scene_position.x()), round(scene_position.y()), value)

    def viewportEvent(self, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if event.type() == QEvent.Type.Paint:
            self._paint_viewport(event)
            return True
        if event.type() == QEvent.Type.MouseMove:
            self.mouseMoveEvent(event)
            return True
        if event.type() == QEvent.Type.MouseButtonPress:
            self.mousePressEvent(event)
            return True
        if event.type() == QEvent.Type.MouseButtonRelease:
            self.mouseReleaseEvent(event)
            return True
        if event.type() == QEvent.Type.Wheel:
            self.wheelEvent(event)
            return True
        if event.type() == QEvent.Type.Leave:
            self.viewport().setCursor(Qt.CursorShape.ArrowCursor)
            self._set_hover_region(None)
            self.pointerMoved.emit(round(self._last_pointer.x()), round(self._last_pointer.y()), None)
        return super().viewportEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        self._pointer_event_serial += 1
        position = event.position().toPoint()
        if self._panning:
            delta = position - self._pan_origin
            self._pan_origin = position
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return
        scene_position = self.mapToScene(position)
        self._last_pointer = scene_position
        region = self._hit_region_at(position)
        self._set_hover_region(region)
        value = None if region is None else str(region["value"])
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor if value is not None else Qt.CursorShape.ArrowCursor
        )
        self.pointerMoved.emit(round(scene_position.x()), round(scene_position.y()), value)
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        position = event.position().toPoint()
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_origin = position
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            self.viewport().grabMouse()
            self.userNavigated.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton:
            scene_position = self.mapToScene(position)
            self.activated.emit(
                round(scene_position.x()),
                round(scene_position.y()),
                self.clickable_value_at(position),
            )
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.skipRequested.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.BackButton:
            self.advanceRequested.emit()
            event.accept()
            return
        if event.button() == Qt.MouseButton.ForwardButton:
            self.skipRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.MiddleButton and self._panning:
            self._panning = False
            self.viewport().releaseMouse()
            self._schedule_pointer_refresh()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt API
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.set_zoom(self._zoom * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15))
            event.accept()
            return
        self.userNavigated.emit()
        bar = (
            self.horizontalScrollBar()
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier
            else self.verticalScrollBar()
        )
        bar.setValue(bar.value() - event.angleDelta().y())
        event.accept()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        code = _qt_key_to_emuera(event.key())
        self.keyStateChanged.emit(code, True, not event.isAutoRepeat())
        navigation = {
            Qt.Key.Key_Up: (self.verticalScrollBar(), -self.verticalScrollBar().singleStep()),
            Qt.Key.Key_Down: (self.verticalScrollBar(), self.verticalScrollBar().singleStep()),
            Qt.Key.Key_Left: (self.horizontalScrollBar(), -self.horizontalScrollBar().singleStep()),
            Qt.Key.Key_Right: (self.horizontalScrollBar(), self.horizontalScrollBar().singleStep()),
            Qt.Key.Key_PageUp: (self.verticalScrollBar(), -self.verticalScrollBar().pageStep()),
            Qt.Key.Key_PageDown: (self.verticalScrollBar(), self.verticalScrollBar().pageStep()),
        }
        if event.key() in navigation:
            bar, delta = navigation[event.key()]
            bar.setValue(bar.value() + delta)
            self.userNavigated.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Home:
            self.verticalScrollBar().setValue(0)
            self.userNavigated.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_End:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
            self.userNavigated.emit()
            event.accept()
            return
        if not event.isAutoRepeat() and not (
            event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        ):
            if event.key() in {Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space}:
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    self.skipRequested.emit()
                else:
                    self.advanceRequested.emit()
                event.accept()
                return
            text = event.text()
            if len(text) == 1 and text.isdigit():
                self.quickInputRequested.emit(text)
                event.accept()
                return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        self.keyStateChanged.emit(_qt_key_to_emuera(event.key()), False, False)
        super().keyReleaseEvent(event)

    def scrollContentsBy(self, _dx: int, _dy: int) -> None:  # noqa: N802 - Qt API
        # Never scroll old backing-store pixels; paint a fresh CPU frame.
        self.viewport().update()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._update_scrollbars()
        self.viewport().update()
        self.viewportResized.emit()


class EraMegatenQtWindow(QMainWindow):
    """Modern Qt desktop frontend for interactive engine inspection/play."""

    MAX_TRANSCRIPT_CHARS = 500_000

    def __init__(
        self,
        *,
        root_path: str = "",
        entry: str = "SYSTEM_TITLE",
        max_steps: int = 30_000,
        auto_run: bool = True,
        persist_settings: bool = True,
    ):
        super().__init__()
        self._settings = QSettings("eraMegaten Engine", "Frontend") if persist_settings else None
        if self._settings is not None and not root_path:
            root_path = str(self._settings.value("root", "") or "")
            if entry == "SYSTEM_TITLE":
                entry = str(self._settings.value("entry", entry) or entry)
            try:
                max_steps = max(1, int(self._settings.value("max_steps", max_steps)))
            except (TypeError, ValueError):
                pass
        self.session = FrontendSession(max_steps=max_steps)
        self._busy = False
        self._closing = False
        self._worker: threading.Thread | None = None
        self._worker_bridge = WorkerBridge(self)
        self._worker_bridge.completed.connect(self._worker_completed)
        self._worker_bridge.failed.connect(self._worker_failed)
        self._resize_timer = QTimer(self)
        self._resize_timer.setSingleShot(True)
        self._resize_timer.setInterval(140)
        self._resize_timer.timeout.connect(self._render_runtime)
        self._input_history: list[str] = []
        self._metric_labels: dict[str, QLabel] = {}
        self._busy_controls: list[QWidget] = []
        self._last_action_signature: tuple[tuple[str, str, int], ...] = ()
        self._project_auto_collapsed = False
        self._clean_mode = False
        self._clean_restore_state = (True, True, True)

        self.setObjectName("mainWindow")
        self.setWindowTitle("eraMegaten Engine")
        self.resize(1500, 920)
        self.setMinimumSize(1050, 680)
        self.setWindowIcon(self._make_window_icon())
        self._build_ui(root_path=root_path, entry=entry, max_steps=max_steps)
        self._install_shortcuts()
        self._apply_theme()
        self._restore_window_state()
        self._update_responsive_ui()
        # A saved geometry can belong to a monitor that has since been
        # disconnected, or to a different DPI/layout.  Wait until the native
        # frame exists, then bring the complete window back into a current
        # screen's work area.
        QTimer.singleShot(0, self._ensure_window_on_screen)
        QTimer.singleShot(0, self._update_responsive_ui)
        QApplication.instance().applicationStateChanged.connect(
            lambda state: self.session.set_active(state == Qt.ApplicationState.ApplicationActive)
        )
        if auto_run and root_path and (Path(root_path).expanduser() / "ERB").is_dir():
            QTimer.singleShot(220, self._load_and_run)

    # ---- UI --------------------------------------------------------------
    def _build_ui(self, *, root_path: str, entry: str, max_steps: int) -> None:
        central = QWidget(self)
        central.setObjectName("central")
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 9, 10, 9)
        root_layout.setSpacing(8)
        self.setCentralWidget(central)

        top = QFrame()
        self.project_panel = top
        top.setObjectName("topCard")
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(12, 8, 12, 8)
        top_layout.setSpacing(7)

        heading = QHBoxLayout()
        brand = QLabel("eraMegaten Engine")
        self.brand_label = brand
        brand.setObjectName("brand")
        subtitle = QLabel("现代 EraBasic 运行时 · 交互检阅与兼容性调试")
        self.subtitle_label = subtitle
        subtitle.setObjectName("subtitle")
        heading.addWidget(brand)
        heading.addSpacing(12)
        heading.addWidget(subtitle)
        heading.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setObjectName("busyProgress")
        self.progress.setRange(0, 0)
        self.progress.setFixedWidth(170)
        self.progress.setFixedHeight(5)
        self.progress.hide()
        heading.addWidget(self.progress)
        self.activity_label = QLabel("就绪")
        self.activity_label.setObjectName("activityLabel")
        heading.addWidget(self.activity_label)
        top_layout.addLayout(heading)

        path_row = QHBoxLayout()
        path_row.setSpacing(10)
        path_label = QLabel("游戏目录")
        path_label.setObjectName("fieldLabel")
        path_label.setFixedWidth(72)
        self.path_edit = QLineEdit(root_path)
        self.path_edit.setPlaceholderText("选择包含 ERB 与 CSV 的游戏目录")
        browse = QToolButton()
        browse.setText("浏览…")
        browse.clicked.connect(self._browse)
        path_row.addWidget(path_label)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(browse)
        top_layout.addLayout(path_row)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        entry_label = QLabel("入口")
        entry_label.setObjectName("fieldLabel")
        entry_label.setFixedWidth(72)
        self.entry_box = QComboBox()
        self.entry_box.setEditable(True)
        self.entry_box.addItems(["SYSTEM_TITLE", "SHOP", "BATTLE_START", "DICTIONARY_MENU"])
        self.entry_box.setCurrentText(entry or "SYSTEM_TITLE")
        self.entry_box.setMinimumWidth(160)
        self.entry_box.setMaximumWidth(260)
        step_label = QLabel("步数片")
        step_label.setObjectName("fieldLabel")
        self.max_steps_edit = QLineEdit(str(max(1, int(max_steps))))
        self.max_steps_edit.setFixedWidth(90)
        self.run_button = QPushButton("加载并运行")
        self.run_button.setObjectName("primaryButton")
        self.run_button.clicked.connect(self._load_and_run)
        self.rerun_button = QPushButton("重新运行")
        self.rerun_button.clicked.connect(self._rerun)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("dangerButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        self.export_button = QPushButton("导出当前页")
        self.export_button.clicked.connect(self._export_page)
        controls.addWidget(entry_label)
        controls.addWidget(self.entry_box)
        controls.addWidget(step_label)
        controls.addWidget(self.max_steps_edit)
        controls.addStretch(1)
        controls.addWidget(self.run_button)
        controls.addWidget(self.rerun_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.export_button)
        top_layout.addLayout(controls)
        root_layout.addWidget(top)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        root_layout.addWidget(self.splitter, 1)

        game_panel = QFrame()
        self.game_panel = game_panel
        game_panel.setMinimumWidth(0)
        game_panel.setObjectName("panel")
        game_layout = QVBoxLayout(game_panel)
        game_layout.setContentsMargins(0, 0, 0, 0)
        game_layout.setSpacing(0)
        game_header = QFrame()
        game_header.setObjectName("panelHeader")
        game_header_layout = QHBoxLayout(game_header)
        game_header_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        game_header_layout.setContentsMargins(14, 9, 12, 9)
        self.state_badge = QLabel("● 未加载")
        self.state_badge.setObjectName("stateBadge")
        game_header_layout.addWidget(self.state_badge)
        self.pointer_label = QLabel("鼠标 —")
        self.pointer_label.setObjectName("pointerLabel")
        self.pointer_label.setFixedWidth(170)
        game_header_layout.addWidget(self.pointer_label)
        self.renderer_badge = QLabel("原版离屏")
        self.renderer_badge.setObjectName("rendererBadge")
        self.renderer_badge.setToolTip("原版 Emuera 样式的不可变离屏瓦片；透明命中层独立处理点击")
        game_header_layout.addWidget(self.renderer_badge)
        game_header_layout.addStretch(1)
        self.follow_check = QCheckBox("跟随输出")
        self.follow_check.setChecked(True)
        game_header_layout.addWidget(self.follow_check)
        self.latest_button = QToolButton()
        self.latest_button.setText("最新")
        self.latest_button.setToolTip("恢复跟随并跳到最新输出（End）")
        self.latest_button.clicked.connect(self._resume_follow)
        game_header_layout.addWidget(self.latest_button)
        self.fit_button = QToolButton()
        self.fit_button.setText("适宽")
        self.fit_button.setToolTip("将完整画布宽度缩放到当前窗口（Ctrl+9）")
        game_header_layout.addWidget(self.fit_button)
        self.zoom_out = QToolButton()
        self.zoom_out.setText("−")
        self.zoom_out.setToolTip("缩小（Ctrl+-）")
        self.zoom_out.clicked.connect(lambda: self.game_view.set_zoom(self.game_view.zoom_factor / 1.15))
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("zoomLabel")
        self.zoom_label.setFixedWidth(48)
        self.zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.zoom_in = QToolButton()
        self.zoom_in.setText("+")
        self.zoom_in.setToolTip("放大（Ctrl++）")
        self.zoom_in.clicked.connect(lambda: self.game_view.set_zoom(self.game_view.zoom_factor * 1.15))
        self.zoom_reset = QToolButton()
        self.zoom_reset.setText("1:1")
        self.zoom_reset.setToolTip("重置缩放（Ctrl+0）")
        self.zoom_reset.clicked.connect(self.game_view.reset_zoom if hasattr(self, "game_view") else lambda: None)
        game_header_layout.addWidget(self.zoom_out)
        game_header_layout.addWidget(self.zoom_label)
        game_header_layout.addWidget(self.zoom_in)
        game_header_layout.addWidget(self.zoom_reset)
        game_layout.addWidget(game_header)

        self.game_view = GameSceneView()
        # zoom_reset was created before the view so reconnect its signal now.
        try:
            self.zoom_reset.clicked.disconnect()
        except Exception:
            pass
        self.zoom_reset.clicked.connect(self.game_view.reset_zoom)
        self.fit_button.clicked.connect(self.game_view.fit_width)
        self.game_view.activated.connect(self._scene_activated)
        self.game_view.pointerMoved.connect(self._pointer_moved)
        self.game_view.keyStateChanged.connect(self._key_state_changed)
        self.game_view.quickInputRequested.connect(self._quick_input)
        self.game_view.advanceRequested.connect(self._advance)
        self.game_view.skipRequested.connect(self._skip_messages)
        self.game_view.zoomChanged.connect(lambda value: self.zoom_label.setText(f"{round(value * 100)}%"))
        self.game_view.userNavigated.connect(lambda: self.follow_check.setChecked(False))
        self.game_view.viewportResized.connect(lambda: self._resize_timer.start())
        self.game_view.setToolTip("左键选择/继续 · 右键批量跳过消息 · 中键拖动画布 · Ctrl+滚轮缩放")
        game_layout.addWidget(self.game_view, 1)
        self.splitter.addWidget(game_panel)

        self.project_toggle = QToolButton()
        self.project_toggle.setText("项目")
        self.project_toggle.setCheckable(True)
        self.project_toggle.setChecked(True)
        self.project_toggle.setToolTip("显示或隐藏项目设置（F8）")
        self.project_toggle.clicked.connect(self._toggle_project_panel)
        game_header_layout.insertWidget(0, self.project_toggle)
        self.inspector_toggle = QToolButton()
        self.inspector_toggle.setText("侧栏")
        self.inspector_toggle.setCheckable(True)
        self.inspector_toggle.setChecked(True)
        self.inspector_toggle.setToolTip("显示或隐藏操作/状态侧栏（F9）")
        self.inspector_toggle.clicked.connect(self._toggle_inspector)
        game_header_layout.insertWidget(1, self.inspector_toggle)
        self.clean_toggle = QToolButton()
        self.clean_toggle.setText("净屏")
        self.clean_toggle.setCheckable(True)
        self.clean_toggle.setToolTip("隐藏项目、侧栏和输入条，最大化原版游戏画布（F10）")
        self.clean_toggle.clicked.connect(self._toggle_clean_mode)
        game_header_layout.insertWidget(2, self.clean_toggle)

        self.inspector = QFrame()
        self.inspector.setObjectName("panel")
        self.inspector.setMinimumWidth(250)
        inspector_layout = QVBoxLayout(self.inspector)
        inspector_layout.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        inspector_layout.addWidget(self.tabs)
        self._build_status_tab()
        self._build_actions_tab()
        self.warning_text = self._plain_text_tab("警告")
        self.transcript_text = self._plain_text_tab("文本")
        self.splitter.addWidget(self.inspector)
        self.splitter.setSizes([1080, 380])

        input_card = QFrame()
        self.input_card = input_card
        input_card.setObjectName("inputCard")
        input_layout = QHBoxLayout(input_card)
        input_layout.setContentsMargins(10, 6, 10, 6)
        input_layout.setSpacing(7)
        prompt = QLabel("输入")
        prompt.setObjectName("fieldLabel")
        self.input_edit = HistoryLineEdit()
        self.input_edit.setPlaceholderText("输入数字或文字；游戏画布聚焦时可直接按数字键")
        self.input_edit.returnPressed.connect(self._submit)
        self.send_button = QPushButton("发送")
        self.send_button.setObjectName("primaryButton")
        self.send_button.clicked.connect(self._submit)
        self.advance_button = QPushButton("继续 / 任意键")
        self.advance_button.clicked.connect(self._advance)
        self.skip_count_box = QComboBox()
        self.skip_count_box.setToolTip("一次最多跳过多少个连续消息等待")
        for count in (5, 20, 50, 100):
            self.skip_count_box.addItem(f"{count} 条", count)
        self.skip_count_box.setCurrentIndex(1)
        self.skip_count_box.setFixedWidth(72)
        self.skip_button = QPushButton("跳过消息")
        self.skip_button.setToolTip("安全跳过连续消息；遇到菜单或文字输入立即停止（右键 / Shift+Space）")
        self.skip_button.setEnabled(False)
        self.skip_button.clicked.connect(self._skip_messages)
        input_layout.addWidget(prompt)
        input_layout.addWidget(self.input_edit, 1)
        input_layout.addWidget(self.send_button)
        input_layout.addWidget(self.advance_button)
        input_layout.addWidget(self.skip_count_box)
        input_layout.addWidget(self.skip_button)
        root_layout.addWidget(input_card)

        self._busy_controls.extend(
            [
                self.run_button,
                self.rerun_button,
                self.export_button,
                self.send_button,
                self.advance_button,
                self.skip_button,
            ]
        )

    def _build_status_tab(self) -> None:
        tab = QWidget()
        self.status_tab = tab
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(14, 14, 14, 12)
        metrics = QGridLayout()
        metric_defs = [
            ("load", "加载耗时"),
            ("program", "文件 / 函数"),
            ("steps", "本次 / 累计步数"),
            ("output", "输出行 / 字符"),
            ("visual", "按钮 / 图片"),
            ("events", "等待 / 声音事件"),
            ("mouse", "引擎鼠标"),
        ]
        for row, (key, title) in enumerate(metric_defs):
            title_label = QLabel(title)
            title_label.setObjectName("metricTitle")
            value_label = QLabel("—")
            value_label.setObjectName("metricValue")
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if key == "mouse":
                value_label.setFixedWidth(170)
            metrics.addWidget(title_label, row, 0)
            metrics.addWidget(value_label, row, 1)
            self._metric_labels[key] = value_label
        metrics.setColumnStretch(1, 1)
        layout.addLayout(metrics)
        stack_title = QLabel("调用栈")
        stack_title.setObjectName("sectionTitle")
        layout.addWidget(stack_title)
        self.stack_tree = QTreeWidget()
        self.stack_tree.setHeaderLabels(["函数", "PC"])
        self.stack_tree.setRootIsDecorated(False)
        self.stack_tree.setAlternatingRowColors(True)
        self.stack_tree.header().setStretchLastSection(False)
        self.stack_tree.header().resizeSection(0, 245)
        layout.addWidget(self.stack_tree, 1)
        self.tabs.addTab(tab, "状态")

    def _build_actions_tab(self) -> None:
        tab = QWidget()
        self.actions_tab = tab
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(9, 9, 9, 8)
        layout.setSpacing(7)
        heading = QHBoxLayout()
        self.action_count_label = QLabel("当前无可选操作")
        self.action_count_label.setObjectName("sectionTitle")
        heading.addWidget(self.action_count_label)
        heading.addStretch(1)
        layout.addLayout(heading)
        self.action_filter = QLineEdit()
        self.action_filter.setPlaceholderText("筛选编号或说明（Ctrl+K）")
        self.action_filter.setClearButtonEnabled(True)
        self.action_filter.textChanged.connect(self._filter_actions)
        self.action_filter.returnPressed.connect(self._activate_first_filtered_action)
        layout.addWidget(self.action_filter)
        self.action_list = QListWidget()
        self.action_list.setObjectName("actionList")
        self.action_list.setViewMode(QListView.ViewMode.IconMode)
        self.action_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.action_list.setMovement(QListView.Movement.Static)
        self.action_list.setFlow(QListView.Flow.LeftToRight)
        self.action_list.setWrapping(True)
        self.action_list.setUniformItemSizes(True)
        self.action_list.setGridSize(QSize(132, 32))
        self.action_list.setSpacing(1)
        self.action_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.action_list.itemClicked.connect(self._activate_action_item)
        layout.addWidget(self.action_list, 1)
        hint = QLabel("单击即选择 · 数字键可直接输入 · 右键画布跳过连续消息")
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.tabs.addTab(tab, "操作")

    def _update_actions(self, layout_model: dict[str, Any]) -> None:
        rows = {
            _int(row.get("index")): str(row.get("text", "")).strip()
            for row in layout_model.get("rows", [])
            if isinstance(row, dict)
        }
        collected: dict[tuple[str, int], dict[str, Any]] = {}
        for order, drawable in enumerate(layout_model.get("drawables", [])):
            if not isinstance(drawable, dict):
                continue
            kind = str(drawable.get("type", ""))
            value: str | None = None
            label = ""
            if kind in {"button", "print_button", "implicit_button"}:
                raw = str(drawable.get("value", ""))
                if raw != "" or drawable.get("activate_empty"):
                    value = raw
                label = str(drawable.get("label", ""))
            elif kind in {"image", "print_image"} and drawable.get("parent") == "button":
                raw = str(drawable.get("parent_value", ""))
                if raw != "":
                    value = raw
                label = str(
                    drawable.get("parent_title", "")
                    or drawable.get("title", "")
                    or drawable.get("src", "")
                )
            if value is None:
                continue
            line = _int(drawable.get("line"))
            if kind == "implicit_button" or not label.strip() or label.strip() == f"[{value}]":
                label = rows.get(line, label)
            label = " ".join(label.split()) or ("确认" if value == "" else f"选项 {value}")
            key = (value, line)
            candidate = {
                "value": value,
                "label": label,
                "line": line,
                "x": _int(drawable.get("x")),
                "order": order,
            }
            previous = collected.get(key)
            if previous is None or len(label) > len(str(previous.get("label", ""))):
                collected[key] = candidate
        actions = sorted(collected.values(), key=lambda row: (row["line"], row["x"], row["order"]))
        signature = tuple((row["value"], row["label"], row["line"]) for row in actions)
        previous_count = len(self._last_action_signature)
        self._last_action_signature = signature
        self.action_list.blockSignals(True)
        self.action_list.clear()
        for row in actions:
            value = str(row["value"])
            label = str(row["label"])
            marker = "Enter" if value == "" else value
            display = label if f"[{value}]" in label or (value == "" and "Enter" in label) else f"[{marker}] {label}"
            item = QListWidgetItem(display)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setToolTip(f"提交 {value!r}\n{label}")
            self.action_list.addItem(item)
        self.action_list.blockSignals(False)
        count = len(actions)
        self.action_count_label.setText(f"当前 {count} 个可选操作" if count else "当前无可选操作")
        self._filter_actions(self.action_filter.text())
        if count >= 10 and previous_count < 10 and self.inspector.isVisible():
            self.tabs.setCurrentWidget(self.actions_tab)

    def _filter_actions(self, query: str) -> None:
        needle = str(query).strip().casefold()
        first_visible: QListWidgetItem | None = None
        for index in range(self.action_list.count()):
            item = self.action_list.item(index)
            hidden = bool(needle and needle not in item.text().casefold())
            item.setHidden(hidden)
            if not hidden and first_visible is None:
                first_visible = item
        if first_visible is not None:
            self.action_list.setCurrentItem(first_visible)

    def _activate_action_item(self, item: QListWidgetItem) -> None:
        if self._busy or self.session.runtime is None:
            return
        self._submit(str(item.data(Qt.ItemDataRole.UserRole) or ""))

    def _activate_first_filtered_action(self) -> None:
        for index in range(self.action_list.count()):
            item = self.action_list.item(index)
            if not item.isHidden():
                self._activate_action_item(item)
                return

    def _plain_text_tab(self, title: str) -> QPlainTextEdit:
        text = QPlainTextEdit()
        text.setReadOnly(True)
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPixelSize(13)
        text.setFont(font)
        self.tabs.addTab(text, title)
        return text

    def _set_project_visible(self, visible: bool) -> None:
        self.project_panel.setVisible(bool(visible))
        self.project_toggle.blockSignals(True)
        self.project_toggle.setChecked(bool(visible))
        self.project_toggle.blockSignals(False)
        self._update_responsive_ui()

    def _toggle_project_panel(self, checked: bool | None = None) -> None:
        visible = not self.project_panel.isVisible() if checked is None else bool(checked)
        self._set_project_visible(visible)

    def _set_inspector_visible(self, visible: bool) -> None:
        self.inspector.setVisible(bool(visible))
        self.inspector_toggle.blockSignals(True)
        self.inspector_toggle.setChecked(bool(visible))
        self.inspector_toggle.blockSignals(False)
        self._update_responsive_ui()

    def _toggle_inspector(self, checked: bool | None = None) -> None:
        visible = not self.inspector.isVisible() if checked is None else bool(checked)
        self._set_inspector_visible(visible)

    def _toggle_clean_mode(self, checked: bool | None = None) -> None:
        enabled = not self._clean_mode if checked is None else bool(checked)
        if enabled == self._clean_mode:
            return
        if enabled:
            self._clean_restore_state = (
                self.project_panel.isVisible(),
                self.inspector.isVisible(),
                self.input_card.isVisible(),
            )
            self._set_project_visible(False)
            self._set_inspector_visible(False)
            self.input_card.hide()
        else:
            project, inspector, input_visible = self._clean_restore_state
            self._set_project_visible(project)
            self._set_inspector_visible(inspector)
            self.input_card.setVisible(input_visible)
        self._clean_mode = enabled
        self.clean_toggle.blockSignals(True)
        self.clean_toggle.setChecked(enabled)
        self.clean_toggle.blockSignals(False)
        self.game_view.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _update_responsive_ui(self) -> None:
        """Keep the game canvas and inspector usable on narrow/high-DPI windows."""

        width = self.width()
        inspector_reserve = min(380, max(250, width // 4)) if self.inspector.isVisible() else 0
        game_width = max(1, width - inspector_reserve)
        wide = game_width >= 1260
        medium = game_width >= 1000
        self.subtitle_label.setVisible(wide and self.project_panel.isVisible())
        self.pointer_label.setVisible(wide)
        self.renderer_badge.setVisible(wide)
        self.fit_button.setVisible(medium)
        for control in (self.zoom_out, self.zoom_label, self.zoom_in, self.zoom_reset):
            control.setVisible(medium)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if hasattr(self, "game_view"):
            self._update_responsive_ui()

    def _resume_follow(self) -> None:
        self.follow_check.setChecked(True)
        self.game_view.jump_to_latest()
        self.game_view.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _focus_actions(self) -> None:
        self._set_inspector_visible(True)
        self.tabs.setCurrentWidget(self.actions_tab)
        self.action_filter.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.action_filter.selectAll()

    def _copy_transcript(self) -> None:
        runtime = self.session.runtime
        if runtime is not None:
            QApplication.clipboard().setText("".join(runtime.output))
            self.activity_label.setText("当前文本已复制")

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _install_shortcuts(self) -> None:
        actions = [
            ("rerun", QKeySequence("F5"), self._rerun),
            ("open", QKeySequence("Ctrl+O"), self._browse),
            ("path", QKeySequence("Ctrl+L"), lambda: (self.path_edit.setFocus(), self.path_edit.selectAll())),
            ("zoom-in", QKeySequence("Ctrl++"), lambda: self.game_view.set_zoom(self.game_view.zoom_factor * 1.15)),
            ("zoom-out", QKeySequence("Ctrl+-"), lambda: self.game_view.set_zoom(self.game_view.zoom_factor / 1.15)),
            ("zoom-reset", QKeySequence("Ctrl+0"), self.game_view.reset_zoom),
            ("zoom-fit", QKeySequence("Ctrl+9"), self.game_view.fit_width),
            ("project", QKeySequence("F8"), lambda: self._toggle_project_panel()),
            ("inspector", QKeySequence("F9"), lambda: self._toggle_inspector()),
            ("clean", QKeySequence("F10"), lambda: self._toggle_clean_mode()),
            ("actions", QKeySequence("Ctrl+K"), self._focus_actions),
            ("latest", QKeySequence("Ctrl+End"), self._resume_follow),
            ("skip", QKeySequence("Ctrl+J"), self._skip_messages),
            ("copy", QKeySequence("Ctrl+Shift+C"), self._copy_transcript),
            ("input", QKeySequence("F2"), lambda: self.input_edit.setFocus()),
            ("fullscreen", QKeySequence("F11"), self._toggle_fullscreen),
        ]
        for name, shortcut, callback in actions:
            action = QAction(name, self)
            action.setShortcut(shortcut)
            action.triggered.connect(callback)
            self.addAction(action)

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#central { background: #090e17; color: #e7edf8; }
            QWidget { font-family: "Microsoft YaHei UI", "Segoe UI"; font-size: 13px; }
            QFrame#topCard, QFrame#panel, QFrame#inputCard {
                background: #111827; border: 1px solid #26344d; border-radius: 11px;
            }
            QFrame#panelHeader { background: #141d2e; border: none; border-bottom: 1px solid #26344d; }
            QLabel#brand { font-size: 20px; font-weight: 700; color: #f2f5ff; }
            QLabel#subtitle, QLabel#pointerLabel, QLabel#zoomLabel { color: #8ea0ba; }
            QLabel#fieldLabel, QLabel#metricTitle { color: #8ea0ba; font-size: 12px; }
            QLabel#metricValue { color: #edf2ff; font-weight: 600; }
            QLabel#sectionTitle { color: #c8d3e8; font-weight: 650; padding-top: 8px; }
            QLabel#stateBadge { color: #f5bd58; font-weight: 650; }
            QLabel#rendererBadge {
                color: #7fe0b2; background: #10251f; border: 1px solid #285b49;
                border-radius: 5px; padding: 2px 7px; font-size: 11px; font-weight: 650;
            }
            QLabel#activityLabel { color: #9aa9bf; padding-left: 4px; }
            QLineEdit, QComboBox, QPlainTextEdit, QTreeWidget, QListWidget {
                background: #0c1320; color: #e7edf8; border: 1px solid #2b3a55;
                border-radius: 7px; padding: 7px 9px; selection-background-color: #5265c9;
            }
            QLineEdit:focus, QComboBox:focus, QPlainTextEdit:focus { border: 1px solid #7c8cff; }
            QComboBox QAbstractItemView { background: #111827; color: #e7edf8; selection-background-color: #35436d; }
            QPushButton, QToolButton {
                background: #1a263a; color: #dce5f5; border: 1px solid #31415e;
                border-radius: 7px; padding: 7px 12px; font-weight: 600;
            }
            QPushButton:hover, QToolButton:hover { background: #253554; border-color: #6376ca; }
            QPushButton:pressed, QToolButton:pressed { background: #18233a; }
            QToolButton:checked { background: #2b3b64; border-color: #7c8cff; color: #ffffff; }
            QPushButton:disabled, QToolButton:disabled { color: #5e6b80; background: #131b29; border-color: #202c40; }
            QPushButton#primaryButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #6676ed, stop:1 #895fdf);
                color: white; border: 1px solid #9aa6ff;
            }
            QPushButton#primaryButton:hover { background: #7d8cff; }
            QPushButton#dangerButton { color: #ff9cad; border-color: #6a3342; background: #281822; }
            QPushButton#dangerButton:hover { color: white; background: #9e4057; border-color: #d65a75; }
            QCheckBox { color: #bcc8dc; spacing: 7px; }
            QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #455675; border-radius: 4px; background: #0c1320; }
            QCheckBox::indicator:checked { background: #7c8cff; border-color: #a5afff; }
            QTabWidget::pane { border: none; background: #111827; }
            QTabBar::tab { color: #899ab3; background: transparent; padding: 10px 16px; border-bottom: 2px solid transparent; }
            QTabBar::tab:selected { color: #f0f4ff; border-bottom: 2px solid #7c8cff; }
            QTabBar::tab:hover { color: #cdd7e8; }
            QTreeWidget { alternate-background-color: #101a2a; padding: 2px; }
            QListWidget#actionList { padding: 2px; background: #050505; }
            QListWidget#actionList::item {
                background: #050505; border: 1px solid #242424; border-radius: 0;
                padding: 3px 5px; margin: 1px; color: #c0c0c0;
            }
            QListWidget#actionList::item:hover { background: #050505; border-color: #666666; color: #ffff00; }
            QListWidget#actionList::item:selected { background: #050505; border-color: #888888; color: #ffff00; }
            QHeaderView::section { background: #162136; color: #91a2bb; border: none; border-bottom: 1px solid #2b3a55; padding: 7px; }
            QProgressBar#busyProgress { background: #1d2940; border: none; border-radius: 2px; }
            QProgressBar#busyProgress::chunk { background: #7c8cff; border-radius: 2px; }
            QSplitter::handle { background: transparent; width: 8px; }
            QScrollBar:vertical { background: transparent; width: 11px; margin: 2px; }
            QScrollBar::handle:vertical { background: #34435d; min-height: 28px; border-radius: 5px; }
            QScrollBar::handle:vertical:hover { background: #506181; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar:horizontal { background: transparent; height: 11px; margin: 2px; }
            QScrollBar::handle:horizontal { background: #34435d; min-width: 28px; border-radius: 5px; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
            QAbstractScrollArea#gameView QScrollBar:vertical {
                background: #000000; width: 14px; margin: 0; border-left: 1px solid #333333;
            }
            QAbstractScrollArea#gameView QScrollBar::handle:vertical {
                background: #c0c0c0; min-height: 24px; border-radius: 0;
            }
            QAbstractScrollArea#gameView QScrollBar:horizontal {
                background: #000000; height: 14px; margin: 0; border-top: 1px solid #333333;
            }
            QAbstractScrollArea#gameView QScrollBar::handle:horizontal {
                background: #c0c0c0; min-width: 24px; border-radius: 0;
            }
            QToolTip { color: #eef3ff; background: #182238; border: 1px solid #506181; padding: 5px; }
            """
        )

    def _restore_window_state(self) -> None:
        if self._settings is None:
            return
        geometry = self._settings.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        splitter = self._settings.value("splitter")
        if splitter is not None:
            self.splitter.restoreState(splitter)
        self.follow_check.setChecked(self._settings.value("follow", True, type=bool))
        self._set_inspector_visible(self._settings.value("inspector_visible", True, type=bool))
        self._set_project_visible(self._settings.value("project_visible", True, type=bool))

    def _ensure_window_on_screen(self) -> None:
        screens = QGuiApplication.screens()
        if not screens:
            return
        frame = self.frameGeometry()
        available_rects = [screen.availableGeometry() for screen in screens]
        if any(available.contains(frame) for available in available_rects):
            return

        def overlap_area(available: QRect) -> int:
            overlap = available.intersected(frame)
            return max(0, overlap.width()) * max(0, overlap.height())

        available = max(available_rects, key=overlap_area)
        target_width = min(frame.width(), available.width())
        target_height = min(frame.height(), available.height())
        if target_width != frame.width() or target_height != frame.height():
            # resize() uses client dimensions while frameGeometry() includes
            # decorations.  The second clamp below uses the actual resulting
            # frame and therefore remains correct across Windows themes/DPI.
            self.resize(target_width, target_height)
            frame = self.frameGeometry()

        max_x = available.right() - frame.width() + 1
        max_y = available.bottom() - frame.height() + 1
        x = available.left() if max_x < available.left() else min(max(frame.left(), available.left()), max_x)
        y = available.top() if max_y < available.top() else min(max(frame.top(), available.top()), max_y)
        self.move(x, y)

    @staticmethod
    def _make_window_icon() -> QIcon:
        pixmap = QPixmap(64, 64)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(4, 4, 56, 56), 14, 14)
        painter.fillPath(path, QBrush(QColor("#6f7df4")))
        font = QFont("Segoe UI", 27, QFont.Weight.Bold)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(QRectF(4, 2, 56, 58), Qt.AlignmentFlag.AlignCenter, "e")
        painter.end()
        return QIcon(pixmap)

    # ---- commands --------------------------------------------------------
    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "选择游戏目录",
            self.path_edit.text().strip() or str(Path.cwd()),
        )
        if selected:
            self.path_edit.setText(selected)

    def _read_max_steps(self) -> int:
        try:
            value = max(1, int(self.max_steps_edit.text().strip()))
        except ValueError:
            value = 30_000
            self.max_steps_edit.setText(str(value))
        self.session.max_steps = value
        return value

    def _load_and_run(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            self._browse()
            path = self.path_edit.text().strip()
        if not path:
            return
        self.follow_check.setChecked(True)
        entry = self.entry_box.currentText().strip() or "SYSTEM_TITLE"
        self._read_max_steps()
        if self._settings is not None:
            self._settings.setValue("root", path)
            self._settings.setValue("entry", entry)
            self._settings.setValue("max_steps", self.session.max_steps)
        self._run_async("正在建立脚本与资源索引…", lambda: self.session.load(path, entry=entry))

    def _rerun(self) -> None:
        if self.session.program is None:
            self._load_and_run()
            return
        self.follow_check.setChecked(True)
        entry = self.entry_box.currentText().strip() or "SYSTEM_TITLE"
        self._read_max_steps()
        self._run_async(f"正在运行 {entry}…", lambda: self.session.run_entry(entry))

    def _submit(self, value: str | None = None) -> None:
        if self.session.runtime is None or self._busy:
            return
        self.follow_check.setChecked(True)
        text = self.input_edit.text() if value is None else str(value)
        if value is None:
            self.input_edit.clear()
        self.input_edit.remember(text)
        self._read_max_steps()
        self._run_async(f"提交输入 {text!r}…", lambda: self.session.submit(text), can_stop=True)

    def _quick_input(self, value: str) -> None:
        if self.session.runtime is not None and self.session.runtime.waiting_for_input:
            quick = str(value)
            if quick.casefold() in {"y", "n"}:
                choices = {
                    str(self.action_list.item(index).data(Qt.ItemDataRole.UserRole) or ""):
                    self.action_list.item(index).text().casefold()
                    for index in range(self.action_list.count())
                }
                yes_text = choices.get("0", "")
                no_text = choices.get("1", "")
                yes_menu = any(token in yes_text for token in (" yes", "]yes", "はい", " 是", "确定"))
                no_menu = any(token in no_text for token in (" no", "]no", "いいえ", " 否", "取消"))
                if yes_menu and no_menu:
                    quick = "0" if quick.casefold() == "y" else "1"
            self._submit(quick)

    def _advance(self) -> None:
        if self.session.runtime is None or self._busy:
            return
        self.follow_check.setChecked(True)
        self._read_max_steps()
        self._run_async("继续执行…", self.session.advance, can_stop=True)

    def _skip_messages(self) -> None:
        if self.session.runtime is None or self._busy:
            return
        self.follow_check.setChecked(True)
        self._read_max_steps()
        count = int(self.skip_count_box.currentData() or 20)
        self._run_async(
            f"正在安全跳过最多 {count} 条消息…",
            lambda: self.session.skip_messages(count),
            can_stop=True,
        )

    def _stop(self) -> None:
        if self.session.request_stop():
            self.activity_label.setText("停止请求已发送…")
            self.stop_button.setEnabled(False)

    def _scene_activated(self, x: int, y: int, value: object) -> None:
        if self.session.runtime is None or self._busy:
            return
        # Scrolling/panning intentionally pauses output following so old text
        # can be inspected.  A deliberate game action means the user is ready
        # for the next prompt, so resume following before that prompt renders.
        self.follow_check.setChecked(True)
        button_value = None if value is None else str(value)
        boundary = self.session.input_boundary() if button_value is None else {"kind": "choice"}
        if button_value is None and boundary.get("kind") in {"input", "choice"}:
            # Never turn a hit-test miss into an empty ONEINPUT/INPUT answer.
            # It makes a menu appear unresponsive because the script simply
            # redraws the same rows.  Active [n] rows and explicit buttons are
            # submitted only through their independent hit geometry.
            self.activity_label.setText("当前在等待选择；请点击带 [编号] 的整行或使用右侧操作列表")
            return
        self._read_max_steps()
        label = f"点击 [{button_value}]" if button_value is not None else "继续执行"
        self._run_async(
            f"{label} @ {x}, {y}…",
            lambda: self.session.activate_pointer(
                x,
                y,
                button_value,
                advance_if_empty=boundary.get("kind") == "message",
            ),
            can_stop=True,
        )

    def _pointer_moved(self, x: int, y: int, value: object) -> None:
        hover = None if value is None else str(value)
        self.session.update_pointer(x, y, hover)
        suffix = f" · 按钮 {hover}" if hover is not None else ""
        self.pointer_label.setText(f"鼠标 {x}, {y}{suffix}")
        if "mouse" in self._metric_labels:
            self._metric_labels["mouse"].setText(f"{x}, {y}" + (f" / {hover}" if hover is not None else ""))

    def _key_state_changed(self, code: int, pressed: bool, triggered: bool) -> None:
        self.session.update_key(code, pressed=pressed, triggered=triggered)

    def _export_page(self) -> None:
        runtime = self.session.runtime
        if runtime is None or self._busy:
            return
        target, _ = QFileDialog.getSaveFileName(self, "导出当前页", "page.png", "PNG 图像 (*.png)")
        if not target:
            return
        char_width, line_height, viewport, html_scale = self._layout_metrics()
        self._run_async(
            "正在导出当前页…",
            lambda: runtime.export_page_png(
                target,
                char_width=char_width,
                line_height=line_height,
                viewport_width=viewport,
                html_unit_scale=html_scale,
            ),
        )

    # ---- worker ----------------------------------------------------------
    def _run_async(self, label: str, action: Callable[[], Any], *, can_stop: bool = False) -> None:
        if self._busy or self._closing:
            return
        self._busy = True
        self.activity_label.setText(label)
        self.progress.show()
        self._set_busy_controls(False)
        self.stop_button.setEnabled(
            can_stop and self.session.runtime is not None and bool(self.session.runtime.stack)
        )

        def worker() -> None:
            try:
                self._worker_bridge.completed.emit(action())
            except Exception:
                self._worker_bridge.failed.emit(traceback.format_exc())

        self._worker = threading.Thread(target=worker, daemon=True, name="eramegaten-qt-worker")
        self._worker.start()

    def _worker_completed(self, payload: object) -> None:
        if self._closing:
            return
        self._busy = False
        self.progress.hide()
        self._set_busy_controls(True)
        self.stop_button.setEnabled(False)
        self._render_runtime()
        if self.session.runtime is not None and not self._project_auto_collapsed:
            self._project_auto_collapsed = True
            self._set_project_visible(False)
        if isinstance(payload, dict) and "skipped" in payload:
            stopped_labels = {
                "input": "输入/选择",
                "choice": "选择",
                "finished": "运行结束",
                "running": "步数片边界",
                "message": "下一条消息",
            }
            stopped = stopped_labels.get(str(payload.get("stopped_at", "")), str(payload.get("stopped_at", "")))
            self.activity_label.setText(f"已跳过 {payload.get('skipped', 0)} 条消息 · 停在{stopped}")

    def _worker_failed(self, detail: str) -> None:
        if self._closing:
            return
        self._busy = False
        self.progress.hide()
        self._set_busy_controls(True)
        self.stop_button.setEnabled(False)
        self.activity_label.setText("操作失败")
        self.warning_text.setPlainText(detail)
        QMessageBox.critical(self, "eraMegaten Engine", detail.splitlines()[-1] if detail else "操作失败")

    def _set_busy_controls(self, enabled: bool) -> None:
        for control in self._busy_controls:
            control.setEnabled(enabled)

    # ---- render/status ---------------------------------------------------
    def _emuera_render_profile(self) -> tuple[str, int, int, int, int]:
        runtime = self.session.runtime
        if runtime is None:
            return "ＭＳ ゴシック", EMUERA_FONT_SIZE, EMUERA_LINE_HEIGHT, 1600, 950

        def config_int(name: str, default: int) -> int:
            return max(1, _int(_config_raw(runtime, name), default))

        family = _config_raw(runtime, "フォント名").strip() or runtime.default_font or "ＭＳ ゴシック"
        return (
            family,
            config_int("フォントサイズ", EMUERA_FONT_SIZE),
            config_int("一行の高さ", EMUERA_LINE_HEIGHT),
            config_int("ウィンドウ幅", 1600),
            config_int("ウィンドウ高さ", 950),
        )

    def _layout_metrics(self) -> tuple[int, int, int, float]:
        family, font_size, line_height, viewport, _viewport_height = self._emuera_render_profile()
        font = _font_for_drawable({"font": family}, pixel_size=font_size)
        metrics = QFontMetrics(font)
        char_width = max(1, metrics.horizontalAdvance("0"))
        return char_width, line_height, viewport, font_size / 100.0

    def _render_runtime(self) -> None:
        runtime = self.session.runtime
        if runtime is None or self._busy:
            return
        _family, font_size, profile_line_height, profile_width, profile_height = self._emuera_render_profile()
        self.game_view.configure_rendering(
            font_pixel_size=font_size,
            line_height=profile_line_height,
            reference_width=profile_width,
            reference_height=profile_height,
        )
        char_width, line_height, viewport, html_scale = self._layout_metrics()
        try:
            layout = self.session.layout(
                char_width=char_width,
                line_height=line_height,
                viewport_width=viewport,
                html_unit_scale=html_scale,
            )
            truncated = self.game_view.set_layout(
                layout,
                runtime,
                follow_output=self.follow_check.isChecked(),
            )
            self._update_actions(layout)
        except Exception:
            self.warning_text.setPlainText(traceback.format_exc())
            self.activity_label.setText("渲染失败")
            return
        self._update_inspector(truncated=truncated)

    def _update_inspector(self, *, truncated: bool = False) -> None:
        status = self.session.status()
        warnings = [str(value) for value in status.get("warnings", [])]
        warnings.extend(f"前端布局: {value}" for value in self.game_view.layout_issues)
        warnings.extend(f"图片渲染失败: {value}" for value in self.game_view.render_failures)
        self._metric_labels["load"].setText(f"{status.get('load_seconds', 0):.2f} 秒")
        self._metric_labels["program"].setText(f"{status.get('files', 0):,} / {status.get('functions', 0):,}")
        self._metric_labels["steps"].setText(f"{status.get('last_steps', 0):,} / {status.get('total_steps', 0):,}")
        self._metric_labels["output"].setText(f"{status.get('lines', 0):,} / {status.get('output_chars', 0):,}")
        self._metric_labels["visual"].setText(f"{status.get('buttons', 0):,} / {status.get('images', 0):,}")
        self._metric_labels["events"].setText(f"{status.get('timed_waits', 0):,} / {status.get('sound_events', 0):,}")
        runtime = self.session.runtime
        if runtime is not None:
            self._metric_labels["mouse"].setText(
                f"{runtime.mouse_x}, {runtime.mouse_y}" + (f" / {runtime.mouse_button}" if runtime.mouse_button else "")
            )

        self.stack_tree.clear()
        for row in status.get("stack", [])[:100]:
            item = QTreeWidgetItem([str(row.get("function", "")), str(row.get("pc", ""))])
            item.setToolTip(0, str(row.get("source", "")))
            self.stack_tree.addTopLevelItem(item)

        warning_text = "\n".join(warnings) if warnings else "无警告"
        if truncated:
            warning_text += "\n\n画布元素过多，仅渲染末尾 30000 个元素。"
        self.warning_text.setPlainText(warning_text)
        transcript = "".join(runtime.output) if runtime is not None else ""
        if len(transcript) > self.MAX_TRANSCRIPT_CHARS:
            transcript = f"[仅显示末尾 {self.MAX_TRANSCRIPT_CHARS} 字符]\n" + transcript[-self.MAX_TRANSCRIPT_CHARS :]
        transcript_bar = self.transcript_text.verticalScrollBar()
        self.transcript_text.setPlainText(transcript)
        if self.follow_check.isChecked():
            transcript_bar.setValue(transcript_bar.maximum())

        if status.get("stopped"):
            label, color = "已停止", DANGER
        elif status.get("waiting"):
            label, color = "等待输入", WARNING
        elif status.get("finished"):
            label, color = "运行结束", SUCCESS
        elif status.get("step_limited"):
            label, color = "可继续 · 步数片结束", ACCENT_BRIGHT
        else:
            label, color = "已暂停", MUTED
        self.state_badge.setText(f"● {label}")
        self.state_badge.setStyleSheet(f"color: {color.name()}; font-weight: 650;")
        self.activity_label.setText(
            f"{status.get('total_steps', 0):,} 步 · {status.get('lines', 0):,} 行 · {len(warnings)} 警告"
        )
        boundary = self.session.input_boundary()
        can_skip = boundary.get("kind") == "message"
        self.skip_button.setEnabled(can_skip and not self._busy)
        self.skip_button.setToolTip(
            "安全跳过连续消息；遇到菜单或文字输入立即停止（右键 / Shift+Space）"
            if can_skip
            else "当前不是可批量跳过的消息等待；不会自动选择菜单"
        )

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._closing = True
        if self._settings is not None:
            self._settings.setValue("root", self.path_edit.text().strip())
            self._settings.setValue("entry", self.entry_box.currentText().strip())
            self._settings.setValue("max_steps", self.session.max_steps)
            self._settings.setValue("geometry", self.saveGeometry())
            self._settings.setValue("splitter", self.splitter.saveState())
            self._settings.setValue("follow", self.follow_check.isChecked())
            project_visible = self._clean_restore_state[0] if self._clean_mode else self.project_panel.isVisible()
            inspector_visible = self._clean_restore_state[1] if self._clean_mode else self.inspector.isVisible()
            self._settings.setValue("inspector_visible", inspector_visible)
            self._settings.setValue("project_visible", project_visible)
            self._settings.sync()
        event.accept()


def launch_gui(
    root: str = "",
    *,
    entry: str = "SYSTEM_TITLE",
    max_steps: int = 30_000,
    auto_run: bool = True,
) -> int:
    # Qt 6 enables high DPI automatically.  Preserve fractional Windows scale
    # factors (125%/150%) so the tile cache can allocate at the exact native
    # resolution instead of passing through an additional rounded resample.
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("eraMegaten Engine")
    app.setOrganizationName("eraMegaten Engine")
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, BACKGROUND)
    palette.setColor(QPalette.ColorRole.WindowText, TEXT)
    palette.setColor(QPalette.ColorRole.Base, SURFACE)
    palette.setColor(QPalette.ColorRole.Text, TEXT)
    palette.setColor(QPalette.ColorRole.Button, SURFACE_ALT)
    palette.setColor(QPalette.ColorRole.ButtonText, TEXT)
    palette.setColor(QPalette.ColorRole.Highlight, ACCENT)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    font = QFont("Microsoft YaHei UI")
    font.setPixelSize(13)
    app.setFont(font)
    window = EraMegatenQtWindow(
        root_path=root,
        entry=entry,
        max_steps=max_steps,
        auto_run=auto_run,
    )
    window.show()
    return app.exec()
