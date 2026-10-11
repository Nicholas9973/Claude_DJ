"""Saved playlists: list, edit, add and remove tracks, play.

Adding a track looks it up in the catalogue exactly as the curator does
(``runtime.curator``), and playing goes through ``runtime.tools``, so the
safety rules apply.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from music_agent.gui.routes.deps import RuntimeDep
from music_agent.learning.playlists import PlaylistStore
from music_agent.runtime import Runtime

router = APIRouter(tags=["playlists"])


class NewPlaylist(BaseModel):
    """A new, empty playlist."""

    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(None, max_length=500)


class PlaylistEdit(BaseModel):
    """Rename and/or describe a playlist; omitted fields stay as they are."""

    name: str | None = Field(None, min_length=1, max_length=120)
    description: str | None = Field(None, max_length=500)


class NewTrack(BaseModel):
    """A song to look up and add to the end."""

    title: str = Field(min_length=1, max_length=300)
    artist: str = Field(min_length=1, max_length=300)
    album: str | None = None


class PlayBody(BaseModel):
    """How to play a playlist."""

    mode: Literal["replace", "append", "next"] = "replace"
    shuffle: bool = False


def _store(rt: Runtime) -> PlaylistStore:
    """The playlist store, or 404 when memory is turned off."""
    if rt.app.playlists is None:
        raise HTTPException(404, "Playlists need memory, which is turned off.")
    return rt.app.playlists


async def _get(rt: Runtime, playlist_id: int) -> dict[str, Any]:
    """One playlist with its tracks, or 404."""
    playlist = await asyncio.to_thread(_store(rt).get, playlist_id)
    if playlist is None:
        raise HTTPException(404, f"No playlist #{playlist_id}.")
    return playlist


@router.get("/playlists")
async def list_playlists(rt: RuntimeDep) -> dict[str, Any]:
    """All playlists with their track counts and lengths."""
    return {"playlists": await asyncio.to_thread(_store(rt).list)}


@router.post("/playlists")
async def create(rt: RuntimeDep, body: NewPlaylist) -> dict[str, Any]:
    """Make an empty playlist."""
    pid = await asyncio.to_thread(_store(rt).create, body.name, body.description)
    return await _get(rt, pid)


@router.get("/playlists/{playlist_id}")
async def get(rt: RuntimeDep, playlist_id: int) -> dict[str, Any]:
    """One playlist with its tracks in order."""
    return await _get(rt, playlist_id)


@router.patch("/playlists/{playlist_id}")
async def edit(rt: RuntimeDep, playlist_id: int, body: PlaylistEdit) -> dict[str, Any]:
    """Rename a playlist or change its description."""
    if not await asyncio.to_thread(_store(rt).update, playlist_id, body.name, body.description):
        raise HTTPException(404, f"No playlist #{playlist_id}.")
    return await _get(rt, playlist_id)


@router.delete("/playlists/{playlist_id}")
async def delete(rt: RuntimeDep, playlist_id: int) -> dict[str, bool]:
    """Delete a playlist and its tracks."""
    if not await asyncio.to_thread(_store(rt).delete, playlist_id):
        raise HTTPException(404, f"No playlist #{playlist_id}.")
    return {"ok": True}


@router.post("/playlists/{playlist_id}/tracks")
async def add_track(rt: RuntimeDep, playlist_id: int, body: NewTrack) -> dict[str, Any]:
    """Look a song up in the catalogue and add it to the end.

    The response has ``saved`` (0 if it wasn't found or was already there)
    and ``not_found`` with the artist's real tracks, when there are any.
    """
    playlist = await _get(rt, playlist_id)
    spec = {"title": body.title, "artist": body.artist, "album": body.album or ""}
    result = await rt.curator.save_playlist(playlist["name"], [spec], mode="append")
    return {**result, "playlist": await _get(rt, playlist_id)}


@router.delete("/playlists/{playlist_id}/tracks/{track_id}")
async def remove_track(rt: RuntimeDep, playlist_id: int, track_id: int) -> dict[str, Any]:
    """Remove one track (by the ``id`` in the playlist's ``tracks``)."""
    if not await asyncio.to_thread(_store(rt).remove_track, playlist_id, track_id):
        raise HTTPException(404, f"No track #{track_id} in playlist #{playlist_id}.")
    return await _get(rt, playlist_id)


@router.post("/playlists/{playlist_id}/play")
async def play(rt: RuntimeDep, playlist_id: int, body: PlayBody) -> dict[str, Any]:
    """Play a playlist on the default rooms (quiet hours and the volume cap still apply)."""
    await _get(rt, playlist_id)
    return await rt.tools.play_playlist(playlist_id, body.mode, None, body.shuffle)
