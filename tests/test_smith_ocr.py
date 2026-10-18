import unittest
from unittest.mock import patch

from touken import sword_db
from touken.flows.smith import SmithMixin
from touken.maa_adapter import Point


class _FakeMaa:
    def __init__(self):
        self.rois = []

    def ocr_all(self, roi):
        self.rois.append(roi.to_list())
        if roi.to_list() == [120, 215, 240, 30]:
            return [("笑面青江", roi.center)]
        return []


class SmithOcrTests(unittest.TestCase):
    def test_scan_whitelist_reads_the_dedicated_name_strip(self):
        sword_id, _ = sword_db.find_by_name("笑面青江")
        flow = SmithMixin()
        flow.maa = _FakeMaa()

        result = flow._scan_whitelist_row({sword_id})

        self.assertEqual(result, ("笑面青江", 195))
        self.assertEqual(flow.maa.rois[0], [120, 215, 240, 30])


class _RevealFakeMaa:
    """收刀用：popup 区永远干净；整屏 OCR 出 reveal_tokens；锻刀状况标题按状态出"""

    def __init__(self, reveal_tokens, on_status_after=2):
        self.reveal_tokens = reveal_tokens
        self.on_status_after = on_status_after
        self.shots = 0
        self.clicked = []

    def screenshot(self, force=False):
        self.shots += 1

    def click(self, pt):
        self.clicked.append((pt.x, pt.y))

    def ocr(self, expected, roi, match_mode="exact"):
        if expected == "锻刀状况" and self.shots > self.on_status_after:
            return Point(640, 70)
        return None

    def ocr_all(self, roi):
        if roi.to_list() == [0, 90, 1280, 630]:  # 整屏认人区
            return list(self.reveal_tokens)
        return []  # 氪金弹窗区没字

    def template_match(self, template, roi=None, threshold=0.8):
        return None


class ForgeCollectRecognitionTests(unittest.TestCase):
    def test_read_forge_sword_strict_match(self):
        flow = SmithMixin()
        flow.maa = _RevealFakeMaa([("大和守安定", Point(500, 600))])

        sword = flow._read_forge_sword()

        self.assertEqual(sword["name"], "大和守安定")
        self.assertEqual(sword["sword_id"], "touken_087_yamato_no_kami_yasusada")

    def test_read_forge_sword_rejects_garbage_and_typos(self):
        flow = SmithMixin()
        # 界面杂字不含刀名；错一个字（大和守安走）不许靠模糊兜底乱认
        flow.maa = _RevealFakeMaa(
            [("刀位", Point(1, 1)), ("大和守安走", Point(2, 2))])

        self.assertIsNone(flow._read_forge_sword())

    def test_find_by_name_fuzzy_switch(self):
        self.assertIsNotNone(sword_db.find_by_name("源清磨"))  # 模糊兜底认对错字
        self.assertIsNone(sword_db.find_by_name("源清磨", fuzzy=False))

    def test_display_name_corrects_ocr_typos_for_logs(self):
        # 换队长/换人日志用：漏字补全成标准名，认不出原样返回
        self.assertEqual(sword_db.display_name("夜左文字"), "小夜左文字")
        self.assertEqual(sword_db.display_name("研藤四郎"), "药研藤四郎")
        self.assertEqual(sword_db.display_name("？？天书？？"), "??天书??")  # 认不出返回清洗后原文

    def test_collect_slot_returns_recognized_sword(self):
        flow = SmithMixin()
        flow.maa = _RevealFakeMaa([("大和守安定", Point(500, 600))])

        with patch("touken.flows.smith.time.sleep"):
            collected, sword = flow._collect_slot(205)

        self.assertTrue(collected)
        self.assertEqual(sword["name"], "大和守安定")

    def test_collect_slot_without_name_still_collects(self):
        flow = SmithMixin()
        flow.maa = _RevealFakeMaa([])  # 获得画面没字（书法字认不出）照常收

        with patch("touken.flows.smith.time.sleep"):
            collected, sword = flow._collect_slot(205)

        self.assertTrue(collected)
        self.assertIsNone(sword)

    def test_read_countdown_parses_and_misses(self):
        flow = SmithMixin()

        class _TimerMaa:
            def __init__(self, tokens):
                self.tokens = tokens

            def screenshot(self, force=False):
                pass

            def ocr_all(self, roi):
                return self.tokens

        flow.maa = _TimerMaa([("01:27:30", Point(500, 300))])
        self.assertEqual(flow._read_countdown(205), ("1:27:30", 5250))
        flow.maa = _TimerMaa([("空闲中", Point(500, 300))])
        self.assertIsNone(flow._read_countdown(205))
        self.assertIsNone(flow._watch_hit(("1:27:30", 5250), set()))

    def test_read_countdown_without_colons(self):
        """OCR 吃掉冒号时按纯数字串从右切 秒/分/时（2026-10-19 误判点火失败现场）"""
        flow = SmithMixin()

        class _TimerMaa:
            def __init__(self, tokens):
                self.tokens = tokens

            def screenshot(self, force=False):
                pass

            def ocr_all(self, roi):
                return self.tokens

        flow.maa = _TimerMaa([("02", Point(0, 0)), ("剩余时间", Point(0, 0)),
                              ("59", Point(0, 0)), ("53", Point(0, 0))])
        self.assertEqual(flow._read_countdown(205), ("2:59:53", 10793))
        # 位数不够（读丢了）或分秒超界（不是倒计时）都不算数
        flow.maa = _TimerMaa([("完成", Point(0, 0)), ("00", Point(0, 0))])
        self.assertIsNone(flow._read_countdown(205))
        flow.maa = _TimerMaa([("997788", Point(0, 0))])
        self.assertIsNone(flow._read_countdown(205))
        self.assertIsNone(flow._watch_hit(("1:27:30", 5250), {5400}))  # 差 150s 超出容忍
        self.assertEqual(flow._watch_hit(("1:27:30", 5250), {5340}), "1:27:30")


