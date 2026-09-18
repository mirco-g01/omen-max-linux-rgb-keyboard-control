"""The renderer: default profiles per surface, plus targeted overlays.

Two things make this more than a loop over two devices.

**The mirror.** While we hold the LampArray the firmware copies the keyboard
onto the light bar. So any tick that writes the keyboard has invalidated the
bar, and the bar must be rewritten even if its own colours did not change.
``_tick`` encodes exactly that, and it is why ``bar_forced`` exists.

**Lazy acquisition.** Taking the LampArray is not free: it silences the
firmware's own lighting and switches the bar into mirrored mode. So the
keyboard is acquired only when something actually wants to paint it -- a base
profile or an overlay -- and handed straight back otherwise. A config with no
``[base.keys]`` therefore behaves exactly as omen-fx did before the keyboard
existed.

**Pacing.** The keyboard firmware accepts a frame every 33 ms and a bar write
costs ~30 ms, so a tick that touches both lands around 41 ms. The tick is paced
against a deadline rather than sleeping a fixed amount, so effects keep their
real-time speed instead of stretching under load.

That rate is for pictures that move, and only for those. A still one -- which
is the ordinary case, since most people leave a colour or a gradient up all day
-- is drawn once and then left alone: the loop asks what could possibly change
it next (a job running out, the idle dim falling due, a hand on the brightness
slider), sleeps until then, and is woken early by anything that arrives
meanwhile. Redrawing a still frame thirty times a second changed nothing on the
hardware -- the surfaces drop an identical frame -- so all of it was heat.
"""

from __future__ import annotations

import logging
import threading
import time

from . import effects as fx
from .keys import KeyboardUnavailable
from . import surface as surface_mod
from .led import BLACK, DeviceUnavailable, Snapshot, blend, parse_color

log = logging.getLogger("omen-fx.engine")

TARGETS = ("bar", "keys", "both")
TICK_S = 0.033          # the keyboard's MinUpdateInterval: the rate things move at
# Nothing is moving. The picture cannot change on its own, so these are poll
# intervals for the one input that has no event to wait on -- the user's
# brightness slider -- and nothing else. Which of the two applies depends on
# whether there is anybody there to touch it.
STILL_S = 0.20          # someone is at the machine
DORMANT_S = 5.0         # nobody has touched it in a while: insurance only
PRESENCE_S = 2.0        # input this recent counts as somebody being there
KEYS_RETRY_S = 2.0      # how often to re-look for a vanished keyboard

# A bar profile of this kind means "let the firmware mirror the keyboard",
# which costs no WMI writes at all and keeps the keyboard at a full 30 Hz.
MIRROR = "mirror"


def normalise_target(value, default: str = "both") -> str:
    text = str(value or default).strip().lower()
    if text in ("keyboard", "key", "keys"):
        return "keys"
    if text in ("bar", "lightbar", "light_bar"):
        return "bar"
    if text in ("both", "all", "*"):
        return "both"
    raise ValueError(f"unknown target: {value!r} (use one of {', '.join(TARGETS)})")


def surfaces_of(target: str) -> tuple[str, ...]:
    return ("bar", "keys") if target == "both" else (target,)


class Job:
    """One overlay: an effect, where it plays, and when it stops."""

    __slots__ = ("key", "spec", "priority", "hold", "target", "deadline",
                 "cancelled", "released", "seq", "elapsed", "effect", "brightness",
                 "last_seen", "stand_in")

    _counter = 0

    def __init__(self, key: str, spec: dict, priority: int, hold: bool,
                 timeout: float | None, target: str = "both",
                 brightness: int | None = None, stand_in: bool = False):
        Job._counter += 1
        self.seq = Job._counter
        self.key = key
        self.spec = spec
        self.priority = priority
        self.hold = hold
        self.target = target
        self.brightness = brightness
        # A stand-in is an overlay that takes the place of the default look --
        # the GUI's live preview -- rather than something that has happened.
        # It is painted like any other job, but it is not an alert: it must
        # not wake a dimmed surface, or the machine would never dim while the
        # GUI is open.
        self.stand_in = stand_in
        self.deadline = (time.monotonic() + timeout) if timeout else None
        self.cancelled = False
        self.released = False
        self.elapsed = 0.0
        # When this job was last on screen, so that ``elapsed`` counts the time
        # it was actually visible. The loop sleeps through a still picture, and
        # a job submitted during one of those sleeps must start at its first
        # frame rather than arrive already old.
        self.last_seen = 0.0
        self.effect = fx.build(spec)

    @property
    def shown(self) -> bool:
        """Whether this job has ever reached a surface."""
        return self.last_seen > 0.0

    def age(self, now: float) -> None:
        """Charge it the time since its last frame -- nothing on its first."""
        if self.last_seen:
            self.elapsed += now - self.last_seen
        self.last_seen = now

    @property
    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    @property
    def finished(self) -> bool:
        """Whether the effect has played itself out (holds never do)."""
        if self.hold:
            return False
        d = self.effect.duration
        return d is not None and self.elapsed >= d

    @property
    def done(self) -> bool:
        return self.cancelled or self.released or self.expired or self.finished

    def __repr__(self) -> str:
        return f"<Job {self.key} target={self.target} prio={self.priority}>"


