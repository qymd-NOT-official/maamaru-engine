"""独立读取游戏记录与 OCR 家底；不执行日课，不领取或消耗资源。"""

import json
from pathlib import Path

from touken.flow_control import FlowAborted
from touken.runtime_paths import STATUS_DIR
from touken.telemetry import record_event


def sync_game_records(adb_path, adb_address):
    """统一采集管线 + 远征观察消费方；行为见 touken.record_sync。"""
    from touken.record_sync import collect_game_records
    from .expedition_observation import FILENAME, save_observations

    return collect_game_records(
        adb_path, adb_address,
        extra_consumers=(lambda events: save_observations(events, STATUS_DIR / FILENAME),))


def refresh_game_inventory(config_path, params, *, make_agent):
    result = {"records": "failed", "ocr": "failed", "resources": {}}
    yield "[家底] 正在读取游戏记录……"
    try:
        cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        if not str(cfg.get("adb_address", "")).strip():
            from touken.emulator_discovery import auto_configure_emulator
            auto_configure_emulator(Path(config_path))
            cfg = json.loads(Path(config_path).read_text(encoding="utf-8"))
        from touken.youzu_log import DEFAULT_ADB, DEFAULT_ADDRESS
        synced = sync_game_records(cfg.get("adb_path") or DEFAULT_ADB,
                                   cfg.get("adb_address") or DEFAULT_ADDRESS)
        result["resources"].update(synced["resources"])
        result["records"] = "ok" if synced["resources"] else "empty"
        yield ("[家底] 游戏记录已读取" if synced["resources"] else
               "[家底] 游戏记录里没有资源读数，继续画面盘点")
    except Exception:
        yield "[家底] 游戏记录读取失败，请确认模拟器连接；继续尝试画面盘点"

    yield "[家底] 正在通过游戏画面盘点资源、小判和符……"
    snapshot = STATUS_DIR / "inventory.json"
    before = snapshot.stat().st_mtime_ns if snapshot.exists() else None
    agent = None
    try:
        agent = make_agent(config_path)
        yield from agent.status_snapshot_stream()
        if snapshot.exists() and snapshot.stat().st_mtime_ns != before:
            resources = json.loads(snapshot.read_text(encoding="utf-8")).get("resources", {})
            known = {name: value for name, value in resources.items()
                     if isinstance(value, (int, float)) and not isinstance(value, bool)}
            result["resources"].update(known)
            result["ocr"] = "ok" if len(known) == 8 else "partial" if known else "empty"
        else:
            yield "[家底] 画面盘点失败，没有取得本次家底"
    except Exception:
        yield "[家底] 画面盘点失败，请确认游戏已进入本丸"
    finally:
        if agent is not None:
            try:
                yield from agent.navigate_to_stream("本丸")
            except Exception:
                yield "[家底] 返回本丸失败，请查看模拟器页面"

    record_event("game_inventory.finished", result)
    if result["records"] != "ok" or result["ocr"] != "ok":
        yield "[家底] 读取未全部完成，已保留确认过的读数；可以重试"
        raise FlowAborted("家底读取未全部完成")
    yield "[家底] ✅ 游戏记录与画面盘点均已完成"
