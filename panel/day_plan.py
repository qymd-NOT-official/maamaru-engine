"""玩家选定的今日时段表：单独玩法、自定义任务流、一键日课都能排。

这里只记计划，不启动任务。块格式 v2：
{start_min: int, kind: "raid"|"activity"|"workflow"|"daily", runs?: int, workflow_id?: str}
（activity 带 script、event_key、runs；workflow_id 仅 workflow 需要；v1 的 {start_min,runs}
块读入时一律视为 raid。）
"""

from __future__ import annotations

import json
import math
import shutil
from pathlib import Path

from touken.runtime_paths import STATE_DIR

PLAN_PATH = STATE_DIR / "day_plan.json"
PLAN_VERSION = 2
MAX_BLOCKS = 6
_CN_DIGITS = "零一二三四五六七八九"
BLOCK_KINDS = ("raid", "activity", "workflow", "daily")
# workflow/daily 没有可靠时长，排计划时统一按 30 分钟估算占用。
GENERIC_BLOCK_MINUTES = 30


def _normalize_block(block: dict) -> dict | None:
    """把块规整成 v2 精确字段；认不出的块返回 None。"""
    if not isinstance(block, dict) or type(block.get("start_min")) is not int:
        return None
    kind = block.get("kind")
    if kind == "raid":
        if type(block.get("runs")) is not int:
            return None
        return {"start_min": block["start_min"], "kind": "raid",
                "runs": block["runs"]}
    if kind == "activity":
        if (type(block.get("runs")) is not int
                or not isinstance(block.get("script"), str)
                or not isinstance(block.get("event_key"), str)):
            return None
        return {key: block[key] for key in ("start_min", "kind", "runs", "script", "event_key")}
    if kind == "workflow":
        if not isinstance(block.get("workflow_id"), str) or not block["workflow_id"]:
            return None
        return {"start_min": block["start_min"], "kind": "workflow",
                "workflow_id": block["workflow_id"]}
    if kind == "daily":
        return {"start_min": block["start_min"], "kind": "daily",
                **({"after_raids": True} if block.get("after_raids") is True else {})}
    return None


def load_plan(path: Path = PLAN_PATH) -> dict | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    blocks = raw.get("blocks")
    if version == 1:
        # v1：{start_min, runs} 块一律视为联队战；读入即升级到 v2 形态。
        if (not isinstance(blocks, list) or not 1 <= len(blocks) <= 2
                or any(not isinstance(block, dict)
                       or type(block.get("start_min")) is not int
                       or type(block.get("runs")) is not int
                       for block in blocks)):
            return None
        blocks = [{"start_min": block["start_min"], "kind": "raid",
                   "runs": block["runs"]} for block in blocks]
    elif version == 2:
        if not isinstance(blocks, list) or not 1 <= len(blocks) <= MAX_BLOCKS:
            return None
        normalized = [_normalize_block(block) for block in blocks]
        if any(block is None for block in normalized):
            return None
        blocks = normalized
    else:
        return None
    return {"version": PLAN_VERSION, "day_start": raw.get("day_start"),
            "event_end_at": raw.get("event_end_at"), "blocks": blocks}


def review_plan(plan: dict, timeline: dict) -> list[str]:
    """用当前时间表重审已保存的安排；旧安排不会被静默改写。

    开工时间已经过去不算错误——过点块的新语义是「尽快排队开工」。
    """
    issues = []
    activity = timeline.get("activity") or {}
    blocks = plan.get("blocks")
    if plan.get("day_start") != timeline.get("day_start"):
        issues.append("这不是今天的安排")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= MAX_BLOCKS:
        return issues + [f"请安排一至{_CN_DIGITS[MAX_BLOCKS]}个时段"]
    raid_blocks = [block for block in blocks
                   if isinstance(block, dict) and block.get("kind") == "raid"]
    if raid_blocks:
        if not activity or activity.get("name") != "联队战":
            issues.append("当前没有可核对的联队战进度")
        elif plan.get("event_end_at") is not None:
            # 活动结束时间由当前卡片和墙钟反推，允许秒级读数误差。
            try:
                changed = abs(float(plan.get("event_end_at") or 0)
                              - float(activity["event_end_at"])) > 60
            except (TypeError, ValueError, OverflowError):
                changed = True
            if changed:
                issues.append("活动时间已变化，请重新安排")
    pace = int(activity.get("seconds_per_loop") or 0)
    deadline = None
    if raid_blocks and activity.get("name") == "联队战":
        if pace <= 0:
            return issues + ["还没有可靠的本期圈速"]
        deadline = min(1680, math.floor(
            (activity["event_end_at"] - timeline["day_start"]) / 60) - 5)
    spans = []
    total = 0
    for i, block in enumerate(blocks, 1):
        if not isinstance(block, dict) or type(block.get("start_min")) is not int:
            issues.append(f"第{i}段的时间或类型不正确")
            continue
        start, kind = block["start_min"], block.get("kind")
        if kind not in BLOCK_KINDS or not 0 <= start < 1680:
            issues.append(f"第{i}段需要填写今天的时间和合法的时段类型")
            continue
        if kind == "raid":
            if type(block.get("runs")) is not int or not 1 <= block["runs"] <= 99:
                issues.append(f"第{i}段需要填写今天的时间和 1–99 圈")
                continue
            total += block["runs"]
            end = start + math.ceil(block["runs"] * pace / 60)
            if deadline is not None and end > deadline:
                issues.append(f"第{i}段预计赶不上今天的收摊时间")
            for occupied in activity.get("occupied", []):
                if start < occupied["end_min"] and end > occupied["start_min"]:
                    issues.append(f"第{i}段会撞上{occupied['label']}")
                    break
        elif kind == "activity":
            from .scheduled_gameplay import issues as gameplay_issues
            if type(block.get("runs")) is not int or not 1 <= block["runs"] <= 99:
                issues.append(f"第{i}段次数要填 1–99")
                continue
            issues.extend(f"第{i}段：{issue}" for issue in gameplay_issues(block, timeline))
            end = start + GENERIC_BLOCK_MINUTES
        elif kind == "workflow":
            if (not isinstance(block.get("workflow_id"), str)
                    or not block["workflow_id"]):
                issues.append(f"第{i}段需要选一个任务流")
                continue
            end = start + 1  # 耗时未知，不用虚构的半小时阻止其他定时安排。
        else:  # daily
            end = start + GENERIC_BLOCK_MINUTES
        spans.append((start, end))
    if total > int(activity.get("remaining_runs") or 0):
        issues.append("安排的圈数超过本期剩余圈数")
    starts = [span[0] for span in spans]
    if starts != sorted(starts):
        issues.append("请按开工时间排列时段")
    for previous, current in zip(spans, spans[1:]):
        if previous[1] > current[0]:
            issues.append("时段互相重叠")
            break
    return issues


def save_plan(day_start: float, event_end_at: float | None, blocks: list[dict],
              path: Path = PLAN_PATH) -> dict:
    """原子替换；写入失败时原安排保持完整。blocks 须为 v2 结构。"""
    plan = {"version": PLAN_VERSION, "day_start": day_start,
            "event_end_at": event_end_at, "blocks": blocks}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    temporary.replace(path)
    return plan
