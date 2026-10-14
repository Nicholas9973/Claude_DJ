"""The orchestrator: when it wakes the agent, and how it stays out of the person's way."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from music_agent import dayplan
from music_agent.app import App
from music_agent.dayplan import DayPlan, Slot
from music_agent.orchestrator import Orchestrator, Situation, decide, slot_key
from tests.fakes import CATALOGUE, Clock, make_memory, make_tools, run

NINE = datetime(2026, 10, 14, 9, 0)      # a Wednesday; wake 08:00, bed 23:00


def plan(**kw):
    slots = [Slot("wake", "gentle"), Slot("14:00", "afternoon groove"), Slot("sleep-1h", "", quiet=True)]
    return DayPlan(**{"enabled": True, "slots": slots, **kw})


def situation(now=NINE, **kw):
    return Situation(now=now, plan=kw.pop("plan", plan()), **{"playing": False, **kw})


def key(now, mood="gentle"):
    return slot_key(now, next(s for s in plan().slots if s.mood == mood))


@pytest.mark.parametrize(("kw", "action"), [
    ({"plan": plan(enabled=False)}, "idle"),
    ({"now": NINE.replace(hour=7)}, "idle"),                                     # before wake-up
    ({"in_safety_quiet": True}, "idle"),
    ({}, "start"),                                                              # nothing playing
    ({"playing": True, "remaining": 2, "acted_slot": key(NINE)}, "top_up"),
    ({"playing": True, "remaining": 10, "acted_slot": key(NINE)}, "idle"),
    ({"playing": True, "remaining": 10}, "adopt"),                              # just started: take over quietly
    ({"last_action": NINE - timedelta(minutes=5)}, "idle"),                     # acted moments ago
])
def test_when_the_agent_is_woken(kw, action):
    assert decide(situation(**kw)).action == action


def test_a_new_part_of_the_day_waits_for_music_you_chose():
    at = NINE.replace(hour=14, minute=30)
    playing = {"now": at, "playing": True, "remaining": 10, "acted_slot": key(at)}
    assert decide(situation(**playing)).action == "new_slot"
    mine = decide(situation(**playing, user_music_at=at - timedelta(minutes=45)))
    assert mine.action == "idle" and "you chose" in mine.reason
    assert decide(situation(**playing, user_music_at=at - timedelta(hours=3))).action == "new_slot"


def test_a_stop_means_quiet_but_a_finished_queue_does_not():
    stopped = decide(situation(was_playing=True, was_remaining=8))
    assert stopped.action == "quiet"
    assert decide(situation(was_playing=True, was_remaining=1)).action == "start"   # the queue just ran out
    quiet = {"quiet_until": NINE + timedelta(hours=2)}
    assert decide(situation(**quiet)).action == "idle"
    assert decide(situation(**quiet, playing=True, was_playing=False)).action == "unquiet"
    assert decide(situation(quiet_until=NINE - timedelta(minutes=1))).action == "start"


def test_quiet_parts_of_the_day_start_nothing():
    late = NINE.replace(hour=22, minute=30)
    first = decide(situation(now=late))
    assert first.action == "adopt" and first.slot.quiet
    assert decide(situation(now=late, acted_slot=slot_key(late, first.slot))).action == "idle"


# ----------------------------------------------------------------- the loop, with fake speakers
@pytest.fixture
def orch(tmp_path):
    """(orchestrator, player, memory, clock, messages sent to the agent)."""
    clock = Clock(NINE)
    memory = make_memory(clock)
    tools, _ = make_tools(now=clock, memory=memory)
    path = tmp_path / "day_plan.toml"
    dayplan.save(plan(), path)
    app = App(tools.config, tools.player, tools.provider, memory=memory, day_plan_path=path)
    sent = []

    async def run_agent(message):
        sent.append(message)
        return {"reply": "Asked for gentle folk.", "steps": ["ask_dj ..."], "error": None}

    return Orchestrator(app, run_agent, now_fn=clock), tools.player, memory, clock, sent


def test_starts_music_then_leaves_it_alone(orch):
    o, player, _, clock, sent = orch
    first = run(o.check())
    assert first["action"] == "start" and first["reply"] == "Asked for gentle folk."
    assert "nothing is playing" in sent[0] and "gentle" in sent[0]
    player.play(CATALOGUE[:6])                       # what the DJ would have queued
    clock.t += timedelta(minutes=15)
    assert run(o.check())["action"] == "idle" and len(sent) == 1
    assert o.status()["runs_today"] == 1 and o.status()["history"][0]["action"] == "start"


def test_stopping_from_the_sonos_app_keeps_it_quiet_for_hours(orch):
    o, player, memory, clock, sent = orch
    player.play(CATALOGUE[:6])
    assert run(o.check())["action"] == "adopt"
    player.pause()                                   # e.g. from the phone
    clock.t += timedelta(minutes=5)
    assert run(o.check())["action"] == "quiet" and memory.quiet_since() == clock.t
    clock.t += timedelta(hours=2)
    assert "staying quiet" in run(o.check())["reason"]
    clock.t += timedelta(hours=1, minutes=5)         # 3 h later
    assert run(o.check())["action"] == "start" and len(sent) == 1


def test_resuming_ends_the_quiet(orch):
    o, player, memory, clock, _ = orch
    player.play(CATALOGUE[:6])
    run(o.check())
    player.pause()
    run(o.check())
    player.resume()
    clock.t += timedelta(minutes=5)
    assert run(o.check())["action"] == "unquiet" and memory.quiet_since() is None


def test_plan_edits_apply_at_the_next_check(orch):
    o, _, _, _, sent = orch
    dayplan.save(plan(enabled=False), o.app.day_plan_path)
    assert run(o.check())["reason"] == "the day plan is turned off" and not sent


def test_unreachable_speakers_mean_doing_nothing(orch, monkeypatch):
    o, player, _, _, sent = orch

    def broken():
        raise OSError("no speakers")
    monkeypatch.setattr(player, "now_playing", broken)
    assert run(o.check())["action"] == "idle" and not sent


# ----------------------------------------------------------------- stops through the tools
def test_stopping_through_the_dj_or_gui_starts_a_quiet_period_and_music_ends_it():
    memory = make_memory()
    tools, _ = make_tools(memory=memory)
    tools.source = "dj"
    run(tools.play_tracks([{"title": "So What", "artist": "Miles Davis"}]))
    out = run(tools.playback_control("stop"))
    assert "day plan" in out["note"] and memory.quiet_since() is not None
    run(tools.play_tracks([{"title": "So What", "artist": "Miles Davis"}]))
    assert memory.quiet_since() is None
    # The orchestrator's own DJ never starts or ends a quiet period.
    tools.source = "auto"
    run(tools.playback_control("pause"))
    assert memory.quiet_since() is None
    tools.source = "gui"
    run(tools.playback_control("pause"))
    memory_since = memory.quiet_since()
    tools.source = "auto"
    run(tools.play_tracks([{"title": "So What", "artist": "Miles Davis"}]))
    assert memory.quiet_since() == memory_since
    assert memory.quiet_history()[0]["from"] == "gui"
