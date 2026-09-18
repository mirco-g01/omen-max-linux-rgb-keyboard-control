"""Configuration loading and trigger matching."""

from __future__ import annotations

import fnmatch
import logging
import os
import tomllib

log = logging.getLogger("omen-fx.config")

CONFIG_PATH = os.environ.get("OMEN_FX_CONFIG", "/etc/omen-fx/config.toml")
STATE_DIR = "/var/lib/omen-fx"
# Effects edited from the GUI land here instead of in config.toml, so the
# hand-written file (and its comments) is never rewritten by a program.
EFFECTS_PATH = os.environ.get("OMEN_FX_EFFECTS", "/etc/omen-fx/effects.toml")
# Same idea for the per-surface default profiles the GUI edits: config.toml
# stays hand-written, this file is the program's.
BASE_PATH = os.environ.get("OMEN_FX_BASE", "/etc/omen-fx/base.toml")
# Zone layouts, likewise written by the GUI: a named, ordered stack of zones
# that any effect or default profile can refer to by name.
LAYOUTS_PATH = os.environ.get("OMEN_FX_LAYOUTS", "/etc/omen-fx/layouts.toml")
# Machine-wide switches the GUI can flip -- idle dimming, per power source.
# Same split as the others: config.toml stays hand-written, this file is the
# program's.
SETTINGS_PATH = os.environ.get("OMEN_FX_SETTINGS", "/etc/omen-fx/settings.toml")
SOCKET_PATH = os.environ.get("OMEN_FX_SOCKET", "/run/omen-fx/control.sock")

# Filters a trigger rule may carry, besides "event".
FILTER_KEYS = ("app", "summary", "body", "urgency", "service", "user")

DEFAULTS = {
    "daemon": {
        "sysfs": "/sys/devices/platform/omen-rgb-keyboard/rgb_zones",
        "socket": SOCKET_PATH,
        "effect_brightness": 100,
        "queue_limit": 4,
        "hold_timeout": 60,
        # Where an overlay plays when neither the effect nor the request says:
        # "bar", "keys" or "both".
        "default_target": "both",
        # Milliseconds spent fading from an alert's last frame back to the
        # default look. 0 cuts straight there; an effect may override it.
        "fade_back_ms": 300,
        # How wide a window each light-bar segment averages the effect over,
        # measured in segments either side of its centre. Higher blends the
        # four segments into one another more; lower keeps them distinct.
        "bar_blend": 1.5,
    },
    # [base] keeps the old meaning -- what to go back to once an effect ends
    # -- and gains two optional sub-tables, [base.bar] and [base.keys], each
    # holding a default profile (any effect spec, static or animated) that the
    # surface shows whenever no overlay is on top of it.
    "base": {"mode": "capture", "apply_on_start": False},
    # Dim both surfaces after a spell of no keyboard or mouse activity. This is
    # a *rendering* level, not the user's brightness: the desktop's slider is
    # left exactly where its owner put it, so nothing goes stale and the
    # machine wakes back to the level it was set to.
    #
    # These are the values for both power sources. [idle.ac] and
    # [idle.battery] may each override any of them, so the machine can go
    # dark on battery and merely dim at the desk; see Config.idle_for.
    "idle": {
        "enabled": False,
        "timeout": 60,      # seconds of no input before dimming
        "brightness": 20,   # percent of the normal level once dimmed
        "fade_ms": 1500,    # how long the dim takes; waking up is immediate
        # Lift the dim on a surface while an alert plays on it, then dim
        # again. The user's own level still applies, so a slider at zero
        # stays dark whatever arrives.
        "wake_for_alerts": False,
    },
    "effects": {},
    "layouts": {},
    "trigger": [],
}


