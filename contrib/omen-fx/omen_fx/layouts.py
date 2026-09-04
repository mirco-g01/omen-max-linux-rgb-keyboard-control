"""Zone layouts: several effects playing on one surface at once.

A layout is an *ordered* list of zones. Each zone owns a set of grid cells and
carries an effect spec of its own. Later zones sit on top of earlier ones: on a
cell claimed by more than one zone the last one wins. That is the painter's
algorithm, and it is why "priority" here is nothing more than position in the
list -- zone 1 is the background, each zone added afterwards paints over it.

Cells claimed by nobody come out *transparent* (``None``) rather than black, so
a layout used as an alert overlay lets the default look show through underneath
instead of punching a hole in it. The engine resolves that when compositing.

The whole thing is an ``Effect`` like any other -- same ``sample(points, t)``,
same ``duration`` -- so the engine, the jobs, the fades and the GUI preview
drive a layout without knowing it is one.
"""

from __future__ import annotations

import logging

from . import effects as fx

log = logging.getLogger("omen-fx.layouts")

# The per-key keyboard is exactly this, in hardware: six rows of twenty lamps.
GRID_ROWS = 6
GRID_COLS = 20
BAR_CELLS = 4

LAYOUT_KIND = "layout"

ON = "1#xX*"


# -- masks -------------------------------------------------------------------

def parse_mask(value, rows: int = GRID_ROWS, cols: int = GRID_COLS) -> frozenset:
    """Cells of a zone, from either of the two shapes a config may use.

    A list of row strings -- ``["11110000...", ...]`` -- is the readable form
    the GUI writes and a human can edit. A list of ``[row, col]`` pairs is
    accepted too, because it is the obvious thing to write by hand for a zone
    of three keys.
    """
    if not value:
        return frozenset()
    cells = set()
    if isinstance(value, str):
        value = [value]
    for item in value:
        if isinstance(item, str):
            continue
        break
    else:   # every item was a string: row-per-line form
        for r, line in enumerate(value[:rows]):
            for c, ch in enumerate(str(line)[:cols]):
                if ch in ON:
                    cells.add((r, c))
        return frozenset(cells)
    for pair in value:      # [[row, col], ...]
        try:
            r, c = int(pair[0]), int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 <= r < rows and 0 <= c < cols:
            cells.add((r, c))
    return frozenset(cells)


def dump_mask(cells, rows: int = GRID_ROWS, cols: int = GRID_COLS) -> list[str]:
    """The row-string form, for writing back out."""
    return ["".join("1" if (r, c) in cells else "0" for c in range(cols))
            for r in range(rows)]


def parse_bar(value) -> frozenset:
    """Which of the four bar segments a zone claims."""
    if not value:
        return frozenset()
    if isinstance(value, str):
        return frozenset(i for i, ch in enumerate(value[:BAR_CELLS]) if ch in ON)
    out = set()
    for item in value:
        try:
            i = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= i < BAR_CELLS:
            out.add(i)
    return frozenset(out)


def dump_bar(segments) -> str:
    return "".join("1" if i in segments else "0" for i in range(BAR_CELLS))


# -- geometry ----------------------------------------------------------------

_grid_cache: dict = {}


def grid_of(points) -> list[tuple[int, int]]:
    """The (row, column) of every point, derived from the geometry itself.

    Rows are the distinct heights, bottom to top in list order; columns are the
    left-to-right rank within a row. Deriving it rather than assuming lamp ids
    are laid out in reading order keeps this honest on any LampArray, and it
    gives the bar (one row of four) a grid for free.
    """
    if not points:
        return []
    key = (len(points), points[0], points[-1])
    got = _grid_cache.get(key)
    if got is not None:
        return got
    levels = sorted({round(v, 4) for _, v in points})
    rank = {v: i for i, v in enumerate(levels)}
    by_row: dict[int, list] = {}
    for i, (u, _v) in enumerate(points):
        by_row.setdefault(rank[round(_v, 4)], []).append((u, i))
    cells: list[tuple[int, int]] = [(0, 0)] * len(points)
    for r, items in by_row.items():
        for c, (_u, i) in enumerate(sorted(items)):
            cells[i] = (r, c)
    _grid_cache[key] = cells
    return cells


