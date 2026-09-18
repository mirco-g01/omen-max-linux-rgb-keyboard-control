# omen-fx — lighting for the HP OMEN MAX: light bar **and** per-key keyboard

A small userspace layer that owns both lit surfaces of an OMEN MAX and paints
them when something happens — a desktop notification, a `sudo`/howdy password
prompt, or anything else you decide to hook up.

```
notify-send / any app ──▶ omen-fx-notifyd (user)  ─┐                  ┌─▶ sysfs ─▶ light bar (4 zones)
                                                   ├─▶ omen-fxd (root)┤
PAM: sudo, polkit, … ──▶ omen-fx-pam (root)       ─┤                  └─▶ hidraw ─▶ keyboard (120 lamps)
you, a script, a Makefile ─▶ omen-fx CLI          ─┘
```

## What this runs on

An **HP OMEN MAX 16**, and nothing else is aimed at. Developed and tested on
exactly one machine — my own personal 16-ah0xxx — and nowhere else. The
variations between MAX models are processor and graphics, which none of this
touches, so other MAX machines should be fine; but that is reasoning, not
testing. If yours is not fine, that is a bug worth reporting, and the contact
address is in the top-level README.

Two pieces of hardware, and they fail independently:

* **the 4-segment light bar**, through the `omen_rgb_keyboard` driver's WMI
  zones. Present on far more OMEN models than the MAX, and it works on them.
* **the per-key keyboard**, a standard HID LampArray. Found by its report
  descriptor rather than by USB id, so any keyboard that really is a LampArray
  is picked up — but only the MAX is tested. Where there is none, omen-fx says
  so once and drives the bar alone.

The udev rule that hands the LampArray node to group `input` *is* matched on
the MAX's USB id (`0d62:54bf`), because handing out the wrong hidraw node would
mean handing out keystrokes. Without it the daemon still works — it runs as
root — but the CLI and the GUI cannot drive the keyboard directly.

## Two surfaces, one effect engine

They are completely different hardware:

| | `bar` | `keys` |
|---|---|---|
| what | the 4-segment light bar | the per-key backlight |
| how | `omen_rgb_keyboard` sysfs (the driver calls them "keyboard zones", but on an OMEN MAX they drive the bar) | a standard **HID LampArray**, HID Usage Page 0x59 — the same thing Windows Dynamic Lighting drives |
| where | `/sys/devices/platform/omen-rgb-keyboard/rgb_zones` | `/dev/hidraw*`, interface 4 of the internal keyboard |
| points | 4 | 120 |
| a frame costs | ~30 ms | ~12 ms |
| ceiling | ~33 Hz | 30 Hz (firmware `MinUpdateInterval`) |

No kernel driver is involved on the keyboard side, and none is needed.

Every lamp reports its **real position in millimetres**, so effects are written
as continuous functions of space and time — `(u, v, t) → colour` — and simply
sampled wherever a surface has points. That is what lets one `center_out` pulse
spread across four bar zones and across 120 keys in step, instead of needing
two separate animations.

### The firmware mirror

The one quirk worth knowing. While omen-fx holds the LampArray, **the firmware
copies the keyboard onto the light bar.** Two consequences, both handled for
you:

* a bar meant to show something *different* has to be rewritten after every
  keyboard frame, which is why running both independently costs ~41 ms a frame
  (about 24 Hz) rather than 12;
* if you *want* the bar to follow the keyboard, say `kind = "mirror"` in
  `[base.bar]` and it happens for free, with the keyboard at a full 30 Hz.

omen-fx also takes the LampArray **only when something actually wants to paint
it**. With no `[base.keys]` and no keyboard-targeted effect, the keyboard keeps
its own firmware lighting and the bar is never pulled into mirrored mode.

## Why a daemon

Three reasons, all of them about not wrecking your normal lighting:

* the driver's zone files are shared state — an effect has to **save what was
  there and put it back**, and only one thing can be doing that at a time;
* the driver scales every colour by `brightness` on write *and* reports the
  scaled value on read, so a naive save/restore darkens the bar a little on
  every cycle. `omen-fx` undoes the scaling (see `omen_fx/led.py`);
* a notification burst must not turn into a queue of twenty animations, so
  effects have **priorities** and duplicates collapse.

## The driver patch this relies on

Out of the box a four-colour frame costs ~860 ms, because `zone_set()` calls
`animation_stop()`, which repaints all four zones even when no animation was
running -- five WMI get/set pairs per zone write. Two changes in the driver fix
that (both confined to `src/zones/` and `src/animations/`, nothing in `src/fan/`):

