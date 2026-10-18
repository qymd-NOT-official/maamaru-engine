import unittest
from unittest.mock import Mock, patch

from touken.flows.battle import BattleMixin
from touken.flows.sortie import SortieMixin
from touken.flows.osaka import OsakaMixin
from touken.flows.raid import RaidMixin, _ocr_int
from touken.maa_adapter import Point


class FakeMaa:
    def __init__(self, ocr_results=None, templates=None, ocr_tokens=None):
        self.ocr_results = iter(ocr_results or [])
        self.ocr_tokens = iter(ocr_tokens or [])
        self.templates = dict(templates or {})
        self.clicks = []
        self.ocr_calls = []
        self.template_calls = []
        self.template_thresholds = []
        self.screenshot_calls = []

    def screenshot(self, force=False):
        self.screenshot_calls.append(force)
        return None

    def ocr(self, expected, roi):
        self.ocr_calls.append(expected)
        return next(self.ocr_results, False)

    def ocr_all(self, roi):
        return next(self.ocr_tokens, [])

    def template_match(self, template, roi=None, threshold=0.7):
        self.template_calls.append((template, roi))
        self.template_thresholds.append((template, threshold))
        return self.templates.get(template)

    def click(self, point):
        self.clicks.append(point)


class Flow(BattleMixin):
    def __init__(self, maa):
        self.maa = maa
        self.config = {
            "team_select": {
                "teams": {"3": [394, 91]},
            },
        }
        self.points = []

    def _click_point(self, point):
        self.points.append(point)


class FormationPageMaa(FakeMaa):
    """阵形页标题在双击确认后消失：前 title_ttl 次查询返回标题，之后不再返回。"""

    def __init__(self, title_ttl=1, **kwargs):
        super().__init__(**kwargs)
        self.title_ttl = title_ttl

    def template_match(self, template, roi=None, threshold=0.7):
        if template == "battle/ui阵形选择.png":
            if self.title_ttl <= 0:
                self.template_calls.append((template, roi))
                return None
            self.title_ttl -= 1
        return super().template_match(template, roi, threshold)


class LaggyTitleMaa(FormationPageMaa):
    """红色标题横幅晚渲染：前 delay 次标题查询未命中，之后按 title_ttl
    正常命中（阵形页进场动画，江户城 8-28 实测标题比模式按钮晚到）。"""

    def __init__(self, delay=2, **kwargs):
        super().__init__(**kwargs)
        self.lag = delay

    def template_match(self, template, roi=None, threshold=0.7):
        if template == "battle/ui阵形选择.png" and self.lag > 0:
            self.lag -= 1
            self.template_calls.append((template, roi))
            return None
        return super().template_match(template, roi, threshold)


