"""Sonos player: rooms, grouping, safety limits, queue modes (fake speakers)."""
from __future__ import annotations

import sqlite3

import pytest

from music_agent.players.base import PlayerError, PolicyError
from tests.fakes import (
    NIGHT,
    make_player,
    track,
)


def test_room_matching_is_loose():
    player, house = make_player()
    assert player.find_room("morn") is house.by_name("Morn’")
    assert player.find_room("dining").player_name == "Dining Room"
    with pytest.raises(PlayerError, match="Unknown room"):
        player.find_room("kitchen")


def test_default_play_groups_all_with_dining_room_volume():
    player, house = make_player()
    result = player.play([track("A"), track("B", n=2)])
    dining = house.by_name("Dining Room")
    assert all(house.coord_of[s] is dining for s in house.speakers)
    assert sorted(result.rooms) == sorted(s.player_name for s in house.speakers)
    assert {s.volume for s in house.speakers} == {13}   # Night went 18 -> 13
    assert dining.state == "PLAYING" and [i.title for i in dining.queue_items] == ["A", "B"]


def test_play_subset_ungroups_others():
    player, house = make_player()
    player.play([track("A")])                 # all grouped under Dining Room
    result = player.play([track("B")], rooms="Night")
    night = house.by_name("Night")
    assert result.rooms == ["Night"] and night.state == "PLAYING"
    assert house.coord_of[night] is night


def test_volume_source_outside_target_uses_first_target():
    player, house = make_player()
    player.play([track("A")], rooms=["Night", "morn"])
    night, morn = house.by_name("Night"), house.by_name("Morn’")
    assert house.coord_of[morn] is night and night.volume == morn.volume == 18


def test_volume_is_capped():
    player, house = make_player(volumes=(("Dining Room", 60), ("Night", 10)))
    result = player.play([track("A")])
    assert result.volume == 35 and {s.volume for s in house.speakers} == {35}
    player.play([track("A")])
    assert player.set_volume(90) == 35 and player.change_volume(+20) == 35
    assert player.set_volume(-5) == 0


def test_quiet_hours_block_start_but_force_overrides():
    player, house = make_player(now=NIGHT)
    with pytest.raises(PolicyError, match="Quiet hours"):
        player.play([track("A")])
    with pytest.raises(PolicyError):
        player.resume()
    assert player.play([track("A")], force=True).queued


def test_quiet_hours_cap_mode():
    player, house = make_player(now=NIGHT, quiet_mode="cap")
    assert player.play([track("A")]).volume == 13    # below quiet cap 15
    assert player.set_volume(30) == 15


def test_append_and_next_keep_current_group():
    player, house = make_player()
    player.play([track("A"), track("B", n=2), track("C", n=3)], rooms="all")
    dining = house.by_name("Dining Room")
    player.play([track("Z", n=9)], mode="append", rooms="Night")   # rooms ignored while playing
    assert house.coord_of[house.by_name("Night")] is dining
    player.play([track("N", n=8)], mode="next")
    assert [i.title for i in dining.queue_items] == ["A", "N", "B", "C", "Z"]


def test_one_bad_track_does_not_sink_request():
    player, house = make_player()
    good, bad = track("Good"), track("Bad", n=2)
    player.provider.bad_refs.add(bad.ref)
    result = player.play([bad, good])
    assert result.queued == [good] and result.failed[0][0] == bad
    dining = house.by_name("Dining Room")
    assert dining.current == 0 and dining.state == "PLAYING"
    player.provider.bad_refs.add(good.ref)
    with pytest.raises(PlayerError, match="None of the items"):
        player.play([bad, good])


def test_now_playing_reports_group():
    player, house = make_player()
    player.play([track("A")])
    now = player.now_playing()
    assert now.state == "PLAYING" and now.volume == 13 and now.queue_position == 1
    assert now.rooms == sorted(s.player_name for s in house.speakers)


# ------------------------------------------------------------------ stale speaker state (seen in bot.log)
def test_stale_group_is_healed_by_rereading_cache():
    player, house = make_player()
    house.by_name("Night").stale_reads = 1            # one stale read, fixed by clearing SoCo's cache
    result = player.play([track("A")])
    assert len(result.rooms) == 3


def test_stale_group_triggers_rediscovery_and_retry(monkeypatch):
    player, house = make_player()
    house.by_name("Night").stale_reads = 2            # still stale after clearing the cache
    discovered = []
    monkeypatch.setattr("soco.discover", lambda timeout: discovered.append(1) or set(house.speakers))
    result = player.play([track("A")])
    assert discovered == [1] and len(result.rooms) == 3
    assert house.by_name("Dining Room").state == "PLAYING"


def test_unreachable_speakers_give_a_clear_error(monkeypatch):
    player, house = make_player()
    for spk in house.speakers:
        spk.stale_reads = 100
    monkeypatch.setattr("soco.discover", lambda timeout: set(house.speakers))
    with pytest.raises(PlayerError, match="Can't reach the speakers"):
        player.now_playing()


def test_room_volume_saves_a_balance_that_group_changes_keep():
    player, house = make_player()                                 # Dining Room 13, Morn' 13, Night 18
    player.play([track("A")])                                     # all levelled to 13
    night, dining = house.by_name("Night"), house.by_name("Dining Room")
    assert player.set_room_volume("night", 8) == 8 and night.volume == 8
    assert player.balance.differences() == {"Night": -5}         # Morn' is level with Dining Room
    vols = {v.room: v for v in player.room_volumes()}
    assert vols["Night"].difference == -5 and vols["Dining Room"].reference and vols["Night"].in_group

    assert player.set_volume(20) == 20                            # the group moves together
    assert (dining.volume, night.volume) == (20, 15)
    assert player.change_volume(-4) == 16 and night.volume == 11
    player.play([track("B")])                                     # a new set keeps the balance too
    assert (dining.volume, night.volume) == (16, 11)
    assert player.now_playing().volume == 16

    assert player.change_room_volume("Night", +2) == 13 and player.balance.difference("Night") == -3
    assert player.reset_balance() == 16 and {s.volume for s in house.speakers} == {16}
    assert player.balance.differences() == {}


def test_balance_is_capped_per_speaker_and_kept_when_not_playing_the_reference():
    player, house = make_player()
    player.play([track("A")])
    player.set_room_volume("Night", 23)                           # Night 10 louder than Dining Room
    assert player.set_volume(30) == 30 and house.by_name("Night").volume == 35   # capped, not 40
    assert player.set_room_volume("Dining Room", 90) == 35        # one room is capped too
    player.set_room_volume("Dining Room", 13)
    player.set_room_volume("Night", 23)
    player.play([track("B")], rooms=["Night"])                    # Dining Room not playing
    assert house.by_name("Night").volume == 23                    # 13 + 10, not re-levelled
    assert player.now_playing().volume == 13                      # the group level stays meaningful
    with pytest.raises(PlayerError, match="Unknown room"):
        player.set_room_volume("Attic", 5)


def test_balance_is_saved_in_the_database():
    from music_agent.players.balance import SpeakerBalance
    from tests.fakes import make_memory
    memory = make_memory()
    SpeakerBalance(memory.db).save({"Night": -5, "Morn’": 0})
    again = SpeakerBalance(memory.db)                             # e.g. after a restart
    assert again.differences() == {"Night": -5} and again.difference("night") == -5
    with pytest.raises(sqlite3.IntegrityError):
        memory.db.update_row("speaker_balance", 1, {"difference": 500})
