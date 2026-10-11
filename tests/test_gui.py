"""The GUI's JSON API and the runtime behind it, with fake speakers and a scripted agent."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from music_agent.agent.session import Event
from music_agent.agent.tools import MusicTools
from music_agent.app import App
from music_agent.discord_bot import BotRunner
from music_agent.gui.server import create_app
from music_agent.learning.playlists import PlaylistStore
from music_agent.players.base import PlayerError
from music_agent.runtime import Runtime
from tests.fakes import CATALOGUE, DAY, FakeProvider, ScriptedAgent, make_memory, make_player


def music_script(text):
    result = {"queued": 2, "tracks": ["a", "b"], "total_duration": "8 min", "rooms": ["Night"], "volume": 13}
    turn = SimpleNamespace(cost_usd=0.02, result=SimpleNamespace(num_turns=3, subtype="success"))
    return [Event("tool_call", "play_tracks", {"tracks": [{"title": "So What", "artist": "Miles Davis"}]}),
            Event("tool_result", data=json.dumps(result)),
            Event("text", data="Queued some jazz."),
            Event("done", data=turn)]


@pytest.fixture
def gui():
    """(client, runtime, household, memory) over fake speakers, with memory on."""
    player, house = make_player(now=DAY)
    provider = FakeProvider(CATALOGUE)
    player.provider = provider
    memory = make_memory()
    app = App(player.config, player, provider, memory=memory, playlists=PlaylistStore(memory.db))
    agents = []

    def factory(a, role="dj"):
        tools = MusicTools(a.config, a.player, a.provider, now_fn=DAY, memory=a.memory, playlists=a.playlists)
        agent = ScriptedAgent(tools, music_script, role=role)
        agents.append(agent)
        return agent

    runtime = Runtime(app, agent_factory=factory)
    runtime.tools._now = DAY
    runtime.agents = agents
    with TestClient(create_app(runtime)) as client:
        yield client, runtime, house, memory


def test_serves_the_page_and_scripts(gui):
    client, *_ = gui
    assert "Claude" in client.get("/").text
    assert "PAGES" in client.get("/js/pages/index.js").text
    assert client.get("/docs").status_code == 200


def test_now_playing_and_queue(gui):
    client, runtime, house, _ = gui
    assert client.get("/api/now").json()["title"] is None
    runtime.app.player.play(CATALOGUE[:2])
    now = client.get("/api/now").json()
    assert now["state"] == "PLAYING" and now["title"] == "One More Time"
    assert now["position_s"] == 10 and now["duration_s"] == 180 and now["volume_cap"] == 35
    q = client.get("/api/queue").json()
    assert q["current_position"] == 1 and len(q["queue"]) == 2


def test_controls_and_volume_cap(gui):
    client, runtime, house, _ = gui
    runtime.app.player.play(CATALOGUE[:2])
    assert client.post("/api/playback/pause").json() == {"done": "pause"}
    assert house.by_name("Dining Room").state == "PAUSED_PLAYBACK"
    assert client.post("/api/playback/dance").status_code == 422
    assert client.post("/api/volume", json={"level": 30}).json()["volume"] == 30
    capped = client.post("/api/volume", json={"change": 50}).json()
    assert capped["volume"] == 35 and "cap" in capped["note"]
    assert client.post("/api/volume", json={}).status_code == 400          # neither level nor change
    assert client.post("/api/volume", json={"level": 101}).status_code == 422


def test_rate_current_track(gui):
    client, runtime, _, memory = gui
    bad = client.post("/api/rate", json={"sentiment": "like"})
    assert bad.status_code == 400 and "Nothing is playing" in bad.json()["detail"]
    runtime.app.player.play(CATALOGUE[:2])
    client.post("/api/rate", json={"sentiment": "dislike", "skip": True})
    assert memory.recent_feedback()[0]["sentiment"] == -1
    assert client.get("/api/now").json()["queue_position"] == 2   # skipped


def test_speakers_offline_is_503(gui, monkeypatch):
    client, runtime, *_ = gui

    def offline():
        raise PlayerError("No Sonos speakers found on the network.")

    monkeypatch.setattr(runtime.app.player, "now_playing", offline)
    res = client.get("/api/now")
    assert res.status_code == 503 and "No Sonos" in res.json()["detail"]


def test_taste_scores_and_forget(gui):
    client, _, _, memory = gui
    fid = memory.record_feedback(1, "artist", artist="Miles Davis")
    memory.record_feedback(-1, "artist", artist="Coldplay")
    t = client.get("/api/taste").json()
    assert t["profile"]["liked_artists"] == ["Miles Davis"]
    assert [a["name"] for a in t["affinities"]["artists"]] == ["Miles Davis", "Coldplay"]
    assert t["affinities"]["artists"][0]["score"] == 3.0
    assert len(client.get("/api/feedback").json()["feedback"]) == 2
    assert client.delete(f"/api/feedback/{fid}").json() == {"ok": True}
    assert client.delete(f"/api/feedback/{fid}").status_code == 404


def test_database_browsing(gui):
    client, _, _, memory = gui
    memory.record_feedback(0, "general", note="less jazz in the morning")
    tables = {t["name"]: t["rows"] for t in client.get("/api/db/tables").json()["tables"]}
    assert tables["feedback"] == 1 and {"requests", "plays", "listens"} <= set(tables)
    page = client.get("/api/db/tables/feedback").json()
    assert page["total"] == 1 and page["rows"][0][page["columns"].index("note")] == "less jazz in the morning"
    assert client.get("/api/db/tables/sqlite_master").status_code == 404   # only our own tables


def test_memory_off_gives_404():
    player, _ = make_player(now=DAY)
    runtime = Runtime(App(player.config, player, FakeProvider()), agent_factory=lambda a: None)
    with TestClient(create_app(runtime)) as client:
        assert client.get("/api/taste").status_code == 404


def test_chat_streams_events_and_keeps_history(gui):
    client, runtime, *_ = gui
    with client.stream("POST", "/api/chat", json={"text": "some jazz"}) as res:
        events = [json.loads(line) for line in res.iter_lines() if line]
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text", "done"]
    assert events[0]["summary"].startswith("play_tracks [replace] 1 tracks: So What")
    assert events[1]["summary"].startswith("queued 2 (8 min)")
    assert events[3] == {"type": "done", "cost_usd": 0.02, "turns": 3, "error": None}

    history = client.get("/api/chat").json()["history"]
    assert [m["role"] for m in history] == ["user", "agent"]
    assert history[1]["text"] == "Queued some jazz." and len(history[1]["steps"]) == 2

    client.post("/api/chat/reset", json={})
    assert client.get("/api/chat").json()["history"] == []
    assert runtime.agents[0].resets == 1


def test_chat_failure_is_reported_and_session_reset(gui):
    client, runtime, *_ = gui
    runtime.conversation("dj").agent.script = lambda text: [RuntimeError("Claude is down")]
    with client.stream("POST", "/api/chat", json={"text": "hi"}) as res:
        events = [json.loads(line) for line in res.iter_lines() if line]
    assert events == [{"type": "error", "message": "Claude is down"}]
    assert runtime.agents[0].resets == 1
    assert client.get("/api/chat").json()["history"][1]["error"] == "Claude is down"


def test_conversations_are_separate(gui):
    client, runtime, *_ = gui
    assert runtime.conversation("dj") is runtime.conversation("dj")
    assert runtime.conversation("dj") is not runtime.conversation("other")
    assert client.post("/api/chat", json={"text": ""}).status_code == 422
    assert client.post("/api/chat", json={"text": "hi", "conversation": "admin"}).status_code == 422


def test_dj_and_curator_chats_are_separate(gui):
    client, runtime, *_ = gui
    with client.stream("POST", "/api/chat", json={"text": "quiz me", "conversation": "curator"}) as res:
        list(res.iter_lines())
    assert client.get("/api/chat?conversation=curator").json()["history"][0]["text"] == "quiz me"
    assert client.get("/api/chat?conversation=dj").json()["history"] == []
    assert runtime.conversation("curator").agent.role == "curator"
    assert runtime.conversation("dj").agent.role == "dj"


def test_status_and_bot_not_configured(gui, monkeypatch, tmp_path):
    client, runtime, *_ = gui
    assert client.get("/api/status").json()["bot"]["state"] == "off"
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    runtime.bot = BotRunner(runtime.app, lambda a: None, env_file=tmp_path / "missing.env")
    bot = client.post("/api/bot/start").json()
    assert bot["state"] == "not_configured" and "DISCORD_TOKEN" in bot["detail"]


def test_logs_tail(gui, tmp_path):
    client, runtime, *_ = gui
    assert client.get("/api/logs").json() == {"lines": []}
    runtime.log_file = tmp_path / "gui.log"
    runtime.log_file.write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
    assert client.get("/api/logs?lines=3").json()["lines"] == ["line 497", "line 498", "line 499"]


def test_preferences_crud(gui):
    client, _, _, memory = gui
    pid = client.post("/api/preferences", json={"kind": "genre", "sentiment": "dislike", "subject": "reggaeton",
                                                 "part_of_day": "morning"}).json()["id"]
    prefs = client.get("/api/preferences").json()["preferences"]
    assert prefs[0]["text"] == "avoid genre: reggaeton (in the morning only)"
    assert client.get("/api/taste").json()["preferences"][0]["id"] == pid
    edited = client.put(f"/api/preferences/{pid}", json={"kind": "genre", "sentiment": "like",
                                                        "subject": "bossa nova"}).json()
    assert edited["text"] == "love genre: bossa nova"
    assert client.post("/api/preferences", json={"kind": "mood", "sentiment": "like", "subject": "x"}).status_code == 422
    assert client.put("/api/preferences/999", json={"kind": "genre", "sentiment": "like", "subject": "x"}).status_code == 404
    assert client.delete(f"/api/preferences/{pid}").json() == {"ok": True}
    assert client.delete(f"/api/preferences/{pid}").status_code == 404


def test_playlists_api(gui):
    client, _, house, _ = gui
    pl = client.post("/api/playlists", json={"name": "Jazz", "description": "Quiet"}).json()
    assert client.post("/api/playlists", json={"name": "jazz"}).status_code == 400      # name taken
    added = client.post(f"/api/playlists/{pl['id']}/tracks", json={"title": "So What", "artist": "Miles Davis"}).json()
    assert added["saved"] == 1 and added["playlist"]["tracks"][0]["title"] == "So What"
    miss = client.post(f"/api/playlists/{pl['id']}/tracks", json={"title": "Nope", "artist": "Nobody"}).json()
    assert miss["saved"] == 0 and miss["not_found"]
    client.post(f"/api/playlists/{pl['id']}/tracks", json={"title": "One More Time", "artist": "Daft Punk"})

    played = client.post(f"/api/playlists/{pl['id']}/play", json={"mode": "replace"}).json()
    assert played["queued"] == 2 and house.by_name("Dining Room").state == "PLAYING"

    renamed = client.patch(f"/api/playlists/{pl['id']}", json={"name": "Jazz & more"}).json()
    assert renamed["name"] == "Jazz & more" and renamed["description"] == "Quiet"
    first = renamed["tracks"][0]["id"]
    assert len(client.delete(f"/api/playlists/{pl['id']}/tracks/{first}").json()["tracks"]) == 1
    assert client.get("/api/playlists").json()["playlists"][0]["tracks"] == 1
    assert client.delete(f"/api/playlists/{pl['id']}").json() == {"ok": True}
    assert client.get(f"/api/playlists/{pl['id']}").status_code == 404


def test_database_editing(gui):
    client, _, _, memory = gui
    fid = memory.record_feedback(-1, "artist", artist="Coldplay")
    url = f"/api/db/tables/feedback/rows/{fid}"
    assert client.patch(url, json={"values": {"note": "fine now", "sentiment": 1}}).json()["row"]["note"] == "fine now"
    assert memory.recent_feedback()[0]["sentiment"] == 1
    bad = client.patch(url, json={"values": {"sentiment": 7}})
    assert bad.status_code == 400 and "CHECK" in bad.json()["detail"]
    assert client.patch(url, json={"values": {"id": 3}}).status_code == 400
    assert client.patch("/api/db/tables/feedback/rows/999", json={"values": {"note": "x"}}).status_code == 404
    assert client.delete(url).json() == {"ok": True}
    assert client.delete(url).status_code == 404
    assert client.delete(f"/api/db/tables/sqlite_master/rows/{fid}").status_code == 404


def test_feedback_can_be_added_and_edited(gui):
    client, _, _, memory = gui
    fid = client.post("/api/feedback", json={"sentiment": "neutral", "scope": "general",
                                             "note": "less jazz in the morning"}).json()["id"]
    assert memory.taste_profile()["notes"][0].startswith("~ less jazz in the morning")
    edited = client.patch(f"/api/feedback/{fid}", json={"sentiment": "dislike"}).json()["feedback"]
    assert edited["sentiment"] == -1 and edited["note"] == "less jazz in the morning"   # untouched fields kept
    assert client.patch(f"/api/feedback/{fid}", json={"note": None}).status_code == 400    # general needs a note
    assert client.post("/api/feedback", json={"sentiment": "like", "scope": "track"}).status_code == 400
    assert client.patch("/api/feedback/999", json={"note": "x"}).status_code == 404


def test_score_adjustments_api(gui):
    client, _, _, memory = gui
    memory.record_feedback(-1, "artist", artist="Coldplay")
    client.post("/api/adjustments", json={"kind": "artist", "subject": "Coldplay", "reset_learning": True})
    adj = client.post("/api/adjustments", json={"kind": "artist", "subject": "Coldplay", "change": 2}).json()["adjustment"]
    t = client.get("/api/taste").json()
    entry = t["affinities"]["artists"][0]
    assert entry["score"] == 2 and entry["parts"] == {"adjusted": 2.0} and entry["adjustment"]["id"] == adj["id"]
    assert t["adjustments"][0]["text"].startswith("artist Coldplay: +2, learning reset on")

    undone = client.patch(f"/api/adjustments/{adj['id']}", json={"ignore_before": None}).json()["adjustment"]
    assert undone["ignore_before"] is None and undone["delta"] == 2                       # only the reset undone
    assert client.get("/api/taste").json()["affinities"]["artists"][0]["score"] == -1    # -3 + 2
    assert client.patch(f"/api/adjustments/{adj['id']}", json={"delta": 99}).status_code == 422
    assert client.post("/api/adjustments", json={"kind": "genre", "subject": "jazz"}).status_code == 422
    assert client.delete(f"/api/adjustments/{adj['id']}").json() == {"ok": True}
    assert client.delete(f"/api/adjustments/{adj['id']}").status_code == 404


def test_notebook_api(gui):
    client, _, _, memory = gui
    nid = client.post("/api/notes", json={"text": "Sundays: mellow", "topic": "habits"}).json()["id"]
    note = memory.notes()[0]
    assert note["author"] == "you" and not note["for_dj"]
    shared = client.patch(f"/api/notes/{nid}", json={"for_dj": True}).json()["note"]
    assert shared["for_dj"] == 1 and shared["text"] == "Sundays: mellow"
    assert memory.taste_profile()["notes"] == ["~ Sundays: mellow (2026-10-01, notebook)"]
    assert client.get("/api/taste").json()["notes"][0]["id"] == nid
    assert client.post("/api/notes", json={"text": ""}).status_code == 422
    assert client.delete(f"/api/notes/{nid}").json() == {"ok": True}
    assert client.patch(f"/api/notes/{nid}", json={"text": "x"}).status_code == 404


def test_tuning_api(gui):
    client, _, _, memory = gui
    memory.record_listen("Pecadora", "Lila Downs", "completed", 230, 230)
    tuned = {t["key"]: t for t in client.put("/api/tuning/completed", json={"value": 2}).json()["tuning"]}
    assert tuned["completed"]["value"] == 2 and tuned["completed"]["changed"]
    assert client.get("/api/taste").json()["profile"]["liked_artists"] == ["Lila Downs"]
    assert client.put("/api/tuning/half_life_days", json={"value": 0}).status_code == 400
    assert client.put("/api/tuning/bogus", json={"value": 1}).status_code == 400
    back = {t["key"]: t for t in client.put("/api/tuning/completed", json={"value": None}).json()["tuning"]}
    assert back["completed"]["value"] == 0.3 and not back["completed"]["changed"]


def test_database_search_add_and_column_info(gui):
    client, _, _, memory = gui
    memory.record_feedback(1, "artist", artist="Nina Simone")
    memory.record_feedback(-1, "artist", artist="Coldplay")
    page = client.get("/api/db/tables/feedback?search=nina").json()
    assert page["total"] == 1 and page["rows"][0][page["columns"].index("artist")] == "Nina Simone"
    info = {c["name"]: c for c in page["info"]}
    assert info["scope"]["choices"] == ["track", "artist", "general"] and info["id"]["type"] == "INTEGER"

    added = client.post("/api/db/tables/notebook/rows", json={"values": {"author": "you", "text": "007 is a title"}})
    row = added.json()["row"]
    assert row["text"] == "007 is a title" and row["ts"] and row["for_dj"] == 0
    bad = client.post("/api/db/tables/feedback/rows", json={"values": {"sentiment": 1, "scope": "track"}})
    assert bad.status_code == 400 and "needs a title" in bad.json()["detail"]
    assert client.patch(f"/api/db/tables/notebook/rows/{row['id']}", json={"values": {"ts": "soon"}}).status_code == 400
    assert client.post("/api/db/tables/nope/rows", json={"values": {}}).status_code == 404


def test_per_speaker_volume(gui):
    client, runtime, house, _ = gui
    runtime.app.player.play(CATALOGUE[:1])
    rooms = {r["room"]: r for r in client.get("/api/volumes").json()["rooms"]}
    assert set(rooms) == {"Dining Room", "Morn\u2019", "Night"} and rooms["Dining Room"]["reference"]
    out = client.post("/api/volume", json={"room": "Night", "level": 8}).json()
    assert out == {"volume": 8, "room": "Night"} and house.by_name("Night").volume == 8
    assert client.post("/api/volume", json={"room": "Night", "change": 2}).json()["volume"] == 10
    client.post("/api/volume", json={"level": 20})                      # group slider keeps the balance
    rooms = {r["room"]: r for r in client.get("/api/volumes").json()["rooms"]}
    assert rooms["Night"]["volume"] == 17 and rooms["Night"]["difference"] == -3
    assert client.post("/api/volume", json={"room": "Attic", "level": 5}).status_code == 400
    assert client.post("/api/volumes/reset").json() == {"volume": 20, "balance": "reset"}
    assert {s.volume for s in house.speakers} == {20}
