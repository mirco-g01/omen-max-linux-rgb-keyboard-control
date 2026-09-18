"""omen-fx -- command line front end for the effect daemon."""

from __future__ import annotations

import argparse
import json
import sys

from . import effects as fx
from .client import ClientError, send
from .config import Config, CONFIG_PATH, SOCKET_PATH
from .engine import TARGETS


def _print(reply: dict, raw: bool) -> int:
    if reply.get("warning") and not raw:
        print(f"warning: {reply['warning']}", file=sys.stderr)
    if raw:
        print(json.dumps(reply, indent=2))
    elif "error" in reply:
        print(f"error: {reply['error']}", file=sys.stderr)
    elif reply.get("result") == "no-rule":
        print(f"no trigger rule matched event {reply.get('event')!r}")
    elif "status" in reply:
        st = reply["status"]
        print(f"bar        {st['device']}"
              f"{'  (MISSING)' if not st['available'] else ''}"
              f"{'  [dry-run]' if st['dry_run'] else ''}")
        print(f"keyboard   {st.get('keyboard') or '-'}"
              f"  {st.get('lamps', 0)} lamps"
              f"{'  (held)' if st.get('keyboard_held') else ''}"
              f"{'  (MISSING)' if not st.get('keyboard_available') else ''}")
        active = st.get("active") or {}
        print(f"playing    bar: {active.get('bar') or '-'}"
              f"   keys: {active.get('keys') or '-'}")
        print(f"queued     {', '.join(st.get('queued') or []) or '-'}")
        fading = {k: v for k, v in (st.get("fading") or {}).items() if v}
        if fading:
            print("fading     " + "  ".join(f"{k}: {v} ms" for k, v in fading.items()))
        if st.get("master_brightness") is not None:
            print(f"brightness {st['master_brightness']}%  (keyboard backlight slider)")
        if st.get("power"):
            print(f"power      {st['power']}")
        idle = st.get("idle") or {}
        if idle:
            state = ("on" if idle.get("enabled") else "off")
            if idle.get("enabled") and not idle.get("available"):
                state = "on but blind (cannot read /dev/input)"
            elif idle.get("enabled") and idle.get("wake_for_alerts"):
                state += ", alerts wake it"
            print(f"idle dim   {state}"
                  + (f"   idle for {idle['seconds']}s, at {idle['level']}%"
                     if idle.get("seconds") is not None else ""))
        profiles = st.get("base") or {}
        print("default    " + "  ".join(
            f"{n}: {profiles.get(n) or '-'}" for n in ("bar", "keys")))
        if st.get("last_error"):
            print(f"last error {st['last_error']}")
        print(f"config     {reply.get('config')}")
        if reply.get("config_error"):
            print(f"           ⚠ {reply['config_error']}")
        print(f"effects    {', '.join(reply.get('effects', [])) or '-'}")
    elif "base" in reply:
        b = reply["base"]
        print(f"zones      {' '.join(b['zones'])}")
        print(f"brightness {b['brightness']}")
        print(f"animation  {b['mode']} @ speed {b['speed']}")
    else:
        bits = [f"{k}={v}" for k, v in reply.items() if k not in ("ok", "warning")]
        print(" ".join(bits) if bits else "ok")
    return 1 if "error" in reply else 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="omen-fx",
        description="Play effects on the OMEN light bar and keyboard via omen-fxd.")
    ap.add_argument("-S", "--socket", default=SOCKET_PATH)
    ap.add_argument("--json", action="store_true", help="print the raw reply")
    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="what the daemon is doing")
    sub.add_parser("stop", help="cancel everything and restore the base lighting")
    sub.add_parser("reload", help="re-read the config file")
    sub.add_parser("ping", help="check the daemon is alive")

    p = sub.add_parser("play", help="play an effect from the config file")
    p.add_argument("effect")
    _add_overrides(p)

    p = sub.add_parser("fx", help="play an ad-hoc effect without touching the config")
    # Aliases still work, but only the canonical names are advertised.
    p.add_argument("kind", choices=list(fx.CANONICAL) + list(fx.ALIASES),
                   metavar="{" + ",".join(fx.CANONICAL) + "}")
    _add_overrides(p)
    p.add_argument("--set", action="append", default=[], metavar="K=V",
                   help="any other effect parameter, e.g. --set step_ms=60")

    p = sub.add_parser("trigger", help="fire an event and let the rules pick the effect")
    p.add_argument("event")
    p.add_argument("--app")
    p.add_argument("--summary")
    p.add_argument("--body")
    p.add_argument("--urgency", type=int)
    p.add_argument("--service")
    p.add_argument("--user")
    p.add_argument("--target", choices=list(TARGETS))

    p = sub.add_parser("release", help="end an effect that is being held open")
    p.add_argument("key")

    p = sub.add_parser("base", help="inspect or pin the lighting to return to")
    p.add_argument("action", nargs="?", default="show",
                   choices=["show", "pin", "clear", "apply"])

    p = sub.add_parser("demo", help="play every effect in the config, one by one")
    p.add_argument("-c", "--config", default=CONFIG_PATH)
    p.add_argument("--pause", type=float, default=1.0)

    sub.add_parser("effects", help="list the effects defined in the config")
    sub.add_parser("layouts", help="list the zone layouts and what is in them")
    return ap


