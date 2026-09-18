"""The zone editor: paint regions of the keyboard and give each one an effect.

A layout is an ordered stack of zones, so the list on the left *is* the
priority: zone 1 is the background and everything below it paints on top. That
is why the list is reorderable and numbered rather than carrying a priority
field -- there is only one thing to understand, and it is visible.

The grid is the hardware: six rows of twenty lamps, exactly what the keyboard
has, plus the bar's four segments underneath.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QPainter
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSplitter, QVBoxLayout, QWidget,
)

from . import effects as fx
from . import layouts as lay
from .surface import bar_frame, preview_key_points

# Fallback colours, used only for the kinds that generate their own hues
# (rainbow, wave) and so have no single colour to show.
ZONE_TINTS = [
    "#4FA3FF", "#FF7A4F", "#5BD97E", "#C77DFF", "#FFD24F",
    "#4FE0D8", "#FF6FA5", "#9FE04F",
]

# Kinds whose colours come from the effect itself rather than from the spec.
GENERATED = ("rainbow", "wave")

# The grid's own frame, and the strip under it that holds the drag hint. Shared
# by _metrics and paintEvent so the two cannot disagree about where the strip
# begins -- they did, and the hint lost its descenders to the bottom edge.
MARGIN = 8
LABEL_H = 13


def tint(index: int) -> QColor:
    return QColor(ZONE_TINTS[index % len(ZONE_TINTS)])


def zone_color(zone: dict, index: int) -> QColor:
    """The colour that stands for a zone in the editor.

    Its own colour, not an arbitrary tint: the grid is a map of what the
    keyboard will look like, so a zone painted teal has to read as teal the
    moment you change it -- otherwise the editor and the hardware disagree and
    only the hardware is right.
    """
    if str(zone.get("kind", "")).lower() in GENERATED:
        return tint(index)
    palette = zone.get("colors") or []
    text = str(palette[0] if palette else zone.get("color", "")).strip()
    if not text:
        return tint(index)
    color = QColor("#" + text.lstrip("#"))
    return color if color.isValid() else tint(index)


class ZoneGrid(QWidget):
    """The paintable map of both surfaces.

    Drag with the left button to add cells to the selected zone, with the right
    button (or holding Ctrl) to take them away.
    """

    changed = pyqtSignal()

    GAP = 2

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(210)
        self.setMouseTracking(False)
        self._zones: list[dict] = []
        self._selected = -1
        self._painting = 0        # +1 adding, -1 erasing, 0 idle
        self._frame_keys: list = []
        self._frame_bar: list = []

    # -- state ------------------------------------------------------------

    def set_zones(self, zones: list[dict], selected: int) -> None:
        self._zones = zones
        self._selected = selected
        self.update()

    def set_frame(self, keys, bar) -> None:
        """Show the composed output instead of the zone map."""
        self._frame_keys, self._frame_bar = keys, bar
        self.update()

    # -- geometry ---------------------------------------------------------

    def _metrics(self):
        margin = MARGIN
        label_h = LABEL_H
        usable = self.height() - 2 * margin - label_h - 10
        keys_h = usable * 0.70
        bar_h = usable - keys_h
        width = self.width() - 2 * margin
        return (margin, width, keys_h, bar_h,
                width / lay.GRID_COLS, keys_h / lay.GRID_ROWS)

    def _cell_at(self, pos):
        """Which cell a point falls in: ("keys", r, c), ("bar", 0, i) or None."""
        margin, width, keys_h, bar_h, cw, ch = self._metrics()
        x, y = pos.x() - margin, pos.y() - margin
        if 0 <= x < width and 0 <= y < keys_h:
            return ("keys", int(y // ch), int(x // cw))
        top = keys_h + 10
        if 0 <= x < width and top <= y < top + bar_h:
            return ("bar", 0, min(lay.BAR_CELLS - 1,
                                  int(x // (width / lay.BAR_CELLS))))
        return None

    # -- painting the map -------------------------------------------------

    def _apply(self, pos) -> None:
        if self._selected < 0 or self._selected >= len(self._zones):
            return
        hit = self._cell_at(pos)
        if hit is None:
            return
        surface, r, c = hit
        zone = self._zones[self._selected]
        if surface == "keys":
            cells = set(lay.parse_mask(zone.get("mask")))
            before = len(cells)
            cells.add((r, c)) if self._painting > 0 else cells.discard((r, c))
            if len(cells) == before:
                return
            zone["mask"] = lay.dump_mask(cells)
        else:
            segs = set(lay.parse_bar(zone.get("bar")))
            before = len(segs)
            segs.add(c) if self._painting > 0 else segs.discard(c)
            if len(segs) == before:
                return
            zone["bar"] = lay.dump_bar(segs)
        self.update()
        self.changed.emit()

    def mousePressEvent(self, event) -> None:
        erase = (event.button() == Qt.MouseButton.RightButton
                 or event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        self._painting = -1 if erase else 1
        self._apply(event.position())

    def mouseMoveEvent(self, event) -> None:
        if self._painting:
            self._apply(event.position())

    def mouseReleaseEvent(self, _event) -> None:
        self._painting = 0

    # -- drawing ----------------------------------------------------------

    def _owner(self, surface: str, r: int, c: int) -> int:
        """Index of the topmost zone claiming a cell, or -1.

        Topmost, not first: this is the same last-wins rule the renderer uses,
        so the editor cannot disagree with the hardware about who owns a cell.
        """
        found = -1
        for i, zone in enumerate(self._zones):
            if surface == "keys":
                if (r, c) in lay.parse_mask(zone.get("mask")):
                    found = i
            elif c in lay.parse_bar(zone.get("bar")):
                found = i
        return found

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(18, 18, 20))
        margin, width, keys_h, bar_h, cw, ch = self._metrics()

        for r in range(lay.GRID_ROWS):
            for c in range(lay.GRID_COLS):
                owner = self._owner("keys", r, c)
                self._draw_cell(painter, margin + c * cw, margin + r * ch,
                                cw, ch, owner, r * lay.GRID_COLS + c,
                                self._frame_keys, 2)

        top = margin + keys_h + 10
        seg_w = width / lay.BAR_CELLS
        for i in range(lay.BAR_CELLS):
            owner = self._owner("bar", 0, i)
            self._draw_cell(painter, margin + i * seg_w, top, seg_w, bar_h,
                            owner, i, self._frame_bar, 5)

        painter.setPen(QColor(110, 110, 118))
        # Inside the band _metrics reserved for it, not flush against the
        # bottom edge -- drawn at height-13 the descenders were clipped off.
        painter.drawText(margin, int(self.height() - MARGIN - LABEL_H),
                         int(width), LABEL_H,
                         Qt.AlignmentFlag.AlignLeft,
                         "drag to assign \u00b7 right button or Ctrl to remove")

    def _draw_cell(self, painter, x, y, w, h, owner, index, frame, radius) -> None:
        selected = owner == self._selected and owner >= 0
        # The composed output, always: the grid is what the hardware will show.
        # The zone's own colour stands in only until the first frame arrives.
        if index < len(frame) and frame[index] is not None:
            r, g, b = frame[index]
            body = QColor(r, g, b)
        elif owner < 0:
            body = QColor(30, 30, 34)          # claimed by nobody: transparent
        else:
            body = zone_color(self._zones[owner], owner)
        painter.setBrush(body)
        if selected:
            # The selection is carried by the outline alone, so the fill can
            # stay truthful even for a very dark zone colour.
            pen = QColor(255, 255, 255)
        elif owner >= 0:
            pen = body.lighter(160)
        else:
            pen = QColor(44, 44, 50)
        painter.setPen(pen)
        painter.drawRoundedRect(int(x + self.GAP), int(y + self.GAP),
                                int(w - 2 * self.GAP), int(h - 2 * self.GAP),
                                radius, radius)


class LayoutPanel(QWidget):
    """The whole tab: layouts, their zones, and the effect inside each zone."""

    changed = pyqtSignal()

    def __init__(self, send, editor_factory):
        super().__init__()
        self._send = send
        self._loading = False
        self.layouts: dict = {}
        self._name: str | None = None
        # Whether there are edits the daemon has not been told about yet.
        self.dirty = False

        root = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Layout"))
        self.picker = QComboBox()
        self.picker.currentIndexChanged.connect(self._pick)
        top.addWidget(self.picker, 1)
        for text, slot in (("New", self._new), ("Duplicate", self._duplicate),
                           ("Rename", self._rename), ("Delete", self._delete)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            top.addWidget(button)
        root.addLayout(top)

        self.grid = ZoneGrid()
        self.grid.changed.connect(self._grid_changed)
        root.addWidget(self.grid)

        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        column = QVBoxLayout(left)
        column.addWidget(QLabel("Zones \u2014 the first is underneath, the "
                               "rest paint over it"))
        self.zones = QListWidget()
        self.zones.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.zones.currentRowChanged.connect(self._select_zone)
        column.addWidget(self.zones, 1)
        buttons = QHBoxLayout()
        for text, slot in (("+", self._add_zone), ("−", self._remove_zone),
                           ("▲", lambda: self._move(-1)),
                           ("▼", lambda: self._move(1))):
            button = QPushButton(text)
            button.setFixedWidth(34)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch()
        self.rename_zone = QPushButton("Rename zone")
        self.rename_zone.clicked.connect(self._rename_zone)
        buttons.addWidget(self.rename_zone)
        column.addLayout(buttons)

        hint = QLabel("A zone can take keys only, bar segments only, or "
                      "both: what you paint is what it lights.")
        hint.setWordWrap(True)
        column.addWidget(hint)

        split.addWidget(left)

        right = QWidget()
        column = QVBoxLayout(right)
        self.editor = editor_factory()
        self.editor.changed.connect(self._editor_changed)
        column.addWidget(self.editor, 1)
        split.addWidget(right)
        split.setSizes([300, 420])
        root.addWidget(split, 1)

        actions = QHBoxLayout()
        self.live = QCheckBox("Show live on the hardware")
        self.live.toggled.connect(self._live_toggled)
        self.save_button = QPushButton("Save the layouts")
        self.save_button.clicked.connect(self.save)
        actions.addWidget(self.live)
        actions.addStretch()
        actions.addWidget(self.save_button)
        root.addLayout(actions)

        self.note = QLabel()
        self.note.setWordWrap(True)
        root.addWidget(self.note)

        # Animates "Mostra i colori veri" and feeds the live preview.
        self._clock = 0.0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._advance)
        self._push_timer = QTimer(self)
        self._push_timer.setSingleShot(True)
        self._push_timer.setInterval(200)
        self._push_timer.timeout.connect(self._push_live)

    # -- data -------------------------------------------------------------

    @property
    def zone_list(self) -> list:
        if self._name is None:
            return []
        return self.layouts.get(self._name, {}).get("zones", [])

    @property
    def zone(self) -> dict | None:
        row = self.zones.currentRow()
        zones = self.zone_list
        return zones[row] if 0 <= row < len(zones) else None

    def load(self, layouts: dict) -> None:
        # "bar_source" is retired -- the bar is driven by the zones' own bar
        # cells now. Dropped on the way in so files written before the change
        # stop carrying it forward.
        self.layouts = {
            k: {**{kk: vv for kk, vv in v.items() if kk != "bar_source"},
                "zones": [dict(z) for z in v.get("zones", [])]}
            for k, v in (layouts or {}).items()}
        self._refresh_picker(self._name)

    def names(self) -> list[str]:
        return sorted(self.layouts)

    def _refresh_picker(self, keep: str | None) -> None:
        self._loading = True
        self.picker.clear()
        for name in self.names():
            self.picker.addItem(name)
        if keep and keep in self.layouts:
            self.picker.setCurrentText(keep)
        self._loading = False
        self._pick()

    # -- layout selection --------------------------------------------------

    def _pick(self, *_args) -> None:
        if self._loading:
            return
        self._name = self.picker.currentText() or None
        self._refresh_zones(0)

    def _new(self) -> None:
        name, ok = QInputDialog.getText(self, "New layout", "Name:")
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in self.layouts:
            QMessageBox.warning(self, "omen-fx", f"\u201c{name}\u201d already exists.")
            return
        self.layouts[name] = {
            "zones": [{"name": "sfondo", "kind": "solid", "color": "202060",
                       "mask": lay.dump_mask({(r, c) for r in range(lay.GRID_ROWS)
                                              for c in range(lay.GRID_COLS)}),
                       "bar": lay.dump_bar({0, 1, 2, 3})}],
        }
        self._refresh_picker(name)
        self._touch()

    def _duplicate(self) -> None:
        if self._name is None:
            return
        name, ok = QInputDialog.getText(self, "Duplicate layout", "New name:",
                                        text=f"{self._name}-2")
        name = (name or "").strip()
        if not ok or not name or name in self.layouts:
            return
        source = self.layouts[self._name]
        self.layouts[name] = {**source,
                              "zones": [dict(z) for z in source.get("zones", [])]}
        self._refresh_picker(name)
        self._touch()

    def _rename(self) -> None:
        if self._name is None:
            return
        name, ok = QInputDialog.getText(self, "Rename layout", "Name:",
                                        text=self._name)
        name = (name or "").strip()
        if not ok or not name or name == self._name or name in self.layouts:
            return
        self.layouts[name] = self.layouts.pop(self._name)
        self._refresh_picker(name)
        self._touch()

    def _delete(self) -> None:
        if self._name is None:
            return
        if QMessageBox.question(self, "omen-fx",
                                f"Delete the \u201c{self._name}\u201d layout?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.layouts.pop(self._name, None)
        self._refresh_picker(None)
        self._touch()

    # -- zones ------------------------------------------------------------

    def _refresh_zones(self, row: int) -> None:
        self._loading = True
        self.zones.clear()
        for i, zone in enumerate(self.zone_list):
            label = zone.get("name") or f"zone {i + 1}"
            item = QListWidgetItem(f"{i + 1}.  {label}   ({zone.get('kind', 'solid')})")
            swatch = zone_color(zone, i)
            # Keep it legible on the dark list background whatever the zone is.
            item.setForeground(swatch if swatch.lightness() > 90
                               else swatch.lighter(220))
            self.zones.addItem(item)
        self._loading = False
        if self.zones.count():
            self.zones.setCurrentRow(min(max(0, row), self.zones.count() - 1))
        else:
            self._select_zone(-1)

    def _select_zone(self, row: int) -> None:
        zone = self.zone
        self.grid.set_zones(self.zone_list, row)
        self.editor.setEnabled(zone is not None)
        if zone is None:
            self._refresh_preview()
            return
        self._loading = True
        self.editor.load({k: v for k, v in zone.items() if k not in lay.ZONE_KEYS})
        self._loading = False
        self._refresh_preview()

    def _add_zone(self) -> None:
        if self._name is None:
            QMessageBox.information(self, "omen-fx",
                                    "Make a layout first, with \u201cNew\u201d.")
            return
        zones = self.zone_list
        zones.append({"name": f"zone {len(zones) + 1}", "kind": "solid",
                      "color": "FF7A00", "mask": lay.dump_mask(set())})
        self._refresh_zones(len(zones) - 1)
        self._touch()
        self.note.setText("Zone added: draw it on the grid above. Being the "
                          "last one, it paints over all the others.")

    def _remove_zone(self) -> None:
        row = self.zones.currentRow()
        zones = self.zone_list
        if not (0 <= row < len(zones)):
            return
        zones.pop(row)
        self._refresh_zones(row - 1)
        self._touch()

    def _move(self, delta: int) -> None:
        row = self.zones.currentRow()
        zones = self.zone_list
        target = row + delta
        if not (0 <= row < len(zones)) or not (0 <= target < len(zones)):
            return
        zones[row], zones[target] = zones[target], zones[row]
        self._refresh_zones(target)
        self._touch()

    def _rename_zone(self) -> None:
        zone = self.zone
        if zone is None:
            return
        name, ok = QInputDialog.getText(self, "Rename zone", "Name:",
                                        text=str(zone.get("name", "")))
        if not ok:
            return
        zone["name"] = (name or "").strip()
        self._refresh_zones(self.zones.currentRow())
        self._touch()

    # -- edits ------------------------------------------------------------

    def _editor_changed(self) -> None:
        zone = self.zone
        if zone is None or self._loading:
            return
        keep = {k: zone[k] for k in lay.ZONE_KEYS if k in zone}
        zone.clear()
        zone.update(self.editor.spec())
        zone.update(keep)
        row = self.zones.currentRow()
        item = self.zones.item(row)
        if item is not None:
            label = zone.get("name") or f"zone {row + 1}"
            item.setText(f"{row + 1}.  {label}   ({zone.get('kind', 'solid')})")
        self._touch()

    def _grid_changed(self) -> None:
        self._touch()

    def _touch(self) -> None:
        self.dirty = True
        self.save_button.setText("Save the layouts \u2022")
        self.grid.set_zones(self.zone_list, self.zones.currentRow())
        self._refresh_preview()
        self.changed.emit()
        if self.live.isChecked():
            self._push_timer.start()

    # -- preview ----------------------------------------------------------

    def spec(self) -> dict | None:
        """The layout being edited, as a playable effect spec."""
        if self._name is None:
            return None
        spec = dict(self.layouts[self._name])
        spec["kind"] = "layout"
        return spec

    def _refresh_preview(self) -> None:
        spec = self.spec()
        if not spec or not spec.get("zones"):
            self._timer.stop()
            self.grid.set_frame([], [])
            return
        if not self._timer.isActive() and self.isVisible():
            self._timer.start(50)
        self._advance(step=False)

    def _advance(self, step: bool = True) -> None:
        spec = self.spec()
        if not spec:
            return
        if step:
            self._clock += 0.05
        try:
            effect = fx.build(spec)
        except ValueError:
            return
        self.grid.set_frame(effect.sample(preview_key_points(), self._clock),
                            bar_frame(effect, self._clock))

    # The animation only needs to run while the tab is on screen; stopping it
    # otherwise keeps an idle GUI from sampling effects twenty times a second.
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._refresh_preview()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    # -- hardware ---------------------------------------------------------

    LIVE_KEY = "gui-layout-live"

    def _live_toggled(self, on: bool) -> None:
        if on:
            self._push_live()
        else:
            self.stop_live()

    def stop_live(self) -> None:
        self._push_timer.stop()
        self._send({"cmd": "release", "key": self.LIVE_KEY})

    def _push_live(self) -> None:
        if not self.live.isChecked():
            return
        spec = self.spec()
        if not spec or not spec.get("zones"):
            return
        # A stand-in for the default look, not an alert: it must not wake a
        # surface the idle dim has put down.
        self._send({"cmd": "play", "spec": spec, "key": self.LIVE_KEY,
                    "target": "both", "priority": 0, "hold": True, "timeout": 0,
                    "stand_in": True})

    def save(self) -> None:
        reply = self._send({"cmd": "config", "action": "save_layouts",
                            "layouts": self.layouts})
        if reply:
            self.dirty = False
            self.save_button.setText("Save the layouts")
            self.note.setText(f"saved to {reply.get('file')} \u2014 "
                              f"{reply.get('saved')} layouts")
            self.changed.emit()
