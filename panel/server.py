"""
まあ丸 控制面板 —— FastAPI 服务
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import quote

# 确保能找到 touken 包（开发模式）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .log_store import get_store
from .honmaru_home import create_home_router
from .template_lab import create_template_lab_router
from .flow_lab import create_flow_lab_router
from .script_runner import _SCRIPTS, get_runner, list_scripts, register_script, ScriptRunner
from .daily_workflow import install_daily_template, recipe_fields, recipe_from_params
from touken.diagnostics import (
    build_diagnostic_bundle, create_diagnostic_bundle, reveal_file_in_explorer,
)
from touken.runtime_paths import (
    BACKUP_DIR, BUNDLE_ROOT, CONFIG_PATH, DEBUG_DIR, LOG_DIR, PANEL_CONFIG_PATH, RESOURCE_DIR, STATUS_DIR, JP_DATA_DIR,
    ensure_runtime_data,
)

# ── 路径 ──
ensure_runtime_data()
_HERE = Path(__file__).resolve().parent
_STATIC = _HERE / "static"
_PROJECT = BUNDLE_ROOT
_CONFIG_PATH = CONFIG_PATH
_PANEL_CONFIG = PANEL_CONFIG_PATH

# 预读游戏配置（用于面板默认值，不在这里写死游戏内容）
try:
    _CFG_DATA = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
except Exception:
    _CFG_DATA = {}

# 刀解默认白名单（兼容旧配置：配置里没有就用这个常量）
from touken.flows.smith import DISMANTLE_WHITELIST as _DISMANTLE_WHITELIST

# 默认 ADB 配置（从 test_daily.py 继承）
_DEFAULT_ADB_PATH = r"D:\MUMU\MuMuPlayer\nx_device\12.0\shell\adb.exe"
_DEFAULT_ADB_ADDR = "127.0.0.1:16384"

# ── App ──
app = FastAPI(title="まあ丸 近侍面板")
app.include_router(create_home_router(STATUS_DIR / "honmaru_home.json", JP_DATA_DIR / "state" / "honmaru_home.json"))
app.include_router(create_template_lab_router())
app.include_router(create_flow_lab_router())
_server_mode = threading.local()


def configure_app_mode(ledger_mode: bool | None) -> None:
    """Pin one Uvicorn server thread to its own mode.

    The launcher can keep the ledger and automation servers alive together.
    A process-wide environment variable cannot distinguish those two threads.
    Passing ``None`` restores the environment-backed default used by tests and
    direct module launches.
    """
    if ledger_mode is None:
        if hasattr(_server_mode, "ledger"):
            del _server_mode.ledger
        return
    _server_mode.ledger = bool(ledger_mode)


def _ledger_mode() -> bool:
    """纯净账房模式只开放数据与规划，不启动任何游戏控制设施。"""
    configured = getattr(_server_mode, "ledger", None)
    if configured is not None:
        return configured
    return os.environ.get("MAAMARU_LEDGER_MODE", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# SSE 广播队列（全局，所有订阅者共享）
_broadcast_queue: asyncio.Queue | None = None


def _start_broadcast():
    global _broadcast_queue
    if _broadcast_queue is None:
        _broadcast_queue = asyncio.Queue()


def _on_script_message(payload: dict):
    """脚本消息回调：从工作线程推到 asyncio 队列"""
    if _broadcast_queue is not None:
        try:
            # 在非 async 上下文推入 async 队列
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(_broadcast_queue.put_nowait, payload)
            else:
                loop.run_until_complete(_broadcast_queue.put(payload))
        except RuntimeError:
            pass  # 没有事件循环时静默丢弃

    # 事件播报器：狐之助主动开口（QQ/ntfy）。播报挂了不许拖累日志管道
    try:
        from .broadcaster import get_broadcaster
        bc = get_broadcaster()
        if bc is not None:
            bc.feed(payload)
    except Exception:
        pass

    # 异常与通知中心：值得追踪的异常立案归档（只记录，不开口）
    try:
        from . import incident_feed
        incident_feed.feed(payload)
    except Exception:
        pass


# ── 注册脚本 ──

def _make_maa(config_path):
    """创建 MAAAdapter（优先读配置，fallback 到 test_daily.py 里的硬编码路径）"""
    from touken import MAAAdapter
    path = Path(config_path)
    cfg = json.loads(path.read_text(encoding="utf-8"))
    # 面板把地址留空表示恢复自动探测。真正到下一次任务启动时再探测，
    # 这样说明和行为一致，也不会把一次临时探测结果冒充用户手填配置。
    if not str(cfg.get("adb_address", "")).strip():
        from touken.emulator_discovery import auto_configure_emulator
        auto_configure_emulator(path)
        cfg = json.loads(path.read_text(encoding="utf-8"))
    return MAAAdapter(
        adb_path=cfg.get("adb_path", _DEFAULT_ADB_PATH),
        adb_address=cfg.get("adb_address", _DEFAULT_ADB_ADDR),
        resource_dir=str(RESOURCE_DIR),
        project_root=str(STATUS_DIR.parent),
        manager_path=cfg.get("emulator_manager"),
        emulator_instance=int(cfg.get("emulator_instance", 0)),
    )


def _make_agent(config_path):
    from touken import ToukenAgent
    maa = _make_maa(config_path)
    if not maa.init():
        raise RuntimeError("MAA 初始化失败，检查 ADB 连接 / 模拟器是不是关了")
    return ToukenAgent(str(config_path), maa)


def _i(params, key, default):
    """参数转 int，空串/None 用默认"""
    v = params.get(key)
    if v in (None, ""):
        return default
    return int(v)


# ── 参数表单零件 ──
_TEAM_OPTIONS = [["1", "部队一"], ["2", "部队二"], ["3", "部队三"],
                 ["4", "部队四"], ["5", "部队五"]]
_TEAM_CN = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}
_DAILY_STEPS = ["登录", "签到", "万屋", "演练", "远征", "内番",
                "锻刀", "刀解", "合成", "出阵", "任务奖励", "库存快照"]


def _team_field(default="3"):
    return {"key": "team_no", "type": "select", "label": "部队",
            "options": _TEAM_OPTIONS, "default": default}


def _run_count_field(*, ticket=False, default=1):
    """四种出阵共用的目标数量；玩法内部再解释成圈数或手形预算。"""
    return {"key": "runs", "type": "number",
            "label": "出阵次数",
            "default": default, "min": 1, "max": 99,
            **({"help": "这是本次任务的目标次数；是否花小判补充手形由下方开关决定。"} if ticket else {})}


def _ticket_refill_field():
    return {"key": "auto_refill", "type": "toggle", "label": "是否自动补充手形？",
            "default": False,
            "help": "开启后，手形不足时将自动使用小判补充，直到完成设定的出阵次数。关闭后，手形不足时结束任务，不消耗小判。"}


def _run_count(params, default, *legacy_keys):
    """读取统一字段，同时兼容改版前已保存的各玩法字段。"""
    if params.get("runs") not in (None, ""):
        return _i(params, "runs", default)
    for key in legacy_keys:
        if params.get(key) not in (None, ""):
            return _i(params, key, default)
    return default


def _positive_run_count(params, default, *legacy_keys):
    """活动出阵次数只接受正数；把旧版的 0 遗留迁回安全默认值。"""
    value = _run_count(params, default, *legacy_keys)
    return value if value > 0 else default


def _captain_rotation_fields(*, daily_inherits: bool = False):
    """部队选择页可共用的换队长设置。"""
    help_text = "出阵前读全队疲劳，把疲劳最低的拖到队长位吃加成（保花用）。"
    if daily_inherits:
        help_text += "一键日课沿用此开关。"
    return [
        {"key": "rotate_captain", "type": "toggle", "label": "自动换队长",
         "default": False,
         "help": help_text},
        {"key": "rotate_captain_margin", "type": "select", "label": "换队长阈值",
         "options": [["5", "相差 5 点"], ["10", "相差 10 点"],
                     ["20", "相差 20 点"]],
         "default": "10",
         "help": "全队最低疲劳比当前队长低到这个差值时才换，避免差距很小时频繁调整。",
         "visibleWhen": {"key": "rotate_captain", "is": True}},
    ]


def _march_and_injury_fields():
    """合战场与异去共享；阵形策略仅脚本行军使用，兜底阵形也同步到游戏委托。"""
    return [
        *_captain_rotation_fields(daily_inherits=True),
        {"key": "auto_march", "type": "toggle", "label": "是否使用自动行军",
         "default": True,
         "help": "委托前同步游戏详细设定；轻伤停止改用脚本行军。"},
        {"key": "stop_on_fatigue", "type": "toggle", "label": "重疲劳时停止",
         "default": True, "visibleWhen": {"key": "auto_march", "is": "true"}},
        {"key": "formation_mode", "type": "select", "label": "阵形选择方式",
         "options": [["manual", "手动阵形"],
                     ["auto", "自动阵形"]],
         "default": "manual",
         "visibleWhen": {"key": "auto_march", "is": "false"}},
        {"key": "formation", "type": "select",
         "label": "固定或识别失败时的兜底阵形",
         "options": [[name, name] for name in
                     ["鱼鳞阵", "横队阵", "雁行阵", "鹤翼阵", "方阵", "逆行阵"]],
         "default": "鱼鳞阵"},
        {"key": "repair_threshold", "type": "select", "label": "伤势停止条件",
         "options": [["light", "轻伤时停止"],
                     ["medium", "中伤时停止"],
                     ["heavy", "重伤时停止"]],
         "default": "light"},
        {"key": "repair_on_injury", "type": "select", "label": "停止后的处理",
         "options": [["continue", "手入加速后继续剩余圈数"],
                     ["repair_stop", "手入后停止任务"],
                     ["stop", "停止任务，不进行手入"]],
         "default": "continue"},
        {"key": "auto_equip", "type": "toggle", "label": "是否自动补充刀装",
         "default": True,
         "help": "任务首次出阵前将当前部队保存到记录一；出现刀装未满提示时，自动用记录一补齐并重新检查伤势。"},
    ]


def _formation_fields():
    """不使用自动行军、但每场仍需选择阵形的玩法共用。
    策略和兜底阵形常显：自动阵形在游戏识别失败（夜战等）时会回落到
    手动按策略选，兜底阵形照样起作用（choose_formation 的设计）。"""
    return [
        {"key": "formation_mode", "type": "select", "label": "阵形选择方式",
         "options": [["manual", "手动阵形"], ["auto", "自动阵形"]],
         "default": "manual"},
        {"key": "formation", "type": "select",
         "label": "固定或识别失败时的兜底阵形",
         "options": [[name, name] for name in
                     ["鱼鳞阵", "横队阵", "雁行阵", "鹤翼阵", "方阵", "逆行阵"]],
         "default": "鱼鳞阵"},
    ]


def _sword_names(raw) -> list[str]:
    if isinstance(raw, list):
        return [str(name).strip() for name in raw if str(name).strip()]
    return [name.strip() for name in str(raw or "").replace("，", ",").split(",")
            if name.strip()]


# ── 各脚本 builder ──
# 玩法脚本统一由 _wrap_inventory 包一层：开工前/收工后各拍一次库存（run 级
# 整体归因）。builder 本体签名统一：(agent, config_path, params) -> generator。

def _sweep_return_screens(agent, tag):
    """回本丸后点穿归来结算屏（远征/内番/修行），别让它们挡着收尾拍照。

    9-04 冤案二号：远征队中途归来，结算屏要等回本丸才弹；收尾只导航不点屏，
    屏就亮在那儿没人收——看着像卡死，顶栏 peek 也拍歪。扫地复用日课登录
    同款的 _popup_sweep（结算屏/弹窗是同一类东西），限定轮数防磨蹭，
    扫不动也不拖垮收尾。
    """
    sweep = getattr(agent, "_popup_sweep", None)
    if sweep is None:
        return
    try:
        yield f"[{tag}] 收尾：扫一遍归来结算屏"
        sweep(max_rounds=6)
    except Exception as exc:
        yield f"[{tag}] ⚠️ 收尾扫地失败（不影响任务结果）：{exc}"


def _wrap_inventory(tag: str, runner, inventory=False, scheduled_startup=False):
    """给玩法脚本套统一收尾：库存盘点（默认关闭）+ 必做的回本丸 + 强制 peek。

    为什么盘点默认关了：完整快照融进了锻刀收工（零额外导航），顶栏五资源靠
    各循环 quick_peek 顺路更新（60s 节流），挖地小判差值由 osaka_stream
    自带的掉落率实验记账。专程跑腿盘点太磨叽（7-27 日课超时两次的教训）。
    想给某个任务恢复 run 级盘点就显式传 inventory=True。

    - before：任务开始前拍一张；after：任务结束后拍一张（含自然失败——异常
      会穿过 try/finally，finally 里照样补拍；紧急停止/看门狗是 kill，子进程
      没机会，由面板 has_after_snapshot=False 提示缺收工快照）。
    - 盘点失败绝不拖垮任务：before/after 各自 try，坏了只打日志继续。
    - 收尾回本丸 + 强制顶栏 peek：与 inventory 开关无关，每个任务跑完都执行。
      这是资源总账的固定“跑完了”观察点；若没能回到本丸，则作为故障信号上报。
      收尾失败同样不拖垮任务结果。
    """
    def _fn(config_path, params):
        from touken.flow_control import FlowAborted
        if scheduled_startup and params.get("scheduled"):
            yield "[排班] 先开模拟器、登录游戏，再派遣远征"
            ok, detail = yield from _workflow._run_node(
                _workflow.NODE_REGISTRY["boot_emulator"], None, {}, config_path)
            if not ok:
                _write_dispatch_result(params.get("slot_key", ""), "failed", "模拟器启动失败")
                raise FlowAborted("远征排班模拟器启动失败，本班未派遣")
        agent = _make_agent(config_path)
        if scheduled_startup and params.get("scheduled"):
            ok, detail = yield from _workflow._run_node(
                _workflow.NODE_REGISTRY["login"], agent, {}, config_path)
            if not ok:
                _write_dispatch_result(params.get("slot_key", ""), "failed", "登录游戏失败")
                raise FlowAborted("远征排班登录游戏失败，本班未派遣")
        enabled = inventory(params) if callable(inventory) else inventory
        if enabled and hasattr(agent, "status_snapshot_stream"):
            try:
                yield f"[{tag}] 开工前先盘点一次家底"
                yield from agent.status_snapshot_stream(phase="before")
            except Exception as exc:
                yield f"[{tag}] ⚠️ 开工盘点失败，继续干活：{exc}"
        try:
            yield from runner(agent, config_path, params)
        finally:
            if enabled and hasattr(agent, "status_snapshot_stream"):
                try:
                    yield f"[{tag}] 收工后再盘点一次，准备结算"
                    yield from agent.status_snapshot_stream(phase="after")
                except Exception as exc:
                    yield f"[{tag}] ⚠️ 收工盘点失败（不影响任务结果）：{exc}"
            # --- 新收尾：每个任务跑完都导航回本丸，并强制拍一次顶栏 peek ---
            # 这是固定观察点：给资源总账一个“跑完了”的锚点；如果没回到本丸，
            # 多半是卡在某个界面了，作为故障信号上报。
            try:
                yield f"[{tag}] 收尾：导航回本丸"
                for nav_msg in agent.navigate_to_stream("本丸"):
                    yield nav_msg
                if getattr(agent, "current_location", None) == "本丸":
                    yield from _sweep_return_screens(agent, tag)
                    yield f"[{tag}] 收尾：已回本丸，强制拍一次顶栏"
                    if hasattr(agent, "quick_peek"):
                        agent.quick_peek(tag=f"{tag}·收尾", force=True)
                else:
                    yield (f"[{tag}] ⚠️ 收尾没能回到本丸，可能卡在某个界面了，"
                           "去看看")
            except Exception as exc:
                yield f"[{tag}] ⚠️ 收尾导航/Peek 失败（不影响任务结果）：{exc}"
            if tag in {"出阵", "锻刀", "刷花", "异去", "挖地", "RAID", "南瓜", "江户城", "花札", "炼糖", "收杂物"}:
                try:
                    from touken import youzu_log
                    from touken.sword_receipts import sync_receipts
                    path = youzu_log.pull_log(agent.maa.adb_path,
                                              agent.maa.adb_address, dest_dir=DEBUG_DIR)
                    try:
                        result = sync_receipts(youzu_log.parse_events(path))
                    finally:
                        path.unlink(missing_ok=True)
                    if result["written"] or result["reconciled"]:
                        yield f"[{tag}] 锻刀和掉落记录已同步到账房"
                except Exception:
                    yield f"[{tag}] ⚠️ 刀剑进账同步失败，可收工后再同步近况"
    return _fn


_DAILY_BATTLE_KEYS = {
    "yosari": ("auto_march", "formation_mode", "formation", "repair_threshold",
               "repair_on_injury", "auto_equip", "rotate_captain",
               "rotate_captain_margin"),
    "sortie": ("auto_march", "formation_mode", "formation", "repair_threshold",
               "repair_on_injury", "auto_equip", "rotate_captain",
               "rotate_captain_margin", "retreat_before_boss"),
    "osaka": ("formation_mode", "formation", "repair_threshold",
              "repair_on_injury", "auto_equip"),
}


def _migrate_daily_battle_settings():
    """一次性快照迁移：一键日课的战斗行为字段过去读「配置」页对应玩法
    （issue#7：日课没安排修刀却自动修刀，病根就在这层继承）。本版起彻底
    切割——把当前出阵模式在配置页的值拷进日课参数里缺席的键，保住老用户
    现状；之后配置页怎么改都不再影响日课/工作流。键是各模式共享的，所以
    天然幂等：用户改过的值（键已存在）永远不会被覆盖，无需迁移标记。"""
    settings = _load_panel_settings()
    params = settings.get("params")
    if not isinstance(params, dict):
        return
    daily = params.get("daily")
    if not isinstance(daily, dict):
        return
    source = params.get(daily.get("sortie_mode") or "", {})
    if not isinstance(source, dict):
        return
    changed = False
    for key in _DAILY_BATTLE_KEYS.get(daily.get("sortie_mode"), ()):
        if key not in daily and key in source:
            daily[key] = source[key]
            changed = True
    if changed:
        _save_panel_settings(settings)


def _daily_plan_inputs(params):
    # 面板传 steps，Agent 网关传 only，都认
    steps = params.get("steps") or params.get("only") or None   # 空列表=全跑
    after = params.get("after") or "none"
    # 出阵安排：面板选的覆盖配置文件里的默认。team_no 允许直接选一套
    # 预设；这里只解析成“目标队 + 预设 id”，真正点游戏由日课步骤执行。
    sortie_team, sortie_preset, sortie_team_error = _resolve_team(params, 3)
    mode = params.get("sortie_mode") or "none"
    if mode == "raid":
        sortie_plan = {"mode": "raid",
                       "rounds": _i(params, "raid_rounds", 3),
                       "team_no": sortie_team,
                       "auto_buy_ticket": True,
                       "max_buys": _i(params, "raid_rounds", 3)}
    elif mode == "pumpkin":
        sortie_plan = {"mode": "pumpkin",
                       "difficulty": _i(params, "pumpkin_difficulty", 1),
                       "team_no": sortie_team,
                       "watch_names": _sword_names(params.get("pumpkin_watch")),
                       "max_skips": _i(params, "pumpkin_runs", 4)}
    elif mode == "yosari":
        sortie_plan = {"mode": "yosari",
                       "map_no": _i(params, "yosari_map_no", 1),
                       "team_no": sortie_team,
                       "loops": _i(params, "yosari_runs", 1),
                       "auto_refill": _bool(params.get("yosari_auto_refill", False)),
                       "auto_march": _bool(params.get("auto_march", True)),
                       "stop_on_fatigue": _bool(params.get("stop_on_fatigue", True)),
                       "formation_mode": params.get("formation_mode") or "manual",
                       "formation": params.get("formation") or "鱼鳞阵",
                       "repair_threshold": params.get("repair_threshold") or "light",
                       "repair_on_injury": params.get("repair_on_injury") or "continue",
                       "auto_equip": _bool(params.get("auto_equip", True)),
                       "rotate_captain": _bool(params.get("rotate_captain", False)),
                       "rotate_captain_margin": _i(params, "rotate_captain_margin", 10)}
    elif mode == "sortie":
        # 战斗行为全由日课自己的参数决定，与「配置」页彻底切割
        # （老用户的配置页现状由 _migrate_daily_battle_settings 快照进日课参数）
        sortie_plan = {"mode": "sortie",
                       "chapter": _i(params, "chapter", 1),
                       "map_no": _i(params, "map_no", 1),
                       "loops": _i(params, "loops", 1),
                       "team_no": sortie_team,
                       "auto_march": _bool(params.get("auto_march", True)),
                       "stop_on_fatigue": _bool(params.get("stop_on_fatigue", True)),
                       "formation_mode": params.get("formation_mode") or "manual",
                       "formation": params.get("formation") or "鱼鳞阵",
                       "repair_threshold": params.get("repair_threshold") or "light",
                       "repair_on_injury": params.get("repair_on_injury") or "continue",
                       "auto_equip": _bool(params.get("auto_equip", True)),
                       "retreat_before_boss": _bool(params.get("retreat_before_boss", False)),
                       "rotate_captain": _bool(params.get("rotate_captain", False)),
                       "rotate_captain_margin": _i(params, "rotate_captain_margin", 10)}
    elif mode == "osaka":
        sortie_plan = {"mode": "osaka",
                       "team_no": sortie_team,
                       "loops": _i(params, "osaka_runs", 1),
                       "select_floor": _bool(params.get("osaka_select_floor", False)),
                       "target_floor": _i(params, "osaka_target_floor", 81),
                       "formation_mode": params.get("formation_mode") or "manual",
                       "formation": params.get("formation") or "鱼鳞阵",
                       "repair_threshold": params.get("repair_threshold") or "light",
                       "repair_on_injury": params.get("repair_on_injury") or "continue",
                       "auto_equip": _bool(params.get("auto_equip", True))}
    else:
        sortie_plan = {"mode": "none"}
    if mode != "none":
        if sortie_preset:
            sortie_plan["formation_id"] = sortie_preset["id"]
        if sortie_team_error:
            sortie_plan["formation_error"] = sortie_team_error
    from .task_parameters import field_defaults
    practice_plan = {**field_defaults(_SCRIPTS["practice"]["params"]), **(params.get("practice") or {})}
    if practice_plan.get("team_no") not in (None, ""):
        practice_team, practice_preset, practice_error = _resolve_team(practice_plan, 2)
        practice_plan["team_no"] = practice_team
        if practice_preset:
            practice_plan["formation_id"] = practice_preset["id"]
        if practice_error:
            practice_plan["formation_error"] = practice_error
    from .scheduler import find_map, managed_teams
    from .task_parameters import expedition_routes
    expedition_plan = expedition_routes(params.get("expedition") or {}, find_map, managed_teams())
    return steps, after, sortie_plan, practice_plan, expedition_plan


def _build_daily(agent, config_path, params):
    steps, after, sortie_plan, practice_plan, expedition_plan = _daily_plan_inputs(params)
    yield from agent.daily_stream(
        only=steps, after=after, sortie_override=sortie_plan,
        practice_override=practice_plan or None,
        expedition_override=expedition_plan,
        forge_times=_i(params, "forge_times", None),
        forge_recipe=recipe_from_params(params))


def _build_daily_standalone(config_path, params):
    """一键日课独立入口：不套 run 级开工/收工盘点（_wrap_inventory）。

    盘点时机由 daily_stream 自己管：登录落本丸后拍 before、⑫ 步骤拍 after。
    之前套 wrapper 时，开工盘点在打开游戏/登录之前就触发，游戏没开只能
    盲点模拟器桌面，把冷启动登录搞挂。

    但统一收尾约定（回本丸 + 强制 peek）仍然要在整个日课 run 结束时执行
    一次，与 inventory 开关无关；收尾失败不拖垮任务结果。
    例外：after 安排了退出游戏/关模拟器/休眠时跳过收尾——游戏都关了
    还导航回本丸只会撞死在离线设备上（9-04 凌晨翻车冤案：日课全绿，
    收尾对着已关的模拟器抢救 25 分钟，MAA 自裁，整轮被记成 failed）。
    """
    agent = _make_agent(config_path)
    after = params.get("after") or "none"
    try:
        yield from _build_daily(agent, config_path, params)
    finally:
        if after != "none":
            yield f"[日课] 已安排「{after}」下班，跳过收尾回本丸"
        else:
            # --- 统一收尾：回本丸 + 强制顶栏 peek ---
            try:
                yield "[日课] 收尾：导航回本丸"
                for nav_msg in agent.navigate_to_stream("本丸"):
                    yield nav_msg
                if getattr(agent, "current_location", None) == "本丸":
                    yield from _sweep_return_screens(agent, "日课")
                    yield "[日课] 收尾：已回本丸，强制拍一次顶栏"
                    if hasattr(agent, "quick_peek"):
                        agent.quick_peek(tag="日课·收尾", force=True)
                else:
                    yield ("[日课] ⚠️ 收尾没能回到本丸，可能卡在某个界面了，"
                           "去看看")
            except Exception as exc:
                yield f"[日课] ⚠️ 收尾导航/Peek 失败（不影响任务结果）：{exc}"


def _resolve_team(params, default=3) -> tuple[int | None, dict | None, str | None]:
    """解析 team_no：普通 "1"~"5" 照旧；"preset:<fid>" 换成预设指向的部队。
    返回 (team_no, 预设记录, 错误消息)；有错时 team_no/记录都是 None。"""
    from touken.custom_formations import find_formation, load_formations
    raw = params.get("team_no")
    if isinstance(raw, str) and raw.startswith("preset:"):
        record = find_formation(load_formations(), raw[len("preset:"):])
        if record is None:
            return None, None, "找不到这套预设编队（可能已被删），重新选一个"
        return record["target_team"], record, None
    return _i(params, "team_no", default), None, None


def _preset_busy_by_schedule(team_no: int) -> bool:
    """预设要覆盖的队正被排班托管、且当前还在外面远征 → 别去动它。"""
    from .scheduler import expedition_records, load_config, managed_teams, team_available
    cfg = load_config()
    return (team_no in managed_teams(cfg)
            and not team_available(team_no, expedition_records(), time.time()))


def _team_with_preset_stream(agent, params, default=3):
    """玩法 builder 共用的部队解析：选了预设就先把它套进游戏再出阵。
    返回解析后的 team_no；被拒/应用失败时已如实播报并返回 None。"""
    team_no, preset, err = _resolve_team(params, default)
    if err:
        yield f"[预设编队] {err}"
        return None
    if preset is not None:
        from touken.custom_formations import apply_formation_preset_stream
        from .scheduler import TEAM_NAMES
        if _preset_busy_by_schedule(team_no):
            yield (f"[预设编队] {TEAM_NAMES.get(team_no, f'部队{team_no}')}"
                   "正被远征排班用着，无法覆盖；换个队覆盖，或去排班那里调整")
            return None
        ok = yield from apply_formation_preset_stream(agent, preset)
        if not ok:
            return None
    return team_no


def _build_raid(agent, config_path, params):
    team_no = yield from _team_with_preset_stream(agent, params)
    if team_no is None:
        return
    runs = _run_count(params, 3, "rounds")
    yield from agent.raid_stream(
        max_rounds=runs,
        team_no=team_no,
        difficulty_no=_i(params, "map_no", 4),
        auto_buy_ticket=_bool(params.get("auto_refill", False)),
        max_buys=runs,
        auto_march=_bool(params.get("auto_march", True)),
        rotate_captain=_bool(params.get("rotate_captain", False)),
        rotate_captain_margin=_i(params, "rotate_captain_margin", 10))


def _build_pumpkin(agent, config_path, params):
    team_no = yield from _team_with_preset_stream(agent, params)
    if team_no is None:
        return
    difficulty = _i(params, "difficulty", 0)
    watch = _sword_names(params.get("watch"))
    runs = _run_count(params, 4, "max_skips")
    yield from agent.pumpkin_stream(
        team_no=team_no,
        difficulty=difficulty or None,
        watch_names=watch or None,
        max_skips=runs,
        # 南瓜新版的更新令牌购买确认后不会刷新数量、也不会自动关闭弹窗。
        # 前端先保留占位，流程端始终禁用，避免误消费小判后卡死。
        auto_refill=False)


def _build_edocastle(agent, config_path, params):
    team_no = yield from _team_with_preset_stream(agent, params)
    if team_no is None:
        return
    refill = _bool(params.get("use_koban_refill", False))
    runs = _positive_run_count(
        params, 6, "max_runs", "refill_run_limit")
    yield from agent.edocastle_stream(
        team_no=team_no,
        use_koban_refill=refill,
        max_runs=runs,
        formation_mode=params.get("formation_mode") or "manual",
        formation=params.get("formation") or "鱼鳞阵")


def _build_hanafuda(agent, config_path, params):
    team_no = yield from _team_with_preset_stream(agent, params)
    if team_no is None:
        return
    refill = _bool(params.get("use_koban_refill", False))
    runs = _positive_run_count(
        params, 6, "max_runs", "refill_run_limit")
    yield from agent.hanafuda_stream(
        team_no=team_no,
        difficulty=_i(params, "difficulty", 4),
        max_runs=runs,
        auto_refill=refill,
        rotate_captain=_bool(params.get("rotate_captain", False)),
        rotate_captain_margin=_i(params, "rotate_captain_margin", 10))


def _build_sortie(agent, config_path, params):
    team_no = yield from _team_with_preset_stream(agent, params)
    if team_no is None:
        return
    yield from agent.sortie_stream(
        chapter=_i(params, "chapter", 1),
        map_no=_i(params, "map_no", 1),
        team_no=team_no,
        auto_march=_bool(params.get("auto_march", True)),
        stop_on_fatigue=_bool(params.get("stop_on_fatigue", True)),
        max_loops=_run_count(params, 1, "loops"),
        formation_mode=params.get("formation_mode") or "manual",
        formation=params.get("formation") or "鱼鳞阵",
        repair_threshold=params.get("repair_threshold") or "light",
        injury_action=params.get("repair_on_injury") or "continue",
        auto_equip=_bool(params.get("auto_equip", True)),
        retreat_before_boss=_bool(params.get("retreat_before_boss", False)),
        rotate_captain=_bool(params.get("rotate_captain", False)),
        rotate_captain_margin=_i(params, "rotate_captain_margin", 10))


def _build_yosari(agent, config_path, params):
    team_no = yield from _team_with_preset_stream(agent, params)
    if team_no is None:
        return
    yield from agent.yosari_stream(
        map_no=_i(params, "map_no", 1),
        team_no=team_no,
        auto_march=_bool(params.get("auto_march", True)),
        stop_on_fatigue=_bool(params.get("stop_on_fatigue", True)),
        auto_refill=_bool(params.get("auto_refill", False)),
        max_loops=_run_count(params, 1, "loops"),
        formation_mode=params.get("formation_mode") or "manual",
        formation=params.get("formation") or "鱼鳞阵",
        repair_threshold=params.get("repair_threshold") or "light",
        injury_action=params.get("repair_on_injury") or "continue",
        auto_equip=_bool(params.get("auto_equip", True)),
        rotate_captain=_bool(params.get("rotate_captain", False)),
        rotate_captain_margin=_i(params, "rotate_captain_margin", 10))


def _build_osaka(agent, config_path, params):
    team_no = yield from _team_with_preset_stream(agent, params)
    if team_no is None:
        return
    # 小判掉落率实验由 osaka_stream 自带记账（开关沿用面板 compare_resources）
    yield from agent.osaka_stream(
        max_floors=_run_count(params, 1, "floors"),
        team_no=team_no,
        select_floor=_bool(params.get("select_floor", False)),
        target_floor=_i(params, "target_floor", 81),
        formation_mode=params.get("formation_mode") or "manual",
        formation=params.get("formation") or "鱼鳞阵",
        repair_threshold=params.get("repair_threshold") or "light",
        injury_action=params.get("repair_on_injury") or "continue",
        auto_equip=_bool(params.get("auto_equip", True)),
        koban_science=_bool(params.get("compare_resources", True)))


def _build_sakura(agent, config_path, params):
    if str(params.get("team_no", "1")) not in ("1", "2", "3", "4", "5"):
        yield "[刷花] 请先选择部队一到五；全本丸轮刷不使用预设名单，未清队"
        return
    yield from agent.sakura_stream(
        team_no=_i(params, "team_no", 1),
        slot=1, sword_count=_i(params, "sword_count", 1),
        repair_threshold=params.get("repair_threshold") or "light")


def _build_sword_inventory(agent, config_path, params):
    yield from agent.sword_inventory_stream()


def _build_forge(agent, config_path, params):
    if str(params.get("forge_limited")) == "true":
        yield from _build_forge_limited(agent, config_path, params)
        return
    watch_raw = params.get("watch") or ""
    if isinstance(watch_raw, list):
        # Agent 网关传的是数组 ["03:20:00", ...]
        watch = [str(w).strip() for w in watch_raw if str(w).strip()]
    else:
        # 面板传的是字符串 "03:20:00, 04:00:00"
        watch = [w.strip() for w in re.split(r"[，,、;；\s]+", str(watch_raw)) if w.strip()]
    yield from agent.forge_stream(
        times=_i(params, "times", 3), watch=watch,
        recipe=recipe_from_params(params))


def _build_forge_limited(agent, config_path, params):
    """十连限锻：times 字段此时是总把数（按十连取整），watch_names 是目标刀名"""
    names_raw = params.get("watch_names") or ""
    if isinstance(names_raw, list):
        names = [str(w).strip() for w in names_raw if str(w).strip()]
    else:
        names = [w.strip() for w in re.split(r"[，,、;；\s]+", str(names_raw)) if w.strip()]
    yield from agent.limited_forge_stream(
        total=_i(params, "times", 50),
        recipe=recipe_from_params(params),
        watch_names=names,
        stop_on_hit=_bool(params.get("stop_on_hit", True)),
        capacity_action=str(params.get("capacity_action") or "stop"))


def _build_repair(agent, config_path, params):
    team_names = {f"部队{i}": i for i in range(1, 6)}
    raw_teams = params.get("speedup_teams")
    speedup_teams = None
    if isinstance(raw_teams, list):
        speedup_teams = [team_names[x] for x in raw_teams if x in team_names]
    yield from agent.repair_stream(
        dry_run=_bool(params.get("dry_run", False)),
        speedup_teams=speedup_teams)


def _write_dispatch_result(key: str, outcome: str, detail: str = ""):
    """派遣结果落盘：排班靠它区分「门卫拒了（refused）」和「点了但没成」。"""
    try:
        (STATUS_DIR / "dispatch_result.json").write_text(json.dumps({
            "key": key, "outcome": outcome,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "detail": detail,
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _dispatch_failure_detail(message: str) -> str:
    """Turn the dispatch flow's terminal line into one player-facing reason."""
    detail = str(message or "").strip()
    if detail.startswith("[远征]"):
        detail = detail[len("[远征]"):].strip()
    return detail or "派遣流程提前停止，原因没有读清"


