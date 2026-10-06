# -*- coding: utf-8 -*-
"""
脚本执行器 —— 子进程版（2026-08-05 重写，恶性卡死修复）

旧版在本进程线程里跑 generator：MAA 的 wait() 硬阻塞一旦卡死，
线程永久死锁，面板除了整体重启没救，日志戛然而止还找不到凶手。

现在每个脚本都是独立子进程（panel/worker.py）：
  - 消息流：子进程 stdout 每行一条 → 落 SQLite + SSE 广播 + 终端打印
  - 沉默看门狗：子进程 300 秒一行输出都没有 = 判定卡死，直接 kill，
    面板/调度器/机器人毫发无损（战斗循环心跳 ~10-20s 一条，300s 很宽）
  - 紧急停止 = 真杀进程，立刻停，不用等 generator 下次 yield
  - 看门狗线程只数秒数、只杀进程，绝不碰 MAA（§14.1 血泪红线）
"""

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Generator

from .log_store import get_store


# —— 可注册的脚本清单 ——
# 每个条目：(名字, 显示名, 描述, 调用函数, 参数表单schema)
# 调用函数签名：(config_path, params: dict) -> Generator[str, None, None]
# 注意：注册表在 panel/server.py 里填充，工人子进程 import server 也能拿到同一份。

_SCRIPTS: dict[str, dict] = {}

# 子进程多久一行输出都没有 = 判定卡死
SILENCE_TIMEOUT_SEC = 300

# 看门狗自己一轮睡了超过这个数 = 整台电脑刚休眠过，静默计时清零不冤枉工人
# （电脑睡着时工人和看门狗一起被冻，醒来墙上时钟跳一大截，不是工人卡死）
SUSPEND_GAP_SEC = 60

# 工人进程约定退出码：玩法安全中止 / MAA 连续识别超时后自我了断
EXIT_FLOW_ABORTED = 42
EXIT_MAA_DEAD = 43


def register_script(name: str, label: str, desc: str,
                    runner_fn: Callable[[str, dict], Generator[str, None, None]],
                    params: list | None = None, hidden: bool = False):
    _SCRIPTS[name] = {"label": label, "desc": desc, "fn": runner_fn,
                      "params": params or [], "hidden": hidden}


def list_scripts() -> dict:
    return {k: {"label": v["label"], "desc": v["desc"], "params": v["params"]}
            for k, v in _SCRIPTS.items() if not v.get("hidden")}


# —— 运行时状态 ——

