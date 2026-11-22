"""时间表的单玩法契约：复用玩法设置与出阵步骤，只覆盖本段次数。"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from touken import event_timeline as events

SCRIPTS = ("raid", "hanafuda", "osaka", "edocastle", "pumpkin", "sortie", "yosari")
COUNT_KEYS = {"runs", "rounds", "loops", "floors", "max_runs", "refill_run_limit"}

# 建议层（时间表淡影）覆盖的非代币活动玩法：raid/hanafuda 有目标算盘走
# 自己的口径；sortie 是常驻合战场不算活动。这四个按「日历确认正在开
# + 已保存玩法设置出次数」给建议，确认不了就不开口。
SUGGESTION_SCRIPTS = ("osaka", "edocastle", "pumpkin", "yosari")

# 异去在 SCRIPT_EVENT_MAP 里按常驻处理（概览页常用功能不收它），但建议
# 层必须有「日历确认正在开」的证据才对玩家开口：公告候选或知识卡对上
# 这些名字才算数。
_YOSARI_EVENT_NAMES = ("异去", "特命调查")

# 各玩法 builder 的取次数字段口径（与 server 的 _run_count 兼容链一致：
# 新版统一 "runs"，旧字段兜底；默认值与注册 schema 相同，schema 缺键时
# 才用得到这里的兜底）。
_RUN_DEFAULTS = {"osaka": 1, "edocastle": 6, "pumpkin": 4, "yosari": 1}
_RUN_LEGACY_KEYS = {
    "osaka": ("floors",),
    "edocastle": ("max_runs", "refill_run_limit"),
    "pumpkin": ("max_skips",),
    "yosari": ("loops",),
}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def event_window(script: str, cards: dict, announcements: list, now: float) -> dict | None:
    """与常用功能使用相同活动证据；固定本期窗口，禁止换期后接着跑。"""
    if script not in events.SCRIPT_EVENT_MAP:
        return {"event_key": "permanent", "end_at": None}
    current = datetime.fromtimestamp(now, events._TZ)
    for name in events.SCRIPT_EVENT_MAP[script]:
        candidates = []
        card = cards.get(name)
        if card:
            start, end, _ = events._card_window(card)
            candidates.append((start, end))
        for announcement in announcements:
            for candidate in announcement.get("schedule_candidates") or []:
                if events._event_names_match(candidate.get("name"), name):
                    candidates.append((events._parse_dt(candidate.get("start_at")),
                                       events._parse_dt(candidate.get("end_at"))))
        for start, end in candidates:
            if start and start <= current and (end is None or current < end):
                return {"event_key": _digest([script, start.timestamp(),
                                               end.timestamp() if end else None]),
                        "end_at": end.timestamp() if end else None}
    return None


def spec(script: str) -> dict:
    from .server import _load_panel_settings, _SCRIPTS
    from touken.custom_formations import load_formations
    if script not in SCRIPTS:
        raise ValueError("这个玩法暂不支持按次数安排，请使用定时任务流")
    info = _SCRIPTS[script]
    params = merged_params(script)
    settings = {key: value for key, value in params.items() if key not in COUNT_KEYS}
    formation = None
    team = settings.get("team_no", "3")
    if isinstance(team, str) and team.startswith("preset:"):
        formation = next((item for item in load_formations() if item.get("id") == team[7:]), None)
        team = formation.get("target_team") if formation else None
    try:
        team = int(team)
    except (ValueError, TypeError):
        team = 0
    if team not in range(1, 6):
        raise ValueError("出阵部队还没认清，请检查玩法设置")
    return {"label": info["label"], "params": settings, "team_no": team,
            "signature": _digest([script, settings, formation])}


def merged_params(script: str, saved: dict | None = None) -> dict:
    """注册默认值打底 + 用户已保存玩法设置覆盖（含次数字段，未经剔除）。

    建议层与 spec 共用这一份，保证「建议按你的设置跑」和实际执行用的是
    同一组参数；没保存过设置时就是注册默认值。
    """
    from .server import _load_panel_settings, _SCRIPTS
    if script not in SCRIPTS:
        raise ValueError("这个玩法暂不支持按次数安排，请使用定时任务流")
    info = _SCRIPTS[script]
    if saved is None:
        saved = _load_settings(script)
    params = {field["key"]: field.get("default", "") for field in info["params"]}
    params.update(saved)
    return params


def _load_settings(script: str) -> dict:
    from .server import _load_panel_settings
    saved = _load_panel_settings().get("params", {}).get(script, {})
    return saved if isinstance(saved, dict) else {}


def configured_runs(script: str) -> tuple[int, bool]:
    """按该玩法 builder 的口径取「这次跑几次」。

    返回 (次数, 是否来自玩家已保存设置)。非法值与旧版的 0 遗留都迁回
    安全默认值（与 server._positive_run_count 同口径），建议层绝不照
    garbage 数字排块。
    """
    saved = _load_settings(script)
    params = merged_params(script, saved)
    default = _RUN_DEFAULTS.get(script, 1)
    raw = params.get("runs")
    if raw in (None, ""):
        for key in _RUN_LEGACY_KEYS.get(script, ()):
            if params.get(key) not in (None, ""):
                raw = params[key]
                break
    try:
        runs = int(raw)
    except (TypeError, ValueError):
        runs = default
    if runs <= 0:
        runs = default
    return runs, bool(saved)


def suggestion_window(script: str, cards: dict, announcements: list,
                      now: float) -> dict | None:
    """建议层的「活动正在开」证据，确认不了返回 None（绝不发明活动）。

    绑定日历的玩法沿用 event_window 的同一套证据（知识卡窗口或公告候选
    窗口）；异去在 event_window 里恒返回常驻，不能当证据，这里额外要求
    公告/知识卡对上「异去/特命调查」名字。收编成的安排块仍用 catalog 的
    event_key 口径（异去=permanent），保证 review 时对得上。
    """
    if script != "yosari":
        return event_window(script, cards, announcements, now)
    current = datetime.fromtimestamp(now, events._TZ)
    for name in _YOSARI_EVENT_NAMES:
        candidates = []
        card = cards.get(name)
        if card:
            start, end, _ = events._card_window(card)
            candidates.append((start, end))
        for announcement in announcements or []:
            for candidate in announcement.get("schedule_candidates") or []:
                if events._event_names_match(candidate.get("name"), name):
                    candidates.append((events._parse_dt(candidate.get("start_at")),
                                       events._parse_dt(candidate.get("end_at"))))
        for start, end in candidates:
            if start and start <= current and (end is None or current < end):
                return {"event_key": "permanent",
                        "end_at": end.timestamp() if end else None}
    return None


def catalog(now: float | None = None) -> list[dict]:
    import time
    from .server import _load_events_calendar, _SCRIPTS
    from touken.advisor import load_event_cards
    from touken.runtime_paths import STATUS_DIR
    calendar, _ = _load_events_calendar()
    cards = load_event_cards(STATUS_DIR)
    result = []
    for script in SCRIPTS:
        window = event_window(script, cards, calendar.get("announcements", []),
                              time.time() if now is None else now)
        result.append({"script": script, "label": _SCRIPTS[script]["label"],
                       "available": window is not None,
                       **(window or {"event_key": "", "end_at": None})})
    return result


def issues(block: dict, timeline: dict, *, check_settings: bool = False) -> list[str]:
    option = next((item for item in timeline.get("gameplay_options", [])
                   if item["script"] == block.get("script")), None)
    if not option or not option["available"]:
        return ["这个玩法当前未开放，请重新安排"]
    if block.get("event_key") != option["event_key"]:
        return ["活动已换期，请重新安排"]
    end_at = option.get("end_at")
    start_at = timeline["day_start"] + block["start_min"] * 60
    if end_at and max(start_at, timeline.get("now", start_at)) >= end_at:
        return ["这段开工时活动已结束"]
    try:
        current = spec(block["script"])
    except ValueError as exc:
        return [str(exc)]
    if check_settings and current["signature"] != block.get("gameplay_signature"):
        return ["玩法设置或预设编队已变化，请重新保存安排"]
    from .day_timeline import _occupied_segments
    # 没有圈速时只核对开工附近的保守占用，真实出阵仍由既有安全流程把关。
    start = max(block["start_min"], (timeline.get("now", start_at) - timeline["day_start"]) / 60)
    for occupied in _occupied_segments(timeline.get("expeditions", []), {}, current["team_no"], include_estimated=False):
        if occupied.get("estimated"):
            continue
        if start < occupied["end_min"] and start + 30 > occupied["start_min"]:
            return [f"会撞上{occupied['label']}"]
    return []
