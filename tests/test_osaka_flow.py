# -*- coding: utf-8 -*-
"""大阪城挖地主循环的回归测试。

2026-08-26 事故：层末结算页转场慢半拍，同一画面被记成两圈；
随后层末找不到行军按钮直接整单收工，游戏被晾在选阵形画面一整夜。
"""
import unittest
from unittest.mock import patch

from touken.flows.osaka import OsakaMixin


class FakeMaa:
    def __init__(self):
        self.clicks = 0

    def screenshot(self, force=False):
        pass

    def click(self, point):
        self.clicks += 1


class OsakaFlow(OsakaMixin):
    """把主循环之外的所有依赖全部钉死，只留巡逻循环本身。"""

    def __init__(self, floor_done_seq):
        self.config = {
            "osaka": {"skip_tap": [775, 695]},
            "team_select": {"teams": {"3": [100, 100]}},
        }
        self.device = FakeMaa()
        self.current_location = "本丸"
        self._floor_done_seq = list(floor_done_seq)
        self.wait_march_result = None   # _wait_for_osaka_march 的返回值
        self.find_march_seq = []        # _find_osaka_march 的剧本，耗尽后恒 None
        self.formation_visible = False
        self.drop_evidence_without_name = False  # 掉刀证据在但名字没认出
        self.events = []

    def record_event(self, event_type, **payload):
        self.events.append({"event_type": event_type,
                            "payload": dict(payload)})

    # ---- 进场阶段：全部直通 ----
    def recover_game_update_stream(self):
        if False:
            yield
        return False

    def recover_network_stream(self):
        if False:
            yield
        return False

    def navigate_to_stream(self, dest):
        self.current_location = dest
        if False:
            yield

    def _open_osaka(self, cfg):
        return True

    def _wait_for_team_select(self, cfg, attempts=15, open_after=5):
        return True

    def _safe_depart_stream(self, cfg, team_no, tag, **kwargs):
        if False:
            yield
        return True, False

    def _confirm_departure(self, cfg):
        return True

    def _deny_heavy_injury_warning(self, cfg):
        return False

    def _expedition_takeover_requested(self, now=None):
        return False

    # ---- 巡逻循环里的感知/操作：按剧本走 ----
    def _osaka_floor_done(self, cfg):
        if self._floor_done_seq:
            return self._floor_done_seq.pop(0)
        return False

    def _team_injury_status(self, cfg):
        return None

    def _combined_injury_status(self, cfg, team_no, cache):
        return self._team_injury_status(cfg), None

    def _wait_for_osaka_march(self, cfg, attempts=8):
        return self.wait_march_result

    def _find_osaka_march(self, cfg):
        if self.find_march_seq:
            return self.find_march_seq.pop(0)
        return None

    def _formation_mode_state(self, allow_auto_without_title=False):
        return "manual" if self.formation_visible else None

    def choose_formation(self, **kwargs):
        self.formation_visible = False
        return "fixed"

    def _read_drop_sword(self):
        if self.drop_evidence_without_name:
            return {"status": "unrecognized", "sword": None}
        return {"status": "none", "sword": None}

    def _click_point(self, point):
        pass

    def quick_peek(self, tag=None, force=False):
        pass


class UnrecognizedDropFactTests(unittest.TestCase):
    """掉刀证据在、名字没认出：挖地没有圈事件可承载 drop_observation，
    必须留下可查询的结构化事实 sword.drop_unrecognized，绝不静默消失。"""

    def test_unrecognized_drop_recorded_once_per_screen(self):
        flow = OsakaFlow(floor_done_seq=[])
        flow.drop_evidence_without_name = True
        msgs = run_flow(flow, max_floors=1)

        facts = [e for e in flow.events
                 if e["event_type"] == "sword.drop_unrecognized"]
        self.assertEqual(len(facts), 1)   # 巡逻反复读到同一画面只记一次
        self.assertEqual(facts[0]["payload"],
                         {"source": "osaka.drop", "floor": 81})
        self.assertEqual([e for e in flow.events
                          if e["event_type"] == "sword.obtained"], [])
        # 空巡逻兜底照常收场，事实落账不影响任务收尾语义
        self.assertTrue(any("连续 300 次" in m for m in msgs))

    def test_no_evidence_leaves_no_fact(self):
        flow = OsakaFlow(floor_done_seq=[])
        msgs = run_flow(flow, max_floors=1)
        self.assertEqual([e for e in flow.events
                          if e["event_type"] == "sword.drop_unrecognized"], [])


def run_flow(flow, max_floors=90):
    with patch("touken.flows.osaka.time.sleep", lambda *_: None):
        return list(flow.osaka_stream(
            max_floors=max_floors, team_no=3, select_floor=False,
            auto_equip=False))


class FloorEndDebounceTests(unittest.TestCase):

    def test_same_floor_end_screen_counts_once(self):
        """层末结算页连续两拍都在（转场慢）：只记一圈，不出现幽灵圈。"""
        flow = OsakaFlow(floor_done_seq=[True, True])
        flow.wait_march_result = (1125, 592)   # 第一拍正常等到行军
        flow.find_march_seq = [(1125, 592)]    # 第二拍去重分支补点一次
        msgs = run_flow(flow)
        done = [m for m in msgs if "✓ 已完成" in m]
        self.assertEqual(len(done), 1, msgs)
        self.assertIn("1/90", done[0])

    def test_march_miss_at_floor_end_does_not_abort_run(self):
        """层末没等到行军按钮：回巡逻位继续观察，不再整单收工。"""
        flow = OsakaFlow(floor_done_seq=[True])
        flow.wait_march_result = None
        msgs = run_flow(flow)
        self.assertTrue(any("回巡逻位继续观察" in m for m in msgs), msgs)
        # 任务没有中止：一路巡逻到 idle 兜底才停
        self.assertTrue(any("连续 300 次没有识别到" in m for m in msgs), msgs)

    def test_stuck_floor_end_screen_eventually_stops(self):
        """结算页赖着不走（行军一直点不动）：去重补丁点，超时才真卡死停机。"""
        flow = OsakaFlow(floor_done_seq=[True] * 40)
        flow.wait_march_result = None
        flow.find_march_seq = []
        msgs = run_flow(flow)
        done = [m for m in msgs if "✓ 已完成" in m]
        self.assertEqual(len(done), 1, msgs)   # 也只记一圈
        self.assertTrue(any("30 秒没动静" in m for m in msgs), msgs)
        # 真卡死是终点，后面不再有巡逻兜底消息
        last = msgs[-1]
        self.assertIn("30 秒没动静", last)


if __name__ == "__main__":
    unittest.main()
