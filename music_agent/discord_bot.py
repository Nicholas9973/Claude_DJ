"""Discord front-end for the music agent.

Run with ``python -m music_agent.discord_bot``.

- Listens in one channel (DISCORD_CHANNEL_ID) and in DMs, and only to the user
  IDs in DISCORD_ALLOWED_USER_IDS. Everyone else is ignored silently.
- Normal messages go to Claude ("play something upbeat", "less jazz please").
- `!` commands are handled instantly without Claude, using no credit:
  !skip !pause !resume !stop !now !queue !vol 20|+3|-3 !new !taste !help
- Replies that started music get 👍 👎 ⏭️ reactions: tap 👍 to like the current
  track, 👎 to dislike and skip it, ⏭️ to just skip.
- The playback watcher runs the whole time, learning from skips and finished tracks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import sys
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import discord
from dotenv import load_dotenv

from music_agent.agent.session import MusicAgent
from music_agent.agent.tools import ToolError
from music_agent.app import App, build
from music_agent.config import PROJECT_ROOT
from music_agent.logs import setup_logging
from music_agent.models import part_of_day
from music_agent.text import format_minutes

log = logging.getLogger("music_agent.discord")

REACTIONS = {"👍": "like", "👎": "dislike", "⏭️": "skip"}
DISCORD_LIMIT = 1900  # Discord allows 2000 characters; keep a margin

HELP = """**Talk to me normally** - "play something upbeat", "calm music in Night only", \
"more like this", "no more Coldplay", "love this".
**Instant commands** (no AI, no credit):
`!now` what's playing · `!queue` upcoming · `!skip` · `!pause` · `!resume` · `!stop`
`!vol 20` / `!vol +3` / `!vol -3` · `!new` fresh conversation · `!taste` what I've learned
**Reactions** under my music replies: 👍 like this track · 👎 dislike + skip · ⏭️ skip"""


# ----------------------------------------------------------------- settings
@dataclass
class BotSettings:
    """Secrets and IDs from ``.env``.

    Attributes:
        token: Discord bot token.
        allowed_user_ids: Only these users can control the music.
        channel_id: The channel the bot listens in; None means DMs only.
    """

    token: str
    allowed_user_ids: set[int]
    channel_id: int | None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BotSettings:
        """Read and validate settings from environment variables.

        Args:
            env: Variables to read; defaults to ``os.environ``.

        Returns:
            The settings.

        Raises:
            ValueError: Listing every missing or malformed value.
        """
        env = dict(os.environ if env is None else env)
        problems = []
        token = env.get("DISCORD_TOKEN", "").strip()
        if not token or token.startswith("paste-"):
            problems.append("DISCORD_TOKEN is missing")
        ids = set()
        for part in re.split(r"[,\s]+", env.get("DISCORD_ALLOWED_USER_IDS", "").strip()):
            if part:
                if part.isdigit():
                    ids.add(int(part))
                else:
                    problems.append(f"DISCORD_ALLOWED_USER_IDS has a non-numeric value: {part!r}")
        if not ids:
            problems.append("DISCORD_ALLOWED_USER_IDS is empty (nobody could use the bot)")
        raw_channel = env.get("DISCORD_CHANNEL_ID", "").strip()
        channel = None
        if raw_channel:
            if raw_channel.isdigit():
                channel = int(raw_channel)
            else:
                problems.append(f"DISCORD_CHANNEL_ID is not a number: {raw_channel!r}")
        if problems:
            raise ValueError("Problem with .env: " + "; ".join(problems) + ". See .env.example.")
        return cls(token, ids, channel)


# ----------------------------------------------------------------- helpers
def split_message(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split text so each part fits in one Discord message.

    Splits on paragraphs, then lines, then spaces, then anywhere.

    Args:
        text: Message text; empty becomes "(no reply)".
        limit: Maximum characters per part.

    Returns:
        The parts, in order.
    """
    text = text.strip() or "(no reply)"
    parts: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 3:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    parts.append(text)
    return parts


def _minutes(text: str | None) -> int:
    """'58 min' -> 58, '1h55' -> 115, 'unknown' -> 0."""
    if not text:
        return 0
    m = re.fullmatch(r"(\d+)h(\d+)", text.strip())
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.match(r"(\d+)\s*min", text.strip())
    return int(m.group(1)) if m else 0


