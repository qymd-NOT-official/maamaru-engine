# -*- coding: utf-8 -*-
"""国服出阵画面适配。

国服模板、OCR、坐标、原生截图和异去界面处理集中在这里。
公共出阵循环只调用这些观察/操作方法；安全出发仍由宿主的 BattleMixin 执行。
老用户配置优先，未配置的默认模板经国服素材包取得。
"""
import os
import re
import time

from .. import sword_db
from ..device_adapter import roi_4to4
from .battle import find_deploy_button
from ..map_read import CV2_AVAILABLE, boss_distance_from_image


def _parse_fragment_counts(tokens: list) -> dict:
    """「可以获得的宝物碎片」弹窗 OCR 词元 → {碎片名: 所持数}。

    tokens: [(文本, x, y)]。名字词元以“碎片”结尾且在弹窗上部导航区
    之外（y>150，排除标题“可以获得的宝物碎片”）；数量词元形如“3 个”，
    取名字正下方（纵向 80px、横向 80px 以内）最近的一个。配不对的名字
    不进结果——宁可缺数，不可瞎编。
    """
    names = [(text, x, y) for text, x, y in tokens
             if text.endswith("碎片") and y > 150]
    counts = []
    for text, x, y in tokens:
        m = re.fullmatch(r"(\d+)\s*个", text)
        if m:
            counts.append((int(m.group(1)), x, y))
    result = {}
    for name, nx, ny in names:
        below = [(c, cx, cy) for c, cx, cy in counts
                 if 0 < cy - ny <= 80 and abs(cx - nx) <= 80]
        if below:
            best = min(below, key=lambda item: (item[2] - ny) ** 2
                       + (item[1] - nx) ** 2)
            result[name] = best[0]
    return result


def _parse_popup_map(tokens: list) -> int | None:
    """弹窗顶部小图选择器（如“四 鸟羽”）→ 小图号；认不出返回 None。"""
    for text, _x, y in tokens:
        if y < 150 and text in ("一", "二", "三", "四"):
            return ("一", "二", "三", "四").index(text) + 1
    return None