def _build_dispatch(agent, config_path, params):
    """排班派遣：刷新结算；临近归来最多等十分钟；绝不启动模拟器。

    scheduled（排班触发）先过接管门卫：只读认当前位置，认得出本丸才继续；
    认不出（战斗中/未知界面 = 多半主人在手动玩）写 refused 结果文件后正常退出。
    """
    from .scheduler import find_map
    code = params.get("map_code") or ""
    team_no = _i(params, "team_no", 2)
    slot_key = str(params.get("slot_key") or "")
    scheduled = bool(params.get("scheduled"))
    m = find_map(code)
    if not m:
        yield f"[远征] 不知道图 {code} 是哪张，没派"
        return
    if scheduled:
        agent.maa.screenshot(force=True)
        if not agent.maa.exists("目录.png", threshold=0.7):
            detail = "画面不在本丸，像主人在手动玩"
            _write_dispatch_result(slot_key, "refused", detail)
            yield f"[远征] {detail}，这班交回排班"
            return
    records = _read_expedition_records()
    remain = _expedition_remaining(records.get(str(team_no), {}))
    if params.get("scheduled") and remain > 0:
        _write_dispatch_result(slot_key, "refused", "队伍仍在外面远征")
        yield "[远征] 队伍仍在外面，交回排班等待，不占用任务位置"
        return
    if remain > 600:
        yield f"[远征] 部队{team_no}还剩 {remain // 60} 分钟，超过十分钟，本次跳过"
        return
    while remain > 0:
        yield f"[远征等待] 部队{team_no}还剩 {remain // 60:02d}:{remain % 60:02d}（紧急停止可取消）"
        time.sleep(min(5, remain))
        remain = _expedition_remaining(_read_expedition_records().get(str(team_no), {}))
    yield from agent.collect_expedition_stream(redispatch=None)
    from .expedition_observation import load_observations, visible_records
    remaining_records = visible_records(_read_expedition_records(), load_observations())
    if str(team_no) in remaining_records:
        detail = "尚未确认这支部队已收菜，暂不续派"
        if scheduled:
            _write_dispatch_result(slot_key, "refused", detail)
        yield f"[远征] {detail}"
        return
    from touken.expedition_sakura import recover_stream
    if not (yield from recover_stream(agent)):
        if scheduled:
            _write_dispatch_result(slot_key, 'failed', '上次补花队伍尚未恢复')
        return
    if params.get("formation_id"):
        from touken.runtime_paths import STATE_DIR
        from .expedition_advisor import (expedition_formation_options, party_levels_from_situation,
                                         _level_ok, _type_shortfall, load_maps)
        from touken.custom_formations import apply_formation_preset_stream
        candidates = expedition_formation_options(team_no, party_levels_from_situation(
            STATE_DIR / "youzu_home_situation.json"))
        candidate = next((item for item in candidates
                          if item["formation_id"] == params["formation_id"]
                          and item["formation_signature"] == params.get("formation_signature")), None)
        meta = load_maps().get(code, {})
        if not candidate or not _level_ok(meta, candidate["party"]) or _type_shortfall(meta, candidate["party"]) is not None:
            detail = "预设已变更、刀剑被占用或远征条件不满足，请重新安排"
            if scheduled:
                _write_dispatch_result(slot_key, "failed", detail)
            yield f"[远征] ✗ {detail}，本次不换队、不派出"
            return
        applied = yield from apply_formation_preset_stream(agent, candidate["record"])
        if not applied:
            if scheduled:
                _write_dispatch_result(slot_key, "failed", "预设没有套好，本次不派出")
            yield "[远征] ✗ 预设没有套好，本次不派出"
            return
    from .scheduler import load_config
    training = load_config().get('automation', {}).get('sakura_before_dispatch', False)
    injury = (_load_panel_settings().get('params', {}).get('sakura', {}) or {}).get('repair_threshold', 'light')
    if scheduled and ':adhoc:' in slot_key:
        training = params.get('sakura_before_dispatch', False)
        injury = params.get('repair_threshold', 'light')
        if type(training) is not bool or injury not in ('light', 'medium', 'heavy'):
            _write_dispatch_result(slot_key, 'failed', '这班的补花设置无效，请重新配置')
            yield '[远征] ✗ 这班的补花设置无效，暂不派遣'
            return
    if scheduled and training:
        yield '[远征] 这班先补花，恢复原队伍后出发；归来时间按实际出发计算'
    dispatch_messages = []
    for message in agent.expedition_stream(
            era=m["era"], map_slot=m["slot"], team_no=team_no,
            sakura_before_dispatch=training, repair_threshold=injury):
        dispatch_messages.append(str(message))
        yield message
    if scheduled:
        terminal = dispatch_messages[-1] if dispatch_messages else ""
        if "✅" in terminal and "已出发" in terminal:
            _write_dispatch_result(slot_key, "done", "已看到部队出发")
            # 确认仍以 expeditions.json 的 dispatched_at 变化为准，结果文件只是辅助
        else:
            # 流程自己已经明确停下时，不能再伪装成「流程走完」交给排班反复重试。
            # 这里保留最后一句具体原因，排班将本班终止并醒目提醒玩家。
            _write_dispatch_result(
                slot_key, "failed", _dispatch_failure_detail(terminal))


def _expedition_remaining(record: dict) -> int:
    """派遣记录剩余秒数；过期或读不懂按 0。"""
    try:
        started = time.mktime(time.strptime(
            record["dispatched_at"], "%Y-%m-%d %H:%M:%S"))
        return max(0, int(started + int(record["duration_min"]) * 60 - time.time()))
    except Exception:
        return 0


def _read_expedition_records() -> dict:
    path = STATUS_DIR / "expeditions.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _build_expedition_manager(agent, config_path, params):
    """收取归来队伍，最多等十分钟，再按“常用安排”派遣。"""
    from .scheduler import find_map, load_config, managed_teams

    plan = [p for p in (params["_routes"] if "_routes" in params else load_config().get("common_plan", []))
            if p.get("enabled") and p.get("map_code")]
    owned = managed_teams()
    plan = [p for p in plan if int(p["team_no"]) not in owned]
    if owned:
        yield "[远征管理] 自动排班负责的队伍只收归来奖励，续派交给排班"
    yield "[远征管理] 先回本丸刷新归来状态并领取结算"
    yield from agent.collect_expedition_stream(redispatch=None)
    from touken.expedition_sakura import recover_stream
    if not (yield from recover_stream(agent)):
        return

    records = _read_expedition_records()
    waitable = []
    skipped = set()
    for row in plan:
        team = str(row["team_no"])
        remain = _expedition_remaining(records.get(team, {})) if team in records else 0
        if 0 < remain <= 600:
            waitable.append((team, remain))
            yield f"[远征管理] 部队{team}还剩 {remain // 60}分{remain % 60:02d}秒，进入等待"
        elif remain > 600:
            skipped.add(team)
            yield (f"[远征管理] 部队{team}还剩 {remain // 60}分{remain % 60:02d}秒，"
                   "超过十分钟，本次跳过")
        else:
            yield f"[远征管理] 部队{team}已归来或空闲，可以派遣"

    if waitable:
        deadline = time.time() + max(remain for _, remain in waitable)
        while time.time() < deadline:
            left_lines = []
            for team, original in waitable:
                left = max(0, int(deadline - time.time()
                                 - (max(r for _, r in waitable) - original)))
                left_lines.append(f"部队{team} {left // 60:02d}:{left % 60:02d}")
            yield "[远征等待] " + " · ".join(left_lines) + "（紧急停止可取消）"
            time.sleep(min(5, max(0.2, deadline - time.time())))
        yield "[远征管理] 等待结束，再回本丸结算"
        yield from agent.collect_expedition_stream(redispatch=None)
        records = _read_expedition_records()

    for row in plan:
        team = str(row["team_no"])
        if team in skipped:
            continue
        remain = _expedition_remaining(records.get(team, {})) if team in records else 0
        if remain > 0:
            yield f"[远征管理] 部队{team}仍显示远征中，本次不碰"
            continue
        m = find_map(row["map_code"])
        if not m:
            yield f"[远征管理] 部队{team}的地图 {row['map_code']} 不存在，跳过"
            continue
        formation_id = str(row.get("formation_id") or "")
        if formation_id:
            from touken.custom_formations import apply_formation_preset_by_id_stream
            applied = yield from apply_formation_preset_by_id_stream(
                agent, formation_id, expected_team=int(team))
            if not applied:
                yield f"[远征管理] ✗ 部队{team}的预设没套好，本次不派这队"
                continue
        yield f"[远征管理] 派部队{team}去 {m['code']}「{m['name']}」"
        yield from agent.expedition_stream(
            era=m["era"], map_slot=m["slot"], team_no=int(team),
            sakura_before_dispatch=(row.get('sakura_before_dispatch', False) if '_routes' in params else load_config().get('automation', {}).get('sakura_before_dispatch', False)),
            repair_threshold=(row.get('repair_threshold', 'light') if '_routes' in params else (_load_panel_settings().get('params', {}).get('sakura', {}) or {}).get('repair_threshold', 'light')))

    yield "[远征管理] 常用安排处理完毕"


def _build_simple(stream_method_name):
    def _run(agent, config_path, params):
        method = getattr(agent, stream_method_name, None)
        if method is None:
            raise RuntimeError(f"Agent 没有 {stream_method_name} 方法")
        yield from method()
    return _run