class BattleComponentTests(unittest.TestCase):
    @staticmethod
    def _record_flow(start="team", save_auto_return=True, yes_template=True):
        points = {
            "record": Point(1200, 145), "save": Point(480, 85),
            "load": Point(740, 85), "yes": Point(784, 510),
            "warning": Point(780, 510), "prepare": Point(500, 510),
        }

        class Maa:
            def __init__(self):
                self.state = start
                self.clicked = []

            def screenshot(self, force=False):
                return object()

            def template_match(self, template, roi=None, threshold=0.7):
                if template == "team/继续出阵.png" and self.state == "warning":
                    return points["warning"]
                if template == "team/整备刀装.png" and self.state == "warning":
                    return points["prepare"]
                if template == "team/部队记录.png" and self.state in ("team", "formation"):
                    return points["record"]
                if template == "team/进行记录.png" and self.state == "record":
                    return points["save"]
                if template == "team/使用记录.png" and self.state == "record":
                    return points["load"]
                if (template == "team/是.png" and yes_template
                        and self.state.startswith("confirm")):
                    return points["yes"]
                return None

            def ocr_all(self, roi):
                if self.state == "confirm_save":
                    return [("部队记录", Point(640, 170)),
                            ("是否记录到记录部队一中", Point(640, 340))]
                if self.state == "confirm_load":
                    return [("部队记录", Point(640, 170)),
                            ("是否使用记录部队一", Point(640, 340))]
                return []

            def click(self, point):
                self.clicked.append(point)
                if point == points["prepare"]:
                    self.state = "formation"
                elif point == points["record"]:
                    self.state = "record"
                elif point == points["save"]:
                    self.state = "confirm_save"
                elif point == points["load"]:
                    self.state = "confirm_load"
                elif point == points["yes"]:
                    self.state = ("team" if self.state == "confirm_load"
                                  or save_auto_return else "record")
                elif point == Point(73, 31) and self.state == "record":
                    self.state = "team"
                return True

            def ocr(self, expected, roi, match_mode="contains"):
                return Point(640, 30) if self.state == "team" else None

        class RecordFlow(BattleMixin):
            def __init__(self):
                self.maa = Maa()
                self.config = {"team_select": {"team_record": {
                    "button": {"template": "team/部队记录.png"},
                    "records": {"1": [1225, 179]},
                    "save_confirm": {"template": "team/进行记录.png"},
                    "load_confirm": {"template": "team/使用记录.png"},
                    "yes_button": {"template": "team/是.png"},
                }}}

            def _click_template_config(self, config):
                point = self.maa.template_match(config.get("template"))
                return self.maa.click(point) if point else False

            def _click_point(self, target):
                return self.maa.click(Point(*target))

        return RecordFlow()

    def test_saves_current_team_to_record_one_and_returns_to_team_select(self):
        flow = self._record_flow("team")
        cfg = {"team_ui_ocr": {"expected": "部队选择", "roi": [0, 0, 10, 10]}}
        with patch("touken.flows.battle.time.sleep"):
            self.assertTrue(flow._save_team_record(cfg, record_no=1))
        self.assertEqual(flow.maa.state, "team")
        self.assertIn(Point(1225, 179), flow.maa.clicked)

    def test_warning_restores_record_one_through_formation_screen(self):
        flow = self._record_flow("warning")
        cfg = {
            "equip_warning_button": {"template": "team/继续出阵.png"},
            "team_ui_ocr": {"expected": "部队选择", "roi": [0, 0, 10, 10]},
        }
        with patch("touken.flows.battle.time.sleep"):
            self.assertTrue(flow._restore_equipment_from_warning(cfg, record_no=1))
        self.assertEqual(flow.maa.state, "team")
        self.assertIn(Point(500, 510), flow.maa.clicked)
        self.assertIn(Point(740, 85), flow.maa.clicked)

    def test_save_uses_back_when_record_page_does_not_close_itself(self):
        flow = self._record_flow("team", save_auto_return=False)
        cfg = {"team_ui_ocr": {"expected": "部队选择", "roi": [0, 0, 10, 10]}}
        with patch("touken.flows.battle.time.sleep"):
            self.assertTrue(flow._save_team_record(cfg, record_no=1))
        self.assertEqual(flow.maa.state, "team")
        self.assertIn(Point(73, 31), flow.maa.clicked)

    def test_record_confirmation_uses_guarded_fallback_when_yes_template_changes(self):
        flow = self._record_flow("team", yes_template=False)
        cfg = {"team_ui_ocr": {"expected": "部队选择", "roi": [0, 0, 10, 10]}}
        with patch("touken.flows.battle.time.sleep"):
            self.assertTrue(flow._save_team_record(cfg, record_no=1))
        self.assertEqual(flow.maa.state, "team")
        self.assertIn(Point(784, 510), flow.maa.clicked)

    def test_injury_templates_are_restricted_to_our_team_side(self):
        flow = Flow(FakeMaa(templates={"battle/轻伤.png": Point(200, 150)}))
        cfg = {
            "injury_stamps": {
                "重伤": {"template": "battle/ui重伤.png"},
                "中伤": {"template": "battle/中伤.png"},
                "轻伤": {"template": "battle/轻伤.png"},
            },
            "injury_status_roi": [0, 90, 570, 690],
        }
        self.assertEqual(flow._team_injury_status(cfg), "轻伤")
        self.assertTrue(flow.maa.template_calls)
        for _, roi in flow.maa.template_calls:
            self.assertEqual((roi.x, roi.y, roi.w, roi.h),
                             (220, 90, 120, 600))
        self.assertTrue(all(threshold == 0.88
                            for _, threshold in flow.maa.template_thresholds))

    def test_partial_injury_ocr_never_promotes_hurt_to_heavy(self):
        class Maa(FakeMaa):
            def ocr_all(self, roi):
                return [("伤", Point(290, 153))]

        flow = Flow(Maa())
        self.assertIsNone(flow._team_injury_status({"injury_stamps": {}}))

    def test_medium_stamp_cannot_be_claimed_by_low_score_heavy_template(self):
        class Maa(FakeMaa):
            scores = {
                "battle/ui重伤.png": 0.704,
                "battle/中伤.png": 0.96,
                "battle/轻伤.png": 0.80,
            }

            def template_match(self, template, roi=None, threshold=0.7):
                self.template_thresholds.append((template, threshold))
                return Point(282, 147) if self.scores[template] >= threshold else None

            def ocr_all(self, roi):
                return []

        cfg = {"injury_stamps": {
            "重伤": {"template": "battle/ui重伤.png"},
            "中伤": {"template": "battle/中伤.png"},
            "轻伤": {"template": "battle/轻伤.png"},
        }}
        flow = Flow(Maa())

        self.assertEqual(flow._team_injury_status(cfg), "中伤")
        self.assertEqual(flow.maa.template_thresholds[:2], [
            ("battle/ui重伤.png", 0.88),
            ("battle/中伤.png", 0.88),
        ])

    def test_heavy_march_warning_always_chooses_no_in_scoped_roi(self):
        deny = Point(785, 468)
        flow = Flow(FakeMaa(ocr_tokens=[
            [("重伤状态下进军可能导致刀剑破坏，确认继续进军吗", Point(600, 320))],
            [("否", deny)], [],
        ]))
        with patch("touken.flows.battle.time.sleep"):
            self.assertTrue(flow._deny_heavy_injury_warning({
                "injury_deny_button": {"template": "否.png"},
            }))
        self.assertEqual(flow.maa.clicks, [deny])
        self.assertEqual(flow.maa.template_calls, [])

    def test_full_injury_ocr_keeps_medium_injury_as_medium(self):
        class Maa(FakeMaa):
            def ocr_all(self, roi):
                return [("米中伤", Point(290, 153))]

        flow = Flow(Maa())
        self.assertEqual(
            flow._team_injury_status({"injury_stamps": {}}), "中伤")

    def test_formation_state_uses_the_two_status_templates(self):
        auto = Point(910, 32)
        title = Point(640, 24)
        flow = Flow(FakeMaa(templates={
            "battle/ui阵形选择.png": title,
            "battle/阵形选择自动.png": auto,
        }))
        flow.config["formation"] = {"auto_mode": {}}
        self.assertEqual(flow._formation_mode_state(), "auto")

        manual = Point(910, 32)
        flow = Flow(FakeMaa(templates={
            "battle/ui阵形选择.png": title,
            "battle/阵形选择手动.png": manual,
        }))
        flow.config["formation"] = {"auto_mode": {}}
        self.assertEqual(flow._formation_mode_state(), "manual")

    def test_manual_formation_switches_from_auto_then_selects_fixed(self):
        flow = Flow(FormationPageMaa(title_ttl=3, templates={
            "battle/ui阵形选择.png": Point(640, 24),
            "battle/阵形选择自动.png": Point(910, 32),
        }))
        flow.config["formation"] = {
            "auto_mode": {"toggle": [910, 32]},
            "formations": {"逆行阵": [1034, 420]},
            "double_click": True,
        }
        with patch("touken.flows.battle.time.sleep"):
            self.assertEqual(flow.choose_formation(
                formation_name="逆行阵", enable_auto=False), "fixed")
        # 拨开关→等标题稳定（吃掉 2 次标题查询）→点两下同一张阵形卡：
        # 第一下选中、第二下即确定，不点偏移热点
        self.assertEqual(flow.points, [[910, 32], [1034, 420], [1034, 420]])

    def test_manual_formation_never_shows_title_means_game_auto_picked(self):
        """拨成手动后标题一直不出现 = 游戏已经自动选完开打（江户城实测：
        auto 模式手快时页面不停留）。这时绝不能盲点卡面，如实按"自动"交差。"""
        flow = Flow(FormationPageMaa(title_ttl=1, templates={
            "battle/ui阵形选择.png": Point(640, 24),
            "battle/阵形选择自动.png": Point(910, 32),
        }))
        flow.config["formation"] = {
            "auto_mode": {"toggle": [910, 32]},
            "formations": {"逆行阵": [1034, 420]},
            "double_click": True,
        }
        with patch("touken.flows.battle.time.sleep"):
            self.assertEqual(flow.choose_formation(
                formation_name="逆行阵", enable_auto=False), "auto")
        # 只拨了开关，卡面一下都没点
        self.assertEqual(flow.points, [[910, 32]])

    def test_auto_formation_switches_from_manual_but_finishes_current_page(self):
        """手动→自动只管下一场，当前阵形页仍必须选完（8-30 江户城实测）。"""
        flow = Flow(FormationPageMaa(title_ttl=5, templates={
            "battle/ui阵形选择.png": Point(640, 24),
            "battle/阵形选择手动.png": Point(910, 32),
        }))
        flow.config["formation"] = {
            "auto_mode": {"toggle": [910, 32]},
            "formations": {"逆行阵": [1034, 420]},
            "double_click": True,
        }
        with patch("touken.flows.battle.time.sleep"):
            self.assertEqual(flow.choose_formation(
                formation_name="逆行阵", enable_auto=True), "fixed")
        self.assertEqual(flow.points, [[910, 32], [1034, 420], [1034, 420]])

    def test_formation_name_ocr_keeps_current_page_after_auto_toggle(self):
        """顶部标题闪掉时，“鱼鳞阵”持续存在也不能把选择页当成已开战。"""
        flow = Flow(FormationPageMaa(
            title_ttl=1,
            templates={
                "battle/ui阵形选择.png": Point(640, 24),
                "battle/阵形选择手动.png": Point(910, 32),
            },
            ocr_tokens=[
                [("鱼鳞阵", Point(210, 180))],
                [("鱼鳞阵", Point(210, 180))],
                [], [],
            ],
        ))
        flow.config["formation"] = {
            "auto_mode": {"toggle": [910, 32]},
            "formations": {"鱼鳞阵": [210, 218]},
            "double_click": True,
        }
        with patch("touken.flows.battle.time.sleep"):
            self.assertEqual(flow.choose_formation(
                formation_name="鱼鳞阵", enable_auto=True), "fixed")
        self.assertEqual(flow.points, [[910, 32], [210, 218], [210, 218]])

    def test_auto_toggle_only_hands_off_when_selection_page_really_left(self):
        """若某次切换后选择页确实消失，连续未见标题/阵形名才交给自动。"""
        class ToggleFlow(Flow):
            def _click_point(self, point):
                super()._click_point(point)
                if point == [910, 32]:
                    self.maa.templates.pop("battle/阵形选择手动.png", None)
                    self.maa.templates["battle/阵形选择自动.png"] = Point(910, 32)

        flow = ToggleFlow(FormationPageMaa(title_ttl=1, templates={
            "battle/ui阵形选择.png": Point(640, 24),
            "battle/阵形选择手动.png": Point(910, 32),
        }))
        flow.config["formation"] = {
            "auto_mode": {"toggle": [910, 32]},
            "formations": {"鱼鳞阵": [210, 218]},
            "double_click": True,
        }
        with patch("touken.flows.battle.time.sleep"):
            self.assertEqual(flow.choose_formation(
                formation_name="鱼鳞阵", enable_auto=True), "auto")
        self.assertEqual(flow.points, [[910, 32]])

    def test_auto_formation_stuck_on_selection_page_falls_back_to_fixed(self):
        """索敌失败敌方阵形「不明」：游戏自动阵形没东西可选，页面挂着不动，
        已经是自动模式时脚本必须兜底——没有「有利」标记可认就点兜底阵形
        （6-2 实战卡死修复）。"""
        flow = Flow(FormationPageMaa(title_ttl=2, templates={
            "battle/ui阵形选择.png": Point(640, 24),
            "battle/阵形选择自动.png": Point(910, 32),
        }))
        flow.config["formation"] = {
            "auto_mode": {"toggle": [910, 32]},
            "formations": {"逆行阵": [1034, 420]},
            "double_click": True,
        }
        with patch("touken.flows.battle.time.sleep"):
            self.assertEqual(flow.choose_formation(
                formation_name="逆行阵", enable_auto=True), "fixed")
        # 没有有利标（认不出），双击兜底阵形卡；没有去拨自动/手动开关
        self.assertEqual(flow.points, [[1034, 420], [1034, 420]])

    def test_auto_stuck_with_advantage_marker_picks_advantage(self):
        """自动模式兜底时页面上有「有利」标记：优先点有利，不动兜底阵形。"""
        flow = Flow(FormationPageMaa(title_ttl=2, templates={
            "battle/ui阵形选择.png": Point(640, 24),
            "battle/阵形选择自动.png": Point(910, 32),
            "battle/ui有利.png": Point(500, 300),
        }))
        flow.config["formation"] = {
            "auto_mode": {"toggle": [910, 32]},
            "formations": {"逆行阵": [1034, 420]},
            "double_click": True,
        }
        with patch("touken.flows.battle.time.sleep"):
            self.assertEqual(flow.choose_formation(
                formation_name="逆行阵", enable_auto=True), "advantage")
        # 双击落在有利标记上，兜底阵形卡一下没碰
        self.assertEqual(flow.points, [])
        self.assertEqual(len(flow.maa.clicks), 2)

    def test_formation_state_waits_for_lagging_title(self):
        """进场动画里红色标题比模式按钮晚渲染：刚进页面读不到状态不能秒报
        failed，要等标题落地再判（江户城 8-28 一秒误判中止战斗修复）。"""
        flow = Flow(LaggyTitleMaa(delay=2, title_ttl=2, templates={
            "battle/ui阵形选择.png": Point(640, 24),
            "battle/阵形选择自动.png": Point(910, 32),
        }))
        flow.config["formation"] = {
            "auto_mode": {"toggle": [910, 32]},
            "formations": {"逆行阵": [1034, 420]},
            "double_click": True,
        }
        with patch("touken.flows.battle.time.sleep"):
            self.assertEqual(flow.choose_formation(
                formation_name="逆行阵", enable_auto=True), "fixed")
        # 本来就是自动模式不拨开关；标题落地后无有利标，双击兜底阵形
        self.assertEqual(flow.points, [[1034, 420], [1034, 420]])

    def test_formation_double_click_gives_up_when_page_never_leaves(self):
        """双击三轮阵形页都不走：如实报失败，让调用方停止这场，绝不盲点。"""
        flow = Flow(FakeMaa(templates={
            "battle/ui阵形选择.png": Point(640, 24),
            "battle/阵形选择手动.png": Point(910, 32),
        }))
        flow.config["formation"] = {
            "auto_mode": {"toggle": [910, 32]},
            "formations": {"逆行阵": [1034, 420]},
            "double_click": True,
        }
        with patch("touken.flows.battle.time.sleep"):
            self.assertEqual(flow.choose_formation(
                formation_name="逆行阵", enable_auto=False), "failed")
        # 三轮双击共 6 击，全部落在阵形卡上，没有偏移热点
        self.assertEqual(flow.points, [[1034, 420]] * 6)


    def test_formation_status_alone_during_battle_is_not_a_selection_screen(self):
        flow = Flow(FakeMaa(templates={
            "battle/阵形选择手动.png": Point(910, 32),
        }))
        flow.config["formation"] = {"auto_mode": {}}

        self.assertIsNone(flow._formation_mode_state())
        self.assertEqual(flow.points, [])

    def test_auto_status_without_title_can_only_be_used_to_switch_to_manual(self):
        flow = Flow(FakeMaa(templates={
            "battle/阵形选择自动.png": Point(910, 32),
        }))
        flow.config["formation"] = {"auto_mode": {}}

        self.assertEqual(
            flow._formation_mode_state(allow_auto_without_title=True), "auto")
        self.assertIsNone(flow._formation_mode_state())
        self.assertIn(("battle/阵形选择自动.png", 0.9),
                      flow.maa.template_thresholds)

    def test_wait_for_team_select_can_open_the_panel(self):
        deploy = Point(100, 200)
        maa = FakeMaa(
            ocr_results=[False, False, deploy, deploy, True],
        )
        flow = Flow(maa)
        cfg = {
            "team_ui_ocr": {"expected": "部队选择", "roi": [0, 0, 10, 10]},
            "deploy_button": {"ocr": {
                "expected": ["部队", "选择"], "roi": [1120, 580, 1279, 710]}},
        }

        self.assertTrue(flow._wait_for_team_select(cfg, attempts=3, open_after=1))
        self.assertEqual(maa.clicks, [deploy])

    def test_deploy_button_requires_both_ocr_words_and_never_uses_template(self):
        point = Point(1200, 640)
        maa = FakeMaa(ocr_results=[point, point])
        flow = Flow(maa)

        self.assertEqual(flow._find_deploy_button({
            "deploy_button": {"ocr": {
                "expected": ["部队", "选择"], "roi": [1120, 580, 1279, 710]}}
        }), point)
        self.assertEqual(maa.ocr_calls, ["部队", "选择"])
        self.assertEqual(maa.template_calls, [])
        self.assertTrue(maa.screenshot_calls[0])

        maa = FakeMaa(ocr_results=[point, False])
        self.assertIsNone(flow._find_deploy_button({
            "deploy_button": {"ocr": {
                "expected": ["部队", "选择"], "roi": [1120, 580, 1279, 710]}}
        }))

    def test_team_select_already_open_never_clicks_bottom_right(self):
        maa = FakeMaa(ocr_results=[Point(640, 29)])
        flow = Flow(maa)
        cfg = {
            "team_ui_ocr": {"expected": "部队选择", "roi": [0, 0, 10, 10]},
            "deploy_button": {"ocr": {
                "expected": ["部队", "选择"], "roi": [1120, 580, 1279, 710]}},
        }

        self.assertTrue(flow._wait_for_team_select(cfg, attempts=2, open_after=0))
        self.assertEqual(maa.clicks, [])
        self.assertEqual(maa.ocr_calls, ["部队选择"])

    def test_pick_team_uses_the_shared_team_coordinates(self):
        flow = Flow(FakeMaa())

        self.assertTrue(flow._pick_team(3))
        self.assertEqual(flow.points, [[394, 91], [394, 91]])
        self.assertFalse(flow._pick_team(5))

    def test_depart_requires_the_button_to_be_visible(self):
        depart = Point(1198, 625)
        flow = Flow(FakeMaa(templates={"team/即刻出阵.png": depart}))

        self.assertTrue(flow._click_depart({
            "depart_button": {"template": "team/即刻出阵.png"},
        }))
        self.assertEqual(flow.maa.clicks, [depart])
        self.assertFalse(flow._click_depart({"depart_button": {}}))

    def test_ticket_refill_is_a_shared_three_step_flow(self):
        refill = Point(100, 100)
        recover = Point(200, 200)
        confirm = Point(300, 300)
        flow = Flow(FakeMaa(templates={
            "补充.png": refill,
            "恢复一个.png": recover,
            "确定.png": confirm,
        }))
        original_click = flow.maa.click
        def click_and_close(point):
            original_click(point)
            if point == confirm:
                flow.maa.templates.pop("确定.png", None)
        flow.maa.click = click_and_close
        cfg = {"ticket_recover": {
            "popup_button": {"template": "补充.png"},
            "recover_button": {"template": "恢复一个.png"},
            "confirm_button": {"template": "确定.png"},
        }}
        with patch("touken.flows.battle.time.sleep"):
            messages = list(flow._recover_ticket_stream(cfg, tag="[异去]"))
        self.assertTrue(flow._recover_ok)
        self.assertEqual(flow.maa.clicks, [refill, recover, confirm])
        self.assertEqual(messages, ["[异去] 手形补充完成"])

    class _SliderMaa(FakeMaa):
        """花札滑条式补充页假人：确认按钮要点 confirm_left 次才消失
        （数量页确认 → 小判消耗二次确认，同一张图）。"""

        def __init__(self, confirm_left=2, **kwargs):
            super().__init__(**kwargs)
            self.confirm_left = confirm_left

        def template_match(self, template, roi=None, threshold=0.7):
            if template == "确认.png":
                return Point(300, 300) if self.confirm_left > 0 else None
            return self.templates.get(template)

        def click(self, point):
            self.clicks.append(point)
            if point == Point(300, 300):
                self.confirm_left -= 1

    _SLIDER_CFG = {"ticket_recover": {
        "popup_button": {"template": "补充.png"},
        "confirm_button": {"template": "确认.png"},
        "quantity_ocr": {"roi": [650, 446, 1030, 490]},
    }}

    def test_slider_style_refill_confirms_directly(self):
        """花札滑条式补充页：没有“恢复一个”，补充→确认；数量页确认后
        还有小判消耗二次确认（2026-09-20 真机），同一张确认图见到就点、
        点到消失为止。"""
        refill = Point(100, 100)
        flow = Flow(self._SliderMaa(
            templates={"补充.png": refill},
            ocr_tokens=[[("1", None), ("个", None)]],
        ))
        with patch("touken.flows.battle.time.sleep"):
            messages = list(flow._recover_ticket_stream(
                self._SLIDER_CFG, tag="[花札]"))
        self.assertTrue(flow._recover_ok)
        self.assertEqual(flow.maa.clicks, [refill, Point(300, 300),
                                           Point(300, 300)])
        self.assertEqual(messages, ["[花札] 手形补充完成"])

    def test_slider_refill_stops_when_quantity_is_not_one(self):
        refill = Point(100, 100)
        flow = Flow(self._SliderMaa(
            templates={"补充.png": refill},
            ocr_tokens=[[("5", None), ("个", None)]],
        ))
        with patch("touken.flows.battle.time.sleep"):
            messages = list(flow._recover_ticket_stream(
                self._SLIDER_CFG, tag="[花札]"))
        self.assertFalse(flow._recover_ok)
        self.assertEqual(flow.maa.clicks, [refill])
        self.assertTrue(any("不是 1" in m for m in messages))

    def test_slider_refill_stops_before_payment_when_quantity_unreadable(self):
        refill = Point(100, 100)
        flow = Flow(self._SliderMaa(
            templates={"补充.png": refill},
            ocr_tokens=[[]],
        ))
        with patch("touken.flows.battle.time.sleep"):
            list(flow._recover_ticket_stream(self._SLIDER_CFG, tag="[花札]"))
        self.assertFalse(flow._recover_ok)
        self.assertEqual(flow.maa.clicks, [refill])

    def test_slider_refill_stops_looping_when_confirm_never_leaves(self):
        refill = Point(100, 100)
        flow = Flow(self._SliderMaa(
            confirm_left=99,
            templates={"补充.png": refill},
            ocr_tokens=[[("1", None)]],
        ))
        with patch("touken.flows.battle.time.sleep"):
            messages = list(flow._recover_ticket_stream(
                self._SLIDER_CFG, tag="[花札]"))
        self.assertFalse(flow._recover_ok)
        self.assertEqual(flow.maa.clicks, [refill] + [Point(300, 300)] * 3)
        self.assertTrue(any("防死循环" in m for m in messages))