* `animation_stop()` returns early when no animation was active;
* a new `zones` attribute writes the whole bar in one get/set pair:
  `echo "FF0000 00FF00 0000FF FFFFFF" > zones`.

Measured on an OMEN MAX, kernel 7.1.11:

| write | before | after |
|-------|-------:|------:|
| one zone | 261 ms | 26 ms |
| four zones separately | 860 ms | 118 ms |
| `zones` (new) | -- | **30 ms** |

That is 860 ms -> 30 ms for a frame, which is what makes the `speed` knob mean
anything. `omen-fx` detects the attribute and falls back to per-zone writes on
an unpatched driver, so it still works -- just slowly.

## Install

Requires the `omen_rgb_keyboard` module loaded and Python 3.11+.

```bash
cd contrib/omen-fx
sudo ./install.sh
```

That installs to `/usr/local/lib/omen-fx`, drops the config in
`/etc/omen-fx/config.toml`, enables `omen-fxd.service` (system) and
`omen-fx-notifyd.service` (user), and puts `omen-fx` on your `PATH`. It also
installs `omen-fx-brightnessd.service` (user) but leaves it **off**: it changes
what your brightness keys do, so it is switched on from the GUI's *System*
tab, or with `systemctl --user enable --now omen-fx-brightnessd`.

Check it:

```bash
omen-fx status
omen-fx play notify
omen-fx demo                       # plays every configured effect in turn
notify-send -u critical hey there  # the bar should flash red
```