def _build_formation(agent, config_path, params):
    """编队换人接线：把前端从本丸档案里选好的完整目标对象原样交给
    共用编队执行器。匹配/翻页/同名裁决全在
    touken/flows/formation_editor.py，这层不做任何识别，
    也不包装、不粉饰结果。

    match_fields 为什么收窄成 ("name", "level")：刀剑男士选择列表没有
    形态直读通道（行 form 恒 None）。档案条目的形态结论是独立的
    form_status（kiwame_date 只是显现日期，不参与形态）；一旦某振
    形态被档案确认，按默认 (name, form, level) 匹配就会因页面缺形态
    证据一路 ambiguous 永远换不成。name+level 是列表真实可见、且
    候选池完整档案必然携带的身份证据；同名同等级拉不开、或有读不清
    的行，执行器照样安全拒绝，不在这条窄证据链上放水。"""
    try:
        team_no = int(params.get("team_no"))
        slot_no = int(params.get("slot_no"))
    except (TypeError, ValueError):
        yield "[编队] 请求无效：部队或位置的编号不是数字，这次不换人"
        return
    target = params.get("target")
    if not isinstance(target, dict) or not (
            target.get("sword_catalog_id")
            or target.get("name") or target.get("name_zh")):
        yield "[编队] 请求无效：换人目标缺身份信息，这次不换人"
        return
    yield from agent.ensure_team_member_from_honmaru_stream(
        team_no, slot_no, target, match_fields=("name", "level"))


register_script("daily", "一键日课", "",
                 _build_daily_standalone,
                 params=[{"key": "steps", "type": "checks", "label": "要干的活（不勾的不跑）",
                          "options": _DAILY_STEPS, "default": _DAILY_STEPS,
                           "help": "这里的出阵安排是日课专用配置，不会修改各玩法的单独配置。"},
                        {"key": "forge_times", "type": "number", "label": "锻刀次数",
                         "default": 3, "min": 1, "max": 200,
                         "help": "日课点火的目标炉数。只使用空闲炉、不消耗加速符；炉位不够时实际次数会少于设定值。上限与锻刀积木一致。"},
                        *recipe_fields(),
                        {"key": "sortie_mode", "type": "select", "label": "出阵安排",
                         "options": [["none", "不出阵"],
                                     ["raid", "联队战"],
                                     ["pumpkin", "南瓜大作战"],
                                     ["yosari", "异去"],
                                     ["osaka", "大阪城挖地"],
                                     ["sortie", "合战场推图"]],
                         "default": "none",
                         "help": "行军、阵形、伤势处理由下方的日课专属字段决定；「配置」页只是单跑该玩法时的设置，两边互不影响。"},
                        {"key": "team_no", "type": "select", "label": "出阵部队",
                         "options": _TEAM_OPTIONS, "default": "3",
                         "visibleWhen": {"key": "sortie_mode", "not": "none"}},
                        {"key": "raid_rounds", "type": "number", "label": "出阵次数",
                         "default": 3, "min": 1, "max": 99,
                         "help": "一键日课会使用小判补充手形，跑够本次次数；单跑的补充开关不影响这里。",
                         "visibleWhen": {"key": "sortie_mode", "is": "raid"}},
                        {"key": "pumpkin_difficulty", "type": "select", "label": "难度",
                         "options": [["1", "低级"], ["2", "中级"], ["3", "高级"]],
                         "default": "1",
                         "visibleWhen": {"key": "sortie_mode", "is": "pumpkin"}},
                        {"key": "pumpkin_runs", "type": "number", "label": "出阵次数",
                         "default": 4, "min": 1, "max": 99,
                         "visibleWhen": {"key": "sortie_mode", "is": "pumpkin"}},
                        {"key": "yosari_map_no", "type": "select", "label": "小图",
                         "options": [[str(i), f"{i}图"] for i in range(1, 5)],
                         "default": "1",
                         "visibleWhen": {"key": "sortie_mode", "is": "yosari"}},
                        {"key": "yosari_runs", "type": "number", "label": "出阵次数",
                         "default": 1, "min": 1, "max": 99,
                         "visibleWhen": {"key": "sortie_mode", "is": "yosari"}},
                        {"key": "yosari_auto_refill", "type": "toggle",
                         "label": "是否自动补充手形？", "default": False,
                         "help": "开启后，手形不足时将自动使用小判补充，直到完成设定的出阵次数。关闭后，手形不足时结束任务，不消耗小判。",
                         "visibleWhen": {"key": "sortie_mode", "is": "yosari"}},
                        {"key": "osaka_runs", "type": "number", "label": "出阵次数",
                         "default": 1, "min": 1, "max": 99,
                         "visibleWhen": {"key": "sortie_mode", "is": "osaka"}},
                        {"key": "osaka_select_floor", "type": "toggle",
                         "label": "指定挂机层数", "default": False,
                         "visibleWhen": {"key": "sortie_mode", "is": "osaka"}},
                        {"key": "osaka_target_floor", "type": "number",
                         "label": "指定层数", "default": 81, "min": 1, "max": 99,
                         "help": "只有开启“指定挂机层数”时才会使用。",
                         "visibleWhen": {"key": "sortie_mode", "is": "osaka"}},
                        {"key": "chapter", "type": "select", "label": "章节",
                         "options": [[str(i), f"{i}章"] for i in range(1, 9)],
                         "default": "1",
                         "visibleWhen": {"key": "sortie_mode", "is": "sortie"}},
                        {"key": "map_no", "type": "select", "label": "小图",
                         "options": [[str(i), f"{i}图"] for i in range(1, 5)],
                         "default": "1",
                         "visibleWhen": {"key": "sortie_mode", "is": "sortie"}},
                        {"key": "loops", "type": "number", "label": "连打几圈",
                         "default": 1, "min": 1, "max": 99,
                         "visibleWhen": {"key": "sortie_mode", "is": "sortie"}},
                        {"key": "retreat_before_boss", "type": "toggle",
                         "label": "王点前撤退", "default": False,
                         "help": "关闭自动行军后生效：下一步将进入王点时主动返回本丸，适合反复进图练级。小地图无法确认时会继续行军。",
                         "visibleWhen": {"key": "sortie_mode", "is": "sortie"}},
                        # ── 战斗行为：日课自管，与「配置」页彻底切割 ──
                        # （issue#7：日课没安排修刀却自动修——旧设计从配置页继承
                        # 伤势设置，太绕。升级时 _migrate_daily_battle_settings
                        # 会把配置页现状快照进来，之后两边互不影响。）
                        {"key": "auto_march", "type": "toggle",
                         "label": "是否使用自动行军", "default": True,
                         "visibleWhen": {"key": "sortie_mode",
                                         "is_any": ["yosari", "sortie"]}},
                        {"key": "stop_on_fatigue", "type": "toggle", "label": "重疲劳时停止",
                         "default": True, "visibleWhen": {"all": [
                             {"key": "sortie_mode", "is_any": ["yosari", "sortie"]},
                             {"key": "auto_march", "is": "true"}]}},
                        {"key": "formation_mode", "type": "select",
                         "label": "阵形选择方式",
                         "options": [["manual", "手动阵形"],
                                     ["auto", "自动阵形"]],
                         "default": "manual",
                         "visibleWhen": {"any": [
                             {"key": "sortie_mode", "is": "osaka"},
                             {"all": [{"key": "sortie_mode",
                                       "is_any": ["yosari", "sortie"]},
                                      {"key": "auto_march", "is": "false"}]}]}},
                        {"key": "formation", "type": "select",
                         "label": "固定或识别失败时的兜底阵形",
                         "options": [[name, name] for name in
                                     ["鱼鳞阵", "横队阵", "雁行阵", "鹤翼阵", "方阵", "逆行阵"]],
                         "default": "鱼鳞阵",
                         "visibleWhen": {"key": "sortie_mode", "is_any": ["yosari", "sortie", "osaka"]}},
                        {"key": "repair_threshold", "type": "select",
                         "label": "伤势停止条件",
                         "options": [["light", "轻伤时停止"],
                                     ["medium", "中伤时停止"],
                                     ["heavy", "重伤时停止"]],
                         "default": "light",
                         "visibleWhen": {"key": "sortie_mode",
                                         "is_any": ["yosari", "sortie", "osaka"]}},
                        {"key": "repair_on_injury", "type": "select",
                         "label": "停止后的处理",
                         "options": [["continue", "手入加速后继续剩余圈数"],
                                     ["repair_stop", "手入后停止任务"],
                                     ["stop", "停止任务，不进行手入"]],
                         "default": "continue",
                         "visibleWhen": {"key": "sortie_mode",
                                         "is_any": ["yosari", "sortie", "osaka"]}},
                        {"key": "auto_equip", "type": "toggle",
                         "label": "是否自动补充刀装", "default": True,
                         "help": "任务首次出阵前将当前部队保存到记录一；出现刀装未满提示时，自动用记录一补齐并重新检查伤势。",
                         "visibleWhen": {"key": "sortie_mode",
                                         "is_any": ["yosari", "sortie", "osaka"]}},
                        {"key": "rotate_captain", "type": "toggle",
                         "label": "自动换队长", "default": False,
                         "help": "出阵前读全队疲劳，把疲劳最低的拖到队长位吃加成（保花用）。",
                         "visibleWhen": {"key": "sortie_mode",
                                         "is_any": ["yosari", "sortie"]}},
                        {"key": "rotate_captain_margin", "type": "select",
                         "label": "换队长阈值",
                         "options": [["5", "相差 5 点"], ["10", "相差 10 点"],
                                     ["20", "相差 20 点"]],
                         "default": "10",
                         "help": "全队最低疲劳比当前队长低到这个差值时才换，避免差距很小时频繁调整。",
                         "visibleWhen": {"all": [
                             {"key": "sortie_mode", "is_any": ["yosari", "sortie"]},
                             {"key": "rotate_captain", "is": True}]}},
                        {"key": "after", "type": "select", "label": "跑完后（默认啥也不干）",
                         "options": [["none", "啥也不干"],
                                     ["logout", "退出游戏"],
                                     ["shutdown", "退出游戏 + 关模拟器"],
                                     ["sleep", "退出 + 关模拟器 + 电脑休眠"]],
                         "default": "none"}])
register_script("raid", "联队战", "",
                _wrap_inventory("RAID", _build_raid),
                params=[{"key": "map_no", "type": "select", "label": "打哪张图",
                         "options": [["1", "1图（坐标待补）"], ["2", "2图（坐标待补）"],
                                     ["3", "3图（坐标待补）"], ["4", "4图"]],
                         "default": "4"},
                        _team_field("3"),
                        _run_count_field(ticket=True, default=3),
                        _ticket_refill_field(),
                        {"key": "auto_march", "type": "toggle",
                         "label": "自动行军委托", "default": True,
                         "help": "每圈出阵前尝试委托自动行军；"
                                 "游戏未允许委托或没能确认勾选时，本圈改为手动打法。"},
                        *_captain_rotation_fields()])
register_script("pumpkin", "南瓜大作战", "刮刮乐刷剪影，能认出是哪把刀，不想要的自动烧令牌换板子",
                _wrap_inventory("南瓜", _build_pumpkin),
                params=[{"key": "difficulty", "type": "select", "label": "打哪张图",
                         "options": [["1", "低级"], ["2", "中级"], ["3", "高级"]],
                         "default": "1"},
                        _team_field("3"),
                        _run_count_field(ticket=True, default=4),
                        {**_ticket_refill_field(),
                         "help": "占位功能，当前暂不执行自动补充。新版南瓜的更新令牌购买后不会正确刷新并关闭弹窗，为避免误消费小判，令牌不足时脚本仍会安全结束。"}])
register_script("edocastle", "江户城潜入调查", "难度四巡游：踩点、钥匙、王点一套带走",
                _wrap_inventory("江户城", _build_edocastle),
                params=[_team_field("3"),
                        _run_count_field(default=6),
                        {"key": "use_koban_refill", "type": "toggle",
                         "label": "是否补充手形", "default": False,
                         "help": "关闭时，现有手形不够完成设定次数便提前收工；开启后才会使用小判补充。"},
                        *_formation_fields()])
register_script("hanafuda", "秘宝之里", "花牌收集：按设定次数出阵，令牌不足时安全收工",
                _wrap_inventory("花札", _build_hanafuda),
                params=[{"key": "difficulty", "type": "select", "label": "打哪个难度",
                         "options": [["1", "难度·易"], ["2", "难度·普"],
                                     ["3", "难度·难"], ["4", "难度·超难"]],
                         "default": "4"},
                        _team_field("3"),
                        _run_count_field(default=6),
                        {"key": "use_koban_refill", "type": "toggle",
                         "label": "是否补充通行令牌", "default": False,
                         "help": "关闭时，现有令牌不够完成设定次数便提前收工；开启后才会使用小判补充。"},
                        *_captain_rotation_fields()])
register_script("sortie", "合战场", "单跑合战场：这里的设置只对本次单跑生效，与一键日课/工作流互不影响",
                _wrap_inventory("出阵", _build_sortie),
                params=[{"key": "chapter", "type": "select", "label": "章节",
                         "options": [[str(i), f"{i}章"] for i in range(1, 9)], "default": "1"},
                        {"key": "map_no", "type": "select", "label": "小图",
                         "options": [[str(i), f"{i}图"] for i in range(1, 5)], "default": "1"},
                        _team_field("3"),
                        _run_count_field(),
                        *_march_and_injury_fields(),
                        {"key": "retreat_before_boss", "type": "toggle",
                         "label": "王点前撤退", "default": False,
                         "help": "脚本手动行军时，看小地图算步数：距王点一步就主动返回本丸，反复进图练级。认不出地图时会照常行军，不会乱撤。需要安装 opencv。",
                         "visibleWhen": {"key": "auto_march", "is": "false"}}])
register_script("yosari", "异去", "单跑异去：这里的设置只对本次单跑生效，与一键日课/工作流互不影响",
                _wrap_inventory("异去", _build_yosari),
                params=[{"key": "chapter", "type": "select", "label": "章节",
                         "options": [["1", "1章"]], "default": "1",
                         "help": "异去目前只开放第一章；以后新增章节会在这里继续添加。"},
                        {"key": "map_no", "type": "select", "label": "小图",
                         "options": [[str(i), f"{i}图"] for i in range(1, 5)], "default": "1"},
                        _team_field("3"),
                        _run_count_field(ticket=True),
                        _ticket_refill_field(),
                        *_march_and_injury_fields(),
                        ])
register_script("osaka", "大阪城挖地", "逐层手动行军；没有自动行军，也不会消耗手形。这里的设置只对单跑生效，与一键日课/工作流互不影响",
                _wrap_inventory("挖地", _build_osaka),
                params=[_team_field("3"),
                        {**_run_count_field(), "label": "出阵次数"},
                        {"key": "compare_resources", "type": "toggle",
                         "label": "小判掉落率实验", "default": True,
                         "help": "开工和收场时各读一次小判，差值和层数记进日志（测掉落概率用）；识别失败不影响挖地。"},
                        {"key": "select_floor", "type": "toggle",
                         "label": "指定挂机层数", "default": False},
                        {"key": "target_floor", "type": "number",
                         "label": "指定层数", "default": 81,
                         "min": 1, "max": 99,
                         "visibleWhen": {"key": "select_floor", "is": True}},
                        {"key": "formation_mode", "type": "select",
                         "label": "阵形选择方式",
                         "options": [["manual", "手动阵形"],
                                     ["auto", "自动阵形"]],
                         "default": "manual"},
                        {"key": "formation", "type": "select",
                         "label": "固定或识别失败时的兜底阵形",
                         "options": [[name, name] for name in
                                     ["鱼鳞阵", "横队阵", "雁行阵", "鹤翼阵", "方阵", "逆行阵"]],
                         "default": "鱼鳞阵"},
                        {"key": "repair_threshold", "type": "select",
                         "label": "伤势停止条件",
                         "options": [["light", "轻伤时停止"],
                                     ["medium", "中伤时停止"],
                                     ["heavy", "重伤时停止"]],
                         "default": "light"},
                        {"key": "repair_on_injury", "type": "select",
                         "label": "停止后的处理",
                         "options": [["continue", "手入加速后继续剩余层数"],
                                     ["repair_stop", "手入后停止任务"],
                                     ["stop", "返回本丸，不进行手入"]],
                         "default": "continue"},
                        {"key": "auto_equip", "type": "toggle",
                         "label": "是否自动补充刀装", "default": True,
                         "help": "任务首次出阵前保存到记录一；出现刀装未满提示时，自动用记录一补齐并重新检查伤势。"}])
register_script("sakura", "刷花", "队长单挑 1-1 刷疲劳到 100，满了自动换人",
                _wrap_inventory("刷花", _build_sakura),
                params=[_team_field("1"),
                        next(field for field in _march_and_injury_fields()
                             if field["key"] == "repair_threshold"),
                        {"key": "sword_count", "type": "number", "label": "本次刷几振",
                         "default": 1, "min": 1, "max": 1000,
                         "help": "先解散所选部队的队员，再替换队长；按樱吹雪升序，只选已上锁、等级大于1、疲劳≤49的刀，不按标签筛选。每振刷到100后卸装换人。"}])
def _build_practice(agent, config_path, params):
    # 面板单跑演练：真打 + 部队可选（_build_simple 裸调会掉进 dry_run 认人演习模式）
    team_no = yield from _team_with_preset_stream(agent, params, default=2)
    if team_no is None:
        return
    yield from agent.practice_stream(
        dry_run=False,
        team_no=team_no,
        formation_mode=params.get("formation_mode") or "manual",
        formation=params.get("formation") or "逆行阵")


register_script("practice", "演练", "",
                _wrap_inventory("演练", _build_practice),
                params=[
                    _team_field("2"),
                    {"key": "formation_mode", "type": "select",
                     "label": "阵形选择方式",
                     "options": [["manual", "手动选择"],
                                 ["auto", "自动阵形"]],
                     "default": "manual",
                     },
                    {"key": "formation", "type": "select",
                     "label": "固定或识别失败时的兜底阵形",
                     "options": [[name, name] for name in
                                 ["鱼鳞阵", "横队阵", "雁行阵",
                                  "鹤翼阵", "方阵", "逆行阵"]],
                     "default": "逆行阵"},
                ])
register_script("expedition", "远征", "收菜、等待临近归来，并按常用安排派遣",
                _wrap_inventory("远征管理", _build_expedition_manager))


def _map_select_field():
    from .scheduler import map_options
    opts = [[o["code"], f'{o["code"]} · {o["name"]}（{o["duration_text"]}）']
            for o in map_options()]
    return {"key": "map_code", "type": "select", "label": "远征图",
            "options": opts, "default": opts[0][0] if opts else ""}


register_script("dispatch", "派遣远征", "立刻派一支部队去指定远征图",
                _wrap_inventory("派遣", _build_dispatch, scheduled_startup=True),
                params=[_team_field("2"), _map_select_field()], hidden=True)
register_script("forge", "锻刀", "收完成的刀，再给空闲炉点火；普通锻刀不使用加速符，十连限锻才烧",
                _wrap_inventory("锻刀", _build_forge),
                params=[{"key": "times", "type": "number", "label": "锻刀数量",
                         "default": 3, "min": 1, "max": 200,
                         "help": "普通锻刀：目标炉数，只使用空闲炉、不消耗加速符，炉位不够时实际次数会少于设定值。十连限锻：要锻的总把数，按十连取整（50=5发十连，不足10把按一发算）。"},
                        {"key": "forge_limited", "type": "select", "label": "锻刀方式",
                         "options": [["false", "普通锻刀（等炉子，不烧加速符）"],
                                     ["true", "十连限锻（瞬间出货，烧加速符）"]],
                         "default": "false",
                         "help": "十连限锻是限锻活动期间冲目标刀用的：每发十连=9委托符+10加速符+配方×10的四资源，十把刀直接进刀位。加速符不可再生，下手前看好库存。"},
                        *recipe_fields(),
                        {"key": "watch", "type": "duration-list",
                         "label": "目标时长（命中时手机报喜，不添加则不盯）",
                         "default": "",
                         "visibleWhen": {"key": "forge_limited", "is": "false"}},
                        {"key": "watch_names", "type": "text",
                         "label": "目标刀剑（锻出就手机报喜）",
                         "swords": True, "default": "",
                         "placeholder": "多个名字用逗号分隔",
                         "visibleWhen": {"key": "forge_limited", "is": "true"}},
                        {"key": "stop_on_hit", "type": "toggle",
                         "label": "锻出目标就收手", "default": True,
                         "help": "每发十连都会先揭榜确认；开着时目标刀一出货就停，保住剩下的加速符。关着则锻满设定把数。",
                         "visibleWhen": {"key": "forge_limited", "is": "true"}},
                        {"key": "capacity_action", "type": "select",
                         "label": "刀位不足时",
                         "options": [["stop", "停下等我（最安全）"],
                                     ["dismantle", "按白名单刀解所需数量"],
                                     ["sugar", "习合现有重刀后复查"]],
                         "default": "stop",
                         "help": "默认不会擅自动刀。刀解只尝试腾出下一发所缺的位置；习合只使用当前刀帐里的可用重刀，不会收取邮箱。处理后仍会重新读取刀位，不够或读不清都不会点火。",
                         "visibleWhen": {"key": "forge_limited", "is": "true"}}])
register_script("repair", "手入", "单独扫描受伤刀剑；黑名单跳过，其余按部队决定是否加速",
                _wrap_inventory("手入", _build_repair),
                params=[{"key": "dry_run", "type": "select",
                         "label": "运行方式",
                         "options": [["false", "实际手入"],
                                     ["true", "只扫描并报告（不点击）"]],
                         "default": "false",
                         "help": "只扫描会报告每把刀的处理方式，不会点击任何按钮。"},
                        {"key": "speedup_teams", "type": "checks",
                         "label": "单独手入时，使用加速符的部队",
                         "options": ["部队一", "部队二", "部队三", "部队四", "部队五"],
                         "default": ["部队三"],
                         "help": "这里只影响单独运行“手入”：黑名单始终跳过，选中部队即时修好，其他队只安排普通手入。连续出阵选择“自动手入后继续”时，会自动加速当前出阵队，不读取这里。"}],
                hidden=True)
register_script("sugar", "炼糖", "收件箱清狗粮 + 习合循环",
                _wrap_inventory("炼糖", _build_simple("sugar_stream")))
register_script("inbox_supplies", "收杂物箱",
                "收件箱只收资源/货币/便利道具/其他物品，刀剑邮件原样躺着",
                _wrap_inventory("收杂物", _build_simple("inbox_supplies_stream"),
                                inventory=True))  # 收的都是资源，收完必拍家底
register_script("snapshot", "库存快照",
                "手动拍一次完整家底（含小判）刷新看板；日常已由锻刀收工顺手拍+顶栏顺路更新覆盖，想立刻刷新看板才用",
                _wrap_inventory("库存", _build_simple("status_snapshot_stream"),
                                inventory=False))


def _build_game_inventory(config_path, params):
    from .game_inventory import refresh_game_inventory
    yield from refresh_game_inventory(config_path, params, make_agent=_make_agent)


register_script("game_inventory", "读取游戏家底",
                "读取游戏记录，再通过画面盘点资源、小判和符；不执行日课",
                _build_game_inventory)