class _ForgeHost(SmithMixin):
    """锻刀主流水的测试宿主：三炉全完成待收、刀位满（每炉第一次收失败→刀解腾位→再收）。
    刀位满时收一刀要烧 2 次循环，3 收×2 + 3 点火 = 9 次；旧上限 range(6) 会在
    收完 3 炉后烧光次数，点火一炉都排不上（2026-08-23 日课只收不锻的 bug）。"""

    def __init__(self):
        self.maa = _RevealFakeMaa([])
        self.current_location = None
        self.completed = [205, 345, 475]  # 待收炉队列（cy）
        self.collect_calls = {}           # cy → 已尝试收的次数
        self.ignited = 0

    def navigate_to_stream(self, loc):
        self.current_location = loc
        yield f"导航到{loc}"

    def _scan_slots(self):
        if self.completed:
            return ("完成", self.completed[0])
        if self.ignited < 3:
            return ("空闲中", 205)
        return None

    def _collect_slot(self, cy):
        self.collect_calls[cy] = self.collect_calls.get(cy, 0) + 1
        if self.collect_calls[cy] == 1:
            return (False, None)  # 刀位满，弹氪金窗
        self.completed.remove(cy)
        return (True, None)

    def dismantle_stream(self, max_dismantle=1, _from_forge=False):
        yield "分解完成 1 把"

    def _start_forge(self, cy, recipe=None):
        self.ignited += 1
        return True

    def _read_countdown(self, cy):
        return None

    def _capture_inventory(self, phase=""):
        return
        yield

class ForgeAttemptBudgetTests(unittest.TestCase):
    def test_full_inventory_collects_still_leave_budget_to_ignite(self):
        host = _ForgeHost()
        with patch("touken.flows.smith.time.sleep"):
            messages = list(host.forge_stream(times=3))

        self.assertEqual(host.completed, [])      # 三炉都收了
        self.assertEqual(host.ignited, 3)         # 且三炉都点了火（旧上限下这里是 0）
        self.assertTrue(any("点了 3 炉" in m for m in messages))


