"""Watches what actually plays and records implicit feedback.

Every `interval` seconds it reads now-playing. When the track changes:
  - the old track reached >= 80% of its length  -> "completed" (mild like)
  - it changed while playing, before 50%        -> "skipped"   (mild dislike)
  - anything else (stopped, paused, unclear)    -> nothing recorded
When the agent itself replaces the queue, the next change is ignored, so a
new request doesn't count as skipping the old music.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from music_agent.learning.memory import Memory
from music_agent.players.base import Player

log = logging.getLogger(__name__)

COMPLETED_AT = 0.8
SKIPPED_BEFORE = 0.5
MIN_TRACK_S = 45  # ignore jingles, very short tracks and missing durations


def parse_hms(value: str | None) -> int | None:
    """Parse a Sonos time like ``"0:03:54"`` or ``"4:05"``.

    Args:
        value: Time text from the speaker.

    Returns:
        Seconds, or None if missing, ``NOT_IMPLEMENTED`` or unparsable.
    """
    if not value or value.upper() == "NOT_IMPLEMENTED":
        return None
    try:
        parts = [int(p) for p in value.split(":")]
    except ValueError:
        return None
    seconds = 0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


@dataclass
class _Current:
    """The track being watched and how far it got."""

    title: str
    artist: str | None
    duration: int | None
    max_pos: int = 0
    playing: bool = False


class PlaybackWatcher:
    """Polls now-playing and records completed and skipped tracks in memory.

    Args:
        player: Speakers to watch.
        memory: Where listens are recorded.
        interval: Seconds between polls.
    """

    def __init__(self, player: Player, memory: Memory, interval: float = 15.0):
        self.player = player
        self.memory = memory
        self.interval = interval
        self._cur: _Current | None = None
        self._ignore_next = False
        self._task: asyncio.Task | None = None

    def ignore_next_change(self) -> None:
        """Call before replacing the queue so the change isn't counted as a skip."""
        self._ignore_next = True

    def observe(self, state: str, title: str | None, artist: str | None,
                position: str | None, duration: str | None) -> str | None:
        """Feed one now-playing sample.

        Args:
            state: Transport state, e.g. ``"PLAYING"``.
            title: Current track title.
            artist: Current track artist.
            position: Position as ``"0:01:23"``.
            duration: Length as ``"0:03:54"``.

        Returns:
            ``"completed"`` or ``"skipped"`` if the previous track was judged
            and recorded, else None.
        """
        playing = state == "PLAYING"
        if not title:
            if self._cur and not playing:
                self._cur.playing = False
            return None
        pos, dur = parse_hms(position), parse_hms(duration)
        cur = self._cur
        if cur and (cur.title, cur.artist) == (title, artist):
            if pos is not None:
                # Position jumping back near 0 while the same track plays = it restarted; keep max.
                cur.max_pos = max(cur.max_pos, pos)
            cur.duration = cur.duration or dur
            cur.playing = playing
            return None

        outcome = None
        if cur is not None:
            if self._ignore_next:
                self._ignore_next = False
            else:
                outcome = self._judge(cur, changed_while_playing=cur.playing and playing)
                if outcome:
                    self.memory.record_listen(cur.title, cur.artist, outcome,
                                              listened_s=cur.max_pos, duration_s=cur.duration)
                    log.debug("listen %s: %s - %s", outcome, cur.title, cur.artist)
        self._cur = _Current(title, artist, dur, pos or 0, playing)
        return outcome

    def _judge(self, cur: _Current, changed_while_playing: bool) -> str | None:
        """Decide whether a track that just ended counts as completed, skipped or neither."""
        if not cur.duration or cur.duration < MIN_TRACK_S:
            return None
        # A sample can be up to `interval` seconds stale, so give credit for that.
        reached = (cur.max_pos + self.interval) / cur.duration
        if reached >= COMPLETED_AT:
            return "completed"
        if changed_while_playing and cur.max_pos / cur.duration < SKIPPED_BEFORE:
            return "skipped"
        return None

    async def poll_once(self) -> str | None:
        """Read now-playing once and feed it to ``observe``; return its outcome."""
        np = await asyncio.to_thread(self.player.now_playing)
        return self.observe(np.state, np.title, np.artist, np.position, np.duration)

    async def run(self) -> None:
        """Poll forever; errors (speakers offline) are logged and polling continues."""
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # speakers offline etc.: keep watching
                log.debug("watcher poll failed: %s", exc)
            await asyncio.sleep(self.interval)

    def start(self) -> asyncio.Task:
        """Start polling in the background (once); return the task."""
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self.run(), name="playback-watcher")
        return self._task

    @property
    def running(self) -> bool:
        """True while background polling is active."""
        return self._task is not None and not self._task.done()

    async def stop(self) -> None:
        """Stop polling and wait for the task to finish."""
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
