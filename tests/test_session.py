"""Agent SDK wiring: tool registration and lockdown (Claude is not called)."""
from __future__ import annotations

import json
import os

import pytest

from music_agent.agent.curator_tools import CuratorTools
from music_agent.agent.server import ROLE_TOOLS, build_tools
from music_agent.agent.session import MusicAgent, use_subscription_login
from music_agent.app import App
from music_agent.learning.playlists import PlaylistStore
from tests.fakes import (
    CATALOGUE,
    NIGHT,
    FakeProvider,
    make_memory,
    make_player,
    make_tools,
    run,
)


def test_sdk_tool_handlers_return_json_and_errors():
    tools, _ = make_tools(now=NIGHT)
    sdk_tools = {t.name: t for t in build_tools(tools)}
    assert set(sdk_tools) == {"get_context", "play_tracks", "play_playlist", "search_music", "play_item",
                              "playback_control", "set_volume", "get_queue", "record_feedback"}
    ok = run(sdk_tools["get_context"].handler({}))
    assert json.loads(ok["content"][0]["text"])["in_quiet_hours"] is True
    err = run(sdk_tools["play_tracks"].handler({"tracks": [{"title": "So What", "artist": "Miles Davis"}]}))
    assert err["is_error"] and "Quiet hours" in err["content"][0]["text"]


def _app():
    player, _ = make_player()
    provider = FakeProvider(CATALOGUE)
    player.provider = provider
    memory = make_memory()
    return App(player.config, player, provider, memory=memory, playlists=PlaylistStore(memory.db))


@pytest.mark.parametrize("role", ["dj", "curator"])
def test_agent_options_are_locked_down(role):
    agent = MusicAgent(_app(), role=role)
    opts = agent.options()
    assert opts.tools == []                       # no Bash/Read/Write/Web
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.allowed_tools == [f"mcp__music__{n}" for n in ROLE_TOOLS[role]]
    assert opts.model == "sonnet" and opts.max_turns == 20


def test_each_role_gets_its_own_prompt_and_tools():
    dj, curator = MusicAgent(_app()), MusicAgent(_app(), role="curator")
    assert "Dining Room" in dj.options().system_prompt and "play_playlist" in dj.options().system_prompt
    assert "taste curator" in curator.options().system_prompt
    # Only the curator edits taste; only the DJ changes the volume.
    assert "set_volume" in ROLE_TOOLS["dj"] and "set_volume" not in ROLE_TOOLS["curator"]
    assert "add_preference" in ROLE_TOOLS["curator"] and "add_preference" not in ROLE_TOOLS["dj"]
    # Only the curator (GUI only) can make the code look tracks up online; the DJ reads Discord messages.
    assert "look_up_tracks" in ROLE_TOOLS["curator"] and "look_up_tracks" not in ROLE_TOOLS["dj"]
    assert (dj.tools.source, curator.tools.source) == ("dj", "curator")   # recorded with each play
    with pytest.raises(ValueError):
        MusicAgent(_app(), role="admin")


def test_curator_tool_handlers():
    tools, _ = make_tools(memory=make_memory())
    sdk = {t.name: t for t in build_tools(tools, CuratorTools(tools), role="curator")}
    ok = run(sdk["add_preference"].handler({"kind": "artist", "sentiment": "dislike", "subject": "Coldplay"}))
    assert json.loads(ok["content"][0]["text"])["preference"] == "avoid artist: Coldplay"
    bad = run(sdk["add_preference"].handler({"kind": "mood", "sentiment": "like", "subject": "x"}))
    assert bad["is_error"] and "kind must be" in bad["content"][0]["text"]
    note = run(sdk["write_note"].handler({"text": "likes 70s soul", "share_with_dj": True}))
    assert json.loads(note["content"][0]["text"])["shared_with_dj"] is True
    nudge = run(sdk["adjust_score"].handler({"kind": "artist", "subject": "Coldplay", "change": 2}))
    assert json.loads(nudge["content"][0]["text"])["score"] == -3   # stated avoid -5, nudged +2
    history = run(sdk["get_listening_history"].handler({}))
    assert json.loads(history["content"][0]["text"])["days"] == 14
    with pytest.raises(ValueError, match="CuratorTools"):
        build_tools(tools, None, role="curator")


def test_api_key_is_removed_so_subscription_is_used(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    use_subscription_login()
    assert "ANTHROPIC_API_KEY" not in os.environ