class YosariRouteTests(unittest.TestCase):
    @staticmethod
    def _drain(generator):
        messages = []
        while True:
            try:
                messages.append(next(generator))
            except StopIteration as done:
                return messages, done.value

    def test_yosari_reuses_the_map_sortie_pipeline(self):
        class Route(SortieMixin, BattleMixin):
            def _map_sortie_stream(self, **kwargs):
                self.route = kwargs
                yield "route"

        flow = Route()
        self.assertEqual(list(flow.yosari_stream(map_no=4, team_no=2)), ["route"])
        self.assertEqual(flow.route["map_type"], "异去")
        self.assertEqual(flow.route["cfg_key"], "yosari")
        self.assertEqual(flow.route["chapter"], 1)
        self.assertEqual(flow.route["map_no"], 4)
        self.assertEqual(flow.route["team_no"], 2)

    def test_round_ends_only_when_title_and_team_button_are_both_visible(self):
        class Route(SortieMixin, BattleMixin):
            pass

        cfg = {
            "entry": {"expected": "归城提灯", "verify_roi": [0, 0, 10, 10]},
            "deploy_button": {"ocr": {
                "expected": ["部队", "选择"], "roi": [1120, 580, 1279, 710]}},
        }
        flow = Route()
        flow.maa = FakeMaa(
            ocr_results=[True, Point(1200, 640), Point(1200, 640)],
        )
        self.assertTrue(flow._yosari_round_done(cfg))

        flow.maa = FakeMaa(ocr_results=[True, Point(1200, 640), False])
        self.assertFalse(flow._yosari_round_done(cfg))

    def test_manual_march_uses_the_button_template_not_partial_ocr(self):
        class Route(SortieMixin, BattleMixin):
            pass

        button = Point(1146, 617)
        flow = Route()
        flow.maa = FakeMaa(templates={"battle/行军.png": button})
        self.assertEqual(flow._find_march_continue({}), button)

    def test_yosari_new_refill_flow_confirms_four_dialogs(self):
        class Maa(FakeMaa):
            def __init__(self):
                super().__init__(ocr_results=[True, True, True, True])
                self.balance_reads = iter([
                    [("788", Point(520, 380)), ("956 枚", Point(570, 380))],
                    [("788,456 枚", Point(750, 380))],
                ])

            def ocr_all(self, roi):
                return next(self.balance_reads, [])

        class Route(SortieMixin, BattleMixin):
            def __init__(self):
                self.maa = Maa()
                self.points = []
                self.events = []

            def _click_point(self, point):
                self.points.append(point)

            def record_event(self, event_type, **payload):
                self.events.append((event_type, payload))

        flow = Route()
        with patch("touken.flows.sortie.time.sleep"):
            messages, result = self._drain(flow._confirm_yosari_departure(
                {"departure_confirm": {}}, auto_refill=True))
        self.assertEqual(result, "refilled")
        self.assertEqual(flow.points, [[640, 603], [638, 611], [784, 604], [638, 511]])
        self.assertIn("[异去] 归城提灯补充完成，小判 -500，已回到部队选择", messages)
        resource_event = next(payload for name, payload in flow.events
                              if name == "resource.change")
        self.assertEqual(resource_event["delta"], -500)
        self.assertEqual(resource_event["before"], 788956)
        self.assertEqual(resource_event["after"], 788456)
        self.assertEqual(resource_event["attribution"], "confirmed")

    def test_yosari_refill_records_fact_without_guessing_unreadable_koban(self):
        class Route(SortieMixin, BattleMixin):
            def __init__(self):
                self.maa = FakeMaa(ocr_results=[True, True, True, True])
                self.points = []
                self.events = []

            def _click_point(self, point):
                self.points.append(point)

            def record_event(self, event_type, **payload):
                self.events.append((event_type, payload))

        flow = Route()
        with patch("touken.flows.sortie.time.sleep"):
            messages, result = self._drain(flow._confirm_yosari_departure(
                {"departure_confirm": {}}, auto_refill=True))
        self.assertEqual(result, "refilled")
        self.assertFalse(any(name == "resource.change" for name, _ in flow.events))
        self.assertTrue(any(name == "yosari.ticket_refilled" for name, _ in flow.events))
        self.assertIn("[异去] 归城提灯补充完成；小判金额未识别，已回到部队选择", messages)

    def test_yosari_refill_off_closes_before_spending_koban(self):
        class Route(SortieMixin, BattleMixin):
            def __init__(self):
                self.maa = FakeMaa(ocr_results=[True, True])
                self.points = []

            def _click_point(self, point):
                self.points.append(point)

        flow = Route()
        with patch("touken.flows.sortie.time.sleep"):
            messages, result = self._drain(flow._confirm_yosari_departure(
                {"departure_confirm": {}}, auto_refill=False))
        self.assertFalse(result)
        self.assertEqual(flow.points, [[640, 603], [1040, 48]])
        self.assertIn("[异去] 归城提灯不足；自动补充已关闭，不消耗小判，收工", messages)


