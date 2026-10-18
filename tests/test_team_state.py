"""Shared team observations and policies work without a client connection."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from touken.flow_control import BattleProgress
from touken.injury_recovery import RecoveryProgress, current_team_injury
from touken.team_state import TeamMemberSnapshot, TeamSnapshot, injury_reaches_threshold
from touken.team_state_reader import read_team_snapshot
from touken.flows.battle import BattleMixin
from touken.flows.osaka import OsakaMixin
from touken.flows.sakura import SakuraMixin
from test_battle_components import FakeMaa
from test_battle_dialog_safety import HOME, YES, RETURN_CFG, tokens
from test_osaka_maintenance import battle_facts


def snapshot(team_no=3, *, injured=True):
    return TeamSnapshot(team_no, tuple(TeamMemberSnapshot(
        slot=slot, serial_id=str(slot), name="同名刀",
        hp=9 if injured and slot == 1 else 45, hp_max=45,
        injury="重伤" if injured and slot == 1 else None,
        injury_source="battle_status", hp_observed_at=1000,
        fatigue=80 if slot == 1 else 30, fatigue_observed_at=1000,
        equipment=(True, False, False), equipment_observed_at=1000,
    ) for slot in range(1, 7)), 1000)


class Host(BattleMixin, SakuraMixin, OsakaMixin):
    config = {}

    def __init__(self, reader):
        self.team_state_reader = reader
        self.maa = SimpleNamespace()


def test_all_gameplay_consumers_use_the_injected_observation_without_client_reads():
    host = Host(Mock(return_value=snapshot()))
    with patch("touken.game_state.available_events", side_effect=AssertionError("no client read")), \
         patch("touken.game_state.read_events", side_effect=AssertionError("no default connection")), \
         patch("touken.team_state_reader.time.time", return_value=1001):
        injury, detail = host._log_injury_status(3, {})
        assert injury == "重伤" and "hp9/45" in detail
        assert current_team_injury(host, 3) == (True, "重伤")
        assert host._rotate_team_fatigue(3)[2]["serial_id"] == "2"
        assert host._rotate_team_fatigue(3)[1]["fatigue"] == 80
        assert host._osaka_maintenance_facts(3)["equipment"]["1"] == (True, False, False)


def test_custom_reader_needs_no_maa_adapter_and_missing_connection_is_unknown():
    host = SimpleNamespace(team_state_reader=lambda *args, **kwargs: snapshot())
    assert read_team_snapshot(host, 3, now=1001).max_injury == "重伤"
    assert read_team_snapshot(SimpleNamespace(), 3, now=1001) is None
    assert read_team_snapshot(SimpleNamespace(maa=SimpleNamespace(adb_address="fake")), 3, now=1001) is None


@pytest.mark.parametrize("failure", [None, {}, snapshot(2), RuntimeError("data unavailable")])
def test_other_reader_failure_or_wrong_team_never_falls_through_to_the_default_client(failure):
    reader = Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    with patch("touken.game_state.available_events", side_effect=AssertionError("wrong client")), \
         patch("touken.game_state.read_events", side_effect=AssertionError("wrong client")):
        assert read_team_snapshot(Host(reader), 3, now=1001, default_connection=True) is None


def test_default_client_adapter_and_common_views_agree_on_current_battle_facts():
    from touken import game_state
    host = SimpleNamespace(maa=SimpleNamespace())
    events = battle_facts()
    with patch.object(game_state, "available_events", return_value=events) as read:
        state = read_team_snapshot(host, 3, now=2001)
    read.assert_called_once()
    assert state.injury_report() == game_state.injury_report(events, 3, now=2001)
    assert state.maintenance_facts() == game_state.team_maintenance_facts(events, 3, now=2001)
    assert state.fatigue_readings(full_team=True)[2]["fatigue"] == 30


@pytest.mark.parametrize("now", [999, 1601])
def test_stale_or_future_membership_cannot_become_a_current_snapshot(now):
    assert read_team_snapshot(Host(lambda *args, **kwargs: snapshot()), 3, now=now) is None


def test_reading_individually_stale_fields_does_not_refresh_them():
    fresh = snapshot()
    first = replace(fresh.members[0], fatigue_observed_at=1, equipment_observed_at=1)
    state = replace(fresh, members=(first,) + fresh.members[1:])
    current = read_team_snapshot(Host(lambda *args, **kwargs: state), 3, now=1001)
    assert current.injury_known and current.max_injury == "重伤"
    assert current.fatigue_readings() is None
    assert current.maintenance_facts() == {"fatigue": None, "equipment": None}
    assert state.members[0].fatigue == 80  # The observation is immutable.


def test_unknown_hp_is_distinct_from_healthy_without_losing_known_fatigue():
    fresh = snapshot()
    first = replace(fresh.members[0], hp_observed_at=1)
    state = replace(fresh, members=(first,) + fresh.members[1:])
    host = Host(lambda *args, **kwargs: state)
    with patch("touken.team_state_reader.time.time", return_value=1001):
        assert current_team_injury(host, 3) == (False, None)
        assert host._log_injury_status(3, {}) == (None, None)
        assert host._rotate_team_fatigue(3)[1]["fatigue"] == 80
        assert host._osaka_maintenance_facts(3) is None
    healthy = read_team_snapshot(Host(lambda *args, **kwargs: snapshot(injured=False)), 3, now=1001)
    assert healthy.injury_known and healthy.max_injury is None


def test_cache_is_local_to_a_safety_chain_and_expiry_is_rechecked():
    provider = Mock(side_effect=[snapshot(), snapshot(injured=False)])
    host, cache = Host(provider), {}
    assert read_team_snapshot(host, 3, cache=cache, now=1001).max_injury == "重伤"
    assert read_team_snapshot(host, 3, cache=cache, now=1002).max_injury == "重伤"
    assert read_team_snapshot(host, 3, cache=cache, now=1601) is None
    assert provider.call_count == 1
    assert read_team_snapshot(host, 3, now=1002).max_injury is None
    assert provider.call_count == 2


def test_bound_method_reader_uses_one_observation_per_safety_chain():
    class Source:
        def __init__(self):
            self.calls = 0

        def read(self, team_no, **kwargs):
            self.calls += 1
            return snapshot(team_no)
    source, cache = Source(), {}
    host = Host(source.read)
    assert read_team_snapshot(host, 3, cache=cache, now=1001) is not None
    host.team_state_reader = source.read
    assert read_team_snapshot(host, 3, cache=cache, now=1002) is not None
    assert source.calls == 1


def test_same_names_are_allowed_but_duplicate_instance_ids_are_rejected():
    state = snapshot()
    assert len(state.members) == 6
    with pytest.raises(ValueError, match="名单"):
        replace(state, members=(state.members[0], replace(state.members[1], serial_id="1")))


@pytest.mark.parametrize("changes", [{"hp": True}, {"fatigue": False},
                                    {"equipment": (None, False, False)}, {"hp_observed_at": None},
                                    {"slot": True}, {"serial_id": "0"}])
def test_invalid_fields_never_turn_into_healthy_defaults(changes):
    with pytest.raises(ValueError):
        replace(snapshot().members[0], **changes)


def test_unknown_injury_still_reaches_the_most_conservative_threshold():
    assert injury_reaches_threshold(None, "heavy")
    assert injury_reaches_threshold("unrecognized", "heavy")
    assert not injury_reaches_threshold("中伤", "heavy")


@pytest.mark.parametrize("observe", [False, True])
def test_shared_return_home_works_without_inheriting_the_sortie_flow(observe):
    host = BattleMixin()
    host.maa = FakeMaa(templates={"return": HOME, "home": YES},
                       ocr_tokens=[[], tokens("是否返回本丸？"), tokens("是", YES)])
    if observe:
        host._watch_battle_frame = Mock()
    with patch("touken.flows.battle.time.sleep"):
        assert host._return_home_from_march(RETURN_CFG)
    assert host.maa.clicks == [HOME, YES]
    if observe:
        host._watch_battle_frame.assert_called_once()


def test_progress_contract_keeps_existing_recovery_callers_compatible():
    assert RecoveryProgress is BattleProgress
    assert BattleProgress(3, 2).remaining == 1
    with pytest.raises(ValueError):
        BattleProgress(3, 4)
