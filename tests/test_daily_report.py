# -*- coding: utf-8 -*-
"""日报 touken.daily_report：按天聚合、跨天边界、掉落分组、练度 diff、空态与容错。

事件构造直接写 TelemetryStore（与 tests/test_resource_ledger.py 同路），
时间戳全部用固定 +8 拼出来，锁住 Asia/Shanghai 的跨天边界。
"""

import json
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from touken import advisor, daily_report
from touken.telemetry import TelemetryStore

# Asia/Shanghai 无夏令时，测试里用固定 +8 拼时间戳
SH = timezone(timedelta(hours=8))


def sh(text: str) -> float:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=SH).timestamp()


D = "2026-10-06"  # 报告日


@pytest.fixture(autouse=True)
def report_today(monkeypatch):
    monkeypatch.setattr(daily_report, "_today", lambda: date.fromisoformat(D))


@pytest.fixture
def store():
    # TestClient 会把路由跑进工作线程，那边的 sqlite 连接不在本线程的
    # thread-local 里关不掉；Windows 锁文件，清理临时目录时允许留残渣。
    temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    handle = TelemetryStore(Path(temp.name) / "telemetry.db")
    yield handle
    handle.close()
    temp.cleanup()


def _event(store, ts, event_type, payload, run_id=None, script="youzu_log"):
    cursor = store._conn().execute(
        "INSERT INTO events(ts, run_id, script, event_type, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts, run_id, script, event_type,
         json.dumps(payload, ensure_ascii=False)))
    store._conn().commit()
    return cursor.lastrowid


def _captured(store, ts, resources, script="youzu_log"):
    return _event(store, ts, "inventory.captured",
                  {"captured_at": datetime.fromtimestamp(ts, SH).strftime(
                      "%Y-%m-%d %H:%M:%S"), "source": script,
                   "resources": resources}, script=script)


def _change(store, ts, resource, delta, note, source="expedition.youzu_log.conquest/complete"):
    return _event(store, ts, "resource.change", {
        "resource": resource, "delta": delta,
        "before": None, "after": None, "source": source,
        "note": note, "attribution": "confirmed"})


def _drop(store, ts, name, source="sortie.drop", chapter=8, map_no=2,
          first=False, sword_id=3):
    return _event(store, ts, "sword.obtained", {
        "name": name, "sword_id": sword_id, "source": source,
        "chapter": chapter, "map_no": map_no, "is_first_get_sword": first,
        "acquired_at": "2026-10-06 20:00:00"})


def _training(store, ts, swords):
    return _event(store, ts, "training.captured", {
        "captured_at": datetime.fromtimestamp(ts, SH).strftime(
            "%Y-%m-%d %H:%M:%S"), "source": "youzu_log", "swords": swords})


# ---------------------------------------------------------------- 空日期


def test_empty_day_returns_all_none_and_serializable(store):
    report = daily_report.build_daily_report(store, D)
    assert report["date"] == D
    assert report["resources"] is None
    assert report["drops"] is None
    assert report["training"] is None
    assert report["attendance"] is None
    assert report["degraded"] == []
    json.dumps(report, ensure_ascii=False)  # 保证可序列化


def test_default_date_is_today(store):
    report = daily_report.build_daily_report(store)
    assert report["date"] == datetime.now(SH).date().isoformat()


# ---------------------------------------------------------------- 今日收支


def test_resources_aggregate_net_and_first_last_balance(store):
    _captured(store, sh(f"{D} 08:00:00"),
              {"木炭": 1000, "玉钢": 2000, "小判": 50})
    _change(store, sh(f"{D} 10:00:00"), "木炭", 500,
            "远征完成·三队·B2 木炭 +500")
    _change(store, sh(f"{D} 11:00:00"), "木炭", -200, "锻刀 木炭 -200",
            source="forge")
    _change(store, sh(f"{D} 12:00:00"), "小判", 300, "任务奖励 小判 +300",
            source="task_rewards.reward_popup")
    _captured(store, sh(f"{D} 23:00:00"),
              {"木炭": 1300, "玉钢": 2000, "小判": 350})
    section = daily_report.build_daily_report(store, D)["resources"]
    assert section["net"] == {"木炭": 300, "小判": 300}
    assert section["opening"]["resources"]["木炭"] == 1000
    assert section["closing"]["resources"]["木炭"] == 1300
    assert section["opening"]["captured_at"].startswith(D)
    notes = [entry["note"] for entry in section["entries"]]
    assert "远征完成·三队·B2 木炭 +500" in notes


