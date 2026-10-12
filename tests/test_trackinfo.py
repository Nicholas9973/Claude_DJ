"""Track qualities from MusicBrainz and Deezer, with the network faked."""
from __future__ import annotations

from datetime import timedelta

import pytest

from music_agent.learning.database import Database
from music_agent.trackinfo import TrackInfoService, plausible_tempo, popularity_band, track_info_key
from tests.fakes import Clock, fake_trackinfo_fetch


def make_service(calls, clock=None, fail=()):
    sleeps = []
    service = TrackInfoService(Database(":memory:"), fetch=fake_trackinfo_fetch(calls, fail),
                               sleep=sleeps.append, now_fn=clock or Clock())
    return service, sleeps


def test_look_up_combines_both_services_and_caches_the_answer():
    calls = []
    service, sleeps = make_service(calls)
    info = service.look_up("Superstition", "Stevie Wonder")
    assert info == {"title": "Superstition", "artist": "Stevie Wonder", "status": "found",
                    "year": 1972,                                   # the original, not a reissue
                    "tempo_bpm": 100, "tempo_note": "approximate (Deezer)",   # 200.7 halved
                    "popularity": "hit",                            # the most popular matching version
                    "tags": ["funk", "soul"],                       # most tagged first; "dance/by-style/ddr" left out
                    "artist_genres": ["soul", "funk"],
                    "sources": "MusicBrainz, Deezer", "looked_up": "2026-10-01"}
    assert len(calls) == 4 and sleeps and sleeps[0] > 1              # MusicBrainz: one request per second
    assert service.look_up("Superstition (2000 Remaster)", "stevie wonder") == info   # cached, same song
    assert len(calls) == 4
    assert service.popularity("Superstition", "Stevie Wonder") == "hit"
    assert service.cached("Unknown", "Nobody") is None


def test_unknown_tracks_and_failures_are_answers_not_errors():
    clock = Clock()
    half, _ = make_service([], clock, fail=("deezer",))
    partly = half.look_up("Superstition", "Stevie Wonder")
    assert partly["status"] == "found" and partly["sources"] == "MusicBrainz" and "Deezer: offline" in partly["error"]
    assert half.look_up("Made Up Song", "Nobody")["status"] == "error"     # unknown, since Deezer couldn't say

    offline, _ = make_service([], clock, fail=("musicbrainz", "deezer"))
    assert offline.look_up("Superstition", "Stevie Wonder")["status"] == "error"

    calls = []
    service, _ = make_service(calls, clock)
    assert service.look_up("Made Up Song", "Nobody")["status"] == "not_found"
    before = len(calls)
    service.look_up("Made Up Song", "Nobody")                          # "not found" is kept for a while...
    assert len(calls) == before
    clock.t += timedelta(days=31)                                      # ...then asked again
    service.look_up("Made Up Song", "Nobody")
    assert len(calls) > before


def test_helpers():
    assert [popularity_band(r) for r in (0, 50_000, 200_000, 500_000, 900_000)] == \
        [None, "deep cut", "known", "well known", "hit"]
    assert [plausible_tempo(b) for b in (0, None, 120.4, 200.7, 400, 30)] == [None, None, 120, 100, None, None]
    assert track_info_key("Dreams (2001 Remaster)", "Fleetwood Mac") == track_info_key("Dreams", "fleetwood mac")


def test_hand_edits_are_checked():
    service, _ = make_service([])
    service.look_up("Superstition", "Stevie Wonder")
    row_id = service.db.rows("track_info")["rows"][0][0]
    with pytest.raises(ValueError, match="year"):
        service.db.update_row("track_info", row_id, {"year": 3000})
    with pytest.raises(ValueError, match="tempo"):
        service.db.update_row("track_info", row_id, {"tempo_bpm": 5})
