"""Discord bot logic with fake Discord objects and a scripted agent (no network)."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from music_agent.agent.session import Event
from music_agent.agent.tools import MusicTools
from music_agent.app import App
from music_agent.discord_bot import BotSettings, MusicBot, TurnSummary, _minutes, format_taste, split_message
from music_agent.learning.memory import Memory
from music_agent.models import MediaItem
from tests.fakes import (
    DAY,
    FakeProvider,
    ScriptedAgent,
    make_player,
    run,
)

ME, STRANGER, CHANNEL, OTHER_CHANNEL = 111, 999, 555, 666


class FakeSent:
    _next = 1000

    def __init__(self, content):
        FakeSent._next += 1
        self.id, self.content, self.reactions = FakeSent._next, content, []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


class Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeChannel:
    def __init__(self, cid):
        self.id, self.sent = cid, []

    def typing(self):
        return Typing()

    async def send(self, content, delete_after=None):
        msg = FakeSent(content)
        self.sent.append(msg)
        return msg


class FakeMessage:
    def __init__(self, content, author=ME, channel=None, dm=False, bot=False):
        self.content = content
        self.author = SimpleNamespace(id=author, bot=bot)
        self.channel = channel or FakeChannel(CHANNEL)
        self.guild = None if dm else SimpleNamespace(id=1)
        self.replies, self.reactions = [], []

    async def reply(self, content, mention_author=True):
        msg = FakeSent(content)
        self.replies.append(msg)
        return msg

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)


def queue_script(text):
    result = {"queued": 13, "tracks": ["a"] * 13, "total_duration": "52 min", "rooms": ["Dining Room"]}
    turn = SimpleNamespace(cost_usd=0.08, result=SimpleNamespace(subtype="success"))
    return [Event("tool_call", "play_tracks", {}), Event("tool_result", data=json.dumps(result)),
            Event("text", data="Queued an upbeat set."), Event("done", data=turn)]


def make_bot(script=queue_script, channel=CHANNEL, **discord_cfg):
    player, house = make_player(now=DAY)
    provider = FakeProvider([MediaItem("fake", "track", "t1", "Pecadora", "Lila Downs", "Album", 230)])
    player.provider = provider
    memory = Memory(":memory:")
    app = App(player.config, player, provider, memory=memory)
    for k, v in discord_cfg.items():
        setattr(app.config.discord, k, v)
    tools = MusicTools(app.config, player, provider, now_fn=DAY, memory=memory)
    agent = ScriptedAgent(tools, script)
    bot = MusicBot(BotSettings("token", {ME}, channel), app=app, agent=agent)
    bot._connection.user = SimpleNamespace(id=42)  # the bot's own user
    return bot, agent, house, memory


def test_settings_from_env():
    s = BotSettings.from_env({"DISCORD_TOKEN": "abc", "DISCORD_ALLOWED_USER_IDS": "1, 2 3",
                              "DISCORD_CHANNEL_ID": "9"})
    assert s.allowed_user_ids == {1, 2, 3} and s.channel_id == 9
    with pytest.raises(ValueError, match="DISCORD_TOKEN is missing.*empty"):
        BotSettings.from_env({"DISCORD_TOKEN": "paste-the-bot-token-here"})
    with pytest.raises(ValueError, match="non-numeric"):
        BotSettings.from_env({"DISCORD_TOKEN": "x", "DISCORD_ALLOWED_USER_IDS": "@me"})


def test_split_message():
    assert split_message("hi") == ["hi"]
    text = "\n\n".join(["x" * 800] * 5)
    parts = split_message(text, limit=1900)
    assert all(len(p) <= 1900 for p in parts) and "".join(parts).replace("\n", "") == "x" * 4000
    assert split_message("y" * 5000, limit=1900)[0] == "y" * 1900


def test_minutes_and_footer():
    assert [_minutes(x) for x in ("52 min", "1h55", "unknown", None)] == [52, 115, 0, 0]
    s = TurnSummary(tracks_queued=30, minutes=115, seconds=41.2, cost_usd=0.091)
    assert s.footer() == "-# 30 tracks · 1h55 · 41s · ~$0.09 credit"
    assert TurnSummary(seconds=2).footer() == "-# 2s"


def test_ignores_strangers_bots_and_other_channels():
    bot, agent, _, _ = make_bot()
    for msg in (FakeMessage("play music", author=STRANGER),
                FakeMessage("play music", bot=True),
                FakeMessage("play music", channel=FakeChannel(OTHER_CHANNEL)),
                FakeMessage("play music", author=STRANGER, dm=True)):
        run(bot.on_message(msg))
        assert not msg.replies
    assert agent.prompts == []


def test_dm_from_allowed_user_works_and_dm_only_mode():
    bot, agent, _, _ = make_bot(channel=None)
    run(bot.on_message(FakeMessage("play music", channel=FakeChannel(CHANNEL))))
    assert agent.prompts == []                       # no channel configured: channels ignored
    msg = FakeMessage("play music", dm=True)
    run(bot.on_message(msg))
    assert agent.prompts == ["play music"] and msg.replies


def test_request_reply_footer_and_reactions():
    bot, agent, _, _ = make_bot()
    msg = FakeMessage("put on something upbeat")
    run(bot.on_message(msg))
    reply = msg.replies[0]
    assert reply.content.startswith("Queued an upbeat set.")
    assert reply.content.endswith("-# 13 tracks · 52 min · 0s · ~$0.08 credit")
    assert reply.reactions == ["👍", "👎", "⏭️"] and reply.id in bot._music_messages


def test_no_reactions_when_nothing_started():
    def chat(text):
        return [Event("text", data="It's 18:00."), Event("done", data=SimpleNamespace(cost_usd=0.01, result=None))]
    bot, _, _, _ = make_bot(script=chat)
    msg = FakeMessage("what time is it")
    run(bot.on_message(msg))
    assert msg.replies[0].reactions == [] and not bot._music_messages


def test_agent_failure_is_reported_and_session_reset():
    bot, agent, _, _ = make_bot(script=lambda t: [RuntimeError("Claude login expired")])
    msg = FakeMessage("play")
    run(bot.on_message(msg))
    assert "Claude login expired" in msg.replies[0].content and agent.resets == 1


def test_conversation_resets_after_max_turns():
    bot, agent, _, _ = make_bot(max_turns_per_conversation=2)
    for i in range(3):
        run(bot.on_message(FakeMessage(f"request {i}")))
    assert agent.resets == 1 and bot._turns == 1


def test_conversation_resets_after_idle():
    bot, agent, _, _ = make_bot(idle_reset_minutes=60)
    run(bot.on_message(FakeMessage("first")))
    bot._last_request -= 61 * 60
    run(bot.on_message(FakeMessage("second")))
    assert agent.resets == 1


def test_long_reply_is_split():
    def long(text):
        return [Event("text", data=("word " * 900).strip()), Event("done", data=SimpleNamespace(cost_usd=None, result=None))]
    bot, _, _, _ = make_bot(script=long)
    msg = FakeMessage("tell me")
    run(bot.on_message(msg))
    assert len(msg.replies) == 1 and len(msg.channel.sent) >= 1
    assert all(len(m.content) <= 2000 for m in msg.replies + msg.channel.sent)


def test_commands_do_not_call_claude():
    bot, agent, house, _ = make_bot()
    asyncio.run(bot.agent.tools.play_tracks([{"title": "Pecadora", "artist": "Lila Downs"}]))
    out = {c: run(bot.command(c)) for c in ("!vol +3", "!now", "!queue", "!pause", "!help", "!vol loud", "!dance")}
    assert out["!vol +3"] == "🔊 Volume 16"
    assert "Pecadora" in out["!now"] and "volume 16" in out["!now"]
    assert out["!queue"].startswith("▶ Pecadora")
    assert out["!pause"] == "⏸️ Paused." and house.by_name("Dining Room").state == "PAUSED_PLAYBACK"
    assert "Instant commands" in out["!help"] and out["!vol loud"].startswith("Usage")
    assert "Unknown command" in out["!dance"]
    assert agent.prompts == []


def test_volume_command_respects_cap():
    bot, _, _, _ = make_bot()
    asyncio.run(bot.agent.tools.play_tracks([{"title": "Pecadora", "artist": "Lila Downs"}]))
    assert run(bot.command("!vol 90")) == "🔊 Volume 35 (At the safety cap (35); cannot go higher.)"


def test_new_and_taste_commands():
    bot, agent, _, memory = make_bot()
    memory.record_feedback(1, "artist", artist="Lila Downs")
    memory.record_feedback(-1, "general", note="no reggaeton")
    assert "Fresh conversation" in run(bot.command("!new")) and agent.resets == 1
    taste = run(bot.command("!taste"))
    assert "**Liked artists**: Lila Downs" in taste and "no reggaeton" in taste


def test_command_error_is_friendly():
    bot, _, _, _ = make_bot()
    assert run(bot.command("!skip")) == "⏭️ Skipped."
    bot.agent.tools.player.now_playing = lambda: (_ for _ in ()).throw(
        __import__("music_agent.players.base", fromlist=["PlayerError"]).PlayerError("speakers offline"))
    assert "speakers offline" in run(bot.command("!now"))


def payload(message_id, emoji, user=ME, channel=CHANNEL, guild=1):
    return SimpleNamespace(message_id=message_id, emoji=emoji, user_id=user, channel_id=channel, guild_id=guild)


def test_reactions_record_feedback_and_skip():
    bot, _, house, memory = make_bot()
    channel = FakeChannel(CHANNEL)
    bot.get_channel = lambda cid: channel
    asyncio.run(bot.agent.tools.play_tracks([{"title": "Pecadora", "artist": "Lila Downs"}]))
    msg = FakeMessage("play", channel=channel)
    run(bot.on_message(msg))
    mid = msg.replies[0].id

    run(bot.on_raw_reaction_add(payload(mid, "👍")))
    assert channel.sent[-1].content == "👍 Noted: you like *Pecadora - Lila Downs*"
    fb = memory.recent_feedback(1)[0]
    assert (fb["sentiment"], fb["scope"], fb["title"]) == (1, "track", "Pecadora")

    before = house.by_name("Dining Room").current
    run(bot.on_raw_reaction_add(payload(mid, "👎")))
    assert channel.sent[-1].content.startswith("👎 Noted, and skipped")
    assert house.by_name("Dining Room").current == before + 1
    assert memory.recent_feedback(1)[0]["sentiment"] == -1


def test_reactions_ignored_from_strangers_bot_and_untracked_messages():
    bot, _, _, memory = make_bot()
    channel = FakeChannel(CHANNEL)
    bot.get_channel = lambda cid: channel
    msg = FakeMessage("play", channel=channel)
    run(bot.on_message(msg))
    mid = msg.replies[0].id
    run(bot.on_raw_reaction_add(payload(mid, "👍", user=STRANGER)))
    run(bot.on_raw_reaction_add(payload(mid, "👍", user=42)))          # the bot's own reaction
    run(bot.on_raw_reaction_add(payload(12345, "👍")))                  # not one of our music replies
    run(bot.on_raw_reaction_add(payload(mid, "🎉")))                     # unrelated emoji
    assert channel.sent == [] and memory.recent_feedback() == []


def test_format_taste_empty():
    assert format_taste({"history": {}}) == "-# 0 tracks played · 0 feedback · 0 skips seen"


def test_reply_drops_pre_action_narration():
    def script(text):
        result = {"queued": 2, "tracks": ["a", "b"], "total_duration": "8 min"}
        return [Event("text", data="Let me queue some jazz."), Event("tool_call", "play_tracks", {}),
                Event("tool_result", data=json.dumps(result)), Event("text", data="Two jazz classics."),
                Event("done", data=SimpleNamespace(cost_usd=0.02, result=None))]
    bot, _, _, _ = make_bot(script=script)
    msg = FakeMessage("jazz")
    run(bot.on_message(msg))
    assert msg.replies[0].content.startswith("Two jazz classics.\n-# 2 tracks")


def test_reconnects_log_once_as_login_then_as_reconnect(caplog):
    bot, _, _, _ = make_bot()
    with caplog.at_level("INFO", logger="music_agent.discord"):
        run(bot.on_ready())
        run(bot.on_ready())
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0].startswith("Logged in as") and messages[1] == "Reconnected to Discord."
