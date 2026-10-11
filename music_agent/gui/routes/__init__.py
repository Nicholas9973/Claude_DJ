"""The JSON API, one module per feature. Each module exposes ``router``.

To add a feature, create a module here and add its router to ``ROUTERS``;
it is served under ``/api`` and appears in the API docs at ``/docs``.
"""

from music_agent.gui.routes import chat, database, playback, playlists, system, taste

ROUTERS = [
    playback.router,
    chat.router,
    playlists.router,
    taste.router,
    database.router,
    system.router,
]
