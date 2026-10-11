"""Player: where music comes out. Implementations enforce the safety limits."""

from __future__ import annotations

from abc import ABC, abstractmethod

from music_agent.models import MediaItem, NowPlaying, PlayResult, RoomVolume

MODES = ("replace", "append", "next")


class PlayerError(Exception):
    """A playback operation failed. The message is shown to the user or agent."""


class PolicyError(PlayerError):
    """Refused by a safety rule (quiet hours, etc.). The message is user-facing."""


class Player(ABC):
    """Controls a set of speakers."""

    @abstractmethod
    def rooms(self) -> list[str]:
        """Return the names of all rooms (speakers)."""

    @abstractmethod
    def play(self, items: list[MediaItem], rooms: str | list[str] | None = None,
             mode: str = "replace", force: bool = False) -> PlayResult:
        """Queue items and, if needed, start playback.

        Args:
            items: What to queue, in order.
            rooms: ``"all"``, room names, or None for the configured default.
            mode: ``"replace"`` = new queue and start; ``"append"`` = add to
                the end; ``"next"`` = play after the current track.
            force: Ignore quiet hours (CLI only; never given to the agent).

        Returns:
            What was queued, what failed, and where it plays.

        Raises:
            PolicyError: A safety rule refused to start playback.
            PlayerError: Nothing could be queued or the speakers are unreachable.
        """

    @abstractmethod
    def pause(self) -> None:
        """Pause the active group."""

    @abstractmethod
    def resume(self, force: bool = False) -> None:
        """Resume the active group.

        Args:
            force: Ignore quiet hours.
        """

    @abstractmethod
    def skip(self) -> None:
        """Skip to the next track."""

    @abstractmethod
    def stop(self) -> None:
        """Stop playback."""

    @abstractmethod
    def set_volume(self, level: int) -> int:
        """Set the volume on every speaker in the active group.

        Args:
            level: Requested level, 0-100.

        Returns:
            The level actually applied, after the safety cap.
        """

    @abstractmethod
    def change_volume(self, delta: int) -> int:
        """Change the group volume relative to its current level.

        Args:
            delta: Amount to add (negative to lower).

        Returns:
            The level actually applied, after the safety cap.
        """

    @abstractmethod
    def room_volumes(self) -> list[RoomVolume]:
        """Every speaker's volume and saved balance, whether it's playing or not."""

    @abstractmethod
    def set_room_volume(self, room: str, level: int) -> int:
        """Set one speaker's volume and remember the balance between speakers.

        Args:
            room: Room name.
            level: Requested level, 0-100.

        Returns:
            The level actually applied, after the safety cap.

        Raises:
            PlayerError: Unknown room, or the speakers are unreachable.
        """

    @abstractmethod
    def change_room_volume(self, room: str, delta: int) -> int:
        """Change one speaker's volume relative to its current level (and save the balance).

        Args:
            room: Room name.
            delta: Amount to add (negative to lower).

        Returns:
            The level actually applied, after the safety cap.
        """

    @abstractmethod
    def reset_balance(self) -> int:
        """Forget the balance and play every speaker at the volume-source room's level.

        Returns:
            The level the speakers are now at.
        """

    @abstractmethod
    def now_playing(self) -> NowPlaying:
        """Return what the active group is playing."""

    @abstractmethod
    def queue(self, limit: int = 20) -> list[str]:
        """List the queue as "Title - Artist" lines.

        Args:
            limit: Maximum number of entries.
        """