You need to be in the `input` group to talk to the daemon (the same group the
driver's own udev rules use). The installer tells you if you are not.

### Password prompts

This is the only part that touches PAM, so it is opt-in:

```bash
sudo ./install.sh pam-enable
```

With howdy in the stack the bar tells you which phase you are in:

| | |
|---|---|
| orange, pulsing | howdy is looking at you -- hold still |
| red, quick | the face scan gave up |
| violet, slow, repeating | type your password |
| green | authenticated |

The middle two come from a hook placed *directly after* the `sufficient` howdy
line. A `sufficient` module that succeeds ends the auth stack immediately, so
anything below it runs only when it did not succeed -- which is exactly "howdy
failed, a password is coming". No polling, no guessing.

A failed authentication never reaches the `account` stage, so there is no event
for "gave up": the violet phase is a hold with a 60 s timeout that clears
itself. `auth.end` clears it earlier when you do get in.

It adds three `optional` `pam_exec` lines to `/etc/pam.d/sudo` — one at the top of
the `auth` stack (bar starts pulsing orange as the prompt comes up, before
howdy runs) and one in `account` (bar goes back to normal once you are through).
A failed authentication never reaches the `account` stage, so the effect also
expires on its own after `timeout` seconds (45 by default).

Safety, since this is the part that can ruin your day:

* both lines are `optional`, so PAM ignores their result;
* `omen-fx-pam` exits 0 no matter what, with a 0.4 s socket timeout — if the
  daemon is dead it costs ~40 ms and nothing else;
* the original file is backed up to `/etc/pam.d/sudo.omen-fx.bak`, and the
  patcher rolls back if any pre-existing line would change.

**Keep a root shell open in another terminal** and test with `sudo -k true`
before closing it. To undo: `sudo ./install.sh pam-disable`.

To cover the KDE polkit dialogs and the screen unlock as well, edit `PAM_FILES`
in `install.sh` to add `/etc/pam.d/kde`, or run the patcher directly:

```bash
sudo /usr/local/lib/omen-fx/pam-hook.py enable /etc/pam.d/kde
```

The `service` filter in a trigger rule (`service = "sudo"`, `service = "kde"`)
lets you give each one a different effect.

## The GUI

```bash
omen-fx-gui          # also in the application menu, under Settings
```

Pick an alert on the left, change colour, duration, speed and the two
smoothness knobs on the right. The strip at the top animates the *same*
generator the daemon uses, so the preview is what the bar will do -- and
"Play on the bar" plays it for real. **Save** writes the effects through the
daemon into `/etc/omen-fx/effects.toml`, which layers over `config.toml`; your
hand-written file and its comments are never rewritten by a program.

The preview shows **both surfaces** — the 120 keys as a 6×20 grid with the four
bar segments beneath — and samples the effect exactly the way the engine does,
one call per tick at the real coordinates. So the preview is what the hardware
will do, not a separate approximation of it. **Surface** picks where the
effect plays; **Play on the bar** sends it to the daemon for real.

These controls shape how the wavefront reads:

* **Spatial** (`smoothness`, 0..1) — how much a point already glows while the
  front is still on its neighbour. 0 is a hard edge, 1 a broad soft wash.
* **Naturalness** (`gamma`, 2.2 by default) corrects the eye's non-linear
  response. Without it a linear ramp looks like it falls off a cliff near the
  top and crawls near the bottom, which reads as a step even when the maths is
  smooth.
* **Trail** (`tail`, 0..1) — how much a point keeps as the front moves past it.

There is no temporal-resolution slider any more: effects are continuous
functions of time, sampled once per frame, so they keep their real speed
whatever the hardware manages to deliver. The line under the sliders states the
frame rate you will actually get — ~30 fps on the keyboard alone, ~33 on the
bar alone, ~24 with both, since the firmware mirror forces a bar rewrite after
every keyboard frame.

## Brightness

Three separate numbers decide how bright a lamp actually is, and keeping them
separate is what makes each one behave sensibly:

```
what you see  =  the effect's colour
              x  the effect's own brightness      (per effect / per profile)
              x  your keyboard-backlight level    (the desktop's slider)
              x  the idle dim                     (omen-fx, while you are away)
```

### Your level is the desktop's slider

The driver exports its `brightness` twice — as `rgb_zones/brightness` and as
the standard LED class device `omen::kbd_backlight` — and the second is what
UPower shows every desktop as its **keyboard backlight** slider. So that file
belongs to you, and omen-fx treats it as read-only: it samples it every frame
and never writes it, which is what lets the slider work live instead of
snapping back to whatever the last effect asked for.

The two surfaces get there differently, which is worth knowing when reading the
code. The bar's half is free — the kernel already scales every colour written
to a zone by that level — so `led.py` applies only the effect's own brightness,
in software, before writing. The keyboard has no brightness register at all, so
`surface.py` applies every factor to every lamp itself.

Set a base profile's `brightness` to 100 and the slider covers the full range.
Lower it only to keep the default look dimmer than an alert that asks for 100.

### The brightness keys

`omen-fx-brightnessd` makes **Alt + the brightness keys** move the keyboard
instead of the screen: Alt for 5%, Shift+Alt for 1%, and holding a key ramps.
Plain presses are untouched and still drive the screen.

It reads the key from evdev instead of registering a desktop shortcut, which
sounds like the harder way round until you try the easy one. Two measurements,
on Plasma 6.7:

* **The compositor keeps the brightness keys.** A brightness key reaches a
  global shortcut only when Shift is held. Bound to `Alt+Monitor Brightness
  Up`, a shortcut registers cleanly, `getGlobalShortcutsByKey` reports it as
  the only claimant, and pressing the key does nothing whatsoever — it does not
  fire and the screen does not move either. Probes with Meta, Ctrl, Ctrl+Alt
  and Meta+Alt behaved identically.
* **The key does not auto-repeat.** The keyboard's consumer-control interface
  sends exactly one press and one release however long the key is held. No
  shortcut, the desktop's own included, can ramp on hold.

Reading evdev answers both, and has the side benefit of working the same on
KDE, GNOME, sway or a bare TTY. The daemon does not grab the device, so plain
presses still reach the compositor; it acts only when Alt is held, which is
exactly the case the compositor discards. Only the media-key device is read as
a stream — modifier state is polled with `EVIOCGKEY` at the instant a
brightness key arrives, so the typing keyboard is never read.

Writing the level goes through PowerDevil if it is there (that is what shows
the OSD), else UPower, else the sysfs LED. That order matters: writing the LED
while UPower is running leaves the desktop's slider showing a value the
hardware no longer has.

### F4, and why the daemon keeps the MCU lit

F4 (the keyboard-backlight key) never reaches the host: the keyboard's own
microcontroller handles it, stepping its lighting level 100% → 60% → off and
round again. While the daemon holds the LampArray the keys ignore that level
— they show whatever is painted — but **the light bar does not**: it goes
dark, and stays dark under every effect, because the level lives in the MCU's
flash and survives reboots and full power cycles. One accidental press is
enough, and from Linux nothing looks wrong: the driver writes colours, the EC
accepts them, and the bar shows nothing.

So the daemon reads that level over the keyboard's vendor interface (the same
frames OMEN Gaming Hub uses) when it takes the LampArray, and every few seconds
afterwards, and sets it back to 100% if it has moved. Brightness is the
desktop's slider and the idle dim, applied in software to both surfaces; F4
simply has nothing left to do while omen-fx is running. It is logged when it
happens:

```
omen-fxd[876]: INFO omen-fx.keys: MCU lighting level was 0%, turning it back on so the light bar follows
```

