# -*- coding: utf-8 -*-
"""
上层业务：领取类——万屋暖心礼包、任务奖励（日常/月常/活动）

暖心礼包是国服独占运营福利（日服万屋没有），整条留在本文件不通用化。

任务奖励拆分（docs/dual-server-pipeline.md 阶段 3）：公共循环
task_rewards_loop.py（编排与领取确认仲裁，与服务器无关），国服画面
适配与数据通道 task_rewards_cn.py，本类组合两层，对外方法面不变。
"""

import time

from ..device_adapter import roi_4to4
from ..game_state import begin_operation, confirm_operation
from .task_rewards_loop import TaskRewardsLoopMixin
from .task_rewards_cn import CnTaskRewardsViewMixin


class RewardsMixin(CnTaskRewardsViewMixin, TaskRewardsLoopMixin):
    """领取类流程。依赖宿主类的 navigate_to_stream、_click_point、_click_template_config。"""

    # ==================== 万屋领取 ====================

    def claim_free_gift(self) -> bool:
        """同步版本：领取暖心礼包，返回是否成功"""
        for msg in self.claim_free_gift_stream():
            print(msg)
        return "领取成功" in msg if 'msg' in dir() else False

    def claim_free_gift_stream(self):
        """
        流式版本：领取暖心礼包，每步 yield 状态消息

        Yields:
            str: 执行状态消息，前端可实时显示
        """
        shop_config = self.config.get("shop", {}).get("free_gift")
        if not shop_config:
            yield "[SHOP] 未配置暖心礼包"
            return

        # 1. 导航到万屋
        yield "[SHOP] 正在导航到万屋..."
        for nav_msg in self.navigate_to_stream("万屋"):
            yield nav_msg
        if self.current_location != "万屋":
            yield "[SHOP] 到达万屋失败"
            return

        # 2. 识别"暖心"文字
        yield "[SHOP] 寻找暖心礼包..."
        find_config = shop_config["find_text"]
        raw_rois = find_config.get("rois")
        if not raw_rois:
            # 兼容旧配置，同时把右上角活动礼包位置纳入搜索；左侧 ROI
            # 不能删，常驻礼包仍可能出现在左上或左下。
            raw_rois = [
                find_config.get("roi", [0, 100, 700, 650]),
                [700, 100, 1280, 370],
            ]
        rois = [roi_4to4(*raw_roi) for raw_roi in raw_rois]

        # 强制刷新截图
        self.device.screenshot(force=True)

        # OCR 依次扫描左侧和右上角；找到哪个位置，后面的领取 ROI 就跟着哪个卡片走。
        result = None
        for roi in rois:
            result = self.device.ocr(
                expected=find_config["expected"],
                roi=roi,
                match_mode=find_config.get("match_mode", "contains")
            )
            if result:
                break

        if not result:
            yield "[SHOP] 未找到暖心礼包，可能已经领过了或界面不对"
            return

        yield f"[SHOP] 找到暖心礼包 at ({result.x}, {result.y})"

        # 3. 点击领取按钮
        claim_config = shop_config["claim_button"]
        # 万屋商品会随限时礼包增减而重新排版，不能用旧的固定纵坐标找“领取”。
        # 以刚识别到的“暖心”标题为锚，只框住它所在的商品卡；范围在
        # 下一行开始前截止，绝不能误点其它商品。左右两列都按标题位置计算。
        card_left = max(0, result.x - 180)
        card_top = max(90, result.y - 55)
        card_right = min(1280, card_left + 680)
        card_bottom = min(720, card_top + 270)
        # “领取/售罄”只可能出现在商品卡右下方的按钮条。
        claim_roi = roi_4to4(
            card_left + 350, card_top + 175, card_right, card_bottom)

        # 售罄是“今天已经领过”的明确成功状态，不要再去点旧兜底坐标。
        # 旧“售罄.png”是很小的灰底白字图，0.5 阈值在商品图片和边框上也会
        # 误命中。售罄属于文字状态，只接受按钮条内 OCR 的明确结果。
        if self.device.ocr("售罄", claim_roi, match_mode="contains"):
            yield "[SHOP] 今日暖心礼包已售罄，说明此前已经领取，跳过"
            return

        claim_result = self.device.template_match(
            template=claim_config["template"],
            roi=claim_roi,
            threshold=0.75
        )

        if claim_result:
            yield f"[SHOP] 点击领取按钮 at ({claim_result.x}, {claim_result.y})"
            self.device.click(claim_result)
        else:
            # 模板更新不及时还可以用文字兜底；两者都失败就不盲点。
            claim_result = self.device.ocr("领取", claim_roi)
            if not claim_result:
                yield "[SHOP] 暖心礼包未售罄，但未识别到领取按钮，本次未点击"
                return
            yield f"[SHOP] 通过文字找到领取按钮 at ({claim_result.x}, {claim_result.y})"
            self.device.click(claim_result)

        yield "[SHOP] 等待弹窗出现..."
        time.sleep(1.5)

        # 4. 验证弹窗：确认价格0
        yield "[SHOP] 验证弹窗价格..."
        verify_config = shop_config["popup_verify"]
        verify_roi_raw = verify_config["roi"]
        verify_roi = roi_4to4(verify_roi_raw[0], verify_roi_raw[1], verify_roi_raw[2], verify_roi_raw[3])

        # 循环等待弹窗出现
        for _ in range(10):
            self.device.screenshot(force=True)
            verify_result = self.device.ocr(
                expected=verify_config["expected"],
                roi=verify_roi,
                match_mode=verify_config.get("match_mode", "contains")
            )
            if verify_result:
                yield "[SHOP] 确认价格0，安全"
                break
            time.sleep(0.5)
        else:
            yield "[SHOP] 未检测到0价格弹窗，可能不是免费礼包，取消"
            # 尝试关闭弹窗
            close_config = shop_config.get("popup_close")
            if close_config:
                self._click_template_config(close_config)
            return

        # 5. 点击弹窗购买按钮（状态驱动：点完必须看到0价标签消失才算买成）
        yield "[SHOP] 点击购买按钮..."
        buy_config = shop_config["popup_buy"]
        buy_roi_raw = buy_config.get("roi", [0, 0, 1280, 720])
        buy_roi = roi_4to4(buy_roi_raw[0], buy_roi_raw[1], buy_roi_raw[2], buy_roi_raw[3])

        boundary = begin_operation(self.device)
        bought = False
        for _ in range(3):
            buy_result = self.device.template_match(
                template=buy_config["template"],
                roi=buy_roi,
                threshold=0.5
            )
            if not buy_result:
                # 模板认不出用文字兜底；都认不出绝不盲点固定坐标
                # （2026-09-07 起禁用盲点：点位飘了会买错东西，花的是真小判）
                buy_result = self.device.ocr("购买", buy_roi)
                if buy_result:
                    yield (f"[SHOP] 通过文字找到购买按钮 at "
                           f"({buy_result.x}, {buy_result.y})")
            if not buy_result:
                break
            self.device.click(buy_result)
            yield f"[SHOP] 点击购买 at ({buy_result.x}, {buy_result.y})"
            time.sleep(0.5)
            outcome = confirm_operation(self.device, boundary, "free_gift")
            if outcome is not None:
                bought = outcome
                break
            # 0价标签连续两帧消失 = 购买被受理（单帧 OCR 抖一下不算数）
            for _ in range(8):
                time.sleep(0.5)
                self.device.screenshot(force=True)
                if self.device.ocr(expected=verify_config["expected"],
                                roi=verify_roi,
                                match_mode=verify_config.get("match_mode",
                                                             "contains")):
                    continue
                self.device.screenshot(force=True)
                if not self.device.ocr(
                        expected=verify_config["expected"], roi=verify_roi,
                        match_mode=verify_config.get("match_mode",
                                                     "contains")):
                    bought = True
                    break
            if bought:
                break
            yield "[SHOP] 购买弹窗还在，补点一次"

        if not bought:
            yield "[SHOP] 购买按钮没识别到或点了没反应，本次未购买"
            # 弹窗别留给下一个流程
            close_config = shop_config.get("popup_close")
            if close_config:
                self._click_template_config(close_config)
            return

        # 6. 关闭可能的后续弹窗
        close_config = shop_config.get("popup_close")
        if close_config:
            for _ in range(3):
                self.device.screenshot(force=True)
                if self.device.exists(close_config.get("template")):
                    self._click_template_config(close_config)
                    time.sleep(0.5)
                else:
                    break

        yield "[SHOP] 领取成功！"
        return
