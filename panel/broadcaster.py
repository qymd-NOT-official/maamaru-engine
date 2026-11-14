# -*- coding: utf-8 -*-
"""
事件播报器 —— 狐之助主动开口的地方（0 token，不过 LLM）

挂在脚本消息管道上（server._on_script_message → feed），只挑「关键事件」说话：
  开工 / 收工 / 翻车 / 被停 / 崩溃 / 日课成绩单 / 时刻表到点 / 锻刀命中

人格从哪来：模板 + 手写狐之助语料池随机抽。陪伴感的本质是「关键时刻主动开口」，
不是当日志复读机——所以这里一分钱不烧，人设还比 LLM 稳。

出口分工（避免和 touken 流程自带的 ntfy 撞车）：
  QQ  ：什么都报（狐之助语气，私聊 admin_qq 列表）
  ntfy：只报流程没覆盖的——时刻表到点、脚本崩溃（事实版，ASCII 标题）
  日课成绩单 / 锻刀命中的 ntfy 由 touken 流程自己发，这里只补 QQ。
"""

import json
import random
import re
import threading
import time
from pathlib import Path

from touken.runtime_paths import PANEL_CONFIG_PATH, STATUS_DIR

_HERE = Path(__file__).resolve().parent
_PANEL_CONFIG = PANEL_CONFIG_PATH
_STATUS_DIR = STATUS_DIR

# ── 狐之助语料池：{label} = 脚本显示名 ──

_START = [
    "狐之助我开工啦——{label}！主君等我的好消息✨",
    "{label}，交给我！（摇着尾巴跑向战场）",
    "收到——{label}开始！刀男们都给我打起精神来！",
]

_FINISH = [
    "{label}收工！一切顺利，主君夸夸我嘛🦊",
    "搞定搞定——{label}圆满完成，本丸今天也很和平。",
    "{label}干完啦！哼，这种程度对狐之助我来说轻而易举。",
]

_STOP = [
    "呜哇——{label}被叫停了！好吧好吧，听主君的。",
    "{label}已停下。主君有什么别的安排吗？",
]

_CRASH = [
    "呜呜…{label}半路摔了一跤（脚本崩溃）！主君快来面板看看！",
    "出事了出事了——{label}崩了！狐之助我也没辙，求支援！",
]

_DAILY_OK = [
    "{label}全绿收工！🌸 本丸今天也是模范员工，主君快夸我！",
    "{label}一条龙全部 ✓！哼，有狐之助盯着，想翻车都难。",
]

_DAILY_FAIL = [
    "{label}跑完了，但有 {n} 项翻车🍂：{fails}。详细成绩单在面板里，主君过目。",
    "呜…{label}有 {n} 项没跑好（{fails}）。不是狐之助的错……大概。",
]

_SCHED = [
    "🕐 到点啦！部队{team}出发去 {map}「{name}」，一路顺风——",
    "执勤时间到！部队{team} → {map}「{name}」，记得回来哦。",
]

_HIT = [
    "🎉🎉 大喜事！！锻刀命中目标时长 {hit}！主君快去看炉子！！",
    "限锻雷达响了——{hit}！这炉有戏，快收！",
]


def _pick(pool: list[str]) -> str:
    return random.choice(pool)