register_script("sword_inventory", "刀帐盘点",
                "走进刀剑男士一览，逐页认出每把刀的等级、疲劳和属性记成快照；全程只看不点，怕漏会如实报缺口",
                _wrap_inventory("刀帐盘点", _build_sword_inventory))
# 旧的单格换人入口只为兼容既有调用保留；部队预设页不再直接触发它。
# target 是整支档案条目对象，任务表单画不出来，故对任务列表隐藏（仍可运行）。
register_script("formation", "编队换人",
                "把指定部队的指定位置换成本丸档案里选好的那振刀；点决定后只确认选人列表正常关闭，不回读当前编队",
                _wrap_inventory("编队", _build_formation),
                hidden=True)


# ── 自定义工作流（乐高排班）──
# 出阵类积木的参数 schema 直接复用上面各 register_script 的注册内容，run 复用
# 各 _build_* builder；参数由节点自己保存，旧参数只在迁移时快照。
# 由 server 注入 NODE_REGISTRY，避免循环依赖。
from touken.flows.report_judge import (  # noqa: E402
    _equip_warning_status,
    _practice_report_status,
)
from . import workflow as _workflow  # noqa: E402
from . import home_layout as _home_layout  # noqa: E402


def _wf_node(script, builder, category, snapshot_saved=False, detail=None):
    info = _SCRIPTS[script]

    def _run(agent, params, config_path):
        from .task_parameters import field_defaults
        merged = {**field_defaults(info["params"]), **(params or {})}
        yield from builder(agent, config_path, merged)

    node = {"type": script, "label": info["label"], "desc": info["desc"],
            "category": category, "params": info["params"], "run": _run, "snapshot_saved": snapshot_saved}
    if detail:
        node["detail"] = list(detail)
    _workflow.register_node(node)


for _wf_script, _wf_builder in (
        ("practice", _build_practice), ("raid", _build_raid),
        ("edocastle", _build_edocastle), ("sortie", _build_sortie),
        ("yosari", _build_yosari), ("osaka", _build_osaka),
        ("pumpkin", _build_pumpkin), ("sakura", _build_sakura),
        ("hanafuda", _build_hanafuda)):
    _wf_node(_wf_script, _wf_builder, "battle", snapshot_saved=True,
             detail=[_equip_warning_status])
# 演练专项判分（打了却一场没赢不算绿）照旧补上
_workflow.NODE_REGISTRY["practice"]["detail"].append(_practice_report_status)
_wf_node("forge", _build_forge, "chore")
_wf_node("repair", _build_repair, "chore")
def _build_workflow_expedition(agent, config_path, params):
    from .scheduler import find_map, managed_teams
    from .task_parameters import expedition_routes
    routes = expedition_routes(params, find_map, managed_teams())
    yield from _build_expedition_manager(agent, config_path, {"_routes": routes})


_wf_node("expedition", _build_workflow_expedition, "chore")
from .task_parameters import expedition_fields
_workflow.NODE_REGISTRY["expedition"]["params"] = expedition_fields(_map_select_field())


install_daily_template(
    _workflow, _SCRIPTS, _load_settings=lambda: _load_panel_settings(),
    config=_CFG_DATA, daily_steps=_DAILY_STEPS, plan_inputs=_daily_plan_inputs)


def _build_workflow(config_path, params):
    """自定义工作流入口：按 id 读预设，再交给 workflow 引擎编排。"""
    from touken.flow_control import FlowAborted
    preset_id = str(params.get("workflow_id") or "")
    scheduled_runs = params.get("scheduled_raid_runs")
    if (params.get("workflow_resume", {}).get("reason") == "expedition"
            and params.get("scheduled_deadline") is not None
            and time.time() >= params["scheduled_deadline"]):
        raise FlowAborted("本日安排或活动已结束，联队战剩余圈数不再补跑")
    if scheduled_runs is not None:
        from .day_conductor import workflow_spec
        if type(scheduled_runs) is not int or not 1 <= scheduled_runs <= 99:
            yield "[工作流] ✗ 今日安排的联队战圈数无效，停止"
            raise FlowAborted("今日安排的联队战圈数无效")
        try:
            spec = workflow_spec(preset_id, (_load_panel_settings().get("params", {})
                                             .get("raid", {}) or {}))
            if params.get("scheduled_workflow_signature") not in spec["compatible_signatures"]:
                raise ValueError("任务流或联队战设置已变化")
            preset = {"nodes": spec["nodes"], "after": spec["after"],
                      "daily_mode": spec["daily_mode"]}
        except ValueError as exc:
            yield f"[工作流] ✗ {exc}，本段不启动"
            raise FlowAborted(str(exc)) from exc
    else:
        preset = _workflow.find_preset(preset_id)
    if preset is None:
        if params.get("workflow_resume"):
            raise FlowAborted("等待期间任务流已删除，续跑已停止，请重新安排")
        yield f"[工作流] 找不到预设 {preset_id!r}，可能已被删除"
        return
    from .day_conductor import _workflow_block_signature
    preset_signature = _workflow_block_signature(preset)
    if params.get("workflow_resume") and params.get("workflow_preset_signature") != preset_signature:
        raise FlowAborted("等待期间任务流已修改，续跑已停止，请重新安排")
    params["workflow_preset_signature"] = preset_signature
    try:
        plan = _workflow.normalize_nodes(preset.get("nodes"))
    except _workflow.WorkflowError as exc:
        yield f"[工作流] 预设校验翻车: {exc}"
        return
    if preset.get('id') == _workflow.DAILY_PRESET_ID and (
            not params.get('workflow_resume') or params.get('daily_bootstrap_version') == 1):
        params['daily_bootstrap_version'] = 1
        plan = [node for node in plan if node['type'] not in {'boot_emulator', 'login'}]
        plan[:0] = [{'type': 'boot_emulator', 'params': {}, 'on_error': 'stop'},
                    {'type': 'login', 'params': {}, 'on_error': 'stop'}]
    if scheduled_runs is not None:
        raid_index = spec["raid_index"]
        plan[raid_index]["params"] = {**plan[raid_index]["params"],
                                      "runs": scheduled_runs, "rounds": scheduled_runs}
        # 推荐圈数只覆盖本次联队战；原有前后步骤、参数及下班安排照常执行。
        if plan[0]["type"] != "boot_emulator":
            plan.insert(0, {"type": "boot_emulator", "params": {}, "on_error": "stop"})
        raid_index = next(i for i, node in enumerate(plan) if node["type"] == "raid")
        if not any(node["type"] == "login" for node in plan[:raid_index]):
            plan.insert(1, {"type": "login", "params": {}, "on_error": "stop"})
        else:
            for node in plan[:raid_index]:
                if node["type"] == "login":
                    node["on_error"] = "stop"
    completed = yield from _workflow.run_workflow(
        config_path, plan, make_agent=_make_agent,
        after=preset.get("after", "none"),
        daily_mode=preset.get("daily_mode", False),
        resume=params.get("workflow_resume"), defer_wait=os.environ.get("MAAMARU_WORKER") == "1",
        defer_expedition=scheduled_runs is not None)
    if scheduled_runs is not None and completed is False:
        raise FlowAborted("今日安排的联队战步骤未完成")


def _build_scheduled_gameplay(config_path, params):
    from touken.flow_control import FlowAborted
    from .scheduled_gameplay import catalog, issues, spec
    try:
        if params.get("scheduled_deadline") is not None and time.time() >= params["scheduled_deadline"]:
            raise ValueError("本日安排或活动已结束，剩余次数不再补跑")
        runs = params.get("runs")
        if type(runs) is not int or not 1 <= runs <= 99:
            raise ValueError("今日安排的次数无效")
        current = spec(params.get("script"))
        if current["signature"] != params.get("gameplay_signature"):
            raise ValueError("玩法设置或预设编队已变化")
        now = time.time()
        problems = issues({**params, "start_min": 0}, {
            "day_start": now, "now": now, "gameplay_options": catalog(now)})
        if problems:
            raise ValueError("；".join(problems))
    except ValueError as exc:
        yield f"[时间表] ✗ {exc}，本段不启动"
        raise FlowAborted(str(exc)) from exc
    nodes = [{"type": "boot_emulator", "params": {}, "on_error": "stop"},
             {"type": "login", "params": {}, "on_error": "stop"},
             {"type": params["script"], "params": {**current["params"], "runs": runs},
              "on_error": "stop"}]
    completed = yield from _workflow.run_workflow(
        config_path, nodes, make_agent=_make_agent, after="none", daily_mode=False,
        resume=params.get("workflow_resume"), defer_expedition=params["script"] == "raid")
    if completed is False:
        raise FlowAborted("今日安排的玩法未完成")


register_script("scheduled_gameplay", "时间表玩法", "按已保存的玩法设置开工",
                _build_scheduled_gameplay, hidden=True)


register_script("workflow", "自定义工作流",
                "把任务积木自由排序拼成流水线，一键运行",
                _build_workflow, hidden=True)


# ── 自定义流程（流程工坊）──
def _build_custom_flow(config_path, params):
    """流程工坊入口：按 flow_id 读流程，交给流程引擎跑（步骤安全红线在引擎里）。"""
    from touken import flow_engine
    flow_id = str(params.get("flow_id") or "")
    flow = flow_engine.find_flow(flow_id)
    if flow is None:
        yield f"[流程工坊] 找不到流程 {flow_id!r}，可能已被删除"
        return
    try:
        plan = flow_engine.normalize_flow(flow)
    except flow_engine.FlowError as exc:
        yield f"[流程工坊] 流程校验翻车: {exc}"
        return
    agent = _make_agent(config_path)
    yield from flow_engine.run_flow(agent, plan)


register_script("custom_flow", "自定义流程",
                "跑流程工坊里拼好的自定义流程（开发版调试工具）",
                _build_custom_flow,
                params=[{"key": "flow_id", "type": "text", "label": "流程 id",
                         "help": "流程工坊里保存的流程 id（/api/flow-lab/flows 里看）。"}],
                hidden=True)


# ── 服务端启动 ──

@app.on_event("startup")
async def startup():
    try:
        await _startup()
    except BaseException:
        # Uvicorn turns lifespan exceptions into SystemExit(3).  Persist the
        # original traceback first so the launcher can show a useful cause.
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            (LOG_DIR / "launcher.log").write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        except OSError:
            pass
        raise


def _migrate_task_parameters():
    """备份后一次性固化旧任务参数；已迁移值不再跟随单跑设置。"""
    import copy
    from .scheduler import load_config
    from .task_parameters import expedition_snapshot, isolate_presets
    settings = _load_panel_settings()
    if settings.get("task_parameter_version") != 1:
        path = _panel_settings_path()
        if path.exists() and not path.with_suffix(path.suffix + ".parameters-v0.bak").exists():
            path.with_suffix(path.suffix + ".parameters-v0.bak").write_bytes(path.read_bytes())
        _migrate_daily_battle_settings()
        settings = _load_panel_settings()
    saved = settings.get("params", {})
    fallback = _CFG_DATA.get("daily", {}).get("practice", {})
    expedition = expedition_snapshot(load_config(), saved.get("sakura", {}) or {})
    if settings.get("task_parameter_version") != 1:
        daily = settings.setdefault("params", {}).setdefault("daily", {})
        daily.setdefault("practice", copy.deepcopy(saved.get("practice") or fallback))
        daily.setdefault("expedition", copy.deepcopy(expedition))
        settings["task_parameter_version"] = 1
        _save_panel_settings(settings)
    presets = _workflow.load_presets()
    # 默认日课也必须持久化，不能每次从其他设置重新生成。
    if not any(p.get("id") == _workflow.DAILY_PRESET_ID for p in presets):
        presets.insert(0, _workflow.daily_template_provider())
    recipe = _CFG_DATA.get("forge", {}).get("recipe") or []
    if len(recipe) == 4:
        from .daily_workflow import RECIPE_KEYS
        for preset in presets:
            if preset.get("parameter_version") == 1:
                continue
            for node in preset.get("nodes", []):
                if node["type"] == "forge":
                    for key, value in zip(RECIPE_KEYS, recipe):
                        node.setdefault("params", {}).setdefault(key, value)
    isolated = isolate_presets(presets, _workflow.NODE_REGISTRY, saved, fallback, expedition)
    if isolated != _workflow.load_presets():
        path = _workflow._presets_path()
        if path.exists() and not path.with_suffix(".parameters-v0.bak").exists():
            path.with_suffix(".parameters-v0.bak").write_bytes(path.read_bytes())
        _workflow.save_presets(isolated)


async def _startup():
    if _ledger_mode():
        # 账房模式必须能在模拟器、ADB、MAA 全都没开的情况下独立使用。
        # 广播、远征调度和机器人都可能间接触发自动化或额外网络连接，
        # 因此这里不初始化；账本、规划与手动录入 API 仍照常可用。
        return

    _migrate_task_parameters()
    _start_broadcast()
    runner = get_runner()
    runner.set_message_callback(_on_script_message)

    # 远征时刻表调度线程：到点自动派遣（面板关着就不会派）
    from .scheduler import start_scheduler
    from .log_store import get_store as _get_store

    def _sched_emit(script, message):
        _get_store().append("scheduler", script, message)
        _on_script_message({"id": None, "ts": time.time(),
                            "run_id": "scheduler", "script": script,
                            "message": message})

    start_scheduler(str(_CONFIG_PATH), _sched_emit)
    from .day_conductor import start_conductor
    start_conductor(str(_CONFIG_PATH), runner, _day_timeline_payload,
                    lambda: (_load_panel_settings().get("params", {})
                             .get("raid", {}) or {}), _sched_emit)

    # Bot 启动（配了 panel_config.json 才启；QQ/TG 各自独立开关）
    from .bot_qq import init_qq
    _qq_sender = init_qq(app, _get_gateway)

    from .bot_telegram import start_bot as _start_bot
    # Disabled bots must not initialize the optional AI/http client.  Besides
    # doing unnecessary work, a damaged AI config used to prevent the entire
    # local panel from starting.
    try:
        bot_cfg = json.loads(_PANEL_CONFIG.read_text(encoding="utf-8")).get("bot", {})
    except (OSError, json.JSONDecodeError):
        bot_cfg = {}
    needs_gateway = bot_cfg.get("enabled", False) and bot_cfg.get("platform", "").lower() == "telegram"
    _bot_instance = _start_bot(_get_gateway() if needs_gateway else None)

    # 事件播报器：挂上 QQ 出口（没配 QQ 也能跑，只发 ntfy）
    from .broadcaster import init_broadcaster
    init_broadcaster(qq_sender=_qq_sender)

    # 暴露给 API 路由用：机器人控制
    import __main__ as _bm
    _bm._bot_instance = _bot_instance


# ── 静态文件 ──

@app.get("/")
async def index():
    built = _STATIC / "vue" / "index.html"
    if built.exists():
        return FileResponse(str(built))
    # 开发环境尚未构建新版时仍可进入旧面板，不让启动器直接白屏。
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/legacy")
async def legacy_index():
    """迁移后的临时回退入口；确认新版长期稳定后再移除。"""
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/next")
async def next_index():
    """兼容迁移期间使用的预览地址。"""
    built = _STATIC / "vue" / "index.html"
    if not built.exists():
        return JSONResponse(
            {"ok": False, "error": "新版面板尚未构建，请先运行前端构建。"},
            status_code=503,
        )
    return FileResponse(str(built))


@app.get("/favicon.ico")
async def favicon():
    return JSONResponse({"ok": False}, status_code=404)


# ── API：日志 ──

@app.get("/api/logs")
async def get_logs(limit: int = 100, after_id: int = 0, run_id: str = ""):
    store = get_store()
    logs = store.get_recent(limit=limit, after_id=after_id,
                            run_id=run_id or None)
    last_id = store.get_last_id()
    return {"logs": logs, "last_id": last_id}


# ── API：异常与通知中心 ──

@app.get("/api/incidents")
async def get_incidents():
    from touken.incidents import list_incidents
    items = list_incidents()
    unread = sum(1 for item in items if item.get("status") == "active")
    return {"items": items, "unread": unread}


@app.post("/api/incidents/{code}/ack")
async def ack_incident(code: str):
    from touken.incidents import set_status
    item = set_status(code, "acknowledged")
    if item is None:
        return JSONResponse({"ok": False, "reason": "没有这张事故单"}, status_code=404)
    return {"ok": True, "item": item}


@app.post("/api/incidents/{code}/resolve")
async def resolve_incident(code: str):
    from touken.incidents import set_status
    item = set_status(code, "resolved")
    if item is None:
        return JSONResponse({"ok": False, "reason": "没有这张事故单"}, status_code=404)
    return {"ok": True, "item": item}


@app.get("/api/diagnostics/export")
def export_diagnostics():
    """Download a sanitized text-only bundle suitable for a public issue."""
    bundle = build_diagnostic_bundle()
    return Response(
        content=bundle.content,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{bundle.filename}"'},
    )


@app.post("/api/diagnostics/export-local")
def export_diagnostics_local():
    """面板跑在 pywebview 里，浏览器下载会被 WebView 静默吞掉：
    反馈包直接落盘，再在资源管理器里替用户选好。"""
    try:
        path = create_diagnostic_bundle()
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "message": f"暂时没能生成错误反馈包：{exc}"},
            status_code=500)
    revealed = reveal_file_in_explorer(path)
    return {"ok": True, "filename": path.name, "revealed": revealed}


@app.get("/api/logs/stream")
async def stream_logs(request: Request):
    """SSE 日志流"""
    async def event_generator():
        store = get_store()
        last_id = store.get_last_id()
        while True:
            if await request.is_disconnected():
                break
            try:
                # 从广播队列取（实时消息）
                msg = await asyncio.wait_for(_broadcast_queue.get(), timeout=2.0)
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                # 新消息检查：取 after_id 之后落盘的日志
                logs = store.get_recent(limit=50, after_id=last_id)
                for log in logs:
                    if log["id"] > last_id:
                        yield f"data: {json.dumps(log, ensure_ascii=False)}\n\n"
                        last_id = log["id"]
                yield ": heartbeat\n\n"
    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── API：脚本 ──


@app.get("/api/app-mode")
async def api_app_mode():
    return {
        "mode": "ledger" if _ledger_mode() else "automation",
        "automation_enabled": not _ledger_mode(),
    }

# 概览页常用功能的活动联动隐藏名单：/api/scripts 每 2 秒被轮询一次，
# 名单本身按分钟级缓存，别每趟都去读卡片和日历文件。
_event_hidden_cache = {"ts": 0.0, "value": []}


def _event_hidden_scripts() -> list[str]:
    if time.time() - _event_hidden_cache["ts"] < 60:
        return _event_hidden_cache["value"]
    from touken import advisor, event_timeline
    calendar, _ = _load_events_calendar()
    value = event_timeline.hidden_event_scripts(
        advisor.load_event_cards(STATUS_DIR),
        calendar.get("announcements", []))
    _event_hidden_cache.update(ts=time.time(), value=value)
    return value


def _scripts_with_preset_options(scripts: dict) -> dict:
    """把当前预设编队追加到各玩法部队选项尾部（只拼响应，不改 _SCRIPTS 本体，
    否则每请求重复追加）。"""
    from touken.custom_formations import load_formations
    formations = load_formations()
    if not formations:
        return scripts
    extras = [[f"preset:{f['id']}",
               f"{f['name']}（覆盖部队{_TEAM_CN[f['target_team']]}）"]
              for f in formations]
    out = {}
    for name, info in scripts.items():
        if name == "sakura":
            out[name] = info
            continue
        params, touched = [], False
        for field in info.get("params") or []:
            if isinstance(field, dict) and field.get("key") == "team_no":
                field = {**field,
                         "options": [list(o) for o in field.get("options") or []]
                                    + [e[:] for e in extras]}
                touched = True
            params.append(field)
        out[name] = {**info, "params": params} if touched else info
    return out


@app.get("/api/scripts")
async def api_scripts():
    if _ledger_mode():
        return {
            "scripts": {},
            "running": False,
            "current": None,
            "run_id": None,
            "workflow": None,
            "event_hidden": [],
        }
    runner = get_runner()
    return {
        "scripts": _scripts_with_preset_options(list_scripts()),
        "running": runner.is_running,
        "current": runner.current_script,
        "run_id": runner.current_run_id if runner.is_running else None,
        "workflow": runner.current_workflow,
        # 概览页「常用功能」联动：绑活动的脚本没开放就先收起来（配置页不受影响）
        "event_hidden": _event_hidden_scripts(),
    }


@app.post("/api/scripts/run")
async def api_run_script(request: Request):
    if _ledger_mode():
        return JSONResponse(
            {"ok": False, "reason": "纯净账房模式不连接游戏"},
            status_code=403,
        )
    body = await request.json()
    script_name = body.get("script", "")
    params = body.get("params", {})
    runner = get_runner()
    run_id = runner.start(script_name, str(_CONFIG_PATH), params=params)
    if run_id is None:
        return JSONResponse({"ok": False, "reason": "不支持或正在运行"}, status_code=400)
    return {"ok": True, "run_id": run_id, "workflow": runner.current_workflow}


@app.post("/api/scripts/stop")
async def api_stop_script():
    runner = get_runner()
    from .workflow_waits import load
    if not runner.is_running and not any(r.get("status") == "waiting" for r in load().values()):
        return {"ok": False, "reason": "没有在运行的脚本"}
    runner.stop()
    return {"ok": True}


@app.post("/api/workflows/waits/{root}/cancel")
async def api_cancel_workflow_wait(root: str):
    from .workflow_waits import load, cancel
    record = load().get(root, {})
    runner = get_runner()
    cancelled = cancel(root)
    if cancelled and runner.is_running and runner.current_run_id == record.get("run_id"):
        runner.stop(cancel_waits=False)
    return {"ok": cancelled}


# ── API：预设编队（玩法出阵前一键覆盖某部队）──

def _formation_from_body(fid: str, body: dict) -> dict:
    """只允许改 name/target_team/slots；id 走路径，created_at 不可动。
    前端表单值常是字符串，target_team 先尽力转成 int 再交校验兜底。"""
    team = body.get("target_team")
    try:
        team = int(team)
    except (TypeError, ValueError):
        pass  # 交给 validate_formation 如实报错
    return {"id": fid, "name": body.get("name"),
            "target_team": team, "slots": body.get("slots") or {}}


@app.get("/api/custom-formations")
async def api_list_custom_formations(server: str = ""):
    _telemetry_store_for(server)
    from touken.custom_formations import load_formations
    return {"formations": load_formations(server)}


