# -*- coding: utf-8 -*-
"""
自定义工作流（乐高排班）—— 把任务积木自由排序拼成流水线，一键运行

积木 = 各玩法的 *_stream() 生成器，只准复用、不自己攒出阵逻辑
（出阵编排仍由玩法内部 _safe_depart_stream 红线保障）。

- 节点目录分四类：cold（冷启动）/ chore（后勤）/ battle（出阵）/ finish（收尾）。
- 预设存用户数据目录 STATUS_DIR/workflows.json，不进程序目录/发布包。
- 节点判分复用日课同款翻车词表（touken.flows.report_judge），
  「没跑成必须 ✗，不许假绿」。
- 每跑完一块就落一次 latest_report.json（与日课同 schema），防中途被杀丢数据。

依赖方向：本模块不 import panel.server（server 在注册时把出阵类积木
的参数 schema 和 builder 注入 NODE_REGISTRY，避免循环 import）。
"""

import json
import copy
import os
import re
import shutil
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from touken.flows.report_judge import (
    _equip_warning_status,
    _is_fail,
    _practice_report_status,
    _shop_report_status,
    _snapshot_report_status,
    _sugar_report_status,
)
from touken.runtime_paths import STATUS_DIR

# ADB 缺省（与 panel/server.py 的 _DEFAULT_ADB_* 保持一致）
_DEFAULT_ADB_PATH = r"D:\MUMU\MuMuPlayer\nx_device\12.0\shell\adb.exe"
_DEFAULT_ADB_ADDR = "127.0.0.1:16384"

MAX_NODES = 30
VALID_ON_ERROR = ("stop", "continue")
VALID_AFTER = ("none", "logout", "shutdown", "sleep")
DAILY_PRESET_ID = "builtin-daily"
daily_template_provider = None

# ⏭ = 前块翻车即停、这块没轮到跑——不是翻车，播报/推送都不许算成失败
_SKIPPED_PREFIX = "⏭"


class WorkflowError(ValueError):
    """预设/节点校验失败（API 层转成 400）。"""


# ── 节点注册表 ──
# 每个节点：{type, label, desc, category, params(schema), run(agent, params, config_path),
#           needs_agent, detail(专项判分函数列表)}
NODE_REGISTRY: dict[str, dict] = {}


def register_node(defn: dict):
    NODE_REGISTRY[defn["type"]] = defn


def _node(type_, label, desc, category, run, params=None, detail=None,
          needs_agent=True):
    register_node({
        "type": type_, "label": label, "desc": desc, "category": category,
        "params": params or [], "run": run, "needs_agent": needs_agent,
        "detail": detail or [],
    })


def _stream_node(type_, label, desc, category, method, detail=None):
    """裸调 agent 上某个无参 *_stream() 的积木。"""
    def run(agent, params, config_path):
        yield from getattr(agent, method)()
    _node(type_, label, desc, category, run, detail=detail)


def node_catalog() -> list[dict]:
    """节点目录（前端渲染积木选择器和参数表单用）。"""
    order = {"cold": 0, "time": 1, "chore": 2, "battle": 3, "finish": 4}
    nodes = [{
        "type": d["type"], "label": d["label"], "desc": d.get("desc", ""),
        "category": d["category"], "params": d.get("params") or [],
        "template_only": d.get("template_only", False),
    } for d in NODE_REGISTRY.values()]
    nodes.sort(key=lambda n: (order.get(n["category"], 9), n["type"]))
    return nodes


# ── 冷启动 ──

