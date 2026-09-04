"""The two lit surfaces of an OMEN MAX, in one shared coordinate space.

The keyboard reports every lamp's real position in millimetres, and the light
bar sits along the front edge of the same chassis. Placing both in one
normalised space -- ``u`` across the machine, ``v`` from the function-key row
down past the bar -- is what lets a single effect sweep from the top of the
keyboard onto the bar as one continuous surface.

    v=0.07  Esc F1 F2 ... F12 Del                 (keyboard, 6 rows)
    v=0.79  Ctrl Fn Win Alt ---- Space ----
    v=0.94  [ zone0 ][ zone1 ][ zone2 ][ zone3 ]  (light bar)
    u=0                                        u=1
"""

from __future__ import annotations

import math

import logging

from .keys import Keyboard, KeyboardUnavailable, synthetic_lamps
from .led import DeviceUnavailable, LightBar, ZONE_COUNT

log = logging.getLogger("omen-fx.surface")

Color = tuple[int, int, int]
Point = tuple[float, float]

# The chassis bounding box the keyboard firmware reports, in millimetres. Both
# surfaces are normalised against it so their coordinates are comparable.
CHASSIS_W = 342.0
CHASSIS_H = 125.0

# Where the four bar segments sit; the bar lives below the bottom key row.
BAR_Y_MM = 118.0

# Both surfaces are the same width, so the keyboard is stretched to span
# exactly [0, 1] across it and the bar's four segments partition that same
# range into quarters.
def _spread(values: list[float]) -> list[float]:
    """Stretch coordinates so the extremes land on 0 and 1."""
    low, high = min(values), max(values)
    span = high - low
    if span <= 0:
        return [0.5] * len(values)
    return [(v - low) / span for v in values]


# A bar segment is an *area*, a quarter of the machine's width, not a point --
# and its light does not stop at the edge of that quarter. Two earlier attempts
# were both wrong in an instructive way:
#
#   one sample per segment    the segment *was* whichever point it sat on, so
#                             it held its brightness until that one key did
#   a box average of its own  no segment knew anything happening outside its
#   quarter                   quarter, so every boundary was a hard step
#
# What a diffused strip actually does is weigh its surroundings: brightest at
# its middle, fading with distance, overlapping its neighbours. So each segment
# is a *weighted* average across the whole width, through a raised cosine
# centred on it and one segment wide either side. Neighbouring windows overlap
# by half, which is what removes the step: a front crossing a boundary is
# already showing on the next segment before it gets there, and still showing
# on the last one after it leaves.
#
# The window is a partition of unity at this spacing -- the weights of the two
# nearest segments sum to exactly 1 at every position -- so the bar reproduces
# the effect's overall level instead of dimming or blooming.
# How wide that window is decides a trade-off with no free lunch in it. Wider
# means the segments hand over sooner and more gently; narrower keeps more
# contrast between them. Measured on a center_out front, as a fraction of a
# segment either side of centre:
#
#   width   outer segment lags inner by   contrast kept
#   box                    12.0 pts           159/255
#   1.0                     9.0                143
#   1.25                    5.8                123
#   1.5                     2.2                106
#   2.0                     0.0                 71
#
# 1.5 is the default: the lag that reads as a step is essentially gone while
# two thirds of the contrast survives. It is a matter of taste and of how much
# the physical bar diffuses, so daemon.bar_blend in config.toml can move it.
BAR_SUBSAMPLES = 10                       # per segment
BAR_SAMPLES = ZONE_COUNT * BAR_SUBSAMPLES
BAR_BLEND_DEFAULT = 1.5                   # window half-width, in segments

BAR_POINTS: list[Point] = [
    ((j + 0.5) / BAR_SAMPLES, BAR_Y_MM / CHASSIS_H) for j in range(BAR_SAMPLES)
]


def _bar_weights(blend: float) -> list[list[float]]:
    """Raised-cosine weight of every sub-sample for every segment."""
    half = max(0.5, float(blend)) / ZONE_COUNT
    table = []
    for i in range(ZONE_COUNT):
        centre = (i + 0.5) / ZONE_COUNT
        table.append([
            0.0 if abs(u - centre) >= half
            else 0.5 * (1.0 + math.cos(math.pi * abs(u - centre) / half))
            for u, _v in BAR_POINTS
        ])
    return table


