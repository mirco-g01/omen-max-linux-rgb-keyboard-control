"""omen-fxd -- owns both lit surfaces and plays effects on request.

The daemon runs as root (sysfs writes, hidraw ioctls) and listens on a UNIX
socket that both the user's session tools and the root-side PAM hook can talk
to. It owns two surfaces -- the 4-zone light bar and the 120-lamp per-key
keyboard -- and keeps one invariant: whatever the lighting looked like before
the first effect started is restored once the last effect finishes, and the
LampArray is always handed back to the firmware on the way out.

Rendering lives in engine.py; this module is the config, the socket and the
request vocabulary.
"""

from __future__ import annotations

import argparse
import atexit
import errno
import grp
import json
import logging
import os
import re
import signal
import socket
import threading
import time

from . import effects as fx
from . import layouts as lay
from .config import (Config, CONFIG_PATH, EFFECTS_PATH, BASE_PATH,
                     LAYOUTS_PATH, SETTINGS_PATH, STATE_DIR)
from .engine import Engine, Job, TARGETS, normalise_target
from .idle import IdleWatcher
from .led import DeviceUnavailable, Snapshot
from .surface import build_surfaces, set_bar_blend

log = logging.getLogger("omen-fx")

BASE_STATE_FILE = os.path.join(STATE_DIR, "base.json")


# Names the GUI is allowed to create, and the value shapes it may store.
EFFECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
EFFECT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def dump_effects(effects: dict) -> str:
    """Serialise just the [effects.*] tables -- no general TOML writer needed."""
    out = ["# Written by omen-fx-gui. Edits here are overwritten on save.",
           "# Hand-written effects belong in config.toml; these win over those.",
           ""]
    for name in sorted(effects):
        out.append(f"[effects.{name}]")
        for key, value in effects[name].items():
            out.append(f"{key} = {_toml_value(value)}")
        out.append("")
    return "\n".join(out)


def dump_base(profiles: dict) -> str:
    out = ["# Written by omen-fx-gui: the default look of each surface.",
           "# Hand-written defaults belong in config.toml; these win over those.",
           ""]
    for name in sorted(profiles):
        out.append(f"[base.{name}]")
        for key, value in profiles[name].items():
            out.append(f"{key} = {_toml_value(value)}")
        out.append("")
    return "\n".join(out)


def dump_settings(idle: dict) -> str:
    out = ["# Written by omen-fx-gui: machine-wide switches.",
           "# Hand-written values belong in config.toml; these win over those.",
           "",
           "[idle]"]
    for key in ("enabled", "timeout", "brightness", "fade_ms"):
        if key in idle:
            out.append(f"{key} = {_toml_value(idle[key])}")
    out.append("")
    return "\n".join(out)


def validate_settings(data) -> dict:
    """Accept only the four idle knobs, within ranges that cannot brick a look.

    A floor of 0 is allowed -- some people want the lights off entirely when
    they walk away -- but the timeout has a floor of five seconds, because a
    one-second timeout dims while you are reading and reads as a fault.
    """
    if not isinstance(data, dict):
        raise ValueError("settings must be a table")
    spec = {"timeout": (5, 3600), "brightness": (0, 100), "fade_ms": (0, 30000)}
    out: dict = {"enabled": bool(data.get("enabled", False))}
    for key, (low, high) in spec.items():
        if key not in data:
            continue
        try:
            value = int(data[key])
        except (TypeError, ValueError):
            raise ValueError(f"idle.{key} must be a whole number") from None
        if not low <= value <= high:
            raise ValueError(f"idle.{key} must be between {low} and {high}")
        out[key] = value
    return out


def dump_layouts(layouts: dict) -> str:
    """Serialise [layouts.*] and their [[layouts.*.zones]] arrays."""
    out = ["# Written by omen-fx-gui: named zone layouts.",
           "# A layout is an ordered stack of zones; later zones paint over",
           "# earlier ones, and cells no zone claims stay transparent.",
           ""]
    for name in sorted(layouts):
        spec = layouts[name]
        out.append(f"[layouts.{name}]")
        for key, value in spec.items():
            if key == "zones":
                continue
            out.append(f"{key} = {_toml_value(value)}")
        out.append("")
        for zone in spec.get("zones", []) or []:
            out.append(f"[[layouts.{name}.zones]]")
            for key, value in zone.items():
                out.append(f"{key} = {_toml_value(value)}")
            out.append("")
    return "\n".join(out)


