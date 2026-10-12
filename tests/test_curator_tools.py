"""The taste curator's tools: preferences, feedback clean-up, building playlists."""
from __future__ import annotations

import pytest

from music_agent.agent.curator_tools import CuratorTools
from music_agent.agent.tools import ToolError
from tests.fakes import FakeTrackInfo, make_memory, make_tools, run


@pytest.fixture
def curator():
    tools, _ = make_tools(memory=make_memory())
    return CuratorTools(tools)


def test_preferences_and_feedback(curator):
    saved = run(curator.add_preference("genre", "dislike", "reggaeton", part_of_day="morning"))
    assert saved["preference"] == "avoid genre: reggaeton (in the morning only)"
    with pytest.raises(ToolError, match="sentiment"):
        run(curator.add_preference("genre", "meh", "jazz"))
    with pytest.raises(ToolError, match="part of day"):
        run(curator.add_preference("genre", "like", "jazz", part_of_day="noon"))
    fid = curator.memory.record_feedback(-1, "artist", artist="Coldplay")

    taste = run(curator.get_taste())
    assert taste["preferences"] == [{"id": saved["id"], "text": saved["preference"]}]
    assert taste["recent_feedback"] == [{"id": fid, "text": "dislike artist: Coldplay", "date": "2026-10-01"}]
    assert taste["profile"]["avoid_artists"] == ["Coldplay"]

    assert run(curator.forget_feedback(fid)) == {"forgotten": True, "id": fid}
    assert run(curator.remove_preference(saved["id"]))["removed"]
    with pytest.raises(ToolError, match="No preference"):
        run(curator.remove_preference(saved["id"]))
    assert run(curator.get_taste())["preferences"] == []


def test_save_playlist_looks_tracks_up_and_reports_misses(curator):
    out = run(curator.save_playlist("Jazz night", [
        {"title": "So What", "artist": "Miles Davis"},
        {"title": "Made Up Song", "artist": "Daft Punk"},
    ], description="Late, quiet jazz"))
    assert out["created"] and out["saved"] == 1 and out["total_tracks"] == 1 and out["length"] == "9 min"
    assert out["not_found"][0]["requested"] == "Made Up Song - Daft Punk"
    assert "One More Time" in out["not_found"][0]["catalogue_tracks_by_artist"]

    more = run(curator.save_playlist("jazz NIGHT", [{"title": "One More Time", "artist": "Daft Punk"}], mode="append"))
    assert not more["created"] and more["total_tracks"] == 2
    shown = run(curator.get_playlist("Jazz night"))
    assert shown["description"] == "Late, quiet jazz"
    assert shown["tracks"] == ["So What - Miles Davis", "One More Time - Daft Punk"]


def test_save_playlist_never_creates_or_wipes_on_total_miss(curator):
    miss = [{"title": "Nope", "artist": "Nobody"}]
    assert run(curator.save_playlist("New", miss))["created"] is False
    assert curator.playlists.find("New") is None
    run(curator.save_playlist("Keep", [{"title": "So What", "artist": "Miles Davis"}]))
    run(curator.save_playlist("Keep", miss, mode="replace"))
    assert run(curator.get_playlist("Keep"))["tracks"] == ["So What - Miles Davis"]


def test_delete_playlist_and_memory_off(curator):
    run(curator.save_playlist("Tmp", [{"title": "So What", "artist": "Miles Davis"}]))
    assert run(curator.delete_playlist("tmp")) == {"deleted": "Tmp"}
    with pytest.raises(ToolError, match="No playlist"):
        run(curator.delete_playlist("tmp"))
    off = CuratorTools(make_tools()[0])
    with pytest.raises(ToolError, match="Memory is turned off"):
        run(off.get_taste())
    with pytest.raises(ToolError, match="need memory"):
        run(off.save_playlist("x", [{"title": "So What", "artist": "Miles Davis"}]))


def test_notebook_is_the_curators_memory(curator):
    saved = run(curator.write_note("Asked about 70s soul: loved Bill Withers", topic="quiz: soul"))
    curator.memory.add_note("I cook on Sundays, keep it mellow", author="you", for_dj=True)
    notes = run(curator.get_taste())["notebook"]
    assert notes[0]["by"] == "person" and notes[0]["shared_with_dj"]
    assert notes[1] == {"id": saved["note_id"], "date": "2026-10-01", "by": "curator",
                        "text": "Asked about 70s soul: loved Bill Withers", "topic": "quiz: soul"}
    # Only the shared note reaches the DJ.
    dj_notes = curator.memory.taste_profile()["notes"]
    assert dj_notes == ["~ I cook on Sundays, keep it mellow (2026-10-01, notebook)"]
    with pytest.raises(ToolError, match="needs some text"):
        run(curator.write_note("  "))
    assert run(curator.remove_note(saved["note_id"]))["removed"]
    with pytest.raises(ToolError, match="No note"):
        run(curator.remove_note(saved["note_id"]))


def test_update_preference(curator):
    pid = run(curator.add_preference("artist", "like", "Miles Davis", part_of_day="evening"))["id"]
    out = run(curator.update_preference(pid, part_of_day="any", note="any time really"))
    assert out["preference"] == "love artist: Miles Davis - any time really"
    out = run(curator.update_preference(pid, sentiment="dislike"))
    assert out["preference"].startswith("avoid artist: Miles Davis")
    with pytest.raises(ToolError, match="Nothing to change"):
        run(curator.update_preference(pid))
    with pytest.raises(ToolError, match="No preference"):
        run(curator.update_preference(999, note="x"))
    with pytest.raises(ToolError, match="part of day"):
        run(curator.update_preference(pid, part_of_day="noon"))


