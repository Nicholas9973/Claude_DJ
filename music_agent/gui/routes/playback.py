"""What's playing, and direct controls (no Claude, no credit)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, Field

from music_agent.gui.routes.deps import MemoryDep, RuntimeDep
from music_agent.learning.watcher import parse_hms

router = APIRouter(tags=["playback"])


class VolumeBody(BaseModel):
    """Give ``level`` (absolute) or ``change`` (relative), not both; ``room`` for one speaker only."""

    level: int | None = Field(None, ge=0, le=100)
    change: int | None = Field(None, ge=-50, le=50)
    room: str | None = Field(None, max_length=100)


class GuestBody(BaseModel):
    """Turn guest mode on for ``hours``, or off with null / 0."""

    hours: float | None = Field(None, ge=0, le=24)


class RateBody(BaseModel):
    """Feedback on the current track."""

    sentiment: Literal["like", "dislike"]
    skip: bool = False


@router.get("/now")
async def now_playing(rt: RuntimeDep) -> dict[str, Any]:
    """What the active group is playing, with times in seconds and the current volume cap."""
    np = await asyncio.to_thread(rt.app.player.now_playing)
    data = asdict(np)
    data["position_s"] = parse_hms(np.position)
    data["duration_s"] = parse_hms(np.duration)
    data["volume_cap"] = rt.app.config.safety.volume_cap(datetime.now().time())
    data["liked"] = bool(await _likes_of_current(rt, np))
    return data


async def _likes_of_current(rt: RuntimeDep, np: Any) -> list[int]:
    """Ids of the likes given to the track now playing (none without memory or a track)."""
    memory = rt.app.memory
    if memory is None or not np.title:
        return []
    artist = np.artist or await asyncio.to_thread(memory.artist_for_title, np.title)
    return await asyncio.to_thread(memory.track_likes, np.title, artist)


@router.get("/rooms")
async def rooms(rt: RuntimeDep) -> dict[str, list[str]]:
    """All room names."""
    return {"rooms": await asyncio.to_thread(rt.app.player.rooms)}


@router.get("/queue")
async def queue(rt: RuntimeDep, limit: int = 30) -> dict[str, Any]:
    """The queue and the current 1-based position."""
    return await rt.tools.get_queue(limit)


@router.post("/playback/{action}")
async def playback(rt: RuntimeDep, action: Literal["pause", "resume", "skip", "stop"]) -> dict[str, Any]:
    """Pause, resume, skip or stop. Quiet hours still apply to resume."""
    return await rt.tools.playback_control(action)


@router.post("/volume")
async def volume(rt: RuntimeDep, body: VolumeBody) -> dict[str, Any]:
    """Set the group volume (keeping the balance) or one room's (saving a new balance); the cap applies."""
    return await rt.tools.set_volume(body.level, body.change, body.room)


@router.get("/volumes")
async def volumes(rt: RuntimeDep) -> dict[str, Any]:
    """Every speaker's volume, saved balance, and whether it's in the playing group."""
    return await rt.tools.room_volumes()


@router.post("/volumes/reset")
async def reset_balance(rt: RuntimeDep) -> dict[str, Any]:
    """Forget the balance: every speaker at the volume-source room's level again."""
    return await rt.tools.reset_balance()


@router.post("/rate")
async def rate(rt: RuntimeDep, body: RateBody) -> dict[str, Any]:
    """Like or dislike the current track (saved to memory), optionally skipping it."""
    saved = await rt.tools.record_feedback(body.sentiment, "current_track")
    if body.skip:
        await rt.tools.playback_control("skip")
    return saved


@router.delete("/rate")
async def unlike(rt: RuntimeDep, memory: MemoryDep) -> dict[str, Any]:
    """Take back the like(s) of the current track (all of them, wherever they were given)."""
    np = await asyncio.to_thread(rt.app.player.now_playing)
    if not np.title:
        raise ValueError("Nothing is playing, so there is no current track to unlike.")
    ids = await _likes_of_current(rt, np)
    for fid in ids:
        await asyncio.to_thread(memory.forget_feedback, fid)
    return {"removed": len(ids), "target": " - ".join(p for p in (np.title, np.artist) if p)}



@router.get("/guest")
async def guest(memory: MemoryDep) -> dict[str, Any]:
    """Whether guest mode is on (music then doesn't train your taste), and until when."""
    until = memory.guest_until()
    return {"on": until is not None, "until": until.isoformat(timespec="minutes") if until else None}


@router.post("/guest")
async def set_guest(memory: MemoryDep, body: GuestBody) -> dict[str, Any]:
    """Turn guest mode on for a number of hours, or off."""
    memory.set_guest_mode(body.hours)
    return await guest(memory)
