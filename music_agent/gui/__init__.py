"""Web GUI: a local control panel for the music agent.

Layout:
    launcher.py   ``python -m music_agent.gui``: runtime + web server + Discord bot
    server.py     builds the FastAPI app from the routers in ``routes/``
    routes/       one module per feature, each exposing ``router`` (JSON API under /api)
    static/       the browser side: plain HTML, CSS and JavaScript modules, no build step

To add a feature: add ``routes/<feature>.py`` and list it in ``routes/__init__.py``,
then add ``static/js/pages/<feature>.js`` and list it in ``static/js/pages/index.js``.
"""