### Dimming when you walk away

Off by default; the GUI's *System* tab has the switch, the delay and how far
to go down — once for when the machine is plugged in and once for battery,
because those are different situations: at the desk a dim glow is company, on
battery it is drain. The daemon reads the power source from
`/sys/class/power_supply` and hears about a change through the kernel's uevent
socket (`power.py`), so a plug going in while nobody is there still brings the
lights to their mains level at once. In `config.toml` the flat `[idle]` keys
apply to both, and `[idle.ac]` / `[idle.battery]` override them one at a time.

It dims the *rendering*, never your level — so the slider stays where you put
it, the desktop's idea of the brightness never goes stale, and one keypress
brings everything straight back. Waking is instant on purpose; only the dim is
a fade, because a keyboard that took a second to come back would feel like it
was thinking about it.

**Alerts wake it** lifts the dim on whichever surface an alert plays on, for
as long as it plays, and then eases back down over the same fade. It is the
rendering level that comes back, not your slider: a keyboard whose backlight
you set to zero stays dark whatever arrives, because you said so.

The same tab also sets the **keyboard level by power source** — 100% at the
desk, 15% on battery, that kind of thing. That one is not the daemon's: it is
your slider, and the desktop's power management is what already moves it when
the plug goes in or out, so the tab writes KDE's own setting (`powerdevilrc`,
the same thing as System Settings → Power Management → Keyboard brightness)
and has PowerDevil reload it. Off KDE the group is greyed out, and the
desktop's own power settings are the place to look.

Inactivity comes from `/dev/input`, not from the desktop: no idle signal is
common to KDE, GNOME, sway and a TTY, and the kernel's answer stays right when
the screen is locked or a full-screen game owns the compositor. `idle.py` reads
keyboards, pointers and media keys — not lid switches or accelerometers, which
emit on their own and would keep the machine looking busy — and takes only the
*time* of an event; nothing decodes what was pressed.

## Zones and layouts

A **layout** puts several effects on the keyboard at once: a static background
with a breathing WASD cluster, a rainbow along the function row, whatever you
paint. It is an *ordered* list of zones, and the order is the priority -- zone 1
is the background and every zone after it paints on top, so on a cell claimed
twice the later zone wins. That is the painter's algorithm, and it is why the
zone list is numbered and reorderable instead of carrying a priority field.

The grid is the hardware, exactly: the keyboard is six rows of twenty lamps,
one cell per lamp, with the bar's four segments underneath.

Both surfaces are the same width. The keyboard's coordinates are stretched to
span exactly [0, 1] across it, and the bar's four segments partition that same
range into quarters -- so a position on one is the same position on the other.

A bar segment is then read as an **area**, not a point, and not with a plain
average either. Two earlier attempts were both wrong in an instructive way:
one sample per segment made the segment *be* whichever key it sat on, so it
held its brightness until that one key did; a flat average of its own quarter
told it nothing about what was happening just outside, so every boundary
between segments was a hard step.

What a diffused strip actually does is weigh its surroundings. So each segment
is a **weighted** average across the whole width, through a raised cosine
centred on it and reaching a segment and a half either side. Neighbouring
windows overlap, which is what removes the step: a front crossing a boundary is
already showing on the next segment before it gets there, and still showing on
the last one after it leaves. At this spacing the window is a partition of
unity, so the bar reproduces the effect's overall level rather than dimming or
blooming.

The width is a trade-off with no free lunch in it -- wider hands over sooner,
narrower keeps more contrast -- so `daemon.bar_blend` sets it, in segments
either side of centre. Measured on a `center_out` front:

| `bar_blend` | outer segment lags inner by | contrast kept |
|---|---|---|
| flat average | 12.0 points of phase | 159/255 |
| 1.0 | 9.0 | 143 |
| 1.5 (default) | 2.2 | 106 |
| 2.0 | 0.0 | 71 |

Zone layouts opt out of the blending: there a boundary between segments is a
line drawn in the editor, so two zones meeting at one stay two colours instead
of fading into each other.

Surfaces decide this for themselves, through `Surface.sample`: effects stay
plain functions of position and time and never learn that one of their
consumers is coarser than the other.

Cells no zone claims come out **transparent**, not black. As a default profile
that means simply off; as an *alert* it means the default look shows through,
so an alert can light up one row without blanking the rest of the keyboard.

Create them in the GUI's **Zones** tab: drag on the grid to add cells to the
selected zone, right-click (or Ctrl-drag) to take them away, and give each zone
an ordinary effect with the usual editor. The grid draws the composed output,
animated, so it is a live map of what the hardware will show; the selected zone
is marked by a white outline rather than by a colour of its own. **Mostra i colori veri** swaps the
zone map for the composed output, animated. Saved layouts then appear as a
choice in both the **Default** and **Alerts** tabs.

