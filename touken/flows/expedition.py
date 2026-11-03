# -*- coding: utf-8 -*-
"""
上层业务：远征（派遣）

流程（用户亲授教材）：
  目录 → 远征 → 选时代卡（固定坐标，一行五个，左右可翻页）
  → OCR 点小图名字（小图卡一行四张，名字是文字，不玩固定坐标）
  → 点"部队选择"按钮 → 常规部队选择界面 → 选部队 tab
  → 【保命】重伤检查（游戏数据新鲜先信数据，缺失才看重伤章），有重伤绝不派遣
  → 点"远征开始" → 二次确认弹窗点"确认"（道具复选框默认不勾）
  → "出发远征"卷轴过场（不用跳过，几秒自己完）
  → 验证：小图卡上"远征时间"变成红色"远征中"

双服阶段 2（docs/dual-server-pipeline.md）：派遣已收编为官方流程
resource/base/flows/expedition.json（去远征 → 内置选图 → 内置安全派遣），
expedition_stream 只剩配置门、补花前置和填参数跑流程；选图/派遣两块活是
 ExpeditionMixin._expedition_pick_map_stream / _expedition_safe_dispatch_stream
（流程引擎 builtin 按契约调用，识别素材经素材包 @expedition/* 解析）。
验伤、游戏结果证明和记账是数据层的事，留在代码不进 DSL。

备注：
  - 时代卡/小图卡坐标已实测校准；OCR 点名字和按卡位坐标点两种选图方式都支持。
  - 收菜（collect_expedition_stream）支持"收完顺手再派"：读结算界面上的
    "第X部队"和"时代-小图"编号，把回来的队派回去；也可按资源目标
    （小判/加速符等）自动挑时薪最高的图，数据来自 data/expedition_maps.json。
    收取主循环在 expedition_collect.py（与服务器无关），国服画面适配与
    结算阅读在 expedition_collect_cn.py，本类组合两者。
  - 远征界面底部右侧的"部队选择"按钮用右下角 OCR 双词确认，避免和
    "即刻出阵"按钮的相似皮肤串台。
"""

import copy
import json
import re
import subprocess
import time
from pathlib import Path

from ..runtime_paths import STATUS_DIR

from ..device_adapter import roi_4to4
from ..flow_engine import FlowError, find_flow, normalize_flow, run_flow
from .expedition_collect import ExpeditionCollectMixin
from .expedition_collect_cn import CnExpeditionCollectViewMixin

_MAPS_TABLE = Path(__file__).parent.parent / "data" / "expedition_maps.json"
_STATUS_DIR = STATUS_DIR
_EXP_RECORD = _STATUS_DIR / "expeditions.json"

EXPEDITION_FLOW_ID = "expedition"

_NUMERALS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}