def _run_boot_emulator(agent, params, config_path):
    """开模拟器：ADB 已通则跳过，否则启动并轮询等待。

    ensure_emulator 是阻塞调用（冷启动约 4 分钟），放线程里跑、
    消息经队列实时 yield 出去——沉默看门狗 300s 无输出会强杀子进程。
    """
    from touken.emulator import ensure_emulator
    cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
    adb_path = cfg.get("adb_path", _DEFAULT_ADB_PATH)
    address = cfg.get("adb_address", _DEFAULT_ADB_ADDR)
    manager = cfg.get("emulator_manager")
    instance = int(cfg.get("emulator_instance", 0))

    messages: queue.Queue = queue.Queue()
    result: dict = {}

    def _work():
        try:
            result["ok"] = ensure_emulator(
                adb_path, address, manager, instance,
                emit=lambda m: messages.put(m))
        except Exception as exc:  # 绝不把异常留在后台线程里
            result["ok"] = False
            result["error"] = exc

    worker = threading.Thread(target=_work, daemon=True, name="workflow-boot")
    worker.start()
    while worker.is_alive():
        try:
            yield messages.get(timeout=2)
        except queue.Empty:
            pass
    while not messages.empty():
        yield messages.get()
    if result.get("ok"):
        yield "[模拟器] ✓ 模拟器已就绪"
    else:
        yield f"[工作流] ✗ 模拟器启动失败，后续停止（{result.get('error', '原因不明')}）"


def _run_login(agent, params, config_path):
    """登录：开游戏 → 点登录 → 弹窗扫地到本丸 → 更新门卫。任一步翻车即节点失败。"""
    started = yield from agent._ensure_game_started()
    if not started:
        yield "[工作流] ✗ 未确认游戏成功启动，登录停止"
        return
    if not agent.login():
        yield "[工作流] ✗ 登录没确认成功，停"
        return
    if agent._popup_sweep():
        yield "[工作流] ✓ 已到本丸，弹窗已清扫"
    else:
        yield "[工作流] ✗ 登录后没到本丸，停"
        return
    update_state = yield from agent._daily_update_gate()
    if update_state is None:
        yield "[工作流] ✗ 更新恢复失败，登录未算完成"


_node("boot_emulator", "开模拟器",
      "ADB 连不上时把模拟器拉起来并等开机；已在跑则秒过。放在第一块，适合定时冷启动。",
      "cold", _run_boot_emulator, needs_agent=False)
_node("login", "登录游戏",
      "开游戏、点登录、清扫登录弹窗到本丸，并过一遍更新门卫。后续积木的前置。",
      "cold", _run_login)


# ── 定时 ──

# 目标时刻刚过去这么久以内算「刚错过」：不睡了，直接接着干活
# （防前置积木超时分把钟，结果一睡睡到明天同刻的事故）。
_WAIT_JUST_PASSED_GRACE = 15 * 60

# 子进程沉默看门狗 300s 无输出即强杀（script_runner.SILENCE_TIMEOUT_SEC），
# 睡眠心跳必须显著更勤。
_WAIT_HEARTBEAT_SEC = 120.0


def _parse_hhmm(text) -> int | None:
    """严格解析 HH:MM → 当天分钟数；非法返回 None。"""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(text or "").strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _wait_seconds(target_min: int, now: float = None) -> tuple[float, str]:
    """距下一个 target_min（当天分钟数）还要等多久。

    返回 (秒数, mode)：
    - today：今天还没到点；
    - tomorrow：今天已过点，睡明天同一时刻；
    - just_passed：刚错过（≤15 分钟），不用等。
    """
    now_dt = datetime.fromtimestamp(now if now is not None else time.time())
    target = now_dt.replace(hour=target_min // 60, minute=target_min % 60,
                            second=0, microsecond=0)
    delta = (target - now_dt).total_seconds()
    if delta > 0:
        return delta, "today"
    if delta >= -_WAIT_JUST_PASSED_GRACE:
        return 0.0, "just_passed"
    return (target + timedelta(days=1) - now_dt).total_seconds(), "tomorrow"


