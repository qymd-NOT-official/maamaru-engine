# -*- coding: utf-8 -*-
"""每圈出阵事实记录（sortie.loop_started / completed / interrupted /
retreated_before_boss 的扩展 payload）与旧数据兼容的测试。

帧驱动假设备：screenshot(force=True) 推进一帧，帧是画面标记集合
（"formation" 阵形页、"result" 战斗结算页、"march" 行军按钮、
"home" 本丸、"march_stop" 自动行军停止横幅、"drop" 掉刀获得画面）。
"""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from touken.flows.sortie import SortieMixin
from touken.flows.battle import BattleMixin
from touken.device_adapter import Point
from touken.telemetry import TelemetryStore


class _FrameMaa:
    def __init__(self, frames):
        self.frames = list(frames)
        self.frame = set()
        # 帧标记 → 名牌 token：{"plate": ("太刀", "乱码丼")} 表示该帧
        # 左下名牌区 OCR 出这些文字（配合 _read_drop_sword 的真实认人）
        self.plate_tokens_by_marker = {}

    def screenshot(self, force=False):
        if force and self.frames:
            self.frame = set(self.frames.pop(0))
        return None

    def template_match(self, template, roi=None, threshold=0.7):
        if template == "home.png" and "home" in self.frame:
            return Point(1, 1)
        if template == "battle/ui战斗结果.png" and "result" in self.frame:
            return Point(640, 100)
        return None

    def ocr(self, expected, roi=None, match_mode="contains"):
        if expected == "自动行军停止" and "march_stop" in self.frame:
            return Point(1, 1)
        if expected == "新的刀剑男士" and "banner" in self.frame:
            return Point(640, 370)
        if expected == "刀派" and "badge" in self.frame:
            return Point(1135, 100)
        return None

    def ocr_all(self, roi, image=None):
        for marker, tokens in self.plate_tokens_by_marker.items():
            if marker in self.frame:
                return [(t, (10, 500)) for t in tokens]
        return []

    def click(self, point):
        return True


class _LoopHost(SortieMixin, BattleMixin):
    """能把 _map_sortie_stream 开进行军监控主循环的最小宿主。"""

    def __init__(self, frames, *, drop_id=None, injuries=(),
                 production_drop_reader=False):
        self.config = {
            "sortie": {
                "decide_button": {"template": "decide.png"},
                "home_ui": {"template": "home.png"},
                "skip_tap": [5, 5],
                "march_stop_ocr": {"expected": "自动行军停止",
                                   "roi": [0, 0, 100, 50]},
            },
            "map_select": {"合战场": {"chapters": {"1": [10, 10]},
                                      "maps": {"1": [20, 20]}}},
            "team_select": {"teams": {"3": [30, 30]}},
        }
        self.current_location = "出阵"
        self.device = _FrameMaa(frames)
        self.events = []
        self._drop_id = drop_id
        self._injuries = list(injuries)
        self._production_drop_reader = production_drop_reader

    def record_event(self, event_type, **payload):
        self.events.append({"ts": time.time(), "event_type": event_type,
                            "payload": dict(payload)})

    def navigate_to_stream(self, dest):
        self.current_location = dest
        yield f"nav→{dest}"

    def _click_point(self, pt):
        pass

    def _expedition_takeover_requested(self, now=None):
        return False  # 测试宿主：默认没有远征排班等接管

    def _wait_for_team_select(self, cfg, attempts=12, open_after=2):
        return True

    def _pick_team(self, team_no):
        pass

    def _team_injury_status(self, cfg):
        return self._injuries.pop(0) if self._injuries else None

    def _combined_injury_status(self, cfg, team_no, log_cache):
        # 委托给视觉桩，保持本文件的遥测语义不变（日志通道由
        # test_youzu_log.py 的 _log_injury_status 专项覆盖）
        return self._team_injury_status(cfg), None

    def _injury_reaches_threshold(self, injury, threshold):
        return True

    def _disable_auto_march(self):
        return True

    def _configure_auto_march_settings(self, policy):
        return True

    def _enable_auto_march(self):
        return True

    def _click_depart(self, cfg):
        return True

    def _cancel_equip_warning(self, cfg):
        return None

    def _deny_heavy_injury_warning(self, cfg):
        return False

    def _formation_mode_state(self, allow_auto_without_title=False):
        return "manual" if "formation" in self.device.frame else None

    def choose_formation(self, formation_name="鱼鳞阵", enable_auto=False):
        return "fixed"

    def _find_march_continue(self, cfg):
        return Point(1100, 600) if "march" in self.device.frame else None

    def _read_drop_sword(self):
        if self._production_drop_reader:
            return super()._read_drop_sword()
        if self._drop_id is not None and "drop" in self.device.frame:
            return {"status": "recognized",
                    "sword": {"sword_id": self._drop_id,
                              "name": "厚藤四郎", "name_jp": "厚藤四郎"}}
        return {"status": "none", "sword": None}

    def _return_home_from_march(self, cfg):
        return True

    # ---- 异去（cfg_key="yosari"）生产链路的宿主桩：只在该路径被走到 ----

    def _enter_yosari(self, cfg):
        return True

    def _dismiss_yosari_milestone(self, cfg):
        return False

    def wait_landmark_skipping(self, **kwargs):
        return True

    def _confirm_yosari_departure(self, cfg, auto_refill=False,
                                  refill_attempted=False):
        yield from ()
        return True

    def _yosari_round_done(self, cfg):
        return False

    def _read_yosari_fragments(self, cfg):
        return None

    def _save_team_record(self, cfg, record_no=1):
        return True

    def _restore_equipment_from_warning(self, cfg, record_no=1):
        return None

    def by_type(self, event_type):
        return [e for e in self.events if e["event_type"] == event_type]