@dataclass
class TurnSummary:
    """What one agent request did, for the Discord reply.

    Attributes:
        reply: Claude's final words (narration before the last tool call is dropped).
        tracks_queued: Tracks queued by ``play_tracks``.
        minutes: Their total length.
        started_music: Music was queued, so feedback reactions are added.
        seconds: Wall-clock time taken.
        cost_usd: Credit used.
        error: SDK error subtype if the request failed.
    """

    reply: str = ""
    tracks_queued: int = 0
    minutes: int = 0
    started_music: bool = False
    seconds: float = 0.0
    cost_usd: float | None = None
    error: str | None = None

    def footer(self) -> str:
        """Small grey line: tracks, length, time taken and credit used."""
        bits = []
        if self.tracks_queued:
            bits.append(f"{self.tracks_queued} tracks · {format_minutes(self.minutes)}")
        bits.append(f"{self.seconds:.0f}s")
        if self.cost_usd:
            bits.append(f"~${self.cost_usd:.2f} credit")
        return "-# " + " · ".join(bits)  # Discord "subtext" line


async def run_agent(agent: MusicAgent, text: str) -> TurnSummary:
    """Run one request through Claude and summarise what happened.

    Args:
        agent: The conversation.
        text: The person's message.

    Returns:
        The summary used to build the reply.
    """
    summary = TurnSummary()
    started = time.monotonic()
    all_text: list[str] = []
    async for event in agent.ask(text):
        if event.kind == "tool_call":
            summary.reply = ""   # keep only what Claude says after its last action, not "Let me..." narration
        elif event.kind == "tool_result" and not event.is_error:
            # Recognise results by shape, not order (tools may run in parallel).
            try:
                data = json.loads(event.data)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(data, dict):
                continue
            if "tracks" in data and isinstance(data.get("queued"), int) and data["queued"] > 0:
                summary.tracks_queued += data["queued"]               # play_tracks
                summary.minutes += _minutes(data.get("total_duration"))
                summary.started_music = True
            elif isinstance(data.get("queued"), str) and "kind" in data:
                summary.started_music = True                          # play_item (album/playlist)
        elif event.kind == "text":
            all_text.append(event.data.strip())
            summary.reply = f"{summary.reply}\n\n{event.data.strip()}".strip()
        elif event.kind == "done":
            turn = event.data
            summary.cost_usd = turn.cost_usd
            if event.is_error:
                summary.error = getattr(turn.result, "subtype", "error")
    if not summary.reply and all_text:
        summary.reply = "\n\n".join(all_text)
    summary.seconds = time.monotonic() - started
    return summary


