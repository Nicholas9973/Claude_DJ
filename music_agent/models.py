"""Provider-neutral data types shared by providers, players and the agent."""

from __future__ import annotations

from dataclasses import dataclass, field

KINDS = ("track", "album", "playlist", "artist")
PARTS_OF_DAY = ("morning", "afternoon", "evening", "night")


def part_of_day(hour: int) -> str:
    """Name the coarse time of day, used for context and time-specific tastes.

    Args:
        hour: Local hour, 0-23.

    Returns:
        One of ``"morning"`` (5-11), ``"afternoon"`` (12-16),
        ``"evening"`` (17-21) or ``"night"``.
    """
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


@dataclass(frozen=True)
class MediaItem:
    """Something playable from a music service: a track, album, playlist or artist.

    Attributes:
        provider: Short provider id, e.g. ``"qobuz"``.
        kind: One of ``KINDS``.
        id: Provider-native id, e.g. ``"track:1068442:6"``.
        title: Display title.
        artist: Artist name, if known.
        album: Album name, if known.
        duration_s: Length in seconds, if known.
    """

    provider: str
    kind: str
    id: str
    title: str
    artist: str | None = None
    album: str | None = None
    duration_s: int | None = None

    @property
    def ref(self) -> str:
        """Stable string handle, safe to store in the database or show to the agent."""
        return f"{self.provider}|{self.kind}|{self.id}"

    @staticmethod
    def split_ref(ref: str) -> tuple[str, str, str]:
        """Split a ref made by ``MediaItem.ref`` back into its parts.

        Args:
            ref: A ref like ``"qobuz|track|track:1068442:6"``.

        Returns:
            ``(provider, kind, id)``.

        Raises:
            ValueError: If the ref doesn't have three ``|``-separated parts.
        """
        provider, kind, item_id = ref.split("|", 2)
        return provider, kind, item_id

    def label(self) -> str:
        """Human-readable "Title - Artist (Album)" text; parts that are unknown are left out."""
        text = self.title
        if self.artist:
            text += f" - {self.artist}"
        if self.album and self.kind == "track":
            text += f" ({self.album})"
        return text


@dataclass
class NowPlaying:
    """Snapshot of what the active speaker group is doing.

    Attributes:
        state: Sonos transport state: ``PLAYING``, ``PAUSED_PLAYBACK``,
            ``STOPPED`` or ``TRANSITIONING``.
        title: Current track title, if any.
        artist: Current track artist, if any.
        album: Current track album, if any.
        position: Position in the track as ``"0:01:23"``.
        duration: Track length as ``"0:03:54"``.
        queue_position: 1-based position in the queue.
        queue_length: Number of items in the queue.
        rooms: Room names in the group.
        volume: Group volume: the volume-source speaker's level (see ``players/balance.py``).
        album_art: URL of the cover image, if the speaker provides one.
    """

    state: str
    title: str | None
    artist: str | None
    album: str | None
    position: str | None
    duration: str | None
    queue_position: int | None
    queue_length: int
    rooms: list[str]
    volume: int | None
    album_art: str | None = None


@dataclass
class RoomVolume:
    """One speaker's volume.

    Attributes:
        room: Room name.
        volume: Its current level, 0-100.
        difference: Saved balance: how much louder (+) or quieter (-) it plays
            than the volume-source room.
        in_group: Whether it is in the group that is playing.
        reference: Whether it is the volume-source room the others are balanced against.
    """

    room: str
    volume: int
    difference: int
    in_group: bool
    reference: bool


@dataclass
class PlayResult:
    """Outcome of a ``Player.play`` call.

    Attributes:
        queued: Items that were added to the queue.
        failed: ``(item, error message)`` for items that could not be queued.
        rooms: Room names playing the queue.
        volume: Volume the group plays at.
    """

    queued: list[MediaItem] = field(default_factory=list)
    failed: list[tuple[MediaItem, str]] = field(default_factory=list)
    rooms: list[str] = field(default_factory=list)
    volume: int | None = None