@app.post("/api/custom-formations")
async def api_create_custom_formation(request: Request, server: str = ""):
    _telemetry_store_for(server)
    from touken import custom_formations as cf
    body = await request.json()
    formations = cf.load_formations(server)
    try:
        fid = cf.new_formation_id(formations)
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    record = _formation_from_body(fid, body)
    err = cf.validate_formation(record, formations)
    if err:
        return JSONResponse({"ok": False, "reason": err}, status_code=400)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    record["created_at"] = now
    record["updated_at"] = now
    formations.append(record)
    cf.save_formations(formations, server)
    return {"ok": True, "formation": record}


@app.put("/api/custom-formations/{fid}")
async def api_update_custom_formation(fid: str, request: Request, server: str = ""):
    _telemetry_store_for(server)
    from touken import custom_formations as cf
    body = await request.json()
    formations = cf.load_formations(server)
    old = cf.find_formation(formations, fid)
    if old is None:
        return JSONResponse({"ok": False, "reason": "找不到这套预设编队"},
                            status_code=404)
    record = _formation_from_body(fid, body)
    # 查重时剔除自己，不然「原样保存」也会被误判 id 占用
    err = cf.validate_formation(
        record, [f for f in formations if f.get("id") != fid])
    if err:
        return JSONResponse({"ok": False, "reason": err}, status_code=400)
    record["created_at"] = old.get("created_at", "")
    record["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    formations[formations.index(old)] = record
    cf.save_formations(formations, server)
    return {"ok": True, "formation": record}


@app.delete("/api/custom-formations/{fid}")
async def api_delete_custom_formation(fid: str, server: str = ""):
    _telemetry_store_for(server)
    from touken import custom_formations as cf
    formations = cf.load_formations(server)
    if cf.find_formation(formations, fid) is None:
        return JSONResponse({"ok": False, "reason": "找不到这套预设编队"},
                            status_code=404)
    cf.save_formations([f for f in formations if f.get("id") != fid], server)
    return {"ok": True}


# ── API：自定义工作流（乐高排班）──

@app.get("/api/workflows")
async def api_list_workflows():
    from .workflow_waits import projection
    return {"presets": [{**preset, "steps": projection(preset, None)} for preset in _workflow.list_presets()]}


@app.get("/api/workflows/nodes")
async def api_workflow_nodes():
    """节点目录：type/label/desc/category/params schema，前端渲染积木选择器用"""
    nodes = _workflow.node_catalog()
    from touken.custom_formations import load_formations
    formations = load_formations()
    formation_options = [[f["id"],
                          f'{f["name"]}（覆盖部队{_TEAM_CN[f["target_team"]]}）']
                         for f in formations]
    for node in nodes:
        if node["type"] == "expedition":
            node["params"] = [
                {**field, "options": [["", "保持当前部队"]] + [
                    [f["id"], f["name"]] for f in formations
                    if int(f["target_team"]) == int(field["key"].split("_")[-1])
                ]} if field["key"].startswith("preset_") else field
                for field in node["params"]]
        if node["type"] == "apply_formation_preset":
            node["params"] = [
                {**field, "options": [option[:] for option in formation_options],
                 "default": (formation_options[0][0] if formation_options else "")}
                if field.get("key") == "preset_id" else field
                for field in node.get("params") or []]
    return {"nodes": nodes}


@app.post("/api/workflows")
async def api_create_workflow(request: Request):
    body = await request.json()
    try:
        preset = _workflow.create_preset(body)
    except _workflow.WorkflowError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "id": preset["id"], "preset": preset}


@app.put("/api/workflows/{preset_id}")
async def api_update_workflow(preset_id: str, request: Request):
    body = await request.json()
    try:
        preset = _workflow.update_preset(preset_id, body)
    except _workflow.WorkflowError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    if preset is None:
        return JSONResponse({"ok": False, "reason": "没有这个预设"},
                            status_code=404)
    return {"ok": True, "preset": preset}


@app.delete("/api/workflows/{preset_id}")
async def api_delete_workflow(preset_id: str):
    try:
        deleted = _workflow.delete_preset(preset_id)
    except _workflow.WorkflowError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    if not deleted:
        return JSONResponse({"ok": False, "reason": "没有这个预设"},
                            status_code=404)
    return {"ok": True}


# ── API：执务页常用功能布局（自定义/排序/隐藏）──

@app.get("/api/home-layout")
async def api_get_home_layout():
    layout = _home_layout.load_layout()
    return {"order": layout["order"], "hidden": layout["hidden"],
            "entries": _home_layout.resolve_layout()}


@app.put("/api/home-layout")
async def api_put_home_layout(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "reason": "请求体不是合法 JSON"},
                            status_code=400)
    try:
        layout = _home_layout.normalize_layout(body)
    except _home_layout.HomeLayoutError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    _home_layout.save_layout(layout)
    return {"ok": True, "entries": _home_layout.resolve_layout()}


# ── API：刀剑名册（供前端名单选择器）──

@app.get("/api/swords")
async def api_swords():
    """返回全部刀剑的名称与刀种，用于候选列表分类"""
    from touken import sword_db
    swords = sword_db.all_swords()
    return {
        "swords": [
            {
                "id": sid,
                "name": info["name"],
                "name_zh": info.get("name_zh", ""),
                "type": info.get("type", "其他"),
            }
            for sid, info in swords.items()
        ]
    }


# ── API：全局名单配置（手入黑名单 / 刀解白名单 / 心愿刀）──

@app.get("/api/config-lists")
async def api_get_config_lists():
    """读取当前游戏配置里的名单"""
    cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "repair_blacklist": cfg.get("repair", {}).get("blacklist", []),
        "dismantle_whitelist": cfg.get("dismantle", {}).get("whitelist", _DISMANTLE_WHITELIST),
        "sword_wishlist": cfg.get("sword_wishlist", []),
    }


@app.post("/api/config-lists")
async def api_save_config_lists(request: Request):
    """把名单写回 touken_config.json（只改名单，不动别的）"""
    body = await request.json()
    cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    if "repair_blacklist" in body:
        cfg.setdefault("repair", {})["blacklist"] = [
            str(x).strip() for x in body["repair_blacklist"] if str(x).strip()
        ]
    if "dismantle_whitelist" in body:
        cfg.setdefault("dismantle", {})["whitelist"] = [
            str(x).strip() for x in body["dismantle_whitelist"] if str(x).strip()
        ]
    if "sword_wishlist" in body:
        cfg["sword_wishlist"] = [
            str(x).strip() for x in body["sword_wishlist"] if str(x).strip()
        ]
    _CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True}


_ADB_ADDRESS_RE = re.compile(r"^(?:[\w.-]+:\d{1,5}|emulator-\d{1,5})$")


def _valid_adb_address(address: str) -> bool:
    if not _ADB_ADDRESS_RE.match(address):
        return False
    if address.startswith("emulator-"):
        return True
    port = int(address.rsplit(":", 1)[1])
    return 1 <= port <= 65535


@app.get("/api/emulator-config")
async def api_get_emulator_config():
    """当前 ADB 连接配置；adb_address 为空表示下次启动时自动探测。"""
    cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "adb_address": str(cfg.get("adb_address", "")).strip(),
        "default_address": _DEFAULT_ADB_ADDR,
        "adb_path": str(cfg.get("adb_path", "")),
    }


@app.post("/api/emulator-config")
async def api_save_emulator_config(request: Request):
    """手动指定 ADB 地址；留空则删掉配置，回到自动探测。"""
    body = await request.json()
    address = str(body.get("adb_address", "")).strip()
    if address and not _valid_adb_address(address):
        return JSONResponse(
            {"error": "地址格式不对，应该类似 127.0.0.1:16384 或 emulator-5554"},
            status_code=400,
        )
    cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    if address:
        cfg["adb_address"] = address
    else:
        cfg.pop("adb_address", None)
    _CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "adb_address": address}


# ── API：聊天（已并轨 Agent 网关，面板聊天也能调脚本）──

_agent_gateway = None


def _get_gateway():
    """Agent 网关单例。chat-config 保存后置 None 重建，新配置即生效"""
    global _agent_gateway
    if _agent_gateway is None:
        from .agent import AgentGateway
        _agent_gateway = AgentGateway(str(_PANEL_CONFIG))
    return _agent_gateway


@app.get("/api/chat/history")
async def api_chat_history():
    store = get_store()
    return {"history": store.get_chat_history()}


# sync 的 LLM 调用走线程池，别卡事件循环（SSE 心跳全靠它）
from starlette.concurrency import run_in_threadpool as _run_io


@app.post("/api/chat")
async def api_chat(request: Request):
    """面板「近侍」tab：已并轨 Agent 网关——聊天归聊天，说干活就真去干活"""
    body = await request.json()
    message = body.get("message", "").strip()
    if not message:
        return JSONResponse({"reply": "（狐之助歪了歪头：主君，你说什么？）"})

    store = get_store()
    store.add_chat("user", message)
    try:
        reply = await _run_io(_get_gateway().process, message, "web")
    except Exception as exc:
        reply = f"（狐之助耳朵耷拉下来：主君…我脑子冒烟了 — {exc}）"
    store.add_chat("assistant", reply)   # 历史展示照旧走 log_store
    return {"reply": reply}


# ── API：Agent 网关（跨渠道 LLM 入口）──

@app.post("/api/agent")
async def api_agent(request: Request):
    """
    Agent 网关入口：接收任意渠道的消息，LLM 理解意图，调用工具。

    Body: {"message": "...", "channel": "qq"}
    Returns: {"reply": "...", "tool_called": true/false}
    """
    body = await request.json()
    message = body.get("message", "").strip()
    channel = body.get("channel", "qq")
    if not message:
        return {"reply": "（狐之助歪了歪头：你说什么？）", "tool_called": False}

    try:
        reply = await _run_io(_get_gateway().process, message, channel)
        return {"reply": reply, "tool_called": True}
    except Exception as exc:
        return {"reply": f"（狐之助耳朵耷拉下来：脑子冒烟了 — {exc}）", "tool_called": False}


# ── API：远征时刻表 ──

@app.get("/api/expedition-schedule")
async def api_get_schedule():
    from .scheduler import load_config, map_options, preset_payload, today_projection
    from .expedition_choices import load_choice_sets
    cfg = load_config()
    choices, forced = load_choice_sets()
    return {**cfg, "maps": map_options(), "presets": preset_payload(),
            "today": today_projection(cfg, choices=choices, forced=forced)}


@app.get("/api/day-timeline")
async def api_day_timeline():
    """今日时间表与玩家选定的联队战安排。"""
    await asyncio.to_thread(_refresh_expedition_observations)
    return _day_timeline_payload()


_expedition_observation_lock = threading.Lock()
_expedition_observation_next = 0.0


def _refresh_expedition_observations():
    """Idle-only, at most once a minute; failure leaves the time estimate intact."""
    global _expedition_observation_next
    from .expedition_observation import FILENAME, load_observations, visible_records
    records = visible_records(_read_expedition_records(),
                              load_observations(STATUS_DIR / FILENAME))
    if (get_runner().is_running or not records
            or all(_expedition_remaining(record) > 0 for record in records.values())):
        return
    if not _expedition_observation_lock.acquire(blocking=False):
        return
    try:
        now = time.monotonic()
        if now < _expedition_observation_next:
            return
        _expedition_observation_next = now + 60
        from touken import youzu_log
        from .expedition_observation import save_observations
        cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        path = youzu_log.pull_log(cfg.get("adb_path") or _DEFAULT_ADB_PATH,
                                  cfg.get("adb_address") or _DEFAULT_ADB_ADDR,
                                  dest_dir=DEBUG_DIR)
        try:
            save_observations(youzu_log.parse_events(path), STATUS_DIR / FILENAME)
        finally:
            path.unlink(missing_ok=True)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
        pass
    finally:
        _expedition_observation_lock.release()


def _day_timeline_payload():
    from .day_timeline import build_day_timeline
    from .day_plan import load_plan, review_plan
    from .day_conductor import projection
    runner = get_runner()
    active = None
    if runner.is_running and runner.current_script:
        active = {"script": runner.current_script,
                  "started": runner.current_started,
                  "label": (runner.current_workflow or {}).get("name")}
    hanafuda_team_no = None
    raid_team_no = None
    try:
        config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        hanafuda_team_no = config.get("hanafuda", {}).get("team_no")
        raid_team_no = config.get("raid", {}).get("team_no")
    except Exception:
        pass
    from .task_reservations import task_windows
    from .day_conductor import load_state
    from .day_timeline import _day_window
    from touken.telemetry import get_telemetry_store
    planning_now = time.time()
    def workflow_name(workflow_id):
        return (_workflow.find_preset(workflow_id) or {}).get("name")
    reservations = task_windows(planning_now, _day_window(planning_now)[0],
                                load_plan(), load_state(), active,
                                get_telemetry_store(), workflow_name)
    timeline = build_day_timeline(
        now=planning_now, player_tasks=reservations,
        script_labels={k: v["label"] for k, v in _SCRIPTS.items()},
        active=active,
        hanafuda_team_no=hanafuda_team_no,
        raid_team_no=raid_team_no)
    from .scheduled_gameplay import catalog
    timeline["gameplay_options"] = catalog(timeline["now"])
    plan = load_plan()
    if plan and plan.get("day_start") == timeline["day_start"]:
        timeline["booking"] = {**plan, "issues": review_plan(plan, timeline)}
    else:
        timeline["booking"] = None
    timeline["conductor"] = projection(
        plan, timeline, (_load_panel_settings().get("params", {})
                         .get("raid", {}) or {}))
    timeline["conductor"]["available"] = not _ledger_mode()
    from .workflow_waits import load as load_waits, projection as workflow_projection, public_records
    waits = load_waits()
    timeline["workflow_waits"] = public_records()
    snapshot = getattr(runner, "workflow_snapshot", None)
    timeline["workflow_active"] = None
    if runner.is_running and runner.current_workflow and isinstance(snapshot, dict) and snapshot.get("nodes"):
        timeline["workflow_active"] = {"name": runner.current_workflow["name"],
            "steps": workflow_projection(snapshot, runner.current_started)}
    for block in (timeline.get("booking") or {}).get("blocks", []):
        if block.get("kind") == "workflow":
            preset = _workflow.find_preset(block["workflow_id"])
            block["steps"] = workflow_projection(preset, timeline["day_start"] + block["start_min"] * 60) if preset else []
    for block in timeline["conductor"]["blocks"]:
        record = waits.get(block.get("run_id"), {})
        if record.get("status") == "waiting":
            block["waiting_until"] = record["wake_at"]
    return timeline


@app.post("/api/today/execute")
def api_execute_today():
    if _ledger_mode():
        raise HTTPException(403, "纯净账房模式不能自动开工")
    from .today_execution import execute_today
    try:
        daily_preset = _workflow.find_preset(_workflow.DAILY_PRESET_ID)
        if not daily_preset:
            raise ValueError("日课设置没有加载出来，请重试")
        return execute_today(
            get_runner(), _day_timeline_payload,
            lambda: (_load_panel_settings().get("params", {}).get("raid", {}) or {}),
            daily_preset=daily_preset)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/day-conductor/resume-raid")
async def api_resume_day_raid(request: Request):
    from .day_conductor import resume_raid
    if _ledger_mode():
        raise HTTPException(403, "纯净账房模式不能自动开工")
    body = await request.json()
    if (not isinstance(body, dict) or not isinstance(body.get("run_id"), str)
            or type(body.get("finished_round")) is not bool):
        raise HTTPException(400, "请确认中断那圈是否已经打完")
    try:
        resume_raid(body["run_id"], body["finished_round"], get_runner(),
                    _day_timeline_payload,
                    lambda: (_load_panel_settings().get("params", {}).get("raid", {}) or {}),
                    str(_CONFIG_PATH), lambda script, msg: get_store().append("conductor", script, msg))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True}


@app.put("/api/day-conductor")
async def api_day_conductor(request: Request):
    from .day_conductor import arm, disarm, projection
    from .day_plan import load_plan

    body = await request.json()
    enabled = body.get("enabled") if isinstance(body, dict) else None
    if type(enabled) is not bool:
        raise HTTPException(400, "请选择是否让大总管自动开工")
    if enabled and _ledger_mode():
        raise HTTPException(403, "纯净账房模式不能自动开工")
    if not enabled:
        disarm()
    else:
        plan = load_plan()
        if not plan:
            raise HTTPException(409, "先记下今天的联队战时间和圈数")
        workflow_id = body.get("workflow_id")
        if not isinstance(workflow_id, str):
            raise HTTPException(400, "请选择联队战任务流")
        timeline = _day_timeline_payload()
        try:
            arm(plan, timeline, workflow_id,
                (_load_panel_settings().get("params", {}).get("raid", {}) or {}))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    timeline = _day_timeline_payload()
    return {"conductor": timeline["conductor"]}


@app.put("/api/day-timeline/expedition-slot")
async def api_set_day_expedition_slot(request: Request):
    """点按今天的一班远征：自描述班（建议采纳的）直接改 forced 记录；
    排班投影里的班走旧契约（会跑 ↔ 跳过/强制）。
    """
    from .expedition_choices import (ADHOC_KEY_MARK, set_forced_adhoc,
                                     set_slot_intention)

    body = await request.json()
    key = body.get("key") if isinstance(body, dict) else None
    will_run = body.get("will_run") if isinstance(body, dict) else None
    if not isinstance(key, str) or type(will_run) is not bool:
        raise HTTPException(400, "请选择今天的一班远征")
    timeline = _day_timeline_payload()
    slot = next((item for item in timeline["expeditions"] if item["key"] == key), None)
    if not slot or not slot["toggleable"]:
        raise HTTPException(409, "这班已临近开班、已处理，或排班里被关掉了；请刷新时间表")
    if ADHOC_KEY_MARK in key:
        # 自描述班：点掉 = 删 forced 记录；点回 = 按块上的自描述信息重写
        set_forced_adhoc(
            key=key, team_no=int(slot["team_no"]), map_code=slot["map_code"],
            start_min=int(slot["time_min"]),
            duration_min=int(slot.get("duration_min") or 0),
            sakura_before_dispatch=slot.get('sakura_before_dispatch', False),
            repair_threshold=slot.get('repair_threshold', 'light'),
            planned_at=time.strftime("%Y-%m-%dT%H:%M:%S",
                                     time.localtime(float(slot["planned_at"]))),
            forced=will_run)
        return {"ok": True}
    if will_run and not slot["base_enabled"] and not slot.get("entry_enabled", True):
        raise HTTPException(409, "这班在排班设置里被关掉了，先去排班里打开再来点")
    set_slot_intention(key=slot["key"], team_no=slot["team_no"],
                       map_code=slot["map_code"], planned_at=slot["planned_at"],
                       will_run=will_run, base_enabled=bool(slot["base_enabled"]))
    return {"ok": True}


@app.get("/api/day-timeline/expedition-settings/{code}")
async def api_expedition_settings(code: str):
    from touken.runtime_paths import STATE_DIR
    from .expedition_advisor import load_maps, expedition_formation_options, party_levels_from_situation
    meta = load_maps().get(code)
    if not meta:
        raise HTTPException(404, "找不到这张远征图")
    parties = party_levels_from_situation(STATE_DIR / "youzu_home_situation.json")
    return {"rewards": {key: meta[key] for key in ("木炭", "玉钢", "冷却材", "砥石", "小判", "委托符", "加速符") if meta.get(key)},
            "formations": {str(team): [{key: item[key] for key in ("formation_id", "formation_name", "formation_signature")} for item in expedition_formation_options(team, parties)] for team in range(1, 6)}}


