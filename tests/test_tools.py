"""The tools Claude calls (plain Python, fake speakers)."""
from __future__ import annotations

import asyncio

import pytest

from music_agent.agent.tools import ToolError
from music_agent.models import MediaItem, part_of_day
from music_agent.weather import WeatherService
from tests.fakes import (
    CATALOGUE,
    NIGHT,
    fake_weather_fetch,
    make_memory,
    make_tools,
    run,
)


def test_context():
    tools, _ = make_tools()
    ctx = run(tools.get_context())
    assert ctx["weekday"] == "Tuesday" and ctx["part_of_day"] == "evening"
    assert ctx["in_quiet_hours"] is False and ctx["volume_cap"] == 35
    assert set(ctx["all_rooms"]) == {"Dining Room", "Morn’", "Night"}
    assert [part_of_day(h) for h in (6, 13, 18, 23, 2)] == ["morning", "afternoon", "evening", "night", "night"]


def test_play_tracks_finds_queues_and_reports_missing():
    tools, house = make_tools()
    out = run(tools.play_tracks([
        {"title": "One More Time", "artist": "Daft Punk"},
        {"title": "So What", "artist": "Miles Davis", "album": "Kind of Blue"},
        {"title": "Nonexistent Song", "artist": "Nobody"},
        {"title": "One More Time", "artist": "Daft Punk"},          # duplicate
    ]))
    assert out["queued"] == 2
    assert out["not_found"] == [{"requested": "Nonexistent Song - Nobody",
                                 "reason": "artist not found in the catalogue"}]
    assert out["total_duration"] == "15 min" and len(out["rooms"]) == 3
    dining = house.by_name("Dining Room")
    assert [i.title for i in dining.queue_items] == ["One More Time", "So What"]


def test_play_tracks_nothing_found_changes_nothing():
    tools, house = make_tools()
    out = run(tools.play_tracks([{"title": "Nope", "artist": "Nobody"}]))
    assert out["queued"] == 0 and house.by_name("Dining Room").state == "STOPPED"


def test_play_tracks_quiet_hours_is_tool_error():
    tools, _ = make_tools(now=NIGHT)
    with pytest.raises(ToolError, match="safety rule"):
        run(tools.play_tracks([{"title": "So What", "artist": "Miles Davis"}]))


def test_play_tracks_limits():
    tools, _ = make_tools()
    with pytest.raises(ToolError, match="At most 50"):
        run(tools.play_tracks([{"title": "x", "artist": "y"}] * 51))
    with pytest.raises(ToolError, match="mode"):
        run(tools.play_tracks([{"title": "x", "artist": "y"}], mode="shuffle"))


def test_search_and_play_item():
    tools, house = make_tools()
    res = run(tools.search_music("kind of blue", "album"))
    ref = res["results"][0]["ref"]
    out = run(tools.play_item(ref))
    assert out["kind"] == "album" and house.by_name("Dining Room").state == "PLAYING"
    with pytest.raises(ToolError):
        run(tools.play_item("spotify|album|zzz"))


def test_volume_tool():
    tools, _ = make_tools()
    run(tools.play_tracks([{"title": "So What", "artist": "Miles Davis"}]))
    assert run(tools.set_volume(change=4)) == {"volume": 17}
    out = run(tools.set_volume(level=80))
    assert out["volume"] == 35 and "cap" in out["note"]
    with pytest.raises(ToolError):
        run(tools.set_volume())


def test_controls_and_queue():
    tools, house = make_tools()
    run(tools.play_tracks([{"title": "So What", "artist": "Miles Davis"},
                           {"title": "Get Lucky", "artist": "Daft Punk"}]))
    assert run(tools.get_queue())["queue"][1].startswith("Get Lucky")
    run(tools.playback_control("pause"))
    assert house.by_name("Dining Room").state == "PAUSED_PLAYBACK"
    with pytest.raises(ToolError):
        run(tools.playback_control("self_destruct"))