class OsakaRouteTests(unittest.TestCase):
    def test_floor_reader_uses_the_full_activity_title(self):
        class Maa(FakeMaa):
            def ocr_all(self, roi):
                return [("大阪城地下", Point(150, 190)),
                        ("81层", Point(300, 190))]

        flow = OsakaMixin()
        flow.maa = Maa()
        self.assertEqual(flow._read_osaka_floor({}), 81)

    def test_floor_selector_rereads_after_every_arrow_click(self):
        class Route(OsakaMixin):
            def __init__(self):
                self.maa = FakeMaa()
                self.readings = iter([99, 89, 88, 87, 86, 85, 84, 83, 82, 81])
                self.points = []

            def _read_osaka_floor(self, cfg):
                return next(self.readings)

            def _click_point(self, point):
                self.points.append(point)

        flow = Route()
        with patch("touken.flows.osaka.time.sleep"):
            self.assertEqual(flow._select_osaka_floor({}, 81), 81)
        self.assertEqual(flow.points[0], [1014, 420])
        self.assertEqual(flow.points[1:], [[1084, 420]] * 8)

    def test_floor_selector_stops_when_a_grey_arrow_does_not_change_floor(self):
        class Route(OsakaMixin):
            def __init__(self):
                self.maa = FakeMaa()
                self.readings = iter([29, 29])
                self.points = []

            def _read_osaka_floor(self, cfg):
                return next(self.readings)

            def _click_point(self, point):
                self.points.append(point)

        flow = Route()
        with patch("touken.flows.osaka.time.sleep"):
            self.assertEqual(flow._select_osaka_floor({}, 39), 29)
        self.assertEqual(flow.points, [[1014, 284]])

    def test_osaka_injury_thresholds_match_the_panel_choices(self):
        self.assertFalse(BattleMixin._injury_reaches_threshold("轻伤", "medium"))
        self.assertTrue(BattleMixin._injury_reaches_threshold("中伤", "medium"))
        self.assertFalse(BattleMixin._injury_reaches_threshold("中伤", "heavy"))
        self.assertTrue(BattleMixin._injury_reaches_threshold("重伤", "heavy"))

    def test_floor_end_uses_stable_label_and_result_witness(self):
        flow = OsakaMixin()
        flow.maa = FakeMaa(ocr_results=[True, True])

        self.assertTrue(flow._osaka_floor_done({}))
        self.assertEqual(flow.maa.ocr_calls, ["当前层数", "传送凭证"])

    def test_route_buttons_never_participate_in_floor_end_detection(self):
        flow = OsakaMixin()
        flow.maa = FakeMaa(
            ocr_results=[False],
            templates={
                "battle/行军.png": Point(1130, 610),
                "battle/返回本丸.png": Point(1200, 440),
                "team/部队恢复.png": Point(1040, 440),
            },
        )

        self.assertFalse(flow._osaka_floor_done({}))
        self.assertEqual(flow.maa.ocr_calls, ["当前层数"])

        flow.maa = FakeMaa(ocr_results=[True, False])
        self.assertFalse(flow._osaka_floor_done({}))

    def test_floor_end_does_not_depend_on_the_changing_floor_number(self):
        flow = OsakaMixin()
        flow.maa = FakeMaa(ocr_results=[False])

        self.assertFalse(flow._osaka_floor_done({
            "floor_end_ocr": {"expected": "当前层数", "roi": [0, 0, 10, 10]},
        }))

    def test_osaka_march_is_limited_to_the_bottom_right_button(self):
        button = Point(1130, 610)
        flow = OsakaMixin()
        flow.maa = FakeMaa(templates={"battle/行军.png": button})

        self.assertEqual(flow._find_osaka_march({}), button)

    def test_floor_end_waits_for_a_late_march_button(self):
        button = Point(1130, 610)

        class Maa(FakeMaa):
            def __init__(self):
                super().__init__()
                self.results = iter([None, None, button])

            def template_match(self, template, roi=None, threshold=0.7):
                return next(self.results)

        flow = OsakaMixin()
        flow.maa = Maa()
        with patch("touken.flows.osaka.time.sleep"):
            self.assertEqual(flow._wait_for_osaka_march({}, attempts=4), button)


