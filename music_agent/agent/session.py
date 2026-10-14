"""The Claude agent: SDK options, one conversation session, and the per-request log.

Security model: the agent gets NO built-in Claude Code tools (no shell, no
files, no web). It can only call the music tools from ``server.py``, and every
safety limit is enforced inside the Player, not in the prompt.

The same class runs every agent; ``role`` picks the prompt and the tool set:
``"dj"`` plays music, ``"curator"`` shapes taste, builds playlists and edits the
day plan, ``"orchestrator"`` follows the day plan by asking the DJ for music.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from music_agent.agent.curator_tools import CuratorTools
from music_agent.agent.dayplan_tools import AskDJ, DayPlanTools
from music_agent.agent.prompt import build_system_prompt
from music_agent.agent.server import ROLE_TOOLS, SERVER, TOOL_PREFIX, build_server
from music_agent.agent.tools import MusicTools
from music_agent.app import App, build
from music_agent.config import PROJECT_ROOT

log = logging.getLogger(__name__)
WORKDIR = PROJECT_ROOT / "data"  # the agent has no file tools; this just keeps it out of the repo


def use_subscription_login() -> None:
    """Make the bundled Claude Code use your subscription login, not an API key.

    If ``ANTHROPIC_API_KEY`` is set, Claude Code bills that API account
    instead, so it is removed from this process's environment.
    """
    if os.environ.pop("ANTHROPIC_API_KEY", None):
        log.warning("ANTHROPIC_API_KEY was set; ignoring it so usage goes to your Claude subscription.")


def model_for(app: App, role: str) -> str:
    """The Claude model an agent role uses: the System page's choice, else config.toml.

    Args:
        app: Wired-up parts (config and memory).
        role: ``"dj"`` or ``"curator"``.
    """
    chosen = app.memory.agent_model(role) if app.memory else None
    return chosen or app.config.agent.model_for(role)


@dataclass
class Event:
    """Something that happened while the agent handled a request.

    Attributes:
        kind: ``"tool_call"``, ``"tool_result"``, ``"text"`` or ``"done"``.
        name: Tool name, for ``"tool_call"``.
        data: Tool input (dict), result text, reply text, or the ``Turn`` when done.
        is_error: The tool call or the whole request failed.
    """

    kind: str
    name: str = ""
    data: Any = None
    is_error: bool = False


@dataclass
class Turn:
    """Record of one request, written to ``agent.jsonl``.

    Attributes:
        prompt: What the person asked.
        tool_calls: ``{"tool", "input", "results"}`` for each call, in order.
        reply: Everything Claude said.
        result: The SDK's final message (turns, timing, usage).
        cost_usd: Credit used by this request only (the SDK reports a running total).
    """

    prompt: str
    tool_calls: list[dict] = field(default_factory=list)
    reply: str = ""
    result: ResultMessage | None = None
    cost_usd: float | None = None


class MusicAgent:
    """One conversation with Claude. Keep it open for follow-ups ("more like that").

    Args:
        app: Wired-up parts; defaults to ``app.build()``.
        role: ``"dj"`` (plays music) or ``"curator"`` (preferences, quizzes, playlists).
        model: Use this model, whatever the config or the System page say
            (``dj-chat --model``).
        ask_dj: How the orchestrator hands requests to the DJ (from the runtime).

    Raises:
        ValueError: Unknown role.
    """

    def __init__(self, app: App | None = None, role: str = "dj", model: str | None = None,
                 ask_dj: AskDJ | None = None):
        if role not in ROLE_TOOLS:
            raise ValueError(f"role must be one of {sorted(ROLE_TOOLS)}")
        self.app = app or build()
        self.role = role
        self.config = self.app.config
        self.tools = MusicTools(self.config, self.app.player, self.app.provider,
                                memory=self.app.memory, weather=self.app.weather,
                                watcher=self.app.watcher, playlists=self.app.playlists)
        self.tools.source = role
        self.curator = CuratorTools(self.tools, self.app.trackinfo) if role == "curator" else None
        self.day = (DayPlanTools(self.tools, self.app.day_plan_path, ask_dj)
                    if role in ("curator", "orchestrator") else None)
        self.memory = self.app.memory
        self._model_override = model
        self._model = ""                     # the model the open conversation uses
        self._client: ClaudeSDKClient | None = None
        self._session_cost = 0.0
        self.log_path = PROJECT_ROOT / self.config.agent.log_dir / "agent.jsonl"

    @property
    def model(self) -> str:
        """The model for the next request (a change on the System page applies right away)."""
        return self._model_override or model_for(self.app, self.role)

    def options(self) -> ClaudeAgentOptions:
        """Build the locked-down SDK options (checked by ``tests/test_session.py``)."""
        server, allowed = build_server(self.tools, self.curator, self.role, self.day)
        try:
            rooms = self.app.player.rooms()
        except Exception:
            rooms = []
        return ClaudeAgentOptions(
            system_prompt=build_system_prompt(self.config, rooms, self.role),
            model=self.model,
            mcp_servers={SERVER: server},
            tools=[],                                   # no built-in tools at all
            allowed_tools=allowed,
            permission_mode="dontAsk",                  # anything not allowed is denied
            setting_sources=[],                         # ignore ~/.claude settings, CLAUDE.md, plugins
            max_turns=self.config.agent.max_turns,
            cwd=str(WORKDIR),
        )

    async def start(self) -> None:
        """Connect to Claude Code and start a new conversation."""
        if self.config.agent.require_subscription:
            use_subscription_login()
        WORKDIR.mkdir(parents=True, exist_ok=True)
        options = self.options()
        self._model = options.model
        self._client = ClaudeSDKClient(options=options)
        self._session_cost = 0.0
        await self._client.connect()

    async def close(self) -> None:
        """Disconnect, if connected."""
        if self._client:
            await self._client.disconnect()
            self._client = None

    async def reset(self) -> None:
        """Forget the conversation and start a fresh one (memory is kept)."""
        await self.close()
        await self.start()

    async def ask(self, text: str) -> AsyncIterator[Event]:
        """Send one request and stream what happens.

        Args:
            text: The person's message.

        Yields:
            Tool calls, tool results and reply text as they happen, then one
            ``"done"`` event whose data is the ``Turn``.
        """
        if self._client is None:
            await self.start()
        elif self.model != self._model:     # chosen on the System page mid-conversation
            await self._client.set_model(self.model)
            self._model = self.model
        turn = Turn(prompt=text)
        if self.memory:
            self.tools.request_id = self.memory.start_request(text, self._model, agent=self.role)
        await self._client.query(text)
        async for msg in self._client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        name = block.name.removeprefix(TOOL_PREFIX)
                        turn.tool_calls.append({"tool": name, "input": block.input})
                        yield Event("tool_call", name, block.input)
                    elif isinstance(block, TextBlock) and block.text.strip():
                        turn.reply += block.text
                        yield Event("text", data=block.text)
            elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                for block in msg.content:
                    if isinstance(block, ToolResultBlock):
                        text = _result_text(block.content)
                        if turn.tool_calls:
                            turn.tool_calls[-1].setdefault("results", []).append(
                                {"error": bool(block.is_error), "output": text[:2000]})
                        yield Event("tool_result", data=text, is_error=bool(block.is_error))
            elif isinstance(msg, ResultMessage):
                turn.result = msg
                if msg.total_cost_usd is not None:
                    turn.cost_usd = max(0.0, msg.total_cost_usd - self._session_cost)
                    self._session_cost = msg.total_cost_usd
                self._write_log(turn)
                if self.memory and self.tools.request_id:
                    self.memory.finish_request(self.tools.request_id, turn.reply, turn.cost_usd)
                yield Event("done", data=turn, is_error=msg.is_error)

    def _write_log(self, turn: Turn) -> None:
        """Append one JSON line describing the request to ``agent.jsonl``."""
        r = turn.result
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "agent": self.role,
            "model": self._model,
            "prompt": turn.prompt,
            "tool_calls": turn.tool_calls,
            "reply": turn.reply,
            "turns": r.num_turns if r else None,
            "duration_s": round(r.duration_ms / 1000, 1) if r else None,
            "cost_usd_equivalent": round(turn.cost_usd, 4) if turn.cost_usd is not None else None,
            "usage": r.usage if r else None,
            "error": r.is_error if r else None,
        }
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        except OSError as exc:
            log.warning("Could not write agent log: %s", exc)


def _result_text(content: Any) -> str:
    """Flatten a tool result's content (string or list of text parts) into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""