def _run(host, **kwargs):
    kwargs.setdefault("auto_equip", False)
    with patch("touken.flows.sortie.time.sleep"), \
         patch("touken.flows.sortie.find_deploy_button",
               return_value=Point(1, 1)):
        return list(host.sortie_stream(chapter=1, map_no=1, team_no=3,
                                       **kwargs))


# 每圈出发前固定消耗 3 帧：章节页确认、重伤检查、出阵后确认
PRE = [{}, {}, {}]


class LoopFactTests(unittest.TestCase):
    def test_yosari_refill_reenters_safety_chain_and_never_refills_twice(self):
        from unittest.mock import Mock
        host = _LoopHost([{}] * 12)
        host.config["yosari"] = host.config["sortie"]
        host.config["map_select"]["异去"] = {
            "chapters": {"1": [10, 10]}, "maps": {"4": [20, 20]}}
        host._pick_team = Mock()
        host._click_depart = Mock(return_value=True)
        confirmations = []

        def confirm(cfg, auto_refill=False, refill_attempted=False):
            confirmations.append((auto_refill, refill_attempted))
            yield from ()
            return False if refill_attempted else "refilled"

        host._confirm_yosari_departure = confirm
        with patch("touken.flows.sortie.time.sleep"), patch(
                "touken.flows.sortie.find_deploy_button", return_value=Point(1, 1)):
            list(host.yosari_stream(map_no=4, auto_march=False,
                                   auto_equip=False, auto_refill=True))
        self.assertEqual(host._pick_team.call_count, 2)
        self.assertEqual(host._click_depart.call_count, 2)
        self.assertEqual(confirmations, [(True, False), (True, True)])
        self.assertEqual(host.by_type("sortie.loop_started"), [])

    def test_yosari_refill_does_not_bypass_new_injury(self):
        from unittest.mock import Mock
        host = _LoopHost([{}] * 12)
        host.config["yosari"] = host.config["sortie"]
        host.config["map_select"]["异去"] = {
            "chapters": {"1": [10, 10]}, "maps": {"4": [20, 20]}}
        host._combined_injury_status = Mock(side_effect=[(None, None), ("重伤", None)])
        host._click_depart = Mock(return_value=True)
        confirmations = []

        def confirm(cfg, **kwargs):
            confirmations.append(kwargs)
            yield from ()
            return "refilled"

        host._confirm_yosari_departure = confirm
        with patch("touken.flows.sortie.time.sleep"), patch(
                "touken.flows.sortie.find_deploy_button", return_value=Point(1, 1)):
            list(host.yosari_stream(map_no=4, auto_march=False,
                                   auto_equip=False, auto_refill=True))
        host._click_depart.assert_called_once()
        self.assertEqual(len(confirmations), 1)
        self.assertEqual(host.by_type("sortie.loop_started"), [])

    def test_two_loops_first_loop_has_duration_and_battles(self):
        frames = (PRE
                  + [{"formation"}, {"formation"}, {},
                     {"result", "march"}, {},
                     {"formation"}, {},
                     {"result", "march"}, {"home"}]
                  + PRE
                  + [{"result", "march"}, {}, {"home"}])
        host = _LoopHost(frames)
        _run(host, auto_march=False, max_loops=2)

        started = host.by_type("sortie.loop_started")
        completed = host.by_type("sortie.completed")
        self.assertEqual([e["payload"]["sequence"] for e in started], [1, 2])
        self.assertEqual([e["payload"]["sequence"] for e in completed], [1, 2])
        for event in started:
            self.assertEqual(event["payload"]["march_mode"], "script")
            self.assertEqual(event["payload"]["attempt"], 1)
        self.assertEqual([e["payload"]["battle_count"] for e in completed],
                         [2, 1])
        for begin, end in zip(started, completed):
            payload = end["payload"]
            self.assertEqual(payload["outcome"], "completed")
            self.assertEqual(payload["mode"], "sortie")
            self.assertEqual(payload["map_no"], 1)
            self.assertEqual(payload["team_no"], 3)
            self.assertEqual(payload["drop_observation"], "confirmed_none")
            self.assertEqual(payload["drops_recognized"], 0)
            # duration 就是「出发 → 回本」的纯游戏流程耗时，第一圈也有
            self.assertIsNotNone(payload["duration_seconds"])
            self.assertGreaterEqual(payload["duration_seconds"], 0)
            self.assertAlmostEqual(payload["duration_seconds"],
                                   end["ts"] - begin["ts"], delta=1.0)

    def test_retreat_before_boss_records_own_outcome(self):
        frames = PRE + [{"result", "march"}, {"march"}]
        host = _LoopHost(frames)
        with patch("touken.flows.sortie.CV2_AVAILABLE", True), \
             patch("touken.flows.sortie.boss_distance_from_image",
                   side_effect=[2, 1]):
            _run(host, auto_march=True, max_loops=1, retreat_before_boss=True)

        retreated = host.by_type("sortie.retreated_before_boss")
        self.assertEqual(len(retreated), 1)
        payload = retreated[0]["payload"]
        self.assertEqual(payload["outcome"], "retreated_before_boss")
        self.assertEqual(payload["battle_count"], 1)
        self.assertEqual(payload["drop_observation"], "confirmed_none")
        self.assertIsNotNone(payload["duration_seconds"])
        self.assertEqual(host.by_type("sortie.completed"), [])

    def test_non_injury_stop_does_not_retry_forever(self):
        host = _LoopHost(PRE + [{"march_stop"}], injuries=[None, None])
        logs = _run(host, auto_march=True, max_loops=1, repair_threshold="medium")
        self.assertEqual(len(host.by_type("sortie.loop_started")), 1)
        self.assertEqual(host.by_type("sortie.interrupted")[0]["payload"]["interrupt_reason"],
                         "auto_march_non_injury_stop")
        self.assertTrue(any("其他条件停止" in msg for msg in logs))

    def test_interruption_then_retry_same_sequence(self):
        frames = (PRE
                  + [{"result"}, {"march_stop"}]
                  + PRE
                  + [{"result"}, {}, {"home"}])
        host = _LoopHost(frames, injuries=[None, "中伤", None])
        _run(host, auto_march=True, max_loops=1, repair_threshold="medium")

        types = [e["event_type"] for e in host.events]
        self.assertEqual(types, ["sortie.loop_started", "sortie.interrupted",
                                 "sortie.loop_started", "sortie.completed"])
        interrupted = host.by_type("sortie.interrupted")[0]["payload"]
        self.assertEqual(interrupted["outcome"], "interrupted")
        self.assertEqual(interrupted["interrupt_reason"], "auto_march_stopped")
        self.assertEqual(interrupted["battle_count"], 1)
        self.assertEqual(interrupted["attempt"], 1)
        # 委托自动行军：游戏跳过获得动画，掉落只能记「没观察到」
        self.assertEqual(interrupted["drop_observation"], "not_observed")
        self.assertEqual(interrupted["drop_observation_reason"],
                         "auto_march_skips_obtain_animation")
        completed = host.by_type("sortie.completed")[0]["payload"]
        self.assertEqual(completed["sequence"], 1)
        self.assertEqual(completed["attempt"], 2)
        self.assertEqual(completed["battle_count"], 1)
        self.assertEqual(completed["drop_observation"], "not_observed")

    def test_drop_recognized_and_deduped(self):
        frames = (PRE
                  + [{"drop"}, {"drop"}, {"result", "march"}, {}, {"home"}])
        host = _LoopHost(frames, drop_id=130)
        _run(host, auto_march=False, max_loops=1)

        obtained = host.by_type("sword.obtained")
        self.assertEqual(len(obtained), 1)  # 获得画面连帧只记一次
        self.assertEqual(obtained[0]["payload"]["sword_id"], 130)
        self.assertEqual(obtained[0]["payload"]["source"], "sortie.drop")
        self.assertEqual(obtained[0]["payload"]["sequence"], 1)
        completed = host.by_type("sortie.completed")[0]["payload"]
        self.assertEqual(completed["drop_observation"], "recognized")
        self.assertEqual(completed["drops_recognized"], 1)

    def test_result_page_repeated_frames_count_once(self):
        frames = (PRE
                  + [{"result"}, {"result"}, {"result"}, {},
                     {"result"}, {"result"}, {"home"}])
        host = _LoopHost(frames)
        _run(host, auto_march=False, max_loops=1)
        payload = host.by_type("sortie.completed")[0]["payload"]
        self.assertEqual(payload["battle_count"], 2)

    def test_completed_loop_without_any_result_page_is_unknown_not_zero(self):
        # 正常完成必有王点结算页；0 场只可能是锚点失明，不许拿 0 交账
        frames = PRE + [{}, {"home"}]
        host = _LoopHost(frames)
        _run(host, auto_march=False, max_loops=1)
        payload = host.by_type("sortie.completed")[0]["payload"]
        self.assertIsNone(payload["battle_count"])
        self.assertIn("battle_count_note", payload)

    def test_monitor_timeout_records_unknown_not_task_status(self):
        frames = PRE + [{}]  # 帧耗尽后停在空画面，300 拍后触发安全上限
        host = _LoopHost(frames)
        logs = _run(host, auto_march=False, max_loops=1)
        self.assertTrue(any("安全上限" in msg for msg in logs))
        interrupted = host.by_type("sortie.interrupted")
        self.assertEqual(len(interrupted), 1)
        payload = interrupted[0]["payload"]
        self.assertEqual(payload["outcome"], "unknown")
        self.assertEqual(payload["interrupt_reason"], "monitor_timeout")
        self.assertEqual(payload["drop_observation"], "not_observed")
        self.assertEqual(payload["drop_observation_reason"],
                         "observation_lost")

    def test_result_edge_consumed_inside_drop_reader_still_counted(self):
        # 回归：生产版 _read_drop_sword 内部会 screenshot(force=True) 取新帧。
        # 第二场战斗的结算页若在掉落观察内部被取到，也绝不能绕过计数。
        # 帧剧本：结算1 →（外层计数）→ 掉落观察内部吃掉 横幅帧 + 结算2 → 空帧
        frames = (PRE
                  + [{"result"}]            # 外层看见第 1 场结算页
                  + [{"banner"}, {"result"}]  # 掉落观察耐心模式内部取帧
                  + [{}] * 8                # 耐心模式耗尽 10 次尝试
                  + [{"home"}])
        host = _LoopHost(frames, production_drop_reader=True)
        _run(host, auto_march=False, max_loops=1)
        payload = host.by_type("sortie.completed")[0]["payload"]
        self.assertEqual(payload["battle_count"], 2)

    def test_drop_belongs_to_the_attempt_that_saw_it(self):
        # attempt 1 认了掉落后中伤中断，attempt 2 同 sequence 重试完成：
        # 刀必须明确挂在 attempt 1 上，completed 的 attempt 2 是 confirmed_none
        frames = (PRE
                  + [{"drop"}, {"march"}]
                  + PRE
                  + [{"result", "march"}, {}, {"home"}])
        host = _LoopHost(frames, drop_id=130,
                         injuries=[None, "中伤", None])
        _run(host, auto_march=False, max_loops=1)

        obtained = host.by_type("sword.obtained")
        self.assertEqual(len(obtained), 1)
        self.assertEqual(obtained[0]["payload"]["sequence"], 1)
        self.assertEqual(obtained[0]["payload"]["attempt"], 1)
        interrupted = host.by_type("sortie.interrupted")[0]["payload"]
        self.assertEqual(interrupted["attempt"], 1)
        self.assertEqual(interrupted["drop_observation"], "recognized")
        completed = host.by_type("sortie.completed")[0]["payload"]
        self.assertEqual(completed["attempt"], 2)
        self.assertEqual(completed["drop_observation"], "confirmed_none")


