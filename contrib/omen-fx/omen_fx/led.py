"""Sysfs backend for the OMEN 4-zone lighting.

On an OMEN MAX the four "keyboard zones" exposed by omen_rgb_keyboard actually
drive the 4-segment light bar, which is exactly what we want to animate.

Driver quirks this module hides:

  * writing a zone (or ``all``) implicitly stops the kernel animation and
    switches the mode to ``static``;
  * every colour written is scaled by ``brightness`` before it reaches the
    hardware, and reading a zone gives back the *scaled* value -- so a naive
    save/restore cycle would darken the bar a little more every time;
  * while an animation is running the zone files report animation frames, not
    the user's base colours. Writing ``static`` first makes the driver restore
    the base colours to the hardware, and only then is a read meaningful.

``brightness`` is *not* ours to write. The driver exports it a second time as
the standard LED class device ``omen::kbd_backlight``, which is what the
desktop's "keyboard backlight" slider moves -- so it belongs to the user, and
the daemon only reads it (see ``LightBar.master``). An effect's own level is
applied here in software instead, and the two multiply: the driver scales
everything we write to the zones by the master on its way to the hardware.
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("omen-fx.led")

DEFAULT_SYSFS = "/sys/devices/platform/omen-rgb-keyboard/rgb_zones"
ZONE_COUNT = 4
MASTER_POLL_S = 0.1     # the render loop asks 30x a second; a slider moves at human speed

KERNEL_MODES = (
    "static", "breathing", "rainbow", "wave", "pulse", "chase",
    "sparkle", "candle", "aurora", "disco", "gradient",
)

Color = tuple[int, int, int]
Frame = tuple[Color, Color, Color, Color]

BLACK: Color = (0, 0, 0)


def parse_color(value) -> Color:
    """Accept ``"#ff0000"``, ``"ff0000"``, ``"f00"`` or an ``(r, g, b)`` tuple."""
    if isinstance(value, (tuple, list)):
        if len(value) != 3:
            raise ValueError(f"expected 3 components, got {value!r}")
        return tuple(max(0, min(255, int(c))) for c in value)  # type: ignore[return-value]

    text = str(value).strip().lstrip("#")
    if len(text) == 3:
        text = "".join(c * 2 for c in text)
    if len(text) != 6:
        raise ValueError(f"invalid colour: {value!r}")
    n = int(text, 16)
    return ((n >> 16) & 0xFF, (n >> 8) & 0xFF, n & 0xFF)


def format_color(color: Color) -> str:
    return "%02X%02X%02X" % color


def scale(color: Color, factor: float) -> Color:
    return tuple(max(0, min(255, round(c * factor))) for c in color)  # type: ignore[return-value]


def blend(a: Color, b: Color, t: float) -> Color:
    t = max(0.0, min(1.0, t))
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))  # type: ignore[return-value]


def uniform(color: Color) -> Frame:
    return (color, color, color, color)


class Snapshot:
    """The lighting state to go back to once an alert is over."""

    __slots__ = ("zones", "brightness", "mode", "speed")

    def __init__(self, zones, brightness: int, mode: str, speed: int):
        self.zones: Frame = tuple(zones)  # type: ignore[assignment]
        self.brightness = brightness
        self.mode = mode
        self.speed = speed

    def to_dict(self) -> dict:
        return {
            "zones": [format_color(c) for c in self.zones],
            "brightness": self.brightness,
            "mode": self.mode,
            "speed": self.speed,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Snapshot":
        zones = [parse_color(c) for c in data.get("zones", ["000000"] * ZONE_COUNT)]
        while len(zones) < ZONE_COUNT:
            zones.append(BLACK)
        return cls(
            zones[:ZONE_COUNT],
            int(data.get("brightness", 100)),
            str(data.get("mode", "static")),
            int(data.get("speed", 5)),
        )

    def __repr__(self) -> str:
        return f"Snapshot({self.to_dict()})"


class DeviceUnavailable(RuntimeError):
    pass


class LightBar:
    """Writes frames to the light bar, skipping zones that did not change."""

    def __init__(self, sysfs: str = DEFAULT_SYSFS, dry_run: bool = False):
        self.sysfs = sysfs
        self.dry_run = dry_run
        self._last: Frame | None = None       # what the caller asked for
        self._last_out: Frame | None = None    # what actually went to the hardware
        self._brightness = 100                 # the effect's level, applied in software
        self._master: int | None = None        # the user's slider, cached
        self._master_read = 0.0
        self._has_zones: bool | None = None   # driver with the "zones" attribute

    # -- low level -------------------------------------------------------

    def _path(self, name: str) -> str:
        return os.path.join(self.sysfs, name)

    @property
    def available(self) -> bool:
        return self.dry_run or os.path.isdir(self.sysfs)

    @property
    def has_zones(self) -> bool:
        """Whether the driver can set all four zones in one WMI round trip.

        Without it each zone is a separate get/set pair, so a four-colour frame
        costs roughly four times as much and animation is visibly sluggish.
        """
        if self._has_zones is None:
            self._has_zones = self.dry_run or os.path.exists(self._path("zones"))
        return self._has_zones

    def _read(self, name: str) -> str:
        if self.dry_run:
            return {"brightness": "100", "animation_mode": "static",
                    "animation_speed": "5"}.get(name, "#000000")
        try:
            with open(self._path(name), "r") as fh:
                return fh.read().strip()
        except OSError as exc:
            raise DeviceUnavailable(f"cannot read {name}: {exc}") from exc

    def _write(self, name: str, value: str) -> None:
        if self.dry_run:
            log.debug("dry-run: %s <- %s", name, value)
            return
        try:
            with open(self._path(name), "w") as fh:
                fh.write(value)
        except OSError as exc:
            raise DeviceUnavailable(f"cannot write {name}: {exc}") from exc

    # -- state -----------------------------------------------------------

    def master(self) -> int:
        """The user's own brightness level, 0-100.

        This is the driver's ``brightness``, which is the same number the
        desktop slider writes through ``/sys/class/leds/omen::kbd_backlight``.
        Read only, and cached for MASTER_POLL_S: the render loop wants it every
        tick, but it only ever changes when someone drags the slider.
        """
        now = time.monotonic()
        if self._master is not None and now < self._master_read + MASTER_POLL_S:
            return self._master
        self._master_read = now
        try:
            value = int(self._read("brightness"))
        except (ValueError, DeviceUnavailable):
            # The bar may simply not be there; a missing slider means "full".
            value = 100 if self._master is None else self._master
        self._master = max(0, min(100, value))
        return self._master

    def capture(self) -> Snapshot:
        """Read back the current lighting so it can be restored later."""
        mode = self._read("animation_mode") or "static"
        try:
            speed = int(self._read("animation_speed"))
        except ValueError:
            speed = 5
        try:
            brightness = int(self._read("brightness"))
        except ValueError:
            brightness = 100

        # An animation is painting the zones; ask the driver to put the user's
        # base colours back on the hardware before reading them.
        if mode != "static":
            self._write("animation_mode", "static")

        if self.has_zones:
            raw = self._read("zones").split()
        else:
            raw = [self._read(f"zone{i:02d}") for i in range(ZONE_COUNT)]

        zones = []
        for text in raw[:ZONE_COUNT]:
            color = parse_color(text)
            if brightness >= 5:
                # Undo the brightness scaling the driver applied on write.
                color = scale(color, 100.0 / brightness)
            zones.append(color)
        while len(zones) < ZONE_COUNT:
            zones.append(BLACK)

        self._last = tuple(zones)  # type: ignore[assignment]
        self._last_out = None
        return Snapshot(zones, brightness, mode, speed)

    def restore(self, snap: Snapshot) -> None:
        """Put the bar back exactly as ``capture`` found it.

        ``snap.brightness`` is deliberately not written back: it is the user's
        master, the snapshot's colours are already stored unscaled, and the
        driver applies whatever the slider says now as they go out.
        """
        self.set_brightness(100, force=True)
        self.set_frame(snap.zones, force=True)
        self.set_speed(snap.speed)
        self.set_mode(snap.mode)
        # A kernel animation now owns the zones; forget our diffing cache.
        if snap.mode != "static":
            self._last = None
            self._last_out = None

    # -- output ----------------------------------------------------------

    def set_frame(self, frame, force: bool = False) -> None:
        frame = tuple(frame)
        # Diffing on the *scaled* frame, not the requested one: a level change
        # with the same colours still has to reach the hardware.
        out = frame if self._brightness == 100 else tuple(
            scale(c, self._brightness / 100.0) for c in frame)
        if not force and self._last_out == out:
            self._last = frame  # type: ignore[assignment]
            return

        changed = [i for i in range(ZONE_COUNT)
                   if force or self._last_out is None or self._last_out[i] != out[i]]

        if self.has_zones and len(changed) > 1:
            # One round trip for the whole bar, whatever changed.
            self._write("zones", " ".join(format_color(c) for c in out))
        elif len(changed) == ZONE_COUNT and len(set(out)) == 1:
            self._write("all", format_color(out[0]))
        else:
            for i in changed:
                self._write(f"zone{i:02d}", format_color(out[i]))
        self._last = frame  # type: ignore[assignment]
        self._last_out = out  # type: ignore[assignment]

    def set_brightness(self, value: int, force: bool = False) -> None:
        """The *effect's* level, in software.

        Not a write to the driver's ``brightness``: that file is the user's
        slider, and an effect setting its own level there would overwrite the
        user's setting and make the desktop slider jump to it.
        """
        self._brightness = max(0, min(100, int(value)))

    def set_speed(self, value: int) -> None:
        self._write("animation_speed", str(max(1, min(10, int(value)))))

    def set_mode(self, mode: str) -> None:
        mode = mode.strip().lower()
        if mode not in KERNEL_MODES:
            raise ValueError(f"unknown animation mode: {mode}")
        self._write("animation_mode", mode)
        if mode != "static":
            self._last = None
            self._last_out = None
