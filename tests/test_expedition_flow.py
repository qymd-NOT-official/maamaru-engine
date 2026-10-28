# -*- coding: utf-8 -*-
"""远征派遣流程单化（双服阶段 2）：官方流程形状 + 薄壳行为对等测试。

expedition_stream 现在只剩配置门/补花前置/填参，选图和安全派遣在
ExpeditionMixin._expedition_pick_map_stream / _expedition_safe_dispatch_stream，
由流程引擎按共同契约经 builtin 调用（pack=ctx["pack"]，cn 素材包解析
@expedition/*）。本文件守：

- 官方流程 expedition.json 的形状（navigate → pick_map → safe_dispatch，
  占位参数 era=1/map_slot=1/map_name=null/team_no=1，不设 on_fail）；
- 端到端分支：正常（卡位/名字）、错图拦停、重伤拦停、开始按钮缺、
  确认弹窗缺、游戏结果 False / None（含「没看到远征中」白名单提醒不收红）；
- pick_map 翻车绝不走到 safe_dispatch（不可自动收拾）。
"""

import copy
import json
import unittest
from unittest.mock import patch

from touken.device_adapter import Point
from touken.flow_engine import load_official_flows
from touken.flows.expedition import (
    _MAPS_TABLE,
    EXPEDITION_FLOW_ID,
    ExpeditionMixin,
)
from touken.flows.report_judge import _is_fail
from touken.runtime_paths import RESOURCE_DIR
from touken.server_pack import load_server_pack


def _cn_map_meta(era, slot):
    maps = json.loads(_MAPS_TABLE.read_text(encoding="utf-8"))["maps"]
    for code, row in maps.items():
        if row.get("era") == era and row.get("slot") == slot:
            return code, row.get("name")
    return None, None


MAP_CODE_1_1, MAP_NAME_1_1 = _cn_map_meta(1, 1)


class ExpeditionDevice:
    """按剧本回应识别的假 DeviceAdapter，同时记录调用。

    识别值走真实 cn 素材包（@expedition/* 解析回来），剧本只控制
    「这帧画面上有没有这东西」。
    """

    def __init__(self, map_visible=True, map_name_hit=True, team_ui=True,
                 start=True, confirm=True, running=True, stamp_hit=False):
        self.map_visible = map_visible      # 全屏 OCR 对图名（选图确认）
        self.map_name_hit = map_name_hit    # 小图名字 OCR（按名字点图）
        self.team_ui = team_ui
        self.start = start
        self.confirm = confirm
        self.running = running
        self.stamp_hit = stamp_hit
        self.ocr_calls = []                 # (expected, match_mode)
        self.clicks = []
        self.tpl_calls = []

    def screenshot(self, force=False):
        return None

    def click(self, pt):
        self.clicks.append((pt.x, pt.y))
        return True

    def ocr(self, expected, roi=None, match_mode="contains"):
        self.ocr_calls.append((expected, match_mode))
        if expected == "部队选择" and self.team_ui:
            return Point(1, 1)
        if expected == "远征开始" and self.start and match_mode == "exact":
            return Point(1, 1)
        if expected == "确认" and self.confirm:
            return Point(1, 1)
        if expected == "远征中" and self.running:
            return Point(1, 1)
        if expected == "鸟羽" and self.map_name_hit:
            return Point(1, 1)
        return None

    def ocr_all(self, roi, image=None):
        if self.map_visible:
            return [("鸟羽伏见之战", Point(300, 200))]
        return [("全然无关的字", Point(300, 200))]

    def template_match(self, template, roi=None, threshold=0.7):
        self.tpl_calls.append(template)
        if "重伤" in template and self.stamp_hit:
            return Point(1, 1)
        return None