def _add_overrides(p: argparse.ArgumentParser) -> None:
    p.add_argument("--color")
    p.add_argument("--times", type=int)
    p.add_argument("--ms", type=int)
    p.add_argument("--speed", type=int)
    p.add_argument("--value", type=int, help="0-100, for the 'progress' effect")
    p.add_argument("--brightness", type=int)
    p.add_argument("--priority", type=int)
    p.add_argument("--hold", action="store_true",
                   help="keep the effect running until 'omen-fx release KEY'")
    p.add_argument("--timeout", type=float, help="seconds before a held effect gives up")
    p.add_argument("--key", help="name for this effect, used by 'release'")
    p.add_argument("--target", choices=list(TARGETS),
                   help="where to play it: the bar, the keyboard, or both")


def _coerce(text: str):
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    if text.lower() in ("true", "false"):
        return text.lower() == "true"
    return text


def main(argv=None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print()  # Ctrl+C is a normal way to stop 'demo'
        return 130


def _main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.command

    if cmd == "demo":
        return _demo(args)

    req: dict = {"cmd": cmd}
    if cmd == "layouts":
        req = {"cmd": "config", "action": "get"}
    elif cmd in ("status", "stop", "reload", "ping", "effects"):
        req["cmd"] = "status" if cmd == "effects" else cmd
    elif cmd == "release":
        req["key"] = args.key
    elif cmd == "base":
        req["action"] = args.action
    elif cmd == "trigger":
        req["event"] = args.event
        for field in ("app", "summary", "body", "urgency", "service",
                      "user", "target"):
            value = getattr(args, field)
            if value is not None:
                req[field] = value
    elif cmd == "play":
        req["effect"] = args.effect
        _apply_overrides(req, args)
    elif cmd == "fx":
        spec: dict = {"kind": args.kind}
        for item in args.set:
            key, _, value = item.partition("=")
            spec[key.strip()] = _coerce(value.strip())
        req["cmd"] = "play"   # "fx" is a CLI shorthand for an ad-hoc play
        req["spec"] = spec
        req["key"] = args.key or f"cli-{args.kind}"
        _apply_overrides(req, args)

    try:
        reply = send(req, args.socket)
    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if cmd == "effects" and not args.json:
        print("\n".join(reply.get("effects", [])) or "(none)")
        return 0
    if cmd == "layouts" and not args.json:
        return _print_layouts(reply)
    return _print(reply, args.json)


def _print_layouts(reply: dict) -> int:
    """One line per zone, in the order they paint -- which is their priority."""
    layouts = reply.get("layouts") or {}
    if not layouts:
        print("(no layouts)  make the first one in omen-fx-gui, Zones tab")
        return 0
    for name in sorted(layouts):
        spec = layouts[name] or {}
        zones = spec.get("zones") or []
        print(f"{name}   bar: {spec.get('bar_source', 'projection')}")
        for i, zone in enumerate(zones, 1):
            cells = sum(str(row).count("1") for row in (zone.get("mask") or []))
            bar = str(zone.get("bar") or "").count("1")
            where = f"{cells} keys" + (f" + {bar} segments" if bar else "")
            print(f"  {i}. {zone.get('name') or '(unnamed)':<16} "
                  f"{zone.get('kind', 'solid'):<10} {where}")
        print()
    return 0


def _apply_overrides(req: dict, args) -> None:
    for field in ("color", "times", "ms", "speed", "value", "brightness",
                  "priority", "timeout", "key", "target"):
        value = getattr(args, field, None)
        if value is not None:
            req[field] = value
    if getattr(args, "hold", False):
        req["hold"] = True


def _demo(args) -> int:
    import time

    config = Config.load(args.config)
    if not config.effects:
        print(f"no effects defined in {args.config}", file=sys.stderr)
        return 1
    try:
        status = send({"cmd": "status"}, args.socket).get("status", {})
    except ClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not status.get("available"):
        print(f"error: {status.get('device')} is missing -- nothing would be visible.\n"
              f"       Load the driver first:  sudo modprobe omen_rgb_keyboard",
              file=sys.stderr)
        return 1
    for name, spec in config.effects.items():
        print(fx.describe(name, spec))
        req = {"cmd": "play", "effect": name, "priority": 90}
        if spec.get("hold") or spec.get("kind") == "kernel":
            req.update(hold=False, ms=1500)
        try:
            send(req, args.socket)
        except ClientError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        time.sleep(args.pause + 2.0)
    send({"cmd": "stop"}, args.socket)
    return 0
