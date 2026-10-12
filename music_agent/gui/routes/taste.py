"""What the agents know about your taste, and every way to change it by hand.

Preferences, quiz findings, feedback, score adjustments, the curator's notebook
and the scoring settings can all be added, edited and removed here. Changes take effect
on the agents' next request.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from music_agent.agent.tools import SENTIMENT
from music_agent.gui.routes.deps import MemoryDep
from music_agent.models import part_of_day

router = APIRouter(tags=["taste"])

PartOfDay = Literal["morning", "afternoon", "evening", "night"]
Sentiment = Literal["like", "dislike", "neutral"]


class Preference(BaseModel):
    """A stated preference (see ``Memory.add_preference``)."""

    kind: Literal["artist", "track", "genre", "rule"]
    sentiment: Sentiment
    subject: str = Field(min_length=1, max_length=300)
    artist: str | None = Field(None, max_length=300)
    part_of_day: PartOfDay | None = None
    note: str | None = Field(None, max_length=500)
    frequency: Literal["regular", "sometimes", "rarely", "on_request"] | None = None
    context: str | None = Field(None, max_length=200)

    def fields(self) -> dict[str, Any]:
        """As keyword arguments for ``Memory.add_preference`` / ``update_preference``."""
        return {"kind": self.kind, "sentiment": SENTIMENT[self.sentiment], "subject": self.subject,
                "artist": self.artist, "part": self.part_of_day, "note": self.note,
                "frequency": self.frequency, "context": self.context}


class NewFeedback(BaseModel):
    """Feedback added by hand: a like/dislike of a track or artist, or a general note."""

    sentiment: Sentiment
    scope: Literal["track", "artist", "general"]
    title: str | None = Field(None, max_length=300)
    artist: str | None = Field(None, max_length=300)
    note: str | None = Field(None, max_length=500)


class FeedbackEdit(BaseModel):
    """New values for some fields of a feedback entry; fields left out stay as they are."""

    sentiment: Sentiment | None = None
    scope: Literal["track", "artist", "general"] | None = None
    title: str | None = Field(None, max_length=300)
    artist: str | None = Field(None, max_length=300)
    note: str | None = Field(None, max_length=500)


class Nudge(BaseModel):
    """A hand-made change to a score (see ``Memory.adjust``)."""

    kind: Literal["artist", "track"]
    subject: str = Field(min_length=1, max_length=300)
    artist: str | None = Field(None, max_length=300)
    change: float = Field(0, ge=-50, le=50)
    reset_learning: bool = False
    note: str | None = Field(None, max_length=500)


class AdjustmentEdit(BaseModel):
    """Set an adjustment's fields directly; send ``ignore_before: null`` to undo a reset."""

    delta: float | None = Field(None, ge=-50, le=50)
    ignore_before: datetime | None = None
    note: str | None = Field(None, max_length=500)


class Note(BaseModel):
    """A notebook entry written by you."""

    text: str = Field(min_length=1, max_length=2000)
    topic: str | None = Field(None, max_length=120)
    for_dj: bool = False


class NoteEdit(BaseModel):
    """New values for some fields of a note."""

    text: str | None = Field(None, min_length=1, max_length=2000)
    topic: str | None = Field(None, max_length=120)
    for_dj: bool | None = None


class FindingEdit(BaseModel):
    """New values for some fields of a finding; confirming still needs enough evidence."""

    statement: str | None = Field(None, min_length=1, max_length=300)
    status: Literal["tentative", "confirmed", "dropped"] | None = None
    note: str | None = Field(None, max_length=500)


class TuningValue(BaseModel):
    """A new value for a scoring setting, or null for its default."""

    value: float | None


def _sent(model: BaseModel) -> dict[str, Any]:
    """Only the fields the request actually sent (so ``null`` can mean "clear it")."""
    return {k: getattr(model, k) for k in model.model_fields_set}


