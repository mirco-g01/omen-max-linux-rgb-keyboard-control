"""HID LampArray backend for the per-key keyboard of an OMEN MAX.

The keyboard backlight is *not* the omen_rgb_keyboard driver's business: it is
a standard HID LampArray (HID Usage Page 0x59, the same thing Windows Dynamic
Lighting drives) sitting on interface 4 of the internal USB keyboard. Everything
here is plain hidraw feature reports, so no kernel module is involved.

Three hardware facts shape this module:

  * ``MinUpdateInterval`` is 33 ms, a hard 30 Hz ceiling. Pushing frames faster
    only wastes CPU -- the firmware coalesces them.
  * a full 120-lamp frame is 15 ``LampMultiUpdate`` reports and costs ~11.6 ms,
    so the transport has roughly 3x headroom over that ceiling.
  * **while we hold the array, the firmware mirrors the keyboard onto the light
    bar.** Anything the bar is meant to show independently has to be rewritten
    after every keyboard frame; see ``Engine`` in engine.py.
  * **the keyboard MCU has a lighting level of its own** (what F4 cycles:
    100% -> 60% -> off) that gates the light bar even while we hold the array.
    The keys keep showing what we paint, so nothing on the keyboard betrays it;
    only the bar goes dark, and it stays dark across a full power cycle because
    the level lives in the MCU's flash. The level is read and set on a vendor
    interface of the same USB device, with the frames OMEN Gaming Hub uses.

Every lamp reports its real position in micrometres, so effects are written as
continuous functions of space rather than against lamp indices.
"""

from __future__ import annotations

import array
import fcntl
import glob
import logging
import os
import select
import struct
import time

log = logging.getLogger("omen-fx.keys")

Color = tuple[int, int, int]

# HID Usage Page 0x59 (Lighting And Illumination), Usage 0x01 (LampArray):
# every LampArray report descriptor starts with these three bytes.
LAMPARRAY_PREFIX = b"\x05\x59\x09\x01"

# Report ids and their sizes, read straight off the report descriptor.
R_ATTRS, SZ_ATTRS = 1, 23
R_LAMP_REQ, SZ_LAMP_REQ = 2, 3
R_LAMP_RESP, SZ_LAMP_RESP = 3, 29
R_MULTI, SZ_MULTI = 4, 51
R_RANGE, SZ_RANGE = 5, 10
R_CONTROL, SZ_CONTROL = 6, 2

LAMPS_PER_MULTI = 8      # LampMultiUpdate carries at most 8 lamps
FLAG_COMPLETE = 1        # LampUpdateFlags: latch everything sent so far

# The MCU's own control channel: interface 3 of the same USB device, a vendor
# usage page (0xFF01) with 64-byte reports. Frames are taken from HP's McuSDK2
# (GeneralCommandHelper): [command, index, length, 0, data...].
VENDOR_PREFIX = b"\x06\x01\xff\x09\x01"
VENDOR_REPORT = 64
MCU_GET_DEVICE_INFO = bytes([0x80, 0x01, 0x00, 0x00])
MCU_LIGHTING_ON = bytes([0x09, 0x00, 0x01, 0x00, 0x01])   # SetKeyboardLightingOnOff(1)
MCU_LEVEL_INDEX = 12      # byte of the device-info reply: 100 + percent
MCU_LEVEL_FULL = 200
MCU_CHECK_S = 5.0         # how often a held keyboard re-checks the level


def _ioc(direction: int, letter: str, nr: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord(letter) << 8) | nr


def _hidiocgfeature(size: int) -> int:
    return _ioc(3, "H", 0x07, size)


def _hidiocsfeature(size: int) -> int:
    return _ioc(3, "H", 0x06, size)


class KeyboardUnavailable(RuntimeError):
    pass


def discover() -> str | None:
    """Find the hidraw node whose report descriptor declares a LampArray.

    hidraw numbering is not stable across boots or re-enumeration, so the node
    is identified by what it *is* rather than by name.
    """
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            with open(os.path.join(path, "device/report_descriptor"), "rb") as fh:
                if fh.read(4) == LAMPARRAY_PREFIX:
                    return "/dev/" + os.path.basename(path)
        except OSError:
            continue
    return None


def discover_vendor(lamparray: str | None) -> str | None:
    """The MCU's vendor interface on the same USB device as the LampArray."""
    if not lamparray:
        return None
    try:
        mine = os.path.realpath(f"/sys/class/hidraw/{os.path.basename(lamparray)}/device")
    except OSError:
        return None
    usb_device = os.path.dirname(os.path.dirname(mine))
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            dev = os.path.realpath(os.path.join(path, "device"))
            if os.path.dirname(os.path.dirname(dev)) != usb_device:
                continue
            with open(os.path.join(dev, "report_descriptor"), "rb") as fh:
                if fh.read(len(VENDOR_PREFIX)) == VENDOR_PREFIX:
                    return "/dev/" + os.path.basename(path)
        except OSError:
            continue
    return None


