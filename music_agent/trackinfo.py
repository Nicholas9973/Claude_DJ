"""Track qualities from public music databases: year, tempo, style tags, popularity.

The agents never go online themselves. When the curator asks for a track's
qualities (``look_up_tracks``), this code queries two fixed, key-free services
with just the title and artist, and caches what it finds in ``track_info``:

- **MusicBrainz** (open music encyclopedia): the original year (the earliest
  release of the recording) and style/mood tags, for the recording and for the
  artist. Its rules ask for at most one request per second and a User-Agent
  that says who is asking; both are respected here.
- **Deezer** (public search API): popularity (``rank``, turned into "hit",
  "well known", "known" or "deep cut") and tempo. Deezer's tempo is
  approximate: often missing, sometimes doubled, so values outside a
  plausible range are halved or left out.

Last.fm (more mood tags) needs an API key and isn't used yet.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from music_agent.learning.database import Database
from music_agent.text import core_title, norm

log = logging.getLogger(__name__)

USER_AGENT = "ClaudeDJ/0.1 ( https://github.com/Nicholas9973/Claude_DJ )"
MUSICBRAINZ = "https://musicbrainz.org/ws/2"
DEEZER = "https://api.deezer.com"
MB_INTERVAL_S = 1.1                 # MusicBrainz allows one request per second
MAX_AGE_DAYS = {"found": 180, "not_found": 30, "error": 1}   # when a cached answer is looked up again
POPULARITY = (("hit", 700_000), ("well known", 400_000), ("known", 150_000), ("deep cut", 0))  # Deezer rank
POPULARITIES = tuple(name for name, _ in POPULARITY)

SCHEMA = """
CREATE TABLE IF NOT EXISTS track_info (       -- qualities looked up in public music databases (a cache)
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,                 -- song title without version labels | artist, normalised
    title TEXT NOT NULL, artist TEXT,
    status TEXT NOT NULL CHECK (status IN ('found', 'not_found', 'error')),
    year INTEGER,                             -- of the original release
    tempo_bpm INTEGER,                        -- approximate
    popularity TEXT CHECK (popularity IN ('hit', 'well known', 'known', 'deep cut')),
    deezer_rank INTEGER,
    tags TEXT,                                -- this recording's style / mood tags, comma-separated
    artist_genres TEXT,                       -- the artist's genres, comma-separated
    sources TEXT,                             -- which services answered
    error TEXT
);
"""

Fetch = Callable[[str, dict[str, Any]], Any]   # (url, params) -> decoded JSON


def _get_json(url: str, params: dict[str, Any], timeout: float = 6.0) -> Any:
    """GET a URL with query parameters (and our User-Agent) and decode the JSON response."""
    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}",
                                     headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def popularity_band(rank: int | None) -> str | None:
    """Turn a Deezer rank (0 to about 1,000,000) into words, e.g. ``"hit"``; None if unknown."""
    if not rank:
        return None
    return next(name for name, floor in POPULARITY if rank >= floor)


def plausible_tempo(bpm: float | None) -> int | None:
    """Deezer's tempo, corrected for the usual doubling; None when missing or implausible."""
    if not bpm:
        return None
    if bpm > 190:
        bpm /= 2
    return round(bpm) if 50 <= bpm <= 190 else None


