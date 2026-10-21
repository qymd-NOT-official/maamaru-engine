import unittest
from unittest.mock import patch

from touken.flows.battle import BattleMixin
from touken.flows.daily import (
    DailyMixin,
    _equip_warning_status,
    _is_fail,
    _is_success_status,
    _practice_report_status,
    _shop_report_status,
)
from touken.flows.rewards import RewardsMixin
from touken.device_adapter import Point, _ocr_text_matches


class FakeMaa:
    def __init__(self, matches):
        self.matches = {name: list(values) for name, values in matches.items()}
        self.clicked = []
        self.screenshots = 0
        self.ocr_results = []

    def template_match(self, template):
        values = self.matches.get(template, [])
        return values.pop(0) if values else None

    def click(self, point):
        self.clicked.append(point)

    def screenshot(self, force=False):
        self.screenshots += 1

    def ocr(self, expected, roi):
        return self.ocr_results.pop(0) if self.ocr_results else None


class EquipWarningTests(unittest.TestCase):
    def test_warning_uses_prepare_and_returns_to_team_select(self):
        flow = BattleMixin()
        prepare = Point(420, 500)
        flow.device = FakeMaa({
            "team/继续出阵.png": [Point(850, 500)],
            "team/整备刀装.png": [prepare],
        })
        flow.device.ocr_results = [Point(640, 29)]

        with patch("touken.flows.battle.time.sleep"):
            result = flow._cancel_equip_warning({
                "equip_warning_button": {"template": "team/继续出阵.png"},
                "team_ui_ocr": {
                    "expected": "部队选择",
                    "roi": [506, 1, 774, 77],
                },
            })

        self.assertTrue(result)
        self.assertEqual(flow.device.clicked, [prepare])

    def test_no_warning_does_nothing(self):
        flow = BattleMixin()
        flow.device = FakeMaa({})
        result = flow._cancel_equip_warning({
            "equip_warning_button": {"template": "team/继续出阵.png"}
        })
        self.assertIsNone(result)
        self.assertEqual(flow.device.clicked, [])