# ----------------------------------------------------------------- bot
class MusicBot(discord.Client):
    """Discord client that passes allowed users' messages to the agent.

    Args:
        settings: Token and access settings.
        app: Wired-up parts; defaults to ``app.build()``.
        agent: The conversation; defaults to a new ``MusicAgent``.
    """

    def __init__(self, settings: BotSettings, app: App | None = None, agent: MusicAgent | None = None):
        intents = discord.Intents.default()
        intents.message_content = True  # also enable "Message Content Intent" in the developer portal
        super().__init__(intents=intents)
        self.settings = settings
        self.app = app or build()
        self.agent = agent or MusicAgent(self.app)
        self.cfg = self.app.config.discord
        self._lock = asyncio.Lock()           # one Claude request at a time
        self._last_request = 0.0
        self._turns = 0
        self._music_messages: deque[int] = deque(maxlen=200)  # replies that carry feedback reactions
        self._ready_once = False

    # ---------------------------------------------------------- lifecycle
    async def setup_hook(self) -> None:
        """Start the agent before connecting to Discord."""
        await self.agent.start()
        log.info("Agent started (model: %s)", self.agent.model)

    async def close(self) -> None:
        """Stop the agent, then disconnect (the runtime owns the playback watcher)."""
        await self.agent.close()
        await super().close()

    async def on_ready(self) -> None:
        """Log the first login; later calls are reconnects."""
        # Fires again after every reconnect (sleep, network blips), not just at startup.
        if self._ready_once:
            log.info("Reconnected to Discord.")
            return
        self._ready_once = True
        log.info("Logged in as %s; listening in channel %s and DMs from %d allowed user(s)",
                 self.user, self.settings.channel_id or "(none: DMs only)", len(self.settings.allowed_user_ids))

    # ---------------------------------------------------------- access
    def allowed(self, user_id: int, channel_id: int | None, is_dm: bool) -> bool:
        """Check whether to act on a message or reaction.

        Args:
            user_id: Who sent it.
            channel_id: Where it was sent.
            is_dm: It's a direct message.

        Returns:
            True for allowed users in a DM or the configured channel.
        """
        if user_id not in self.settings.allowed_user_ids:
            return False
        return is_dm or (self.settings.channel_id is not None and channel_id == self.settings.channel_id)

    # ---------------------------------------------------------- messages
    async def on_message(self, message: Any) -> None:
        """Route allowed messages: ``!`` commands run directly, everything else goes to Claude."""
        if message.author.bot:
            return
        is_dm = getattr(message, "guild", None) is None
        if not self.allowed(message.author.id, message.channel.id, is_dm):
            return
        text = (message.content or "").strip()
        if not text:
            return
        if text.startswith("!"):
            await self.handle_command(message, text)
        else:
            await self.handle_request(message, text)

    async def _maybe_reset_conversation(self) -> None:
        """Start a fresh conversation after a long idle or many turns (keeps requests cheap)."""
        idle = time.monotonic() - self._last_request if self._last_request else 0
        if self._turns and (idle > self.cfg.idle_reset_minutes * 60
                            or self._turns >= self.cfg.max_turns_per_conversation):
            log.info("Starting a fresh conversation (idle %.0f min, %d turns)", idle / 60, self._turns)
            await self.agent.reset()
            self._turns = 0

    async def handle_request(self, message: Any, text: str) -> None:
        """Answer one message with the agent, one request at a time.

        Args:
            message: The Discord message to reply to.
            text: Its content.
        """
        if self._lock.locked():
            await _safe(message.add_reaction("⏳"))
        async with self._lock:
            await self._maybe_reset_conversation()
            try:
                async with message.channel.typing():
                    summary = await run_agent(self.agent, text)
            except Exception as exc:
                log.exception("Agent request failed")
                await message.reply(f"Sorry, something went wrong: {exc}", mention_author=False)
                await _safe(self.agent.reset())  # a broken session shouldn't poison the next request
                self._turns = 0
                return
            finally:
                self._last_request = time.monotonic()
            self._turns += 1

        reply = summary.reply.strip() or ("Something went wrong on my side; please try again."
                                          if summary.error else "Done.")
        chunks = split_message(f"{reply}\n{summary.footer()}")
        last = None
        for i, chunk in enumerate(chunks):
            last = await (message.reply(chunk, mention_author=False) if i == 0 else message.channel.send(chunk))
        if last is not None and summary.started_music and self.cfg.feedback_reactions:
            self._music_messages.append(last.id)
            for emoji in REACTIONS:
                await _safe(last.add_reaction(emoji))

    # ---------------------------------------------------------- reactions
    async def on_raw_reaction_add(self, payload: Any) -> None:
        """Handle 👍 👎 ⏭️ on music replies from allowed users."""
        if self.user and payload.user_id == self.user.id:
            return
        if payload.message_id not in self._music_messages:
            return
        is_dm = payload.guild_id is None
        if not self.allowed(payload.user_id, payload.channel_id, is_dm):
            return
        action = REACTIONS.get(str(payload.emoji))
        if not action:
            return
        text = await self.reaction_action(action)
        channel = self.get_channel(payload.channel_id) or await self.fetch_channel(payload.channel_id)
        await channel.send(text, delete_after=60)

    async def reaction_action(self, action: str) -> str:
        """Apply a feedback reaction to the current track.

        Args:
            action: ``"like"``, ``"dislike"`` (also skips) or ``"skip"``.

        Returns:
            Short confirmation or error text.
        """
        tools = self.agent.tools
        try:
            if action == "skip":
                await tools.playback_control("skip")
                return "⏭️ Skipped."
            saved = await tools.record_feedback(action, "current_track")
            if action == "dislike":
                await tools.playback_control("skip")
                return f"👎 Noted, and skipped: *{saved['target']}*"
            return f"👍 Noted: you like *{saved['target']}*"
        except ToolError as exc:
            return f"Couldn't do that: {exc}"

    # ---------------------------------------------------------- commands
    async def handle_command(self, message: Any, text: str) -> None:
        """Reply to a ``!`` command."""
        reply = await self.command(text)
        for chunk in split_message(reply):
            await message.reply(chunk, mention_author=False)

    async def command(self, text: str) -> str:
        """Run a ``!`` command without Claude (no credit used).

        Args:
            text: The message, e.g. ``"!vol +3"``.

        Returns:
            The reply text.
        """
        cmd, _, arg = text[1:].strip().partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        tools = self.agent.tools
        try:
            if cmd in ("help", "h", "?"):
                return HELP
            if cmd in ("skip", "next", "pause", "resume", "play", "stop"):
                action = {"next": "skip", "play": "resume"}.get(cmd, cmd)
                await tools.playback_control(action)
                return {"skip": "⏭️ Skipped.", "pause": "⏸️ Paused.", "resume": "▶️ Resumed.",
                        "stop": "⏹️ Stopped."}[action]
            if cmd in ("vol", "volume"):
                if not re.fullmatch(r"[+-]?\d{1,3}", arg):
                    return "Usage: `!vol 20`, `!vol +3` or `!vol -3`"
                out = (await tools.set_volume(change=int(arg)) if arg[0] in "+-"
                       else await tools.set_volume(level=int(arg)))
                return f"🔊 Volume {out['volume']}" + (f" ({out['note']})" if out.get("note") else "")
            if cmd in ("now", "np"):
                ctx = await tools.get_context()
                pb = ctx.get("playback", {})
                if "error" in pb:
                    return f"Couldn't reach the speakers: {pb['error']}"
                current = pb.get("current") or "nothing"
                return (f"**{pb.get('state', '?').replace('_PLAYBACK', '').title()}**: {current}\n"
                        f"Track {pb.get('queue_position') or '-'} of {pb.get('queue_length', 0)} · "
                        f"volume {pb.get('volume')} · {', '.join(pb.get('rooms_in_group', []))}")
            if cmd in ("queue", "q"):
                out = await tools.get_queue(15)
                pos = out.get("current_position") or 0
                lines = [f"{'▶' if i == pos else f'{i}.'} {t}" for i, t in enumerate(out["queue"], 1)]
                return "\n".join(lines) or "The queue is empty."
            if cmd in ("new", "reset"):
                async with self._lock:
                    await self.agent.reset()
                    self._turns = 0
                return "🆕 Fresh conversation started (your taste profile is kept)."
            if cmd == "taste":
                if not self.app.memory:
                    return "Memory is turned off."
                p = await asyncio.to_thread(self.app.memory.taste_profile,
                                            part_of_day(time.localtime().tm_hour))
                return format_taste(p)
            return f"Unknown command `!{cmd}`. Try `!help`."
        except ToolError as exc:
            return f"Couldn't do that: {exc}"


