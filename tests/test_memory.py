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
    clock = Clock()
    m = make_memory(clock)
    m.record_feedback(1, "artist", artist="Lila Downs")
    m.record_feedback(1, "track", title="Mas Que Nada", artist="Sergio Mendes")
    m.record_feedback(-1, "artist", artist="Coldplay")
    m.record_feedback(-1, "general", note="less reggaeton please")
    p = m.taste_profile("evening")
    assert p["liked_artists"][0] == "Lila Downs" and "Sergio Mendes" in p["liked_artists"]
    assert p["avoid_artists"] == ["Coldplay"]
    assert p["liked_tracks"] == ["Mas Que Nada - Sergio Mendes"]
    assert p["notes"][0].startswith("- less reggaeton please (2026-10-01, evening)")
    assert "liked_in_the_evening" not in p                         # one evening isn't a pattern yet
    clock.t += timedelta(days=1)
    m.record_feedback(1, "track", title="Pecadora", artist="Lila Downs")
    assert m.taste_profile("evening")["liked_in_the_evening"] == ["Lila Downs"]   # liked on two evenings
    assert "liked_in_the_morning" not in m.taste_profile("morning")


def test_artist_names_merge_across_spelling():
    m = make_memory()
    m.record_feedback(1, "artist", artist="Sérgio Mendes")
    m.record_feedback(1, "artist", artist="sergio mendes")
    assert m.taste_profile()["liked_artists"] == ["Sérgio Mendes"]


def test_skips_and_completions():
    clock = Clock()
    m = make_memory(clock)
    for _ in range(2):
        m.record_listen("Despacito", "Luis Fonsi", "skipped", 20, 230)
    for _ in range(20):
        m.record_listen("Pecadora", "Lila Downs", "completed", 230, 230)
    p = m.taste_profile()
    assert p["avoid_tracks"] == ["Despacito - Luis Fonsi"]       # skipped twice recently
    assert "liked_artists" not in p and "liked_tracks" not in p   # background music isn't a like
    assert "avoid_artists" not in p                               # skips barely touch the artist
    assert p["history"]["skips_seen"] == 2
    clock.t += timedelta(days=30)                                  # one skip half-life: the avoid wears off
    assert "avoid_tracks" not in m.taste_profile()


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
    old.execute("CREATE TABLE notebook (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, "
                "author TEXT NOT NULL CHECK (author IN ('curator', 'you')), topic TEXT, text TEXT NOT NULL, "
                "for_dj INTEGER NOT NULL DEFAULT 0 CHECK (for_dj IN (0, 1)))")
    old.execute("INSERT INTO notebook (ts, author, text) VALUES ('2026-10-01T10:00:00', 'you', 'old note')")
    old.commit()
    old.close()
    m = Memory(path)
    m.start_request("hi", "sonnet", agent="curator")
    rows = m.db.rows("requests")
    assert rows["rows"][0][rows["columns"].index("agent")] == "curator"
    # The notebook was rebuilt to allow notes by the DJ, keeping the old ones.
    m.add_note("bossa nova asked for 4 times", "playlist idea: bossa nova", author="dj")
    assert [n["author"] for n in m.notes()] == ["dj", "you"]
    assert "dj" in m.db.column_info("notebook")[2]["choices"]
    m.close()
    Memory(path).close()   # opening it again doesn't migrate twice


def test_scores_show_where_they_came_from():
    m = make_memory()
    m.add_preference("artist", 1, "Radiohead")
    m.record_feedback(1, "track", title="Creep", artist="Radiohead")
    m.record_listen("Creep", "Radiohead", "skipped", 20, 230)
    radiohead = m.affinities()["artists"][0]
    assert radiohead["name"] == "Radiohead" and radiohead["artist"] == "Radiohead"
    assert radiohead["parts"] == {"stated": 5.0, "feedback": 2.0, "listening": -0.2}
    assert radiohead["score"] == 6.8 and radiohead["adjustment"] is None
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
    assert "liked_tracks" not in m.taste_profile()                     # finishing counts 0 by default
    m.set_tuning("completed", 2)
    assert m.taste_profile()["liked_tracks"] == ["Pecadora - Lila Downs"]
    info = {t["key"]: t for t in m.tuning_info()}
    assert info["completed"]["value"] == 2 and info["completed"]["changed"] and info["completed"]["default"] == 0
    assert info["half_life_days"]["value"] == 60
    m.set_tuning("completed", None)                                     # back to the default
    assert m.tuning()["completed"] == 0 and "liked_tracks" not in m.taste_profile()
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


