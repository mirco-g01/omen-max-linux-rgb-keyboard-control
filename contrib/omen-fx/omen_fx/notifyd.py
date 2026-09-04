"""omen-fx-notifyd -- turns desktop notifications into light bar events.

Runs as a *user* service: the notifications live on the session bus, which the
root daemon cannot see. It becomes a D-Bus monitor for
``org.freedesktop.Notifications.Notify`` and forwards app / summary / urgency
to omen-fxd, where the trigger rules decide what to play.
"""

from __future__ import annotations

import argparse
import logging
import time

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

from .client import send_quiet
from .config import SOCKET_PATH

log = logging.getLogger("omen-fx.notifyd")

MATCH = ("type='method_call',interface='org.freedesktop.Notifications',"
         "member='Notify'")
# BecomeMonitor rejects (or ignores) the eavesdrop flag; only the legacy
# add_match fallback wants it.
LEGACY_MATCH = MATCH + ",eavesdrop='true'"
URGENCY_HINT = "urgency"


class Watcher:
    def __init__(self, socket_path: str, min_interval: float, ignore: list[str]):
        self.socket_path = socket_path
        self.min_interval = min_interval
        self.ignore = [a.lower() for a in ignore]
        self._last = 0.0
        # Kept on the instance: a private BusConnection that goes out of scope
        # is closed, and the filter silently stops firing.
        self.bus: dbus.Bus | None = None

    def connect(self) -> None:
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = self.bus = dbus.SessionBus(private=True)
        bus.add_message_filter(self.on_message)
        try:
            monitoring = dbus.Interface(
                bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus"),
                "org.freedesktop.DBus.Monitoring")
            monitoring.BecomeMonitor([MATCH], dbus.UInt32(0))
            log.info("monitoring notifications on the session bus")
        except dbus.DBusException as exc:
            # Older buses: fall back to the deprecated eavesdropping match.
            log.warning("BecomeMonitor failed (%s), falling back to eavesdrop", exc)
            bus.add_match_string(LEGACY_MATCH)

    def on_message(self, _bus, message) -> None:
        try:
            if message.get_member() != "Notify":
                return
            args = message.get_args_list()
        except Exception:
            return

        now = time.monotonic()
        if now - self._last < self.min_interval:
            return

        app = str(args[0]) if len(args) > 0 else ""
        summary = str(args[3]) if len(args) > 3 else ""
        body = str(args[4]) if len(args) > 4 else ""
        urgency = 1
        if len(args) > 6 and isinstance(args[6], dict):
            try:
                urgency = int(args[6].get(URGENCY_HINT, 1))
            except (TypeError, ValueError):
                urgency = 1

        if app.lower() in self.ignore:
            return

        self._last = now
        log.debug("notification from %r urgency=%s: %s", app, urgency, summary)
        send_quiet({"cmd": "trigger", "event": "notification", "app": app,
                    "summary": summary, "body": body, "urgency": urgency},
                   self.socket_path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="omen-fx-notifyd",
                                 description="Forward desktop notifications to omen-fxd")
    ap.add_argument("-S", "--socket", default=SOCKET_PATH)
    ap.add_argument("--min-interval", type=float, default=0.4,
                    help="seconds to wait before reacting to another notification")
    ap.add_argument("--ignore", action="append", default=[],
                    help="app name to ignore (repeatable)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")

    watcher = Watcher(args.socket, args.min_interval, args.ignore)
    watcher.connect()
    GLib.MainLoop().run()
    return 0