class _LimitedForgeMaa:
    """限锻确认窗现场（issue#7）：点火后先弹「是否进行锻刀？」，
    点【是】才回状况页；不限锻时状况页直接出现"""

    def __init__(self, popup):
        self.popup = popup
        self.clicked = []

    def screenshot(self, force=False):
        pass

    def click(self, pt):
        self.clicked.append((pt.x, pt.y))
        if (pt.x, pt.y) == (660, 380):  # 【是】
            self.popup = False

    def ocr(self, expected, roi, match_mode="exact"):
        if expected == "锻刀资源投入":
            return Point(640, 70)
        if expected == "锻刀状况":
            return None if self.popup else Point(640, 70)
        if self.popup and expected == "是否进行锻刀":
            return Point(640, 300)
        if self.popup and expected == "是" and match_mode == "exact":
            return Point(660, 380)
        return None

    def ocr_all(self, roi):
        # 配比页四行当前值都是 700（与默认配方一致 → _apply_recipe 全部跳过）
        if 690 <= roi.to_list()[0] <= 700:
            return [("700", Point(800, 200))]
        if roi.to_list() == [150, 150, 630, 110] and not self.popup:
            return [("01:30:00", Point(400, 205))]
        return []


class StartForgeLimitedCampaignTests(unittest.TestCase):
    def test_popup_is_confirmed_with_yes(self):
        flow = SmithMixin()
        flow.maa = _LimitedForgeMaa(popup=True)

        with patch("touken.flows.smith.time.sleep"):
            ok = flow._start_forge(205)

        self.assertTrue(ok)
        self.assertEqual(flow.maa.clicked, [(850, 205), (1146, 608), (660, 380)])

    def test_no_popup_unchanged(self):
        flow = SmithMixin()
        flow.maa = _LimitedForgeMaa(popup=False)

        with patch("touken.flows.smith.time.sleep"):
            ok = flow._start_forge(205)

        self.assertTrue(ok)
        self.assertEqual(flow.maa.clicked, [(850, 205), (1146, 608)])


class RecipeValidationTests(unittest.TestCase):
    def test_override_beats_config_and_range_is_enforced(self):
        flow = SmithMixin()
        flow.config = {"forge": {"recipe": [350, 350, 350, 350]}}
        self.assertEqual(flow._forge_recipe(), [350, 350, 350, 350])
        self.assertEqual(flow._forge_recipe([300, 300, 300, 300]),
                         [300, 300, 300, 300])
        # 越界（键盘只接受三位数，下限 10）/ 缺项 → 回落配置；配置也坏 → 700×4
        self.assertEqual(flow._forge_recipe([5, 300, 300, 300]),
                         [350, 350, 350, 350])
        self.assertEqual(flow._forge_recipe([1000, 300, 300, 300]),
                         [350, 350, 350, 350])
        flow.config = {"forge": {"recipe": [1, 2, 3]}}
        self.assertEqual(flow._forge_recipe(), [700, 700, 700, 700])


class _RecipeMaa:
    """配比页现场：行 OCR 出当前值；点行数字区开键盘（顶部 OCR 出资源名）；
    敲数字入缓冲；点「输入」(640,602) 把缓冲写回该行并关窗"""

    def __init__(self, current, broken_row=None):
        self.current = dict(current)
        self.broken_row = broken_row  # 模拟某行键盘打不开
        self.dialog = None
        self.digits = ""
        self.clicked = []

    _ROWS = {"木炭": 221, "玉钢": 353, "冷却材": 486, "砥石": 577}

    def screenshot(self, force=False):
        pass

    def ocr(self, expected, roi, match_mode="exact"):
        if expected == "锻刀资源投入" and self.dialog is None:
            return Point(640, 70)
        if self.dialog is not None and expected == self.dialog:
            return Point(557, 198)
        return None

    def ocr_all(self, roi):
        if self.dialog is None:
            for name, y in self._ROWS.items():
                if roi.to_list() == [690, y - 45, 220, 90]:
                    return [(str(self.current[name]), Point(800, y))]
        return []

    def click(self, pt):
        pos = (pt.x, pt.y)
        self.clicked.append(pos)
        if self.dialog is None and pos[0] == 700:
            for name, y in self._ROWS.items():
                if pos[1] == y and name != self.broken_row:
                    self.dialog = name
                    self.digits = ""
            return
        if self.dialog is not None and pos == (640, 602):  # 输入
            self.current[self.dialog] = int(self.digits)
            self.dialog = None
            return
        if self.dialog is not None:
            for d, p in SmithMixin._KEYPAD.items():
                if pos == p:
                    self.digits += d


