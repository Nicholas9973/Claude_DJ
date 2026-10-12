"""The long-running service that every front-end shares.

One ``Runtime`` owns the speakers, memory and playback watcher (via ``App``),
the Discord bot, and any number of named conversations with Claude. The GUI
and the Discord bot are just two ways of talking to it, so they see the same
speakers and the same memory and never run two watchers.

To add a new front-end, give it a ``Runtime`` and use ``conversation(name, role)``
for its own chat with Claude, and ``tools`` / ``curator`` for direct, AI-free
controls with the same validation the agents get.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from music_agent.agent.curator_tools import CuratorTools
from music_agent.agent.describe import describe_call, describe_result
from music_agent.agent.session import Event, MusicAgent
from music_agent.agent.tools import MusicTools
from music_agent.app import App
from music_agent.discord_bot import BotRunner

log = logging.getLogger(__name__)

AgentFactory = Callable[..., MusicAgent]  # (app, role="dj") -> agent; MusicAgent itself fits
HISTORY_LIMIT = 100  # messages kept per conversation for the GUI


class Conversation:
    """One named conversation with Claude, safe to use from several requests.

    Requests run one at a time. Each request runs in its own task, so it
    finishes (and is recorded) even if whoever asked stops listening, e.g.
    the browser tab is closed mid-reply.

    Args:
        name: Label, e.g. ``"gui"``.
        agent: The conversation with Claude.

    Attributes:
        history: Messages so far, oldest first: ``{"role": "user", "text"}``
            and ``{"role": "agent", "text", "steps", ...}``.
    """

    def __init__(self, name: str, agent: MusicAgent):
        self.name = name
        self.agent = agent
        self.history: list[dict[str, Any]] = []
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    @property
    def busy(self) -> bool:
        """True while a request is being handled."""
        return self._lock.locked()

    async def ask(self, text: str) -> AsyncIterator[dict[str, Any]]:
        """Send a request and stream what happens as plain dicts.

        Args:
            text: The person's message.

        Yields:
            ``{"type": "tool_call" | "tool_result" | "text" | "done" | "error", ...}``.
        """
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        task = asyncio.create_task(self._run(text, queue), name=f"conversation-{self.name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        while (item := await queue.get()) is not None:
            yield item

    async def reset(self) -> None:
        """Start a fresh conversation (the taste profile is kept)."""
        async with self._lock:
            await self.agent.reset()
            self.history.clear()

    async def close(self) -> None:
        """Disconnect from Claude."""
        await self.agent.close()

    async def _run(self, text: str, queue: asyncio.Queue) -> None:
        """Handle one request, feeding events to ``queue`` and recording them in ``history``."""
        async with self._lock:
            self._remember({"role": "user", "text": text, "time": _now()})
            reply: dict[str, Any] = {"role": "agent", "text": "", "steps": [], "time": _now()}
            started = time.monotonic()
            try:
                async for event in self.agent.ask(text):
                    item = event_to_dict(event)
                    if item["type"] in ("tool_call", "tool_result"):
                        reply["steps"].append(item)
                    elif item["type"] == "text":
                        reply["text"] = f"{reply['text']}\n\n{item['text']}".strip()
                    elif item["type"] == "done":
                        reply.update(cost_usd=item["cost_usd"], error=item["error"])
                    queue.put_nowait(item)
            except Exception as exc:  # a broken session shouldn't poison the next request
                log.exception("Conversation %s failed", self.name)
                reply["error"] = str(exc)
                queue.put_nowait({"type": "error", "message": str(exc)})
                with contextlib.suppress(Exception):
                    await self.agent.reset()
            finally:
                reply["seconds"] = round(time.monotonic() - started, 1)
                self._remember(reply)
                queue.put_nowait(None)

    def _remember(self, message: dict[str, Any]) -> None:
        """Append to history, keeping it bounded."""
        self.history.append(message)
        del self.history[:-HISTORY_LIMIT]


class Runtime:
    """Everything that runs for as long as the program runs.

    Args:
        app: Shared parts (speakers, memory, watcher, ...).
        agent_factory: Makes a conversation with Claude; replaced in tests.

    Attributes:
        tools: Direct controls (no Claude), with the same safety rules and
            validation the agent gets.
        curator: Direct taste and playlist editing (no Claude), as the curator does it.
        bot: The Discord bot's lifecycle.
        log_file: Where this process logs, if anywhere (shown in the GUI).
    """

    def __init__(self, app: App, agent_factory: AgentFactory = MusicAgent):
        self.app = app
        self._agent_factory = agent_factory
        self.tools = MusicTools(app.config, app.player, app.provider, memory=app.memory,
                                weather=app.weather, watcher=app.watcher, playlists=app.playlists)
        self.tools.source = "gui"
        self.curator = CuratorTools(self.tools, app.trackinfo)
        self.bot = BotRunner(app, agent_factory)
        self.started_at = datetime.now()
        self.log_file: Path | None = None
        self._conversations: dict[str, Conversation] = {}

    def conversation(self, name: str, role: str = "dj") -> Conversation:
        """Return the named conversation, creating it on first use.

        Args:
            name: Any label; each name is a separate chat with Claude.
            role: Agent for a new conversation: ``"dj"`` or ``"curator"``.
        """
        if name not in self._conversations:
            self._conversations[name] = Conversation(name, self._agent_factory(self.app, role=role))
        return self._conversations[name]

    async def start(self) -> None:
        """Start background work (the playback watcher)."""
        if self.app.watcher:
            self.app.watcher.start()

    async def stop(self) -> None:
        """Stop the bot, the conversations and the watcher."""
        await self.bot.stop()
        for conv in self._conversations.values():
            with contextlib.suppress(Exception):
                await conv.close()
        if self.app.watcher:
            await self.app.watcher.stop()


def event_to_dict(event: Event) -> dict[str, Any]:
    """Turn an agent event into JSON-friendly data with a one-line summary.

    Args:
        event: From ``MusicAgent.ask``.

    Returns:
        A dict with ``type`` plus fields for that type.
    """
    if event.kind == "tool_call":
        return {"type": "tool_call", "name": event.name, "input": event.data,
                "summary": describe_call(event.name, event.data or {})}
    if event.kind == "tool_result":
        return {"type": "tool_result", "is_error": event.is_error, "summary": describe_result(event.data)}
    if event.kind == "text":
        return {"type": "text", "text": event.data.strip()}
    turn = event.data
    result = getattr(turn, "result", None)
    return {
        "type": "done",
        "cost_usd": getattr(turn, "cost_usd", None),
        "turns": getattr(result, "num_turns", None),
        "error": getattr(result, "subtype", "error") if event.is_error else None,
    }


def _now() -> str:
    """Local time as ISO text, for history entries."""
    return datetime.now().isoformat(timespec="seconds")
