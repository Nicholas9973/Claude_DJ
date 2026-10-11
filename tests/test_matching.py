"""Choosing the right recording from search results."""
from __future__ import annotations

from music_agent.models import MediaItem
from music_agent.text import norm
from tests.fakes import CATALOGUE, best_match, item, track


def test_norm_handles_curly_apostrophe_and_accents():
    assert norm("Morn’") == "morn" and norm("Beyoncé") == "beyonce"


def test_best_match_prefers_original_over_covers():
    items = [track("One More Time (Karaoke Version)", "Party Hits", n=1),
             track("One More Time", "Britney Tribute Band", n=2),
             track("One More Time", "Daft Punk", "Discovery", n=3),
             track("One More Time (Live)", "Daft Punk", n=4)]
    assert best_match(items, "One More Time", "Daft Punk").id == "track:3"


def test_best_match_rejects_wrong_artist_and_wrong_title():
    items = [track("One More Time", "Britney Spears")]
    assert best_match(items, "One More Time", "Daft Punk") is None
    assert best_match([track("Get Lucky")], "Around the World", "Daft Punk") is None


def test_best_match_accepts_bracketed_suffix():
    assert best_match([track("Get Lucky (Radio Edit) [feat. Pharrell]")], "Get Lucky", "Daft Punk")


def test_ref_roundtrip():
    item = track("X", n=1068442)
    assert MediaItem.split_ref(item.ref) == ("fake", "track", "track:1068442")


def test_prefers_album_version_over_single():
    tracks = [i for i in CATALOGUE if i.kind == "track"]
    assert best_match(tracks, "One More Time", "Daft Punk").album == "Discovery"
    assert best_match(tracks, "Get Lucky", "Daft Punk").id == "t4"
    assert best_match(tracks, "Get Lucky (Radio Edit)", "Daft Punk").id == "t3"


def test_album_hint_wins():
    tracks = [i for i in CATALOGUE if i.kind == "track"]
    assert best_match(tracks, "One More Time", "Daft Punk", album="One More Time").id == "t1"


def test_live_is_whole_word_only():
    alive = MediaItem("fake", "track", "x", "Stayin' Alive", "Bee Gees", "Saturday Night Fever")
    live = MediaItem("fake", "track", "y", "Stayin' Alive (Live)", "Bee Gees", "One Night Only")
    assert best_match([live, alive], "Stayin' Alive", "Bee Gees").id == "x"


def test_dash_suffix_and_accents():
    items = [item("Samba De Verao - Remastered 2004", "Astrud Gilberto")]
    assert best_match(items, "Samba de Verão", "Astrud Gilberto")
