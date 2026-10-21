# -*- coding: utf-8 -*-
"""
MuMu 模拟器自启动 —— ADB 连不上时把模拟器自己拉起来

MuMu 12 的命令行管家：nx_main/MuMuManager.exe
  启动实例:  MuMuManager.exe control -v 0 launch
  查状态:    MuMuManager.exe info -v all（is_android_started）

血泪实测两条：
  1. 冷启动到 ADB 可用要约 4 分钟（轮询上限给 6 分钟）
  2. 实例起来后必须 adb connect 一次设备才会出现，光等着没用
"""

import json
import subprocess
import time


def _run(cmd, timeout=30):
    # CREATE_NO_WINDOW：无控制台父进程（worker/打包exe）里裸起
    # adb.exe/MuMuManager.exe（控制台程序）会弹窗抢焦点，必须隐藏
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(cmd, capture_output=True, timeout=timeout,
                          encoding="utf-8", errors="replace", creationflags=flags)


def adb_alive(adb_path: str, address: str) -> bool:
    try:
        r = _run([adb_path, "-s", address, "shell", "echo", "ok"], timeout=15)
        return (r.stdout or "").strip() == "ok"
    except Exception:
        return False


def resolve_adb_address(adb_path: str, address: str, emit=print,
                        instance: int = 0) -> str:
    """配置的 ADB 地址连不上时找备胎设备。

    备胎按安全程度排序：
      1. MuMu 标准端口（16384 + 32×实例号）——配置端口打错一位也能自愈
         （2026-09-17 现场：16384 被手滑改成 16385，窗口模式下 emulator-5554
         不存在，备胎全军覆没，工作流对着错端口空等 3 分钟）
      2. adb devices 里已在册且恰好只有一台的 127.0.0.1:* 设备
      3. 无头运行的 MuMu（窗口被关但 VM 还活着）不开 16384，只暴露经典
         模拟器口 emulator-5554（2026-09-07 晚实测：窗口消失后 16384 拒连，
         工作流三趟全死在连接上）。仅当 adb devices 里恰好只有一台
         emulator-* 时才换线——有多台说明可能跑着别的模拟器，不乱认。
    """
    try:
        _run([adb_path, "connect", address], timeout=15)
        if adb_alive(adb_path, address):
            return address

        mumu_port = 16384 + 32 * int(instance or 0)
        candidate = f"127.0.0.1:{mumu_port}"
        if candidate != address:
            _run([adb_path, "connect", candidate], timeout=15)
            if adb_alive(adb_path, candidate):
                emit(f"[模拟器] {address} 连不上，但 MuMu 标准端口 {candidate} 通了，"
                     f"改走这条线（建议把配置里的端口改回 {mumu_port}）")
                return candidate

        r = _run([adb_path, "devices"], timeout=15)
        lines = [ln for ln in (r.stdout or "").splitlines()
                 if ln.strip().endswith("device")]
        local = [ln.split()[0] for ln in lines
                 if ln.startswith("127.0.0.1:") and ln.split()[0] != address]
        if len(local) == 1 and adb_alive(adb_path, local[0]):
            emit(f"[模拟器] {address} 连不上，发现已在册的设备 {local[0]}，改走这条线")
            return local[0]
        cands = [ln.split()[0] for ln in lines if ln.startswith("emulator-")]
        if len(cands) == 1:
            emit(f"[模拟器] {address} 连不上，发现无头模拟器 {cands[0]}，改走这条线")
            return cands[0]
    except Exception:
        pass
    return address  # 维持原地址，后面的自启动逻辑接着兜底


def ensure_emulator(adb_path: str, address: str, manager_path: str = None,
                    instance: int = 0, emit=print, max_wait_s: int = 360) -> bool:
    """确保模拟器在线：已经在跑秒回 True；没在跑就拉起来等开机"""
    if adb_alive(adb_path, address):
        return True
    # 备胎通道：配置端口打错了先试 MuMu 标准口；无头 MuMu 不开 16384，
    # 恰好一台 emulator-* 活着就算在线
    if resolve_adb_address(adb_path, address, emit=emit,
                           instance=instance) != address:
        return True
    if not manager_path:
        emit("[模拟器] ADB 连不上，也没配 MuMuManager 路径，没法自启动")
        return False

    emit(f"[模拟器] ADB 连不上，正在启动模拟器（实例 {instance}）...")
    try:
        r = _run([manager_path, "control", "-v", str(instance), "launch"], timeout=60)
        if r.returncode != 0 or '"errcode": 0' not in (r.stdout or ""):
            emit(f"[模拟器] 启动命令失败: {(r.stdout or r.stderr or '').strip()}")
            return False
    except Exception as exc:
        emit(f"[模拟器] 启动命令异常: {exc}")
        return False

    # 等安卓系统起来（实测约 4 分钟）
    deadline = time.time() + max_wait_s
    booted = False
    while time.time() < deadline:
        try:
            info = _run([manager_path, "info", "-v", "all"], timeout=30)
            d = json.loads(info.stdout or "{}")
            if d.get(str(instance), {}).get("is_android_started"):
                booted = True
                break
        except Exception:
            pass
        emit("[模拟器] 开机中，再等等...")
        time.sleep(10)
    if not booted:
        emit("[模拟器] 等了 6 分钟还没开完机，放弃")
        return False

    # adb connect（不 connect 设备不出现）+ 等系统 boot_completed
    for _ in range(24):
        _run([adb_path, "connect", address], timeout=15)
        if adb_alive(adb_path, address):
            try:
                r = _run([adb_path, "-s", address, "shell",
                          "getprop", "sys.boot_completed"], timeout=15)
                if (r.stdout or "").strip() == "1":
                    emit("[模拟器] 开机完毕，ADB 已连接 ✓")
                    return True
            except Exception:
                pass
        time.sleep(5)
    emit("[模拟器] 系统起了但 ADB/开机检查过不去，放弃")
    return False


def shutdown_emulator(manager_path: str, instance: int = 0, emit=print) -> bool:
    """关闭模拟器实例（跑完日课的'用完即走'可选项）"""
    try:
        r = _run([manager_path, "control", "-v", str(instance), "shutdown"],
                 timeout=60)
        ok = '"errcode": 0' in (r.stdout or "")
        if not ok:
            emit(f"[模拟器] 关闭命令失败: {(r.stdout or r.stderr or '').strip()}")
        return ok
    except Exception as exc:
        emit(f"[模拟器] 关闭异常: {exc}")
        return False


def sleep_computer(emit=print) -> bool:
    """电脑休眠（可选项！用户被休眠黑过屏，这功能必须手动勾才会触发）"""
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                         creationflags=flags)
        return True
    except Exception as exc:
        emit(f"[日课] 休眠命令失败: {exc}")
        return False