@app.put("/api/day-timeline/expedition-adopt")
async def api_adopt_day_expedition_suggestion(request: Request):
    """采纳一条远征建议 = 写一班自描述 forced（排班关着也到点单独派出）。

    v2：建议由引擎按缺口现算，不引用排班条目；forced 记录自带
    队伍/图/时刻/时长。重复采纳 409；建议过期（队伍已有安排等）409。
    """
    from . import expedition_advisor
    from touken.runtime_paths import STATE_DIR
    from .expedition_choices import (adhoc_key, load_choice_sets,
                                     set_forced_adhoc)

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "请选一条远征建议")
    if ('sakura_before_dispatch' in body and type(body['sakura_before_dispatch']) is not bool
            or 'repair_threshold' in body and body['repair_threshold'] not in ('light', 'medium', 'heavy')):
        raise HTTPException(400, '请选择有效的补花和伤势设置')
    try:
        team_no = int(body.get("team_no"))
        start_min = int(body.get("start_min"))
    except (TypeError, ValueError):
        raise HTTPException(400, "请选一条远征建议") from None
    map_code = body.get("map_code")
    if team_no not in expedition_advisor.VALID_TEAMS \
            or not isinstance(map_code, str) or not map_code:
        raise HTTPException(400, "请选一条远征建议")
    timeline = _day_timeline_payload()
    configured = "source_start_min" in body or bool(body.get("source_key"))
    source_key = body.get("source_key") or ""
    if not isinstance(source_key, str):
        raise HTTPException(400, "请选择今天的一班远征")
    if source_key:
        suggestion = next((item for item in timeline["expeditions"] if item["key"] == source_key and item.get("toggleable")), None)
        if not suggestion or ":adhoc:" not in source_key:
            raise HTTPException(409, "这班已临近开班或已处理，请刷新时间表")
        if suggestion["team_no"] != team_no or suggestion["map_code"] != map_code:
            raise HTTPException(400, "请保留这班的部队和远征图")
    else:
        candidates = [item for item in timeline.get("expedition_suggestions") or []
                      if item.get("team_no") == team_no and item.get("map_code") == map_code]
        # Time advances while the player reads the popup or another task runs.
        # Match the actual team/map recommendation, not its moving clock value.
        source_min = body.get("source_start_min", start_min)
        if type(source_min) is not int:
            raise HTTPException(400, '请选择有效的出发时间')
        suggestion = min(candidates, key=lambda item: abs(item['start_min'] - source_min)) if candidates else None
    if not suggestion:
        raise HTTPException(409, "这条建议已经变了，刷新时间表再看看")
    if configured:
        now_min = int((time.time() - timeline["day_start"]) // 60)
        if start_min < now_min or start_min > 1679:
            raise HTTPException(400, "请选择现在至次日03:59之间的出发时间")
        meta = expedition_advisor.load_maps()[map_code]
        duration = int(meta["duration_min"])
        for item in timeline["expeditions"]:
            if item["key"] == source_key or not item.get("will_run"):
                continue
            if item["team_no"] != team_no and item["map_code"] != map_code:
                continue
            begin = item["time_min"]
            if start_min < begin + item["duration_min"] + 10 and start_min + duration > begin:
                raise HTTPException(409, "这支部队或远征图在该时间已有安排，请换个出发时间")
        parties = expedition_advisor.party_levels_from_situation(STATE_DIR / "youzu_home_situation.json")
        preset = None
        if body.get("formation_id"):
            preset = next((item for item in expedition_advisor.expedition_formation_options(team_no, parties)
                           if item["formation_id"] == body["formation_id"]
                           and item["formation_signature"] == body.get("formation_signature")), None)
            if not preset:
                raise HTTPException(409, "预设已变更或刀剑不可用，请重新选择")
        party = preset["party"] if preset else (parties or {}).get(team_no)
        shortfall = expedition_advisor._type_shortfall(meta, party)
        detail = expedition_advisor._level_shortfall(meta, party)
        if shortfall or detail:
            raise HTTPException(400, detail or expedition_advisor._type_block_detail(team_no, party, shortfall))
        suggestion = {**suggestion, "duration_min": duration,
                      "formation_id": preset["formation_id"] if preset else "",
                      "formation_name": preset["formation_name"] if preset else "",
                      "formation_signature": preset["formation_signature"] if preset else ""}
    elif (suggestion.get("formation_id") or "") != (body.get("formation_id") or "") or (suggestion.get("formation_signature") or "") != (body.get("formation_signature") or ""):
        raise HTTPException(409, "预设建议已经变了，刷新时间表再看看")
    if not configured:
        start_min = int(suggestion['start_min'])
    today = time.strftime("%Y-%m-%d", time.localtime(timeline["day_start"] + start_min * 60))
    key = adhoc_key(today, team_no, start_min)
    _, forced = load_choice_sets()
    if key in forced and key != source_key:
        raise HTTPException(409, "这班已经点上了，到点会单独派出")
    set_forced_adhoc(
        key=key, replace_key=source_key, team_no=team_no, map_code=map_code, start_min=start_min,
        duration_min=int(suggestion.get("duration_min") or 0),
        formation_id=suggestion.get("formation_id") or "",
        formation_name=suggestion.get("formation_name") or "",
        formation_signature=suggestion.get("formation_signature") or "",
        sakura_before_dispatch=body.get('sakura_before_dispatch', suggestion.get('sakura_before_dispatch', False)),
        repair_threshold=body.get('repair_threshold', suggestion.get('repair_threshold', 'light')),
        planned_at=time.strftime(
            "%Y-%m-%dT%H:%M:%S",
            time.localtime(timeline["day_start"] + start_min * 60)))
    fresh = _day_timeline_payload()
    return {"ok": True,
            "expeditions": fresh.get("expeditions") or [],
            "expedition_suggestions": fresh.get("expedition_suggestions") or [],
            "expedition_help": fresh.get("expedition_help"),
            "expedition_advice_note": fresh.get("expedition_advice_note")}


@app.put("/api/expedition-help-prefs")
async def api_save_expedition_help_prefs(request: Request):
    """记住长期偏好：每队各派几次 + 哪些队可以丢；换日不重置，想改再改。"""
    from . import expedition_advisor

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "偏好格式不对")
    try:
        prefs = expedition_advisor.save_prefs(
            rounds_per_team=body.get("rounds_per_team",
                                     body.get("teams_out")),  # 旧前端兜底
            available_teams=body.get("available_teams"),
            resource_focus=body.get("resource_focus"),
            team_formations=body.get("team_formations"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "expedition_help": prefs}


@app.put("/api/day-timeline/raid-plan")
async def api_save_day_raid_plan(request: Request):
    from .day_plan import review_plan, save_plan

    body = await request.json()
    timeline = _day_timeline_payload()
    activity = timeline.get("activity")
    if not activity or activity.get("name") != "联队战":
        raise HTTPException(409, "现在没有可安排的联队战，等任务收工并更新进度后再试")
    blocks = body.get("blocks") if isinstance(body, dict) else None
    plan = {"day_start": timeline["day_start"],
            "event_end_at": activity["event_end_at"],
            "blocks": [{"start_min": b["start_min"], "kind": "raid",
                        "runs": b["runs"]} for b in blocks or []]}
    issues = review_plan(plan, timeline)
    if issues:
        raise HTTPException(409, "；".join(issues))
    saved = save_plan(plan["day_start"], plan["event_end_at"], plan["blocks"])
    return {"booking": {**saved, "issues": []}}


@app.put("/api/day-timeline/schedule")
async def api_save_day_schedule(request: Request):
    """保存今日时段表并当场开启大总管：保存即开工，一次到位。"""
    from .day_conductor import BUILTIN_ID, arm
    from .day_plan import review_plan, save_plan

    if _ledger_mode():
        raise HTTPException(403, "纯净账房模式不能自动开工")
    body = await request.json()
    blocks = body.get("blocks") if isinstance(body, dict) else None
    timeline = _day_timeline_payload()
    activity = timeline.get("activity") or {}
    plan = {"day_start": timeline["day_start"],
            "event_end_at": activity.get("event_end_at"), "blocks": blocks}
    if blocks == []:
        from .day_conductor import disarm
        disarm()
        save_plan(plan["day_start"], plan["event_end_at"], [])
        timeline = _day_timeline_payload()
        return {"conductor": timeline["conductor"], "booking": timeline["booking"]}
    issues = review_plan(plan, timeline)
    if issues:
        raise HTTPException(409, "；".join(issues))
    plan["blocks"] = _clean_schedule_blocks(blocks)
    workflow_id = body.get("raid_workflow_id")
    workflow_id = workflow_id if isinstance(workflow_id, str) and workflow_id else BUILTIN_ID
    raid_settings = (_load_panel_settings().get("params", {}).get("raid", {}) or {})
    try:
        arm(plan, timeline, workflow_id, raid_settings, persist=False)
        saved = save_plan(plan["day_start"], plan["event_end_at"], plan["blocks"])
        arm(saved, timeline, workflow_id, raid_settings)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    timeline = _day_timeline_payload()
    return {"conductor": timeline["conductor"], "booking": timeline["booking"]}


def _clean_schedule_blocks(blocks) -> list[dict]:
    """只留前端该给的字段；review_plan 已把关，这里放心转型。"""
    clean = []
    for block in blocks or []:
        kind = block.get("kind")
        if kind == "raid":
            clean.append({"start_min": int(block["start_min"]), "kind": "raid",
                          "runs": int(block["runs"])})
        elif kind == "activity":
            clean.append({key: block[key] for key in
                          ("start_min", "kind", "runs", "script", "event_key")})
        elif kind == "workflow":
            clean.append({"start_min": int(block["start_min"]),
                          "kind": "workflow",
                          "workflow_id": str(block["workflow_id"])})
        else:
            clean.append({"start_min": int(block["start_min"]),
                          "kind": "daily",
                          **({"after_raids": True} if block.get("after_raids") is True else {})})
    return clean


@app.post("/api/expedition-schedule")
async def api_save_schedule(request: Request):
    from .scheduler import load_config, save_config
    body = await request.json()
    cfg = load_config()
    entries = body.get("entries", [])
    # 只留前端该给的字段，别什么都往里塞
    clean = [{
        "time": str(e.get("time", ""))[:5],
        "team_no": int(e.get("team_no", 2)),
        "map_code": str(e.get("map_code", "")),
        "map_name": str(e.get("map_name", "")),
        "enabled": bool(e.get("enabled", True)),
        "last_fired": str(e.get("last_fired", "")),
    } for e in entries if e.get("time") and e.get("map_code")]
    common = []
    for row in body.get("common_plan", []):
        try:
            team = int(row.get("team_no"))
        except Exception:
            continue
        if team not in range(1, 6):
            continue
        formation_id = str(row.get("formation_id") or "")
        if formation_id:
            from touken.custom_formations import find_formation, load_formations
            preset = find_formation(load_formations(), formation_id)
            if preset is None or preset.get("target_team") != team:
                formation_id = ""
        common.append({
            "team_no": team,
            "map_code": str(row.get("map_code", "")),
            "enabled": bool(row.get("enabled", False)),
            "formation_id": formation_id,
        })
    auto_in = body.get("automation", {})
    auto = cfg.get("automation", {})
    try:
        max_delay = int(auto_in.get("max_delay_min", auto.get("max_delay_min", 30)))
    except (TypeError, ValueError):
        max_delay = 30
    auto.update({
        "enabled": bool(auto_in.get("enabled", False)),
        "mode": auto_in.get("mode") if auto_in.get("mode") in ("preset", "custom") else "preset",
        "preset": str(auto_in.get("preset", "小判")),
        "start_time": str(auto_in.get("start_time", "08:00"))[:5],
        "teams": [int(x) for x in auto_in.get("teams", [2, 3, 4])][:3],
        "capitalist": bool(auto_in.get("capitalist", False)),
        "sakura_before_dispatch": bool(auto_in.get("sakura_before_dispatch", auto.get("sakura_before_dispatch", False))),
        "paused_until": str(auto_in.get("paused_until", auto.get("paused_until", ""))),
        "max_delay_min": min(1440, max(1, max_delay)),
    })
    cfg.update({"entries": clean, "common_plan": common, "automation": auto})
    save_config(cfg)
    return {"ok": True, "count": len(clean)}


@app.post("/api/expedition-pause")
async def api_pause_expedition(request: Request):
    from .scheduler import load_config, save_config
    body = await request.json()
    minutes = int(body.get("minutes", 0))
    cfg = load_config()
    if minutes <= 0:
        until = ""
    elif minutes >= 999:
        until = time.strftime("%Y-%m-%d") + " 23:59:59"
    else:
        until = time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(time.time() + minutes * 60))
    cfg["automation"]["paused_until"] = until
    save_config(cfg)
    return {"ok": True, "paused_until": until}


# ── API：状态 ──

@app.get("/api/status")
async def api_status():
    """读取最新的日课成绩单和库存"""
    status_dir = STATUS_DIR
    data = {}
    for fn in ("latest_report.json", "inventory.json"):
        fp = status_dir / fn
        if fp.exists():
            data[fn.replace(".json", "")] = json.loads(fp.read_text(encoding="utf-8"))
    return data


# ── API：仪表盘（总览首页聚合数据）──

# 运行中横幅文案：优先按「进度步骤」细分，其次按脚本名兜底
_STEP_FLAVOR = {
    "raid:lulian": "正在和时间溯行军搏斗中⚔️",
    "raid:hailian": "正在拿水枪喷死对面🔫",
    "daily:内番": "正在安排苦力干活💦",
    "daily:远征": "正在流放刀剑男士⛺",
    "daily:出阵": "正在和时间溯行军搏斗中⚔️",
    "daily:演练": "正在演练场挑软柿子捏🥊",
    "daily:锻刀": "正在盯炉火🔥",
    "daily:刀解": "正在拆快递🗡",
    "daily:合成": "正在喂刀🍡",
    "daily:签到": "正在打卡签到📅",
    "daily:万屋": "正在万屋蹭免费鸡蛋🥚",
    "daily:任务奖励": "正在收日课工资💰",
    "daily:库存快照": "正在盘点家底📦",
}

_SCRIPT_FLAVOR = {
    "daily": "正在爆肝日课📋",
    "workflow": "正在跑自定义工作流🧩",
    "raid": "正在和时间溯行军搏斗中⚔️",
    "pumpkin": "正在南瓜田里刨剪影🎃",
    "edocastle": "正在江户城摸黑巡游🏯",
    "hanafuda": "正在秘宝之里收集花牌🎴",
    "sortie": "正在出阵打图🗡",
    "yosari": "正在提灯照耀的异去探索🏮",
    "osaka": "正在大阪城地下咔咔挖土⛏️",
    "sakura": "正在给刀剑男士刷樱花🌸",
    "practice": "正在演练场挑软柿子捏🥊",
    "expedition": "正在流放刀剑男士⛺",
    "dispatch": "正在流放刀剑男士⛺",
    "forge": "正在盯炉火🔥",
    "sugar": "正在炼糖🍬",
    "inbox_supplies": "正在收件箱翻杂物📮",
    "snapshot": "正在盘点家底📦",
    "game_inventory": "正在读取游戏家底📦",
}


def _flavor_text(script: str | None, step: str) -> str:
    if step in _STEP_FLAVOR:
        return _STEP_FLAVOR[step]
    return _SCRIPT_FLAVOR.get(script or "", "正在本丸干活🔧")


def _dashboard_inventory(inventory: dict | None, now: float) -> dict | None:
    """把库存快照里的炉子剩余时间换算成看板此刻的秒数。"""
    if not inventory:
        return inventory
    result = dict(inventory)
    try:
        captured = time.mktime(time.strptime(
            str(inventory.get("captured_at", "")), "%Y-%m-%d %H:%M:%S"))
        age = max(0, now - captured)
    except Exception:
        age = 0
    furnaces = []
    for raw in inventory.get("furnaces", []):
        furnace = dict(raw)
        remain = furnace.get("remain")
        match = re.fullmatch(r"(\d{1,2}):([0-5]\d):([0-5]\d)", str(remain or ""))
        if furnace.get("state") == "锻造中" and match:
            seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
            furnace["remain_sec"] = max(0, int(seconds - age))
        else:
            furnace["remain_sec"] = None
        furnaces.append(furnace)
    result["furnaces"] = furnaces
    return result


def _dashboard_expeditions(raw_exp: dict, now: float,
                           overdue_grace_sec: int = 6 * 3600) -> list[dict]:
    """整理远征记录；已到点太久的旧记录不再永久占着看板。"""
    expeditions = []
    for team_no, raw in raw_exp.items():
        item = dict(raw)
        item["team_no"] = team_no
        try:
            dispatched = time.mktime(time.strptime(
                raw["dispatched_at"], "%Y-%m-%d %H:%M:%S"))
            remain = dispatched + float(raw.get("duration_min", 0)) * 60 - now
            if remain < -overdue_grace_sec:
                continue
            item["remain_sec"] = max(0, int(remain))
            item["done"] = remain <= 0
        except Exception:
            item["remain_sec"] = None
            item["done"] = False
        expeditions.append(item)
    return sorted(expeditions, key=lambda item: item.get("remain_sec") or 0)


@app.get("/api/dashboard")
async def api_dashboard():
    """首页仪表盘：家底 + 远征倒计时 + 日课成绩单 + 内番，一次拿全"""
    status_dir = STATUS_DIR

    def _read(fn):
        fp = status_dir / fn
        if fp.exists():
            try:
                return json.loads(fp.read_text(encoding="utf-8"))
            except Exception:
                return None
        return None

    now = time.time()
    data = {
        "server_time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "inventory": _dashboard_inventory(_read("inventory.json"), now),
        "latest_report": _read("latest_report.json"),
        "naihanka": _read("naihanka.json"),
    }

    # 远征：给每条算好剩余秒数，前端只管倒计时
    raw_exp = _read("expeditions.json") or {}
    data["expeditions"] = _dashboard_expeditions(raw_exp, now)

    # 远征时刻表：今天还没派的安排（前端显示用）
    try:
        from .scheduler import load_entries
        today = time.strftime("%Y-%m-%d")
        data["schedule"] = [
            e for e in load_entries()
            if e.get("enabled") and e.get("last_fired") != today
        ]
    except Exception:
        data["schedule"] = []

    # 运行中横幅：当前脚本 + 最新进度步骤 → 狐之助文案
    runner = get_runner()
    active = runner.is_running
    script = runner.current_script if active else None
    started = runner.current_started
    progress = _read("progress.json") or {}
    step = progress.get("step", "")

    # 面板起跑的新任务：进度文件的时间戳比任务启动还早 = 上一轮留下的陈年老步，
    # 作废（不然联队战刚点开还没上报，会顶着上次日课的「远征」文案到处跑）
    if active and script != "external" and started and progress.get("at"):
        try:
            if time.mktime(time.strptime(progress["at"], "%Y-%m-%d %H:%M:%S")) < started:
                step = ""
        except Exception:
            pass

    # 定时任务/命令行跑引擎时不走面板 runner，但会写 progress.json：
    # 3 分钟内更新过就算「在跑」，横幅照样营业
    # runner.current_script 有值但线程已结束，说明这是刚跑完的面板任务；
    # 此时 progress.json 仍然很新，不能反过来把它误判成外部任务继续展示。
    if not active and runner.current_script is None and step and progress.get("at"):
        try:
            age = time.time() - time.mktime(time.strptime(progress["at"], "%Y-%m-%d %H:%M:%S"))
            if 0 <= age <= 180:
                active = True
                script = "external"
                started = time.time() - age
        except Exception:
            pass
    if not active:
        step = ""

    label = ""
    if script == "external":
        label = "定时/命令行任务"
    elif script:
        label = list_scripts().get(script, {}).get("label", script)
    data["running"] = {
        "active": active,
        "script": script,
        "label": label,
        "started": started,
        "step": step,
        "flavor": _flavor_text(script, step),
    }

    return data


# ── API：聊天 AI 配置（设置弹窗真正落盘 + 热重载，不用重启）──

def _mask(value: str) -> str:
    """敏感字符串脱敏：头 4 位 + … + 尾 4 位。空值/短值原样返回"""
    if not value:
        return ""
    if len(value) <= 10:
        return "***"
    return value[:4] + "…" + value[-4:]


def _bool(v) -> bool:
    return v in (True, "true", "on", "yes", 1, "1")


def _int_list(v) -> list:
    if not v:
        return []
    if isinstance(v, list):
        return [int(x) for x in v if str(x).strip().lstrip("-").isdigit()]
    return [int(x.strip()) for x in str(v).replace("，", ",").split(",") if x.strip().lstrip("-").isdigit()]


@app.get("/api/bot-config")
async def api_get_bot_config():
    """读取 bot 配置，token 全程脱敏。"""
    cfg = json.loads(_PANEL_CONFIG.read_text(encoding="utf-8"))
    bot = cfg.get("bot", {})
    tg = bot.get("telegram", {})
    qq = bot.get("qq", {})
    bc = bot.get("broadcast", {})
    return {
        "enabled": bool(bot.get("enabled", False)),
        "platform": bot.get("platform", "telegram"),
        "telegram": {
            "token_masked": _mask(tg.get("token", "")),
            "has_token": bool(tg.get("token", "")),
            "allowed_users": list(tg.get("allowed_users", []) or []),
        },
        "qq": {
            "enabled": bool(qq.get("enabled", False)),
            "provider": qq.get("provider", "napcat"),
            "snowluma_http": qq.get("snowluma_http", "http://127.0.0.1:3000"),
            "snowluma_gui_http": qq.get("snowluma_gui_http", "http://127.0.0.1:5099"),
            "admin_qq": list(qq.get("admin_qq", []) or []),
        },
        "broadcast": {
            "qq": bool(bc.get("qq", True)),
            "ntfy": bool(bc.get("ntfy", True)),
        },
        # 哪些改了能热生效，哪些得重启
        "hot_reloadable": {
            "telegram": True,
            "qq": False,   # QQ webhook 在启动时挂载，运行时不能安全卸载
        },
    }


@app.get("/api/qq-status")
async def api_qq_status():
    """探测 OneBot API 与管理页；只检测，不启动或下载任何程序。"""
    import httpx

    cfg = json.loads(_PANEL_CONFIG.read_text(encoding="utf-8"))
    qq = cfg.get("bot", {}).get("qq", {})
    api_url = str(qq.get("snowluma_http", "http://127.0.0.1:3000")).rstrip("/")
    gui_url = str(qq.get("snowluma_gui_http", "http://127.0.0.1:5099")).rstrip("/")

    async def probe(url, suffix=""):
        if not url:
            return False, "未配置地址"
        try:
            async with httpx.AsyncClient(timeout=3, follow_redirects=True) as client:
                r = await client.get(url + suffix)
            return r.status_code < 500, f"HTTP {r.status_code}"
        except Exception as exc:
            name = type(exc).__name__.replace("Error", "")
            return False, name or "连接失败"

    api_ok, api_detail = await probe(api_url, "/get_status")
    gui_ok, gui_detail = await probe(gui_url)
    webhook_ready = any(getattr(route, "path", "") == "/onebot/webhook"
                        for route in app.routes)
    return {
        "enabled": bool(qq.get("enabled", False)),
        "provider": qq.get("provider", "napcat"),
        "api_url": api_url,
        "gui_url": gui_url,
        "api_online": api_ok,
        "api_detail": api_detail,
        "gui_online": gui_ok,
        "gui_detail": gui_detail,
        "webhook_ready": webhook_ready,
        "webhook_url": "http://127.0.0.1:8080/onebot/webhook",
        "state": "connected" if api_ok else "unavailable",
    }


@app.post("/api/bot-config")
async def api_save_bot_config(request: Request):
    """保存 bot 配置。
    - token 留空 = 不改（防止掩码被当新 key 写回去）
    - TG token 改了尝试热重启；QQ 改了下次面板启动才生效
    """
    body = await request.json()
    cfg = json.loads(_PANEL_CONFIG.read_text(encoding="utf-8"))
    bot = cfg.setdefault("bot", {})

    if "enabled" in body:
        bot["enabled"] = _bool(body["enabled"])
    if body.get("platform") in ("telegram", "qq"):
        bot["platform"] = body["platform"]

    # Telegram
    tg = bot.setdefault("telegram", {})
    if "telegram" in body and isinstance(body["telegram"], dict):
        t = body["telegram"]
        # token 留空不动；非空就改
        if t.get("token"):
            tg["token"] = str(t["token"]).strip()
        tg["allowed_users"] = _int_list(t.get("allowed_users", tg.get("allowed_users", [])))

    # QQ
    qq_block = bot.setdefault("qq", {})
    if "qq" in body and isinstance(body["qq"], dict):
        q = body["qq"]
        qq_block["enabled"] = _bool(q.get("enabled"))
        if q.get("provider") in ("napcat", "snowluma", "custom"):
            qq_block["provider"] = q["provider"]
        if q.get("snowluma_http"):
            qq_block["snowluma_http"] = str(q["snowluma_http"]).strip()
        # SnowLuma 远程桌面 / GUI 端口：留空 = 不变，存了就更新
        if "snowluma_gui_http" in q and q.get("snowluma_gui_http") is not None:
            qq_block["snowluma_gui_http"] = str(q["snowluma_gui_http"]).strip()
        qq_block["admin_qq"] = _int_list(q.get("admin_qq", qq_block.get("admin_qq", [])))

    # Broadcast
    bc = bot.setdefault("broadcast", {})
    if "broadcast" in body and isinstance(body["broadcast"], dict):
        b = body["broadcast"]
        bc["qq"] = _bool(b.get("qq", bc.get("qq", True)))
        bc["ntfy"] = _bool(b.get("ntfy", bc.get("ntfy", True)))

    _PANEL_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    # 热重试 Telegram（QQ 提示用户重启面板）
    tg_reload_msg = ""
    if bot.get("platform") == "telegram" and bot.get("enabled"):
        try:
            import __main__ as _bm
            from .bot_telegram import stop_bot, start_bot
            stop_bot(getattr(_bm, "_bot_instance", None))
            _bm._bot_instance = start_bot(_get_gateway())
            tg_reload_msg = "Telegram 已热重启，新 token 立即生效。"
        except Exception as exc:
            tg_reload_msg = f"Telegram 热重启失败：{exc}"

    return {"ok": True, "tg_reload_msg": tg_reload_msg,
            "qq_restart_required": qq_block.get("enabled", False)}


