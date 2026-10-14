"""The day plan: a rough outline of what music you want when, read by the orchestrator.

It lives in ``day_plan.toml`` (project root), so you can edit it by hand, on the
GUI's Day page, or through the curator. Times can be fixed (``"13:00"``) or
relative to when you wake up or go to bed (``"wake"``, ``"wake+90m"``,
``"sleep-1h"``), so a later weekend wake-up moves the morning with it.

The file is read again on every check, so edits apply straight away.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

from music_agent.config import PROJECT_ROOT

DAY_PLAN_PATH = PROJECT_ROOT / "day_plan.toml"
DAYS = ("all", "weekdays", "weekend")
_RELATIVE = re.compile(r"^(wake|sleep)\s*(?:([+-])\s*(\d+)\s*(h|m|min)?)?$")

HEADER = """\
# The day plan: a rough outline of the music you want through the day.
# The orchestrator reads it every few minutes and asks the DJ for fitting music.
# Edit it here, on the GUI's Day page, or ask the curator. Changes apply at the next check.
#
# Times are "HH:MM", or relative to waking up / going to bed: "wake", "wake+90m", "sleep-1h".
# A slot lasts until the next one starts. days = "all", "weekdays" or "weekend".
# quiet = true means: don't start music then (music already playing is left alone).
# rooms = ["Night"] plays only there (leave it out for the default rooms).
"""


@dataclass
class Slot:
    """One part of the day.

    Attributes:
        start: ``"HH:MM"``, or ``"wake"`` / ``"sleep"`` with an optional offset
            like ``"+90m"`` or ``"-1h"``.
        mood: What you want then, in your own words (the rougher the better).
        days: ``"all"``, ``"weekdays"`` or ``"weekend"``.
        rooms: Only these rooms; empty for the default.
        quiet: Don't start music during this slot.
    """

    start: str
    mood: str = ""
    days: str = "all"
    rooms: list[str] = field(default_factory=list)
    quiet: bool = False


@dataclass
class DayPlan:
    """The whole outline plus the orchestrator's settings.

    Attributes:
        enabled: The orchestrator runs only when this is true.
        wake: When you get up on weekdays, ``"HH:MM"``.
        sleep: When music should end on weekdays.
        weekend_wake: Wake-up on Saturday and Sunday.
        weekend_sleep: Bedtime on Saturday and Sunday.
        check_every_minutes: How often the speakers are checked (cheap; Claude
            is only asked when something needs doing).
        quiet_after_stop_hours: After you stop or pause the music, stay quiet this long.
        respect_requests_minutes: Music you asked for yourself isn't replaced
            when a new slot starts until it is this old.
        slots: The parts of the day.
    """

    enabled: bool = False
    wake: str = "08:00"
    sleep: str = "23:00"
    weekend_wake: str = "09:30"
    weekend_sleep: str = "23:30"
    check_every_minutes: int = 5
    quiet_after_stop_hours: float = 3
    respect_requests_minutes: int = 120
    slots: list[Slot] = field(default_factory=list)

    # ------------------------------------------------------------ reading the plan
    def is_weekend(self, day: date) -> bool:
        """Saturday or Sunday."""
        return day.weekday() >= 5

    def wake_sleep(self, day: date) -> tuple[time, time]:
        """Wake-up and bedtime on this day."""
        if self.is_weekend(day):
            return parse_clock(self.weekend_wake), parse_clock(self.weekend_sleep)
        return parse_clock(self.wake), parse_clock(self.sleep)

    def slots_for(self, day: date) -> list[tuple[time, Slot]]:
        """The slots that apply on this day with their start times, earliest first."""
        wake, sleep = self.wake_sleep(day)
        weekend = self.is_weekend(day)
        out = [(resolve_time(s.start, wake, sleep), s) for s in self.slots
               if s.days == "all" or (s.days == "weekend") == weekend]
        return sorted(out, key=lambda pair: pair[0])

    def awake(self, now: datetime) -> bool:
        """Between wake-up and bedtime."""
        wake, sleep = self.wake_sleep(now.date())
        return wake <= now.time() < sleep

    def current(self, now: datetime) -> tuple[time, Slot] | None:
        """The slot in effect now (the first one before it starts), or None outside waking hours."""
        if not self.awake(now):
            return None
        slots = self.slots_for(now.date())
        started = [pair for pair in slots if pair[0] <= now.time()]
        return started[-1] if started else (slots[0] if slots else None)

    def upcoming(self, now: datetime) -> tuple[time, Slot] | None:
        """The next slot to start today, if any."""
        sleep = self.wake_sleep(now.date())[1]
        return next(((t, s) for t, s in self.slots_for(now.date()) if now.time() < t < sleep), None)

    def today(self, now: datetime) -> dict[str, Any]:
        """Today's outline in plain data: wake, sleep and each slot with its time."""
        wake, sleep = self.wake_sleep(now.date())
        current = self.current(now)
        return {
            "date": now.strftime("%A %Y-%m-%d"), "weekend": self.is_weekend(now.date()),
            "wake": f"{wake:%H:%M}", "sleep": f"{sleep:%H:%M}",
            "slots": [{"time": f"{t:%H:%M}", **slot_dict(s), "now": current is not None and current[1] is s}
                      for t, s in self.slots_for(now.date())],
        }

    def as_dict(self) -> dict[str, Any]:
        """The plan as plain data (what the GUI edits and the file stores)."""
        return asdict(self)


