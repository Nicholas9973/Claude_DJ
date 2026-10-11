"""Playback watcher: completed vs skipped tracks."""
from __future__ import annotations

from music_agent.learning.watcher import PlaybackWatcher, parse_hms
from tests.fakes import (
    make_memory,
)


def test_parse_hms():
    assert parse_hms("0:03:54") == 234 and parse_hms("4:05") == 245
    assert parse_hms("NOT_IMPLEMENTED") is None and parse_hms("") is None


def feed(w, *samples):
    return [w.observe(*s) for s in samples]


def test_watcher_completed_and_skipped():
    m = make_memory()
    w = PlaybackWatcher(player=None, memory=m, interval=15)
    out = feed(w,
               ("PLAYING", "A", "X", "0:00:10", "0:04:00"),
               ("PLAYING", "A", "X", "0:03:30", "0:04:00"),
               ("PLAYING", "B", "Y", "0:00:05", "0:04:00"),   # A changed at 3:30/4:00 -> completed
               ("PLAYING", "B", "Y", "0:00:40", "0:04:00"),
               ("PLAYING", "C", "Z", "0:00:02", "0:04:00"))   # B changed at 0:40 -> skipped
    assert out == [None, None, "completed", None, "skipped"]
    rows = m._query("SELECT title, outcome FROM listens ORDER BY id")
    assert [(r["title"], r["outcome"]) for r in rows] == [("A", "completed"), ("B", "skipped")]


def test_watcher_ignores_stops_short_tracks_and_agent_replacements():
    w = PlaybackWatcher(player=None, memory=make_memory(), interval=15)
    out = feed(w,
               ("PLAYING", "A", "X", "0:00:30", "0:04:00"),
               ("STOPPED", "A", "X", "0:00:30", "0:04:00"),
               ("PLAYING", "B", "Y", "0:00:01", "0:04:00"),    # changed after a stop -> not a skip
               ("PLAYING", "Jingle", "Y", "0:00:01", "0:00:20"),  # B skipped at once -> skip
               ("PLAYING", "C", "Y", "0:00:01", "0:04:00"))    # jingle too short -> ignored
    assert out == [None, None, None, "skipped", None]
    w.ignore_next_change()
    assert w.observe("PLAYING", "D", "Y", "0:00:01", "0:04:00") is None