class Broadcaster:
    """订阅脚本消息，关键事件 → QQ（狐之助版）+ ntfy（事实版）"""

    def __init__(self, qq_sender=None):
        self._qq = qq_sender            # bot_qq.QQSender，没配 QQ 就是 None
        self._runs: dict[str, float] = {}  # run_id -> 首次见到的时间戳（开工去重用）
        self._lock = threading.Lock()

    # ── 配置 ──

    def _panel_cfg(self) -> dict:
        try:
            return json.loads(_PANEL_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _bot_cfg(self) -> dict:
        return self._panel_cfg().get("bot", {})

    def _broadcast_cfg(self) -> dict:
        bc = self._bot_cfg().get("broadcast", {})
        return {"qq": bc.get("qq", True), "ntfy": bc.get("ntfy", True)}

    def _admin_qq(self) -> list[int]:
        return [int(q) for q in self._bot_cfg().get("qq", {}).get("admin_qq", [])]

    # ── 出口 ──

    def _send_qq(self, text: str):
        """狐之助版，私聊所有管理员。没配 sender / 没填 admin_qq 就静默跳过。"""
        if not self._broadcast_cfg()["qq"] or not self._qq:
            return
        for uid in self._admin_qq():
            try:
                self._qq.send_private(uid, text)
            except Exception as exc:
                print(f"[播报] QQ 发送失败（{uid}）: {exc}", flush=True)

    def _send_ntfy(self, text: str, title: str, tags: str = "fox",
                   priority: str = "default"):
        """事实版手机推送。title 必须 ASCII（notify 模块的坑，中文标题会静默失败）。"""
        if not self._broadcast_cfg()["ntfy"]:
            return
        try:
            from touken.notify import notify
            notify(text, title=title, tags=tags, priority=priority)
        except Exception as exc:
            print(f"[播报] ntfy 发送失败: {exc}", flush=True)

    def announce(self, qq_text: str, *, ntfy_text: str | None = None,
                 title: str = "Maamaru Notice", tags: str = "fox",
                 priority: str = "default"):
        """主动提醒类公共出口（收件箱临期、活动开结束等）：
        QQ 狐之助版照发；给了 ntfy 文案就顺手推手机，没给就只发 QQ。"""
        self._send_qq(qq_text)
        if ntfy_text:
            self._send_ntfy(ntfy_text, title=title, tags=tags, priority=priority)

    # ── 事件分类 ──

    def feed(self, payload: dict):
        """消息管道喂进来的每一条。只挑关键事件开口，流水账一律无视。"""
        script = str(payload.get("script") or "")
        msg = str(payload.get("message") or "")
        run_id = payload.get("run_id")

        # 时刻表到点派遣（scheduler 线程直接 emit，不走 runner）
        if script == "scheduler":
            self._on_schedule(msg)
            return

        if not run_id:
            return

        # 生命周期消息
        if msg.startswith("[脚本] 完成"):
            self._on_finish(script, run_id)
        elif msg.startswith("[脚本] 用户请求停止"):
            self._on_stop(script, run_id)
        elif "脚本崩溃" in msg:
            self._on_crash(script, run_id, msg)
        elif "目标时长命中" in msg:
            self._on_forge_hit(msg)
        else:
            # 新 run 的第一条消息 = 开工（完成/停止之类上面已拦）
            with self._lock:
                is_new = run_id not in self._runs
                if is_new:
                    self._runs[run_id] = time.time()
            if is_new:
                self._on_start(script)

    # ── 各事件 ──

    def _label(self, script: str) -> str:
        try:
            from .script_runner import _SCRIPTS
            return _SCRIPTS.get(script, {}).get("label") or script or "脚本"
        except Exception:
            return script or "脚本"

    def _on_start(self, script: str):
        self._send_qq(_pick(_START).format(label=self._label(script)))

    def _on_finish(self, script: str, run_id: str):
        with self._lock:
            self._runs.pop(run_id, None)
        if script in ("daily", "workflow"):
            # workflow（自定义工作流）落的是同 schema 的成绩单，一并发
            self._on_daily_report(label=self._label(script))
        else:
            self._send_qq(_pick(_FINISH).format(label=self._label(script)))

    def _on_stop(self, script: str, run_id: str):
        with self._lock:
            self._runs.pop(run_id, None)
        self._send_qq(_pick(_STOP).format(label=self._label(script)))

    def _on_crash(self, script: str, run_id: str, msg: str):
        with self._lock:
            self._runs.pop(run_id, None)
        label = self._label(script)
        self._send_qq(_pick(_CRASH).format(label=label))
        # 崩溃值得手机响一声（高优先级）
        detail = msg.replace("[面板] 脚本崩溃: ", "")[:120]
        self._send_ntfy(f"{label} 脚本崩溃：{detail}",
                        title="Maamaru Crash", tags="warning", priority="high")

    def _on_daily_report(self, label: str = "一键日课"):
        """日课/工作流收工：读落盘的成绩单（比解析日志流靠谱），QQ 播报。
        ntfy 版由 touken.flows.daily / panel.workflow 自己发，这里不重复。"""
        try:
            fp = _STATUS_DIR / "latest_report.json"
            rep = json.loads(fp.read_text(encoding="utf-8"))
        except Exception:
            self._send_qq(_pick(_FINISH).format(label=label))
            return
        fails = []
        for step in rep.get("steps", []):
            status = str(step.get("status", ""))
            if status.startswith("✓") or status.startswith("⏭"):
                continue
            detail = status.lstrip("✗⚠ ")
            fails.append(f"{step['name']}（{detail}）" if detail else step["name"])
        if fails:
            self._send_qq(_pick(_DAILY_FAIL).format(
                label=label, n=len(fails), fails="、".join(fails)))
        else:
            self._send_qq(_pick(_DAILY_OK).format(label=label))

    def _on_schedule(self, msg: str):
        if msg.startswith("⏳"):
            self._send_qq(msg)
            self._send_ntfy(msg, title="远征即将接管游戏", tags="warning,clock1")
            return
        if msg.startswith("🕐 开始派遣"):
            self._send_qq(msg)
            self._send_ntfy(msg, title="Maamaru Dispatch", tags="clock1")
            return
        # 消息格式：🕐 到点啦！派部队5 去 E2「江户·鸟羽」（scheduler.py 拼的）
        m = re.search(r"派部队(\d+)\s*去\s*(\S+)「([^」]*)」", msg)
        team, map_code, map_name = (m.group(1), m.group(2), m.group(3)) if m else ("?", "?", "")
        self._send_qq(_pick(_SCHED).format(team=team, map=map_code, name=map_name))
        self._send_ntfy(f"已派部队{team} 去 {map_code}「{map_name}」",
                        title="Maamaru Dispatch", tags="clock1")

    def _on_forge_hit(self, msg: str):
        # 消息格式：[锻刀] 🎉🎉🎉 喜报！这炉倒计时 03:20:00，目标时长命中！快去看！
        m = re.search(r"倒计时\s*(\S+?)，目标时长命中", msg)
        hit = m.group(1) if m else "?"
        self._send_qq(_pick(_HIT).format(hit=hit))
        # ntfy 喜报由 touken.flows.smith 自己发，不重复


# ── 单例 ──

_bc: Broadcaster | None = None


def init_broadcaster(qq_sender=None) -> Broadcaster:
    """server startup 调一次：挂上 QQ 出口（没有也能跑，只发 ntfy）"""
    global _bc
    _bc = Broadcaster(qq_sender)
    outs = []
    if qq_sender:
        outs.append("QQ")
    outs.append("ntfy")
    print(f"[播报] 事件播报器就位，出口: {' + '.join(outs)}", flush=True)
    return _bc


def get_broadcaster() -> Broadcaster | None:
    return _bc