# ---------------------------------------------------------------- overview
@router.get("/taste")
async def taste(memory: MemoryDep) -> dict[str, Any]:
    """Everything on the Taste page: the profile Claude sees, the scores behind it, and what you can edit."""
    part = part_of_day(datetime.now().hour)
    profile, scores, prefs, findings, adjustments, notes, tuning = await asyncio.gather(
        asyncio.to_thread(memory.taste_profile, part),
        asyncio.to_thread(memory.affinities, 500),
        asyncio.to_thread(memory.preferences),
        asyncio.to_thread(memory.findings, True),
        asyncio.to_thread(memory.adjustments),
        asyncio.to_thread(memory.notes, 200),
        asyncio.to_thread(memory.tuning_info),
    )
    return {"part_of_day": part, "profile": profile, "affinities": scores, "preferences": prefs,
            "findings": findings, "adjustments": adjustments, "notes": notes, "tuning": tuning}


# ---------------------------------------------------------------- feedback
@router.get("/feedback")
async def feedback(memory: MemoryDep, limit: int = 50) -> dict[str, Any]:
    """Recent feedback, newest first."""
    return {"feedback": await asyncio.to_thread(memory.recent_feedback, max(1, min(limit, 500)))}


@router.post("/feedback")
async def add_feedback(memory: MemoryDep, body: NewFeedback) -> dict[str, Any]:
    """Add feedback by hand, e.g. a general note for the DJ."""
    fid = await asyncio.to_thread(lambda: memory.record_feedback(
        SENTIMENT[body.sentiment], body.scope, body.title or None, body.artist or None, body.note or None))
    return {"id": fid}


@router.patch("/feedback/{feedback_id}")
async def edit_feedback(memory: MemoryDep, feedback_id: int, body: FeedbackEdit) -> dict[str, Any]:
    """Change a feedback entry (same rules as adding one)."""
    changes = _sent(body)
    if "sentiment" in changes:
        changes["sentiment"] = SENTIMENT[changes["sentiment"]]
    try:
        return {"feedback": await asyncio.to_thread(lambda: memory.update_feedback(feedback_id, **changes))}
    except KeyError:
        raise HTTPException(404, f"No feedback #{feedback_id}.") from None


@router.delete("/feedback/{feedback_id}")
async def forget(memory: MemoryDep, feedback_id: int) -> dict[str, bool]:
    """Delete one feedback entry."""
    if not await asyncio.to_thread(memory.forget_feedback, feedback_id):
        raise HTTPException(404, f"No feedback #{feedback_id}.")
    return {"ok": True}


# ---------------------------------------------------------------- preferences
@router.get("/preferences")
async def preferences(memory: MemoryDep) -> dict[str, Any]:
    """Stated preferences, newest first."""
    return {"preferences": await asyncio.to_thread(memory.preferences)}


@router.post("/preferences")
async def add_preference(memory: MemoryDep, body: Preference) -> dict[str, Any]:
    """Add a stated preference."""
    pid = await asyncio.to_thread(lambda: memory.add_preference(**body.fields()))
    return {"id": pid}


@router.put("/preferences/{pref_id}")
async def update_preference(memory: MemoryDep, pref_id: int, body: Preference) -> dict[str, Any]:
    """Replace a stated preference's fields."""
    try:
        return await asyncio.to_thread(lambda: memory.update_preference(pref_id, **body.fields()))
    except KeyError:
        raise HTTPException(404, f"No preference #{pref_id}.") from None


@router.delete("/preferences/{pref_id}")
async def remove_preference(memory: MemoryDep, pref_id: int) -> dict[str, bool]:
    """Delete a stated preference."""
    if not await asyncio.to_thread(memory.remove_preference, pref_id):
        raise HTTPException(404, f"No preference #{pref_id}.")
    return {"ok": True}