def _fmt_sleep_duration(seconds: float) -> str:
    minutes = max(1, int((seconds + 30) // 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分钟" if hours else f"{minutes} 分钟"


def _run_wait_until(agent, params, config_path):
    """定时唤醒：睡到指定墙钟时刻再往下走。

    睡眠期间只按墙钟睡 + 周期心跳，全程不碰游戏画面——过日课刷新、
    扫地、更新门卫是后面「登录游戏」积木的活。睡觉期间 runner 被
    占用，远征时刻表调度会让路，醒来后恢复。
    """
    raw = str((params or {}).get("time") or "").strip()
    target_min = _parse_hhmm(raw)
    if target_min is None:
        yield (f"✗ [闹钟] 「睡到几点」这时间我看不懂：「{raw or '（空）'}」"
               "——要 24 小时制 HH:MM，比如 04:05")
        return
    label = f"{target_min // 60:02d}:{target_min % 60:02d}"
    seconds, mode = _wait_seconds(target_min)
    if mode == "just_passed":
        yield f"[闹钟] ⏰ {label} 刚过没几分钟，不睡了，直接接着干活"
        return
    when = "今天" if mode == "today" else "明天"
    # 截止时刻一次钉死，之后每片睡完都拿墙钟重算剩余——电脑中途休眠
    # 醒来墙上时钟跳变也能睡够/及时醒，绝不按睡眠次数递减。
    deadline = time.time() + seconds
    yield (f"[闹钟] ⏰ 闹钟定好了：{when} {label} 起床"
           f"（还要睡 {_fmt_sleep_duration(seconds)}），zzZ…")
    last_beat = time.time()
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        now = time.time()
        if now - last_beat >= _WAIT_HEARTBEAT_SEC:
            last_beat = now
            yield (f"[闹钟] ⏰ 还在睡，{label} 起床"
                   f"（还有 {_fmt_sleep_duration(remaining)}）…")
        time.sleep(min(5.0, remaining))
    yield f"[闹钟] ⏰ {label} 到点，起床接着干活"


_node("wait_until", "定时开启",
      "到指定时间后开始执行后续步骤，等待期间其他任务可以照常运行。",
      "time", _run_wait_until, needs_agent=False,
      params=[{"key": "time", "type": "text", "label": "几点继续（24小时制）",
               "default": "04:05",
               "help": "HH:MM，比如 04:05。今天的点已经过了就睡到明天同一时刻；"
                       "刚过不到 15 分钟算刚错过，直接醒不睡。"}])


# ── 后勤 ──

_stream_node("signin", "签到", "公告里的每日奖励，幂等保险", "chore",
             "signin_stream")
_stream_node("free_gift", "万屋领免费礼包", "万屋暖心礼包（免费鸡蛋）", "chore",
             "claim_free_gift_stream", detail=[_shop_report_status])
_stream_node("naihanka", "内番", "安排内番上工，24 小时的活儿早点派", "chore",
             "naihanka_stream")
_stream_node("dismantle", "刀解", "按刀解白名单解一把；今天解过会自己跳过",
             "chore", "dismantle_stream")
_stream_node("synthesize", "合成", "按合成白名单喂一把", "chore",
             "synthesize_stream")
_stream_node("sugar", "炼糖", "收件箱清狗粮 + 习合循环", "chore",
             "sugar_stream", detail=[_sugar_report_status])
_stream_node("inbox_supplies", "收杂物箱",
             "收件箱只收资源/货币/便利道具，刀剑邮件原样躺着", "chore",
             "inbox_supplies_stream")
_stream_node("task_rewards", "领任务奖励", "任务页一键领取日常/周常/月常奖励",
             "chore", "claim_task_rewards_stream")
_stream_node("snapshot", "库存快照", "拍一次完整家底刷新看板", "chore",
             "status_snapshot_stream", detail=[_snapshot_report_status])


def _run_apply_formation_preset(agent, params, config_path):
    from touken.custom_formations import apply_formation_preset_by_id_stream
    preset_id = str((params or {}).get("preset_id") or "")
    yield from apply_formation_preset_by_id_stream(agent, preset_id)


_node("apply_formation_preset", "套用部队预设",
      "把保存好的成员套到目标部队；整套开工前先检查刀账和同位冲突，没套完就停。后面再接出阵或远征。",
      "battle", _run_apply_formation_preset,
      params=[{"key": "preset_id", "type": "select", "label": "部队预设",
               "options": [], "default": ""}])


# ── 收尾 ──

def _run_logout(agent, params, config_path):
    mode = str((params or {}).get("mode") or "logout")
    if mode not in VALID_AFTER[1:]:
        raise WorkflowError("不支持的下班方式")
    yield from agent.logout_stream(
        kill_game=True,
        close_emulator=mode in ("shutdown", "sleep"),
        sleep_pc=mode == "sleep")


_node("logout", "下班", "退出游戏；可选择在退出后关掉模拟器、甚至让电脑休眠",
      "finish", _run_logout,
      params=[{"key": "mode", "type": "select", "label": "下班方式",
               "options": [["logout", "退出游戏"],
                           ["shutdown", "退出游戏 + 关模拟器"],
                           ["sleep", "退出 + 关模拟器 + 电脑休眠"]],
               "default": "logout"}])


# ── 校验 ──

def normalize_nodes(nodes) -> list[dict]:
    """校验并规范化积木列表；非法输入抛 WorkflowError（写入存储前必须过这关）。"""
    if not isinstance(nodes, list) or not nodes:
        raise WorkflowError("工作流至少要有一块积木")
    if len(nodes) > MAX_NODES:
        raise WorkflowError(f"工作流最多 {MAX_NODES} 块积木")
    out = []
    for i, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise WorkflowError(f"第 {i + 1} 块积木不是对象")
        type_ = node.get("type")
        if type_ not in NODE_REGISTRY:
            raise WorkflowError(f"第 {i + 1} 块：不支持的积木类型 {type_!r}")
        on_error = node.get("on_error") or "stop"
        if on_error not in VALID_ON_ERROR:
            raise WorkflowError(
                f"第 {i + 1} 块：翻车后策略只准 stop/continue，收到 {on_error!r}")
        params = node.get("params") or {}
        if not isinstance(params, dict):
            raise WorkflowError(f"第 {i + 1} 块：参数必须是对象")
        out.append({"type": type_, "params": params, "on_error": on_error})
    return out


# ── 预设存取（STATUS_DIR/workflows.json）──

def _presets_path() -> Path:
    return STATUS_DIR / "workflows.json"


def normalize_after(value):
    if value not in VALID_AFTER:
        raise WorkflowError("结束后的安排只能是留在本丸、退出游戏、关模拟器或休眠")
    return value


def validate_ending(preset):
    if preset.get("after", "none") != "none" and any(n["type"] == "logout" for n in preset["nodes"]):
        raise WorkflowError("已有下班步骤，请先移除再设置结束后的安排")


def present_preset(preset):
    """兼容旧流程，仅将唯一且位于末尾的下班步骤投影为结束安排；读取不改文件。"""
    result = copy.deepcopy(preset)
    nodes = result.get("nodes") or []
    if "after" not in result and len(nodes) > 1 and sum(n.get("type") == "logout" for n in nodes) == 1 and nodes[-1].get("type") == "logout":
        mode = (nodes[-1].get("params") or {}).get("mode") or "logout"
        if mode in VALID_AFTER[1:]:
            result["after"] = mode
            result["nodes"] = nodes[:-1]
    result.setdefault("after", "none")
    result.setdefault("daily_mode", False)
    return result


def list_presets():
    presets = load_presets()
    daily = next((p for p in presets if p.get("id") == DAILY_PRESET_ID), None)
    if daily is None and daily_template_provider is not None:
        daily = daily_template_provider()
    return ([daily] if daily else []) + [p for p in presets if p.get("id") != DAILY_PRESET_ID]


def load_presets() -> list[dict]:
    """读预设；坏文件备份后重置为空，绝不让面板崩。"""
    path = _presets_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        try:
            bad = path.with_name(path.name + ".bad-"
                                 + time.strftime("%Y%m%d-%H%M%S"))
            path.replace(bad)
        except OSError:
            pass
        return []
    if not isinstance(data, dict) or not isinstance(data.get("presets"), list):
        return []
    return [present_preset(p) for p in data["presets"] if isinstance(p, dict)]


def save_presets(presets: list[dict]):
    path = _presets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 保存前保留旧结构，替换失败时原文件仍在；旧版迁移不会丢失原始顺序。
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.bak"))
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps({"presets": presets}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def find_preset(preset_id) -> dict | None:
    for preset in list_presets():
        if preset.get("id") == preset_id:
            return preset
    return None


def _daily_ui(body, nodes):
    """只保留与实际执行节点一致的日课清单，避免流程编辑后显示旧设置。"""
    ui = body.get("daily_ui")
    if not isinstance(ui, dict) or not isinstance(ui.get("rows"), list):
        return None
    if len(ui["rows"]) > MAX_NODES:
        raise WorkflowError("日课项目太多")
    compiled = []
    for row in ui["rows"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise WorkflowError("日课项目格式不正确")
        normalized = normalize_nodes(row["nodes"]) if row.get("nodes") else []
        if row.get("enabled") is True:
            compiled.extend(normalized)
    compiled = [node for node in compiled if node["type"] not in {"boot_emulator", "login"}]
    actual = [node for node in nodes if node["type"] not in {"snapshot", "ledger_sync", "boot_emulator", "login"}]
    return copy.deepcopy(ui) if compiled == actual else None


def create_preset(body: dict) -> dict:
    if not isinstance(body, dict):
        raise WorkflowError("预设必须是对象")
    name = str(body.get("name") or "").strip()
    if not name:
        raise WorkflowError("预设名字不能为空")
    if len(name) > 30:
        raise WorkflowError("预设名字最多 30 个字")
    preset = {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "nodes": normalize_nodes(body.get("nodes")),
        "after": normalize_after(body.get("after", "none")),
        "daily_mode": body.get("daily_mode") is True,
        "parameter_version": 1,
        "daily_ui": _daily_ui(body, normalize_nodes(body.get("nodes"))),
    }
    validate_ending(preset)
    presets = load_presets()
    presets.append(preset)
    save_presets(presets)
    return preset


def update_preset(preset_id: str, body: dict) -> dict | None:
    """改预设；id 以路径为准。返回更新后的预设，找不到返回 None。"""
    if not isinstance(body, dict):
        raise WorkflowError("预设必须是对象")
    presets = load_presets()
    if preset_id == DAILY_PRESET_ID and not any(p.get("id") == preset_id for p in presets) and daily_template_provider is not None:
        presets.append(daily_template_provider())
    for i, existing in enumerate(presets):
        if existing.get("id") != preset_id:
            continue
        name = str(body.get("name") or existing.get("name") or "").strip()
        if not name:
            raise WorkflowError("预设名字不能为空")
        if len(name) > 30:
            raise WorkflowError("预设名字最多 30 个字")
        presets[i] = {
            "id": preset_id,
            "name": name,
            "nodes": normalize_nodes(
                body.get("nodes") if body.get("nodes") is not None
                else existing.get("nodes")),
            "after": normalize_after(body.get("after", existing.get("after", "none"))),
            "daily_mode": body.get("daily_mode", existing.get("daily_mode", False)) is True,
            "parameter_version": existing.get("parameter_version", 1),
            "daily_ui": _daily_ui({**body, "daily_ui": body.get("daily_ui", existing.get("daily_ui"))}, normalize_nodes(body.get("nodes") if body.get("nodes") is not None else existing.get("nodes"))),
        }
        validate_ending(presets[i])
        save_presets(presets)
        return presets[i]
    return None


def delete_preset(preset_id: str) -> bool:
    if preset_id == DAILY_PRESET_ID:
        raise WorkflowError("默认日课请保留；可以复制后自由调整")
    presets = load_presets()
    remaining = [p for p in presets if p.get("id") != preset_id]
    if len(remaining) == len(presets):
        return False
    save_presets(remaining)
    return True


# ── 成绩单（与日课 latest_report.json 同 schema）──

def _flush_report(report, finished: bool):
    """每跑完一块就写一次（finished=False），防超时被杀丢数据；终版 finished=True。"""
    try:
        status_dir = STATUS_DIR
        status_dir.mkdir(parents=True, exist_ok=True)
        fails = [n for n, s in report
                 if str(s).lstrip().startswith(("✗", "⚠"))]
        payload = {
            "run_id": os.environ.get("MAAMARU_RUN_ID") or None,
            "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "finished": finished,
            "all_green": finished and not fails,
            "steps": [{"name": n, "status": s} for n, s in report],
        }
        (status_dir / "latest_report.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    except Exception:
        return None


def _skipped_entries(rest: list[dict]) -> list[tuple]:
    return [(NODE_REGISTRY[n["type"]]["label"], f"{_SKIPPED_PREFIX} 跳过（翻车即停）")
            for n in rest]


# ── 运行 ──

def _run_node(defn: dict, agent, params: dict, config_path: str):
    """执行一块积木：yield 消息，返回 (ok, detail_status)。"""
    ok = True
    detail = None
    try:
        for msg in defn["run"](agent, params or {}, config_path):
            yield msg
            text = str(msg)
            if _is_fail(text) or text.lstrip().startswith("✗"):
                ok = False
            for detail_fn in defn.get("detail") or []:
                detail = detail_fn(text, detail)
    except Exception as exc:  # 含 FlowAborted：玩法安全中止 = 这块没跑成
        ok = False
        yield f"[工作流] 节点执行翻车: {exc}"
    return ok, detail


def _finale(report, payload):
    """成绩单文本 + ntfy 推送（与日课同款出口）。"""
    yield "========== 工作流成绩单 =========="
    for name, status in report:
        yield f"  {name}: {status}"
    fails = [n for n, s in report
             if str(s).lstrip().startswith(("✗", "⚠"))]
    yield ("[工作流] 全部跑完"
           + (f"，但有翻车项: {'、'.join(fails)}" if fails else "，全绿"))
    if payload is None:
        yield "[工作流] 成绩单落盘失败（不影响跑）"
    try:
        from touken.notify import notify_daily_report, notify_destination
        destination = notify_destination()
        if payload and notify_daily_report(payload):
            yield (f"[工作流] 成绩单已发送到 ntfy 频道「{destination}」；"
                   "手机订阅该频道后才能收到")
        elif not destination:
            yield "[工作流] 未配置 ntfy 频道，成绩单只保存在本机"
        else:
            yield "[工作流] ntfy 频道发送失败（网络或服务问题），成绩单已保存在本机"
    except Exception as exc:
        yield f"[工作流] 手机推送翻车（不影响跑）: {exc}"


class WorkflowPaused(Exception):
    def __init__(self, wake_at, resume):
        self.wake_at = wake_at
        self.resume = resume
        super().__init__("任务流正在等待，到点再继续")


def run_workflow(config_path, nodes, make_agent, after="none", daily_mode=False,
                 resume=None, defer_wait=False, defer_expedition=False):
    """
    流式跑一条工作流。

    Args:
        config_path: touken_config.json 路径
        nodes: [{"type", "params", "on_error"}]，会先过 normalize_nodes 校验
        make_agent: (config_path) -> ToukenAgent，可注入便于测试

    Yields:
        str: 执行状态消息
    """
    plan = normalize_nodes(nodes)
    after = normalize_after(after)
    if after != "none" and any(n["type"] == "logout" for n in plan):
        raise WorkflowError("已有下班步骤，请先移除再设置结束后的安排")
    config_path = str(config_path)
    report: list[tuple] = list((resume or {}).get("report", []))
    game_closed = bool((resume or {}).get("game_closed", False))

    start = (resume or {}).get("next_index", 0)
    if type(start) is not int or not 0 <= start <= len(plan):
        raise WorkflowError("续跑位置无效，请重新安排")
    expedition_resume = bool(resume and resume.get("reason") == "expedition")
    remaining = (resume or {}).get("remaining_runs")
    valid_expedition = (defer_expedition and expedition_resume and start < len(plan)
                        and plan[start]["type"] == "raid"
                        and type(remaining) is int and 1 <= remaining <= plan[start]["params"].get("runs", 0))
    if resume and (resume.get("nodes") != plan or len(report) != start
                   or (not valid_expedition and (expedition_resume or start == 0
                       or plan[start - 1]["type"] != "wait_until"))):
        raise WorkflowError("等待记录与任务流不一致，不能猜着续跑，请重新安排")
    if resume and report and not expedition_resume:
        report[-1] = (report[-1][0], "✓ 等待结束，已继续")
    if expedition_resume:
        ok, _ = yield from _run_node(NODE_REGISTRY["boot_emulator"], None, {}, config_path)
        if not ok:
            raise WorkflowError("续跑前模拟器未就绪，停止")
    if start == 0 and plan[0]["type"] == "boot_emulator":
        # 开模拟器不需要 agent（游戏都还没开），先跑它再建 agent
        yield "【工作流】▶ 第 1 块：开模拟器"
        ok, detail = yield from _run_node(NODE_REGISTRY["boot_emulator"], None,
                                          plan[0]["params"], config_path)
        report.append(("开模拟器", detail or ("✓" if ok else "✗")))
        _flush_report(report, finished=False)
        start = 1
        if not ok:
            yield "【工作流】模拟器没就绪，后面也没法跑，停下"
            report.extend(_skipped_entries(plan[start:]))
            payload = _flush_report(report, finished=True)
            yield from _finale(report, payload)
            return False

    yield "【工作流】正在连接游戏（创建 Agent）..."
    agent = make_agent(config_path)
    if expedition_resume:
        ok, _ = yield from _run_node(NODE_REGISTRY["login"], agent, {}, config_path)
        if not ok:
            raise WorkflowError("续跑前登录未确认，停止")
    agent._workflow_forge_ran = bool((resume or {}).get("forge_ran", False))
    completed = True

    for i in range(start, len(plan)):
        node = plan[i]
        defn = NODE_REGISTRY[node["type"]]
        if node["type"] == "wait_until" and defer_wait:
            minute = _parse_hhmm(node["params"].get("time"))
            if minute is not None:
                now = time.time()
                seconds, _ = _wait_seconds(minute, now)
                if seconds > 0:
                    report.append((defn["label"], f"等待至 {node['params']['time']}"))
                    _flush_report(report, finished=False)
                    yield f"【工作流】等待至 {node['params']['time']} 再继续，执行位置已让给其他任务"
                    raise WorkflowPaused(now + seconds, {"nodes": plan, "next_index": i + 1,
                        "report": report, "game_closed": game_closed,
                        "forge_ran": agent._workflow_forge_ran})
        if daily_mode:
            if (yield from agent._daily_update_gate()) is None:
                yield "【工作流】✗ 游戏更新未完成，后续日课停止"
                report.append((defn["label"], "✗ 游戏更新未完成"))
                report.extend(_skipped_entries(plan[i + 1:]))
                completed = False
                break
            if defn.get("daily_run"):
                defn = {**defn, "run": defn["daily_run"]}
        suffix = "（翻车跳过继续）" if node["on_error"] == "continue" else ""
        yield f"【工作流】▶ 第 {i + 1} 块：{defn['label']}{suffix}"
        try:
            if hasattr(agent, "set_progress"):
                agent.set_progress(f"workflow:{node['type']}")
        except Exception:
            pass
        node_params = node["params"]
        if expedition_resume and i == start:
            node_params = {**node_params, "runs": remaining, "rounds": remaining}
        if node["type"] == "raid":
            agent._raid_takeover_remaining = None
        ok, detail = yield from _run_node(defn, agent, node_params, config_path)
        pending_runs = getattr(agent, "_raid_takeover_remaining", None)
        if defer_expedition and ok and node["type"] == "raid" and type(pending_runs) is int and pending_runs > 0:
            _flush_report(report + [(defn["label"], f"等待远征派遣，剩余 {pending_runs} 圈")], finished=False)
            yield f"【工作流】联队战让位给远征，剩余 {pending_runs} 圈待续跑；未计作完成"
            # 返回本丸后才释放执行位置，不运行后续步骤或下班安排。
            yield from agent.navigate_to_stream("本丸")
            if agent.current_location != "本丸":
                raise WorkflowError("让位时未确认回到本丸，续跑停止")
            raise WorkflowPaused(time.time() + 5, {"nodes": plan, "next_index": i,
                "report": report, "game_closed": False, "reason": "expedition",
                "remaining_runs": pending_runs, "forge_ran": agent._workflow_forge_ran})
        if node["type"] == "forge":
            agent._workflow_forge_ran = True
        report.append((defn["label"], detail or ("✓" if ok else "✗")))
        if ok and node["type"] == "logout":
            # 下班积木会杀游戏（甚至关模拟器/休眠），收尾导航只会撞死在
            # 离线设备上（日课 9-04 凌晨翻车冤案同款），标记跳过收尾。
            game_closed = True
        # 每跑完一块就落盘一次，防中途被杀丢数据
        _flush_report(report, finished=False)
        if not ok and (node["on_error"] == "stop" or (daily_mode and node["type"] == "login")):
            completed = False
            yield (f"【工作流】这块翻车了，按设定「翻车即停」，"
                   f"剩余 {len(plan) - i - 1} 块不跑")
            report.extend(_skipped_entries(plan[i + 1:]))
            _flush_report(report, finished=False)
            break

    if after != "none" and completed:
        # This action is outside the movable steps. A stopped/aborted run never
        # reaches it. Defer PC sleep until after the final report/notification.
        yield "【工作流】结束后的安排：下班"
        mode = "shutdown" if after == "sleep" else after
        ok, detail = yield from _run_node(NODE_REGISTRY["logout"], agent, {"mode": mode}, config_path)
        report.append(("下班", detail or ("✓" if ok else "✗")))
        game_closed = ok

    # 收尾（整个 run 只此一次）：回本丸 + 强制顶栏 peek。
    # 中间节点不套 run 级盘点/收尾，避免每块都磨叽一遍（日课同款约定）。
    if game_closed:
        yield "【工作流】已执行下班积木，游戏已关，跳过收尾回本丸"
    else:
        try:
            yield "【工作流】收尾：导航回本丸"
            for nav_msg in agent.navigate_to_stream("本丸"):
                yield nav_msg
            if getattr(agent, "current_location", None) == "本丸":
                # 回本丸后点穿归来结算屏（远征/内番/修行），别让它们挡着
                # 收尾拍照——9-04 冤案二号：屏亮着没人收，看着像卡死。
                sweep = getattr(agent, "_popup_sweep", None)
                if sweep is not None:
                    try:
                        yield "【工作流】收尾：扫一遍归来结算屏"
                        sweep(max_rounds=6)
                    except Exception as exc:
                        yield f"【工作流】⚠️ 收尾扫地失败（不影响成绩单）: {exc}"
                yield "【工作流】收尾：已回本丸，强制拍一次顶栏"
                if hasattr(agent, "quick_peek"):
                    agent.quick_peek(tag="工作流·收尾", force=True)
            else:
                yield "【工作流】⚠️ 收尾没能回到本丸，可能卡在某个界面了，去看看"
        except Exception as exc:
            yield f"【工作流】⚠️ 收尾导航/Peek 失败（不影响成绩单）: {exc}"

    payload = _flush_report(report, finished=True)
    yield from _finale(report, payload)
    if after == "sleep" and completed and game_closed:
        def sleep_only(agent, params, config_path):
            yield from agent.logout_stream(kill_game=False, close_emulator=False, sleep_pc=True)
        yield "【工作流】成绩单已记录，准备休眠电脑"
        ok, detail = yield from _run_node({"run": sleep_only}, agent, {}, config_path)
        report.append(("电脑休眠", detail or ("✓" if ok else "✗")))
        _flush_report(report, finished=True)
    return completed