class ScriptRunner:
    """在子进程里跑一个脚本，stdout 逐行回收"""

    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._current_run_id: str | None = None
        self._current_script: str | None = None
        self._current_workflow: dict | None = None
        self._workflow_snapshot: dict | None = None
        self._current_started: float | None = None  # 启动时间戳，仪表盘算已跑多久
        self._on_message = None  # callback(message_dict)
        self._last_output: float = 0.0   # 最后一次收到子进程输出的时间
        self._stop_reason: str = ""      # "user" / "watchdog" / ""
        self._stop_reasons: dict[str, str] = {}
        self._last_run_id: str | None = None
        self._last_run_status: str | None = None

    # noinspection PyAttributeOutsideInit
    def set_message_callback(self, cb):
        self._on_message = cb

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def current_script(self) -> str | None:
        return self._current_script

    @property
    def current_run_id(self) -> str | None:
        return self._current_run_id

    @property
    def last_run_result(self) -> tuple[str | None, str | None]:
        return self._last_run_id, self._last_run_status

    @property
    def current_started(self) -> float | None:
        """本轮启动时间戳（没在跑就是 None）"""
        return self._current_started if self.is_running else None

    @property
    def current_workflow(self) -> dict | None:
        """本轮启动时的流程身份；不暴露参数，也不跟随后续重命名改变。"""
        return dict(self._current_workflow) if self.is_running and self._current_workflow else None

    @property
    def workflow_snapshot(self):
        return json.loads(json.dumps(self._workflow_snapshot)) if self.is_running and self._workflow_snapshot else None

    def start(self, script_name: str, config_path: str, params: dict | None = None) -> str | None:
        """启动脚本。返回 run_id 或 None（不支持/已在跑/子进程没起来）"""
        with self._lock:
            if self.is_running:
                return None
            if (params or {}).get("workflow_wait_root"):
                from .workflow_waits import load
                if load().get(params["workflow_wait_root"], {}).get("status") != "launching":
                    return None
            if script_name not in _SCRIPTS:
                return None

            run_id = uuid.uuid4().hex[:12]
            self._current_run_id = run_id
            self._current_script = script_name
            self._current_started = time.time()
            self._last_output = time.time()
            self._stop_reason = ""
            self._current_workflow = None
            self._workflow_snapshot = None

            try:
                if script_name == "workflow":
                    from .workflow import find_preset
                    preset = find_preset(str((params or {}).get("workflow_id") or ""))
                    if preset:
                        self._current_workflow = {"id": preset["id"], "name": preset["name"]}
                        resume = (params or {}).get("workflow_resume")
                        nodes = resume["nodes"][resume["next_index"]:] if resume else preset.get("nodes", [])
                        self._workflow_snapshot = json.loads(json.dumps({"nodes": nodes}))
                    elif ((params or {}).get("workflow_id") == "builtin-scheduled-raid"
                          and "scheduled_raid_runs" in (params or {})):
                        self._current_workflow = {
                            "id": "builtin-scheduled-raid", "name": "今日联队战 · 自动开工"}
                self._proc = self._spawn(script_name, config_path, params or {}, run_id)
                if (params or {}).get("workflow_wait_root"):
                    from .workflow_waits import segment_started
                    segment_started(params["workflow_wait_root"], run_id)
                from touken.telemetry import get_telemetry_store
                workflow_label = (self._current_workflow or {}).get("name") or None
                if script_name == "scheduled_gameplay":
                    gameplay = _SCRIPTS.get((params or {}).get("script"), {})
                    workflow_label = f"今日{gameplay.get('label', '玩法')} · {(params or {}).get('runs', '?')} 次"
                get_telemetry_store().start_run(
                    run_id, script_name, self._current_started, label=workflow_label)
            except Exception as exc:
                self._proc = None
                msg = f"[面板] 工人子进程启动失败: {exc}"
                print(msg, file=sys.stderr)
                self._emit(run_id, script_name, msg)
                return None

            threading.Thread(target=self._pump,
                             args=(self._proc, run_id, script_name, (self._current_workflow or {}).get("name")),
                             daemon=True, name=f"worker-pump-{run_id}").start()
            threading.Thread(target=self._watchdog,
                             args=(self._proc, run_id, script_name),
                             daemon=True, name=f"worker-dog-{run_id}").start()
            return run_id

    def _spawn(self, script_name: str, config_path: str, params: dict,
               run_id: str) -> subprocess.Popen:
        """起工人子进程。源码模式走 python -m，exe 模式走 --worker 自分发。"""
        project_root = Path(__file__).resolve().parent.parent
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"        # 输出不缓冲，看门狗才数得准
        env["PYTHONIOENCODING"] = "utf-8"    # 日文/emoji 不许被 GBK 掐死
        env["MAAMARU_WORKER"] = "1"          # 告诉 maa_adapter：可以自我了断
        env["MAAMARU_WORKER_PARAMS"] = json.dumps(params, ensure_ascii=False)
        env["MAAMARU_RUN_ID"] = run_id        # 结构化观测数据关联本轮任务
        env["MAAMARU_SCRIPT"] = script_name

        if getattr(sys, "frozen", False):
            cmd = [sys.executable, "--worker", script_name, config_path]
        else:
            cmd = [sys.executable, "-m", "panel.worker", script_name, config_path]

        # vbs / windowed exe 下起子进程不许蹦黑窗
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        return subprocess.Popen(
            cmd, cwd=str(project_root), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags,
        )

    def stop(self, cancel_waits=True):
        """紧急停止 = 直接杀子进程（真停，不用等它下次开口）"""
        from .workflow_waits import cancel
        if cancel_waits:
            cancel()
            if self._current_run_id and self._current_script in {"workflow", "scheduled_gameplay"}:
                cancel(self._current_run_id, remember=True)
        proc = self._proc
        if proc is not None and proc.poll() is None:
            self._stop_reason = "user"
            self._stop_reasons[self._current_run_id] = "user"
            try:
                proc.kill()
            except Exception:
                pass

    # ---------- 内部：收输出 ----------

    def _pump(self, proc: subprocess.Popen, run_id: str, script_name: str, workflow_name=None):
        store = get_store()
        pause_saved = False
        pause_cancelled = False
        try:
            for line in proc.stdout:
                msg = line.rstrip()
                if not msg:
                    continue
                if script_name in {"workflow", "scheduled_gameplay"} and msg.startswith("@@MAAMARU_WORKFLOW_WAIT@@"):
                    from .workflow_waits import park
                    payload = json.loads(msg[len("@@MAAMARU_WORKFLOW_WAIT@@"):])
                    pause_saved = park(run_id, payload["config_path"], payload["params"], payload["wake_at"],
                         payload["resume"], workflow_name or "时间表任务", script=script_name)
                    pause_cancelled = not pause_saved
                    continue
                self._last_output = time.time()
                store.append(run_id, script_name, msg)
                self._emit(run_id, script_name, msg)
                print(msg, flush=True)
        except Exception as exc:
            err = f"[面板] 收子进程输出时出错: {exc}"
            store.append(run_id, script_name, err)
            self._emit(run_id, script_name, err)
            print(err, file=sys.stderr)

        rc = proc.wait()
        stop_reason = self._stop_reasons.pop(run_id, "")
        if rc == 44 and not pause_saved and not pause_cancelled:
            rc = 1  # 没保存续跑位置绝不能宣称正常等待。
        if pause_cancelled:
            stop_reason = "user"
        if stop_reason == "user":
            final = f"[脚本] 已手动停止（工人进程已杀）— run {run_id}"
        elif stop_reason == "watchdog":
            final = f"[脚本] 看门狗已处决卡死的工人进程 — run {run_id}"
        elif rc == 0:
            final = f"[脚本] 完成 — run {run_id}"
        elif rc == 44:
            final = f"[脚本] 等待指定时间，其他任务可以开工 — run {run_id}"
        elif rc == EXIT_FLOW_ABORTED:
            final = f"[脚本] 玩法遇到异常，已安全停止且未计作完成 — run {run_id}"
        elif rc == EXIT_MAA_DEAD:
            final = (f"[脚本] MAA 连续超时，工人进程自我了断 — run {run_id}。"
                     "建议重启模拟器后再跑")
        else:
            final = f"[脚本] 工人进程异常退出（代码 {rc}）— run {run_id}"
        status = ("stopped" if stop_reason == "user" else
                  "watchdog" if stop_reason == "watchdog" else
                  "completed" if rc == 0 else "waiting" if rc == 44 else "failed")
        from .workflow_waits import finish
        finish(run_id, status)
        try:
            from touken.telemetry import get_telemetry_store
            telemetry = get_telemetry_store()
            telemetry.finish_run(run_id, status)
            summary = telemetry.run_summary(run_id)
            result = self._format_result(summary)
            if result:
                store.append(run_id, script_name, result)
                self._emit(run_id, script_name, result)
                print(result)
        except Exception:
            pass
        store.append(run_id, script_name, final)
        self._emit(run_id, script_name, final)
        print(final)

        with self._lock:
            if self._proc is proc:
                self._last_run_id = run_id
                self._last_run_status = status
                self._proc = None

    @staticmethod
    def _format_result(summary: dict | None) -> str | None:
        if not summary or not summary.get("loops"):
            return None
        floor = summary.get("selected_floor")
        script = summary.get("script") or ""
        if script == "osaka":
            place = f"大阪城 {floor}F" if floor is not None else "大阪城"
        else:
            place = {
                "sortie": "合战场", "yosari": "异去", "raid": "联队战",
                "pumpkin": "南瓜大作战", "hanafuda": "秘宝之里", "daily": "一键日课",
            }.get(script, "本次出阵")
        parts = [f"{place} {summary['loops']} 圈"]
        average = summary.get("average_loop_seconds")
        if average:
            minutes, seconds = divmod(int(round(average)), 60)
            parts.append(f"平均 {minutes}分{seconds:02d}秒/圈")
            parts.append(f"照这个速度 6 小时约 {summary['estimated_6h_loops']} 圈")
        parts.append(f"手入 {summary.get('repair_sessions', 0)} 次")
        parts.append(f"加速符 {summary.get('speedups', 0)} 枚")
        parts.append(f"补刀装 {summary.get('equipment_restores', 0)} 次")
        deltas = summary.get("resource_delta") or {}
        for name in ("小判", "木炭", "玉钢", "冷却材", "砥石", "委托符", "加速符"):
            value = deltas.get(name)
            if value:
                parts.append(f"{name} {value:+,}")
        return "[挂机成绩单] " + " · ".join(parts)

    # ---------- 内部：沉默看门狗 ----------

    def _watchdog(self, proc: subprocess.Popen, run_id: str, script_name: str):
        """只数秒数 + 杀进程，绝不碰 MAA（碰 MAA 的线程看门狗已经咬死过人）"""
        last_tick = time.time()
        while True:
            time.sleep(5)
            now = time.time()
            if now - last_tick > SUSPEND_GAP_SEC:
                # 看门狗自己都被冻了这么久 → 整台电脑刚休眠过，
                # 工人和输出也一起冻着，不是卡死；静默计时清零重来
                gap = int(now - last_tick)
                self._last_output = now
                last_tick = now
                note = (f"[看门狗] 检测到系统休眠 {gap} 秒（工人只是陪电脑睡了一觉），"
                        "静默计时已清零，继续盯梢")
                get_store().append(run_id, script_name, note)
                self._emit(run_id, script_name, note)
                print(note)
                continue
            last_tick = now
            if proc.poll() is not None:
                return  # 正常/异常退出都归 _pump 收尾
            silent = now - self._last_output
            if silent <= SILENCE_TIMEOUT_SEC:
                continue
            self._stop_reason = "watchdog"
            self._stop_reasons[run_id] = "watchdog"
            msg = (f"[看门狗] ⚠️ 工人进程 {int(silent)} 秒一行输出都没有，判定卡死，已强杀。"
                   "日志最后一行就是它咽气前在干的事；建议重启模拟器再跑")
            get_store().append(run_id, script_name, msg)
            self._emit(run_id, script_name, msg)
            print(msg)
            try:
                proc.kill()
            except Exception:
                pass
            return

    def _emit(self, run_id: str, script: str, message: str):
        payload = {
            "id": None,  # 由 log_store 分配
            "ts": time.time(),
            "run_id": run_id,
            "script": script,
            "message": message,
        }
        if self._on_message:
            self._on_message(payload)


# 全局单例
_runner: ScriptRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> ScriptRunner:
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = ScriptRunner()
    return _runner