A zone's effect is always rescaled to the zone's own bounding box, so a rainbow
in a narrow zone shows a whole rainbow rather than a slice of a wider one.
(A hand-written zone may set `scope = "surface"` to be a window onto an effect
spanning the whole surface instead; the GUI does not offer it.)

A zone has no life of its own to end, so it has no total length -- only a
**rate**, in cycles per second: one breath, one blink, one pass of a wavefront.
How long the thing runs is set by the *alert* that plays it, which is the only
thing that knows when it should stop. The same holds for a default profile,
which never ends either.

For a plain effect an alert says how **long** and the effect says how **fast**;
how many times it repeats is simply what fits. Given both, the older reading --
stretch one cycle to fill the time -- meant "5 seconds" could silently become
"one very slow pass", which is not what a duration should do.

An alert playing a **layout** says its length either way round: a duration, or
a number of repetitions counted on the layout's **slowest moving zone** -- the
only common measure when every zone runs at a rate of its own. Never both at
once. Static zones do not count towards "slowest": a zone that never changes
has no cycle to count. An alert on a layout also has no surface picker, because
which surfaces light up is already decided by the cells its zones claim. Opening an editor on a profile stored under the older `speed` knob
converts it by reading the cycle length off the effect itself, so nothing
changes look; the rate control's 0.1 cycles/s granularity is the only rounding.

Because a layout covers the bar as well, the **mirror is not available while
the keyboard is on a layout**. The bar is part of the layout: its four segments
are painted in the Zones tab beside the keys, so a zone can claim keys only, bar
segments only, or both -- which is more direct and more capable than copying
the keyboard wholesale. Switching the keyboard to a layout moves the bar onto
the same layout and greys the mirror out, and the bar keeps following later
changes of layout. A bar given an effect of its own is left alone.

Bar segments no zone claims stay transparent, exactly like unclaimed keys: off
in a default profile, showing what is underneath during an alert.

Layouts live in `/etc/omen-fx/layouts.toml`, written by the GUI and readable by
hand:

```toml
[layouts.gaming]

[[layouts.gaming.zones]]
name = "sfondo"
kind = "solid"
color = "202060"
mask = ["11111111111111111111",
        "11111111111111111111",
        "11111111111111111111",
        "11111111111111111111",
        "11111111111111111111",
        "11111111111111111111"]
bar = "1111"

[[layouts.gaming.zones]]
name = "wasd"
kind = "breathe"
color = "FF4000"
cycles = 0            # zones never end
period_ms = 2000      # one cycle every two seconds: 0.5 cycles/s
mask = ["00000000000000000000",
        "00000000000000000000",
        "00100000000000000000",
        "01110000000000000000",
        "00000000000000000000",
        "00000000000000000000"]
```

Refer to one from a default profile or an effect by name -- never by copying
its zones, so editing the layout updates everything that uses it:

```toml
[base.keys]
kind = "layout"
layout = "gaming"
brightness = 80

[effects.alert]
kind = "layout"
layout = "allarme-rosso"
duration_ms = 4000      # required: the zones themselves never stop
priority = 90
```

`omen-fx layouts` lists them with their zones in painting order.

## Everyday use

```bash
omen-fx play alert                       # a configured effect
omen-fx fx blink --color 00FF00 --times 3   # ad-hoc, nothing to configure
omen-fx fx sweep --color FF00FF --set step_ms=60 --set cycles=3
omen-fx fx progress --value 70 --color 00A8FF
omen-fx fx wave --speed 2 --hold --key mood --target keys
omen-fx release mood
omen-fx stop                             # cancel and restore
omen-fx status
```

Fire your own events and let the rules decide what to play:

```bash
omen-fx trigger build.done
omen-fx trigger notification --app Slack --urgency 2
```

Anything that plays takes `--target`, so the same effect can go to the bar
only, the keyboard only, or both:

```bash
omen-fx play alert --target keys
omen-fx play notify --target bar
omen-fx fx ripple --color 00FFAA --target both
```

Without it, the effect's own `target` decides, then `daemon.default_target`
(`"both"` as shipped).

Handy in scripts:

```bash
long-build && omen-fx play ok || omen-fx play alert
```

## Configuring

Everything lives in `/etc/omen-fx/config.toml`; `omen-fx reload` picks up
changes without restarting.

