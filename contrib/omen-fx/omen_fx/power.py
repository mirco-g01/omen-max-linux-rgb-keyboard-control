"""Whether the machine is on battery or plugged in.

The answer is in sysfs: every ``/sys/class/power_supply/*`` of type ``Mains``
has an ``online`` flag, and the machine is on battery when none of them is set.
That is the same file the desktop reads, so the two never disagree.

Learning about a *change* is the interesting part. The kernel announces one as
a uevent on the ``power_supply`` subsystem, and a netlink socket receives those
with no polling at all -- which matters because the moment somebody pulls the
plug is exactly when nobody is at the machine to wake the render loop by any
other means. Where the socket cannot be opened (a container, an odd kernel) the
watch falls back to reading the flag every few seconds, which is late but never
wrong.
"""

from __future__ import annotations

import glob
import logging
import os
import select
import socket
import threading

log = logging.getLogger("omen-fx.power")

POWER_SUPPLY = "/sys/class/power_supply"
NETLINK_KOBJECT_UEVENT = 15
UEVENT_GROUP = 1
POLL_S = 5.0            # fallback only, when the netlink socket is unavailable


def on_battery() -> bool | None:
    """True on battery, False on mains, None when sysfs says nothing at all.

    A desktop machine has no battery and no ``Mains`` supply either; treating
    that as "plugged in" is the right reading of it, and the engine does.
    """
    mains = []
    for path in glob.glob(os.path.join(POWER_SUPPLY, "*")):
        try:
            with open(os.path.join(path, "type")) as fh:
                kind = fh.read().strip()
            if kind != "Mains":
                continue
            with open(os.path.join(path, "online")) as fh:
                mains.append(fh.read().strip() == "1")
        except OSError:
            continue
    if not mains:
        return None
    return not any(mains)


class PowerWatcher(threading.Thread):
    """Tracks ``on_battery``; ``on_change`` is called from this thread."""

    daemon = True

    def __init__(self, on_change=None):
        super().__init__(name="power")
        self.on_change = on_change
        self.running = True
        self.available = False
        state = on_battery()
        self.known = state is not None
        self._on_battery = bool(state)
        self._wake_r, self._wake_w = os.pipe()

    @property
    def on_battery(self) -> bool:
        return self._on_battery

    def _refresh(self) -> None:
        state = on_battery()
        if state is None:
            return
        self.known = True
        if state == self._on_battery:
            return
        self._on_battery = state
        log.info("now on %s", "battery" if state else "mains")
        callback = self.on_change
        if callback is None:
            return
        try:
            callback(state)
        except Exception:       # a listener must never take the watch down
            log.exception("power callback failed")

    def _open_netlink(self):
        try:
            sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW,
                                 NETLINK_KOBJECT_UEVENT)
            sock.bind((os.getpid(), UEVENT_GROUP))
            sock.setblocking(False)
            return sock
        except OSError as exc:
            log.warning("no uevent socket (%s): polling the power supply "
                        "every %.0fs instead", exc, POLL_S)
            return None

    def run(self) -> None:
        sock = self._open_netlink()
        self.available = sock is not None
        timeout = None if sock is not None else POLL_S
        watched = [self._wake_r] + ([sock] if sock is not None else [])
        while self.running:
            try:
                ready, _, _ = select.select(watched, [], [], timeout)
            except OSError as exc:
                log.warning("power watch stopped: %s", exc)
                break
            if self._wake_r in ready:
                break               # stop(); nothing else ever writes that pipe
            if sock is not None and sock in ready:
                # Every uevent on the machine arrives here -- USB, input, the
                # lot -- so only the subsystem is looked at, and even that is
                # just a hint: the flag itself is what gets read.
                try:
                    data = sock.recv(8192)
                except OSError:
                    continue
                if b"SUBSYSTEM=power_supply" not in data:
                    continue
            self._refresh()
        if sock is not None:
            sock.close()
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def stop(self) -> None:
        self.running = False
        try:
            os.write(self._wake_w, b"\x00")
        except OSError:
            pass
