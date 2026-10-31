# -*- coding: utf-8 -*-
"""
上层业务：出战相关——选地图、选部队、选阵形
（从 touken_agent_engine_v2.py 原样搬家，逻辑未改）
"""

import json
import re
import time
from datetime import datetime

from ..device_adapter import roi_4to4, Point
from ..runtime_paths import STATE_DIR
from ..team_state_reader import read_team_snapshot
from ..team_state import injury_from_ocr_tokens, injury_reaches_threshold


def find_deploy_button(device, cfg: dict):
    """用右下角 OCR 找“部队选择”，返回可点击的文字中心点。

    不能再用“部队选择”PNG：它和“即刻出阵”按钮皮肤相近，模板
    全屏搜索会偶发串台。两个词分别命中才算找到，且完全不走模板。
    """
    deploy_cfg = cfg.get("deploy_button", {})
    ocr_cfg = deploy_cfg.get("ocr", {})
    expected = ocr_cfg.get("expected", ["部队", "选择"])
    if isinstance(expected, str):
        expected = [expected]
    expected = [str(word) for word in expected if word]
    if not expected:
        return None
    # OCR 默认复用 MaaAdapter._last_image。页面切换后的固定 sleep 不会让
    # 缓存失效：旧图上的“部队选择”和新图上的“即刻出阵”恰好同坐标，
    # 曾造成识别完全正确、点击却落在下一页按钮上的跨帧竞态。
    device.screenshot(force=True)
    roi = roi_4to4(*ocr_cfg.get("roi", [1110, 580, 1279, 710]))
    points = []
    for word in expected:
        point = device.ocr(expected=word, roi=roi)
        if not point:
            return None
        points.append(point)
    return points[0]