The shipped config makes every effect a **center_out** sweep: the pulse starts
between the two middle zones and runs outward. Zones are 1..4 left to right, so
2+3 are the centre pair and 1+4 the outer one. Alert types differ only in
colour and speed:

```toml
[effects.mine]
kind = "center_out"
color = "00FF88"
speed = 8             # 1 slow .. 10 fast
cycles = 2
priority = 60         # higher interrupts lower (default 50)
```

**Timing.** `cycles` is how many times the effect repeats, and `0` means it
never stops — which is what a default profile normally wants. `duration_ms` is
the total run length and wins over `speed`: the cycle is stretched or squeezed
so exactly `cycles` of them fit, so the motion is rescaled rather than cut off
part way. Both work on every kind (`times` and `steps` are accepted as aliases
of `cycles` for the kinds that used those names historically).

**Colours.** `color` is a single hex value; `colors` is an ordered list and
wins over it when present — so never leave a stale `colors` behind when you
mean to set `color`. `gradient` reads the list as stops across the machine and
takes an optional `stops` list of positions in 0..1 to weight them (evenly
spaced when omitted), `alternate` uses the list as its two alternating colours,
and the rest cycle one colour per repetition.

Rarely needed: `tail` (how much a point keeps as the front passes, 0.45),
`off_color`, `brightness` (overrides `daemon.effect_brightness`), `target`.

The full set of kinds:

Not every knob applies to every kind, and the GUI hides the ones the chosen
kind ignores: `smoothness`, `gamma` and `tail` describe a travelling wavefront,
so only `center_out`, `sweep` and `ripple` read them, while a static kind reads
no timing knob at all. The editor also stops *writing* the hidden ones — a
leftover `cycles` on a `solid` would give it a duration and make it end.

| kind | what it does |
|---|---|
| `solid` | one flat colour |
| `gradient` | a static ramp across the machine; `axis = "v"` runs it top to bottom |
| `blink` | hard on/off, `times` |
| `breathe` | smooth fade in and out; `cycles = 0` never ends |
| `center_out` | a pulse born mid-machine, running outward |
| `sweep` | a comet running left to right, `bounce` |
| `wipe` | fill, then clear |
| `wave` | a hue wave sweeping across; `spread`, `axis`, `cycles = 0` for endless |
| `rainbow` | a narrower `wave` |
| `alternate` | alternating quarters flip between two colours |
| `sparkle` | random points, `density` |
| `progress` | level meter, `value` 0-100 |
| `ripple` | an expanding ring from `origin = [u, v]` |

`static`, `pulse`, `knightrider` and `centerout` are accepted as aliases of
`solid`, `breathe`, `sweep` and `center_out` — the same effects under older
names, kept so existing configs keep loading.

**Coming back afterwards.** When an alert ends the surface returns to its
default look. Cutting straight there is a visible snap — black to near-white in
one frame — so the return is faded over `fade_back_ms`.

The value is **per effect**: setting it on one alert does nothing for the
others. Leave it out (the GUI shows *eredita dal daemon*) and `daemon.fade_back_ms`
applies, 300 ms as shipped — that is the one to change for the same return
everywhere. `0` cuts straight there. `omen-fx status` reports the remaining
fade per surface, which is the quickest way to tell a value that did not take
effect from one that is simply short.

```toml
[effects.other]
kind = "sweep"
color = "00FF88"
cycles = 2
speed = 6
```

> **Removed with the move to the unified engine:** `kind = "frames"` and
> `kind = "kernel"`, along with the `resolution` and `fade_steps` knobs.
> Hand-written `frames` were written zone by zone and had no meaning on a
> 120-lamp surface, and `kernel` handed the animation to the driver's own
> engine, which cannot be sampled spatially and only ever drove the bar. The
> software effects now cost about the same as the kernel ones did, so `auth`
> uses `center_out` instead. If you had either in your config, `omen-fx reload`
> will report the kind as unknown.

A **trigger** binds an event to an effect. The most specific matching rule
wins, so ordering in the file does not matter:

```toml
[[trigger]]
event = "notification"
effect = "notify"          # catch-all

[[trigger]]
event = "notification"
urgency = 2                # 0 low, 1 normal, 2 critical
effect = "notify-urgent"

[[trigger]]
event = "notification"
app = "*telegram*"         # glob, case-insensitive
summary = "message"        # substring, case-insensitive
effect = "message"

[[trigger]]
event = "auth.start"
service = "sudo"
effect = "auth"
hold = "auth"              # runs until released…
timeout = 45               # …or until this many seconds pass

[[trigger]]
event = "auth.end"
release = "auth"
```

