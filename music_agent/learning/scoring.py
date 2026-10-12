"""How signals add up to a score per artist and per track: the numbers behind the taste profile.

Every signal adds to the score of the artist and/or track it is about:

    signal                                  default   fades?
    stated love / avoid (a preference)      +5 / -5   never
    like / dislike of an artist             +3 / -3   yes
    like / dislike of a track               +2 / -2   yes (also counts for its artist)
    reaction to a quiz sample               +2 / -2   yes (the track; only +1 / -1 for its artist)
    track listened to the end               0         yes (track only)
    track skipped early                     -0.7      yes, faster (track; -0.2 for the artist)
    manual adjustment (a nudge)             any       never

Some signals deliberately count for little or nothing:

- Finishing a track mostly means the music was on in the background, so it
  scores 0 by default (it still shows in the history and keeps sets fresh).
- A skip often means "not now", so it fades faster (``skip_half_life_days``)
  and counts mostly against the track, not the artist.
- Quiz reactions are real opinions about the sample, so they count fully for
  that track (a rejected sample stays avoided). But one long quiz can produce
  dozens in an evening, and one famous song says little about an artist's
  other work, so each counts less for the artist. A liked quiz sample never
  becomes a "liked track" (that would put quiz songs in rotation), and quiz
  answers never decide time-of-day favourites.
- Quiz samples and music played for other people (``audience = 'others'``)
  are left out of listening signals altogether.

Fading signals lose half their weight every ``half_life_days``. Every number
here is a *tunable*: ``TUNING`` holds the defaults, and you can change them on
the GUI's Taste page (they're stored in the ``taste_settings`` table).

Each score remembers where it came from (``Entry.parts``), so the GUI can show
e.g. "Radiohead +6.2 = stated +5, feedback +1.5, quiz -0.3".
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from music_agent.text import norm

SOURCES = ("stated", "feedback", "quiz", "listening", "adjusted")  # where a score's parts come from


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
    "preference_sometimes": Tunable(0.5, "Share for a 'sometimes' love",
                                    "Part of a stated love's score that counts when you said 'sometimes'. Loves "
                                    "you said come up 'rarely' or 'only when asked' count 0 and never join "
                                    "the liked lists.", minimum=0, maximum=1),
    "artist_feedback": Tunable(3, "Artist like / dislike",
                               "Score of a like (+) or dislike (-) of an artist.", minimum=0),
    "track_feedback": Tunable(2, "Track like / dislike",
                              "Score of a like (+) or dislike (-) of a track; it also counts for the artist.",
                              minimum=0),
    "quiz_feedback": Tunable(1, "Quiz reaction (artist)",
                             "What a reaction to one of the curator's quiz samples adds to the artist (the track "
                             "itself gets the full track like / dislike). Lower than a normal like, so one long quiz "
                             "doesn't outweigh everyday listening.", minimum=0),
    "completed": Tunable(0, "Finished a track",
                         "Added to a track each time it plays to the end. 0 by default: music often plays in the "
                         "background, so finishing a track says little about liking it."),
    "skipped": Tunable(-0.7, "Skipped a track", "Added to a track each time it is skipped early (negative = dislike)."),
    "skipped_artist": Tunable(-0.2, "Skipped a track (artist)",
                              "Added to the artist when one of its tracks is skipped. Small: a skip often means "
                              "'not this one' or 'not now'.", maximum=0),
    "skip_half_life_days": Tunable(30, "Skip half-life (days)",
                                   "Skips lose half their weight after this many days, so an avoid that came only "
                                   "from skips wears off.", minimum=1),
    "part_of_day_min_days": Tunable(2, "Time-of-day favourite after (days)",
                                    "An artist becomes a favourite for a time of day (without a stated preference) "
                                    "only after likes at that time on this many different days. Quiz answers "
                                    "don't count.", minimum=1),
    "liked_artist_at": Tunable(1.0, "Liked artist from",
                               "Artists with at least this score are listed as liked.", minimum=0.01),
    "avoid_artist_at": Tunable(-2.0, "Avoided artist from",
                               "Artists with this score or lower are avoided.", maximum=-0.01),
    "liked_track_at": Tunable(1.5, "Liked track from",
                              "Tracks with at least this score are listed as liked.", minimum=0.01),
    "avoid_track_at": Tunable(-1.5, "Avoided track from",
                              "Tracks with this score or lower are avoided.", maximum=-0.01),
    "freshness_days": Tunable(14, "Freshness window (days)",
                              "Tracks played twice or more in this many days are listed as 'played often lately' "
                              "(the DJ rests them), and so are artists played a lot.", minimum=1),
    "overplayed_artist_plays": Tunable(8, "Rest an artist after (plays)",
                                       "An artist played this many times within the freshness window is listed "
                                       "for the DJ to rest for a while.", minimum=2),
    "finding_min_samples": Tunable(3, "Quiz finding: samples needed",
                                   "A conclusion from quizzes (e.g. 'dislikes K-pop') stays tentative until it "
                                   "rests on at least this many samples.", minimum=1),
    "finding_min_artists": Tunable(3, "Quiz finding: different artists needed",
                                   "...from at least this many different artists (different songs, for a "
                                   "finding about one artist), so one famous song can't decide a genre.",
                                   minimum=1),
    "finding_min_agreement": Tunable(0.75, "Quiz finding: agreement needed",
                                     "Share of the samples' reactions that must agree with the conclusion.",
                                     minimum=0.5, maximum=1),
    "skips_to_avoid": Tunable(2, "Skips before avoiding",
                              "A track skipped this many times recently is avoided, whatever its score. Old skips "
                              "count less (see the skip half-life).", minimum=1),
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
        part_of_day_days: The dates those signals came from (a favourite needs several).
        part_of_day_stated: A stated preference makes it a favourite at this part of day.
        skips: Recent skips, faded (a skip from one skip half-life ago counts 0.5).
        skip_count: Times the track was skipped early, ever (for showing).
        adjustment: The manual adjustment row for this entry, if there is one.
    """

    name: str
    title: str | None
    artist: str | None
    score: float = 0.0
    parts: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    part_of_day_score: float = 0.0
    part_of_day_days: set[str] = field(default_factory=set)
    part_of_day_stated: bool = False
    skips: float = 0.0
    skip_count: int = 0
    adjustment: dict[str, Any] | None = None

    def add(self, source: str, value: float, row_part: str | None = None, part: str | None = None,
            day: str | None = None) -> None:
        """Add one signal.

        Args:
            source: One of ``SOURCES``.
            value: Its weighted (and faded) value.
            row_part: Part of day the signal happened in; leave it out for
                signals that shouldn't shape time-of-day favourites (quiz answers).
            part: The current part of day; positive signals then also count
                towards ``part_of_day_score``.
            day: Date of the signal (``"2026-10-08"``), to count distinct days.
        """
        self.score += value
        self.parts[source] += value
        if part and row_part == part and value > 0:
            self.part_of_day_score += value
            if day:
                self.part_of_day_days.add(day)


class Scores:
    """Running totals while scoring, keyed by normalised names (so spellings merge).

    Attributes:
        artists: ``{norm(artist): Entry}``.
        tracks: ``{(norm(title), norm(artist)): Entry}``.
        preferences: The stated preferences, newest first (for the profile).
        occasional: Keys (``norm(artist)`` or ``track_key``) of loves that should
            come up only sometimes, rarely or when asked: kept out of the liked lists.
    """

    def __init__(self) -> None:
        self.artists: dict[str, Entry] = {}
        self.tracks: dict[tuple[str, str], Entry] = {}
        self.preferences: list[Any] = []
        self.occasional: set[Any] = set()

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