class DropHonestyTests(unittest.TestCase):
    """工单 P1：掉刀证据 vs 无证据必须分家。

    认不出名字的掉刀圈落 not_observed(recognizer_error)，绝不落
    confirmed_none；后续拍认出名字照常 recognized；失败状态每圈重置，
    不许泄漏到下一圈。帧剧本全走真实 _read_drop_sword。
    """

    def _run_frames(self, frames, **kwargs):
        kwargs.setdefault("auto_march", False)
        kwargs.setdefault("max_loops", 1)
        host = _LoopHost(
            frames,
            production_drop_reader=kwargs.pop("production_drop_reader", True))
        logs = _run(host, **kwargs)
        return host, logs

    def _completed(self, host):
        return host.by_type("sortie.completed")[0]["payload"]

    def test_no_evidence_stays_confirmed_none(self):
        # 普通画面没有横幅/立牌/名牌证据：无掉落照实记 confirmed_none
        host, _ = self._run_frames(PRE + [{"result", "march"}, {}, {"home"}])
        payload = self._completed(host)
        self.assertEqual(payload["drop_observation"], "confirmed_none")
        self.assertNotIn("drop_observation_reason", payload)
        self.assertEqual(host.by_type("sword.obtained"), [])

    def test_banner_with_garbled_plate_is_not_observed(self):
        # 横幅在（确知掉刀）但名牌拍数耗尽仍没认出：
        # 必须 not_observed(recognizer_error)，绝不许落 confirmed_none
        host, _ = self._run_frames(
            PRE + [{"result", "march"}, {}] + [{"banner"}] * 9 + [{"home"}])
        payload = self._completed(host)
        self.assertEqual(payload["drop_observation"], "not_observed")
        self.assertEqual(payload["drop_observation_reason"], "recognizer_error")
        self.assertEqual(host.by_type("sword.obtained"), [])

    def test_type_prefix_evidence_without_banner_also_downgrades(self):
        # 名牌读出刀种前缀但名字乱码、且没有横幅：同样是有证据认不出，
        # 同样降级——不能漏掉这条证据路径
        host = _LoopHost(
            PRE + [{"result", "march"}, {}] + [{"plate"}] * 9 + [{"home"}],
            production_drop_reader=True)
        host.device.plate_tokens_by_marker = {"plate": ("太刀", "乱码丼")}
        _run(host, auto_march=False, max_loops=1)
        payload = self._completed(host)
        self.assertEqual(payload["drop_observation"], "not_observed")
        self.assertEqual(payload["drop_observation_reason"], "recognizer_error")
        self.assertEqual(host.by_type("sword.obtained"), [])

    def test_transient_failure_then_success_stays_recognized(self):
        # 前几拍名牌读花（刀种在、名字乱码），后续拍认出大和守安定：
        # 正常 recognized + 一条掉落事实，不因短暂 OCR 失败降级
        host = _LoopHost(
            PRE + [{"result", "march"}, {}]
            + [{"plate"}] * 3 + [{"plate_ok"}] * 3 + [{"home"}],
            production_drop_reader=True)
        host.device.plate_tokens_by_marker = {
            "plate": ("太刀", "乱码丼"),
            "plate_ok": ("打刀", "大和守安定"),
        }
        _run(host, auto_march=False, max_loops=1)
        payload = self._completed(host)
        self.assertEqual(payload["drop_observation"], "recognized")
        obtained = host.by_type("sword.obtained")
        self.assertEqual(len(obtained), 1)
        self.assertEqual(obtained[0]["payload"]["name"], "大和守安定")

    def test_recognized_then_unrecognized_leaves_both_facts(self):
        # 同一圈先认出一振（大和守安定），随后另一振明确掉落但没认出：
        # 圈总结保持 recognized（事实优先），但第二振的独立事实必须留下——
        # 一条 sword.obtained 和一条 sword.drop_unrecognized 同时存在
        host = _LoopHost(
            PRE + [{"result", "march"}, {}]
            + [{"plate_ok"}] * 3 + [{"banner"}] * 9 + [{"home"}],
            production_drop_reader=True)
        host.device.plate_tokens_by_marker = {
            "plate_ok": ("打刀", "大和守安定"),
        }
        _run(host, auto_march=False, max_loops=1)
        payload = self._completed(host)
        self.assertEqual(payload["drop_observation"], "recognized")
        obtained = host.by_type("sword.obtained")
        self.assertEqual(len(obtained), 1)
        self.assertEqual(obtained[0]["payload"]["name"], "大和守安定")
        facts = host.by_type("sword.drop_unrecognized")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["payload"], {
            "source": "sortie.drop", "mode": "sortie", "chapter": 1,
            "map_no": 1, "sequence": 1, "attempt": 1})

    def test_yosari_production_path_emits_mode(self):
        # 异去生产链路（yosari_stream）真实产出的未识别事实必须带
        # mode="yosari"，否则成绩单会把异去掉刀错标成合战场——
        # 不能只靠手工构造前端测试数据做验收
        host = _LoopHost(
            PRE + [{"result", "march"}, {}] + [{"plate"}] * 9 + [{"home"}],
            production_drop_reader=True)
        host.device.plate_tokens_by_marker = {"plate": ("太刀", "乱码丼")}
        host.config["yosari"] = host.config["sortie"]
        host.config["map_select"]["异去"] = {"chapters": {"1": [10, 10]},
                                             "maps": {"4": [20, 20]}}
        with patch("touken.flows.sortie.time.sleep"), \
             patch("touken.flows.sortie.find_deploy_button",
                   return_value=Point(1, 1)):
            list(host.yosari_stream(map_no=4, auto_march=False, max_loops=1))
        facts = host.by_type("sword.drop_unrecognized")
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["payload"], {
            "source": "sortie.drop", "mode": "yosari", "chapter": 1,
            "map_no": 4, "sequence": 1, "attempt": 1})

    def test_recognizer_exception_is_not_observed(self):
        # 认人流程自身抛异常：同样 not_observed(recognizer_error)
        host = _LoopHost(
            PRE + [{"result", "march"}, {}, {"plate"}, {"home"}],
            production_drop_reader=True)
        host.device.plate_tokens_by_marker = {"plate": ("太刀", "乱码丼")}
        with patch("touken.flows.sortie.sword_db.find_by_name",
                   side_effect=RuntimeError("recognizer boom")):
            _run(host, auto_march=False, max_loops=1)
        payload = self._completed(host)
        self.assertEqual(payload["drop_observation"], "not_observed")
        self.assertEqual(payload["drop_observation_reason"], "recognizer_error")

    def test_failure_does_not_leak_into_next_loop(self):
        # 第 1 圈掉刀证据认不出（降级），第 2 圈全程无证据：
        # 圈边界必须重置失败状态，第 2 圈照常 confirmed_none
        host, _ = self._run_frames(
            PRE + [{"result", "march"}, {}] + [{"banner"}] * 9 + [{"home"}]
            + PRE + [{"result", "march"}, {}, {"home"}],
            max_loops=2)
        first, second = (host.by_type("sortie.completed")[0]["payload"],
                         host.by_type("sortie.completed")[1]["payload"])
        self.assertEqual(first["drop_observation"], "not_observed")
        self.assertEqual(first["drop_observation_reason"], "recognizer_error")
        self.assertEqual(second["drop_observation"], "confirmed_none")
        self.assertNotIn("drop_observation_reason", second)


