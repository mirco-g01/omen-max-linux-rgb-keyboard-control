"""Effects as continuous functions of space and time.

An effect is ``(u, v, t) -> colour``, sampled at whatever points a surface
happens to have. That is the whole reason both the 4-zone bar and the 120-lamp
keyboard can run the same effect: the bar simply samples it at four places.

Coordinates are the shared chassis space from ``surface.py``: ``u`` runs left
to right, ``v`` from the function-key row down to the light bar, both in [0, 1].

Speed keeps the meaning it had when this was bar-only -- 1 (slow) to 10 (fast),
where the number sets how long a wavefront takes to cross **one bar zone**, a
quarter of the machine's width. Effects therefore look the same on the bar as
they did before, and the keyboard just resolves them far more finely.
"""

from __future__ import annotations

import math
import random

from .led import BLACK, Color, blend, parse_color, scale

# Width of one bar zone in u units; the historical unit of speed.
ZONE_U = 0.25
_ZONE_S_AT_5 = 0.36     # _zone_seconds() at the default speed of 5


def _color(spec: dict, key: str = "color", default="FFFFFF") -> Color:
    return parse_color(spec.get(key, default))


def _colors(spec: dict) -> list[Color]:
    raw = spec.get("colors")
    if raw:
        return [parse_color(c) for c in raw]
    return [_color(spec)]


def _f(spec: dict, key: str, default: float) -> float:
    try:
        return float(spec.get(key, default))
    except (TypeError, ValueError):
        return default


def _i(spec: dict, key: str, default: int) -> int:
    try:
        return int(spec.get(key, default))
    except (TypeError, ValueError):
        return default


def _zone_seconds(spec: dict) -> float:
    """Seconds for a wavefront to cross one bar zone."""
    speed = max(1, min(10, _i(spec, "speed", 5)))
    return (560 - (speed - 1) * 50) / 1000.0


def _rate(spec: dict, key: str, default_ms: float) -> float:
    """Seconds for one cycle: an explicit ``key`` in ms, else from ``speed``.

    The fallback is scaled so speed 5 -- the default -- gives exactly
    ``default_ms``, which keeps every existing effect looking as it did while
    making the speed knob mean something for kinds that had no rate of their own.
    """
    if key in spec:
        return max(0.01, _f(spec, key, default_ms) / 1000.0)
    return max(0.01, _zone_seconds(spec) * (default_ms / 1000.0) / _ZONE_S_AT_5)


def _hsv(h: float, s: float, v: float) -> Color:
    i = int(h * 6) % 6
    f = h * 6 - int(h * 6)
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r, g, b = ((v, t, p), (q, v, p), (p, v, t),
               (p, q, v), (t, p, v), (v, p, q))[i]
    return (round(r * 255), round(g * 255), round(b * 255))


