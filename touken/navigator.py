# -*- coding: utf-8 -*-
"""
中层：通用导航与识别机械
根据配置在游戏里"走路"：识别元素、点开目录、跳转界面、处理弹窗。
不知道什么是联队战、什么是领奖励——那是上层 flows/ 的事。
（从 touken_agent_engine_v2.py 原样搬家，逻辑未改）
"""

import time
from typing import Optional

from .device_adapter import Point, Region, roi_4to4


class NavigationMixin:
    """
    通用识别 + 导航 + 弹窗处理
    依赖宿主类提供：self.config、self.device、self.current_location
    """

    # ==================== 通用识别方法 ====================

    def _recognize(self, method_config: dict) -> Optional[Point]:
        """
        通用识别：根据配置选择 OCR 或 TemplateMatch

        Args:
            method_config: 来自配置文件的识别配置

        Returns:
            识别成功返回中心坐标，失败返回 None
        """
        method_type = method_config.get("type", "TemplateMatch")

        # 把 [x1,y1,x2,y2] 转成 Region(x,y,w,h)
        if "roi" in method_config:
            roi_raw = method_config["roi"]
            if len(roi_raw) == 4:
                roi = roi_4to4(roi_raw[0], roi_raw[1], roi_raw[2], roi_raw[3])
            else:
                roi = Region(*roi_raw)
        else:
            roi = None

        if method_type == "OCR":
            return self.device.ocr(
                expected=method_config["expected"],
                roi=roi,
                match_mode=method_config.get("match_mode", "contains")
            )
        elif method_type == "TemplateMatch":
            threshold = method_config.get("threshold", 0.7)
            # 模板匹配默认全屏搜索，除非配置明确指定了 roi
            # 因为模板图片本身就是精确匹配，不需要额外限制区域
            if "roi" in method_config:
                tm_roi = roi
            else:
                tm_roi = None  # 全屏
            return self.device.template_match(
                template=method_config["template"],
                roi=tm_roi,
                threshold=threshold
            )
        return None

    def _try_with_fallback(self, config: dict) -> Optional[Point]:
        """
        尝试 primary 方法，失败后用 fallback
        """
        if "primary" in config:
            result = self._recognize(config["primary"])
            if result:
                return result

        if "fallback" in config:
            result = self._recognize(config["fallback"])
            if result:
                return result

        return None

    def _click_point(self, target: list) -> bool:
        """点击坐标点 [x, y]"""
        point = Point(target[0], target[1])
        return self.device.click(point)

    # ==================== 安全区跳过（公共层） ====================
    # 所有"点安全区跳动画/对话"都走这里，别在流程里自己写 for 循环了。
    # 默认安全点是右下角 (775,695)，全游戏通用；各流程配置里的 skip_tap 优先。

    DEFAULT_SKIP_POINT = (775, 695)

    # 黑屏转花加载的耐心上限（秒）：超过这个还在转，基本是断网/炸服
    LOADING_PATIENCE_S = 90.0

    def _is_loading(self) -> bool:
        """是否在黑屏转花加载中。旧版 FakeMaa 没这个能力时当作不在加载。"""
        probe = getattr(self.device, "looks_like_loading", None)
        return bool(probe and probe())

    def _skip_point(self, point=None) -> tuple:
        """安全点优先级：显式传 > 顶层配置 skip_tap > 全局默认"""
        if point is None:
            point = self.config.get("skip_tap") or self.DEFAULT_SKIP_POINT
        return (point[0], point[1])

    def skip_safe(self, times: int = 3, interval: float = 0.8, point=None):
        """点安全区跳动画/对话。多页对话一页一下，几页就点几下。"""
        pt = self._skip_point(point)
        for _ in range(max(1, int(times))):
            self._click_point(pt)
            time.sleep(interval)

    def wait_landmark_skipping(self, template: str = None,
                               ocr_expected: str = None, roi=None,
                               timeout_s: float = 60.0, stable_hits: int = 3,
                               skip_point=None, interval: float = 0.8,
                               tap_even_when_found: bool = False) -> bool:
        """
        边点安全区边等地标出现，地标要连续 stable_hits 次都在才算就绪。

        为什么要连续命中：渐入式剧情演出会"先露脸再被盖住"——异去的火车切
        进场说话前，"决定"按钮会先短暂可见，看一眼就动手会正好点进动画里
        卡死（2026-08-19 实测翻车）。每一轮没找到地标就点安全区，顺便把
        挡路的对话往前推。

        template / ocr_expected 至少给一个；roi 同时约束两者。
        tap_even_when_found=True 时找到地标也照点（仅限安全点被验证过
        安全的画面，比如异去章节页），推对话更快。
        """
        deadline = time.monotonic() + max(1.0, float(timeout_s))
        hits = 0
        need = max(1, int(stable_hits))
        while time.monotonic() < deadline:
            self.device.screenshot(force=True)
            found = False
            if template and self.device.template_match(template, roi=roi):
                found = True
            if not found and ocr_expected \
                    and self.device.ocr(expected=ocr_expected, roi=roi):
                found = True
            if found:
                hits += 1
                if hits >= need:
                    return True
                if not tap_even_when_found:
                    time.sleep(interval)
                    continue
            else:
                hits = 0
            self._click_point(self._skip_point(skip_point))
            time.sleep(interval)
        return False

    def _click_template_config(self, config: dict) -> bool:
        """根据模板配置点击"""
        if "target" in config:
            return self._click_point(config["target"])
        elif "template" in config:
            if "roi" in config:
                roi_raw = config["roi"]
                roi = roi_4to4(roi_raw[0], roi_raw[1], roi_raw[2], roi_raw[3])
            else:
                roi = None
            result = self.device.template_match(config["template"], roi)
            if result:
                return self.device.click(result)
            return False
        return False

    # ==================== 导航流程 ====================

    def _open_menu(self, max_attempts: int = 10, settle_seen: list = None) -> bool:
        """
        循环点击目录按钮，直到菜单展开

        Args:
            max_attempts: 最大尝试次数
            settle_seen: 已记账远征结算屏的像素指纹列表（救援分支和主循环
                         共用，防止同一屏记两次）；None 时内部新建

        Returns:
            是否成功打开菜单
        """
        nav_config = self.config.get("navigation", {})
        common_config = nav_config.get("通用入口", {})

        # 如果菜单已经打开了，直接返回
        if self.current_location == "通用入口":
            return True

        attempt = 0
        loading_waits = 0
        if settle_seen is None:
            settle_seen = []
        # 加载等待按「次」数不按墙钟：固定 1.5s 一拍，次数上限 = 耐心/拍长。
        # （Windows 的 time.monotonic 粒度十几毫秒，测试里快转会永远不够耐心值）
        max_loading_waits = max(1, int(self.LOADING_PATIENCE_S / 1.5))
        while attempt < max_attempts:
            attempt += 1
            print(f"[NAV] 尝试打开目录 (第{attempt}次)")

            # 强制刷新截图
            self.device.screenshot(force=True)

            # 黑屏转花 = 游戏在加载（断网/切场景），点屏幕没意义，等它转完；
            # 加载不占尝试次数，但转太久就是断网/炸服了，别死等
            if self._is_loading():
                attempt -= 1
                loading_waits += 1
                if loading_waits > max_loading_waits:
                    print("[NAV] 加载转太久还没完，没能打开目录（断网/炸服？）")
                    return False
                print("[NAV] 游戏加载中（转樱花），等它转完...")
                time.sleep(1.5)
                continue
            loading_waits = 0

            # 检测菜单是否已展开：检测 ui目录.png
            if self.device.exists("menu/ui目录.png"):
                print("[NAV] 目录已展开")
                self.current_location = "通用入口"
                return True

            # 菜单没展开：
            # 1) 可能在演结算动画/对话（连目录按钮都看不见），点跳过点把动画往前推
            # 2) 也可能被收件箱这种全屏界面压住（15:00 实测卡死元凶）——先关 X
            if self.device.exists("目录.png", threshold=0.7):
                self._click_template_config(common_config)
            else:
                # 远征结算屏会盖住目录按钮：归来收益先照实记账再点过，
                # 不许盲点跳过把账点没了（收尾回本丸常在这里撞见归来结算）
                obs = None
                if hasattr(self, "observe_expedition_settlement"):
                    obs = self.observe_expedition_settlement(
                        via="open_menu", seen=settle_seen)
                if obs is not None:
                    if obs.get("new"):
                        print("[NAV] 远征结算屏挡路，照实记账后点过")
                    self.device.click(Point(993, 690))
                else:
                    close_pt = self.device.template_match("通用_关闭.png", threshold=0.7)
                    if close_pt:
                        print("[NAV] 发现全屏界面/弹窗，点关闭")
                        self.device.click(close_pt)
                    else:
                        self.device.click(Point(993, 690))
            time.sleep(0.8)

        print("[NAV] 目录始终未展开")
        return False

    def navigate_to(self, target: str, max_retries: int = 3) -> bool:
        """
        导航到指定界面

        Args:
            target: 目标界面名称，如 "本丸", "出阵", "远征"
            max_retries: 最大重试次数

        Returns:
            是否成功到达
        """
        nav_config = self.config.get("navigation", {})

        if target not in nav_config:
            print(f"[ERROR] 未知导航目标: {target}")
            return False

        target_config = nav_config[target]

        # 特殊处理：通用入口不是导航目标，而是动作（点击目录按钮）
        if target == "通用入口":
            print(f"[NAV] 点击通用入口（目录按钮）")
            self._click_template_config(target_config)
            time.sleep(0.5)
            self.current_location = "通用入口"
            return True

        # 1. 如果需要先打开通用入口（目录），循环点击直到展开
        if "from" in target_config:
            from_target = target_config["from"]
            if from_target == "通用入口" and self.current_location != "通用入口":
                if not self._open_menu():
                    return False

        # 2. 尝试进入目标界面
        # 给界面一点稳定时间，并强制刷新截图
        time.sleep(0.5)
        self.device.screenshot(force=True)
        for attempt in range(max_retries):
            print(f"[NAV] 尝试进入 {target} (第{attempt+1}次)")

            result = self._try_with_fallback(target_config)
            if result:
                self.device.click(result)

                # ===== 关键：等待界面切换完成 =====
                if "verify" in target_config:
                    verify_timeout = 10  # 最多等10秒
                    verify_start = time.time()
                    loading_waits = 0
                    max_loading_waits = max(1, int(self.LOADING_PATIENCE_S / 0.8))
                    while time.time() - verify_start < verify_timeout:
                        time.sleep(0.8)
                        # 强制刷新截图，确保识别的是最新画面
                        self.device.screenshot(force=True)
                        verify_result = self._recognize(target_config["verify"])
                        if verify_result:
                            self.current_location = target
                            print(f"[NAV] 成功到达: {target}")
                            return True
                        # 黑屏转花 = 还在加载，等它转完，不占验证预算
                        if self._is_loading():
                            loading_waits += 1
                            if loading_waits > max_loading_waits:
                                print(f"[NAV] 加载转太久还没完，没能到达 "
                                      f"{target}（断网/炸服？）")
                                return False
                            verify_start += 0.8
                            print("[NAV] 游戏加载中（转樱花），等它转完...")
                            continue
                        loading_waits = 0
                        print(f"[NAV] 等待 {target} 界面加载中...")

                    print(f"[NAV] 等待 {target} 加载超时，重试")
                else:
                    # 没有 verify，等固定时间后认为成功
                    time.sleep(2.0)
                    self.current_location = target
                    print(f"[NAV] 成功到达: {target}（无验证）")
                    return True

            time.sleep(1)

        print(f"[NAV] 进入 {target} 失败")
        return False

    def navigate_to_stream(self, target: str, max_retries: int = 3):
        """
        流式导航到指定界面，每步 yield 状态消息

        Yields:
            str: 执行状态消息
        """
        nav_config = self.config.get("navigation", {})

        if target not in nav_config:
            yield f"[ERROR] 未知导航目标: {target}"
            return

        target_config = nav_config[target]

        # 特殊处理：通用入口
        if target == "通用入口":
            yield f"[NAV] 点击通用入口（目录按钮）"
            self._click_template_config(target_config)
            time.sleep(0.5)
            self.current_location = "通用入口"
            yield f"[NAV] 目录已点击"
            return

        # 1. 如果需要先打开通用入口（目录），循环点击直到展开
        if "from" in target_config:
            from_target = target_config["from"]
            if from_target == "通用入口" and self.current_location != "通用入口":
                yield f"[NAV] 需要先打开目录"
                if not self._open_menu():
                    yield f"[NAV] 目录始终未展开，导航失败"
                    return
                yield f"[NAV] 目录已展开"

        # 2. 尝试进入目标界面
        yield f"[NAV] 给界面一点稳定时间..."
        time.sleep(0.5)
        self.device.screenshot(force=True)

        for attempt in range(max_retries):
            yield f"[NAV] 尝试进入 {target} (第{attempt+1}次)..."

            result = self._try_with_fallback(target_config)
            if result:
                self.device.click(result)

                # 验证循环
                if "verify" in target_config:
                    verify_timeout = 10
                    verify_start = time.time()
                    loading_waits = 0
                    # 次数制耐心（0.8s 一拍），理由同 _open_menu
                    max_loading_waits = max(1, int(self.LOADING_PATIENCE_S / 0.8))
                    while time.time() - verify_start < verify_timeout:
                        time.sleep(0.8)
                        self.device.screenshot(force=True)
                        verify_result = self._recognize(target_config["verify"])
                        if verify_result:
                            self.current_location = target
                            yield f"[NAV] 成功到达: {target}"
                            return
                        # 黑屏转花 = 游戏还在加载，等它转完；加载时间不占
                        # 验证预算，但转太久就是断网/炸服，如实报失败
                        if self._is_loading():
                            loading_waits += 1
                            if loading_waits > max_loading_waits:
                                yield (f"[NAV] 加载转太久还没完，没能到达 "
                                       f"{target}（断网/炸服？）")
                                return
                            verify_start += 0.8
                            yield "[NAV] 游戏加载中（转樱花），等它转完..."
                            continue
                        loading_waits = 0
                        yield f"[NAV] 等待 {target} 界面加载中..."

                    yield f"[NAV] 等待 {target} 加载超时，重试..."

                    # 救援：可能是弹窗/余波动画挡路（15:00 实测锻刀导航卡循环的教训）。
                    # 有关闭按钮就点掉，没有就点跳过点推一把，再进下一轮重试。
                    self.device.screenshot(force=True)
                    close_pt = self.device.template_match("通用_关闭.png", threshold=0.7)
                    if close_pt:
                        yield "[NAV] 救援：发现弹窗，点关闭"
                        self.device.click(close_pt)
                        time.sleep(1.0)
                    elif not self.device.exists("menu/ui目录.png"):
                        # 菜单都不见了：多半被动画/结算顶走了。远征结算屏先记账
                        # 再推，seen 传给 _open_menu 防止同一屏记两次
                        rescue_seen = []
                        if hasattr(self, "observe_expedition_settlement"):
                            obs = self.observe_expedition_settlement(
                                via="open_menu", seen=rescue_seen)
                            if obs is not None and obs.get("new"):
                                yield "[NAV] 救援：远征结算屏挡路，照实记账后点过"
                        yield "[NAV] 救援：界面被顶走，点跳过点推一把"
                        self.device.click(Point(993, 690))
                        time.sleep(1.0)
                        self.current_location = None
                        if not self._open_menu(settle_seen=rescue_seen):
                            yield "[NAV] 救援后目录仍打不开，导航失败"
                            return
                else:
                    time.sleep(2.0)
                    self.current_location = target
                    yield f"[NAV] 成功到达: {target}（无验证）"
                    return

            time.sleep(1)

        yield f"[NAV] 进入 {target} 失败"

    # ==================== 弹窗处理 ====================

    def handle_popup(self, popup_type: str) -> bool:
        """
        处理确认弹窗

        Args:
            popup_type: 弹窗类型，如 "联队战道具", "远征确认", "内番确认"

        Returns:
            是否处理成功
        """
        popup_config = self.config.get("popup", {}).get(popup_type)
        if not popup_config:
            print(f"[ERROR] 未知弹窗类型: {popup_type}")
            return False

        # 联队战道具特殊处理
        if popup_type == "联队战道具":
            # 使用三倍枡
            triple = popup_config["triple"]
            self._click_point(triple["target"])
            time.sleep(0.3)

            # 检查是否勾选成功
            check_roi_raw = triple["check_roi"]
            check_roi = roi_4to4(check_roi_raw[0], check_roi_raw[1],
                                 check_roi_raw[2], check_roi_raw[3])
            if not self.device.exists(popup_config["check_template"], check_roi):
                print("[POPUP] 三倍枡可能已用完")

        # 点击确定
        if "confirm" in popup_config:
            self._click_template_config(popup_config["confirm"])

        return True