class Config:
    def __init__(self, data: dict, path: str | None = None, error: str | None = None):
        self.path = path
        self.data = data
        # A config that fails to parse falls back to built-in defaults, which
        # silently disables every trigger. Carry the reason so status can say
        # so out loud instead of leaving it in the journal.
        self.error = error
        self.daemon = {**DEFAULTS["daemon"], **data.get("daemon", {})}
        self.base = {**DEFAULTS["base"], **data.get("base", {})}
        self.idle = {**DEFAULTS["idle"], **(data.get("idle") or {})}
        self.effects: dict[str, dict] = data.get("effects", {}) or {}
        self.layouts: dict[str, dict] = data.get("layouts", {}) or {}
        rules = data.get("trigger", []) or []
        # More specific rules win, regardless of the order they appear in the
        # file: a rule filtering on app *and* urgency beats a catch-all.
        self.rules = sorted(
            rules,
            key=lambda r: sum(1 for k in FILTER_KEYS if k in r),
            reverse=True,
        )

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> "Config":
        error = None
        try:
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
        except FileNotFoundError:
            log.warning("no config at %s, using built-in defaults", path)
            data = {}
            error = f"{path} not found -- running on built-in defaults"
        except tomllib.TOMLDecodeError as exc:
            log.error("config %s is invalid (%s), using built-in defaults", path, exc)
            data = {}
            error = (f"{path} is not valid TOML ({exc}) -- running on built-in "
                     "defaults, so NO triggers are active")
        overlay = cls._load_overlay()
        if overlay:
            merged = dict(data.get("effects", {}) or {})
            merged.update(overlay)
            data = {**data, "effects": merged}
        layout_overlay = cls._load_table(LAYOUTS_PATH, "layouts")
        if layout_overlay:
            merged_layouts = dict(data.get("layouts", {}) or {})
            merged_layouts.update(layout_overlay)
            data = {**data, "layouts": merged_layouts}
        base_overlay = cls._load_base_overlay()
        if base_overlay:
            merged_base = dict(data.get("base", {}) or {})
            merged_base.update(base_overlay)
            data = {**data, "base": merged_base}
        idle_overlay = cls._load_table(SETTINGS_PATH, "idle")
        if idle_overlay:
            # The GUI writes [idle.ac] and [idle.battery]; an older file has
            # flat keys. Either way the file wins over config.toml key by key,
            # sub-tables included, and a flat key it sets applies to both.
            merged_idle = dict(data.get("idle", {}) or {})
            merged_idle.update(idle_overlay)
            data = {**data, "idle": merged_idle}
        return cls(data, path, error)

    @classmethod
    def _load_overlay(cls) -> dict:
        """Effects saved by the GUI, layered on top of config.toml."""
        return cls._load_table(EFFECTS_PATH, "effects")

    @staticmethod
    def _load_table(path: str, key: str) -> dict:
        """One table out of a GUI-written overlay file, or {} if unusable."""
        try:
            with open(path, "rb") as fh:
                return tomllib.load(fh).get(key, {}) or {}
        except FileNotFoundError:
            return {}
        except (OSError, tomllib.TOMLDecodeError) as exc:
            log.error("ignoring %s: %s", path, exc)
            return {}

    def expand(self, spec: dict | None) -> dict | None:
        """Resolve a ``layout = "name"`` reference into the zones themselves.

        Done here rather than in the effect factory so that everything below --
        the engine, the jobs, the preview -- receives a self-contained spec and
        never has to reach back for the layout registry.
        """
        from .layouts import expand as expand_layout
        return expand_layout(spec, self.layouts)

    def base_profile(self, surface: str) -> dict | None:
        """The default look for ``bar`` or ``keys``, or None to leave it alone.

        Leaving it unset matters: with no ``[base.keys]`` the daemon never takes
        the LampArray, so the keyboard keeps its own firmware lighting and the
        light bar is not pulled into the firmware's mirrored mode.
        """
        spec = self.base.get(surface)
        if isinstance(spec, dict) and spec:
            return self.expand(dict(spec))
        return None

    def idle_for(self, on_battery: bool) -> dict:
        """The idle-dim settings in force on one power source.

        Three layers: the built-in defaults, the flat keys of ``[idle]`` (which
        apply to both, and are all an older settings file has), then
        ``[idle.battery]`` or ``[idle.ac]`` on top.
        """
        flat = {k: v for k, v in self.idle.items() if not isinstance(v, dict)}
        specific = self.idle.get("battery" if on_battery else "ac")
        return {**DEFAULTS["idle"], **flat,
                **(specific if isinstance(specific, dict) else {})}

    @classmethod
    def _load_base_overlay(cls) -> dict:
        """Default profiles saved by the GUI, layered on top of config.toml."""
        return cls._load_table(BASE_PATH, "base")

    def effect(self, name: str) -> dict:
        try:
            return self.expand(dict(self.effects[name]))
        except KeyError:
            raise KeyError(f"no effect named {name!r} in {self.path}") from None

    def match(self, event: str, attrs: dict) -> dict | None:
        """Return the first trigger rule matching ``event`` with ``attrs``."""
        for rule in self.rules:
            if rule.get("event") != event:
                continue
            if _matches(rule, attrs):
                return rule
        return None


def _matches(rule: dict, attrs: dict) -> bool:
    for key in FILTER_KEYS:
        if key not in rule:
            continue
        want, got = rule[key], attrs.get(key)
        if got is None:
            return False
        if key == "urgency":
            if int(got) != int(want):
                return False
        elif key in ("summary", "body"):
            if str(want).lower() not in str(got).lower():
                return False
        else:  # glob, case-insensitive
            if not fnmatch.fnmatch(str(got).lower(), str(want).lower()):
                return False
    return True
