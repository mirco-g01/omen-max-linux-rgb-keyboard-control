#!/usr/bin/env python3
"""Render the GUI's four tabs to PNGs for the README.

Offscreen and against canned data, not against the running daemon, for two
reasons: it needs no display, and the shots then show the effects this project
actually ships rather than whatever the machine it ran on happened to have.

    python3 make-screenshots.py ../../docs
"""

from __future__ import annotations

import os
import sys
import time
import tomllib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QColor, QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication, QSizePolicy  # noqa: E402

from omen_fx import layouts as lay  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
WIDTH = 1420


def cells(*spans):
    """(row, col_from, col_to) triples -> the set of grid cells they cover."""
    out = set()
    for row, first, last in spans:
        out.update((row, col) for col in range(first, last + 1))
    return out


ALL_CELLS = {(r, c) for r in range(lay.GRID_ROWS) for c in range(lay.GRID_COLS)}

DEMO_LAYOUT = {
    "bar_source": "projection",
    "zones": [
        {"name": "backdrop", "kind": "gradient", "colors": ["06103A", "2A0A60"],
         "cycles": 0, "mask": lay.dump_mask(ALL_CELLS), "bar": "1111"},
        {"name": "function row", "kind": "wave", "speed": 2, "spread": 1.0,
         "cycles": 0, "mask": lay.dump_mask(cells((0, 0, 19))), "bar": "0000"},
        {"name": "WASD", "kind": "solid", "color": "FF6A00",
         "mask": lay.dump_mask(cells((2, 3, 3), (3, 2, 4))), "bar": "0000"},
        {"name": "arrows", "kind": "breathe", "color": "00E0FF", "period_ms": 2600,
         "cycles": 0, "mask": lay.dump_mask(cells((4, 17, 17), (5, 16, 18))),
         "bar": "0000"},
    ],
}

LAYOUTS = {"gaming": DEMO_LAYOUT}
BASE_RAW = {"keys": {"kind": "layout", "layout": "gaming", "brightness": 100},
            "bar": {"kind": "layout", "layout": "gaming"}}
IDLE = {"enabled": True, "timeout": 90, "brightness": 15, "fade_ms": 1500}

with open(os.path.join(HERE, "config.toml"), "rb") as fh:
    EFFECTS = tomllib.load(fh).get("effects", {})


def fake_send(request: dict):
    if request.get("cmd") == "status":
        return {"status": {
            "device": "/sys/devices/platform/omen-rgb-keyboard/rgb_zones",
            "keyboard": "/dev/hidraw5", "available": True,
            "keyboard_available": True, "keyboard_held": True, "lamps": 120,
            "dry_run": False, "fast_write": True, "bar_blend": 1.5,
            "active": {"bar": None, "keys": None}, "queued": [],
            "fading": {"bar": None, "keys": None},
            "base": {"bar": "layout", "keys": "layout"},
            "bar_base": None, "last_error": None, "master_brightness": 70,
            "idle": {"enabled": True, "available": True, "seconds": 4.0, "level": 100},
        }}
    if request.get("cmd") == "config" and request.get("action", "get") == "get":
        return {"ok": True, "effects": EFFECTS, "layouts": LAYOUTS,
                "config": "/etc/omen-fx/config.toml",
                "effects_file": "/etc/omen-fx/effects.toml",
                "base_file": "/etc/omen-fx/base.toml",
                "layouts_file": "/etc/omen-fx/layouts.toml",
                "settings_file": "/etc/omen-fx/settings.toml",
                "base_raw": BASE_RAW, "base": BASE_RAW, "idle": IDLE}
    return {"ok": True, "file": "/etc/omen-fx/settings.toml"}