class Effect:
    """Base class, and the single place where timing is decided.

    Every effect has a natural *cycle* -- one blink, one breath, one pass of a
    wavefront. Subclasses say how long that cycle takes and then call
    ``_finish``, which applies the two user-facing knobs uniformly:

      cycles       how many times to repeat (``times`` and ``steps`` are
                   accepted as aliases). 0 means "never stop".
      duration_ms  the total run length. Given, it wins: the cycle is
                   stretched or squeezed so exactly ``cycles`` of them fit.

    Subclasses must express position as a fraction of ``per_cycle`` -- see
    ``phase`` -- rather than in seconds, so overriding the duration
    automatically rescales the motion instead of just truncating it.
    """

    duration: float | None = None
    per_cycle: float = 1.0
    cycles: int = 1

    # Whether ``sample`` depends on ``t`` at all. A still picture is drawn once
    # and then left alone -- the render loop stops ticking for it entirely --
    # so this is the difference between thirty frames a second all day and
    # none. Anything that moves leaves it True.
    animated: bool = True

    # Which editor knobs this effect actually reads. Declared here, beside the
    # code that uses them, so an editor can hide the rest instead of offering
    # sliders that quietly do nothing.
    KNOBS: frozenset = frozenset({"speed", "cycles", "duration"})

    def __init__(self, spec: dict):
        self.spec = spec
        self.bg = _color(spec, "off_color", "000000")
        self.colors = _colors(spec)

    def _finish(self, per_cycle: float, default_cycles: int = 1) -> None:
        spec = self.spec
        # An explicit cycle length overrides whatever the subclass worked out
        # from "speed". Zones use it to express speed as cycles per second,
        # which is the only rate that means anything when nothing ever ends.
        if spec.get("period_ms"):
            per_cycle = max(0.02, float(spec["period_ms"]) / 1000.0)
        asked = spec.get("cycles", spec.get("times", spec.get("steps")))
        total = spec.get("duration_ms", spec.get("ms"))
        if total:
            self.duration = max(0.02, float(total) / 1000.0)
            if asked is None:
                # Duration alone says how *long*, never how fast: the effect
                # keeps its own cycle and repeats as many times as fit. The
                # old reading -- stretch one cycle to fill the time -- made
                # "5 seconds" silently mean "one very slow pass".
                self.per_cycle = max(0.02, per_cycle)
                self.cycles = max(1, int(round(self.duration / self.per_cycle)))
            else:
                # Both given: squeeze exactly that many cycles into the time.
                self.cycles = max(1, int(asked))
                self.per_cycle = self.duration / self.cycles
        else:
            cycles = default_cycles if asked is None else int(asked)
            self.cycles = cycles
            self.per_cycle = max(0.02, per_cycle)
            self.duration = None if cycles <= 0 else self.per_cycle * cycles

    def phase(self, t: float) -> float:
        """Where we are inside the current cycle, as a fraction in [0, 1)."""
        return (t % self.per_cycle) / self.per_cycle

    def color_at(self, t: float) -> Color:
        """Which colour this run is using, for effects that cycle a palette."""
        if len(self.colors) == 1:
            return self.colors[0]
        return self.colors[int(t / self.per_cycle) % len(self.colors)]

    def sample(self, points, t: float) -> list[Color]:
        raise NotImplementedError


class _Static(Effect):
    """No motion of its own: it runs until told otherwise, or for ``ms``."""

    KNOBS = frozenset({"duration"})
    animated = False

    def __init__(self, spec):
        super().__init__(spec)
        self._finish(1.0, default_cycles=0)


class Solid(_Static):
    def sample(self, points, t):
        return [self.colors[0]] * len(points)


class Gradient(_Static):
    """A static ramp across the machine -- a good default look.

    ``stops`` places each colour along the axis as a fraction in [0, 1]. Left
    out, the colours are spread evenly; given, it is what lets a gradient be
    weighted -- a long wash of one colour with a narrow band of another, say.
    """

    def __init__(self, spec):
        super().__init__(spec)
        if len(self.colors) < 2:
            self.colors = [self.colors[0], _color(spec, "color2", "000000")]
        self.axis = str(spec.get("axis", "u")).lower()
        self.stops = self._read_stops(spec.get("stops"))

    KNOBS = frozenset({"duration", "stops"})

    def _read_stops(self, raw) -> list[float]:
        count = len(self.colors)
        even = [i / (count - 1) for i in range(count)]
        if not raw:
            return even
        try:
            stops = [max(0.0, min(1.0, float(v))) for v in raw][:count]
        except (TypeError, ValueError):
            return even
        if len(stops) < count:
            stops += even[len(stops):]
        # Sorted and strictly increasing, so the segment search below is safe
        # and a dragged handle can never invert the ramp.
        stops.sort()
        for i in range(1, count):
            if stops[i] <= stops[i - 1]:
                stops[i] = min(1.0, stops[i - 1] + 1e-4)
        return stops

    def sample(self, points, t):
        out = []
        stops, colors = self.stops, self.colors
        last = len(colors) - 1
        for u, v in points:
            pos = max(0.0, min(1.0, v if self.axis == "v" else u))
            if pos <= stops[0]:
                out.append(colors[0])
                continue
            if pos >= stops[last]:
                out.append(colors[last])
                continue
            seg = 0
            while seg < last - 1 and pos > stops[seg + 1]:
                seg += 1
            span = stops[seg + 1] - stops[seg]
            local = (pos - stops[seg]) / span if span > 0 else 0.0
            out.append(blend(colors[seg], colors[seg + 1], local))
        return out


