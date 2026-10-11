"""Speaker balance: how much louder or quieter each room plays than the volume-source room.

When you set one room's volume by hand (GUI, or the DJ: "turn Night down a
bit"), the difference between every speaker and the volume-source room
(Dining Room by default) is saved here. Group volume changes, new DJ sets and
resuming then move all speakers together and keep those differences, e.g.
Dining Room 20, Morn’ 20, Night 15. The safety cap still applies to every
speaker.

The differences live in the ``speaker_balance`` table of ``music.db`` (so the
Database page shows them), or only in memory when memory is turned off.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

from music_agent.learning.database import Database
from music_agent.text import norm

SCHEMA = """
CREATE TABLE IF NOT EXISTS speaker_balance (   -- volume difference from the volume-source room
    id INTEGER PRIMARY KEY, room TEXT NOT NULL UNIQUE,
    difference INTEGER NOT NULL CHECK (difference BETWEEN -100 AND 100),
    ts TEXT NOT NULL
);
"""


class SpeakerBalance:
    """Per-room volume differences, saved in the database if one is given.

    Args:
        db: Where to keep them; None keeps them in memory only (tests, no memory).
        now_fn: Clock, replaceable in tests.
    """

    def __init__(self, db: Database | None = None, now_fn: Callable[[], datetime] = datetime.now):
        self.db = db
        self._now = now_fn
        self._lock = threading.Lock()
        self._memory: dict[str, tuple[str, int]] = {}   # used when there is no database
        if db is not None:
            db.add_schema(SCHEMA)

    def differences(self) -> dict[str, int]:
        """``{room name: difference}`` for every room that has one."""
        if self.db is None:
            with self._lock:
                return {room: diff for room, diff in self._memory.values()}
        return {r["room"]: int(r["difference"]) for r in self.db.query("SELECT room, difference FROM speaker_balance")}

    def difference(self, room: str) -> int:
        """The saved difference for a room (names compared loosely), or 0."""
        wanted = norm(room)
        return next((d for name, d in self.differences().items() if norm(name) == wanted), 0)

    def save(self, differences: dict[str, int]) -> None:
        """Replace all saved differences (rooms at 0 are not stored).

        Args:
            differences: ``{room name: volume difference from the volume-source room}``.
        """
        kept = {room: max(-100, min(100, int(d))) for room, d in differences.items() if int(d) != 0}
        if self.db is None:
            with self._lock:
                self._memory = {norm(room): (room, d) for room, d in kept.items()}
            return
        ts = self._now().isoformat(timespec="seconds")
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM speaker_balance")
            for room, d in kept.items():
                conn.execute("INSERT INTO speaker_balance (room, difference, ts) VALUES (?, ?, ?)", (room, d, ts))

    def clear(self) -> None:
        """Forget the balance: every room plays at the volume-source room's level again."""
        self.save({})
