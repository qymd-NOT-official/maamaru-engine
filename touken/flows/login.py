# -*- coding: utf-8 -*-
"""
上层业务：登录流程
（从 touken_agent_engine_v2.py 原样搬家，逻辑未改）
"""

import time

from ..device_adapter import roi_4to4


class LoginMixin:
    """登录流程。依赖宿主类的 _click_template_config（见 navigator.py）。"""

    UPDATE_TEXT_ROI = roi_4to4(300, 180, 980, 410)
    UPDATE_CONFIRM_ROI = roi_4to4(430, 400, 850, 560)
    UPDATE_LINE_ROI = roi_4to4(160, 240, 1120, 650)

    # 「网络请求超时,请重试!」弹窗：正文和按钮区域（弹窗居中，位置固定）
    NET_TIMEOUT_TEXT_ROI = roi_4to4(300, 180, 980, 420)
    NET_CONFIRM_ROI = roi_4to4(430, 400, 850, 560)
    # 「连接中断。是否要从中断处重新开始？」弹窗的【是】按钮（1280x720 固定坐标）
    NET_RESUME_YES_POINT = (505, 467)

    def _ocr_text(self, roi) -> str:
        """把 roi 里 OCR 出的所有文字拼成一串（判断弹窗正文用）"""
        results = self.device.ocr_all(roi)
        return "".join(str(item[0]) for item in results if item)

    def _network_timeout_visible(self) -> bool:
        """只在超时正文明确出现时才动手，避免误点普通「确定」。"""
        return "网络请求超时" in self._ocr_text(self.NET_TIMEOUT_TEXT_ROI)

    def _network_resume_visible(self) -> bool:
        """「连接中断…从中断处重新开始？」续打弹窗。"""
        text = self._ocr_text(self.NET_TIMEOUT_TEXT_ROI)
        return "中断" in text and "重新开始" in text

    def recover_network_stream(self):
        """处理运行途中的「网络请求超时」弹窗（MuMu 模拟器断网）。

        返回 False 表示当前没有超时弹窗；
        "resumed" 表示恢复成功且游戏回到了中断前的画面（调用方继续巡逻即可）；
        "home" 表示恢复成功但落在了本丸（原战斗状态作废，调用方必须从入口重来）；
        None 表示恢复失败，调用方应安全停止。

        原理：MuMu 断网时弹窗上的「确定」点了会原地复活（重试也连不上），
        必须重启模拟器。游戏重登后会问「是否从中断处重新开始」，点【是】
        就能接着断点继续打。单次任务最多重启 network.max_restarts 次，
        防止模拟器抽风时整夜反复重启。
        """
        if not self._network_timeout_visible():
            return False

        net_cfg = self.config.get("network", {})
        max_restarts = int(net_cfg.get("max_restarts", 2))
        restarts = getattr(self, "_net_restart_count", 0)

        yield "[断网] 检测到「网络请求超时」弹窗，先点确定试试水..."
        confirm = self.device.template_match(
            "通用_确定.png", roi=self.NET_CONFIRM_ROI, threshold=0.7)
        if not confirm:
            confirm = self.device.ocr("确定", self.NET_CONFIRM_ROI,
                                   match_mode="exact")
        if confirm:
            self.device.click(confirm)
            time.sleep(10.0)
            self.device.screenshot(force=True)
            if not self._network_timeout_visible():
                yield "[断网] 虚惊一场，点确定重连上了，继续"
                return "resumed"

        if restarts >= max_restarts:
            yield (f"[断网] ⚠️ 本次任务已经重启过 {restarts} 回模拟器了，"
                   "网络还在断，摆烂收工——天亮了你看看家里网咋回事")
            return None

        self._net_restart_count = restarts + 1
        if hasattr(self, "record_event"):
            self.record_event("network.outage_detected",
                              restart_count=self._net_restart_count)
        yield (f"[断网] 点了确定也没用，是模拟器网络断死，"
               f"开始重启模拟器（本次任务第 {self._net_restart_count}/{max_restarts} 回）...")

        from ..emulator import shutdown_emulator, ensure_emulator
        adb_path = self.config.get("adb_path")
        address = self.config.get("adb_address")
        manager = self.config.get("emulator_manager")
        instance = int(self.config.get("emulator_instance", 0))
        boot_wait = int(net_cfg.get("boot_wait_s", 360))
        if not manager:
            yield "[断网] 没配 emulator_manager，没法重启模拟器，停"
            return None

        msgs = []
        emit = msgs.append
        shutdown_emulator(manager, instance=instance, emit=emit)
        for m in msgs:
            yield f"[断网] {m}" if not str(m).startswith("[") else m
        msgs.clear()
        time.sleep(8.0)

        yield "[断网] 模拟器已关闭，重新开机（实测冷启动约 4 分钟）..."
        if not ensure_emulator(adb_path, address, manager_path=manager,
                               instance=instance, emit=emit,
                               max_wait_s=boot_wait):
            for m in msgs:
                yield str(m) if str(m).startswith("[") else f"[断网] {m}"
            yield "[断网] ⚠️ 模拟器重启失败，停"
            return None
        for m in msgs:
            yield str(m) if str(m).startswith("[") else f"[断网] {m}"
        yield "[断网] 模拟器回来了，启动游戏..."
        time.sleep(5.0)

        # 冷启动优先按包名直启；只有直启失败才走带 OCR 担保的图标回退。
        started = yield from self._ensure_game_started()
        if not started:
            yield "[断网] ⚠️ 没有确认游戏成功启动，停止恢复"
            return None
        self.login()
        time.sleep(3.0)

        # 登录后两种情况：弹出「从中断处重新开始」→ 点【是】续打；
        # 或者直接被清回本丸（中断数据没保住/当时不在出阵）。
        deadline = time.time() + 180
        while time.time() < deadline:
            self.device.screenshot(force=True)
            if self._network_resume_visible():
                yield "[断网] 游戏问要不要续打，点【是】从中断处继续"
                self._click_point(self.NET_RESUME_YES_POINT)
                time.sleep(3.0)
                self.device.screenshot(force=True)
                if not self._network_resume_visible():
                    yield "[断网] ✓ 续打成功，战斗画面接回来了"
                    if hasattr(self, "record_event"):
                        self.record_event("network.recovered", mode="resumed")
                    return "resumed"
                yield "[断网] 点了【是】弹窗还在，不再盲点，停"
                return None
            if self.device.exists("目录.png", threshold=0.7):
                yield "[断网] 重登后落在本丸，没有续打弹窗（原战斗状态作废）"
                self.current_location = "本丸"
                if hasattr(self, "record_event"):
                    self.record_event("network.recovered", mode="home")
                return "home"
            # 登录页可能迟到，看到了就再点一次
            login = self.device.template_match("登录.png", threshold=0.7)
            if login:
                self.device.click(login)
                time.sleep(2.0)
            time.sleep(3.0)

        yield "[断网] ⚠️ 重登后 3 分钟内既没续打弹窗也没回本丸，停"
        return None

    def _game_update_prompt_visible(self) -> bool:
        """只在更新正文明确出现时才允许点弹窗，避免把普通「确定」误点掉。"""
        results = self.device.ocr_all(self.UPDATE_TEXT_ROI)
        text = "".join(str(item[0]) for item in results if item)
        return "检测到更新" in text and ("重新启动" in text or "进行更新" in text)

    def recover_game_update_stream(self, timeout: int = 1800):
        """处理运行途中强制更新，并等到重新落回本丸。

        返回 True 表示确实处理了一次更新；False 表示当前并非更新弹窗；
        None 表示识别到了更新，但未能在限时内恢复。调用方在 True 后必须从
        玩法入口重来，不能沿用更新前的画面状态继续点击。
        """
        if not self._game_update_prompt_visible():
            return False

        if hasattr(self, "record_event"):
            self.record_event("game_update.detected")
        yield "[更新] 检测到游戏强制更新，暂停当前玩法"
        confirm = self.device.template_match(
            "通用_确定.png", roi=self.UPDATE_CONFIRM_ROI, threshold=0.7)
        if not confirm:
            confirm = self.device.ocr("确定", self.UPDATE_CONFIRM_ROI,
                                   match_mode="exact")
        if not confirm:
            yield "[更新] 看到了更新提示，但没能安全找到【确定】，停止点击"
            return None
        self.device.click(confirm)
        time.sleep(2.0)

        deadline = time.time() + max(60, int(timeout))
        last_notice = 0.0
        line_selected = False
        while time.time() < deadline:
            self.device.screenshot(force=True)

            # 更新完成后可能先回登录，也可能自动进入本丸。
            if (self.device.exists("目录.png", threshold=0.7)
                    and not self._game_update_prompt_visible()):
                yield "[更新] ✓ 游戏已更新并重新回到本丸"
                self.current_location = "本丸"
                if hasattr(self, "record_event"):
                    self.record_event("game_update.recovered")
                return True

            login = self.device.template_match("登录.png", threshold=0.7)
            if login:
                yield "[更新] 登录页已出现，点击登录"
                self.device.click(login)
                time.sleep(2.0)
                continue

            # 登录后若客户端需要下载资源，会要求选线路。优先线路一；线路一
            # 没识别到才用线路二。点击一次后只等待，避免下载页反复误触。
            if not line_selected:
                line = (self.device.ocr("线路一", self.UPDATE_LINE_ROI,
                                     match_mode="contains")
                        or self.device.ocr("线路二", self.UPDATE_LINE_ROI,
                                        match_mode="contains"))
                if line:
                    self.device.click(line)
                    line_selected = True
                    yield "[更新] 已选择更新线路，等待游戏下载资源（最长 30 分钟）"
                    time.sleep(3.0)
                    continue

            now = time.time()
            if now - last_notice >= 60:
                yield "[更新] 仍在等待更新/登录完成……"
                last_notice = now
            time.sleep(3.0)

        yield "[更新] 等待更新完成超时；当前任务已安全停止，请查看游戏画面"
        return None

    def login(self) -> bool:
        """执行登录流程"""
        login_steps = self.config.get("login", {})

        for step_name, step_config in login_steps.items():
            print(f"\n[LOGIN] 执行: {step_name}")

            if step_config.get("repeat") == "until_gone":
                # 上限 8 次：识别源冻帧时点一万次模板也不会消失（2026-09-17
                # 显存通道冻帧死循环现场），宁可留残局给扫地，不许死磕。
                for _ in range(8):
                    if "roi" in step_config:
                        roi_raw = step_config["roi"]
                        roi = roi_4to4(roi_raw[0], roi_raw[1], roi_raw[2], roi_raw[3])
                    else:
                        roi = None

                    if not self.device.exists(step_config["template"], roi):
                        break
                    self._click_template_config(step_config)
                    time.sleep(0.5)
                else:
                    print(f"[LOGIN] {step_name} 连点 8 次还在，疑似识别源冻帧，"
                          "跳过这步交给扫地")
            else:
                self._click_template_config(step_config)
                time.sleep(step_config.get("post_delay", 500) / 1000)

        print("[LOGIN] 登录完成")
        return True
