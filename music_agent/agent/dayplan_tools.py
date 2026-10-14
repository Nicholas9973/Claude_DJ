"""Day plan actions: read the plan (orchestrator, curator), change it (curator), ask the DJ (orchestrator).

Like ``tools.py``, plain Python returning dicts; ``server.py`` decides which
role gets which.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from music_agent import dayplan
from music_agent.agent.tools import MusicTools, ToolError
from music_agent.dayplan import DayPlan
from music_agent.learning.memory import DAY_PLAN_PREFIX

AskDJ = Callable[[str], Awaitable[dict[str, Any]]]    # request -> {"reply", "steps", "error"}
DJ_PREFIX = DAY_PLAN_PREFIX + " "                     # how the DJ (and the history) recognise the orchestrator's requests


class DayPlanTools:
    """Read and change the day plan, and hand requests to the DJ.

    Args:
        music: The DJ's tools (for the clock and memory).
        path: The day plan file.
        ask_dj: Sends a request to the DJ; only the orchestrator gets one.
    """

    def __init__(self, music: MusicTools, path: Path = dayplan.DAY_PLAN_PATH, ask_dj: AskDJ | None = None):
        self.music = music
        self.path = path
        self._ask_dj = ask_dj

    def _load(self) -> DayPlan:
        """The plan from its file; a broken file is reported to Claude."""
        try:
            return dayplan.load(self.path)
        except ValueError as exc:
            raise ToolError(f"The day plan file can't be read: {exc}") from exc

    async def get_day_plan(self) -> dict[str, Any]:
        """The day plan, today's times, and how the orchestrator has been following it.

        Returns:
            ``plan`` (the settings and slots as stored, for editing), ``today``
            (each slot with today's time; ``now`` marks the current one),
            ``quiet_until`` while the person's stop is respected, and from
            memory the orchestrator's ``recent_runs`` and the person's
            ``recent_stops``.
        """
        plan = await asyncio.to_thread(self._load)
        now = self.music.now()
        out: dict[str, Any] = {"plan": plan.as_dict(), "today": plan.today(now)}
        memory = self.music.memory
        if memory:
            since = await asyncio.to_thread(memory.quiet_since)
            if since and since + timedelta(hours=float(plan.quiet_after_stop_hours)) > now:
                out["quiet_until"] = f"{since + timedelta(hours=float(plan.quiet_after_stop_hours)):%H:%M}"
            runs = await asyncio.to_thread(memory.recent_requests, "orchestrator", 10)
            out["recent_runs"] = [{**r, "reply": (r["reply"] or "")[:300]} for r in runs]
            out["recent_stops"] = await asyncio.to_thread(memory.quiet_history, 14)
        return out

    async def update_day_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Replace the whole day plan (send it all back, changed only where needed).

        Args:
            plan: Settings and ``slots``, as ``get_day_plan`` returns them in ``plan``.

        Returns:
            ``saved`` and today's outline under the new plan.

        Raises:
            ToolError: The plan doesn't make sense (the message says why); nothing is saved then.
        """
        try:
            new = dayplan.from_dict(plan)
            await asyncio.to_thread(dayplan.save, new, self.path)
        except (ValueError, TypeError) as exc:
            raise ToolError(f"Not saved: {exc}") from exc
        return {"saved": True, "enabled": new.enabled, "today": new.today(self.music.now())}

    async def ask_dj(self, request: str) -> dict[str, Any]:
        """Hand a request to the DJ, which chooses and queues the music.

        Args:
            request: What to play, e.g. "about an hour of gentle acoustic folk, low energy, replace the queue".

        Returns:
            The DJ's ``reply`` and its ``steps`` (tool calls and results in one line each).

        Raises:
            ToolError: No DJ to ask, or an empty request.
        """
        if self._ask_dj is None:
            raise ToolError("There is no DJ to ask here.")
        if not (request or "").strip():
            raise ToolError("Say what the DJ should play.")
        result = await self._ask_dj(DJ_PREFIX + request.strip())
        if result.get("error"):
            raise ToolError(f"The DJ failed: {result['error']}. Reply: {result.get('reply') or '(none)'}")
        return {"dj_reply": result.get("reply", ""), "dj_steps": result.get("steps", [])}