# ── API：结构化运行数据 ──


def _telemetry_store_for(server: str = ""):
    """按服务器选账房库：jp = 日服独立库，其余一律国服主库。"""
    from touken.telemetry import get_jp_telemetry_store, get_telemetry_store
    if str(server or "").strip().lower() == "jp":
        return get_jp_telemetry_store()
    return get_telemetry_store()


@app.get("/api/data/summary")
async def api_data_summary(days: int = 30):
    """稳定机器数据总览；前端与智能建议共用，契约由 schema_version 标识。"""
    from touken.telemetry import get_telemetry_store
    data = get_telemetry_store().summary(days=days)

    def _state(name: str):
        path = STATUS_DIR / name
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, ValueError):
            return None

    data["current_state"] = {
        "inventory": _state("inventory.json"),
        "daily_report": _state("latest_report.json"),
        "expeditions": _state("expeditions.json") or {},
        "naihanka": _state("naihanka.json"),
    }
    return data


@app.get("/api/data/events")
async def api_data_events(limit: int = 100, event_type: str = "",
                          script: str = "", before_id: int | None = None,
                          from_ts: float | None = None,
                          to_ts: float | None = None, server: str = ""):
    """结构化玩法事件；payload 只含机器字段，不依赖中文日志文案。"""
    from touken.telemetry import TELEMETRY_SCHEMA_VERSION
    page_limit = max(1, min(int(limit), 1000))
    items = _telemetry_store_for(server).recent_events(
        limit=page_limit + 1, event_type=event_type or None, script=script or None,
        before_id=before_id, from_ts=from_ts, to_ts=to_ts)
    has_more = len(items) > page_limit
    items = items[:page_limit]
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "items": items,
        "has_more": has_more,
        "next_cursor": items[-1]["id"] if items else None,
    }


@app.get("/api/data/sword-inventory/latest")
async def api_latest_sword_inventory():
    """最近一份所持刀剑盘点；逐把保留，同名刀不会合并。
    只服务 owned_inventory 来源——图鉴（album）快照不是本丸里的具体刀，
    不能冒充盘点；来源不明（老库无法可靠分类）同样不冒充。"""
    from touken.telemetry import get_telemetry_store, TELEMETRY_SCHEMA_VERSION
    store = get_telemetry_store()
    owned = store.latest_sword_snapshot(source="owned_inventory")
    latest = (store.sword_snapshot_detail(owned["id"])
              if owned else None)
    return {"schema_version": TELEMETRY_SCHEMA_VERSION, "snapshot": latest}


@app.get("/api/honmaru-home/situation")
def api_home_situation(server: str = ""):
    if server not in ("", "cn", "jp"):
        raise HTTPException(400, "本丸来源不正确。")
    if server == "jp":
        from touken.jp_home import current_situation
        return {"situation": current_situation(_telemetry_store_for(server))}
    path = STATUS_DIR / "youzu_home_situation.json"
    if not path.exists():
        return {"situation": None}
    try:
        situation = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(situation, dict) or situation.get("schema") != 1
                or not isinstance(situation.get("secretary"), dict)
                or not isinstance(situation.get("parties"), list)
                or not isinstance(situation.get("kiwame_return"), list)
                or not isinstance(situation.get("forge_slots"), list)):
            raise ValueError("unsupported homepage situation")
        return {"situation": situation}
    except (OSError, ValueError) as exc:
        raise HTTPException(503, "本丸近况暂时读不到，原记录已保留。") from exc


@app.get('/api/journal/avatar/{sword_id}')
def api_journal_avatar(sword_id: int):
    if sword_id < 1 or sword_id > 9999:
        raise HTTPException(404, '暂无头像')
    portraits = sorted((RESOURCE_DIR / 'image' / '头像').glob(f'{sword_id:04d}_*.png'))
    if not portraits:
        raise HTTPException(404, '暂无头像')
    return FileResponse(str(portraits[0]), media_type='image/png')


@app.post("/api/honmaru-home/situation/refresh")
def api_refresh_home_situation(server: str = ""):
    if server not in ("", "cn", "jp"):
        raise HTTPException(400, "本丸来源不正确。")
    if server == "jp":
        return api_home_situation(server)
    from touken import youzu_log
    if get_runner().is_running:
        raise HTTPException(409, "执务进行中，收工后再同步近况。")
    try:
        cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        path = youzu_log.pull_log(
            cfg.get("adb_path") or _DEFAULT_ADB_PATH,
            cfg.get("adb_address") or _DEFAULT_ADB_ADDR,
            dest_dir=DEBUG_DIR)
        try:
            events = youzu_log.parse_events(path)
            from touken.sword_receipts import sync_receipts
            sync_receipts(events)
            from .expedition_observation import FILENAME, save_observations
            save_observations(events, STATUS_DIR / FILENAME)
            situation = youzu_log.save_home_situation(
                events,
                STATUS_DIR / "youzu_home_situation.json")
        finally:
            path.unlink(missing_ok=True)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        raise HTTPException(503, "没能从游戏读取近况，请确认模拟器和游戏正在运行。") from exc
    if situation is None:
        raise HTTPException(503, "这次记录里还没有本丸近况，请进入本丸后再试。")
    return {"situation": situation}


@app.get("/api/data/honmaru-profile")
async def api_honmaru_profile(server: str = ""):
    """当前本丸共用档案（候选池 + 编队链接层），只读生成，契约见
    docs/telemetry-data.md「当前本丸共用档案」。"""
    from touken.honmaru_profile import get_honmaru_profile
    if server == 'jp':
        from touken.sword_archive import build_jp_sword_archive
        archive = build_jp_sword_archive(_telemetry_store_for(server))
        entries = [{**row, 'same_team_exclusion_key': row.get('sword_catalog_id')} for row in archive['entries']]
        return {'schema_version': 1, 'generated_at': time.time(), 'roster': {'teams': []},
            'candidate_pool': {'done': bool(entries), 'entries': entries,
                'observed_at': archive.get('observed_at'), 'entry_count': len(entries),
                'completeness': 'full' if archive.get('roster_complete') else 'partial'}}
    _telemetry_store_for(server)
    return get_honmaru_profile()


@app.get("/api/data/sword-archive")
async def api_sword_archive(server: str = ""):
    """刀帐档案：机器盘点 + 人工标注的合并视图（形态确认/要练的刀），
    只读生成，契约见 docs/telemetry-data.md「刀帐档案」。"""
    from touken.sword_archive import get_sword_archive, build_jp_sword_archive
    if server == "jp":
        return build_jp_sword_archive(_telemetry_store_for(server))
    return get_sword_archive()


@app.post("/api/data/sword-archive/annotations")
async def api_save_sword_annotation(request: Request, server: str = ""):
    """保存一条刀帐人工标注；同指纹已存在时更新传入的非空字段。"""
    body = await request.json()
    from touken.telemetry import get_telemetry_store
    keeper = body.get("keeper")
    favorite = body.get("favorite")
    watch = body.get("watch")
    try:
        annotation = _telemetry_store_for(server).save_sword_annotation(
            sword_catalog_id=body.get("sword_catalog_id"),
            kiwame_date=body.get("kiwame_date"),
            level_at_mark=body.get("level_at_mark"),
            form_confirmed=body.get("form_confirmed"),
            keeper=None if keeper is None else int(bool(keeper)),
            favorite=None if favorite is None else int(bool(favorite)),
            watch=None if watch is None else int(bool(watch)),
            note=body.get("note"),
            level_confirmed=body.get("level_confirmed"),
            serial_id=body.get("serial_id"))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "annotation": annotation}


@app.delete("/api/data/sword-archive/annotations/{annotation_id}")
async def api_revoke_sword_annotation(annotation_id: int, server: str = ""):
    """软删一条人工标注（撤销形态确认/要练标记），历史保留不丢。"""
    from touken.telemetry import get_telemetry_store
    try:
        _telemetry_store_for(server).revoke_sword_annotation(annotation_id)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True}


@app.get("/api/data/training/overview")
async def api_training_overview(server: str = ""):
    """练度总览：最新 training.captured 快照逐振列出 level/exp/乱舞，
    附到下一级乱舞还差的习合值与估算振数（need_swords_est 按公告口径
    100 习合值/振估算，未实测）。没有快照 404。契约见
    docs/telemetry-data.md「练度档案（training）」。
    server=jp 时读日服独立账房库。"""
    from touken.training_view import build_training_overview
    result = build_training_overview(_telemetry_store_for(server))
    if result is None:
        raise HTTPException(404, "还没有练度快照，先让收账跑一轮。")
    return result


@app.get("/api/data/training/history/{serial_id}")
async def api_training_history(serial_id: int, server: str = ""):
    """单振刀的练度快照链时间线（training.captured，时间升序），附
    first_max_level_observed_at（链上首次观测到 level=99，不是首次达成）。
    该编号没有快照记录 404。server=jp 时读日服独立账房库。"""
    from touken.training_view import build_training_history
    result = build_training_history(_telemetry_store_for(server), serial_id)
    if result is None:
        raise HTTPException(404, f"编号 {serial_id} 还没有练度快照记录。")
    return result


# ── 数据视图区块 · 掉落统计 + 内番养成 ─────────────────────────────────
# 本区块集中放只读数据视图的新路由；聚合逻辑本体在 touken/drop_stats.py
# 与 touken/training_view.py，契约写在 docs/telemetry-data.md 对应章节。
# 后面工人加新数据路由（锻刀串链/履历包等）请紧挨本区块往下续，别散落。
# ─────────────────────────────────────────────────────────────────────

@app.get("/api/data/drop-stats")
async def api_drop_stats(days: int = 30, server: str = ""):
    """掉落统计：手动 battle.completed + 面板逐圈记录（剔除 not_observed
    圈）合并当分母，sword.obtained 掉落收据当分子，按地图/玩法分组。
    掉率不预算死，前端拿 drops/drop_total 和 battles 自己除；分母口径与
    boss_reached 的 null 语义（手动侧王点未校准一律 null）见
    docs/telemetry-data.md「掉落统计」。days 缺省 30，0=全部。"""
    from touken.drop_stats import build_drop_stats
    from touken.telemetry import get_telemetry_store
    return build_drop_stats(_telemetry_store_for(server), days=days)


@app.get("/api/data/training/internal-affairs")
async def api_training_internal_affairs(server: str = ""):
    """内番养成视图：最新练度快照在册的每振刀，列内番已喂的生存/侦察
    数值与平台期启发式结论（plateau_k=3，连续 K 条快照不增长；上限表
    未校准，true 只是「连续多次收账没再涨」，可能喂满也可能没喂）。
    没有快照 404。契约见 docs/telemetry-data.md「内番养成视图」。
    server=jp 时读日服独立账房库。"""
    from touken.training_view import build_internal_affairs
    result = build_internal_affairs(_telemetry_store_for(server))
    if result is None:
        raise HTTPException(404, "还没有练度快照，先让收账跑一轮。")
    return result


@app.get("/api/data/forge-history")
async def api_forge_history(days: int = 30, server: str = ""):
    """锻刀串链：forge.started ⋈ forge.collected 按炉位+时间序配对，
    附开炉前最近一条近侍观测与配方消耗估计。days 缺省 30，0=全部；
    窗口口径、近侍局限（登录时刻观测，局内换人无法分辨）、cost_est 不含
    委托符/加速符等，见 docs/telemetry-data.md「锻刀串链」。"""
    from touken.forge_history import build_forge_history
    from touken.telemetry import get_telemetry_store
    return build_forge_history(_telemetry_store_for(server), days=days)


@app.get("/api/data/event-points")
async def api_event_points(event_id: str = ""):
    """活动点数历史：不带 event_id 返回最新活动日历的 events 原样列表
    （event_id→活动名本地没有，原样给 id）；带 event_id 返回该活动期内
    「活动点数·{event_id}」读数时间线（结束后 1 天内算收尾读数）。
    契约见 docs/telemetry-data.md「活动点数历史」。"""
    from touken.event_points import (build_event_points_list,
                                     build_event_points_timeline)
    from touken.telemetry import get_telemetry_store
    store = get_telemetry_store()
    event_id = str(event_id or "").strip()
    if not event_id:
        return build_event_points_list(store)
    try:
        result = build_event_points_timeline(store, event_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if result is None:
        raise HTTPException(404, f"活动日历里没有活动 {event_id}。")
    return result


@app.get("/api/data/sword-journal/{serial_id}")
async def api_sword_journal(serial_id: int, server: str = ""):
    """单振刀入手履历：入手（刀帐档案 created_at + sword.obtained 收据）、
    修行进出、首次观测满级，按 ts 升序；缺哪段就没有哪段，不编。
    契约见 docs/telemetry-data.md「入手履历」。"""
    from touken.sword_journal import build_sword_journal
    from touken.telemetry import get_telemetry_store
    result = build_sword_journal(_telemetry_store_for(server), serial_id)
    if result is None:
        raise HTTPException(404, f"编号 {serial_id} 还没有履历记录。")
    return result


@app.get("/api/data/runs")
async def api_data_runs(limit: int = 20, script: str = "",
                        before_started_at: float | None = None,
                        from_ts: float | None = None,
                        to_ts: float | None = None,
                        status: str = "", server: str = ""):
    """每轮任务的结构化结算；圈速按相邻完成事件计算，不含盘点时间。"""
    from touken.telemetry import get_telemetry_store, TELEMETRY_SCHEMA_VERSION
    page_limit = max(1, min(int(limit), 100))
    items = _telemetry_store_for(server).recent_run_summaries(
        limit=page_limit + 1, script=script or None,
        before_started_at=before_started_at, from_ts=from_ts, to_ts=to_ts,
        status=status or None)
    has_more = len(items) > page_limit
    items = items[:page_limit]
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "items": items,
        "has_more": has_more,
        "next_cursor": items[-1]["started_at"] if items else None,
    }


@app.delete("/api/data/runs/{run_id}")
async def api_delete_run(run_id: str):
    """删掉一条任务运行记录（连同它名下的事件明细）。"""
    from touken.telemetry import get_telemetry_store
    if not get_telemetry_store().delete_run(run_id):
        return JSONResponse({"ok": False, "reason": "找不到这条任务记录"}, status_code=404)
    return {"ok": True}


@app.post("/api/data/runs/{run_id}/attach-inventory")
async def api_attach_run_inventory(run_id: str):
    """把用户刚手动盘点的库存补为指定任务的收工快照。"""
    inventory_path = STATUS_DIR / "inventory.json"
    try:
        snapshot = json.loads(inventory_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return JSONResponse(
            {"ok": False, "reason": "还没有库存快照，请先运行“库存快照”"}, status_code=400)
    except (OSError, ValueError):
        return JSONResponse(
            {"ok": False, "reason": "最近的库存快照无法读取，请重新盘点"}, status_code=400)
    captured_ts = inventory_path.stat().st_mtime
    captured_at = str(snapshot.get("captured_at") or "")
    try:
        captured_ts = time.mktime(time.strptime(captured_at, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        pass
    from touken.telemetry import get_telemetry_store
    try:
        summary = get_telemetry_store().attach_inventory_snapshot(
            run_id, snapshot, captured_ts=captured_ts)
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "run": summary}


@app.get("/api/data/client-inventory")
async def api_client_inventory(server: str = ""):
    from touken.telemetry import get_telemetry_store
    data = _telemetry_store_for(server).client_item_inventory()
    if server == "jp":
        from touken.jp_items import inventory
        data['items'] = inventory(_telemetry_store_for(server))
        for reading in data["resources"].values():
            reading.pop("source", None)
    return data


@app.get("/api/data/resource-ledger")
async def api_data_resource_ledger(days: int = 7,
                                   from_ts: float | None = Query(None, alias="from"),
                                   to: float | None = None, server: str = ""):
    """资源总账：窗口内八资源的观察链/归因/缺口，聚合全部在服务端完成。

    from/to（Unix 秒）优先于 days；days 默认 7。契约见 docs/telemetry-data.md。
    """
    to_ts = float(to) if to else time.time()
    start = float(from_ts) if from_ts is not None \
        else to_ts - max(1, min(int(days), 365)) * 86400
    return _telemetry_store_for(server).resource_ledger(start, to_ts)


@app.get("/api/daily_report")
async def api_daily_report(date: str = "", server: str = ""):
    """日报：一天的收支 / 掉落 / 练度 / 目标进度 / 出勤，全部服务端聚合。

    date 缺省=今天（Asia/Shanghai），格式 YYYY-MM-DD。契约见 touken/daily_report.py。
    server=jp 时读日服独立账房库。
    """
    from datetime import date as date_type

    from touken import daily_report
    day = str(date or "").strip()
    if day:
        try:
            date_type.fromisoformat(day)
        except ValueError:
            return JSONResponse({"error": "日期格式得是 YYYY-MM-DD"}, status_code=400)
    return daily_report.build_daily_report(
        _telemetry_store_for(server), day or None)


@app.get("/api/data/ledger-onboarding")
async def api_ledger_onboarding():
    """只给真正空账本的新用户显示一次三步引导。"""
    from touken.ledger_onboarding import ONBOARDING_FILENAME, get_onboarding
    from touken.telemetry import get_telemetry_store
    return get_onboarding(get_telemetry_store(), STATUS_DIR / ONBOARDING_FILENAME)


@app.get("/api/data/game-inventory")
async def api_game_inventory_result():
    from touken.telemetry import get_telemetry_store
    events = get_telemetry_store().recent_events(
        limit=1, event_type="game_inventory.finished")
    return {"result": events[0] if events else None}


@app.post("/api/data/ledger-onboarding")
async def api_update_ledger_onboarding(request: Request):
    """保存引导进度；完成或明确跳过后不再打扰。"""
    from touken.ledger_onboarding import ONBOARDING_FILENAME, update_onboarding
    from touken.telemetry import get_telemetry_store
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("引导请求格式不正确")
        result = update_onboarding(
            get_telemetry_store(), STATUS_DIR / ONBOARDING_FILENAME,
            str(body.get("action") or ""), step=body.get("step"),
        )
    except (OSError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, **result}


@app.get("/api/data/ledger-export")
async def api_ledger_export(format: str = "xlsx"):
    """导出账房快照：Excel 含完整流水/当前家底/每日汇总，CSV 为完整流水。"""
    from touken.ledger_transfer import export_ledger_csv, export_ledger_xlsx
    from touken.telemetry import get_telemetry_store
    selected = str(format or "xlsx").strip().lower()
    if selected not in {"xlsx", "csv"}:
        return JSONResponse(
            {"ok": False, "reason": "只支持 xlsx 或 csv"}, status_code=400)
    store = get_telemetry_store()
    if selected == "xlsx":
        body = export_ledger_xlsx(store)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        body = export_ledger_csv(store)
        media_type = "text/csv; charset=utf-8"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"maamaru-ledger-{stamp}.{selected}"
    return Response(
        content=body, media_type=media_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.post("/api/data/ledger-import/preview")
async def api_ledger_import_preview(request: Request, filename: str = ""):
    """只解析不落盘；返回新记录、重复、冲突和无法识别行。"""
    from touken.ledger_transfer import create_import_preview
    from touken.telemetry import get_telemetry_store
    try:
        result = create_import_preview(
            get_telemetry_store(), await request.body(), Path(filename).name)
    except (OSError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, **result}


@app.post("/api/data/ledger-import/apply")
async def api_ledger_import_apply(request: Request):
    """重新检查预览内容，先备份 telemetry.db，再只写入玩家手动记录。"""
    from touken.ledger_transfer import apply_import_preview
    from touken.telemetry import get_telemetry_store
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError("导入请求格式不正确")
        result = apply_import_preview(
            get_telemetry_store(), str(body.get("preview_id") or ""), BACKUP_DIR,
            accept_conflicts=bool(body.get("accept_conflicts")),
        )
    except (OSError, TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=409)
    return {"ok": True, **result}


@app.post("/api/data/jp-netlog-import")
async def api_jp_netlog_import(request: Request, filename: str = ""):
    """导入日服抓包（chrome://net-export 导出的 JSON）：解析后落日服账房。

    只读导入、纯追加：已落过的抓包重复导入会产生重复快照，
    由落账层去重兜底（内容不变的连续快照不重复记）。
    """
    from touken import jp_import, jp_ledger, netlog
    from touken.telemetry import get_jp_telemetry_store
    body = await request.body()
    if not body:
        return JSONResponse({"ok": False, "reason": "文件是空的"},
                            status_code=400)
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
                "wb", suffix=".json", prefix="jp-netlog-",
                delete=False) as fh:
            fh.write(body)
            tmp_path = fh.name
        transactions = netlog.parse_transactions(
            tmp_path, host_filter="touken-ranbu.jp")
    except (OSError, ValueError) as exc:
        return JSONResponse(
            {"ok": False,
             "reason": f"这份文件读不出来：{exc}。得是 chrome://net-export "
                       f"导出的 JSON（勾了 Include raw bytes）"},
            status_code=400)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    if not transactions:
        return JSONResponse(
            {"ok": False,
             "reason": "里面没找到日服游戏的报文。确认抓包时玩了游戏、"
                       "且导出时勾了 Include raw bytes"},
            status_code=400)
    stats = jp_ledger.import_transactions(
        get_jp_telemetry_store(), transactions)
    summary = jp_import.summarize(transactions)
    return {"ok": True, "stats": stats, "summary": summary,
            "transactions": len(transactions)}


@app.get("/api/jp-listener/status")
async def api_jp_listener_status():
    """日服实时听包状态（off/waiting_browser/listening/error + 计数）。"""
    from touken import jp_listener
    return jp_listener.listener_status()


@app.get('/api/jp-browser-probe')
def api_jp_browser_probe():
    from touken.jp_browser_probe import status
    return status()


@app.get('/api/jp-click-probe')
def api_jp_click_probe():
    from touken.jp_click_probe import status
    return status()


@app.post('/api/jp-click-probe/{action}')
async def api_jp_click_probe_action(action: str, request: Request):
    import asyncio
    from touken import jp_click_probe as probe
    try:
        if action == 'prepare':
            return await asyncio.to_thread(probe.prepare)
        if action == 'cancel':
            return probe.cancel()
        if action in ('background', 'screenoff'):
            body = await request.json()
            return probe.start(body.get('x'), body.get('y'), mode=action)
        if action in ('start', 'immediate'):
            body = await request.json()
            return probe.start(body.get('x'), body.get('y'), immediate=action == 'immediate')
        raise HTTPException(404, '没有这个测试操作')
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except RuntimeError as error:
        raise HTTPException(400, '浏览器未连接或画面读取失败，请打开日服游戏后重试') from error
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(400, '浏览器画面读取超时或连接失败；请恢复游戏窗口后重新获取截图') from error


@app.post('/api/jp-browser-probe')
def api_start_jp_browser_probe():
    from touken.jp_browser_probe import start
    return start()


@app.post("/api/jp-listener/start")
async def api_jp_listener_start():
    """开始听包：日服浏览器没在跑就用专用配置档拉一个起来。

    纯订阅 Network 事件，不注入脚本、不发请求；与手动抓包同质。"""
    from touken import jp_listener
    return jp_listener.start_listener(launch_browser_if_needed=True)


@app.post("/api/jp-listener/stop")
async def api_jp_listener_stop():
    """停止听包（不动浏览器本身，也不动已落的账）。"""
    from touken import jp_listener
    return jp_listener.stop_listener()


@app.post("/api/data/manual-inventory")
async def api_add_manual_inventory(request: Request):
    """手动记家底：只把实际填写的资源作为当前时刻的库存观察。"""
    body = await request.json()
    from touken.telemetry import get_telemetry_store
    try:
        snapshot = get_telemetry_store().add_manual_inventory(
            body.get("resources") or {}, observed_at=body.get("observed_at"))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "snapshot": snapshot}


@app.get("/api/data/manual-inventory")
async def api_manual_inventory(limit: int = 200):
    """列出审神者自己抄入的家底，供“我的手账”纠错。"""
    from touken.telemetry import get_telemetry_store, TELEMETRY_SCHEMA_VERSION
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "items": get_telemetry_store().manual_inventory(limit=limit),
    }


