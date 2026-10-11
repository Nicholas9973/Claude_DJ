"""One logging setup for the CLI, the terminal chat and the Discord bot."""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"

# Third-party loggers that are chatty at INFO but rarely useful.
QUIET = {
    "soco": logging.WARNING,
    # SoCo warns about every non-LAN network adapter (VPN, WSL, Hyper-V) it can't
    # discover on; harmless as long as speakers are found.
    "soco.discovery": logging.ERROR,
    "urllib3": logging.WARNING,
    "discord.http": logging.WARNING,
    "discord.gateway": logging.WARNING,
}


class DiscordNoiseFilter(logging.Filter):
    """Tidy discord.py's connection logging.

    - Drops "PyNaCl/davey is not installed, voice will NOT be supported": this
      bot never joins voice channels.
    - Turns "Attempting a reconnect" (logged at ERROR with a full traceback every
      time the internet blips or the PC wakes from sleep) into one WARNING line.
      discord.py reconnects by itself; the traceback adds nothing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Drop or rewrite one record; returns False to drop it."""
        message = record.getMessage()
        if "voice will NOT be supported" in message:
            return False
        if message.startswith("Attempting a reconnect"):
            reason = ""
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                reason = f" ({type(exc).__name__}: {exc})"
            record.msg = f"Lost connection to Discord{reason}. {message}"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.levelno, record.levelname = logging.WARNING, "WARNING"
        return True


def setup_logging(verbose: bool = False, level: int = logging.WARNING,
                  logfile: Path | None = None) -> None:
    """Configure logging for an entry point.

    Args:
        verbose: Log everything at DEBUG, including chatty third-party loggers.
        level: Console level otherwise.
        logfile: Also log to this rotating file (2 MB x 3).
    """
    formatter = logging.Formatter(FORMAT, DATEFMT)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if logfile:
        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            logfile, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    for handler in handlers:
        handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(logging.DEBUG if verbose else level)
    if not verbose:
        for name, lvl in QUIET.items():
            logging.getLogger(name).setLevel(lvl)
    discord_client = logging.getLogger("discord.client")
    if not any(isinstance(f, DiscordNoiseFilter) for f in discord_client.filters):
        discord_client.addFilter(DiscordNoiseFilter())
