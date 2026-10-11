"""Browse, search, add, edit and delete rows in the memory database, newest first.

Edits go straight to SQLite, so they take effect on the agents' next request.
The tables' own constraints and the checks each feature registers still apply
(e.g. a sentiment must be -1, 0 or 1, and a time must be a real time).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from music_agent.gui.routes.deps import MemoryDep

router = APIRouter(tags=["database"])


class RowEdit(BaseModel):
    """New values for some columns of one row."""

    values: dict[str, str | int | float | None]


@router.get("/db/tables")
async def tables(memory: MemoryDep) -> dict[str, Any]:
    """Table names and row counts."""
    return {"tables": await asyncio.to_thread(memory.db.tables)}


@router.get("/db/tables/{name}")
async def rows(memory: MemoryDep, name: str, limit: int = 50, offset: int = 0,
               search: str | None = None) -> dict[str, Any]:
    """One page of a table, newest first; search keeps rows where any column contains it."""
    try:
        return await asyncio.to_thread(memory.db.rows, name, limit, offset, search)
    except KeyError:
        raise HTTPException(404, f"No table '{name}'.") from None


@router.post("/db/tables/{name}/rows")
async def add_row(memory: MemoryDep, name: str, body: RowEdit) -> dict[str, Any]:
    """Add a row; empty time columns get the current time."""
    try:
        return {"row": await asyncio.to_thread(memory.db.insert_row, name, body.values)}
    except KeyError:
        raise HTTPException(404, f"No table '{name}'.") from None


@router.patch("/db/tables/{name}/rows/{row_id}")
async def edit_row(memory: MemoryDep, name: str, row_id: int, body: RowEdit) -> dict[str, Any]:
    """Change some columns of one row (not its ``id``)."""
    try:
        return {"row": await asyncio.to_thread(memory.db.update_row, name, row_id, body.values)}
    except KeyError:
        raise HTTPException(404, f"No row {row_id} in '{name}'.") from None


@router.delete("/db/tables/{name}/rows/{row_id}")
async def delete_row(memory: MemoryDep, name: str, row_id: int) -> dict[str, bool]:
    """Delete one row. Deleting a playlist also deletes its tracks."""
    try:
        deleted = await asyncio.to_thread(memory.db.delete_row, name, row_id)
    except KeyError:
        raise HTTPException(404, f"No table '{name}'.") from None
    if not deleted:
        raise HTTPException(404, f"No row {row_id} in '{name}'.")
    return {"ok": True}
