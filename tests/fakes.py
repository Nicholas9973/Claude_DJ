"""Test doubles and helpers shared by the test suite: fake speakers, a fake
catalogue, fixed clocks, and builders for the player / tools / memory."""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

from music_agent.agent.tools import MusicTools
from music_agent.config import Config
from music_agent.learning.memory import Memory
from music_agent.learning.playlists import PlaylistStore
from music_agent.learning.watcher import PlaybackWatcher
from music_agent.models import MediaItem
from music_agent.players.sonos import SonosPlayer
from music_agent.providers.base import MusicProvider
from music_agent.providers.matching import rank
from music_agent.text import norm


class Household:
    def __init__(self, names_and_volumes):
        self.speakers = [FakeSpeaker(self, n, v) for n, v in names_and_volumes]
        self.coord_of = {s: s for s in self.speakers}  # speaker -> its coordinator

    def by_name(self, name):
        return next(s for s in self.speakers if s.player_name == name)


class FakeGroup:
    def __init__(self, house, coord):
        self.coordinator = coord
        self.members = {s for s, c in house.coord_of.items() if c is coord}


class FakeSpeaker:
    def __init__(self, house, name, volume):
        self.house, self.player_name, self.volume = house, name, volume
        self.queue_items, self.state, self.current = [], "STOPPED", 0
        self.calls = []
        self.stale_reads = 0  # > 0: the next N `.group` reads return None (stale SoCo cache)
        self.zone_group_state = SimpleNamespace(clear_cache=lambda: None)

    def __repr__(self):
        return f"<{self.player_name}>"

    @property
    def group(self):
        if self.stale_reads:
            self.stale_reads -= 1
            return None
        return FakeGroup(self.house, self.house.coord_of[self])

    def join(self, master):
        self.calls.append(("join", master.player_name))
        # Joining moves only this speaker; if it was a coordinator its group dissolves.
        for s, c in list(self.house.coord_of.items()):
            if c is self and s is not self:
                self.house.coord_of[s] = s
        self.house.coord_of[self] = master

    def unjoin(self):
        self.calls.append(("unjoin",))
        self.house.coord_of[self] = self

    # queue / transport (only meaningful on a coordinator)
    def clear_queue(self):
        self.queue_items = []

    def add_to_queue(self, item, position=0):
        if getattr(item, "fail", False):
            raise Refused("UPnP Error 800 received")
        if position:
            self.queue_items.insert(position - 1, item)
            return position
        self.queue_items.append(item)
        return len(self.queue_items)

    def play_from_queue(self, index):
        self.current, self.state = index, "PLAYING"

    def play(self):
        self.state = "PLAYING"

    def pause(self):
        self.state = "PAUSED_PLAYBACK"

    def stop(self):
        self.state = "STOPPED"

    def next(self):
        self.current += 1

    def get_current_transport_info(self):
        return {"current_transport_state": self.state}

    def get_current_track_info(self):
        pos = self.current + 1 if self.queue_items else 0
        item = self.queue_items[self.current] if self.queue_items else None
        return {"title": getattr(item, "title", ""), "artist": "", "album": "",
                "position": "0:00:10", "duration": "0:03:00", "playlist_position": str(pos)}

    @property
    def queue_size(self):
        return len(self.queue_items)

    def get_queue(self, max_items=20):
        return [SimpleNamespace(title=i.title, creator="") for i in self.queue_items[:max_items]]


class FakeProvider(MusicProvider):
    """Provider whose native objects are simple stand-ins; search matches words."""
    name = "fake"

    def __init__(self, catalogue=()):
        self.catalogue = list(catalogue)
        self.bad_refs = set()

    def search(self, query, kind="track", limit=10):
        words = norm(query).split()
        hits = [i for i in self.catalogue if i.kind == kind and
                any(w in norm(f"{i.title} {i.artist}") for w in words)]
        return hits[:limit]

    def to_native(self, item):
        return SimpleNamespace(title=item.title, fail=item.ref in self.bad_refs)


class Refused(Exception):
    """What SoCo raises when a speaker won't queue an item (UPnP error 800)."""

    error_code = "800"


# ----------------------------------------------------------------- clocks
def DAY():  # noqa: N802 - used like a constant: a fixed Tuesday evening
    return datetime(2026, 9, 29, 18, 0)


def NIGHT():  # noqa: N802 - inside the default quiet hours
    return datetime(2026, 9, 29, 23, 30)


class Clock:
    """A settable clock: `clock.t += timedelta(days=1)`."""

    def __init__(self, t=datetime(2026, 10, 1, 18, 30)):
        self.t = t

    def __call__(self):
        return self.t


# ----------------------------------------------------------------- catalogue
def track(title, artist="Daft Punk", album=None, n=1):
    return MediaItem("fake", "track", f"track:{n}", title, artist, album, 200)


def item(title, artist, n=1):
    return MediaItem("fake", "track", f"t{n}", title, artist, "Album", 240)


CATALOGUE = [
    MediaItem("fake", "track", "t1", "One More Time", "Daft Punk", "One More Time", 234),
    MediaItem("fake", "track", "t2", "One More Time", "Daft Punk", "Discovery", 320),
    MediaItem("fake", "track", "t3", "Get Lucky (Radio Edit)", "Daft Punk", "Get Lucky", 248),
    MediaItem("fake", "track", "t4", "Get Lucky", "Daft Punk", "Random Access Memories", 369),
    MediaItem("fake", "track", "t5", "So What", "Miles Davis", "Kind of Blue", 562),
    MediaItem("fake", "track", "t10", "Pecadora", "Lila Downs", "Album", 240),
    MediaItem("fake", "track", "t11", "Chan Chan", "Buena Vista Social Club", "Album", 240),
    MediaItem("fake", "album", "a1", "Kind of Blue", "Miles Davis"),
]


