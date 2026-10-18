# -*- coding: utf-8 -*-
import pytest

from touken import jp_party
from touken.team_state import TeamSnapshot


def make_payload(members, status=1, team_no=1):
    """合成 /sally 响应：members = [(slot, serial_id, sword_id, hp, hp_max, fatigue)]"""
    slot = {str(i): {'serial_id': None} for i in range(1, 7)}
    sword = {}
    for slot_no, serial, sword_id, hp, hp_max, fatigue in members:
        slot[str(slot_no)] = {'serial_id': serial}
        sword[str(serial)] = {
            'serial_id': serial, 'sword_id': sword_id,
            'hp': hp, 'hp_max': hp_max, 'fatigue': fatigue,
            'equip_serial_id1': 1, 'equip_serial_id2': None,
            'equip_serial_id3': None,
        }
    return {'party': {str(team_no): {'party_no': team_no, 'status': status,
                                     'slot': slot}},
            'sword': sword}


HEALTHY = [(1, 101, 3, 56, 56, 82), (2, 102, 5, 33, 33, 90)]


def test_build_snapshot_healthy():
    snap = jp_party.build_team_snapshot(make_payload(HEALTHY), 1, now=1000.0)
    assert isinstance(snap, TeamSnapshot)
    assert snap.team_no == 1 and len(snap.members) == 2
    assert snap.injury_known and snap.max_injury is None
    first = snap.members[0]
    assert first.slot == 1 and first.serial_id == '101'
    assert first.name == '三日月宗近'
    assert first.fatigue == 82
    assert first.equipment == (True, False, False)


def test_build_snapshot_injury_tiers():
    members = [(1, 101, 3, 40, 56, 50),    # 轻伤
               (2, 102, 5, 20, 56, 50),    # 中伤
               (3, 103, 7, 10, 56, 50)]    # 重伤（<31%）
    snap = jp_party.build_team_snapshot(make_payload(members), 1, now=1000.0)
    injuries = [m.injury for m in snap.members]
    assert injuries == ['轻伤', '中伤', '重伤']
    assert snap.max_injury == '重伤'
    assert all(m.injury_source == 'hp' for m in snap.members)


def test_build_snapshot_missing_sword_data_rejected():
    payload = make_payload(HEALTHY)
    del payload['sword']['102']
    assert jp_party.build_team_snapshot(payload, 1, now=1000.0) is None


def test_build_snapshot_empty_team_rejected():
    assert jp_party.build_team_snapshot(make_payload([]), 1, now=1000.0) is None


def test_build_snapshot_missing_hp_rejected():
    payload = make_payload(HEALTHY)
    payload['sword']['101']['hp'] = None
    assert jp_party.build_team_snapshot(payload, 1, now=1000.0) is None


def test_build_snapshot_wrong_team_rejected():
    assert jp_party.build_team_snapshot(make_payload(HEALTHY), 2,
                                        now=1000.0) is None


def test_party_status():
    assert jp_party.party_status(make_payload(HEALTHY, status=2), 1) == 2
    assert jp_party.party_status(make_payload(HEALTHY), 3) is None
    assert jp_party.party_status({}, 1) is None
