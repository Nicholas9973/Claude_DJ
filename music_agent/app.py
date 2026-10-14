"""Wire config, provider, player, memory, weather and the playback watcher together."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from music_agent.config import PROJECT_ROOT, Config, load_config
from music_agent.dayplan import DAY_PLAN_PATH
from music_agent.learning.memory import Memory
from music_agent.learning.playlists import PlaylistStore
from music_agent.learning.watcher import PlaybackWatcher
from music_agent.players.balance import SpeakerBalance
from music_agent.players.sonos import SonosPlayer
from music_agent.providers.base import MusicProvider
from music_agent.providers.sonos_service import SonosServiceProvider
from music_agent.trackinfo import TrackInfoService
from music_agent.weather import WeatherService


@dataclass
class App:
    """Everything the entry points need, built once.

    Attributes:
        config: Loaded settings.
        player: Controls the speakers.
        provider: Searches the music service.
        memory: Long-term memory, or None when turned off.
        weather: Local weather, or None.
        watcher: Learns from skips and finished tracks; None without memory.
        playlists: Saved playlists (in the memory's database); None without memory.
        trackinfo: Looks up track qualities online for the curator (cached in
            the memory's database); None without memory.
        day_plan_path: The day plan the orchestrator follows (``day_plan.toml``).
    """

    config: Config
    player: SonosPlayer
    provider: MusicProvider
    memory: Memory | None = None
    weather: WeatherService | None = None
    watcher: PlaybackWatcher | None = None
    playlists: PlaylistStore | None = None
    trackinfo: TrackInfoService | None = None
    day_plan_path: Path = DAY_PLAN_PATH


def build(config: Config | None = None, with_memory: bool = True) -> App:
    """Create and connect all the parts.

    Nothing touches the network here; speakers are discovered on first use.

    Args:
        config: Settings to use. Defaults to ``load_config()``.
        with_memory: Open the SQLite memory and playlists, and create the playback watcher.

    Returns:
        The wired-up app.
    """
    config = config or load_config()
    player = SonosPlayer(config)
    provider = SonosServiceProvider(config.music.service, device_fn=player.any_speaker)
    player.provider = provider
    app = App(config, player, provider)
    loc = config.location
    app.weather = WeatherService(loc.city, loc.latitude, loc.longitude)
    if with_memory:
        app.memory = Memory(PROJECT_ROOT / config.memory.path, config.memory.half_life_days)
        app.watcher = PlaybackWatcher(player, app.memory, config.memory.watch_interval_s)
        app.playlists = PlaylistStore(app.memory.db)
        app.trackinfo = TrackInfoService(app.memory.db)
        player.balance = SpeakerBalance(app.memory.db)   # remember per-room volumes across restarts
    return app
