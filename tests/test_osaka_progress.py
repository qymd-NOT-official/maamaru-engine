import json
from datetime import datetime

from touken import advisor, youzu_log
from touken.telemetry import TelemetryStore
from test_osaka_rewards import battle, event


def test_osaka_provenance_requires_boss_confirmation():
    route = event("S->C", {"status": 0, "reward": [{"item_type": 4, "item_num": 50}]},
                  endpoint="/sally/eventforward")
    start = event("C->S", {}, endpoint="/sally/eventsally")
    assert "activity" not in youzu_log.build_ledger([start, route])["changes"][0]
    changes = youzu_log.build_ledger([start, route] + battle())["changes"]
    assert [change["activity"] for change in changes] == ["osaka", "osaka"]
    other = event("C->S", {}, endpoint="/sally/start")
    changes = youzu_log.build_ledger([start, route, other] + battle())["changes"]
    assert "activity" not in changes[0]


def test_existing_receipt_metadata_is_backed_up_and_not_duplicated(tmp_path):
    store = TelemetryStore(tmp_path / "events.db")
    ledger = youzu_log.build_ledger(battle())
    original = json.loads(json.dumps(ledger))
    original["changes"][0].pop("activity")
    state = tmp_path / "state.json"
    youzu_log.write_ledger(store, original, state)
    assert youzu_log.write_ledger(store, ledger, state)["sources_repaired"] == 1
    assert youzu_log.write_ledger(store, ledger, state)["sources_repaired"] == 0
    rows = store.recent_events(event_type="resource.change")
    assert len(rows) == 1
    assert rows[0]["payload"]["activity"] == "osaka"
    assert rows[0]["payload"]["delta"] == 1050
    assert len(list((tmp_path / "backups").glob("*.bak"))) == 1
    store.close()


def test_period_income_excludes_other_sources_and_deduplicates_session(tmp_path):
    store = TelemetryStore(tmp_path / "events.db")
    card = {"start_at": "2026-10-15T10:00:00+08:00", "end_at": "2026-10-29T05:00:00+08:00"}
    now = datetime.fromisoformat("2026-10-16T15:00:00+08:00")
    start = datetime.fromisoformat(card["start_at"]).timestamp()
    assert advisor.osaka_period_income(store, card, now=now) is None
    store.start_run("run", "osaka", started_at=start + 1)
    conn = store._conn()

    def add(ts, kind, payload, run=None):
        conn.execute("INSERT INTO events(ts,run_id,script,event_type,payload) VALUES(?,?, 'youzu_log',?,?)",
                     (ts, run, kind, json.dumps(payload)))

    receipt = {"resource": "小判", "delta": 1050, "activity": "osaka", "attribution": "confirmed"}
    add(start - 1, "resource.change", receipt)
    add(start + 2, "resource.change", receipt)
    add(start + 3, "resource.change", {**receipt, "activity": "expedition", "delta": 999})
    add(start + 4, "osaka.koban_session", {"delta": 1050, "summary_only": True, "source": "game_rewards"}, "run")
    add(now.timestamp() + 1, "resource.change", receipt)
    add(datetime.fromisoformat(card["end_at"]).timestamp(), "resource.change", receipt)
    conn.commit()
    assert advisor.osaka_period_income(store, card, now=now) == 1050
    store.start_run("legacy", "osaka", started_at=start + 10)
    add(start + 20, "osaka.koban_session", {"before": 100, "after": 400}, "legacy")
    conn.commit()
    assert advisor.osaka_period_income(store, card, now=now) == 1050
    add(start + 21, "osaka.koban_session", {"delta": 300, "source": "game_rewards"}, "legacy")
    conn.commit()
    assert advisor.osaka_period_income(store, card, now=now) == 1350
    store.close()
