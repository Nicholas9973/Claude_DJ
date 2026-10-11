"""How signals add up to a score per artist and per track: the numbers behind the taste profile.

Every signal adds to the score of the artist and/or track it is about:

    signal                                  default   fades?
    stated love / avoid (a preference)      +5 / -5   never
    like / dislike of an artist             +3 / -3   yes
    like / dislike of a track               +2 / -2   yes (also counts for its artist)
    track listened to the end               +0.3      yes
    track skipped early                     -0.7      yes
    manual adjustment (a nudge)             any       never

Fading signals lose half their weight every ``half_life_days``. Every number
here is a *tunable*: ``TUNING`` holds the defaults, and you can change them on
the GUI's Taste page (they're stored in the ``taste_settings`` table).

Each score remembers where it came from (``Entry.parts``), so the GUI can show
e.g. "Radiohead +6.2 = stated +5, feedback +1.5, listening -0.3".
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from music_agent.text import norm

SOURCES = ("stated", "feedback", "listening", "adjusted")  # where a score's parts come from


@dataclass(frozen=True)
class Tunable:
    """One adjustable number in the scoring.

    Attributes:
        default: Value when you haven't changed it.
        label: Short name for the GUI.
        description: What it does, in plain words.
        minimum: Lowest allowed value, if any.
        maximum: Highest allowed value, if any.
    """

    default: float
    label: str
    description: str
    minimum: float | None = None
    maximum: float | None = None


TUNING: dict[str, Tunable] = {
    "half_life_days": Tunable(60, "Fading half-life (days)",
                              "Feedback, skips and finished tracks lose half their weight after this many days. "
                              "The default comes from config.toml.", minimum=1),
    "preference": Tunable(5, "Stated love / avoid",
                          "Score of a love (+) or avoid (-) you stated for an artist or track. Never fades.",
                          minimum=0),
    "artist_feedback": Tunable(3, "Artist like / dislike",
                               "Score of a like (+) or dislike (-) of an artist.", minimum=0),
    "track_feedback": Tunable(2, "Track like / dislike",
                              "Score of a like (+) or dislike (-) of a track; it also counts for the artist.",
                              minimum=0),
    "completed": Tunable(0.3, "Finished a track", "Added each time a track is listened to the end."),
    "skipped": Tunable(-0.7, "Skipped a track", "Added each time a track is skipped early (negative = dislike)."),
    "liked_artist_at": Tunable(1.0, "Liked artist from",
                               "Artists with at least this score are listed as liked.", minimum=0.01),
    "avoid_artist_at": Tunable(-2.0, "Avoided artist from",
                               "Artists with this score or lower are avoided.", maximum=-0.01),
    "liked_track_at": Tunable(1.5, "Liked track from",
                              "Tracks with at least this score are listed as liked.", minimum=0.01),
    "avoid_track_at": Tunable(-1.5, "Avoided track from",
                              "Tracks with this score or lower are avoided.", maximum=-0.01),
    "skips_to_avoid": Tunable(2, "Skips before avoiding",
                              "A track skipped this many times is avoided, whatever its score.", minimum=1),
}


def check_tuning(key: str, value: Any) -> float:
    """Validate a new value for a tunable.

    Args:
        key: A key of ``TUNING``.
        value: The new value.

    Returns:
        The value as a float.

    Raises:
        ValueError: Unknown key, not a number, or outside the allowed range.
    """
    if key not in TUNING:
        raise ValueError(f"Unknown setting '{key}'; settings: {', '.join(TUNING)}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be a number, not '{value}'.") from None
    if not math.isfinite(number):
        raise ValueError(f"{key} must be a finite number.")
    t = TUNING[key]
    if t.minimum is not None and number < t.minimum:
        raise ValueError(f"{t.label} must be at least {t.minimum}.")
    if t.maximum is not None and number > t.maximum:
        raise ValueError(f"{t.label} must be at most {t.maximum}.")
    return number


@dataclass
class Entry:
    """The score of one artist or one track, and where it came from.

    Attributes:
        name: As shown, e.g. ``"Radiohead"`` or ``"Creep - Radiohead"``.
        title: Track title (None for an artist).
        artist: The artist (for a track, may be None).
        score: Sum of every signal.
        parts: The score split by source (``SOURCES``).
        part_of_day_score: Positive signals at the current part of day (artists only).
        skips: Times the track was skipped early.
        adjustment: The manual adjustment row for this entry, if there is one.
    """

    name: str
    title: str | None
    artist: str | None
    score: float = 0.0
    parts: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    part_of_day_score: float = 0.0
    skips: int = 0
    adjustment: dict[str, Any] | None = None

    def add(self, source: str, value: float, row_part: str | None = None, part: str | None = None) -> None:
        """Add one signal.

        Args:
            source: One of ``SOURCES``.
            value: Its weighted (and faded) value.
            row_part: Part of day the signal happened in.
            part: The current part of day; positive signals then also count
                towards ``part_of_day_score``.
        """
        self.score += value
        self.parts[source] += value
        if part and row_part == part and value > 0:
            self.part_of_day_score += value


class Scores:
    """Running totals while scoring, keyed by normalised names (so spellings merge).

    Attributes:
        artists: ``{norm(artist): Entry}``.
        tracks: ``{(norm(title), norm(artist)): Entry}``.
        preferences: The stated preferences, newest first (for the profile).
    """

    def __init__(self) -> None:
        self.artists: dict[str, Entry] = {}
        self.tracks: dict[tuple[str, str], Entry] = {}
        self.preferences: list[Any] = []

    def artist(self, name: str) -> Entry:
        """The entry for an artist, created on first use (the first spelling seen is kept)."""
        key = norm(name)
        if key not in self.artists:
            self.artists[key] = Entry(name, None, name)
        return self.artists[key]

    def track(self, title: str, artist: str | None) -> Entry:
        """The entry for a track, created on first use."""
        key = track_key(title, artist)
        if key not in self.tracks:
            self.tracks[key] = Entry(f"{title} - {artist}" if artist else title, title, artist)
        return self.tracks[key]


def track_key(title: str | None, artist: str | None) -> tuple[str, str]:
    """What makes two mentions "the same track": title and artist, loosely compared."""
    return norm(title), norm(artist)


def fade(age_days: float, half_life_days: float) -> float:
    """Weight of a signal this many days old: 1.0 when new, 0.5 after one half-life."""
    return 0.5 ** (max(0.0, age_days) / half_life_days)