class _SafeDepartHost(BattleMixin):
    """通用安全出阵链的最小宿主：组件全部钉死，只看编排。"""

    def __init__(self, injury=None, deny=False, cancel=None, maa=None):
        from types import SimpleNamespace
        self.maa = maa or SimpleNamespace(screenshot=lambda force=False: None)
        self.config = {"team_select": {"teams": {"3": [394, 91]}},
                       "injury_check": {"use_youzu_log": False}}
        self._injury = injury
        self._deny = deny
        self._cancel = cancel
        self.saved_record = False
        self.restored = False
        self.events = []
        self.steps = []

    def record_event(self, event_type, **payload):
        self.events.append((event_type, payload))
        return len(self.events)

    def record_resource_change(self, resource, delta, **payload):
        return self.record_event(
            "resource.change", resource=resource, delta=delta,
            attribution="confirmed", **payload)

    def _pick_team(self, team_no):
        self.steps.append("pick")
        return True

    def _team_injury_status(self, cfg):
        self.steps.append("injury")
        return self._injury

    def _rotate_captain_here(self, margin=10, team_no=None):
        self.steps.append(f"rotate:{margin}:team{team_no}")
        yield "[换队长] done"

    def _click_depart(self, cfg):
        return True

    def _cancel_equip_warning(self, cfg):
        return self._cancel

    def _deny_heavy_injury_warning(self, cfg):
        return self._deny

    def _save_team_record(self, cfg, record_no=1):
        self.saved_record = True
        return True

    def _restore_equipment_from_warning(self, cfg, record_no=1):
        self.restored = True
        return None  # 恢复后不再有空缺警告


def _drain_chain(flow, cfg=None, **kwargs):
    gen = flow._safe_depart_stream(cfg or {}, 3, "[测试]", **kwargs)
    msgs = []
    while True:
        try:
            msgs.append(next(gen))
        except StopIteration as stop:
            return msgs, stop.value


class _RefillMaa:
    """票尽补票弹窗：确定/取消被点击后才消失；popup_sticks 时确定也点不掉。"""

    CONFIRM = Point(700, 500)
    CANCEL = Point(500, 500)

    def __init__(self, popup_sticks=False):
        self.popup = True
        self.popup_sticks = popup_sticks
        self.clicked = []

    def screenshot(self, force=False):
        return None

    def template_match(self, template, roi=None, threshold=0.7):
        if not self.popup:
            return None
        if template == "江户城/补票弹窗.png":
            return Point(640, 300)
        if template == "通用_确定.png":
            return self.CONFIRM
        if template == "通用_取消.png":
            return self.CANCEL
        return None

    def click(self, point):
        self.clicked.append(point)
        if point == self.CANCEL:
            self.popup = False
        elif point == self.CONFIRM and not self.popup_sticks:
            self.popup = False


