"""The actions Claude can take. Pure Python (no SDK), so it's easy to test.

``server.py`` wraps each public method as an Agent SDK tool. Every method
returns a plain dict that is sent back to Claude as JSON.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from typing import Any

from music_agent.config import Config
from music_agent.learning.memory import Memory
from music_agent.learning.playlists import PlaylistStore, track_item
from music_agent.learning.watcher import PlaybackWatcher
from music_agent.models import MediaItem, part_of_day
from music_agent.players.base import MODES, Player, PlayerError, PolicyError
from music_agent.providers.base import MusicProvider, ProviderError, TrackLookup
from music_agent.text import format_minutes, format_track_time
from music_agent.weather import WeatherService

MAX_TRACKS_PER_CALL = 50
SEARCH_CONCURRENCY = 4
SENTIMENT = {"like": 1, "dislike": -1, "neutral": 0}


class ToolError(Exception):
    """Reported back to Claude as a failed tool call (the message is shown to it)."""


class MusicTools:
    """Everything the DJ can do: context, search, play, control, feedback, saved playlists.

    Args:
        config: Settings.
        player: Controls the speakers.
        provider: Searches the music service.
        now_fn: Clock, replaceable in tests.
        memory: Long-term memory; without it feedback can't be saved.
        weather: Adds local weather to the context.
        watcher: Told to ignore the next track change when the agent replaces the queue.
        playlists: Saved playlists; without it there are none to play.

    Attributes:
        request_id: Memory id of the request being handled; set by the agent.
        source: Who is acting, recorded with each queued track: "dj", "curator"
            or "gui" (quiz samples from the curator don't count as recently played).
    """

    def __init__(self, config: Config, player: Player, provider: MusicProvider,
                 now_fn: Callable[[], datetime] = datetime.now,
                 memory: Memory | None = None, weather: WeatherService | None = None,
                 watcher: PlaybackWatcher | None = None, playlists: PlaylistStore | None = None):
        self.config = config
        self.player = player
        self.provider = provider
        self._now = now_fn
        self.memory = memory
        self.weather = weather
        self.watcher = watcher
        self.playlists = playlists
        self.request_id: int | None = None
        self.source: str | None = None
        self.random = random.Random()  # for shuffling playlists; seeded in tests

    async def _run(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run a blocking player/provider call in a thread, turning its errors into ToolError."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except PolicyError as exc:
            raise ToolError(f"Refused by safety rule: {exc} Tell the user; do not try to work around it.") from exc
        except (PlayerError, ProviderError) as exc:
            raise ToolError(str(exc)) from exc

    def now(self) -> datetime:
        """Current local time (from the clock given to the constructor)."""
        return self._now()

    # ------------------------------------------------------------ context
    async def get_context(self) -> dict[str, Any]:
        """Describe the moment: time, safety limits, weather, taste, saved playlists and playback.

        Returns:
            Context for choosing music. If the speakers are unreachable,
            ``playback`` holds an ``error`` instead.
        """
        now = self._now()
        safety = self.config.safety
        ctx: dict[str, Any] = {
            "local_time": now.strftime("%H:%M"),
            "weekday": now.strftime("%A"),
            "date": now.strftime("%Y-%m-%d"),
            "part_of_day": part_of_day(now.hour),
            "quiet_hours": f"{safety.quiet_start:%H:%M}-{safety.quiet_end:%H:%M}",
            "in_quiet_hours": safety.in_quiet_hours(now.time()),
            "volume_cap": safety.volume_cap(now.time()),
        }
        if self.weather and self.weather.configured:
            weather = await asyncio.to_thread(self.weather.current)
            if weather:
                ctx["weather"] = weather.as_dict()
        if self.memory:
            ctx["taste"] = await asyncio.to_thread(self.memory.taste_profile, part_of_day(now.hour))
            guest_until = self.memory.guest_until()
            if guest_until:
                ctx["guest_mode"] = f"on until {guest_until:%H:%M}: music is for guests and doesn't train the taste"
        if self.playlists:
            saved = await asyncio.to_thread(self.playlists.list)
            if saved:
                ctx["saved_playlists"] = [
                    {"name": p["name"], "description": p["description"] or "", "tracks": p["tracks"],
                     "length": format_minutes(round(p["duration_s"] / 60)) if p["duration_s"] else "unknown"}
                    for p in saved if p["tracks"]]
        try:
            np = await self._run(self.player.now_playing)
            ctx["playback"] = {
                "state": np.state,
                "current": " - ".join(p for p in (np.title, np.artist) if p) or None,
                "queue_position": np.queue_position,
                "queue_length": np.queue_length,
                "rooms_in_group": np.rooms,
                "volume": np.volume,
            }
            ctx["all_rooms"] = await self._run(self.player.rooms)
        except ToolError as exc:
            ctx["playback"] = {"error": str(exc)}
        return ctx

    # ------------------------------------------------------------ search
    async def search_music(self, query: str, kind: str = "track", limit: int = 8) -> dict[str, Any]:
        """Search the catalogue.

        Args:
            query: Search text.
            kind: ``"track"``, ``"album"``, ``"playlist"`` or ``"artist"``.
            limit: Number of results, clamped to 1-25.

        Returns:
            Results with refs that ``play_item`` accepts.
        """
        limit = max(1, min(int(limit or 8), 25))
        items = await self._run(self.provider.search, query, kind, limit)
        return {"query": query, "kind": kind, "results": [_item_dict(i) for i in items]}

    # ------------------------------------------------------------ play
    async def play_tracks(self, tracks: list[dict[str, str]], mode: str = "replace",
                          rooms: str | list[str] | None = None, for_others: bool = False) -> dict[str, Any]:
        """Look up each requested song, then queue the ones found, in order.

        Lookups run in parallel (``SEARCH_CONCURRENCY`` at a time); duplicates
        are queued once.

        Args:
            tracks: ``{"title", "artist", "album"?}`` dicts.
            mode: ``"replace"``, ``"append"`` or ``"next"``.
            rooms: Room names, or None for the default.
            for_others: The music is for other people (guests, a party), so
                listening to it doesn't train the owner's taste.

        Returns:
            What was queued, total duration, and for each miss why it was
            missed plus the artist's real tracks to substitute.

        Raises:
            ToolError: Bad arguments, a safety refusal, or a player error.
        """
        if mode not in MODES:
            raise ToolError(f"mode must be one of {MODES}")
        if not tracks:
            raise ToolError("tracks is empty")
        if len(tracks) > MAX_TRACKS_PER_CALL:
            raise ToolError(f"At most {MAX_TRACKS_PER_CALL} tracks per call; split into several calls with mode='append'.")

        items, not_found = await self.lookup_tracks(tracks)
        if not items:
            return {"queued": 0, "not_found": not_found,
                    "note": "Nothing was found, so nothing changed. Pick other tracks, "
                            "preferably from catalogue_tracks_by_artist."}
        return {**await self._queue(items, mode, rooms, for_others), "not_found": not_found}

    async def lookup_tracks(self, tracks: list[dict[str, str]]) -> tuple[list[MediaItem], list[dict[str, Any]]]:
        """Find requested songs in the catalogue (``SEARCH_CONCURRENCY`` lookups at a time).

        Args:
            tracks: ``{"title", "artist", "album"?}`` dicts.

        Returns:
            ``(found items in order without duplicates, misses)``; each miss
            says why and may list the artist's real tracks to substitute.
        """
        lookups = await self._lookup_each(tracks)
        items, not_found, seen = [], [], set()
        for spec, lk in zip(tracks, lookups, strict=True):
            if lk is not None and lk.item is not None:
                if lk.item.ref not in seen:
                    seen.add(lk.item.ref)
                    items.append(lk.item)
                continue
            not_found.append(_miss(spec, lk))
        return items, not_found

    async def _lookup_each(self, tracks: list[dict[str, str]]) -> list[TrackLookup | None]:
        """Look up each requested song, in parallel; None where no title was given."""
        sem = asyncio.Semaphore(SEARCH_CONCURRENCY)

        async def lookup(spec: dict[str, str]) -> TrackLookup | None:
            title = (spec.get("title") or "").strip()
            if not title:
                return None
            async with sem:
                return await asyncio.to_thread(self.provider.lookup_track, title,
                                               spec.get("artist") or None, spec.get("album") or None)

        return list(await asyncio.gather(*(lookup(t) for t in tracks)))

    async def _queue(self, items: list[MediaItem], mode: str, rooms: str | list[str] | None,
                     for_others: bool = False) -> dict[str, Any]:
        """Queue found tracks, record them as plays (and for whom), and summarise what happened."""
        self._starting(mode, for_others)
        result = await self._run(self.player.play, items, rooms, mode)
        if self.memory and result.queued:
            await asyncio.to_thread(self.memory.record_plays, result.queued, self.request_id, self.source,
                                    "others" if for_others else "me")
        total = sum(i.duration_s or 0 for i in result.queued)
        out = {
            "queued": len(result.queued),
            "mode": mode,
            "total_duration": format_minutes(round(total / 60)) if total else "unknown",
            "rooms": result.rooms,
            "volume": result.volume,
            "tracks": [i.label() for i in result.queued],
            "failed_to_queue": [f"{i.label()}: {err}" for i, err in result.failed],
        }
        if for_others or (self.memory and self.memory.guest_until()):
            out["for_others"] = True
        return out

    def _starting(self, mode: str, for_others: bool) -> None:
        """Before queueing: a new queue isn't a skip of the old one, and remember who it's for."""
        if mode != "replace":
            return
        if self.watcher:
            self.watcher.ignore_next_change()
        if self.memory:
            self.memory.queue_audience = "others" if for_others else "me"

    async def play_playlist(self, name: str | int, mode: str = "replace", rooms: str | list[str] | None = None,
                            shuffle: bool = False, for_others: bool = False) -> dict[str, Any]:
        """Play a saved playlist. Its tracks are already known, so this needs no searching.

        Tracks saved under another music service (after switching service)
        are looked up again by title and artist.

        Args:
            name: Playlist name (or id).
            mode: ``"replace"``, ``"append"`` or ``"next"``.
            rooms: Room names, or None for the default.
            shuffle: Play the tracks in random order.
            for_others: The music is for other people; see ``play_tracks``.

        Returns:
            What was queued (the first few titles) and the total length.

        Raises:
            ToolError: No such playlist, it is empty, or playback failed.
        """
        if mode not in MODES:
            raise ToolError(f"mode must be one of {MODES}")
        playlist = await self.find_playlist(name)
        if not playlist["tracks"]:
            raise ToolError(f"Playlist '{playlist['name']}' is empty.")
        tracks = list(playlist["tracks"])
        if shuffle:
            self.random.shuffle(tracks)

        # Keep the order: use stored refs, and look up only the tracks without a usable one.
        slots: list[MediaItem | dict[str, str]] = []
        for t in tracks:
            item = track_item(t)
            slots.append(item if item and item.provider == self.provider.name
                         else {"title": t["title"], "artist": t["artist"] or "", "album": t["album"] or ""})
        lookups = iter(await self._lookup_each([s for s in slots if isinstance(s, dict)]))
        items, not_found, seen = [], [], set()
        for slot in slots:
            item = slot
            if isinstance(slot, dict):
                lk = next(lookups)
                if lk is None or lk.item is None:
                    not_found.append(f"{slot['title']} - {slot['artist'] or '?'}")
                    continue
                item = lk.item
            if item.ref not in seen:
                seen.add(item.ref)
                items.append(item)
        if not items:
            raise ToolError(f"None of the tracks in '{playlist['name']}' could be found in the catalogue.")

        out = await self._queue(items, mode, rooms, for_others)
        shown = 8
        more = len(out["tracks"]) - shown
        return {"playlist": playlist["name"], **out, "shuffled": shuffle,
                "tracks": out["tracks"][:shown] + ([f"... and {more} more"] if more > 0 else []),
                "not_found": not_found}

    async def find_playlist(self, name: str | int) -> dict[str, Any]:
        """Return a saved playlist with its tracks, by name or id.

        Raises:
            ToolError: Playlists are off, or there's no such playlist (the
                message lists the ones that exist).
        """
        if not self.playlists:
            raise ToolError("Saved playlists need memory, which is turned off.")
        playlist = await asyncio.to_thread(self.playlists.find, name)
        if playlist is None:
            names = [p["name"] for p in await asyncio.to_thread(self.playlists.list)]
            raise ToolError(f"No playlist called '{name}'. Saved playlists: {', '.join(names) or 'none yet'}.")
        return playlist

    async def play_item(self, ref: str, mode: str = "replace",
                        rooms: str | list[str] | None = None, for_others: bool = False) -> dict[str, Any]:
        """Play a search result (album, playlist or track) by its ref.

        Args:
            ref: A ref from ``search_music``.
            mode: ``"replace"``, ``"append"`` or ``"next"``.
            rooms: Room names, or None for the default.
            for_others: The music is for other people; see ``play_tracks``.

        Returns:
            What was queued and where.

        Raises:
            ToolError: The ref is malformed or from another provider, or playback failed.
        """
        try:
            provider, kind, item_id = MediaItem.split_ref(ref)
        except ValueError:
            raise ToolError("ref must come from search_music results") from None
        if provider != self.provider.name:
            raise ToolError(f"ref is for provider '{provider}', current provider is '{self.provider.name}'")
        item = MediaItem(provider, kind, item_id, title=ref)
        self._starting(mode, for_others)
        result = await self._run(self.player.play, [item], rooms, mode)
        return {"queued": ref, "kind": kind, "mode": mode, "rooms": result.rooms, "volume": result.volume}

    # ------------------------------------------------------------ control
    async def playback_control(self, action: str) -> dict[str, Any]:
        """Pause, resume, skip or stop.

        Args:
            action: ``"pause"``, ``"resume"``, ``"skip"`` or ``"stop"``.

        Returns:
            ``{"done": action}``.
        """
        actions = {"pause": self.player.pause, "resume": self.player.resume,
                   "skip": self.player.skip, "stop": self.player.stop}
        if action not in actions:
            raise ToolError(f"action must be one of {sorted(actions)}")
        await self._run(actions[action])
        return {"done": action}

    async def set_volume(self, level: int | None = None, change: int | None = None,
                         room: str | None = None) -> dict[str, Any]:
        """Set the volume of the whole group, or of one room. Give exactly one of ``level`` / ``change``.

        The group keeps its balance (rooms set louder or quieter by hand stay
        that way); setting one room saves a new balance.

        Args:
            level: Absolute level, 0-100.
            change: Relative change, e.g. +4 or -4.
            room: Only this room; None for the whole group.

        Returns:
            The applied volume (and the room), with a note when it hit the safety cap.
        """
        if (level is None) == (change is None):
            raise ToolError("Give exactly one of level (0-100) or change (e.g. +4 / -4).")
        if room and level is not None:
            applied = await self._run(self.player.set_room_volume, room, int(level))
        elif room:
            applied = await self._run(self.player.change_room_volume, room, int(change))
        elif level is not None:
            applied = await self._run(self.player.set_volume, int(level))
        else:
            applied = await self._run(self.player.change_volume, int(change))
        cap = self.config.safety.volume_cap(self._now().time())
        out: dict[str, Any] = {"volume": applied}
        if room:
            out["room"] = room
        if applied == cap:
            out["note"] = f"At the safety cap ({cap}); cannot go higher."
        return out

    async def room_volumes(self) -> dict[str, Any]:
        """Every speaker's volume and saved balance.

        Returns:
            ``rooms``: one entry per speaker (``room``, ``volume``, ``difference``,
            ``in_group``, ``reference``), and the current ``volume_cap``.
        """
        rooms = await self._run(self.player.room_volumes)
        return {"rooms": [asdict(r) for r in rooms],
                "volume_cap": self.config.safety.volume_cap(self._now().time())}

    async def reset_balance(self) -> dict[str, Any]:
        """Forget the per-room balance: every speaker plays at the same level again."""
        return {"volume": await self._run(self.player.reset_balance), "balance": "reset"}

    async def get_queue(self, limit: int = 20) -> dict[str, Any]:
        """List the queue.

        Args:
            limit: Number of entries, clamped to 1-100.

        Returns:
            The current 1-based position and the queue lines.
        """
        tracks = await self._run(self.player.queue, max(1, min(int(limit or 20), 100)))
        np = await self._run(self.player.now_playing)
        return {"current_position": np.queue_position, "queue": tracks}

    # ------------------------------------------------------------ feedback
    async def record_feedback(self, sentiment: str, about: str, title: str | None = None,
                              artist: str | None = None, note: str | None = None) -> dict[str, Any]:
        """Store the person's opinion so future choices improve.

        Args:
            sentiment: ``"like"``, ``"dislike"`` or ``"neutral"``.
            about: ``"current_track"``, ``"track"``, ``"artist"`` or ``"general"``.
            title: Track title, for ``about="track"``.
            artist: Artist, for ``about="track"`` or ``"artist"``.
            note: The person's point in a few words; required for ``"general"``.

        Returns:
            The saved entry's id and what it is about.

        Raises:
            ToolError: Memory is off, nothing is playing, or arguments are missing.
        """
        if not self.memory:
            raise ToolError("Memory is turned off; feedback cannot be saved.")
        if sentiment not in SENTIMENT:
            raise ToolError(f"sentiment must be one of {sorted(SENTIMENT)}")
        if about == "current_track":
            np = await self._run(self.player.now_playing)
            if not np.title:
                raise ToolError("Nothing is playing, so there is no current track to rate.")
            about, title, artist = "track", np.title, np.artist
            if not artist:
                artist = await asyncio.to_thread(self.memory.artist_for_title, title)
        if about not in ("track", "artist", "general"):
            raise ToolError("about must be current_track, track, artist or general")
        try:
            fid = await asyncio.to_thread(self.memory.record_feedback, SENTIMENT[sentiment], about,
                                          title, artist, note, self.request_id, self.source)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        target = {"track": f"{title} - {artist}", "artist": artist, "general": note}[about]
        return {"saved": True, "id": fid, "sentiment": sentiment, "about": about, "target": target}


def _item_dict(item: MediaItem) -> dict[str, Any]:
    """A search result as compact JSON for Claude (unknown fields left out)."""
    d = {"ref": item.ref, "title": item.title, "artist": item.artist, "album": item.album}
    if item.duration_s:
        d["duration"] = format_track_time(item.duration_s)
    return {k: v for k, v in d.items() if v is not None}


def _miss(spec: dict[str, str], lk: TrackLookup | None) -> dict[str, Any]:
    """Explain a miss and show the artist's real catalogue so Claude can substitute."""
    miss: dict[str, Any] = {"requested": f"{spec.get('title')} - {spec.get('artist', '?')}"}
    if lk is None:
        miss["reason"] = "no title given"
    elif lk.error:
        miss["reason"] = f"search error: {lk.error}"
    elif lk.artist_tracks:
        miss["reason"] = "artist is in the catalogue but this title was not found (it may not exist under this name)"
        miss["catalogue_tracks_by_artist"] = [i.title for i in lk.artist_tracks[:10]]
    else:
        miss["reason"] = "artist not found in the catalogue"
    return miss
