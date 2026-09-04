"""How long since a human last touched the machine.

Read from evdev rather than from the desktop, for one reason: there is no idle
signal that every desktop provides. KDE, GNOME, sway and a bare TTY each answer
differently or not at all, while the kernel always knows. Reading the devices
also means the answer is the same whether the screen is locked, a game has the
compositor's attention, or nothing graphical is running at all.

Only the *time* of an event is taken. Events are read because that is the only
way the kernel offers to notice one, and then discarded unlooked-at: nothing
here decodes which key, which button, or where the pointer went.

The watch costs nothing while nobody is there -- it waits with no timeout, so a
machine at rest never wakes this thread -- and it deliberately learns about
input at a coarse resolution: after an event it stands back for GRACE_S rather
than following every report of a moving mouse. A thousand events a second and
one a second answer the same question identically.
"""

from __future__ import annotations

import glob
import logging
import os
import select
import threading
import time

log = logging.getLogger("omen-fx.idle")

GRACE_S = 0.5           # how long to stand back after an event; see the docstring

EV_KEY, EV_REL, EV_ABS = 0x01, 0x02, 0x03
KEY_A = 30              # any keyboard has it; a power button does not
KEY_VOLUMEUP = 115      # the media-key device, so brightness keys count too
REL_X = 0x00
BTN_TOUCH = 0x14A


def _bits(sysfs: str, name: str) -> list[int]:
    try:
        with open(os.path.join(sysfs, "capabilities", name)) as fh:
            return [int(word, 16) for word in fh.read().split()][::-1]
    except OSError:
        return []


def _declares(words: list[int], code: int) -> bool:
    word, bit = divmod(code, 64)
    return word < len(words) and bool((words[word] >> bit) & 1)


def find_devices() -> list[str]:
    """Keyboards, pointers and media keys -- the things a person actually uses.

    Deliberately not "every input device": lid switches, power buttons,
    accelerometers and the video bus all emit on their own and would keep the
    machine looking busy while nobody is there.
    """
    found = []
    for sysfs in sorted(glob.glob("/sys/class/input/event*/device")):
        node = "/dev/input/" + os.path.basename(os.path.dirname(sysfs))
        keys, rel, absolute = _bits(sysfs, "key"), _bits(sysfs, "rel"), _bits(sysfs, "abs")
        if (_declares(keys, KEY_A) or _declares(keys, KEY_VOLUMEUP)
                or _declares(rel, REL_X) or _declares(keys, BTN_TOUCH)
                or _declares(absolute, 0)):
            found.append(node)
    return found


class IdleWatcher(threading.Thread):
    """Tracks seconds since the last input; ``seconds`` is safe to poll."""

    daemon = True

    def __init__(self, on_input=None):
        super().__init__(name="idle")
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.running = True
        self.devices: list[str] = []
        self.available = False
        # Called from this thread on the first event of a burst. It is how the
        # render loop hears that someone is back without polling for it.
        self.on_input = on_input
        # A pipe to interrupt the wait, so the wait itself needs no timeout:
        # with nothing to expire, a machine nobody is touching costs this
        # thread not one wakeup.
        self._wake_r, self._wake_w = os.pipe()

    @property
    def seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last

    def touch(self) -> None:
        with self._lock:
            self._last = time.monotonic()

    def _announce(self) -> None:
        callback = self.on_input
        if callback is None:
            return
        try:
            callback()
        except Exception:       # a listener must never take the watch down
            log.exception("input callback failed")

    def run(self) -> None:
        # Raw non-blocking descriptors: a device is drained with one read that
        # must not block, and a buffered file object would happily wait for the
        # rest of the 4 KiB that is never coming.
        streams = {}
        for node in find_devices():
            try:
                streams[os.open(node, os.O_RDONLY | os.O_NONBLOCK)] = node
            except OSError as exc:
                log.debug("cannot watch %s: %s", node, exc)
        self.devices = list(streams.values())
        self.available = bool(streams)
        if not streams:
            log.warning("no input device is readable: idle dimming is off "
                        "(the daemon needs read access to /dev/input)")
            self._close(streams)
            return
        log.info("idle watch on %d input devices", len(streams))

        while self.running:
            try:
                # No timeout: there is nothing to check on a schedule, so this
                # waits on the devices and on stop()'s pipe and on nothing else.
                ready, _, _ = select.select(list(streams) + [self._wake_r], [], [])
            except OSError as exc:
                log.warning("idle watch stopped: %s", exc)
                break
            if self._wake_r in ready:
                break               # stop(); nothing else ever writes that pipe
            for fd in ready:
                try:
                    # Drained, not parsed. The event's only useful part here is
                    # that it happened.
                    while os.read(fd, 4096):
                        pass
                except BlockingIOError:
                    pass
                except OSError as exc:
                    log.debug("dropping %s: %s", streams[fd], exc)
                    os.close(fd)
                    del streams[fd]
                    break
            self.touch()
            self._announce()
            if not streams:
                self.available = False
                break
            # One event says as much as a thousand: the machine is in use
            # either way. Standing back here, rather than going straight back
            # to select(), is what keeps a moving mouse from waking this thread
            # at its report rate to learn something it already knows -- the
            # events pile up in the kernel's own buffers and the next pass
            # drains the lot. stop() still cuts it short.
            select.select([self._wake_r], [], [], GRACE_S)
        self._close(streams)

    def _close(self, streams: dict) -> None:
        for fd in list(streams) + [self._wake_r, self._wake_w]:
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