@app.put("/api/data/manual-inventory/{event_id}")
async def api_update_manual_inventory(event_id: int, request: Request):
    body = await request.json()
    from touken.telemetry import get_telemetry_store
    try:
        snapshot = get_telemetry_store().update_manual_inventory(
            event_id, body.get("resources") or {}, observed_at=body.get("observed_at"))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "snapshot": snapshot}


@app.delete("/api/data/manual-inventory/{event_id}")
async def api_delete_manual_inventory(event_id: int):
    from touken.telemetry import get_telemetry_store
    if not get_telemetry_store().delete_manual_inventory(event_id):
        return JSONResponse(
            {"ok": False, "reason": "找不到这条手动家底记录"}, status_code=404)
    return {"ok": True}


@app.get("/api/data/manual-sessions")
async def api_manual_sessions(limit: int = 200, from_ts: float | None = None,
                              to_ts: float | None = None):
    """审神者手动活动记录；与まあ丸 runs 分表、分接口返回。"""
    from touken.telemetry import get_telemetry_store, TELEMETRY_SCHEMA_VERSION
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "items": get_telemetry_store().manual_sessions(
            limit=limit, from_ts=from_ts, to_ts=to_ts),
    }


@app.post("/api/data/manual-sessions")
async def api_add_manual_session(request: Request):
    body = await request.json()
    from touken.telemetry import get_telemetry_store
    try:
        item = get_telemetry_store().add_manual_session(
            script=body.get("script"),
            started_at=float(body.get("started_at")),
            ended_at=float(body.get("ended_at")),
            loops=body.get("loops"), note=body.get("note", ""),
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "item": item}


@app.put("/api/data/manual-sessions/{session_id}")
async def api_update_manual_session(session_id: int, request: Request):
    body = await request.json()
    from touken.telemetry import get_telemetry_store
    try:
        item = get_telemetry_store().update_manual_session(
            session_id, script=body.get("script"),
            started_at=float(body.get("started_at")),
            ended_at=float(body.get("ended_at")),
            loops=body.get("loops"), note=body.get("note", ""),
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "item": item}


@app.delete("/api/data/manual-sessions/{session_id}")
async def api_delete_manual_session(session_id: int):
    from touken.telemetry import get_telemetry_store
    if not get_telemetry_store().delete_manual_session(session_id):
        return JSONResponse(
            {"ok": False, "reason": "找不到这条手动活动记录"}, status_code=404)
    return {"ok": True}


@app.get("/api/data/human-reports")
async def api_human_reports(limit: int = 200):
    from touken.telemetry import get_telemetry_store, TELEMETRY_SCHEMA_VERSION
    store = get_telemetry_store()
    return {"schema_version": TELEMETRY_SCHEMA_VERSION,
            "items": store.human_reports(limit=limit),
            "inventory_gaps": store.inventory_gaps(limit=50)}


@app.post("/api/data/human-reports")
async def api_add_human_report(request: Request):
    body = await request.json()
    from touken.telemetry import get_telemetry_store
    try:
        item = get_telemetry_store().add_human_report(
            occurred_at=float(body.get("occurred_at") or time.time()),
            activities=body.get("activities") or [], note=body.get("note", ""),
            source=body.get("source", "proactive"), gap_key=body.get("gap_key"),
            resource=body.get("resource"), claimed_delta=body.get("claimed_delta"))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "item": item}


@app.post("/api/data/human-reports/batch")
async def api_add_human_report_batch(request: Request):
    body = await request.json()
    from touken.telemetry import get_telemetry_store
    entries = body.get("entries") or {}
    if not isinstance(entries, dict):
        return JSONResponse({"ok": False, "reason": "多资源收支格式不正确"}, status_code=400)
    try:
        items = get_telemetry_store().add_human_report_group(
            occurred_at=float(body.get("occurred_at") or time.time()),
            activities=body.get("activities") or [], note=body.get("note", ""),
            entries=entries)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "items": items, "group_id": items[0]["group_id"]}


@app.put("/api/data/human-reports/group/{group_id}")
async def api_update_human_report_group(group_id: str, request: Request):
    body = await request.json()
    from touken.telemetry import get_telemetry_store
    entries = body.get("entries") or {}
    if not isinstance(entries, dict):
        return JSONResponse({"ok": False, "reason": "多资源收支格式不正确"}, status_code=400)
    try:
        items = get_telemetry_store().update_human_report_group(
            group_id, occurred_at=float(body.get("occurred_at") or time.time()),
            activities=body.get("activities") or [], note=body.get("note", ""),
            entries=entries)
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "items": items, "group_id": group_id}


@app.put("/api/data/human-reports/{report_id}")
async def api_update_human_report(report_id: int, request: Request):
    body = await request.json()
    from touken.telemetry import get_telemetry_store
    try:
        item = get_telemetry_store().update_human_report(
            report_id, occurred_at=float(body.get("occurred_at") or time.time()),
            activities=body.get("activities") or [], note=body.get("note", ""),
            resource=body.get("resource"), claimed_delta=body.get("claimed_delta"))
    except (TypeError, ValueError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "item": item}


@app.delete("/api/data/human-reports/group/{group_id}")
async def api_delete_human_report_group(group_id: str):
    from touken.telemetry import get_telemetry_store
    if not get_telemetry_store().delete_human_report_group(group_id):
        return JSONResponse({"ok": False, "reason": "找不到这组手账"}, status_code=404)
    return {"ok": True}


@app.delete("/api/data/human-reports/{report_id}")
async def api_delete_human_report(report_id: int):
    from touken.telemetry import get_telemetry_store
    if not get_telemetry_store().delete_human_report(report_id):
        return JSONResponse({"ok": False, "reason": "找不到这条审神者报备"}, status_code=404)
    return {"ok": True}


# ── API：规划建议（攒钱小目标） ──

# 活动日历源：腾讯云服务器上 scripts/bili_events_crawler.py 每天扒一次
# B 站官方号公告生成 events.json（部署见交接文档 §23）
EVENTS_CALENDAR_URL = "http://49.235.132.50:8321/events.json"
EVENTS_CACHE_TTL = 6 * 3600


def _load_events_calendar() -> tuple[dict, bool]:
    """读活动日历（先本地 6h 缓存，过期则拉服务器，拉不动用旧缓存）。
    返回 (数据, 是否陈旧的兜底)。"""
    import urllib.request
    cache_path = STATUS_DIR / "events_calendar.json"
    cached = None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if cached and time.time() - cached.get("fetched_at", 0) < EVENTS_CACHE_TTL:
        return cached["data"], False
    try:
        with urllib.request.urlopen(EVENTS_CALENDAR_URL, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        cache_path.write_text(json.dumps({"fetched_at": time.time(), "data": data},
                                         ensure_ascii=False), encoding="utf-8")
        return data, False
    except Exception:
        if cached:
            return cached["data"], True
        return {"announcements": []}, True


@app.get("/api/events")
async def api_events():
    """活动日历：拉服务器上的 events.json，带 6h 本地缓存；拉不动就用旧缓存。"""
    data, stale = _load_events_calendar()
    if not data.get("announcements") and stale:
        return {"announcements": [], "stale": True,
                "reason": "活动日历服务器暂时联系不上"}
    return {**data, "stale": stale}


@app.get("/api/events/timeline")
async def api_events_timeline():
    """事件时间轴：已核实活动按 进行中/7天内/更远 分组排序，
    公告时间候选沉底待确认。契约见 touken/event_timeline.py。"""
    from touken import advisor, event_history, event_timeline
    from touken.telemetry import get_telemetry_store
    planning = advisor.get_planning(get_telemetry_store(),
                                    STATUS_DIR / advisor.GOALS_FILENAME)
    calendar, stale = _load_events_calendar()
    timeline = event_timeline.build_timeline(
        advisor.load_event_cards(STATUS_DIR),
        planning.get("events", []),
        calendar.get("announcements", []),
        periods=event_history.load_history(STATUS_DIR))
    return {**timeline, "calendar_stale": stale}


@app.get("/api/planning")
async def api_planning(server: str = ""):
    """攒钱目标 + 按近日净收支速率推算的到期预测。契约见 touken/advisor.py。"""
    from touken import advisor
    if server == 'jp':
        from touken.jp_planning import report
        return report(_telemetry_store_for(server), JP_DATA_DIR / 'state' / advisor.GOALS_FILENAME)
    _telemetry_store_for(server)
    from touken.telemetry import get_telemetry_store
    try:
        config = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    return advisor.get_planning(get_telemetry_store(),
                                STATUS_DIR / advisor.GOALS_FILENAME,
                                forge_recipe=(config.get("forge") or {}).get("recipe"))


@app.post("/api/planning/gameplay")
async def api_gameplay_planning(request: Request):
    from touken.gameplay_planning import estimate
    from touken.telemetry import get_telemetry_store
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "规划参数无效"}, status_code=400)
    try:
        return estimate(get_telemetry_store(), body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/planning/gameplay-goal")
async def api_add_gameplay_budget_goal(request: Request):
    """把服务端玩法试算的结果保存为独立活动预算。"""
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "规划参数无效"}, status_code=400)
    from touken import advisor
    from touken.telemetry import get_telemetry_store
    try:
        result = advisor.add_gameplay_budget_goal(
            get_telemetry_store(), STATUS_DIR / advisor.GOALS_FILENAME, body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"ok": True, **result}


@app.post("/api/planning/goals")
async def api_add_planning_goal(request: Request, server: str = ""):
    body = await request.json()
    from touken import advisor
    _telemetry_store_for(server)
    path = (JP_DATA_DIR / 'state' if server == 'jp' else STATUS_DIR) / advisor.GOALS_FILENAME
    if server == 'jp' and (body.get('kind', 'resource') != 'resource'
                           or body.get('goal_mode') not in ('amount_target', 'deadline_target')):
        raise HTTPException(400, '日服目前只支持资源数量或日期目标')
    if server == 'jp' and path.exists():
        path.with_suffix('.json.bak').write_bytes(path.read_bytes())
    try:
        if str(body.get("kind") or "") == "fragment":
            goal = advisor.add_fragment_goal(
                path,
                fragment=str(body.get("fragment") or ""),
                target=body.get("target"),
                note=str(body.get("note") or ""))
            return {"ok": True, "goal": goal}
        goal = advisor.add_goal(path,
                                resource=str(body.get("resource") or ""),
                                target=body.get("target"),
                                deadline=str(body.get("deadline") or ""),
                                goal_mode=str(body.get("goal_mode") or "combined"),
                                note=str(body.get("note") or ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "goal": goal}


@app.delete("/api/planning/goals/{goal_id}")
async def api_delete_planning_goal(goal_id: int, server: str = ""):
    from touken import advisor
    _telemetry_store_for(server)
    path = (JP_DATA_DIR / 'state' if server == 'jp' else STATUS_DIR) / advisor.GOALS_FILENAME
    if server == 'jp' and path.exists():
        path.with_suffix('.json.bak').write_bytes(path.read_bytes())
    if not advisor.delete_goal(path, goal_id):
        return JSONResponse({"ok": False, "reason": "找不到这个小目标"}, status_code=404)
    return {"ok": True}


@app.post("/api/planning/event-estimate")
async def api_save_event_estimate(request: Request):
    """保存用户手填的活动场均钥匙预估；实测数据来了自动盖过它。"""
    body = await request.json()
    from touken import advisor
    try:
        card = advisor.save_key_estimate(STATUS_DIR,
                                         str(body.get("event") or ""),
                                         body.get("keys_per_run"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "card": card}


@app.post("/api/planning/event-target")
async def api_save_event_target(request: Request):
    """保存玩家给本期活动定下的货币目标（玉/夜光贝同口径）。"""
    body = await request.json()
    from touken import advisor
    try:
        card = advisor.save_currency_target(
            STATUS_DIR, str(body.get("event") or ""), body.get("target"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "card": card}


@app.post("/api/planning/hanafuda-target")
async def api_save_hanafuda_target(request: Request):
    """老前端兼容口：保存玩家给本期秘宝之里定下的玉目标。"""
    body = await request.json()
    from touken import advisor
    try:
        card = advisor.save_hanafuda_tama_target(
            STATUS_DIR, str(body.get("event") or ""), body.get("target"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, "card": card}


@app.post("/api/planning/event-goals")
async def api_add_event_goal(request: Request):
    """把活动准备立成目标。预算和活动截止时间都由服务端知识卡决定。"""
    body = await request.json()
    from touken import advisor
    from touken.telemetry import get_telemetry_store
    try:
        result = advisor.add_event_goal(get_telemetry_store(),
                                        STATUS_DIR / advisor.GOALS_FILENAME,
                                        str(body.get("event") or ""),
                                        target=body.get("target"))
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=400)
    return {"ok": True, **result}


@app.get("/api/data/ocr")
async def api_data_ocr(limit: int = 100, script: str = "",
                       matched: bool | None = None):
    """OCR 观测明细；供识别质量页面及后续建议引擎使用。"""
    from touken.telemetry import get_telemetry_store, TELEMETRY_SCHEMA_VERSION
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "items": get_telemetry_store().recent_observations(
            limit=limit, script=script or None, matched=matched),
    }


@app.get("/api/stats/ocr")
async def api_ocr_stats():
    """旧前端兼容入口；数据已改从结构化仓库读取，不再解析中文日志。"""
    try:
        from touken.telemetry import get_telemetry_store
        store = get_telemetry_store()
        summary = store.summary(days=7)
        sword_counts = {}
        for event in store.recent_events(
                limit=1000, event_type="pumpkin.sword_obtained"):
            if event["ts"] < summary["window"]["since"]:
                continue
            name = str(event["payload"].get("name", "")).strip()
            if name:
                sword_counts[name] = sword_counts.get(name, 0) + 1
        sword_ranks = sorted(sword_counts.items(), key=lambda x: -x[1])[:20]
        return {
            "sword_ranks": [{"name": n, "count": c} for n, c in sword_ranks],
            "script_counts": summary["runs"]["by_script"],
            "total_logs": summary["ocr"]["total"],
            "ok": True,
            "source": "telemetry-v1",
        }
    except Exception as exc:
        return {"sword_ranks": [], "script_counts": {}, "total_logs": 0,
                "ok": False, "error": str(exc)}


# ── API：聊天 AI 配置（设置弹窗真正落盘 + 热重载，不用重启）──

@app.get("/api/chat-config")
async def api_get_chat_config():
    cfg = json.loads(_PANEL_CONFIG.read_text(encoding="utf-8"))
    ai = cfg.get("ai", {})
    key = ai.get("api_key", "")
    masked = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ""
    from .chat_ai import KITSUNE_SYSTEM_PROMPT
    return {
        "has_key": bool(key) and key != "YOUR_OPENAI_API_KEY",
        "api_key_masked": masked,
        "base_url": ai.get("base_url", ""),
        "model": ai.get("model", ""),
        "system_prompt": ai.get("system_prompt", ""),
        "default_prompt": KITSUNE_SYSTEM_PROMPT,
    }


@app.post("/api/chat-config")
async def api_save_chat_config(request: Request):
    body = await request.json()
    cfg = json.loads(_PANEL_CONFIG.read_text(encoding="utf-8"))
    ai = cfg.setdefault("ai", {})
    # key 留空 = 不改（防止掩码被当成真 key 写回去）
    if body.get("api_key"):
        ai["api_key"] = str(body["api_key"]).strip()
    if body.get("base_url"):
        ai["base_url"] = str(body["base_url"]).strip()
    if body.get("model"):
        ai["model"] = str(body["model"]).strip()
    # 角色 prompt：空字符串 = 恢复默认狐之助（显式清空也合法，所以用 in 判断）
    if "system_prompt" in body:
        sp = str(body["system_prompt"]).strip()
        if sp:
            ai["system_prompt"] = sp
        else:
            ai.pop("system_prompt", None)
    _PANEL_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    from .chat_ai import reload_ai
    reload_ai(str(_PANEL_CONFIG))  # 热重载，不用重启面板
    global _agent_gateway
    _agent_gateway = None            # Agent 网关也重建，新 key/模型/人设即生效
    return {"ok": True}


# ── API：保存/加载面板设置 ──

_SETTINGS_FILE = STATUS_DIR / "panel_settings.json"


def _panel_settings_path(server: str = ""):
    if server in ("", "cn"):
        return _SETTINGS_FILE
    if server == "jp":
        return JP_DATA_DIR / "state" / "panel_settings.json"
    raise HTTPException(400, "没有这个本丸")


def _load_panel_settings(server: str = "") -> dict:
    path = _panel_settings_path(server)
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_panel_settings(data: dict, server: str = ""):
    path = _panel_settings_path(server)
    path.parent.mkdir(parents=True, exist_ok=True)
    if server == "jp" and path.exists():
        path.with_suffix(path.suffix + ".bak").write_bytes(path.read_bytes())
    data["_saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@app.get("/api/gameplay-settings/{script}")
async def api_gameplay_settings(script: str):
    from .scheduled_gameplay import SCRIPTS
    if script not in SCRIPTS:
        raise HTTPException(404, "没有这个玩法")
    info = _scripts_with_preset_options(list_scripts())[script]
    params = {field["key"]: field.get("default", "") for field in info["params"]}
    params.update(_load_panel_settings().get("params", {}).get(script, {}) or {})
    return {"info": info, "params": params}


@app.put("/api/gameplay-settings/{script}")
async def api_save_gameplay_settings(script: str, request: Request):
    from .scheduled_gameplay import SCRIPTS, COUNT_KEYS
    if script not in SCRIPTS:
        raise HTTPException(404, "没有这个玩法")
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("params"), dict):
        raise HTTPException(400, "玩法设置格式不正确")
    allowed = {field["key"] for field in _SCRIPTS[script]["params"]} - COUNT_KEYS
    existing = _load_panel_settings()
    params = existing.setdefault("params", {}).setdefault(script, {})
    params.update({key: value for key, value in body["params"].items() if key in allowed})
    _save_panel_settings(existing)
    return {"params": params}


@app.get("/api/saved-settings")
async def api_get_saved_settings(server: str = ""):
    """获取服务器端保存的面板设置（所有脚本的参数记忆）"""
    return _load_panel_settings(server)


@app.post("/api/saved-settings")
async def api_save_settings(request: Request, server: str = ""):
    """保存面板设置到服务器端（合并式：脚本参数、主题各存各的，互不覆盖）"""
    body = await request.json()
    existing = _load_panel_settings(server)
    if not isinstance(body, dict):
        raise HTTPException(400, "设置格式不正确")
    if server == "jp" and set(body) - {"theme", "scenery", "companion", "backdrop"}:
        raise HTTPException(400, "日服暂不支持自动执行设置")
    existing.pop("_saved_at", None)
    # body 格式: {"params": {"daily": {...}, ...}, "theme": "pixel"}
    params = body.get("params")
    if isinstance(params, dict):
        clean = {k: v for k, v in params.items() if isinstance(v, dict)}
        existing["params"] = clean
    if body.get("theme") in ("washi", "pixel"):
        existing["theme"] = body["theme"]
    if body.get("scenery") in (
        "spring", "autumn", "moonview", "winter", "after_rain",
        "seaside_day", "seaside_sunset", "wisteria", "osaka_hall", "random",
    ):
        existing["scenery"] = body["scenery"]
    if body.get("companion") in ("kogitsune", "hasebe"):
        existing["companion"] = body["companion"]
    backdrop = body.get("backdrop")
    if isinstance(backdrop, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", backdrop):
        existing["backdrop"] = backdrop.lower()
    _save_panel_settings(existing, server)
    return {"ok": True}


# ── 入口 ──

def main():
    import uvicorn
    import argparse

    parser = argparse.ArgumentParser(description="まあ丸 近侍面板")
    parser.add_argument("--host", default="0.0.0.0",
                        help="默认 0.0.0.0 监听全网卡，手机才能连；只想本机用就传 127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--config", default=str(_CONFIG_PATH),
                        help="touken_config.json 路径")
    parser.add_argument("--panel-config", default=str(_PANEL_CONFIG),
                        help="面板配置（AI key 等）")
    args = parser.parse_args()

    print(f"⚡ まあ丸 近侍面板 → http://{args.host}:{args.port}")
    if args.host == "0.0.0.0":
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                lan_ip = s.getsockname()[0]
            print(f"   📱 手机同一 WiFi 下访问 → http://{lan_ip}:{args.port}")
        except OSError:
            pass
    print(f"   配置: {args.config}")
    print(f"   面板配置: {args.panel_config}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
