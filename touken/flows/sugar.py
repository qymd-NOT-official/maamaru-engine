# -*- coding: utf-8 -*-
"""
上层业务：炼糖 = 收件箱清狗粮 + 习合循环（按需工具，不是日课）

总流程（用户亲授预期）：
  收件 → 习合一直喂，喂到没人 → 再收件 → 回来接着喂 → 邮件里没刀了收工。
  所持满了领不动：喂一轮腾位置再试；腾不出位置（没重刀）就收工。

收件箱：
  筛选 → 物品种类 → 全刀剑 → 确定 → 一键领取
  → 奖励确认窗关 X → 可能蹦购买详情（劝氪金）关 X
习合：
  强化 → 习合标签 → 逐行选择符合条件的本体 → 一键选择 → 习合 → 二次确认 → 跳动画
  → 黄锁本体持续喂；未锁本体每次喂后返回列表，乱舞达到 7 级就跳过。
保护（黄锁）的刀只是当不了素材，游戏原生防误喂；本体该吃吃，
黄锁本体不设乱舞上限；未锁本体仅选乱舞低于 7 级的刀，不按名字排除。

坐标（真机校准）：
  收件箱：筛选 (990,126) 物品种类 (1011,305) 全刀剑 (803,380) 确定 (640,625)
          一键领取 (1180,620) 奖励确认X (1144,48) 购买详情X (972,154) 收件箱X (1248,32)
  习合：标签模板 习合.png；本体行心 y [166,268,369,470,572,666]，选择 x≈1220
        隐藏无法习合的刀剑 (142,102)，红勾区域 x[133,151] y[93,112]
        （2026-10-15 MAAAdapter 运行截图校准；进入本体列表后确认开启）
        本体名字 OCR roi x[160,290] y[cy+12,cy+50]
        一键选择 (1203,481) 习合开始模板 习合开始.png（蓝才命中）
        确认弹窗 确认 (785,630)；返回箭头 (141,25)；跳动画安全点 (290,550)
"""

import time

from ..maa_adapter import roi_4to4, Point
from ..flow_control import FlowAborted

_MAX_NO_PROGRESS = 3  # 连续三次未完成习合才停止；成功一次即清零


