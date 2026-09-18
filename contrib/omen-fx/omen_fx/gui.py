"""omen-fx-gui -- edit the light bar effects and watch them before saving.

The preview widget on the right runs the *same* generator the daemon uses, so
what it draws is what the bar will do. "Play on the bar" plays it for real.

Effects are read from and written back through the daemon's control socket:
it runs as root and owns /etc/omen-fx/effects.toml, which layers on top of the
hand-written config.toml without ever rewriting it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPalette, QLinearGradient
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QSlider,
    QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from . import effects as fx
from . import layouts as lay
from .zonegui import LayoutPanel
from .client import ClientError, send
from .config import SOCKET_PATH
from .led import ZONE_COUNT, format_color, parse_color
from .surface import bar_frame, preview_key_points, set_bar_blend

# What a four-colour frame costs on the patched driver. Used only to warn when
# a requested duration is shorter than the hardware can actually deliver.
FRAME_FLOOR_MS = 30
FRAME_FLOOR_SLOW_MS = 860

def build_effect(spec: dict, layouts: dict):
    """An effect from a spec that may name a layout instead of being one.

    Resolved through the same function the daemon uses, so the preview cannot
    disagree with the hardware about what a layout contains.
    """
    resolved = lay.expand(spec, layouts or {})
    if not resolved:
        return None
    return fx.build(resolved)


DEFAULTS = {
    "kind": "center_out",
    "color": "00A8FF",
    "speed": 7,
    "cycles": 2,
    "smoothness": 0.45,
    "gamma": 2.2,
    "tail": 0.45,
    "priority": 50,
}


class ColorList(QWidget):
    """One or more colours, in order.

    Effects read the list in two different ways -- ``gradient`` spreads it
    across the machine as stops, everything else cycles through it one colour
    per repetition -- so the editor keeps a single ordered list either way and
    lets the effect decide what it means.
    """

    changed = pyqtSignal()
    MAXIMUM = 8

    # How many colours each kind can actually use. Offering more would just be
    # a knob with no effect; offering fewer would hide a real feature.
    PER_KIND = {
        "solid": (1, 1), "static": (1, 1), "progress": (1, 1),
        "gradient": (2, 8), "alternate": (2, 2),
        "wave": (0, 0), "rainbow": (0, 0),
    }

    def __init__(self):
        super().__init__()
        self._limits = (1, self.MAXIMUM)
        self._colors: list[str] = ["00A8FF"]
        self._row = QHBoxLayout(self)
        self._row.setContentsMargins(0, 0, 0, 0)
        self._buttons: list[QPushButton] = []

        self.add_button = QPushButton("+")
        self.add_button.setFixedWidth(30)
        self.add_button.setToolTip("add a colour")
        self.add_button.clicked.connect(self._add)
        self.del_button = QPushButton("−")
        self.del_button.setFixedWidth(30)
        self.del_button.setToolTip("remove the last colour")
        self.del_button.clicked.connect(self._remove)
        self._rebuild()

    # -- state ------------------------------------------------------------

    def colors(self) -> list[str]:
        return list(self._colors)

    def set_kind(self, kind: str) -> None:
        """Clamp the palette to what this kind can use, silently."""
        low, high = self.PER_KIND.get(str(kind).lower(), (1, self.MAXIMUM))
        self._limits = (low, high)
        if high == 0:
            self._rebuild()
            return
        while len(self._colors) < low:
            self._colors.append(self._colors[-1] if self._colors else "00A8FF")
        del self._colors[high:]
        self._rebuild()

    def set_colors(self, colors) -> None:
        clean = []
        for value in (colors or ["00A8FF"]):
            try:
                clean.append(format_color(parse_color(value)))
            except (ValueError, TypeError):
                continue
        low, high = self._limits
        self._colors = clean[:max(1, high)] or ["00A8FF"]
        while len(self._colors) < low:
            self._colors.append(self._colors[-1])
        self._rebuild()

    # -- widgets ----------------------------------------------------------

    def _rebuild(self) -> None:
        while self._row.count():
            item = self._row.takeAt(0)
            if item.widget() is not None:
                item.widget().setParent(None)
        self._buttons = []
        for index, color in enumerate(self._colors):
            button = QPushButton(f"#{color}")
            button.setFixedSize(86, 26)
            button.setStyleSheet(
                f"background-color: #{color}; color: {_ink(color)}; border: 1px solid #444;")
            button.setToolTip("click to change this colour")
            button.clicked.connect(lambda _checked, i=index: self._pick(i))
            self._row.addWidget(button)
            self._buttons.append(button)
        self._row.addWidget(self.add_button)
        self._row.addWidget(self.del_button)
        self._row.addStretch()
        low, high = self._limits
        self.add_button.setVisible(high > low)
        self.del_button.setVisible(high > low)
        self.add_button.setEnabled(len(self._colors) < high)
        self.del_button.setEnabled(len(self._colors) > low)
        for button in self._buttons:
            button.setVisible(high > 0)

    def _pick(self, index: int) -> None:
        current = QColor(f"#{self._colors[index]}")
        chosen = QColorDialog.getColor(current, self, f"Colour {index + 1}")
        if not chosen.isValid():
            return
        self._colors[index] = f"{chosen.red():02X}{chosen.green():02X}{chosen.blue():02X}"
        self._rebuild()
        self.changed.emit()

    def _add(self) -> None:
        if len(self._colors) >= self._limits[1]:
            return
        self._colors.append(self._colors[-1])
        self._rebuild()
        self.changed.emit()

    def _remove(self) -> None:
        if len(self._colors) <= self._limits[0]:
            return
        self._colors.pop()
        self._rebuild()
        self.changed.emit()


def _ink(color: str) -> str:
    """Readable text over a swatch: dark on light backgrounds, light on dark."""
    r, g, b = parse_color(color)
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 140 else "#FFFFFF"


class GradientBar(QWidget):
    """The gradient itself, with a draggable handle per colour stop.

    Dragging is the point: evenly spaced stops make every gradient look the
    same, and what usually reads well is uneven -- a long wash of one colour
    with a narrow band of another.
    """

    changed = pyqtSignal()
    MARGIN = 12
    BAR_H = 30
    GRIP = 7

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(56)
        self._colors: list[str] = ["001A40", "2A0A60"]
        self._stops: list[float] = [0.0, 1.0]
        self._drag: int | None = None
        self.setToolTip("drag the handles to move the gradient stops")

    # -- state ------------------------------------------------------------

    def set_gradient(self, colors, stops=None) -> None:
        self._colors = list(colors) or ["000000", "FFFFFF"]
        count = len(self._colors)
        even = [i / (count - 1) for i in range(count)] if count > 1 else [0.0]
        if stops and len(stops) >= count:
            self._stops = sorted(max(0.0, min(1.0, float(v))) for v in stops[:count])
        else:
            self._stops = even
        self.update()

    def stops(self) -> list[float]:
        return [round(v, 4) for v in self._stops]

    # -- geometry ---------------------------------------------------------

    def _x_for(self, stop: float) -> float:
        usable = self.width() - 2 * self.MARGIN
        return self.MARGIN + stop * usable

    def _stop_for(self, x: float) -> float:
        usable = max(1, self.width() - 2 * self.MARGIN)
        return max(0.0, min(1.0, (x - self.MARGIN) / usable))

    # -- painting ---------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(18, 18, 20))
        if len(self._colors) < 2:
            return

        gradient = QLinearGradient(self._x_for(0.0), 0, self._x_for(1.0), 0)
        for color, stop in zip(self._colors, self._stops):
            gradient.setColorAt(max(0.0, min(1.0, stop)), QColor(f"#{color}"))
        painter.setBrush(gradient)
        painter.setPen(QColor(60, 60, 66))
        painter.drawRoundedRect(self.MARGIN, 6,
                                self.width() - 2 * self.MARGIN, self.BAR_H, 4, 4)

        y = 6 + self.BAR_H + 6
        for index, (color, stop) in enumerate(zip(self._colors, self._stops)):
            x = int(self._x_for(stop))
            painter.setBrush(QColor(f"#{color}"))
            painter.setPen(QColor(230, 230, 235) if index == self._drag
                           else QColor(120, 120, 128))
            painter.drawEllipse(x - self.GRIP, y, self.GRIP * 2, self.GRIP * 2)

    # -- dragging ---------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        x = event.position().x()
        best, best_dx = None, 1e9
        for index, stop in enumerate(self._stops):
            dx = abs(self._x_for(stop) - x)
            if dx < best_dx:
                best, best_dx = index, dx
        if best is not None and best_dx <= 14:
            self._drag = best
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            return
        # Keep the order: a handle may not pass its neighbours, or the ramp
        # would invert under the cursor.
        low = self._stops[self._drag - 1] + 0.005 if self._drag > 0 else 0.0
        high = (self._stops[self._drag + 1] - 0.005
                if self._drag < len(self._stops) - 1 else 1.0)
        self._stops[self._drag] = max(low, min(high, self._stop_for(event.position().x())))
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, event) -> None:
        if self._drag is not None:
            self._drag = None
            self.update()
            self.changed.emit()


class SurfacePreview(QWidget):
    """Both surfaces, animated exactly as the daemon would drive them.

    The effect is sampled here the same way the engine samples it -- one call
    per tick at the two surfaces' real coordinates -- so what you see is what
    the hardware will do, not a separate approximation of it.
    """

    TICK_MS = 33          # the keyboard's MinUpdateInterval

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(190)
        self._effect = None
        self._t = 0.0
        self._keys = preview_key_points()
        self._key_colors = [(0, 0, 0)] * len(self._keys)
        self._bar_colors = [(0, 0, 0)] * ZONE_COUNT
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)

    def play(self, effect) -> None:
        if effect is None:
            self.stop()
            return
        self._effect = effect
        self._t = 0.0
        self._timer.stop()
        if effect is not None:
            self._sample()
            self._timer.start(self.TICK_MS)

    def stop(self) -> None:
        self._timer.stop()
        self._effect = None
        self._key_colors = [(0, 0, 0)] * len(self._keys)
        self._bar_colors = [(0, 0, 0)] * ZONE_COUNT
        self.update()

    def _advance(self) -> None:
        self._t += self.TICK_MS / 1000.0
        duration = getattr(self._effect, "duration", None)
        if duration is not None and self._t >= duration:
            self._t = 0.0     # loop the preview so it stays watchable
        self._sample()

    OFF = (0, 0, 0)

    def _sample(self) -> None:
        try:
            keys = self._effect.sample(self._keys, self._t)
            bar = bar_frame(self._effect, self._t)
        except Exception:
            self._timer.stop()
            return
        # A layout leaves unclaimed cells transparent. In a default profile
        # nothing is underneath, which is exactly how the hardware will show
        # it, so they are drawn off rather than skipped.
        self._key_colors = [c if c is not None else self.OFF for c in keys]
        self._bar_colors = [c if c is not None else self.OFF for c in bar]
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(18, 18, 20))

        margin = 10
        label_h = 14
        usable_h = self.height() - 2 * margin - label_h
        keys_h = usable_h * 0.62
        bar_h = usable_h - keys_h - 8
        width = self.width() - 2 * margin

        # -- keyboard: 20 columns x 6 rows, laid out on the real coordinates
        cols, rows = 20, 6
        cell_w = width / cols
        cell_h = keys_h / rows
        painter.setPen(Qt.PenStyle.NoPen)
        for i, (r, g, b) in enumerate(self._key_colors):
            col, row = i % cols, i // cols
            x = margin + col * cell_w
            y = margin + row * cell_h
            painter.setBrush(QColor(r, g, b))
            painter.drawRoundedRect(int(x + 1), int(y + 1),
                                    int(cell_w - 2), int(cell_h - 2), 2, 2)

        # -- light bar: four wide segments under it
        top = margin + keys_h + 8
        gap = 6
        seg_w = (width - gap * (ZONE_COUNT - 1)) / ZONE_COUNT
        for i, (r, g, b) in enumerate(self._bar_colors):
            x = margin + i * (seg_w + gap)
            # A soft vertical falloff reads more like a diffused light bar than
            # a flat rectangle would.
            gradient = QLinearGradient(0, top, 0, top + bar_h)
            gradient.setColorAt(0.0, QColor(r, g, b).lighter(130))
            gradient.setColorAt(0.5, QColor(r, g, b))
            gradient.setColorAt(1.0, QColor(r // 2, g // 2, b // 2))
            painter.setBrush(gradient)
            painter.setPen(QColor(40, 40, 44))
            painter.drawRoundedRect(int(x), int(top), int(seg_w), int(bar_h), 6, 6)

        painter.setPen(QColor(120, 120, 128))
        for i in range(ZONE_COUNT):
            x = margin + i * (seg_w + gap)
            painter.drawText(int(x), self.height() - label_h, int(seg_w), label_h,
                             Qt.AlignmentFlag.AlignCenter, f"zone {i + 1}")


class EffectEditor(QWidget):
    """The parameter form for one effect."""

    changed = pyqtSignal()

    def __init__(self, base_mode: bool = False, allow_layout: bool = True,
                 zone_mode: bool = False):
        super().__init__()
        self._spec: dict = dict(DEFAULTS)
        self._extra: dict = {}      # keys we do not edit but must not lose
        self._loading = False
        # A default profile has nowhere to be queued and nothing to preempt, so
        # priority and target make no sense there and are hidden below.
        self.base_mode = base_mode
        # A zone inside a layout: it has no life of its own to end, and its
        # place in the stack is its priority.
        self.zone_mode = zone_mode
        # Neither a default profile nor a zone ever finishes, so "how many
        # repetitions" and "how long in total" have no answer here -- they
        # belong to the alert that plays the thing. What is left is a rate,
        # and the only rate that means anything without an end is cycles per
        # second, which is what the speed control becomes.
        self.endless = base_mode or zone_mode
        # knob name -> (label, field); used to hide what the chosen kind ignores
        self._rows: dict = {}

        # A zone inside a layout may not itself be a layout, so the picker is
        # left out there rather than offered and then rejected.
        self.allow_layout = allow_layout
        self._layout_registry: dict = {}
        # The layout named by the spec last loaded. Kept so that a picker not
        # yet populated -- or one missing a layout that was since deleted --
        # cannot silently turn a layout profile into a plain effect.
        self._loaded_layout = ""
        layout = QVBoxLayout(self)

        # -- zone layout ---------------------------------------------------
        self.layout_box = QGroupBox("Zones")
        form = QFormLayout(self.layout_box)
        self.use_layout = QCheckBox("Use a zone layout")
        self.use_layout.setToolTip(
            "Instead of one effect over the whole surface, apply a layout:\n"
            "each zone gets its own effect. Layouts are made in the Zones tab.")
        self.use_layout.toggled.connect(self._touch)
        form.addRow("", self.use_layout)
        self.layout_pick = QComboBox()
        self.layout_pick.currentTextChanged.connect(self._touch)
        form.addRow("Layout", self.layout_pick)
        self.layout_hint = QLabel()
        self.layout_hint.setWordWrap(True)
        form.addRow("", self.layout_hint)
        self.layout_box.setVisible(allow_layout)
        layout.addWidget(self.layout_box)

        # -- colour --------------------------------------------------------
        look = QGroupBox("Look")
        form = QFormLayout(look)
        self.colors = ColorList()
        self.colors.changed.connect(self._touch)
        form.addRow("Colours", self.colors)
        self.colors_row_label = form.labelForField(self.colors)
        self.gradient_bar = GradientBar()
        self.gradient_bar.changed.connect(self._touch)
        self.gradient_row_label = QLabel("Distribution")
        form.addRow(self.gradient_row_label, self.gradient_bar)
        self.colors_hint = QLabel()
        self.colors_hint.setWordWrap(True)
        form.addRow("", self.colors_hint)

        self.brightness = QSpinBox()
        self.brightness.setRange(-1, 100)
        self.brightness.setSpecialValueText("inherit")
        self.brightness.setSuffix(" %")
        self.brightness.valueChanged.connect(self._touch)
        form.addRow("Brightness", self.brightness)
        layout.addWidget(look)

        # -- timing --------------------------------------------------------
        timing = QGroupBox("Timing")
        form = QFormLayout(timing)

        self.auto_duration = QCheckBox("derive from the speed")
        self.auto_duration.setChecked(True)
        self.auto_duration.toggled.connect(self._toggle_duration)
        self.duration = QSpinBox()
        self.duration.setRange(50, 20000)
        self.duration.setSingleStep(50)
        self.duration.setSuffix(" ms")
        self.duration.setValue(1400)
        self.duration.valueChanged.connect(self._touch)
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.duration)
        row.addWidget(self.auto_duration)
        self._add_row(form, "Duration", holder, "duration")

        self.speed = self._slider(1, 10, 7, form, "Speed",
                                  "how fast the front crosses one zone",
                                  key="speed")
        # A layout has no single rate, so its length can be said two ways.
        # Never both at once: giving a count *and* a total is what used to make
        # a duration silently stretch one cycle instead of repeating it.
        self.length_mode = QComboBox()
        self.length_mode.addItem("duration", "duration")
        self.length_mode.addItem("repeats", "cycles")
        self.length_mode.setToolTip(
            "How long the alert lasts. \u201crepeats\u201d counts cycles of the\n"
            "layout's slowest zone, which is the only measure they share\n"
            "when every zone runs at a pace of its own.")
        self.length_mode.currentIndexChanged.connect(self._touch)
        self._add_row(form, "Lunghezza", self.length_mode, "length_mode")

        self.repeats = QSpinBox()
        self.repeats.setRange(1, 99)
        self.repeats.setValue(1)
        self.repeats.setSuffix(" cycles of the slowest zone")
        self.repeats.valueChanged.connect(self._touch)
        self._add_row(form, "Repeats", self.repeats, "repeats")

        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.1, 10.0)
        self.rate.setSingleStep(0.1)
        self.rate.setDecimals(1)
        self.rate.setValue(1.0)
        self.rate.setSuffix(" cycles/s")
        self.rate.setToolTip(
            "How many full cycles the effect completes per second:\n"
            "one breath, one blink, one pass of the front.\n"
            "0.5 = one cycle every two seconds.")
        self.rate.valueChanged.connect(self._touch)
        self._add_row(form, "Cycles per second", self.rate, "rate")

        self.fade = QSpinBox()
        # -1 rather than a number, so an effect that has never been given a
        # value reads as "inherits" instead of showing a figure the user never
        # chose and then wondering why editing it changes nothing elsewhere.
        self.fade.setRange(-1, 15000)
        self.fade.setSingleStep(100)
        self.fade.setSuffix(" ms")
        self.fade.setSpecialValueText("inherit from the daemon")
        self.fade.setValue(-1)
        self.fade.setToolTip(
            "How long the fade back to the default look takes when THIS\n"
            "alert ends -- the value is per alert.\n"
            "  \u201cinherit from the daemon\u201d uses daemon.fade_back_ms (300 ms as shipped)\n"
            "  0 cuts straight there\n"
            "To change it for every alert at once, edit daemon.fade_back_ms\n"
            "in /etc/omen-fx/config.toml.")
        self.fade.valueChanged.connect(self._touch)
        self._add_row(form, "Fade back", self.fade, "fade")
        self.timing_group = timing
        layout.addWidget(timing)

        # -- smoothness ----------------------------------------------------
        smooth = QGroupBox("Smoothness")
        form = QFormLayout(smooth)
        self.smoothness = self._slider(
            0, 100, 45, form, "Spatial",
            "how much a zone lights up while the front is still on the one "
            "next to it: raise it to remove the step from one LED to the next",
            scale=100.0, key="smoothness")
        self.gamma = self._slider(
            100, 300, 220, form, "Naturalness (gamma)",
            "corrects for the eye's non-linear response; without it a fade "
            "seems to collapse at the top and crawl at the bottom",
            scale=100.0, key="gamma")
        self.tail = self._slider(
            2, 95, 45, form, "Trail",
            "how much a zone holds on as the front leaves it", scale=100.0,
            key="tail")
        self.smooth_group = smooth
        layout.addWidget(smooth)

        # -- misc ----------------------------------------------------------
        other = QGroupBox("Other")
        form = QFormLayout(other)
        self.priority = QSpinBox()
        self.priority.setRange(0, 100)
        self.priority.valueChanged.connect(self._touch)
        form.addRow("Priority", self.priority)
        self.kind = QComboBox()
        self.kind.addItems(fx.CANONICAL)
        self.kind.currentTextChanged.connect(self._touch)
        form.addRow("Kind", self.kind)
        self.kind_label = form.labelForField(self.kind)
        self.target = QComboBox()
        self.target.addItems(["both", "bar", "keys"])
        self.target.setToolTip(
            "where the effect plays: the bar, the keyboard, or both")
        self.target.currentTextChanged.connect(self._touch)
        form.addRow("Surface", self.target)
        self.target_label = form.labelForField(self.target)
        if self.base_mode:
            for widget in (self.priority, self.target):
                label = form.labelForField(widget)
                if label is not None:
                    label.hide()
                widget.hide()
        layout.addWidget(other)

        self.info = QLabel()
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        layout.addStretch()

    def _add_row(self, form, label: str, field, key: str) -> None:
        holder = QLabel(label)
        form.addRow(holder, field)
        self._rows[key] = (holder, field)

    def _slider(self, low, high, value, form, label, tip, scale=1.0, key=None):
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        slider.setValue(value)
        slider.setToolTip(tip)
        readout = QLabel()
        readout.setMinimumWidth(44)
        slider.valueChanged.connect(
            lambda v: readout.setText(f"{v / scale:g}" if scale != 1 else str(v)))
        slider.valueChanged.connect(self._touch)
        readout.setText(f"{value / scale:g}" if scale != 1 else str(value))
        field = QWidget()
        row = QHBoxLayout(field)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(slider)
        row.addWidget(readout)
        holder = QLabel(label)
        holder.setToolTip(tip)
        form.addRow(holder, field)
        if key:
            self._rows[key] = (holder, field)
        slider.scale = scale  # type: ignore[attr-defined]
        return slider

    # -- state ------------------------------------------------------------

    def load(self, spec: dict) -> None:
        self._loading = True
        self._spec = dict(spec)
        # "colors" belongs here: left out, it fell through to _extra and was
        # re-emitted verbatim, so a palette from a previous kind kept
        # overriding the colour actually picked.
        known = {"kind", "color", "colors", "speed", "cycles", "smoothness",
                 "target", "gamma", "tail", "priority", "brightness",
                 "duration_ms", "color2", "stops", "fade_back_ms", "period_ms",
                 # A layout reference is rebuilt from the picker, and its zones
                 # belong to the layout file -- neither may survive in _extra.
                 "layout", "zones", "bar_source"}
        self._extra = {k: v for k, v in spec.items() if k not in known}

        is_layout = str(spec.get("kind", "")).lower() == lay.LAYOUT_KIND
        self._loaded_layout = str(spec.get("layout") or "") if is_layout else ""
        self.use_layout.setChecked(is_layout and self.allow_layout)
        if self._loaded_layout:
            self.layout_pick.setCurrentText(self._loaded_layout)
        self.kind.setCurrentText(
            fx.canonical(spec.get("kind", "center_out")) if not is_layout
            else self.kind.currentText())
        palette = spec.get("colors") or [spec.get("color", "00A8FF")]
        if str(spec.get("kind", "")).lower() == "gradient" and len(palette) < 2:
            palette = list(palette) + [spec.get("color2", "000000")]
        self.colors.set_kind(str(spec.get("kind", "center_out")))
        self.colors.set_colors(palette)
        self.gradient_bar.set_gradient(self.colors.colors(), spec.get("stops"))
        self.speed.setValue(int(spec.get("speed", 7)))
        # A profile stored under the old "speed" knob keeps its look: the rate
        # is read back off the effect it actually builds, not guessed, so
        # opening the editor cannot quietly change what the surface shows.
        period = spec.get("period_ms")
        if (not period and self.endless and spec.get("speed") is not None
                and str(spec.get("kind", "")).lower() != lay.LAYOUT_KIND):
            try:
                period = fx.build(spec).per_cycle * 1000.0
            except (ValueError, TypeError, KeyError):
                period = None
        rate = 1000.0 / float(period) if period else 1.0
        self.rate.setValue(round(min(10.0, max(0.1, rate)), 1))
        self.smoothness.setValue(round(float(spec.get("smoothness", 0.45)) * 100))
        self.target.setCurrentText(str(spec.get("target", "both")))
        self.gamma.setValue(round(float(spec.get("gamma", 2.2)) * 100))
        self.tail.setValue(round(float(spec.get("tail", 0.45)) * 100))
        self.priority.setValue(int(spec.get("priority", 50)))
        self.fade.setValue(int(spec.get("fade_back_ms", -1)))
        self.brightness.setValue(int(spec.get("brightness", -1)))
        # An alert tuned with a repetition count keeps its length: the total is
        # read off the effect it actually builds, so dropping the count from
        # the editor cannot quietly shorten or stretch what it already does.
        if (not self.endless and "duration_ms" not in spec
                and spec.get("cycles") not in (None, 0)):
            try:
                built = fx.build(spec).duration
            except (ValueError, TypeError, KeyError):
                built = None
            if built:
                spec = {**spec, "duration_ms": int(round(built * 1000))}
        by_cycles = ("duration_ms" not in spec
                     and str(spec.get("kind", "")).lower() == lay.LAYOUT_KIND
                     and spec.get("cycles") not in (None, 0))
        self.length_mode.setCurrentIndex(1 if by_cycles else 0)
        self.repeats.setValue(int(spec["cycles"]) if by_cycles else 1)
        has_duration = "duration_ms" in spec
        self.auto_duration.setChecked(not has_duration)
        if has_duration:
            self.duration.setValue(int(spec["duration_ms"]))
        self.duration.setEnabled(has_duration)
        self._loading = False
        self._touch()

    def spec(self) -> dict:
        """Only the parameters this kind actually reads.

        Emitting a hidden knob would be worse than useless: a leftover
        ``cycles`` on a ``solid`` would give it a duration and make it end,
        which is exactly the sort of ghost value that is impossible to explain
        from the editor.
        """
        if self._layout_mode:
            return self._layout_spec()
        kind = self.kind.currentText()
        knobs = fx.knobs_for(kind)
        chosen = self.colors.colors()
        out = dict(self._extra)
        out["kind"] = kind
        out["color"] = chosen[0]
        for key in ("speed", "cycles", "smoothness", "gamma", "tail"):
            out.pop(key, None)
        out.pop("period_ms", None)
        if "speed" in knobs:
            if self.endless:
                out["period_ms"] = int(round(1000.0 / self.rate.value()))
            else:
                out["speed"] = self.speed.value()
        if self.endless:
            # Explicit rather than omitted: 0 is what makes it endless, and
            # leaving it out would let an effect's own default end it.
            out["cycles"] = 0
        else:
            # Alerts say how long, never how many times: the effect keeps its
            # own rate and repeats as many times as fit. With a zone layout a
            # single count would be meaningless anyway, since zones run at
            # rates of their own.
            out.pop("cycles", None)
        if "smoothness" in knobs:
            out["smoothness"] = round(self.smoothness.value() / 100, 2)
        if "gamma" in knobs:
            out["gamma"] = round(self.gamma.value() / 100, 2)
        if "tail" in knobs:
            out["tail"] = round(self.tail.value() / 100, 2)

        if not self.base_mode:
            out["priority"] = self.priority.value()
            out["target"] = self.target.currentText()
            if self.fade.value() >= 0:
                out["fade_back_ms"] = self.fade.value()
            else:
                out.pop("fade_back_ms", None)
        else:
            for key in ("priority", "target", "fade_back_ms"):
                out.pop(key, None)

        # Emit the two colour keys coherently: a stale "colors" list would win
        # over "color" inside the effect and silently ignore the pick.
        if len(chosen) > 1:
            out["colors"] = chosen
        else:
            out.pop("colors", None)
        out.pop("color2", None)
        if "stops" in knobs and len(chosen) > 1:
            out["stops"] = self.gradient_bar.stops()
        else:
            out.pop("stops", None)

        if self.brightness.value() >= 0:
            out["brightness"] = self.brightness.value()
        else:
            out.pop("brightness", None)
        if ("duration" in knobs and not self.endless
                and not self.auto_duration.isChecked()):
            out["duration_ms"] = self.duration.value()
        else:
            out.pop("duration_ms", None)
        return out

    @property
    def _layout_name(self) -> str:
        return self.layout_pick.currentText() or self._loaded_layout

    @property
    def _layout_mode(self) -> bool:
        return (self.allow_layout and self.use_layout.isChecked()
                and bool(self._layout_name))

    def _layout_spec(self) -> dict:
        """A reference to a stored layout, plus the knobs that still apply.

        The zones themselves are deliberately not copied in: they live in
        layouts.toml, so editing a layout updates everything using it instead
        of leaving stale copies behind in each effect.
        """
        out = dict(self._extra)
        out["kind"] = lay.LAYOUT_KIND
        out["layout"] = self._layout_name
        for key in ("color", "colors", "stops", "speed", "cycles", "smoothness",
                    "gamma", "tail", "zones", "bar_source"):
            out.pop(key, None)
        if self.base_mode:
            for key in ("priority", "target", "fade_back_ms"):
                out.pop(key, None)
        else:
            out["priority"] = self.priority.value()
            # Which surfaces light up is the layout's own business: its zones
            # claim keys, bar segments or both. Forcing it onto one surface
            # here could only contradict what was painted.
            out["target"] = "both"
            if self.fade.value() >= 0:
                out["fade_back_ms"] = self.fade.value()
            else:
                out.pop("fade_back_ms", None)
        if self.brightness.value() >= 0:
            out["brightness"] = self.brightness.value()
        else:
            out.pop("brightness", None)
        # Zones have no end of their own any more -- they are rates, not runs
        # -- so the alert that plays a layout is the only thing that can say
        # when it stops. A default profile is meant to last forever and says
        # nothing.
        out.pop("cycles", None)
        out.pop("duration_ms", None)
        if not self.base_mode:
            if self.length_mode.currentData() == "cycles":
                out["cycles"] = self.repeats.value()
            else:
                out["duration_ms"] = self.duration.value()
        return out

    def set_layouts(self, names, registry: dict | None = None) -> None:
        """Refresh the pickable layouts, keeping the current choice if it survives."""
        keep = self.layout_pick.currentText()
        blocked = self._loading
        self._loading = True
        self.layout_pick.clear()
        self.layout_pick.addItems(list(names))
        if keep:
            self.layout_pick.setCurrentText(keep)
        self._loading = blocked
        self._layout_registry = dict(registry or {})
        self.use_layout.setEnabled(bool(names))
        self._update_layout_hint()

    def _update_layout_hint(self) -> None:
        if self.layout_pick.count() == 0:
            self.layout_hint.setText(
                "No layouts yet: make one in the Zones tab.")
            return
        if not self._layout_mode:
            self.layout_hint.setText("")
        elif self.base_mode:
            self.layout_hint.setText(
                "Zones are edited in the Zones tab; here you only pick "
                "which layout to use.")
        elif self.length_mode.currentData() == "cycles":
            self.layout_hint.setText(
                "The zones loop forever: the alert lasts the number of "
                "cycles below, counted on the slowest zone.")
        else:
            self.layout_hint.setText(
                "The zones loop forever, so the duration below is what "
                "decides how long the alert lasts. Each zone runs inside it at "
                "a pace of its own.")

    def _sync_layout(self) -> None:
        """In layout mode the whole-surface effect controls do not apply.

        Runs last in ``_touch`` so it overrides what the per-kind syncs just
        decided. Brightness is deliberately left alone: it scales whatever the
        layout draws, exactly as it scales a plain effect.
        """
        self.layout_pick.setVisible(self.allow_layout and self.use_layout.isChecked())
        if not self._layout_mode:
            self._update_layout_hint()
            self.auto_duration.setVisible(not self.endless)
            visible = not self.base_mode
            self.target.setVisible(visible)
            if self.target_label is not None:
                self.target_label.setVisible(visible)
            for key in ("length_mode", "repeats"):
                row = self._rows.get(key)
                if row is not None:
                    row[0].setVisible(False)
                    row[1].setVisible(False)
            return
        for widget in (self.colors, self.gradient_bar, self.gradient_row_label,
                       self.colors_hint, self.kind, self.kind_label,
                       self.colors_row_label):
            if widget is not None:
                widget.setVisible(False)
        self.smooth_group.setVisible(False)
        self._update_layout_hint()

        # An alert says how long the layout runs, either way round; a default
        # profile runs until something else takes over and says neither.
        alert = not self.base_mode
        by_cycles = self.length_mode.currentData() == "cycles"
        for key, wanted in (("length_mode", alert),
                            ("repeats", alert and by_cycles),
                            ("duration", alert and not by_cycles)):
            row = self._rows.get(key)
            if row is not None:
                row[0].setVisible(wanted)
                row[1].setVisible(wanted)

        # Which surfaces light up is decided by the zones, not here.
        self.target.setVisible(False)
        if self.target_label is not None:
            self.target_label.setVisible(False)

        # "derive from the speed" has nothing to derive from: a layout has no
        # single speed, and its zones never end.
        self.auto_duration.setVisible(False)
        if self.auto_duration.isChecked():
            self.auto_duration.setChecked(False)
        self.duration.setEnabled(True)
        for key in ("speed", "cycles", "smoothness", "gamma", "tail"):
            row = self._rows.get(key)
            if row is not None:
                row[0].setVisible(False)
                row[1].setVisible(False)

    def _toggle_duration(self, auto: bool) -> None:
        self.duration.setEnabled(not auto)
        self._touch()

    def _sync_knobs(self) -> None:
        """Show only the knobs this kind reads, and hide groups left empty."""
        knobs = fx.knobs_for(self.kind.currentText())
        for key, (label, field) in self._rows.items():
            if key == "fade":
                # Nothing that never ends has anything to fade back to.
                visible = not self.endless
            elif key == "rate":
                visible = self.endless and "speed" in knobs
            elif key == "speed":
                visible = (not self.endless) and "speed" in knobs
            elif key == "duration":
                # Total length is the playing alert's business.
                visible = (not self.endless) and key in knobs
            else:
                visible = key in knobs
            label.setVisible(visible)
            field.setVisible(visible)
        self.smooth_group.setVisible(
            bool(knobs & {"smoothness", "gamma", "tail"}))

    def _sync_colors(self) -> None:
        """Show only the colour controls this kind can actually use."""
        kind = self.kind.currentText().lower()
        self.colors.set_kind(kind)
        uses_colors = self.colors.PER_KIND.get(kind, (1, self.colors.MAXIMUM))[1] > 0
        self.colors.setVisible(uses_colors)
        if self.colors_row_label is not None:
            self.colors_row_label.setVisible(uses_colors)
        is_gradient = kind == "gradient"
        self.gradient_bar.setVisible(is_gradient)
        self.gradient_row_label.setVisible(is_gradient)
        if is_gradient:
            # Keep the handles where they were when only a colour changed.
            self.gradient_bar.set_gradient(self.colors.colors(),
                                           self.gradient_bar.stops())

    def _update_hint(self) -> None:
        kind = self.kind.currentText().lower()
        count = len(self.colors.colors())
        if kind == "gradient":
            text = "the gradient stops, left to right (or top down when axis=v)"
        elif kind == "alternate":
            text = "the two colours alternate by quarters"
        elif kind in ("wave", "rainbow"):
            text = "this kind generates its own rainbow colours"
        elif count > 1:
            text = f"one colour per repetition, cycled ({count} in all)"
        elif self.colors.PER_KIND.get(kind, (1, 8))[1] > 1:
            text = "add more with + to cycle them one per repetition"
        else:
            text = ""
        self.colors_hint.setText(text)

    def _touch(self, *_args) -> None:
        self._sync_knobs()
        self._sync_colors()
        self._update_hint()
        self._sync_layout()
        if not self._loading:
            self.changed.emit()

    def describe(self, effect, fast: bool) -> None:
        """Say what the effect will really do, hardware limits included."""
        if effect is None:
            self.info.setText("")
            return
        target = self.target.currentText()
        duration = getattr(effect, "duration", None)
        text = ("duration: unlimited (until released)"
                if duration is None else f"duration: {duration:.2f} s")

        # The keyboard firmware caps updates at 30 Hz; a bar write costs about
        # 30 ms on top, so driving both at once lands near 24 Hz.
        if target == "keys":
            text += " · ~30 fps"
        elif target == "bar":
            text += f" · ~{1000 / (FRAME_FLOOR_MS if fast else FRAME_FLOOR_SLOW_MS):.0f} fps"
        else:
            text += " \u00b7 ~24 fps (keyboard and bar together)"
        if target != "keys" and not fast:
            text += ("\n\u26a0 the driver has no \u201czones\u201d attribute: every bar frame "
                     "costs four WMI round trips instead of one.")
        self.info.setText(text)


class BasePanel(QWidget):
    """The default look of each surface -- what shows when no alert is running.

    Kept apart from the alert list on purpose: these are not queued, have no
    priority and never end, so they are a different kind of thing. They live in
    their own file (`base.toml`), the same arrangement as the alerts, so the
    hand-written config.toml is never rewritten by a program.

    The mirror switch belongs to the *keyboard* section even though what it
    stores is the bar's profile, because that is where the decision is made:
    "the bar copies the keyboard". While it is on, the bar has no look of its
    own, so its section is locked rather than silently ignored.
    """

    def __init__(self, send):
        super().__init__()
        self._send = send
        self._loading = False
        self.profiles: dict = {"keys": {}, "bar": {}}
        # Filled in by MainWindow.reload: needed to resolve a profile that
        # names a layout instead of describing an effect.
        self.layouts: dict = {}
        # Hook for saving pending layout edits first; see _ensure_layouts_saved.
        self.before_save = None
        # What to put back on the bar when the mirror is switched off again,
        # so toggling it does not throw the bar's colours away.
        self._bar_backup: dict = {}

        layout = QVBoxLayout(self)

        picker = QHBoxLayout()
        picker.addWidget(QLabel("Surface"))
        self.surface = QComboBox()
        self.surface.addItem("Keyboard \u2014 120 keys", "keys")
        self.surface.addItem("Light bar \u2014 4 zones", "bar")
        self.surface.currentIndexChanged.connect(self._switch)
        picker.addWidget(self.surface)
        picker.addStretch()
        layout.addLayout(picker)

        self.managed = QCheckBox("Let omen-fx manage this surface")
        self.managed.setToolTip(
            "Unticked, omen-fx does not touch this surface at all.\n"
            "For the keyboard that means never taking the LampArray, so the\n"
            "firmware keeps its own lighting and the bar is not mirrored.")
        self.managed.toggled.connect(self._toggle)
        layout.addWidget(self.managed)

        self.mirror = QCheckBox("The light bar copies the keyboard")
        self.mirror.setToolTip(
            "While omen-fx drives the keyboard the firmware already copies\n"
            "it onto the bar. Using that costs no WMI writes at all and keeps\n"
            "the keyboard at a full 30 fps instead of ~24.\n"
            "While it is on, the bar has no look of its own.")
        self.mirror.toggled.connect(self._toggle_mirror)
        layout.addWidget(self.mirror)

        self.preview = SurfacePreview()
        layout.addWidget(self.preview)
        self.editor = EffectEditor(base_mode=True)
        self.editor.changed.connect(self._preview)
        layout.addWidget(self.editor, 1)

        actions = QHBoxLayout()
        self.live = QCheckBox("Show live on the hardware")
        self.live.setChecked(True)
        self.live.setToolTip(
            "Holds both surfaces on what you are preparing, for as long as\n"
            "this window is open. Alerts play over it, exactly as they will\n"
            "for real. It is only a preview: \u201cSave as default\u201d makes it stick.")
        self.live.toggled.connect(self._live_toggled)
        self.save_button = QPushButton("Save as default")
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.live)
        actions.addStretch()
        actions.addWidget(self.save_button)
        layout.addLayout(actions)

        # Sliders fire continuously; without this the socket would get a
        # request per pixel of drag.
        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.setInterval(150)
        self._push_timer.timeout.connect(self._push_live)

        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

    # -- state ------------------------------------------------------------

    @property
    def current(self) -> str:
        return self.surface.currentData()

    @property
    def mirrored(self) -> bool:
        return str(self.profiles.get("bar", {}).get("kind", "")).lower() == "mirror"

    @property
    def keys_layout(self) -> str | None:
        """The layout the keyboard is on, if it is on one."""
        spec = self.profiles.get("keys") or {}
        if str(spec.get("kind", "")).lower() != lay.LAYOUT_KIND:
            return None
        return str(spec.get("layout") or "") or None

    def _enforce_layout_bar(self) -> None:
        """Keep the bar on the same layout as the keyboard.

        A layout covers both surfaces -- its four bar cells are painted in the
        Zone tab beside the keys -- so the mirror has no job left, and the bar
        goes on the layout instead of copying the keyboard.

        It must *follow* later changes too, not just the first one. Firing only
        while the bar was still mirrored left it pointing at whichever layout
        it was put on first, so switching the keyboard to another one left the
        bar playing the old layout's effect.

        A bar given an effect of its own is not touched: only a mirrored bar,
        or one already on a layout, follows.
        """
        name = self.keys_layout
        if name is None:
            return
        bar = self.profiles.get("bar") or {}
        kind = str(bar.get("kind", "")).lower()
        if kind == lay.LAYOUT_KIND and bar.get("layout") == name:
            return                          # already in step
        if kind not in ("mirror", lay.LAYOUT_KIND):
            return                          # the bar has a look of its own
        self.profiles["bar"] = {"kind": lay.LAYOUT_KIND, "layout": name}
        self._bar_backup = dict(self.profiles["bar"])
        was_loading = self._loading
        self._loading = True
        self.mirror.setChecked(False)
        self._loading = was_loading

    def load(self, profiles: dict) -> None:
        self.profiles = {"keys": dict(profiles.get("keys") or {}),
                         "bar": dict(profiles.get("bar") or {})}
        if not self.mirrored and self.profiles["bar"]:
            self._bar_backup = dict(self.profiles["bar"])
        # A hand-edited config may still pair a keyboard layout with a mirrored
        # bar; correct it on the way in rather than showing an impossible state.
        self._enforce_layout_bar()
        self._show(self.current)

    def _fallback(self, name: str) -> dict:
        return {"kind": "gradient", "colors": ["001A40", "2A0A60"],
                # 100 = "as bright as the desktop's keyboard-backlight slider
                # says"; that slider is the master, so a default profile has no
                # reason to hold itself back.
                "cycles": 0, "brightness": 100 if name == "keys" else -1}

    def _show(self, name: str) -> None:
        self._loading = True
        spec = self.profiles.get(name) or {}
        is_mirror = str(spec.get("kind", "")).lower() == "mirror"

        # The switch lives with the keyboard; the bar only suffers it.
        self.mirror.setVisible(name == "keys")
        self.mirror.setChecked(self.mirrored)
        self.mirror.setEnabled(self.keys_layout is None)
        self.managed.setVisible(not (name == "bar" and is_mirror))
        self.managed.setChecked(bool(spec) and not is_mirror)
        self.editor.load(spec if spec and not is_mirror else
                         (self._bar_backup if name == "bar" and self._bar_backup
                          else self._fallback(name)))
        self._loading = False
        self._sync()
        self._preview()

    def _sync(self) -> None:
        """Enable what can be edited, and say why when something cannot."""
        name = self.current
        mirrored = self.mirrored
        # Make the blocked bar section visible as blocked rather than hiding it.
        item = self.surface.model().item(1)
        if item is not None:
            item.setEnabled(not mirrored)
        self.surface.setItemText(
            1, "Light bar \u2014 copies the keyboard" if mirrored
            else "Light bar — 4 zones")

        if name == "bar" and mirrored:
            self.editor.setEnabled(False)
            self.live.setEnabled(False)
            self.note.setText(
                "The bar is copying the keyboard, so it has no look of its "
                "own to set. To give it colours, go back to the Keyboard "
                "section and untick “The light bar copies the keyboard”.")
            return

        self.mirror.setEnabled(self.keys_layout is None)
        managed = self.managed.isChecked()
        self.editor.setEnabled(managed)
        self.live.setEnabled(managed)
        if not managed:
            self.note.setText(
                "omen-fx does not touch this surface: the firmware keeps it."
                if name == "keys" else "omen-fx does not touch the light bar.")
        elif self.keys_layout is not None:
            self.note.setText(
                f"The keyboard is on the “{self.keys_layout}” layout, which "
                "covers the bar too: paint its 4 segments in the Zones tab, "
                "beside the keys. That is why the mirror is not needed here "
                "and is switched off.")
        elif name == "keys" and mirrored:
            self.note.setText("The bar copies this look, with no WMI writes.")
        else:
            self.note.setText("")

    def _commit(self) -> None:
        if self._loading:
            return
        name = self.current
        if name == "bar" and self.mirrored:
            return                      # nothing of the bar's own to store
        if not self.managed.isChecked():
            self.profiles[name] = {}
        else:
            self.profiles[name] = self.editor.spec()
        if name == "bar":
            self._bar_backup = dict(self.profiles["bar"])
        self._enforce_layout_bar()

    def _switch(self) -> None:
        # Both surfaces stay previewed; only the editor moves.
        self._show(self.current)

    def _toggle(self) -> None:
        if self._loading:
            return
        self._commit()
        self._sync()
        self._preview()

    def _toggle_mirror(self, checked: bool) -> None:
        if self._loading:
            return
        self._commit()          # save whatever the keyboard section holds
        if checked:
            if not self.mirrored and self.profiles.get("bar"):
                self._bar_backup = dict(self.profiles["bar"])
            self.profiles["bar"] = {"kind": "mirror"}
        else:
            # Hand the bar back with something to show, so the section is
            # immediately usable instead of arriving switched off.
            self.profiles["bar"] = (dict(self._bar_backup) if self._bar_backup
                                    else self._fallback("bar"))
        self._sync()
        self._preview()

    # One held job per surface: the tab is about the whole machine, so what it
    # shows must not depend on which section happens to be selected. Previewing
    # only the selected surface made the other one snap back to its stored
    # profile the moment you switched -- which read as the lighting resetting.
    LIVE_KEYS = {"keys": "gui-base-live-keys", "bar": "gui-base-live-bar"}

    def _live_toggled(self, on: bool) -> None:
        if on:
            self._push_live()
        else:
            self.stop_live()

    def stop_live(self) -> None:
        """Give both surfaces back to whatever is actually configured."""
        self._push_timer.stop()
        for key in self.LIVE_KEYS.values():
            self._send({"cmd": "release", "key": key})

    def _push_live(self) -> None:
        """Put both surfaces on what the tab currently describes."""
        if not self.live.isChecked():
            return
        for name, key in self.LIVE_KEYS.items():
            spec = dict(self.profiles.get(name) or {})
            # An unmanaged surface, or a bar set to mirror, is one we must not
            # paint: release so the firmware (or the saved profile) has it.
            if not spec or str(spec.get("kind", "")).lower() == "mirror":
                self._send({"cmd": "release", "key": key})
                continue
            spec["cycles"] = 0          # a preview must not end on its own
            # timeout 0: held until we release it, so it cannot expire behind
            # the user's back and look like the lighting resetting itself.
            # Priority 0: the preview stands in for the *default* look, so every
            # alert still interrupts it, and being a hold it comes back after.
            # stand_in says the same to the idle dim: this is not an alert, so
            # it must not wake a dimmed surface.
            self._send({"cmd": "play", "spec": spec, "key": key,
                        "target": name, "priority": 0,
                        "hold": True, "timeout": 0, "stand_in": True})

    def _preview(self) -> None:
        self._commit()
        self._sync()
        if self.live.isChecked():
            self._push_timer.start()
        name = self.current
        spec = self.profiles.get(name) or {}
        if name == "bar" and self.mirrored:
            # Show what the bar will actually display: the keyboard's look.
            spec = self.profiles.get("keys") or {}
        if not spec or str(spec.get("kind", "")).lower() == "mirror":
            self.preview.stop()
            return
        try:
            self.preview.play(build_effect(spec, self.layouts))
        except ValueError:
            self.preview.stop()

    # -- daemon -----------------------------------------------------------

    def save(self) -> None:
        self._commit()
        if self.before_save is not None:
            self.before_save()
        reply = self._send({"cmd": "config", "action": "save_base",
                            "base": self.profiles})
        if reply:
            # Drop the preview so what stays on screen is the saved profile
            # itself, not an overlay that happens to look like it.
            self.stop_live()
            self.note.setText(f"saved to {reply.get('file')} — it is now the default")

KEYS_UNIT = "omen-fx-brightnessd.service"

# The desktop's per-power-source keyboard level lives in KDE's power
# management, which is what already moves the slider when the plug goes in or
# out. omen-fx never writes that slider itself (see led.py), so the level is
# set where the desktop keeps it: powerdevilrc, one [Profile][Keyboard] group
# per profile. LowBattery follows Battery -- the tab offers two cases, not
# three, and nobody wants the keyboard *brighter* once the battery is low.
POWERDEVIL_PROFILES = {"ac": ("AC",), "battery": ("Battery", "LowBattery")}
POWERDEVIL_DEST = "org.kde.Solid.PowerManagement"
POWERDEVIL_PATH = "/org/kde/Solid/PowerManagement"


def _run(argv: list[str], timeout: float = 10) -> tuple[int, str]:
    if not shutil.which(argv[0]):
        return 1, f"{argv[0]} not available"
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, (done.stdout + done.stderr).strip()


def read_powerdevil_levels() -> dict | None:
    """The keyboard level KDE applies on each power source, or None off KDE.

    ``enabled`` is PowerDevil's own "use profile-specific brightness" switch:
    with it off the desktop leaves the keyboard alone when the plug moves.
    """
    if not shutil.which("kreadconfig6"):
        return None
    out: dict = {}
    for source, profiles in POWERDEVIL_PROFILES.items():
        group = profiles[0]
        code, level = _run(["kreadconfig6", "--file", "powerdevilrc",
                            "--group", group, "--group", "Keyboard",
                            "--key", "KeyboardBrightness", "--default", "100"])
        if code != 0:
            return None
        _, use = _run(["kreadconfig6", "--file", "powerdevilrc",
                       "--group", group, "--group", "Keyboard",
                       "--key", "UseProfileSpecificKeyboardBrightness",
                       "--default", "false"])
        try:
            out[source] = int(level)
        except ValueError:
            out[source] = 100
        out.setdefault("enabled", False)
        out["enabled"] = out["enabled"] or use.strip().lower() == "true"
    return out


def write_powerdevil_levels(levels: dict, enabled: bool) -> str | None:
    """Write the levels and have PowerDevil pick them up; an error text, or None."""
    for source, profiles in POWERDEVIL_PROFILES.items():
        for group in profiles:
            for key, value in (("KeyboardBrightness", str(int(levels[source]))),
                               ("UseProfileSpecificKeyboardBrightness",
                                "true" if enabled else "false")):
                code, out = _run(["kwriteconfig6", "--file", "powerdevilrc",
                                  "--group", group, "--group", "Keyboard",
                                  "--key", key, value])
                if code != 0:
                    return out or f"kwriteconfig6 failed ({code})"
    # Two calls, measured rather than assumed: reparseConfiguration only
    # re-reads the file, and it takes loadProfile(true) to run the profile
    # again -- which is what applies the level for the source the machine is
    # on right now, the same as a plug going in or out would.
    for method, args in (("reparseConfiguration", []), ("loadProfile", ["true"])):
        code, out = _run(["gdbus", "call", "--session", "--dest", POWERDEVIL_DEST,
                          "--object-path", POWERDEVIL_PATH,
                          "--method", f"{POWERDEVIL_DEST}.{method}", *args])
        if code != 0:
            return "saved, but PowerDevil could not be told: " + (out or str(code))
    return None


class IdleColumn(QWidget):
    """The idle-dim knobs for one power source."""

    def __init__(self, title: str):
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        heading = QLabel(f"<b>{title}</b>")
        form.addRow(heading)

        self.enabled = QCheckBox("Dim")
        self.enabled.toggled.connect(self._toggle)
        form.addRow(self.enabled)

        self.timeout = QSpinBox()
        self.timeout.setRange(5, 3600)
        self.timeout.setSuffix(" s")
        self.timeout.setSingleStep(5)
        form.addRow("After", self.timeout)

        self.brightness = QSpinBox()
        self.brightness.setRange(0, 100)
        self.brightness.setSuffix(" %")
        self.brightness.setToolTip(
            "How much light is left once dimmed. 0 turns them off.\n"
            "It does not touch your own level: the brightness slider stays\n"
            "where it is, and the first keypress goes straight back to it.")
        form.addRow("Down to", self.brightness)

        self.fade = QDoubleSpinBox()
        self.fade.setRange(0.0, 30.0)
        self.fade.setSingleStep(0.5)
        self.fade.setDecimals(1)
        self.fade.setSuffix(" s")
        self.fade.setToolTip("How long the dim takes. Waking is immediate.")
        form.addRow("Fade", self.fade)

        self.wake = QCheckBox("Alerts wake it")
        self.wake.setToolTip(
            "While an alert plays on a dimmed surface, that surface comes\n"
            "back to full for the alert and dims again after it.\n"
            "Your own level still applies: with the keyboard slider at 0,\n"
            "nothing lights up.")
        form.addRow(self.wake)

    def _toggle(self, on: bool) -> None:
        for widget in (self.timeout, self.brightness, self.fade, self.wake):
            widget.setEnabled(on)

    def load(self, idle: dict) -> None:
        self.enabled.setChecked(bool(idle.get("enabled")))
        self.timeout.setValue(int(idle.get("timeout", 60)))
        self.brightness.setValue(int(idle.get("brightness", 20)))
        self.fade.setValue(float(idle.get("fade_ms", 1500)) / 1000.0)
        self.wake.setChecked(bool(idle.get("wake_for_alerts")))
        self._toggle(self.enabled.isChecked())

    def values(self) -> dict:
        return {
            "enabled": self.enabled.isChecked(),
            "timeout": self.timeout.value(),
            "brightness": self.brightness.value(),
            "fade_ms": int(round(self.fade.value() * 1000)),
            "wake_for_alerts": self.wake.isChecked(),
        }


class SystemPanel(QWidget):
    """Machine-wide switches that are not about how the lights look.

    Three owners, three routes. The brightness-key half is a *user* service,
    driven with `systemctl --user` from here: the daemon runs as root and has
    no business enabling things in someone's session. The per-power-source
    keyboard level belongs to the desktop's power management, which is what
    moves the slider when the plug goes in or out, so it is written where KDE
    keeps it. The idle half is the daemon's, and goes over the control socket
    like every other setting the GUI owns.
    """

    def __init__(self, send):
        super().__init__()
        self._send = send
        self._loading = False
        self._levels_known = False

        layout = QVBoxLayout(self)

        keys = QGroupBox("Brightness keys")
        keys_column = QVBoxLayout(keys)
        self.keys_enabled = QCheckBox(
            "Alt + the brightness keys adjust the keyboard")
        self.keys_enabled.setToolTip(
            "Alt + brightness up/down moves 5%, Shift+Alt moves 1%,\n"
            "and holding a key ramps.\n"
            "The keys on their own still adjust the screen.")
        self.keys_enabled.toggled.connect(self._toggle_keys)
        keys_column.addWidget(self.keys_enabled)
        self.keys_note = QLabel("")
        self.keys_note.setWordWrap(True)
        keys_column.addWidget(self.keys_note)
        layout.addWidget(keys)

        level = QGroupBox("Keyboard level by power source")
        level_column = QVBoxLayout(level)
        self.level_enabled = QCheckBox(
            "Set the keyboard brightness when the plug goes in or out")
        self.level_enabled.setToolTip(
            "This is your brightness slider, moved by the desktop's power\n"
            "management (KDE) whenever the power source changes. Alerts and\n"
            "the idle dim are scaled by it.")
        self.level_enabled.toggled.connect(self._toggle_level)
        level_column.addWidget(self.level_enabled)
        level_row = QHBoxLayout()
        self.level_ac = QSpinBox()
        self.level_ac.setRange(0, 100)
        self.level_ac.setSuffix(" %")
        self.level_battery = QSpinBox()
        self.level_battery.setRange(0, 100)
        self.level_battery.setSuffix(" %")
        level_row.addWidget(QLabel("Plugged in"))
        level_row.addWidget(self.level_ac)
        level_row.addSpacing(16)
        level_row.addWidget(QLabel("On battery"))
        level_row.addWidget(self.level_battery)
        level_row.addStretch()
        level_column.addLayout(level_row)
        self.level_note = QLabel("")
        self.level_note.setWordWrap(True)
        level_column.addWidget(self.level_note)
        layout.addWidget(level)

        idle = QGroupBox("Dim when you are away")
        columns = QHBoxLayout(idle)
        self.idle_ac = IdleColumn("Plugged in")
        self.idle_battery = IdleColumn("On battery")
        columns.addWidget(self.idle_ac, 1)
        columns.addWidget(self.idle_battery, 1)
        layout.addWidget(idle)

        actions = QHBoxLayout()
        self.note = QLabel("")
        self.note.setWordWrap(True)
        actions.addWidget(self.note, 1)
        self.apply_button = QPushButton("Apply")
        self.apply_button.clicked.connect(self._apply)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        layout.addStretch()

    # -- the brightness-key service --------------------------------------

    @staticmethod
    def _systemctl(*args: str) -> tuple[int, str]:
        return _run(["systemctl", "--user", *args])

    def _load_keys(self) -> None:
        code, out = self._systemctl("is-enabled", KEYS_UNIT)
        known = out.splitlines()[0].strip() if out else ""
        # "not-found" is what systemd says for a unit that is not installed;
        # an empty answer means systemctl itself is missing or failed.
        if known in ("", "not-found", "systemctl not available"):
            self.keys_enabled.setEnabled(False)
            self.keys_note.setText(
                "Service not installed. Re-run install.sh to get it.")
            return
        self.keys_enabled.setEnabled(True)
        self._loading = True
        self.keys_enabled.setChecked(known == "enabled")
        self._loading = False
        active, _ = self._systemctl("is-active", KEYS_UNIT)
        self.keys_note.setText("running" if active == 0 else "stopped")

    def _toggle_keys(self, on: bool) -> None:
        if self._loading:
            return
        action = ["enable", "--now"] if on else ["disable", "--now"]
        code, out = self._systemctl(*action, KEYS_UNIT)
        if code != 0:
            self.keys_note.setText(f"failed: {out.splitlines()[0] if out else code}")
            return
        self.keys_note.setText("running" if on else "stopped")

    # -- the keyboard level per power source -----------------------------

    def _toggle_level(self, on: bool) -> None:
        for widget in (self.level_ac, self.level_battery):
            widget.setEnabled(on)

    def _load_level(self) -> None:
        levels = read_powerdevil_levels()
        self._levels_known = levels is not None
        if levels is None:
            for widget in (self.level_enabled, self.level_ac, self.level_battery):
                widget.setEnabled(False)
            self.level_note.setText(
                "Needs KDE's power management (kreadconfig6 / kwriteconfig6 "
                "not found). On another desktop, set the keyboard backlight "
                "per power source in its own power settings.")
            return
        self.level_enabled.setEnabled(True)
        self._loading = True
        self.level_enabled.setChecked(bool(levels.get("enabled")))
        self.level_ac.setValue(int(levels.get("ac", 100)))
        self.level_battery.setValue(int(levels.get("battery", 100)))
        self._loading = False
        self._toggle_level(self.level_enabled.isChecked())
        self.level_note.setText(
            "Written to KDE's power management, the same setting as "
            "System Settings → Power Management → Keyboard brightness.")

    def _save_level(self) -> str | None:
        if not self._levels_known:
            return None
        return write_powerdevil_levels(
            {"ac": self.level_ac.value(), "battery": self.level_battery.value()},
            self.level_enabled.isChecked())

    # -- idle dimming -----------------------------------------------------

    def load(self, idle: dict, available: bool = True) -> None:
        self._loading = True
        # The daemon answers per source; an older one answers with one flat
        # table, which then stands for both.
        if "ac" in idle or "battery" in idle:
            self.idle_ac.load(idle.get("ac") or {})
            self.idle_battery.load(idle.get("battery") or {})
        else:
            self.idle_ac.load(idle)
            self.idle_battery.load(idle)
        self._loading = False
        if not available:
            self.note.setText(
                "The daemon cannot read /dev/input, so it does not know when "
                "you are away: dimming stays off.")
        self._load_level()
        self._load_keys()

    def _apply(self) -> None:
        notes = []
        reply = self._send({"cmd": "config", "action": "save_settings", "idle": {
            "ac": self.idle_ac.values(),
            "battery": self.idle_battery.values(),
        }})
        if reply:
            notes.append(f"saved to {reply.get('file')}")
        error = self._save_level()
        if error:
            notes.append(f"keyboard level: {error}")
        elif self._levels_known:
            notes.append("keyboard levels saved to KDE")
        self.note.setText("; ".join(notes))


class MainWindow(QMainWindow):
    def __init__(self, socket_path: str):
        super().__init__()
        self.socket_path = socket_path
        self.effects: dict = {}
        self.layouts: dict = {}
        self.fast_write = True
        # Alerts, unlike the default profiles, only reach the daemon on Save.
        # Without a visible mark, an edited value looks live and is not.
        self._dirty = False
        self._loading_effect = False
        self.setWindowTitle("omen-fx — light bar and keyboard")
        self.resize(980, 760)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        column = QVBoxLayout(left)
        column.addWidget(QLabel("Alerts"))
        self.list = QListWidget()
        self.list.currentItemChanged.connect(self._select)
        column.addWidget(self.list)
        buttons = QHBoxLayout()
        for label, slot in (("New", self._add), ("Duplicate", self._duplicate),
                            ("Delete", self._remove)):
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        column.addLayout(buttons)
        splitter.addWidget(left)

        right = QWidget()
        column = QVBoxLayout(right)
        self.preview = SurfacePreview()
        column.addWidget(self.preview)
        self.editor = EffectEditor()
        self.editor.changed.connect(self._preview)
        self.editor.changed.connect(self._mark_dirty)
        column.addWidget(self.editor, 1)

        actions = QHBoxLayout()
        self.play_button = QPushButton("Play on the bar")
        self.play_button.clicked.connect(self._play_on_bar)
        self.save_button = QPushButton("Save")
        self.save_button.clicked.connect(self._save)
        self.revert_button = QPushButton("Re-read from the daemon")
        self.revert_button.clicked.connect(self.reload)
        actions.addWidget(self.play_button)
        actions.addStretch()
        actions.addWidget(self.revert_button)
        actions.addWidget(self.save_button)
        column.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes([230, 710])

        # Two different kinds of thing, so two tabs: what shows normally, and
        # what interrupts it.
        self.base_panel = BasePanel(self._send)
        # A zone's effect is an ordinary effect, so the same editor serves --
        # minus the layout picker, since a zone cannot contain a layout.
        self.layout_panel = LayoutPanel(
            self._send,
            lambda: EffectEditor(base_mode=True, allow_layout=False,
                                 zone_mode=True))
        self.layout_panel.changed.connect(self._layouts_changed)
        self.base_panel.before_save = self._ensure_layouts_saved
        self.tabs = QTabWidget()
        self.system_panel = SystemPanel(self._send)
        self.tabs.addTab(self.layout_panel, "Zones")
        self.tabs.addTab(self.base_panel, "Default")
        self.tabs.addTab(splitter, "Alerts")
        self.tabs.addTab(self.system_panel, "System")
        self.tabs.setCurrentIndex(1)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.setCentralWidget(self.tabs)
        self.statusBar().showMessage("ready")
        self.reload()

    def _tab_changed(self, index: int) -> None:
        # The preview deliberately survives the tab change. It stands in for the
        # default look, and alerts outrank it, so leaving it up while the alert
        # list is open shows exactly what the machine will do once saved --
        # whereas dropping it here made a half-finished default vanish from the
        # hardware just for looking at another tab.
        # Compared by widget, not by index: the tab order is a presentation
        # choice and has already changed once.
        current = self.tabs.widget(index)
        if current is not self.layout_panel:
            # Two priority-0 holds on the same surface would fight over it, and
            # which one won would look arbitrary. The zone tab keeps its preview
            # only while it is the tab you are looking at.
            self.layout_panel.stop_live()
        if current is self.base_panel and self.base_panel.live.isChecked():
            self.base_panel._push_live()

    def closeEvent(self, event) -> None:
        self.base_panel.stop_live()
        self.layout_panel.stop_live()
        super().closeEvent(event)

    # -- daemon -----------------------------------------------------------

    def _send(self, request: dict) -> dict | None:
        try:
            reply = send(request, self.socket_path)
        except ClientError as exc:
            self.statusBar().showMessage(f"daemon unreachable: {exc}")
            return None
        if "error" in reply:
            self.statusBar().showMessage(f"error: {reply['error']}")
            return None
        return reply

    def reload(self) -> None:
        status = self._send({"cmd": "status"})
        if status:
            self.fast_write = bool(status.get("status", {}).get("fast_write", True))
            # Match the daemon, so the preview blends the bar the same way.
            blend = status.get("status", {}).get("bar_blend")
            if blend:
                set_bar_blend(blend)
            if not status.get("status", {}).get("available"):
                self.statusBar().showMessage(
                    "the driver is not loaded: the preview works, the bar does not")
        reply = self._send({"cmd": "config", "action": "get"})
        if not reply:
            return
        self.effects = reply.get("effects", {})
        # `status` is None when the daemon is unreachable; the config reply
        # got through on its own socket call, so do not assume both worked.
        idle_status = ((status or {}).get("status") or {}).get("idle") or {}
        self.system_panel.load(reply.get("idle") or {},
                               available=bool(idle_status.get("available", True)))
        self.layouts = reply.get("layouts", {}) or {}
        self.layout_panel.load(self.layouts)
        self.base_panel.layouts = self.layouts
        # Before any profile is loaded: an editor whose picker is still empty
        # cannot represent "this surface is on a layout", and committing that
        # empty state would overwrite the choice with a plain effect.
        self._publish_layouts()
        # The unexpanded profiles: the editor must see `layout = "name"` to
        # show which layout is chosen, not the zones it was expanded into.
        self.base_panel.load(reply.get("base_raw") or reply.get("base", {}))
        current = self.list.currentItem().text() if self.list.currentItem() else None
        self.list.clear()
        for name in sorted(self.effects):
            self.list.addItem(QListWidgetItem(name))
        self._select_name(current or (sorted(self.effects)[0] if self.effects else None))
        self.statusBar().showMessage(
            f"{len(self.effects)} alerts from {reply.get('config')} · "
            f"defaults in {reply.get('base_file')} · "
            f"{len(self.layouts)} layouts")

    def _publish_layouts(self) -> None:
        """Offer the same layout names everywhere they can be chosen."""
        names = self.layout_panel.names()
        for editor in (self.editor, self.base_panel.editor):
            editor.set_layouts(names, self.layout_panel.layouts)

    def _layouts_changed(self) -> None:
        """A layout was edited: refresh every preview that may show it."""
        self.layouts = self.layout_panel.layouts
        self.base_panel.layouts = self.layouts
        self._publish_layouts()
        self.base_panel._preview()
        self._preview()

    def _ensure_layouts_saved(self) -> None:
        """Write pending layout edits before anything that refers to them.

        Saving an alert that names a layout the daemon has never seen would
        store a dangling reference, and the surface would simply stay dark.
        """
        if self.layout_panel.dirty:
            self.layout_panel.save()

    def _save(self) -> None:
        self._commit()
        self._ensure_layouts_saved()
        reply = self._send({"cmd": "config", "action": "save", "effects": self.effects})
        if reply:
            self._dirty = False
            self.save_button.setText("Save")
            self.statusBar().showMessage(
                f"saved {reply.get('saved')} effects to {reply.get('file')}")

    # -- list -------------------------------------------------------------

    def _commit(self) -> None:
        item = self.list.currentItem()
        if item:
            self.effects[item.text()] = self.editor.spec()

    def _mark_dirty(self) -> None:
        if self._loading_effect or self._dirty:
            return
        self._dirty = True
        self._refresh_dirty()

    def _refresh_dirty(self) -> None:
        self.save_button.setText("Save •" if self._dirty else "Save")
        self.statusBar().showMessage(
            "unsaved changes — alerts use the saved value until you press "
            "\u201cSave\u201d" if self._dirty else "saved")

    def _select(self, current, previous) -> None:
        if previous and previous.text() in self.effects:
            self.effects[previous.text()] = self.editor.spec()
        if current:
            self._loading_effect = True
            self.editor.load(self.effects.get(current.text(), dict(DEFAULTS)))
            self._loading_effect = False

    def _select_name(self, name: str | None) -> None:
        if not name:
            return
        for i in range(self.list.count()):
            if self.list.item(i).text() == name:
                self.list.setCurrentRow(i)
                return

    def _add(self) -> None:
        name, ok = QInputDialog.getText(self, "New effect", "Name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        self.effects[name] = dict(DEFAULTS)
        self.list.addItem(QListWidgetItem(name))
        self.list.sortItems()
        self._select_name(name)

    def _duplicate(self) -> None:
        item = self.list.currentItem()
        if not item:
            return
        name, ok = QInputDialog.getText(self, "Duplicate", "Name:",
                                        text=f"{item.text()}-2")
        if not ok or not name.strip():
            return
        self._commit()
        self.effects[name.strip()] = dict(self.effects[item.text()])
        self.list.addItem(QListWidgetItem(name.strip()))
        self.list.sortItems()
        self._select_name(name.strip())

    def _remove(self) -> None:
        item = self.list.currentItem()
        if not item:
            return
        if QMessageBox.question(self, "Delete", f"Delete \u201c{item.text()}\u201d?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.effects.pop(item.text(), None)
        self.list.takeItem(self.list.row(item))

    # -- preview ----------------------------------------------------------

    def _effect(self):
        try:
            return build_effect(self.editor.spec(), self.layouts)
        except ValueError as exc:
            self.statusBar().showMessage(str(exc))
            return None

    def _preview(self) -> None:
        effect = self._effect()
        self.editor.describe(effect, self.fast_write)
        self.preview.play(effect)

    def _play_on_bar(self) -> None:
        self._commit()
        item = self.list.currentItem()
        if not item:
            return
        reply = self._send({"cmd": "play", "spec": self.editor.spec(),
                            "key": f"gui-{item.text()}", "priority": 95})
        if reply:
            self.statusBar().showMessage(f"playing: {item.text()}")


def main(argv=None) -> int:
    app = QApplication(argv or sys.argv)
    window = MainWindow(SOCKET_PATH)
    window.show()
    return app.exec()
