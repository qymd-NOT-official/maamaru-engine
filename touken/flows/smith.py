# -*- coding: utf-8 -*-
"""
上层业务：锻刀 + 刀解（俩界面挨着，互相救场，放一起）

锻刀规矩（用户亲授）：
  1. 配方默认 700×4（配置 forge.recipe 或面板字段可改，点火前自动在
     配比页数字键盘设值，账目跟着配方走），加速符不勾（省着）
  2. 有"完成"的炉子顺手收刀；刀位满了会蹦氪金弹窗——
     关掉，去刀解一把白名单腾出位置，再回来收
  3. 每日锻 3 次做日课

限锻十连规矩（2026-09-11 真机科研，限锻赌刀专用，与日常锻刀截然相反）：
  1. 状况页空闲炉行内「十连锻刀」→ 配比页十连模式，勾「使用加速符」
  2. 一发十连 = 委托符 -9（十连优惠）+ 加速符 -10 + 配方×10 四资源，
     瞬间完成，十把刀直接进刀位；加速符不可再生，跑前必须核库存
  3. 每发的金色结算榜（5×2 卡牌）整版 OCR 认人，竖排刀名实测 10/10；
     揭示动画有时攒着等离开页面才播，收尾退出时一路快进+读榜

刀解规矩（白名单模式）：
  1. 只解白名单里的不稀有刀（任务奖励加速符），每天一把
  2. 保护（上锁）的刀界面里根本不显示，天然安全
  3. 选中白名单 → 大刀解（灰变蓝）→ 二次确认点确认

坐标（真机校准）：
  锻刀状况三炉：y心 [208,334,460]，炉按钮 x[160,371]，锻刀按钮 x[794,909]
  配比界面：锻刀开始 (1146,608)，标题 OCR「锻刀资源投入」
  刀解选择：行顶 147 起、行距 96，选择按钮 (1046, 行顶+48)
  大刀解 (1200,615)，确认弹窗 确认 (785,630)
"""

import json
import re
import subprocess
import time
from pathlib import Path

from ..runtime_paths import STATUS_DIR

from ..device_adapter import roi_4to4, Point
from .. import sword_db

_SLOT_CY = [205, 345, 475]
_ROW_TOP = 147
_ROW_PITCH = 96
_VISIBLE_ROWS = 6
# 刀名位于行底部的独立文字带；向上会读到头像，向右会混入「疲劳/生存」。
_NAME_ROI_X1 = 120
_NAME_ROI_X2 = 360
_NAME_ROI_DY1 = 68
_NAME_ROI_DY2 = 98

_STATUS_DIR = STATUS_DIR
_FLAGS_PATH = _STATUS_DIR / "daily_flags.json"

# 点火配方四资源的名称（顺序固定，对应配置 forge.recipe 的四个数）
_FORGE_RES = ("木炭", "玉钢", "冷却材", "砥石")
_DEFAULT_RECIPE = [700, 700, 700, 700]
# 选中一把刀后，首行切成四资源收益预览；这些 ROI 来自
# DeviceAdapter.screenshot(force=True) 的 1280×720 MuMu 同源运行帧。
_DISMANTLE_RESOURCE_ROIS = (
    (590, 196, 635, 235),
    (730, 196, 775, 235),
    (870, 196, 915, 235),
    (1010, 196, 1055, 235),
)


