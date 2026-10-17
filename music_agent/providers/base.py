"""MusicProvider: where music is found. Swap implementations to change service."""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from music_agent.models import MediaItem
from music_agent.providers.matching import rank, same_artist

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """A search or service call failed. The message is shown to the user or agent."""


@dataclass
class SearchAttempt:
    """One search made while looking up a song.

    Attributes:
        query: Text sent to the service.
        results: ``(score, item)`` pairs, best first.
        error: Error message if the search failed.
    """

    query: str
    results: list[tuple[float, MediaItem]] = field(default_factory=list)
    error: str | None = None


@dataclass
class TrackLookup:
    """Outcome of looking up one requested song.

    Attributes:
        title: Requested title.
        artist: Requested artist.
        item: Best match, or None if nothing matched.
        attempts: Every search made, in order (for ``cli find --explain``).
        artist_tracks: On a miss, real catalogue tracks by the artist, so the
            agent can substitute instead of guessing.
    """

    title: str
    artist: str | None
    item: MediaItem | None = None
    attempts: list[SearchAttempt] = field(default_factory=list)
    artist_tracks: list[MediaItem] = field(default_factory=list)

    @property
    def error(self) -> str | None:
        """Last error, set only when every search failed (vs. simply finding no match)."""
        if self.item is None and self.attempts and all(a.error for a in self.attempts):
            return self.attempts[-1].error
        return None


class MusicProvider(ABC):
    """Searches a music catalogue and hands playable objects to a Player.

    Attributes:
        name: Short id used in ``MediaItem.provider``, e.g. ``"qobuz"``.
    """

    name: str

    @abstractmethod
    def search(self, query: str, kind: str = "track", limit: int = 10) -> list[MediaItem]:
        """Search the catalogue.

        Args:
            query: Search text.
            kind: ``"track"``, ``"album"``, ``"playlist"`` or ``"artist"``.
            limit: Maximum number of results.

        Returns:
            Results in the service's order.

        Raises:
            ProviderError: The search failed.
        """

    @abstractmethod
    def to_native(self, item: MediaItem) -> Any:
        """Return the object the Player needs to queue this item."""

    @property
    def unplayable(self) -> set[str]:
        """Refs the speakers refused to queue (e.g. not licensed here); lookups pass over them.

        Kept only while the program runs: the next start tries them again.
        """
        if not hasattr(self, "_unplayable"):
            self._unplayable: set[str] = set()
        return self._unplayable

    # ------------------------------------------------------------ lookup
    def lookup_track(self, title: str, artist: str | None = None,
                     album: str | None = None) -> TrackLookup:
        """Find a specific song, trying progressively broader searches.

        1. "artist title": precise when the catalogue indexes both.
        2. "title": wider net (25 results) for catalogues that match titles only.
        3. "artist": the artist's own tracks (50 results); finds songs whose
           title has hundreds of other versions ("Cielito Lindo").

        Args:
            title: Requested title.
            artist: Requested artist, if known.
            album: Requested album, if known; prefers that version.

        Returns:
            The match (the best-scoring version the speakers haven't refused),
            every attempt, and on a miss the artist's real tracks.
        """
        result = TrackLookup(title, artist)
        plans = [(f"{artist} {title}", 10)] if artist else []
        plans.append((title, 25))
        if artist:
            plans.append((artist, 50))

        for query, limit in plans:
            attempt = self._search_scored(query, title, artist, album, limit)
            result.attempts.append(attempt)
            best = next((item for score, item in attempt.results
                         if score > 0 and item.ref not in self.unplayable), None)
            if best:
                result.item = best
                return result

        if artist:
            seen = set()
            for attempt in result.attempts:
                for _, item in attempt.results:
                    key = item.title.lower()
                    if same_artist(item, artist) and key not in seen:
                        seen.add(key)
                        result.artist_tracks.append(item)
        return result

    def find_track(self, title: str, artist: str | None = None,
                   album: str | None = None) -> MediaItem | None:
        """Return the best catalogue match for a song, avoiding covers/karaoke; see ``lookup_track``."""
        return self.lookup_track(title, artist, album).item

    def _search_scored(self, query: str, title: str, artist: str | None, album: str | None,
                       limit: int, retries: int = 1) -> SearchAttempt:
        """Run one track search (retrying errors) and score the results against the request."""
        attempt = SearchAttempt(query)
        for n in range(retries + 1):
            try:
                items = self.search(query, "track", limit)
                attempt.results = rank(items, title, artist, album)
                attempt.error = None
                return attempt
            except ProviderError as exc:
                attempt.error = str(exc)
                log.info("search %r failed (try %d): %s", query, n + 1, exc)
                if n < retries:
                    time.sleep(0.5 * (n + 1))
        return attempt