def format_taste(p: dict[str, Any]) -> str:
    """Format a taste profile for Discord.

    Args:
        p: Output of ``Memory.taste_profile``.

    Returns:
        A few bold-labelled lines plus a totals footer.
    """
    def line(label: str, key: str, n: int = 10) -> str | None:
        items = p.get(key) or []
        return f"**{label}**: {', '.join(items[:n])}" if items else None
    lines = [
        line("Your preferences", "preferences", 8),
        line("Liked artists", "liked_artists"),
        line("Avoiding artists", "avoid_artists"),
        line("Liked tracks", "liked_tracks", 6),
        line("Avoiding tracks", "avoid_tracks", 6),
        line("Your notes", "notes", 6),
    ]
    h = p.get("history", {})
    lines.append(f"-# {h.get('tracks_played', 0)} tracks played · {h.get('feedback_given', 0)} feedback · "
                 f"{h.get('skips_seen', 0)} skips seen")
    return "\n".join(x for x in lines if x)


async def _safe(coro: Awaitable[Any]) -> None:
    """Best-effort Discord calls (e.g. reactions) must never break a reply."""
    try:
        await coro
    except Exception as exc:
        log.debug("ignored: %s", exc)


# ----------------------------------------------------------------- entry point
class BotRunner:
    """Starts, stops and reports on the Discord bot inside a running event loop.

    Used by the runtime, so the GUI can show the bot's status and restart it.

    Args:
        app: Shared parts (speakers, memory, ...).
        agent_factory: Makes the bot's conversation from ``app``.
        env_file: Where ``DISCORD_*`` settings are read from.
    """

    def __init__(self, app: App, agent_factory: Callable[[App], MusicAgent],
                 env_file: Path = PROJECT_ROOT / ".env"):
        self.app = app
        self._agent_factory = agent_factory
        self._env_file = env_file
        self.bot: MusicBot | None = None
        self.problem: str | None = None
        self._state = "off"
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        """True while the bot task is alive (connecting or online)."""
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        """Return the bot's state for display.

        Returns:
            ``state`` is one of ``off``, ``not_configured``, ``connecting``,
            ``online``, ``stopped`` or ``error``; ``detail`` explains it.
        """
        if self.running and self.bot is not None:
            if self.bot.is_ready():
                return {"state": "online", "detail": f"Logged in as {self.bot.user}"}
            return {"state": "connecting", "detail": "Connecting to Discord..."}
        if self.problem:
            return {"state": self._state, "detail": self.problem}
        return {"state": self._state, "detail": None}

    def start(self) -> bool:
        """Read ``.env`` and start the bot in the background.

        Returns:
            False if the settings are missing or invalid (see ``problem``).
        """
        if self.running:
            return True
        load_dotenv(self._env_file, override=True)  # pick up edits made since the last start
        try:
            settings = BotSettings.from_env()
        except ValueError as exc:
            self._state, self.problem = "not_configured", str(exc)
            log.warning("Discord bot not started: %s", exc)
            return False
        self._state, self.problem = "connecting", None
        self.bot = MusicBot(settings, app=self.app, agent=self._agent_factory(self.app))
        self._task = asyncio.create_task(self._run(self.bot, settings.token), name="discord-bot")
        return True

    async def stop(self) -> None:
        """Disconnect the bot and wait for it to finish."""
        if self.bot is not None and not self.bot.is_closed():
            await self.bot.close()
        if self._task is not None:
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await self._task
        self._task = None
        if self._state != "error":
            self._state = "stopped"

    async def wait(self) -> None:
        """Wait until the bot stops by itself (login failure, Ctrl+C)."""
        if self._task is not None:
            await self._task

    async def _run(self, bot: MusicBot, token: str) -> None:
        """Run the bot until it's closed, recording why it stopped."""
        try:
            async with bot:
                await bot.start(token)
        except discord.LoginFailure:
            self.problem = "Discord rejected the token. Reset it in the developer portal and update .env."
        except discord.PrivilegedIntentsRequired:
            self.problem = ("Turn on 'Message Content Intent' for the bot in the Discord developer "
                            "portal (Bot page).")
        except Exception as exc:
            log.exception("Discord bot crashed")
            self.problem = f"The bot stopped unexpectedly: {exc}"
        if self.problem:
            self._state = "error"
            log.error("%s", self.problem)
        else:
            self._state = "stopped"


async def main_async() -> int:
    """Run the bot (and the playback watcher) until it stops.

    Returns:
        Exit code: 2 for configuration or login problems.
    """
    # Imported here because runtime.py imports this module.
    from music_agent.runtime import Runtime

    runtime = Runtime(build())
    await runtime.start()
    try:
        if not runtime.bot.start():
            return 2
        await runtime.bot.wait()
        return 2 if runtime.bot.problem else 0
    finally:
        await runtime.stop()


def main() -> int:
    """Entry point for ``python -m music_agent.discord_bot`` (``-v`` for debug logs)."""
    setup_logging("-v" in sys.argv or "--verbose" in sys.argv, level=logging.INFO,
                  logfile=PROJECT_ROOT / "data" / "logs" / "bot.log")
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