class TrackInfoService:
    """Looks up and caches track qualities. Never raises on network trouble: the answer says what failed.

    Args:
        db: The shared SQLite file; the ``track_info`` table is added to it.
        fetch: HTTP JSON getter, replaceable in tests.
        sleep: Waits between MusicBrainz requests, replaceable in tests.
        now_fn: Clock, replaceable in tests.
    """

    def __init__(self, db: Database, fetch: Fetch = _get_json, sleep: Callable[[float], None] = time.sleep,
                 now_fn: Callable[[], datetime] = datetime.now):
        self.db = db
        self._fetch = fetch
        self._sleep = sleep
        self._now = now_fn
        self._mb_lock = threading.Lock()
        self._mb_last = 0.0
        db.add_schema(SCHEMA)
        db.add_check("track_info", _check_row)

    # ------------------------------------------------------------ cache
    def cached(self, title: str, artist: str | None) -> dict[str, Any] | None:
        """What is known about a track without going online, or None."""
        rows = self.db.query("SELECT * FROM track_info WHERE key = ?", (track_info_key(title, artist),))
        return describe(rows[0]) if rows else None

    def popularity(self, title: str, artist: str | None) -> str | None:
        """The cached popularity band of a track, if known."""
        info = self.cached(title, artist)
        return info.get("popularity") if info else None

    # ------------------------------------------------------------ lookups
    def look_up(self, title: str, artist: str | None, refresh: bool = False) -> dict[str, Any]:
        """Return a track's qualities, from the cache when fresh enough, else from the services.

        Args:
            title: Track title.
            artist: Artist (strongly recommended: titles alone are ambiguous).
            refresh: Ignore the cache.

        Returns:
            ``title``, ``artist``, ``status`` ("found", "not_found" or
            "error"), and when found some of ``year``, ``tempo_bpm``,
            ``popularity``, ``tags``, ``artist_genres``, ``sources``.
        """
        key = track_info_key(title, artist)
        rows = self.db.query("SELECT * FROM track_info WHERE key = ?", (key,))
        if rows and not refresh and not self._stale(rows[0]):
            return describe(rows[0])

        row: dict[str, Any] = {"key": key, "title": title.strip(), "artist": (artist or "").strip() or None}
        errors, sources = [], []
        try:
            mb = self._musicbrainz(title, artist)
            if mb:
                row.update(mb)
                sources.append("MusicBrainz")
        except Exception as exc:   # network down, rate limit, API change: report it, keep going
            errors.append(f"MusicBrainz: {exc}")
        try:
            dz = self._deezer(title, artist)
            if dz:
                row.update(dz)
                sources.append("Deezer")
        except Exception as exc:
            errors.append(f"Deezer: {exc}")
        row["status"] = "found" if sources else "error" if errors else "not_found"
        row["sources"] = ", ".join(sources) or None
        row["error"] = "; ".join(errors) or None
        row["ts"] = self._now().isoformat(timespec="seconds")
        if errors:
            log.info("Track info for %s - %s partly failed: %s", title, artist, row["error"])
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM track_info WHERE key = ?", (key,))
            conn.execute(f"INSERT INTO track_info ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})",
                         tuple(row.values()))
        return describe(row)

    def _stale(self, row: Any) -> bool:
        """True when a cached answer is old enough to ask again."""
        try:
            age = self._now() - datetime.fromisoformat(row["ts"])
        except (TypeError, ValueError):
            return True
        return age > timedelta(days=MAX_AGE_DAYS.get(row["status"], 1))

    def _mb_get(self, path: str, params: dict[str, Any]) -> Any:
        """One MusicBrainz request, at most one per ``MB_INTERVAL_S`` across threads."""
        with self._mb_lock:
            wait = self._mb_last + MB_INTERVAL_S - time.monotonic()
            if wait > 0:
                self._sleep(wait)
            try:
                return self._fetch(f"{MUSICBRAINZ}/{path}", {**params, "fmt": "json"})
            finally:
                self._mb_last = time.monotonic()

    def _musicbrainz(self, title: str, artist: str | None) -> dict[str, Any] | None:
        """Original year and tags from the recordings that match, plus the artist's genres."""
        query = f'recording:"{_lucene(core_title_text(title))}"'
        if artist:
            query += f' AND artist:"{_lucene(artist)}"'
        data = self._mb_get("recording", {"query": query, "limit": 25})
        matches = [r for r in data.get("recordings", [])
                   if r.get("score", 0) >= 85 and core_title(r.get("title")) == core_title(title)
                   and (not artist or any(_same_artist(c.get("name"), artist) for c in r.get("artist-credit", [])))]
        if not matches:
            return None
        years = [int(r["first-release-date"][:4]) for r in matches
                 if re.match(r"^\d{4}", r.get("first-release-date") or "")]
        tags: dict[str, int] = {}
        for r in matches:
            for tag in r.get("tags", []):
                if _useful_tag(tag.get("name")):
                    tags[tag["name"]] = tags.get(tag["name"], 0) + int(tag.get("count", 1))
        out: dict[str, Any] = {"year": min(years) if years else None,
                               "tags": ", ".join(sorted(tags, key=lambda t: -tags[t])[:8]) or None}
        artist_id = next((c["artist"]["id"] for r in matches for c in r.get("artist-credit", [])
                          if c.get("artist", {}).get("id") and (not artist or _same_artist(c.get("name"), artist))),
                         None)
        if artist_id:
            a = self._mb_get(f"artist/{artist_id}", {"inc": "genres"})
            genres = sorted(a.get("genres", []), key=lambda g: -int(g.get("count", 0)))
            out["artist_genres"] = ", ".join(g["name"] for g in genres[:6] if _useful_tag(g.get("name"))) or None
        return out

    def _deezer(self, title: str, artist: str | None) -> dict[str, Any] | None:
        """Popularity and tempo of the most popular Deezer track that matches."""
        data = self._fetch(f"{DEEZER}/search", {"q": f"{artist or ''} {core_title_text(title)}".strip(), "limit": 15})
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(data["error"].get("message", "error"))
        matches = [d for d in data.get("data", [])
                   if core_title(d.get("title")) == core_title(title)
                   and (not artist or _same_artist(d.get("artist", {}).get("name"), artist))]
        if not matches:
            return None
        best = max(matches, key=lambda d: d.get("rank") or 0)
        track = self._fetch(f"{DEEZER}/track/{int(best['id'])}", {})
        rank = int(best.get("rank") or 0) or None
        return {"deezer_rank": rank, "popularity": popularity_band(rank),
                "tempo_bpm": plausible_tempo(track.get("bpm") if isinstance(track, dict) else None)}


