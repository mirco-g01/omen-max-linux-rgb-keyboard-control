# The Fn row dies after suspend, and only a power cycle brings it back

On some OMEN laptops, after resuming from suspend:

* the **keyboard backlight is dead** and nothing turns it back on;
* **F2, F3 and F4** (screen brightness down/up, keyboard backlight) stop working
  and instead open System Settings, or do nothing, depending on the desktop;
* every other key — including the other Fn-row keys such as mute and volume —
  keeps working perfectly;
* a **reboot does not fix it**. Only a full power off brings the keys back.

This is not a hardware fault, and it is not a bug in this driver. This document
explains what actually happens, how to confirm you have the same problem, and
how to stop it.

The whole thing was diagnosed on one machine (OMEN MAX 16-ah0xxx, keyboard
`0d62:54bf`, Arch Linux, kernel 7.1). The mechanism is generic, but the
verification steps below matter: please confirm rather than assume.

## What is actually happening

The keyboard is an internal USB device with its own microcontroller. It can
handle the Fn row in two ways:

* **firmware-handled** — the MCU acts on its own. F2/F3 emit standard HID
  consumer-control brightness codes, and F4 changes the backlight *internally*,
  emitting nothing at all on the bus.
* **host-handled** — the MCU stops acting. All three keys emit one generic
  sentinel key and wait for host software to do the work. On Windows that
  software is OMEN Gaming Hub. On Linux there is nobody listening, so the keys
  do nothing and the backlight is never driven.

The switch is triggered by the **ACPI LPS0 `_DSM`**, the Modern Standby
handshake. Entering s2idle, the kernel calls it on `\_SB.PEPD`
(`acpi_s2idle_prepare_late()` → `SCREEN_OFF(3)`, `MS_ENTRY(7)`, `ENTRY(5)`) to
tell the firmware and the EC that the system is entering modern standby. The HP
firmware responds by handing the Fn row to host software that does not exist
here.

The mode lives in the MCU's RAM. A warm reboot does not clear it, because the
MCU never loses power — which is exactly why only a full power cycle helps.

Measured, on the same keys, before and after:

| key | working | broken |
| --- | --- | --- |
| F2 | `0x000c0070` `KEY_BRIGHTNESSDOWN` on the consumer-control interface | `0x00070068` (F13) on the boot-keyboard interface |
| F3 | `0x000c006f` `KEY_BRIGHTNESSUP` on the consumer-control interface | `0x00070068` (F13) |
| F4 | **no event at all** — handled inside the MCU | `0x00070068` (F13) |
| F5 | `0x000c00e2` `KEY_MUTE` | unchanged |
| Fn+F4 | `0x0007003d` `KEY_F4` | unchanged |

Three different keys collapsing onto one code is the signature. `KEY_F13` maps
to the `XF86Tools` keysym, which is why KDE opens System Settings.

## Confirming you have this

`scripts/omen-kbd-suspend-test` does both checks. It needs root, and a kernel
built with `CONFIG_PM_DEBUG` for the second one.

```bash
# Is it reproducible on demand? Suspends for 5 s and re-checks.
sudo scripts/omen-kbd-suspend-test trial

# Which layer of the suspend sequence breaks it?
sudo scripts/omen-kbd-suspend-test ladder
```

The ladder uses `/sys/power/pm_test` to walk the suspend sequence, stopping at
increasing depth, and checks the keyboard after each step. On the affected
machine it passes `freezer` and `devices` and breaks at `platform` — which
rules out the USB suspend/resume path entirely and points at the ACPI layer,
where the LPS0 `_DSM` lives.

Note that a level that passes costs nothing, so the ladder can walk all the way
up in one go; the first level that breaks ends the run, because from there you
need a power cycle before testing again.

## The fix

Stop the kernel announcing modern standby to the firmware:

```
acpi_x86.sleep_no_lps0=1
```

Add it to your kernel command line. With systemd-boot, append it to the
`options` line of your loader entry; with GRUB, to `GRUB_CMDLINE_LINUX_DEFAULT`
and re-run `grub-mkconfig`. Then reboot and check:

```bash
cat /sys/module/acpi_x86/parameters/sleep_no_lps0   # must print Y
```

On kernels older than ~6.5 the parameter lives in the `acpi` module instead, so
use `acpi.sleep_no_lps0=1` and check
`/sys/module/acpi/parameters/sleep_no_lps0`.

It can also be flipped at runtime, without rebooting, which is the quickest way
to test it before committing to it:

```bash
echo Y | sudo tee /sys/module/acpi_x86/parameters/sleep_no_lps0
```

### What it costs

Two things, both measured rather than estimated:

* **S0ix residency drops from ~99% to ~96%** on this machine
  (`/sys/power/suspend_stats/last_hw_sleep` against the wall-clock length of the
  suspend). The SoC still reaches its deep state almost all of the time.
* **The keyboard backlight stays lit for the whole suspend.** The EC never
  receives the screen-off notification, so it never turns off what it would
  normally turn off. This is the more annoying half, and it is worth fixing —
  see below.

Measuring the actual power draw needs a full night on battery: on a
half-hour suspend this laptop's charge gauge does not move enough to tell the
two configurations apart.

## Turning the backlight off yourself

Since the firmware is no longer told that the screen went off, do it from a
systemd-sleep hook.

If nothing else is driving the keyboard — that is, the firmware still owns the
lighting — a hook that writes `0` to
`/sys/class/leds/omen::kbd_backlight/brightness` on `pre` and puts the old value
back on `post` should be enough. This has not been verified here, because the
machine it was diagnosed on runs `omen-fx`; if it does not work for you, treat
the note below as the general answer.

If something *is* driving the keyboard over the HID LampArray, that write is not
enough: the value is a colour scaler applied to what the owner paints, so the
keyboard changes colour but stays lit. Tell the owner to paint black instead.
With `contrib/omen-fx` there is a ready-made hook that does exactly that:

```bash
sudo install -m755 contrib/omen-fx/systemd/omen-fx-blank-on-suspend.sh \
     /usr/lib/systemd/system-sleep/omen-fx-blank-on-suspend.sh
```

It paints the keyboard black through omen-fx before sleeping and releases the
effect on resume, so the base lighting comes back.

> If you put anything in `/usr/lib/systemd/system-sleep/`, remember that
> systemd runs **every executable file** in that directory, in parallel. Never
> leave backup copies there with the executable bit set.

## If you already had a delay in a resume hook

Some setups carry a `sleep` of a second or two in a resume hook, added while
chasing this bug on the theory that the keyboard needed settling time. That
theory was wrong — the trigger is the `_DSM` call, not timing — so once the
kernel parameter is in place the delay can usually go. On the machine this was
diagnosed on, dropping it took the time from `PM: suspend exit` to
`user.slice: Unit now thawed` from **2.82 s to 0.66 s**, with no regression over
repeated suspends.

Lower it in steps and test rather than removing it outright: a short delay
before re-binding `atkbd` may still be doing something useful on your hardware,
independently of this bug.

## Dead ends, so nobody repeats them

Each of these was tested and disproved on the affected machine.

* **USB.** No USB event of any kind occurs across the suspend — the device is
  enumerated once at power-on and never touched again. HID report descriptors
  are byte-identical in both states. Forcing a reset with
  `usbcore.quirks=…:b`, unbinding and rebinding the device, and disabling the
  root-hub port all fail to repair it; the port disable is a logical disconnect
  only, VBUS is never removed and the MCU never loses power. The `pm_test`
  ladder passing `devices` is the clean proof that this whole area is innocent.
* **HID LampArray `AutonomousMode`** (report 6). Refuted twice independently:
  writing `1` by hand, and stopping a daemon that holds the array (which writes
  `1` on the way out). Neither repairs a broken keyboard.
* **HP's "F24 event" mechanism** (`GET_F24_EVENT_KEY_CODE` /`CLEAR_F24_EVENT`,
  vendor frames `80 04 00 00` and `01 04 01 00 FF`). The returned key code stays
  `0x00` and does not change when the keys are pressed. In HP's own newer SDK
  (`McuSDK2`) both functions are empty stubs, so the V2 protocol does not use it
  at all.
* **The WMI hotkey path.** The sparse-keymap input device this driver creates
  emits nothing for these keys, in either state.
* **`hp_wmi_enable_hotkeys()`.** Guarded by
  `!hp_wmi_bios_2009_later() && hp_wmi_bios_2008_later()`, which is false on any
  recent BIOS, so it never runs.
* **Suspend duration and S0ix residency.** Both were red herrings from a small
  sample. The fault reproduces on a suspend of a fraction of a second, on the
  first suspend after a cold boot, with residency at 98%.
* **`acpi_call`.** Calling the individual `_DSM` functions from userspace to
  find out which one is responsible caused a **reproducible kernel panic**, twice
  out of two attempts, immediately after `SCREEN_OFF(3)`. Do not do this. If you
  want to isolate the single function, the safe route is an SSDT override loaded
  from the initramfs, which runs in the context the firmware expects.

## What is still open

Which of the three `_DSM` functions actually triggers the handover is not
known — only that disabling the interface as a whole prevents it. Isolating it
would allow a narrower fix that keeps the last few percent of S0ix residency and
lets the EC turn the backlight off by itself. That work needs the SSDT override
route described above.