class OldDataCompatTests(unittest.TestCase):
    """旧版 telemetry 事件（没有新字段）必须照常聚合，新字段不顶坏老读者。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TelemetryStore(Path(self.tmp.name) / "telemetry.db")
        self.env = patch.dict(os.environ, {"MAAMARU_RUN_ID": "run-1",
                                           "MAAMARU_SCRIPT": "sortie"})
        self.env.start()
        self.store.start_run("run-1", "sortie", started_at=time.time() - 600)

    def tearDown(self):
        self.env.stop()
        self.store.close()
        self.tmp.cleanup()

    def test_old_style_events_still_drive_run_summary(self):
        # 旧 payload 只有 mode/chapter/map_no/team_no/sequence
        self.store.record_event("sortie.completed",
                                {"mode": "sortie", "chapter": 5, "map_no": 4,
                                 "team_no": 3, "sequence": 1})
        time.sleep(0.01)
        self.store.record_event("sortie.completed",
                                {"mode": "sortie", "chapter": 5, "map_no": 4,
                                 "team_no": 3, "sequence": 2})
        self.store.finish_run("run-1", "completed")
        summary = self.store.run_summary("run-1")
        self.assertEqual(summary["loops"], 2)
        self.assertIsNotNone(summary["average_loop_seconds"])

    def test_new_fields_do_not_break_loop_aggregation(self):
        self.store.record_event("sortie.loop_started",
                                {"mode": "yosari", "sequence": 1})
        self.store.record_event(
            "sortie.completed",
            {"mode": "yosari", "chapter": 1, "map_no": 2, "team_no": 3,
             "sequence": 1, "attempt": 1, "outcome": "completed",
             "duration_seconds": 301.5, "march_mode": "delegated",
             "battle_count": 4, "battle_count_basis": "battle_result_page_edges",
             "drop_observation": "not_observed", "drops_recognized": 0})
        # 中断不算完成圈，不进圈速分母
        self.store.record_event(
            "sortie.interrupted",
            {"mode": "yosari", "chapter": 1, "map_no": 2, "team_no": 3,
             "sequence": 2, "attempt": 1, "outcome": "interrupted",
             "interrupt_reason": "auto_march_stopped",
             "duration_seconds": 122.0, "march_mode": "delegated",
             "battle_count": 2, "drop_observation": "not_observed",
             "drops_recognized": 0})
        self.store.finish_run("run-1", "completed")
        summary = self.store.run_summary("run-1")
        self.assertEqual(summary["loops"], 1)

    def test_drop_links_to_loop_by_run_and_sequence(self):
        self.store.record_event(
            "sortie.completed",
            {"mode": "sortie", "chapter": 5, "map_no": 4, "team_no": 3,
             "sequence": 1, "outcome": "completed",
             "drop_observation": "recognized", "drops_recognized": 1})
        self.store.record_event("sword.obtained",
                                {"sword_id": 130, "name": "厚藤四郎",
                                 "source": "sortie.drop", "chapter": 5,
                                 "map_no": 4, "sequence": 1})
        events = self.store.recent_events(limit=10)
        obtained = next(e for e in events
                        if e["event_type"] == "sword.obtained")
        completed = next(e for e in events
                         if e["event_type"] == "sortie.completed")
        self.assertEqual(obtained["run_id"], "run-1")
        self.assertEqual(obtained["run_id"], completed["run_id"])
        self.assertEqual(obtained["payload"]["sequence"],
                         completed["payload"]["sequence"])


class LoopRecordPairingTests(unittest.TestCase):
    """成绩单逐圈事实：run_summary.loop_records 的配对与兜底口径。

    老数据没有 loop_started 也照常出明细；没有结束事件的出发必须
    「结果未知」，绝不假定成功；耗时只认这一圈自己的出发/结束事实，
    整个任务的耗时（比如混了异去和锻刀的 workflow）不许冒充圈速。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TelemetryStore(Path(self.tmp.name) / "telemetry.db")
        self.env = patch.dict(os.environ, {"MAAMARU_RUN_ID": "run-1",
                                           "MAAMARU_SCRIPT": "workflow"})
        self.env.start()
        self.store.start_run("run-1", "workflow", started_at=time.time() - 600)

    def tearDown(self):
        self.env.stop()
        self.store.close()
        self.tmp.cleanup()

    def _summary(self):
        self.store.finish_run("run-1", "completed")
        return self.store.run_summary("run-1")

    def test_paired_loop_carries_full_facts(self):
        # 验收样本同构：异去 1-4，出发 85 秒后完成，委托行军没观察掉落
        self.store.record_event("sortie.loop_started",
                                {"mode": "yosari", "chapter": 1, "map_no": 4,
                                 "team_no": 2, "sequence": 1, "attempt": 1,
                                 "march_mode": "delegated"})
        time.sleep(0.05)
        self.store.record_event(
            "sortie.completed",
            {"mode": "yosari", "chapter": 1, "map_no": 4, "team_no": 2,
             "sequence": 1, "attempt": 1, "outcome": "completed",
             "duration_seconds": 84.3, "march_mode": "delegated",
             "battle_count": 3, "drop_observation": "not_observed",
             "drops_recognized": 0,
             "drop_observation_reason": "auto_march_skips_obtain_animation"})
        records = self._summary()["loop_records"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["mode"], "yosari")
        self.assertEqual((record["chapter"], record["map_no"]), (1, 4))
        self.assertEqual((record["sequence"], record["attempt"]), (1, 1))
        self.assertEqual(record["end_type"], "sortie.completed")
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(record["duration_seconds"], 84.3)
        self.assertEqual(record["battle_count"], 3)
        self.assertEqual(record["march_mode"], "delegated")
        self.assertEqual(record["drop_observation"], "not_observed")
        self.assertEqual(record["drop_observation_reason"],
                         "auto_march_skips_obtain_animation")
        self.assertIsNotNone(record["started_at"])
        self.assertIsNotNone(record["ended_at"])

    def test_unclosed_started_is_unknown_never_success(self):
        self.store.record_event("sortie.loop_started",
                                {"mode": "yosari", "chapter": 1, "map_no": 4,
                                 "sequence": 2, "attempt": 1,
                                 "march_mode": "script"})
        records = self._summary()["loop_records"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIsNone(record["end_type"])
        self.assertEqual(record["outcome"], "unknown")
        self.assertIsNone(record["duration_seconds"])
        self.assertIsNone(record["ended_at"])
        self.assertEqual(record["march_mode"], "script")

    def test_interrupted_and_retry_pair_by_attempt(self):
        # 同 sequence 第 1 次出发中断，第 2 次出发完成：两条事实各归各的
        self.store.record_event("sortie.loop_started",
                                {"mode": "yosari", "chapter": 1, "map_no": 4,
                                 "sequence": 1, "attempt": 1,
                                 "march_mode": "delegated"})
        time.sleep(0.01)
        self.store.record_event(
            "sortie.interrupted",
            {"mode": "yosari", "chapter": 1, "map_no": 4, "sequence": 1,
             "attempt": 1, "outcome": "interrupted",
             "interrupt_reason": "auto_march_stopped",
             "duration_seconds": 122.0, "march_mode": "delegated",
             "battle_count": 2, "drop_observation": "not_observed"})
        time.sleep(0.01)
        self.store.record_event("sortie.loop_started",
                                {"mode": "yosari", "chapter": 1, "map_no": 4,
                                 "sequence": 1, "attempt": 2,
                                 "march_mode": "delegated"})
        time.sleep(0.01)
        self.store.record_event(
            "sortie.completed",
            {"mode": "yosari", "chapter": 1, "map_no": 4, "sequence": 1,
             "attempt": 2, "outcome": "completed",
             "duration_seconds": 90.0, "march_mode": "delegated",
             "battle_count": 3, "drop_observation": "confirmed_none"})
        records = self._summary()["loop_records"]
        self.assertEqual([r["attempt"] for r in records], [1, 2])
        self.assertEqual([r["outcome"] for r in records],
                         ["interrupted", "completed"])
        self.assertEqual([r["duration_seconds"] for r in records],
                         [122.0, 90.0])
        self.assertEqual(records[0]["interrupt_reason"], "auto_march_stopped")

    def test_start_facts_survive_partial_end_payload(self):
        # 结束事件缺 team_no / march_mode 时，出发事实里的已知信息不丢；
        # 双方都有的字段以结束事实为准
        self.store.record_event("sortie.loop_started",
                                {"mode": "yosari", "chapter": 1, "map_no": 4,
                                 "team_no": 2, "sequence": 1, "attempt": 1,
                                 "march_mode": "delegated"})
        time.sleep(0.01)
        self.store.record_event(
            "sortie.completed",
            {"mode": "yosari", "chapter": 1, "map_no": 4, "sequence": 1,
             "attempt": 1, "outcome": "completed",
             "duration_seconds": 90.0, "battle_count": 3,
             "drop_observation": "confirmed_none"})
        record = self._summary()["loop_records"][0]
        self.assertEqual(record["team_no"], 2)          # 出发事实补位
        self.assertEqual(record["march_mode"], "delegated")  # 出发事实补位
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(record["duration_seconds"], 90.0)
        self.assertEqual(record["battle_count"], 3)

    def test_old_style_end_without_started_still_records(self):
        # 旧版 payload 没有出发事件/attempt/outcome：明细照出，attempt 实话
        self.store.record_event("sortie.completed",
                                {"mode": "sortie", "chapter": 5, "map_no": 4,
                                 "team_no": 3, "sequence": 1})
        records = self._summary()["loop_records"]
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["end_type"], "sortie.completed")
        self.assertEqual(record["outcome"], "completed")  # 完成事件兜底口径
        self.assertIsNone(record["attempt"])
        self.assertIsNone(record["duration_seconds"])
        self.assertIsNone(record["drop_observation"])

    def test_duration_falls_back_to_start_end_interval(self):
        # 结束事件没带 duration_seconds（老版本）：用本圈出发→结束的时间差
        self.store.record_event("sortie.loop_started",
                                {"mode": "yosari", "chapter": 1, "map_no": 2,
                                 "sequence": 1, "attempt": 1})
        time.sleep(0.06)
        self.store.record_event(
            "sortie.completed",
            {"mode": "yosari", "chapter": 1, "map_no": 2, "sequence": 1,
             "attempt": 1, "outcome": "completed"})
        record = self._summary()["loop_records"][0]
        self.assertIsNotNone(record["duration_seconds"])
        self.assertGreaterEqual(record["duration_seconds"], 0.05)
        self.assertLess(record["duration_seconds"], 5.0)


if __name__ == "__main__":
    unittest.main()
