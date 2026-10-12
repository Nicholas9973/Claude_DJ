"""Small text helpers shared by search matching, the player, memory and the front-ends."""

from __future__ import annotations

import re
import unicodedata


def norm(text: str | None) -> str:
    """Normalise text for loose comparison.

    Lowercases, strips accents and turns punctuation into single spaces.

    Args:
        text: Any text, or None.

    Returns:
        The normalised text, e.g. ``"Beyoncé!"`` becomes ``"beyonce"``.
        Empty string for None or empty input.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


VERSION_SUFFIX = re.compile(r"\s+-\s+.*\b(remaster(ed)?|version|edit|mono|stereo|mix|live)\b.*$", re.IGNORECASE)


def core_title(title: str | None) -> str:
    """A song's title without version labels, normalised: what makes two plays "the same song".

    Args:
        title: A track title as the catalogue or the speaker gives it.

    Returns:
        E.g. ``"dreams"`` for ``"Dreams (2001 Remaster)"`` or ``"Dreams - 2004 Remaster"``.
    """
    text = re.sub(r"[\(\[].*?[\)\]]", "", title or "")
    return norm(VERSION_SUFFIX.sub("", text)) or norm(title)


def format_track_time(seconds: int | None) -> str:
    """Format a track length as minutes and seconds.

    Args:
        seconds: Length in seconds, or None if unknown.

    Returns:
        Text like ``"3:54"``, or ``"?"`` when the length is unknown or zero.
    """
    return f"{seconds // 60}:{seconds % 60:02d}" if seconds else "?"


def format_minutes(minutes: int) -> str:
    """Format a total listening time.

    Args:
        minutes: Whole minutes.

    Returns:
        Text like ``"58 min"`` or, from one hour up, ``"1h55"``.
    """
    return f"{minutes // 60}h{minutes % 60:02d}" if minutes >= 60 else f"{minutes} min"