def test_quiz_answers_count_for_the_track_but_less_for_the_artist():
    clock = Clock()
    m = make_memory(clock)
    rid = m.start_request("quiz me", agent="curator")
    m.record_feedback(1, "track", title="Ho Hey", artist="The Lumineers", request_id=rid)   # old rows: via request
    m.record_feedback(-1, "track", title="Hurt", artist="Johnny Cash", source="curator")   # new rows: source
    m.record_feedback(1, "artist", artist="The Hu", source="curator")      # said about the artist itself
    p = m.taste_profile("evening")
    assert "Hurt - Johnny Cash" in p["avoid_tracks"]                       # a rejected sample stays avoided
    assert "liked_tracks" not in p                                         # a liked sample doesn't go in rotation
    assert p["liked_artists"] == ["The Hu", "The Lumineers"]               # +3 (stated about the artist), +1
    lumineers = next(a for a in m.affinities()["artists"] if a["name"] == "The Lumineers")
    assert lumineers["parts"] == {"quiz": 1.0}
    clock.t += timedelta(days=1)
    m.record_feedback(1, "track", title="Big Parade", artist="The Lumineers", source="curator")
    assert "liked_in_the_evening" not in m.taste_profile("evening")        # quizzes don't set times of day
    m.record_feedback(1, "track", title="Ho Hey", artist="The Lumineers", source="dj")
    assert m.taste_profile()["liked_tracks"] == ["Ho Hey - The Lumineers"]  # liked again while really listening


def test_quiz_samples_and_music_for_others_dont_train_the_taste():
    m = make_memory()
    m.record_plays([item("Chan Chan", "Buena Vista Social Club")], source="curator")
    m.record_plays([item("Gasolina", "Daddy Yankee", 2)], source="dj", audience="others")
    for _ in range(3):
        m.record_listen("Chan Chan", "Buena Vista Social Club", "skipped", 20, 240)
        m.record_listen("Gasolina", "Daddy Yankee", "skipped", 20, 240)
    assert "avoid_tracks" not in m.taste_profile()
    contexts = {x["track"]: x["context"] for x in m.listening_history()["listens"]}
    assert contexts == {"Chan Chan - Buena Vista Social Club": "quiz sample", "Gasolina - Daddy Yankee": "for others"}
    assert m.listening_history()["by_artist"] == []
    with pytest.raises(ValueError):
        m.record_plays([item("X", "Y")], audience="neighbours")


def test_guest_mode_marks_everything_as_for_others_until_it_ends():
    clock = Clock()
    m = make_memory(clock)
    until = m.set_guest_mode(2)
    assert m.guest_until() == until == clock.t + timedelta(hours=2)
    m.record_plays([item("Gasolina", "Daddy Yankee")], source="dj")       # not marked: guest mode decides
    m.record_listen("Despacito", "Luis Fonsi", "skipped", 20, 230)          # started from the Sonos app
    rows = m.db.query("SELECT audience FROM plays UNION ALL SELECT audience FROM listens")
    assert [r["audience"] for r in rows] == ["others", "others"]
    clock.t += timedelta(hours=3)
    assert m.guest_until() is None
    m.record_listen("Despacito", "Luis Fonsi", "skipped", 20, 230)
    assert m.listening_history()["by_artist"][0]["skipped"] == 1
    assert m.set_guest_mode(None) is None
    with pytest.raises(ValueError):
        m.set_guest_mode(48)


def test_a_new_queue_for_others_covers_tracks_it_didnt_list():
    m = make_memory()
    m.queue_audience = "others"                                           # e.g. a whole album for a party
    m.record_listen("Track 3", "Some Band", "skipped", 20, 230)
    assert m.listening_history()["listens"][0]["context"] == "for others"


