# -*- coding: utf-8 -*-
"""日服南瓜数据同步：板子状态观测 + 新刀差分（jp_ledger）。

样本是按真实报文结构手工构造的最小合成数据（真实样本永不进仓）。
"""
import json

from touken.jp_ledger import JpLedgerSession, import_transactions
from touken.netlog import Transaction
from touken.telemetry import TelemetryStore


def _tx(path, payload):
    return Transaction(url=path, path=path, method='POST', request_line=None,
                       response_body=json.dumps(payload).encode())


def _events(store, event_type):
    rows = store._conn().execute(
        "SELECT payload FROM events WHERE event_type=? ORDER BY ts",
        (event_type,)).fetchall()
    return [json.loads(raw) for (raw,) in rows]


def _sheet_payload(minute, cleared_mask):
    """9 格板子，cleared_mask 里为 1 的格是翻开的。"""
    return {
        "now": f"2026-11-05 12:{minute:02d}:00",
        "event": {
            "point": {"105": 130},
            "strike": {
                "clear_panel": None,
                "sheet_reward": None,
                "sheet": {
                    "1": {
                        "count": 1, "rest_item": 8, "max_rest_item": 8,
                        "silhouette": 1, "sheet_id": 20010, "reward_id": 1287,
                        "panel": [{"panel_id": 1000 + i, "is_clear": bool(cleared_mask[i])}
                                  for i in range(9)],
                    },
                },
            },
        },
    }


def test_sheet_observation_records_dedups_and_flags_full(tmp_path):
    store = TelemetryStore(tmp_path / 'jp.db')

    # 去重状态挂在会话上（与 inventory/roster 快照同款语义），
    # 生产路径是常驻 listener 的单会话
    session = JpLedgerSession(store)
    session.feed(_tx('/sally', _sheet_payload(0, [0] * 9)))
    assert session.stats["pumpkin.sheet_observed"] == 1
    observed = _events(store, "pumpkin.sheet_observed")
    assert len(observed) == 1
    sheet = observed[0]["sheets"]["1"]
    assert sheet["cleared"] == 0 and sheet["total"] == 9 and not sheet["is_full"]
    assert sheet["rest_item"] == 8 and sheet["reward_id"] == 1287
    assert observed[0]["point"] == {"105": 130}

    # 一模一样的状态不重复落账
    session.feed(_tx('/sally', _sheet_payload(1, [0] * 9)))
    assert session.stats["pumpkin.sheet_observed"] == 1
    assert session.stats["dedup_snapshots"] >= 1
    assert len(_events(store, "pumpkin.sheet_observed")) == 1

    # 翻了一格 → 新观测；9 格全翻 → is_full
    session.feed(_tx('/sally', _sheet_payload(2, [1, 0, 0, 0, 0, 0, 0, 0, 0])))
    session.feed(_tx('/sally', _sheet_payload(3, [1] * 9)))
    observed = _events(store, "pumpkin.sheet_observed")
    assert [o["sheets"]["1"]["cleared"] for o in observed] == [0, 1, 9]
    assert observed[-1]["sheets"]["1"]["is_full"] is True
    store.close()


def _roster_payload(minute, serials):
    return {
        "now": f"2026-11-05 12:{minute:02d}:00",
        "sword": {str(s): {"serial_id": s, "sword_id": 3, "level": 1}
                  for s in serials},
    }


def test_newcomers_first_roster_is_baseline_then_diff_fires(tmp_path):
    store = TelemetryStore(tmp_path / 'jp.db')
    session = JpLedgerSession(store)

    # 第一眼刀帐 = 基线，不补录历史
    session.feed(_tx('/sally', _roster_payload(0, [101, 102])))
    assert session.stats["training.newcomers"] == 0
    assert _events(store, "training.newcomers") == []

    # 出现新 serial → 新刀事件，带名字
    session.feed(_tx('/sally', _roster_payload(1, [101, 102, 103])))
    assert session.stats["training.newcomers"] == 1
    newcomers = _events(store, "training.newcomers")
    assert len(newcomers) == 1
    assert newcomers[0]["count"] == 1
    assert newcomers[0]["swords"][0]["serial_id"] == 103
    assert newcomers[0]["swords"][0]["sword_id"] == 3
    assert newcomers[0]["swords"][0]["name"]

    # 老快照重放 / 刀变少都不误报
    session.feed(_tx('/sally', _roster_payload(2, [101])))
    assert session.stats["training.newcomers"] == 1
    assert len(_events(store, "training.newcomers")) == 1
    store.close()


def test_newcomers_baseline_seeded_from_store_across_sessions(tmp_path):
    store = TelemetryStore(tmp_path / 'jp.db')

    # 会话 1：全量刀帐进账（/party/list = roster_scope full）
    import_transactions(store, [_tx('/party/list', _roster_payload(0, [201, 202]))])

    # 会话 2（新 JpLedgerSession）：基线从账房种子恢复，
    # 老刀不报、会话间歇期掉落的新刀要补上
    session = JpLedgerSession(store)
    session.feed(_tx('/sally', _roster_payload(1, [201, 202])))
    assert session.stats["training.newcomers"] == 0
    session.feed(_tx('/sally', _roster_payload(2, [201, 202, 203])))
    assert session.stats["training.newcomers"] == 1
    newcomers = _events(store, "training.newcomers")
    assert [s["serial_id"] for s in newcomers[0]["swords"]] == [203]
    store.close()


def test_encrypted_battle_still_skipped(tmp_path):
    """战斗结算是密文，板子判定不依赖它（红线：解密器不进仓）。"""
    store = TelemetryStore(tmp_path / 'jp.db')
    stats = import_transactions(store, [_tx('/battle/battle', {
        "now": "2026-11-05 12:00:00", "data": "deadbeef", "iv": "00" * 16})])
    assert stats["skipped_encrypted"] == 1
    assert stats["pumpkin.sheet_observed"] == 0
    store.close()