class ExpeditionFlowAgent(ExpeditionMixin):
    """最小宿主：导航直达（记录调用），点坐标只记录。"""

    def __init__(self, device, config):
        self.device = device
        self.config = config
        self.current_location = "远征"
        self.nav_calls = []
        self.point_clicks = []

    def navigate_to_stream(self, dest):
        self.nav_calls.append(dest)
        return iter(())

    def _click_point(self, pt):
        self.point_clicks.append(tuple(pt))

    def _find_deploy_button(self, cfg):
        return None


CONFIG = {
    "expedition": {
        "eras": {"1": [100, 100]},
        "map_slots": {"1": [100, 200]},
        "map_ocr_roi": [0, 100, 1280, 400],
        "deploy_button": {"ocr": {"expected": ["部队", "选择"],
                                  "roi": [1110, 580, 1279, 710]}},
        "team_ui_ocr": {"expected": "部队选择", "roi": [506, 1, 774, 77]},
        "injury_stamp": {"template": "battle/ui重伤.png"},
        "start_button": {"template": "远征开始.png"},
        "start_ocr": {"expected": "远征开始", "roi": [1100, 560, 1280, 700]},
        "confirm_ocr": {"expected": "确认", "roi": [650, 520, 950, 640]},
        "running_ocr": {"expected": "远征中", "roi": [60, 280, 1230, 580]},
    },
    "team_select": {"teams": {"2": [274, 91]}},
}


def _run(device, **kwargs):
    agent = ExpeditionFlowAgent(device, copy.deepcopy(CONFIG))
    params = {"era": 1, "team_no": 2}
    params.update(kwargs)
    with patch("touken.flows.expedition.time.sleep"), \
            patch.object(ExpeditionMixin, "_dispatch_injury_report",
                         return_value=(None, None)), \
            patch.object(agent, "_dispatch_game_result",
                         return_value={"payload": {"status": 0}}), \
            patch.object(agent, "_record_dispatch") as save:
        logs = list(agent.expedition_stream(**params))
    return agent, logs, save


class OfficialFlowShapeTests(unittest.TestCase):
    """官方流程单本身的形状：三步骤、占位参数、不设 on_fail、全逻辑名。"""

    def _flow(self):
        flows = {f["id"]: f for f in load_official_flows()}
        self.assertIn(EXPEDITION_FLOW_ID, flows)
        return flows[EXPEDITION_FLOW_ID]

    def test_shape_navigate_pick_dispatch(self):
        flow = self._flow()
        self.assertIs(flow["official"], True)
        self.assertEqual(flow["name"], "远征派遣")
        steps = flow["steps"]
        self.assertEqual([s["type"] for s in steps],
                         ["navigate", "builtin", "builtin"])
        self.assertEqual(steps[0]["params"]["target"], "@expedition/nav")
        pick, dispatch = steps[1], steps[2]
        self.assertEqual(pick["params"]["name"], "expedition_pick_map")
        self.assertEqual(dispatch["params"]["name"], "expedition_safe_dispatch")
        for step in (pick, dispatch):
            self.assertEqual(step["params"]["era"], 1)
            self.assertEqual(step["params"]["map_slot"], 1)
            self.assertIsNone(step["params"]["map_name"])
            self.assertEqual(step["params"]["team_no"], 1)

    def test_no_explicit_on_fail(self):
        # 翻车收拾上交引擎；选图/派遣翻车由不可恢复积木兜底（引擎层测试）
        flow = self._flow()
        for step in flow["steps"]:
            self.assertEqual(step["on_fail"], "recover", step["id"])

    def test_official_file_on_disk_keeps_placeholders(self):
        raw = json.loads((RESOURCE_DIR / "flows" / "expedition.json")
                         .read_text(encoding="utf-8"))
        self.assertEqual(raw["id"], EXPEDITION_FLOW_ID)
        builtin_params = [s["params"] for s in raw["steps"]
                          if s["type"] == "builtin"]
        self.assertEqual(len(builtin_params), 2)
        for params in builtin_params:
            self.assertEqual(params["era"], 1)
            self.assertEqual(params["map_slot"], 1)
            self.assertIsNone(params["map_name"])
            self.assertEqual(params["team_no"], 1)

    def test_pack_entries_resolve_and_mirror_example_config(self):
        # cn 包 @expedition/* 与 touken_config.example.json 的值逐一相同
        pack = load_server_pack("cn")
        self.assertEqual(pack.resolve_navigation("@expedition/nav"), "远征")
        self.assertEqual(pack.resolve_ocr("@expedition/team-ui"), "部队选择")
        self.assertEqual(pack.resolve_ocr("@expedition/start"), "远征开始")
        self.assertEqual(pack.resolve_ocr("@expedition/confirm"), "确认")
        self.assertEqual(pack.resolve_ocr("@expedition/running"), "远征中")
        self.assertEqual(pack.resolve_template("@expedition/start"), "远征开始.png")
        self.assertEqual(pack.resolve_template("@expedition/injury-stamp"),
                         "battle/ui重伤.png")
        self.assertEqual(pack.resolve_roi("@expedition/team-ui"), [506, 1, 774, 77])
        self.assertEqual(pack.resolve_roi("@expedition/deploy"),
                         [1110, 580, 1279, 710])
        self.assertEqual(pack.resolve_roi("@expedition/start"),
                         [1100, 560, 1280, 700])
        self.assertEqual(pack.resolve_roi("@expedition/confirm"),
                         [650, 520, 950, 640])
        self.assertEqual(pack.resolve_roi("@expedition/running"),
                         [60, 280, 1230, 580])
        self.assertEqual(pack.resolve_roi("@expedition/map-ocr"),
                         [60, 280, 1230, 580])