def test_listens_from_before_the_change_are_matched_to_their_plays(tmp_path):
    path = tmp_path / "music.db"
    clock = Clock()
    m = Memory(path, now_fn=clock)
    rid = m.start_request("quiz me", agent="curator")
    m.record_plays([item("Chan Chan", "Buena Vista Social Club")], rid)   # an old play: no source column yet
    m.db.execute("UPDATE plays SET source = NULL, audience = NULL")
    clock.t += timedelta(minutes=4)
    m.record_listen("Chan chan", "Buena Vista Social Club", "skipped", 20, 240)
    m.record_listen("Unknown", "Someone", "completed", 200, 200)
    m.db.execute("UPDATE listens SET source = NULL, audience = NULL")      # as an old file would have them
    m.close()
    m = Memory(path, now_fn=clock)
    rows = m.db.query("SELECT title, source, audience FROM listens ORDER BY id")
    assert [tuple(r) for r in rows] == [("Chan chan", "curator", "me"), ("Unknown", None, None)]
    m.close()


def test_a_love_can_say_how_often_and_when():
    m = make_memory()
    m.add_preference("artist", 1, "The Hu", frequency="rarely", context="at the gym")
    m.add_preference("artist", 1, "Norah Jones", frequency="sometimes", note="a track or two per session")
    m.add_preference("artist", 1, "Enya", frequency="on_request", context="massages or deep relaxation")
    m.add_preference("artist", 1, "Rosalia")
    m.add_preference("genre", 0, "opera", frequency="on_request")
    for artist, title in (("The Hu", "Wolf Totem"), ("Norah Jones", "Seven Years"), ("Enya", "Caribbean Blue")):
        m.record_feedback(1, "track", title=title, artist=artist)          # liked while playing, still occasional
    p = m.taste_profile()
    assert p["preferences"] == [
        "note on genre: opera (only when asked for, or when the moment clearly calls for it)",
        "love artist: Rosalia",
        "love artist: Enya (only when asked for, or when the moment clearly calls for it; "
        "when: massages or deep relaxation)",
        "love artist: Norah Jones (sometimes: now and then, not a staple) - a track or two per session",
        "love artist: The Hu (rarely: an occasional surprise at most, never a regular; when: at the gym)",
    ]
    assert p["liked_artists"] == ["Rosalia"]                               # no negative nudges needed
    assert "liked_tracks" not in p
    scores = {a["name"]: a["parts"].get("stated") for a in m.affinities()["artists"]}
    assert scores == {"Rosalia": 5.0, "Norah Jones": 2.5, "The Hu": 0.0, "Enya": 0.0}


def test_how_often_is_validated_and_can_be_changed():
    m = make_memory()
    with pytest.raises(ValueError):
        m.add_preference("artist", 1, "X", frequency="weekly")
    with pytest.raises(ValueError):
        m.add_preference("artist", -1, "Coldplay", frequency="rarely")     # an avoid is never played anyway
    pid = m.add_preference("artist", 1, "The Hu", frequency="regular")
    assert m.preferences()[0]["frequency"] is None                         # regular is the default
    assert m.update_preference(pid, frequency="rarely", context="gym")["text"] == \
        "love artist: The Hu (rarely: an occasional surprise at most, never a regular; when: gym)"
    assert m.update_preference(pid, frequency="regular", context="")["text"] == "love artist: The Hu"
    row_id = m.preferences()[0]["id"]
    with pytest.raises(ValueError):
        m.db.update_row("preferences", row_id, {"sentiment": -1, "frequency": "rarely"})   # Database page too
    info = {c["name"]: c for c in m.db.column_info("preferences")}
    assert info["frequency"]["choices"] == ["regular", "sometimes", "rarely", "on_request"]