Filters available: `app`, `summary`, `body`, `urgency`, `service`, `user`.
Events: `notification`, `auth.start`, `auth.end`, plus anything you send
yourself with `omen-fx trigger NAME`.

### The default look of each surface

The GUI's **Default** tab is the easy way in: pick a surface and choose the
look. **Show live on the hardware** keeps the real surface on whatever you
are editing for as long as you stay there, so you judge it on the machine
rather than in a thumbnail; **Save as default** is what makes it stick.
The preview covers **both** surfaces at once and lasts for as long as the window
is open, tab changes included — a half-finished default must not vanish from
the hardware just because you went to look at something else. It is held at the
lowest priority, so alerts play over it and it comes back on its own afterwards,
which is exactly what the machine will do once you save. It never expires on a
timer; it goes away when you save, untick it, or close the window. It writes `/etc/omen-fx/base.toml`, which layers over `config.toml`
exactly the way `effects.toml` does for alerts.

**The light bar copies the keyboard** lives in the *keyboard* section,
because that is where the decision is made. While it is on, the bar has no look
of its own, so its section is shown locked rather than quietly ignored; switch
it off and the bar gets its colours back, ready to edit.

**Colours** adapt to the kind, so the editor only ever offers what that effect
can use: one swatch for `solid` and `progress`, exactly two for `alternate`,
none at all for `wave` and `rainbow` (they generate their own hues), and one to
eight for the rest, cycled one per repetition. `gradient` additionally gets a
**Distribution** bar: the gradient itself with a draggable handle per colour,
because evenly spaced stops make every gradient look alike and what usually
reads well is uneven — a long wash of one colour with a narrow band of another.

In the file, `[base.bar]` and `[base.keys]` each hold an ordinary effect spec —
static or animated — shown whenever no overlay is on top of it:

```toml
[base.keys]
kind = "gradient"
colors = ["001A40", "2A0A60"]
brightness = 100     # relative to the desktop slider — see below

[base.bar]
kind = "mirror"      # follow the keyboard, for free
```

Swap in `kind = "wave"` with `speed = 2` for a slow endless hue sweep, or
`kind = "solid"` for one flat colour. Omit a table entirely and omen-fx leaves
that surface alone — for the keyboard that means never taking the LampArray at
all.

### Brightness: the desktop slider owns it

The driver exports its `brightness` twice — as `rgb_zones/brightness` and as
the standard LED class device `omen::kbd_backlight` — and the second one is
what UPower shows the desktop as its **keyboard backlight** slider. So that
file is the *user's* level, and the daemon treats it as read-only: it samples
it every tick and never writes it, which is what makes the slider work live
instead of snapping back to whatever the last effect asked for.

The `brightness` in an effect or a base profile is a level *relative* to that:

```
what the hardware shows  =  colour  x  effect brightness  x  slider
```

The two surfaces get there differently, and this is worth knowing when reading
the code. The bar's half is free: the kernel already scales every colour
written to a zone by the master, so `omen_fx/led.py` applies only the effect's
level, in software, before writing. The keyboard has no brightness register at
all, so `omen_fx/surface.py` applies **both** factors to every lamp itself.

Two consequences:

* a slider at 40 % dims alerts too, which is the point of a master;
* the slider reaches the keyboard only while the daemon is painting it. With no
  `[base.keys]` the LampArray belongs to the firmware, and only the bar follows.

### What it goes back to

By default the daemon reads the lighting just before the first effect starts
and restores exactly that (colours, brightness, animation mode and speed). If
you would rather always land on a fixed look:

```toml
[base]
mode = "pinned"
apply_on_start = true
```

then set the bar how you like it and run `omen-fx base pin`.

## Troubleshooting

```bash
systemctl status omen-fxd
journalctl -u omen-fxd -f
systemctl --user status omen-fx-notifyd
omen-fx status
```

* **`device ... (MISSING)`** — the driver is not loaded. `sudo modprobe
  omen_rgb_keyboard`; if that fails, `dkms status` and check the module was
  built for the kernel you are actually running (`uname -r`).
* **nothing happens on notifications** — `systemctl --user status
  omen-fx-notifyd`, then `omen-fx-notifyd -v` in a terminal and send a
  `notify-send`; it logs every notification it sees.
* **`no permission on /run/omen-fx/control.sock`** — `sudo usermod -aG input
  $USER`, then log out and back in.
* **`no HID LampArray found`** — the keyboard backend could not open its
  hidraw node. Check `ls -l /dev/hidraw*`: the LampArray node should be
  `root input`. If not, reinstall the rule with
  `sudo install -m0644 99-omen-lamparray.rules /etc/udev/rules.d/ &&
  sudo udevadm control --reload-rules && sudo udevadm trigger`.