class FlowDispatchTests(unittest.TestCase):
    """薄壳 + 官方流程 + 真 cn 素材包的端到端分支。"""

    def test_dispatch_by_map_slot_is_green_and_records(self):
        device = ExpeditionDevice()
        agent, logs, save = _run(device, map_slot=1)
        self.assertTrue(any("已出发" in m for m in logs), logs)
        self.assertTrue(any("全部跑完，全绿" in m for m in logs), logs)
        save.assert_called_once()
        team, era, slot, name = save.call_args.args
        self.assertEqual((team, era, slot), (2, 1, 1))
        self.assertEqual(name, MAP_NAME_1_1)  # 卡位派图回填收益表图名
        # 时代卡 + 小图卡位 + 两次部队点按配置坐标盲点
        self.assertIn((100, 100), agent.point_clicks)
        self.assertIn((100, 200), agent.point_clicks)
        self.assertIn((274, 91), agent.point_clicks)
        # 识别走的是素材包路径（模板存在，真实文件名）
        self.assertIn("远征开始.png", device.tpl_calls)
        self.assertIn("battle/ui重伤.png", device.tpl_calls)

    def test_dispatch_by_map_name_ocr_clicks(self):
        device = ExpeditionDevice()
        agent, logs, save = _run(device, map_name="鸟羽")
        self.assertTrue(any("已出发" in m for m in logs), logs)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[3], "鸟羽")
        # 按名字点图 = OCR 点认到的位置，不是卡位盲点
        self.assertNotIn((100, 200), agent.point_clicks)
        self.assertIn(("鸟羽", "contains"), device.ocr_calls)

    def test_wrong_map_blocks_before_any_dispatch(self):
        device = ExpeditionDevice(map_visible=False)
        agent, logs, save = _run(device, map_name="鸟羽")
        refusal = [m for m in logs if "怕派错队伍" in m]
        self.assertTrue(refusal)
        self.assertTrue(_is_fail(refusal[-1]))
        self.assertTrue(any("不可自动收拾" in m for m in logs), logs)
        self.assertTrue(any("有翻车项" in m for m in logs), logs)
        # 选图翻车绝不继续派遣：安全派遣一步没轮到跑
        self.assertNotIn(("部队选择", "contains"), device.ocr_calls)
        self.assertNotIn(("远征开始", "exact"), device.ocr_calls)
        save.assert_not_called()

    def test_map_name_not_found_stops(self):
        device = ExpeditionDevice(map_name_hit=False)
        _agent, logs, save = _run(device, map_name="鸟羽")
        self.assertTrue(any("没找到小图「鸟羽」" in m for m in logs), logs)
        self.assertTrue(any("不可自动收拾" in m for m in logs), logs)
        save.assert_not_called()

    def test_json_heavy_injury_blocks_dispatch(self):
        device = ExpeditionDevice()
        agent = ExpeditionFlowAgent(device, copy.deepcopy(CONFIG))
        with patch("touken.flows.expedition.time.sleep"), \
                patch.object(ExpeditionMixin, "_dispatch_injury_report",
                             return_value=("重伤", "三日月宗近重伤(hp5/45)")), \
                patch.object(agent, "_record_dispatch") as save:
            logs = list(agent.expedition_stream(era=1, map_name="鸟羽",
                                                team_no=2))
        self.assertTrue(any("游戏数据验出重伤" in m for m in logs), logs)
        self.assertTrue(any("不可自动收拾" in m for m in logs), logs)
        self.assertNotIn(("远征开始", "exact"), device.ocr_calls)
        self.assertFalse(any("已出发" in m for m in logs))
        save.assert_not_called()

    def test_stamp_blocks_when_json_silent(self):
        device = ExpeditionDevice(stamp_hit=True)
        agent = ExpeditionFlowAgent(device, copy.deepcopy(CONFIG))
        with patch("touken.flows.expedition.time.sleep"), \
                patch.object(ExpeditionMixin, "_dispatch_injury_report",
                             return_value=(None, None)), \
                patch.object(agent, "_record_dispatch") as save:
            logs = list(agent.expedition_stream(era=1, map_name="鸟羽",
                                                team_no=2))
        self.assertTrue(any("检测到重伤标记" in m for m in logs), logs)
        self.assertFalse(any("已出发" in m for m in logs))
        save.assert_not_called()

    def test_team_select_ui_missing_stops(self):
        device = ExpeditionDevice(team_ui=False)
        _agent, logs, save = _run(device, map_slot=1)
        self.assertTrue(any("部队选择界面没打开" in m for m in logs), logs)
        self.assertTrue(any("有翻车项" in m for m in logs), logs)
        save.assert_not_called()

    def test_start_button_missing_stops(self):
        device = ExpeditionDevice(start=False)
        _agent, logs, save = _run(device, map_slot=1)
        self.assertTrue(any("找不到可用的远征开始按钮" in m for m in logs), logs)
        self.assertTrue(any("有翻车项" in m for m in logs), logs)
        save.assert_not_called()

    def test_confirm_dialog_missing_stops(self):
        device = ExpeditionDevice(confirm=False)
        _agent, logs, save = _run(device, map_slot=1)
        self.assertTrue(any("确认弹窗没出现" in m for m in logs), logs)
        self.assertTrue(any("有翻车项" in m for m in logs), logs)
        save.assert_not_called()

    def test_false_game_result_cannot_be_made_green(self):
        device = ExpeditionDevice()
        agent = ExpeditionFlowAgent(device, copy.deepcopy(CONFIG))
        with patch("touken.flows.expedition.time.sleep"), \
                patch.object(ExpeditionMixin, "_dispatch_injury_report",
                             return_value=(None, None)), \
                patch.object(agent, "_dispatch_game_result",
                             return_value=False), \
                patch.object(agent, "_record_dispatch") as save:
            logs = list(agent.expedition_stream(era=1, map_slot=1, team_no=2))
        self.assertTrue(any("未确认本次" in m for m in logs), logs)
        self.assertFalse(any("已出发" in m for m in logs))
        self.assertTrue(any("有翻车项" in m for m in logs), logs)
        save.assert_not_called()

    def test_missing_game_result_falls_back_to_running_text(self):
        device = ExpeditionDevice(running=True)
        agent = ExpeditionFlowAgent(device, copy.deepcopy(CONFIG))
        with patch("touken.flows.expedition.time.sleep"), \
                patch.object(ExpeditionMixin, "_dispatch_injury_report",
                             return_value=(None, None)), \
                patch.object(agent, "_dispatch_game_result",
                             return_value=None), \
                patch.object(agent, "_record_dispatch") as save:
            logs = list(agent.expedition_stream(era=1, map_slot=1, team_no=2))
        self.assertTrue(any("已出发" in m for m in logs), logs)
        save.assert_called_once()

    def test_unverified_dispatch_stays_warning_not_failure(self):
        # 旧语义保持：确认点过但游戏数据缺、画面也没认到「远征中」——
        # 这条 ⚠️ 在翻车词表白名单里（可能派遣失败也可能已回本丸），不收红不记账
        device = ExpeditionDevice(running=False)
        agent = ExpeditionFlowAgent(device, copy.deepcopy(CONFIG))
        with patch("touken.flows.expedition.time.sleep"), \
                patch.object(ExpeditionMixin, "_dispatch_injury_report",
                             return_value=(None, None)), \
                patch.object(agent, "_dispatch_game_result",
                             return_value=None), \
                patch.object(agent, "_record_dispatch") as save:
            logs = list(agent.expedition_stream(era=1, map_slot=1, team_no=2))
        warn = next(m for m in logs if "没看到「远征中」字样" in m)
        self.assertFalse(_is_fail(warn))
        self.assertTrue(any("全部跑完，全绿" in m for m in logs), logs)
        save.assert_not_called()