class DailyReportTests(unittest.TestCase):
    def test_equip_warning_is_not_green(self):
        msg = "[出阵] ⚠️ 刀装未满警告；已取消出阵并返回部队选择，本次跳过"
        self.assertTrue(_is_fail(msg))
        self.assertEqual(
            _equip_warning_status(msg),
            "⚠ 刀装未满，已取消出阵并跳过",
        )

    def test_failed_cancel_is_reported_as_failure(self):
        msg = "[出阵] ⚠️ 刀装未满警告；没能安全取消整备，本次出阵停止"
        self.assertEqual(
            _equip_warning_status(msg),
            "✗ 刀装未满，取消整备失败，出阵停止",
        )

    def test_daily_sortie_step_keeps_detailed_skip_reason(self):
        flow = DailyMixin()
        flow.raid_stream = lambda **kwargs: iter([
            "[RAID] ⚠️ 刀装未满警告；已取消出阵并返回部队选择，本次跳过"
        ])
        report = []

        list(flow._sortie_step({"sortie": {"mode": "raid"}}, report))

        self.assertEqual(report, [
            ("出阵(活动)", "⚠ 刀装未满，已取消出阵并跳过")
        ])

    def test_shop_sold_out_is_a_detailed_success(self):
        msg = "[SHOP] 今日暖心礼包已售罄，说明此前已经领取，跳过"
        self.assertFalse(_is_fail(msg))
        self.assertEqual(_shop_report_status(msg), "✓ 此前已领取（售罄）")

    def test_detailed_shop_success_is_green_in_final_report(self):
        self.assertTrue(_is_success_status("✓ 本次领取成功"))
        self.assertTrue(_is_success_status("✓ 此前已领取（售罄）"))
        self.assertFalse(_is_success_status("✗ 未识别到领取按钮，未点击"))
        self.assertFalse(_is_success_status("⚠ 刀装未满，已取消出阵并跳过"))

    def test_shop_recognition_failure_is_not_green(self):
        msg = "[SHOP] 暖心礼包未售罄，但未识别到领取按钮，本次未点击"
        self.assertTrue(_is_fail(msg))
        self.assertEqual(
            _shop_report_status(msg),
            "✗ 未识别到领取按钮，未点击",
        )

    def test_20260824_practice_formation_stall_is_not_green(self):
        """8-24 事故：阵形页卡死连环翻车 7 步，成绩单却全绿。"""
        for msg in (
            "[演练] 阵形选择失败，停止这场，避免在选择页误点",
            "[演练] 阵形仍未确认，停止这场，避免在选择页误点",
            "[演练] : 再次进入失败，跳过",
        ):
            self.assertTrue(_is_fail(msg), msg)

    def test_practice_zero_new_wins_is_a_detailed_failure(self):
        msg = "[演练] 收工：本次新赢 0 场，当前刷新场累计 0 场"
        self.assertFalse(_is_fail(msg))  # 词表不背这锅，专项判分接住
        self.assertEqual(_practice_report_status(msg), "✗ 一场没赢")
        self.assertFalse(_is_success_status("✗ 一场没赢"))

    def test_practice_enough_wins_and_real_wins_stay_green(self):
        self.assertEqual(
            _practice_report_status("[演练] 已有胜场 3/3，无需重复挑战，收工"),
            "✓ 已有胜场够数")
        self.assertIsNone(_practice_report_status(
            "[演练] 收工：本次新赢 2 场，当前刷新场累计 2 场"))

    def test_step_abort_wording_across_flows_is_not_green(self):
        """各流程自己的"中止"话术都得判红。"""
        for msg in (
            "[远征] 找不到远征开始按钮（条件不满足/部队已在远征？），停",
            "[远征] 确认弹窗没出现，停，你去看看卡哪了",
            "[远征] 没找到小图「5-4」（名字写错了？），停",
            "[远征] 未配置远征",
            "[内番] 既没看到内番开始按钮也没看到内番中标记，画面不对劲，停",
            "[内番] 二次确认弹窗没出现，停",
            "[锻刀] 点火失败，停",
            "[锻刀] 刀解腾位置失败，这炉先不收",
            "[合成] 动画等太久没回素材界面，可能成了也可能没成",
            "[TASK] 失效弹窗后没找到一键领取按钮，无法补点",
            "[出阵] 找不到返回本丸按钮，停止点击，等你手动处理",
            "[出阵] 未配置异去",
            "[挖地] 没找到大阪城活动入口",
            "[挖地] 层数没有成功切到目标（现在是 50），已停止出阵",
            "[RAID] 既没确认弹窗也没补充弹窗，卡在未知画面，停",
            "[快照] 等待 90 秒后目录仍不可用，取消本次收工盘点",
            "[远征] 🛑 检测到重伤标记！按规矩绝不派遣，停。去修刀吧",
            "[收菜] ⚠️ 连续认不出画面，停，你去看看卡哪了",
            "[出阵] ⚠️ 行军监控超过安全上限，强制停，你去看看卡哪了",
            "[南瓜] 剪影更新没生效（令牌烧完了，或者有弹窗没驱散掉），收工",
            "[南瓜] 更新完部队选择按钮没回来",
            "[SHOP] 购买按钮没识别到或点了没反应，本次未购买",
        ):
            self.assertTrue(_is_fail(msg), msg)

    def test_benign_skip_and_fallback_wording_stays_green(self):
        """幂等跳过、可自愈的提示不许误伤。"""
        for msg in (
            "[SHOP] 今日暖心礼包已售罄，说明此前已经领取，跳过",
            "[签到] 没有领取奖励按钮（今天签过了？），跳过",
            "[签到] 没直接落在签到页，点签到标签",
            "[收菜] ✓ 没有远征回来，本丸风平浪静～",
            "[远征] 没有启用常用安排，本次只收取归来奖励",
            "[远征] ⚠️ 没看到「远征中」字样，可能派遣失败也可能已回本丸",
            "[演练] 已有胜场 3/3，无需重复挑战，收工",
            "[演练] 打完没回到对手列表，重新导航",
            "[挖地] 🧪 开工小判没读到（不在本丸？），收场再补",
            "[挖地] 98 层，已达到停止条件，本次不出阵",
            "[南瓜] ⚠️ 剪影素材库加载失败，这局当死板版刷",
            "[异去] 归城提灯补充完成；小判金额未识别，已回到部队选择",
            "[快照] 小判读取失败（不影响其他数据）",
            "[日课] ✓ 今天已经刀解过了（锻刀收刀腾位置时顺手解的），这步跳过",
        ):
            self.assertFalse(_is_fail(msg), msg)

    def test_daily_expedition_dispatches_idle_teams_from_common_plan(self):
        flow = DailyMixin()
        flow.collect_expedition_stream = lambda redispatch=None: iter(["collected"])
        dispatched = []
        flow.expedition_stream = lambda **kwargs: (
            dispatched.append(kwargs) or iter(["dispatched"]))
        routes = [{"team_no": 2, "map_code": "B2", "map_name": "测试图",
                   "era": 2, "map_slot": 2}]

        with patch("touken.flows.expedition._load_exp_record", return_value={}):
            messages = list(flow._daily_expedition_step(routes))

        self.assertEqual(dispatched, [{"era": 2, "map_slot": 2, "team_no": 2}])
        self.assertIn("collected", messages)

    def test_daily_expedition_does_not_touch_team_still_away(self):
        flow = DailyMixin()
        flow.collect_expedition_stream = lambda redispatch=None: iter(())
        dispatched = []
        flow.expedition_stream = lambda **kwargs: (
            dispatched.append(kwargs) or iter(()))
        routes = [{"team_no": 2, "map_code": "B2", "map_name": "测试图",
                   "era": 2, "map_slot": 2}]
        future_record = {"2": {
            "dispatched_at": "2099-01-01 00:00:00", "duration_min": 60,
        }}

        with patch("touken.flows.expedition._load_exp_record",
                   return_value=future_record):
            messages = list(flow._daily_expedition_step(routes))

        self.assertFalse(dispatched)
        self.assertTrue(any("仍在外面" in message for message in messages))


