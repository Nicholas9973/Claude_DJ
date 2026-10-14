"""Load ``config.toml`` (overlaid with ``config.local.toml``) into typed settings."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"


@dataclass
class SafetyConfig:
    """Limits the player enforces in code; the agent cannot override them.

    Attributes:
        max_volume: Highest volume ever applied.
        quiet_start: Start of quiet hours (local time).
        quiet_end: End of quiet hours; may be earlier than ``quiet_start``
            for a window that crosses midnight.
        quiet_mode: ``"block"`` refuses to start music in quiet hours;
            ``"cap"`` allows it at ``quiet_max_volume``.
        quiet_max_volume: Volume cap in quiet hours when ``quiet_mode`` is ``"cap"``.
    """

    max_volume: int = 35
    quiet_start: time = time(23, 0)
    quiet_end: time = time(8, 0)
    quiet_mode: str = "block"
    quiet_max_volume: int = 15

    def in_quiet_hours(self, now: time) -> bool:
        """Check whether a time falls inside quiet hours.

        Args:
            now: Local time of day.

        Returns:
            True inside the window. Always False if start equals end.
        """
        start, end = self.quiet_start, self.quiet_end
        if start == end:
            return False
        if start < end:
            return start <= now < end
        return now >= start or now < end  # window crosses midnight

    def volume_cap(self, now: time) -> int:
        """Return the highest volume allowed at a given time.

        Args:
            now: Local time of day.

        Returns:
            ``max_volume``, or the lower quiet-hours cap in "cap" mode.
        """
        if self.quiet_mode == "cap" and self.in_quiet_hours(now):
            return min(self.max_volume, self.quiet_max_volume)
        return self.max_volume


@dataclass
class SonosConfig:
    """Speaker settings.

    Attributes:
        default_rooms: ``"all"``, a comma-separated string or a list of room names.
        volume_source: Room whose volume the whole group follows; also the
            preferred group coordinator.
        hosts: Fixed speaker IPs. Empty means discover them on the network.
        discovery_timeout: Seconds to wait for network discovery.
    """

    default_rooms: str | list[str] = "all"
    volume_source: str = "Dining Room"
    hosts: list[str] = field(default_factory=list)
    discovery_timeout: int = 5


@dataclass
class MusicConfig:
    """Music service settings.

    Attributes:
        service: Name of a service linked in the Sonos app, e.g. ``"Qobuz"``.
    """

    service: str = "Qobuz"


@dataclass
class AgentConfig:
    """Claude agent settings.

    Attributes:
        model: The DJ's model: ``"sonnet"``, ``"haiku"``, ``"opus"`` or a full model id.
        curator_model: The curator's model (it runs less often, so a stronger one).
        orchestrator_model: The model of the orchestrator, which runs the day plan.
        max_turns: Safety net against runaway tool loops.
        require_subscription: Ignore ``ANTHROPIC_API_KEY`` so usage goes to
            the Claude subscription.
        log_dir: Folder (relative to the project) for the per-request JSONL log.
    """

    model: str = "sonnet"
    curator_model: str = "opus"
    orchestrator_model: str = "sonnet"
    max_turns: int = 20
    require_subscription: bool = True
    log_dir: str = "data/logs"

    def model_for(self, role: str) -> str:
        """The configured model for an agent role (``"dj"``, ``"curator"`` or ``"orchestrator"``)."""
        return {"curator": self.curator_model, "orchestrator": self.orchestrator_model}.get(role, self.model)


@dataclass
class LocationConfig:
    """Where the speakers are, for weather. Give a city or exact coordinates.

    Attributes:
        city: Town name, e.g. ``"Lyon"``.
        latitude: Latitude in degrees, used instead of ``city`` when set.
        longitude: Longitude in degrees, used instead of ``city`` when set.
    """

    city: str = ""
    latitude: float | None = None
    longitude: float | None = None


@dataclass
class MemoryConfig:
    """Long-term memory settings.

    Attributes:
        path: SQLite file, relative to the project.
        half_life_days: Feedback loses half its weight after this many days.
        watch_interval_s: How often the watcher checks what's playing.
    """

    path: str = "data/music.db"
    half_life_days: float = 60.0
    watch_interval_s: float = 15.0


@dataclass
class DiscordConfig:
    """Discord bot settings (the token and IDs live in ``.env``).

    Attributes:
        idle_reset_minutes: Start a fresh conversation after this much quiet.
        max_turns_per_conversation: ...or after this many requests, which
            keeps each request cheap.
        feedback_reactions: Add 👍 👎 ⏭️ under replies that started music.
    """

    idle_reset_minutes: int = 60
    max_turns_per_conversation: int = 20
    feedback_reactions: bool = True


@dataclass
class GuiConfig:
    """Web GUI settings.

    Attributes:
        host: Address to serve on. Keep ``127.0.0.1`` (this PC only): the GUI
            has no login and controls the speakers.
        port: Port to serve on.
        open_browser: Open the GUI in the browser on start.
        start_bot: Also start the Discord bot.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    open_browser: bool = True
    start_bot: bool = True


@dataclass
class Config:
    """All settings, one section per ``[table]`` in ``config.toml``."""

    music: MusicConfig = field(default_factory=MusicConfig)
    sonos: SonosConfig = field(default_factory=SonosConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    location: LocationConfig = field(default_factory=LocationConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    discord: DiscordConfig = field(default_factory=DiscordConfig)
    gui: GuiConfig = field(default_factory=GuiConfig)


def load_config(path: str | Path | None = None, local: str | Path | None = None) -> Config:
    """Load settings from TOML files.

    ``config.toml`` (shared, committed) is overlaid with ``config.local.toml``
    (personal, git-ignored); missing files and keys fall back to defaults.

    Args:
        path: Main config file. Defaults to ``$MUSIC_AGENT_CONFIG``, then
            ``config.toml`` in the project root.
        local: Override file. Defaults to ``config.local.toml`` next to ``path``.

    Returns:
        The merged settings.

    Raises:
        ValueError: If ``safety.quiet_mode`` is not ``"block"`` or ``"cap"``.
        TypeError: If a section contains an unknown key.
    """
    path = Path(path or os.environ.get("MUSIC_AGENT_CONFIG") or DEFAULT_CONFIG)
    raw = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    local = Path(local) if local else path.with_name("config.local.toml")
    if local.exists():
        raw = _merge(raw, tomllib.loads(local.read_text(encoding="utf-8")))

    safety = dict(raw.get("safety", {}))
    for key in ("quiet_start", "quiet_end"):
        if key in safety:
            safety[key] = _parse_time(safety[key])
    if safety.get("quiet_mode", "block") not in ("block", "cap"):
        raise ValueError("safety.quiet_mode must be 'block' or 'cap'")

    return Config(
        music=MusicConfig(**raw.get("music", {})),
        sonos=SonosConfig(**raw.get("sonos", {})),
        safety=SafetyConfig(**safety),
        agent=AgentConfig(**raw.get("agent", {})),
        location=LocationConfig(**raw.get("location", {})),
        memory=MemoryConfig(**raw.get("memory", {})),
        discord=DiscordConfig(**raw.get("discord", {})),
        gui=GuiConfig(**raw.get("gui", {})),
    )


def _parse_time(value: str) -> time:
    """Parse ``"HH:MM"`` into a time."""
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


def _merge(base: dict, override: dict) -> dict:
    """Deep-merge two dicts; values in ``override`` win."""
    out = dict(base)
    for key, value in override.items():
        out[key] = _merge(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out
