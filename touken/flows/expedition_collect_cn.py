# -*- coding: utf-8 -*-
"""国服远征收取的画面适配与结算阅读。

收取公共循环（expedition_collect.py）只调用本层的 _collect_* 钩子；
国服配置素材（config.expedition 下的 OCR 词/ROI/点击点/本丸模板）、
结算屏阅读、像素指纹和国服数据通道（游戏事实/收据仲裁）都收在这里。
公共循环不直接访问 device、config 或像素坐标。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from uuid import uuid4

import numpy as np

from ..device_adapter import roi_4to4


class CnExpeditionCollectViewMixin:
    """国服远征收取的画面钩子与结算阅读。"""

    # ==================== 公共循环钩子 ====================

    def _collect_gate(self):
        """配置门：远征素材缺失趁早炸，与原行为一致（导航前读出全部素材键）。"""
        cfg = self.config.get("expedition", {})
        if not cfg:
            return "[收菜] 未配置远征"
        cfg["return_banner_ocr"]
        cfg["result_title_ocr"]
        cfg["collect_tap"]
        cfg["home_ui"]["template"]
        return None

    def _collect_trigger_return_animation(self):
        """远征界面 → 回本丸，强制换场景勾出归来动画；到位返回 True。"""
        yield "[收菜] 🧺 先绕去远征界面再回本丸，勾一下归来动画..."
        for nav_msg in self.navigate_to_stream("远征"):
            yield nav_msg
        if self.current_location != "远征":
            yield "[收菜] 没能到达远征界面，停"
            return False
        for nav_msg in self.navigate_to_stream("本丸"):
            yield nav_msg
        if self.current_location != "本丸":
            yield "[收菜] 没能回到本丸，停"
            return False
        return True

    def _collect_refresh_frame(self):
        self.device.screenshot(force=True)

    def _collect_at_settlement(self) -> bool:
        title_ocr = self.config["expedition"]["result_title_ocr"]
        return bool(self.device.ocr(expected=title_ocr["expected"],
                                    roi=roi_4to4(*title_ocr["roi"])))

    def _collect_at_banner(self) -> bool:
        banner_ocr = self.config["expedition"]["return_banner_ocr"]
        return bool(self.device.ocr(expected=banner_ocr["expected"],
                                    roi=roi_4to4(*banner_ocr["roi"])))

    def _collect_at_home(self) -> bool:
        return bool(self.device.template_match(
            self.config["expedition"]["home_ui"]["template"]))

    def _collect_tap(self):
        self._click_point(self.config["expedition"]["collect_tap"])

    # ==================== 结算观察与记账 ====================

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
        img = self.device.screenshot(force=True)
        if img is None or not self.device.ocr(expected=title_ocr["expected"],
                                           roi=title_roi):
            return None
        # 转场半途保护：标题连看两帧都在才读账，防止读进翻页动画里
        time.sleep(0.4)
        img = self.device.screenshot(force=True)
        if img is None or not self.device.ocr(expected=title_ocr["expected"],
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
        from .expedition import _load_exp_record, _save_exp_record

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
        if not getattr(self.device, "adb_address", None):
            return None
        from ..game_state import read_events, expedition_settlements
        try:
            events = read_events(self.device.adb_path, self.device.adb_address)
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
        ocr_all = getattr(self.device, "ocr_all", None)
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
                pt = self.device.template_match(tpl, roi=col_roi)
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
        save = getattr(self.device, "save_screenshot", None)
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
            rows = self.device.ocr_all(col_roi) or []
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
        from .expedition import _NUMERALS

        info = {"team_no": None, "era": None, "slot": None,
                "map_name": None, "header": None}

        team_texts = self.device.ocr_all(roi_4to4(*cfg["settlement_team_roi"]))
        team_join = "".join(t for t, _ in team_texts)
        m = re.search(r"第\s*([1-5一二三四五])\s*部队", team_join)
        if m:
            ch = m.group(1)
            info["team_no"] = int(ch) if ch.isdigit() else _NUMERALS.get(ch)

        map_texts = self.device.ocr_all(roi_4to4(*cfg["settlement_map_roi"]))
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
        from .expedition import _MAPS_TABLE

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
        from .expedition import _MAPS_TABLE

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

    # ==================== 数据通道 ====================

    def _collection_facts(self, after, exclude=()):
        if not getattr(self.device, "adb_address", None):
            return None
        from ..game_state import read_events, expedition_settlements
        from ..record_sync import consume_game_records
        try:
            events = read_events(self.device.adb_path, self.device.adb_address)
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
        from .expedition import _load_exp_record, _save_exp_record
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
