#!/usr/bin/env bash
# Load the freshly built module and measure what the patch actually bought.
#
# Nothing is installed by this script: the DKMS module on disk is untouched, so
# a reboot puts the previous driver back. Run it with sudo.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
KO="$REPO/src/omen_rgb_keyboard.ko"
Z=/sys/devices/platform/omen-rgb-keyboard/rgb_zones
F=/sys/devices/platform/omen-rgb-keyboard/fan

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
[[ -f "$KO" ]] || { echo "build it first:  make -C $REPO/src"; exit 1; }

fanstate() {
  for f in max_fan thermal_profile fan_curve_enable; do
    printf "  %-18s %s\n" "$f" "$(cat "$F/$f" 2>/dev/null | head -1)"
  done
  printf "  %-18s %s\n" platform_profile \
    "$(cat /sys/firmware/acpi/platform_profile 2>/dev/null)"
}

echo "=== fans/power BEFORE ==="
fanstate

systemctl stop omen-fxd 2>/dev/null

echo
echo "=== reloading the module ==="
rmmod omen_rgb_keyboard 2>/dev/null && echo "  vecchio modulo scaricato"
if ! insmod "$KO"; then
  echo "  insmod FALLITO -- rimetto quello installato"
  modprobe omen_rgb_keyboard
  systemctl start omen-fxd
  exit 1
fi
echo "  new module loaded, version $(modinfo -F version "$KO")"
sleep 1

echo
echo "=== fans/power AFTER (must be identical) ==="
fanstate

echo
if [[ -e $Z/zones ]]; then
  echo "=== attributo 'zones' presente ==="
else
  echo "=== the 'zones' attribute is MISSING -- something is wrong ==="
  exit 1
fi
chgrp input "$Z"/zones "$Z"/zone0* "$Z"/all "$Z"/brightness "$Z"/animation_* 2>/dev/null

echo
echo "=== benchmark (before the patch: 1 zone 261 ms, 4 zones 860 ms) ==="
python3 - <<'PY'
import time

Z = "/sys/devices/platform/omen-rgb-keyboard/rgb_zones"


def w(name, value):
    with open(f"{Z}/{name}", "w") as fh:
        fh.write(value)


def bench(label, fn, n=5):
    fn(0)
    start = time.monotonic()
    for i in range(1, n + 1):
        fn(i)
    print(f"  {label:34} {(time.monotonic() - start) / n * 1000:7.1f} ms")


bench("one zone write", lambda i: w("zone00", f"{i * 40 % 256:02x}0000"))
bench("four separate zone writes",
      lambda i: [w(f"zone{z:02d}", f"{(i * 40 + z * 10) % 256:02x}0000") for z in range(4)])
bench("one 'zones' write (new)",
      lambda i: w("zones", " ".join(f"{(i * 40 + z * 10) % 256:02x}0000" for z in range(4))))
bench("scrittura 'all'", lambda i: w("all", f"{i * 40 % 256:02x}0000"))
PY

for i in 00 01 02 03; do echo 000000 > "$Z/zone$i"; done
echo 1 > "$Z/brightness"
echo static > "$Z/animation_mode"

systemctl start omen-fxd
echo
echo "=== omen-fxd: $(systemctl is-active omen-fxd) ==="
