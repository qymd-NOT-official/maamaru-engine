# -*- coding: utf-8 -*-
"""远征派遣记录的只读解析（touken 层共享入口）。

唯一事实来源：STATUS_DIR/expeditions.json——flows/expedition.py 派遣成功
时落盘（map_code/map_name/era/slot/duration_min/dispatched_at），
panel/server.py、panel/scheduler.py 也读同一文件。本模块只读不写，
不新建第二份状态；规划器与测试可注入显式记录做确定性求值。

诚实契约：
  - 文件不存在 = 没有任何派遣记录（正常状态，status=missing）；
  - JSON 损坏/顶层不是 dict/条目结构坏 = corrupt，绝不静默当全员空闲；
  - dispatched_at 或 duration_min 不可靠 → 队伍确认状态未知
    （unknown_end），调用方必须排除该队并降级；
  - 活跃记录缺 map_code → unknown_map_active，占用地点未知，
    调用方不得伪称其余地图都安全；
  - 调用方带游戏事件流（load_active_expeditions(events=...)）时，
    游戏确认已回家的残留记录以游戏为准从视图翻篇（玩家优先，
    2026-10-14），本模块仍只读不写，文件销账归收发流程。
"""

import json
import math
import time

from .runtime_paths import STATUS_DIR

EXP_RECORD_PATH = STATUS_DIR / "expeditions.json"
_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _parse_dispatched_at(value):
    """'%Y-%m-%d %H:%M:%S' 本地时间 → epoch 秒；不可靠返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        return time.mktime(time.strptime(value, _TIME_FORMAT))
    except (ValueError, OverflowError):
        return None


def parse_expedition_records(raw, now: float) -> dict:
    """把 expeditions.json 的原始 dict 解析成进行中远征视图（纯函数）。

    Args:
        raw: 文件内容 dict（{str(team_no): record}）；None/非 dict 视为损坏。
        now: 当前时刻 epoch 秒。

    Returns:
        {"status": "ok" | "corrupt",
         "active": {team_no: {"map_code", "map_name", "duration_min",
                              "return_at", "return_text", "remain_min"}},
         "unknown_end": [team_no],         # 在外与否无法判断 → 按未知排除
         "unknown_map_active": [team_no],  # 活跃但占用地点未知
         "warnings": [str]}
    """
    out = {"status": "ok", "active": {}, "unknown_end": [],
           "unknown_map_active": [], "warnings": []}
    if not isinstance(raw, dict):
        out["status"] = "corrupt"
        out["warnings"].append("远征派遣记录结构损坏（顶层不是队伍表）："
                               "无法确认有没有队伍在外面，按未知降级")
        return out
    for key, rec in raw.items():
        try:
            team_no = int(key)
        except (TypeError, ValueError):
            out["status"] = "corrupt"
            out["warnings"].append(f"远征派遣记录含无法识别的队伍键 {key!r}，"
                                   "该条作废并降级（不伪造队号）")
            continue
        if not isinstance(rec, dict):
            # 队号已知但条目损坏：无法判断该队是否仍在外面，
            # 按状态未知排除该队，绝不放进候选
            out["status"] = "corrupt"
            out["unknown_end"].append(team_no)
            out["warnings"].append(
                f"部队{team_no}的远征记录结构损坏：该队是否仍在外面"
                "无法判断，排除出本轮规划并降级")
            continue
        dispatched = _parse_dispatched_at(rec.get("dispatched_at"))
        duration = rec.get("duration_min")
        if (dispatched is None or isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)):
            # 队伍有派遣记录但算不出结束时间：不能当它空闲
            out["unknown_end"].append(team_no)
            out["warnings"].append(
                f"部队{team_no}的远征记录时间不可靠（dispatched_at/"
                "duration_min 读不出）：该队状态未知，排除出本轮规划")
            continue
        map_code = rec.get("map_code")
        if not isinstance(map_code, str) or not map_code:
            map_code = None  # 非字符串/不可哈希/空串一律按地点未知处理
        return_at = dispatched + duration * 60
        if return_at <= now:
            continue  # 已到期：视为已归来，不再占队或地图
        info = {"map_code": map_code,
                "map_name": rec.get("map_name")
                if isinstance(rec.get("map_name"), str) else "",
                "duration_min": duration,
                "return_at": return_at,
                "return_text": time.strftime(_TIME_FORMAT,
                                             time.localtime(return_at)),
                "remain_min": round((return_at - now) / 60.0, 1)}
        out["active"][team_no] = info
        if not info["map_code"]:
            out["unknown_map_active"].append(team_no)
            out["warnings"].append(
                f"部队{team_no}正在远征但记录缺 map_code：占用地点未知，"
                "其余地图是否安全无法确认，整卷降级")
    return out


def reconcile_view_with_game(parsed, events, now=None) -> list[int]:
    """把游戏确认已回家的记录从解析视图里剔除（只动视图，不碰文件）。

    玩家优先规矩（2026-10-14）：脚本自己记的派遣旧账与游戏现实冲突时
    以现实为准。/party/list 新鲜成功响应明说该队 status=1（待命）→
    记录是残留（多半玩家手动收过菜），从 active/unknown_end 剔除、
    记进 parsed["reconciled"]；游戏说还在外、数据缺失/过期一律不动。
    返回被翻篇的队号列表。剔除记录不写 warnings——冲突已被游戏数据
    正面解决，不该再让整个规划卷降级 needs_confirmation。
    """
    from . import game_state
    now = time.time() if now is None else now
    candidates = list(parsed.get("active") or {}) + \
        list(parsed.get("unknown_end") or [])
    cleared = [team_no for team_no in candidates
               if game_state.party_home(events, team_no, now=now) is True]
    for team_no in cleared:
        active = parsed.get("active")
        if isinstance(active, dict):
            active.pop(team_no, None)
        unknown_end = parsed.get("unknown_end")
        if isinstance(unknown_end, list) and team_no in unknown_end:
            unknown_end.remove(team_no)
    if cleared:
        parsed["reconciled"] = sorted(
            set(parsed.get("reconciled") or []) | set(cleared))
    return cleared


def load_active_expeditions(now=None, path=None, events=None) -> dict:
    """读取并解析 expeditions.json（只读）。

    文件不存在 → status=missing（没有任何派遣记录，属正常状态）；
    读取/解析失败 → status=corrupt + warning，绝不静默当全员空闲。
    events 给了（游戏事件流）就顺手做对账：游戏确认已回家的残留记录
    从视图剔除（见 reconcile_view_with_game）；本模块只读，文件本身
    的销账归 flows/expedition.py 的收发流程管。
    """
    now = now if now is not None else time.time()
    path = path or EXP_RECORD_PATH
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"status": "missing", "active": {}, "unknown_end": [],
                "unknown_map_active": [], "warnings": []}
    except OSError as exc:
        return {"status": "corrupt", "active": {}, "unknown_end": [],
                "unknown_map_active": [],
                "warnings": [f"远征派遣记录读取失败（{exc}）："
                             "无法确认有没有队伍在外面，按未知降级"]}
    try:
        raw = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"status": "corrupt", "active": {}, "unknown_end": [],
                "unknown_map_active": [],
                "warnings": ["远征派遣记录 JSON 损坏："
                             "无法确认有没有队伍在外面，按未知降级"]}
    parsed = parse_expedition_records(raw, now)
    if events:
        reconcile_view_with_game(parsed, events, now=now)
    return parsed