def _load_exp_record() -> dict:
    try:
        if _EXP_RECORD.exists():
            return json.loads(_EXP_RECORD.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_exp_record(rec: dict):
    try:
        _STATUS_DIR.mkdir(exist_ok=True)
        _EXP_RECORD.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass


def reconcile_records_with_game(device) -> list:
    """派遣记录与游戏现状对账，把游戏确认已回家的残留记录翻篇删档。

    玩家优先规矩（2026-10-14）：脚本自己记的旧账与游戏现实冲突时以
    游戏为准。收菜时数据通道不可用会留下残留记录，玩家手动收菜后该队
    其实早已在家，之后按 dispatched_at+duration_min 死等会白跳过几
    小时——这里对每条还没到期的记录用 /party/list 的部队状态对账，
    游戏明说 status=1（待命）就说明记录是残留，删掉落盘；游戏说还在
    外、数据缺失/过期一律不动（回退旧行为，绝不误删）。返回被翻篇的
    队号列表；对账任何一步翻车都不许炸主流程。
    """
    try:
        from ..game_state import available_events
        from ..expedition_state import (parse_expedition_records,
                                        reconcile_view_with_game)
        events = available_events(device)
        if not events:
            return []
        records = _load_exp_record()
        if not isinstance(records, dict):
            return []
        view = parse_expedition_records(records, time.time())
        cleared = reconcile_view_with_game(view, events)
        if cleared:
            for team_no in cleared:
                records.pop(str(team_no), None)
            _save_exp_record(records)
        return cleared
    except Exception:
        return []


def _map_meta(era, slot):
    """按时代+卡位查收益表，拿图名和时长"""
    try:
        with open(_MAPS_TABLE, encoding="utf-8") as f:
            maps = json.load(f)["maps"]
        for code, v in maps.items():
            if v.get("era") == era and v.get("slot") == slot:
                return code, v.get("name"), v.get("duration_min")
    except Exception:
        pass
    return None, None, None


def _map_slot(era, name):
    """Resolve a unique manual map name to the same calibrated map ID."""
    target = re.sub(r"[^一-鿿]", "", name or "")
    if len(target) < 2:
        return None
    try:
        maps = json.loads(_MAPS_TABLE.read_text(encoding="utf-8"))["maps"]
        matches = [row["slot"] for row in maps.values() if row.get("era") == era
                   and target in re.sub(r"[^一-鿿]", "", row.get("name") or "")]
        return matches[0] if len(matches) == 1 else None
    except (OSError, ValueError, KeyError):
        return None


class ExpeditionMixin(CnExpeditionCollectViewMixin, ExpeditionCollectMixin):
    """远征派遣。依赖宿主类的 navigate_to_stream、_click_point、_find_deploy_button。"""

    # 地图元数据走 self.* 供日服宿主覆盖：日服收益表/图名与国服不同，
    # 继承时覆写这三个（docs/dual-server-pipeline.md 阶段 2 共同契约），
    # 绝不让国服数据写进日服账本。
    _map_meta = staticmethod(_map_meta)
    _map_slot = staticmethod(_map_slot)

    def expedition_stream(self, era: int, map_name: str = None, team_no: int = 2,
                          map_slot: int = None, sakura_before_dispatch: bool = False,
                          repair_threshold: str = 'light'):
        """
        流式派遣一支部队去远征

        Args:
            era: 时代编号（1-5，对应当前页一行五张时代卡；更多时代以后加翻页）
            map_name: 小图名字（OCR 匹配，如 "鸟羽·伏见"，包含匹配不用写全）
            team_no: 部队编号（1-5）。注意别派正在远征/有重伤的部队
            map_slot: 小图卡位（1-4，从左到右）。给了就按坐标点，
                      不给就走 OCR 点名字——算法派图用卡位，人点图用名字

        Yields:
            str: 执行状态消息
        """
        cfg = self.config.get("expedition", {})
        if not cfg:
            yield "[远征] 未配置远征"
            return

        teams = self.config.get("team_select", {}).get("teams", {})
        if str(era) not in cfg.get("eras", {}):
            yield f"[远征] 配置里没有时代{era}的坐标"
            return
        if str(team_no) not in teams:
            yield f"[远征] 配置里没有部队{team_no}的坐标"
            return

        # ========== 1. 导航到远征 ==========
        yield "[远征] 正在导航到远征..."
        for nav_msg in self.navigate_to_stream("远征"):
            yield nav_msg
        if self.current_location != "远征":
            yield "[远征] 到达远征失败"
            return
        time.sleep(1.0)

        # ========== 2/3 前置门：小图卡位坐标 / 图名二选一（保持旧配置门）==========
        slots = cfg.get("map_slots", {})
        if map_slot is not None and str(map_slot) not in slots:
            yield f"[远征] 配置里没有小图卡位{map_slot}的坐标"
            return
        if map_slot is None and not map_name:
            yield "[远征] 既没给地图名字也没给卡位，不知道去哪，停"
            return

        # ========== 补花前置（pending 恢复 / 续派前练兵），语义不动 ==========
        from ..expedition_sakura import pending_restore, recover_stream, train_stream
        pending = pending_restore()
        if pending:
            if not (yield from recover_stream(self)):
                return
        if sakura_before_dispatch:
            if repair_threshold not in ('light', 'medium', 'heavy'):
                yield '[远征] ✗ 补花伤势条件无效，暂不派遣'
                return
            if not (yield from train_stream(self, team_no, repair_threshold)):
                return
        if pending or sakura_before_dispatch:
            yield from self.navigate_to_stream('远征')
            if self.current_location != '远征':
                yield '[远征] ✗ 补花后未回到远征页，暂不派遣'
                return

        # ========== 选图 + 安全派遣：官方流程单填入实际参数后交给流程引擎 ==========
        # 流程单本身是展品（资源目录只读），深拷贝一份填参，不动官方原件；
        # 选图/安全语义在 _expedition_pick_map_stream / _expedition_safe_dispatch_stream。
        flow = find_flow(EXPEDITION_FLOW_ID)
        if flow is None:
            yield ("[远征] ✗ 找不到官方远征流程"
                   "（resource/base/flows/expedition.json 缺失），暂不派遣")
            return
        plan = copy.deepcopy(flow)
        for step in plan["steps"]:
            if step.get("type") != "builtin":
                continue
            params = step.get("params") or {}
            if params.get("name") == "expedition_pick_map":
                params.update({"era": era, "map_slot": map_slot,
                               "map_name": map_name})
            elif params.get("name") == "expedition_safe_dispatch":
                params.update({"team_no": team_no, "era": era,
                               "map_slot": map_slot, "map_name": map_name})
        try:
            plan = normalize_flow(plan)
        except FlowError as exc:
            yield f"[远征] ✗ 官方远征流程校验翻车: {exc}"
            return
        yield from run_flow(self, plan, server='cn')

    def _expedition_pick_map_stream(self, era, map_slot=None, map_name=None,
                                    pack=None):
        """选时代卡 → 选小图（卡位盲点或 OCR 点名字）→ 全屏 OCR 对图名。

        从 expedition_stream 抽出（双服阶段 2，流程引擎 expedition_pick_map
        积木按契约调用）。时代卡/小图卡位是盲点坐标，点歪了会把队伍派去错误
        的图，而结果验证只认红字不认图，派错了也假绿——所以选完必须全屏 OCR
        对一遍图名，对不上重选一次，再不行就停。
        识别素材优先经素材包 @expedition/* 解析（pack=None 回退配置旧值）；
        地图元数据（图名回填）保持代码现状。前置：游戏已在远征页。
        返回 True/False。
        """
        cfg = (getattr(self, "config", None) or {}).get("expedition", {})
        if not cfg:
            yield "[远征] 未配置远征"
            return False
        if str(era) not in cfg.get("eras", {}):
            yield f"[远征] 配置里没有时代{era}的坐标"
            return False
        expect_name = map_name
        if not expect_name and map_slot is not None:
            _, expect_name, _ = self._map_meta(era, map_slot)
        # 卡位派图时 map_name 本来就没有，回填查到的图名，
        # 免得收工日志打出「None」
        map_name = map_name or expect_name
        slots = cfg.get("map_slots", {})
        if map_slot is not None and str(map_slot) not in slots:
            yield f"[远征] 配置里没有小图卡位{map_slot}的坐标"
            return False
        if map_slot is None and not map_name:
            yield "[远征] 既没给地图名字也没给卡位，不知道去哪，停"
            return False

        selected = False
        for attempt in range(2):
            if attempt:
                yield "[远征] 没确认选中目标图，重选一次"
            yield f"[远征] 🗺️ 翻去时代{era}..."
            self._click_point(cfg["eras"][str(era)])
            time.sleep(1.0)

            if map_slot is not None:
                yield f"[远征] 🗺️ 点小图卡位{map_slot}..."
                self._click_point(slots[str(map_slot)])
                time.sleep(1.0)
            else:
                yield f"[远征] 🔍 找小图「{map_name}」..."
                if pack is not None:
                    map_roi = roi_4to4(*pack.resolve_roi("@expedition/map-ocr"))
                else:
                    map_roi = roi_4to4(*cfg["map_ocr_roi"])
                map_pt = None
                for _ in range(6):
                    self.device.screenshot(force=True)
                    map_pt = self.device.ocr(expected=map_name, roi=map_roi)
                    if map_pt:
                        break
                    time.sleep(0.5)
                if not map_pt:
                    yield f"[远征] 没找到小图「{map_name}」（名字写错了？时代不对？要翻页？），停"
                    return False
                self.device.click(map_pt)
                time.sleep(1.0)

            if not expect_name or self._confirm_map_selected(expect_name):
                selected = True
                break
        if not selected:
            yield f"[远征] 两次都没确认选中「{expect_name}」，怕派错队伍，停"
            return False
        return True

    def _expedition_safe_dispatch_stream(self, team_no, era, map_slot=None,
                                         map_name=None, pack=None):
        """开部队选择 → 选部队 →【保命】重伤检查 → 远征开始 → 二次确认
        → 游戏响应确认 → 记账。

        从 expedition_stream 抽出（双服阶段 2，流程引擎
        expedition_safe_dispatch 积木按契约调用）。前置：选图已成功。
        识别素材优先经素材包 @expedition/* 解析（pack=None 回退配置旧值）；
        验伤/结果证明/记账是数据层的事留在代码（日服宿主覆盖这三处 CN 专属
        实现，绝不让国服数据写进日服账本）。返回 True/False。
        """
        cfg = (getattr(self, "config", None) or {}).get("expedition", {})
        if not cfg:
            yield "[远征] 未配置远征"
            return False
        teams = (getattr(self, "config", None) or {}).get(
            "team_select", {}).get("teams", {})
        if str(team_no) not in teams:
            yield f"[远征] 配置里没有部队{team_no}的坐标"
            return False
        expect_name = map_name
        if not expect_name and map_slot is not None:
            _, expect_name, _ = self._map_meta(era, map_slot)
        map_name = map_name or expect_name

        # ========== 4. 点"部队选择" → 等部队选择界面 ==========
        if pack is not None:
            team_expected = pack.resolve_ocr("@expedition/team-ui")
            team_roi = roi_4to4(*pack.resolve_roi("@expedition/team-ui"))
        else:
            ocr_cfg = cfg["team_ui_ocr"]
            team_expected = ocr_cfg["expected"]
            team_roi = roi_4to4(*ocr_cfg["roi"])
        team_ui_ok = False
        for attempt in range(12):
            self.device.screenshot(force=True)
            if self.device.ocr(expected=team_expected, roi=team_roi):
                team_ui_ok = True
                break
            # 前几次还没进就找"部队选择"按钮点（远征界面是按钮进入，不是直通）
            if attempt <= 3:
                deploy_ocr = (cfg.get("deploy_button", {}).get("ocr", {}) or {})
                deploy_words = deploy_ocr.get("expected", ["部队", "选择"])
                if pack is not None:
                    deploy_roi = pack.resolve_roi("@expedition/deploy")
                else:
                    deploy_roi = deploy_ocr.get("roi", [1110, 580, 1279, 710])
                deploy = self._find_deploy_button(
                    {"deploy_button": {"ocr": {"expected": deploy_words,
                                               "roi": list(deploy_roi)}}})
                if deploy:
                    self.device.click(deploy)
                    time.sleep(1.5)
            time.sleep(0.5)
        if not team_ui_ok:
            yield "[远征] 部队选择界面没打开（条件不满足按钮可能是灰的？），停"
            return False

        # ========== 5. 选部队 ==========
        self._click_point(teams[str(team_no)])
        time.sleep(0.3)
        self._click_point(teams[str(team_no)])
        time.sleep(0.5)

        # ========== 6. 【保命】重伤检查 ==========
        # 游戏数据新鲜就先信数据（全队血量一个不缺才算数，缺一员都算「不知道」）；
        # 数据缺失、过旧才回退重伤章模板。宁停不放，两条路判出重伤都拒派。
        if pack is not None:
            stamp_template = pack.resolve_template("@expedition/injury-stamp")
        else:
            stamp_template = cfg["injury_stamp"]["template"]
        log_injury, detail = self._dispatch_injury_report(team_no, {})
        if detail is not None:
            yield f"[远征] 📋 日志验伤：{detail}"
            if log_injury == "重伤":
                yield "[远征] 🛑 游戏数据验出重伤！按规矩绝不派遣，停。去修刀吧"
                return False
        else:
            self.device.screenshot(force=True)
            if self.device.template_match(stamp_template):
                yield "[远征] 🛑 检测到重伤标记！按规矩绝不派遣，停。去修刀吧"
                return False

        # ========== 7. 点"远征开始" ==========
        if pack is not None:
            start_template = pack.resolve_template("@expedition/start")
            start_expected = pack.resolve_ocr("@expedition/start")
            start_roi = roi_4to4(*pack.resolve_roi("@expedition/start"))
        else:
            start_template = cfg["start_button"]["template"]
            start_expected = cfg["start_ocr"]["expected"]
            start_roi = roi_4to4(*cfg["start_ocr"]["roi"])
        start = self.device.template_match(start_template)
        if not start:
            start = self.device.ocr(expected=start_expected, roi=start_roi,
                                    match_mode="exact")
        if not start:
            yield ("[远征] 找不到可用的远征开始按钮；这支部队可能不满足该图条件，"
                   "或仍在远征，停止派遣")
            return False
        self.device.click(start)
        time.sleep(1.5)

        # ========== 8. 二次确认弹窗：点"确认"（道具复选框不碰，默认不用） ==========
        if pack is not None:
            confirm_expected = pack.resolve_ocr("@expedition/confirm")
            confirm_roi = roi_4to4(*pack.resolve_roi("@expedition/confirm"))
        else:
            confirm_ocr = cfg["confirm_ocr"]
            confirm_expected = confirm_ocr["expected"]
            confirm_roi = roi_4to4(*confirm_ocr["roi"])
        confirm = None
        for _ in range(8):
            self.device.screenshot(force=True)
            confirm = self.device.ocr(expected=confirm_expected, roi=confirm_roi)
            if confirm:
                break
            time.sleep(0.5)
        if not confirm:
            yield "[远征] 确认弹窗没出现，停，你去看看卡哪了"
            return False
        confirmed_at = int(time.time())
        self.device.click(confirm)
        yield "[远征] 🐎 确认完毕，出发远征！过场动画跑着..."

        # ========== 9. 游戏响应确认结果，缺数据才看画面 ==========
        time.sleep(1.0)
        selected_slot = (map_slot if map_slot is not None
                       else self._map_slot(era, expect_name))
        proof = self._dispatch_game_result(team_no, era, selected_slot, confirmed_at)
        if proof is False:
            yield "[远征] ✗ 游戏未确认本次部队和地图派遣成功，停止派遣"
            return False
        if proof is not None:
            yield f"[远征] ✅ 部队{team_no}已出发：时代{era}「{map_name}」，收工"
            self._record_dispatch(team_no, era, selected_slot, map_name)
            return True
        if pack is not None:
            running_expected = pack.resolve_ocr("@expedition/running")
            running_roi = roi_4to4(*pack.resolve_roi("@expedition/running"))
        else:
            running_ocr = cfg["running_ocr"]
            running_expected = running_ocr["expected"]
            running_roi = roi_4to4(*running_ocr["roi"])
        for _ in range(20):  # 过场几秒，给足 10 秒
            time.sleep(0.5)
            self.device.screenshot(force=True)
            if self.device.ocr(expected=running_expected, roi=running_roi):
                yield f"[远征] ✅ 部队{team_no}已出发：时代{era}「{map_name}」，收工"
                self._record_dispatch(team_no, era, selected_slot, map_name)
                return True

        # 过场后可能回了本丸也可能还在目的地界面，找不到"远征中"就截个图让人看
        # （这条提醒在翻车词表白名单里：派遣已点确认，结果不确定 ≠ 翻车，
        #   保持旧语义不记红——但也没记账，玩家优先，去看一眼）
        yield "[远征] ⚠️ 没看到「远征中」字样，可能派遣失败也可能已回本丸，你去看一眼"
        return True

    def _dispatch_game_result(self, team_no, era, slot, after):
        if slot is None or not getattr(self.device, "adb_address", None):
            return None
        from ..game_state import expedition_started, read_events
        try:
            events = read_events(self.device.adb_path, self.device.adb_address)
            return expedition_started(events, team_no, (era - 1) * 4 + slot, after)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return None

    def _dispatch_injury_report(self, team_no: int, cache: dict):
        """派遣前 JSON 验伤（照出阵链的规矩）：游戏血量新鲜且全队一个不缺才作准，
        返回 (最高伤势, 人话明细)；拉不到、数据过旧、成员缺数据一律返回
        (None, None)——「不知道」就回退重伤章模板，绝不拿半残数据放行。

        cache 在一次派遣链内复用同一批事件，免去重复拉取；原始日志解析完即焚。
        """
        cfg = {}
        if isinstance(getattr(self, "config", None), dict):
            cfg = self.config.get("injury_check", {}) or {}
        if not cfg.get("use_youzu_log", True):
            return None, None
        if not getattr(self.device, "adb_address", None):
            return None, None
        try:
            if "events" not in cache:
                from ..game_state import read_events
                cache["events"] = read_events(self.device.adb_path,
                                              self.device.adb_address)
            from ..game_state import injury_report
            report = injury_report(cache["events"], team_no,
                                   max_age=float(cfg.get("max_age_sec", 600)))
        except Exception:
            return None, None
        if not report:
            return None, None
        hurt = [m for m in report["members"] if m["injury"]]
        detail = ("、".join(f"{m['label'] or m['name']}{m['injury']}"
                            f"(hp{m['hp']}/{m['hp_max']})" for m in hurt)
                  if hurt else "全员满血")
        return report["max_injury"], detail

    def _record_dispatch(self, team_no, era, slot, map_name):
        """Countdown is a display record; failure to save does not undo departure."""
        code, name, dur = _map_meta(era, slot)
        rec = _load_exp_record()
        rec[str(team_no)] = {
            "map_code": code, "map_name": map_name or name,
            "era": era, "slot": slot, "duration_min": dur,
            "dispatched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        _save_exp_record(rec)
        if hasattr(self, "record_event"):
            self.record_event("expedition.dispatched", team_no=team_no,
                              map_code=code, map_name=map_name or name,
                              era=era, slot=slot)

    # ==================== 收菜辅助 ====================

    def _confirm_map_selected(self, expect_name: str) -> bool:
        """全屏 OCR 确认目标图名在画面上（选中后详情面板和卡列表都有名字）。
        只比汉字、双向包含，防 OCR 吃掉「·」或详略名差异。"""
        target = re.sub(r"[^一-鿿]", "", expect_name or "")
        if len(target) < 2:
            return True  # 名字太短没法对，别挡路
        self.device.screenshot(force=True)
        try:
            tokens = self.device.ocr_all(roi_4to4(0, 0, 1280, 720)) or []
        except Exception:
            return False
        for text, _point in tokens:
            t = re.sub(r"[^一-鿿]", "", str(text))
            if len(t) >= 2 and (t in target or target in t):
                return True
        return False

    # ==================== 结算观察哨（收菜/扫地/导航共用） ====================