class _RecoverMaa:
    """联队战式两层补充：不足弹窗→恢复一个页→补充完成。"""

    REFILL = Point(490, 470)
    CLOSE = Point(780, 470)
    RECOVER = Point(200, 200)
    CONFIRM = Point(300, 300)

    def __init__(self):
        self.state = "popup"
        self.clicked = []

    def screenshot(self, force=False):
        return None

    def template_match(self, template, roi=None, threshold=0.7):
        if self.state == "popup" and template == "team/补充.png":
            return self.REFILL
        if self.state == "popup" and template == "team/关闭.png":
            return self.CLOSE
        if self.state == "recover" and template == "team/恢复一个.png":
            return self.RECOVER
        if self.state == "recover" and template == "通用_确定.png":
            return self.CONFIRM
        if self.state == "confirm" and template == "team/ui出阵确认.png":
            return Point(640, 72)
        # 复现 8-29 真机：确认页绿色“确定”以 0.706 擦线命中“补充”。
        if self.state == "confirm" and template == "team/补充.png":
            return Point(639, 603) if threshold <= 0.706 else None
        return None

    def click(self, point):
        self.clicked.append(point)
        if point == self.REFILL:
            self.state = "recover"
        elif point in (self.CLOSE, self.CONFIRM):
            self.state = "done"


_REFILL_CFG = {"ticket_refill": {
    "popup": {"template": "江户城/补票弹窗.png"},
    "confirm_button": {"template": "通用_确定.png"},
    "cancel_button": {"template": "通用_取消.png"},
}}

_RECOVER_CFG = {"ticket_recover": {
    "popup_button": {"template": "team/补充.png", "threshold": 0.9},
    "close_button": {"template": "team/关闭.png"},
    "recover_button": {"template": "team/恢复一个.png"},
    "confirm_button": {"template": "通用_确定.png"},
}}


class _RecoverDepartHost(_SafeDepartHost):
    def __init__(self, maa):
        super().__init__(maa=maa)
        self.departs = 0

    def _click_depart(self, cfg):
        self.departs += 1
        if self.departs >= 2:
            self.maa.state = "confirm"
        return True


