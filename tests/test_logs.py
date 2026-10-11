"""Logging setup: Discord noise is filtered, real problems still show."""
from __future__ import annotations

import logging

from music_agent.logs import DiscordNoiseFilter


def record(msg, level=logging.ERROR, exc=None):
    exc_info = (type(exc), exc, None) if exc else None
    return logging.LogRecord("discord.client", level, __file__, 1, msg, (), exc_info)


def test_voice_warnings_are_dropped():
    f = DiscordNoiseFilter()
    assert f.filter(record("PyNaCl is not installed, voice will NOT be supported", logging.WARNING)) is False
    assert f.filter(record("davey is not installed, voice will NOT be supported", logging.WARNING)) is False


def test_reconnects_become_one_line_warnings():
    r = record("Attempting a reconnect in 3.00s", exc=OSError("getaddrinfo failed"))
    assert DiscordNoiseFilter().filter(r) is True
    assert r.levelno == logging.WARNING and r.exc_info is None
    assert r.getMessage() == "Lost connection to Discord (OSError: getaddrinfo failed). Attempting a reconnect in 3.00s"


def test_other_messages_untouched():
    r = record("Something else went wrong")
    assert DiscordNoiseFilter().filter(r) is True and r.levelno == logging.ERROR