class ShopGiftTests(unittest.TestCase):
    def test_claim_button_roi_is_anchored_to_warm_gift_card(self):
        class Maa:
            def __init__(self):
                self.template_rois = []

            def screenshot(self, force=False):
                pass

            def ocr(self, expected, roi, match_mode="contains"):
                if expected == "暖心":
                    return Point(145, 145)
                return None

            def template_match(self, template, roi=None, threshold=0.7):
                self.template_rois.append((template, roi))
                return None

        class Flow(RewardsMixin):
            def __init__(self):
                self.current_location = "万屋"
                self.device = Maa()
                self.config = {"shop": {"free_gift": {
                    "find_text": {"expected": "暖心", "roi": [0, 100, 700, 650]},
                    "claim_button": {"template": "领取.png"},
                    "sold_out": {"template": "售罄.png"},
                }}}

            def navigate_to_stream(self, location):
                return iter(())

        flow = Flow()
        list(flow.claim_free_gift_stream())
        rois = [roi for template, roi in flow.device.template_rois
                if template == "领取.png"]
        self.assertTrue(rois)
        for roi in rois:
            self.assertEqual((roi.x, roi.y, roi.w, roi.h), (350, 265, 330, 95))
            self.assertLessEqual(roi.y + roi.h, 360)

    def test_right_top_warm_gift_keeps_left_roi_and_follows_card(self):
        class Maa:
            def __init__(self):
                self.ocr_rois = []
                self.template_rois = []

            def screenshot(self, force=False):
                pass

            def ocr(self, expected, roi, match_mode="contains"):
                self.ocr_rois.append((expected, roi))
                if expected == "暖心" and roi.x >= 700:
                    return Point(715, 145)
                return None

            def template_match(self, template, roi=None, threshold=0.7):
                self.template_rois.append((template, roi))
                return None

        class Flow(RewardsMixin):
            def __init__(self):
                self.current_location = "万屋"
                self.device = Maa()
                self.config = {"shop": {"free_gift": {
                    "find_text": {
                        "expected": "暖心",
                        "rois": [[0, 100, 700, 650], [700, 100, 1280, 370]],
                    },
                    "claim_button": {"template": "领取.png"},
                }}}

            def navigate_to_stream(self, location):
                return iter(())

        flow = Flow()
        list(flow.claim_free_gift_stream())
        rois = [roi for template, roi in flow.device.template_rois
                if template == "领取.png"]
        self.assertTrue(rois)
        self.assertEqual((rois[0].x, rois[0].y, rois[0].w, rois[0].h),
                         (885, 265, 330, 95))
        self.assertEqual([roi.x for expected, roi in flow.device.ocr_rois
                          if expected == "暖心"], [0, 700])

    class _ShopMaa:
        """走完暖心礼包全程的柜台：认"暖心"、认领取模板、0价标签在买完之前一直在。
        buy_via_ocr=True 时文字兜底能找到"购买"按钮 (500,500)。"""

        def __init__(self, buy_via_ocr=False):
            self.clicks = []
            self.buy_via_ocr = buy_via_ocr
            self.bought = False

        def screenshot(self, force=False):
            pass

        def click(self, point):
            self.clicks.append((point.x, point.y))
            if (point.x, point.y) == (500, 500):
                self.bought = True

        def ocr(self, expected, roi, match_mode="contains"):
            if expected == "暖心":
                return Point(145, 145)
            if expected == "0":
                return None if self.bought else Point(850, 417)
            if expected == "购买" and self.buy_via_ocr:
                return Point(500, 500)
            return None

        def template_match(self, template, roi=None, threshold=0.7):
            if template == "领取.png":
                return Point(400, 300)
            return None

        def exists(self, template):
            return False

    @staticmethod
    def _shop_flow(device):
        class Flow(RewardsMixin):
            def __init__(self):
                self.current_location = "万屋"
                self.device = device
                self.config = {"shop": {"free_gift": {
                    "find_text": {"expected": "暖心", "roi": [0, 100, 700, 650]},
                    "claim_button": {"template": "领取.png"},
                    "popup_verify": {"expected": "0",
                                     "roi": [806, 400, 911, 435]},
                    "popup_buy": {"template": "购买.png",
                                  "roi": [400, 450, 900, 600],
                                  "fallback_target": [638, 502]},
                }}}

            def navigate_to_stream(self, location):
                return iter(())

        return Flow()

    def test_unrecognized_buy_button_never_blind_clicks(self):
        # 购买按钮模板和文字都认不出：绝不盲点固定坐标（2026-09-07 起禁用），
        # 如实报"本次未购买"让成绩单判红
        flow = self._shop_flow(self._ShopMaa(buy_via_ocr=False))
        with patch("touken.flows.rewards.time.sleep"):
            logs = list(flow.claim_free_gift_stream())

        self.assertTrue(any("本次未购买" in m for m in logs))
        self.assertEqual(flow.device.clicks, [(400, 300)])  # 只点过领取
        self.assertNotIn((638, 502), flow.device.clicks)    # 旧盲点坐标不许碰

    def test_ocr_fallback_buy_confirmed_by_price_vanishing(self):
        # 模板认不出购买按钮 → 文字兜底点 (500,500)；0价标签消失才算买成
        flow = self._shop_flow(self._ShopMaa(buy_via_ocr=True))
        with patch("touken.flows.rewards.time.sleep"):
            logs = list(flow.claim_free_gift_stream())

        self.assertIn((500, 500), flow.device.clicks)
        self.assertTrue(any("领取成功" in m for m in logs))