# ------------------------------------------------------------ parsing and checking
def parse_clock(text: str) -> time:
    """``"07:30"`` -> ``time(7, 30)``.

    Raises:
        ValueError: Not a time of day.
    """
    try:
        return datetime.strptime(str(text).strip(), "%H:%M").time()
    except ValueError:
        raise ValueError(f"'{text}' is not a time like 07:30") from None


def resolve_time(start: str, wake: time, sleep: time) -> time:
    """A slot's start on a day with this wake-up and bedtime.

    Args:
        start: ``"HH:MM"``, ``"wake"``, ``"wake+90m"``, ``"sleep-1h"``, ...
        wake: Wake-up that day.
        sleep: Bedtime that day.

    Raises:
        ValueError: Not a time this understands, or it falls outside the day.
    """
    text = str(start).strip().lower()
    m = _RELATIVE.match(text)
    if not m:
        return parse_clock(text)
    base, sign, amount, unit = m.groups()
    minutes = int(amount or 0) * (60 if unit == "h" else 1) * (-1 if sign == "-" else 1)
    anchor = datetime.combine(date(2000, 1, 2), wake if base == "wake" else sleep)
    moved = anchor + timedelta(minutes=minutes)
    if moved.date() != anchor.date():
        raise ValueError(f"'{start}' falls outside the day")
    return moved.time()


def from_dict(data: dict[str, Any]) -> DayPlan:
    """Build and check a plan from plain data (the file, the GUI or the curator).

    Raises:
        ValueError: Anything wrong, with a message saying what.
    """
    known = set(DayPlan.__dataclass_fields__)
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"Unknown day plan setting(s): {', '.join(sorted(unknown))}")
    slots = []
    for i, raw in enumerate(data.get("slots") or [], 1):
        if not isinstance(raw, dict):
            raise ValueError(f"Slot {i} must be a table of settings")
        extra = set(raw) - set(Slot.__dataclass_fields__)
        if extra:
            raise ValueError(f"Slot {i} has unknown setting(s): {', '.join(sorted(extra))}")
        rooms = raw.get("rooms") or []
        if isinstance(rooms, str):
            rooms = [r.strip() for r in rooms.split(",") if r.strip()]
        slots.append(Slot(start=str(raw.get("start", "")).strip(), mood=str(raw.get("mood") or "").strip(),
                          days=str(raw.get("days") or "all"), rooms=[str(r) for r in rooms],
                          quiet=bool(raw.get("quiet", False))))
    plan = DayPlan(**{**{k: v for k, v in data.items() if k != "slots"}, "slots": slots})
    plan.enabled = bool(plan.enabled)
    check(plan)
    return plan


def check(plan: DayPlan) -> None:
    """Raise ValueError unless the plan makes sense."""
    for day in (date(2026, 1, 5), date(2026, 1, 10)):        # a Monday and a Saturday
        wake, sleep = plan.wake_sleep(day)
        if not wake < sleep:
            raise ValueError("Bedtime must be after wake-up on the same day")
    if not 1 <= int(plan.check_every_minutes) <= 60:
        raise ValueError("check_every_minutes must be 1-60")
    if not 0 <= float(plan.quiet_after_stop_hours) <= 24:
        raise ValueError("quiet_after_stop_hours must be 0-24")
    if not 0 <= int(plan.respect_requests_minutes) <= 24 * 60:
        raise ValueError("respect_requests_minutes must be 0-1440")
    for i, s in enumerate(plan.slots, 1):
        if s.days not in DAYS:
            raise ValueError(f"Slot {i}: days must be one of {', '.join(DAYS)}")
        if not s.quiet and not s.mood:
            raise ValueError(f"Slot {i} ({s.start}) needs a mood, or quiet = true")
        for day in (date(2026, 1, 5), date(2026, 1, 10)):
            try:
                resolve_time(s.start, *plan.wake_sleep(day))
            except ValueError as exc:
                raise ValueError(f"Slot {i}: {exc}") from None


def slot_dict(s: Slot) -> dict[str, Any]:
    """A slot as compact data (empty fields left out)."""
    d: dict[str, Any] = {"start": s.start, "mood": s.mood}
    if s.days != "all":
        d["days"] = s.days
    if s.rooms:
        d["rooms"] = s.rooms
    if s.quiet:
        d["quiet"] = True
    return d


# ------------------------------------------------------------ the file
def load(path: Path = DAY_PLAN_PATH) -> DayPlan:
    """Read the plan; a missing file is a disabled, empty plan.

    Raises:
        ValueError: The file isn't valid TOML or the plan doesn't make sense.
    """
    if not path.exists():
        return DayPlan()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path.name} is not valid TOML: {exc}") from None
    return from_dict(data)


def save(plan: DayPlan, path: Path = DAY_PLAN_PATH) -> None:
    """Check and write the plan (comments are replaced by the standard header)."""
    check(plan)
    lines = [HEADER]
    for key, value in plan.as_dict().items():
        if key != "slots":
            lines.append(f"{key} = {_toml(value)}")
    for s in plan.slots:
        lines += ["", "[[slots]]", f"start = {_toml(s.start)}", f"mood = {_toml(s.mood)}"]
        if s.days != "all":
            lines.append(f"days = {_toml(s.days)}")
        if s.rooms:
            lines.append(f"rooms = {_toml(s.rooms)}")
        if s.quiet:
            lines.append("quiet = true")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)           # never leave a half-written plan


def _toml(value: Any) -> str:
    """One value in TOML syntax (only the types a plan uses)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    return f'"{text}"'
