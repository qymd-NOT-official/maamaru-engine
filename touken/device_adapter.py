# -*- coding: utf-8 -*-
"""
底层：模拟器适配器
只管跟模拟器打交道：截图、点击、滑动、OCR、模板匹配。
不知道什么是刀剑乱舞，不知道什么是联队战。

- 截图：MuMu 显存通道优先（共享内存直读渲染帧，免疫 screencap 流抽风
  吐半张图），通道没建成就回退裸 adb screencap。
- 点击/滑动：裸 adb 子进程（subprocess 真超时可杀）。
- 内核滑动：minitouch 触摸注入（刀帐竖向网格、一览横向翻页等页面
  input swipe 不认，内核注入才有效）。
- OCR：自研 cn_ocr（仓内 PP-OCRv6 权重，本地推理，不碰 ADB）。
- 模板匹配：自研 vision_cv2。
"""

import sys
import time  # noqa: F401  # 保留原文件的模块级导入，防止外部有人 from 这里拿
from pathlib import Path
from typing import Optional, Any
from dataclasses import dataclass


@dataclass
class Point:
    """坐标点"""
    x: int
    y: int

    def to_list(self) -> list:
        return [self.x, self.y]

    def to_tuple(self) -> tuple:
        return (self.x, self.y)


@dataclass
class Region:
    """ROI 区域: (x, y, w, h)"""
    x: int
    y: int
    w: int
    h: int

    def to_list(self) -> list:
        return [self.x, self.y, self.w, self.h]

    def to_tuple(self) -> tuple:
        return (self.x, self.y, self.w, self.h)

    @property
    def center(self) -> Point:
        return Point(self.x + self.w // 2, self.y + self.h // 2)

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h


def roi_4to4(x1: int, y1: int, x2: int, y2: int) -> Region:
    """把 [x1, y1, x2, y2] 转成 (x, y, w, h)"""
    return Region(x1, y1, x2 - x1, y2 - y1)


def _ocr_text_matches(text: str, expected: str, match_mode: str) -> bool:
    """匹配 OCR 文本；空结果不能借由 ``"" in expected`` 误报命中。"""
    text = str(text or "").strip()
    expected = str(expected or "").strip()
    if not text or not expected:
        return False
    if match_mode == "exact":
        return text == expected
    if match_mode == "contains":
        return expected in text or text in expected
    return False


def _safe_print(message: object, flush: bool = False) -> None:
    """Best-effort console output; logging must never change automation results."""
    text = str(message)
    try:
        print(text, flush=flush)
    except UnicodeEncodeError:
        # Windows workers may inherit a GBK console. Preserve a readable escaped
        # form for unsupported characters such as © instead of failing OCR.
        try:
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            escaped = text.encode(encoding, errors="backslashreplace").decode(encoding)
            print(escaped, flush=flush)
        except Exception:
            pass
    except Exception:
        pass


class DeviceAdapter:
    """
    模拟器设备适配器
    封装所有对模拟器的调用：截图、点击、滑动、OCR、模板匹配。
    """

    def __init__(self, adb_path: str, adb_address: str, resource_dir: str,
                 project_root: Optional[str] = None,
                 manager_path: Optional[str] = None,
                 emulator_instance: int = 0):
        """
        初始化设备适配器

        Args:
            adb_path: ADB 可执行文件路径
            adb_address: ADB 设备地址，如 "127.0.0.1:16384"
            resource_dir: 资源包目录路径（如 "resource/base"）
            project_root: 项目根目录
            manager_path: MuMuManager.exe 路径（给了就支持模拟器自启动）
            emulator_instance: MuMu 实例编号
        """
        self.adb_path = adb_path
        self.adb_address = adb_address
        self.resource_dir = resource_dir
        self.project_root = project_root or "."
        self.manager_path = manager_path
        self.emulator_instance = emulator_instance

        self._last_image: Optional[Any] = None  # 缓存上次截图
        self._mumu_screen = None  # MuMu 显存截图通道（touken/mumu_screen.py）
        self._mumu_tried = False  # 通道只尝试建一次，建不起来就一辈子 ADB
        self._minitouch = None    # 内核触摸注入通道（touken/minitouch_client.py）
        self._initialized = False

    # 国服帧固定 1280×720，基准像素空间（game_device.GameDevice 协议）
    # 即实际像素空间，恒等不换算。
    FRAME_W, FRAME_H = 1280, 720

    @property
    def base_size(self) -> tuple[int, int]:
        """基准分辨率：国服恒为实际帧尺寸。"""
        return (self.FRAME_W, self.FRAME_H)

    def init(self) -> bool:
        """初始化：ADB 可用性检查 + 模拟器自启动。"""
        if self._initialized:
            return True

        from .emulator import adb_alive, resolve_adb_address
        # 配置的地址拒连时先找备胎：标准端口自愈，无头 MuMu 只开 emulator-5554
        if not adb_alive(self.adb_path, self.adb_address):
            self.adb_address = resolve_adb_address(self.adb_path,
                                                   self.adb_address,
                                                   instance=self.emulator_instance)
        print(f"[适配器] 连接 ADB: {self.adb_address}")
        if not Path(self.adb_path).exists():
            print(f"[适配器] 错误：找不到 adb: {self.adb_path}")
            return False

        if adb_alive(self.adb_path, self.adb_address):
            print("[适配器] ADB 连接成功")
            self._initialized = True
            return True

        # 连不上就试试把模拟器拉起来再查一次（配了 MuMuManager 才会）
        if self.manager_path:
            from .emulator import ensure_emulator
            if ensure_emulator(self.adb_path, self.adb_address,
                               manager_path=self.manager_path,
                               instance=self.emulator_instance) \
                    and adb_alive(self.adb_path, self.adb_address):
                print("[适配器] ADB 连接成功（模拟器自启动）")
                self._initialized = True
                return True
        print("[适配器] 错误：ADB 连接失败，请确认模拟器已启动")
        return False

    # ---------- 黑屏转花加载检测 ----------

    # 1280×720 实测：加载画面≈纯黑，左下角一朵白色樱花常亮。
    # 不做模板匹配——樱花是旋转动画，静态模板会间歇性失明；
    # 「全屏近乎纯黑 + 左下角有亮斑」两条特征与旋转角度无关。
    LOADING_DARK_RATIO = 0.95       # 全屏暗像素占比下限
    LOADING_DARK_MAX = 40           # 暗像素的亮度上限（BGR 三通道最大值）
    LOADING_CORNER = (0, 640, 70, 80)   # 左下樱花区 (x, y, w, h)
    LOADING_BRIGHT_MIN = 160        # 亮像素亮度下限
    LOADING_BRIGHT_COUNT = 80       # 角落亮像素个数下限

    def looks_like_loading(self) -> bool:
        """是否在黑屏转花加载中（切场景/断网时的服务器等待画面）。

        加载态下点屏幕没意义，NAV 用它把「还在加载」和「导航失败」分开。
        """
        image = self.screenshot()
        if image is None:
            return False
        try:
            brightness = image.max(axis=2)
            dark_ratio = float((brightness < self.LOADING_DARK_MAX).mean())
            if dark_ratio < self.LOADING_DARK_RATIO:
                return False
            x, y, w, h = self.LOADING_CORNER
            corner = brightness[y:y + h, x:x + w]
            return int((corner > self.LOADING_BRIGHT_MIN).sum()) >= \
                self.LOADING_BRIGHT_COUNT
        except Exception:
            return False

    # ---------- 裸 ADB 通道（截图/点击/滑动） ----------
    #
    # 截图/点击/滑动走裸 adb 子进程：subprocess 真超时，挂了直接 kill，
    # 不传染、不堵识别引擎。OCR/模板匹配都是本地推理，不碰 ADB。

    def _adb_run(self, args: list, timeout: float = 15.0, binary: bool = False) -> Optional[bytes]:
        """
        执行裸 adb 命令（子进程 + 真超时 + 可杀）。
        binary=True 时返回原始输出（截图 PNG bytes），否则只返回 stdout。
        """
        import subprocess
        cmd = [self.adb_path, "-s", self.adb_address] + args
        # CREATE_NO_WINDOW：worker 子进程没控制台，裸起 adb.exe（控制台程序）
        # 会让 Windows 新建控制台窗口弹窗抢焦点——必须隐藏（8/8 血泪教训）
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                                  check=False, shell=False, creationflags=flags)
            if proc.returncode != 0:
                print(f"[ADB] 命令失败 ({proc.returncode}): {' '.join(cmd[-3:])} | {proc.stderr.decode('utf-8', 'ignore')[:100]}")
                return None
            out = proc.stdout
            return out if binary else (out or b"")
        except subprocess.TimeoutExpired:
            print(f"[ADB 超时] {' '.join(cmd[-3:])} 超过 {timeout}s，已终止")
            return None
        except Exception as exc:
            print(f"[ADB 错误] {' '.join(cmd[-3:])}: {exc}")
            return None

    def _adb_capture_bgr(self) -> Optional[Any]:
        """裸 adb 截图抓一帧：exec-out screencap -p → PNG bytes → numpy BGR。

        独立成 helper 是为了让 MuMu 冻帧「静止核验」也能走这条路——
        verifier 在 capture() 回调里被调，绝不能反过来碰 screenshot()
        （递归截图），所以这里直接抓 ADB 原始帧，不读/不写截图缓存。
        任何失败返回 None。
        """
        png = self._adb_run(["exec-out", "screencap", "-p"], timeout=15.0, binary=True)
        if not png:
            print("[ADB] 截图失败/超时")
            return None
        try:
            import numpy as np
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(png)).convert("RGB")
            # PIL 是 RGB，识别引擎要 BGR → 反转通道
            return np.array(img)[:, :, ::-1].copy()
        except Exception as exc:
            print(f"[ADB] 截图解析失败: {exc}")
            return None

    def _verify_static_frame(self, frame) -> Optional[bool]:
        """MuMu 冻帧核验：拿 ADB 新截的一帧和 MuMu 帧逐像素比。

        返回 True=两条通道同画面（画面真静止，放行）；False=不同（通道
        真冻了）；None=ADB 截图拿不到（核验不可用，调用方走安全回退）。
        """
        adb_frame = self._adb_capture_bgr()
        if adb_frame is None:
            return None
        try:
            import numpy as np
            return bool(np.array_equal(frame, adb_frame))
        except Exception:
            return None

    def screenshot(self, force: bool = False) -> Optional[Any]:
        """
        截图并返回图像对象

        Args:
            force: 是否强制重新截图（否则用缓存）

        Returns:
            numpy 数组（BGR 格式）或 None
        """
        if not self._initialized:
            print("[适配器] 错误：未初始化，请先调用 init()")
            return None

        if not force and self._last_image is not None:
            return self._last_image

        # MuMu 显存通道优先：共享内存直接读渲染帧，不走 ADB，
        # 免疫 screencap 流抽风吐半张图（8-25/26 两次炸死根因）。
        # 通道没建成或抓帧失败都回退裸 ADB 老路。
        if self._mumu_screen is None and not self._mumu_tried:
            self._mumu_tried = True
            try:
                from .mumu_screen import MumuScreen
                # 注入 ADB 静止核验：选人页等真静止画面会字节级连撞冻帧线，
                # 撞线时用它交叉确认「ADB 也截到同一张图」再放行，免得误判
                # 把显存通道断开重连（误判会把截图速度打回 ADB 级）。
                self._mumu_screen = MumuScreen.from_adb(
                    self.adb_path, self.adb_address,
                    static_verifier=self._verify_static_frame)
            except Exception as exc:
                print(f"[MuMu截图] 通道初始化失败，用 ADB 截图: {exc}")
                self._mumu_screen = None
        if self._mumu_screen is not None:
            frame = self._mumu_screen.capture()
            if frame is not None:
                self._last_image = frame
                return frame
            if self._mumu_screen._dead:
                self._mumu_screen = None  # 通道已自判死刑，别再问了

        # 裸 adb 截图兜底
        arr = self._adb_capture_bgr()
        if arr is None:
            return None
        self._last_image = arr
        return arr

    def save_screenshot(self, path: str, force: bool = True) -> bool:
        """截图存盘（调试用）。BGR numpy → RGB → PNG"""
        image = self.screenshot(force=force)
        if image is None:
            return False
        try:
            from PIL import Image as _PILImage
            _PILImage.fromarray(image[:, :, ::-1]).save(path)
            print(f"[适配器] 截图已存: {path}")
            return True
        except Exception as exc:
            print(f"[适配器] 错误：截图存盘异常: {exc}")
            return False

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400) -> bool:
        """滑动（用于列表翻页）。裸 adb 子进程，真超时可杀。"""
        if not self._initialized:
            print("[适配器] 错误：未初始化")
            return False
        ok = self._adb_run(
            ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
            timeout=10.0)
        if ok is None:
            print(f"[ADB] 滑动 ({x1},{y1}) → ({x2},{y2}) 失败/超时")
            return False
        print(f"[ADB] 滑动 ({x1},{y1}) → ({x2},{y2})")
        return True

    def touch_swipe(self, x1: int, y1: int, x2: int, y2: int,
                    duration_ms: int = 400) -> bool:
        """minitouch 内核触摸注入的滑动。

        裸 adb input swipe 在部分游戏 UI 上根本不被识别——刀帐图鉴的
        竖向网格、一览的横向翻页都实测只有内核注入有效；而 input tap
        哪里都好使。两种通道并存，按页面选。"""
        if not self._initialized:
            print("[适配器] 错误：未初始化")
            return False
        if self._minitouch is None:
            from .minitouch_client import MinitouchClient
            from pathlib import Path as _P
            self._minitouch = MinitouchClient(
                self.adb_path, self.adb_address,
                str(_P(self.resource_dir) / 'bin' / 'minitouch'))
        return self._minitouch.swipe(x1, y1, x2, y2, duration_ms)

    def click(self, target: Point) -> bool:
        """
        点击坐标

        Args:
            target: 点击位置

        Returns:
            是否成功
        """
        if not self._initialized:
            print("[适配器] 错误：未初始化")
            return False

        ok = self._adb_run(
            ["shell", "input", "tap", str(target.x), str(target.y)],
            timeout=10.0)
        if ok is None:
            print(f"[ADB] 点击 ({target.x}, {target.y}) 失败/超时")
            return False
        print(f"[ADB] 点击 ({target.x}, {target.y})")
        return True

    def ocr(self, expected: str, roi: Region,
            match_mode: str = "contains") -> Optional[Point]:
        """OCR 识别文字：命中返回中心坐标，失败返回 None。"""
        from . import cn_ocr
        image = self.screenshot()
        if image is None:
            self._record_ocr("match", roi, [], expected, match_mode, False,
                             "screenshot_unavailable")
            return None
        try:
            tokens = cn_ocr.read_region(image, roi)
        except Exception as exc:
            _safe_print(f"[cnOCR 错误] OCR 异常: {exc}")
            self._record_ocr("match", roi, [], expected, match_mode, False,
                             type(exc).__name__)
            return None
        structured = [{"text": t, "score": s, "center": [cx, cy], "box": None}
                      for t, cx, cy, s in tokens]
        for text, cx, cy, score in tokens:
            if cn_ocr.text_matches(text, expected, match_mode):
                _safe_print(f'[cnOCR] OCR 命中: "{text}" (目标: "{expected}") '
                            f'at ({cx}, {cy}), score={score:.3f}')
                self._record_ocr("match", roi, structured, expected,
                                 match_mode, True)
                return Point(cx, cy)
        _safe_print(f'[cnOCR] OCR 识别到文字但未匹配: '
                    f'{[t for t, *_ in tokens]}, expected="{expected}"')
        self._record_ocr("match", roi, structured, expected, match_mode, False)
        return None

    def ocr_all(self, roi: Region, image=None) -> list:
        """识别区域内所有文字（不筛选，全都要）。

        用途：读取界面上的信息文本（第几部队、地图名、刀名），
        拿到文字后由上层自己做解析判断。

        Args:
            roi: 识别区域
            image: 可选，直接对这张预处理过的图（如放大后的裁剪块）识别，
                   此时 roi 是相对该图的坐标。不给就实时截图。

        Returns:
            [(文本, 中心点Point), ...]，识别失败返回 []
        """
        from . import cn_ocr
        if image is None:
            image = self.screenshot()
        if image is None:
            self._record_ocr("all", roi, [], error="screenshot_unavailable")
            return []
        try:
            raw = cn_ocr.read_region(image, roi)
        except Exception as exc:
            _safe_print(f"[cnOCR 错误] OCR 全量识别异常: {exc}")
            self._record_ocr("all", roi, [], error=type(exc).__name__)
            return []
        out = [(text, Point(cx, cy)) for text, cx, cy, _s in raw]
        _safe_print(f"[cnOCR] OCR 全量识别: {[t for t, _ in out]}")
        self._record_ocr("all", roi,
                         [{"text": t, "score": s, "center": [cx, cy],
                           "box": None} for t, cx, cy, s in raw])
        return out

    @staticmethod
    def _record_ocr(kind: str, roi: Region, tokens: list[dict],
                    expected: str | None = None, match_mode: str | None = None,
                    matched: bool | None = None, error: str | None = None) -> None:
        """Best-effort only: data collection must never affect automation."""
        try:
            from .telemetry import get_telemetry_store
            get_telemetry_store().record_ocr(
                kind=kind, roi=roi.to_list(), tokens=tokens,
                expected=expected, match_mode=match_mode,
                matched=matched, error=error,
            )
        except Exception:
            pass

    def template_match(self, template: str, roi: Optional[Region] = None,
                       threshold: float = 0.7) -> Optional[Point]:
        """模板匹配：命中返回中心坐标，失败返回 None。"""
        from . import vision_cv2
        image = self.screenshot()
        if image is None:
            return None
        try:
            from .server_pack import resolve_template_path
            path = resolve_template_path(template, self.resource_dir)
            hit = vision_cv2.best(image, path, roi.to_tuple() if roi else None)
        except FileNotFoundError:
            print(f"[cv2模板] 模板不存在: {template}")
            return None
        except Exception as exc:
            print(f"[cv2模板] 匹配异常: {exc}")
            return None
        if hit is None or hit[2] < threshold:
            print(f"[cv2] 模板未命中: {template}, threshold={threshold}")
            return None
        print(f"[cv2] 模板命中: {template} at ({hit[0]}, {hit[1]}), "
              f"score={hit[2]:.3f}")
        return Point(hit[0], hit[1])

    def template_match_score(self, template: str, roi: Optional[Region] = None,
                             threshold: float = 0.5) -> float:
        """模板匹配只取分数（编队页伤势章"比分数选最佳"分类用）。

        以较低阈值获取候选的真实分；未识别出结果返回 0.0，
        调用方自行做阈值与区分度判定。
        """
        from . import vision_cv2
        image = self.screenshot()
        if image is None:
            return 0.0
        try:
            from .server_pack import resolve_template_path
            path = resolve_template_path(template, self.resource_dir)
            return vision_cv2.match_score(
                image, path, roi.to_tuple() if roi else None, threshold)
        except Exception as exc:
            print(f"[cv2模板] 分数异常: {exc}")
            return 0.0

    def exists(self, template: str, roi: Optional[Region] = None,
               threshold: float = 0.7) -> bool:
        """判断模板是否存在"""
        result = self.template_match(template, roi, threshold)
        return result is not None
