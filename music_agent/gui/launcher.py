"""Start everything: the runtime, the web GUI, the day plan's orchestrator and (optionally) the Discord bot.

Examples:
    python -m music_agent.gui                     # GUI + Discord bot + orchestrator, opens the browser
    python -m music_agent.gui --no-bot            # GUI only (no orchestrator either: safe next to a running bot)
    python -m music_agent.gui --no-orchestrator   # don't follow the day plan in this process
    python -m music_agent.gui --no-browser        # don't open a browser tab
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import sys
import webbrowser

import uvicorn

from music_agent.app import build
from music_agent.config import PROJECT_ROOT, load_config
from music_agent.gui.server import create_app
from music_agent.logs import setup_logging
from music_agent.runtime import Runtime

log = logging.getLogger("music_agent.gui")
LOG_FILE = PROJECT_ROOT / "data" / "logs" / "gui.log"


async def serve(host: str, port: int, start_bot: bool, open_browser: bool, orchestrate: bool = True) -> int:
    """Run the GUI (and bot) until Ctrl+C.

    Args:
        host: Address to serve on.
        port: Port to serve on.
        start_bot: Also start the Discord bot.
        open_browser: Open the GUI in the default browser once it's up.
        orchestrate: Follow the day plan (only one process should).

    Returns:
        Exit code: 1 if the port is already taken.
    """
    if not _port_free(host, port):
        log.error("Port %d is already in use. Is the GUI already running? Open http://%s:%d", port, host, port)
        return 1

    runtime = Runtime(build())
    runtime.log_file = LOG_FILE
    server = uvicorn.Server(uvicorn.Config(create_app(runtime), host=host, port=port,
                                           log_config=None, access_log=False))
    url = f"http://{host}:{port}"
    await runtime.start(orchestrate=orchestrate)
    if start_bot:
        runtime.bot.start()
    if open_browser:
        asyncio.create_task(_open_when_ready(server, url))
    log.info("GUI running at %s (Ctrl+C to stop)", url)
    try:
        await server.serve()
    finally:
        with contextlib.suppress(asyncio.CancelledError):
            await runtime.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m music_agent.gui``.

    Args:
        argv: Arguments; defaults to ``sys.argv[1:]``.

    Returns:
        Exit code.
    """
    cfg = load_config().gui
    p = argparse.ArgumentParser(prog="music_agent.gui", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=cfg.port)
    p.add_argument("--no-bot", action="store_true", help="don't start the Discord bot")
    p.add_argument("--no-browser", action="store_true", help="don't open the browser")
    p.add_argument("--no-orchestrator", action="store_true",
                   help="don't follow the day plan here (--no-bot implies this)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)
    setup_logging(args.verbose, level=logging.INFO, logfile=LOG_FILE)
    try:
        return asyncio.run(serve(cfg.host, args.port, cfg.start_bot and not args.no_bot,
                                 cfg.open_browser and not args.no_browser,
                                 orchestrate=not (args.no_bot or args.no_orchestrator)))
    except KeyboardInterrupt:
        log.info("Stopped.")
        return 0


async def _open_when_ready(server: uvicorn.Server, url: str) -> None:
    """Open the browser once the server accepts connections."""
    while not server.started:
        await asyncio.sleep(0.1)
    await asyncio.to_thread(webbrowser.open, url)


def _port_free(host: str, port: int) -> bool:
    """True if nothing is listening on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) != 0


if __name__ == "__main__":
    sys.exit(main())