def test_resources_none_when_only_outside_events(store):
    _change(store, sh("2026-10-05 23:00:00"), "木炭", 500, "昨天 木炭 +500")
    assert daily_report.build_daily_report(store, D)["resources"] is None


def test_resources_entry_cap_keeps_largest(store):
    for index in range(25):
        _change(store, sh(f"{D} 10:{index:02d}:00"), "木炭", index + 1,
                f"第{index}笔 木炭 +{index + 1}")
    section = daily_report.build_daily_report(store, D)["resources"]
    assert section["entry_total"] == 25
    assert len(section["entries"]) == 20
    assert section["truncated"] == 5
    assert section["entries"][0]["delta"] == 25  # 按金额绝对值取大头


def test_entry_label_strips_amount_suffix_from_note(store):
    _change(store, sh(f"{D} 10:00:00"), "木炭", 500,
            "远征完成·三队·B2 木炭 +500")
    _change(store, sh(f"{D} 11:00:00"), "加速符·极", -1,
            "手入加速 加速符·极 -1")
    entries = daily_report.build_daily_report(store, D)["resources"]["entries"]
    labels = {entry["resource"]: entry["label"] for entry in entries}
    assert labels["木炭"] == "远征完成·三队·B2"
    assert labels["加速符"] == "手入加速"  # 尾巴按原文「加速符·极」剥


def test_entry_label_falls_back_to_source_when_note_empty(store):
    _change(store, sh(f"{D} 10:00:00"), "木炭", 1200, "",
            source="task_rewards.reward_popup")
    _change(store, sh(f"{D} 11:00:00"), "玉钢", -700, "",
            source="forge.started")
    entries = daily_report.build_daily_report(store, D)["resources"]["entries"]
    labels = {entry["resource"]: entry["label"] for entry in entries}
    assert labels["木炭"] == "任务奖励"
    assert labels["玉钢"] == "锻刀"


def test_entry_label_blank_for_unmapped_source(store):
    _change(store, sh(f"{D} 10:00:00"), "木炭", 100, "",
            source="youzu_log.unknown")
    entry, = daily_report.build_daily_report(store, D)["resources"]["entries"]
    assert entry["label"] == ""  # 前端据此显示「来源未确认」


def test_cross_day_boundary_shanghai_timezone(store):
    # 23:59:59 属于 D；00:00:00 属于 D+1——差一秒都不能串
    _change(store, sh(f"{D} 23:59:59"), "木炭", 111, "深夜 木炭 +111")
    _change(store, sh("2026-10-07 00:00:00"), "木炭", 222, "凌晨 木炭 +222")
    report = daily_report.build_daily_report(store, D)
    assert report["resources"]["net"] == {"木炭": 111}
    next_day = daily_report.build_daily_report(store, "2026-10-07")
    assert next_day["resources"]["net"] == {"木炭": 222}


# ---------------------------------------------------------------- 今日掉落


def test_drops_group_by_map_and_count_first_get(store):
    _drop(store, sh(f"{D} 09:00:00"), "三日月宗近", chapter=8, map_no=2)
    _drop(store, sh(f"{D} 09:30:00"), "小狐丸", chapter=8, map_no=2,
          first=True)
    _drop(store, sh(f"{D} 10:00:00"), "今剣", chapter=1, map_no=1)
    _drop(store, sh(f"{D} 11:00:00"), "岩融", source="raid.drop",
          chapter=None, map_no=None, sword_id=9)
    _event(store, sh(f"{D} 12:00:00"), "forge.collected", {
        "source": "forge", "slot": 1, "count": 2, "swords": [
            {"name": "压切长谷部", "sword_id": 118, "serial_id": 501,
             "is_first_get_sword": False},
            {"name": "数珠丸恒次", "sword_id": 11, "serial_id": 502,
             "is_first_get_sword": True}]})
    section = daily_report.build_daily_report(store, D)["drops"]
    assert section["total"] == 6
    assert section["first_get_total"] == 2
    groups = {group["label"]: group for group in section["groups"]}
    assert groups["8-2"]["count"] == 2
    assert groups["8-2"]["first_get_count"] == 1
    assert groups["1-1"]["count"] == 1
    assert groups["联队战"]["count"] == 1
    assert groups["锻刀"]["count"] == 2
    names = [row["name"] for row in groups["锻刀"]["swords"]]
    assert names == ["压切长谷部", "数珠丸恒次"]


