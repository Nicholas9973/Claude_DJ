"""Generic browsing and editing of the SQLite file (the GUI's Database page)."""
from __future__ import annotations

import sqlite3

import pytest

from tests.fakes import make_memory


def test_browse_edit_and_delete_rows():
    m = make_memory()
    fid = m.record_feedback(-1, "artist", artist="Coldplay")
    db = m.db
    assert {"requests", "plays", "listens", "feedback", "preferences"} <= {t["name"] for t in db.tables()}
    page = db.rows("feedback")
    assert page["total"] == 1 and page["rows"][0][page["columns"].index("artist")] == "Coldplay"

    row = db.update_row("feedback", fid, {"sentiment": 1, "note": "grew on me"})
    assert row["sentiment"] == 1 and row["note"] == "grew on me"
    assert m.taste_profile()["liked_artists"] == ["Coldplay"]       # the agents see edits straight away

    assert db.delete_row("feedback", fid) and not db.delete_row("feedback", fid)


def test_edits_are_checked():
    m = make_memory()
    fid = m.record_feedback(-1, "artist", artist="Coldplay")
    db = m.db
    with pytest.raises(KeyError):
        db.rows("sqlite_master")                                    # only our own tables
    with pytest.raises(KeyError):
        db.update_row("nope", fid, {"note": "x"})
    with pytest.raises(KeyError):
        db.update_row("feedback", 999, {"note": "x"})
    with pytest.raises(ValueError, match="id"):
        db.update_row("feedback", fid, {"id": 5})                   # ids can't change
    with pytest.raises(ValueError, match="bogus"):
        db.update_row("feedback", fid, {"bogus": 1})
    with pytest.raises(sqlite3.IntegrityError):
        db.update_row("feedback", fid, {"sentiment": 5})            # the table's CHECK still applies


def test_bad_edits_are_refused_and_rolled_back():
    m = make_memory()
    fid = m.record_feedback(-1, "artist", artist="Coldplay")
    pid = m.add_preference("artist", 1, "Radiohead")
    db = m.db
    with pytest.raises(ValueError, match="date and time"):
        db.update_row("feedback", fid, {"ts": "last week"})         # would have broken the taste profile
    with pytest.raises(ValueError, match="needs an artist"):
        db.update_row("feedback", fid, {"artist": None})            # same rules as record_feedback
    with pytest.raises(ValueError, match="needs a subject"):
        db.update_row("preferences", pid, {"subject": "   "})      # SQL allows it; add_preference doesn't
    row = m.recent_feedback()[0]
    assert row["artist"] == "Coldplay" and row["ts"] == "2026-10-01T18:30:00"   # nothing changed
    assert db.update_row("feedback", fid, {"ts": "2026-09-01T10:00:00"})["ts"] == "2026-09-01T10:00:00"
    m.taste_profile()


def test_unreadable_time_does_not_break_the_profile():
    m = make_memory()
    m.record_feedback(-1, "artist", artist="Coldplay")
    m.db.execute("UPDATE feedback SET ts = 'garbage'")             # e.g. edited outside the app
    assert m.taste_profile()["avoid_artists"] == ["Coldplay"]


def test_insert_rows_search_and_column_info():
    m = make_memory()
    db = m.db
    row = db.insert_row("feedback", {"sentiment": 1, "scope": "artist", "artist": "Nina Simone"})
    assert row["id"] and row["ts"]                                 # time filled in
    assert m.taste_profile()["liked_artists"] == ["Nina Simone"]
    with pytest.raises(ValueError, match="needs a title"):
        db.insert_row("feedback", {"sentiment": 1, "scope": "track"})
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_row("preferences", {"kind": "artist"})          # required columns missing
    assert db.rows("feedback")["total"] == 1                        # failed inserts left nothing behind

    db.insert_row("feedback", {"sentiment": -1, "scope": "artist", "artist": "Coldplay"})
    found = db.rows("feedback", search="nina")
    assert found["total"] == 1 and found["rows"][0][found["columns"].index("artist")] == "Nina Simone"

    info = {c["name"]: c for c in db.column_info("feedback")}
    assert info["sentiment"]["choices"] == [-1, 0, 1] and info["sentiment"]["type"] == "INTEGER"
    assert info["scope"]["choices"] == ["track", "artist", "general"]
    assert info["ts"]["required"] and info["note"]["choices"] is None
