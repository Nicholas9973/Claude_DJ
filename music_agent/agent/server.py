"""Expose the tools to Claude as an in-process MCP server.

This file is the whole contract with Claude: each tool's name, description and
JSON schema, and which tools each agent role gets (``ROLE_TOOLS``). The
behaviour lives in ``tools.py`` and ``curator_tools.py``; the conversation in
``session.py``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from music_agent.agent.curator_tools import CuratorTools
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

# Which tools each agent gets. Anything not listed is invisible to that agent.
ROLE_TOOLS = {
    "dj": ("get_context", "play_tracks", "play_playlist", "search_music", "play_item",
           "playback_control", "set_volume", "get_queue", "record_feedback"),
    "curator": ("get_context", "get_taste", "get_listening_history",
                "add_preference", "update_preference", "remove_preference",
                "record_feedback", "forget_feedback", "adjust_score", "remove_adjustment",
                "write_note", "remove_note",
                "play_tracks", "playback_control", "search_music",
                "save_playlist", "get_playlist", "delete_playlist", "play_playlist"),
}

ToolFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def build_tools(mt: MusicTools, curator: CuratorTools | None = None, role: str = "dj") -> list[SdkMcpTool]:
    """Wrap the tool methods a role may use as Agent SDK tools.

    Args:
        mt: The music tools.
        curator: The curator's tools; needed for ``role="curator"``.
        role: A key of ``ROLE_TOOLS``.

    Returns:
        SDK tool definitions, in ``ROLE_TOOLS`` order. Handlers return the
        method's dict as JSON text, and turn exceptions into error results so
        a bug never ends the session.

    Raises:
        ValueError: Unknown role, or ``"curator"`` without ``curator``.
    """
    if role not in ROLE_TOOLS:
        raise ValueError(f"role must be one of {sorted(ROLE_TOOLS)}")
    available = {t.name: t for t in _music_tools(mt)}
    if curator:
        available.update((t.name, t) for t in _curator_tools(curator))
    missing = [n for n in ROLE_TOOLS[role] if n not in available]
    if missing:
        raise ValueError(f"role '{role}' needs CuratorTools for {', '.join(missing)}")
    return [available[n] for n in ROLE_TOOLS[role]]


def _music_tools(mt: MusicTools) -> list[SdkMcpTool]:
    """Every ``MusicTools`` method as an SDK tool."""
    return [
        tool("get_context",
             "Current local time, weekday, part of day, what's playing, queue, rooms, volume and safety limits. "
             "Call this before choosing music.",
             _schema({}))(_wrap(lambda a: mt.get_context())),

        tool("play_tracks",
             "Look up specific songs in the catalogue and queue the ones found, in the given order. "
             "Returns what was queued, total duration, and which songs were not found.",
             _schema({"tracks": TRACKS, "mode": MODE, "rooms": ROOMS}, ["tracks"]))(
            _wrap(lambda a: mt.play_tracks(a["tracks"], a.get("mode", "replace"), _rooms(a)))),

        tool("play_playlist",
             "Play one of the saved playlists listed in get_context (saved_playlists) by name. Fast: "
             "its tracks are already known, so nothing needs searching.",
             _schema({"name": {"type": "string"}, "mode": MODE, "rooms": ROOMS,
                      "shuffle": {"type": "boolean", "description": "Play in random order."}}, ["name"]))(
            _wrap(lambda a: mt.play_playlist(a["name"], a.get("mode", "replace"), _rooms(a),
                                             bool(a.get("shuffle", False))))),

        tool("search_music",
             "Search the catalogue. Use to discover albums/playlists/artists; returns refs for play_item.",
             _schema({
                 "query": {"type": "string"},
                 "kind": {"type": "string", "enum": ["track", "album", "playlist", "artist"]},
                 "limit": {"type": "integer", "minimum": 1, "maximum": 25},
             }, ["query"]))(_wrap(lambda a: mt.search_music(a["query"], a.get("kind", "track"), a.get("limit", 8)))),

        tool("play_item",
             "Play a single search result (e.g. a whole album or playlist) by its ref from search_music.",
             _schema({"ref": {"type": "string"}, "mode": MODE, "rooms": ROOMS}, ["ref"]))(
            _wrap(lambda a: mt.play_item(a["ref"], a.get("mode", "replace"), _rooms(a)))),

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
             "What really happened lately: tracks finished or skipped (with when and how much was heard), "
             "counts per artist, and what the person asked the DJ for. Use it to spot patterns worth asking about.",
             _schema({"days": {"type": "integer", "minimum": 1, "maximum": 90, "description": "default 14"},
                      "artist": {"type": "string", "description": "only this artist"}}))(
            _wrap(lambda a: ct.get_listening_history(a.get("days", 14), a.get("artist")))),

        tool("add_preference",
             "Save a lasting preference the person states, e.g. 'I love Radiohead' (artist, like), "
             "'no reggaeton in the morning' (genre, dislike, part_of_day=morning), 'instrumental only while "
             "I work' (rule, neutral). It never fades and outranks learned scores.",
             _schema({
                 "kind": {"type": "string", "enum": ["artist", "track", "genre", "rule"]},
                 "sentiment": {"type": "string", "enum": ["like", "dislike", "neutral"]},
                 "subject": {"type": "string", "description": "the artist, track title, genre, or the rule in a few words"},
                 "artist": {"type": "string", "description": "for kind=track"},
                 "part_of_day": {**PART_OF_DAY, "description": "only at this time of day; omit for any time"},
                 "note": {"type": "string", "description": "nuance in the person's words"},
             }, ["kind", "sentiment", "subject"]))(
            _wrap(lambda a: ct.add_preference(a["kind"], a["sentiment"], a["subject"], a.get("artist"),
                                              a.get("part_of_day"), a.get("note")))),

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
             }, ["id"]))(
            _wrap(lambda a: ct.update_preference(a["id"], a.get("kind"), a.get("sentiment"), a.get("subject"),
                                                 a.get("artist"), a.get("part_of_day"), a.get("note")))),

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


def build_server(mt: MusicTools, curator: CuratorTools | None = None,
                 role: str = "dj") -> tuple[Any, list[str]]:
    """Create the MCP server for the SDK options.

    Args:
        mt: The music tools.
        curator: The curator's tools, for ``role="curator"``.
        role: Which tool set to expose (``ROLE_TOOLS``).

    Returns:
        ``(server config, allowed tool names)``; the names are what
        ``ClaudeAgentOptions.allowed_tools`` must list, and nothing else.
    """
    tools = build_tools(mt, curator, role)
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