def test_drops_none_when_empty(store):
    assert daily_report.build_daily_report(store, D)["drops"] is None


# ---------------------------------------------------------------- 今日练度


def _sword(serial, sword_id, level, exp, ranbu_level=1, ranbu_exp=0):
    return {"serial_id": serial, "sword_id": sword_id, "level": level,
            "exp": exp, "ranbu_level": ranbu_level, "ranbu_exp": ranbu_exp,
            "hp_up": 0, "atk_up": 0, "def_up": 0, "mobile_up": 0,
            "back_up": 0, "scout_up": 0, "hide_up": 0}


def test_training_diff_level_ranbu_and_exp_top(store):
    _training(store, sh("2026-10-05 22:00:00"), [
        _sword(111, 3, 95, 100000, ranbu_level=2),   # 三日月宗近
        _sword(222, 118, 35, 5000),                  # 压切长谷部
    ])
    _training(store, sh(f"{D} 22:00:00"), [
        _sword(111, 3, 96, 104500, ranbu_level=3),   # 升 1 级 + 乱舞 +4500 exp
        _sword(222, 118, 35, 5200),                  # 只涨经验
    ])
    section = daily_report.build_daily_report(store, D)["training"]
    assert section["has_previous"] is True
    assert section["sword_count"] == 2
    assert section["snapshot_captured_at"].startswith(D)
    assert section["previous_captured_at"].startswith("2026-10-05")
    assert section["level_ups"] == [
        {"name": "三日月宗近", "serial_id": 111, "from": 95, "to": 96}]
    assert section["ranbu_ups"] == [
        {"name": "三日月宗近", "serial_id": 111, "from": 2, "to": 3}]
    assert section["exp_top"][0]["name"] == "三日月宗近"
    assert section["exp_top"][0]["exp_gain"] == 4500
    assert section["exp_top"][0]["exp"] == 104500
    assert len(section["exp_top"]) <= 5


def test_training_first_snapshot_has_no_previous(store):
    _training(store, sh(f"{D} 22:00:00"), [_sword(111, 3, 95, 100000)])
    section = daily_report.build_daily_report(store, D)["training"]
    assert section["has_previous"] is False
    assert section["previous_ts"] is None
    assert section["level_ups"] == []
    assert section["ranbu_ups"] == []
    assert section["exp_top"] == []


def test_training_none_without_snapshot_that_day(store):
    _training(store, sh("2026-10-05 22:00:00"), [_sword(111, 3, 95, 100000)])
    assert daily_report.build_daily_report(store, D)["training"] is None


# ---------------------------------------------------------------- 目标进度


def test_goals_empty_when_no_goals_file(store, tmp_path, monkeypatch):
    monkeypatch.setattr(daily_report, "STATUS_DIR", tmp_path)
    report = daily_report.build_daily_report(store, D)
    assert report["goals"] == []
    assert "goals" not in report["degraded"]


def test_goals_evaluated_for_report_date(store, tmp_path, monkeypatch):
    monkeypatch.setattr(daily_report, "STATUS_DIR", tmp_path)
    advisor.add_goal(tmp_path / advisor.GOALS_FILENAME,
                     resource="小判", target=100000, goal_mode="amount_target")
    report = daily_report.build_daily_report(store, D)
    assert len(report["goals"]) == 1
    goal = report["goals"][0]
    assert goal["resource"] == "小判"
    assert goal["target"] == 100000
    assert goal["status"] in {"unknown", "active"}
    assert goal["message"]