# ---------------------------------------------------------------- findings
@router.patch("/findings/{finding_id}")
async def edit_finding(memory: MemoryDep, finding_id: int, body: FindingEdit) -> dict[str, Any]:
    """Reword, confirm (only with enough evidence), drop or reopen a finding."""
    try:
        return {"finding": await asyncio.to_thread(lambda: memory.update_finding(finding_id, **_sent(body)))}
    except KeyError:
        raise HTTPException(404, f"No finding #{finding_id}.") from None


@router.delete("/findings/{finding_id}")
async def remove_finding(memory: MemoryDep, finding_id: int) -> dict[str, bool]:
    """Delete a finding and its evidence."""
    if not await asyncio.to_thread(memory.remove_finding, finding_id):
        raise HTTPException(404, f"No finding #{finding_id}.")
    return {"ok": True}


# ---------------------------------------------------------------- score adjustments
@router.post("/adjustments")
async def nudge(memory: MemoryDep, body: Nudge) -> dict[str, Any]:
    """Nudge a score and/or reset what was learned; nudges for the same artist or track add up."""
    adj = await asyncio.to_thread(lambda: memory.adjust(body.kind, body.subject, body.change, body.artist,
                                                        body.reset_learning, body.note))
    return {"adjustment": adj}


@router.patch("/adjustments/{adj_id}")
async def edit_adjustment(memory: MemoryDep, adj_id: int, body: AdjustmentEdit) -> dict[str, Any]:
    """Set an adjustment's nudge, reset time or note directly."""
    changes = _sent(body)
    if changes.get("ignore_before") is not None:
        changes["ignore_before"] = changes["ignore_before"].isoformat(timespec="seconds")
    try:
        return {"adjustment": await asyncio.to_thread(lambda: memory.update_adjustment(adj_id, **changes))}
    except KeyError:
        raise HTTPException(404, f"No adjustment #{adj_id}.") from None


@router.delete("/adjustments/{adj_id}")
async def remove_adjustment(memory: MemoryDep, adj_id: int) -> dict[str, bool]:
    """Delete an adjustment: its nudge and its reset."""
    if not await asyncio.to_thread(memory.remove_adjustment, adj_id):
        raise HTTPException(404, f"No adjustment #{adj_id}.")
    return {"ok": True}


# ---------------------------------------------------------------- notebook
@router.post("/notes")
async def add_note(memory: MemoryDep, body: Note) -> dict[str, Any]:
    """Write a note in the notebook (the curator reads it; ``for_dj`` shows it to the DJ too)."""
    nid = await asyncio.to_thread(lambda: memory.add_note(body.text, body.topic, "you", body.for_dj))
    return {"id": nid}


@router.patch("/notes/{note_id}")
async def edit_note(memory: MemoryDep, note_id: int, body: NoteEdit) -> dict[str, Any]:
    """Change a note's text, topic or whether the DJ sees it."""
    try:
        return {"note": await asyncio.to_thread(lambda: memory.update_note(note_id, **_sent(body)))}
    except KeyError:
        raise HTTPException(404, f"No note #{note_id}.") from None


@router.delete("/notes/{note_id}")
async def remove_note(memory: MemoryDep, note_id: int) -> dict[str, bool]:
    """Delete a note."""
    if not await asyncio.to_thread(memory.remove_note, note_id):
        raise HTTPException(404, f"No note #{note_id}.")
    return {"ok": True}


# ---------------------------------------------------------------- scoring settings
@router.get("/tuning")
async def tuning(memory: MemoryDep) -> dict[str, Any]:
    """The scoring settings with their defaults and descriptions."""
    return {"tuning": await asyncio.to_thread(memory.tuning_info)}


@router.put("/tuning/{key}")
async def set_tuning(memory: MemoryDep, key: str, body: TuningValue) -> dict[str, Any]:
    """Change one scoring setting, or send ``null`` to go back to its default."""
    await asyncio.to_thread(memory.set_tuning, key, body.value)
    return {"tuning": await asyncio.to_thread(memory.tuning_info)}