def grid_size(points) -> tuple[int, int]:
    cells = grid_of(points)
    if not cells:
        return (0, 0)
    return (max(r for r, _ in cells) + 1, max(c for _, c in cells) + 1)


# -- zones -------------------------------------------------------------------

# Keys that belong to the zone itself rather than to the effect it plays.
ZONE_KEYS = ("name", "mask", "bar", "scope")


class Zone:
    """One region of a surface, and the effect that plays inside it."""

    def __init__(self, spec: dict):
        self.name = str(spec.get("name", "") or "")
        self.cells = parse_mask(spec.get("mask"))
        self.bar = parse_bar(spec.get("bar"))
        # "zone": the effect is rescaled to the zone's own bounding box, so a
        # rainbow in a narrow zone shows a whole rainbow. "surface": the zone
        # is a window onto an effect spanning the whole surface, which is what
        # you want when several zones should stay in step.
        self.scope = str(spec.get("scope", "zone")).lower()
        self.spec = {k: v for k, v in spec.items() if k not in ZONE_KEYS}
        self.effect = fx.build(self.spec)
        self._points: dict = {}

    def resolve(self, points, cells, key):
        """Indices this zone owns, and the coordinates to sample it at."""
        got = self._points.get(key)
        if got is not None:
            return got
        idx = [i for i, cell in enumerate(cells) if cell in self.cells]
        pts = [points[i] for i in idx]
        if self.scope == "zone" and pts:
            us = [p[0] for p in pts]
            vs = [p[1] for p in pts]
            u0, v0 = min(us), min(vs)
            du = (max(us) - u0) or 1.0
            dv = (max(vs) - v0) or 1.0
            pts = [((u - u0) / du, (v - v0) / dv) for u, v in pts]
        got = (idx, pts)
        self._points[key] = got
        return got

