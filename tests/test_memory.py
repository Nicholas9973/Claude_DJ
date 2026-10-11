"""Long-term memory and the taste profile."""
from __future__ import annotations

from datetime import timedelta

import pytest

from music_agent.learning.memory import Memory
from tests.fakes import (
    Clock,
    item,
    make_memory,
)


def test_empty_profile_is_small():
    p = make_memory().taste_profile("evening")
    assert p == {"history": {"tracks_played": 0, "feedback_given": 0, "skips_seen": 0}}


def test_likes_dislikes_and_notes():
    m = make_memory()
    m.record_feedback(1, "artist", artist="Lila Downs")
    m.record_feedback(1, "track", title="Mas Que Nada", artist="Sergio Mendes")
    m.record_feedback(-1, "artist", artist="Coldplay")
    m.record_feedback(-1, "general", note="less reggaeton please")
    p = m.taste_profile("evening")
    assert p["liked_artists"][0] == "Lila Downs" and "Sergio Mendes" in p["liked_artists"]
    assert p["avoid_artists"] == ["Coldplay"]
    assert p["liked_tracks"] == ["Mas Que Nada - Sergio Mendes"]
    assert p["notes"][0].startswith("- less reggaeton please (2026-10-01, evening)")
    assert p["liked_in_the_evening"][0] == "Lila Downs"
    assert "liked_in_the_morning" not in m.taste_profile("morning") or \
        m.taste_profile("morning").get("liked_in_the_morning") == []


def test_artist_names_merge_across_spelling():
    m = make_memory()
    m.record_feedback(1, "artist", artist="Sérgio Mendes")
    m.record_feedback(1, "artist", artist="sergio mendes")
    assert m.taste_profile()["liked_artists"] == ["Sérgio Mendes"]


def test_skips_and_completions():
    m = make_memory()
    for _ in range(2):
        m.record_listen("Despacito", "Luis Fonsi", "skipped", 20, 230)
    for _ in range(5):
        m.record_listen("Pecadora", "Lila Downs", "completed", 230, 230)
    p = m.taste_profile()
    assert "Despacito - Luis Fonsi" in p["avoid_tracks"]          # skipped twice
    assert "Lila Downs" in p["liked_artists"]                     # 5 x 0.3 = 1.5
    assert p["history"]["skips_seen"] == 2


def test_old_feedback_fades():
    clock = Clock()
    m = make_memory(clock)
    m.record_feedback(-1, "artist", artist="Coldplay")            # -3 today
    clock.t += timedelta(days=120)                                # two half-lives: -0.75
    assert "avoid_artists" not in m.taste_profile()


def test_recent_plays_and_requests():
    clock = Clock()
    m = make_memory(clock)
    rid = m.start_request("play something", "sonnet")
    m.record_plays([item("Chan Chan", "Buena Vista Social Club")], rid)
    m.finish_request(rid, "Queued 1 track", 0.03)
    assert m.taste_profile()["played_in_last_24h"] == ["Chan Chan - Buena Vista Social Club"]
    clock.t += timedelta(hours=25)
    assert "played_in_last_24h" not in m.taste_profile()


def test_feedback_validation_and_forget():
    m = make_memory()
    with pytest.raises(ValueError):
        m.record_feedback(1, "track")              # no title
    with pytest.raises(ValueError):
        m.record_feedback(1, "general")            # no note
    fid = m.record_feedback(-1, "artist", artist="Coldplay")
    assert m.forget_feedback(fid) and not m.forget_feedback(fid)
    assert "avoid_artists" not in m.taste_profile()


def test_stated_preferences_shape_the_profile_and_never_fade():
    clock = Clock()
    m = make_memory(clock)
    m.add_preference("artist", 1, "Radiohead")
    m.add_preference("artist", -1, "Coldplay", note="too sweet")
    m.add_preference("genre", -1, "reggaeton", part="morning")
    m.add_preference("rule", 0, "instrumental only while I work")
    m.add_preference("track", 1, "Paranoid Android", artist="Radiohead")
    m.add_preference("artist", 1, "Miles Davis", part="evening")
    clock.t += timedelta(days=3650)                               # ten years later: still there
    p = m.taste_profile("evening")
    assert p["preferences"] == [
        "love artist: Miles Davis (especially in the evening)",
        "love track: Paranoid Android - Radiohead",
        "rule: instrumental only while I work",
        "avoid genre: reggaeton (in the morning only)",
        "avoid artist: Coldplay - too sweet",
        "love artist: Radiohead",
    ]
    assert p["liked_artists"] == ["Radiohead"] and p["avoid_artists"] == ["Coldplay"]
    assert p["liked_tracks"] == ["Paranoid Android - Radiohead"]
    assert p["liked_in_the_evening"] == ["Miles Davis"]           # only counts at its time of day
    assert "liked_in_the_morning" not in m.taste_profile("morning")


def test_preference_validation_update_and_remove():
    m = make_memory()
    for bad in (dict(kind="mood", sentiment=1, subject="x"), dict(kind="artist", sentiment=2, subject="x"),
                dict(kind="artist", sentiment=1, subject="  "), dict(kind="genre", sentiment=1, subject="x", part="noon")):
        with pytest.raises(ValueError):
            m.add_preference(**bad)
    pid = m.add_preference("artist", -1, "Coldplay", artist="ignored for artists")
    assert m.preferences()[0]["artist"] is None
    updated = m.update_preference(pid, sentiment=1, note="changed my mind")
    assert updated["text"] == "love artist: Coldplay - changed my mind"
    with pytest.raises(KeyError):
        m.update_preference(999, sentiment=1)
    assert m.remove_preference(pid) and not m.remove_preference(pid)
    assert m.preferences() == []