BAR_BLEND = BAR_BLEND_DEFAULT
BAR_WEIGHTS = _bar_weights(BAR_BLEND)


def set_bar_blend(segments: float) -> None:
    """Widen or narrow the window each bar segment averages over."""
    global BAR_BLEND, BAR_WEIGHTS
    BAR_BLEND = max(0.5, min(4.0, float(segments)))
    BAR_WEIGHTS = _bar_weights(BAR_BLEND)
    log.debug("bar blend window: +/-%.2f segments", BAR_BLEND)


def average_bar(raw: list, blend: bool = True) -> list:
    """Collapse the sub-samples into one colour per segment.

    A segment none of whose own sub-samples are lit stays dark, whatever its
    neighbours are doing -- otherwise the overlapping windows would spill light
    into segments nothing is playing on.

    ``blend=False`` keeps each segment strictly to its own quarter. That is for
    zone layouts: there a boundary between segments is a line the user drew, so
    two zones meeting at one must stay two colours, not fade into each other.
    """
    out: list = []
    for i in range(ZONE_COUNT):
        lo, hi = i * BAR_SUBSAMPLES, (i + 1) * BAR_SUBSAMPLES
        if all(c is None for c in raw[lo:hi]):
            out.append(None)
            continue
        weights = BAR_WEIGHTS[i]
        total = 0.0
        acc = [0.0, 0.0, 0.0]
        span = range(len(raw)) if blend else range(lo, min(hi, len(raw)))
        for j in span:
            color = raw[j]
            if color is None:
                continue
            w = weights[j] if blend else 1.0
            if w <= 0.0:
                continue
            total += w
            acc[0] += color[0] * w
            acc[1] += color[1] * w
            acc[2] += color[2] * w
        if total <= 0.0:
            out.append(None)
            continue
        out.append((clamp8(acc[0] / total), clamp8(acc[1] / total),
                    clamp8(acc[2] / total)))
    return out


def bar_frame(effect, t: float) -> list:
    """One colour per bar segment, blended unless the effect asks for edges."""
    return average_bar(effect.sample(BAR_POINTS, t),
                       blend=not getattr(effect, "crisp_bar", False))


def clamp8(value: float) -> int:
    return 0 if value < 0 else (255 if value > 255 else int(value))


def scale_color(color: Color, factor: float) -> Color:
    return (clamp8(color[0] * factor), clamp8(color[1] * factor), clamp8(color[2] * factor))


class Surface:
    """A set of addressable points plus a way to push colours to them."""

    name = "surface"

    def __init__(self):
        self.points: list[Point] = []
        self._brightness = 100
        self._master = 100
        self._idle = 100

    def __len__(self) -> int:
        return len(self.points)

    @property
    def available(self) -> bool:
        raise NotImplementedError

    def sample(self, effect, t: float) -> list:
        """Read an effect into one colour per addressable element.

        A hook rather than a plain call, because the two surfaces read the same
        effect differently: a lamp is a point, a bar segment is an area.
        """
        return effect.sample(self.points, t)

    def write(self, colors: list[Color], force: bool = False) -> bool:
        """Push a frame; return True if the hardware was actually touched."""
        raise NotImplementedError

    def set_brightness(self, value: int, force: bool = False) -> None:
        self._brightness = max(0, min(100, int(value)))

    def set_master(self, value: int) -> None:
        """The user's keyboard-backlight slider, on top of the effect's level.

        Two independent things: an effect says how bright it wants to be
        relative to the machine's setting, the user says how bright the machine
        is. They multiply, so dimming the slider dims an alert too.
        """
        self._master = max(0, min(100, int(value)))

    def set_idle(self, value: float) -> None:
        """How far the idle dim has come, 100 awake down to its floor asleep.

        A third, separate factor on purpose: dimming here never writes the
        user's slider, so the machine wakes back to the level its owner chose
        and the desktop's own reading of that level never goes stale.
        """
        self._idle = max(0.0, min(100.0, float(value)))

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def master(self) -> int:
        return self._master

    @property
    def idle(self) -> float:
        return self._idle

    @property
    def factor(self) -> float:
        return (self._brightness * self._master * self._idle) / 1e6


