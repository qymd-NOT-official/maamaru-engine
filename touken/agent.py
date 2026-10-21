# -*- coding: utf-8 -*-
"""
ToukenAgent 主引擎 = 各层能力的组装
真正的实现分散在：
  - device_adapter.py   底层：截图/点击/OCR/模板匹配
  - navigator.py     中层：通用识别 + 导航 + 弹窗
  - flows/           上层：登录、出战、领取等各业务
"""

import json
import time
from pathlib import Path

from .device_adapter import DeviceAdapter
from .navigator import NavigationMixin
from .runtime_paths import STATUS_DIR
from .flows import LoginMixin, BattleMixin, RewardsMixin, RaidMixin, PumpkinMixin, NaihankaMixin, SortieMixin, ExpeditionMixin, RepairMixin, PracticeMixin, SigninMixin, DailyMixin, SmithMixin, SynthesizeMixin, SugarMixin, SakuraMixin, LogoutMixin, SnapshotMixin, OsakaMixin, EdocastleMixin, HanafudaMixin, SwordInventoryMixin, TeamRosterMixin, FormationEditorMixin


class ToukenAgent(LoginMixin, NavigationMixin, BattleMixin, RewardsMixin, RaidMixin, PumpkinMixin, NaihankaMixin, SortieMixin, ExpeditionMixin, RepairMixin, PracticeMixin, SigninMixin, DailyMixin, SmithMixin, SynthesizeMixin, SugarMixin, SakuraMixin, LogoutMixin, SnapshotMixin, OsakaMixin, EdocastleMixin, HanafudaMixin, SwordInventoryMixin, TeamRosterMixin, FormationEditorMixin):
    """
    刀剑乱舞 Agent 主引擎
    所有操作基于配置文件，不硬编码任何游戏特定内容
    """

    def __init__(self, config_path: str, device: DeviceAdapter):
        with open(config_path, "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.device = device
        self.current_location = None
        self._root = Path(config_path).resolve().parent
        self._progress_file = STATUS_DIR / "progress.json"

    def set_progress(self, step: str):
        """上报当前进度给面板仪表盘横幅（如 'raid:lulian'、'daily:内番'）。

        写失败绝不许影响干活，所以全部异常吞掉。
        """
        try:
            self._progress_file.parent.mkdir(parents=True, exist_ok=True)
            self._progress_file.write_text(json.dumps({
                "step": step,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def record_event(self, event_type: str, **payload):
        """记录稳定的机器事件；展示文案变化不会影响统计和智能建议。

        返回事件 id（写失败或异常返回 None），供 resource.change 的
        source_event_id 双写关联用。
        """
        try:
            from .telemetry import record_event
            return record_event(event_type, payload)
        except Exception:
            return None

    def record_resource_change(self, resource: str, delta: int | float, *,
                               source: str, attribution: str = "confirmed",
                               **evidence):
        """统一资源流水入口；玩法事件只说明事实，资源收支都从这里交账。"""
        try:
            from .telemetry import LEDGER_RESOURCES
            if resource not in LEDGER_RESOURCES or isinstance(delta, bool):
                return None
            amount = float(delta)
            if not amount or not amount.is_integer():
                return None
            payload = {
                "resource": resource,
                "delta": int(amount),
                "source": source,
                "attribution": attribution,
                **evidence,
            }
            return self.record_event("resource.change", **payload)
        except Exception:
            return None