class CnSortieViewMixin:
    """国服默认适配，与 SortieLoopMixin 组合；其他服须提供自己的画面适配。"""

    def _sortie_config(self, cfg_key, map_type):
        cfg = dict(self.config.get("sortie", {}))
        cfg.update(self.config.get(cfg_key, {}))
        return (cfg, self.config.get("map_select", {}).get(map_type, {}),
                self.config.get("team_select", {}).get("teams", {}))

    def _sortie_capture_frame(self):
        return self.device.screenshot(force=True)

    def _sortie_tap(self, point):
        return self._click_point(point)

    def _sortie_click(self, match):
        return self.device.click(match)

    def _sortie_find_deploy(self, cfg):
        return find_deploy_button(self.device, cfg)

    def _sortie_decide_button(self, cfg):
        return self.device.template_match(cfg["decide_button"]["template"])

    def _sortie_home_visible(self, cfg):
        return bool(self.device.template_match(cfg["home_ui"]["template"]))

    def _sortie_march_stopped(self, cfg):
        marker = cfg["march_stop_ocr"]
        return bool(self.device.ocr(expected=marker["expected"],
                                    roi=roi_4to4(*marker["roi"])))

    def _sortie_can_read_boss(self):
        return CV2_AVAILABLE

    def _sortie_boss_distance(self):
        return boss_distance_from_image(self.device.screenshot())

    def _sortie_wait_decide(self, cfg, timeout_s):
        return self.wait_landmark_skipping(
            template=cfg["decide_button"]["template"],
            skip_point=cfg.get("skip_tap"), timeout_s=timeout_s,
            stable_hits=3, tap_even_when_found=True)

    def _sortie_template(self, logical):
        from pathlib import Path
        from ..runtime_paths import RESOURCE_DIR
        from ..server_pack import resolve_template_path
        root = Path(getattr(self.device, "resource_dir", RESOURCE_DIR)).resolve()
        return resolve_template_path("@" + logical, root).relative_to(root / "image").as_posix()

    def _on_team_select_page(self, cfg: dict) -> bool:
        """顶部标题 OCR 判断当前是否已经在部队选择页（复用 _last_image）。"""
        ocr_cfg = cfg.get("team_ui_ocr", {})
        roi_raw = ocr_cfg.get("roi")
        if not roi_raw:
            return False
        return bool(self.device.ocr(expected=ocr_cfg.get("expected", "部队选择"),
                                 roi=roi_4to4(*roi_raw)))

    def _watch_battle_frame(self):
        """行军期间每张实际运行帧的统一战斗结算观察入口。

        锚点：结算页「戦闘結果」每场战斗必现且停留 ≥2 帧（2026-09-05 异去
        委托行军 148 帧运行实录，9 场全中、场间空窗 ≥6 帧）。出现沿计一场、
        消失后重新武装，同一画面重复帧不重数。掉落识别、返回本丸等流程
        内部取到的新帧也必须过这里，不允许任何帧绕过计数。
        未处于行军监控（self._battle_watch 为 None）时直接返回。
        """
        watch = getattr(self, "_battle_watch", None)
        if watch is None:
            return
        if self.device.template_match(self._sortie_template("battle/result")):
            if not watch["visible"]:
                watch["battles"] += 1
            watch["visible"] = True
        else:
            watch["visible"] = False

    # 掉落获得画面的识别点位（1280x720，真机截图校准）
    _OBTAIN_BADGE_ROI = (1105, 40, 1170, 160)   # 右侧立牌的红底「刀派」徽（稀有款）
    # 掉刀预告横幅「发现了新的刀剑男士！」：点掉结算页后 +0.4~1.6s 贴在结算页
    # 上（2026-08-24 真机连拍），是掉刀最早最稳的信号
    _OBTAIN_BANNER_ROI = (100, 280, 1180, 460)
    _OBTAIN_BANNER_TEXT = "新的刀剑男士"
    # 左下对话框名牌「打刀 大和守安定」。获得画面有两种布局：带刀派立牌的
    # 对话框偏下（名牌 y≈630-690），不带的偏上（y≈500-560，2026-08-24 真机
    # 连拍实测），宽 ROI 两种都罩住。
    _NAME_PLATE_ROI = (0, 480, 430, 710)
    # 名牌 = 刀种前缀 + 空格 + 名字；名字里不许夹标点（防 OCR 粘连对话框台词）
    _STRIP_RE = re.compile(
        r"^\s*(短刀|胁差|打刀|太刀|大太刀|枪|薙刀|剑)[\s_　]*([^\s，。,.、！!？?]+)")
    _SWORD_TYPES = ("短刀", "胁差", "打刀", "太刀", "大太刀", "枪", "薙刀", "剑")

    def _read_drop_sword(self):
        """掉落获得画面认人：横幅预告 + 名牌确认，两段式。

        时机（2026-08-24 真机连拍，从点掉结算页起算）：+0.4~1.6s 横幅
        「发现了新的刀剑男士！」→ +2~3s 樱花转场 → +3.5~4.8s 裸立绘
        （名牌区全空，啥都认不到）→ +5s 起立牌+对话框+名牌齐活，画面
        静止等戳。所以名牌区空时先看横幅：有横幅 = 掉刀预告，耐心等名牌
        到位（约 5 秒），绝不提前放弃——提前返回 None，下一拍安全区点击
        正好把刚到位的获得画面点掉（漏认的元凶）。没横幅按老节奏快速退场，
        不拖慢正常行军圈。
        名牌的「刀种+名字」组合只有获得画面有；结果页成员栏裸名没有刀种
        前缀，天然拒认（2026-08-24 假博多事故的根治）。认错比认不到糟，
        名字走名册严格匹配（关模糊兜底）。

        返回三态（调用方必须区分，绝不能把"没看到"和"看到但没认出"都
        当成无掉落）：
          {"status": "recognized",   "sword": {...}}  名字认出
          {"status": "unrecognized", "sword": None}   见过横幅/刀种/立牌等
                掉刀证据但名字没认出，或认人流程自身异常。返回该状态时
                本方法已置 _drop_watch_failed：本圈观察不可信，不许落
                confirmed_none；后续拍若认出名字，掉落事实照常优先
          {"status": "none",         "sword": None}   全程没有任何掉刀证据
        """

        def _recognized(found):
            sid, info = found
            return {"status": "recognized",
                    "sword": {"sword_id": sid,
                              "name": info.get("name_zh") or info["name"],
                              "name_jp": info["name"]}}

        try:
            def _grab():
                # 内部取的每一张新帧也是行军观察帧，必须过战斗计数入口
                self.device.screenshot(force=True)
                self._watch_battle_frame()

            evidence = False  # 见过横幅/刀种前缀/立牌徽章 = 确知掉刀
            patient = False
            for attempt in range(10):  # 耐心模式覆盖横幅→名牌约 5 秒，余量翻倍
                tokens = self.device.ocr_all(roi_4to4(*self._NAME_PLATE_ROI))
                saw_prefix = False
                for text, _pt in tokens:
                    m = self._STRIP_RE.match(text)
                    if m:
                        saw_prefix = True
                        found = sword_db.find_by_name(m.group(2), fuzzy=False)
                        if found:
                            return _recognized(found)
                    elif text.strip() in self._SWORD_TYPES:
                        saw_prefix = True
                if saw_prefix:
                    evidence = True
                    # 刀种和名字被 OCR 拆成两条：有刀种在场，裸名也认
                    # （结果页成员栏没有刀种前缀，进不来这个分支）
                    for text, _pt in tokens:
                        if self._STRIP_RE.match(text) or text.strip() in self._SWORD_TYPES:
                            continue
                        found = sword_db.find_by_name(text.strip(), fuzzy=False)
                        if found:
                            return _recognized(found)
                    time.sleep(0.8)  # 名牌在但名字没匹配上（OCR 花了），重读
                    _grab()
                    continue
                # 名牌区空空：横幅在 = 掉刀预告，转耐心模式等获得画面到位
                if not patient and self.device.ocr(
                        self._OBTAIN_BANNER_TEXT,
                        roi_4to4(*self._OBTAIN_BANNER_ROI)):
                    evidence = True
                    patient = True
                if patient:
                    time.sleep(0.8)
                    _grab()
                    continue
                # 没横幅：有立牌=获得画面，等对话框滑入重读；
                # 没立牌先给一拍（对话框可能还在路上），第二拍还空就不是获得画面
                if self.device.ocr("刀派", roi_4to4(*self._OBTAIN_BADGE_ROI)):
                    evidence = True
                    time.sleep(0.8)
                    _grab()
                    continue
                if attempt == 0:
                    time.sleep(0.6)
                    _grab()
                    continue
                return {"status": "none", "sword": None}
        except Exception:
            # 认人流程翻车 = 这段行军掉落观察不可信，本圈不许 confirmed_none
            self._drop_watch_failed = True
            return {"status": "unrecognized", "sword": None}
        # 耐心拍数耗尽：见过掉刀证据就得承认"有掉刀但没认出名字"，
        # 绝不许假装没看见（那会把真实掉落记成"确认无掉落"）
        if evidence:
            self._drop_watch_failed = True
            return {"status": "unrecognized", "sword": None}
        return {"status": "none", "sword": None}

    def _dismiss_yosari_milestone(self, cfg: dict) -> bool:
        """收掉异去累计圈数里程碑弹窗（500/800/1100 圈送火车切）。

        弹窗是「确定」键而不是章节页的「决定」键，只会等决定的循环
        被它卡死（2026-09-10 第 62 圈实测翻车：老大手动点确定后才
        看到火车切获得窗）。双条件防误点：确定按钮 + 画面里出现
        「奖励」才动手（老大回忆弹窗原句说的是"奖励"而非刀名，
        「火车切」留作兜底）；点完确定还有获得窗，用安全区收掉。
        返回 True = 本回合收过弹窗。
        """
        full = roi_4to4(0, 0, 1280, 720)
        handled = False
        for _ in range(3):
            self.device.screenshot(force=True)
            confirm = self.device.template_match(self._sortie_template("dialog/confirm"))
            if not confirm:
                break
            if not (self.device.ocr("奖励", full) or self.device.ocr("火车切", full)):
                break
            self.device.click(confirm)
            handled = True
            time.sleep(1.2)
        if handled:
            self.skip_safe(3, point=cfg.get("skip_tap"))
            if hasattr(self, "record_event"):
                self.record_event("yosari.milestone_claimed")
        return handled

    def _enter_yosari(self, cfg: dict) -> bool:
        """从默认的“过去”切换到右上角“异去”，并用文字复核。"""
        entry = cfg.get("entry", {})
        roi = roi_4to4(*entry.get("verify_roi", [285, 50, 850, 145]))
        expected = entry.get("expected", "归城提灯")
        self.device.screenshot(force=True)
        if self.device.ocr(expected, roi):
            return True
        self._click_point(entry.get("target", [978, 93]))
        # 异去是渐入进场的动画演出：进场途中归城提灯会被盖住/误认，
        # 先等 2 秒让页面稳定再认，别在动画里乱判
        time.sleep(2.0)
        for _ in range(6):
            self.device.screenshot(force=True)
            if self.device.ocr(expected, roi):
                return True
            time.sleep(0.5)
        return False

    def _confirm_yosari_departure(self, cfg: dict, auto_refill: bool = False,
                                  refill_attempted: bool = False):
        """处理新版归城提灯确认；缺灯时按四段确认链补充后返回部队选择。"""
        prompt = {"expected": "归城提灯进行出阵", "roi": [310, 155, 970, 330],
                  "confirm_target": [640, 603], **cfg.get("departure_confirm", {})}
        confirmed = False
        for attempt in range(2):
            if self._confirm_departure(cfg, text_prompt=prompt):
                confirmed = True
                break
            if attempt > 0:
                break
            # 确认框没来但即刻出阵还亮着 = 刚才那下被自动行军面板的关闭
            # 动画吞了（2026-10-19 现场：关面板后 2 秒内点出阵，画面停在
            # 部队选择页空等 17 秒）。按钮真的在才补点：确认框若已弹出
            # 只是文字没认到，按钮被遮罩盖住匹配不上，不会误点。
            depart_cfg = cfg.get("depart_button", {})
            template = depart_cfg.get("template")
            depart = self.device.template_match(template) if template else None
            if not depart:
                break
            yield "[异去] 确认框没出现但即刻出阵还亮着，刚才的点击可能被动画吞了，补点一次"
            self.device.click(depart)
            time.sleep(1.5)
        if not confirmed:
            yield "[异去] 没看到归城提灯出阵确认框；停止"
            return False

        refill = cfg.get("ticket_refill", {})

        def _wait_text(step: dict, fallback_expected: str, fallback_roi: list) -> bool:
            text = step.get("expected", fallback_expected)
            text_roi = roi_4to4(*step.get("roi", fallback_roi))
            for _ in range(8):
                self.device.screenshot(force=True)
                if self.device.ocr(text, text_roi):
                    return True
                time.sleep(0.4)
            return False

        purchase_screen = refill.get("purchase_screen", {})
        if not _wait_text(purchase_screen, "补充所需", [270, 75, 1010, 555]):
            # 没进入购买页，说明现有提灯有效，第一次确认后已经真正出阵。
            yield "[异去] 已确认使用现有归城提灯（未勾选探索道具）"
            return True

        if refill_attempted:
            yield "[异去] 补充后仍进入购买页，停止，避免重复消费小判"
            return False
        if not auto_refill:
            self._click_point(purchase_screen.get("close_target", [1040, 48]))
            yield "[异去] 归城提灯不足；自动补充已关闭，不消耗小判，收工"
            return False

        purchase_ledger = self._read_yosari_koban_purchase(purchase_screen)
        yield "[异去] 归城提灯不足，开始四段确认中的补充步骤"
        self._click_point(purchase_screen.get("confirm_target", [638, 611]))
        time.sleep(1.2)

        spend_confirm = refill.get("spend_confirm", {})
        if not _wait_text(spend_confirm, "是否消耗", [300, 80, 1010, 560]):
            yield "[异去] 没看到消耗小判的确认页，停止点击"
            return False
        self._click_point(spend_confirm.get("confirm_target", [784, 604]))
        time.sleep(1.2)

        completed = refill.get("completed", {})
        if not _wait_text(completed, "补充了归城提灯", [330, 120, 950, 460]):
            yield "[异去] 没看到归城提灯补充完成页，停止点击"
            return False
        event_payload = {
            "item": "归城提灯一",
            "source": "yosari.ticket_refill",
            "evidence": "purchase_completed",
        }
        if purchase_ledger:
            event_payload.update(purchase_ledger)
            self.record_event(
                "resource.change",
                resource="小判",
                delta=purchase_ledger["delta"],
                before=purchase_ledger["before"],
                after=purchase_ledger["after"],
                source="yosari.ticket_refill",
                attribution="confirmed",
                evidence="purchase_preview_balances",
                note="异去归城提灯补充",
            )
        self.record_event("yosari.ticket_refilled", **event_payload)
        self._click_point(completed.get("confirm_target", [638, 511]))
        time.sleep(1.2)
        if purchase_ledger:
            yield (f"[异去] 归城提灯补充完成，小判 "
                   f"{purchase_ledger['delta']:+d}，已回到部队选择")
        else:
            yield "[异去] 归城提灯补充完成；小判金额未识别，已回到部队选择"
        return "refilled"

    def _read_yosari_koban_purchase(self, purchase_screen: dict):
        """读取购买页展示的购买前/后小判；读不全时不猜金额。"""
        ocr_all = getattr(self.device, "ocr_all", None)
        if not callable(ocr_all):
            return None

        def _read_one(key: str, fallback: list):
            try:
                rows = ocr_all(roi_4to4(*purchase_screen.get(key, fallback)))
            except Exception:
                return None
            ordered = sorted(rows or [], key=lambda row: getattr(row[1], "x", 0))
            digits = "".join(re.sub(r"\D", "", str(text))
                             for text, _point in ordered)
            return int(digits) if digits else None

        before = _read_one("balance_before_roi", [465, 345, 625, 410])
        after = _read_one("balance_after_roi", [675, 345, 835, 410])
        if before is None or after is None or after >= before:
            return None
        return {"before": before, "after": after, "delta": after - before}

    def _yosari_round_done(self, cfg: dict) -> bool:
        """归城提灯标题与部队选择同时出现，才算确实回到异去小图页。"""
        marker = cfg.get("round_end_ocr", cfg.get("entry", {}))
        roi_raw = marker.get("roi", marker.get("verify_roi", [285, 50, 850, 145]))
        expected = marker.get("expected", "归城提灯")
        if not self.device.ocr(expected, roi_4to4(*roi_raw)):
            return False
        return bool(find_deploy_button(self.device, cfg))

    def _yosari_watch_dir(self, cfg_key: str):
        """蹲点目录：仅异去且显式开环境变量才启用，返回 None 表示不蹲。"""
        if cfg_key != "yosari" or os.environ.get("MAAMARU_YOSARI_WATCH") != "1":
            return None
        try:
            from ..runtime_paths import STATUS_DIR
            folder = STATUS_DIR / "debug" / "yosari_watch" / time.strftime("%Y%m%d_%H%M%S")
            folder.mkdir(parents=True, exist_ok=True)
            return folder
        except Exception:
            return None

    def _dump_watch_frame(self, folder, loop_no: int, seq: int):
        """把行军监控刚取的帧存成 JPEG；任何异常都不许打断行军。"""
        try:
            image = self.device.screenshot(force=False)
            if image is None:
                return
            from PIL import Image as _PILImage
            _PILImage.fromarray(image[:, :, ::-1]).save(
                str(folder / f"loop{loop_no}_{seq:04d}.jpg"), quality=70)
        except Exception:
            pass

    def _read_yosari_fragments(self, cfg: dict):
        """小图页开「宝物碎片」弹窗，OCR 读各类碎片所持数，读完关窗。

        返回 {"map_no": int|None, "counts": {碎片名: 个数}}；
        弹窗没开起来或识别翻车都返回 None，绝不影响出阵；
        弹窗关不掉返回 "stuck"——调用方必须收工，弹窗会挡住选图点击。
        """
        popup = cfg.get("fragments_popup", {})
        button = cfg.get("fragments_button", [895, 632])
        title = popup.get("title_expected", "可以获得的宝物碎片")
        close = popup.get("close", [1255, 30])
        roi = roi_4to4(*popup.get("ocr_roi", [30, 30, 1230, 500]))

        def _popup_tokens() -> list:
            self.device.screenshot(force=True)
            return [(text, pt.x, pt.y) for text, pt in self.device.ocr_all(roi)]

        try:
            self._click_point(button)
            time.sleep(1.2)
            tokens = _popup_tokens()
            if not any(title in text for text, _, _ in tokens):
                return None
            result = {"map_no": _parse_popup_map(tokens),
                      "counts": _parse_fragment_counts(tokens)}
        except Exception:
            return None
        for _ in range(3):
            self._click_point(close)
            time.sleep(0.8)
            try:
                if not any(title in text for text, _, _ in _popup_tokens()):
                    return result
            except Exception:
                return None
        return "stuck"

    def _save_map_miss(self, chapter: int, map_no: int, loop_no: int):
        """只保存无法判读的小地图，供用户主动反馈；正常识别不落盘。"""
        if self._map_miss_count >= 5:
            return
        try:
            from ..runtime_paths import STATUS_DIR
            folder = STATUS_DIR / "map_miss"
            folder.mkdir(parents=True, exist_ok=True)
            name = (f"miss_{chapter}-{map_no}_loop{loop_no}_"
                    f"{time.strftime('%H%M%S')}.png")
            if self.device.save_screenshot(str(folder / name), force=False):
                self._map_miss_count += 1
        except Exception:
            pass

    def _find_march_continue(self, cfg: dict):
        """只在右下角按钮区寻找完整“行军”按钮，避免命中自动行军横幅。"""
        march_cfg = cfg.get("march_continue_button", {})
        roi = roi_4to4(*march_cfg.get("roi", [990, 510, 1280, 720]))
        template = (march_cfg["template"] if "template" in march_cfg
                    else self._sortie_template("battle/march"))
        return self.device.template_match(template, roi)