class SafeDepartChainTests(unittest.TestCase):
    """选队→伤势→刀装→重伤拦截的编排语义：各玩法只准调用，不准手搓。"""

    def test_unconfirmed_rotation_never_saves_record_or_departs(self):
        host = _SafeDepartHost()
        host._click_depart = Mock()
        host._save_team_record = Mock()

        def rotate(*args, **kwargs):
            yield "队长编号未确认"
            return False

        host._rotate_captain_here = rotate
        _, result = _drain_chain(host, rotate_captain=True, auto_equip=True)
        self.assertFalse(result[0])
        host._save_team_record.assert_not_called()
        host._click_depart.assert_not_called()

    def test_preparation_failure_never_clicks_depart(self):
        host = _SafeDepartHost()
        host._click_depart = Mock()

        def prepare():
            yield "准备未确认"
            return False

        _, result = _drain_chain(host, prepare_team_stream=prepare)
        self.assertFalse(result[0])
        host._click_depart.assert_not_called()

    def test_blocked_injury_is_distinct_from_other_failures_and_cleared(self):
        state = {"blocked_injury": "重伤"}
        host = _SafeDepartHost(injury="中伤")
        _, result = _drain_chain(host, departure_state=state)
        self.assertFalse(result[0])
        self.assertEqual(state, {"blocked_injury": "中伤"})
        host._injury = None
        host._click_depart = Mock(return_value=False)
        _, result = _drain_chain(host, departure_state=state)
        self.assertFalse(result[0])
        self.assertEqual(state, {})

    def test_equipment_restore_refreshes_injury_and_rechecks_preparation(self):
        host = _SafeDepartHost()
        host._click_depart = Mock(return_value=True)
        host._restore_equipment_from_warning = Mock(side_effect=[True, None])
        reads = []

        def injury(cfg, team, cache):
            reads.append(dict(cache))
            cache["events"] = "old members"
            return None, None

        def prepare():
            yield "重新确认行军"
            return True

        host._combined_injury_status = injury
        with patch("touken.flows.battle.time.sleep"):
            _, result = _drain_chain(host, auto_equip=True,
                                    after_equipment_stream=prepare)
        self.assertTrue(result[0])
        self.assertEqual(reads, [{}, {}])
        self.assertEqual(host._click_depart.call_count, 2)

    def test_equipment_preparation_failure_never_repeats_depart(self):
        host = _SafeDepartHost()
        host._click_depart = Mock(return_value=True)
        host._restore_equipment_from_warning = Mock(return_value=True)

        def prepare():
            yield "行军设置无法确认"
            return False

        _, result = _drain_chain(host, auto_equip=True,
                                after_equipment_stream=prepare)
        self.assertFalse(result[0])
        host._click_depart.assert_called_once()

    def test_second_confirmation_uses_its_configured_threshold(self):
        maa = FakeMaa(templates={"海联确认": Point(640, 179),
                                 "海联确定": Point(648, 610)})
        host = _SafeDepartHost(maa=maa)
        cfg = {"confirm_ui": {"template": "海联确认"},
               "confirm_button": {"template": "海联确定",
                                  "roi": [500, 560, 800, 660],
                                  "threshold": 0.8}}
        with patch("touken.flows.battle.time.sleep"):
            self.assertTrue(host._confirm_departure(cfg))
        self.assertIn(("海联确定", 0.8), maa.template_thresholds)

    def test_hailian_departure_title_accepts_special_popup_button(self):
        """特别合战场正文不同，但顶部标题和确定按钮与普通海联相同。"""
        maa = Mock()
        maa.ocr.return_value = Point(640, 79)
        maa.template_match.return_value = Point(648, 610)
        host = _SafeDepartHost(maa=maa)
        cfg = {"confirm_ui": {"ocr": {"expected": "出阵",
                                       "roi": [510, 48, 770, 112],
                                       "match_mode": "exact"}},
               "confirm_button": {"template": "lulian/ui海联确定.png",
                                  "roi": [500, 560, 800, 660],
                                  "threshold": 0.8}}
        with patch("touken.flows.battle.time.sleep"):
            self.assertTrue(host._confirm_departure(cfg))
        maa.ocr.assert_called_with("出阵", unittest.mock.ANY,
                                   match_mode="exact")
        maa.click.assert_called_once_with(Point(648, 610))

    def test_departure_title_alone_never_clicks(self):
        maa = Mock()
        maa.ocr.return_value = Point(640, 79)
        maa.template_match.return_value = None
        host = _SafeDepartHost(maa=maa)
        cfg = {"confirm_ui": {"ocr": {"expected": "出阵",
                                       "roi": [510, 48, 770, 112]}},
               "confirm_button": {"template": "lulian/ui海联确定.png"}}
        with patch("touken.flows.battle.time.sleep"):
            self.assertFalse(host._confirm_departure(cfg))
        maa.click.assert_not_called()

    def test_activity_preparation_runs_after_injury_check(self):
        host = _SafeDepartHost()

        def prepare():
            host.steps.append("prepare")
            yield "委托尝试"

        msgs, result = _drain_chain(
            host, prepare_team_stream=prepare)
        self.assertEqual(result, (True, False))
        self.assertIn("委托尝试", msgs)
        self.assertLess(host.steps.index("injury"), host.steps.index("prepare"))

    def test_pre_depart_injury_over_threshold_stops(self):
        msgs, result = _drain_chain(
            _SafeDepartHost(injury="重伤"), repair_threshold="heavy")
        self.assertEqual(result, (False, False))
        self.assertTrue(any("已达到停止条件" in m for m in msgs), msgs)

    def test_injury_below_threshold_passes(self):
        """虚拟伤害活动中伤照跑：threshold=heavy 时中伤不拦。"""
        msgs, result = _drain_chain(
            _SafeDepartHost(injury="中伤"), repair_threshold="heavy")
        self.assertEqual(result, (True, False))

    def test_heavy_injury_popup_is_denied_and_stops(self):
        msgs, result = _drain_chain(_SafeDepartHost(deny=True))
        self.assertEqual(result, (False, False))
        self.assertTrue(any("绝不出阵" in m for m in msgs), msgs)

    def test_equip_warning_cancel_without_auto_equip_stops(self):
        msgs, result = _drain_chain(
            _SafeDepartHost(cancel=True), auto_equip=False)
        self.assertEqual(result, (False, False))
        self.assertTrue(any("刀装未满" in m for m in msgs), msgs)

    def test_auto_equip_saves_record_once_and_restores(self):
        host = _SafeDepartHost()
        msgs, result = _drain_chain(host, auto_equip=True)
        self.assertEqual(result, (True, True))
        self.assertTrue(host.saved_record)
        self.assertTrue(host.restored)

    def test_clean_pass_returns_ok(self):
        msgs, result = _drain_chain(_SafeDepartHost())
        self.assertEqual(result, (True, False))

    def test_captain_rotation_runs_after_team_pick_before_injury_check(self):
        host = _SafeDepartHost()
        msgs, result = _drain_chain(
            host, rotate_captain=True, rotate_captain_margin=20)
        self.assertEqual(result, (True, False))
        self.assertEqual(host.steps[:3], ["pick", "rotate:20:team3", "injury"])
        self.assertIn("[换队长] done", msgs)

    def test_refill_popup_declined_cancels_and_stops(self):
        """票尽弹窗 + 不补票：点取消收工，不碰确定。"""
        maa = _RefillMaa()
        msgs, result = _drain_chain(
            _SafeDepartHost(maa=maa), _REFILL_CFG, auto_refill=False)
        self.assertEqual(result, (False, False))
        self.assertIn(_RefillMaa.CANCEL, maa.clicked)
        self.assertNotIn(_RefillMaa.CONFIRM, maa.clicked)
        self.assertTrue(any("不补票" in m for m in msgs), msgs)

    def test_refill_popup_confirmed_then_redeparts(self):
        """票尽弹窗 + 自动补票：点确定补一张，重新出阵后正常通过。"""
        maa = _RefillMaa()
        msgs, result = _drain_chain(
            _SafeDepartHost(maa=maa), _REFILL_CFG, auto_refill=True)
        self.assertEqual(result, (True, False))
        self.assertIn(_RefillMaa.CONFIRM, maa.clicked)
        self.assertTrue(any("补上一张" in m for m in msgs), msgs)

    def test_refill_popup_sticking_stops_before_double_spend(self):
        """补完票弹窗还在（没补上）：停手，绝不重复点小判。"""
        maa = _RefillMaa(popup_sticks=True)
        msgs, result = _drain_chain(
            _SafeDepartHost(maa=maa), _REFILL_CFG, auto_refill=True)
        self.assertEqual(result, (False, False))
        self.assertEqual(maa.clicked.count(_RefillMaa.CONFIRM), 1)
        self.assertTrue(any("防止重复消费" in m for m in msgs), msgs)

    def test_raid_style_recover_is_reused_by_safe_depart_chain(self):
        maa = _RecoverMaa()
        cfg = {**_RECOVER_CFG, "ticket_price": 300}
        host = _SafeDepartHost(maa=maa)
        msgs, result = _drain_chain(
            host, cfg, auto_refill=True)
        self.assertEqual(result, (True, False))
        self.assertEqual(maa.clicked, [
            _RecoverMaa.REFILL, _RecoverMaa.RECOVER, _RecoverMaa.CONFIRM])
        self.assertTrue(any("手形补充完成" in m for m in msgs), msgs)
        self.assertIn(("ticket.refilled", {
            "source": "测试", "resource": "小判", "delta": -300,
            "ticket_price": 300,
        }), host.events)
        self.assertIn(("resource.change", {
            "resource": "小判", "delta": -300,
            "attribution": "confirmed", "source": "ticket.refilled",
            "evidence": "confirmed_refill_flow", "note": "测试补手形",
            "source_event_id": 1,
        }), host.events)

    def test_unknown_ticket_price_keeps_fact_without_guessing_resource_change(self):
        host = _SafeDepartHost()

        host._record_ticket_refill({}, "[RAID]")

        self.assertEqual(host.events, [("ticket.refilled", {"source": "RAID"})])

    def test_raid_style_recover_declined_uses_close(self):
        maa = _RecoverMaa()
        msgs, result = _drain_chain(
            _SafeDepartHost(maa=maa), _RECOVER_CFG, auto_refill=False)
        self.assertEqual(result, (False, False))
        self.assertEqual(maa.clicked, [_RecoverMaa.CLOSE])
        self.assertTrue(any("不补票" in m for m in msgs), msgs)

    def test_depart_confirm_wins_over_false_refill_match(self):
        maa = _RecoverMaa()
        cfg = {
            **_RECOVER_CFG,
            "confirm_ui": {"template": "team/ui出阵确认.png"},
        }
        host = _RecoverDepartHost(maa)
        msgs, result = _drain_chain(host, cfg, auto_refill=True)
        self.assertEqual(result, (True, False))
        self.assertEqual(host.departs, 2)
        self.assertFalse(any("防止重复消费" in m for m in msgs), msgs)


class _RaidLoopTime:
    """战斗循环的假钟：time() 每次前进 30s（秒过 20s 冷静期），sleep 空转。"""

    def __init__(self):
        self.now = 0.0

    def time(self):
        self.now += 30.0
        return self.now

    def sleep(self, _seconds):
        pass


class _RaidLoopHost(RaidMixin):
    def __init__(self, maa, raid_cfg):
        self.maa = maa
        self.config = {"raid": raid_cfg}

    def _click_point(self, _point):
        pass

    def quick_peek(self, tag=""):
        pass

    def recover_network_stream(self):
        if False:
            yield
        return False


def _raid_loop_cfg(extra=None):
    cfg = {
        "round_end": {
            "template": "lulian/ui陆联.png",
            "roi": [320, 55, 640, 135],
            "threshold": 0.9,
        },
        "battle_ocr": {"expected": "战斗", "roi": [1034, 586, 1154, 646]},
        "skip_tap": [775, 695],
    }
    cfg.update(extra or {})
    return cfg


