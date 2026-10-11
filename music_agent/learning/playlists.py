"""Saved playlists: named, described lists of tracks the DJ can play in one call.

The curator agent (or you, on the Playlists page) builds them; each track is
stored with its catalogue ref, so playing a playlist needs no searching. The
description says when it fits ("calm, mostly instrumental, for working") and is
what the DJ reads to decide whether a playlist suits a request.

Playlists live in the same SQLite file as the memory.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from music_agent.learning.database import Database
from music_agent.models import MediaItem
from music_agent.text import norm

SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE, description TEXT,
    created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS playlist_tracks (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,                -- 1-based order within the playlist
    ref TEXT,                                 -- catalogue ref; NULL means "look it up by title and artist"
    title TEXT NOT NULL, artist TEXT, album TEXT, duration_s INTEGER
);
CREATE INDEX IF NOT EXISTS playlist_tracks_order ON playlist_tracks(playlist_id, position);
"""

MAX_TRACKS = 200


class PlaylistStore:
    """Create, read, change and delete playlists.

    Args:
        db: The shared database (``Memory.db``).
        now_fn: Clock, replaceable in tests.
    """

    def __init__(self, db: Database, now_fn: Callable[[], datetime] = datetime.now):
        self.db = db
        self._now = now_fn
        db.add_schema(SCHEMA)

    def _stamp(self) -> str:
        """Current time as ISO text."""
        return self._now().isoformat(timespec="seconds")

    # ------------------------------------------------------------ reads
    def list(self) -> list[dict[str, Any]]:
        """All playlists, most recently changed first.

        Returns:
            ``{"id", "name", "description", "tracks", "duration_s", "updated"}`` each;
            ``tracks`` is the number of tracks.
        """
        rows = self.db.query(
            "SELECT p.id, p.name, p.description, p.updated, COUNT(t.id) AS tracks, "
            "COALESCE(SUM(t.duration_s), 0) AS duration_s "
            "FROM playlists p LEFT JOIN playlist_tracks t ON t.playlist_id = p.id "
            "GROUP BY p.id ORDER BY p.updated DESC, p.id DESC")
        return [dict(r) for r in rows]

    def get(self, playlist_id: int) -> dict[str, Any] | None:
        """One playlist with its tracks in order, or None.

        Returns:
            The playlist's fields plus ``tracks``: a list of
            ``{"id", "position", "ref", "title", "artist", "album", "duration_s"}``.
        """
        rows = self.db.query("SELECT * FROM playlists WHERE id = ?", (playlist_id,))
        if not rows:
            return None
        tracks = self.db.query("SELECT id, position, ref, title, artist, album, duration_s FROM playlist_tracks "
                               "WHERE playlist_id = ? ORDER BY position", (playlist_id,))
        return {**dict(rows[0]), "tracks": [dict(t) for t in tracks]}

    def find(self, key: str | int) -> dict[str, Any] | None:
        """Find a playlist by id or by name (ignoring case, accents and punctuation).

        Args:
            key: An id, or a name.

        Returns:
            Same as ``get``, or None.
        """
        if isinstance(key, int) or str(key).strip().isdigit():
            found = self.get(int(key))
            if found:
                return found
        wanted = norm(str(key))
        for p in self.list():
            if norm(p["name"]) == wanted:
                return self.get(p["id"])
        return None

    # ------------------------------------------------------------ writes
    def create(self, name: str, description: str | None = None) -> int:
        """Make an empty playlist.

        Args:
            name: Unique name (case is ignored when comparing).
            description: When it fits, e.g. "calm, instrumental, for working".

        Returns:
            The new playlist's id.

        Raises:
            ValueError: The name is empty or already used.
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("A playlist needs a name.")
        if self.find(name):
            raise ValueError(f"There is already a playlist called '{name}'.")
        now = self._stamp()
        return self.db.insert("playlists", {"name": name, "description": (description or "").strip() or None,
                                            "created": now, "updated": now})

    def update(self, playlist_id: int, name: str | None = None, description: str | None = None) -> bool:
        """Rename a playlist and/or change its description (None leaves a field as it is).

        Returns:
            True if the playlist exists.

        Raises:
            ValueError: The new name is empty or used by another playlist.
        """
        changes: dict[str, Any] = {"updated": self._stamp()}
        if name is not None:
            name = name.strip()
            other = self.find(name) if name else None
            if not name or (other and other["id"] != playlist_id):
                raise ValueError(f"Can't rename to '{name}': " + ("empty name." if not name else "name already used."))
            changes["name"] = name
        if description is not None:
            changes["description"] = description.strip() or None
        sets = ", ".join(f"{c} = ?" for c in changes)
        return self.db.execute(f"UPDATE playlists SET {sets} WHERE id = ?", [*changes.values(), playlist_id]) > 0

    def delete(self, playlist_id: int) -> bool:
        """Delete a playlist and its tracks.

        Returns:
            True if it existed.
        """
        return self.db.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,)) > 0

    def set_tracks(self, playlist_id: int, items: list[MediaItem], mode: str = "replace") -> int:
        """Replace a playlist's tracks, or add to the end. Tracks already in it are skipped.

        Args:
            playlist_id: Playlist to change.
            items: Tracks in order (usually found by ``MusicProvider.lookup_track``).
            mode: ``"replace"`` or ``"append"``.

        Returns:
            Number of tracks added.

        Raises:
            KeyError: No such playlist.
            ValueError: Bad mode, or the playlist would exceed ``MAX_TRACKS``.
        """
        if mode not in ("replace", "append"):
            raise ValueError("mode must be replace or append")
        if not self.get(playlist_id):
            raise KeyError(playlist_id)
        with self.db.transaction() as conn:
            if mode == "replace":
                conn.execute("DELETE FROM playlist_tracks WHERE playlist_id = ?", (playlist_id,))
            existing = conn.execute("SELECT ref, title, artist FROM playlist_tracks WHERE playlist_id = ?",
                                    (playlist_id,)).fetchall()
            seen = {_track_key(r["ref"], r["title"], r["artist"]) for r in existing}
            new = []
            for item in items:
                key = _track_key(item.ref, item.title, item.artist)
                if key not in seen:
                    seen.add(key)
                    new.append(item)
            if len(existing) + len(new) > MAX_TRACKS:
                raise ValueError(f"A playlist holds at most {MAX_TRACKS} tracks.")
            for n, item in enumerate(new, start=len(existing) + 1):
                conn.execute("INSERT INTO playlist_tracks (playlist_id, position, ref, title, artist, album, "
                             "duration_s) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (playlist_id, n, item.ref, item.title, item.artist, item.album, item.duration_s))
            conn.execute("UPDATE playlists SET updated = ? WHERE id = ?", (self._stamp(), playlist_id))
        return len(new)

    def remove_track(self, playlist_id: int, track_id: int) -> bool:
        """Remove one track (by its row id from ``get``) and close the gap in the order.

        Returns:
            True if it was removed.
        """
        with self.db.transaction() as conn:
            cur = conn.execute("DELETE FROM playlist_tracks WHERE id = ? AND playlist_id = ?",
                               (track_id, playlist_id))
            if not cur.rowcount:
                return False
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM playlist_tracks WHERE playlist_id = ? ORDER BY position", (playlist_id,))]
            for n, tid in enumerate(ids, start=1):
                conn.execute("UPDATE playlist_tracks SET position = ? WHERE id = ?", (n, tid))
            conn.execute("UPDATE playlists SET updated = ? WHERE id = ?", (self._stamp(), playlist_id))
        return True


def track_item(track: dict[str, Any]) -> MediaItem | None:
    """Turn a stored track back into a playable ``MediaItem``.

    Args:
        track: A track dict from ``PlaylistStore.get``.

    Returns:
        The item, or None if the track has no usable ref (it must be looked up).
    """
    try:
        provider, kind, item_id = MediaItem.split_ref(track["ref"] or "")
    except ValueError:
        return None
    return MediaItem(provider, kind, item_id, track["title"], track.get("artist"), track.get("album"),
                     track.get("duration_s"))


def _track_key(ref: str | None, title: str, artist: str | None) -> str:
    """What makes two playlist entries "the same track"."""
    return ref or f"{norm(title)}|{norm(artist)}"