class Progress(Effect):
    """Level meter: ``value`` percent of the machine lit."""

    KNOBS = frozenset({"duration", "value"})
    animated = False        # the level is fixed when the effect is built

    def __init__(self, spec):
        super().__init__(spec)
        self.value = max(0, min(100, _i(spec, "value", 100))) / 100.0
        self._finish(1.5, default_cycles=1)

    def sample(self, points, t):
        color = self.colors[0]
        out = []
        for u, _v in points:
            # Soften the boundary over one lamp pitch so the edge is not ragged.
            k = max(0.0, min(1.0, (self.value - u) / 0.02 + 1.0))
            out.append(blend(self.bg, color, k))
        return out


class Blink(Effect):
    def __init__(self, spec):
        super().__init__(spec)
        on = _f(spec, "on_ms", 0.0) / 1000.0
        off = _f(spec, "off_ms", 0.0) / 1000.0
        if on or off:
            cycle = (on or 0.12) + (off or 0.12)
            self.duty = (on or 0.12) / cycle
        else:
            # No explicit on/off: an even flash whose rate follows the speed
            # knob, calibrated so speed 5 gives the historical 120+120 ms.
            cycle = max(0.02, _zone_seconds(spec) * 0.240 / _ZONE_S_AT_5)
            self.duty = 0.5
        self._finish(cycle, default_cycles=3)

    def sample(self, points, t):
        color = self.color_at(t) if self.phase(t) < self.duty else self.bg
        return [color] * len(points)


class Breathe(Effect):
    def __init__(self, spec):
        super().__init__(spec)
        self.floor = _f(spec, "min_level", 0.0)
        self._finish(_rate(spec, "period_ms", 1400), default_cycles=2)

    def sample(self, points, t):
        k = self.floor + (1.0 - self.floor) * (
            1.0 - math.cos(2 * math.pi * self.phase(t))) / 2
        return [blend(self.bg, self.color_at(t), k)] * len(points)


class _Front(Effect):
    """Shared machinery for effects built around a travelling wavefront.

    ``smoothness`` widens the leading shoulder so a point lights up gradually
    while the front is still on its neighbour; ``tail`` is what a point keeps
    once the front has gone past; ``gamma`` corrects the eye's response, without
    which a linear ramp looks top-heavy.

    The front is placed from ``phase``, not from elapsed seconds, so setting
    ``duration_ms`` really does make the pulse travel faster or slower rather
    than cutting it off part way.
    """

    def __init__(self, spec):
        super().__init__(spec)
        smoothness = max(0.0, min(1.0, _f(spec, "smoothness", 0.45)))
        self.width = 0.12 + smoothness * 0.9
        self.tail = max(0.02, min(0.95, _f(spec, "tail", 0.45)))
        self.gamma = max(1.0, min(3.0, _f(spec, "gamma", 2.2)))
        # How far behind the front a point still glows, in zone units.
        self.trail = max(0.5, min(3.0, math.log(0.02) / math.log(self.tail)))

    def level(self, distance: float, front: float) -> float:
        if front < distance:
            return math.exp(-((distance - front) / self.width) ** 2) ** self.gamma
        return (self.tail ** (front - distance)) ** self.gamma

    def _travel(self, travel: float, spec: dict, default_cycles: int = 1) -> None:
        self.travel = travel
        self._finish(travel * _zone_seconds(spec), default_cycles)


class CenterOut(_Front):
    """A pulse born at the middle of the machine and running outward.

    On the bar this is exactly the old ``center_out``: the four zones sit at
    distances 1.5, 0.5, 0.5, 1.5 zone-widths from the centre, so the wavefront
    reaches them in the same order and at the same times as before. On the
    keyboard the very same function is simply sampled at 120 points.
    """

    KNOBS = frozenset({"speed", "cycles", "duration", "smoothness", "gamma", "tail"})

    def __init__(self, spec):
        super().__init__(spec)
        # |u - 0.5| reaches 0.5, which is 2 zone widths.
        self._travel(0.5 / ZONE_U + self.trail, spec)

    def sample(self, points, t):
        front = self.phase(t) * self.travel
        color = self.color_at(t)
        return [blend(self.bg, color, self.level(abs(u - 0.5) / ZONE_U, front))
                for u, _v in points]


