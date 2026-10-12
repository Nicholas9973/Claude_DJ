"""Long-term memory: what was asked, what was played, how it went, what you said.

Everything lives in one SQLite file (data/music.db by default). The agent never
sees the raw tables; it gets a compact *taste profile* computed from them, so
you can always inspect (or change) what shapes its choices.

What shapes the profile, from strongest to most subtle:

- *Preferences* are things you state outright, usually via the curator or the
  Taste page ("never Coldplay", "bossa nova in the evening"). They never fade
  and appear in the profile in your own words. A love can say *how often*
  (``frequency``: regular, sometimes, rarely, or only when asked) and *when*
  (``context``, e.g. "at the gym"): "I love The Hu, but rarely" keeps them out
  of the liked lists without a negative nudge.
- *Adjustments* are nudges you give a score by hand ("+2 for Nina Simone"), and
  can also *reset what was learned* about an artist or track (older feedback
  and listens are then ignored, but kept in the history). They never fade.
- *Feedback* is a reaction in the moment ("love this track"). It fades.
  Reactions during a curator's quiz count less (see ``scoring.py``).
- *Listens* are what the watcher saw: tracks finished or skipped. They fade.
  Listens to quiz samples and to music played for other people (a party,
  guests: ``audience = 'others'``, or *guest mode*) are kept in the history
  but don't shape the taste.
- The *notebook* holds notes by the curator (what it asked in quizzes, what it
  noticed) and by you. The curator reads them all; notes marked ``for_dj``
  reach the DJ too, alongside the notes in your feedback.
- *Findings* are conclusions the curator draws from quizzes ("dislikes K-pop"),
  each with its evidence: the samples played and how you reacted. A finding
  stays *tentative* (a soft hint for the DJ) until the evidence is enough:
  several samples, from several artists, not all famous hits, mostly agreeing,
  and for a dislike at least one *salvage* try (a sample from another side of
  it). The same bar applies to every genre; ``check_finding`` says what's missing.

How these add up is in ``scoring.py``; all the weights are tunable
(``taste_settings`` table, Taste page).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from music_agent.learning.database import Database
from music_agent.learning.scoring import TUNING, Entry, Scores, check_tuning, fade, track_key
from music_agent.models import PARTS_OF_DAY, MediaItem, part_of_day
from music_agent.text import core_title, norm

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, prompt TEXT, reply TEXT, model TEXT,
    cost_usd REAL, part_of_day TEXT, weekday TEXT, weather TEXT
);
CREATE TABLE IF NOT EXISTS plays (            -- tracks the agents (or the GUI) queued
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, request_id INTEGER, ref TEXT,
    title TEXT, artist TEXT, album TEXT, part_of_day TEXT, weekday TEXT
);
CREATE TABLE IF NOT EXISTS listens (          -- what the watcher saw happen
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, title TEXT, artist TEXT,
    outcome TEXT CHECK (outcome IN ('completed', 'skipped')),
    listened_s INTEGER, duration_s INTEGER, part_of_day TEXT, weekday TEXT
);
CREATE TABLE IF NOT EXISTS feedback (         -- what you said
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, sentiment INTEGER CHECK (sentiment IN (-1, 0, 1)),
    scope TEXT CHECK (scope IN ('track', 'artist', 'general')),
    title TEXT, artist TEXT, note TEXT, part_of_day TEXT, weekday TEXT, request_id INTEGER
);
CREATE TABLE IF NOT EXISTS preferences (      -- what you stated outright; never fades
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('artist', 'track', 'genre', 'rule')),
    sentiment INTEGER NOT NULL CHECK (sentiment IN (-1, 0, 1)),
    subject TEXT NOT NULL,                    -- artist, track title, genre or the rule itself
    artist TEXT,                              -- for kind = 'track'
    part_of_day TEXT CHECK (part_of_day IN ('morning', 'afternoon', 'evening', 'night')),  -- NULL = any time
    note TEXT
);
CREATE TABLE IF NOT EXISTS adjustments (      -- hand-made changes to a score; never fade
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('artist', 'track')),
    subject TEXT NOT NULL,                    -- artist, or track title
    artist TEXT,                              -- for kind = 'track'
    delta REAL NOT NULL DEFAULT 0,            -- added to the score
    ignore_before TEXT,                       -- feedback and listens up to this time are ignored
    note TEXT
);
CREATE TABLE IF NOT EXISTS notebook (         -- notes by the curator and you; the curator reads them all
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
    author TEXT NOT NULL CHECK (author IN ('curator', 'you')),
    topic TEXT,                               -- e.g. "quiz: 70s soul"
    text TEXT NOT NULL,
    for_dj INTEGER NOT NULL DEFAULT 0 CHECK (for_dj IN (0, 1))   -- 1: the DJ sees it too, with your notes
);
CREATE TABLE IF NOT EXISTS findings (         -- conclusions the curator draws from quizzes
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL, updated TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('genre', 'artist', 'era', 'mood', 'other')),
    subject TEXT NOT NULL,                    -- e.g. "K-pop", "70s soul", "long ambient tracks"
    sentiment INTEGER NOT NULL CHECK (sentiment IN (-1, 0, 1)),   -- likes it / mixed / dislikes it
    statement TEXT NOT NULL,                  -- the conclusion in words, as the DJ reads it
    status TEXT NOT NULL DEFAULT 'tentative' CHECK (status IN ('tentative', 'confirmed', 'dropped')),
    note TEXT
);
CREATE TABLE IF NOT EXISTS finding_evidence ( -- the samples behind a finding, and the reaction to each
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL,
    finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    title TEXT, artist TEXT NOT NULL,
    reaction INTEGER NOT NULL CHECK (reaction IN (-1, 0, 1)),
    salvage INTEGER NOT NULL DEFAULT 0 CHECK (salvage IN (0, 1)),   -- 1: a try from another side, a fair chance
    popularity TEXT CHECK (popularity IN ('hit', 'well known', 'known', 'deep cut')),   -- when known
    note TEXT
);
CREATE TABLE IF NOT EXISTS taste_settings (   -- your values for the tunables in scoring.TUNING
    id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, value REAL NOT NULL, ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS plays_ts ON plays(ts);
CREATE INDEX IF NOT EXISTS listens_ts ON listens(ts);
CREATE INDEX IF NOT EXISTS feedback_ts ON feedback(ts);
"""

PREFERENCE_KINDS = ("artist", "track", "genre", "rule")
FINDING_KINDS = ("genre", "artist", "era", "mood", "other")
FINDING_STATUSES = ("tentative", "confirmed", "dropped")
ADJUSTMENT_KINDS = ("artist", "track")
AUDIENCES = ("me", "others")          # who music was played for; "others" doesn't train the taste
FREQUENCIES = ("regular", "sometimes", "rarely", "on_request")   # how often a love should come up
FREQUENCY_WORDS = {                   # how the profile words it for Claude (wording matters: see CLAUDE.md)
    "sometimes": "sometimes: now and then, not a staple",
    "rarely": "rarely: an occasional surprise at most, never a regular",
    "on_request": "only when asked for, or when the moment clearly calls for it",
}
MAX_DELTA = 50.0
LISTEN_MATCH_HOURS = 12               # a listen belongs to the latest play of that title within this window