def test_context_includes_weather_and_taste():
    m = make_memory()
    m.record_feedback(1, "artist", artist="Lila Downs")
    tools, _ = make_tools(memory=m, weather=WeatherService("Lyon", fetch=fake_weather_fetch([])))
    ctx = asyncio.run(tools.get_context())
    assert ctx["weather"]["summary"].startswith("light rain")
    assert ctx["taste"]["liked_artists"] == ["Lila Downs"]


def test_play_records_plays_with_request_and_tells_watcher():
    m = make_memory()
    tools, _ = make_tools(memory=m)
    tools.request_id = m.start_request("latin music")
    asyncio.run(tools.play_tracks([{"title": "Pecadora", "artist": "Lila Downs"}]))
    rows = m._query("SELECT title, request_id FROM plays")
    assert [(r["title"], r["request_id"]) for r in rows] == [("Pecadora", tools.request_id)]
    assert tools.watcher._ignore_next is True


def test_music_for_others_is_recorded_as_such():
    memory = make_memory()
    tools, _ = make_tools(memory=memory)
    out = run(tools.play_tracks([{"title": "So What", "artist": "Miles Davis"}], for_others=True))
    assert out["for_others"] is True and memory.queue_audience == "others"
    assert memory.db.query("SELECT audience FROM plays")[0]["audience"] == "others"
    run(tools.play_item("fake|album|a1"))                                # a new queue for the owner again
    assert memory.queue_audience == "me"
    memory.set_guest_mode(3)
    ctx = run(tools.get_context())
    assert ctx["guest_mode"].startswith("on until")
    assert run(tools.play_tracks([{"title": "Pecadora", "artist": "Lila Downs"}]))["for_others"] is True


def test_feedback_tool_resolves_current_track():
    m = make_memory()
    tools, house = make_tools(memory=m)
    asyncio.run(tools.play_tracks([{"title": "Chan Chan", "artist": "Buena Vista Social Club"}]))
    # Fake speakers report no artist; it is filled in from what the agent queued.
    out = asyncio.run(tools.record_feedback("like", "current_track"))
    assert out["saved"] and out["target"] == "Chan Chan - Buena Vista Social Club"
    out = asyncio.run(tools.record_feedback("dislike", "general", note="no reggaeton"))
    assert out["target"] == "no reggaeton"
    with pytest.raises(ToolError):
        asyncio.run(tools.record_feedback("love", "general", note="x"))
    with pytest.raises(ToolError, match="needs a note"):
        asyncio.run(tools.record_feedback("dislike", "general"))


def test_feedback_without_memory_is_clear_error():
    tools, _ = make_tools()
    with pytest.raises(ToolError, match="Memory is turned off"):
        asyncio.run(tools.record_feedback("like", "artist", artist="X"))


def _saved(tools, name, items, description="for testing"):
    pid = tools.playlists.create(name, description)
    tools.playlists.set_tracks(pid, items)
    return pid


def test_context_lists_saved_playlists():
    tools, _ = make_tools(memory=make_memory())
    _saved(tools, "Cooking", CATALOGUE[:2], "Warm and easy")
    tools.playlists.create("Empty one")                             # empty playlists aren't offered
    assert run(tools.get_context())["saved_playlists"] == [
        {"name": "Cooking", "description": "Warm and easy", "tracks": 2, "length": "9 min"}]


def test_play_playlist_uses_stored_refs_without_searching():
    tools, house = make_tools(memory=make_memory())
    _saved(tools, "Mix", [CATALOGUE[4], CATALOGUE[0]])
    tools.provider.search = lambda *a, **k: pytest.fail("should not search")
    out = run(tools.play_playlist("mix"))
    assert out["playlist"] == "Mix" and out["queued"] == 2 and out["not_found"] == []
    assert [i.title for i in house.by_name("Dining Room").queue_items] == ["So What", "One More Time"]
    assert "So What - Miles Davis" in tools.memory.taste_profile()["played_in_last_24h"]


