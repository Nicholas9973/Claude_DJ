"""Sonos player via SoCo (local network control).

Grouping: by default every speaker is grouped with the volume-source speaker
(Dining Room) as coordinator. Volume: the group plays at the volume-source
speaker's level, and each other speaker at that level plus its saved balance
(``balance.py``; 0 unless you set a room's volume by hand).
Safety limits from config are applied on every volume change and play start.

Resilience: speaker state is cached by SoCo. After the PC sleeps, the network
drops or a speaker reboots, that cache can be stale (a speaker "has no group").
Every operation first resolves the current group; if that fails it clears the
caches, rediscovers the speakers and tries once more.
"""

from __future__ import annotations

import contextlib
import functools
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import requests
import soco
from soco.exceptions import SoCoException

from music_agent.config import Config
from music_agent.models import MediaItem, NowPlaying, PlayResult, RoomVolume
from music_agent.players.balance import SpeakerBalance
from music_agent.players.base import MODES, Player, PlayerError, PolicyError
from music_agent.providers.base import MusicProvider
from music_agent.text import norm

log = logging.getLogger(__name__)

# Errors that usually mean "our picture of the speakers is out of date".
TRANSIENT = (SoCoException, OSError, requests.RequestException)
UNPLAYABLE_UPNP_ERROR = "800"   # the speaker refused this item (e.g. not licensed in this country)

Rooms = str | list[str] | None


class StaleSpeakerState(PlayerError):
    """A speaker isn't where SoCo's cached state says it is."""


def _refresh_and_retry_once(method: Callable) -> Callable:
    """Retry once with freshly discovered speakers if the speaker state was stale.

    Only for operations that are safe to repeat (they run before anything is
    queued, or they set an absolute state). Never for ``skip`` or relative
    volume changes, which a retry could apply twice.

    Args:
        method: A ``SonosPlayer`` method.

    Returns:
        The wrapped method. A second failure is raised as ``PlayerError``.
    """
    @functools.wraps(method)
    def wrapper(self: SonosPlayer, *args: Any, **kwargs: Any) -> Any:
        try:
            return method(self, *args, **kwargs)
        except (StaleSpeakerState, *TRANSIENT) as exc:
            log.warning("Speaker state looked stale (%s: %s); rediscovering and retrying.",
                        type(exc).__name__, exc)
            self.refresh()
            try:
                return method(self, *args, **kwargs)
            except (StaleSpeakerState, *TRANSIENT) as exc2:
                raise PlayerError(f"Can't reach the speakers right now ({exc2}). "
                                  "Are they powered on and on the same network as this PC?") from exc2
    return wrapper


