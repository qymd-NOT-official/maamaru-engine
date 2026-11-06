# -*- coding: utf-8 -*-
"""国服南瓜大作战的画面适配与数据通道。

南瓜公共循环（pumpkin_loop.py）只调用本层的钩子；国服配置
（config["pumpkin"]）、模板、OCR 词、像素坐标、剪影识别库和
battle_loop_stream 等国服战斗设施都收在这里。公共循环不直接访问
device、config 或像素坐标。

玩法（用户口述 + 实屏确认）：
  活动界面点"部队选择" → 选部队 → 即刻出阵 → 确认弹窗确定
  → 战斗循环（OCR"战斗"连点 + 安全区跳动画）→ 回到活动界面
  → 重复，直到九宫格 9 格全翻完 → 获得刀剑男士（动画要点掉）
  → 点"剪影更新"（二次弹窗确定）开新一局 → 继续刷

画面读取的血泪校准：
  - "九宫格满了"的判定：左侧面板「已解锁所有面板」OCR，不靠猜——
    猜错的代价是误点剪影更新白烧一枚更新令牌
  - 活动图不存在刀剑破坏；刀装警告仍按老规矩停下上报
"""
from __future__ import annotations

import time
from pathlib import Path

from ..device_adapter import roi_4to4
from ..silhouette import load_library, extract_observed, identify, is_confident, MIN_BLACK_PIXELS