def test_goals_failure_degrades_section_only(store, tmp_path, monkeypatch):
    monkeypatch.setattr(daily_report, "STATUS_DIR", tmp_path)
    monkeypatch.setattr(advisor, "get_planning",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    _change(store, sh(f"{D} 10:00:00"), "木炭", 500, "远征完成·木炭 +500")
    report = daily_report.build_daily_report(store, D)
    assert report["goals"] is None
    assert report["degraded"] == ["goals"]
    assert report["resources"]["net"] == {"木炭": 500}  # 别的小节照常


# ---------------------------------------------------------------- 今日出勤


def test_attendance_lists_runs_that_day(store):
    store.start_run("run-1", "osaka", started_at=sh(f"{D} 09:00:00"),
                    label="挖地收菜")
    store.finish_run("run-1", "completed", ended_at=sh(f"{D} 11:00:00"))
    store.start_run("run-2", "daily", started_at=sh(f"{D} 12:00:00"))
    store.finish_run("run-2", "failed", ended_at=sh(f"{D} 12:30:00"))
    store.start_run("run-3", "osaka", started_at=sh("2026-10-05 23:00:00"))
    store.finish_run("run-3", "completed", ended_at=sh(f"{D} 01:00:00"))
    section = daily_report.build_daily_report(store, D)["attendance"]
    by_id = {row["run_id"]: row for row in section}
    assert by_id["run-1"]["label"] == "挖地收菜"
    assert by_id["run-1"]["status"] == "completed"
    assert by_id["run-2"]["status"] == "failed"
    assert by_id["run-3"]["script"] == "osaka"  # 跨天轮次也算今天的出勤


def test_attendance_none_when_no_runs(store):
    assert daily_report.build_daily_report(store, D)["attendance"] is None


def test_old_unfinished_runs_are_not_today_attendance(store):
    store.start_run("old", "osaka", started_at=sh("2026-08-14 12:00:00"))
    assert daily_report.build_daily_report(store, D)["attendance"] is None
    assert store.runs_between(sh(f"{D} 00:00:00"), sh("2026-10-07 00:00:00"))[0]["status"] == "running"


def test_group_totals_include_entries_beyond_preview_limit(store):
    for i in range(30):
        _change(store, sh(f"{D} 12:00:00") + i, "木炭", 100, "", source="task_rewards.reward_popup")
    section = daily_report.build_daily_report(store, D)["resources"]
    assert len(section["entries"]) == 20
    assert section["groups"][0]["count"] == 30
    assert section["groups"][0]["net"] == {"木炭": 3000}


def test_historical_report_does_not_evaluate_current_goals(store, monkeypatch):
    spy = lambda *args, **kwargs: pytest.fail("不能用当前家底计算历史目标")
    monkeypatch.setattr(advisor, "get_planning", spy)
    assert daily_report.build_daily_report(store, "2026-10-05")["goals"] is None


def test_past_deadline_goal_is_hidden_even_if_marked_done(store, monkeypatch):
    monkeypatch.setattr(advisor, "get_planning", lambda *args, **kwargs: {"goals": [
        {"deadline": "2026-09-10", "status": "done", "message": "旧目标"},
        {"deadline": D, "status": "active", "message": "今天的目标"}]})
    goals = daily_report.build_daily_report(store, D)["goals"]
    assert [goal["message"] for goal in goals] == ["今天的目标"]


# ---------------------------------------------------------------- API 路由


def test_api_route_returns_report_and_validates_date(store, monkeypatch):
    from fastapi.testclient import TestClient
    from panel import server
    from touken import telemetry

    monkeypatch.setattr(telemetry, "get_telemetry_store", lambda: store)
    _change(store, sh(f"{D} 10:00:00"), "木炭", 500, "远征完成·木炭 +500")
    client = TestClient(server.app)
    ok = client.get(f"/api/daily_report?date={D}")
    assert ok.status_code == 200
    assert ok.json()["resources"]["net"] == {"木炭": 500}
    assert ok.json()["date"] == D
    missing = client.get("/api/daily_report")
    assert missing.status_code == 200
    assert missing.json()["date"]  # 缺省=今天
    bad = client.get("/api/daily_report?date=2026-10-06 10:00")
    assert bad.status_code == 400
    assert "YYYY-MM-DD" in bad.json()["error"]
