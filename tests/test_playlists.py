"""Saved playlists in the memory database."""
from __future__ import annotations

import pytest

from music_agent.learning.playlists import MAX_TRACKS, PlaylistStore, track_item
from tests.fakes import CATALOGUE, item, make_memory


@pytest.fixture
def store():
    return PlaylistStore(make_memory().db)


def test_create_find_rename_delete(store):
    pid = store.create("Cooking", "Warm jazz for cooking")
    assert store.find("cooking")["id"] == pid and store.find(str(pid))["name"] == "Cooking"
    with pytest.raises(ValueError, match="already"):
        store.create("COOKING")
    with pytest.raises(ValueError):
        store.create("  ")
    other = store.create("Work")
    with pytest.raises(ValueError, match="already used"):
        store.update(other, name="cooking")
    assert store.update(pid, name="Dinner", description="")
    assert store.get(pid)["name"] == "Dinner" and store.get(pid)["description"] is None
    assert store.delete(pid) and store.find("Dinner") is None and not store.delete(pid)


def test_tracks_replace_append_dedupe_and_remove(store):
    pid = store.create("Mix")
    assert store.set_tracks(pid, CATALOGUE[:3]) == 3
    assert store.set_tracks(pid, [CATALOGUE[2], CATALOGUE[4]], mode="append") == 1    # t3 already there
    tracks = store.get(pid)["tracks"]
    assert [t["position"] for t in tracks] == [1, 2, 3, 4] and tracks[3]["title"] == "So What"
    assert store.list()[0]["tracks"] == 4 and store.list()[0]["duration_s"] == 234 + 320 + 248 + 562

    assert store.remove_track(pid, tracks[1]["id"]) and not store.remove_track(pid, tracks[1]["id"])
    assert [t["position"] for t in store.get(pid)["tracks"]] == [1, 2, 3]
    assert store.set_tracks(pid, [CATALOGUE[5]]) == 1 and len(store.get(pid)["tracks"]) == 1   # replace

    with pytest.raises(ValueError, match="at most"):
        store.set_tracks(pid, [item(f"Song {n}", "X", n) for n in range(MAX_TRACKS + 1)])
    assert len(store.get(pid)["tracks"]) == 1                                         # rolled back
    with pytest.raises(KeyError):
        store.set_tracks(999, CATALOGUE[:1])


def test_deleting_a_playlist_deletes_its_tracks(store):
    pid = store.create("Mix")
    store.set_tracks(pid, CATALOGUE[:2])
    store.delete(pid)
    assert store.db.rows("playlist_tracks")["total"] == 0


def test_stored_tracks_play_back_as_the_same_items(store):
    pid = store.create("Mix")
    store.set_tracks(pid, [CATALOGUE[4]])
    assert track_item(store.get(pid)["tracks"][0]) == CATALOGUE[4]
    assert track_item({"ref": None, "title": "x"}) is None