class CnPumpkinViewMixin:
    """国服南瓜的画面钩子与数据通道。依赖宿主类的
    navigate_to_stream、_click_point、battle_loop_stream 等国服设施。"""

    _profile_lib = None      # 剪影素材库缓存；False = 加载失败别再试

    # ==================== 公共循环钩子 ====================

    def _pumpkin_activity_config(self) -> dict:
        return self.config.get("pumpkin", {})

    def _pumpkin_team_error(self, cfg: dict, team_no: int):
        teams = self.config.get("team_select", {}).get("teams", {})
        if str(team_no) not in teams:
            return f"[南瓜] 配置里没有部队{team_no}的坐标"
        return None

    def _pumpkin_identify_supported(self) -> bool:
        return True

    def _pumpkin_takeover_requested(self, remaining: int) -> bool:
        if self._expedition_takeover_requested():
            self._expedition_takeover_remaining = remaining
            return True
        return False

    def _pumpkin_goto_sally(self):
        for nav_msg in self.navigate_to_stream("出阵"):
            yield nav_msg
        return self.current_location == "出阵"

    def _pumpkin_enter_event(self, cfg: dict, difficulty: int):
        entered = False
        for _ in range(6):
            self.device.screenshot(force=True)
            if self.device.template_match(cfg["ui_title"]["template"]):
                entered = True
                break
            entry = self.device.template_match(cfg["activity_entry"]["template"])
            if entry:
                self.device.click(entry)
                time.sleep(2.0)
            else:
                time.sleep(1.0)
        if not entered:
            return False

        # 选难度（None=保持游戏当前 tab）
        if difficulty:
            tab = cfg.get("difficulty_tabs", {}).get(str(difficulty))
            if tab:
                self._click_point(tab)
                time.sleep(0.8)
                yield f"[南瓜] 已切难度 tab（{difficulty}）"
        return True

    def _pumpkin_board_full(self, cfg: dict) -> bool:
        full_cfg = cfg["board_full_ocr"]
        self.device.screenshot(force=True)
        return bool(self.device.ocr(expected=full_cfg["expected"],
                                    roi=roi_4to4(*full_cfg["roi"])))

    def _pumpkin_read_obtained(self, cfg: dict):
        """获得动画里 OCR 刀名（记日志用，不截图）；认不到返回 ""。"""
        return self._read_obtained_sword(cfg)

    def _pumpkin_skip_safe(self, cfg: dict, times: int):
        self.skip_safe(times, point=cfg["skip_tap"])

    def _pumpkin_after_battle_debug(self, cfg: dict, debug_dir: str, battles_total: int):
        self._ensure_on_board(cfg)
        self.device.save_screenshot(f"{debug_dir}/board_after_b{battles_total:02d}.png")

    def _pumpkin_battle_loop(self, cfg: dict, battle_debug, fought: int):
        # 南瓜是全自动战斗（没有"战斗"按钮可点），靠冷静期+标题回归判结束
        for battle_msg in self.battle_loop_stream(cfg_key="pumpkin", tag="[南瓜]",
                                                  need_battle=False,
                                                  debug_dir=battle_debug,
                                                  fought=fought):
            yield battle_msg
        round_done, _ = self._battle_loop_result
        return round_done

    # ---------- 获得动画 OCR ----------

    def _read_obtained_sword(self, cfg: dict) -> str:
        """
        打完九宫格的获得动画里 OCR 刀名（记日志用，不截图）。
        区域可配：cfg["obtain_ocr"] = {"roi": [...], "exclude": ["刀剑乱舞", "獲得"]}
        认不到返回 ""。
        """
        try:
            ocr_cfg = cfg.get("obtain_ocr")
            if not ocr_cfg:
                return ""
            roi = roi_4to4(*ocr_cfg["roi"])
            exclude = ocr_cfg.get("exclude", [])
            texts = self.device.ocr_all(roi)
            for text, _pt in texts:
                t = text.strip()
                if not t or any(x in t for x in exclude):
                    continue
                return t
        except Exception:
            pass
        return ""

    # ---------- 智能认人 ----------

    def _get_profile_lib(self, cfg: dict):
        """加载剪影素材库（只加载一次；失败置 False 不再重试）"""
        if self._profile_lib is not None and self._profile_lib is not False:
            return self._profile_lib
        if self._profile_lib is False:
            return None
        from ..runtime_paths import BUNDLED_PROFILES_DIR, USER_PROFILES_DIR

        configured = cfg.get("profiles", "profiles")
        if configured and configured != "profiles":
            custom = Path(configured)
            if not custom.is_absolute():
                custom = self._root / custom
            lib_dirs = (BUNDLED_PROFILES_DIR, custom, USER_PROFILES_DIR)
        else:
            lib_dirs = (BUNDLED_PROFILES_DIR, USER_PROFILES_DIR)
        try:
            lib = load_library(lib_dirs)
            if not lib:
                raise ValueError("库里一个模板都没有")
            self._profile_lib = lib
            return lib
        except Exception as exc:
            joined = "、".join(str(path) for path in lib_dirs)
            print(f"[南瓜] 剪影素材库加载失败: {exc}（{joined}）")
            self._profile_lib = False
            return None

    def _identify_board_stream(self, cfg: dict, watch_names: list):
        """
        认当前板子的剪影。结果放在 self._identify_decision + self._identify_name：
          None  = 认不准/素材太少，继续打下一场回来再认
          "keep" = 是目标刀，刷完它
          "skip" = 不是目标刀，烧令牌换板子
        """
        self._identify_decision = None
        self._identify_name = None

        lib = self._get_profile_lib(cfg)
        if lib is None:
            yield "[南瓜] ⚠️ 剪影素材库加载失败，这局当死板版刷"
            self._identify_decision = "keep"  # 别误烧令牌，也别再认了
            return

        img = self.device.screenshot(force=True)
        if img is None:
            return
        obs = extract_observed(img)
        black = int(obs.sum())
        if black < MIN_BLACK_PIXELS:
            yield f"[南瓜] 剪影才翻出 {black} 个黑像素，认不出，继续打"
            return

        t0 = time.time()
        results = identify(obs, lib)
        if not results:
            return
        top_str = " / ".join(f"{name}({view}) {score:.2f}" for score, name, view in results[:3])
        yield f"[南瓜] 剪影识别（{time.time() - t0:.0f}s）：{top_str}"

        if not is_confident(results):
            yield "[南瓜] 分差不够，认不准，再打一场回来看"
            return

        name = results[0][1]
        self._identify_name = name
        if name in watch_names:
            yield f"[南瓜] 认出来了，是【{name}】——目标刀！刷完这块板子"
            self._identify_decision = "keep"
        else:
            yield f"[南瓜] 认出来了，是【{name}】，不是目标，烧令牌换板子"
            self._identify_decision = "skip"

    # ---------- 确保在活动界面 ----------

    def _ensure_on_board(self, cfg: dict) -> bool:
        """在活动界面返回 True；被动画/弹窗盖着就点安全区扒拉，扒拉不回来返回 False。"""
        return self.wait_landmark_skipping(
            template=cfg["ui_title"]["template"],
            skip_point=cfg["skip_tap"],
            timeout_s=8, stable_hits=1, interval=1.0)

    # ---------- 开一场战斗 ----------

    def _start_battle_stream(self, cfg: dict, team_no: int):
        """
        点部队选择 → 选部队 → 即刻出阵 → 确定。
        结果放在 self._battle_started；九宫格满了开不起来时置 False（不算错误）。
        遇上必须人来看的情况置 self._abort。
        """
        self._battle_started = False

        deploy = self._find_deploy_button(cfg)
        if not deploy:
            yield "[南瓜] 没找到部队选择按钮"
            return
        self.device.click(deploy)
        time.sleep(1.5)

        ocr_cfg = cfg["team_ui_ocr"]
        roi = roi_4to4(*ocr_cfg["roi"])
        if not self._wait_for_team_select(cfg, attempts=8):
            # 部队界面没开：可能是弹窗挡路，不代表板子满了（满没满看「已解锁」OCR）
            yield "[南瓜] 部队选择界面没打开"
            # 把拦路弹窗点掉再退出去，免得挡后面的剪影更新
            self.device.screenshot(force=True)
            ok = self.device.template_match(cfg["confirm_button"]["template"])
            if ok:
                self.device.click(ok)
                time.sleep(1.0)
            else:
                self._click_point(cfg["skip_tap"])
                time.sleep(0.8)
            return

        # 选部队（固定坐标，点两下确认）
        self._pick_team(team_no)

        # 即刻出阵
        if not self._click_depart(cfg):
            yield "[南瓜] 找不到即刻出阵按钮"
            return

        self.device.screenshot(force=True)

        # 刀装未满警告 → 安全取消整备，给后续日课让路
        equip_cancelled = self._cancel_equip_warning(cfg)
        if equip_cancelled is not None:
            if equip_cancelled:
                self._abort = "[南瓜] ⚠️ 刀装未满警告；已取消出阵并返回部队选择，本次跳过"
            else:
                self._abort = "[南瓜] ⚠️ 刀装未满警告；没能安全取消整备，本次出阵停止"
            return

        # 确认弹窗（可能有也可能直接进图）
        if self.device.template_match(cfg["confirm_ui"]["template"]):
            confirm_cfg = cfg["confirm_button"]
            confirm_roi = roi_4to4(*confirm_cfg["roi"]) if "roi" in confirm_cfg else None
            confirm = self.device.template_match(confirm_cfg["template"], confirm_roi)
            if confirm:
                self.device.click(confirm)
                time.sleep(2.0)

        # 验证真出去了：格子有编队任务，条件不满足会被游戏拒回部队选择
        # （拒的时候弹个提示窗，先点掉再看标题）
        time.sleep(2.0)
        self.device.screenshot(force=True)
        reject_ok = self.device.template_match(cfg["confirm_button"]["template"])
        if reject_ok:
            self.device.click(reject_ok)
            time.sleep(1.0)
            self.device.screenshot(force=True)
        if self.device.ocr(expected=ocr_cfg["expected"], roi=roi):
            self._abort = ("[南瓜] ⚠️ 即刻出阵被游戏拒了，还停在部队选择界面"
                           "——编队不满足这个格子的任务条件（左侧任务栏写着要啥），你去改下编队再来")
            return

        self._battle_started = True
        yield "[南瓜] 出发，进入战斗循环"

    # ---------- 剪影更新 ----------

    def _refresh_board_stream(self, cfg: dict, auto_refill: bool = False):
        """
        剪影更新 → 二次弹窗确定。结果放在 self._refresh_ok。
        更新完会校验：能不能重新点开部队选择界面（能 = 新板子出来了）。

        注意：南瓜 Pt 里程碑的狐狸对话（"已解锁所有面板…"那个看板娘弹窗）
        是模态的，会挡住剪影更新的确认弹窗。它没有关闭按钮，
        靠点 skip_tap 翻页（一页一句，一般 2 页），翻完自己消失。
        所以确认弹窗没出来就先驱散几轮再重试，别急着收工。
        """
        self._refresh_ok = False

        confirm = None
        for attempt in range(3):
            recovered = False
            if attempt:
                yield f"[南瓜] 剪影更新第 {attempt + 1} 次尝试..."
            # 先驱散可能挡路的狐狸对话（每点一下翻一页，没弹窗时点的是安全区，无害）
            self.skip_safe(2, point=cfg["skip_tap"])

            self.device.screenshot(force=True)
            refresh = self.device.template_match(cfg["refresh_button"]["template"])
            if not refresh:
                yield "[南瓜] 找不到剪影更新按钮"
                return
            self.device.click(refresh)
            time.sleep(1.5)

            # 二次确认弹窗（可能被狐狸对话挡着，多等几拍）
            for _ in range(4):
                self.device.screenshot(force=True)
                confirm = self.device.template_match(cfg["refresh_confirm"]["template"])
                if confirm:
                    break
                recover_cfg = self.config.get("raid", {}).get("ticket_recover", {})
                popup_template = recover_cfg.get("popup_button", {}).get("template")
                if popup_template and self.device.template_match(popup_template):
                    if not auto_refill:
                        close_template = recover_cfg.get("close_button", {}).get("template")
                        close = self.device.template_match(close_template) if close_template else None
                        if close:
                            self.device.click(close)
                        yield "[南瓜] 手形不足；自动补充已关闭，不消耗小判，收工"
                        return
                    yield "[南瓜] 手形不足，按设置使用小判自动补充..."
                    recovery = dict(cfg)
                    recovery["ticket_recover"] = recover_cfg
                    for recover_msg in self._recover_ticket_stream(recovery, tag="[南瓜]"):
                        yield recover_msg
                    if not self._recover_ok:
                        yield "[南瓜] 手形补充失败，收工"
                        return
                    # 旧活动没有已验证票价；先落补票事实，不猜小判金额。
                    self._record_ticket_refill(recovery, "[南瓜]")
                    recovered = True
                    break
                time.sleep(0.8)
            if not confirm and recovered:
                continue
            if confirm:
                break
            # 确认没出来 → 狐狸对话之类挡路，翻页驱散后重试
            yield "[南瓜] 确认弹窗没出来，可能有狐狸里程碑对话挡路，翻页驱散后重试..."
            self.skip_safe(3, point=cfg["skip_tap"])
        else:
            yield "[南瓜] 剪影更新的确认弹窗一直没出来（令牌烧完了，或者弹窗没驱散掉）"
            return

        self.device.click(confirm)
        time.sleep(2.0)

        # 可能有"新剪影出现了"之类的动画，点掉
        self.skip_safe(3, point=cfg["skip_tap"])

        # 校验：回活动界面 + 部队选择按钮回来了 = 更新成功
        if not self._ensure_on_board(cfg):
            yield "[南瓜] 更新完回不到活动界面"
            return
        self.device.screenshot(force=True)
        if self._find_deploy_button(cfg):
            self._refresh_ok = True
            yield "[南瓜] 剪影更新完成，新板子就位"
        else:
            yield "[南瓜] 更新完部队选择按钮没回来"