def test_play_playlist_looks_up_tracks_from_another_service_and_shuffles():
    tools, house = make_tools(memory=make_memory())
    old_service = [MediaItem("spotify", "track", "x1", "So What", "Miles Davis"),
                   MediaItem("spotify", "track", "x2", "Nonexistent", "Nobody"),
                   CATALOGUE[0]]
    _saved(tools, "Old", old_service)
    tools.random.seed(1)
    out = run(tools.play_playlist("Old", shuffle=True))
    assert out["queued"] == 2 and out["shuffled"] and out["not_found"] == ["Nonexistent - Nobody"]
    assert {i.title for i in house.by_name("Dining Room").queue_items} == {"So What", "One More Time"}


def test_play_playlist_errors():
    tools, _ = make_tools(memory=make_memory())
    _saved(tools, "Mix", CATALOGUE[:1])
    tools.playlists.create("Empty")
    with pytest.raises(ToolError, match="Saved playlists: Empty, Mix"):
        run(tools.play_playlist("Nope"))
    with pytest.raises(ToolError, match="empty"):
        run(tools.play_playlist("Empty"))
    with pytest.raises(ToolError, match="memory"):
        run(make_tools()[0].play_playlist("Mix"))
    night, _ = make_tools(now=NIGHT, memory=make_memory())
    _saved(night, "Mix", CATALOGUE[:1])
    with pytest.raises(ToolError, match="safety rule"):
        run(night.play_playlist("Mix"))


def test_genre_of_the_set_holds_for_an_hour():
    from datetime import timedelta

    from tests.fakes import Clock
    clock = Clock()
    memory = make_memory(clock)
    tools, _ = make_tools(now=clock, memory=memory)
    tools.request_id = memory.start_request("some bossa nova", agent="dj")
    run(tools.play_tracks([{"title": "So What", "artist": "Miles Davis"}], genre="bossa nova"))
    clock.t += timedelta(minutes=40)
    tools.request_id = memory.start_request("more please", agent="dj")
    run(tools.play_tracks([{"title": "Chan Chan", "artist": "Buena Vista Social Club"}], mode="append",
                          genre="Bossa Nova"))
    current = run(tools.get_context())["current_genre"]
    assert current["genre"] == "Bossa Nova" and current["since"] == "18:30" and current["for_minutes"] == 40
    assert current["last_request"] == "more please"
    clock.t += timedelta(minutes=61)
    assert "current_genre" not in run(tools.get_context())
    # A curator's quiz sample never sets the genre of the DJ's set.
    tools.request_id = memory.start_request("quiz me", agent="curator")
    run(tools.play_tracks([{"title": "So What", "artist": "Miles Davis"}], genre="jazz"))
    assert "current_genre" not in run(tools.get_context())


def test_often_requested_genres_and_playlist_suggestions():
    memory = make_memory()
    tools, _ = make_tools(memory=memory)
    for genre in ("bossa nova", "Bossa Nova", "bossa nova", "techno"):
        memory.set_request_genre(memory.start_request("music", agent="dj"), genre)
    assert memory.common_genres() == [{"genre": "bossa nova", "requests": 3}]
    assert run(tools.get_context())["often_requested_genres_last_30_days"][0]["requests"] == 3
    out = run(tools.suggest_playlist("bossa nova for cooking", "asked for 3 times, no playlist fits"))
    assert out["saved"] is True
    assert run(tools.suggest_playlist("Bossa nova for cooking!", "again"))["already_noted"] is True
    [note] = memory.notes()
    assert note["author"] == "dj" and note["topic"] == "playlist idea: bossa nova for cooking"
    with pytest.raises(ToolError):
        run(tools.suggest_playlist(" ", "why"))
    history = memory.listening_history()
    assert history["dj_requests"][0]["genre"] == "techno"


def test_a_version_the_speakers_refuse_is_passed_over_next_time():
    tools, _ = make_tools()
    first = tools.provider.lookup_track("One More Time", "Daft Punk").item
    tools.provider.bad_refs.add(first.ref)            # e.g. not licensed in this country
    with pytest.raises(ToolError, match="could be queued"):
        run(tools.play_tracks([{"title": "One More Time", "artist": "Daft Punk"}]))
    out = run(tools.play_tracks([{"title": "One More Time", "artist": "Daft Punk"}]))
    assert out["queued"] == 1 and first.ref in tools.provider.unplayable