class RaidDepartureSafetyTests(unittest.TestCase):
    def test_missing_team_screen_stops_instead_of_trying_next_round(self):
        agent = Mock()
        agent.config = {
            "raid": {"ui_title": {"template": "raid-title"},
                     "activity_entry": {"template": "entry"},
                     "difficulty_target": [1118, 328]},
            "team_select": {"teams": {"3": {}}}}
        agent.current_location = "出阵"
        agent.navigate_to_stream.return_value = iter(())
        agent._expedition_takeover_requested.return_value = False
        agent.maa.template_match.side_effect = (
            lambda template, *a, **kw: Point(500, 100)
            if template == "raid-title" else None)
        agent._find_deploy_button.return_value = Point(1150, 620)
        agent._wait_for_team_select.return_value = False

        with patch("touken.flows.raid.time.sleep"):
            messages = list(RaidMixin.raid_stream(
                agent, max_rounds=3, team_no=3, auto_march=False))
        agent._save_raid_failure_frame.assert_called_once_with(
            "team_select_missing")
        self.assertEqual(agent._find_deploy_button.call_count, 1)
        self.assertFalse(any("全部圈数跑完" in msg for msg in messages))

    def test_lulian_keeps_triple_click_and_skips_hailian_march(self):
        agent = Mock()
        agent.config = {
            "raid": {
                "ui_title": {"template": "raid-title"},
                "activity_entry": {"template": "entry"},
                "difficulty_target": [1118, 328],
                "auto_march": {"button": [1203, 407]},
                "fish_basket3": {"click": [345, 487]},
                "triple": {"click": [326, 308],
                           "check_template": "old-triple-selected",
                           "check_roi": [309, 295, 345, 332]},
            },
            "team_select": {"teams": {"3": {}}},
        }
        agent.current_location = "出阵"
        agent.navigate_to_stream.return_value = iter(())
        agent._expedition_takeover_requested.return_value = False
        agent.maa.template_match.side_effect = (
            lambda template, *a, **kw: Point(500, 100)
            if template == "raid-title" else None)
        agent._find_deploy_button.return_value = Point(1150, 620)
        agent._wait_for_team_select.return_value = True

        def ready(*_args, **_kwargs):
            if False:
                yield
            return True, False

        agent._safe_depart_stream.side_effect = ready
        agent._confirm_departure.return_value = True
        agent.battle_loop_stream.return_value = iter(())
        agent._battle_loop_result = (True, 1)
        agent._read_shells_total.return_value = None
        with patch("touken.flows.raid.time.sleep"):
            list(RaidMixin.raid_stream(agent, max_rounds=1, team_no=3))
        self.assertIsNone(
            agent._safe_depart_stream.call_args.kwargs["prepare_team_stream"])
        agent._raid_auto_march_stream.assert_not_called()
        agent._click_point.assert_any_call([326, 308])
        agent._use_fish_basket3_stream.assert_not_called()
        recorded = agent.record_event.call_args
        self.assertEqual(recorded.args[0], "raid.round_completed")
        self.assertEqual(recorded.kwargs["battle_taps"], 1)
        self.assertNotIn("battles", recorded.kwargs)

    def test_zero_fish_baskets_are_never_selected(self):
        maa = FakeMaa(ocr_tokens=[[("0", Point(600, 540))]])
        host = _RaidLoopHost(maa, {})
        messages = list(host._use_fish_basket3_stream({
            "count_ocr": {"roi": [515, 515, 665, 560]},
            "check_roi": [325, 465, 365, 507],
            "check_template": "lulian/ui勾选.png",
            "click": [345, 487],
        }))
        self.assertTrue(any("持有 0" in msg for msg in messages))
        self.assertEqual(maa.clicks, [])

    def test_ocr_count_rejects_multiple_numbers(self):
        maa = Mock()
        maa.ocr_all.return_value = [("3,000/300,000", Point(10, 10))]
        self.assertIsNone(_ocr_int(maa, [0, 0, 100, 100]))
        maa.ocr_all.return_value = [("3,000", Point(10, 10))]
        self.assertEqual(_ocr_int(maa, [0, 0, 100, 100]), 3000)

    def test_raid_stops_when_shared_departure_gate_rejects(self):
        agent = Mock()
        agent.config = {
            "raid": {"ui_title": {"template": "raid-title"},
                     "activity_entry": {"template": "entry"},
                     "difficulty_target": [1118, 328]},
            "team_select": {"teams": {"3": {}}}}
        agent.current_location = "出阵"
        agent.navigate_to_stream.return_value = iter(())
        agent._expedition_takeover_requested.return_value = False
        agent.maa.template_match.side_effect = (
            lambda template, *a, **kw: Point(500, 100)
            if template == "raid-title" else None)
        agent._find_deploy_button.return_value = Point(1150, 620)
        agent._wait_for_team_select.return_value = True

        def blocked(*_args, **_kwargs):
            yield "伤势门闩停"
            return False, False

        agent._safe_depart_stream.side_effect = blocked
        with patch("touken.flows.raid.time.sleep"):
            messages = list(RaidMixin.raid_stream(
                agent, max_rounds=1, team_no=3, auto_march=False))
        self.assertIn("伤势门闩停", messages)
        agent._safe_depart_stream.assert_called_once()
        agent._confirm_departure.assert_not_called()
        agent._click_depart.assert_not_called()


class RaidRoundEndVariantTests(unittest.TestCase):
    """一圈结束判定要同时认陆联/海联横幅（2026-09-24 海联接线）。"""

    def _run(self, maa, cfg):
        host = _RaidLoopHost(maa, cfg)
        with patch("touken.flows.raid.time", _RaidLoopTime()):
            list(host.battle_loop_stream(need_battle=False))
        return host._battle_loop_result

    def test_round_ends_on_hailian_banner(self):
        maa = FakeMaa(templates={"lulian/ui海联.png": (582, 94)})
        cfg = _raid_loop_cfg({
            "round_end_hailian": {
                "template": "lulian/ui海联.png",
                "roi": [320, 55, 880, 140],
                "threshold": 0.9,
            },
        })
        self.assertEqual(self._run(maa, cfg), (True, 0))
        hailian_calls = [t for t, _ in maa.template_calls
                         if t == "lulian/ui海联.png"]
        self.assertTrue(hailian_calls)

    def test_round_still_ends_on_lulian_banner(self):
        maa = FakeMaa(templates={"lulian/ui陆联.png": (480, 95)})
        self.assertEqual(self._run(maa, _raid_loop_cfg()), (True, 0))

    def test_missing_round_end_raises(self):
        cfg = _raid_loop_cfg()
        del cfg["round_end"]
        host = _RaidLoopHost(FakeMaa(), cfg)
        with self.assertRaises(KeyError):
            list(host.battle_loop_stream(need_battle=False))


if __name__ == "__main__":
    unittest.main()

class DisableAutoMarchTests(unittest.TestCase):
    def host(self, delegated, opened=True, cancel=True, final=False):
        host = BattleMixin()
        host.config = {'team_select': {'auto_march': {
            'check_delegated': {'template': 'checked', 'roi': [1100, 340, 1279, 430]},
            'enable_button': {'template': 'button'},
            'delegate_button': {'template': 'delegate'}, 'close_window': [1038, 47]}}}
        host.maa = Mock()
        host.maa.exists.side_effect = [delegated, final]
        host.maa.template_match.side_effect = lambda tpl: opened if tpl == 'delegate' else True
        host.maa.ocr.side_effect = lambda text, *args, **kwargs: (
            Point(400, 300) if (text == '自动行军' and opened) or (text == '不委托' and cancel) else None)
        host._click_template_config = Mock(return_value=True)
        host._click_point = Mock()
        return host

    @patch('touken.flows.battle.time.sleep')
    def test_already_off_does_not_toggle(self, sleep):
        host = self.host(False)
        self.assertTrue(host._disable_auto_march())
        host._click_template_config.assert_not_called()

    @patch('touken.flows.battle.time.sleep')
    def test_on_is_cancelled_and_verified(self, sleep):
        host = self.host(True)
        self.assertTrue(host._disable_auto_march())
        self.assertEqual(host.maa.ocr.call_args.args[0], '不委托')
        self.assertEqual(host.maa.ocr.call_args.kwargs['match_mode'], 'exact')
        host.maa.click.assert_called_once_with(Point(400, 300))
        host._click_point.assert_called_once_with([1038, 47])

    @patch('touken.flows.battle.time.sleep')
    def test_missing_popup_never_closes_team_panel(self, sleep):
        host = self.host(True, opened=False)
        self.assertFalse(host._disable_auto_march())
        host.maa.click.assert_not_called()
        host._click_point.assert_not_called()

    @patch('touken.flows.battle.time.sleep')
    def test_missing_cancel_or_still_on_is_failure(self, sleep):
        self.assertFalse(self.host(True, cancel=False)._disable_auto_march())
        self.assertFalse(self.host(True, final=True)._disable_auto_march())