class ApplyRecipeTests(unittest.TestCase):
    def test_rows_already_matching_are_skipped(self):
        flow = SmithMixin()
        flow.config = {"forge": {}}
        flow.maa = _RecipeMaa({"木炭": 700, "玉钢": 700, "冷却材": 700, "砥石": 700})
        with patch("touken.flows.smith.time.sleep"):
            self.assertTrue(flow._apply_recipe([700, 700, 700, 700]))
        self.assertEqual(flow.maa.clicked, [])  # 一致就一下都不点

    def test_mismatching_rows_get_typed_in_full(self):
        flow = SmithMixin()
        flow.config = {"forge": {}}
        flow.maa = _RecipeMaa({"木炭": 700, "玉钢": 300, "冷却材": 700, "砥石": 300})
        with patch("touken.flows.smith.time.sleep"):
            self.assertTrue(flow._apply_recipe([300, 300, 300, 300]))
        self.assertEqual(flow.maa.current["木炭"], 300)
        self.assertEqual(flow.maa.current["冷却材"], 300)
        # 已一致的行不重设
        self.assertNotIn((700, 353), flow.maa.clicked)
        self.assertNotIn((700, 577), flow.maa.clicked)

    def test_dialog_not_opening_aborts_safely(self):
        flow = SmithMixin()
        flow.config = {"forge": {}}
        flow.maa = _RecipeMaa({"木炭": 700, "玉钢": 700, "冷却材": 700, "砥石": 700},
                              broken_row="木炭")
        with patch("touken.flows.smith.time.sleep"):
            self.assertFalse(flow._apply_recipe([300, 300, 300, 300]))
        # 键盘没开就一个数字键都不许敲
        keypad = set(SmithMixin._KEYPAD.values())
        self.assertFalse(any(c in keypad for c in flow.maa.clicked))

    def test_unreadable_row_is_reset_not_guessed(self):
        flow = SmithMixin()
        flow.config = {"forge": {}}
        flow.maa = _RecipeMaa({"木炭": 700, "玉钢": 700, "冷却材": 700, "砥石": 700})
        flow.maa.ocr_all = lambda roi: []  # 全读不出
        with patch("touken.flows.smith.time.sleep"):
            self.assertTrue(flow._apply_recipe([700, 700, 700, 700]))
        # 读不出就当不一致，重设一遍（幂等无害）
        self.assertIn((700, 221), flow.maa.clicked)


class PanelRecipeFieldTests(unittest.TestCase):
    def test_forge_and_daily_both_carry_recipe_fields(self):
        from panel import server
        from panel.daily_workflow import recipe_from_params
        scripts = server.list_scripts()
        for key in ("forge", "daily"):
            keys = {f["key"] for f in scripts[key]["params"]}
            for rk in ("recipe_charcoal", "recipe_steel",
                       "recipe_coolant", "recipe_whetstone"):
                self.assertIn(rk, keys, f"{key} 缺 {rk}")
        self.assertEqual(recipe_from_params(
            {"recipe_charcoal": "300", "recipe_steel": "300",
             "recipe_coolant": "300", "recipe_whetstone": "300"}),
            [300, 300, 300, 300])
        self.assertIsNone(recipe_from_params({"recipe_charcoal": "abc"}))
        self.assertIsNone(recipe_from_params({"recipe_charcoal": 5,
                                              "recipe_steel": 300,
                                              "recipe_coolant": 300,
                                              "recipe_whetstone": 300}))
        self.assertIsNone(recipe_from_params({}))