def _mark_dismantled_today():
    """今天已经刀解过了（锻刀收刀腾位置也会解）——日课的刀解步据此跳过"""
    try:
        _STATUS_DIR.mkdir(exist_ok=True)
        _FLAGS_PATH.write_text(json.dumps(
            {"date": time.strftime("%Y-%m-%d"), "dismantled": True},
            ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def dismantled_today() -> bool:
    """今天是否已经成功刀解过（任何途径：日课、锻刀腾位置、手动单跑）"""
    try:
        d = json.loads(_FLAGS_PATH.read_text(encoding="utf-8"))
        return d.get("date") == time.strftime("%Y-%m-%d") and bool(d.get("dismantled"))
    except Exception:
        return False


def dismantled_today_in_log(device):
    """今天的游戏记录里有没有刀解成功响应——玩家手动刀解也走
    /sword/dismantle_many，所以这个信号覆盖脚本没经手的场景。

    True=今天解过；False=拿到数据但没解过；None=没有数据（回退旧标记）。
    日期口径与 dismantled_today 一致用自然日，不擅自改成游戏日。
    """
    from ..game_state import available_events, successful_body
    from ..youzu_log import _event_epoch
    events = available_events(device)
    if not events:
        return None
    today = time.strftime("%Y-%m-%d")
    for event in events:
        if event.get('endpoint') != '/sword/dismantle_many':
            continue
        epoch = _event_epoch(event)
        if (epoch is None
                or time.strftime("%Y-%m-%d", time.localtime(epoch)) != today):
            continue
        if event.get('direction') == 'S->C' and successful_body(event):
            return True
    return False

# 刀解白名单（用户给的，不稀有的刀）
DISMANTLE_WHITELIST = [
    "山姥切国广", "陆奥守吉行", "宗三左文字", "加州清光", "歌仙兼定",
    "乱藤四郎", "小夜左文字", "五虎退", "秋田藤四郎", "药研藤四郎",
    "大和守安定", "鸣狐", "蜂须贺虎彻", "前田藤四郎", "今剑",
    "笑面青江", "鲶尾藤四郎", "骨喰藤四郎", "山伏国广", "狮子王",
    "大俱利伽罗", "烛台切光忠", "同田贯正国", "和泉守兼定",
]


def _dur_to_sec(s):
    """'3:20:00' / '03：20：00' → 秒数。认不出来返回 None"""
    m = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$", s or "")
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


class SmithMixin:
    """锻刀+刀解。依赖宿主类的 navigate_to_stream、device。"""

    # ==================== 锻刀 ====================

    def forge_stream(self, times: int = 3, watch: list | None = None,
                     recipe: list | None = None):
        """
        流式锻刀：收完成的炉，给空闲炉点火，一天锻 times 次

        Args:
            times: 点几炉
            watch: 目标时长清单（限锻刀的时间身份证，如 ["03:20:00"]）。
                   点火后倒计时命中（±90秒，倒计时会走字）就报喜+手机推送
            recipe: 点火配方 [木炭,玉钢,冷却材,砥石]，None 读配置 forge.recipe

        Yields:
            str: 执行状态消息
        """
        recipe = self._forge_recipe(recipe)
        watch_secs = {s for s in (_dur_to_sec(w) for w in (watch or [])) if s}
        if watch_secs:
            yield f"[锻刀] 🎯 盯梢目标时长：{'、'.join(watch or [])}"
        yield "[锻刀] 正在导航到锻刀..."
        for nav_msg in self.navigate_to_stream("锻刀"):
            yield nav_msg
        if self.current_location != "锻刀":
            yield "[锻刀] 到达锻刀失败"
            return
        time.sleep(1.0)

        forged = 0
        slot_durations = {}  # 炉号 → 点火时的倒计时文本（本轮点火的才有；跨轮收的炉时长在昨天的 forge.started 里）
        # 刀位满时收一刀要烧 2 次循环（收失败→刀解腾位→再收），上限按双倍成本留余量
        for attempt in range(times * 2 + 4):
            if forged >= times:
                break
            self.device.screenshot(force=True)
            action = self._scan_slots()
            if action is None:
                break
            kind, cy = action

            if kind == "完成":
                yield f"[锻刀] 收一炉（y={cy}）"
                collected, sword = self._collect_slot(cy)
                if not collected:
                    # 刀位满了：刀解一把白名单腾位置再收
                    yield "[锻刀] 刀位满了！去刀解一把白名单腾位置..."
                    freed = False
                    for msg in self.dismantle_stream(max_dismantle=1, _from_forge=True):
                        yield msg
                        if "分解完成" in msg:
                            freed = True
                    if not freed:
                        yield "[锻刀] 刀解腾位置失败，这炉先不收"
                        break
                    for nav_msg in self.navigate_to_stream("锻刀"):
                        yield nav_msg
                    time.sleep(1.0)
                else:
                    if sword:
                        yield f"[锻刀] 🎉 收到【{sword['name']}】"
                    if hasattr(self, "record_event"):
                        slot_no = _SLOT_CY.index(cy) + 1
                        payload = {"slot": slot_no}
                        if sword:
                            payload.update(sword)
                        duration = slot_durations.pop(slot_no, None)
                        if duration:
                            payload["duration"] = duration
                        self.record_event("forge.collected", **payload)
                continue

            if kind == "空闲中":
                yield f"[锻刀] 给炉子点火（第 {forged + 1}/{times} 炉）"
                if self._start_forge(cy, recipe):
                    forged += 1
                    slot_no = _SLOT_CY.index(cy) + 1
                    countdown = self._read_countdown(cy)
                    hit = self._watch_hit(countdown, watch_secs)
                    if countdown:
                        slot_durations[slot_no] = countdown[0]
                    if hasattr(self, "record_event"):
                        started_payload = {"slot": slot_no, "sequence": forged,
                                           "target_hit": hit}
                        # 配方进事件：锻刀手记的配方展示/成本估算全靠它
                        # （曾只记 slot，履历配方全是 ?/?/?/?）
                        for key, value in zip(("charcoal", "steel", "coolant", "file"),
                                              self._forge_recipe(recipe)):
                            started_payload[key] = value
                        if countdown:
                            started_payload["duration"] = countdown[0]
                            started_payload["duration_secs"] = countdown[1]
                        started_id = self.record_event("forge.started",
                                                       **started_payload)
                        self._emit_forge_costs(started_id, recipe)
                    if hit:
                        yield f"[锻刀] 🎉🎉🎉 喜报！这炉倒计时 {hit}，目标时长命中！快去看！"
                        try:
                            from ..notify import notify
                            # 标题必须 ASCII（http.client 按 latin-1 编码头，中文/emoji 会静默发不出去）
                            notify(f"锻刀命中目标时长 {hit}！快去看炉子",
                                   title="Forge Hit!", tags="tada,sword")
                        except Exception:
                            pass
                else:
                    yield "[锻刀] 点火失败，停"
                    break
                continue

        yield f"[锻刀] 收工：点了 {forged} 炉"
        # 人已经在锻刀页面了，顺手把家底拍了（完整快照含小判，零额外导航）。
        # 拍歪了也不耽误正事：顶栏数字不够 4 个 _capture_inventory 自己会吱声。
        for msg in self._capture_inventory(phase="forge"):
            yield msg

    def _scan_slots(self):
        """扫三炉状态，返回 (状态, y心)。优先收完成，其次点空闲。
        框要罩住整行：完成/空闲的字在左，锻造中显示剩余时间"""
        idle = None
        for cy in _SLOT_CY:
            tokens = self.device.ocr_all(roi_4to4(150, cy - 55, 780, cy + 55))
            text = "".join(t for t, _ in tokens)
            if "完成" in text:
                return ("完成", cy)
            if "空闲" in text and idle is None:
                idle = ("空闲中", cy)
        return idle

    def _collect_slot(self, cy: int) -> tuple:
        """收一炉，返回 (是否收成功, 认出的刀 or None)。刀位满弹氪金窗则 (False, None)。
        获得动画要轮询着点穿：一口气连点会顺手把下一个完成的炉也开了，
        后面的步骤全踩在动画上（血泪）

        认人全程走游戏日志（_sword_from_forge_log）：获得画面的刀名是竖排
        书法字，OCR 检出率趋近于零（历史上只认出过 1 次），/forge/complete
        响应直接带 sword_id，数据走 JSON 不养笨蛋眼珠子。"""
        slot_no = _SLOT_CY.index(cy) + 1
        clicked_at = time.time()
        self.device.click(Point(265, cy))
        time.sleep(2.5)
        for _ in range(10):
            self.device.screenshot(force=True)
            # 刀位满的氪金弹窗特征（先查它，弹窗后面还露着状况标题）
            tokens = self.device.ocr_all(roi_4to4(200, 150, 1080, 550))
            text = "".join(t for t, _ in tokens)
            if "刀位" in text or "所持数" in text or "购买详情" in text:
                pt = self.device.template_match("通用_关闭.png", threshold=0.7)
                self.device.click(pt if pt else Point(1062, 70))
                time.sleep(1.5)
                return (False, None)
            if self.device.ocr("锻刀状况", roi_4to4(400, 45, 880, 110)):
                return (True, self._sword_from_forge_log(slot_no, clicked_at))
            # 安全点：点穿获得动画；回到状况界面这点是空地，点不坏
            self.device.click(Point(290, 600))
            time.sleep(1.5)
        return (True, self._sword_from_forge_log(slot_no, clicked_at))  # 超时也当收了，别卡死

    def _sword_from_forge_log(self, slot_no, clicked_at):
        """收炉认人走游戏落盘日志：/forge/complete 响应直接带 sword_id。
        取点击之后同炉位最新一条；认不到返回 None（手记记「结果未记录」，
        不编）。
        """
        try:
            from ..game_state import forge_completions
            hits = [h for h in forge_completions(
                self._forge_events(), after=clicked_at - 300)  # 时钟偏差余量
                    if h["slot_no"] == slot_no]
        except Exception:
            return None
        if not hits:
            return None
        hit = hits[-1]
        found = sword_db.find_game_sword(hit["sword_id"])
        if not found:
            return None
        sid, info, _form = found
        return {"sword_id": sid,
                "name": info.get("name_zh") or info["name"],
                "name_jp": info["name"],
                "serial_id": hit["serial_id"],
                "is_first_get_sword": hit["is_first_get_sword"]}

    def _start_forge(self, cy: int, recipe: list | None = None) -> bool:
        """给空闲炉点火：进配比界面 → 设配方 → 点锻刀 → 等回到状况界面"""
        self.device.click(Point(850, cy))  # 该行锻刀按钮
        time.sleep(2.5)
        self.device.screenshot(force=True)
        if not self.device.ocr("锻刀资源投入", roi_4to4(400, 45, 880, 110)):
            return False
        # 点火前把配比设成配置配方；游戏会记住上次值，一致时自动跳过
        if not self._apply_recipe(recipe):
            return False
        from ..game_state import begin_operation, confirm_operation
        boundary = begin_operation(self.device)
        boundary.update(forge_slot=_SLOT_CY.index(cy) + 1,
                        forge_recipe=self._forge_recipe(recipe))
        self.device.click(Point(1146, 608))  # 锻刀（不勾加速符）
        # 过场与确认弹窗按原流程处理，随后统一核对游戏结果
        returned = False
        for _ in range(12):
            time.sleep(1.5)
            self.device.screenshot(force=True)
            if self.device.ocr("锻刀状况", roi_4to4(400, 45, 880, 110)):
                returned = True
                break
            # 限锻期间拦一道确认窗：「当前的资源数无法增加显现积分。
            # 是否进行锻刀？」（issue#7 用户现场）。窗不关，状况页回不来，
            # 后面的刀解/合成也全被它挡死——认出就点【是】继续锻。
            # 「今日不再提醒」勾选框不动，那是用户自己的偏好。
            if self.device.ocr("是否进行锻刀", roi_4to4(300, 200, 980, 500)):
                yes = self.device.ocr("是", roi_4to4(400, 300, 880, 620),
                                   match_mode="exact")
                if yes:
                    self.device.click(yes)
                    time.sleep(1.0)
                    continue
        outcome = confirm_operation(self.device, boundary, "forge", timeout=5.0)
        if outcome is not None:
            return outcome
        # Returning to the page alone is not proof; the chosen furnace must be running.
        countdown = self._read_countdown(cy) if returned else None
        return bool(countdown and countdown[1] > 0)

    def _forge_recipe(self, override: list | None = None) -> list:
        """点火配方：运行时覆盖 > 配置 forge.recipe（顺序 木炭/玉钢/冷却材/砥石），
        缺省/配置坏了回落 700×4。合法范围 10~999（配比键盘是三位数）"""
        for recipe in (override,
                       ((getattr(self, "config", None) or {}).get("forge") or {})
                       .get("recipe")):
            if (isinstance(recipe, list) and len(recipe) == 4
                    and all(isinstance(v, (int, float)) and 10 <= v <= 999
                            for v in recipe)):
                return [int(v) for v in recipe]
        return list(_DEFAULT_RECIPE)

    # ---- 配比键盘（2026-09-11 真机科研）：点行内数字区弹三位数字键盘，
    # 敲键写入高亮位并自动前进（百→十→个→绕回），「输入」确认。
    # X 取消实测点不掉，所以写值永远三位全敲覆盖，不依赖原值。----
    _RECIPE_ROW_Y = dict(zip(_FORGE_RES, (221, 353, 486, 577)))
    _KEYPAD = {"1": (506, 273), "2": (640, 273), "3": (768, 273),
               "4": (506, 352), "5": (640, 352), "6": (768, 352),
               "7": (506, 433), "8": (640, 433), "9": (768, 433),
               "0": (640, 512)}
    _KEYPAD_CONFIRM = (640, 602)  # 「输入」

    def _read_recipe_row(self, name: str):
        """OCR 读配比页某行的当前三位数；读不出返回 None（宁可重设也不猜）"""
        y = self._RECIPE_ROW_Y[name]
        tokens = self.device.ocr_all(roi_4to4(690, y - 45, 910, y + 45))
        m = re.search(r"\d{1,3}", "".join(str(t) for t, _ in tokens))
        return int(m.group()) if m else None

    def _set_forge_row(self, name: str, value: int) -> bool:
        """点开某行的数字键盘，三位全敲后确认。任何一步对不上都算失败。"""
        self.device.click(Point(700, self._RECIPE_ROW_Y[name]))
        time.sleep(1.5)
        self.device.screenshot(force=True)
        if not self.device.ocr(name, roi_4to4(400, 150, 900, 240)):
            return False  # 键盘没开 / 开错行，别乱敲
        for d in f"{value:03d}":
            self.device.click(Point(*self._KEYPAD[d]))
            time.sleep(0.6)
        self.device.click(Point(*self._KEYPAD_CONFIRM))
        time.sleep(1.5)
        self.device.screenshot(force=True)
        return bool(self.device.ocr("锻刀资源投入", roi_4to4(400, 45, 880, 110)))

    def _apply_recipe(self, override: list | None = None) -> bool:
        """点火前把配比页四项设成目标配方；OCR 读出来已一致的行跳过"""
        target = self._forge_recipe(override)
        self.device.screenshot(force=True)
        for name, value in zip(_FORGE_RES, target):
            if self._read_recipe_row(name) == value:
                continue
            if not self._set_forge_row(name, value):
                return False
        return True

    def _emit_forge_costs(self, started_event_id, recipe=None):
        """点火成功的资源记账：四资源按配方负扣 + 委托符 -1。

        配方是配置已知值（不勾加速符是流程定死的），不用 OCR，
        attribution=confirmed / evidence=known_recipe。
        """
        payload = {"source": "forge.started", "attribution": "confirmed",
                   "evidence": "known_recipe", "script": "forge"}
        if isinstance(started_event_id, int):
            payload["source_event_id"] = started_event_id
        for name, cost in zip(_FORGE_RES, self._forge_recipe(recipe)):
            if hasattr(self, "record_resource_change"):
                self.record_resource_change(name, -cost, **payload)
            else:
                self.record_event(
                    "resource.change", resource=name, delta=-cost, **payload)
        if hasattr(self, "record_resource_change"):
            self.record_resource_change("委托符", -1, **payload)
        else:
            self.record_event(
                "resource.change", resource="委托符", delta=-1, **payload)

    def _read_countdown(self, cy: int):
        """读这炉的剩余时间，返回 ("01:27:30", 秒数) 或 None。
        点火后倒计时已经开始走字，读到的会比名义时长短一两秒。
        OCR 经常把冒号吃掉（'02','59','53' 拆成三个 token，2026-10-19
        因此把点着的炉误判成点火失败），冒号正则落空时按纯数字串
        从右往左切 秒/分/时。"""
        try:
            self.device.screenshot(force=True)
            tokens = self.device.ocr_all(roi_4to4(150, cy - 55, 780, cy + 55))
            text = "".join(t for t, _ in tokens)
            m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{1,2})\s*[:：]\s*(\d{1,2})", text)
            if m:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                return (f"{int(m.group(1))}:{m.group(2)}:{m.group(3)}", secs)
            digits = re.sub(r"\D", "", text)
            if 4 <= len(digits) <= 6:
                secs_part = int(digits[-2:])
                mins_part = int(digits[-4:-2])
                hours_part = int(digits[:-4])
                if secs_part < 60 and mins_part < 60:
                    return (f"{hours_part}:{digits[-4:-2]}:{digits[-2:]}",
                            hours_part * 3600 + mins_part * 60 + secs_part)
            return None
        except Exception:
            return None

    @staticmethod
    def _watch_hit(countdown, watch_secs: set):
        """倒计时命中盯梢目标（走字容忍少 90 秒）就返回显示文本"""
        if not countdown or not watch_secs:
            return None
        shown, secs = countdown
        for target in watch_secs:
            if target - 90 <= secs <= target:
                return shown
        return None

    # ==================== 限锻十连（十连锻刀+加速符） ====================

    _TENREN_ROW_BTN_X = 985       # 状况页空闲行右侧「十连锻刀」
    _TENREN_FIRE = (1176, 615)    # 配比页（十连模式）右下「十连锻刀」
    _SPEEDUP_BOX = (1193, 422)    # 配比页「使用加速符」勾选框
    _SPEEDUP_PREVIEW_ROI = (1150, 390, 1278, 560)  # 勾选后预览变两个数（现值▼扣后）
    _REVEAL_SKIP = (113, 78)      # 揭示动画左上角 » 快进
    _BACK_BTN = (141, 87)         # 锻刀页返回
    _BOARD_TAP = (640, 360)       # 结算榜点穿
    _BOARD_NAME_ROI = (150, 40, 1240, 620)    # 卡牌区（避开左缘花字和底部积分条）
    _BOARD_MARK_ROI = (880, 625, 1250, 705)   # 榜右下「显现积分」——榜的身份证
    _CAPACITY_ROI = (960, 45, 1100, 85)       # 顶栏「所持刀剣 196 / 200」
    # 状况页右侧栏库存（真机 OCR 定位：委托符数 y心364、加速符数 y心527；
    # 加速符·极的数也记到账本正名「加速符」上）
    _SIDE_COUNT_ROIS = (("委托符", (1140, 345, 1275, 385)),
                        ("加速符", (1140, 508, 1275, 548)))

    def limited_forge_stream(self, total: int = 50, recipe: list | None = None,
                             watch_names: list | None = None,
                             stop_on_hit: bool = True,
                             capacity_action: str = "stop"):
        """
        限锻十连：十连锻刀+加速符瞬间出货，限锻活动期间集中赌刀专用。

        Args:
            total: 要锻多少把（按十连取整，如 50=5 发；10~200）
            recipe: 点火配方，None 读配置 forge.recipe
            watch_names: 目标刀名清单，出货命中报喜（中/日名都行，过名册校正）
            stop_on_hit: 命中目标立刻收手（保住剩下的加速符）
            capacity_action: 刀位不足一发时 stop / dismantle / sugar

        Yields:
            str: 执行状态消息
        """
        recipe = self._forge_recipe(recipe)
        total = max(10, min(int(total or 50), 200))
        batches = (total + 9) // 10
        watch_ids, watch_raw = self._resolve_watch_names(watch_names)
        capacity_action = str(capacity_action or "stop")
        if capacity_action not in {"stop", "dismantle", "sugar"}:
            capacity_action = "stop"

        yield (f"[限锻] 目标 {batches * 10} 把（{batches} 发十连），"
               f"配方 {'/'.join(str(v) for v in recipe)}")
        yield (f"[限锻] 账单预告：委托符 {9 * batches} + 加速符 {10 * batches} "
               f"+ 四资源各 {'/'.join(str(v * 10) for v in recipe)}")
        if watch_raw:
            yield f"[限锻] 🎯 目标刀剑：{'、'.join(watch_raw)}"

        yield "[限锻] 正在导航到锻刀..."
        for nav_msg in self.navigate_to_stream("锻刀"):
            yield nav_msg
        if self.current_location != "锻刀":
            yield "[限锻] ✗ 到达锻刀失败"
            return
        time.sleep(1.0)

        # 加速符不可再生：开跑前核库存，读不出来宁可不跑
        counts = self._read_sidebar_counts()
        if counts.get("委托符") is None or counts.get("加速符") is None:
            yield "[限锻] ✗ 库存读不出来（委托符/加速符），不敢动手"
            return
        yield (f"[限锻] 库存：委托符 {counts['委托符']}、加速符 {counts['加速符']}")
        if counts["委托符"] < 9 * batches or counts["加速符"] < 10 * batches:
            yield (f"[限锻] ✗ 库存不够：需要委托符 {9 * batches}、加速符 "
                   f"{10 * batches}，差得远呢，收摊")
            return

        done = 0
        got = []           # 出货名单（认出来的才进，认不出不耽误锻）
        hit_names = []

        for batch in range(batches):
            # 刀位是不可省略的点火门闩：读不出来就停，绝不能把 OCR 失败
            # 当作“空间足够”。每发都从状况页重新读取，上一发的旧值不复用。
            self.device.screenshot(force=True)
            cap = self._read_capacity()
            if cap is None:
                yield "[限锻] ✗ 刀位数量读不出来，不敢点火，收摊"
                break
            free_slots = cap[1] - cap[0]
            if free_slots < 10:
                need = 10 - free_slots
                if capacity_action == "stop":
                    yield (f"[限锻] ✗ 刀位只剩 {free_slots} 个，一发需要 10 个；"
                           "按设定停下，不自动处理刀剑")
                    break
                if capacity_action == "dismantle":
                    yield (f"[限锻] 刀位只剩 {free_slots} 个，按白名单刀解 "
                           f"{need} 把，只腾本发所需位置...")
                    for msg in self.dismantle_stream(
                            max_dismantle=need, _from_forge=True):
                        yield msg
                    if not self._back_to_status():
                        yield "[限锻] ✗ 刀解后回不到锻刀状况，收摊"
                        break
                else:
                    yield (f"[限锻] 刀位只剩 {free_slots} 个，按设定先习合现有重刀，"
                           "完成后重新核对刀位...")
                    if not hasattr(self, "_shugo_loop_stream"):
                        yield "[限锻] ✗ 当前流程没有习合能力，收摊"
                        break
                    fed = yield from self._shugo_loop_stream(False)
                    yield f"[限锻] 习合完成 {int(fed or 0)} 轮，返回锻刀复查"
                    for nav_msg in self.navigate_to_stream("锻刀"):
                        yield nav_msg
                    if self.current_location != "锻刀":
                        yield "[限锻] ✗ 习合后回不到锻刀状况，收摊"
                        break
                    time.sleep(1.0)
                self.device.screenshot(force=True)
                cap = self._read_capacity()
                if cap is None:
                    yield "[限锻] ✗ 处理后仍读不出刀位，不敢点火，收摊"
                    break
                free_slots = cap[1] - cap[0]
                if free_slots < 10:
                    yield (f"[限锻] ✗ 处理后仍只有 {free_slots} 个空位，"
                           "没有继续点火")
                    break

            if not self._enter_tenren():
                yield "[限锻] ✗ 没有空闲炉能进十连（炉子都在烧？），收摊"
                break
            if not self._apply_recipe(recipe):
                yield "[限锻] ✗ 配方没设上，收摊"
                yield from self._leave_tenren(0, [])
                break
            if not self._ensure_speedup():
                yield "[限锻] ✗ 「使用加速符」勾不上，收摊（不勾就烧时间了，不干）"
                yield from self._leave_tenren(0, [])
                break

            yield f"[限锻] 第 {batch + 1}/{batches} 发十连，点火！"
            out = {"ok": False, "swords": [], "board_seen": False,
                   "uncertain": False}
            from ..game_state import begin_operation, confirm_operation
            boundary = begin_operation(self.device)
            for msg in self._tenren_batch(cap, out):
                yield msg
            outcome = confirm_operation(self.device, boundary, "forge_ten")
            if outcome is not None:
                out["ok"] = outcome
                out["uncertain"] = False
            batch_swords = list(out["swords"])
            left_status = False
            if not out["ok"]:
                # 配比页重新出现但刀位结果没读出来时，点火结果未知。
                # 只允许退出揭榜/复读刀位来确认，绝不再补点。
                recovery = {"boards_seen": 0}
                if out["uncertain"]:
                    yield "[限锻] 点火结果暂时看不清，退出揭榜确认；不会重复点火"
                    left_status = yield from self._leave_tenren(
                        1, batch_swords, recovery)
                    self.device.screenshot(force=True)
                    cap_after = self._read_capacity() if left_status else None
                    out["ok"] = bool(
                        recovery["boards_seen"] > 0
                        or (cap_after and cap_after[0] == cap[0] + 10))
                    out["board_seen"] = recovery["boards_seen"] > 0
                if not out["ok"]:
                    if not left_status:
                        yield from self._leave_tenren(0, [])
                    yield ("[限锻] ✗ 无法确认这一发是否点成，已停手；"
                           "请看游戏现场和库存，绝不自动补点")
                    break
            if not left_status:
                pending = 0 if out["board_seen"] else 1
                recovery = {"boards_seen": 0}
                left_status = yield from self._leave_tenren(
                    pending, batch_swords, recovery)

            done += 1
            swords = batch_swords
            if swords:
                got.extend(swords)
            if hasattr(self, "record_event"):
                event_id = self.record_event(
                    "forge.tenren", batch=batch + 1, recipe=recipe,
                    swords=[s["name"] for s in swords])
                self._emit_tenren_costs(event_id, recipe)
            hits = [s for s in swords if self._watch_name_hit(s, watch_ids, watch_raw)]
            if hits:
                names = "、".join(f"【{s['name']}】" for s in hits)
                hit_names.extend(s["name"] for s in hits)
                yield f"[限锻] 🎉🎉🎉 喜报！第 {batch + 1} 发出了 {names}！"
                try:
                    from ..notify import notify
                    # 标题必须 ASCII（http.client 按 latin-1 编码头，中文/emoji 会静默发不出去）
                    notify(f"限锻出货：{names}！快去看",
                           title="Limited Forge Hit!", tags="tada,sword")
                except Exception:
                    pass
                if stop_on_hit:
                    yield "[限锻] 目标到手，按设定收手"
                    break
            if not left_status:
                yield "[限锻] ✗ 这一发完成了，但没能安全回到锻刀状况，停止后续批次"
                break

        names = "、".join(f"【{s}】" for s in hit_names)
        yield (f"[限锻] 收工：锻了 {done * 10} 把（{done} 发十连），"
               f"认出 {len(got)} 把" + (f"，目标命中 {names}" if hit_names else ""))
        for msg in self._capture_inventory(phase="forge"):
            yield msg

    @staticmethod
    def _resolve_watch_names(watch_names):
        """目标刀名清单 → (名册ID集合, 原文清单)。查不到名册的留原文硬匹配"""
        ids, raw = set(), []
        for w in watch_names or []:
            w = str(w).strip()
            if not w:
                continue
            raw.append(w)
            found = sword_db.find_by_name(w)
            if found:
                ids.add(found[0])
        return ids, raw

    @staticmethod
    def _watch_name_hit(sword, watch_ids, watch_raw):
        return (sword.get("sword_id") in watch_ids
                or sword.get("name") in watch_raw
                or sword.get("name_jp") in watch_raw)

    def _forge_events(self):
        if not getattr(self.device, "adb_address", None):
            return []
        from ..game_state import read_events
        try:
            return read_events(self.device.adb_path, self.device.adb_address)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            return []

    def _read_capacity(self):
        """Use current game roster/limit; missing evidence falls back to the screen."""
        from ..game_state import forge_capacity
        capacity = forge_capacity(self._forge_events())
        if capacity is not None:
            return capacity
        tokens = self.device.ocr_all(roi_4to4(*self._CAPACITY_ROI))
        m = re.search(r"(\d+)\s*/\s*(\d+)", "".join(str(t) for t, _ in tokens))
        return (int(m.group(1)), int(m.group(2))) if m else None

    def _read_sidebar_counts(self):
        """Game balances first; OCR fills only missing ticket counts."""
        from ..game_state import fresh_resources, resource_facts
        resources = fresh_resources(resource_facts(self._forge_events()))
        counts = {name: resources.get(name) for name, _ in self._SIDE_COUNT_ROIS}
        for name, roi in self._SIDE_COUNT_ROIS:
            if counts[name] is not None:
                continue
            self.device.screenshot(force=True)
            tokens = self.device.ocr_all(roi_4to4(*roi))
            m = re.search(r"\d+", "".join(str(t) for t, _ in tokens))
            counts[name] = int(m.group()) if m else None
        return counts

    def _enter_tenren(self) -> bool:
        """状况页找空闲炉点「十连锻刀」进十连配比页。
        十连模式的身份证是委托符预览上的「节省1枚！」标签（十连优惠）——
        按钮本身的「十连锻刀」是竖排四字，OCR 实机会读成「十整刀」，不可靠"""
        self.device.screenshot(force=True)
        for cy in _SLOT_CY:
            tokens = self.device.ocr_all(roi_4to4(150, cy - 55, 780, cy + 55))
            if "空闲" not in "".join(str(t) for t, _ in tokens):
                continue
            self.device.click(Point(self._TENREN_ROW_BTN_X, cy))
            time.sleep(2.5)
            self.device.screenshot(force=True)
            if (self.device.ocr("锻刀资源投入", roi_4to4(400, 45, 880, 110))
                    and self.device.ocr("节省", roi_4to4(1140, 200, 1278, 280))):
                return True
        return False

    def _speedup_checked(self) -> bool:
        """加速符勾没勾：预览框勾了是两个数（现值▼扣后），没勾只有一个。
        按「纯数字 token」数，不 join 后再数——join 会把两个数黏成一个"""
        tokens = self.device.ocr_all(roi_4to4(*self._SPEEDUP_PREVIEW_ROI))
        nums = [t for t, _ in tokens if re.fullmatch(r"\d+", str(t).strip())]
        return len(nums) >= 2

    def _ensure_speedup(self) -> bool:
        """确保「使用加速符」勾上；勾不上返回 False"""
        self.device.screenshot(force=True)
        if self._speedup_checked():
            return True
        self.device.click(Point(*self._SPEEDUP_BOX))
        time.sleep(1.5)
        self.device.screenshot(force=True)
        return self._speedup_checked()

    def _board_visible(self) -> bool:
        """金色结算榜：右下「显现积分」在，且没有配比页标题（配比页右侧也有积分区）"""
        return (bool(self.device.ocr("显现积分", roi_4to4(*self._BOARD_MARK_ROI)))
                and not self.device.ocr("锻刀资源投入", roi_4to4(400, 45, 880, 110)))

    def _read_tenren_board(self):
        """十连结算榜认人：整版 OCR + 名册严格匹配（竖排刀名实测 10/10）。
        重复出货保留重复（抽到几把算几把），认不出的不硬猜。"""
        swords = []
        try:
            for text, _pt in self.device.ocr_all(roi_4to4(*self._BOARD_NAME_ROI)):
                found = sword_db.find_by_name(text, fuzzy=False)
                if found:
                    sid, info = found
                    swords.append({"sword_id": sid,
                                   "name": info.get("name_zh") or info["name"],
                                   "name_jp": info["name"]})
        except Exception:
            pass
        return swords

    def _dismiss_popup(self):
        """关锻刀弹窗（刀位满氪金窗/素材不足窗同款关法）"""
        pt = self.device.template_match("通用_关闭.png", threshold=0.7)
        self.device.click(pt if pt else Point(1062, 70))
        time.sleep(1.5)

    def _tenren_batch(self, cap_before, out):
        """打一发十连的状态机：点火 → 快进动画 → 读榜 → 回配比页确认刀位 +10。
        out 填 ok/swords。结果不明时只回报 uncertain，由上层退出揭榜确认；
        加速符不可再生，这里绝不重复点火。"""
        self.device.click(Point(*self._TENREN_FIRE))
        time.sleep(1.0)
        stale = 0
        swords = None
        for _ in range(40):  # 约 60 秒上限
            time.sleep(1.2)
            self.device.screenshot(force=True)
            # 限锻确认窗（issue#7 同款）：认出就点【是】继续
            if self.device.ocr("是否进行锻刀", roi_4to4(300, 200, 980, 500)):
                yes = self.device.ocr("是", roi_4to4(400, 300, 880, 620),
                                   match_mode="exact")
                if yes:
                    self.device.click(yes)
                    time.sleep(1.0)
                continue
            # 刀位满氪金窗：腾不出位置，这发算没点成
            mid = "".join(str(t) for t, _ in
                          self.device.ocr_all(roi_4to4(200, 150, 1080, 550)))
            if "刀位" in mid or "所持数" in mid or "购买详情" in mid:
                yield "[限锻] 刀位满了，弹窗关掉落跑"
                self._dismiss_popup()
                return
            # 素材/加速符不足窗：库存见底，同上
            if "不足" in mid:
                yield "[限锻] 弹了「不足」窗，库存见底，落跑"
                self._dismiss_popup()
                return
            if self._board_visible():
                out["board_seen"] = True
                swords = self._read_tenren_board()
                if swords:
                    yield ("[限锻] 揭榜："
                           + "、".join(f"【{s['name']}】" for s in swords))
                else:
                    yield "[限锻] 揭榜（一个名字都没认出来，照记十把）"
                self.device.click(Point(*self._BOARD_TAP))
                time.sleep(1.5)
                continue
            if self.device.ocr("锻刀资源投入", roi_4to4(400, 45, 880, 110)):
                cap = self._read_capacity()
                if cap_before and cap and cap[0] == cap_before[0] + 10:
                    out["ok"] = True
                    out["swords"] = swords or []
                    return
                if swords is not None:
                    # 榜都见过了，刀位又读不出来——成了，别卡着
                    out["ok"] = True
                    out["swords"] = swords
                    return
                stale += 1
                if stale >= 3:
                    out["uncertain"] = True
                    return
                continue
            # 动画/过场：快进
            self.device.click(Point(*self._REVEAL_SKIP))

    def _back_to_status(self) -> bool:
        """刀解页点回锻刀状况标签"""
        self.device.click(Point(26, 175))  # 左栏「锻刀」标签
        time.sleep(2.5)
        self.device.screenshot(force=True)
        return bool(self.device.ocr("锻刀状况", roi_4to4(400, 45, 880, 110)))

    def _leave_tenren(self, pending_boards, got, result=None):
        """退出十连：攒着的揭示动画一路快进，补读没见过的榜，直到回状况页"""
        result = result if isinstance(result, dict) else {}
        result.setdefault("boards_seen", 0)
        for _ in range(10 + pending_boards * 6):
            self.device.screenshot(force=True)
            if self.device.ocr("锻刀状况", roi_4to4(400, 45, 880, 110)):
                return True
            if self._board_visible():
                result["boards_seen"] += 1
                swords = self._read_tenren_board()
                if pending_boards > 0:
                    pending_boards -= 1
                    if swords:
                        got.extend(swords)
                        yield ("[限锻] 补读揭榜："
                               + "、".join(f"【{s['name']}】" for s in swords))
                self.device.click(Point(*self._BOARD_TAP))
                time.sleep(1.5)
                continue
            if self.device.ocr("锻刀资源投入", roi_4to4(400, 45, 880, 110)):
                self.device.click(Point(*self._BACK_BTN))
                time.sleep(1.5)
                continue
            self.device.click(Point(*self._REVEAL_SKIP))
            time.sleep(1.2)
        yield "[限锻] ⚠ 退出时动画没播完，直接回状况页超时了"
        return False

    def _emit_tenren_costs(self, event_id, recipe=None):
        """一发十连的记账：四资源按配方×10 负扣 + 委托符 -9（十连优惠）+ 加速符 -10。
        成本全是真机实测的固定规则，attribution=confirmed / evidence=tenren_cost。"""
        payload = {"source": "forge.tenren", "attribution": "confirmed",
                   "evidence": "tenren_cost", "script": "forge10"}
        if isinstance(event_id, int):
            payload["source_event_id"] = event_id
        costs = list(zip(_FORGE_RES, self._forge_recipe(recipe)))
        costs += [("委托符", 0.9), ("加速符", 1.0)]
        for name, per in costs:
            amount = -int(round(per * 10))
            if hasattr(self, "record_resource_change"):
                self.record_resource_change(name, amount, **payload)
            else:
                self.record_event(
                    "resource.change", resource=name, delta=amount, **payload)

    # ==================== 刀解 ====================

    def dismantle_stream(self, max_dismantle: int = 1, dry_run: bool = False,
                         _from_forge: bool = False, whitelist: list = None):
        """
        流式刀解：扫列表找白名单，解 max_dismantle 把

        Args:
            max_dismantle: 解几把（日课=1）
            dry_run: 只报决策不动手
            _from_forge: 从锻刀界面直接切标签（跳过导航）
            whitelist: 运行时覆盖白名单，None 则读配置 / 默认常量

        Yields:
            str: 执行状态消息
        """
        # 白名单：运行时覆盖 > 配置 > 默认常量
        if whitelist is None:
            whitelist = self.config.get("dismantle", {}).get("whitelist", DISMANTLE_WHITELIST)
        # 白名单预解析：过一遍名册校正（OCR 错字有字典兜底）
        whitelist_ids = set()
        for zh in whitelist:
            r = sword_db.find_by_name(zh)
            if r:
                whitelist_ids.add(r[0])
        yield f"[刀解] 白名单 {len(whitelist)} 人（解析 {len(whitelist_ids)} 个ID）"

        if not _from_forge:
            yield "[刀解] 正在导航到锻刀..."
            for nav_msg in self.navigate_to_stream("锻刀"):
                yield nav_msg
            if self.current_location != "锻刀":
                yield "[刀解] 到达锻刀失败"
                return
            time.sleep(1.0)

        # 切到刀解标签
        self.device.click(Point(26, 345))
        time.sleep(2.5)
        self.device.screenshot(force=True)
        if not self.device.ocr("刀解选择", roi_4to4(450, 45, 830, 110)):
            yield "[刀解] 没进到刀解选择界面，放弃"
            return

        dismantled = 0
        for page in range(8):  # 翻页安全上限
            self.device.screenshot(force=True)
            hit = self._scan_whitelist_row(whitelist_ids)
            if hit is None:
                # 本页没有白名单，往下翻
                yield f"[刀解] 第{page + 1}页没有白名单，翻页"
                self.device.swipe(640, 550, 640, 250, 800)
                time.sleep(2.0)
                continue

            name, cy = hit
            yield f"[刀解] 选中白名单: {name}"
            if dry_run:
                yield "[刀解] （演习模式：不点确认）"
                return

            self.device.click(Point(1046, cy))  # 该行选择
            time.sleep(1.5)
            resource_preview = self._read_dismantle_resources()
            self.device.click(Point(1200, 615))  # 大刀解（选中后灰变蓝）
            time.sleep(2.0)
            self.device.screenshot(force=True)
            if not self.device.ocr("是否确认", roi_4to4(350, 100, 930, 160)):
                yield "[刀解] 确认弹窗没出现，放弃"
                return
            from ..game_state import begin_operation, confirm_operation
            boundary = begin_operation(self.device)
            self.device.click(Point(785, 630))  # 确认
            time.sleep(2.5)
            outcome = confirm_operation(self.device, boundary, "dismantle")
            if outcome is not True:
                yield "[刀解] 未确认刀解结果，本次停止，不再重复消耗刀剑" if outcome is None else "[刀解] 游戏拒绝本次刀解，本次停止"
                return
            dismantled += len(boundary.get("serial_ids", [None]))
            _mark_dismantled_today()
            if hasattr(self, "record_event"):
                completed_id = self.record_event(
                    "dismantle.completed", sword=name,
                    serial_ids=boundary.get("serial_ids"),
                    resource_preview=resource_preview)
                self._emit_dismantle_resources(resource_preview, completed_id)
            yield f"[刀解] 分解完成: {name}（{dismantled}/{max_dismantle}）"
            if dismantled >= max_dismantle:
                return
            # 列表顶上来了，重扫（不翻页）

        yield f"[刀解] 收工：解了 {dismantled} 把" + ("（没找到够的白名单）" if dismantled < max_dismantle else "")

    def _read_dismantle_resources(self):
        """读取选中刀后首行的四资源收益预览；读不出的位置保留 None。"""
        self.device.screenshot(force=True)
        result = {}
        for resource, raw_roi in zip(_FORGE_RES, _DISMANTLE_RESOURCE_ROIS):
            try:
                tokens = self.device.ocr_all(roi_4to4(*raw_roi))
                raw = "".join(str(text).strip() for text, _ in tokens)
                # 该窄字体的数字 1 在冷却材格实机会稳定读成 i；只在整个
                # 数量框恰好是单个形似 1 的字符时修复，不污染普通文字。
                if raw in {"i", "I", "l", "|"}:
                    raw = "1"
                match = re.search(r"\d+", raw.replace(",", ""))
                result[resource] = int(match.group()) if match else None
            except Exception:
                result[resource] = None
        return result

    def _emit_dismantle_resources(self, preview, completed_event_id):
        """刀解成功后才把选择页预览落账；部分 OCR 失败不猜数值。"""
        for resource in _FORGE_RES:
            amount = preview.get(resource) if isinstance(preview, dict) else None
            payload = {
                "source": "dismantle.completed",
                "script": "dismantle",
                "evidence": "dismantle_preview_ocr",
            }
            if isinstance(completed_event_id, int):
                payload["source_event_id"] = completed_event_id
            if isinstance(amount, int) and amount > 0:
                if hasattr(self, "record_resource_change"):
                    self.record_resource_change(
                        resource, amount, attribution="confirmed", **payload)
                else:
                    self.record_event(
                        "resource.change", resource=resource, delta=amount,
                        attribution="confirmed", **payload)
            else:
                self.record_event(
                    "resource.change", resource=resource, delta=None,
                    attribution="unknown", note="刀解收益预览数量读取失败",
                    **payload)

    def _scan_whitelist_row(self, whitelist_ids: set):
        """
        扫当前页每一行，找第一把白名单刀

        Returns:
            (名字, 行心y) 或 None
        """
        for i in range(_VISIBLE_ROWS):
            top = _ROW_TOP + i * _ROW_PITCH
            cy = top + 48
            tokens = self.device.ocr_all(roi_4to4(
                _NAME_ROI_X1, top + _NAME_ROI_DY1,
                _NAME_ROI_X2, top + _NAME_ROI_DY2,
            ))
            if not tokens:
                continue
            raw = max((t for t, _ in tokens), key=len)
            if len(raw) < 2:
                continue
            found = sword_db.find_by_name(raw)
            if found and found[0] in whitelist_ids:
                name = found[1].get("name_zh") or found[1]["name"]
                return (name, cy)
        return None
