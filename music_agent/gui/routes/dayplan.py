"""The day plan (``day_plan.toml``) and the orchestrator that follows it."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Body

from music_agent import dayplan
from music_agent.gui.routes.deps import MemoryDep, RuntimeDep

router = APIRouter(tags=["day plan"])


@router.get("/dayplan")
async def get_plan(rt: RuntimeDep) -> dict[str, Any]:
    """The plan as stored, today's times, any quiet period, and what the orchestrator did lately."""
    plan = await asyncio.to_thread(dayplan.load, rt.app.day_plan_path)
    now = rt.tools.now()
    quiet_until = None
    if rt.app.memory:
        since = await asyncio.to_thread(rt.app.memory.quiet_since)
        if since and since + timedelta(hours=float(plan.quiet_after_stop_hours)) > now:
            quiet_until = (since + timedelta(hours=float(plan.quiet_after_stop_hours))).isoformat(timespec="minutes")
    return {"plan": plan.as_dict(), "today": plan.today(now), "quiet_until": quiet_until,
            "orchestrator": rt.orchestrator.status(), "path": str(rt.app.day_plan_path)}


@router.put("/dayplan")
async def save_plan(rt: RuntimeDep, plan: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    """Replace the whole plan (checked first; a bad plan is a 400 and nothing is saved)."""
    new = dayplan.from_dict(plan)
    await asyncio.to_thread(dayplan.save, new, rt.app.day_plan_path)
    return await get_plan(rt)


@router.post("/dayplan/check")
async def check_now(rt: RuntimeDep) -> dict[str, Any]:
    """Run the orchestrator's check now (it may ask Claude, which takes a little while)."""
    entry = await rt.orchestrator.check()
    return {"check": entry, **await get_plan(rt)}


@router.post("/dayplan/resume")
async def resume(rt: RuntimeDep, memory: MemoryDep) -> dict[str, Any]:
    """End the quiet period after a stop, so the orchestrator may start music again."""
    await asyncio.to_thread(memory.end_quiet)
    return await get_plan(rt)