class BattleMixin:
    """地图/部队/阵形选择。依赖宿主类的 _click_point、_click_template_config。"""

    # ==================== 远征排班接管旗标 ====================

    def _expedition_takeover_requested(self, now: float = None) -> bool:
        """远征排班有班次在等自家任务收工（waiting_busy）时落的旗标。

        玩法主循环只在每圈之间查一次：命中就正常收工，把画面让给排班；
        只读文件，绝不碰游戏画面，更不许在图内战斗中途响应。
        旗标超过 2 小时没人认领按作废处理（落旗的面板多半已经关了）。
        """
        if getattr(self, '_expedition_sakura_active', False):
            return False
        try:
            raw = json.loads((STATE_DIR / "expedition_takeover.json")
                             .read_text(encoding="utf-8"))
            requested_at = float(raw.get("requested_at", 0))
        except (OSError, ValueError, TypeError):
            return False
        age = (time.time() if now is None else now) - requested_at
        return 0 <= age <= 2 * 3600

    def _cancel_equip_warning(self, cfg):
        """安全退出“刀装未满”弹窗；返回 None 表示当前没有该弹窗。"""
        warning_cfg = cfg.get("equip_warning_button", {})
        warning_template = warning_cfg.get("template")
        if not warning_template or not self.device.template_match(warning_template):
            return None

        # 绝不点“继续出阵”。“整备刀装”会关闭警告并回到部队选择。
        prepare_cfg = cfg.get("equip_warning_prepare", {})
        prepare_template = prepare_cfg.get("template", "team/整备刀装.png")
        prepare = self.device.template_match(prepare_template)
        if not prepare:
            return False
        self.device.click(prepare)
        time.sleep(0.8)

        team_ui = cfg.get("team_ui_ocr", {})
        team_roi_raw = team_ui.get("roi")
        team_roi = roi_4to4(*team_roi_raw) if team_roi_raw else None
        for _ in range(6):
            self.device.screenshot(force=True)
            if (team_roi and team_ui.get("expected")
                    and self.device.ocr(team_ui["expected"], team_roi)):
                return True
            time.sleep(0.4)
        return False

    def _match_template_config(self, item: dict):
        """按配置中的 template/roi/threshold 识别，避免相似按钮全屏擦线。"""
        template = item.get("template") if isinstance(item, dict) else None
        if not template:
            return None
        roi_raw = item.get("roi")
        roi = roi_4to4(*roi_raw) if roi_raw else None
        return self.device.template_match(
            template, roi, threshold=float(item.get("threshold", 0.7)))

    def _match_departure_prompt(self, cfg: dict):
        """确认页可用原有模板，或在限定区域识别正文/标题并核对按钮。"""
        prompt = cfg.get("confirm_ui", {})
        ocr_cfg = prompt.get("ocr", {})
        if ocr_cfg:
            roi_raw = ocr_cfg.get("roi")
            if not roi_raw or not ocr_cfg.get("expected"):
                return None
            title = self.device.ocr(
                ocr_cfg["expected"], roi_4to4(*roi_raw),
                match_mode=ocr_cfg.get("match_mode", "exact"))
            if not title:
                return None
            # 顶部标题也会出现在其他出阵相关弹窗；同时核对确认按钮。
            return self._match_departure_button(cfg)
        return self._match_template_config(prompt)

    def _match_departure_button(self, cfg: dict):
        """确认按钮可按限定 ROI 读字；未配置 OCR 的玩法保持原模板识别。"""
        button = cfg.get("confirm_button", {})
        marker = button.get("ocr", {})
        if marker:
            if not marker.get("roi") or not marker.get("expected"):
                return None
            return self.device.ocr(
                marker["expected"], roi_4to4(*marker["roi"]),
                match_mode=marker.get("match_mode", "exact"))
        return self._match_template_config(button)

    def _wait_for_team_record_page(self, record_config: dict,
                                   attempts: int = 10) -> bool:
        """部队记录页同时有“进行记录”和“使用记录”，认任意一个即可。"""
        for _ in range(attempts):
            self.device.screenshot(force=True)
            for key in ("save_confirm", "load_confirm"):
                button = record_config.get(key, {})
                template = button.get("template")
                if template and self.device.template_match(template):
                    return True
            time.sleep(0.4)
        return False

    def _wait_team_select_after_record(self, cfg: dict,
                                       attempts: int = 12) -> bool:
        """保存/使用记录后必须确认回到部队选择，不能靠固定睡眠猜。"""
        team_ui = cfg.get("team_ui_ocr", {})
        roi_raw = team_ui.get("roi")
        expected = team_ui.get("expected", "部队选择")
        if not roi_raw:
            return False
        roi = roi_4to4(*roi_raw)
        for _ in range(attempts):
            self.device.screenshot(force=True)
            if self.device.ocr(expected, roi):
                return True
            time.sleep(0.5)
        return False

    def _open_team_record(self) -> bool:
        """从部队选择/编成页打开共用部队记录。"""
        record = self.config.get("team_select", {}).get("team_record", {})
        button = record.get("button", {})
        if not button or not self._click_template_config(button):
            return False
        time.sleep(0.8)
        return self._wait_for_team_record_page(record)

    def _finish_team_record(self, cfg: dict, record: dict) -> bool:
        """确认回到部队选择；保存记录不自动退页时，安全点左上返回。"""
        if self._wait_team_select_after_record(cfg, attempts=4):
            return True
        if not self._wait_for_team_record_page(record, attempts=1):
            return False
        back = record.get("back", [73, 31])
        self._click_point(back.get("target", [73, 31])
                          if isinstance(back, dict) else back)
        time.sleep(0.8)
        return self._wait_team_select_after_record(cfg, attempts=8)

    def _confirm_team_record(self, record: dict, action: str,
                             record_no: int = 1) -> bool:
        """确认保存/使用记录；模板失败时只在验明弹窗正文后用固定按钮位。"""
        self.device.screenshot(force=True)
        yes_cfg = record.get("yes_button", {})
        yes_roi_raw = yes_cfg.get("roi", [650, 430, 920, 590])
        yes_roi = roi_4to4(*yes_roi_raw)
        template = yes_cfg.get("template", "team/是.png")
        yes = self.device.template_match(template, yes_roi, threshold=0.65)
        if yes:
            self.device.click(yes)
            return True

        # 游戏更新会微调按钮皮肤，旧模板可能失效。固定坐标只能在 OCR 同时
        # 认出弹窗标题、动作和记录一时使用，绝不能见到任意“是”就点。
        modal_roi = roi_4to4(*record.get("confirm_ocr_roi", [330, 130, 950, 590]))
        tokens = self.device.ocr_all(modal_roi)
        text = "".join(str(token) for token, _ in tokens).replace(" ", "")
        action_text = "记录到" if action == "save" else "使用"
        record_labels = ("记录部队一", "记录部队1") if record_no == 1 else (
            f"记录部队{record_no}",)
        guarded = ("部队记录" in text and "是否" in text
                   and action_text in text
                   and any(label in text for label in record_labels))
        if not guarded:
            return False

        ocr_yes = self.device.ocr("是", yes_roi, match_mode="exact")
        self.device.click(ocr_yes or Point(784, 510))
        return True

    def _save_team_record(self, cfg: dict, record_no: int = 1) -> bool:
        """把当前出阵部队保存到固定记录；目前产品只使用记录一。"""
        from ..expedition_sakura import pending_restore
        if pending_restore() and not getattr(self, '_expedition_sakura_active', False):
            return False
        record = self.config.get("team_select", {}).get("team_record", {})
        if not self._open_team_record():
            return False
        target = record.get("records", {}).get(str(record_no))
        if target:
            self._click_point(target)
            time.sleep(0.3)
        if not self._click_template_config(record.get("save_confirm", {})):
            return False
        time.sleep(0.5)
        if not self._confirm_team_record(record, "save", record_no):
            return False
        if not self._finish_team_record(cfg, record):
            return False
        if hasattr(self, "record_event"):
            self.record_event("team_record.saved", record_no=record_no)
        return True

    def _load_team_record_confirmed(self, cfg: dict, record_no: int = 1) -> bool:
        """Use a selected record only after confirming the page and modal."""
        record = self.config.get('team_select', {}).get('team_record', {})
        target = record.get('records', {}).get(str(record_no))
        if not target or not self._open_team_record():
            return False
        self._click_point(target)
        time.sleep(.3)
        if not self._click_template_config(record.get('load_confirm', {})):
            return False
        time.sleep(.5)
        return (self._confirm_team_record(record, 'load', record_no)
                and self._finish_team_record(cfg, record))

    def _restore_equipment_from_warning(self, cfg: dict,
                                        record_no: int = 1) -> bool | None:
        """空刀装警告 → 整备刀装 → 使用记录一 → 回部队选择。

        None 表示没有警告；False 表示确有警告但恢复链路失败。
        """
        warning = cfg.get("equip_warning_button", {}).get("template")
        if not warning or not self.device.template_match(warning):
            return None
        prepare = cfg.get("equip_warning_prepare", {}).get(
            "template", "team/整备刀装.png")
        prepare_pt = self.device.template_match(prepare)
        if not prepare_pt:
            return False
        self.device.click(prepare_pt)
        time.sleep(1.0)

        record = self.config.get("team_select", {}).get("team_record", {})
        # “整备刀装”落在部队编成页，等右侧部队记录按钮真正可点。
        opened = False
        for _ in range(12):
            self.device.screenshot(force=True)
            if self._click_template_config(record.get("button", {})):
                opened = True
                break
            time.sleep(0.5)
        if not opened or not self._wait_for_team_record_page(record):
            return False

        target = record.get("records", {}).get(str(record_no))
        if target:
            self._click_point(target)
            time.sleep(0.3)
        if not self._click_template_config(record.get("load_confirm", {})):
            return False
        time.sleep(0.5)
        if not self._confirm_team_record(record, "load", record_no):
            return False
        if not self._finish_team_record(cfg, record):
            return False
        if hasattr(self, "record_event"):
            self.record_event("equipment.restored", record_no=record_no)
        return True

    # 部队选择页六行「装备」按钮（2026-10-22 真机校准：行距 ~94.5px，
    # 与编队页 98px 不同，两页坐标不共用——刷花模块踩过这个坑）
    _TEAM_SELECT_EQUIP_X = 960
    _TEAM_SELECT_ROW_CY = (172, 266, 361, 456, 550, 645)

    def _refill_equipment_from_warning_stream(self, cfg: dict, team_no: int,
                                              tag: str, record_no: int = 1):
        """刀装空缺警告 → 整备刀装 → 逐成员「自动装备刀装」补空位 → 重存记录。

        警告上的「整备刀装」只是关掉警告回到部队选择页（2026-10-22 真机实测），
        这页每行自带「装备」入口。游戏的自动装备只填空位：已配齐的成员身上
        点它是无操作（无确认弹窗、不替换现有刀装），所以可以安全逐成员刷一遍。
        比「使用记录一」安全得多：绝不整队替换，也不依赖记录一里存的是哪支队
        ——记录一可能被进场前就缺装的快照污染（2026-10-22 挖地连续两天翻车：
        存了缺装快照再拿它补，拿空气补空气）。

        Yields 日志；返回 (结果, 记录是否已重存)：
          结果 None=没有警告；True=补装链路完成；False=链路失败（调用方停止）。
        """
        warning = cfg.get("equip_warning_button", {}).get("template")
        if not warning or not self.device.template_match(warning):
            return None, False
        prepare = cfg.get("equip_warning_prepare", {}).get(
            "template", "team/整备刀装.png")
        prepare_pt = self.device.template_match(prepare)
        if not prepare_pt:
            return False, False
        self.device.click(prepare_pt)
        time.sleep(1.0)
        if not self._wait_team_select_after_record(cfg, attempts=8):
            yield f"{tag} 整备刀装后没回到部队选择页，停止点击"
            return False, False

        # 有数据只刷在编成员的行；数据不在册则六行都试——空成员行没有
        # 「装备」按钮，点了装备页不会打开，逐行验证后跳过，不盲点。
        from ..team_state_reader import read_team_snapshot
        snapshot = read_team_snapshot(self, team_no, default_connection=True)
        slots = (sorted(m.slot for m in snapshot.members)
                 if snapshot is not None else [])
        if not slots:
            slots = list(range(1, 7))
        yield f"{tag} 刀装有空缺，改用整备逐成员补装（不动记录一的队伍）"
        for slot in slots:
            if slot not in range(1, 7):
                return False, False
            self._click_point((self._TEAM_SELECT_EQUIP_X,
                               self._TEAM_SELECT_ROW_CY[slot - 1]))
            time.sleep(1.5)
            opened = False
            for _ in range(4):
                self.device.screenshot(force=True)
                if self.device.ocr("更换装备", roi_4to4(530, 0, 750, 60)):
                    opened = True
                    break
                time.sleep(0.5)
            if not opened:
                # 数据说在编却打不开装备页才算异常；无数据试行时跳过即可
                if snapshot is not None:
                    yield f"{tag} {slot}号位装备页没打开，停止点击"
                    return False, False
                continue
            if not (yield from self._sakura_clear_equipment_filters(tag=tag)):
                return False, False
            auto_pt = self.device.template_match("team/自动装备刀装.png",
                                                 threshold=0.85)
            if not auto_pt:
                yield f"{tag} {slot}号位没找到自动装备刀装按钮，停止点击"
                return False, False
            self.device.click(auto_pt)
            time.sleep(2.0)
            self.device.screenshot(force=True)
            confirm = self.device.template_match("通用_确定.png", threshold=0.7)
            if confirm:
                self.device.click(confirm)
                time.sleep(1.0)
            self._click_point((135, 25))
            time.sleep(1.5)
            if not self._wait_team_select_after_record(cfg, attempts=8):
                yield f"{tag} {slot}号位补装后没回到部队选择页，停止点击"
                return False, False

        # 补完整存一次记录：把之前可能存进去的缺装快照洗成满装快照，
        # 本次后续圈层的「使用记录一」快速通道才有东西可补。
        if self._save_team_record(cfg, record_no=record_no):
            yield f"{tag} 🛡️ 已逐成员补齐刀装，记录一已更新"
            if hasattr(self, "record_event"):
                self.record_event("equipment.refilled", team_no=team_no,
                                  record_no=record_no, source="equip_warning")
            return True, True
        yield f"{tag} 🛡️ 已逐成员补齐刀装；记录一更新失败（不影响本次出阵）"
        if hasattr(self, "record_event"):
            self.record_event("equipment.refilled", team_no=team_no,
                              source="equip_warning")
        return True, False

    _recover_ok: bool = False

    def _record_ticket_refill(self, cfg: dict, tag: str):
        """补票事实与标准资源流水双写；无明确票价时只保留玩法事实。"""
        if not hasattr(self, "record_event"):
            return
        source = tag.strip("[]")
        receipt = getattr(self, "_ticket_refill_receipt", None)
        self._ticket_refill_receipt = None
        price = receipt.get("spent") if isinstance(receipt, dict) else int(cfg["ticket_price"]) if cfg.get("ticket_price") else None
        refill_payload = {"source": source}
        if price:
            refill_payload.update(
                resource="小判", delta=-price, ticket_price=price)
        refill_id = self.record_event("ticket.refilled", **refill_payload)
        if not price:
            return
        change = {
            "source": "ticket.refilled",
            "evidence": "game_refill_result" if isinstance(receipt, dict) else "confirmed_refill_flow",
            "note": f"{source}补手形",
        }
        if isinstance(refill_id, int):
            change["source_event_id"] = refill_id
        if hasattr(self, "record_resource_change"):
            self.record_resource_change("小判", -price, **change)
        else:
            self.record_event(
                "resource.change", resource="小判", delta=-price,
                attribution="confirmed", **change)

    def _recover_ticket_stream(self, cfg: dict, tag: str = "[出阵]"):
        """通用手形补充。两种 UI 都支持：
        - 联队战式：补充 → 恢复1个 → 确定（配了 recover_button）；
        - 花札滑条式：补充 → 数量页确认 → 小判消耗确认 → 结果提示确认
          （不配 recover_button；三连确认是同一张图，见到就点、消失
          为止，最多 3 次防死循环。数量页默认补 1 个，可配
          quantity_ocr 复核数量，读不清或不是 1 就停手不点）。"""
        from ..game_state import begin_operation, confirm_ticket
        self._recover_ok = False
        self._ticket_refill_receipt = None
        boundary = begin_operation(self.device)
        rec_cfg = cfg["ticket_recover"]

        self.device.screenshot(force=True)
        refill = self._match_template_config(rec_cfg["popup_button"])
        if not refill:
            yield f"{tag} 找不到补充按钮"
            return
        self.device.click(refill)
        time.sleep(1.5)

        recover_cfg = rec_cfg.get("recover_button", {})
        slider_style = not recover_cfg.get("template")
        if not slider_style:
            self.device.screenshot(force=True)
            recover = self._match_template_config(recover_cfg)
            if not recover:
                yield f"{tag} 找不到恢复1个按钮"
                return
            self.device.click(recover)
            time.sleep(1.5)

        qty_roi_raw = rec_cfg.get("quantity_ocr", {}).get("roi")
        if qty_roi_raw:
            self.device.screenshot(force=True)
            qty = self._ocr_refill_quantity(roi_4to4(*qty_roi_raw))
            if qty is None:
                yield f"{tag} 补充数量没读清，停手没点确认"
                return
            if qty != 1:
                yield (f"{tag} 补充数量读出来是 {qty} 不是 1，"
                       "停手没点确认，你去看一眼补充页")
                return

        max_clicks = 3 if slider_style else 1
        clicks = 0
        while clicks < max_clicks:
            self.device.screenshot(force=True)
            confirm = self._match_template_config(rec_cfg["confirm_button"])
            if not confirm:
                break
            self.device.click(confirm)
            clicks += 1
            time.sleep(1.5)
            receipt = confirm_ticket(self.device, boundary)
            if receipt is False:
                yield f"{tag} 游戏未受理本次补充，停止点击"
                return
            if isinstance(receipt, dict):
                self._ticket_refill_receipt = receipt
        if clicks == 0:
            yield f"{tag} 找不到恢复完毕的确定按钮"
            return
        if slider_style and clicks >= max_clicks:
            self.device.screenshot(force=True)
            if self._match_template_config(rec_cfg["confirm_button"]) and not isinstance(self._ticket_refill_receipt, dict):
                yield f"{tag} 确认点了 {clicks} 次还在，停手防死循环"
                return
        time.sleep(0.5)

        receipt = confirm_ticket(self.device, boundary)
        if receipt is None and isinstance(self._ticket_refill_receipt, dict):
            receipt = self._ticket_refill_receipt
        if receipt is False:
            yield f"{tag} 游戏未受理本次补充，停止点击"
            return
        if isinstance(receipt, dict):
            self._ticket_refill_receipt = receipt
        elif self._match_template_config(rec_cfg["confirm_button"]):
            yield f"{tag} 补充结果未确认，停止点击，不再重复付款"
            return
        self._recover_ok = True
        yield f"{tag} 手形补充完成"

    def _ocr_refill_quantity(self, roi) -> int | None:
        """读补充页数量框里的数字；读不出返回 None，由调用方停止付款。"""
        try:
            tokens = self.device.ocr_all(roi)
            digits = "".join(
                re.sub(r"\D", "", str(text)) for text, _ in tokens or [])
            return int(digits) if digits else None
        except Exception:
            return None

    # ==================== 地图选择 ====================

    def select_map(self, map_type: str, chapter: str = None,
                   map_no: str = None) -> bool:
        """
        通用地图选择

        Args:
            map_type: 地图类型，如 "合战场", "活动", "远征"
            chapter: 章节编号
            map_no: 小图编号

        Returns:
            是否选择成功
        """
        map_config = self.config.get("map_select", {}).get(map_type)
        if not map_config:
            print(f"[ERROR] 未知地图类型: {map_type}")
            return False

        print(f"[MAP] 选择地图: {map_type}, 章节={chapter}, 图={map_no}")

        # 活动类型特殊处理
        if map_type == "活动":
            entry = map_config["entry"]
            self._click_template_config(entry)

            # 验证是否到达活动界面
            for template in map_config.get("verify_templates", []):
                roi_raw = map_config["verify_roi"]
                roi = roi_4to4(roi_raw[0], roi_raw[1], roi_raw[2], roi_raw[3])
                if self.device.exists(template, roi):
                    print(f"[MAP] 到达活动界面")
                    return True
            return False

        # 选择章节
        if "chapters" in map_config and chapter:
            chapter_key = str(chapter)
            if chapter_key in map_config["chapters"]:
                self._click_point(map_config["chapters"][chapter_key])
                time.sleep(0.3)

        # 选择小图
        if "maps" in map_config and map_no:
            map_key = str(map_no)
            if map_key in map_config["maps"]:
                self._click_point(map_config["maps"][map_key])
                time.sleep(0.3)

        # 点击决定/确认
        if "confirm" in map_config:
            self._click_point(map_config["confirm"])

        return True

    # ==================== 部队选择 ====================

    def _find_deploy_button(self, cfg: dict):
        return find_deploy_button(self.device, cfg)

    def _wait_for_team_select(self, cfg: dict, attempts: int = 10,
                              open_after: int = None) -> bool:
        """等待部队选择页；需要时点击玩法页上的“部队选择”按钮。"""
        ocr_cfg = cfg.get("team_ui_ocr", {})
        roi_raw = ocr_cfg.get("roi")
        expected = ocr_cfg.get("expected", "部队选择")
        if not roi_raw:
            return False
        roi = roi_4to4(*roi_raw)

        for attempt in range(attempts):
            self.device.screenshot(force=True)
            if self.device.ocr(expected=expected, roi=roi):
                return True
            if open_after is not None and attempt == open_after:
                deploy = self._find_deploy_button(cfg)
                if deploy:
                    self.device.click(deploy)
                    time.sleep(1.5)
            time.sleep(0.5)
        return False

    def _pick_team(self, team_no: int) -> bool:
        """在部队选择页切换部队；第二次点击用于确认当前标签。"""
        teams = self.config.get("team_select", {}).get("teams", {})
        target = teams.get(str(team_no))
        if not target:
            return False
        self._click_point(target)
        time.sleep(0.3)
        self._click_point(target)
        time.sleep(0.5)
        return True

    def _click_depart(self, cfg: dict) -> bool:
        """点击各玩法共用的“即刻出阵”按钮。"""
        depart_cfg = cfg.get("depart_button", {})
        template = depart_cfg.get("template")
        depart = self.device.template_match(template) if template else None
        if not depart:
            return False
        self.device.click(depart)
        time.sleep(1.5)
        return True

    @staticmethod
    def _injury_reaches_threshold(injury: str, threshold: str) -> bool:
        """共用伤势阈值：轻伤 < 中伤 < 重伤。"""
        return injury_reaches_threshold(injury, threshold)

    def _log_injury_status(self, team_no: int, cache: dict):
        """当前部队验伤：共用快照里的精确 hp、实例编号和伤势，
        同名复制人不会互相误判。返回 (伤势, 人话明细)；拉不到、数据过旧、
        成员缺数据一律返回 (None, None)——「不知道」就回退视觉链，
        绝不拿半残数据放行。

        cache 在一次出阵链内复用：同圈复检不再 pull（一次 pull 含
        adb root + 传输，秒级耗时，不能进热循环）。原始日志解析完即焚。
        """
        cfg = {}
        if isinstance(getattr(self, "config", None), dict):
            cfg = self.config.get("injury_check", {}) or {}
        if not cfg.get("use_youzu_log", True):
            return None, None
        try:
            snapshot = read_team_snapshot(self, team_no, cache=cache,
                max_age=float(cfg.get("max_age_sec", 600)), default_connection=True)
            report = snapshot.injury_report() if snapshot is not None else None
        except Exception:
            return None, None
        if not report:
            return None, None
        hurt = [m for m in report["members"] if m["injury"]]
        detail = ("、".join(f"{m['label'] or m['name']}{m['injury']}"
                            f"(hp{m['hp']}/{m['hp_max']})" for m in hurt)
                  if hurt else "全员满血")
        return report["max_injury"], detail

    def _combined_injury_status(self, cfg: dict, team_no: int,
                                log_cache: dict):
        """有效游戏血量作准；数据缺失或过时才使用画面验伤。"""
        log_injury, detail = self._log_injury_status(team_no, log_cache)
        if detail is not None:
            return log_injury, detail
        return self._team_injury_status(cfg), None

    def _team_injury_status(self, cfg):
        """在部队列表或战斗结果页识别当前最高伤势。"""
        # 只看左侧我方六人。大阪城结果页右侧敌军会出现红色“破坏”章，
        # 全屏识别会把它误认成同为红底的“重伤”，进而错误触发手入。
        roi = roi_4to4(*cfg.get("injury_status_roi", [0, 90, 570, 690]))
        stamp_roi = roi_4to4(*cfg.get("injury_stamp_roi", [220, 90, 340, 690]))
        # OCR 读章面文字为主：红章转灰度后模板匹配会系统性丢分（2026-10-19
        # AB 实测真章 cv2 0.85 擦线、无章画面噪声峰同高），文字识别没有
        # 这层校准差。模板只作 OCR 漏读（糊帧/动画）时的兜底，且只搜六行
        # 头像右缘的伤势章窄栏。
        hit = injury_from_ocr_tokens(self.device.ocr_all(roi))
        if hit:
            return hit
        # 三种伤势章版式几乎一样：实测中伤章能以 0.704 擦线命中重伤模板，
        # 分类阈值必须守住 0.88。
        stamps = cfg.get("injury_stamps", {})
        for severity in ("重伤", "中伤", "轻伤"):
            stamp_cfg = stamps.get(severity, {})
            template = stamp_cfg.get("template")
            threshold = float(stamp_cfg.get("threshold", 0.88))
            if template and self.device.template_match(
                    template, stamp_roi, threshold=threshold):
                return severity
        if not stamps:
            legacy = cfg.get("injury_stamp", {}).get("template")
            if legacy and self.device.template_match(legacy, roi):
                return "重伤"
        return None

    def _battle_dialog_text(self):
        """只读中央弹窗正文，避免把背景按钮当作确认内容。"""
        return "".join(str(text).replace(" ", "") for text, _ in
                       self.device.ocr_all(roi_4to4(330, 240, 950, 440)))

    @staticmethod
    def _is_heavy_injury_dialog(text):
        return ("重伤" in text and any(word in text for word in ("继续", "进军", "出阵", "取消"))
                or "刀剑破坏" in text and any(word in text for word in ("后果", "继续", "确认", "取消")))

    def _battle_dialog_button(self, label):
        # 国服两级重伤确认、返程确认都使用左右按钮；必须读到完整按钮文字。
        bounds = (650, 400, 950, 580) if label == "否" else (330, 400, 650, 580)
        roi = roi_4to4(*bounds)
        return next((point for text, point in self.device.ocr_all(roi)
                     if str(text).strip() == label
                     and roi.x <= point.x < roi.x + roi.w
                     and roi.y <= point.y < roi.y + roi.h), None)

    def _deny_heavy_injury_warning(self, cfg: dict) -> bool:
        """两级重伤警告均只允许点否；无法确认取消时立即停止。"""
        from ..flow_control import FlowAborted
        if not self._is_heavy_injury_dialog(self._battle_dialog_text()):
            return False
        for _ in range(3):
            deny = self._battle_dialog_button("否")
            if deny is None:
                raise FlowAborted("重伤警告的【否】未确认，已停止点击，请手动查看")
            self.device.click(deny)
            time.sleep(0.8)
            self.device.screenshot(force=True)
            if not self._is_heavy_injury_dialog(self._battle_dialog_text()):
                break
        else:
            raise FlowAborted("重伤警告取消后仍未关闭，已停止点击，请手动查看")
        if hasattr(self, "record_event"):
            self.record_event("injury_warning.denied", severity="heavy")
        return True

    # ==================== 通用安全出阵链 ====================

    def _safe_depart_stream(self, cfg: dict, team_no: int, tag: str,
                            repair_threshold: str = "light",
                            auto_equip: bool = False,
                            team_record_saved: bool = False,
                            auto_refill: bool = False,
                            rotate_captain: bool = False,
                            rotate_captain_margin: int = 10,
                            prepare_team_stream=None,
                            refill_state: dict | None = None,
                            departure_state: dict | None = None,
                            after_equipment_stream=None):
        """通用安全出阵链：部队选择页已打开之后调用，串起——

        选择部队 → 出阵前伤势检查 → （可选）保存记录一 → 即刻出阵 →
        票尽补票弹窗（可选，异去同款交互：点确定补一张 → 重新出阵）→
        刀装未满警告处理（记录一恢复，或逐成员整备自动装备，或安全取消）→
        重伤确认弹窗拦截。

        各玩法只负责把部队选择页打开、以及之后自己的二次确认与入图验证；
        中间的人身安全语义全在这里，不准各玩法再自己手搓一遍。

        两种补票 UI 都支持：
        - cfg["ticket_refill"]：单层 popup/confirm_button/cancel_button；
        - cfg["ticket_recover"]：联队战式“补充→恢复1个→确定”。
        没配对应模板就不认（票尽时二次确认会等不到，安全停）。
        每圈最多补一张（refill_done），补完又弹说明没补上，停手防重复消费。
        prepare_team_stream 在选队和伤势检查之后、即刻出阵之前执行玩法准备；
        refill_state 在确实补票后写入 used，供玩法遵守本次运行的购买上限。
        departure_state 的 blocked_injury 只在出发前验伤拦截时写入，供
        连续出阵按原设置转去手入；其他失败绝不能被当成需要手入后重试。
        准备回调明确返回 False 就停止；恢复刀装后可重新验收行军设置。

        Yields 日志；返回 (ok, team_record_saved)。ok=False 时不得再点
        出阵确认；仅明确得到 blocked_injury 的调用方可按设置转去手入。
        """
        if departure_state is not None:
            departure_state.clear()
        # 调用方已经通过 _wait_for_team_select 确认进入部队选择页，并在
        # 启动时检查过部队坐标。_pick_team 只负责按坐标点两次，本身无法
        # 观察游戏是否真的选中；不要把它的返回值冒充真机选队验证。
        from ..expedition_sakura import pending_restore
        if pending_restore() and not getattr(self, '_expedition_sakura_active', False):
            yield f'{tag} ✗ 远征补花队伍尚未恢复，绝不出阵，请先执行远征管理'
            return False, team_record_saved
        self._pick_team(team_no)
        if rotate_captain:
            try:
                rotated = yield from self._rotate_captain_here(rotate_captain_margin,
                                                               team_no=team_no)
                if rotated is False:
                    return False, team_record_saved
            except Exception as exc:
                # 无法知道异常发生在拖动前还是拖动后，不能带着未验收的
                # 队伍继续保存记录或出阵。
                yield f"{tag} 自动换队长未确认，停止出阵: {exc}"
                return False, team_record_saved
        self.device.screenshot(force=True)
        log_cache: dict = {}
        injury, log_note = self._combined_injury_status(cfg, team_no,
                                                        log_cache)
        if log_note:
            yield f"{tag} 日志验伤：{log_note}"
        if injury and self._injury_reaches_threshold(
                injury, repair_threshold):
            if departure_state is not None:
                departure_state["blocked_injury"] = injury
            yield f"{tag} 出阵前检测到{injury}，已达到停止条件，本次不出阵"
            return False, team_record_saved
        if auto_equip and not team_record_saved:
            yield f"{tag} 自动补充刀装已开启，先把当前部队保存到记录一"
            if self._save_team_record(cfg, record_no=1):
                team_record_saved = True
                yield f"{tag} ✓ 当前部队已保存到记录一"
            else:
                yield (f"{tag} ⚠️ 没能安全保存记录一，已停止；"
                       "请查看是否有确认弹窗未处理")
                return False, team_record_saved

        if prepare_team_stream is not None:
            if (yield from prepare_team_stream()) is False:
                return False, team_record_saved

        equip_retries = 0
        record_restore_tried = False
        refill_done = False
        while True:
            if not self._click_depart(cfg):
                yield f"{tag} 找不到即刻出阵按钮"
                return False, team_record_saved
            self.device.screenshot(force=True)

            # 明确的出阵确认标题优先级最高。江户城二次出阵确认页的绿色
            # “确定”曾以 0.706 擦线命中绿色“补充”，若先查补票会被误判成
            # 又缺票并触发防重复消费。看到标题就结束补票分支，交给调用方
            # 的 _confirm_departure 正常点击确定。
            if self._match_departure_prompt(cfg):
                break

            # 江户城/联队战同款两层令牌恢复 UI：不足弹窗先点“补充”，
            # 再在补充页选择“恢复一个”，最后确认。直接复用已经实战验证的
            # _recover_ticket_stream，不另造江户城模板和点击顺序。
            recover = cfg.get("ticket_recover", {})
            recover_popup = recover.get("popup_button", {})
            if self._match_template_config(recover_popup):
                if not auto_refill or refill_done:
                    if refill_done:
                        yield (f"{tag} 补票后仍弹补票窗，停止点击，"
                               "防止重复消费小判")
                        return False, team_record_saved
                    close = self._match_template_config(
                        recover.get("close_button", {}))
                    if close:
                        self.device.click(close)
                        time.sleep(1.0)
                        yield f"{tag} 票用完了，不补票，已关闭弹窗，收工"
                    else:
                        yield (f"{tag} 票用完了，不补票；已停止点击，"
                               "请手动关闭补充弹窗")
                    return False, team_record_saved
                for recover_msg in self._recover_ticket_stream(cfg, tag=tag):
                    yield recover_msg
                if not self._recover_ok:
                    yield f"{tag} 手形补充失败，停止出阵"
                    close = self._match_template_config(
                        recover.get("close_button", {}))
                    if close:
                        self.device.click(close)
                        time.sleep(1.0)
                        yield f"{tag} 已顺手关闭补充弹窗"
                    return False, team_record_saved
                refill_done = True
                if refill_state is not None:
                    refill_state["used"] = True
                self._record_ticket_refill(cfg, tag)
                yield f"{tag} 🎫 票已用小判补上一张，重新点即刻出阵"
                continue

            # 票尽时游戏自己弹补票窗（不用提前数票）：认出来才处理，
            # 不补就点取消收工；补就点确定，然后重新点即刻出阵。
            refill = cfg.get("ticket_refill", {})
            popup_template = refill.get("popup", {}).get("template")
            if popup_template and self.device.template_match(popup_template):
                if not auto_refill or refill_done:
                    if refill_done:
                        yield (f"{tag} 补票后仍弹补票窗，停止点击，"
                               "防止重复消费小判")
                        return False, team_record_saved
                    cancel_template = refill.get(
                        "cancel_button", {}).get("template")
                    cancel = (self.device.template_match(cancel_template)
                              if cancel_template else None)
                    if cancel:
                        self.device.click(cancel)
                        time.sleep(1.0)
                        yield f"{tag} 票用完了，不补票，已取消出阵，收工"
                    else:
                        yield (f"{tag} 票用完了，不补票；已停止点击，"
                               "请手动关掉购票弹窗")
                    return False, team_record_saved
                confirm_template = refill.get(
                    "confirm_button", {}).get("template")
                confirm = (self.device.template_match(confirm_template)
                           if confirm_template else None)
                if not confirm:
                    yield (f"{tag} 补票弹窗的确定按钮没认出，停止点击，"
                           "请手动看一眼")
                    return False, team_record_saved
                from ..game_state import begin_operation, confirm_ticket
                boundary = begin_operation(self.device)
                self._ticket_refill_receipt = None
                self.device.click(confirm)
                time.sleep(1.5)
                receipt = confirm_ticket(self.device, boundary)
                if receipt is False:
                    yield f"{tag} 游戏未受理本次补票，停止出阵"
                    return False, team_record_saved
                if isinstance(receipt, dict):
                    self._ticket_refill_receipt = receipt
                elif self.device.template_match(popup_template):
                    yield f"{tag} 补票结果未确认，停止点击，防止重复消费小判"
                    return False, team_record_saved
                refill_done = True
                if refill_state is not None:
                    refill_state["used"] = True
                self._record_ticket_refill(cfg, tag)
                yield f"{tag} 🎫 票已用小判补上一张，重新点即刻出阵"
                continue

            if auto_equip:
                # 两条补装通道：记录一已存本队好快照时先用「使用记录一」（快）；
                # 恢复后警告还在（记录一存的就是缺装快照，拿空气补空气）或根本
                # 没存过记录，就改走整备逐成员自动装备（慢但真补，不整队替换）。
                if team_record_saved and not record_restore_tried:
                    equip_result = self._restore_equipment_from_warning(
                        cfg, record_no=1)
                    if equip_result is None:
                        break
                    if not equip_result:
                        yield f"{tag} 刀装有空缺，但从记录一恢复失败，停止出阵"
                        return False, team_record_saved
                    record_restore_tried = True
                    yield f"{tag} 🛡️ 刀装有空缺，已使用记录一自动补齐"
                else:
                    equip_result, resaved = (
                        yield from self._refill_equipment_from_warning_stream(
                            cfg, team_no, tag))
                    if equip_result is None:
                        break
                    if not equip_result:
                        yield f"{tag} 刀装有空缺，整备补装没完成，停止出阵"
                        return False, team_record_saved
                    if resaved:
                        team_record_saved = True
                        record_restore_tried = False  # 新记录是满装快照，快速通道恢复可用
                equip_retries += 1
                if equip_retries >= 3:
                    yield (f"{tag} 补装后仍报刀装空缺（刀装库存可能不足），"
                           "停止重试；请手动整备刀装")
                    return False, team_record_saved
                self.device.screenshot(force=True)
                # 使用记录可能改变队员；不得复用恢复前的验伤数据。
                log_cache.clear()
                restored_injury, _ = self._combined_injury_status(
                    cfg, team_no, log_cache)
                if restored_injury and self._injury_reaches_threshold(
                        restored_injury, repair_threshold):
                    yield f"{tag} 恢复刀装后检测到{restored_injury}，不再出阵"
                    return False, team_record_saved
                if after_equipment_stream is not None:
                    if (yield from after_equipment_stream()) is False:
                        return False, team_record_saved
                continue
            equip_cancelled = self._cancel_equip_warning(cfg)
            if equip_cancelled is None:
                break
            yield (f"{tag} 刀装未满，已进入整备，本次停止" if equip_cancelled
                   else f"{tag} 刀装未满且无法安全进入整备，停止")
            return False, team_record_saved
        if self._deny_heavy_injury_warning(cfg):
            yield f"{tag} 队员重伤确认弹窗，已点【否】；有重伤绝不出阵"
            return False, team_record_saved
        return True, team_record_saved

    def _confirm_departure(self, cfg: dict, *, text_prompt: dict | None = None) -> bool:
        """通用出阵二次确认：认弹窗标题后点确认按钮。

        已用专属标题确认弹窗后，模板失配时才允许点实测坐标兜底
        （比如大阪城确认窗的绿色“确定”与通用灰按钮不是同一皮肤）；
        没配 target 的玩法只用识别结果，绝不盲点；按钮配置 OCR 时
        必须读到按钮文字，不允许退回旧坐标。
        text_prompt 用于限定区域正文确认页，同样必须先认文字再点目标。
        """
        prompt = cfg.get("confirm_ui", {})
        for _ in range(10):
            self.device.screenshot(force=True)
            if text_prompt is not None:
                # 文字确认页使用已限定区域的正文作为门闩；认到才可点目标。
                target = text_prompt.get("confirm_target")
                if target and self.device.ocr(
                        text_prompt["expected"], roi_4to4(*text_prompt["roi"])):
                    self._click_point(target)
                    time.sleep(1.5)
                    return True
                time.sleep(0.5)
                continue
            if not prompt or self._match_departure_prompt(cfg):
                confirm = cfg.get("confirm_button", {})
                button = self._match_departure_button(cfg)
                if button:
                    self.device.click(button)
                    time.sleep(1.5)
                    return True
                target = confirm.get("target")
                if target and not confirm.get("ocr"):
                    self._click_point(target)
                    time.sleep(1.5)
                    return True
            time.sleep(0.5)
        return False

    def select_team(self, team_no: int, auto_march: bool = False,
                    load_record: int = None, equip: bool = False) -> bool:
        """
        通用部队选择

        Args:
            team_no: 部队编号 1-5
            auto_march: 是否启用自动行军
            load_record: 加载第几号记录（None 表示不加载）
            equip: 是否补充刀装

        Returns:
            是否成功出发
        """
        team_config = self.config.get("team_select", {})

        # 1. 点击"部队选择"按钮
        enter = team_config["enter_button"]
        deploy = self._find_deploy_button({"deploy_button": enter})
        if deploy:
            self.device.click(deploy)
        time.sleep(1.5)

        # 2. 选择部队
        team_key = str(team_no)
        if team_key in team_config["teams"]:
            self._click_point(team_config["teams"][team_key])
            time.sleep(0.3)
            # 双击确认
            self._click_point(team_config["teams"][team_key])
            time.sleep(0.5)

        # 3. 可选：加载记录
        if load_record:
            self._load_team_record(load_record)

        # 4. 可选：补充刀装
        if equip:
            self._equip_swords()

        # 5. 可选：自动行军
        if auto_march:
            self._enable_auto_march()

        # 6. 出发
        self._click_point(team_config["depart"])
        return True

    def _load_team_record(self, record_no: int) -> bool:
        """旧调用兼容：加载部队记录（新战斗流程使用带验证的恢复方法）。"""
        record_config = self.config["team_select"]["team_record"]

        # 点击部队记录按钮
        self._click_template_config(record_config["button"])
        time.sleep(0.5)

        # 选择记录
        record_key = str(record_no)
        if record_key in record_config["records"]:
            self._click_point(record_config["records"][record_key])
            time.sleep(0.3)

        # 点击使用记录
        self._click_template_config(record_config["load_confirm"])
        time.sleep(0.3)

        # 确认弹窗
        self._click_template_config(record_config["yes_button"])
        return True

    def _equip_swords(self) -> bool:
        """补充刀装"""
        record_config = self.config["team_select"]["team_record"]

        # 点击部队记录
        self._click_template_config(record_config["button"])
        time.sleep(0.5)

        # 点击使用记录
        self._click_template_config(record_config["load_confirm"])
        time.sleep(0.3)

        # 确认
        self._click_template_config(record_config["yes_button"])
        return True

    def _enable_auto_march(self) -> bool:
        """启用自动行军（委托）
        真机校准的完整流程：点"自动行军"开弹窗 → 等"委托"按钮出现 →
        点它只是【选中单选框】→ 必须再点 X 关闭弹窗才生效。
        每步都验证绝不盲点——旧版 0.3s 短睡，弹窗没开就点 X，
        把部队选择面板关掉的 bug 就是这么来的。
        """
        march_config = self.config["team_select"]["auto_march"]
        check_roi_raw = march_config["check_delegated"]["roi"]
        check_roi = roi_4to4(check_roi_raw[0], check_roi_raw[1],
                             check_roi_raw[2], check_roi_raw[3])

        # 检查是否已经在委托中
        delegated = self.device.exists(
            march_config["check_delegated"]["template"],
            check_roi
        )
        if delegated:
            print("[AUTO_MARCH] 已经在委托中，跳过")
            return True

        # 点击自动行军
        if not self._click_template_config(march_config["enable_button"]):
            print("[AUTO_MARCH] 没找到自动行军按钮")
            return False

        # 等弹窗里的"委托"按钮出现（弹窗动画要时间，旧版 0.3s 必漏）
        delegate_tpl = march_config["delegate_button"]["template"]
        pt = None
        for _ in range(10):
            time.sleep(0.5)
            self.device.screenshot(force=True)
            pt = self.device.template_match(delegate_tpl)
            if pt:
                break
        if not pt:
            print("[AUTO_MARCH] 委托弹窗没开，放弃（不盲点关闭）")
            return False

        # 点"委托"只是选中单选框，点 X 关闭弹窗才生效
        self.device.click(pt)
        time.sleep(0.5)
        self._click_point(march_config["close_window"])
        time.sleep(1.0)

        # 验证结果
        self.device.screenshot(force=True)
        ok = self.device.exists(march_config["check_delegated"]["template"], check_roi)
        print(f"[AUTO_MARCH] 委托{'成功' if ok else '失败（标记没出现）'}")
        return ok

    def _configure_auto_march_settings(self, policy, *, switches=None) -> bool:
        from ..auto_march_settings import apply_details
        cfg = self.config["team_select"]["auto_march"]
        self.device.screenshot(force=True)
        if not self._click_template_config(cfg["enable_button"]):
            return False
        for _ in range(10):
            time.sleep(0.5)
            self.device.screenshot(force=True)
            if self.device.ocr("自动行军", roi_4to4(500, 40, 800, 125), match_mode="exact"):
                break
        else:
            return False
        detail = self.device.ocr("详细设定", roi_4to4(450, 400, 820, 510),
                              match_mode="exact")
        if not detail:
            return False
        self.device.click(detail)
        time.sleep(0.6)
        if not apply_details(self.device, policy, **({'switches': switches} if switches is not None else {})):
            return False
        # 标题已验证，此 X 只关闭详细设定。
        self._click_point([1250, 30])
        time.sleep(0.6)
        self.device.screenshot(force=True)
        if not self.device.ocr("自动行军", roi_4to4(500, 40, 800, 125), match_mode="exact"):
            return False
        self._click_point(cfg["close_window"])
        time.sleep(0.6)
        self.device.screenshot(force=True)
        return bool(self.device.template_match(cfg["enable_button"]["template"]))

    def _disable_auto_march(self) -> bool:
        """清除游戏记住的委托状态；弹窗未确认时绝不盲点关闭。"""
        cfg = self.config["team_select"]["auto_march"]
        check = cfg["check_delegated"]
        roi = roi_4to4(*check["roi"])
        self.device.screenshot(force=True)
        if not self.device.exists(check["template"], roi):
            return True
        if not self._click_template_config(cfg["enable_button"]):
            return False
        opened = False
        for _ in range(10):
            time.sleep(0.5)
            self.device.screenshot(force=True)
            if self.device.ocr("自动行军", roi_4to4(500, 40, 800, 125), match_mode="exact"):
                opened = True
                break
        if not opened:
            return False
        # “不委托”与“委托”是两个单选项，必须命中完整文字。
        point = self.device.ocr("不委托", roi_4to4(200, 100, 1100, 650),
                             match_mode="exact")
        if not point:
            self._click_point(cfg["close_window"])
            return False
        self.device.click(point)
        time.sleep(0.5)
        self._click_point(cfg["close_window"])
        time.sleep(1.0)
        self.device.screenshot(force=True)
        # 既要回到部队选择页，也要看到委托标记消失。
        return bool(self.device.template_match(cfg["enable_button"]["template"])
                    and not self.device.exists(check["template"], roi))

    # ==================== 阵形选择 ====================

    @staticmethod
    def _formation_name(name: str) -> str:
        """同时兼容旧配置中的“逆行”和标准名称“逆行阵”."""
        value = str(name or "逆行阵")
        return value if value.endswith("阵") else value + "阵"

    def choose_formation(self, formation_name: str = "逆行阵",
                         enable_auto: bool = False) -> str:
        """读取右上角状态，按需要切换自动/手动；轮到脚本选阵形时：
        手动模式固定点指定阵形；自动模式（游戏抓瞎、脚本兜底时）先认
        「有利」标记，认不出再点指定阵形。"""
        cfg = self.config.get("formation", {})
        mode = cfg.get("auto_mode", {})
        current = self._formation_mode_state(
            allow_auto_without_title=not enable_auto)
        if current is None:
            # 阵形页进场动画里，顶部红色大标题比右上角模式按钮晚渲染一两秒，
            # 刚侦测到页面就读状态会空手而归（江户城实测：页面明明卡在
            # 选择页，标题未到 → 一秒误判 failed 中止战斗）。给几秒重试。
            deadline = time.monotonic() + 5.0
            while current is None and time.monotonic() < deadline:
                time.sleep(0.5)
                self.device.screenshot(force=True)
                current = self._formation_mode_state(
                    allow_auto_without_title=not enable_auto)
            if current is None:
                return "failed"
        wanted = "auto" if enable_auto else "manual"
        toggled = False
        if current != wanted:
            self._click_point(mode.get("toggle", [910, 32]))
            time.sleep(0.6)
            toggled = True
            if wanted == "manual":
                # 拨成手动后阵型页会重绘，等红色大标题稳定两帧再点卡，
                # 否则点击全落在重绘动画上（江户城实测：双击被吞、页面卡死）。
                # 等不到说明游戏已经自动选完开打，这时再点卡面就是盲点
                # 战斗画面，如实交给调用方按"游戏自动阵形"处理。
                if not self._wait_formation_title_stable():
                    return "auto"
        if enable_auto:
            if toggled:
                # 游戏的“自动阵形”开关只对下一次阵形选择生效：当前这张
                # 已经打开的手动选择页仍要点完一张阵形卡。旧逻辑在拨开关
                # 后立刻返回 auto，江户城会把仍停着的“鱼鳞阵”页面误当成
                # 已经开战，随后白等战果。这里用标题 + 阵形名 OCR 连续确认
                # 当前页仍在；只有页面真的消失且自动标志出现才交给游戏。
                if not self._wait_formation_selection_present():
                    auto_took_over = self._formation_mode_state(
                        allow_auto_without_title=True) == "auto"
                    return "auto" if auto_took_over else "failed"
            # 已经是自动、选择页却还挂着：索敌失败时敌方阵形「不明」，
            # 游戏没东西可选会卡在页面上等手动；或刚从手动拨到自动、当前
            # 这一场仍归手动处理。两种情况都先认“有利”，认不出再点兜底。
            point = self.device.template_match(
                cfg.get("advantage_template", "battle/ui有利.png"),
                threshold=0.7,
            )
            if point:
                self.device.click(Point(point.x, point.y))
                time.sleep(0.6)
                self.device.click(Point(point.x, point.y))
                return "advantage"

        return ("fixed" if self.select_formation(
            self._formation_name(formation_name), verified=True) else "failed")

    def _wait_formation_title_stable(self, timeout_s: float = 6.0,
                                     stable_hits: int = 2) -> bool:
        """等阵形选择页连续命中，确认页面真加载完了。

        进战斗/切模式时页面有进场和重绘动画，单帧命中或未命中都可能是
        动画中间态。优先认顶部标题；标题闪烁时再用“鱼鳞阵”等阵形名
        OCR 兜底，避免把仍停着的选择页误判成已经开战。
        """
        hits = 0
        attempts = max(stable_hits, int(max(0.4, timeout_s) / 0.4))
        for _ in range(attempts):
            self.device.screenshot(force=True)
            if self._formation_selection_visible():
                hits += 1
                if hits >= stable_hits:
                    return True
            else:
                hits = 0
            time.sleep(0.4)
        return False

    def _formation_selection_visible(self) -> bool:
        """标题或任一阵形名出现，都说明当前选择页仍未处理完。

        OCR 只在标题模板未命中时启用，而且只检查阵形选择可能出现的区域；
        这条门闩只用于已经确认进入阵形流程的窄上下文，不会拿战斗中的
        右上角自动标志单独判断页面。
        """
        formation = self.config.get("formation", {})
        verify = formation.get("verify", {})
        if self.device.template_match(
                verify.get("template", "battle/ui阵形选择.png"),
                roi_4to4(*verify.get("roi", [571, 5, 707, 44]))):
            return True

        ocr_all = getattr(self.device, "ocr_all", None)
        if not callable(ocr_all):
            return False
        try:
            roi = roi_4to4(*formation.get(
                "name_ocr_roi", [0, 80, 1280, 720]))
            tokens = ocr_all(roi) or []
        except Exception:
            return False

        names = [self._formation_name(name)
                 for name in formation.get("formations", {})]
        for text, _point in tokens:
            compact = "".join(str(text or "").split())
            for name in names:
                stem = name[:-1] if name.endswith("阵") else name
                expected = stem if len(stem) >= 2 else name
                if expected and expected in compact:
                    return True
        return False

    def _wait_formation_selection_present(self, attempts: int = 8,
                                          stable_hits: int = 2) -> bool:
        """拨动模式后，确认当前阵形选择页仍连续存在。"""
        hits = 0
        for _ in range(max(stable_hits, attempts)):
            self.device.screenshot(force=True)
            if self._formation_selection_visible():
                hits += 1
                if hits >= stable_hits:
                    return True
            else:
                hits = 0
            time.sleep(0.4)
        return False

    def _wait_formation_selection_departed(self, attempts: int = 10,
                                           stable_misses: int = 2) -> bool:
        """点阵形后要求标题和阵形名连续消失，才算真正开战。"""
        misses = 0
        for _ in range(max(stable_misses, attempts)):
            self.device.screenshot(force=True)
            if self._formation_selection_visible():
                misses = 0
            else:
                misses += 1
                if misses >= stable_misses:
                    return True
            time.sleep(0.4)
        return False

    def _formation_mode_state(self, allow_auto_without_title: bool = False):
        """先确认阵形选择页，再读取右上角当前模式。"""
        formation = self.config.get("formation", {})
        verify = formation.get("verify", {})
        verify_template = verify.get("template", "battle/ui阵形选择.png")
        verify_roi = roi_4to4(*verify.get("roi", [571, 5, 707, 44]))
        # 右上角模式按钮在整场战斗中常驻，不能单独作为阵形页判据。
        # 只有顶部红色“阵形选择”标题出现时，才允许切模式或点击阵形卡。
        mode = formation.get("auto_mode", {})
        roi = roi_4to4(*mode.get("roi", [840, 0, 980, 68]))
        auto_template = mode.get("auto_template", "battle/阵形选择自动.png")
        manual_template = mode.get("manual_template", "battle/阵形选择手动.png")
        threshold = float(mode.get("threshold", 0.9))
        title_visible = bool(self.device.template_match(verify_template, verify_roi))
        # 两张图的上半截完全相同，实测整图相关度约 0.879；旧阈值 0.7 会让
        # “手动”继续命中“自动”，于是脚本在战斗中反复拨动按钮。
        if self.device.template_match(auto_template, roi, threshold=threshold) and (
                title_visible or allow_auto_without_title):
            # 自动阵形会跳过选择页，因此切回手动时只能抓住常驻的自动状态按钮。
            # 调用方仅在“想要手动”时开启此例外；想保持自动时不会在战斗中反复处理。
            return "auto"
        if title_visible and self.device.template_match(
                manual_template, roi, threshold=threshold):
            return "manual"
        return None

    def select_formation(self, formation_name: str, verified: bool = False) -> bool:
        """
        选择阵形

        Args:
            formation_name: 阵形名称，如 "鱼鳞阵", "雁行阵"

        Returns:
            是否选择成功
        """
        formation_name = self._formation_name(formation_name)
        formation_config = self.config.get("formation", {})

        # 必须同时看到顶部阵形页标题和右上角模式状态。
        if not verified and self._formation_mode_state() is None:
            print("[ERROR] 不在阵形选择界面")
            return False

        # 选择阵形
        formations = formation_config.get("formations", {})
        if formation_name in formations:
            target = formations[formation_name]
            if not formation_config.get("double_click"):
                self._click_point(target)
                time.sleep(0.3)
                return True

            # 点两下同一张阵形卡：第一下选中、第二下即确定（和部队选择同一交互，
            # 选中不联网、确定才发送）。不再戳卡内"确定"热点的偏移坐标——
            # 旧偏移实测擦着按钮下沿点空，阵形页不走导致整场卡死。
            # 两下之间留 0.6s 给页面响应：快速双击会被当成一下吞掉（老大实测）。
            # 每轮点完验证页面是否离开，要求连续两帧都看不到阵形页才算真走——
            # 单帧未命中可能只是页面还在动画里（江户城实测误判翻车）。
            # 三轮还不走如实报错，交给调用方停止这场，绝不盲信一次必中。
            for _ in range(3):
                self._click_point(target)
                time.sleep(0.6)
                self._click_point(target)
                time.sleep(0.8)
                if self._wait_formation_selection_departed():
                    return True
            print(f"[ERROR] 阵形 {formation_name} 点两下后仍未离开选择页")
            return False

        print(f"[ERROR] 未知阵形: {formation_name}")
        return False

    def _return_home_from_march(self, cfg) -> bool:
        """在行军选择/停止画面安全返回本丸，并等待本丸真正出现。

        长时间挂机后游戏回本丸偶尔会卡在加载过渡几十秒。这里必须等到本丸
        标志实际出现，不能只因点击了确认按钮就把后续盘点放出去。
        """
        stop_btn = None
        for _ in range(6):
            self.device.screenshot(force=True)
            # 弹窗会透出背景的返程按钮，先消除重伤确认，不能点穿它。
            self._deny_heavy_injury_warning(cfg)
            observer = getattr(self, "_watch_battle_frame", None)
            if callable(observer):
                observer()  # 有行军观察器的宿主继续记录本帧。
            stop_btn = self.device.template_match(cfg["march_stop_button"]["template"])
            if stop_btn:
                break
            time.sleep(0.5)
        if not stop_btn:
            return False
        self.device.click(stop_btn)
        time.sleep(1.5)
        self.device.screenshot(force=True)
        text = self._battle_dialog_text()
        if self._is_heavy_injury_dialog(text):
            self._deny_heavy_injury_warning(cfg)
            return False
        # 只准确认返程；孤立的“是”也可能是在同意重伤进军。
        if "返回本丸" in text or "回到本丸" in text:
            yes = self._battle_dialog_button("是")
            if yes is None:
                return False
            self.device.click(yes)
        elif not self.device.template_match(cfg["home_ui"]["template"]):
            return False
        time.sleep(2.0)
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            self.device.screenshot(force=True)
            if self.device.template_match(cfg["home_ui"]["template"]):
                return True
            time.sleep(0.8)
        return False