def test_old_database_files_get_new_columns(tmp_path):
    import sqlite3
    path = tmp_path / "music.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE requests (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, prompt TEXT, reply TEXT, "
                "model TEXT, cost_usd REAL, part_of_day TEXT, weekday TEXT, weather TEXT)")
    old.commit()
    old.close()
    m = Memory(path)
    m.start_request("hi", "sonnet", agent="curator")
    assert m.db.rows("requests")["rows"][0][-1] == "curator"
    m.close()


def test_scores_show_where_they_came_from():
    m = make_memory()
    m.add_preference("artist", 1, "Radiohead")
    m.record_feedback(1, "track", title="Creep", artist="Radiohead")
    m.record_listen("Creep", "Radiohead", "skipped", 20, 230)
    radiohead = m.affinities()["artists"][0]
    assert radiohead["name"] == "Radiohead" and radiohead["artist"] == "Radiohead"
    assert radiohead["parts"] == {"stated": 5.0, "feedback": 2.0, "listening": -0.7}
    assert radiohead["score"] == 6.3 and radiohead["adjustment"] is None
    creep = m.affinities()["tracks"][0]
    assert (creep["title"], creep["artist"], creep["skips"]) == ("Creep", "Radiohead", 1)


def test_nudges_add_up_never_fade_and_disappear_at_zero():
    clock = Clock()
    m = make_memory(clock)
    m.adjust("artist", "Nina Simone", +1)
    adj = m.adjust("artist", "nina simone", +1, note="more of her")     # same artist: adds to it
    assert adj["delta"] == 2 and adj["text"] == "artist Nina Simone: +2 - more of her"
    clock.t += timedelta(days=3650)
    assert m.taste_profile()["liked_artists"] == ["Nina Simone"]
    assert m.affinities()["artists"][0]["parts"] == {"adjusted": 2.0}
    assert m.adjust("artist", "Nina Simone", -2) is None                # back to nothing: removed
    assert m.adjustments() == []
    track = m.adjust("track", "Creep", -3, artist="Radiohead")
    assert "Creep - Radiohead" in m.taste_profile()["avoid_tracks"]
    assert m.update_adjustment(track["id"], delta=4)["delta"] == 4
    assert m.taste_profile()["liked_tracks"] == ["Creep - Radiohead"]
    for bad in (dict(kind="genre", subject="jazz", delta=1), dict(kind="artist", subject=" ", delta=1),
                dict(kind="artist", subject="X", delta=99)):
        with pytest.raises(ValueError):
            m.adjust(**bad)


def test_reset_learning_hides_old_signals_but_keeps_history():
    clock = Clock()
    m = make_memory(clock)
    m.record_feedback(-1, "artist", artist="Coldplay")
    for _ in range(3):
        m.record_listen("Yellow", "Coldplay", "skipped", 20, 260)
    assert m.taste_profile()["avoid_artists"] == ["Coldplay"]
    clock.t += timedelta(minutes=1)
    adj = m.adjust("artist", "Coldplay", reset_learning=True, note="giving them another go")
    p = m.taste_profile()
    assert "avoid_artists" not in p and "avoid_tracks" not in p          # skips are forgotten too
    assert len(m.recent_feedback()) == 1 and m.db.rows("listens")["total"] == 3   # history kept
    clock.t += timedelta(minutes=1)
    m.record_feedback(1, "artist", artist="Coldplay")                    # new signals count again
    assert m.taste_profile()["liked_artists"] == ["Coldplay"]
    m.update_adjustment(adj["id"], ignore_before=None)                   # undo the reset
    assert m.affinities()["artists"][0]["parts"]["feedback"] == 0.0      # -3 and +3 again


def test_tuning_changes_the_scoring():
    m = make_memory()
    m.record_listen("Pecadora", "Lila Downs", "completed", 230, 230)
    assert "liked_artists" not in m.taste_profile()                    # 0.3 < 1.0
    m.set_tuning("completed", 1.2)
    assert m.taste_profile()["liked_artists"] == ["Lila Downs"]
    info = {t["key"]: t for t in m.tuning_info()}
    assert info["completed"]["value"] == 1.2 and info["completed"]["changed"] and info["completed"]["default"] == 0.3
    assert info["half_life_days"]["value"] == 60
    m.set_tuning("completed", None)                                     # back to the default
    assert m.tuning()["completed"] == 0.3 and "liked_artists" not in m.taste_profile()
    for key, value in (("nope", 1), ("half_life_days", 0), ("avoid_artist_at", 1), ("completed", "lots")):
        with pytest.raises(ValueError):
            m.set_tuning(key, value)
    m.set_tuning("skips_to_avoid", 3)
    row_id = m.db.rows("taste_settings")["rows"][0][0]
    with pytest.raises(ValueError):
        m.db.update_row("taste_settings", row_id, {"value": 0})           # the Database page follows the rules too


def test_quiz_samples_dont_count_as_recently_played():
    m = make_memory()
    m.record_plays([item("Chan Chan", "Buena Vista Social Club")], source="curator")
    m.record_plays([item("So What", "Miles Davis", 2)], source="dj")
    assert m.taste_profile()["played_in_last_24h"] == ["So What - Miles Davis"]
    assert m.taste_profile()["history"]["tracks_played"] == 2
