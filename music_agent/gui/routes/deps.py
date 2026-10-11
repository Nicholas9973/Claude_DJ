"""Shared helpers for route modules."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request

from music_agent.learning.memory import Memory
from music_agent.runtime import Runtime


def get_runtime(request: Request) -> Runtime:
    """Return the runtime the app was created with."""
    return request.app.state.runtime


def get_memory(request: Request) -> Memory:
    """Return the memory, or answer 404 if memory is turned off."""
    memory = get_runtime(request).app.memory
    if memory is None:
        raise HTTPException(404, "Memory is turned off.")
    return memory


# Use these as parameter types: `def route(rt: RuntimeDep): ...`
RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
MemoryDep = Annotated[Memory, Depends(get_memory)]