class Sweep(_Front):
    """A lit front running left to right, optionally bouncing back."""

    KNOBS = frozenset({"speed", "cycles", "duration", "smoothness", "gamma"})

    def __init__(self, spec):
        super().__init__(spec)
        self.bounce = bool(spec.get("bounce", True))
        span = 1.0 / ZONE_U
        # A little margin so the comet leaves the far edge before restarting;
        # deliberately not `trail`, which belongs to the tail-decay model that
        # this effect does not use.
        self._travel(span * (2 if self.bounce else 1) + 0.5, spec)

    def sample(self, points, t):
        span = 1.0 / ZONE_U
        pos = self.phase(t) * self.travel
        head = max(0.0, 2 * span - pos) if (self.bounce and pos > span) else min(pos, span)
        color = self.color_at(t)
        out = []
        for u, _v in points:
            # Symmetric comet around the head, so the bounce looks the same
            # in both directions.
            d = abs(u / ZONE_U - head)
            out.append(blend(self.bg, color,
                             math.exp(-(d / self.width) ** 2) ** self.gamma))
        return out


class Wipe(_Front):
    """Fill the machine up to the front, then clear it the same way."""

    KNOBS = frozenset({"speed", "cycles", "duration"})

    def __init__(self, spec):
        super().__init__(spec)
        self.clear = bool(spec.get("clear", True))
        span = 1.0 / ZONE_U
        self._travel(span * (2 if self.clear else 1), spec)

    def sample(self, points, t):
        span = 1.0 / ZONE_U
        pos = self.phase(t) * self.travel
        filling = pos <= span
        head = (pos if filling else pos - span) * ZONE_U
        color = self.color_at(t)
        return [(color if ((u <= head) if filling else (u > head)) else self.bg)
                for u, _v in points]


class Wave(Effect):
    """A hue wave sweeping across the machine -- the flagship dynamic look."""

    def __init__(self, spec):
        super().__init__(spec)
        self.spread = _f(spec, "spread", 1.0)
        self.axis = str(spec.get("axis", "u")).lower()
        self.sat = max(0.0, min(1.0, _f(spec, "saturation", 1.0)))
        self.val = max(0.0, min(1.0, _f(spec, "value", 1.0)))
        # One cycle is the hue travelling the whole machine.
        self._finish(_zone_seconds(spec) / ZONE_U, default_cycles=0)

    def sample(self, points, t):
        phase = t / self.per_cycle
        out = []
        for u, v in points:
            pos = v if self.axis == "v" else u
            out.append(_hsv((pos * self.spread - phase) % 1.0, self.sat, self.val))
        return out


class Rainbow(Wave):
    """Kept as its own name for configs that ask for it; a narrower wave."""

    def __init__(self, spec):
        super().__init__({**spec, "spread": spec.get("spread", 0.25)})


class Alternate(Effect):
    """Alternating quarters flip between two colours -- very hard to miss."""

    def __init__(self, spec):
        super().__init__(spec)
        self.a = self.colors[0]
        # Second colour comes from the palette; "color2" stays supported for
        # hand-written configs that predate the multi-colour editor.
        self.b = (self.colors[1] if len(self.colors) > 1
                  else _color(spec, "color2", "000000"))
        self._finish(_rate(spec, "step_ms", 130), default_cycles=6)

    def sample(self, points, t):
        flip = int(t / self.per_cycle) % 2
        out = []
        for u, _v in points:
            quarter = min(3, int(u / ZONE_U))
            out.append(self.a if (quarter % 2 == 0) ^ bool(flip) else self.b)
        return out