* **the keyboard is stuck on one frame** — something exited without handing the
  LampArray back. `omen-fx stop`, or restart `omen-fxd`; the daemon releases it
  on every exit path, including SIGTERM.
* **Alt + brightness does nothing** — `systemctl --user status
  omen-fx-brightnessd`. It needs the `input` group; without it the daemon
  cannot read the key and says so in its log. `touch
  ~/.cache/omen-fx-brightness.log` to have every step recorded there.
* **the desktop's slider disagrees with the hardware** — something wrote
  `/sys/class/leds/omen::kbd_backlight/brightness` directly. Use
  `omen-fx-brightness <N>` instead; it goes through PowerDevil or UPower, which
  is what the slider reads.
* **idle dimming never happens** — `omen-fx status` prints `idle dim on but
  blind` when the daemon cannot read `/dev/input`. It runs as root, so this
  normally means the devices are missing, not a permission problem.
* **the light bar is dark under every effect, the keys are fine** — F4 was
  pressed while the daemon held the keyboard, and the MCU's own lighting level
  went to off; see [F4, and why the daemon keeps the MCU lit](#f4-and-why-the-daemon-keeps-the-mcu-lit).
  Since that section was written the daemon fixes it by itself; if you are on
  an older build, press F4 until the bar comes back (with `omen-fxd` stopped
  you can see the keyboard follow) and update.
* **experiment without touching the hardware** — `omen-fxd --dry-run -v` logs
  every write it would make instead of making it.
* **the keyboard stays lit while the machine sleeps** — you are running with
  `acpi_x86.sleep_no_lps0=1`, the workaround for the Fn row dying after suspend
  (see [../../docs/modern-standby-fn-keys.md](../../docs/modern-standby-fn-keys.md)).
  That parameter also stops the EC being told the screen went off, so it never
  turns the backlight off. Install the hook that does it instead:
  `sudo install -m755 systemd/omen-fx-blank-on-suspend.sh
  /usr/lib/systemd/system-sleep/omen-fx-blank-on-suspend.sh`. Writing `0` to
  `/sys/class/leds/omen::kbd_backlight/brightness` will not do it — that is a
  colour scaler, not an off switch.

## Layout

| File | Role |
|------|------|
| `omen_fx/led.py` | light bar sysfs backend, snapshot/restore, brightness unscaling |
| `omen_fx/keys.py` | HID LampArray backend: discovery, geometry, frame writes |
| `omen_fx/surface.py` | both surfaces in one shared coordinate space |
| `omen_fx/effects.py` | effects as `(u, v, t) → colour` (add new kinds here) |
| `omen_fx/layouts.py` | zone layouts: several effects composited on one surface |
| `omen_fx/engine.py` | render loop, base profiles, targeted overlays, the mirror rule |
| `omen_fx/daemon.py` | priority queue, control socket, request vocabulary |
| `omen_fx/config.py` | TOML config and trigger matching |
| `omen_fx/idle.py` | seconds since the last input, read from evdev |
| `omen_fx/power.py` | battery or mains, from sysfs, told of changes by uevent |
| `omen_fx/notifyd.py` | D-Bus notification monitor (user session) |
| `omen_fx/zonegui.py` | the zone editor: paintable grid, zone list, per-zone effect |
| `omen_fx/cli.py`, `omen_fx/client.py` | `omen-fx` command line |
| `omen-fx-brightnessd` | Alt + brightness keys → the keyboard, with hold-to-ramp |
| `omen-fx-brightness` | set or step the backlight: PowerDevil, else UPower, else sysfs |
| `omen-fx-pam` | PAM hook, kept deliberately tiny |
| `pam-hook.py` | patches/unpatches `/etc/pam.d/*` |
| `99-omen-lamparray.rules` | hands out *only* the LampArray hidraw node to group `input` |
| `systemd/omen-fx-blank-on-suspend.sh` | turns the keyboard off while asleep, for setups running `acpi_x86.sleep_no_lps0=1` |

Config files, in increasing precedence: `/etc/omen-fx/config.toml` (yours, hand
written, never rewritten by a program), then `/etc/omen-fx/effects.toml` (the
GUI's alerts), `/etc/omen-fx/base.toml` (the GUI's default profiles) and
`/etc/omen-fx/layouts.toml` (the GUI's zone layouts) and
`/etc/omen-fx/settings.toml` (the GUI's idle-dim switches, per power source).
