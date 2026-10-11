"""Talk to the agents from the GUI, streaming each step as it happens.

There are two conversations, one per agent: ``dj`` (on the Now playing page)
and ``curator`` (on the Chat page). They are separate from Discord's.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from music_agent.gui.routes.deps import RuntimeDep

router = APIRouter(tags=["chat"])

Agent = Literal["dj", "curator"]  # the conversation's name is also its agent's role


class ChatBody(BaseModel):
    """A message for one of the agents."""

    text: str = Field(min_length=1, max_length=4000)
    conversation: Agent = "dj"


class ResetBody(BaseModel):
    """Which conversation to start afresh."""

    conversation: Agent = "dj"


@router.get("/chat")
async def history(rt: RuntimeDep, conversation: Agent = "dj") -> dict[str, Any]:
    """Messages so far in a conversation, and whether a request is running."""
    conv = rt.conversation(conversation, role=conversation)
    return {"conversation": conversation, "busy": conv.busy, "history": conv.history}


@router.post("/chat")
async def ask(rt: RuntimeDep, body: ChatBody) -> StreamingResponse:
    """Send a message; the response is NDJSON, one event per line.

    Events are ``tool_call``, ``tool_result``, ``text``, then ``done`` (or ``error``).
    """
    conv = rt.conversation(body.conversation, role=body.conversation)

    async def lines() -> AsyncIterator[str]:
        async for item in conv.ask(body.text.strip()):
            yield json.dumps(item, ensure_ascii=False, default=str) + "\n"

    return StreamingResponse(lines(), media_type="application/x-ndjson")


@router.post("/chat/reset")
async def reset(rt: RuntimeDep, body: ResetBody) -> dict[str, bool]:
    """Forget the conversation (the taste profile is kept)."""
    await rt.conversation(body.conversation, role=body.conversation).reset()
    return {"ok": True}
