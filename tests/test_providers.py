"""Catalogue search: the lookup cascade and the Sonos-linked service provider."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest
from soco.core import to_didl_string
from soco.music_services.data_structures import get_class

from music_agent.agent.tools import MusicTools
from music_agent.models import MediaItem
from music_agent.providers.base import ProviderError
from music_agent.providers.sonos_service import SonosServiceProvider
from tests.fakes import (
    FakeProvider,
    make_player,
)


def t(n, title, artist, album="Album"):
    return MediaItem("fake", "track", f"t{n}", title, artist, album, 200)


class TitleOnlyCatalogue(FakeProvider):
    """Mimics a service whose track search ranks by title and returns `limit` results,
    with 30 other artists' "Cielito Lindo" ahead of Lila Downs'."""
    def __init__(self):
        covers = [t(100 + i, "Cielito Lindo", f"Mariachi Band {i}") for i in range(30)]
        lila = [t(1, "Cielito Lindo", "Lila Downs", "Pecados y Milagros"),
                t(2, "La Cumbia del Mole", "Lila Downs"), t(3, "Zapata Se Queda", "Lila Downs")]
        super().__init__(covers + lila)
        self.queries = []

    def search(self, query, kind="track", limit=10):
        self.queries.append((query, limit))
        q = query.lower()
        if q == "lila downs":                       # artist query -> artist's tracks
            return [i for i in self.catalogue if i.artist == "Lila Downs"][:limit]
        if "lila downs" in q:                       # combined query -> nothing (title-only index)
            return []
        return [i for i in self.catalogue if i.title.lower() in q or q in i.title.lower()][:limit]


def test_cascade_finds_song_buried_under_other_versions():
    p = TitleOnlyCatalogue()
    lk = p.lookup_track("Cielito Lindo", "Lila Downs")
    assert lk.item.artist == "Lila Downs" and lk.item.album == "Pecados y Milagros"
    assert [q for q, _ in p.queries] == ["Lila Downs Cielito Lindo", "Cielito Lindo", "Lila Downs"]


def test_miss_lists_real_tracks_by_artist():
    p = TitleOnlyCatalogue()
    lk = p.lookup_track("La Bikina", "Lila Downs")
    assert lk.item is None and lk.error is None
    assert [i.title for i in lk.artist_tracks] == ["Cielito Lindo", "La Cumbia del Mole", "Zapata Se Queda"]


def test_errors_are_retried_then_reported_not_hidden(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda s: None)
    calls = []

    class Flaky(FakeProvider):
        def search(self, query, kind="track", limit=10):
            calls.append(query)
            if len(calls) == 1:
                raise ProviderError("timeout")      # first try fails, retry succeeds
            return super().search(query, kind, limit)

    p = Flaky([t(1, "So What", "Miles Davis")])
    assert p.lookup_track("So What", "Miles Davis").item is not None
    assert len(calls) == 2

    class Down(FakeProvider):
        def search(self, query, kind="track", limit=10):
            raise ProviderError("service unavailable")
    lk = Down().lookup_track("So What", "Miles Davis")
    assert lk.item is None and lk.error == "service unavailable"


def test_play_tracks_reports_reasons_and_suggestions():
    player, house = make_player()
    p = TitleOnlyCatalogue()
    player.provider = p
    tools = MusicTools(player.config, player, p, now_fn=lambda: __import__("datetime").datetime(2026, 9, 30, 17))
    out = asyncio.run(tools.play_tracks([
        {"title": "Cielito Lindo", "artist": "Lila Downs"},
        {"title": "La Bikina", "artist": "Lila Downs"},
        {"title": "Zambo", "artist": "Bebel Gilberto"},
    ]))
    assert out["queued"] == 1 and out["tracks"] == ["Cielito Lindo - Lila Downs (Pecados y Milagros)"]
    bikina, zambo = out["not_found"]
    assert "La Cumbia del Mole" in bikina["catalogue_tracks_by_artist"]
    assert zambo["reason"] == "artist not found in the catalogue"


FAKE_SERVICE = SimpleNamespace(desc="SA_RINCON7943_", service_id=31,
                               sonos_uri_from_id=lambda i: f"soco://{i}?sid=31&sn=0")


def qobuz_track(tid, title, artist, album, duration):
    cls = get_class("MediaMetadataTrack")
    return cls.from_music_service(FAKE_SERVICE, {
        "id": f"track:{tid}:6", "itemType": "track", "title": title, "mimeType": "audio/flac",
        "trackMetadata": {"artist": artist, "album": album, "duration": str(duration)},
    })


@pytest.fixture
def provider():
    p = SonosServiceProvider("Qobuz", device_fn=lambda: None)
    svc = mock.MagicMock()
    svc.available_search_categories = ["artists", "albums", "tracks", "playlists"]
    svc.desc, svc.service_id = FAKE_SERVICE.desc, FAKE_SERVICE.service_id
    svc.sonos_uri_from_id = FAKE_SERVICE.sonos_uri_from_id
    svc.search.return_value = [
        qobuz_track(1, "One More Time (Karaoke)", "Hit Crew", "Karaoke Hits", 320),
        qobuz_track(1068442, "One More Time", "Daft Punk", "Discovery", 320),
    ]
    p._service = svc
    return p


def test_search_parses_artist_album_duration(provider):
    items = provider.search("one more time")
    assert items[1].artist == "Daft Punk" and items[1].album == "Discovery"
    assert items[1].duration_s == 320 and items[1].kind == "track"
    assert items[1].ref == "qobuz|track|track:1068442:6"


def test_find_track_picks_original(provider):
    assert provider.find_track("One More Time", "Daft Punk").id == "track:1068442:6"


def test_to_native_rebuilds_identical_item_after_cache_loss(provider):
    item = provider.search("x")[1]
    original = provider.to_native(item)
    provider._native.clear()                      # e.g. item loaded from the database later
    rebuilt = provider.to_native(item)
    assert rebuilt is not original
    assert rebuilt.uri == original.uri and rebuilt.item_id == original.item_id
    assert to_didl_string(rebuilt) == to_didl_string(original)


def test_unsupported_category_is_clear_error(provider):
    provider._service.available_search_categories = ["tracks"]
    with pytest.raises(ProviderError, match="cannot search albums"):
        provider.search("x", "album")


def test_service_errors_mention_relinking(provider):
    provider._service.search.side_effect = RuntimeError("SOAP fault: auth")
    with pytest.raises(ProviderError, match="re-link"):
        provider.search("x")


# ------------------------------------------------------------------ SoCo category race (seen in bot.log)
class RacySoCoService:
    """Mimics SoCo: marks the category map as loaded *before* downloading it."""

    def __init__(self, download_ok=True):
        self._search_prefix_map = None
        self.download_ok = download_ok
        self.downloads = 0

    @property
    def available_search_categories(self):
        import time as _time
        if self._search_prefix_map is not None:
            return list(self._search_prefix_map)
        self._search_prefix_map = {}
        self.downloads += 1
        _time.sleep(0.05)                       # network download
        if self.download_ok:
            self._search_prefix_map.update({"tracks": "x", "albums": "y"})
        return list(self._search_prefix_map)

    def search(self, category, term, count):
        if category not in (self._search_prefix_map or {}):
            raise RuntimeError(f"Qobuz does not support the '{category}' search category")
        return []


def test_parallel_searches_never_see_a_half_loaded_category_list():
    from concurrent.futures import ThreadPoolExecutor
    p = SonosServiceProvider("Qobuz", device_fn=lambda: None)
    p._service = RacySoCoService()
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda q: p.search(q), [f"song {i}" for i in range(16)]))
    assert results == [[]] * 16 and p._service.downloads == 1


def test_failed_category_download_is_retried_not_kept():
    p = SonosServiceProvider("Qobuz", device_fn=lambda: None)
    p._service = RacySoCoService(download_ok=False)
    with pytest.raises(ProviderError, match="Will retry"):
        p.search("x")
    assert p._service._search_prefix_map is None   # SoCo's empty map was discarded
    p._service.download_ok = True
    assert p.search("x") == [] and p._service.downloads == 2