class SonosPlayer(Player):
    """Plays music on Sonos speakers on the local network.

    Args:
        config: Settings; ``config.sonos`` and ``config.safety`` are used.
        provider: Turns ``MediaItem``s into something Sonos can queue. Can be
            set later (``app.build`` does this).
        now_fn: Clock, replaceable in tests.
        balance: Per-room volume differences; in memory only unless given
            (``app.build`` gives one saved in ``music.db``).
    """

    def __init__(self, config: Config, provider: MusicProvider | None = None,
                 now_fn: Callable[[], datetime] = datetime.now, balance: SpeakerBalance | None = None):
        self.config = config
        self.provider = provider
        self._now = now_fn
        self.balance = balance or SpeakerBalance()
        self._speakers: list[soco.SoCo] | None = None

    # ------------------------------------------------------------ speakers
    def refresh(self) -> None:
        """Forget cached speakers and group state; the next call rediscovers them."""
        for spk in self._speakers or []:
            with contextlib.suppress(Exception):  # best effort
                spk.zone_group_state.clear_cache()
        self._speakers = None

    def speakers(self, refresh: bool = False) -> list[soco.SoCo]:
        """Return all speakers, discovering them on first use.

        Args:
            refresh: Discover again even if speakers are cached.

        Returns:
            Speakers sorted by room name.

        Raises:
            PlayerError: No speakers were found.
        """
        if self._speakers is None or refresh:
            cfg = self.config.sonos
            if cfg.hosts:
                found = [soco.SoCo(ip) for ip in cfg.hosts]
            else:
                found = list(soco.discover(timeout=cfg.discovery_timeout) or [])
            if not found:
                raise PlayerError("No Sonos speakers found on the network.")
            self._speakers = sorted(found, key=lambda s: s.player_name)
        return self._speakers

    def any_speaker(self) -> soco.SoCo:
        """Return one speaker, e.g. for talking to the music service."""
        return self.speakers()[0]

    @_refresh_and_retry_once
    def rooms(self) -> list[str]:
        """Return the names of all rooms."""
        return [s.player_name for s in self.speakers()]

    def find_room(self, name: str) -> soco.SoCo:
        """Find a speaker by room name, ignoring case, accents, spaces and punctuation.

        An exact match wins; otherwise a unique partial match ("morn" finds "Morn’").

        Args:
            name: Room name as typed by a person or the agent.

        Returns:
            The speaker.

        Raises:
            PlayerError: No room or more than one room matches.
        """
        want = norm(name).replace(" ", "")
        speakers = self.speakers()
        key = {s: norm(s.player_name).replace(" ", "") for s in speakers}
        hits = [s for s in speakers if key[s] == want] or [s for s in speakers if want and want in key[s]]
        if len(hits) != 1:
            problem = "Ambiguous" if hits else "Unknown"
            raise PlayerError(f"{problem} room '{name}'. Rooms: {', '.join(self.rooms())}")
        return hits[0]

    def _targets(self, rooms: Rooms) -> list[soco.SoCo]:
        """Resolve a rooms argument (None = configured default) into speakers."""
        rooms = rooms if rooms is not None else self.config.sonos.default_rooms
        if isinstance(rooms, str):
            if rooms.strip().lower() == "all":
                return list(self.speakers())
            rooms = rooms.split(",")
        targets = []
        for name in rooms:
            spk = self.find_room(name.strip())
            if spk not in targets:
                targets.append(spk)
        return targets

    def _volume_source(self, targets: list[soco.SoCo]) -> soco.SoCo:
        """The configured volume-source speaker if it's among targets, else the first target."""
        try:
            src = self.find_room(self.config.sonos.volume_source)
        except PlayerError:
            return targets[0]
        return src if src in targets else targets[0]

    @staticmethod
    def _group_of(spk: soco.SoCo) -> Any:
        """Return the speaker's current group, re-reading SoCo's cache once if it has none.

        Always use this instead of ``spk.group``, which can be None after sleep.

        Raises:
            StaleSpeakerState: The speaker still reports no group.
        """
        group = spk.group
        if group is None:
            spk.zone_group_state.clear_cache()
            group = spk.group
        if group is None:
            raise StaleSpeakerState(f"'{spk.player_name}' is not reporting a group")
        return group

    def _group(self, targets: list[soco.SoCo]) -> soco.SoCo:
        """Make exactly ``targets`` one group and return its coordinator."""
        coord = self._volume_source(targets)
        if self._group_of(coord).coordinator != coord:
            coord.unjoin()
        for spk in targets:
            if spk is not coord and self._group_of(spk).coordinator != coord:
                spk.join(coord)
        for member in list(self._group_of(coord).members):
            if member not in targets and member != coord:
                member.unjoin()
        return coord

    def _coordinator(self) -> soco.SoCo:
        """Coordinator of the group containing the volume-source speaker (or first speaker)."""
        return self._group_of(self._volume_source(self.speakers())).coordinator

    def _members(self, coord: soco.SoCo) -> list[soco.SoCo]:
        """Speakers in the coordinator's group."""
        return list(self._group_of(coord).members)

    # ------------------------------------------------------------ safety
    def _check_start_allowed(self, force: bool) -> None:
        """Raise PolicyError if quiet hours forbid starting music (unless forced)."""
        safety = self.config.safety
        now = self._now().time()
        if safety.quiet_mode == "block" and safety.in_quiet_hours(now) and not force:
            raise PolicyError(
                f"Quiet hours ({safety.quiet_start:%H:%M}-{safety.quiet_end:%H:%M}): "
                "not starting music."
            )

    def _cap(self) -> int:
        """The volume cap right now."""
        return self.config.safety.volume_cap(self._now().time())

    def _apply_volume(self, speakers: list[soco.SoCo], level: int) -> int:
        """Set the group to ``level`` (clamped to 0..cap), keeping the saved balance.

        The volume-source room gets ``level``; every other speaker gets
        ``level`` plus its saved difference, also clamped to 0..cap.

        Returns:
            The applied group level.
        """
        cap = self._cap()
        level = max(0, min(int(level), cap))
        differences = self._differences()
        for spk in speakers:
            spk.volume = max(0, min(level + differences.get(spk.player_name, 0), cap))
        return level

    def _differences(self) -> dict[str, int]:
        """Saved balance by exact room name (the volume-source room is always 0)."""
        saved = {norm(room): d for room, d in self.balance.differences().items()}
        reference = self._reference()
        return {s.player_name: 0 if s is reference else saved.get(norm(s.player_name), 0)
                for s in self.speakers()}

    def _reference(self) -> soco.SoCo | None:
        """The configured volume-source speaker, or None if there's no such room."""
        try:
            return self.find_room(self.config.sonos.volume_source)
        except PlayerError:
            return None

    def _group_level(self, speakers: list[soco.SoCo]) -> int:
        """The group's level: the volume-source room's volume, or worked out from its balance if it's not playing."""
        ref = self._volume_source(speakers)
        return ref.volume - self._differences().get(ref.player_name, 0)

    # ------------------------------------------------------------ playback
    def play(self, items: list[MediaItem], rooms: Rooms = None, mode: str = "replace",
             force: bool = False) -> PlayResult:
        """Queue items and, if needed, group the rooms and start playback.

        See ``Player.play``. One item failing to queue doesn't stop the others.
        Not retried as a whole, because a retry after queueing could add items twice.
        """
        if mode not in MODES:
            raise PlayerError(f"mode must be one of {MODES}")
        if not items:
            raise PlayerError("Nothing to play.")
        if self.provider is None:
            raise PlayerError("No music provider configured.")

        starting, coord, targets = self._prepare(rooms, mode, force)
        result = PlayResult(rooms=sorted(s.player_name for s in targets))

        if mode == "replace":
            coord.clear_queue()
            insert_at = 0  # 0 = end of queue
        elif mode == "next":
            pos = coord.get_current_track_info().get("playlist_position")
            insert_at = int(pos) + 1 if pos and str(pos).isdigit() and int(pos) > 0 else 0
        else:
            insert_at = 0

        first_pos = None
        for item in items:
            try:
                qpos = coord.add_to_queue(self.provider.to_native(item), position=insert_at)
                if insert_at:
                    insert_at += 1
                first_pos = first_pos or qpos
                result.queued.append(item)
            except Exception as exc:  # one bad track shouldn't sink the request
                log.warning("Could not queue %s: %s", item.ref, exc)
                result.failed.append((item, str(exc)))
                if str(getattr(exc, "error_code", "")) == UNPLAYABLE_UPNP_ERROR:
                    self.provider.unplayable.add(item.ref)   # lookups pick another version next time

        if not result.queued:
            raise PlayerError("None of the items could be queued: "
                              + "; ".join(f"{i.label()}: {e}" for i, e in result.failed))

        if starting:
            result.volume = self._apply_volume(targets, self._group_level(targets))
            coord.play_from_queue(first_pos - 1)
        else:
            result.volume = self._group_level(targets)
        return result

    @_refresh_and_retry_once
    def _prepare(self, rooms: Rooms, mode: str, force: bool) -> tuple[bool, soco.SoCo, list[soco.SoCo]]:
        """Decide whether playback (re)starts, and get the group ready. Nothing is queued yet.

        "Starting" means we (re)start playback: group the target rooms and apply
        start rules. Adding to a queue that is already playing leaves the group alone.

        Returns:
            ``(starting, coordinator, speakers in the group)``.
        """
        starting = mode == "replace" or self._state(self._coordinator()) != "PLAYING"
        if starting:
            self._check_start_allowed(force)
            targets = self._targets(rooms)
            return True, self._group(targets), targets
        coord = self._coordinator()
        return False, coord, self._members(coord)

    @_refresh_and_retry_once
    def pause(self) -> None:
        """Pause the active group."""
        self._coordinator().pause()

    @_refresh_and_retry_once
    def resume(self, force: bool = False) -> None:
        """Resume the active group, re-applying the volume cap first.

        Args:
            force: Ignore quiet hours.

        Raises:
            PolicyError: Quiet hours and not forced.
        """
        self._check_start_allowed(force)
        coord = self._coordinator()
        members = self._members(coord)
        self._apply_volume(members, self._group_level(members))
        coord.play()

    def skip(self) -> None:
        """Skip to the next track. Not retried: a retry could skip two tracks."""
        self._coordinator().next()

    @_refresh_and_retry_once
    def stop(self) -> None:
        """Stop the active group."""
        self._coordinator().stop()

    @_refresh_and_retry_once
    def set_volume(self, level: int) -> int:
        """Set the group volume; see ``Player.set_volume``."""
        return self._apply_volume(self._members(self._coordinator()), level)

    def change_volume(self, delta: int) -> int:
        """Change the group volume; see ``Player.change_volume``. Not retried: it's relative."""
        members = self._members(self._coordinator())
        return self._apply_volume(members, self._group_level(members) + delta)

    @_refresh_and_retry_once
    def room_volumes(self) -> list[RoomVolume]:
        """Every speaker's volume and saved balance; see ``Player.room_volumes``."""
        members = set(self._members(self._coordinator()))
        differences = self._differences()
        reference = self._reference()
        return [RoomVolume(s.player_name, s.volume, differences.get(s.player_name, 0), s in members, s is reference)
                for s in sorted(self.speakers(), key=lambda s: s.player_name)]

    @_refresh_and_retry_once
    def set_room_volume(self, room: str, level: int) -> int:
        """Set one speaker and save the balance; see ``Player.set_room_volume``.

        The balance saved is the difference between each speaker and the
        volume-source room right now, so it matches what you hear.
        """
        spk = self.find_room(room)
        applied = max(0, min(int(level), self._cap()))
        spk.volume = applied
        reference = self._reference()
        if reference is not None:
            ref_volume = reference.volume
            self.balance.save({s.player_name: s.volume - ref_volume for s in self.speakers() if s is not reference})
        return applied

    def change_room_volume(self, room: str, delta: int) -> int:
        """Change one speaker relative to its level; see ``Player.change_room_volume``. Not retried."""
        return self.set_room_volume(room, self.find_room(room).volume + int(delta))

    @_refresh_and_retry_once
    def reset_balance(self) -> int:
        """Forget the balance and level the group; see ``Player.reset_balance``."""
        self.balance.clear()
        members = self._members(self._coordinator())
        return self._apply_volume(members, self._volume_source(members).volume)

    # ------------------------------------------------------------ status
    @staticmethod
    def _state(coord: soco.SoCo) -> str:
        """Transport state of a coordinator, e.g. ``"PLAYING"``."""
        return coord.get_current_transport_info().get("current_transport_state", "UNKNOWN")

    @_refresh_and_retry_once
    def now_playing(self) -> NowPlaying:
        """Return what the active group is playing."""
        coord = self._coordinator()
        info = coord.get_current_track_info()
        members = self._members(coord)
        pos = info.get("playlist_position")
        return NowPlaying(
            state=self._state(coord),
            title=info.get("title") or None,
            artist=info.get("artist") or None,
            album=info.get("album") or None,
            position=info.get("position") or None,
            duration=info.get("duration") or None,
            queue_position=int(pos) if pos and str(pos).isdigit() else None,
            queue_length=coord.queue_size,
            rooms=sorted(s.player_name for s in members),
            volume=self._group_level(members),
            album_art=info.get("album_art") or None,
        )

    @_refresh_and_retry_once
    def queue(self, limit: int = 20) -> list[str]:
        """List the queue as "Title - Artist" lines; see ``Player.queue``."""
        coord = self._coordinator()
        return [f"{getattr(t, 'title', '?')} - {getattr(t, 'creator', '')}".rstrip(" -")
                for t in coord.get_queue(max_items=limit)]
