"""The day plan file: times relative to waking up, slots per day, checking and saving."""
from __future__ import annotations

from datetime import datetime, time

import pytest

from music_agent import dayplan
from music_agent.config import PROJECT_ROOT
from music_agent.dayplan import DayPlan, Slot, from_dict, resolve_time

WEDNESDAY = datetime(2026, 10, 14, 9, 0)
SATURDAY = datetime(2026, 10, 17, 9, 0)


def plan(**kw):
    slots = [Slot("wake", "gentle"), Slot("wake+1h", "bright"), Slot("10:00", "focus", days="weekdays"),
             Slot("11:00", "lazy", days="weekend"), Slot("sleep-1h", "", quiet=True)]
    return DayPlan(**{"enabled": True, "slots": slots, **kw})


def test_times_relative_to_waking_up_and_bedtime():
    wake, sleep = time(8), time(23)
    assert resolve_time("wake", wake, sleep) == time(8)
    assert resolve_time("wake+90m", wake, sleep) == time(9, 30)
    assert resolve_time("WAKE + 2h", wake, sleep) == time(10)
    assert resolve_time("sleep-1h", wake, sleep) == time(22)
    assert resolve_time("14:00", wake, sleep) == time(14)
    for bad in ("noon", "wake+30x", "25:00"):
        with pytest.raises(ValueError):
            resolve_time(bad, wake, sleep)
    with pytest.raises(ValueError, match="outside the day"):
        resolve_time("sleep+2h", wake, sleep)


def test_current_part_of_the_day_follows_weekday_and_weekend():
    p = plan()
    assert [s.mood for _, s in p.slots_for(WEDNESDAY.date())] == ["gentle", "bright", "focus", ""]
    assert p.current(WEDNESDAY)[1].mood == "bright"                       # 09:00 = wake+1h
    assert p.current(WEDNESDAY.replace(hour=12))[1].mood == "focus"
    assert p.upcoming(WEDNESDAY)[1].mood == "focus"
    assert p.current(SATURDAY) is None                                   # still asleep at 9:00 on Saturday
    assert p.current(SATURDAY.replace(hour=10))[1].mood == "gentle"      # woke at 9:30
    assert p.current(SATURDAY.replace(hour=12))[1].mood == "lazy"
    assert p.current(WEDNESDAY.replace(hour=22, minute=30))[1].quiet
    assert p.current(WEDNESDAY.replace(hour=23, minute=30)) is None       # after bedtime
    today = p.today(WEDNESDAY)
    assert today["wake"] == "08:00" and [s["now"] for s in today["slots"]] == [False, True, False, False]


def test_save_and_load_round_trip(tmp_path):
    path = tmp_path / "day_plan.toml"
    p = plan()
    p.slots[0].mood = 'soft "warm" music'
    p.slots[1].rooms = ["Morn’", "Night"]
    p.slots[1].volume_change = -2
    p.morning_volume = 9
    dayplan.save(p, path)
    assert dayplan.load(path) == p
    assert path.read_text(encoding="utf-8").startswith("# The day plan")


def test_the_real_plan_file_is_valid():
    assert dayplan.load(PROJECT_ROOT / "day_plan.toml").slots


def test_missing_file_is_an_empty_plan_that_is_off(tmp_path):
    assert dayplan.load(tmp_path / "nope.toml") == DayPlan()


@pytest.mark.parametrize(("change", "message"), [
    ({"wake": "7"}, "not a time"),
    ({"sleep": "07:00"}, "Bedtime must be after"),
    ({"colour": "blue"}, "Unknown day plan setting"),
    ({"check_every_minutes": 0}, "check_every_minutes"),
    ({"slots": [{"start": "wake"}]}, "needs a mood"),
    ({"slots": [{"start": "wake", "mood": "x", "days": "mondays"}]}, "days must be"),
    ({"slots": [{"start": "sleep+3h", "mood": "x"}]}, "outside the day"),
    ({"slots": [{"start": "wake", "mood": "x", "colour": 9}]}, "unknown setting"),
    ({"morning_volume": 120}, "morning_volume must be 0-100"),
    ({"slots": [{"start": "wake", "mood": "x", "volume_change": 40}]}, "between -30 and \\+30"),
    ({"slots": [{"start": "wake", "mood": "x", "volume_change": "loud"}]}, "whole number"),
])
def test_bad_plans_are_refused_with_a_reason(change, message):
    with pytest.raises(ValueError, match=message):
        from_dict({**plan().as_dict(), **change})


def test_rooms_may_be_typed_as_text():
    p = from_dict({"slots": [{"start": "wake", "mood": "x", "rooms": "Night, Morn"}]})
    assert p.slots[0].rooms == ["Night", "Morn"]


def test_volume_changes_add_up_through_the_day():
    p = from_dict({"morning_volume": "8", "slots": [
        {"start": "wake", "mood": "a"}, {"start": "wake+1h", "mood": "b", "volume_change": "+4"},
        {"start": "14:00", "mood": "c", "volume_change": -3}]})
    assert p.morning_volume == 8 and p.slots[1].volume_change == 4
    assert [p.volume_changes_until(WEDNESDAY.replace(hour=h)) for h in (8, 9, 13, 15, 23)] == [0, 4, 4, 1, 0]