class Sparkle(Effect):
    def __init__(self, spec):
        super().__init__(spec)
        self.density = max(0.0, min(1.0, _f(spec, "density", 0.08)))
        self._finish(_rate(spec, "step_ms", 90), default_cycles=20)

    def sample(self, points, t):
        # Seed from the cycle index so a frame is stable however often it is
        # sampled, and so the pattern is reproducible.
        rng = random.Random(int(t / self.per_cycle))
        color = self.color_at(t)
        return [color if rng.random() < self.density else self.bg for _ in points]


class Ripple(Effect):
    """An expanding ring, by default from the middle of the keyboard.

    ``origin`` is a ``[u, v]`` pair, so a key press can drop a ripple exactly
    where the key is.
    """

    KNOBS = frozenset({"speed", "cycles", "duration", "smoothness", "gamma"})

    def __init__(self, spec):
        super().__init__(spec)
        origin = spec.get("origin") or [0.5, 0.5]
        self.ou, self.ov = float(origin[0]), float(origin[1])
        self.width = 0.05 + max(0.0, min(1.0, _f(spec, "smoothness", 0.45))) * 0.25
        self.gamma = max(1.0, min(3.0, _f(spec, "gamma", 2.2)))
        self._finish(_zone_seconds(spec) / ZONE_U, default_cycles=1)

    def sample(self, points, t):
        radius = self.phase(t) * 1.2
        color = self.color_at(t)
        out = []
        for u, v in points:
            d = math.hypot(u - self.ou, v - self.ov)
            out.append(blend(self.bg, color,
                             math.exp(-((d - radius) / self.width) ** 2) ** self.gamma))
        return out


# The names worth offering in a picker: one entry per distinct behaviour.
CANONICAL = ("solid", "gradient", "blink", "breathe", "center_out", "sweep",
             "wipe", "wave", "rainbow", "alternate", "sparkle", "progress",
             "ripple")

# Aliases kept so hand-written configs keep loading: "static" is "solid",
# "pulse" is "breathe", "knightrider" is "sweep", "centerout" is "center_out".
# Not a kind like the others: a composite that plays several effects at once,
# one per zone. Kept out of CANONICAL so the plain-effect pickers do not offer
# it as if it were a shape.
LAYOUT = "layout"

ALIASES = {"static": "solid", "pulse": "breathe",
           "knightrider": "sweep", "centerout": "center_out"}

KINDS = {
    "solid": Solid,
    "static": Solid,
    "gradient": Gradient,
    "blink": Blink,
    "breathe": Breathe,
    "pulse": Breathe,
    "center_out": CenterOut,
    "centerout": CenterOut,
    "sweep": Sweep,
    "knightrider": Sweep,
    "wipe": Wipe,
    "wave": Wave,
    "rainbow": Rainbow,
    "alternate": Alternate,
    "sparkle": Sparkle,
    "progress": Progress,
    "ripple": Ripple,
}


def build(spec: dict) -> Effect:
    kind = str(spec.get("kind", "solid")).lower()
    if kind == LAYOUT:
        # Imported here, not at module scope: layouts are built out of ordinary
        # effects, so the dependency only runs one way at import time.
        from .layouts import Layout
        return Layout(spec)
    try:
        cls = KINDS[kind]
    except KeyError:
        raise ValueError(
            f"unknown effect kind: {kind} (known: {', '.join(sorted(set(KINDS)))})"
        ) from None
    return cls(spec)


def knobs_for(kind: str) -> frozenset:
    """Which editor knobs the given kind honours."""
    if str(kind).lower() == LAYOUT:
        # A layout has no knobs of its own worth showing: every one of them
        # belongs to a zone, and the zone editor offers them there.
        return frozenset({"duration"})
    cls = KINDS.get(str(kind).lower())
    return cls.KNOBS if cls is not None else Effect.KNOBS


def canonical(kind: str) -> str:
    """The preferred spelling of a kind, resolving aliases."""
    text = str(kind).lower()
    return ALIASES.get(text, text)


def describe(name: str, spec: dict) -> str:
    bits = [f"{name}: {spec.get('kind', 'solid')}"]
    for key in ("color", "colors", "target", "speed", "cycles", "priority"):
        if key in spec:
            bits.append(f"{key}={spec[key]}")
    return "  ".join(bits)