class KeySurface(Surface):
    """The 120-lamp per-key backlight."""

    name = "keys"

    def __init__(self, keyboard: Keyboard):
        super().__init__()
        self.kb = keyboard
        self._geometry_loaded = False

    def open(self) -> None:
        self.kb.open()
        _w, h = self.kb.bounds
        h = h or CHASSIS_H
        # Stretched across the width for the same reason the bar is: the two
        # surfaces have to agree on where "the left edge" is. Height is left
        # physical -- the bar really does sit below the keys.
        us = _spread([l.x for l in self.kb.lamps])
        self.points = [(u, l.y / h) for u, l in zip(us, self.kb.lamps)]
        self._geometry_loaded = True

    @property
    def available(self) -> bool:
        return self.kb.available

    def reattach(self) -> bool:
        """Follow the LampArray to a new device node, geometry and all."""
        if not self.kb.reattach():
            return False
        self.open()
        return True

    @property
    def min_interval_ms(self) -> float:
        return self.kb.min_interval_ms

    def write(self, colors: list[Color], force: bool = False) -> bool:
        # The keyboard has no brightness register of its own, so both the
        # effect's level and the user's slider are applied in software here.
        # On the bar the driver applies the slider itself, so only the effect's
        # level is left to userspace -- see BarSurface.write.
        factor = self.factor
        if factor != 1.0:
            colors = [scale_color(c, factor) for c in colors]
        return self.kb.set_frame(colors, force=force)

    def release(self) -> None:
        self.kb.release()

    def lamps_for_key(self, usage: int) -> list[int]:
        return self.kb.lamps_for_key(usage)


class BarSurface(Surface):
    """The 4-segment light bar, driven through the omen_rgb_keyboard sysfs."""

    name = "bar"

    def __init__(self, bar: LightBar):
        super().__init__()
        self.bar = bar
        self.points = list(BAR_POINTS)

    @property
    def available(self) -> bool:
        return self.bar.available

    def __len__(self) -> int:
        return ZONE_COUNT       # four segments, however many samples feed them

    def sample(self, effect, t: float) -> list:
        return average_bar(effect.sample(self.points, t),
                           blend=not getattr(effect, "crisp_bar", False))

    def write(self, colors: list[Color], force: bool = False) -> bool:
        # No master here: the driver scales every colour written to the zones
        # by the very same slider value on its way to the hardware, so applying
        # it again would square it. The idle dim *is* ours, though -- the driver
        # knows nothing about it -- so it rides along with the effect's level.
        self.bar.set_brightness(round(self._brightness * self._idle / 100.0))
        frame = tuple(colors[:ZONE_COUNT])
        if len(frame) < ZONE_COUNT:
            frame += ((0, 0, 0),) * (ZONE_COUNT - len(frame))
        before = self.bar._last
        self.bar.set_frame(frame, force=force)
        return force or before != frame

    def set_brightness(self, value: int, force: bool = False) -> None:
        # Recorded only; write() is what hands the combined level to the bar,
        # so a change of idle level alone still reaches the hardware.
        super().set_brightness(value)


def preview_key_points() -> list[Point]:
    """Keyboard geometry for previews, without needing the hardware present."""
    lamps = synthetic_lamps()
    us = _spread([l.x for l in lamps])
    return [(u, l.y / CHASSIS_H) for u, l in zip(us, lamps)]


def build_surfaces(sysfs: str, dry_run: bool = False):
    """Open both surfaces, tolerating either one being absent."""
    bar = BarSurface(LightBar(sysfs, dry_run=dry_run))
    keys = KeySurface(Keyboard(dry_run=dry_run))
    try:
        keys.open()
    except KeyboardUnavailable as exc:
        log.warning("per-key keyboard unavailable: %s", exc)
    if not bar.available:
        log.warning("light bar unavailable at %s", sysfs)
    return bar, keys