def best_match(items, title, artist=None, album=None):
    """The top-ranked match, or None (what lookup_track does per search)."""
    ranked = rank(items, title, artist, album)
    return ranked[0][1] if ranked and ranked[0][0] > 0 else None


# ----------------------------------------------------------------- builders
def make_player(now=DAY, volumes=(("Dining Room", 13), ("Morn\u2019", 13), ("Night", 18)), **safety):
    """A SonosPlayer wired to a fake household. Returns (player, household)."""
    house = Household(volumes)
    cfg = Config()
    for k, v in safety.items():
        setattr(cfg.safety, k, v)
    player = SonosPlayer(cfg, FakeProvider(), now_fn=now)
    player._speakers = sorted(house.speakers, key=lambda s: s.player_name)
    return player, house


def make_memory(clock=None):
    return Memory(":memory:", half_life_days=60, now_fn=clock or Clock())


def make_tools(now=DAY, memory=None, weather=None, catalogue=CATALOGUE):
    """MusicTools over fake speakers and the fake catalogue. Returns (tools, household).

    With a memory, the tools also get saved playlists in the same database.
    """
    player, house = make_player(now=now)
    provider = FakeProvider(catalogue)
    player.provider = provider
    watcher = PlaybackWatcher(player, memory, 15) if memory else None
    playlists = PlaylistStore(memory.db, now_fn=Clock()) if memory else None
    tools = MusicTools(player.config, player, provider, now_fn=now,
                       memory=memory, weather=weather, watcher=watcher, playlists=playlists)
    return tools, house


def run(coro):
    return asyncio.run(coro)


def fake_weather_fetch(calls):
    """Stands in for the Open-Meteo HTTP calls; records each URL in `calls`."""
    def fetch(url, params):
        calls.append(url)
        if "geocoding" in url:
            return {"results": [{"name": "Lyon", "country": "France", "latitude": 45.76, "longitude": 4.84}]}
        return {"current": {"temperature_2m": 14.6, "weather_code": 61, "is_day": 0,
                            "precipitation": 0.4, "wind_speed_10m": 12.2},
                "daily": {"sunrise": ["2026-10-01T07:47"], "sunset": ["2026-10-01T19:24"]}}
    return fetch


def fake_trackinfo_fetch(calls, fail=()):
    """Stands in for MusicBrainz and Deezer: knows "Superstition" by Stevie Wonder, nothing else.

    Records each URL in `calls`; raises for URLs containing any text in `fail`.
    """
    def fetch(url, params):
        calls.append(url)
        if any(f in url for f in fail):
            raise OSError("offline")
        known = "Superstition" in str(params.get("query", params.get("q", "")))
        if url.endswith("/recording"):
            stevie = [{"name": "Stevie Wonder", "artist": {"id": "sw"}}]
            return {"recordings": [] if not known else [
                {"score": 100, "title": "Superstition", "first-release-date": "1972-10-27", "artist-credit": stevie,
                 "tags": [{"name": "soul", "count": 5}, {"name": "funk", "count": 3},
                          {"name": "dance/by-style/ddr", "count": 9}]},
                {"score": 100, "title": "Superstition (live)", "first-release-date": "1980", "artist-credit": stevie,
                 "tags": [{"name": "funk", "count": 4}]},
                {"score": 100, "title": "Superstition", "first-release-date": "1971",       # another artist's song
                 "artist-credit": [{"name": "Beck, Bogert & Appice", "artist": {"id": "bba"}}]},
            ]}
        if "/artist/" in url:
            return {"genres": [{"name": "soul", "count": 11}, {"name": "funk", "count": 4}]}
        if url.endswith("/search"):
            return {"data": [] if not known else [
                {"id": 1, "title": "Superstition", "rank": 500_000, "artist": {"name": "Stevie Wonder"}},
                {"id": 2, "title": "Superstition", "rank": 736_361, "artist": {"name": "Stevie Wonder"}},
                {"id": 3, "title": "Superstition", "rank": 999_999, "artist": {"name": "Someone Else"}}]}
        if "/track/" in url:
            return {"bpm": 200.7}    # Deezer's usual doubling
        raise AssertionError(url)
    return fetch


class FakeTrackInfo:
    """Stands in for TrackInfoService in the curator's tests: popularity from a dict, no network."""

    def __init__(self, popularity=None):
        self.popularity_of = popularity or {}
        self.calls = []

    def look_up(self, title, artist, refresh=False):
        self.calls.append((title, artist))
        if title not in self.popularity_of:
            return {"title": title, "artist": artist, "status": "not_found"}
        return {"title": title, "artist": artist, "status": "found", "popularity": self.popularity_of[title]}


# ----------------------------------------------------------------- agent
class ScriptedAgent:
    """Stands in for MusicAgent: real MusicTools, scripted Claude behaviour."""
    def __init__(self, tools, script=None, role="dj"):
        self.tools = tools
        self.role = role
        self.script = script or (lambda text: [])
        self.started = self.resets = 0
        self.prompts = []

    async def start(self):
        self.started += 1

    async def close(self):
        pass

    async def reset(self):
        self.resets += 1

    async def ask(self, text):
        self.prompts.append(text)
        for ev in self.script(text):
            if isinstance(ev, Exception):
                raise ev
            yield ev