class TaskRewardTests(unittest.TestCase):
    class Maa:
        def __init__(self, active_first=False, inactive=False):
            self.active_first = active_first
            self.inactive = inactive
            self.active_calls = 0
            self.clicked = []

        def screenshot(self, force=False):
            pass

        def template_match(self, template, roi=None, threshold=0.7):
            if template == "一键领取.png":
                self.active_calls += 1
                return Point(1165, 620) if self.active_first and self.active_calls == 1 else None
            if template == "一键领取_灰.png":
                return Point(1165, 620) if self.inactive else None
            return None

        def exists(self, template):
            return False

        def click(self, point):
            self.clicked.append(point)

    def _flow(self, device):
        flow = RewardsMixin()
        flow.device = device
        flow.current_location = "任务"
        flow.config = {"task_reward": {
            "tabs": {"日常": [48, 204]},
            "claim_button": {"template": "一键领取.png"},
        }}
        flow.navigate_to_stream = lambda location: iter(())
        flow._click_point = lambda point: None
        flow.events = []
        flow.record_event = lambda kind, **payload: flow.events.append((kind, payload))
        return flow

    def test_claim_is_counted_only_after_button_turns_gray(self):
        flow = self._flow(self.Maa(active_first=True, inactive=True))
        with patch("touken.flows.rewards.time.sleep"):
            messages = list(flow.claim_task_rewards_stream())

        self.assertIn(("task_rewards.claimed", {"tab": "日常"}), flow.events)
        self.assertTrue(any("已确认按钮变灰" in msg for msg in messages))

    def test_gray_button_records_no_reward_without_counting_claim(self):
        flow = self._flow(self.Maa(inactive=True))
        with patch("touken.flows.rewards.time.sleep"):
            messages = list(flow.claim_task_rewards_stream())

        self.assertEqual(flow.events, [("task_rewards.none", {"tab": "日常"})])
        self.assertTrue(any("已确认没有可领奖励" in msg for msg in messages))

    def test_reward_popup_confirms_claim_when_gray_button_is_late(self):
        flow = self._flow(self.Maa(active_first=True, inactive=False))
        flow._read_reward_popup = lambda: ([("木炭", 1050)], [])
        flow._emit_reward_popup_changes = lambda items, event_id: flow.events.append(
            ("popup.resources", {"items": items}))
        with patch("touken.flows.rewards.time.sleep"):
            messages = list(flow.claim_task_rewards_stream())

        self.assertIn(("task_rewards.claimed", {"tab": "日常"}), flow.events)
        self.assertIn(("popup.resources", {"items": [("木炭", 1050)]}), flow.events)
        self.assertTrue(any("已确认报酬弹窗" in msg for msg in messages))

    def test_late_reward_popup_still_confirms_claim(self):
        """弹窗比首次读取慢（安装版实测月常/活动）：等待窗口里弹出来也算领到。"""
        device = self.Maa(active_first=True, inactive=False)
        device.exists = lambda template: template == "ui完成任务.png"
        flow = self._flow(device)
        reads = []

        def fake_read():
            reads.append(1)
            # 首次读时弹窗还没弹；等待窗口里二次读才有
            return None if len(reads) == 1 else ([("玉钢", 800)], [])

        flow._read_reward_popup = fake_read
        with patch("touken.flows.rewards.time.sleep"):
            messages = list(flow.claim_task_rewards_stream())

        self.assertIn(("task_rewards.claimed", {"tab": "日常"}), flow.events)
        self.assertTrue(any("已确认报酬弹窗" in msg for msg in messages))
        self.assertFalse(any("未确认领取成功" in msg for msg in messages))

    def test_unknown_button_state_is_not_clicked_or_counted(self):
        device = self.Maa()
        flow = self._flow(device)
        with patch("touken.flows.rewards.time.sleep"):
            messages = list(flow.claim_task_rewards_stream())

        self.assertEqual(flow.events, [("task_rewards.unconfirmed", {
            "tab": "日常", "stage": "before_click",
        })])
        self.assertFalse(device.clicked)
        self.assertTrue(any("不点击也不计成绩" in msg for msg in messages))


class OcrMatchingTests(unittest.TestCase):
    def test_empty_ocr_result_never_matches(self):
        self.assertFalse(_ocr_text_matches("", "重伤", "contains"))
        self.assertFalse(_ocr_text_matches("   ", "重伤", "contains"))

    def test_existing_partial_match_behavior_is_preserved(self):
        self.assertTrue(_ocr_text_matches("暖心礼包", "暖心", "contains"))
        self.assertTrue(_ocr_text_matches("万", "万屋", "contains"))


if __name__ == "__main__":
    unittest.main()