def test_the_profile_lists_what_was_repeated_lately_so_the_dj_keeps_it_fresh():
    clock = Clock()
    m = make_memory(clock)
    m.record_feedback(1, "track", title="Dreams", artist="Fleetwood Mac")
    m.record_feedback(1, "track", title="Lovely Day", artist="Bill Withers")
    for n, title in enumerate(("Dreams (2001 Remaster)", "Dreams - 2004 Remaster", "Dreams")):
        m.record_plays([item(title, "Fleetwood Mac", n)], source="dj")
        clock.t += timedelta(days=1)
    for n in range(8):
        m.record_plays([item(f"Song {n}", "Olivia Dean", 10 + n)], source="dj")
    m.record_plays([item("Lovely Day", "Bill Withers", 30)] * 2, source="curator")   # quiz samples don't count
    p = m.taste_profile()
    assert p["played_often_lately"] == ["Dreams - Fleetwood Mac (3x)"]    # versions count as one song
    assert p["artists_to_rest"] == ["Olivia Dean (8 plays)"]
    assert p["liked_tracks"] == ["Lovely Day - Bill Withers"]             # Dreams rests for a while
    clock.t += timedelta(days=15)
    p = m.taste_profile()
    assert "played_often_lately" not in p and "artists_to_rest" not in p
    assert set(p["liked_tracks"]) == {"Dreams - Fleetwood Mac", "Lovely Day - Bill Withers"}


def test_a_finding_stays_tentative_until_the_evidence_is_enough():
    m = make_memory()
    fid = m.add_finding("genre", "K-pop", -1, "Dislikes K-pop")
    with pytest.raises(ValueError, match="already a finding"):
        m.add_finding("genre", "k-pop", -1, "Really dislikes K-pop")
    m.add_evidence(fid, "BTS", -1, "Dynamite", popularity="hit")
    m.add_evidence(fid, "BLACKPINK", -1, "Pink Venom", popularity="hit")
    check = m.check_finding(fid)
    assert not check["ready"] and check["missing"] == [
        "1 more sample(s)", "samples from 1 more different artists",
        "a sample that isn't a famous hit (all so far were hits)",
        "a salvage try: a sample from another side of it, to give it a fair chance"]
    with pytest.raises(ValueError, match="Not enough evidence"):
        m.confirm_finding(fid)
    p = m.taste_profile()
    assert p["tentative_findings"] == ["Dislikes K-pop (tentative: 2 sample(s) from 2 artist(s))"]
    assert "findings" not in p

    m.add_evidence(fid, "IU", -1, "Through the Night", salvage=True, popularity="known")
    assert m.check_finding(fid)["ready"]
    assert m.confirm_finding(fid)["status"] == "confirmed"
    p = m.taste_profile()
    assert p["findings"] == ["Dislikes K-pop"] and "tentative_findings" not in p


def test_every_finding_meets_the_same_bar_even_when_edited_by_hand():
    m = make_memory()
    fid = m.add_finding("genre", "dubstep", -1, "Dislikes dubstep")
    for artist, reaction, salvage in (("Skrillex", -1, False), ("Burial", 1, True), ("Flux Pavilion", -1, False)):
        m.add_evidence(fid, artist, reaction, salvage=salvage)
    assert m.check_finding(fid)["missing"] == ["more agreement: only 2 of 3 reactions fit the conclusion"]
    with pytest.raises(ValueError, match="Not enough evidence"):
        m.db.update_row("findings", fid, {"status": "confirmed"})       # the Database page can't skip it either
    m.update_finding(fid, statement="Dislikes aggressive dubstep; melodic, moody electronic is fine",
                     status="dropped")
    assert "tentative_findings" not in m.taste_profile() and m.findings() == []
    assert len(m.findings(include_dropped=True)) == 1
    with pytest.raises(ValueError, match="dropped"):
        m.add_evidence(fid, "Zomboy", -1)
    with pytest.raises(ValueError):
        m.update_finding(fid, kind="mood")
    with pytest.raises(KeyError):
        m.add_evidence(999, "X", 1)
    assert m.remove_finding(fid) and m.db.rows("finding_evidence")["total"] == 0


def test_a_finding_about_one_artist_needs_different_songs_and_likes_need_no_salvage():
    m = make_memory()
    fid = m.add_finding("artist", "Johnny Cash", 1, "Likes Johnny Cash's upbeat side")
    for title in ("Ring of Fire", "Folsom Prison Blues", "Jackson"):
        m.add_evidence(fid, "Johnny Cash", 1, title)
    assert m.check_finding(fid) == {"ready": True, "missing": [], "samples": 3, "artists": 3, "agreeing": 3,
                                    "salvage_tries": 0, "hits": 0}
