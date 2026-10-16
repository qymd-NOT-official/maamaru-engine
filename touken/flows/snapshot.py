# -*- coding: utf-8 -*-
"""
上层业务：库存快照——日课收尾优先同步游戏数据，缺字段时读画面补齐

抓什么：
  锻刀界面：顶栏整条 木炭/玉钢/冷却材/砥石/甲州金（按 x 顺序认）
            右栏 委托符/加速符·极/所持刀剑
            三炉 完成/空闲/锻造中（剩多少时间）
  所持道具界面：右上「所持小判」（真小判只在这看，顶栏那个是甲州金！）

落盘 status/inventory.json，看板的读取任务自己算剩余时间。
写坏了不影响日课（调用方自己兜 try）。

坐标（真机校准）：
  顶栏条 (400,5,1100,52)，数字按 x 升序
  委托符 (1145,355,1255,390) 加速符 (1145,510,1255,550) 所持 (840,45,1080,85)
  炉行心 y [205,345,475]，状态/倒计时框 (150,cy-55,780,cy+55)
  所持小判 (940,20,1210,80) —— 目录→所持道具界面右上，读出 "672,416 枚"
"""

import json
import re
import time
from pathlib import Path

from ..runtime_paths import STATUS_DIR

from ..maa_adapter import roi_4to4
from ..game_state import available_events, resource_facts, fresh_resources, RESOURCE_NAMES, forge_capacity, furnace_facts

_STATUS_DIR = STATUS_DIR
_SLOT_CY = [205, 345, 475]
_RES_NAMES = ["木炭", "玉钢", "冷却材", "砥石", "甲州金"]


def _to_int(text: str):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _parse_furnace_remain(text: str):
    """从炉行 OCR 文本里抠倒计时（"HH:MM:SS"）。

    OCR 读小字容易把冒号吃掉（9-04 冤案：三炉同时点火，只读出一只的时间），
    先认带冒号的，再退到连续 5~6 位数字。分/秒超 59 的匹配是杂物，扔掉。
    """
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})\s*[:：]\s*(\d{2})", text or "")
    if not m:
        m = re.search(r"(?<!\d)(\d{1,2})\s?(\d{2})\s?(\d{2})(?!\d)", text or "")
    if not m:
        return None
    hours, minutes, seconds = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if minutes > 59 or seconds > 59:
        return None
    return f"{hours}:{m.group(2)}:{m.group(3)}"


