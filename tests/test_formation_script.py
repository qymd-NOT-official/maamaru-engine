# -*- coding: utf-8 -*-
"""编队换人接线：注册形状、目标原样透传、运行占用与结果事件。

接线层（panel/server.py _build_formation）的纪律只有一条：把前端选好的
完整候选池条目原样交给 ensure_team_member_from_honmaru_stream——
匹配/裁决/验收全在执行器，这层不重建目标、不包装结果；请求缺身份时
一行日志如实说「不换人」，绝不碰游戏。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_data = tempfile.TemporaryDirectory(prefix="formation_script_",
                                    ignore_cleanup_errors=True)
with patch.dict("os.environ", {"MAAMARU_DATA_DIR": _data.name}):
    from panel import server
    from panel.script_runner import ScriptRunner, _SCRIPTS, list_scripts


def _params(**over):
    params = {"team_no": 2, "slot_no": 3,
              "target": {"observation_id": "9:41",
                         "sword_catalog_id": "00003",
                         "name_zh": "三日月宗近",
                         "level": 99, "kiwame_date": "2026-01-01"}}
    params.update(over)
    return params


def _agent():
    agent = Mock()
    agent.ensure_team_member_from_honmaru_stream.return_value = iter(
        ["[编队] 干活"])
    agent.navigate_to_stream.return_value = iter([])
    agent.current_location = "本丸"
    return agent


class FormationBuilderTests(unittest.TestCase):
    def _run_builder(self, agent, params):
        with patch.object(server, "_make_agent", return_value=agent):
            return list(_SCRIPTS["formation"]["fn"]("fake.json", params))

    def test_target_passed_through_verbatim(self):
        agent = _agent()
        params = _params()
        logs = self._run_builder(agent, params)
        args, kwargs = agent.ensure_team_member_from_honmaru_stream.call_args
        self.assertEqual(args[0], 2)
        self.assertEqual(args[1], 3)
        self.assertIs(args[2], params["target"])  # 原样交接，不重建对象
        # 证据链收窄由后端明说：选择列表没有形态直读通道，默认
        # (name, form, level) 下档案对象永远缺 form 证据，只会 ambiguous
        self.assertEqual(kwargs, {"match_fields": ("name", "level")})
        self.assertIn("[编队] 干活", logs)

    def test_string_numbers_are_normalized(self):
        agent = _agent()
        self._run_builder(agent, _params(team_no="4", slot_no="6"))
        args, _ = agent.ensure_team_member_from_honmaru_stream.call_args
        self.assertEqual(args[:2], (4, 6))

    def test_missing_target_never_touches_game(self):
        agent = _agent()
        logs = self._run_builder(agent, {"team_no": 1, "slot_no": 1})
        agent.ensure_team_member_from_honmaru_stream.assert_not_called()
        self.assertTrue(any("请求无效" in m for m in logs))

    def test_target_without_identity_never_touches_game(self):
        agent = _agent()
        logs = self._run_builder(agent, _params(target={"level": 12}))
        agent.ensure_team_member_from_honmaru_stream.assert_not_called()
        self.assertTrue(any("请求无效" in m for m in logs))

    def test_bad_numbers_never_touches_game(self):
        agent = _agent()
        logs = self._run_builder(agent, _params(team_no="abc"))
        agent.ensure_team_member_from_honmaru_stream.assert_not_called()
        self.assertTrue(any("请求无效" in m for m in logs))


class FormationRegistrationTests(unittest.TestCase):
    def test_registered_but_hidden_from_task_form(self):
        self.assertIn("formation", _SCRIPTS)
        self.assertNotIn("formation", list_scripts())

    def test_runnable_and_rejects_second_start(self):
        runner = ScriptRunner()
        proc = Mock()
        proc.poll.return_value = None
        with patch.object(runner, "_spawn", return_value=proc) as spawn, \
                patch("panel.script_runner.threading.Thread"), \
                patch("touken.telemetry.get_telemetry_store"):
            run_id = runner.start("formation", "fake.json", _params())
            self.assertIsNotNone(run_id)
            # 参数（含完整目标对象）原样进子进程通道
            self.assertEqual(spawn.call_args[0][2]["target"],
                             _params()["target"])
            # 全局单任务：占用中再起跑一律拒
            self.assertIsNone(
                runner.start("formation", "fake.json", _params()))


class FormationContractTests(unittest.TestCase):
    """契约实证：选择列表没有形态直读通道（行 form 恒 None）。档案条目的
    形态结论是独立的 form_status（kiwame_date 是显现日期，不参与）。
    目标形态已被档案确认（form_status=kiwame）时，默认三字段匹配只会
    一路 ambiguous（页面给不出 form 证据）；接线收窄成 (name, level)
    后能 unique/changed；同名同等级仍安全拒绝。"""

    def _stage(self, pages):
        from tests.test_formation_editor import (
            KOGI, _EditorHost, _FakeMaa, _six, _slot)
        teams = {2: _six()}
        teams[2][2] = _slot(3, catalog=KOGI, name="小狐丸")
        device = _FakeMaa(shell="formation", pages=pages)
        return device, _EditorHost(device, teams)

    def _pool_entry(self, catalog, name, level, kiwame_date=None,
                    form_status="unknown"):
        """真实候选池条目形状（honmaru_profile._pool_entry）。
        kiwame_date 是显现日期（每振都有），form_status 才是形态结论。"""
        return {"observation_id": "9:41", "row_no": 41,
                "sword_catalog_id": catalog,
                "same_team_exclusion_key": catalog,
                "name_zh": name, "level": level, "tou_level": 1,
                "survival": 50, "survival_max": 50,
                "fatigue": 100, "fatigue_max": 100, "stats": {"打击": 55},
                "kiwame_date": kiwame_date, "form_status": form_status,
                "form_evidence": [], "locked": 1, "page_no": 1,
                "unknown_fields": [], "observed_at": 100,
                "source_snapshot_id": 9}

    def _play(self, host, entry, **kw):
        with patch("touken.flows.formation_editor.time.sleep",
                   lambda *_: None):
            return host.ensure_team_member(2, 3, entry, **kw)

    def test_pool_entry_changes_member_with_narrowed_match_fields(self):
        from tests.test_formation_editor import CHANGED, _DECIDE_X
        from tests.test_formation_editor import HASEBE, _row, _slot
        device, host = self._stage([
            [_row("小狐丸", 200, level=99, fatigue=100),
             _row("三日月宗近", 300, level=99, fatigue=100)],
            [_row("压切长谷部", 200, level=35, fatigue=80,
                  becomes=_slot(3, catalog=HASEBE, name="压切长谷部",
                                level=35)),
             _row("前田藤四郎", 300, level=60, fatigue=100)],
        ])
        entry = self._pool_entry(HASEBE, "压切长谷部", 35)
        result = self._play(host, entry, match_fields=("name", "level"))
        self.assertEqual(result["result"], CHANGED, result.get("reason"))
        self.assertIn((1197, 178), device.clicks)  # 决定落在目标行
        ensured = [e for e in host.events
                   if e["event_type"] == "formation.member_selected"]
        self.assertEqual(ensured[-1]["payload"]["result"], CHANGED)

    def test_confirmed_form_is_doomed_with_default_match_fields(self):
        """形态已被档案确认（form_status=kiwame）的条目走默认
        (name, form, level)：页面给不出 form 证据，只能 ambiguous——
        这就是接线必须收窄的原因。"""
        from tests.test_formation_editor import AMBIGUOUS, _DECIDE_X
        from tests.test_formation_editor import HASEBE, _row, _slot
        device, host = self._stage([
            [_row("小狐丸", 200, level=99, fatigue=100),
             _row("三日月宗近", 300, level=99, fatigue=100)],
            [_row("压切长谷部", 200, level=35, fatigue=80),
             _row("前田藤四郎", 300, level=60, fatigue=100)],
        ])
        entry = self._pool_entry(HASEBE, "压切长谷部", 35,
                                 kiwame_date="2024-01-01",  # 显现日期，与形态无关
                                 form_status="kiwame")
        result = self._play(host, entry)
        self.assertEqual(result["result"], AMBIGUOUS)
        self.assertIn("form", result.get("missing_evidence", []))
        self.assertFalse(any(x == _DECIDE_X for x, _y in device.clicks))

    def test_same_name_same_level_still_safely_refused(self):
        """窄证据链不放水：同名同等级拉不开，ambiguous，不点决定。"""
        from tests.test_formation_editor import AMBIGUOUS, _DECIDE_X
        from tests.test_formation_editor import HASEBE, _row, _slot
        device, host = self._stage([
            [_row("压切长谷部", 200, level=35, fatigue=80),
             _row("小狐丸", 300, level=99, fatigue=100)],
            [_row("压切长谷部", 200, level=35, fatigue=60),
             _row("前田藤四郎", 300, level=60, fatigue=100)],
        ])
        entry = self._pool_entry(HASEBE, "压切长谷部", 35)
        result = self._play(host, entry, match_fields=("name", "level"))
        self.assertEqual(result["result"], AMBIGUOUS)
        self.assertFalse(any(x == _DECIDE_X for x, _y in device.clicks))

    def test_same_name_different_level_picks_the_right_one(self):
        from tests.test_formation_editor import CHANGED
        from tests.test_formation_editor import HASEBE, _row, _slot
        device, host = self._stage([
            [_row("压切长谷部", 200, level=60, fatigue=80),
             _row("小狐丸", 300, level=99, fatigue=100)],
            [_row("压切长谷部", 200, level=35, fatigue=60,
                  becomes=_slot(3, catalog=HASEBE, name="压切长谷部",
                                level=35)),
             _row("前田藤四郎", 300, level=60, fatigue=100)],
        ])
        entry = self._pool_entry(HASEBE, "压切长谷部", 35)
        result = self._play(host, entry, match_fields=("name", "level"))
        self.assertEqual(result["result"], CHANGED, result.get("reason"))

    def test_changed_does_not_record_roster_observation(self):
        """决定后不回读，也不把未经独立盘点的队伍写进编队档案。"""
        from tests.test_formation_editor import CHANGED
        from tests.test_formation_editor import HASEBE, _row, _slot
        device, host = self._stage([
            [_row("小狐丸", 200, level=99, fatigue=100),
             _row("三日月宗近", 300, level=99, fatigue=100)],
            [_row("压切长谷部", 200, level=35, fatigue=80,
                  becomes=_slot(3, catalog=HASEBE, name="压切长谷部",
                                level=35)),
             _row("前田藤四郎", 300, level=60, fatigue=100)],
        ])
        entry = self._pool_entry(HASEBE, "压切长谷部", 35)
        result = self._play(host, entry, match_fields=("name", "level"))
        self.assertEqual(result["result"], CHANGED, result.get("reason"))
        roster = [e for e in host.events
                  if e["event_type"] == "team_roster.observed"]
        self.assertEqual(roster, [])
        self.assertEqual(host.events[-1]["event_type"],
                         "formation.member_selected")

    def test_already_correct_does_not_record_roster_observation(self):
        """换前看见目标已在位也不顺手刷新五队档案。"""
        from tests.test_formation_editor import ALREADY_CORRECT, _DECIDE_X
        from tests.test_formation_editor import (
            HASEBE, _EditorHost, _FakeMaa, _six)
        teams = {2: _six(catalog=HASEBE, name="压切长谷部", level=35)}
        device = _FakeMaa(shell="formation", pages=[])
        host = _EditorHost(device, teams)
        entry = self._pool_entry(HASEBE, "压切长谷部", 35)
        host.visible_link = {"status": "linked",
                             "observation_id": entry["observation_id"]}
        result = self._play(host, entry, match_fields=("name", "level"))
        self.assertEqual(result["result"], ALREADY_CORRECT,
                         result.get("reason"))
        roster = [e for e in host.events
                  if e["event_type"] == "team_roster.observed"]
        self.assertEqual(roster, [])
        # 没有换人动作：只有切队确认的标签点击，不碰替换/决定
        self.assertFalse(any(x in (1033, _DECIDE_X) for x, _y in device.clicks))

    def test_refusal_records_no_roster_observation(self):
        """ambiguous/失败不落队伍事实：存疑读数不冒充事实。"""
        from tests.test_formation_editor import AMBIGUOUS
        from tests.test_formation_editor import HASEBE, _row
        # 同名同等级拉不开 → 窄证据链下也 ambiguous，不点决定
        device, host = self._stage([
            [_row("压切长谷部", 200, level=35, fatigue=80),
             _row("小狐丸", 300, level=99, fatigue=100)],
            [_row("压切长谷部", 200, level=35, fatigue=60),
             _row("前田藤四郎", 300, level=60, fatigue=100)],
        ])
        entry = self._pool_entry(HASEBE, "压切长谷部", 35)
        result = self._play(host, entry, match_fields=("name", "level"))
        self.assertEqual(result["result"], AMBIGUOUS)
        self.assertFalse(any(e["event_type"] == "team_roster.observed"
                             for e in host.events))


class FormationResultEventTests(unittest.TestCase):
    def test_result_event_readable_by_run_id(self):
        """前端拿到机器可读结果的唯一通道：/api/data/events 按
        event_type+run_id 查回 result/reason/target。"""
        from fastapi.testclient import TestClient
        from touken.telemetry import TelemetryStore
        # 显式指到临时目录：模块级 env patch 只在 import 期间有效，
        # 这里绝不能碰真实用户数据库（2026-09 实录教训：污染了真库）
        store = TelemetryStore(Path(_data.name) / "telemetry.db")
        self.addCleanup(store.close)
        with patch.dict("os.environ", {"MAAMARU_RUN_ID": "runxyz",
                                       "MAAMARU_SCRIPT": "formation"}):
            store.record_event("formation.member_selected", {
                "team_no": 2, "slot_no": 3, "result": "changed",
                "reason": "已点击决定且选择列表正常关闭",
                "target": {"name": "三日月宗近"}})
        client = TestClient(server.app)
        self.addCleanup(client.close)
        with patch("touken.telemetry.get_telemetry_store",
                   return_value=store):
            body = client.get(
                "/api/data/events",
                params={"event_type": "formation.member_selected"}).json()
        mine = [e for e in body["items"] if e["run_id"] == "runxyz"]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["payload"]["result"], "changed")
        self.assertEqual(mine[0]["payload"]["reason"],
                         "已点击决定且选择列表正常关闭")
        self.assertEqual(mine[0]["payload"]["target"]["name"], "三日月宗近")


if __name__ == "__main__":
    unittest.main()