def dark(app: QApplication) -> None:
    """A dark palette, because the offscreen platform has no theme of its own.

    Most people run these panels on a dark desktop, and the lit surfaces the
    preview draws only read as lit against a dark ground.
    """
    app.setStyle("Fusion")
    p = QPalette()
    base, alt, text = QColor(30, 31, 36), QColor(38, 40, 46), QColor(226, 228, 233)
    p.setColor(QPalette.ColorRole.Window, base)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, QColor(24, 25, 29))
    p.setColor(QPalette.ColorRole.AlternateBase, alt)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, alt)
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.ToolTipBase, alt)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.Highlight, QColor(255, 106, 0))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor(20, 20, 20))
    p.setColor(QPalette.ColorRole.PlaceholderText, QColor(140, 143, 150))
    for group in (QPalette.ColorGroup.Disabled,):
        p.setColor(group, QPalette.ColorRole.Text, QColor(120, 122, 128))
        p.setColor(group, QPalette.ColorRole.ButtonText, QColor(120, 122, 128))
        p.setColor(group, QPalette.ColorRole.WindowText, QColor(120, 122, 128))
    app.setPalette(p)


def settle(app: QApplication, seconds: float = 0.25) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def freeze(preview, t: float) -> None:
    """Hold the preview at one moment of the effect.

    Waiting for the animation to reach a good frame by wall clock is a lottery
    -- an alert that ends dark photographs as a black rectangle -- so the shot
    picks its own phase instead.
    """
    preview._timer.stop()
    preview._t = t
    preview._sample()


def fit_to_tab(window, app) -> None:
    """Shrink the window to the tab being shown.

    A QTabWidget asks for the tallest page it holds, so every shot would
    otherwise be as tall as the alert editor -- leaving the System tab, which
    needs a third of that, floating in a field of empty grey. Marking the other
    pages Ignored makes the stack follow the current one instead.
    """
    tabs = window.tabs
    for i in range(tabs.count()):
        page = tabs.widget(i)
        policy = (QSizePolicy.Policy.Preferred if page is tabs.currentWidget()
                  else QSizePolicy.Policy.Ignored)
        page.setSizePolicy(policy, policy)
    # The layout has to recompute with the new policies before the window is
    # asked how small it can be; without this it answers with the old page's
    # height and nothing shrinks.
    settle(app, 0.05)
    # minimumSizeHint, not sizeHint: the tab stack's own hint lags a page
    # behind the policy change, while the minimum follows it at once. Plus a
    # little room so nothing sits squeezed against the frame.
    # Twice: the very first fit runs before the policies have ever been
    # applied, and one pass then still answers with the tallest page's height.
    for _ in range(2):
        window.resize(WIDTH, window.minimumSizeHint().height() + 30)
        settle(app, 0.05)


def shoot(window, app, name: str) -> None:
    fit_to_tab(window, app)
    settle(app)
    shot = window.grab()
    path = os.path.join(OUT, name)
    shot.save(path)
    print("wrote", path, f"{shot.width()}x{shot.height()}")


def main() -> int:
    app = QApplication([])
    dark(app)

    from omen_fx import gui as gui_mod
    # Patched on the class, not the instance: the panels are handed
    # ``self._send`` while MainWindow is still building itself, so an instance
    # attribute set afterwards would leave them talking to the real daemon --
    # and the shots would then show whatever that machine happens to have.
    gui_mod.MainWindow._send = staticmethod(fake_send)
    window = gui_mod.MainWindow("/run/omen-fx/control.sock")
    window.reload()
    window.resize(WIDTH, 1000)
    window.show()

    tabs = {window.tabs.tabText(i): i for i in range(window.tabs.count())}

    window.tabs.setCurrentIndex(tabs["Default"])
    window.base_panel.surface.setCurrentIndex(0)          # the keyboard
    settle(app)
    freeze(window.base_panel.preview, 0.85)
    shoot(window, app, "gui-default.png")

    window.tabs.setCurrentIndex(tabs["Zones"])
    panel = window.layout_panel
    if panel.picker.count():
        panel.picker.setCurrentIndex(0)
    if panel.zones.count() > 2:
        panel.zones.setCurrentRow(2)                      # the WASD zone
    shoot(window, app, "gui-zones.png")

    window.tabs.setCurrentIndex(tabs["Alerts"])
    window._select_name("notify-urgent")
    settle(app)
    freeze(window.preview, 0.30)                          # mid-flash, not the dark tail
    shoot(window, app, "gui-alerts.png")

    window.tabs.setCurrentIndex(tabs["System"])
    shoot(window, app, "gui-system.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