def _clean_table(spec: dict, where: str) -> dict:
    """Scalars and scalar lists only -- nothing a client could smuggle through."""
    entry: dict = {}
    for key, value in spec.items():
        if not EFFECT_KEY_RE.match(str(key)):
            raise ValueError(f"{where}: invalid parameter {key!r}")
        if isinstance(value, (list, tuple)):
            if not all(isinstance(v, (str, int, float, bool)) for v in value):
                raise ValueError(f"{where}.{key}: list may only hold scalars")
            entry[key] = list(value)
        elif isinstance(value, (str, int, float, bool)):
            entry[key] = value
        else:
            raise ValueError(f"{where}.{key}: unsupported value type")
    return entry


def validate_layouts(layouts) -> dict:
    """Check the zone layouts the GUI wants to store."""
    if not isinstance(layouts, dict):
        raise ValueError("layouts must be a table")
    clean: dict = {}
    for name, spec in layouts.items():
        if not EFFECT_NAME_RE.match(str(name)):
            raise ValueError(f"invalid layout name: {name!r}")
        if not isinstance(spec, dict):
            raise ValueError(f"{name}: layout must be a table")
        zones = spec.get("zones")
        if not isinstance(zones, list) or not zones:
            raise ValueError(f"{name}: a layout needs at least one zone")
        entry = _clean_table({k: v for k, v in spec.items() if k != "zones"}, str(name))
        # Retired: the bar is driven by the zones' own bar cells now, not by a
        # projection of the keyboard.
        entry.pop("bar_source", None)
        entry["zones"] = [
            _clean_table(z if isinstance(z, dict) else {}, f"{name}.zone{i}")
            for i, z in enumerate(zones, 1)
        ]
        problem = lay.validate(entry)
        if problem:
            raise ValueError(f"{name}: {problem}")
        clean[str(name)] = entry
    return clean


def validate_profiles(profiles) -> dict:
    """Check the per-surface default profiles the GUI wants to store."""
    if not isinstance(profiles, dict):
        raise ValueError("base profiles must be a table")
    clean: dict = {}
    for name, spec in profiles.items():
        if name not in ("bar", "keys"):
            raise ValueError(f"unknown surface: {name!r} (use 'bar' or 'keys')")
        if spec in (None, {}):
            continue          # an empty profile means "leave this surface alone"
        if not isinstance(spec, dict):
            raise ValueError(f"{name}: profile must be a table")
        entry: dict = {}
        for key, value in spec.items():
            if not EFFECT_KEY_RE.match(str(key)):
                raise ValueError(f"{name}: invalid parameter {key!r}")
            if isinstance(value, (list, tuple)):
                if not all(isinstance(v, (str, int, float, bool)) for v in value):
                    raise ValueError(f"{name}.{key}: list may only hold scalars")
                entry[key] = list(value)
            elif isinstance(value, (str, int, float, bool)):
                entry[key] = value
            else:
                raise ValueError(f"{name}.{key}: unsupported value type")
        # "mirror" is a bar-only pseudo-kind handled by the engine, not an effect.
        if str(entry.get("kind", "")).lower() == "mirror":
            if name != "bar":
                raise ValueError('kind "mirror" only makes sense for the bar')
        else:
            fx.build(entry)   # must actually be playable
        clean[str(name)] = entry
    return clean


def validate_effects(effects) -> dict:
    """Reject anything that is not a plain, buildable effect definition."""
    if not isinstance(effects, dict):
        raise ValueError("effects must be a table")
    clean: dict = {}
    for name, spec in effects.items():
        if not EFFECT_NAME_RE.match(str(name)):
            raise ValueError(f"invalid effect name: {name!r}")
        if not isinstance(spec, dict):
            raise ValueError(f"{name}: effect must be a table")
        entry: dict = {}
        for key, value in spec.items():
            if not EFFECT_KEY_RE.match(str(key)):
                raise ValueError(f"{name}: invalid parameter {key!r}")
            if isinstance(value, (list, tuple)):
                if not all(isinstance(v, (str, int, float, bool)) for v in value):
                    raise ValueError(f"{name}.{key}: list may only hold scalars")
                entry[key] = list(value)
            elif isinstance(value, (str, int, float, bool)):
                entry[key] = value
            else:
                raise ValueError(f"{name}.{key}: unsupported value type")
        fx.build(entry)   # must actually be playable
        if "target" in entry:
            normalise_target(entry["target"])   # raises ValueError if bogus
        clean[str(name)] = entry
    return clean


