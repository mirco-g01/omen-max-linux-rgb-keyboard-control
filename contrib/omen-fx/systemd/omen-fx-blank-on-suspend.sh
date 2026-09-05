#!/bin/sh
# Turn the keyboard off while the machine sleeps.
#
# Only needed alongside acpi_x86.sleep_no_lps0=1, the workaround for the Fn row
# dying after suspend (see docs/modern-standby-fn-keys.md). That parameter stops
# the kernel announcing modern standby to the firmware, which is what keeps the
# Fn keys alive -- but it also means the EC never gets the screen-off
# notification, so it never turns the backlight off. We do it instead.
#
# This goes through omen-fx rather than writing to the hardware: omen-fxd owns
# the LampArray while it runs, so a direct write would simply be painted over.
# Writing 0 to /sys/class/leds/omen::kbd_backlight is not enough either -- that
# is a colour scaler, not an off switch.
#
# Install:
#   sudo install -m755 omen-fx-blank-on-suspend.sh \
#        /usr/lib/systemd/system-sleep/omen-fx-blank-on-suspend.sh
#
# Note that systemd runs *every* executable file in that directory, in parallel.
# Never leave a backup copy there with the executable bit set.

OMEN_FX="${OMEN_FX:-/usr/local/bin/omen-fx}"
KEY=suspend-blank

[ -x "$OMEN_FX" ] || exit 0

case "$1" in
    pre)
        # A held effect, so nothing else repaints over it on the way down.
        "$OMEN_FX" fx solid --color 000000 --target keys \
            --hold --key "$KEY" >/dev/null 2>&1
        ;;
    post)
        # Releasing restores whatever the base lighting was.
        "$OMEN_FX" release "$KEY" >/dev/null 2>&1
        ;;
esac

exit 0