class Lamp:
    __slots__ = ("id", "x", "y", "z", "binding", "purpose")

    def __init__(self, lamp_id, x, y, z, binding, purpose):
        self.id = lamp_id
        self.x = x          # millimetres
        self.y = y
        self.z = z
        self.binding = binding   # HID keyboard usage id, 0 or 3 when unbound
        self.purpose = purpose

    def __repr__(self) -> str:
        return f"<Lamp {self.id} at ({self.x:.0f},{self.y:.0f})mm key={self.binding}>"


class Keyboard:
    """The per-key backlight, as a list of lamps with real positions."""

    def __init__(self, device: str | None = None, dry_run: bool = False):
        self.dry_run = dry_run
        self.device = device or (discover() if not dry_run else "/dev/null")
        self._fd = None
        self._lamps: list[Lamp] = []
        self._last: list[Color] | None = None
        self._acquired = False
        self.vendor: str | None = None
        self._vendor_fd = None
        self._vendor_warned = False
        self._level_checked = 0.0
        self.min_interval_ms = 33.0
        self.kind = 0
        self.bounds = (342.0, 125.0)

    # -- lifecycle -------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.dry_run or bool(self.device and os.path.exists(self.device))

    def open(self) -> None:
        if self._fd is not None or self.dry_run:
            if self.dry_run and not self._lamps:
                self._lamps = synthetic_lamps()
            return
        if not self.device:
            raise KeyboardUnavailable(
                "no HID LampArray found. This project targets the OMEN MAX, "
                "whose keyboard is one; on other OMEN models the per-key "
                "backlight may not be, and only the light bar will work. If "
                "yours is a MAX, please open an issue with the output of "
                "'lsusb' and 'ls /sys/class/hidraw/*/device/uevent'.")
        try:
            self._fd = open(self.device, "rb+", buffering=0)
        except PermissionError as exc:
            raise KeyboardUnavailable(
                f"{self.device}: {exc}. Install 99-omen-lamparray.rules and "
                "make sure you are in the 'input' group.") from None
        except OSError as exc:
            raise KeyboardUnavailable(f"{self.device}: {exc}") from None
        self._read_attributes()
        self._read_geometry()
        self.vendor = discover_vendor(self.device)
        log.info("LampArray on %s: %d lamps, %.0fx%.0f mm, max %.0f Hz",
                 self.device, len(self._lamps), *self.bounds,
                 1000.0 / self.min_interval_ms)

    def reattach(self) -> bool:
        """Re-find the LampArray after its device node moved or its fd died.

        hidraw numbering is not stable: a re-enumeration of the internal
        keyboard (suspend/resume, a firmware reset) hands us a new node and
        leaves the old fd pointing at nothing.  A long-lived daemon has to
        notice and follow, or it goes silently blind until it is restarted.
        """
        if self.dry_run:
            return True
        if self._fd is not None:
            try:
                self._fd.close()
            except OSError:
                pass
            self._fd = None
        # The firmware owns the array again after a re-enumeration, so the
        # acquire has to be redone and no frame can be assumed still on screen.
        self._acquired = False
        self._last = None
        self._close_vendor()
        found = discover()
        if not found:
            self.device = None
            return False
        if found != self.device:
            log.info("LampArray moved from %s to %s", self.device, found)
        self.device = found
        self.open()
        return True

    def close(self) -> None:
        try:
            self.release()
        finally:
            self._close_vendor()
            if self._fd is not None:
                try:
                    self._fd.close()
                except OSError:
                    pass
                self._fd = None

    def __enter__(self) -> "Keyboard":
        self.open()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- the MCU's own lighting level ------------------------------------

    def _close_vendor(self) -> None:
        if self._vendor_fd is not None:
            try:
                os.close(self._vendor_fd)
            except OSError:
                pass
            self._vendor_fd = None

    def _vendor_xfer(self, frame: bytes, timeout: float = 0.3) -> bytes | None:
        """One request/reply on the vendor interface, or None if it cannot."""
        if self.dry_run or not self.vendor:
            return None
        try:
            if self._vendor_fd is None:
                self._vendor_fd = os.open(self.vendor, os.O_RDWR | os.O_NONBLOCK)
            fd = self._vendor_fd
            # Drain anything the MCU volunteered since last time.
            while True:
                r, _, _ = select.select([fd], [], [], 0)
                if not r or not os.read(fd, VENDOR_REPORT):
                    break
            os.write(fd, b"\x00" + frame.ljust(VENDOR_REPORT, b"\x00"))
            end = time.monotonic() + timeout
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    return None
                r, _, _ = select.select([fd], [], [], left)
                if not r:
                    return None
                data = os.read(fd, VENDOR_REPORT)
                if data and data[0] == frame[0]:
                    return data
        except PermissionError as exc:
            if not self._vendor_warned:
                self._vendor_warned = True
                log.warning("%s: %s -- cannot keep the MCU's lighting on, so F4 "
                            "can still turn the light bar off", self.vendor, exc)
            self._close_vendor()
            return None
        except OSError as exc:
            log.debug("vendor interface %s: %s", self.vendor, exc)
            self._close_vendor()
            return None

    def firmware_level(self) -> int | None:
        """The MCU's lighting level in percent, or None if unreadable."""
        reply = self._vendor_xfer(MCU_GET_DEVICE_INFO)
        if reply is None or len(reply) <= MCU_LEVEL_INDEX:
            return None
        return max(0, reply[MCU_LEVEL_INDEX] - 100)

    def ensure_lit(self, force: bool = False) -> None:
        """Keep the MCU's own lighting at full while we hold the array.

        F4 is handled inside the MCU and steps that level down to off. The
        keys do not care -- we paint them -- but the light bar does, and the
        level survives reboots and power cycles, so a single press leaves the
        bar permanently dark under every effect. Checked at a low rate rather
        than on every frame: it is two 64-byte reports on another interface.
        """
        if not self.vendor or self.dry_run:
            return
        now = time.monotonic()
        if not force and now - self._level_checked < MCU_CHECK_S:
            return
        self._level_checked = now
        reply = self._vendor_xfer(MCU_GET_DEVICE_INFO)
        if reply is None or len(reply) <= MCU_LEVEL_INDEX:
            return
        if reply[MCU_LEVEL_INDEX] == MCU_LEVEL_FULL:
            return
        log.info("MCU lighting level was %d%%, turning it back on so the "
                 "light bar follows", max(0, reply[MCU_LEVEL_INDEX] - 100))
        self._vendor_xfer(MCU_LIGHTING_ON)

    # -- raw feature reports ---------------------------------------------

    def _get(self, report_id: int, size: int) -> bytes:
        buf = array.array("B", [0] * size)
        buf[0] = report_id
        try:
            fcntl.ioctl(self._fd, _hidiocgfeature(size), buf, True)
        except OSError as exc:
            raise KeyboardUnavailable(f"{self.device}: get report {report_id}: {exc}") from None
        return bytes(buf)

    def _set(self, payload: bytes) -> None:
        if self.dry_run:
            return
        buf = array.array("B", payload)
        try:
            fcntl.ioctl(self._fd, _hidiocsfeature(len(buf)), buf, True)
        except OSError as exc:
            raise KeyboardUnavailable(f"{self.device}: set report {payload[0]}: {exc}") from None

    # -- enumeration -----------------------------------------------------

    def _read_attributes(self) -> None:
        data = self._get(R_ATTRS, SZ_ATTRS)
        count, w, h, _d, kind, interval = struct.unpack("<HIIIII", data[1:23])
        self._count = count
        self.kind = kind
        self.bounds = (w / 1000.0, h / 1000.0)
        # Never poll faster than the firmware can absorb; guard against a
        # device reporting 0 so we do not end up dividing by it later.
        self.min_interval_ms = (interval / 1000.0) if interval else 33.0

    def _read_geometry(self) -> None:
        """Read every lamp's position, in lamp-id order.

        The list is sorted afterwards because this firmware does not always
        answer the question it was asked. A LampAttributesRequest names a lamp
        and the following response should describe it; measured on an OMEN MAX,
        the response can come back describing lamp *id + k* instead -- k was 5
        on one open of the device and 0 on the next, so it is a pipeline skew
        in the firmware, not a fixed quirk to hard-code.

        It matters because everything downstream -- the coordinates an effect
        is sampled at, and the lamp a colour is then written to -- keys off the
        position in this list. Left unsorted, a skew of five paints every key
        five places away from where the geometry says it is: the picture comes
        out rotated, sliding off one edge and wrapping onto the other.
        """
        lamps = []
        for lamp_id in range(self._count):
            self._set(struct.pack("<BH", R_LAMP_REQ, lamp_id))
            data = self._get(R_LAMP_RESP, SZ_LAMP_RESP)
            lid, x, y, z, _lat, purpose = struct.unpack("<HIIIII", data[1:23])
            binding = data[28]
            lamps.append(Lamp(lid, x / 1000.0, y / 1000.0, z / 1000.0, binding, purpose))

        ids = [l.id for l in lamps]
        if ids != sorted(ids):
            skew = next((i for i, lid in enumerate(ids) if lid != i), None)
            log.info("lamp attributes came back skewed (position 0 reported id "
                     "%s); sorting by lamp id", ids[0] if skew is not None else "?")
            lamps.sort(key=lambda l: l.id)
        if [l.id for l in lamps] != list(range(len(lamps))):
            # Sorting only rescues a permutation of 0..N-1. Anything else and
            # position no longer means lamp id, so say so instead of painting
            # a subtly wrong picture in silence.
            log.warning("lamp ids are not 0..%d as expected (%s...); positions "
                        "may not line up with the keys",
                        len(lamps) - 1, [l.id for l in lamps[:5]])
        self._lamps = lamps

    # -- geometry --------------------------------------------------------

    @property
    def lamps(self) -> list[Lamp]:
        return self._lamps

    def __len__(self) -> int:
        return len(self._lamps)

    def lamps_for_key(self, usage: int) -> list[int]:
        """Lamp ids bound to a HID keyboard usage; wide keys have several."""
        return [l.id for l in self._lamps if l.binding == usage]

    # -- output ----------------------------------------------------------

    def acquire(self) -> None:
        """Take the array away from the firmware's own animation."""
        if self._acquired:
            return
        self._set(struct.pack("<BB", R_CONTROL, 0))
        self._acquired = True
        self._last = None
        log.debug("LampArray acquired")
        self.ensure_lit(force=True)

    def release(self) -> None:
        """Hand the lighting back to the firmware.

        Must happen on every exit path: without it the keyboard stays frozen on
        whatever frame we wrote last.
        """
        if not self._acquired:
            return
        self._acquired = False
        self._last = None
        try:
            self._set(struct.pack("<BB", R_CONTROL, 1))
            log.debug("LampArray released")
        except KeyboardUnavailable as exc:
            log.warning("could not release the LampArray: %s", exc)

    @property
    def acquired(self) -> bool:
        return self._acquired

    def set_uniform(self, color: Color) -> None:
        """One report for the whole array -- far cheaper than a full frame."""
        if self.dry_run:
            self._last = [color] * len(self._lamps)
            return
        self.acquire()
        r, g, b = color
        self._set(struct.pack("<BBHHBBBB", R_RANGE, FLAG_COMPLETE,
                              0, len(self._lamps) - 1, r, g, b, 255))
        self._last = [color] * len(self._lamps)

    def set_frame(self, colors, force: bool = False) -> bool:
        """Push one frame. Returns True if anything was actually written.

        The frame is split across ``LampMultiUpdate`` reports and only the last
        one carries ``LampUpdateComplete``, so the firmware latches the whole
        frame at once instead of showing it torn.
        """
        self.ensure_lit()
        colors = list(colors)
        n = len(self._lamps)
        if len(colors) < n:
            colors += [(0, 0, 0)] * (n - len(colors))
        colors = colors[:n]

        if not force and self._last == colors:
            return False
        if self.dry_run:
            self._last = colors
            return True

        self.acquire()
        if len(set(colors)) == 1:
            self.set_uniform(colors[0])
            return True

        for off in range(0, n, LAMPS_PER_MULTI):
            chunk = colors[off:off + LAMPS_PER_MULTI]
            ids = [0] * LAMPS_PER_MULTI
            payload = [0] * (LAMPS_PER_MULTI * 4)
            for k, color in enumerate(chunk):
                # The lamp's own id, not its place in the list. They agree once
                # _read_geometry has sorted, and this keeps them agreeing if a
                # future firmware hands back something stranger than a rotation.
                ids[k] = self._lamps[off + k].id
                payload[4 * k:4 * k + 4] = [color[0], color[1], color[2], 255]
            last = off + LAMPS_PER_MULTI >= n
            self._set(struct.pack("<BBB", R_MULTI, len(chunk),
                                  FLAG_COMPLETE if last else 0)
                      + struct.pack("<8H", *ids) + bytes(payload))
        self._last = colors
        return True


def synthetic_lamps() -> list[Lamp]:
    """A 6x20 stand-in matching the real OMEN MAX layout, for --dry-run."""
    lamps = []
    ys = (9.0, 25.0, 44.0, 62.0, 81.0, 99.0)
    for row, y in enumerate(ys):
        for col in range(20):
            lamps.append(Lamp(row * 20 + col, 12.0 + col * 16.6, y, 0.0, 0, 1))
    return lamps
