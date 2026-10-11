"""Build the FastAPI app: the JSON API from ``routes/`` plus the static browser files."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from music_agent.agent.tools import ToolError
from music_agent.gui.routes import ROUTERS
from music_agent.players.base import PlayerError, PolicyError
from music_agent.providers.base import ProviderError
from music_agent.runtime import Runtime

STATIC_DIR = Path(__file__).parent / "static"


def create_app(runtime: Runtime) -> FastAPI:
    """Create the web app. Starting and stopping the runtime is the caller's job.

    Args:
        runtime: The shared service the routes act on.

    Returns:
        The app; API docs are served at ``/docs``.
    """
    app = FastAPI(title="Claude Music DJ", summary="Local control panel for the music agent.")
    app.state.runtime = runtime
    for router in ROUTERS:
        app.include_router(router, prefix="/api")
    _add_error_handlers(app)

    @app.middleware("http")
    async def no_cache(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        # Edits to the HTML/CSS/JS show up on a plain reload, without a hard refresh.
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-cache")
        return response

    # Mounted last so /api and /docs win over static files.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def _add_error_handlers(app: FastAPI) -> None:
    """Turn known errors into JSON ``{"detail": message}`` with a fitting status code.

    Routes can then just call tools and the player and let errors propagate.
    """
    def handler(status: int) -> Callable[[Request, Exception], Awaitable[JSONResponse]]:
        async def handle(request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=status)
        return handle

    app.add_exception_handler(PolicyError, handler(409))      # refused by a safety rule
    app.add_exception_handler(ToolError, handler(400))        # bad request or refused action
    app.add_exception_handler(PlayerError, handler(503))      # speakers unreachable
    app.add_exception_handler(ProviderError, handler(502))    # music service failed
    app.add_exception_handler(ValueError, handler(400))       # invalid edit (memory, playlists, database)
    app.add_exception_handler(sqlite3.IntegrityError, handler(400))  # an edit broke a table constraint
