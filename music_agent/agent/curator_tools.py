"""The extra actions the taste curator can take: preferences, score nudges, its notebook, playlists.

The curator also borrows some of the DJ's ``MusicTools`` (context, playing
samples for a quiz, recording reactions); ``server.py`` decides which. Like
``tools.py``, this is plain Python returning dicts, so it is easy to test.
"""

from __future__ import annotations

import asyncio
from typing import Any

from music_agent.agent.tools import SENTIMENT, MusicTools, ToolError
from music_agent.learning.memory import Memory
from music_agent.learning.playlists import PlaylistStore
from music_agent.models import part_of_day
from music_agent.text import format_minutes, norm

MAX_PLAYLIST_TRACKS_PER_CALL = 50


class CuratorTools:
    """Look at and change what shapes the DJ's choices.

    Args:
        music: The DJ's tools; their memory, playlists and catalogue search are reused.
    """

    def __init__(self, music: MusicTools):
        self.music = music

    @property
    def memory(self) -> Memory:
        """The long-term memory; raises ToolError when it is turned off."""
        if not self.music.memory:
            raise ToolError("Memory is turned off, so preferences can't be read or saved.")
        return self.music.memory

    @property
    def playlists(self) -> PlaylistStore:
        """The saved playlists; raises ToolError when memory is turned off."""
        if not self.music.playlists:
            raise ToolError("Saved playlists need memory, which is turned off.")
        return self.music.playlists

    # ------------------------------------------------------------ taste
    async def get_taste(self) -> dict[str, Any]:
        """Everything that shapes the DJ's choices, with ids for changing it.

        Returns:
            ``profile`` (exactly what the DJ sees), ``preferences``, recent
            ``feedback``, score ``adjustments`` and the ``notebook``, all with
            ids; the strongest learned ``scores`` with where they came from;
            and the ``saved_playlists``.
        """
        memory = self.memory
        part = part_of_day(self.music.now().hour)
        profile, prefs, feedback, scores, adjustments, notes = await asyncio.gather(
            asyncio.to_thread(memory.taste_profile, part),
            asyncio.to_thread(memory.preferences),
            asyncio.to_thread(memory.recent_feedback, 25),
            asyncio.to_thread(memory.affinities, 15),
            asyncio.to_thread(memory.adjustments),
            asyncio.to_thread(memory.notes, 40),
        )
        playlists = await asyncio.to_thread(self.music.playlists.list) if self.music.playlists else []
        compact = {kind: [{"name": e["name"], "score": e["score"], "from": {k: v for k, v in e["parts"].items() if v}}
                          for e in entries] for kind, entries in scores.items()}
        return {
            "profile": profile,
            "preferences": [{"id": p["id"], "text": p["text"]} for p in prefs],
            "recent_feedback": [{"id": f["id"], "text": _feedback_text(f), "date": f["ts"][:10]} for f in feedback],
            "adjustments": [{"id": a["id"], "text": a["text"]} for a in adjustments],
            "notebook": [_note_dict(n) for n in notes],
            "scores": compact,
            "saved_playlists": [{"name": p["name"], "description": p["description"] or "", "tracks": p["tracks"]}
                                for p in playlists],
        }

    async def add_preference(self, kind: str, sentiment: str, subject: str, artist: str | None = None,
                             part_of_day: str | None = None, note: str | None = None) -> dict[str, Any]:
        """Save something the person states about their taste. It never fades.

        Args:
            kind: ``"artist"``, ``"track"``, ``"genre"`` or ``"rule"``.
            sentiment: ``"like"``, ``"dislike"`` or ``"neutral"`` (for rules).
            subject: The artist, track title, genre, or the rule in a few words.
            artist: The artist, for ``kind="track"``.
            part_of_day: Only at this time of day; omit for any time.
            note: Nuance in the person's words.

        Returns:
            The saved preference's id and how it reads.
        """
        if sentiment not in SENTIMENT:
            raise ToolError(f"sentiment must be one of {sorted(SENTIMENT)}")
        memory = self.memory
        try:
            pid = await asyncio.to_thread(memory.add_preference, kind, SENTIMENT[sentiment], subject,
                                          artist, part_of_day or None, note)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        saved = next(p for p in await asyncio.to_thread(memory.preferences) if p["id"] == pid)
        return {"saved": True, "id": pid, "preference": saved["text"]}

    async def update_preference(self, preference_id: int, kind: str | None = None, sentiment: str | None = None,
                                subject: str | None = None, artist: str | None = None,
                                part_of_day: str | None = None, note: str | None = None) -> dict[str, Any]:
        """Change a stated preference; fields left out stay as they are.

        Args:
            preference_id: Id from ``get_taste``.
            kind: ``"artist"``, ``"track"``, ``"genre"`` or ``"rule"``.
            sentiment: ``"like"``, ``"dislike"`` or ``"neutral"``.
            subject: The artist, track title, genre, or the rule.
            artist: The artist, for ``kind="track"``.
            part_of_day: A part of day, or ``"any"`` for any time.
            note: Nuance in the person's words (``""`` removes it).

        Returns:
            How the preference reads now.
        """
        changes: dict[str, Any] = {k: v for k, v in
                                   {"kind": kind, "subject": subject, "artist": artist, "note": note}.items()
                                   if v is not None}
        if sentiment is not None:
            if sentiment not in SENTIMENT:
                raise ToolError(f"sentiment must be one of {sorted(SENTIMENT)}")
            changes["sentiment"] = SENTIMENT[sentiment]
        if part_of_day is not None:
            changes["part"] = None if part_of_day in ("", "any") else part_of_day
        if not changes:
            raise ToolError("Nothing to change.")
        try:
            updated = await asyncio.to_thread(lambda: self.memory.update_preference(int(preference_id), **changes))
        except KeyError:
            raise ToolError(f"No preference #{preference_id}.") from None
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {"updated": True, "id": preference_id, "preference": updated["text"]}

    async def remove_preference(self, preference_id: int) -> dict[str, Any]:
        """Delete a stated preference by id (from ``get_taste``)."""
        if not await asyncio.to_thread(self.memory.remove_preference, int(preference_id)):
            raise ToolError(f"No preference #{preference_id}.")
        return {"removed": True, "id": preference_id}

    async def forget_feedback(self, feedback_id: int) -> dict[str, Any]:
        """Delete one piece of past feedback by id (from ``get_taste``)."""
        if not await asyncio.to_thread(self.memory.forget_feedback, int(feedback_id)):
            raise ToolError(f"No feedback #{feedback_id}.")
        return {"forgotten": True, "id": feedback_id}

    # ------------------------------------------------------------ scores
    async def adjust_score(self, kind: str, subject: str, change: float = 0, artist: str | None = None,
                           reset_learning: bool = False, note: str | None = None) -> dict[str, Any]:
        """Nudge the learned score of an artist or track, and/or reset what was learned about it.

        Args:
            kind: ``"artist"`` or ``"track"``.
            subject: The artist, or the track title.
            change: Added to its score (nudges add up; they never fade).
            artist: The artist, for ``kind="track"``.
            reset_learning: Ignore older feedback, skips and finished tracks for it.
            note: Why, in the person's words.

        Returns:
            The adjustment now in place, and the new score.
        """
        memory = self.memory
        try:
            adj = await asyncio.to_thread(memory.adjust, kind, subject, float(change or 0), artist,
                                          bool(reset_learning), note)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        scores = await asyncio.to_thread(memory.affinities, 10_000)
        entries = scores["artists" if kind == "artist" else "tracks"]
        wanted = subject if kind == "artist" else (f"{subject} - {artist}" if artist else subject)
        score = next((e["score"] for e in entries if norm(e["name"]) == norm(wanted)), 0.0)
        if adj is None:
            return {"adjustment": None, "note": "No adjustment left for it (back to what was learned).",
                    "score": score}
        return {"adjustment": adj["text"], "id": adj["id"], "score": score}

    async def remove_adjustment(self, adjustment_id: int) -> dict[str, Any]:
        """Delete a score adjustment (its nudge and any reset) by id from ``get_taste``."""
        if not await asyncio.to_thread(self.memory.remove_adjustment, int(adjustment_id)):
            raise ToolError(f"No adjustment #{adjustment_id}.")
        return {"removed": True, "id": adjustment_id}

    async def get_listening_history(self, days: int = 14, artist: str | None = None) -> dict[str, Any]:
        """What really happened lately: finished and skipped tracks, and what the DJ was asked for.

        Args:
            days: How far back, 1-90.
            artist: Only this artist.

        Returns:
            See ``Memory.listening_history``.
        """
        days = max(1, min(int(days or 14), 90))
        return await asyncio.to_thread(self.memory.listening_history, days, artist or None)

    # ------------------------------------------------------------ notebook
    async def write_note(self, text: str, topic: str | None = None, share_with_dj: bool = False) -> dict[str, Any]:
        """Write in the curator's notebook (it is read back at the start of every conversation).

        Args:
            text: The note, e.g. what was asked in a quiz and how they answered.
            topic: A short heading, e.g. ``"quiz: 70s soul"``.
            share_with_dj: The DJ sees it too, as one of the person's notes.

        Returns:
            The note's id.
        """
        try:
            nid = await asyncio.to_thread(self.memory.add_note, text, topic, "curator", bool(share_with_dj))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return {"saved": True, "note_id": nid, "shared_with_dj": bool(share_with_dj)}

    async def remove_note(self, note_id: int) -> dict[str, Any]:
        """Delete a notebook entry by id."""
        if not await asyncio.to_thread(self.memory.remove_note, int(note_id)):
            raise ToolError(f"No note #{note_id}.")
        return {"removed": True, "note_id": note_id}

    # ------------------------------------------------------------ playlists
    async def save_playlist(self, name: str, tracks: list[dict[str, str]], description: str | None = None,
                            mode: str = "replace") -> dict[str, Any]:
        """Create or update a saved playlist. Each song is looked up in the catalogue first.

        Args:
            name: Playlist name; a new one is created if it doesn't exist.
            tracks: ``{"title", "artist", "album"?}`` dicts, in order.
            description: When the playlist fits; the DJ reads this to decide
                whether to play it. Kept as it was if omitted.
            mode: ``"replace"`` the tracks, or ``"append"`` to them.

        Returns:
            How many tracks were saved, the playlist's size and length, and for
            each miss why plus the artist's real tracks to substitute.
        """
        store = self.playlists
        if mode not in ("replace", "append"):
            raise ToolError("mode must be replace or append")
        if not tracks:
            raise ToolError("tracks is empty")
        if len(tracks) > MAX_PLAYLIST_TRACKS_PER_CALL:
            raise ToolError(f"At most {MAX_PLAYLIST_TRACKS_PER_CALL} tracks per call; "
                            "send the rest with mode='append'.")
        items, not_found = await self.music.lookup_tracks(tracks)
        try:
            existing = await asyncio.to_thread(store.find, name)
            created = existing is None
            if created:
                if not items:
                    return {"saved": 0, "created": False, "not_found": not_found,
                            "note": "None of the songs were found, so no playlist was created."}
                pid = await asyncio.to_thread(store.create, name, description)
            else:
                pid = existing["id"]
                if description is not None:
                    await asyncio.to_thread(store.update, pid, None, description)
            # Nothing found leaves the playlist as it was, even in replace mode.
            added = await asyncio.to_thread(store.set_tracks, pid, items, mode) if items else 0
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        summary = next(p for p in await asyncio.to_thread(store.list) if p["id"] == pid)
        return {"playlist": summary["name"], "created": created, "saved": added,
                "total_tracks": summary["tracks"],
                "length": format_minutes(round(summary["duration_s"] / 60)) if summary["duration_s"] else "unknown",
                "not_found": not_found}

    async def get_playlist(self, name: str) -> dict[str, Any]:
        """Show a saved playlist's description and tracks."""
        p = await self.music.find_playlist(name)
        return {"name": p["name"], "description": p["description"] or "",
                "tracks": [f"{t['title']} - {t['artist'] or '?'}" for t in p["tracks"]]}

    async def delete_playlist(self, name: str) -> dict[str, Any]:
        """Delete a saved playlist."""
        p = await self.music.find_playlist(name)
        await asyncio.to_thread(self.playlists.delete, p["id"])
        return {"deleted": p["name"]}


def _note_dict(n: dict[str, Any]) -> dict[str, Any]:
    """A notebook entry as the curator sees it."""
    d = {"id": n["id"], "date": n["ts"][:10], "by": "person" if n["author"] == "you" else "curator",
         "text": n["text"]}
    if n["topic"]:
        d["topic"] = n["topic"]
    if n["for_dj"]:
        d["shared_with_dj"] = True
    return d


def _feedback_text(f: dict[str, Any]) -> str:
    """One feedback row in words, e.g. ``"dislike artist: Coldplay"``."""
    verb = {1: "like", -1: "dislike", 0: "note"}[f["sentiment"]]
    if f["scope"] == "track":
        text = f"{verb} track: {f['title']} - {f['artist'] or '?'}"
    elif f["scope"] == "artist":
        text = f"{verb} artist: {f['artist']}"
    else:
        text = f"{verb} (general)"
    return f"{text} - {f['note']}" if f.get("note") else text
