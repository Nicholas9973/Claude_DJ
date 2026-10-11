"""Pick the right search result for a (title, artist) request.

Search results often contain covers, karaoke versions and live takes; the agent
asks for "One More Time by Daft Punk" and should get the original.
"""

from __future__ import annotations

import re

from music_agent.models import MediaItem
from music_agent.text import norm

VARIANT_WORDS = ("karaoke", "tribute", "cover", "instrumental", "remix", "live",
                 "acoustic", "lullaby", "8-bit", "made famous", "originally performed")
EDIT_WORDS = ("edit", "radio edit", "single version", "single edit", "short version")


def score(item: MediaItem, title: str, artist: str | None = None, album: str | None = None) -> float:
    """Score how well a search result matches a requested song.

    Prefers: exact title, right artist, the requested album if given, album
    versions over single/radio edits, originals over covers/karaoke/live.

    Args:
        item: A search result.
        title: Requested title.
        artist: Requested artist. A different artist always scores 0.
        album: Requested album, if any.

    Returns:
        Higher is better; 0 or less means "not a match".
    """
    want_title = norm(title)
    got_full = norm(item.title)
    # "Cielito Lindo - Remastered 2011 (feat. X)" -> "cielito lindo"
    got_base = norm(_strip_brackets(item.title or "").split(" - ")[0])

    if got_full == want_title or got_base == want_title:
        s = 10.0
    elif got_full.startswith(want_title):
        s = 6.0
    elif want_title and want_title in got_full:
        s = 3.0
    else:
        return 0.0

    if artist:
        want_artist, got_artist = norm(artist), norm(item.artist)
        if not got_artist:
            s -= 2
        elif want_artist in got_artist or got_artist in want_artist:
            s += 8
        else:
            return 0.0  # wrong artist: never accept

    haystack = f"{norm(item.title)} {norm(item.album)} {norm(item.artist)}"
    request = f"{want_title} {norm(artist)} {norm(album)}"
    for word in VARIANT_WORDS:
        if _has_word(norm(haystack), norm(word)) and not _has_word(request, norm(word)):
            s -= 4

    got_album = norm(item.album)
    if album and got_album and norm(album) in got_album:
        s += 3
    # Album version beats the single: a release named after the track is usually the single.
    if got_album and got_album == norm(_strip_brackets(item.title)) and not album:
        s -= 1
    for word in EDIT_WORDS:
        if _has_word(norm(item.title), word) and not _has_word(request, word):
            s -= 1
            break
    return s


def rank(items: list[MediaItem], title: str, artist: str | None = None,
         album: str | None = None) -> list[tuple[float, MediaItem]]:
    """Score every item against a request.

    Args:
        items: Search results, in the service's order.
        title: Requested title.
        artist: Requested artist, if any.
        album: Requested album, if any.

    Returns:
        ``(score, item)`` pairs, best first; ties keep the service's order.
    """
    scored = sorted(((score(i, title, artist, album), n, i) for n, i in enumerate(items)),
                    key=lambda t: (-t[0], t[1]))
    return [(s, i) for s, _, i in scored]


def same_artist(item: MediaItem, artist: str) -> bool:
    """Check whether an item is by the given artist (loose, either name containing the other)."""
    want, got = norm(artist), norm(item.artist)
    return bool(want and got) and (want in got or got in want)


def _strip_brackets(text: str) -> str:
    """Remove ``(...)`` and ``[...]`` parts, e.g. "(Remastered)"."""
    return re.sub(r"[\(\[].*?[\)\]]", "", text or "")


def _has_word(haystack: str, phrase: str) -> bool:
    """Whole-word phrase search in normalised text."""
    return f" {phrase} " in f" {haystack} "