class SugarMixin:
    """炼糖。依赖宿主类的 navigate_to_stream、maa。"""

    def sugar_stream(self, dry_run: bool = False):
        """
        流式炼糖（按需工具，不是日课）：
        收件 → 习合喂到没人 → 再收件 → 再喂……直到邮件里没刀了收工。
        所持满了领不动也不死循环：喂过一轮（消耗重刀腾位置）再试一次，
        连续三圈未完成习合才停止；有进展则继续。

        Yields:
            str: 执行状态消息
        """
        cycle = 0
        stalled = 0
        while True:
            cycle += 1
            yield f"[炼糖] ===== 第 {cycle} 圈 ====="
            inbox = yield from self._inbox_claim_stream(dry_run)
            fed = yield from self._shugo_loop_stream(dry_run, skip_unlocked_seven=True)
            yield f"[炼糖] 第 {cycle} 圈结算：收件={inbox} 喂了={fed} 轮"
            if dry_run:
                yield "[炼糖] 演习模式只跑一圈，收工"
                return
            if inbox == "empty":
                yield "[炼糖] 邮件里没刀了，收工"
                return
            stalled = stalled + 1 if fed == 0 else 0
            if stalled >= _MAX_NO_PROGRESS:
                yield "[炼糖] 持续没有进展：连续 3 圈未完成习合，停止操作，邮箱未确认清完"
                return
            if fed == 0:
                yield f"[炼糖] 本圈未完成习合，重新收件检查（连续 {stalled} 圈）"

    # ==================== 收件箱 ====================

    # 筛选面板按钮坐标（真机校准；面板里按钮大，点位取中心即可）
    _FILTER_OPEN = (990, 126)      # 收件箱列表页「筛选」
    _FILTER_CLEAR = (803, 157)     # 面板内「取消筛选」
    _FILTER_SWORDS = (803, 380)    # 「全刀剑」
    _FILTER_SUPPLIES = [           # 杂物四项：资源/货币/便利道具/其他物品
        (275, 455), (620, 455), (795, 455), (275, 530)]
    _FILTER_SORT_BY_KIND = (1011, 305)  # 右侧排序栏「物品种类」
    _FILTER_OK = (640, 625)        # 「确定」

    def _apply_inbox_filter(self, mode: str):
        """打开筛选面板并选过滤项。
        mode: "swords"（全刀剑，炼糖用）/ "supplies"（杂物四项）。
        supplies 先点「取消筛选」清场再点选，进来什么状态都不挑。"""
        self.maa.click(Point(*self._FILTER_OPEN))
        time.sleep(1.0)
        if mode == "supplies":
            self.maa.click(Point(*self._FILTER_CLEAR))
            time.sleep(0.6)
            for x, y in self._FILTER_SUPPLIES:
                self.maa.click(Point(x, y))
                time.sleep(0.5)
        else:
            self.maa.click(Point(*self._FILTER_SORT_BY_KIND))
            time.sleep(0.8)
            self.maa.click(Point(*self._FILTER_SWORDS))
            time.sleep(0.8)
        self.maa.click(Point(*self._FILTER_OK))
        time.sleep(1.5)

    def _inbox_claim_stream(self, dry_run: bool, filter_mode: str = "swords"):
        """
        收件箱清一波。
        filter_mode: "swords" 收全刀剑（炼糖默认）/ "supplies" 收杂物。
        Returns: "claimed" 领到了 / "blocked" 所持满领不动 / "empty" 没刀 / "failed" 导航失败 / "dry" 演习
        """
        yield "[炼糖·收件箱] 正在导航到收件箱..."
        for nav_msg in self.navigate_to_stream("收件箱"):
            yield nav_msg
        if self.current_location != "收件箱":
            yield "[炼糖·收件箱] 到达收件箱失败"
            return "failed"
        time.sleep(1.5)

        if dry_run:
            yield "[炼糖·收件箱] （演习模式：不点领取）"
            return "dry"

        # 筛选（收刀还是收杂物看 filter_mode）→ 确定
        from ..game_state import begin_operation, confirm_operation
        boundary = begin_operation(self.maa)
        self._apply_inbox_filter(filter_mode)
        listed = confirm_operation(self.maa, boundary, "inbox_list", timeout=2.0)
        selected = boundary.get("result_filter", "")
        matching_filter = selected == "2" if filter_mode == "swords" else bool(selected) and "2" not in selected.split(",")
        entries = boundary.get("result_body", {}).get("receive") if listed is True and matching_filter else None
        if entries is not None and not entries:
            yield "[炼糖·收件箱] 当前筛选下没有待领邮件，收工"
            self.maa.click(Point(1248, 32))
            time.sleep(1.0)
            return "empty"

        self.maa.screenshot(force=True)
        go = self.maa.template_match(
            "收件箱一键领取.png",
            roi_4to4(1080, 540, 1275, 700), threshold=0.7)
        if not go:
            if entries is None:
                raise FlowAborted("收件箱：未确认邮件为空，也没找到可领取按钮，停止操作")
            yield "[炼糖·收件箱] 还有待领邮件，但当前无法领取"
            self.maa.click(Point(1248, 32))  # 收箱子
            time.sleep(1.0)
            return "blocked"
        claim_boundary = begin_operation(self.maa)
        self.maa.click(go)
        time.sleep(2.5)

        result = None

        # 奖励确认窗 → 关 X
        self.maa.screenshot(force=True)
        if self.maa.ocr("奖励确认", roi_4to4(500, 40, 780, 100)):
            self.maa.click(Point(1144, 48))
            time.sleep(1.5)
            yield "[炼糖·收件箱] 领了一批，奖励确认已关"
            result = "claimed"

        # 可能蹦购买详情（劝氪金）→ 关 X
        self.maa.screenshot(force=True)
        if self.maa.ocr("购买详情", roi_4to4(500, 140, 780, 200)):
            self.maa.click(Point(972, 154))
            time.sleep(1.5)
            yield "[炼糖·收件箱] 所持满了蹦氪金窗，已关（领不动的先躺箱里）"
            result = "blocked"

        if result is None:
            claimed = confirm_operation(self.maa, claim_boundary, "inbox_claim", timeout=2.0)
            if claimed is True:
                yield "[炼糖·收件箱] 游戏已确认领取邮件"
                result = "claimed"
            else:
                raise FlowAborted("收件箱：点击领取后未确认领到邮件，停止操作，不再反复往返")

        # 收箱子
        self.maa.click(Point(1248, 32))
        time.sleep(1.0)
        return result

    def inbox_supplies_stream(self, dry_run: bool = False):
        """收件箱只收杂物（资源/货币/便利道具/其他物品），不动刀剑邮件。

        从炼糖里拎出来的独立小工具：导航到收件箱 → 筛选杂物四项 →
        一键领取 → 关弹窗 → 收工。
        """
        result = yield from self._inbox_claim_stream(dry_run,
                                                     filter_mode="supplies")
        if result == "claimed":
            yield "[收杂物] 杂物领完收工"
        elif result == "blocked":
            yield "[收杂物] 有东西领不动（上限满了），收工"
        elif result == "empty":
            yield "[收杂物] 没有能领的杂物，收工"
        elif result == "dry":
            yield "[收杂物] 演习完毕（没真点领取）"
        else:
            yield "[收杂物] 没到收件箱，收工"
        return result

    # ==================== 习合 ====================

    def _shugo_loop_stream(self, dry_run: bool, *, skip_unlocked_seven: bool = False):
        """
        炼糖开启七级跳过时，未锁本体每次喂后重新按乱舞等级选人。
        黄锁本体及其他调用方沿用持续习合当前本体的操作。
        Returns: 喂了几轮
        """
        yield "[炼糖·习合] 正在导航到强化..."
        for nav_msg in self.navigate_to_stream("强化"):
            yield nav_msg
        if self.current_location != "强化":
            raise FlowAborted("炼糖：到达强化失败，停止操作")
        time.sleep(1.5)

        tab = self.maa.template_match("习合.png", threshold=0.75)
        if not tab:
            raise FlowAborted("炼糖：没找到习合标签，停止操作")
        self.maa.click(tab)
        time.sleep(2.0)

        successes = 0
        in_materials = False
        body_locked = False
        stalled = 0
        while True:
            if stalled >= _MAX_NO_PROGRESS:
                raise FlowAborted("炼糖：连续 3 次重选都没能完成习合，停止操作，未确认喂完")
            self.maa.screenshot(force=True)
            if not in_materials:
                yield from self._sugar_ensure_filter_stream()
                body = yield from self._sugar_find_body_stream(skip_unlocked_seven=skip_unlocked_seven)
                if body is None:
                    yield ("[炼糖·习合] 本轮没有可继续喂的本体（未锁乱舞七级及以上已跳过）"
                           if skip_unlocked_seven else "[炼糖·习合] 本轮没有可继续喂的本体")
                    break
                btn, body_locked = body
                self.maa.click(btn)
                time.sleep(2.0)
                self.maa.screenshot(force=True)
                if not self._sugar_materials_visible():
                    # 可能被突然蹦出的弹窗挡了：能关就关，再确认一次
                    self._sugar_close_popup()
                    time.sleep(1.0)
                    self.maa.screenshot(force=True)
                    if not self._sugar_materials_visible():
                        yield "[炼糖·习合] 点了选择但没进素材界面，返回重选"
                        self.maa.click(Point(141, 25))
                        time.sleep(1.5)
                        stalled += 1
                        continue
                in_materials = True
                if dry_run:
                    yield "[炼糖·习合] （演习）已进入符合条件本体的素材界面，不动手"
                    self.maa.click(Point(141, 25))
                    return 0

            # 满乱舞后按钮会变灰，仍是素材页；返回后换新的第一行。
            select = self.maa.template_match("一键选择.png", threshold=0.75)
            if not select:
                self.maa.click(Point(141, 25))
                time.sleep(1.5)
                in_materials = False
                stalled += 1
                continue
            self.maa.click(select)
            time.sleep(1.5)
            self.maa.screenshot(force=True)
            go = self.maa.template_match("习合开始.png", threshold=0.7)
            if not go:
                self.maa.click(Point(141, 25))
                time.sleep(1.5)
                in_materials = False
                stalled += 1
                continue
            self.maa.click(go)
            # 确认弹窗偶尔慢半拍，或这一记点击被选人动画吞掉（2026-09-12 实测
            # 翻车点：弹窗没蹦出来，素材界面顶着"习合确认不在"被误判翻车）。
            # 连看三轮：没弹窗但蓝钮还亮着就补点，补出弹窗为止。
            confirmed = False
            for _ in range(3):
                time.sleep(2.0)
                self.maa.screenshot(force=True)
                if self.maa.ocr("是否确认", roi_4to4(350, 100, 930, 160)):
                    confirmed = True
                    break
                again = self.maa.template_match("习合开始.png", threshold=0.7)
                if again:
                    self.maa.click(again)
            if not confirmed:
                self.maa.screenshot(force=True)
                if self.maa.ocr("一键选择", roi_4to4(1120, 435, 1275, 525)):
                    # 还坐在素材界面上：弹窗压根没来，直接返回重选这个本体
                    yield "[炼糖·习合] 习合开始点了没反应（弹窗没蹦出来），返回重选"
                else:
                    # 蹦了别的窗（不是习合确认）：不替玩家按确认，能关就关
                    yield "[炼糖·习合] 没等到确认弹窗，关掉可能的弹窗后重选"
                    self._sugar_close_popup()
                self.maa.click(Point(141, 25))
                time.sleep(1.5)
                in_materials = False
                stalled += 1
                continue
            from ..game_state import begin_operation, confirm_operation
            boundary = begin_operation(self.maa)
            self.maa.click(Point(785, 630))

            outcome = confirm_operation(self.maa, boundary, "union", timeout=8.0)
            if outcome is not True:
                reason = ("未确认习合结果，停止操作，不再重复消耗素材"
                          if outcome is None else "游戏拒绝本次习合，停止操作")
                yield f"[炼糖·习合] {reason}"
                raise FlowAborted(f"炼糖：{reason}")
            stalled = 0
            successes += 1
            yield f"[炼糖·习合] 完成第 {successes} 轮习合"
            for _ in range(12):
                time.sleep(1.5)
                self.maa.screenshot(force=True)
                if self._sugar_materials_visible():
                    break
                self.maa.click(Point(290, 550))
            else:
                raise FlowAborted("炼糖：习合已完成，但回不到素材界面，停止操作，未确认喂完")
            if not body_locked:
                self.maa.click(Point(141, 25))
                time.sleep(1.5)
                in_materials = False

        yield f"[炼糖·习合] 收工：炼了 {successes} 轮"
        return successes

    @staticmethod
    def _sugar_row_locked(image, center_y) -> bool | None:
        """习合列表锁底色；2026-10-16 MAAAdapter 1280×720 BGR 运行帧校准。"""
        if image is None or image.shape != (720, 1280, 3):
            return None
        region = image[center_y - 35:center_y + 35, 85:94]
        gold = (region[..., 0] < 70) & (region[..., 1] > 130) & (region[..., 2] > 180)
        if gold.mean() > 0.6:
            return True
        gray = region.max(axis=2).astype("int16") - region.min(axis=2).astype("int16") < 15
        if gray.mean() > 0.8:
            return False
        return None

    def _sugar_find_body_stream(self, *, skip_unlocked_seven: bool = True):
        import numpy as np
        from .sword_inventory import parse_levels_cell

        while True:
            before = self.maa.screenshot(force=True)
            if before is None:
                raise FlowAborted("炼糖：本体列表截图失败，停止操作")
            self.maa.swipe(640, 240, 640, 600, 600)
            time.sleep(1.5)
            after = self.maa.screenshot(force=True)
            if after is None:
                raise FlowAborted("炼糖：翻页后截图失败，停止操作")
            if np.array_equal(before[125:690, 1258:1278], after[125:690, 1258:1278]):
                break
        unreadable = False
        while True:
            image = self.maa.screenshot(force=True)
            tokens = self.maa.ocr_all(roi_4to4(1180, 121, 1255, 690), image=image)
            buttons = [point for text, point in tokens
                       if "".join(text.split()) == "选择" and 164 <= point.y <= 645]
            if not buttons:
                for center_y in (166, 268, 369, 470, 572):
                    button = self.maa.template_match(
                        "选择png.png", roi_4to4(1150, center_y - 45, 1275, center_y + 45), threshold=0.75)
                    if button:
                        buttons.append(button)
            if not buttons:
                if self.maa.ocr("乱舞", roi_4to4(540, 120, 665, 690)):
                    raise FlowAborted("炼糖：列表仍有刀剑，但未识别到选择按钮，停止操作，未确认喂完")
                return None
            for row_no, button in enumerate(sorted(buttons, key=lambda point: point.y), 1):
                if not skip_unlocked_seven:
                    return button, True
                locked = self._sugar_row_locked(image, button.y)
                if locked is True:
                    return button, True
                level_roi = roi_4to4(540, button.y - 46, 665, button.y + 8)
                levels = parse_levels_cell(self.maa.ocr_all(level_roi, image=image))
                level = levels.get("tou_level")
                if locked is None or level is None:
                    # 滑动回弹没停稳就截图，顶部的行必糊（2026-10-19 第一行
                    # 稳定碎成渣）：等一拍重拍重读一次，还读不清才放弃这行
                    time.sleep(0.8)
                    retry_image = self.maa.screenshot(force=True)
                    retry_locked = self._sugar_row_locked(retry_image, button.y)
                    if retry_locked is True:
                        return button, True
                    retry_levels = parse_levels_cell(self.maa.ocr_all(
                        level_roi, image=retry_image))
                    if retry_locked is not None:
                        locked = retry_locked
                    if retry_levels.get("tou_level") is not None:
                        level = retry_levels["tou_level"]
                if locked is None or level is None:
                    unreadable = True
                    yield f"[炼糖·习合] 当前第 {row_no} 行锁定状态或乱舞等级未读清，先看下一行"
                    continue
                if level >= 7:
                    yield f"[炼糖·习合] 跳过当前第 {row_no} 行：未上黄锁，乱舞 {level} 级"
                    continue
                return button, False
            position = image[125:690, 1258:1278].copy()
            self.maa.swipe(640, 600, 640, 240, 600)
            time.sleep(1.5)
            after = self.maa.screenshot(force=True)
            if after is None:
                raise FlowAborted("炼糖：翻页后截图失败，停止操作")
            if np.array_equal(position, after[125:690, 1258:1278]):
                if unreadable:
                    raise FlowAborted("炼糖：部分本体乱舞等级或锁定状态未读清，未确认喂完")
                return None

    def _sugar_filter_checked(self) -> bool | None:
        """读取同源 BGR 运行帧里的红勾；截图或勾选框不明确时返回未知。"""
        image = self.maa.screenshot()
        if image is None or image.shape != (720, 1280, 3):
            return None
        region = image[93:112, 133:151]
        white = (region > 200).all(axis=2).sum()
        if white < region.shape[0] * region.shape[1] / 2:
            return None
        red = ((region[..., 2] > 140) & (region[..., 1] < 110) & (region[..., 0] < 110)).sum()
        return bool(red > 10)

    def _sugar_ensure_filter_stream(self):
        self.maa.screenshot(force=True)
        label = roi_4to4(155, 80, 405, 120)
        if not self.maa.ocr("隐藏无法习合的刀剑", label):
            raise FlowAborted("炼糖：未确认习合本体列表，停止操作")
        checked = self._sugar_filter_checked()
        if checked is None:
            raise FlowAborted("炼糖：未识别隐藏无法习合的刀剑勾选状态，停止操作")
        if checked:
            return
        self.maa.click(Point(142, 102))
        time.sleep(1.5)
        self.maa.screenshot(force=True)
        if (not self.maa.ocr("隐藏无法习合的刀剑", label)
                or self._sugar_filter_checked() is not True):
            raise FlowAborted("炼糖：未能开启隐藏无法习合的刀剑，停止操作")
        yield "[炼糖·习合] 已自动开启隐藏无法习合的刀剑"

    def _sugar_close_popup(self):
        """挡路的弹窗能认出 X 就点 X；认不出就点弹窗左下的取消位
        （实机校准 (497,628)，这游戏的弹窗规矩左钮=取消/关闭这类反义动作，
        确认类的右钮绝对不碰）。都没关掉就留给后续步骤自己判断。"""
        self.maa.screenshot(force=True)
        x = self.maa.template_match("通用_关闭.png", threshold=0.7)
        if x:
            self.maa.click(x)
        else:
            self.maa.click(Point(497, 628))
        time.sleep(1.0)

    def _sugar_materials_visible(self):
        # 文字可同时识别亮/灰按钮，不能用亮按钮判断满级后的返回状态。
        return bool(self.maa.ocr("一键选择", roi_4to4(1120, 435, 1275, 525)))