def track_info_key(title: str | None, artist: str | None) -> str:
    """What makes two lookups the same song: title without version labels, and artist."""
    return f"{core_title(title)}|{norm(artist)}"


def core_title_text(title: str) -> str:
    """The title without bracketed version labels, for a search (keeps the original spelling)."""
    return re.sub(r"\s*[\(\[].*?[\)\]]", "", title).strip() or title


def describe(row: Any) -> dict[str, Any]:
    """A ``track_info`` row as the curator and the CLI see it (empty fields left out)."""
    r = dict(row)
    out = {"title": r["title"], "artist": r["artist"], "status": r["status"], "year": r.get("year"),
           "tempo_bpm": r.get("tempo_bpm"), "popularity": r.get("popularity"),
           "tags": _split(r.get("tags")), "artist_genres": _split(r.get("artist_genres")),
           "sources": r.get("sources"), "looked_up": str(r.get("ts") or "")[:10] or None}
    if r.get("tempo_bpm"):
        out["tempo_note"] = "approximate (Deezer)"
    if r.get("error"):
        out["error"] = r["error"]
    return {k: v for k, v in out.items() if v not in (None, [], "")}


def _split(text: str | None) -> list[str]:
    """``"soul, funk"`` -> ``["soul", "funk"]``."""
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def _same_artist(name: str | None, wanted: str) -> bool:
    """Loose artist match: one name contains the other ("Stevie Wonder" / "Wonder, Stevie" aside)."""
    a, b = norm(name), norm(wanted)
    return bool(a and b) and (a in b or b in a)


def _lucene(text: str) -> str:
    """Make text safe inside a quoted MusicBrainz (Lucene) search phrase."""
    return re.sub(r'[\\"]', " ", text).strip()


def _useful_tag(name: str | None) -> bool:
    """Leave out tags that aren't about the music (games, lists, very long ones)."""
    return bool(name) and len(name) <= 30 and "/" not in name and not name.isdigit()


def _check_row(r: dict[str, Any]) -> None:
    """Rules for hand edits of ``track_info`` on the GUI's Database page."""
    if r["year"] is not None and not 1800 <= int(r["year"]) <= 2100:
        raise ValueError("year must be between 1800 and 2100")
    if r["tempo_bpm"] is not None and not 30 <= int(r["tempo_bpm"]) <= 300:
        raise ValueError("tempo must be between 30 and 300 bpm")