def _load_pinned(config: Config) -> Snapshot | None:
    """The base to return to when ``base.mode = "pinned"``."""
    if str(config.base.get("mode", "capture")) != "pinned":
        return None
    try:
        with open(BASE_STATE_FILE) as fh:
            return Snapshot.from_dict(json.load(fh))
    except (OSError, ValueError):
        pass
    if "zones" in config.base or "color" in config.base:
        data = dict(config.base)
        if "color" in data and "zones" not in data:
            data["zones"] = [data["color"]] * 4
        return Snapshot.from_dict(data)
    return None


# -- request handling ----------------------------------------------------


class Server:
    def __init__(self, config: Config, engine: Engine, path: str):
        self.config = config
        self.engine = engine
        self.path = path
        self.sock = self._bind(path)

    @staticmethod
    def _bind(path: str) -> socket.socket:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            os.unlink(path)
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        sock.listen(8)
        # Members of "input" already control the RGB sysfs files (the driver's
        # own udev rules), so that is the right group for the control socket.
        try:
            os.chown(path, 0, grp.getgrnam("input").gr_gid)
            os.chmod(path, 0o660)
        except (KeyError, PermissionError, OSError) as exc:
            log.warning("could not restrict %s to group input (%s); using 0666", path, exc)
            os.chmod(path, 0o666)
        return sock

    def serve_forever(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(5)
            try:
                data = conn.recv(65536).decode("utf-8", "replace").strip()
                reply = self.dispatch(json.loads(data)) if data else {"error": "empty request"}
            except json.JSONDecodeError as exc:
                reply = {"error": f"bad json: {exc}"}
            except Exception as exc:  # never let a client kill the daemon
                log.exception("request failed")
                reply = {"error": str(exc)}
            try:
                conn.sendall((json.dumps(reply) + "\n").encode())
            except OSError:
                pass

    # -- commands --------------------------------------------------------

    def dispatch(self, req: dict) -> dict:
        cmd = str(req.get("cmd", "")).lower()
        handler = getattr(self, f"cmd_{cmd}", None)
        if handler is None:
            return {"error": f"unknown command: {cmd or '(none)'}"}
        return handler(req)

    def cmd_ping(self, req: dict) -> dict:
        return {"ok": True, "pong": True}

    def cmd_status(self, req: dict) -> dict:
        reply = {"ok": True, "status": self.engine.status(),
                 "effects": sorted(self.config.effects), "config": self.config.path}
        if self.config.error:
            reply["config_error"] = self.config.error
        return reply

    def cmd_stop(self, req: dict) -> dict:
        self.engine.stop_all()
        return {"ok": True}

    def cmd_release(self, req: dict) -> dict:
        key = str(req.get("key") or req.get("name") or "")
        if not key:
            return {"error": "release needs a key"}
        return {"ok": True, "result": self.engine.release(key)}

    def cmd_reload(self, req: dict) -> dict:
        self.config = Config.load(self.config.path or CONFIG_PATH)
        set_bar_blend(self.config.daemon.get("bar_blend", 1.5))
        self.engine.reload(self.config)
        return {"ok": True, "effects": sorted(self.config.effects)}

    def cmd_base(self, req: dict) -> dict:
        """Inspect or pin the lighting the daemon returns to after an effect."""
        action = str(req.get("action", "show")).lower()
        if action == "show":
            with self.engine.cv:
                base = self.engine._bar_base
            snap = base or self.engine.bar.bar.capture()
            return {"ok": True, "base": snap.to_dict(), "live": base is None,
                    "mode": self.config.base.get("mode", "capture"),
                    "profiles": {n: self.config.base_profile(n)
                                 for n in ("bar", "keys")}}
        if action == "pin":
            snap = self.engine.bar.bar.capture()
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(BASE_STATE_FILE, "w") as fh:
                json.dump(snap.to_dict(), fh, indent=2)
            return {"ok": True, "base": snap.to_dict(), "file": BASE_STATE_FILE}
        if action == "clear":
            try:
                os.unlink(BASE_STATE_FILE)
            except OSError:
                pass
            return {"ok": True}
        if action == "apply":
            snap = _load_pinned(self.config)
            if snap is None:
                return {"error": "no pinned base (set base.mode = \"pinned\" and run: omen-fx base pin)"}
            self.engine.stop_all()
            self.engine.bar.bar.restore(snap)
            with self.engine.cv:
                self.engine._bar_base = None
            return {"ok": True, "base": snap.to_dict()}
        return {"error": f"unknown base action: {action}"}

    def cmd_config(self, req: dict) -> dict:
        """Read or replace the effect definitions (used by omen-fx-gui).

        Only [effects.*] is writable, and it goes to a separate file: the
        daemon never rewrites the hand-maintained config.toml, and a client on
        the control socket cannot reach settings like daemon.sysfs -- which the
        daemon acts on as root.
        """
        action = str(req.get("action", "get")).lower()
        if action == "get":
            return {"ok": True, "effects": self.config.effects,
                    "settings_file": SETTINGS_PATH,
                    "idle": dict(self.config.idle),
                    "config": self.config.path, "effects_file": EFFECTS_PATH,
                    "base_file": BASE_PATH,
                    "base_raw": {n: dict(self.config.base.get(n) or {})
                                 for n in ("bar", "keys")},
                    "layouts": self.config.layouts,
                    "layouts_file": LAYOUTS_PATH,
                    "base": {n: self.config.base_profile(n) or {}
                             for n in ("bar", "keys")}}
        if action == "save_settings":
            try:
                idle = validate_settings(req.get("idle"))
            except (ValueError, KeyError) as exc:
                return {"error": str(exc)}
            written = self._write_toml(SETTINGS_PATH, dump_settings(idle))
            if written is not None:
                return written
            self.cmd_reload({})
            return {"ok": True, "file": SETTINGS_PATH, "idle": idle}
        if action == "save_base":
            try:
                profiles = validate_profiles(req.get("base"))
            except (ValueError, KeyError) as exc:
                return {"error": str(exc)}
            written = self._write_toml(BASE_PATH, dump_base(profiles))
            if written is not None:
                return written
            self.cmd_reload({})
            return {"ok": True, "saved": len(profiles), "file": BASE_PATH}
        if action == "save_layouts":
            try:
                layouts = validate_layouts(req.get("layouts"))
            except (ValueError, KeyError) as exc:
                return {"error": str(exc)}
            written = self._write_toml(LAYOUTS_PATH, dump_layouts(layouts))
            if written is not None:
                return written
            self.cmd_reload({})
            return {"ok": True, "saved": len(layouts), "file": LAYOUTS_PATH}
        if action == "save":
            try:
                effects = validate_effects(req.get("effects"))
            except (ValueError, KeyError) as exc:
                return {"error": str(exc)}
            written = self._write_toml(EFFECTS_PATH, dump_effects(effects))
            if written is not None:
                return written
            self.cmd_reload({})
            return {"ok": True, "saved": len(effects), "file": EFFECTS_PATH}
        return {"error": f"unknown config action: {action}"}

    @staticmethod
    def _write_toml(path: str, text: str) -> dict | None:
        """Atomically replace ``path``; returns an error reply, or None on success."""
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                fh.write(text)
            os.chmod(tmp, 0o644)
            os.replace(tmp, path)
        except OSError as exc:
            return {"error": f"cannot write {path}: {exc}"}
        return None

    def cmd_play(self, req: dict) -> dict:
        spec, key = self._resolve_spec(req)
        if isinstance(spec, dict) and "error" in spec:
            return spec
        return self._submit(key, spec, req)

    def cmd_trigger(self, req: dict) -> dict:
        event = str(req.get("event", ""))
        attrs = {k: v for k, v in req.items() if k not in ("cmd", "event")}
        rule = self.config.match(event, attrs)
        if rule is None:
            return {"ok": True, "result": "no-rule", "event": event}

        released = None
        if rule.get("release"):
            released = self.engine.release(str(rule["release"]))

        name = rule.get("effect")
        if not name:
            # A pure "release" rule -- e.g. the end of an authentication.
            if released is not None:
                return {"ok": True, "result": released}
            return {"error": f"trigger rule for {event} has no effect"}
        try:
            spec = self.config.effect(str(name))
        except KeyError as exc:
            return {"error": str(exc)}

        hold = rule.get("hold")
        key = str(hold) if isinstance(hold, str) and hold not in ("true", "false") else str(name)
        merged = dict(req)
        if hold:
            merged["hold"] = True
        if "timeout" in rule:
            merged["timeout"] = rule["timeout"]
        if "priority" in rule:
            merged["priority"] = rule["priority"]
        if "target" in rule:
            merged["target"] = rule["target"]
        reply = self._submit(key, spec, merged, rule=name)
        if released is not None:
            reply["released"] = released
        return reply

    def _resolve_spec(self, req: dict):
        if isinstance(req.get("spec"), dict):
            # Expanded here too, so a client may name a stored layout instead
            # of shipping every zone over the socket.
            spec = self.config.expand(dict(req["spec"]))
            if spec is None:
                return {"error": "unknown layout"}, ""
            return spec, str(req.get("key") or "adhoc")
        name = req.get("effect")
        if not name:
            return {"error": "play needs 'effect' or 'spec'"}, ""
        try:
            spec = self.config.effect(str(name))
        except KeyError as exc:
            return {"error": str(exc)}, ""
        if spec is None:
            # The effect names a layout that is no longer defined.
            return {"error": f"{name}: refers to a layout that does not exist"}, ""
        return spec, str(req.get("key") or name)

    def _submit(self, key: str, spec: dict, req: dict, rule: str | None = None) -> dict:
        # Per-request overrides let the CLI tweak a configured effect.
        for field in ("color", "times", "ms", "brightness", "speed", "value"):
            if field in req and req[field] is not None:
                spec[field] = req[field]

        # Where the overlay plays: the request wins over the effect, which wins
        # over the daemon-wide default.
        try:
            target = normalise_target(
                req.get("target") or spec.get("target")
                or self.config.daemon.get("default_target", "both"))
        except ValueError as exc:
            return {"error": str(exc)}

        hold = bool(req.get("hold", spec.get("hold", False)))
        priority = int(req.get("priority", spec.get("priority", 50)))
        timeout = req.get("timeout", spec.get("timeout"))
        if hold and timeout is None:
            timeout = self.config.daemon.get("hold_timeout", 60)
        # An explicit 0 means "until something releases it". A held effect that
        # quietly expires looks to the user like the lighting resetting itself,
        # so anything long-lived has to be able to opt out of the deadline.
        if timeout is not None and float(timeout) <= 0:
            timeout = None
        try:
            job = Job(key, spec, priority, hold,
                      float(timeout) if timeout else None, target=target)
        except ValueError as exc:   # unknown effect kind
            return {"error": str(exc)}

        result = self.engine.submit(job)
        reply = {"ok": True, "result": result, "key": key, "effect": rule or key,
                 "priority": priority, "hold": hold, "target": target}
        if target in ("bar", "both") and not self.engine.bar.available:
            # Queueing succeeded but nothing will be visible: say so rather
            # than let the caller believe the effect played.
            reply["warning"] = (f"{self.engine.bar.bar.sysfs} is missing -- the effect "
                                "will not be visible. Is omen_rgb_keyboard loaded?")
        if target in ("keys", "both") and not self.engine.keys.available:
            reply["warning"] = ("no HID LampArray found -- the keyboard part of "
                                "this effect will not be visible.")
        return reply


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="omen-fxd",
        description="OMEN lighting daemon: light bar and per-key keyboard")
    ap.add_argument("-c", "--config", default=CONFIG_PATH)
    ap.add_argument("-s", "--socket", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="log writes instead of touching the hardware")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    config = Config.load(args.config)
    set_bar_blend(config.daemon.get("bar_blend", 1.5))
    bar, keys = build_surfaces(config.daemon["sysfs"], dry_run=args.dry_run)
    if not bar.available:
        log.warning("%s not present -- is omen_rgb_keyboard loaded? bar effects "
                    "will start working as soon as it appears", bar.bar.sysfs)
    if not keys.available:
        log.warning("no HID LampArray found -- keyboard effects are disabled")

    # Started even when idle dimming is off: the config can be turned on from
    # the GUI at any moment, and a watcher that only began counting then would
    # report the machine as freshly used and wait out a whole timeout first.
    idle_watcher = IdleWatcher()
    idle_watcher.start()

    engine = Engine(bar, keys, config, idle_watcher=idle_watcher)
    engine.start()

    # The keyboard stays frozen on our last frame unless the firmware gets it
    # back, so make the hand-back unconditional -- a crash or a SIGKILL of the
    # parent must not leave the machine stuck mid-animation.
    atexit.register(keys.kb.close)

    if config.base.get("apply_on_start"):
        pinned = _load_pinned(config)
        if pinned is not None:
            try:
                bar.bar.restore(pinned)
            except DeviceUnavailable as exc:
                log.warning("could not apply base lighting: %s", exc)

    server = Server(config, engine, args.socket or config.daemon["socket"])
    log.info("listening on %s (bar=%s, lamps=%d)",
             server.path, bar.available, len(keys))

    def _term(_signum, _frame):
        engine.shutdown()
        server.sock.close()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)

    try:
        server.serve_forever()
    finally:
        engine.shutdown()
        engine.join(timeout=3)
        keys.kb.close()
        try:
            os.unlink(server.path)
        except OSError:
            pass
    return 0