class Memory:
    """Store of requests, plays, listens, feedback, preferences and adjustments, plus the taste profile.

    Thread-safe: tools and the watcher write from worker threads.

    Args:
        path: Database file, or ``":memory:"`` for tests.
        half_life_days: Default for how fast feedback fades (the Taste page can override it).
        now_fn: Clock, replaceable in tests.

    Attributes:
        db: The SQLite file; ``PlaylistStore`` and the GUI's database page share it.
        queue_audience: Who the current queue was started for (``"me"`` or
            ``"others"``); set whenever an agent or the GUI replaces the queue,
            so listens to tracks it didn't record (a whole album) still count right.
    """

    def __init__(self, path: str | Path, half_life_days: float = 60.0,
                 now_fn: Callable[[], datetime] = datetime.now):
        self.path = Path(path)
        self.half_life_days = half_life_days
        self._now = now_fn
        self.queue_audience = "me"
        self._guest_until: datetime | None = None
        self.db = Database(path)
        self.db.add_schema(SCHEMA)
        # Small migrations for files made by older versions.
        self.db.ensure_column("requests", "agent", "TEXT")   # added with the curator
        self.db.ensure_column("plays", "source", "TEXT")     # "dj", "curator" or "gui"
        audience = "TEXT CHECK (audience IN ('me', 'others'))"
        self.db.ensure_column("plays", "audience", audience)
        self.db.ensure_column("listens", "source", "TEXT")   # copied from the play it belongs to
        self.db.ensure_column("listens", "audience", audience)
        self.db.ensure_column("feedback", "source", "TEXT")  # "dj", "curator" (a quiz) or "gui"
        self.db.ensure_column("preferences", "frequency",
                              "TEXT CHECK (frequency IN ('regular', 'sometimes', 'rarely', 'on_request'))")
        self.db.ensure_column("preferences", "context", "TEXT")   # when it applies, in your words
        self._match_old_listens()
        # Edits made on the GUI's Database page must follow the same rules as these methods.
        self.db.add_check("feedback", lambda r: _check_feedback(r["sentiment"], r["scope"], r["title"],
                                                                 r["artist"], r["note"]))
        self.db.add_check("preferences", lambda r: _check_preference(
            r["kind"], r["sentiment"], r["subject"], r["artist"], r["part_of_day"], r["note"],
            r["frequency"], r["context"]))
        self.db.add_check("adjustments", lambda r: _check_adjustment(r["kind"], r["subject"], r["delta"],
                                                                      r["ignore_before"]))
        self.db.add_check("taste_settings", lambda r: check_tuning(r["key"], r["value"]))
        self.db.add_check("notebook", lambda r: _check_note(r["author"], r["text"]))
        self.db.add_check("findings", self._check_finding_row)
        self.db.add_check("finding_evidence", lambda r: _check_evidence(r["artist"]))

    # ------------------------------------------------------------ helpers
    def _stamp(self) -> dict[str, str]:
        """Timestamp, part of day and weekday columns for a new row."""
        now = self._now()
        return {"ts": now.isoformat(timespec="seconds"), "part_of_day": part_of_day(now.hour),
                "weekday": now.strftime("%A")}

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        """Insert one row; return its id."""
        return self.db.insert(table, row)

    def _query(self, sql: str, params: Iterable = ()) -> list[Any]:
        """Run a SELECT and return all rows."""
        return self.db.query(sql, params)

    def _weight(self, ts: str, half_life_days: float, now: datetime) -> float:
        """Fade factor for a signal recorded at ``ts``: 1.0 now, 0.5 after one half-life."""
        try:
            age_days = (now - datetime.fromisoformat(ts)).total_seconds() / 86400
        except (TypeError, ValueError):  # a hand-edited time that isn't one: count it as new, don't crash
            log.warning("Ignoring unreadable time %r in memory", ts)
            return 1.0
        return fade(age_days, half_life_days)

    def _play_for(self, title: str | None, ts: str) -> dict[str, Any] | None:
        """The latest play of this title in the hours up to ``ts``: who queued it, and for whom."""
        try:
            until = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            return None
        since = (until - timedelta(hours=LISTEN_MATCH_HOURS)).isoformat(timespec="seconds")
        rows = self._query("SELECT p.title, p.source, p.audience, r.agent FROM plays p "
                           "LEFT JOIN requests r ON r.id = p.request_id "
                           "WHERE p.ts BETWEEN ? AND ? ORDER BY p.ts DESC, p.id DESC", (since, ts))
        for r in rows:   # titles compared loosely: the speaker and the catalogue may spell them differently
            if norm(r["title"]) == norm(title):
                return {"source": r["source"] or r["agent"], "audience": r["audience"]}
        return None

    def _match_old_listens(self) -> None:
        """Fill in who queued each listen, and for whom, for listens recorded before that was kept.

        Listens with no matching play (music started from the Sonos app) stay
        empty, which counts as your own listening.
        """
        for r in self._query("SELECT id, ts, title FROM listens WHERE source IS NULL AND audience IS NULL"):
            play = self._play_for(r["title"], r["ts"])
            if play:
                self.db.execute("UPDATE listens SET source = ?, audience = ? WHERE id = ?",
                                (play["source"], play["audience"] or "me", r["id"]))

    def close(self) -> None:
        """Close the database."""
        self.db.close()

    # ------------------------------------------------------------ guest mode
    def set_guest_mode(self, hours: float | None) -> datetime | None:
        """Turn guest mode on for a while, or off. Music played meanwhile doesn't train your taste.

        It lasts until the time runs out or the program restarts.

        Args:
            hours: How long, up to 24; None or 0 turns it off.

        Returns:
            When it ends, or None if it is off.

        Raises:
            ValueError: More than 24 hours, or not a number.
        """
        if not hours:
            self._guest_until = None
            return None
        hours = float(hours)
        if not 0 < hours <= 24:
            raise ValueError("guest mode lasts between a few minutes and 24 hours")
        self._guest_until = self._now() + timedelta(hours=hours)
        return self._guest_until

    def guest_until(self) -> datetime | None:
        """When guest mode ends, or None if it is off."""
        if self._guest_until and self._now() >= self._guest_until:
            self._guest_until = None
        return self._guest_until

    def _audience(self, wanted: str | None) -> str:
        """Who music is for: ``"others"`` in guest mode, else ``wanted`` (default ``"me"``)."""
        if self.guest_until():
            return "others"
        if wanted not in (None, *AUDIENCES):
            raise ValueError(f"audience must be one of {', '.join(AUDIENCES)}")
        return wanted or "me"

    # ------------------------------------------------------------ writes
    def start_request(self, prompt: str, model: str | None = None, weather: str | None = None,
                      agent: str | None = None) -> int:
        """Record an incoming request.

        Args:
            prompt: The person's message.
            model: Claude model handling it.
            weather: Weather summary at the time, if known.
            agent: Which agent handled it, ``"dj"`` or ``"curator"``.

        Returns:
            The request id, to link plays and feedback to it.
        """
        return self._insert("requests", {**self._stamp(), "prompt": prompt, "model": model,
                                         "weather": weather, "agent": agent})

    def finish_request(self, request_id: int, reply: str, cost_usd: float | None) -> None:
        """Store the reply and credit used for a request.

        Args:
            request_id: Id from ``start_request``.
            reply: What Claude answered.
            cost_usd: Credit used, if known.
        """
        self.db.execute("UPDATE requests SET reply = ?, cost_usd = ? WHERE id = ?", (reply, cost_usd, request_id))

    def record_plays(self, items: list[MediaItem], request_id: int | None = None, source: str | None = None,
                     audience: str | None = None) -> None:
        """Record queued tracks.

        Args:
            items: Queued tracks.
            request_id: Request that queued them.
            source: Who queued them: ``"dj"``, ``"curator"`` (quiz samples,
                which the DJ may repeat) or ``"gui"``.
            audience: ``"me"`` (default) or ``"others"`` (guests, a party);
                always ``"others"`` in guest mode.

        Raises:
            ValueError: Unknown audience.
        """
        stamp = self._stamp()
        audience = self._audience(audience)
        for item in items:
            self._insert("plays", {**stamp, "request_id": request_id, "ref": item.ref, "title": item.title,
                                   "artist": item.artist, "album": item.album, "source": source,
                                   "audience": audience})

    def record_listen(self, title: str, artist: str | None, outcome: str,
                      listened_s: int | None = None, duration_s: int | None = None) -> int:
        """Record what the watcher saw happen to a track.

        The listen is linked to the play it came from (if an agent or the GUI
        queued it), so quiz samples and music for others can be left out of
        the taste.

        Args:
            title: Track title.
            artist: Track artist.
            outcome: ``"completed"`` or ``"skipped"``.
            listened_s: Furthest position reached, in seconds.
            duration_s: Track length, in seconds.

        Returns:
            The new row id.
        """
        stamp = self._stamp()
        play = self._play_for(title, stamp["ts"])
        if self.guest_until() or (play["audience"] if play else self.queue_audience) == "others":
            audience = "others"
        else:
            audience = "me"
        return self._insert("listens", {**stamp, "title": title, "artist": artist, "outcome": outcome,
                                        "listened_s": listened_s, "duration_s": duration_s,
                                        "source": play["source"] if play else None, "audience": audience})

    def record_feedback(self, sentiment: int, scope: str, title: str | None = None,
                        artist: str | None = None, note: str | None = None,
                        request_id: int | None = None, source: str | None = None) -> int:
        """Record what the person said about a track, an artist or music in general.

        Args:
            sentiment: 1 (like), -1 (dislike) or 0 (neutral note).
            scope: ``"track"``, ``"artist"`` or ``"general"``.
            title: Track title; required for ``"track"``.
            artist: Artist; required for ``"artist"``.
            note: The person's own words; required for ``"general"``.
            request_id: Request it came from.
            source: Where it was given: ``"dj"``, ``"curator"`` (a quiz
                answer, which counts less) or ``"gui"``.

        Returns:
            The feedback id (``cli forget <id>`` deletes it).

        Raises:
            ValueError: Bad sentiment or scope, or a required field is missing.
        """
        _check_feedback(sentiment, scope, title, artist, note)
        return self._insert("feedback", {**self._stamp(), "sentiment": sentiment, "scope": scope,
                                         "title": title, "artist": artist, "note": note,
                                         "request_id": request_id, "source": source})

    def update_feedback(self, feedback_id: int, **changes: Any) -> dict[str, Any]:
        """Change some fields of one feedback entry (same rules as ``record_feedback``).

        Args:
            feedback_id: Id from ``recent_feedback()``.
            **changes: ``sentiment``, ``scope``, ``title``, ``artist`` and/or ``note``.

        Returns:
            The entry after the change.

        Raises:
            KeyError: No such entry.
            ValueError: An unknown field, or the result would be invalid.
        """
        bad = set(changes) - {"sentiment", "scope", "title", "artist", "note"}
        if bad:
            raise ValueError(f"Can't change {', '.join(sorted(bad))} of feedback.")
        return self.db.update_row("feedback", feedback_id, changes)

    def forget_feedback(self, feedback_id: int) -> bool:
        """Delete one feedback entry.

        Args:
            feedback_id: Id shown by ``cli feedback``.

        Returns:
            True if an entry was deleted.
        """
        return self.db.execute("DELETE FROM feedback WHERE id = ?", (feedback_id,)) > 0

    # ------------------------------------------------------------ preferences
    def add_preference(self, kind: str, sentiment: int, subject: str, artist: str | None = None,
                       part: str | None = None, note: str | None = None, frequency: str | None = None,
                       context: str | None = None) -> int:
        """Record something the person stated outright. It never fades.

        Args:
            kind: ``"artist"``, ``"track"``, ``"genre"`` or ``"rule"``.
            sentiment: 1 (love), -1 (avoid) or 0 (neither, e.g. a rule).
            subject: The artist, track title, genre, or the rule in a few words.
            artist: The track's artist, for ``kind="track"``.
            part: ``"morning"``, ``"afternoon"``, ``"evening"`` or ``"night"``
                if it only applies then; None for any time.
            note: Extra nuance in the person's words.
            frequency: For a love or a note: ``"regular"`` (default),
                ``"sometimes"``, ``"rarely"`` or ``"on_request"``. Anything but
                regular keeps it out of the liked lists the DJ leans on.
            context: When it applies, in a few words (e.g. ``"at the gym"``).

        Returns:
            The preference id.

        Raises:
            ValueError: A field is missing or not one of the allowed values.
        """
        row = _check_preference(kind, sentiment, subject, artist, part, note, frequency, context)
        return self._insert("preferences", {"ts": self._stamp()["ts"], **row})

    def update_preference(self, pref_id: int, **changes: Any) -> dict[str, Any]:
        """Change some fields of a preference (same fields and rules as ``add_preference``).

        Args:
            pref_id: Id from ``preferences()``.
            **changes: ``kind``, ``sentiment``, ``subject``, ``artist``, ``part``, ``note``,
                ``frequency`` and/or ``context``.

        Returns:
            The preference after the change, with its ``text`` summary.

        Raises:
            KeyError: No such preference.
            ValueError: The result would be invalid.
        """
        rows = self._query("SELECT * FROM preferences WHERE id = ?", (pref_id,))
        if not rows:
            raise KeyError(pref_id)
        current = dict(rows[0])
        current["part"] = current.pop("part_of_day")
        merged = {k: changes.get(k, current[k])
                  for k in ("kind", "sentiment", "subject", "artist", "part", "note", "frequency", "context")}
        row = _check_preference(**merged)
        self.db.execute(f"UPDATE preferences SET {', '.join(f'{c} = ?' for c in row)} WHERE id = ?",
                        [*row.values(), pref_id])
        updated = self._query("SELECT * FROM preferences WHERE id = ?", (pref_id,))[0]
        return {**dict(updated), "text": describe_preference(updated)}

    def remove_preference(self, pref_id: int) -> bool:
        """Delete one preference.

        Args:
            pref_id: Id from ``preferences()``.

        Returns:
            True if one was deleted.
        """
        return self.db.execute("DELETE FROM preferences WHERE id = ?", (pref_id,)) > 0

    def preferences(self) -> list[dict[str, Any]]:
        """All stated preferences, newest first, each with a ``text`` summary."""
        return [{**dict(r), "text": describe_preference(r)}
                for r in self._query("SELECT * FROM preferences ORDER BY id DESC")]

    # ------------------------------------------------------------ adjustments
    def adjust(self, kind: str, subject: str, delta: float = 0.0, artist: str | None = None,
               reset_learning: bool = False, note: str | None = None) -> dict[str, Any] | None:
        """Nudge an artist's or track's score by hand, and/or reset what was learned about it.

        There is one adjustment per artist or track: nudging again adds to it.
        Nudges never fade. Resetting makes the scoring ignore older feedback
        and listens for it (they stay in the history; ``update_adjustment``
        with ``ignore_before=None`` undoes the reset).

        Args:
            kind: ``"artist"`` or ``"track"``.
            subject: The artist, or the track title.
            delta: Added to the score, e.g. ``+2`` or ``-1``.
            artist: The track's artist, for ``kind="track"``.
            reset_learning: Ignore feedback and listens from before now.
            note: Why, in your words (kept if omitted).

        Returns:
            The adjustment with its ``text``, or None if it ended up empty
            (no nudge, no reset) and was removed.

        Raises:
            ValueError: Bad kind, empty subject, or a nudge out of range.
        """
        artist = ((artist or "").strip() or None) if kind == "track" else None
        _check_adjustment(kind, subject, delta, None)
        existing = self._find_adjustment(kind, subject, artist)
        now = self._stamp()["ts"]
        if existing is None:
            if not delta and not reset_learning:
                return None
            row = {"ts": now, "kind": kind, "subject": subject.strip(), "artist": artist,
                   "delta": float(delta), "ignore_before": now if reset_learning else None,
                   "note": (note or "").strip() or None}
            return self.adjustment(self._insert("adjustments", row))
        total = round(existing["delta"] + float(delta), 4)
        _check_adjustment(kind, subject, total, None)
        ignore_before = now if reset_learning else existing["ignore_before"]
        if not total and not ignore_before:
            self.remove_adjustment(existing["id"])
            return None
        changes = {"delta": total, "ignore_before": ignore_before, "ts": now}
        if note is not None:
            changes["note"] = note.strip() or None
        self.db.update_row("adjustments", existing["id"], changes)
        return self.adjustment(existing["id"])

    def update_adjustment(self, adj_id: int, **changes: Any) -> dict[str, Any]:
        """Set fields of an adjustment directly, e.g. ``delta=3`` or ``ignore_before=None``.

        Args:
            adj_id: Id from ``adjustments()``.
            **changes: ``delta``, ``ignore_before`` and/or ``note``.

        Returns:
            The adjustment after the change, with its ``text``.

        Raises:
            KeyError: No such adjustment.
            ValueError: An unknown field, or an invalid value.
        """
        bad = set(changes) - {"delta", "ignore_before", "note"}
        if bad:
            raise ValueError(f"Can't change {', '.join(sorted(bad))} of an adjustment.")
        self.db.update_row("adjustments", adj_id, changes)
        return self.adjustment(adj_id)

    def remove_adjustment(self, adj_id: int) -> bool:
        """Delete an adjustment (its nudge and its reset).

        Returns:
            True if one was deleted.
        """
        return self.db.execute("DELETE FROM adjustments WHERE id = ?", (adj_id,)) > 0

    def adjustment(self, adj_id: int) -> dict[str, Any]:
        """One adjustment with its ``text``.

        Raises:
            KeyError: No such adjustment.
        """
        rows = self._query("SELECT * FROM adjustments WHERE id = ?", (adj_id,))
        if not rows:
            raise KeyError(adj_id)
        return {**dict(rows[0]), "text": describe_adjustment(rows[0])}

    def adjustments(self) -> list[dict[str, Any]]:
        """All adjustments, newest first, each with a ``text`` summary."""
        return [{**dict(r), "text": describe_adjustment(r)}
                for r in self._query("SELECT * FROM adjustments ORDER BY ts DESC, id DESC")]

    def _find_adjustment(self, kind: str, subject: str, artist: str | None) -> dict[str, Any] | None:
        """The adjustment for this artist or track, matching names loosely."""
        for r in self._query("SELECT * FROM adjustments WHERE kind = ?", (kind,)):
            same = (norm(r["subject"]) == norm(subject) if kind == "artist"
                    else track_key(r["subject"], r["artist"]) == track_key(subject, artist))
            if same:
                return dict(r)
        return None

    # ------------------------------------------------------------ notebook
    def add_note(self, text: str, topic: str | None = None, author: str = "curator", for_dj: bool = False) -> int:
        """Write a note in the notebook.

        Args:
            text: The note.
            topic: A short heading, e.g. ``"quiz: 70s soul"``.
            author: ``"curator"`` or ``"you"``.
            for_dj: Show it to the DJ too.

        Returns:
            The note's id.

        Raises:
            ValueError: Empty text or unknown author.
        """
        _check_note(author, text)
        return self._insert("notebook", {"ts": self._stamp()["ts"], "author": author,
                                         "topic": (topic or "").strip() or None, "text": text.strip(),
                                         "for_dj": int(bool(for_dj))})

    def update_note(self, note_id: int, **changes: Any) -> dict[str, Any]:
        """Change a note's ``text``, ``topic`` and/or ``for_dj``.

        Returns:
            The note after the change.

        Raises:
            KeyError: No such note.
            ValueError: An unknown field, or empty text.
        """
        bad = set(changes) - {"text", "topic", "for_dj"}
        if bad:
            raise ValueError(f"Can't change {', '.join(sorted(bad))} of a note.")
        if "for_dj" in changes:
            changes["for_dj"] = int(bool(changes["for_dj"]))
        return self.db.update_row("notebook", note_id, changes)

    def remove_note(self, note_id: int) -> bool:
        """Delete a note.

        Returns:
            True if one was deleted.
        """
        return self.db.execute("DELETE FROM notebook WHERE id = ?", (note_id,)) > 0

    def notes(self, limit: int = 100) -> list[dict[str, Any]]:
        """Notebook entries, newest first."""
        return [dict(r) for r in self._query("SELECT * FROM notebook ORDER BY ts DESC, id DESC LIMIT ?", (limit,))]

    # ------------------------------------------------------------ findings
    def add_finding(self, kind: str, subject: str, sentiment: int, statement: str,
                    note: str | None = None) -> int:
        """Start a tentative conclusion from quizzes; evidence is added with ``add_evidence``.

        Args:
            kind: ``"genre"``, ``"artist"``, ``"era"``, ``"mood"`` or ``"other"``.
            subject: What it is about, e.g. ``"K-pop"``.
            sentiment: 1 (likes it), -1 (dislikes it) or 0 (mixed).
            statement: The conclusion in words, as the DJ will read it.
            note: Anything else worth keeping.

        Returns:
            The finding's id.

        Raises:
            ValueError: A bad field, or an open finding about the same subject already exists.
        """
        row = _check_finding(kind, subject, sentiment, statement)
        for f in self._query("SELECT id, subject FROM findings WHERE kind = ? AND status != 'dropped'", (kind,)):
            if norm(f["subject"]) == norm(row["subject"]):
                raise ValueError(f"There is already a finding about {f['subject']} (#{f['id']}); add evidence to it.")
        now = self._stamp()["ts"]
        return self._insert("findings", {**row, "ts": now, "updated": now, "status": "tentative",
                                         "note": (note or "").strip() or None})

    def add_evidence(self, finding_id: int, artist: str, reaction: int, title: str | None = None,
                     salvage: bool = False, popularity: str | None = None, note: str | None = None) -> int:
        """Record one sample behind a finding and how the person reacted to it.

        Args:
            finding_id: Id from ``add_finding``.
            artist: The sample's artist.
            reaction: 1 (liked it), 0 (it's OK) or -1 (not for them).
            title: The sample's title.
            salvage: It was chosen to give the subject a fair chance: a
                different side of it than the samples so far.
            popularity: ``"hit"``, ``"well known"``, ``"known"`` or ``"deep cut"``, if known.
            note: Their words.

        Returns:
            The evidence id.

        Raises:
            KeyError: No such finding.
            ValueError: The finding was dropped, or a bad field.
        """
        finding = self._finding_row(finding_id)
        if finding["status"] == "dropped":
            raise ValueError(f"Finding #{finding_id} was dropped; start a new one.")
        if reaction not in (-1, 0, 1):
            raise ValueError("reaction must be -1, 0 or 1")
        _check_evidence(artist)
        eid = self._insert("finding_evidence", {
            "ts": self._stamp()["ts"], "finding_id": finding_id, "title": (title or "").strip() or None,
            "artist": artist.strip(), "reaction": reaction, "salvage": int(bool(salvage)),
            "popularity": popularity, "note": (note or "").strip() or None})
        self.db.execute("UPDATE findings SET updated = ? WHERE id = ?", (self._stamp()["ts"], finding_id))
        return eid

    def check_finding(self, finding_id: int) -> dict[str, Any]:
        """Is there enough evidence to confirm a finding? The same bar for every subject.

        Returns:
            ``ready`` (bool), ``missing`` (what's still needed, in words) and
            the counts behind it: ``samples``, ``artists``, ``agreeing``,
            ``salvage_tries``, ``hits``.

        Raises:
            KeyError: No such finding.
        """
        return self._evidence_check(self._finding_row(finding_id), self.tuning())

    def confirm_finding(self, finding_id: int) -> dict[str, Any]:
        """Mark a finding as confirmed, if its evidence is enough.

        Returns:
            The finding, with its evidence check.

        Raises:
            KeyError: No such finding.
            ValueError: Not enough evidence yet (the message says what's missing).
        """
        self.update_finding(finding_id, status="confirmed")
        return self.finding(finding_id)

    def update_finding(self, finding_id: int, **changes: Any) -> dict[str, Any]:
        """Change a finding's ``statement``, ``subject``, ``sentiment``, ``status`` or ``note``.

        Raises:
            KeyError: No such finding.
            ValueError: An unknown field, a bad value, or confirming without enough evidence.
        """
        bad = set(changes) - {"statement", "subject", "sentiment", "status", "note"}
        if bad:
            raise ValueError(f"Can't change {', '.join(sorted(bad))} of a finding.")
        self.db.update_row("findings", finding_id, {**changes, "updated": self._stamp()["ts"]})
        return self.finding(finding_id)

    def remove_finding(self, finding_id: int) -> bool:
        """Delete a finding and its evidence.

        Returns:
            True if one was deleted.
        """
        return self.db.execute("DELETE FROM findings WHERE id = ?", (finding_id,)) > 0

    def finding(self, finding_id: int) -> dict[str, Any]:
        """One finding with its evidence and its check.

        Raises:
            KeyError: No such finding.
        """
        f = self._finding_row(finding_id)
        evidence = [dict(e) for e in self._query("SELECT * FROM finding_evidence WHERE finding_id = ? ORDER BY id",
                                                 (finding_id,))]
        return {**f, "evidence": evidence, "check": self._evidence_check(f, self.tuning())}

    def findings(self, include_dropped: bool = False) -> list[dict[str, Any]]:
        """All findings, newest first, each with its evidence and check."""
        where = "" if include_dropped else "WHERE status != 'dropped'"
        return [self.finding(r["id"]) for r in self._query(f"SELECT id FROM findings {where} ORDER BY id DESC")]

    def _finding_row(self, finding_id: int) -> dict[str, Any]:
        """The ``findings`` row as a dict; KeyError if there's none."""
        rows = self._query("SELECT * FROM findings WHERE id = ?", (finding_id,))
        if not rows:
            raise KeyError(finding_id)
        return dict(rows[0])

    def _evidence_check(self, f: dict[str, Any], t: dict[str, float]) -> dict[str, Any]:
        """Count a finding's evidence against the bar in ``scoring.TUNING``."""
        rows = self._query("SELECT * FROM finding_evidence WHERE finding_id = ?", (f["id"],))
        samples = len(rows)
        if f["kind"] == "artist":   # about one artist: different songs are what count
            distinct = len({norm(r["title"]) for r in rows if r["title"]})
        else:
            distinct = len({norm(r["artist"]) for r in rows})
        agreeing = sum(1 for r in rows if r["reaction"] == f["sentiment"])
        salvage = sum(1 for r in rows if r["salvage"])
        hits = sum(1 for r in rows if r["popularity"] == "hit")
        missing = []
        if samples < t["finding_min_samples"]:
            missing.append(f"{int(t['finding_min_samples']) - samples} more sample(s)")
        what = "different songs" if f["kind"] == "artist" else "different artists"
        if distinct < t["finding_min_artists"]:
            missing.append(f"samples from {int(t['finding_min_artists']) - distinct} more {what}")
        if samples and hits == samples:
            missing.append("a sample that isn't a famous hit (all so far were hits)")
        if f["sentiment"] != 0 and samples and agreeing / samples < t["finding_min_agreement"]:
            missing.append(f"more agreement: only {agreeing} of {samples} reactions fit the conclusion")
        if f["sentiment"] < 0 and not salvage:
            missing.append("a salvage try: a sample from another side of it, to give it a fair chance")
        return {"ready": not missing, "missing": missing, "samples": samples, "artists": distinct,
                "agreeing": agreeing, "salvage_tries": salvage, "hits": hits}

    def _check_finding_row(self, r: dict[str, Any]) -> None:
        """Rules for a ``findings`` row, for the methods above and the GUI's Database page alike."""
        _check_finding(r["kind"], r["subject"], r["sentiment"], r["statement"], r["status"])
        if r["status"] == "confirmed":
            check = self._evidence_check(r, self.tuning())
            if not check["ready"]:
                raise ValueError(f"Not enough evidence to confirm yet: {'; '.join(check['missing'])}.")

    # ------------------------------------------------------------ tuning
    def tuning(self) -> dict[str, float]:
        """The scoring numbers in use: the defaults from ``scoring.TUNING``, with your changes."""
        values = {key: float(t.default) for key, t in TUNING.items()}
        values["half_life_days"] = float(self.half_life_days)
        for r in self._query("SELECT key, value FROM taste_settings"):
            if r["key"] in values:
                values[r["key"]] = float(r["value"])
        return values

    def tuning_info(self) -> list[dict[str, Any]]:
        """Each tunable with its label, description, default and current value (for the GUI)."""
        values = self.tuning()
        changed = {r["key"] for r in self._query("SELECT key FROM taste_settings")}
        out = []
        for key, t in TUNING.items():
            default = float(self.half_life_days) if key == "half_life_days" else float(t.default)
            out.append({"key": key, "label": t.label, "description": t.description, "default": default,
                        "value": values[key], "changed": key in changed,
                        "minimum": t.minimum, "maximum": t.maximum})
        return out

    def set_tuning(self, key: str, value: float | None) -> dict[str, float]:
        """Change one scoring number, or go back to its default.

        Args:
            key: A key of ``scoring.TUNING``.
            value: The new value, or None for the default.

        Returns:
            All values now in use.

        Raises:
            ValueError: Unknown key or invalid value.
        """
        if value is None:
            if key not in TUNING:
                raise ValueError(f"Unknown setting '{key}'.")
            self.db.execute("DELETE FROM taste_settings WHERE key = ?", (key,))
        else:
            number = check_tuning(key, value)
            self.db.execute("INSERT INTO taste_settings (key, value, ts) VALUES (?, ?, ?) "
                            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, ts = excluded.ts",
                            (key, number, self._stamp()["ts"]))
        return self.tuning()

    # ------------------------------------------------------------ reads
    def artist_for_title(self, title: str) -> str | None:
        """Most recent artist the agent queued under this title (fills gaps in speaker metadata)."""
        rows = self._query("SELECT artist FROM plays WHERE title = ? AND artist IS NOT NULL "
                           "ORDER BY id DESC LIMIT 1", (title,))
        return rows[0]["artist"] if rows else None

    def recent_feedback(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return the newest feedback rows, newest first."""
        return [dict(r) for r in self._query("SELECT * FROM feedback ORDER BY id DESC LIMIT ?", (limit,))]

    def listening_history(self, days: int = 14, artist: str | None = None, limit: int = 40) -> dict[str, Any]:
        """What actually happened lately: tracks finished and skipped, and what the DJ was asked for.

        Args:
            days: How far back to look.
            artist: Only this artist (names compared loosely).
            limit: Most listens and requests to list.

        Returns:
            ``listens`` (newest first: date, part of day, outcome, track, how
            much was heard, and ``context`` "quiz sample" or "for others" when
            it doesn't count towards taste), ``by_artist`` (completed/skipped
            counts of your own listening, most skipped first) and
            ``dj_requests`` (what you asked the DJ for).
        """
        since = (self._now() - timedelta(days=days)).isoformat(timespec="seconds")
        rows = [r for r in self._query("SELECT * FROM listens WHERE ts >= ? ORDER BY ts DESC, id DESC", (since,))
                if artist is None or norm(r["artist"]) == norm(artist)]
        counts: dict[str, dict[str, Any]] = {}
        for r in rows:
            if _listen_context(r) is None:
                c = counts.setdefault(norm(r["artist"]), {"artist": r["artist"] or "?", "completed": 0, "skipped": 0})
                c[r["outcome"]] += 1
        listens = [{k: v for k, v in {
                       "date": r["ts"][:16].replace("T", " "), "part_of_day": r["part_of_day"],
                       "outcome": r["outcome"], "track": f"{r['title']} - {r['artist'] or '?'}",
                       "heard": f"{round(100 * r['listened_s'] / r['duration_s'])}%"
                       if r["listened_s"] is not None and r["duration_s"] else None,
                       "context": _listen_context(r)}.items() if v is not None}
                   for r in rows[:limit]]
        requests = [{"date": r["ts"][:16].replace("T", " "), "asked": r["prompt"]} for r in self._query(
            "SELECT ts, prompt FROM requests WHERE ts >= ? AND COALESCE(agent, 'dj') = 'dj' "
            "ORDER BY ts DESC, id DESC LIMIT ?", (since, limit))]
        return {"days": days, "listens": listens,
                "by_artist": sorted(counts.values(), key=lambda c: (-c["skipped"], -c["completed"]))[:25],
                "dj_requests": requests}

    def taste_profile(self, part: str | None = None, recent_hours: int = 24) -> dict[str, Any]:
        """Summarise what shapes the agent's choices.

        Args:
            part: Current part of day; adds ``liked_in_the_<part>`` favourites.
            recent_hours: Window for ``played_in_last_<n>h``.

        Returns:
            Liked/avoided artists and tracks, notes, recent plays and totals.
            Empty lists are left out.
        """
        t = self.tuning()
        sc = self._scores(part, t)
        artists, tracks = list(sc.artists.values()), list(sc.tracks.values())
        # Loves stated as "sometimes", "rarely" or "only when asked" stay out of the liked lists (their
        # preference tells the DJ how often), whatever likes and listening added up to.
        liked_artists = [e for e in artists if norm(e.name) not in sc.occasional]
        liked_tracks_pool = [e for e in tracks if track_key(e.title, e.artist) not in sc.occasional
                             and norm(e.artist) not in sc.occasional]

        avoid_tracks = _names(tracks, t["avoid_track_at"], liked=False, limit=30)
        for e in tracks:
            if e.skips >= t["skips_to_avoid"] and e.name not in avoid_tracks:
                avoid_tracks.append(e.name)
        played_often, rest_artists, often_keys = self._played_lately(t)
        findings = self.findings()
        # A liked quiz sample shouldn't become a regular: only what you liked outside quizzes counts here.
        # Favourites heard often lately rest for a while (they're in played_often_lately instead).
        liked_tracks = sorted((e for e in liked_tracks_pool if _without_quiz_likes(e) >= t["liked_track_at"]
                               and (core_title(e.title), norm(e.artist)) not in often_keys),
                              key=lambda e: -_without_quiz_likes(e))

        # Your notes: from feedback, and notebook entries marked for the DJ. Newest first.
        dated = [(r["ts"], f"{'+' if r['sentiment'] > 0 else '-' if r['sentiment'] < 0 else '~'} {r['note']} "
                           f"({r['ts'][:10]}, {r['part_of_day']})")
                 for r in self._query("SELECT * FROM feedback WHERE note IS NOT NULL AND note != '' "
                                      "ORDER BY id DESC LIMIT 15")]
        dated += [(r["ts"], f"~ {r['text']} ({r['ts'][:10]}, notebook)")
                  for r in self._query("SELECT * FROM notebook WHERE for_dj = 1 ORDER BY ts DESC LIMIT 15")]
        notes = [text for _, text in sorted(dated, key=lambda d: d[0], reverse=True)[:20]]

        # Quiz samples from the curator don't count as "already played": the DJ may play them again.
        since = (self._now() - timedelta(hours=recent_hours)).isoformat(timespec="seconds")
        recent = [f"{r['title']} - {r['artist']}" for r in self._query(
            "SELECT title, artist, MAX(ts) AS ts FROM plays WHERE ts >= ? AND COALESCE(source, '') != 'curator' "
            "GROUP BY title, artist ORDER BY ts DESC LIMIT 60", (since,))]

        totals = self._query("SELECT (SELECT COUNT(*) FROM plays) AS plays, "
                             "(SELECT COUNT(*) FROM feedback) AS feedback, "
                             "(SELECT COUNT(*) FROM listens WHERE outcome = 'skipped') AS skips")[0]

        profile = {
            "preferences": [describe_preference(r) for r in sc.preferences],
            "liked_artists": _names(liked_artists, t["liked_artist_at"], liked=True, limit=15),
            "avoid_artists": _names(artists, t["avoid_artist_at"], liked=False, limit=15),
            "liked_tracks": [e.name for e in liked_tracks[:20]],
            "findings": [f["statement"] for f in findings if f["status"] == "confirmed"],
            "tentative_findings": [f"{f['statement']} (tentative: {f['check']['samples']} sample(s) from "
                                   f"{f['check']['artists']} {'song' if f['kind'] == 'artist' else 'artist'}(s))"
                                   for f in findings if f["status"] == "tentative"],
            "avoid_tracks": avoid_tracks[:30],
            "notes": notes,
            f"played_in_last_{recent_hours}h": recent,
            "played_often_lately": played_often,
            "artists_to_rest": rest_artists,
            "history": {"tracks_played": totals["plays"], "feedback_given": totals["feedback"],
                        "skips_seen": totals["skips"]},
        }
        if part:
            # A favourite for this time of day: stated so, or liked at this time on several different days.
            favourites = sorted((e for e in liked_artists if e.part_of_day_stated or (
                                    e.part_of_day_score >= t["liked_artist_at"]
                                    and len(e.part_of_day_days) >= t["part_of_day_min_days"])),
                                key=lambda e: -e.part_of_day_score)
            profile[f"liked_in_the_{part}"] = [e.name for e in favourites[:8]]
        return {k: v for k, v in profile.items() if v not in ([], None)}

    def _played_lately(self, t: dict[str, float]) -> tuple[list[str], list[str], set[tuple[str, str]]]:
        """What the DJ has been repeating within the freshness window (quiz samples aside).

        Returns:
            ``(tracks played twice or more, artists played a lot, keys of those tracks)``,
            each list most played first, with counts, e.g. ``"Ho Hey - The Lumineers (7x)"``.
        """
        since = (self._now() - timedelta(days=t["freshness_days"])).isoformat(timespec="seconds")
        tracks: dict[tuple[str, str], list] = {}
        artists: dict[str, list] = {}
        for r in self._query("SELECT title, artist FROM plays WHERE ts >= ? AND COALESCE(source, '') != 'curator' "
                             "ORDER BY id DESC", (since,)):
            key = (core_title(r["title"]), norm(r["artist"]))
            tracks.setdefault(key, [f"{r['title']} - {r['artist'] or '?'}", 0])[1] += 1
            if r["artist"]:
                artists.setdefault(norm(r["artist"]), [r["artist"], 0])[1] += 1
        often = sorted(((k, v) for k, v in tracks.items() if v[1] >= 2), key=lambda kv: -kv[1][1])
        busy = sorted((v for v in artists.values() if v[1] >= t["overplayed_artist_plays"]), key=lambda v: -v[1])
        return ([f"{name} ({n}x)" for _, (name, n) in often[:30]],
                [f"{name} ({n} plays)" for name, n in busy[:12]],
                {k for k, _ in often})

    def affinities(self, limit: int = 50) -> dict[str, list[dict[str, Any]]]:
        """Return the scores behind the taste profile, with where each came from.

        Args:
            limit: Maximum artists and tracks each.

        Returns:
            ``{"artists": [...], "tracks": [...]}`` sorted from most liked to
            most disliked. Each has ``name``, ``score``, ``parts`` (the score
            by source: stated, feedback, listening, adjusted) and
            ``adjustment`` (the hand-made change, or None). Artists have
            ``artist``; tracks have ``title``, ``artist`` and ``skips``.
        """
        sc = self._scores(None, self.tuning())

        def entry(e: Entry, track: bool) -> dict[str, Any]:
            d = {"name": e.name, "score": round(e.score, 2),
                 "parts": {k: round(v, 2) for k, v in e.parts.items()}, "adjustment": e.adjustment,
                 "artist": e.artist}
            if track:
                d.update(title=e.title, skips=e.skip_count)
            return d

        artists = sorted((entry(e, False) for e in sc.artists.values() if e.parts), key=lambda d: -d["score"])
        tracks = sorted((entry(e, True) for e in sc.tracks.values() if e.parts), key=lambda d: -d["score"])
        return {"artists": artists[:limit], "tracks": tracks[:limit]}

    # ------------------------------------------------------------ scoring
    def _scores(self, part: str | None, t: dict[str, float]) -> Scores:
        """Add up every weighted, faded signal per artist and per track (see ``scoring.py``)."""
        sc = Scores()
        now = self._now()

        # Adjustments first: their nudges, and the resets that hide older signals.
        artist_reset: dict[str, str] = {}
        track_reset: dict[tuple[str, str], str] = {}
        for r in self._query("SELECT * FROM adjustments ORDER BY id"):
            e = sc.artist(r["subject"]) if r["kind"] == "artist" else sc.track(r["subject"], r["artist"])
            e.add("adjusted", float(r["delta"]))
            e.adjustment = {**dict(r), "text": describe_adjustment(r)}
            if r["ignore_before"]:
                if r["kind"] == "artist":
                    artist_reset[norm(r["subject"])] = r["ignore_before"]
                else:
                    track_reset[track_key(r["subject"], r["artist"])] = r["ignore_before"]

        def reset_hides(row: Any, title: str | None) -> bool:
            """True if a reset makes the scoring ignore this (older) signal."""
            cutoff = [artist_reset.get(norm(row["artist"])) if row["artist"] else None,
                      track_reset.get(track_key(title, row["artist"])) if title else None]
            return any(c and row["ts"] <= c for c in cutoff)

        # Stated preferences: full weight, never fading. One limited to a part of
        # the day only counts at that time (as a favourite); "avoid, but only
        # then" ones are left to Claude, in their own words.
        # A love that should come up only sometimes counts partly; rarely / only
        # when asked not at all (``Scores.occasional`` keeps it out of the liked lists).
        sc.preferences = self._query("SELECT * FROM preferences ORDER BY id DESC")
        share = {"regular": 1.0, "sometimes": t["preference_sometimes"], "rarely": 0.0, "on_request": 0.0}
        for r in sc.preferences:
            if r["sentiment"] == 0 or r["kind"] not in ("artist", "track"):
                continue
            frequency = r["frequency"] or "regular"
            if r["sentiment"] > 0 and frequency != "regular":
                sc.occasional.add(norm(r["subject"]) if r["kind"] == "artist" else track_key(r["subject"], r["artist"]))
            w = t["preference"] * share.get(frequency, 1.0)
            if r["part_of_day"] is None:
                if r["kind"] == "artist":
                    sc.artist(r["subject"]).add("stated", w * r["sentiment"])
                else:  # a loved track says little about the artist's other songs
                    sc.track(r["subject"], r["artist"]).add("stated", w * r["sentiment"])
            elif r["kind"] == "artist" and r["part_of_day"] == part and r["sentiment"] > 0 and frequency == "regular":
                e = sc.artist(r["subject"])
                e.part_of_day_score += w
                e.part_of_day_stated = True

        # Feedback. Quiz answers (given to the curator) count less, and say
        # nothing about the time of day: a quiz happens whenever you have time.
        half_life = t["half_life_days"]
        weights = {"artist": t["artist_feedback"], "track": t["track_feedback"]}
        for r in self._query("SELECT f.*, COALESCE(f.source, q.agent) AS origin FROM feedback f "
                             "LEFT JOIN requests q ON q.id = f.request_id "
                             "WHERE f.scope IN ('track', 'artist') AND f.sentiment != 0"):
            title = r["title"] if r["scope"] == "track" else None
            if reset_hides(r, title):
                continue
            quiz = r["origin"] == "curator"
            source = "quiz" if quiz else "feedback"
            signed = r["sentiment"] * self._weight(r["ts"], half_life, now)
            if r["artist"]:
                # One quiz sample says less about the artist's other songs than everyday likes do.
                weight = t["quiz_feedback"] if quiz and title else weights[r["scope"]]
                sc.artist(r["artist"]).add(source, weight * signed, None if quiz else r["part_of_day"], part,
                                           str(r["ts"])[:10])
            if title:
                sc.track(title, r["artist"]).add(source, weights["track"] * signed)

        # Listening: only your own (not quiz samples, not music for others), and
        # mostly about the track. Skips fade faster than what you say.
        skip_half_life = t["skip_half_life_days"]
        for r in self._query("SELECT * FROM listens WHERE COALESCE(source, '') != 'curator' "
                             "AND COALESCE(audience, 'me') != 'others'"):
            if not r["title"] or reset_hides(r, r["title"]):
                continue
            track = sc.track(r["title"], r["artist"])
            if r["outcome"] == "completed":
                if t["completed"]:
                    track.add("listening", t["completed"] * self._weight(r["ts"], half_life, now))
                continue
            faded = self._weight(r["ts"], skip_half_life, now)
            track.add("listening", t["skipped"] * faded)
            track.skips += faded
            track.skip_count += 1
            if r["artist"] and t["skipped_artist"]:
                sc.artist(r["artist"]).add("listening", t["skipped_artist"] * faded)
        return sc


def _listen_context(r: Any) -> str | None:
    """Why a listen doesn't count towards taste ("quiz sample", "for others"), or None if it does."""
    if r["source"] == "curator":
        return "quiz sample"
    if r["audience"] == "others":
        return "for others"
    return None


def _without_quiz_likes(e: Entry) -> float:
    """An entry's score minus what liked quiz answers added (quiz dislikes still count)."""
    return e.score - max(0.0, e.parts.get("quiz", 0.0))


def _names(entries: list[Entry], at: float, liked: bool, limit: int) -> list[str]:
    """Names of the entries past a threshold, strongest first."""
    picked = [e for e in entries if (e.score >= at if liked else e.score <= at)]
    picked.sort(key=lambda e: -e.score if liked else e.score)
    return [e.name for e in picked[:limit]]


def describe_preference(p: Any) -> str:
    """Put a preference in plain words, e.g. ``"avoid genre: reggaeton (in the morning only)"``.

    A time of day narrows an *avoid* ("only then") but only highlights a
    *love* ("especially then"); the wording makes that clear to Claude. How
    often and when come next, e.g. ``"love artist: The Hu (rarely: an
    occasional surprise at most, never a regular; when: at the gym)"``.

    Args:
        p: A ``preferences`` row (sqlite Row or dict).

    Returns:
        One line, as Claude and the GUI show it.
    """
    what = f"{p['subject']} - {p['artist']}" if p["kind"] == "track" and p["artist"] else p["subject"]
    if p["kind"] == "rule":
        text = f"rule: {what}"
    else:
        verb = {1: "love", -1: "avoid", 0: "note on"}[p["sentiment"]]
        text = f"{verb} {p['kind']}: {what}"
    if p["part_of_day"]:
        text += {1: f" (especially in the {p['part_of_day']})",
                 -1: f" (in the {p['part_of_day']} only)"}.get(p["sentiment"], f" (in the {p['part_of_day']})")
    how = [FREQUENCY_WORDS[f] for f in [_get(p, "frequency")] if f in FREQUENCY_WORDS]
    if _get(p, "context"):
        how.append(f"when: {_get(p, 'context')}")
    if how:
        text += f" ({'; '.join(how)})"
    if p["note"]:
        text += f" - {p['note']}"
    return text


def describe_adjustment(a: Any) -> str:
    """Put an adjustment in plain words, e.g. ``"artist Radiohead: +2, learning reset on 2026-10-08"``.

    Args:
        a: An ``adjustments`` row (sqlite Row or dict).

    Returns:
        One line, as the curator and the GUI show it.
    """
    what = f"{a['subject']} - {a['artist']}" if a["kind"] == "track" and a["artist"] else a["subject"]
    bits = []
    if a["delta"]:
        bits.append(f"{a['delta']:+g}")
    if a["ignore_before"]:
        bits.append(f"learning reset on {str(a['ignore_before'])[:10]}")
    text = f"{a['kind']} {what}: {', '.join(bits) or 'no change'}"
    return f"{text} - {a['note']}" if a["note"] else text


def _check_feedback(sentiment: int, scope: str, title: str | None, artist: str | None, note: str | None) -> None:
    """Raise ValueError unless this is valid feedback (see ``Memory.record_feedback``)."""
    if sentiment not in (-1, 0, 1):
        raise ValueError("sentiment must be -1, 0 or 1")
    if scope not in ("track", "artist", "general"):
        raise ValueError("scope must be track, artist or general")
    if scope == "track" and not title:
        raise ValueError("track feedback needs a title")
    if scope == "artist" and not artist:
        raise ValueError("artist feedback needs an artist")
    if scope == "general" and not note:
        raise ValueError("general feedback needs a note")


def _get(row: Any, key: str) -> Any:
    """``row[key]``, or None for a row from before that column existed."""
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _check_preference(kind: str, sentiment: int, subject: str, artist: str | None = None,
                      part: str | None = None, note: str | None = None, frequency: str | None = None,
                      context: str | None = None) -> dict[str, Any]:
    """Validate and tidy a preference; return it as a row for the ``preferences`` table."""
    subject = (subject or "").strip()
    frequency = (frequency or "").strip() or None
    if frequency not in (None, *FREQUENCIES):
        raise ValueError(f"how often must be one of {', '.join(FREQUENCIES)}")
    if frequency not in (None, "regular") and sentiment == -1:
        raise ValueError("how often applies to a love or a note, not to an avoid (an avoid is never played)")
    if kind not in PREFERENCE_KINDS:
        raise ValueError(f"kind must be one of {', '.join(PREFERENCE_KINDS)}")
    if sentiment not in (-1, 0, 1):
        raise ValueError("sentiment must be -1, 0 or 1")
    if not subject:
        raise ValueError("a preference needs a subject (artist, title, genre or rule)")
    if part not in (None, *PARTS_OF_DAY):
        raise ValueError(f"part of day must be one of {', '.join(PARTS_OF_DAY)}, or empty for any time")
    artist = (artist or "").strip() or None
    return {"kind": kind, "sentiment": sentiment, "subject": subject,
            "artist": artist if kind == "track" else None,
            "part_of_day": part, "note": (note or "").strip() or None,
            "frequency": None if frequency == "regular" else frequency,
            "context": (context or "").strip() or None}


def _check_finding(kind: str, subject: str, sentiment: int, statement: str,
                   status: str = "tentative") -> dict[str, Any]:
    """Validate and tidy a finding's fields; return them as a row."""
    if kind not in FINDING_KINDS:
        raise ValueError(f"a finding is about one of: {', '.join(FINDING_KINDS)}")
    if sentiment not in (-1, 0, 1):
        raise ValueError("sentiment must be -1, 0 or 1")
    if status not in FINDING_STATUSES:
        raise ValueError(f"status must be one of {', '.join(FINDING_STATUSES)}")
    if not (subject or "").strip() or not (statement or "").strip():
        raise ValueError("a finding needs a subject and a statement")
    return {"kind": kind, "subject": subject.strip(), "sentiment": sentiment, "statement": statement.strip()}


def _check_evidence(artist: str | None) -> None:
    """Raise ValueError unless a piece of evidence names its artist."""
    if not (artist or "").strip():
        raise ValueError("a sample needs its artist")


def _check_note(author: str, text: str | None) -> None:
    """Raise ValueError unless this is a valid notebook entry."""
    if author not in ("curator", "you"):
        raise ValueError("a note's author is 'curator' or 'you'")
    if not (text or "").strip():
        raise ValueError("a note needs some text")


def _check_adjustment(kind: str, subject: str, delta: Any, ignore_before: str | None) -> None:
    """Raise ValueError unless this is a valid adjustment (see ``Memory.adjust``)."""
    if kind not in ADJUSTMENT_KINDS:
        raise ValueError("an adjustment is for an artist or a track")
    if not (subject or "").strip():
        raise ValueError("an adjustment needs an artist or a track title")
    try:
        delta = float(delta)
    except (TypeError, ValueError):
        raise ValueError(f"the change must be a number, not '{delta}'") from None
    if not -MAX_DELTA <= delta <= MAX_DELTA:
        raise ValueError(f"the change must be between -{MAX_DELTA:g} and +{MAX_DELTA:g}")
    if ignore_before is not None:
        try:
            datetime.fromisoformat(str(ignore_before))
        except ValueError:
            raise ValueError(f"ignore_before must be a date and time, not '{ignore_before}'") from None