class ShellGateTests(unittest.TestCase):
    """配置门保持旧语义：不进流程、不点坐标。"""

    def _logs(self, config, **kwargs):
        agent = ExpeditionFlowAgent(ExpeditionDevice(), config)
        params = {"era": 1, "team_no": 2}
        params.update(kwargs)
        with patch("touken.flows.expedition.time.sleep"):
            return agent, list(agent.expedition_stream(**params))

    def test_missing_expedition_config(self):
        agent, logs = self._logs({"expedition": {}}, map_slot=1)
        self.assertIn("[远征] 未配置远征", logs)
        self.assertEqual(agent.point_clicks, [])

    def test_missing_era_coords(self):
        config = copy.deepcopy(CONFIG)
        config["expedition"]["eras"] = {}
        agent, logs = self._logs(config, map_slot=1)
        self.assertIn("[远征] 配置里没有时代1的坐标", logs)
        self.assertEqual(agent.point_clicks, [])

    def test_missing_team_coords(self):
        config = copy.deepcopy(CONFIG)
        config["team_select"] = {"teams": {}}
        agent, logs = self._logs(config, map_slot=1)
        self.assertIn("[远征] 配置里没有部队2的坐标", logs)
        self.assertEqual(agent.point_clicks, [])

    def test_missing_slot_coords(self):
        config = copy.deepcopy(CONFIG)
        config["expedition"]["map_slots"] = {}
        agent, logs = self._logs(config, map_slot=1)
        self.assertIn("[远征] 配置里没有小图卡位1的坐标", logs)
        self.assertEqual(agent.point_clicks, [])

    def test_neither_name_nor_slot(self):
        agent, logs = self._logs(copy.deepcopy(CONFIG))
        self.assertIn("[远征] 既没给地图名字也没给卡位，不知道去哪，停", logs)
        self.assertEqual(agent.point_clicks, [])

    def test_navigation_failure_stops_before_flow(self):
        agent = ExpeditionFlowAgent(ExpeditionDevice(),
                                    copy.deepcopy(CONFIG))
        agent.current_location = "本丸"  # 导航没到达
        with patch("touken.flows.expedition.time.sleep"):
            logs = list(agent.expedition_stream(era=1, map_slot=1, team_no=2))
        self.assertIn("[远征] 到达远征失败", logs)
        self.assertEqual(agent.point_clicks, [])


if __name__ == "__main__":
    unittest.main()
