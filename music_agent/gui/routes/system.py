"""Health of the moving parts: Discord bot, agent settings (each agent's model), safety limits, logs."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from music_agent.agent.server import ROLE_TOOLS
from music_agent.agent.session import model_for
from music_agent.gui.routes.deps import MemoryDep, RuntimeDep
from music_agent.learning.memory import AGENT_MODELS

router = APIRouter(tags=["system"])


class ModelChoice(BaseModel):
    """An agent's model: one of ``AGENT_MODELS``, or None to use the one in config.toml."""

    model: str | None = None


@router.get("/status")
async def status(rt: RuntimeDep) -> dict[str, Any]:
    """Everything the System page and the sidebar show."""
    cfg = rt.app.config
    now = datetime.now().time()
    return {
        "bot": rt.bot.status(),
        "agent": {"models": _models(rt), "choices": list(AGENT_MODELS), "service": cfg.music.service},
        "safety": {
            "max_volume": cfg.safety.max_volume,
            "volume_cap_now": cfg.safety.volume_cap(now),
            "quiet_hours": f"{cfg.safety.quiet_start:%H:%M}-{cfg.safety.quiet_end:%H:%M}",
            "quiet_mode": cfg.safety.quiet_mode,
            "in_quiet_hours": cfg.safety.in_quiet_hours(now),
        },
        "memory": {"enabled": rt.app.memory is not None, "path": cfg.memory.path},
        "watcher": bool(rt.app.watcher and rt.app.watcher.running),
        "started_at": rt.started_at.isoformat(timespec="seconds"),
        "log_file": str(rt.log_file) if rt.log_file else None,
    }


@router.put("/agents/{role}/model")
async def set_model(rt: RuntimeDep, memory: MemoryDep, role: str, body: ModelChoice) -> dict[str, Any]:
    """Choose an agent's model; its conversations use it from their next request."""
    await asyncio.to_thread(memory.set_agent_model, role, body.model or None)
    return _models(rt)


def _models(rt: RuntimeDep) -> dict[str, dict[str, Any]]:
    """Per agent role: the model in use, the config.toml default, and whether it was chosen here."""
    return {role: {"model": model_for(rt.app, role), "default": rt.app.config.agent.model_for(role),
                   "chosen": bool(rt.app.memory and rt.app.memory.agent_model(role))}
            for role in ROLE_TOOLS}


@router.post("/bot/start")
async def start_bot(rt: RuntimeDep) -> dict[str, Any]:
    """Start the Discord bot (re-reads ``.env``)."""
    rt.bot.start()
    return rt.bot.status()


@router.post("/bot/stop")
async def stop_bot(rt: RuntimeDep) -> dict[str, Any]:
    """Disconnect the Discord bot."""
    await rt.bot.stop()
    return rt.bot.status()


@router.get("/logs")
async def logs(rt: RuntimeDep, lines: int = 200) -> dict[str, Any]:
    """The last lines of this process's log file."""
    if rt.log_file is None:
        return {"lines": []}
    return {"lines": await asyncio.to_thread(_tail, rt.log_file, max(1, min(lines, 2000)))}


def _tail(path: Path, lines: int, chunk: int = 256_000) -> list[str]:
    """Last ``lines`` lines of a text file, reading only its end."""
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(0, 2)
        fh.seek(max(0, fh.tell() - chunk))
        text = fh.read().decode("utf-8", errors="replace")
    return text.splitlines()[-lines:]
