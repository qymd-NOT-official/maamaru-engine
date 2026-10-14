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

备注：
  - 时代卡/小图卡坐标已实测校准；OCR 点名字和按卡位坐标点两种选图方式都支持。
  - 收菜（collect_expedition_stream）支持"收完顺手再派"：读结算界面上的
    "第X部队"和"时代-小图"编号，把回来的队派回去；也可按资源目标
    （小判/加速符等）自动挑时薪最高的图，数据来自 data/expedition_maps.json。
  - 远征界面底部右侧的"部队选择"按钮用右下角 OCR 双词确认，避免和
    "即刻出阵"按钮的相似皮肤串台。
"""

import json
import re
import subprocess
import time
from uuid import uuid4
from pathlib import Path

import numpy as np

from ..runtime_paths import STATUS_DIR

from ..maa_adapter import roi_4to4

_MAPS_TABLE = Path(__file__).parent.parent / "data" / "expedition_maps.json"
_STATUS_DIR = STATUS_DIR
_EXP_RECORD = _STATUS_DIR / "expeditions.json"

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


def reconcile_records_with_game(maa) -> list:
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
        events = available_events(maa)
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


class ExpeditionMixin:
    """远征派遣。依赖宿主类的 navigate_to_stream、_click_point。"""

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

        # ========== 2/3. 选时代卡 → 选小图（点完必须看到图名才算数） ==========
        # 时代卡和小图卡位都是盲点坐标；点歪了会把队伍派去错误的图，
        # 而第 9 步的"远征中"验证只认红字不认图，派错了也假绿。
        # 所以选完必须全屏 OCR 对一遍图名，对不上重选一次，再不行就停。
        expect_name = map_name
        if not expect_name and map_slot is not None:
            _, expect_name, _ = _map_meta(era, map_slot)
        slots = cfg.get("map_slots", {})
        if map_slot is not None and str(map_slot) not in slots:
            yield f"[远征] 配置里没有小图卡位{map_slot}的坐标"
            return
        if map_slot is None and not map_name:
            yield "[远征] 既没给地图名字也没给卡位，不知道去哪，停"
            return

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
                map_roi = roi_4to4(*cfg["map_ocr_roi"])
                map_pt = None
                for _ in range(6):
                    self.maa.screenshot(force=True)
                    map_pt = self.maa.ocr(expected=map_name, roi=map_roi)
                    if map_pt:
                        break
                    time.sleep(0.5)
                if not map_pt:
                    yield f"[远征] 没找到小图「{map_name}」（名字写错了？时代不对？要翻页？），停"
                    return
                self.maa.click(map_pt)
                time.sleep(1.0)

            if not expect_name or self._confirm_map_selected(expect_name):
                selected = True
                break
        if not selected:
            yield f"[远征] 两次都没确认选中「{expect_name}」，怕派错队伍，停"
            return

        # ========== 4. 点"部队选择" → 等部队选择界面 ==========
        ocr_cfg = cfg["team_ui_ocr"]
        team_roi = roi_4to4(*ocr_cfg["roi"])
        team_ui_ok = False
        for attempt in range(12):
            self.maa.screenshot(force=True)
            if self.maa.ocr(expected=ocr_cfg["expected"], roi=team_roi):
                team_ui_ok = True
                break
            # 前两次还没进就找"部队选择"按钮点（远征界面是按钮进入，不是直通）
            if attempt <= 3:
                deploy = self._find_deploy_button(cfg)
                if deploy:
                    self.maa.click(deploy)
                    time.sleep(1.5)
            time.sleep(0.5)
        if not team_ui_ok:
            yield "[远征] 部队选择界面没打开（条件不满足按钮可能是灰的？），停"
            return

        # ========== 5. 选部队 ==========
        self._click_point(teams[str(team_no)])
        time.sleep(0.3)
        self._click_point(teams[str(team_no)])
        time.sleep(0.5)

        # ========== 6. 【保命】重伤检查 ==========
        # 游戏数据新鲜就先信数据（全队血量一个不缺才算数，缺一员都算「不知道」）；
        # 数据缺失、过旧才回退重伤章模板。宁停不放，两条路判出重伤都拒派。
        log_injury, detail = self._dispatch_injury_report(team_no, {})
        if detail is not None:
            yield f"[远征] 📋 日志验伤：{detail}"
            if log_injury == "重伤":
                yield "[远征] 🛑 游戏数据验出重伤！按规矩绝不派遣，停。去修刀吧"
                return
        else:
            self.maa.screenshot(force=True)
            if self.maa.template_match(cfg["injury_stamp"]["template"]):
                yield "[远征] 🛑 检测到重伤标记！按规矩绝不派遣，停。去修刀吧"
                return

        # ========== 7. 点"远征开始" ==========
        start = self.maa.template_match(cfg["start_button"]["template"])
        if not start:
            start_ocr = cfg["start_ocr"]
            start = self.maa.ocr(expected=start_ocr["expected"],
                                 roi=roi_4to4(*start_ocr["roi"]),
                                 match_mode="exact")
        if not start:
            yield ("[远征] 找不到可用的远征开始按钮；这支部队可能不满足该图条件，"
                   "或仍在远征，停止派遣")
            return
        self.maa.click(start)
        time.sleep(1.5)

        # ========== 8. 二次确认弹窗：点"确认"（道具复选框不碰，默认不用） ==========
        confirm_ocr = cfg["confirm_ocr"]
        confirm = None
        for _ in range(8):
            self.maa.screenshot(force=True)
            confirm = self.maa.ocr(expected=confirm_ocr["expected"],
                                   roi=roi_4to4(*confirm_ocr["roi"]))
            if confirm:
                break
            time.sleep(0.5)
        if not confirm:
            yield "[远征] 确认弹窗没出现，停，你去看看卡哪了"
            return
        confirmed_at = int(time.time())
        self.maa.click(confirm)
        yield "[远征] 🐎 确认完毕，出发远征！过场动画跑着..."

        # ========== 9. 游戏响应确认结果，缺数据才看画面 ==========
        time.sleep(1.0)
        selected_slot = map_slot if map_slot is not None else _map_slot(era, expect_name)
        proof = self._dispatch_game_result(team_no, era, selected_slot, confirmed_at)
        if proof is False:
            yield "[远征] ✗ 游戏未确认本次部队和地图派遣成功，停止派遣"
            return
        if proof is not None:
            yield f"[远征] ✅ 部队{team_no}已出发：时代{era}「{map_name}」，收工"
            self._record_dispatch(team_no, era, selected_slot, map_name)
            return
        running_ocr = cfg["running_ocr"]
        running_roi = roi_4to4(*running_ocr["roi"])
        for _ in range(20):  # 过场几秒，给足 10 秒
            time.sleep(0.5)
            self.maa.screenshot(force=True)
            if self.maa.ocr(expected=running_ocr["expected"], roi=running_roi):
                yield f"[远征] ✅ 部队{team_no}已出发：时代{era}「{map_name}」，收工"
                self._record_dispatch(team_no, era, selected_slot, map_name)
                return

        # 过场后可能回了本丸也可能还在目的地界面，找不到"远征中"就截个图让人看
        yield "[远征] ⚠️ 没看到「远征中」字样，可能派遣失败也可能已回本丸，你去看一眼"
        return

    def _dispatch_game_result(self, team_no, era, slot, after):
        if slot is None or not getattr(self.maa, "adb_address", None):
            return None
        from ..game_state import expedition_started, read_events
        try:
            events = read_events(self.maa.adb_path, self.maa.adb_address)
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
        if not getattr(self.maa, "adb_address", None):
            return None, None
        try:
            if "events" not in cache:
                from ..game_state import read_events
                cache["events"] = read_events(self.maa.adb_path,
                                              self.maa.adb_address)
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

    def collect_expedition_stream(self, redispatch=None):
        """
        流式收菜：领取远征完成的奖励

        原理（用户亲授）：远征完成后，【重新进入本丸场景】会自动播放
        「远征归来」横幅 → 点一下进「远征结果」结算 → 再点翻页，
        多支队伍同时回来就多张结算连翻，翻完回到本丸。

        所以流程：远征界面 → 回本丸（强制换场景触发动画）→ 循环
        识别横幅/结算并点过，直到确认站在本丸且无事发生。

        Args:
            redispatch: 收完顺手再派的模式
                None   = 只收不派（默认）
                "same" = 哪支队回来就把哪支队派回原来那张图
                资源名  = "小判"/"加速符"/"委托符"/"木炭"/"玉钢"/"冷却材"/"砥石"
                          回来的队全部派去该资源时薪最高的图
                          （收益表 data/expedition_maps.json）

        Yields:
            str: 执行状态消息
        """
        cfg = self.config.get("expedition", {})
        if not cfg:
            yield "[收菜] 未配置远征"
            return

        banner_ocr = cfg["return_banner_ocr"]
        banner_roi = roi_4to4(*banner_ocr["roi"])
        title_ocr = cfg["result_title_ocr"]
        title_roi = roi_4to4(*title_ocr["roi"])
        tap = cfg["collect_tap"]
        home_tpl = cfg["home_ui"]["template"]

        baseline = self._collection_facts(0) or []
        excluded = {receipt["payload"]["receipt_key"] for receipt in baseline}
        started_at = int(time.time())
        screen_unresolved = False

        # ========== 1. 换场景回本丸，触发归来动画 ==========
        yield "[收菜] 🧺 先绕去远征界面再回本丸，勾一下归来动画..."
        for nav_msg in self.navigate_to_stream("远征"):
            yield nav_msg
        if self.current_location != "远征":
            yield "[收菜] 没能到达远征界面，停"
            return
        for nav_msg in self.navigate_to_stream("本丸"):
            yield nav_msg
        if self.current_location != "本丸":
            yield "[收菜] 没能回到本丸，停"
            return

        # ========== 2. 收菜循环 ==========
        collected = 0        # 点掉的结算界面的数量
        settlements = []     # 每份结算的归来信息（第几部队、哪张图）
        home_streak = 0      # 连续看到本丸的次数
        unknown_streak = 0   # 连续认不出画面的次数
        settle_seen = []     # 本轮已记账结算屏的像素指纹（同一屏没翻动不重复记）
        time.sleep(2.0)      # 等归来动画冒出来

        for _ in range(30):  # 安全上限
            self.maa.screenshot(force=True)

            # 结算界面 → 观察哨读账记账（认不出记 unknown），再点一下翻页
            obs = None
            if self.maa.ocr(expected=title_ocr["expected"], roi=title_roi):
                obs = self.observe_expedition_settlement(
                    via="collect", seen=settle_seen, sequence=collected + 1, record=False)
            if obs is not None:
                if not obs["new"]:
                    # 同一屏没翻动（转场慢半拍），再点一下，不重复记账
                    self._click_point(tap)
                    time.sleep(1.2)
                    continue
                collected += 1
                settlements.append(obs)
                yield f"[收菜] 已翻过第{collected}份远征结算"
                self._click_point(tap)
                home_streak = 0
                unknown_streak = 0
                time.sleep(1.2)
                continue

            # 归来横幅 → 点一下进结算
            if self.maa.ocr(expected=banner_ocr["expected"], roi=banner_roi):
                yield "[收菜] 远征归来横幅，点过"
                self._click_point(tap)
                home_streak = 0
                unknown_streak = 0
                time.sleep(1.2)
                continue

            # 确认站在本丸：连续两次看到且无横幅/结算，收工
            if self.maa.template_match(home_tpl):
                home_streak += 1
                if home_streak >= 2:
                    break
                time.sleep(1.0)
                continue

            # 认不出的画面（过场动画途中等）→ 点一下安全区帮忙快进
            home_streak = 0
            unknown_streak += 1
            if unknown_streak > 5:
                screen_unresolved = True
                break
            self._click_point(tap)
            time.sleep(1.0)
        else:
            screen_unresolved = True

        receipts = self._collection_facts(started_at, excluded)
        if receipts:
            settlements = [{**receipt["payload"], "map_name": None} for receipt in receipts]
            collected = len(settlements)
            self._forget_collected_records(receipts)
            yield f"[收菜] 游戏已确认 {collected} 份结算的部队、地图和奖励"
            if getattr(self, "_collection_sync_failed", False):
                yield "[收菜] 账房同步未完成，可稍后同步近况；本次结算已确认"
        else:
            for sequence, obs in enumerate(settlements, 1):
                self._record_visual_settlement(obs, sequence)
        if screen_unresolved:
            yield "[收菜] ⚠️ 当前页面未确认回到本丸，结算记录已保留，暂不再派"
            return

        if collected > 0:
            yield f"[收菜] ✅ 收了 {collected} 份远征奖励"
        else:
            yield "[收菜] ✓ 没有远征回来，本丸风平浪静～"
            return

        # ========== 3. 顺手再派 ==========
        if not redispatch or not settlements:
            return

        if redispatch != "same":
            pick = self._pick_map_for_goal(redispatch)
            if not pick:
                yield f"[收菜] 收益表里没有产「{redispatch}」的图，再派跳过"
                return
            goal_code, goal_era, goal_slot = pick
            yield f"[收菜] 目标「{redispatch}」时薪最高的图：{goal_code}"

        dispatched = set()
        for info in settlements:
            team = info["team_no"]
            if not team or team in dispatched:
                continue
            dispatched.add(team)

            if redispatch == "same":
                era, slot, name = info["era"], info["slot"], info["map_name"]
                if not era or (not slot and not name):
                    yield f"[收菜] 部队{team}的归来信息没读全，这队跳过"
                    continue
                yield f"[收菜] 顺手把部队{team}派回原图（时代{era}卡位{slot}）..."
            else:
                era, slot, name = goal_era, goal_slot, None
                yield f"[收菜] 把部队{team}派去 {goal_code}（时代{era}卡位{slot}）..."

            for dis_msg in self.expedition_stream(era=era, map_name=name,
                                                  team_no=team, map_slot=slot):
                yield dis_msg

        yield f"[收菜] 再派完毕，共处理 {len(dispatched)} 支队"
        return

    def _collection_facts(self, after, exclude=()):
        if not getattr(self.maa, "adb_address", None):
            return None
        from ..game_state import read_events, expedition_settlements
        from ..record_sync import consume_game_records
        try:
            events = read_events(self.maa.adb_path, self.maa.adb_address)
            facts = expedition_settlements(events, after, exclude)
            try:
                consume_game_records(events)
                if after > 0:
                    from ..expedition_receipts import link_execution
                    from ..telemetry import get_telemetry_store
                    link_execution(facts, get_telemetry_store())
                self._collection_sync_failed = False
            except Exception:
                self._collection_sync_failed = True
            return facts
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _forget_collected_records(receipts):
        from ..youzu_log import _event_epoch
        records = _load_exp_record()
        if not isinstance(records, dict):
            return
        for receipt in receipts:
            team = str(receipt["payload"]["team_no"])
            record = records.get(team)
            if not isinstance(record, dict):
                continue
            stamp = record.get("dispatched_at")
            if not isinstance(stamp, str):
                continue
            started = _event_epoch({"ts": stamp})
            if started is not None and receipt["ts"] > started:
                records.pop(team, None)
        _save_exp_record(records)

    # ==================== 收菜辅助 ====================

    def _confirm_map_selected(self, expect_name: str) -> bool:
        """全屏 OCR 确认目标图名在画面上（选中后详情面板和卡列表都有名字）。
        只比汉字、双向包含，防 OCR 吃掉「·」或详略名差异。"""
        target = re.sub(r"[^一-鿿]", "", expect_name or "")
        if len(target) < 2:
            return True  # 名字太短没法对，别挡路
        self.maa.screenshot(force=True)
        try:
            tokens = self.maa.ocr_all(roi_4to4(0, 0, 1280, 720)) or []
        except Exception:
            return False
        for text, _point in tokens:
            t = re.sub(r"[^一-鿿]", "", str(text))
            if len(t) >= 2 and (t in target or target in t):
                return True
        return False

    # ==================== 结算观察哨（收菜/扫地/导航共用） ====================

    def observe_expedition_settlement(self, via: str, seen: list = None,
                                      sequence: int = None, record: bool = True) -> dict | None:
        """
        远征结算屏观察哨。当前画面是「远征结果」结算屏时，把能读的事实全部
        读下来并照实记账，返回观察记录；不是结算屏返回 None。

        诚实契约：认不清的一律记 unknown——结果字样读不出不会默认成「成功」，
        资源行读不出不会默认成「没给」，道具栏有内容认不出不会默认成「空」。
        记账另有仲裁：同一次结算有新鲜 JSON 收据时账本以收据为准，画面数字
        只留旁证日志（见 _record_visual_settlement）。

        只观察、不点击——翻页/跳过永远是调用方的动作。
        seen 是调用方（一轮收菜/扫地/导航）持有的像素指纹列表：同一屏没翻动
        时凭指纹只记一次账；不同队伍的结算「第X部队」字样必然不同，OCR 全灭
        也分得开屏。via 写进事件 payload 区分观察来源（collect/popup_sweep/
        open_menu）。
        """
        cfg = self.config.get("expedition", {})
        title_ocr = cfg.get("result_title_ocr")
        if not title_ocr or not cfg.get("settlement_team_roi"):
            return None

        title_roi = roi_4to4(*title_ocr["roi"])
        img = self.maa.screenshot(force=True)
        if img is None or not self.maa.ocr(expected=title_ocr["expected"],
                                           roi=title_roi):
            return None
        # 转场半途保护：标题连看两帧都在才读账，防止读进翻页动画里
        time.sleep(0.4)
        img = self.maa.screenshot(force=True)
        if img is None or not self.maa.ocr(expected=title_ocr["expected"],
                                           roi=title_roi):
            return None

        fingerprint = self._settlement_fingerprint(cfg, img)
        if fingerprint and seen is not None:
            for old in seen:
                if self._fingerprint_same(old, fingerprint):
                    return {"new": False}

        info = self._read_settlement_info(cfg)
        rewards, rewards_status, result = self._read_settlement_rewards(cfg)
        special, special_status, special_ink = self._read_special_rewards(cfg, img)
        # 道具栏认不出：留一帧同源运行截图供日后校准（攒样本，不硬识别）。
        # 在去重判定之后，每个唯一结算屏至多留一张；判空/认出都不留。
        sample_saved = False
        if special_status == "unknown":
            sample_saved = self._save_special_unknown_sample(via, sequence)
        obs = {"new": True, "via": via, "result": result,
               "rewards": rewards, "rewards_status": rewards_status,
               "special_rewards": special, "special_status": special_status,
               "special_sample_saved": sample_saved,
               **info}
        if special_ink is not None:
            obs["special_ink"] = special_ink

        if record:
            self._record_visual_settlement(obs, sequence)
        if fingerprint and seen is not None:
            seen.append(fingerprint)
        return obs

    def _record_visual_settlement(self, obs, sequence):
        # 记账仲裁：同一次结算有新鲜 JSON 收据时，账本以收据为准（收据由数据
        # 同步落账，按 receipt_key 幂等不会重复），画面 OCR 逐项对一遍、只留
        # 旁证日志，不重复记账；收据缺失才走视觉记账兜底。
        receipt = self._settlement_receipt_for(obs)
        if receipt is not None:
            self._log_settlement_corroboration(obs, receipt, sequence)
        elif hasattr(self, "record_event"):
            payload = {k: v for k, v in obs.items() if k != "new"}
            payload["sequence"] = sequence
            self.record_event("expedition.settled", **payload)
            note = (obs["header"] or obs["map_name"]
                    or f"第{sequence}份远征结算")
            for resource, amount in obs["rewards"].items():
                self.record_event("resource.change", resource=resource,
                                  delta=amount, source="expedition.settlement",
                                  attribution="confirmed",
                                  evidence="settlement_ocr", note=note,
                                  settlement_sequence=sequence,
                                  team_no=obs["team_no"], result=obs["result"],
                                  via=obs["via"])
            for entry in obs["special_rewards"]:
                self.record_event("resource.change", resource=entry["name"],
                                  delta=entry["amount"],
                                  source="expedition.settlement",
                                  attribution="confirmed",
                                  evidence="settlement_special_ocr", note=note,
                                  settlement_sequence=sequence,
                                  team_no=obs["team_no"], result=obs["result"],
                                  via=obs["via"])
        # 这支队回来了，派遣记录销掉（看板倒计时用）
        if obs["team_no"]:
            try:
                rec = _load_exp_record()
                if rec.pop(str(obs["team_no"]), None) is not None:
                    _save_exp_record(rec)
            except Exception:
                pass

    def _settlement_receipt_for(self, obs):
        """给这份画面观察找对应的 JSON 结算收据：同部队、同图（画面读得出
        时代卡位就比，读不出只认部队）、且 10 分钟内——同一队会反复去同一张
        图，只认刚回来的那份，旧账不作数。数据通道不在或拉取失败返回 None，
        调用方回退视觉记账。
        """
        if not getattr(self.maa, "adb_address", None):
            return None
        from ..game_state import read_events, expedition_settlements
        try:
            events = read_events(self.maa.adb_path, self.maa.adb_address)
            receipts = expedition_settlements(events, 0)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return None
        now = time.time()
        best = None
        for receipt in receipts:
            payload = receipt["payload"]
            if ((obs.get("team_no") and payload["team_no"] != obs["team_no"])
                    or (obs.get("era") and payload["era"] != obs["era"])
                    or (obs.get("slot") and payload["slot"] != obs["slot"])):
                continue
            if not 0 <= now - receipt["ts"] <= 600:
                continue
            if best is None or receipt["ts"] > best["ts"]:
                best = receipt
        return best

    @staticmethod
    def _log_settlement_corroboration(obs, receipt, sequence):
        """JSON 收据在账：OCR 数字与收据逐项对一遍，结果只写日志不碰账本。"""
        payload = receipt["payload"]
        json_rewards = payload.get("rewards") or {}
        diffs = []
        for resource, amount in (obs.get("rewards") or {}).items():
            if json_rewards.get(resource) != amount:
                diffs.append(f"{resource}：画面读 {amount}、数据 {json_rewards.get(resource)}")
        for entry in obs.get("special_rewards") or []:
            if json_rewards.get(entry["name"]) != entry["amount"]:
                diffs.append(
                    f"{entry['name']}：画面读 {entry['amount']}、"
                    f"数据 {json_rewards.get(entry['name'])}")
        note = f"部队{payload['team_no']} {payload.get('map_code') or ''}".strip()
        if sequence:
            note += f"（第{sequence}份）"
        if diffs:
            print(f"[远征] ⚠️ {note} 结算画面与游戏数据对不上："
                  f"{'；'.join(diffs)}。账本以游戏数据为准，画面数字仅留作旁证")
        else:
            print(f"[远征] 📎 {note} 结算画面与游戏数据一致，账本以游戏数据为准")

    @staticmethod
    def _settlement_fingerprint(cfg, img):
        """结算屏像素指纹：「第X部队」标签区 + 各行资源数字区。
        静止画面跨帧逐像素一致（2026-09 实测同屏三帧 meanabs=0.0）；
        不同队伍回来「第X部队」字样必然不同，OCR 全灭也分得开屏。"""
        regions = [cfg.get("settlement_team_roi")]
        regions.extend((cfg.get("settlement_rewards", {})
                        .get("resource_rois", {})).values())
        crops = []
        h, w = img.shape[:2]
        for r in regions:
            if not r or len(r) != 4:
                continue
            x1, y1, x2, y2 = (int(v) for v in r)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 <= x1 or y2 <= y1:
                continue
            crops.append(np.ascontiguousarray(img[y1:y2, x1:x2]))
        return tuple(crops) or None

    @staticmethod
    def _fingerprint_same(a, b) -> bool:
        if len(a) != len(b):
            return False
        for ca, cb in zip(a, b):
            if ca.shape != cb.shape:
                return False
            diff = np.abs(ca.astype(np.int16) - cb.astype(np.int16)).mean()
            if float(diff) > 0.5:  # 同屏实测 0.0；换一个字远不止这个数
                return False
        return True

    def _read_settlement_rewards(self, cfg) -> tuple[dict, dict, str]:
        """
        读结算页四行基础资源 + 结果字样。
        返回 (rewards, rewards_status, result)：
          rewards 只放 OCR 确认的正值；rewards_status 逐行 ok/zero/unknown
          （一行没读清只影响该行，不猜数）；result ∈ 成功/大成功/失败/unknown，
          读不清就是 unknown，绝不默认成成功。
        """
        reward_cfg = cfg.get("settlement_rewards", {})
        resources = ("木炭", "玉钢", "冷却材", "砥石")
        fallback_rois = (
            [875, 470, 920, 525], [875, 520, 920, 575],
            [875, 570, 920, 625], [875, 620, 920, 680],
        )
        rewards, status = {}, {}
        ocr_all = getattr(self.maa, "ocr_all", None)
        if callable(ocr_all):
            for resource, fallback in zip(resources, fallback_rois):
                roi = reward_cfg.get("resource_rois", {}).get(resource, fallback)
                try:
                    rows = ocr_all(roi_4to4(*roi)) or []
                except Exception:
                    status[resource] = "unknown"
                    continue
                ordered = sorted(rows, key=lambda row: getattr(row[1], "x", 0))
                digits = "".join(re.sub(r"\D", "", str(text))
                                 for text, _point in ordered)
                if not digits:
                    status[resource] = "unknown"
                elif int(digits) > 0:
                    rewards[resource] = int(digits)
                    status[resource] = "ok"
                else:
                    status[resource] = "zero"

        result = "unknown"
        result_cfg = reward_cfg.get("result", {})
        if callable(ocr_all):
            try:
                rows = ocr_all(roi_4to4(*result_cfg.get(
                    "roi", [680, 35, 940, 150]))) or []
                result_text = "".join(str(text) for text, _point in rows)
                if "大成功" in result_text:
                    result = "大成功"
                elif "失败" in result_text:
                    result = "失败"
                elif "成功" in result_text:
                    result = "成功"
            except Exception:
                pass
        return rewards, status, result

    def _read_special_rewards(self, cfg, img) -> tuple[list, str, float | None]:
        """
        读「获得道具」栏（小判/委托符/加速符等特殊奖励）。
        返回 (entries, status, ink)：
          entries = [{"name": 资源名, "amount": 数量}]——图标模板命中 + 同行
                    数量读出才算（templates 待真机道具栏取帧校准，未配则为空）
          status: none（栏体墨水低于阈值，判空栏）/ ok（栏里内容全部认出）/
                  unknown（栏里有内容但认不出，或读数失败——不猜）
          ink: 栏体墨水占比实测值，写进事件供日后校准阈值和模板
        """
        special_cfg = cfg.get("settlement_rewards", {}).get("special_items", {})
        col = special_cfg.get("column_roi")
        if not col or img is None:
            return [], "unknown", None
        try:
            x1, y1, x2, y2 = (int(v) for v in col)
            gray = img[y1:y2, x1:x2].astype(np.float32).mean(axis=2)
            ink = float((gray < 100).mean())
        except Exception:
            return [], "unknown", None
        ink = round(ink, 4)

        entries = []
        col_roi = roi_4to4(*[int(v) for v in col])
        for resource, tpl in special_cfg.get("templates", {}).items():
            try:
                pt = self.maa.template_match(tpl, roi=col_roi)
            except Exception:
                pt = None
            if not pt:
                continue
            amount = self._read_special_amount(col_roi, pt)
            if amount is None:
                return entries, "unknown", ink
            entries.append({"name": resource, "amount": amount})
        if entries:
            return entries, "ok", ink
        if ink <= special_cfg.get("empty_ink_max", 0.16):
            return [], "none", ink
        return [], "unknown", ink

    def _save_special_unknown_sample(self, via: str, sequence) -> bool:
        """道具栏认不出时留一帧同源运行截图供校准（DEBUG_DIR/expedition/）。

        用 save_screenshot(force=False) 存当前缓存帧（就是读账用的那张稳定帧），
        不另走 ADB 截图；失败只记日志，不阻塞收菜和事实落库。
        """
        save = getattr(self.maa, "save_screenshot", None)
        if not callable(save):
            return False
        try:
            from ..runtime_paths import DEBUG_DIR
            # 系统时钟可能连续返回相同值，独立标识避免不同帧互相覆盖。
            now = time.time()
            target = DEBUG_DIR / "expedition" / (
                f"settle-special-unknown-{via}-s{sequence or 0}-"
                f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(now))}"
                f"-{uuid4().hex}.png")
            target.parent.mkdir(parents=True, exist_ok=True)
            return bool(save(str(target), force=False))
        except Exception as exc:
            print(f"[远征] 特殊奖励留样失败（不影响收菜记账）: {exc}")
            return False

    def _read_special_amount(self, col_roi, icon_pt):
        """道具图标右侧同行的 ×数量；读不出返回 None（不猜）"""
        try:
            rows = self.maa.ocr_all(col_roi) or []
        except Exception:
            return None
        best = None
        for text, point in rows:
            digits = re.sub(r"\D", "", str(text))
            if not digits:
                continue
            dy = abs(getattr(point, "y", 0) - icon_pt.y)
            if best is None or dy < best[0]:
                best = (dy, int(digits))
        return best[1] if best else None

    def _read_settlement_info(self, cfg) -> dict:
        """
        读结算界面上的归来信息：
          左上「第2部队」→ 哪支队
          右上「一-一 鸟羽·伏见之战」→ 时代-卡位 + 图名
        """
        info = {"team_no": None, "era": None, "slot": None,
                "map_name": None, "header": None}

        team_texts = self.maa.ocr_all(roi_4to4(*cfg["settlement_team_roi"]))
        team_join = "".join(t for t, _ in team_texts)
        m = re.search(r"第\s*([1-5一二三四五])\s*部队", team_join)
        if m:
            ch = m.group(1)
            info["team_no"] = int(ch) if ch.isdigit() else _NUMERALS.get(ch)

        map_texts = self.maa.ocr_all(roi_4to4(*cfg["settlement_map_roi"]))
        map_join = "".join(t for t, _ in map_texts)
        info["header"] = map_join or None
        m2 = re.search(r"([一二三四五])\s*[-—–一]\s*([一二三四五])", map_join)
        if m2:
            info["era"] = _NUMERALS.get(m2.group(1))
            info["slot"] = _NUMERALS.get(m2.group(2))
            # 编号后面的文字就是图名，洗掉分隔符和空白
            name = map_join[m2.end():]
            name = re.sub(r"[\s\-—–]+", "", name)
            info["map_name"] = name or None
        else:
            # 小框里的编号汉字 OCR 认不出（实测会漏）→ 整段当图名
            info["map_name"] = re.sub(r"[\s\-—–]+", "", map_join) or None

        # 编号没读到就拿图名去收益表里反查时代/卡位（名字只留汉字再比对）
        if (not info["era"] or not info["slot"]) and info["map_name"]:
            hit = self._lookup_map_by_name(info["map_name"])
            if hit:
                info["era"], info["slot"] = hit

        return info

    @staticmethod
    def _lookup_map_by_name(name: str):
        """用结算界面的图名反查收益表，返回 (时代, 卡位) 或 None"""
        def cjk(s):
            return re.sub(r"[^一-鿿]", "", s or "")

        target = cjk(name)
        if not target:
            return None
        try:
            with open(_MAPS_TABLE, encoding="utf-8") as f:
                maps = json.load(f)["maps"]
        except Exception:
            return None
        for v in maps.values():
            table_name = cjk(v.get("name"))
            if table_name and (target in table_name or table_name in target):
                return v["era"], v["slot"]
        return None

    def _pick_map_for_goal(self, resource: str):
        """
        按资源目标挑时薪最高的图（大成功收益 / 时长）

        Returns:
            (地图编号, 时代, 卡位) 或 None
        """
        try:
            with open(_MAPS_TABLE, encoding="utf-8") as f:
                maps = json.load(f)["maps"]
        except Exception:
            return None

        best_code, best_rate = None, 0.0
        for code, v in maps.items():
            gain = v.get(resource, 0)
            if gain <= 0:
                continue
            rate = gain / (v["duration_min"] / 60)
            if rate > best_rate:
                best_code, best_rate = code, rate

        if not best_code:
            return None
        v = maps[best_code]
        return best_code, v["era"], v["slot"]
