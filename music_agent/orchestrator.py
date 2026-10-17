"""The orchestrator's clock: when to wake the orchestrator agent during the day.

Checking the speakers is cheap; asking Claude isn't. So code decides *when*
something needs doing (``decide``), and only then the orchestrator agent decides
*what* to ask the DJ for. It is woken when:

- nothing is playing during waking hours (e.g. at wake-up, or a queue ran out),
- the queue is almost finished (``TOP_UP_TRACKS`` or fewer left),
- a new part of the day plan started, unless you chose the current music
  yourself recently (``respect_requests_minutes`` in the plan).

At bedtime, if the music still playing is the orchestrator's own (the latest
queued tracks came from it), the volume is faded out over ``FADE_SECONDS`` and
the music stopped; the volume is then put back, silently, so the next music
doesn't start at zero. Music you chose yourself is left alone, and it fades at
most once a night, so pressing play again isn't fought.

It stays out of your way: after you stop or pause the music (from the DJ, the
GUI, or the Sonos app) it starts nothing for ``quiet_after_stop_hours``, and it
never stops music. Asking for music ends the quiet early.

Volume is handled here in code (the agent can't touch it), gently, so it never
overrides you: at the first check each morning (up to ``MORNING_RESET_HOURS``
after waking) it is reset to the plan's ``morning_volume``, so last night's level
doesn't carry over. After that, when a part of the day begins, its
``volume_change`` is *added* to whatever the volume is then, so a change you
made yourself is kept. A running total (``_volume_mark``) catches up on parts
that began between checks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from music_agent import dayplan
from music_agent.app import App
from music_agent.dayplan import DayPlan, Slot

log = logging.getLogger(__name__)

TOP_UP_TRACKS = 2          # ask for more when this many tracks or fewer are left
MIN_GAP_MINUTES = 10       # don't wake the agent again sooner than this after it acted
MAX_RUNS_PER_DAY = 40      # a safety net against a loop that keeps asking Claude
FADE_SECONDS = 60          # how long the bedtime fade-out takes
FADE_STEPS = 12
FADE_ONCE_HOURS = 12       # after a bedtime fade, don't fade again for this long (you pressed play again)
MORNING_RESET_HOURS = 2    # the morning volume reset happens only this long after waking (not after a mid-day restart)
PLAYING_STATES = ("PLAYING", "TRANSITIONING")

RunAgent = Callable[[str], Awaitable[dict[str, Any]]]   # message -> {"reply", "steps", "error"}


@dataclass
class Situation:
    """Everything ``decide`` looks at, gathered once per check.

    Attributes:
        now: Local time.
        plan: The day plan.
        playing: Music is playing.
        remaining: Tracks left in the queue after the current one, if known.
        was_playing: Music was playing at the previous check (None: no previous check).
        was_remaining: Tracks left at the previous check.
        quiet_until: The person stopped the music; stay quiet until then.
        user_music_at: When the person last started music themselves (not the orchestrator).
        music_is_ours: The latest queued tracks came from the orchestrator (its DJ).
        faded_at: When the orchestrator last faded the music out at bedtime.
        last_action: When the orchestrator last asked the DJ for music.
        acted_slot: The slot (``slot_key``) the orchestrator last played for.
        in_safety_quiet: Inside the quiet hours from config.toml.
    """

    now: datetime
    plan: DayPlan
    playing: bool
    remaining: int | None = None
    was_playing: bool | None = None
    was_remaining: int | None = None
    quiet_until: datetime | None = None
    user_music_at: datetime | None = None
    music_is_ours: bool = False
    faded_at: datetime | None = None
    last_action: datetime | None = None
    acted_slot: str | None = None
    in_safety_quiet: bool = False


@dataclass
class Decision:
    """What a check decided.

    Attributes:
        action: ``"idle"`` (nothing to do), ``"quiet"`` (someone stopped the
            music: start a quiet period), ``"unquiet"`` (they started it again),
            ``"adopt"`` (remember the current slot without acting), ``"bedtime"``
            (fade out the orchestrator's music), or a reason to wake the agent:
            ``"start"``, ``"top_up"`` or ``"new_slot"``.
        reason: In words, for the log, the GUI and the agent.
        slot: The slot in effect, if any.
        slot_time: When that slot started today.
    """

    action: str
    reason: str
    slot: Slot | None = None
    slot_time: str | None = None

    @property
    def wakes_agent(self) -> bool:
        """True if Claude should be asked."""
        return self.action in ("start", "top_up", "new_slot")


def slot_key(now: datetime, slot: Slot) -> str:
    """Identifies a slot on a given day (a new day is a new slot)."""
    return f"{now:%Y-%m-%d}|{slot.start}|{slot.mood}"


def decide(s: Situation) -> Decision:
    """Decide what a check should do. Pure: no speakers, no Claude, no clock.

    Args:
        s: The situation.

    Returns:
        The decision.
    """
    plan, now = s.plan, s.now
    if not plan.enabled:
        return Decision("idle", "the day plan is turned off")
    wake, sleep = plan.wake_sleep(now.date())
    if not plan.awake(now):
        faded_lately = s.faded_at is not None and now - s.faded_at < timedelta(hours=FADE_ONCE_HOURS)
        if s.playing and s.music_is_ours and not faded_lately:
            return Decision("bedtime", "bedtime: fading out the day plan's music")
        return Decision("idle", f"outside waking hours ({wake:%H:%M}-{sleep:%H:%M})")
    if s.in_safety_quiet:
        return Decision("idle", "inside the quiet hours from config.toml")

    stopped = s.was_playing and not s.playing and (s.was_remaining is None or s.was_remaining > 1)
    started = s.was_playing is False and s.playing
    if s.quiet_until and now < s.quiet_until:
        if started:
            return Decision("unquiet", "music was started again, so the quiet period is over")
        return Decision("idle", f"staying quiet until {s.quiet_until:%H:%M} because the music was stopped")
    if stopped:
        return Decision("quiet", "the music was stopped or paused")

    current = plan.current(now)
    if current is None:
        return Decision("idle", "the day plan has no slots")
    at, slot = current
    here = {"slot": slot, "slot_time": f"{at:%H:%M}"}
    if s.last_action and now - s.last_action < timedelta(minutes=MIN_GAP_MINUTES):
        return Decision("idle", "asked the DJ only a few minutes ago", **here)
    if slot.quiet:
        if s.acted_slot != slot_key(now, slot):
            return Decision("adopt", "a quiet part of the day: no music is started", **here)
        return Decision("idle", "a quiet part of the day", **here)
    if not s.playing:
        return Decision("start", "nothing is playing", **here)
    if s.remaining is not None and s.remaining <= TOP_UP_TRACKS:
        return Decision("top_up", f"the queue is almost finished ({s.remaining} track(s) left)", **here)
    if s.acted_slot != slot_key(now, slot):
        if s.acted_slot is None:
            return Decision("adopt", "music is already playing; the plan takes over from here", **here)
        respect = timedelta(minutes=int(plan.respect_requests_minutes))
        if s.user_music_at and now - s.user_music_at < respect:
            return Decision("idle", "a new part of the day started, but you chose the current music "
                                    "recently, so it keeps playing", **here)
        return Decision("new_slot", "a new part of the day started", **here)
    return Decision("idle", "music is playing as planned", **here)


class Orchestrator:
    """Checks the speakers every few minutes and wakes the orchestrator agent when needed.

    Args:
        app: Speakers, memory, config and the day plan's path.
        run_agent: Sends a message to the orchestrator agent and returns
            ``{"reply", "steps", "error"}`` (the runtime provides it).
        now_fn: Clock, replaceable in tests.

    Attributes:
        history: The latest checks that did something, newest last.
        last_check: The latest check, whatever it decided.
    """

    def __init__(self, app: App, run_agent: RunAgent, now_fn: Callable[[], datetime] = datetime.now):
        self.app = app
        self._run_agent = run_agent
        self._now = now_fn
        self.history: deque[dict[str, Any]] = deque(maxlen=30)
        self.last_check: dict[str, Any] | None = None
        self._seen: tuple[bool, int | None] | None = None   # (playing, remaining) at the previous check
        self._acted_slot: str | None = None
        self._reset_day: date | None = None    # the day the morning volume was handled
        self._volume_mark = 0                   # today's volume changes already applied (see volume_changes_until)
        self._last_action: datetime | None = None
        self._faded_at: datetime | None = None
        self.fade_seconds: float = FADE_SECONDS     # shortened in tests
        self._runs: dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        """True while the background loop runs."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Start checking in the background (does nothing if already running)."""
        if not self.running:
            self._task = asyncio.create_task(self._loop(), name="orchestrator")

    async def stop(self) -> None:
        """Stop checking."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def plan(self) -> DayPlan:
        """The day plan, read fresh from its file (so edits apply at the next check)."""
        return dayplan.load(self.app.day_plan_path)

    async def _loop(self) -> None:
        """Check, sleep, repeat. Errors are logged, never fatal."""
        while True:
            minutes = 5
            try:
                minutes = self.plan().check_every_minutes
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Orchestrator check failed")
            await asyncio.sleep(max(1, minutes) * 60)

    async def check(self) -> dict[str, Any]:
        """Look at the speakers and the plan, and act if needed.

        Returns:
            ``{"time", "action", "reason", ...}``; when the agent was asked,
            also its ``reply`` and the DJ's ``steps``.

        Raises:
            ValueError: The day plan file is invalid.
        """
        async with self._lock:
            situation = await self._situation()
            if isinstance(situation, str):     # the speakers couldn't be read
                self.last_check = {"time": self._now().isoformat(timespec="seconds"), "action": "idle",
                                   "reason": situation}
                return self.last_check
            decision = decide(situation)
            entry: dict[str, Any] = {"time": situation.now.isoformat(timespec="seconds"),
                                     "action": decision.action, "reason": decision.reason,
                                     **await self._follow_volume(situation)}
            if decision.slot:
                entry["slot"] = {"time": decision.slot_time, **dayplan.slot_dict(decision.slot)}
            memory = self.app.memory
            if decision.action == "quiet" and memory:
                await asyncio.to_thread(memory.start_quiet, "speakers")
            elif decision.action == "unquiet" and memory:
                await asyncio.to_thread(memory.end_quiet)
            elif decision.action == "bedtime":
                entry.update(await self._fade_out(situation.now))
            elif decision.action == "adopt" and decision.slot:
                self._acted_slot = slot_key(situation.now, decision.slot)
            elif decision.wakes_agent:
                entry.update(await self._wake(situation, decision))
            self.last_check = entry
            if decision.action != "idle" or "volume" in entry:
                self.history.append(entry)
                log.info("Orchestrator: %s (%s)%s%s", decision.action, decision.reason,
                         f"; volume {entry['volume']} ({entry['volume_note']})" if "volume" in entry else "",
                         f": {entry['reply']}" if entry.get("reply") else "")
            return entry

    async def _situation(self) -> Situation | str:
        """Gather what ``decide`` needs: the plan, the speakers, and memory.

        Returns:
            The situation, or why it couldn't be read (then nothing is done).
        """
        now = self._now()
        plan = await asyncio.to_thread(self.plan)
        playing, remaining = False, None
        try:
            np = await asyncio.to_thread(self.app.player.now_playing)
            playing = np.state in PLAYING_STATES
            if np.queue_position and np.queue_length:
                remaining = max(0, np.queue_length - np.queue_position)
        except Exception as exc:     # speakers unreachable: don't act on a guess
            log.warning("Orchestrator couldn't read the speakers: %s", exc)
            return f"couldn't reach the speakers ({exc})"
        was = self._seen
        self._seen = (playing, remaining)
        quiet_until = user_music_at = None
        music_is_ours = False
        memory = self.app.memory
        if memory:
            since = await asyncio.to_thread(memory.quiet_since)
            if since:
                quiet_until = since + timedelta(hours=float(plan.quiet_after_stop_hours))
            last = await asyncio.to_thread(memory.last_play)
            music_is_ours = bool(last and last["source"] == "auto")
            if last and last["source"] != "auto":
                with contextlib.suppress(ValueError, TypeError):
                    user_music_at = datetime.fromisoformat(last["ts"])
        return Situation(now, plan, playing, remaining,
                         was_playing=was[0] if was else None, was_remaining=was[1] if was else None,
                         quiet_until=quiet_until, user_music_at=user_music_at, last_action=self._last_action,
                         music_is_ours=music_is_ours, faded_at=self._faded_at,
                         acted_slot=self._acted_slot,
                         in_safety_quiet=self.app.config.safety.in_quiet_hours(now.time()))

    async def _wake(self, s: Situation, d: Decision) -> dict[str, Any]:
        """Ask the orchestrator agent to act on a decision."""
        today = f"{s.now:%Y-%m-%d}"
        if self._runs.get(today, 0) >= MAX_RUNS_PER_DAY:
            return {"action": "idle", "reason": f"already asked Claude {MAX_RUNS_PER_DAY} times today"}
        self._runs = {today: self._runs.get(today, 0) + 1}
        self._last_action = s.now
        self._acted_slot = slot_key(s.now, d.slot)
        result = await self._run_agent(self.message(s, d))
        return {"reply": result.get("reply", ""), "steps": result.get("steps", []),
                **({"error": result["error"]} if result.get("error") else {})}

    async def _fade_out(self, now: datetime) -> dict[str, Any]:
        """Lower the volume step by step, stop, then put the volume back (silently).

        Returns:
            ``{"volume_note"}``, or ``{"error"}`` if the speakers refused (it's
            tried again at the next check).
        """
        player = self.app.player
        try:
            start = (await asyncio.to_thread(player.now_playing)).volume
            for i in range(1, FADE_STEPS + 1):
                await asyncio.to_thread(player.set_volume, round(start * (FADE_STEPS - i) / FADE_STEPS))
                await asyncio.sleep(self.fade_seconds / FADE_STEPS)
            await asyncio.to_thread(player.stop)
            await asyncio.to_thread(player.set_volume, start)
        except Exception as exc:     # leave it be; the next check tries again
            log.warning("Orchestrator couldn't fade out at bedtime: %s", exc)
            return {"error": f"couldn't fade out: {exc}"}
        self._faded_at = now
        self._seen = (False, None)   # our own stop, not the person's
        return {"volume_note": f"faded out and stopped; volume back at {start} for next time"}

    async def _follow_volume(self, s: Situation) -> dict[str, Any]:
        """Reset the volume in the morning, then add each part's change as it begins.

        Returns:
            ``{"volume", "volume_note"}`` when the volume was changed, else ``{}``.
        """
        plan, now = s.plan, s.now
        if not plan.enabled or not plan.awake(now) or s.in_safety_quiet:
            return {}
        changes = plan.volume_changes_until(now)
        if self._reset_day != now.date():                  # the first check today
            self._reset_day, self._volume_mark = now.date(), changes
            wake = datetime.combine(now.date(), plan.wake_sleep(now.date())[0])
            if plan.morning_volume is None or now - wake > timedelta(hours=MORNING_RESET_HOURS):
                return {}
            target = plan.morning_volume + changes
            applied = await self._volume(self.app.player.set_volume, target)
            return {"volume": applied, "volume_note": f"morning reset to {target}"} if applied is not None else {}
        step = changes - self._volume_mark
        if not step:
            return {}
        applied = await self._volume(self.app.player.change_volume, step)
        if applied is None:
            return {}                                       # try again at the next check
        self._volume_mark = changes
        return {"volume": applied, "volume_note": f"{step:+d} for the new part of the day"}

    async def _volume(self, fn: Callable[[int], int], value: int) -> int | None:
        """Set or change the group volume (capped by the safety limits); None if the speakers refused."""
        try:
            return await asyncio.to_thread(fn, value)
        except Exception as exc:     # the music matters more than the volume: carry on
            log.warning("Orchestrator couldn't change the volume (%s): %s", value, exc)
            return None

    def message(self, s: Situation, d: Decision) -> str:
        """The note that wakes the agent: why, which part of the day, and what's playing."""
        plan = s.plan
        upcoming = plan.upcoming(s.now)
        user_chose = (s.user_music_at is not None
                      and s.now - s.user_music_at < timedelta(minutes=int(plan.respect_requests_minutes)))
        lines = [
            f"Automatic check, {s.now:%A %H:%M}.",
            f"Why you're asked: {d.reason} ({d.action}).",
            f"Part of the day now (since {d.slot_time}): {d.slot.mood}"
            + (f" [rooms: {', '.join(d.slot.rooms)}]" if d.slot.rooms else ""),
            f"Next: {upcoming[1].mood} at {upcoming[0]:%H:%M}." if upcoming else "Next: bedtime, nothing after this.",
            f"Queue: {s.remaining} track(s) left after the current one." if s.playing and s.remaining is not None
            else ("Queue: playing." if s.playing else "Nothing is playing."),
        ]
        if user_chose:
            lines.append(f"The person chose the current music themselves at {s.user_music_at:%H:%M}.")
        return "\n".join(lines)

    def status(self) -> dict[str, Any]:
        """For the GUI: running, the latest check, recent actions and today's run count."""
        return {"running": self.running, "busy": self._lock.locked(), "last_check": self.last_check,
                "history": list(reversed(self.history)),
                "runs_today": self._runs.get(f"{self._now():%Y-%m-%d}", 0), "max_runs_per_day": MAX_RUNS_PER_DAY}

