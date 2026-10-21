# -*- coding: utf-8 -*-
"""
上层业务：合成（日课一次，白名单喂刀）

规矩（用户亲授）：
  1. 本体直接点第一把带"选择"按钮的刀（远征/修行的刀没按钮，天然跳过）
  2. 素材只喂白名单里的不稀有刀（跟刀解同一份名单）
  3. 素材界面先取消勾选"显示保护中的刀剑男士"，锁刀直接不显示
  4. 选中素材 → 合成（灰的不点）→ 二次确认 → 动画不管它自己关

坐标（真机校准）：
  本体/素材列表行心 y [166,268,369,470,572,666]，行距 101
  本体选择按钮 x≈1220；素材选择按钮 x≈1068
  素材名字 OCR roi x[420,540] y[cy+10,cy+55]
  保护勾选框 (140,100)，红✓检测区 x[110,135] y[85,110]
  合成按钮模板区 x[1130,1275] y[560,700]；确认弹窗 确认 (785,630)
"""

import time

from ..device_adapter import roi_4to4, Point
from .. import sword_db
from .smith import DISMANTLE_WHITELIST

_ROW_CY = [166, 268, 369, 470, 572, 666]


class SynthesizeMixin:
    """合成。依赖宿主类的 navigate_to_stream、device。"""

    def synthesize_stream(self, dry_run: bool = False):
        """
        流式合成：第一把本体 + 一把白名单素材，做掉日课

        Yields:
            str: 执行状态消息
        """
        yield "[合成] 正在导航到强化..."
        for nav_msg in self.navigate_to_stream("强化"):
            yield nav_msg
        if self.current_location != "强化":
            yield "[合成] 到达强化失败"
            return
        time.sleep(1.5)

        # ---- 选本体：第一把带选择按钮的 ----
        self.device.screenshot(force=True)
        base_pt = None
        for cy in _ROW_CY:
            base_pt = self.device.template_match(
                "选择png.png", roi_4to4(1150, cy - 45, 1275, cy + 45), threshold=0.75)
            if base_pt:
                break
        if not base_pt:
            yield "[合成] 没找到能选的本体，放弃"
            return
        self.device.click(base_pt)
        time.sleep(2.0)

        # ---- 素材界面：取消勾选"显示保护中的刀剑男士" ----
        self.device.screenshot(force=True)
        if not self.device.ocr("一键选择", roi_4to4(1130, 400, 1275, 470)):
            yield "[合成] 没进到素材选择界面，放弃"
            return
        if self._protect_filter_on():
            self.device.click(Point(140, 100))
            time.sleep(1.5)
            self.device.screenshot(force=True)

        # ---- 找一把白名单素材 ----
        whitelist_ids = set()
        for zh in DISMANTLE_WHITELIST:
            r = sword_db.find_by_name(zh)
            if r:
                whitelist_ids.add(r[0])

        hit = None
        for cy in _ROW_CY:
            tokens = self.device.ocr_all(roi_4to4(420, cy + 10, 540, cy + 55))
            raw = max((t for t, _ in tokens), key=len, default="")
            if len(raw) < 2:
                continue
            found = sword_db.find_by_name(raw)
            if not found or found[0] not in whitelist_ids:
                continue
            btn = self.device.template_match(
                "选择png.png", roi_4to4(1000, cy - 50, 1180, cy + 50), threshold=0.75)
            if btn:
                hit = (found[1].get("name_zh") or found[1]["name"], btn)
                break
        if not hit:
            yield "[合成] 当前页没有白名单素材，放弃（日课少一次合成）"
            return
        name, btn = hit
        yield f"[合成] 素材选中白名单: {name}"
        if dry_run:
            yield "[合成] （演习模式：不点合成）"
            return

        self.device.click(btn)
        time.sleep(1.5)

        # ---- 点合成（灰色配不上模板，天然防呆）----
        self.device.screenshot(force=True)
        go = self.device.template_match(
            "合成.png", roi_4to4(1130, 560, 1275, 700), threshold=0.7)
        if not go:
            yield "[合成] 合成按钮没亮（本体属性可能满了），放弃"
            return
        self.device.click(go)
        time.sleep(2.0)

        # ---- 二次确认 ----
        self.device.screenshot(force=True)
        if not self.device.ocr("是否确认", roi_4to4(350, 100, 930, 160)):
            yield "[合成] 确认弹窗没出现，放弃"
            return
        from ..game_state import begin_operation, confirm_operation
        boundary = begin_operation(self.device)
        self.device.click(Point(785, 630))

        # ---- 等动画自己演完，回到素材界面（认一键选择按钮，跟炼糖同款判据）----
        for _ in range(15):
            time.sleep(2.0)
            self.device.screenshot(force=True)
            if self.device.template_match("一键选择.png", threshold=0.75):
                break
        outcome = confirm_operation(self.device, boundary, "compose")
        if outcome is True:
            yield f"[合成] 合成完成: {name} 已喂"
        elif outcome is False:
            yield "[合成] 游戏拒绝本次合成，本次停止"
        else:
            yield "[合成] 未确认合成结果，本次停止，不再重复消耗刀剑"

    def _protect_filter_on(self) -> bool:
        """检测"显示保护中的刀剑男士"是否勾着（红✓）"""
        img = self.device.screenshot()
        if img is None:
            return False
        region = img[85:110, 110:135]  # BGR
        red = ((region[..., 2] > 140) & (region[..., 1] < 110) & (region[..., 0] < 110)).sum()
        return red > 10
