"""Expose the tools to Claude as an in-process MCP server.

This file is the whole contract with Claude: each tool's name, description and
JSON schema, and which tools each agent role gets (``ROLE_TOOLS``). The
behaviour lives in ``tools.py``, ``curator_tools.py`` and ``dayplan_tools.py``;
the conversation in ``session.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from music_agent.agent.curator_tools import CuratorTools
from music_agent.agent.dayplan_tools import DayPlanTools
from music_agent.agent.tools import MusicTools, ToolError

log = logging.getLogger(__name__)

SERVER = "music"
TOOL_PREFIX = f"mcp__{SERVER}__"  # how Claude Code names tools from this server

ROOMS = {"type": "array", "items": {"type": "string"},
         "description": "Room names to play in. Omit to use the default (all speakers)."}
MODE = {"type": "string", "enum": ["replace", "append", "next"],
        "description": "replace = new queue and start now (default); append = add to end; next = play after the current track."}

TRACKS = {"type": "array", "maxItems": 50, "items": {
    "type": "object", "required": ["title", "artist"], "properties": {
        "title": {"type": "string"},
        "artist": {"type": "string"},
        "album": {"type": "string", "description": "Optional; helps pick the album version."},
    }}}
PART_OF_DAY = {"type": "string", "enum": ["morning", "afternoon", "evening", "night"]}
FREQUENCY = {"type": "string", "enum": ["regular", "sometimes", "rarely", "on_request"],
             "description": "for a love: how often it should come up. sometimes = now and then; rarely = an "
                            "occasional surprise; on_request = only when asked or the context fits. Anything "
                            "but regular keeps it out of the DJ's liked lists. Use this, not a negative nudge."}
CONTEXT = {"type": "string", "description": "when it applies, in a few words, e.g. 'at the gym', "
                                            "'for massages or deep relaxation'"}
GENRE = {"type": "string",
         "description": "the one genre (or tight style family) you chose for this set, e.g. 'bossa nova' or "
                        "'70s soul'; get_context shows it as current_genre for the next hour"}
FOR_OTHERS = {"type": "boolean",
              "description": "true when the music is for other people (guests, a party, kids, someone else's "
                             "request), so it doesn't train the owner's taste profile."}

# Which tools each agent gets. Anything not listed is invisible to that agent.
ROLE_TOOLS = {
    "dj": ("get_context", "play_tracks", "play_playlist", "search_music", "play_item",
           "playback_control", "set_volume", "get_queue", "record_feedback", "suggest_playlist"),
    "curator": ("get_context", "get_taste", "get_listening_history",
                "add_preference", "update_preference", "remove_preference",
                "add_finding", "record_quiz_answer", "confirm_finding", "update_finding", "look_up_tracks",
                "record_feedback", "forget_feedback", "adjust_score", "remove_adjustment",
                "write_note", "remove_note",
                "play_tracks", "playback_control", "search_music",
                "save_playlist", "get_playlist", "delete_playlist", "play_playlist",
                "get_day_plan", "update_day_plan"),
    # Runs the day: reads the plan and the taste, and asks the DJ for music. It can't stop
    # music or change the volume, so it can't fight the person.
    "orchestrator": ("get_day_plan", "get_context", "ask_dj"),
}

SLOT = {"type": "object", "required": ["start"], "properties": {
    "start": {"type": "string", "description": "'HH:MM', or 'wake' / 'sleep' with an offset like 'wake+90m', "
                                               "'sleep-1h'"},
    "mood": {"type": "string", "description": "what the person wants then, roughly, in their words"},
    "days": {"type": "string", "enum": ["all", "weekdays", "weekend"]},
    "rooms": {"type": "array", "items": {"type": "string"}, "description": "only these rooms; omit for the default"},
    "quiet": {"type": "boolean", "description": "true: don't start music in this part of the day"},
}}
DAY_PLAN = {"type": "object", "properties": {
    "enabled": {"type": "boolean", "description": "the orchestrator runs only when true"},
    "wake": {"type": "string", "description": "weekday wake-up, HH:MM"},
    "sleep": {"type": "string", "description": "weekday bedtime (music isn't started after it), HH:MM"},
    "weekend_wake": {"type": "string"},
    "weekend_sleep": {"type": "string"},
    "check_every_minutes": {"type": "integer", "minimum": 1, "maximum": 60},
    "quiet_after_stop_hours": {"type": "number", "minimum": 0, "maximum": 24},
    "respect_requests_minutes": {"type": "integer", "minimum": 0, "maximum": 1440,
                                 "description": "music the person chose isn't replaced at a new slot until "
                                                "it is this old"},
    "slots": {"type": "array", "items": SLOT},
}, "description": "the WHOLE plan, as get_day_plan returns it under 'plan', changed only where needed"}

ToolFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def build_tools(mt: MusicTools, curator: CuratorTools | None = None, role: str = "dj",
                day: DayPlanTools | None = None) -> list[SdkMcpTool]:
    """Wrap the tool methods a role may use as Agent SDK tools.

    Args:
        mt: The music tools.
        curator: The curator's tools; needed for ``role="curator"``.
        role: A key of ``ROLE_TOOLS``.
        day: The day plan tools; needed for the curator and the orchestrator.

    Returns:
        SDK tool definitions, in ``ROLE_TOOLS`` order. Handlers return the
        method's dict as JSON text, and turn exceptions into error results so
        a bug never ends the session.

    Raises:
        ValueError: Unknown role, or a role without the tools it needs.
    """
    if role not in ROLE_TOOLS:
        raise ValueError(f"role must be one of {sorted(ROLE_TOOLS)}")
    available = {t.name: t for t in _music_tools(mt)}
    if curator:
        available.update((t.name, t) for t in _curator_tools(curator))
    if day:
        available.update((t.name, t) for t in _day_plan_tools(day))
    missing = [n for n in ROLE_TOOLS[role] if n not in available]
    if missing:
        raise ValueError(f"role '{role}' is missing the tools {', '.join(missing)}")
    return [available[n] for n in ROLE_TOOLS[role]]


def _music_tools(mt: MusicTools) -> list[SdkMcpTool]:
    """Every ``MusicTools`` method as an SDK tool."""
    return [
        tool("get_context",
             "Current local time, weekday, part of day, what's playing, queue, rooms, volume and safety limits, "
             "the taste profile, saved playlists, the genre of the current set and often requested genres. "
             "Call this before choosing music.",
             _schema({}))(_wrap(lambda a: mt.get_context())),

        tool("play_tracks",
             "Look up specific songs in the catalogue and queue the ones found, in the given order. "
             "Returns what was queued, total duration, and which songs were not found.",
             _schema({"tracks": TRACKS, "mode": MODE, "rooms": ROOMS, "for_others": FOR_OTHERS,
                      "genre": GENRE}, ["tracks"]))(
            _wrap(lambda a: mt.play_tracks(a["tracks"], a.get("mode", "replace"), _rooms(a),
                                           bool(a.get("for_others", False)), a.get("genre")))),

        tool("play_playlist",
             "Play one of the saved playlists listed in get_context (saved_playlists) by name. Fast: "
             "its tracks are already known, so nothing needs searching.",
             _schema({"name": {"type": "string"}, "mode": MODE, "rooms": ROOMS,
                      "shuffle": {"type": "boolean", "description": "Play in random order."},
                      "for_others": FOR_OTHERS, "genre": GENRE}, ["name"]))(
            _wrap(lambda a: mt.play_playlist(a["name"], a.get("mode", "replace"), _rooms(a),
                                             bool(a.get("shuffle", False)), bool(a.get("for_others", False)),
                                             a.get("genre")))),

        tool("search_music",
             "Search the catalogue. Use to discover albums/playlists/artists; returns refs for play_item.",
             _schema({
                 "query": {"type": "string"},
                 "kind": {"type": "string", "enum": ["track", "album", "playlist", "artist"]},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 25},
             }, ["query"]))(_wrap(lambda a: mt.search_music(a["query"], a.get("kind", "track"), a.get("limit", 8)))),

        tool("play_item",
             "Play a single search result (e.g. a whole album or playlist) by its ref from search_music.",
             _schema({"ref": {"type": "string"}, "mode": MODE, "rooms": ROOMS, "for_others": FOR_OTHERS,
                      "genre": GENRE}, ["ref"]))(
            _wrap(lambda a: mt.play_item(a["ref"], a.get("mode", "replace"), _rooms(a),
                                         bool(a.get("for_others", False)), a.get("genre")))),

        tool("playback_control", "Pause, resume, skip to the next track, or stop.",
             _schema({"action": {"type": "string", "enum": ["pause", "resume", "skip", "stop"]}}, ["action"]))(
            _wrap(lambda a: mt.playback_control(a["action"]))),

        tool("set_volume",
             "Set the volume: an absolute level (0-100) or a relative change. Without room it moves the whole "
             "playing group (rooms keep their balance); with room (e.g. 'turn Night down a bit') only that room.",
             _schema({"level": {"type": "integer", "minimum": 0, "maximum": 100},
                      "change": {"type": "integer", "minimum": -50, "maximum": 50},
                      "room": {"type": "string", "description": "only this room; omit for the whole group"}}))(
            _wrap(lambda a: mt.set_volume(a.get("level"), a.get("change"), a.get("room")))),

        tool("get_queue", "List the upcoming queue.",
             _schema({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}))(
            _wrap(lambda a: mt.get_queue(a.get("limit", 20)))),

        tool("record_feedback",
             "Save the person's opinion so future choices improve. Use whenever they express a like or "
             "dislike, e.g. 'love this' (current_track, like), 'no more Coldplay' (artist, dislike), "
             "'less reggaeton in the mornings' (general, dislike, note). Add a short note with any nuance.",
             _schema({
                 "sentiment": {"type": "string", "enum": ["like", "dislike", "neutral"]},
                 "about": {"type": "string", "enum": ["current_track", "track", "artist", "general"]},
                 "title": {"type": "string", "description": "for about=track"},
                 "artist": {"type": "string", "description": "for about=track or artist"},
                 "note": {"type": "string", "description": "the person's point in a few words; required for general"},
             }, ["sentiment", "about"]))(
            _wrap(lambda a: mt.record_feedback(a["sentiment"], a["about"], a.get("title"),
                                               a.get("artist"), a.get("note")))),

        tool("suggest_playlist",
             "Leave a note for the taste curator: a kind of request comes up often enough to deserve its own "
             "saved playlist. The same idea is only noted once.",
             _schema({"idea": {"type": "string",
                               "description": "what the playlist would be for, e.g. 'bossa nova for cooking'"},
                      "why": {"type": "string",
                              "description": "the evidence, e.g. 'bossa nova asked for 4 times in 30 days, "
                                             "no saved playlist fits'"}}, ["idea", "why"]))(
            _wrap(lambda a: mt.suggest_playlist(a["idea"], a["why"]))),
    ]


def _curator_tools(ct: CuratorTools) -> list[SdkMcpTool]:
    """Every ``CuratorTools`` method as an SDK tool."""
    return [
        tool("get_taste",
             "Everything that shapes the DJ's choices: the profile it sees, stated preferences, recent feedback, "
             "score adjustments and your notebook (all with ids), learned scores with where they came from, and "
             "saved playlists. Call this first.",
             _schema({}))(_wrap(lambda a: ct.get_taste())),

        tool("get_listening_history",
             "What really happened lately: tracks finished or skipped (with when, how much was heard and the "
             "set's genre), counts per artist and per genre, how often each genre was chosen (by the person's "
             "requests and by the day plan), and what the person asked the DJ for. Use it to spot patterns "
             "worth asking about.",
             _schema({"days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "default 14"},
                      "artist": {"type": "string", "description": "only this artist"}}))(
            _wrap(lambda a: ct.get_listening_history(a.get("days", 14), a.get("artist")))),

        tool("add_preference",
             "Save a lasting preference the person states, e.g. 'I love Radiohead' (artist, like), "
             "'no reggaeton in the morning' (genre, dislike, part_of_day=morning), 'instrumental only while "
             "I work' (rule, neutral), 'I love Enya but only for massages' (artist, like, frequency=on_request, "
             "context='massages or deep relaxation'). It never fades and outranks learned scores.",
             _schema({
                 "kind": {"type": "string", "enum": ["artist", "track", "genre", "rule"]},
                 "sentiment": {"type": "string", "enum": ["like", "dislike", "neutral"]},
                 "subject": {"type": "string", "description": "the artist, track title, genre, or the rule in a few words"},
                 "artist": {"type": "string", "description": "for kind=track"},
                 "part_of_day": {**PART_OF_DAY, "description": "only at this time of day; omit for any time"},
                 "note": {"type": "string", "description": "nuance in the person's words"},
                 "frequency": FREQUENCY,
                 "context": CONTEXT,
             }, ["kind", "sentiment", "subject"]))(
            _wrap(lambda a: ct.add_preference(a["kind"], a["sentiment"], a["subject"], a.get("artist"),
                                              a.get("part_of_day"), a.get("note"), a.get("frequency"),
                                              a.get("context")))),

        tool("update_preference",
             "Change a stated preference by its id from get_taste; fields you leave out stay as they are. "
             "Use part_of_day='any' to make it apply at any time.",
             _schema({
                 "id": {"type": "integer"},
                 "kind": {"type": "string", "enum": ["artist", "track", "genre", "rule"]},
                 "sentiment": {"type": "string", "enum": ["like", "dislike", "neutral"]},
                 "subject": {"type": "string"},
                 "artist": {"type": "string", "description": "for kind=track"},
                 "part_of_day": {"type": "string", "enum": ["morning", "afternoon", "evening", "night", "any"]},
                 "note": {"type": "string", "description": "empty string removes the note"},
                 "frequency": FREQUENCY,
                 "context": {**CONTEXT, "description": "when it applies; empty string removes it"},
             }, ["id"]))(
            _wrap(lambda a: ct.update_preference(a["id"], a.get("kind"), a.get("sentiment"), a.get("subject"),
                                                 a.get("artist"), a.get("part_of_day"), a.get("note"),
                                                 a.get("frequency"), a.get("context")))),

        tool("add_finding",
             "Start a TENTATIVE conclusion from a quiz, e.g. 'dislikes K-pop' (genre, dislike). It reaches the DJ "
             "only as a soft hint until confirm_finding succeeds. Back it with record_quiz_answer.",
             _schema({
                 "kind": {"type": "string", "enum": ["genre", "artist", "era", "mood", "other"]},
                 "subject": {"type": "string", "description": "what it's about, e.g. 'K-pop', '70s soul'"},
                 "sentiment": {"type": "string", "enum": ["like", "dislike", "neutral"],
                               "description": "neutral = mixed"},
                 "statement": {"type": "string", "description": "the conclusion as the DJ should read it"},
                 "note": {"type": "string"},
             }, ["kind", "subject", "sentiment", "statement"]))(
            _wrap(lambda a: ct.add_finding(a["kind"], a["subject"], a["sentiment"], a["statement"],
                                           a.get("note")))),

        tool("record_quiz_answer",
             "Record the person's reaction to a quiz sample (use instead of record_feedback in a quiz). Saves it "
             "as feedback and as evidence for the findings you list; returns what each finding still needs.",
             _schema({
                 "title": {"type": "string"},
                 "artist": {"type": "string"},
                 "reaction": {"type": "string", "enum": ["love", "ok", "not_for_me"]},
                 "finding_ids": {"type": "array", "items": {"type": "integer"},
                                 "description": "findings this sample is evidence for"},
                 "salvage": {"type": "boolean",
                             "description": "true if you picked it to give the subject a fair chance: a different "
                                            "side of it (sub-style, era, a less obvious artist) than before"},
                 "note": {"type": "string", "description": "their words, if they said more"},
             }, ["title", "artist", "reaction"]))(
            _wrap(lambda a: ct.record_quiz_answer(a["title"], a["artist"], a["reaction"], a.get("finding_ids"),
                                                  bool(a.get("salvage", False)), a.get("note")))),

        tool("confirm_finding",
             "Confirm a finding once the person agrees. The code refuses until the evidence is enough (enough "
             "samples from different artists, not all famous hits, mostly agreeing, and a salvage try for a "
             "dislike) and says what's missing.",
             _schema({"id": {"type": "integer"}}, ["id"]))(_wrap(lambda a: ct.confirm_finding(a["id"]))),

        tool("update_finding",
             "Reword a finding, drop it (status=dropped) when the evidence goes against it, or reopen it.",
             _schema({"id": {"type": "integer"}, "statement": {"type": "string"},
                      "status": {"type": "string", "enum": ["tentative", "dropped"]},
                      "note": {"type": "string"}}, ["id"]))(
            _wrap(lambda a: ct.update_finding(a["id"], a.get("statement"), a.get("status"), a.get("note")))),

        tool("look_up_tracks",
             "Look up year, approximate tempo, style/mood tags and popularity (hit / well known / known / deep "
             "cut) of up to 10 tracks in public music databases (done by code, cached). Use it to choose quiz "
             "samples that aren't all famous hits, and to describe how music sounds.",
             _schema({"tracks": {"type": "array", "maxItems": 10, "items": {
                          "type": "object", "required": ["title", "artist"],
                          "properties": {"title": {"type": "string"}, "artist": {"type": "string"}}}},
                      "refresh": {"type": "boolean", "description": "ignore the cache"}}, ["tracks"]))(
            _wrap(lambda a: ct.look_up_tracks(a["tracks"], bool(a.get("refresh", False))))),

        tool("remove_preference", "Delete a stated preference by its id from get_taste.",
             _schema({"id": {"type": "integer"}}, ["id"]))(_wrap(lambda a: ct.remove_preference(a["id"]))),

        tool("forget_feedback",
             "Delete one piece of past feedback by its id from get_taste (e.g. an old dislike the person "
             "takes back).",
             _schema({"id": {"type": "integer"}}, ["id"]))(_wrap(lambda a: ct.forget_feedback(a["id"]))),

        tool("adjust_score",
             "Nudge the learned score of an artist or track up or down (e.g. 'a bit less Radiohead lately' -> "
             "artist, change -2), and/or reset what was learned about it (e.g. 'give Coldplay another chance' -> "
             "reset_learning). Nudges add up and never fade. Softer than a preference: no wording reaches the DJ, "
             "only the score.",
             _schema({
                 "kind": {"type": "string", "enum": ["artist", "track"]},
                 "subject": {"type": "string", "description": "the artist, or the track title"},
                 "artist": {"type": "string", "description": "for kind=track"},
                 "change": {"type": "number", "minimum": -10, "maximum": 10,
                            "description": "added to the score; a stated love is +5, an artist like +3"},
                 "reset_learning": {"type": "boolean",
                                    "description": "ignore older feedback, skips and finished tracks for it"},
                 "note": {"type": "string", "description": "why, in the person's words"},
             }, ["kind", "subject"]))(
            _wrap(lambda a: ct.adjust_score(a["kind"], a["subject"], a.get("change", 0), a.get("artist"),
                                            bool(a.get("reset_learning", False)), a.get("note")))),

        tool("remove_adjustment", "Delete a score adjustment (its nudge and any reset) by its id from get_taste.",
             _schema({"id": {"type": "integer"}}, ["id"]))(_wrap(lambda a: ct.remove_adjustment(a["id"]))),

        tool("write_note",
             "Write in your notebook, which you read back (via get_taste) at the start of every conversation. "
             "Note what you asked in a quiz and how they answered, patterns you noticed, open questions. "
             "share_with_dj=true also shows it to the DJ as one of the person's notes.",
             _schema({
                 "text": {"type": "string"},
                 "topic": {"type": "string", "description": "short heading, e.g. 'quiz: 70s soul'"},
                 "share_with_dj": {"type": "boolean"},
             }, ["text"]))(
            _wrap(lambda a: ct.write_note(a["text"], a.get("topic"), bool(a.get("share_with_dj", False))))),

        tool("remove_note", "Delete a notebook entry by its id (e.g. one that's out of date).",
             _schema({"id": {"type": "integer"}}, ["id"]))(_wrap(lambda a: ct.remove_note(a["id"]))),

        tool("save_playlist",
             "Create or update a saved playlist the DJ can play instantly. Songs are looked up in the catalogue "
             "first; misses come back in not_found with the artist's real tracks. The description is what the "
             "DJ reads to decide when the playlist fits, so say when and for what it suits.",
             _schema({
                 "name": {"type": "string"},
                 "description": {"type": "string", "description": "when it fits: mood, activity, time of day"},
                 "tracks": TRACKS,
                 "mode": {"type": "string", "enum": ["replace", "append"],
                          "description": "replace the tracks (default) or add to the end"},
             }, ["name", "tracks"]))(
            _wrap(lambda a: ct.save_playlist(a["name"], a["tracks"], a.get("description"),
                                             a.get("mode", "replace")))),

        tool("get_playlist", "Show a saved playlist's description and tracks.",
             _schema({"name": {"type": "string"}}, ["name"]))(_wrap(lambda a: ct.get_playlist(a["name"]))),

        tool("delete_playlist", "Delete a saved playlist. Only when the person asks for it.",
             _schema({"name": {"type": "string"}}, ["name"]))(_wrap(lambda a: ct.delete_playlist(a["name"]))),
    ]


def _day_plan_tools(dt: DayPlanTools) -> list[SdkMcpTool]:
    """Every ``DayPlanTools`` method as an SDK tool."""
    return [
        tool("get_day_plan",
             "The person's day plan (a rough outline of the music they want when), today's times for each part "
             "of the day (the current one marked now), whether a stop is being respected (quiet_until), and "
             "how it went lately: the orchestrator's recent runs and when the person stopped the music.",
             _schema({}))(_wrap(lambda a: dt.get_day_plan())),

        tool("update_day_plan",
             "Replace the day plan the orchestrator follows. Send the whole plan back (from get_day_plan's "
             "'plan'), changed only where needed. It is checked before saving; the error says what's wrong.",
             _schema({"plan": DAY_PLAN}, ["plan"]))(_wrap(lambda a: dt.update_day_plan(a["plan"]))),

        tool("ask_dj",
             "Ask the DJ to play music: describe the style, energy, roughly how long, the queue mode "
             "(replace / append) and rooms if needed. The DJ picks and queues the tracks and replies.",
             _schema({"request": {"type": "string", "description": "e.g. 'About an hour of gentle acoustic folk "
                                                                   "and soft jazz, low energy building slowly. "
                                                                   "Replace the queue.'"}}, ["request"]))(
            _wrap(lambda a: dt.ask_dj(a["request"]))),
    ]


def build_server(mt: MusicTools, curator: CuratorTools | None = None, role: str = "dj",
                 day: DayPlanTools | None = None) -> tuple[Any, list[str]]:
    """Create the MCP server for the SDK options.

    Args:
        mt: The music tools.
        curator: The curator's tools, for ``role="curator"``.
        role: Which tool set to expose (``ROLE_TOOLS``).
        day: The day plan tools, for the curator and the orchestrator.

    Returns:
        ``(server config, allowed tool names)``; the names are what
        ``ClaudeAgentOptions.allowed_tools`` must list, and nothing else.
    """
    tools = build_tools(mt, curator, role, day)
    server = create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=tools)
    return server, [f"{TOOL_PREFIX}{t.name}" for t in tools]


def _schema(properties: dict, required: list[str] | None = None) -> dict:
    """JSON schema for an object with the given properties."""
    return {"type": "object", "properties": properties, "required": required or []}


def _rooms(args: dict[str, Any]) -> str | list[str] | None:
    """The ``rooms`` argument, with an empty list treated as "use the default"."""
    value = args.get("rooms")
    if isinstance(value, str):
        return value
    return value or None


def _wrap(fn: ToolFn) -> ToolFn:
    """Turn a tool method into an SDK handler returning MCP content."""
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = await fn(args)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": f"Error: {exc}"}], "is_error": True}
        except Exception as exc:  # never crash the session on a tool bug
            log.exception("tool failed")
            return {"content": [{"type": "text", "text": f"Unexpected error: {exc}"}], "is_error": True}
        return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}]}
    return handler