class SnapshotMixin:
    """库存快照。依赖宿主类的 navigate_to_stream、maa。"""

    def status_snapshot_stream(self, phase=None):
        """
        流式库存快照：游戏数据优先，缺数据才导航盘点，沿用 status/inventory.json

        Yields:
            str: 执行状态消息
        """
        events = available_events(self.maa)
        resources = fresh_resources(resource_facts(events))
        if RESOURCE_NAMES.issubset(resources):
            capacity = forge_capacity(events)
            yield from self._save_inventory(resources, f"{capacity[0]}/{capacity[1]}" if capacity else None,
                                            furnace_facts(events) or [], phase)
            return
        # 收工回城可能在长时间挂机后加载几十秒。即使战斗流程自己的回城
        # 确认先超时了，盘点也要独立等到目录真正可用，不能马上撞导航失败。
        if phase == "after":
            yield "[快照] 等待本丸加载完成、目录恢复可用..."
            ready = False
            deadline = time.monotonic() + 90.0
            last_notice = time.monotonic()
            while time.monotonic() < deadline:
                self.maa.screenshot(force=True)
                if (self.maa.exists("menu/ui目录.png")
                        or self.maa.exists("目录.png", threshold=0.7)):
                    ready = True
                    break
                if time.monotonic() - last_notice >= 15.0:
                    yield "[快照] 游戏仍在回城加载，继续等待..."
                    last_notice = time.monotonic()
                time.sleep(1.0)
            if not ready:
                yield "[快照] 等待 90 秒后目录仍不可用，取消本次收工盘点"
                return

        yield "[快照] 正在导航到锻刀..."
        for nav_msg in self.navigate_to_stream("锻刀"):
            yield nav_msg
        if self.current_location != "锻刀":
            yield "[快照] 到达锻刀失败"
            return
        for msg in self._capture_inventory(phase):
            yield msg

    def _capture_inventory(self, phase=None):
        """
        家底拍摄核心：人已经站在锻刀页面上时直接调（零导航）。
        锻刀步骤收工时被顺手调用，免了专程跑一趟（7-27 日课超时两次的教训）。
        """
        events = available_events(self.maa)
        resources = fresh_resources(resource_facts(events))
        if RESOURCE_NAMES.issubset(resources):
            capacity = forge_capacity(events)
            yield from self._save_inventory(resources, f"{capacity[0]}/{capacity[1]}" if capacity else None,
                                            furnace_facts(events) or [], phase)
            return
        time.sleep(1.5)
        self.maa.screenshot(force=True)

        # ---- 顶栏五资源：整条 OCR，数字按 x 升序对应 木炭/玉钢/冷却材/砥石/甲州金 ----
        tokens = [] if set(_RES_NAMES).issubset(resources) else self.maa.ocr_all(roi_4to4(400, 5, 1100, 52))
        nums = sorted(
            ((t, pt.x) for t, pt in tokens if re.fullmatch(r"[\d,]+", t)),
            key=lambda p: p[1])
        for i, name in enumerate(_RES_NAMES):
            if name not in resources:
                resources[name] = _to_int(nums[i][0]) if len(nums) == 5 else None
        if len(nums) < 5 and any(resources.get(name) is None for name in _RES_NAMES):
            yield f"[快照] 顶栏只读出 {len(nums)}/5 个数字，部分资源缺失"

        # ---- 右栏：委托符/加速符/所持 ----
        def first_number(roi):
            ts = self.maa.ocr_all(roi_4to4(*roi))
            for t, _ in ts:
                v = _to_int(t)
                if v is not None:
                    return v
            return None

        if "委托符" not in resources:
            resources["委托符"] = first_number((1145, 355, 1255, 390))
        if "加速符" not in resources:
            resources["加速符"] = first_number((1145, 510, 1255, 550))

        capacity = forge_capacity(events)
        doko = f"{capacity[0]}/{capacity[1]}" if capacity else None
        ts = [] if doko is not None else self.maa.ocr_all(roi_4to4(840, 45, 1080, 85))
        # OCR 会在数字间吃出杂物（"200./200"），允许斜杠两边夹非数字
        m = re.search(r"(\d+)\D{0,3}/\D{0,2}(\d+)", "".join(t for t, _ in ts))
        if m and doko is None:
            doko = f"{m.group(1)}/{m.group(2)}"

        # ---- 三炉状态 ----
        furnaces = []
        game_furnaces = furnace_facts(events)
        for i, cy in (() if game_furnaces is not None else enumerate(_SLOT_CY, 1)):
            ts = self.maa.ocr_all(roi_4to4(150, cy - 55, 780, cy + 55))
            text = "".join(t for t, _ in ts)
            if "完成" in text:
                furnaces.append({"slot": i, "state": "完成", "remain": None})
            elif "空闲" in text:
                furnaces.append({"slot": i, "state": "空闲中", "remain": None})
            else:
                remain = _parse_furnace_remain(text)
                furnaces.append({"slot": i, "state": "锻造中" if remain else "未知",
                                 "remain": remain})

        # ---- 所持道具界面读真小判（顶栏那个是甲州金，真小判只在这看）----
        koban = resources.get("小判")
        if koban is None:
            koban = self._read_koban()
        if koban is not None:
            resources["小判"] = koban
        else:
            yield "[快照] 小判读取失败（不影响其他数据）"

        yield from self._save_inventory(resources, doko, game_furnaces if game_furnaces is not None else furnaces, phase)

    def _save_inventory(self, resources, doko, furnaces, phase):
        self._last_full_snapshot_complete = resources.get("小判") is not None
        payload = {
            "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "phase": phase,
            "resources": resources,
            "doko": doko,
            "furnaces": furnaces,
        }
        _STATUS_DIR.mkdir(exist_ok=True)
        (_STATUS_DIR / "inventory.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        if hasattr(self, "record_event"):
            self.record_event("inventory.captured", **payload)

        yield (f"[快照] 落盘：小判{resources.get('小判')} 甲州金{resources.get('甲州金')} "
               f"委托符{resources.get('委托符')} 加速符{resources.get('加速符')} 所持{doko} "
               f"炉:{'/'.join(f['state'] for f in furnaces)}")

    def _read_koban(self):
        """目录→所持道具→右上 OCR 所持小判。失败返回 None，绝不抛异常。"""
        known = fresh_resources(resource_facts(available_events(self.maa))).get("小判")
        if known is not None:
            return known
        opened_items = False
        try:
            # 打开目录
            if not self._open_menu():
                return None
            time.sleep(1.0)
            self.maa.screenshot(force=True)
            pt = self.maa.template_match("menu/目录_所持道具.png")
            if not pt:
                return None
            opened_items = True
            self.maa.click(pt)
            time.sleep(2.5)
            self.maa.screenshot(force=True)
            # 右上「所持小判 672,416 枚」：取最长数字串
            # 页面模板偶尔会因活动背景变化漏掉；小判栏本身的限定 OCR 才是
            # 真正的数据门闩。无论最终读没读到，finally 都负责退出该页面。
            tokens = self.maa.ocr_all(roi_4to4(940, 20, 1210, 80))
            best = None
            for t, _ in tokens:
                v = _to_int(t)
                if v is not None and (best is None or v > best):
                    best = v
            return best
        except Exception as e:  # noqa: BLE001 - 快照兜底，任何错都不许炸日课
            print(f"[快照] 小判读取异常: {e}")
            return None
        finally:
            if opened_items:
                # 进过所持道具就必须退出来。旧逻辑在 ui所持.png 漏识别时直接
                # return，把整张所持页面留给后续刀解/合成/出阵，造成连环翻车。
                try:
                    from ..maa_adapter import Point
                    self.maa.click(Point(1248, 35))
                    time.sleep(0.8)
                except Exception as cleanup_error:  # noqa: BLE001
                    print(f"[快照] 退出所持道具异常: {cleanup_error}")
                finally:
                    # _open_menu() 会把位置记成通用入口；进入并退出所持后该状态
                    # 已经过期，清掉后下一步会重新确认并打开目录。
                    self.current_location = None

    # ---------- 顺路顶栏快照（零导航） ----------

    PEEK_INTERVAL_S = 60  # 节流：一分钟最多拍一次

    def quick_peek(self, tag: str = "", force: bool = False, *, data_only: bool = False) -> bool:
        """
        顺路顶栏快照：游戏几乎每个画面顶部都挂着四大资源+甲州金，
        循环巡逻时顺手 OCR 一下，零导航、不打印日志，拍完合并进
        status/inventory.json（只更新顶栏五资源，保留完整快照的其它字段）。

        60 秒节流（force=True 时绕开，用于任务收尾的刻意观察点）；
        顶栏数字读出少于 4 个就当这帧不算数。
        永不抛异常——顺手活绝不能耽误正事。
        data_only 用于任务收尾：只合并已有游戏资源数据，不调用画面识别。
        """
        try:
            now = time.monotonic()
            if not force and now - getattr(self, "_last_peek", 0.0) < self.PEEK_INTERVAL_S:
                return False
            known = fresh_resources(resource_facts(available_events(self.maa)))
            tokens = [] if data_only or set(_RES_NAMES).issubset(known) else self.maa.ocr_all(roi_4to4(400, 5, 1100, 52))
            nums = sorted(
                ((t, pt.x) for t, pt in tokens if re.fullmatch(r"[\d,]+", t)),
                key=lambda p: p[1])
            if len(nums) != 5 and not known:
                return False
            self._last_peek = now

            peeked = dict(known)
            for i, name in enumerate(_RES_NAMES):
                if len(nums) == 5 and name not in peeked:
                    v = _to_int(nums[i][0])
                    if v is not None:
                        peeked[name] = v
            if not peeked:
                return False

            path = _STATUS_DIR / "inventory.json"
            try:
                old = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                old = {}
            resources = dict(old.get("resources") or {})
            resources.update(peeked)
            payload = dict(old)
            payload["resources"] = resources
            payload["peek_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _STATUS_DIR.mkdir(exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            if hasattr(self, "record_event"):
                self.record_event("inventory.peek", tag=tag, **peeked)
            return True
        except Exception:
            return False