class Layout(fx.Effect):
    """An ordered stack of zones, presented as a single effect."""

    KNOBS = frozenset({"duration"})

    # Segment boundaries in a layout are lines the user drew in the editor, so
    # the bar must not soften them the way it softens a continuous effect.
    crisp_bar = True

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.zones = [Zone(z) for z in spec.get("zones", []) or []]
        # A stack of still zones is a still picture, whatever the stack looks
        # like: one zone that moves is enough to keep the whole layout running.
        self.animated = any(z.effect.animated for z in self.zones)
        self._finish_layout(spec)

    def _finish_layout(self, spec: dict) -> None:
        total = spec.get("duration_ms", spec.get("ms"))
        if total:
            # An explicit total wins, so an alert layout can be told to last
            # exactly five seconds whatever its zones do. Each zone keeps
            # running at its own rate inside that time.
            self.duration = max(0.02, float(total) / 1000.0)
        else:
            # Zones run at whatever rate each was given, so "one cycle of the
            # layout" is ambiguous. The slowest zone is the reference: long
            # enough that every zone completes at least one full cycle, and the
            # unit a repetition count is counted in.
            # Only zones that actually move: a static zone has a nominal cycle
            # of its own, and letting it be "the slowest" would set the length
            # from something that never changes.
            spans = [z.effect.per_cycle for z in self.zones
                     if "speed" in fx.knobs_for(z.spec.get("kind", "solid"))]
            slowest = max(spans) if spans else None
            asked = spec.get("cycles", spec.get("times"))
            if slowest is None or (asked is not None and int(asked) <= 0):
                self.duration = None
            elif asked is None:
                self.duration = slowest
            else:
                self.duration = slowest * int(asked)
        self.per_cycle = self.duration or 1.0
        self.cycles = 1

    # -- sampling --------------------------------------------------------

    def sample(self, points, t: float):
        if not points:
            return []
        # One row of points means the bar, however finely it is sampled --
        # more robust than counting them, now that a segment is read as an
        # area rather than a point.
        if grid_size(points)[0] == 1 and self.zones:
            return self._sample_bar(points, t)
        return self._sample_grid(points, t)

    def _sample_grid(self, points, t: float):
        cells = grid_of(points)
        key = (len(points), points[0], points[-1])
        out: list = [None] * len(points)
        for zone in self.zones:
            idx, pts = zone.resolve(points, cells, key)
            if not idx:
                continue
            colors = zone.effect.sample(pts, t)
            for slot, i in enumerate(idx):
                if slot < len(colors):
                    out[i] = colors[slot]
        return out

    def _sample_bar(self, points, t: float):
        """Only the segments a zone explicitly claims.

        The bar is part of the layout rather than a projection of it: its four
        cells are painted in the editor beside the keys, so one zone can cover
        the bar alone, the keys alone, or both. Segments nobody claims stay
        transparent, exactly like keys nobody claims.
        """
        cells = grid_of(points)
        ncols = max(c for _r, c in cells) + 1
        # Several samples may feed one segment. Mapping each sample to the
        # segment it falls in keeps this working whatever the surface's
        # sampling density is.
        seg_of = [min(BAR_CELLS - 1, (c * BAR_CELLS) // ncols) for _r, c in cells]
        out: list = [None] * len(points)
        for zone in self.zones:
            if not zone.bar:
                continue
            idx = [i for i, seg in enumerate(seg_of) if seg in zone.bar]
            if not idx:
                continue
            pts = [points[i] for i in idx]
            if len(pts) > 1:
                # Rescaled to the claimed segments, the same way a zone on the
                # keys is rescaled to its own cells.
                us = [u for u, _v in pts]
                u0 = min(us)
                du = (max(us) - u0) or 1.0
                pts = [((u - u0) / du, v) for u, v in pts]
            colors = zone.effect.sample(pts, t)
            for slot, i in enumerate(idx):
                if slot < len(colors):
                    out[i] = colors[slot]
        return out


def expand(spec: dict | None, registry: dict) -> dict | None:
    """Resolve a ``layout = "name"`` reference into the zones themselves.

    Kept a plain function so the daemon and the GUI resolve references exactly
    the same way -- the preview would otherwise be a second implementation of
    the same rule, free to drift from the one that actually runs.

    Returns None when the layout is missing: better to leave a surface alone
    than to light it with something the user did not ask for.
    """
    if not spec or str(spec.get("kind", "")).lower() != LAYOUT_KIND:
        return spec
    name = spec.get("layout")
    if not name or "zones" in spec:
        return spec
    layout = (registry or {}).get(str(name))
    if layout is None:
        log.error("no layout named %r -- leaving the surface untouched", name)
        return None
    merged = {**layout, **{k: v for k, v in spec.items() if k != "layout"}}
    merged["kind"] = LAYOUT_KIND
    merged["layout"] = str(name)
    return merged


def zone_names(spec: dict) -> list[str]:
    return [str(z.get("name", "") or f"zone {i + 1}")
            for i, z in enumerate(spec.get("zones", []) or [])]


def validate(spec: dict) -> str | None:
    """Why this layout cannot be built, or None if it can."""
    zones = spec.get("zones")
    if not isinstance(zones, list) or not zones:
        return "a layout needs at least one zone"
    for i, zone in enumerate(zones, 1):
        if not isinstance(zone, dict):
            return f"zone {i} is not a table"
        if not parse_mask(zone.get("mask")) and not parse_bar(zone.get("bar")):
            return f"zone {i} ({zone.get('name', '')!r}) claims no cells"
        try:
            fx.build({k: v for k, v in zone.items() if k not in ZONE_KEYS})
        except ValueError as exc:
            return f"zone {i}: {exc}"
    return None