class Engine(threading.Thread):
    daemon = True

    def __init__(self, bar, keys, config, idle_watcher=None, power_watcher=None):
        super().__init__(name="engine")
        self.bar = bar
        self.keys = keys
        self.config = config
        self.idle_watcher = idle_watcher
        if idle_watcher is not None:
            # So a dimmed or long-settled picture comes back the moment
            # somebody touches the machine, rather than at the next poll.
            idle_watcher.on_input = self._on_input
        self.power_watcher = power_watcher
        if power_watcher is not None:
            power_watcher.on_change = self._on_power
        # When the plug last went in or out: the desktop moves the user's
        # level right after, and the loop watches for that as if a hand were
        # on the slider.
        self._power_changed = 0.0
        self._idle_level = 100.0
        # Per surface, when an alert was last seen on it while dimmed, so
        # the dim can ease back once the alert has gone. See _surface_idle.
        self._alert_seen: dict[str, float | None] = {"bar": None, "keys": None}
        # Set while the loop is sleeping the long interval, which is the only
        # time an input event has anything to tell it.
        self._dormant = False
        self._last_tick = 0.0
        self.cv = threading.Condition()
        self.jobs: list[Job] = []
        self.running = True
        # Raised by _wake for the render loop to find. A bare notify would be
        # lost if it landed between a frame and the wait that follows it, and
        # the loop now sleeps for seconds at a time rather than milliseconds --
        # so a missed notification would hold an alert back that long.
        self._dirty = False
        self.last_error: str | None = None
        self._bar_base: Snapshot | None = None
        self._base_effects: dict[str, fx.Effect | None] = {}
        self._base_clock = 0.0
        self._active: dict[str, Job | None] = {"bar": None, "keys": None}
        self._previous: dict[str, Job | None] = {"bar": None, "keys": None}
        # The last frame actually pushed to each surface, and any fade running
        # back towards the default look.
        self._last_frame: dict[str, list | None] = {"bar": None, "keys": None}
        self._fades: dict[str, dict | None] = {"bar": None, "keys": None}
        # Set when the keyboard needs re-finding; see _recover_keys.
        self._keys_stale = False
        self._keys_retry = 0.0

    # -- public API ------------------------------------------------------

    def _wake(self) -> None:
        """Rouse the render loop. Must be called with ``cv`` held."""
        self._dirty = True
        self.cv.notify_all()

    def submit(self, job: Job) -> str:
        with self.cv:
            for existing in self.jobs:
                # Repeating the same hold just extends it -- but it must be the
                # same effect too, or auth.fail handing the "auth" key over to
                # the password effect would silently swallow the new one.
                if (existing.key == job.key and existing.hold and job.hold
                        and existing.spec == job.spec and not existing.done):
                    existing.deadline = job.deadline
                    self._wake()
                    return "refreshed"
            # Collapse duplicates so a notification burst does not queue up.
            self.jobs = [j for j in self.jobs if j.key != job.key]
            limit = int(self.config.daemon.get("queue_limit", 4))
            waiting = [j for j in self.jobs if not j.shown]
            if len(waiting) >= limit:
                self.jobs.remove(waiting[0])
            self.jobs.append(job)
            self._wake()
        return "queued"

    def release(self, key: str) -> str:
        with self.cv:
            hit = False
            for job in self.jobs:
                if job.key == key:
                    job.released = True
                    hit = True
            self._wake()
        return "released" if hit else "not-running"

    def stop_all(self) -> None:
        with self.cv:
            for job in self.jobs:
                job.cancelled = True
            self._wake()

    def shutdown(self) -> None:
        with self.cv:
            self.running = False
            for job in self.jobs:
                job.cancelled = True
            self._wake()

    def status(self) -> dict:
        with self.cv:
            return {
                "device": self.bar.bar.sysfs,
                "keyboard": self.keys.kb.device,
                "available": self.bar.available,
                "keyboard_available": self.keys.available,
                "keyboard_held": self.keys.kb.acquired,
                "lamps": len(self.keys),
                "dry_run": self.bar.bar.dry_run,
                "fast_write": self.bar.available and self.bar.bar.has_zones,
                # The user's keyboard-backlight slider, which scales both.
                "master_brightness": self.keys.master,
                "power": ("battery" if self._on_battery() else "mains"
                          if self.power_watcher and self.power_watcher.known
                          else None),
                "idle": {
                    "enabled": bool(self._idle_settings().get("enabled")),
                    "wake_for_alerts": bool(self._idle_settings().get("wake_for_alerts")),
                    "available": bool(self.idle_watcher and self.idle_watcher.available),
                    "seconds": (round(self.idle_watcher.seconds, 1)
                                if self.idle_watcher else None),
                    "level": round(self._idle_level),
                },
                # So the GUI's preview blends the bar exactly as the daemon does.
                "bar_blend": surface_mod.BAR_BLEND,
                "active": {k: (j.key if j else None) for k, j in self._active.items()},
                "queued": [j.key for j in self.jobs if not j.shown],
                # Remaining fade-back per surface, so "why did it snap?" is
                # answerable without watching the machine.
                "fading": {n: (None if not f else
                               round(max(0.0, f["dur"] - (time.monotonic() - f["start"])) * 1000))
                           for n, f in self._fades.items()},
                "base": {name: self._base_spec(name).get("kind") if self._base_spec(name) else None
                         for name in ("bar", "keys")},
                "bar_base": self._bar_base.to_dict() if self._bar_base else None,
                "last_error": self.last_error,
            }

    # -- base profiles ---------------------------------------------------

    def _base_spec(self, name: str) -> dict | None:
        spec = self.config.base_profile(name)
        return spec or None

    def _base_effect(self, name: str) -> fx.Effect | None:
        """The default look for a surface, compiled once and cached."""
        spec = self._base_spec(name)
        if spec is None:
            self._base_effects[name] = None
            return None
        cached = self._base_effects.get(name)
        if cached is not None and getattr(cached, "_spec_snapshot", None) == spec:
            return cached
        if str(spec.get("kind", "")).lower() == MIRROR:
            self._base_effects[name] = None
            return None
        try:
            effect = fx.build(spec)
        except ValueError as exc:
            log.error("base profile for %s is invalid: %s", name, exc)
            self._base_effects[name] = None
            return None
        effect._spec_snapshot = spec
        self._base_effects[name] = effect
        return effect

    def _bar_is_mirrored(self) -> bool:
        spec = self._base_spec("bar")
        return bool(spec) and str(spec.get("kind", "")).lower() == MIRROR

    def reload(self, config) -> None:
        with self.cv:
            self.config = config
            self._base_effects.clear()
            self._wake()

    # -- render loop -----------------------------------------------------

    def run(self) -> None:
        deadline = time.monotonic()
        animating = False
        while True:
            with self.cv:
                if not self.running:
                    break
                idle = not self.jobs and not any(
                    self._base_spec(n) for n in ("bar", "keys"))
                if idle:
                    # Nothing to paint at all. Everything that could give us
                    # work -- submit, release, reload, shutdown -- notifies, so
                    # the timeout is only insurance against a change we have no
                    # way of being told about.
                    self._restore_idle()
                    self._dormant = False   # no picture: input tells us nothing
                    self._last_tick = 0.0   # the base clock starts again with it
                    if not self._dirty:
                        self.cv.wait(timeout=DORMANT_S)
                    self._dirty = False
                    deadline = time.monotonic()
                    animating = False
                    continue
            try:
                moving_before = animating
                animating = self._tick()
                self.last_error = None
            except KeyboardUnavailable as exc:
                # The fd is dead even if the node still exists: force a
                # re-discovery instead of retrying through a stale handle.
                self.last_error = str(exc)
                self._keys_stale = True
                log.warning("keyboard lost: %s", exc)
                time.sleep(1.0)
                deadline = time.monotonic()
                continue
            except DeviceUnavailable as exc:
                self.last_error = str(exc)
                log.warning("lighting unavailable: %s", exc)
                time.sleep(1.0)
                deadline = time.monotonic()
                continue
            except Exception:
                log.exception("render tick failed")
                time.sleep(0.5)
                deadline = time.monotonic()
                continue

            if animating:
                self._dormant = False   # already watching closely
                if not moving_before:
                    # The picture has just started moving. Whatever deadline is
                    # left over belongs to the long sleep this frame cut short,
                    # and pacing the next frame against it would put the second
                    # frame of an alert seconds after its first.
                    deadline = time.monotonic()
                deadline += TICK_S
                wait = deadline - time.monotonic()
                if wait <= 0:
                    # Behind schedule: do not accrue debt, or the loop would
                    # spin trying to catch up frames it can never deliver.
                    deadline = time.monotonic()
                    continue
            else:
                # A still picture: sleep until something could change it.
                wait = self._still_wait()
            with self.cv:
                # Anything that arrived while the frame was being drawn is
                # already waiting in _dirty: act on it now rather than sleep
                # through it.
                if self.running and not self._dirty:
                    self.cv.wait(timeout=wait)
                self._dirty = False
        self._shutdown_surfaces()

    def _pick(self, surface: str) -> Job | None:
        """Highest-priority live job painting this surface."""
        best = None
        for job in self.jobs:
            if job.done or surface not in surfaces_of(job.target):
                continue
            if best is None or (job.priority, job.seq) > (best.priority, best.seq):
                best = job
        return best

    def _tick(self) -> bool:
        """Draw one frame, and say whether anything still has to move after it."""
        now = time.monotonic()
        # Real elapsed time, not the nominal tick: the loop sleeps for as long
        # as the picture allows, so an effect's clock has to come from the
        # machine's rather than from how often we happened to wake up.
        dt = (now - self._last_tick) if self._last_tick else 0.0
        self._last_tick = now
        # The user's slider can move between any two frames, and both surfaces
        # have to agree on where it is: the keyboard applies it in software,
        # the bar gets it from the driver on every write.
        master = self.bar.bar.master()
        self.keys.set_master(master)
        self.bar.set_master(master)
        dimming = self._update_idle()
        with self.cv:
            self.jobs = [j for j in self.jobs if not j.done]
            chosen = {name: self._pick(name) for name in ("bar", "keys")}
            self._active = dict(chosen)
            # The dim is one number for the machine, but an alert may lift it
            # on the surface it plays on, so each surface gets its own.
            for name, surface in (("bar", self.bar), ("keys", self.keys)):
                level, moving = self._surface_idle(name, chosen[name], now)
                surface.set_idle(level)
                dimming = dimming or moving
            live = {id(j) for j in chosen.values() if j}
            # Only the jobs actually on screen age, so a queued notification
            # waits its turn instead of expiring unseen behind a held effect.
            for job in {id(j): j for j in chosen.values() if j}.values():
                job.age(now)
            # An effect that had started and has now lost every surface it was
            # painting is dropped, not paused. Resuming it would replay a stale
            # message -- auth-fail coming back *after* auth-ok, say. Holds are
            # exempt: they exist to persist until something releases them.
            for job in self.jobs:
                if job.shown and not job.hold and id(job) not in live:
                    job.cancelled = True
            # An alert that just ended hands the surface over. Cutting straight
            # to what is underneath is a visible snap -- black to near-white in
            # one frame -- so fade across unless the effect asks not to.
            #
            # The test is "the previous job ended", not "nothing is on top now":
            # what comes next is often another job rather than the bare default
            # -- the GUI's live preview sits under every alert, for one -- and
            # keying on None meant the fade silently never ran while it was up.
            # A job that is merely preempted is still alive, and must cut in at
            # once, so it is excluded.
            for name in ("bar", "keys"):
                previous, current = self._previous[name], chosen[name]
                if previous is not None and previous is not current and previous.done:
                    self._start_fade(name, previous)
                self._previous[name] = current
            self._base_clock += dt

        keys_written = self._render_keys(chosen["keys"])
        self._render_bar(chosen["bar"], forced=keys_written)
        return dimming or self._moving(chosen)

    def _effect_for(self, name: str, job: Job | None):
        """What is painting a surface: the overlay on it, or its default look."""
        return job.effect if job is not None else self._base_effect(name)

    def _moving(self, chosen: dict) -> bool:
        """Whether the next frame could differ from the one just drawn.

        Only what is really on screen counts. An animated profile for a surface
        the machine does not have is not motion, and neither is one on a
        keyboard we have currently lost -- otherwise a missing device would
        hold the loop at full rate for ever.
        """
        for name in ("bar", "keys"):
            surface = self.keys if name == "keys" else self.bar
            if not surface.available:
                continue
            if self._fades.get(name):
                return True
            effect = self._effect_for(name, chosen[name])
            if effect is not None and effect.animated:
                return True
        return False

    def _still_wait(self) -> float:
        """How long the picture on screen can be left to itself.

        Nothing is animating, so only three things can still change it, and
        each is asked when it will happen: a job that runs out, the idle dim
        falling due, and the user's brightness slider. The slider is the odd
        one out -- sysfs offers nothing to wait on, so it has to be polled --
        but it can only move while there is a hand at the machine, and the idle
        watch already knows whether there is one. Once there is not, the poll
        becomes a formality and this loop all but stops.
        """
        now = time.monotonic()
        waits = []
        for job in {id(j): j for j in self._active.values() if j}.values():
            if job.deadline is not None:
                waits.append(job.deadline - now)
            if not job.hold and job.effect.duration is not None:
                waits.append(job.effect.duration - job.elapsed)

        present = True
        watcher = self.idle_watcher
        if watcher is not None and watcher.available:
            quiet = watcher.seconds
            # A plug going in or out counts as a hand on the slider: the
            # desktop moves the user's level a moment later, and that has to
            # be picked up whether or not anybody is there.
            present = (quiet < PRESENCE_S
                       or now - self._power_changed < PRESENCE_S)
            settings = self._idle_settings()
            if settings.get("enabled") and self._idle_level >= 100.0:
                timeout = max(1.0, float(settings.get("timeout", 60)))
                if quiet < timeout:
                    waits.append(timeout - quiet)
        self._dormant = not present
        waits.append(STILL_S if present else DORMANT_S)

        # A keyboard we are trying to get back is the one thing here that has
        # to be looked for rather than waited on.
        if ((self._keys_stale or not self.keys.available)
                and self._effect_for("keys", self._active["keys"]) is not None):
            waits.append(KEYS_RETRY_S)
        return max(0.0, min(waits))

    def _on_input(self) -> None:
        """Somebody touched the machine; called from the idle watch's thread.

        Two cases are worth a wakeup: a dimmed picture, which has to come back
        at once, and a settled one the loop had stopped watching closely --
        whoever just arrived may be reaching for the brightness keys. While
        someone is already there and nothing is dimmed, this costs nothing.
        """
        if self._idle_level >= 100.0 and not self._dormant:
            return
        with self.cv:
            self._wake()

    def _on_power(self, on_battery: bool) -> None:
        """The plug went in or out; called from the power watch's thread.

        Two things follow from it. The idle settings in force may be a
        different set now -- a machine dark on battery has to light up to its
        mains level the moment the plug goes in, with nobody touching it --
        and the desktop is about to move the user's slider to its level for
        the new source, which the loop must not sleep through.
        """
        with self.cv:
            self._power_changed = time.monotonic()
            self._wake()

    def _on_battery(self) -> bool:
        watcher = self.power_watcher
        return bool(watcher is not None and watcher.on_battery)

    def _idle_settings(self) -> dict:
        return self.config.idle_for(self._on_battery())

    def _surface_idle(self, name: str, job: Job | None, now: float) -> tuple[float, bool]:
        """The idle level for one surface: the machine's, unless an alert lifts it.

        With ``wake_for_alerts`` on, a surface an alert is playing on renders
        at full while the alert lasts, and then eases back down to the dim over
        the same fade the dim itself uses -- a snap back to dark the instant
        the alert ends would read as the lights failing. Returns the level and
        whether it is still on its way somewhere.

        "Full" here is the *rendering* level; the user's own slider still
        multiplies everything, so a keyboard whose level is zero stays dark
        whatever arrives -- somebody who turned the lights off has said so.
        """
        level = self._idle_level
        settings = self._idle_settings()
        if level >= 100.0 or not settings.get("wake_for_alerts"):
            self._alert_seen[name] = None
            return level, False
        if job is not None and not job.stand_in:
            self._alert_seen[name] = now
            return 100.0, False
        seen = self._alert_seen[name]
        if seen is None:
            return level, False
        fade = max(0.0, float(settings.get("fade_ms", 1500))) / 1000.0
        progress = 1.0 if fade <= 0 else min(1.0, (now - seen) / fade)
        if progress >= 1.0:
            self._alert_seen[name] = None
            return level, False
        return 100.0 - (100.0 - level) * progress, True

    def _update_idle(self) -> bool:
        """Where the idle dim has got to, from the seconds since the last input.

        Returns whether the level is still on its way somewhere, which is one
        of the things that keeps the loop running at full rate.

        Asymmetric on purpose. Dimming is a slow fade nobody should catch
        happening; waking is instantaneous, because a keyboard that took a
        second to come back would feel like it was thinking about it. Deriving
        the fade from the idle clock rather than a timer of its own means there
        is no state to get out of step -- one keypress and the whole thing is
        back at full, whatever it was in the middle of.
        """
        settings = self._idle_settings()
        watcher = self.idle_watcher
        if not settings.get("enabled") or watcher is None or not watcher.available:
            self._idle_level = 100.0
            return False
        timeout = max(1.0, float(settings.get("timeout", 60)))
        idle_for = watcher.seconds
        if idle_for < timeout:
            self._idle_level = 100.0
            return False
        floor = max(0.0, min(100.0, float(settings.get("brightness", 20))))
        fade = max(0.0, float(settings.get("fade_ms", 1500))) / 1000.0
        progress = 1.0 if fade <= 0 else min(1.0, (idle_for - timeout) / fade)
        self._idle_level = 100.0 - (100.0 - floor) * progress
        return progress < 1.0

    def _fade_ms(self, job: Job) -> float:
        value = job.spec.get("fade_back_ms",
                             self.config.daemon.get("fade_back_ms", 300))
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            return 0.0

    def _start_fade(self, name: str, ending: Job) -> None:
        ms = self._fade_ms(ending)
        frame = self._last_frame.get(name)
        if ms <= 0 or not frame:
            self._fades[name] = None
            return
        surface = self.keys if name == "keys" else self.bar
        self._fades[name] = {"from": list(frame), "start": time.monotonic(),
                             "dur": ms / 1000.0,
                             "from_brightness": surface.brightness}

    @staticmethod
    def _render_time(job: Job | None, effect, base_clock: float) -> float:
        """When to sample the effect.

        A held effect loops. A finite one must never be sampled past its own
        end: the phase is a modulo, so one tick beyond the duration wraps back
        to the *first* frame -- which then gets picked up as the fade's source
        and makes the return start from the wrong picture entirely.
        """
        if job is None:
            return base_clock
        t = job.elapsed
        duration = effect.duration
        if not duration:
            return t
        if job.hold:
            return t % duration
        return min(t, duration - 1e-6)

    def _fade_progress(self, name: str) -> float | None:
        fade = self._fades.get(name)
        if not fade:
            return None
        k = (time.monotonic() - fade["start"]) / fade["dur"]
        if k >= 1.0:
            self._fades[name] = None
            return None
        return k

    def _fade_brightness(self, name: str, target: int | None) -> int | None:
        """Ramp brightness alongside the colours.

        An alert usually runs at full brightness over a dimmed default, so
        without this the level snaps at the very moment the colours start to
        fade -- which reads as a flicker rather than a hand-over.
        """
        fade = self._fades.get(name)
        if not fade or target is None:
            return target
        k = self._fade_progress(name)
        if k is None:
            return target
        start = fade.get("from_brightness")
        if start is None:
            return target
        return int(round(start + (target - start) * k))

    def _fade_frame(self, name: str, colors: list) -> list:
        """Blend from where the surface was towards where it is going."""
        fade = self._fades.get(name)
        k = self._fade_progress(name)
        if k is None:
            return colors
        source = fade["from"]
        black = (0, 0, 0)
        return [blend(source[i] if i < len(source) else black, c, k)
                for i, c in enumerate(colors)]

    def _recover_keys(self) -> bool:
        """Try to re-find the keyboard, at most once every KEYS_RETRY_S.

        Rate limited because discover() walks /sys/class/hidraw, which is far
        too expensive to do on every 33 ms tick while the device is genuinely
        absent.
        """
        now = time.monotonic()
        if now < self._keys_retry:
            return False
        self._keys_retry = now + KEYS_RETRY_S
        try:
            if not self.keys.reattach():
                return False
        except KeyboardUnavailable as exc:
            log.warning("keyboard still unavailable: %s", exc)
            return False
        self._keys_stale = False
        self.last_error = None
        log.info("keyboard back on %s", self.keys.kb.device)
        return True

    def _flatten(self, name: str, colors: list, surface, job: Job | None) -> list:
        """Resolve the transparent cells a layout leaves behind.

        A zone layout returns None for every cell no zone claimed. Under an
        overlay those cells must show the default look, so that an alert can
        light up one row without blanking the rest of the keyboard; with no
        default underneath, they are simply off.
        """
        if not any(c is None for c in colors):
            return colors
        under = None
        if job is not None:
            base = self._base_effect(name)
            if base is not None:
                under = surface.sample(base, self._base_clock)
                under = [c if c is not None else BLACK for c in under]
        if under is None:
            under = [BLACK] * len(colors)
        return [under[i] if c is None else c for i, c in enumerate(colors)]

    def _render_keys(self, job: Job | None) -> bool:
        if (self._keys_stale or not self.keys.available) and not self._recover_keys():
            return False
        effect = self._effect_for("keys", job)
        if effect is None:
            # Nobody wants the keyboard: give it back to the firmware.
            if self.keys.kb.acquired:
                self.keys.release()
            return False
        brightness = self._fade_brightness("keys", self._brightness_for(job, "keys"))
        if brightness is not None:
            self.keys.set_brightness(brightness)
        t = self._render_time(job, effect, self._base_clock)
        colors = self._flatten("keys", self.keys.sample(effect, t),
                               self.keys, job)
        colors = self._fade_frame("keys", colors)
        self._last_frame["keys"] = colors
        return self.keys.write(colors)

    def _render_bar(self, job: Job | None, forced: bool) -> None:
        if not self.bar.available:
            return
        # No overlay and no profile -- or a profile that says "let the firmware
        # mirror the keyboard" -- means nobody wants us writing the bar.
        effect = self._effect_for("bar", job)
        if effect is None:
            return
        if self._bar_base is None:
            self._capture_bar_base()
        brightness = self._brightness_for(job, "bar")
        if brightness is not None:
            self.bar.set_brightness(brightness)
        t = self._render_time(job, effect, self._base_clock)
        colors = self._flatten("bar", self.bar.sample(effect, t),
                               self.bar, job)
        colors = self._fade_frame("bar", colors)
        self._last_frame["bar"] = colors
        # `forced` because a keyboard write just happened and the firmware
        # mirror has overwritten whatever the bar was showing.
        self.bar.write(colors, force=forced or self._fades.get("bar") is not None)

    def _brightness_for(self, job: Job | None, surface: str) -> int | None:
        if job is not None:
            value = job.brightness
            if value is None:
                value = job.spec.get("brightness",
                                     self.config.daemon.get("effect_brightness"))
        else:
            spec = self._base_spec(surface) or {}
            value = spec.get("brightness")
        if value is None:
            return None
        value = int(value)
        return None if value < 0 else value

    # -- start / stop state ----------------------------------------------

    def _capture_bar_base(self) -> None:
        try:
            self._bar_base = self.bar.bar.capture()
            log.debug("bar base captured: %s", self._bar_base.to_dict())
        except DeviceUnavailable as exc:
            log.warning("could not capture the bar: %s", exc)

    def _restore_idle(self) -> None:
        """No profiles and no overlays: put everything back and stand down."""
        # Nothing of ours is lit, so there is no dim to be part-way through.
        self._idle_level = 100.0
        if self.keys.kb.acquired:
            try:
                self.keys.release()
            except KeyboardUnavailable as exc:
                log.warning("could not release the keyboard: %s", exc)
        if self._bar_base is not None:
            snap, self._bar_base = self._bar_base, None
            try:
                self.bar.bar.restore(snap)
            except DeviceUnavailable as exc:
                log.warning("could not restore the bar: %s", exc)

    def _shutdown_surfaces(self) -> None:
        try:
            self.keys.kb.close()
        except Exception:
            log.debug("keyboard close failed", exc_info=True)
        if self._bar_base is not None:
            try:
                self.bar.bar.restore(self._bar_base)
            except DeviceUnavailable as exc:
                log.warning("could not restore the bar on exit: %s", exc)
            self._bar_base = None
