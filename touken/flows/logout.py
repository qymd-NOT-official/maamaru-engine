# -*- coding: utf-8 -*-
"""
上层业务：下线——打完收工（用户要的方舟MAA同款）

三段开关（配置 daily.logout）：
  kill_game:       adb force-stop 杀游戏进程（默认开，最干净的退法）
  close_emulator:  taskkill 关模拟器（默认关，进程名配置里改）
  sleep_pc:        休眠电脑（默认关！这是真休眠，别开着玩）

注意：杀了游戏 adb 连接就断了，所以这必须是日课的最后一步。
"""

import subprocess
import time

# 无控制台父进程（worker/打包exe）里裸起控制台程序会弹窗抢焦点，一律隐藏
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class LogoutMixin:
    """下线。依赖宿主类的 device。"""

    def logout_stream(self, kill_game: bool = None,
                      close_emulator: bool = None, sleep_pc: bool = None):
        """
        流式下线

        Args:
            kill_game/close_emulator/sleep_pc: 显式覆盖配置 daily.logout 的三段
                开关；None（缺省）回落到配置，行为与旧版完全一致。

        Yields:
            str: 执行状态消息
        """
        cfg = self.config.get("daily", {}).get("logout", {})
        if kill_game is None:
            kill_game = cfg.get("kill_game", True)
        if close_emulator is None:
            close_emulator = cfg.get("close_emulator")
        if sleep_pc is None:
            sleep_pc = cfg.get("sleep_pc")

        if kill_game:
            pkg = cfg.get("package", "com.youzu.djlw")
            try:
                subprocess.run(
                    [self.device.adb_path, "-s", self.device.adb_address,
                     "shell", "am", "force-stop", pkg],
                    timeout=15, capture_output=True, creationflags=_NO_WINDOW)
                yield f"[下线] 游戏进程已杀（{pkg}）"
            except Exception as exc:
                yield f"[下线] 杀游戏失败: {exc}"
            time.sleep(1.0)

        if close_emulator:
            for proc in cfg.get("emulator_processes", ["MuMuPlayer.exe"]):
                try:
                    subprocess.run(["taskkill", "/F", "/IM", proc],
                                   timeout=15, capture_output=True,
                                   creationflags=_NO_WINDOW)
                    yield f"[下线] 模拟器进程已关（{proc}）"
                except Exception as exc:
                    yield f"[下线] 关模拟器（{proc}）失败: {exc}"
            time.sleep(2.0)

        if sleep_pc:
            yield "[下线] 3 秒后休眠电脑..."
            time.sleep(3.0)
            try:
                subprocess.run(
                    ["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                    timeout=15, capture_output=True, creationflags=_NO_WINDOW)
            except Exception as exc:
                yield f"[下线] 休眠失败: {exc}"

        if not kill_game and not close_emulator and not sleep_pc:
            yield "[下线] 配置里三段全关，啥也没干"
