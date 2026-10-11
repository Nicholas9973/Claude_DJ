"""Health of the moving parts: Discord bot, agent settings, safety limits, logs."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from music_agent.gui.routes.deps import RuntimeDep

router = APIRouter(tags=["system"])


@router.get("/status")
async def status(rt: RuntimeDep) -> dict[str, Any]:
    """Everything the System page and the sidebar show."""
    cfg = rt.app.config
    now = datetime.now().time()
    return {
        "bot": rt.bot.status(),
        "agent": {"model": cfg.agent.model, "service": cfg.music.service},
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