def test_adjust_score_and_reset(curator):
    curator.memory.record_feedback(-1, "artist", artist="Coldplay")
    out = run(curator.adjust_score("artist", "Coldplay", reset_learning=True, note="another chance"))
    assert out["score"] == 0 and "learning reset" in out["adjustment"]
    out = run(curator.adjust_score("artist", "Coldplay", change=1.5))
    assert out["score"] == 1.5
    taste = run(curator.get_taste())
    assert taste["adjustments"] == [{"id": out["id"], "text": out["adjustment"]}]
    assert taste["scores"]["artists"][0] == {"name": "Coldplay", "score": 1.5, "from": {"adjusted": 1.5}}
    with pytest.raises(ToolError, match="artist or a track"):
        run(curator.adjust_score("genre", "jazz", change=1))
    assert run(curator.remove_adjustment(out["id"]))["removed"]
    assert run(curator.get_taste())["profile"]["avoid_artists"] == ["Coldplay"]   # learning is back


def test_listening_history(curator):
    m = curator.memory
    m.record_listen("Says", "Nils Frahm", "skipped", 30, 300)
    m.record_listen("Says", "Nils Frahm", "skipped", 40, 300)
    m.record_listen("So What", "Miles Davis", "completed", 562, 562)
    m.start_request("something calm for reading", agent="dj")
    m.start_request("quiz me", agent="curator")
    h = run(curator.get_listening_history())
    assert h["by_artist"][0] == {"artist": "Nils Frahm", "completed": 0, "skipped": 2}
    assert h["listens"][0]["track"] == "So What - Miles Davis"                  # newest first
    assert h["listens"][-1]["heard"] == "10%" and h["listens"][-1]["outcome"] == "skipped"
    assert [r["asked"] for r in h["dj_requests"]] == ["something calm for reading"]
    only = run(curator.get_listening_history(days=7, artist="miles davis"))
    assert [x["track"] for x in only["listens"]] == ["So What - Miles Davis"]


def test_quiz_answers_build_findings_that_the_code_confirms_only_with_enough_evidence():
    tools, _ = make_tools(memory=make_memory())
    info = FakeTrackInfo({"Dynamite": "hit", "Pink Venom": "hit", "Through the Night": "known"})
    curator = CuratorTools(tools, info)
    f = run(curator.add_finding("genre", "K-pop", "dislike", "Dislikes K-pop"))
    assert f["status"] == "tentative" and not f["can_confirm"] and f["still_needs"]
    for title, artist in (("Dynamite", "BTS"), ("Pink Venom", "BLACKPINK")):
        out = run(curator.record_quiz_answer(title, artist, "not_for_me", [f["id"]]))
    assert out["popularity"] == "hit" and info.calls[-1] == ("Pink Venom", "BLACKPINK")
    assert out["findings"][0]["evidence"] == ["Dynamite - BTS: not for me (hit)",
                                             "Pink Venom - BLACKPINK: not for me (hit)"]
    with pytest.raises(ToolError, match="salvage"):
        run(curator.confirm_finding(f["id"]))
    out = run(curator.record_quiz_answer("Through the Night", "IU", "not_for_me", [f["id"]], salvage=True))
    assert out["findings"][0]["can_confirm"]
    assert run(curator.confirm_finding(f["id"]))["status"] == "confirmed"
    taste = run(curator.get_taste())
    assert taste["profile"]["findings"] == ["Dislikes K-pop"]
    assert taste["findings"][0]["status"] == "confirmed"
    feedback = curator.memory.recent_feedback()
    assert {r["source"] for r in feedback} == {"curator"} and len(feedback) == 3   # also saved as quiz feedback
    with pytest.raises(ToolError, match="reaction"):
        run(curator.record_quiz_answer("X", "Y", "meh"))
    with pytest.raises(ToolError, match="No finding"):
        run(curator.record_quiz_answer("X", "Y", "love", [999]))
    with pytest.raises(ToolError, match="confirm_finding"):
        run(curator.update_finding(f["id"], status="confirmed"))
    assert run(curator.update_finding(f["id"], status="dropped"))["status"] == "dropped"


def test_look_up_tracks_needs_the_service_and_limits_each_call(curator):
    with pytest.raises(ToolError, match="turned off"):
        run(curator.look_up_tracks([{"title": "Dynamite", "artist": "BTS"}]))
    curator.trackinfo = FakeTrackInfo({"Dynamite": "hit"})
    out = run(curator.look_up_tracks([{"title": "Dynamite", "artist": "BTS"}, {"title": "", "artist": "X"}]))
    assert [t["status"] for t in out["tracks"]] == ["found", "skipped"]
    with pytest.raises(ToolError, match="At most"):
        run(curator.look_up_tracks([{"title": "a", "artist": "b"}] * 11))


def test_occasional_loves_through_the_curator(curator):
    saved = run(curator.add_preference("artist", "like", "Enya", frequency="on_request",
                                       context="massages or deep relaxation"))
    assert saved["preference"].startswith("love artist: Enya (only when asked for")
    updated = run(curator.update_preference(saved["id"], frequency="rarely", context=""))
    assert updated["preference"] == "love artist: Enya (rarely: an occasional surprise at most, never a regular)"