class _TenrenMaa:
    """十连限锻现场（2026-09-11 真机流程）：状况页→十连配比页→点火→
    揭示动画→金色结算榜→回配比页；deferred=True 时动画攒着等退出才播。"""

    def __init__(self, cap=180, tokens=(858, 420), boards=(), deferred=False,
                 swallow_first_fire=False):
        self.screen = "status"
        self.cap = cap
        self.cap_max = 200
        self.tokens = {"委托符": tokens[0], "加速符": tokens[1]}
        self.boards = list(boards)
        self.deferred = deferred
        self.swallow_first_fire = swallow_first_fire
        self.speedup = False
        self.fired = 0
        self.pending = 0          # 攒着没播的揭示动画（deferred 模式）
        self.clicks = []

    def screenshot(self, force=False):
        pass

    def click(self, pt):
        x, y = pt.x, pt.y
        self.clicks.append((x, y))
        if self.screen == "status" and x == 985:            # 行内十连锻刀
            self.screen = "recipe"
        elif self.screen == "recipe" and (x, y) == (1176, 615):  # 点火
            if self.swallow_first_fire:
                self.swallow_first_fire = False
                return
            self.fired += 1
            self.cap += 10
            self.tokens["委托符"] -= 9
            self.tokens["加速符"] -= 10
            if self.deferred:
                self.pending += 1
                self.screen = "recipe"
            else:
                self.screen = "anim"
        elif self.screen == "anim" and (x, y) == (113, 78):      # » 快进
            self.screen = "board"
        elif self.screen == "board" and (x, y) == (640, 360):    # 点穿结算榜
            self.pending = max(0, self.pending - 1)
            self.screen = "recipe"
        elif (x, y) == (1193, 422):                              # 加速符勾选
            self.speedup = not self.speedup
        elif (x, y) == (141, 87):                                # 配比页返回
            self.screen = "anim" if self.pending else "status"
        elif (x, y) == (26, 175):                                # 左栏锻刀标签
            self.screen = "status"

    def ocr(self, expected, roi, match_mode="exact"):
        x1, y1, x2, y2 = roi.to_list()
        if expected == "锻刀资源投入":
            return Point(640, 70) if self.screen == "recipe" else None
        if expected == "锻刀状况":
            return Point(640, 70) if self.screen == "status" else None
        if expected == "十连锻刀" and self.screen == "recipe":
            return Point(1176, 615)
        if expected == "节省" and self.screen == "recipe":
            return Point(1215, 248)
        if expected == "显现积分" and self.screen == "board" and y1 >= 600:
            return Point(1000, 660)
        return None

    def ocr_all(self, roi):
        x1, y1, x2, y2 = roi.to_list()
        if self.screen == "status" and 100 <= x1 <= 200:         # 炉行状态
            return [("空闲中", Point(300, (y1 + y2) // 2))]
        if self.screen == "recipe" and 690 <= x1 <= 700:         # 配方行当前 700
            return [("700", Point(800, 200))]
        if 960 <= x1 <= 1100 and y1 <= 90:                       # 顶栏刀位
            return [(f"{self.cap} / {self.cap_max}", Point(1020, 65))]
        if self.screen == "status" and 1140 <= x1 and 300 < y1 < 400:
            return [(str(self.tokens["委托符"]), Point(1200, 350))]
        if self.screen == "status" and 1140 <= x1 and 400 < y1 < 520:
            return [(str(self.tokens["加速符"]), Point(1200, 480))]
        if self.screen == "recipe" and x1 >= 1100 and 380 < y1 < 560:
            # 加速符预览：勾了两个数（现值▼扣后），没勾一个数
            n = self.tokens["加速符"]
            toks = [("使用", Point(1200, 415)), ("加速符", Point(1200, 445)),
                    (str(n), Point(1200, 480))]
            if self.speedup:
                toks.append((str(n - 10), Point(1200, 520)))
            return toks
        if self.screen == "board" and y2 <= 640:                 # 结算榜刀名区
            idx = min(self.fired, len(self.boards)) - 1
            names = self.boards[idx] if self.boards and idx >= 0 else []
            return [(n, Point(300, 200)) for n in names]
        return []

    def template_match(self, name, threshold=0.8):
        return None


class _TenrenHost(SmithMixin):
    def __init__(self, maa):
        self.maa = maa
        self.current_location = None
        self.config = {}
        self.events = []
        self.changes = []
        self.dismantled = 0

    def navigate_to_stream(self, loc):
        self.current_location = loc
        yield f"导航到{loc}"

    def record_event(self, kind, **kw):
        self.events.append((kind, kw))
        return len(self.events)

    def record_resource_change(self, name, delta, **kw):
        self.changes.append((name, delta))

    def dismantle_stream(self, max_dismantle=1, _from_forge=False, whitelist=None):
        self.dismantled += max_dismantle
        self.maa.cap -= max_dismantle
        yield f"分解完成 {max_dismantle} 把"

    def _capture_inventory(self, phase=""):
        return
        yield

    def _shugo_loop_stream(self, dry_run):
        self.maa.cap -= 10
        yield "炼糖完成"
        return 1


_BOARD1 = ["加州清光", "大和守安定", "加州清光", "陆奥守吉行", "加州清光",
           "加州清光", "小狐丸", "压切长谷部", "宗三左文字", "陆奥守吉行"]
_BOARD2 = ["歌仙兼定", "蜂须贺虎彻", "千子村正", "鹤丸国永", "蜂须贺虎彻",
           "宗三左文字", "加州清光", "山姥切国广", "蜂须贺虎彻", "今剑"]


class LimitedForgeTests(unittest.TestCase):
    def _run(self, maa, **kw):
        host = _TenrenHost(maa)
        with patch("touken.flows.smith.time.sleep"):
            messages = list(host.limited_forge_stream(**kw))
        return host, messages

    def test_two_batches_costs_and_names(self):
        maa = _TenrenMaa(boards=[_BOARD1, _BOARD2])
        host, messages = self._run(maa, total=20)
        self.assertEqual(maa.fired, 2)
        self.assertTrue(maa.speedup)                       # 加速符勾上了
        self.assertEqual(maa.screen, "status")             # 收工回到状况页
        # 每发：四资源各 -7000、委托符 -9、加速符 -10
        self.assertEqual(host.changes.count(("委托符", -9)), 2)
        self.assertEqual(host.changes.count(("加速符", -10)), 2)
        self.assertEqual(host.changes.count(("木炭", -7000)), 2)
        tenren_events = [kw for kind, kw in host.events if kind == "forge.tenren"]
        self.assertEqual(len(tenren_events), 2)
        self.assertIn("小狐丸", tenren_events[0]["swords"])
        self.assertTrue(any("揭榜" in m and "小狐丸" in m for m in messages))
        self.assertTrue(any("锻了 20 把" in m for m in messages))

    def test_stop_on_hit(self):
        # 目标小狐丸第一发就中，总共要 5 发也立刻收手
        maa = _TenrenMaa(boards=[_BOARD1, _BOARD2])
        host, messages = self._run(maa, total=50, watch_names=["小狐丸"])
        self.assertEqual(maa.fired, 1)
        self.assertTrue(any("喜报" in m and "小狐丸" in m for m in messages))
        self.assertTrue(any("收手" in m for m in messages))

    def test_watch_no_hit_runs_all(self):
        maa = _TenrenMaa(boards=[_BOARD1, _BOARD2])
        host, messages = self._run(maa, total=20, watch_names=["三日月宗近"])
        self.assertEqual(maa.fired, 2)
        self.assertFalse(any("喜报" in m for m in messages))

    def test_preflight_insufficient_tokens(self):
        maa = _TenrenMaa(tokens=(5, 3))
        host, messages = self._run(maa, total=50)
        self.assertEqual(maa.fired, 0)
        self.assertEqual(maa.clicks, [])                   # 一发没点，白嫖动画都没看
        self.assertTrue(any("库存不够" in m for m in messages))

    def test_unreadable_capacity_never_fires(self):
        maa = _TenrenMaa(boards=[_BOARD1])
        original = maa.ocr_all

        def unread_capacity(roi):
            x1, y1, _x2, _y2 = roi.to_list()
            if 960 <= x1 <= 1100 and y1 <= 90:
                return []
            return original(roi)

        maa.ocr_all = unread_capacity
        _host, messages = self._run(maa, total=10)
        self.assertEqual(maa.fired, 0)
        self.assertTrue(any("刀位数量读不出来" in m for m in messages))

    def test_capacity_short_defaults_to_stop(self):
        maa = _TenrenMaa(cap=196, boards=[_BOARD1])
        host, messages = self._run(maa, total=10)
        self.assertEqual(host.dismantled, 0)
        self.assertEqual(maa.fired, 0)
        self.assertTrue(any("不自动处理刀剑" in m for m in messages))

    def test_full_slots_dismantle_rescue(self):
        maa = _TenrenMaa(cap=196, boards=[_BOARD1])
        host, messages = self._run(
            maa, total=10, capacity_action="dismantle")
        self.assertEqual(host.dismantled, 6)               # 只腾本发缺的 6 格
        self.assertEqual(maa.fired, 1)
        self.assertTrue(any("刀位只剩" in m for m in messages))

    def test_missed_fire_tap_is_not_retried(self):
        maa = _TenrenMaa(boards=[_BOARD1], swallow_first_fire=True)
        host, messages = self._run(maa, total=10)
        self.assertEqual(maa.fired, 0)
        self.assertEqual(
            sum(1 for point in maa.clicks if point == SmithMixin._TENREN_FIRE), 1)
        self.assertTrue(any("绝不自动补点" in m for m in messages))

    def test_deferred_reveal_read_at_exit(self):
        # 动画攒着：批量中没揭榜，退出时补读
        maa = _TenrenMaa(boards=[_BOARD1], deferred=True)
        host, messages = self._run(maa, total=10)
        self.assertEqual(maa.fired, 1)
        self.assertEqual(maa.screen, "status")
        self.assertTrue(any("补读揭榜" in m and "小狐丸" in m for m in messages))

    def test_deferred_reveal_with_lost_capacity_never_double_fires(self):
        maa = _TenrenMaa(boards=[_BOARD1], deferred=True)
        original = maa.ocr_all

        def lose_capacity_after_fire(roi):
            x1, y1, _x2, _y2 = roi.to_list()
            if maa.fired and 960 <= x1 <= 1100 and y1 <= 90:
                return []
            return original(roi)

        maa.ocr_all = lose_capacity_after_fire
        _host, messages = self._run(maa, total=10)
        self.assertEqual(maa.fired, 1)
        self.assertEqual(
            sum(1 for point in maa.clicks if point == SmithMixin._TENREN_FIRE), 1)
        self.assertTrue(any("不会重复点火" in m for m in messages))

    def test_sugar_capacity_action_rechecks_then_fires(self):
        maa = _TenrenMaa(cap=196, boards=[_BOARD1])
        _host, messages = self._run(
            maa, total=10, capacity_action="sugar")
        self.assertEqual(maa.fired, 1)
        self.assertTrue(any("习合完成 1 轮" in m for m in messages))

    def test_deferred_hit_still_celebrated_at_exit(self):
        # 攒着播的榜里中了目标：本发退出揭榜后立刻收手，不得烧下一发
        maa = _TenrenMaa(boards=[_BOARD1], deferred=True)
        host, messages = self._run(maa, total=50, watch_names=["小狐丸"])
        self.assertEqual(maa.fired, 1)
        self.assertTrue(any("喜报" in m and "小狐丸" in m for m in messages))
        self.assertTrue(any("按设定收手" in m for m in messages))
        self.assertTrue(any("目标命中" in m for m in messages))


class PanelLimitedFieldTests(unittest.TestCase):
    def test_forge_script_carries_limited_fields(self):
        from panel import server
        fields = {f["key"]: f for f in server.list_scripts()["forge"]["params"]}
        for key in ("forge_limited", "watch_names", "stop_on_hit"):
            self.assertIn(key, fields, f"forge 缺 {key}")
        self.assertEqual(fields["capacity_action"]["default"], "stop")
        self.assertEqual(fields["watch"]["visibleWhen"],
                         {"key": "forge_limited", "is": "false"})

    def test_build_forge_routes_limited(self):
        from panel import server

        calls = []

        class _Agent:
            def limited_forge_stream(self, **kw):
                calls.append(kw)
                yield "ok"

            def forge_stream(self, **kw):
                calls.append({"normal": kw})
                yield "ok"

        list(server._build_forge(_Agent(), None, {
            "forge_limited": "true", "times": "50",
            "recipe_charcoal": 950, "recipe_steel": 950,
            "recipe_coolant": 950, "recipe_whetstone": 950,
            "watch_names": "小狐丸, 山姥切国广", "stop_on_hit": True}))
        self.assertEqual(calls, [{"total": 50, "recipe": [950, 950, 950, 950],
                                  "watch_names": ["小狐丸", "山姥切国广"],
                                  "stop_on_hit": True,
                                  "capacity_action": "stop"}])

        calls.clear()
        list(server._build_forge(_Agent(), None, {"times": "3"}))
        self.assertEqual(calls, [{"normal": {"times": 3, "watch": [],
                                             "recipe": None}}])


if __name__ == "__main__":
    unittest.main()
