# -*- coding: utf-8 -*-
"""
底层：模拟器适配器
只管跟模拟器/maa 模块打交道：截图、点击、OCR、模板匹配。
不知道什么是刀剑乱舞，不知道什么是联队战。

截图/点击/滑动走裸 adb 子进程（subprocess 真超时可杀，MAA 的 ADB 通道
一旦卡住 wait() 会永久死锁——2026-08-05 南瓜 battle_loop 两次卡死根因）。
OCR/模板匹配走 MAA 本地推理（不碰 ADB，天然安全）。

2026-08-05 二次加固：MAA 的所有 post_* 不再裸 .wait()（硬阻塞无超时），
改同线程轮询 job.done 限时等待（_wait_job）。不开新线程、不包 MAA 调用，
守 §14.1 血泪红线。连续多次识别超时 = MAA 已死：先照 MAA 本家的思路抢救
（重启 ADB 服务 + 重建控制器 + 重绑 Tasker），救不回来工人进程再自我了断
（退出码 43），面板看门狗/退出码会给出明确死因，不再无声卡死。
"""

import os
import sys
import time  # noqa: F401  # 保留原文件的模块级导入，防止外部有人 from 这里拿
from pathlib import Path
from typing import Optional, Any
from dataclasses import dataclass
from enum import Enum


# MaaFramework 导入
try:
    from maa.controller import AdbController
    from maa.pipeline import JOCR, JRecognitionType, JTemplateMatch
    from maa.resource import Resource
    from maa.tasker import Tasker
    MAAFW_AVAILABLE = True
except ImportError:
    MAAFW_AVAILABLE = False
    print("[警告] maa 模块未安装，MAAAdapter 将无法使用")


class RecognizeType(Enum):
    """识别方式"""
    OCR = "OCR"
    TEMPLATE_MATCH = "TemplateMatch"


class ActionType(Enum):
    """操作类型"""
    CLICK = "Click"
    DO_NOTHING = "DoNothing"
    STOP = "Stop"
    SHELL = "Shell"


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
    """ROI 区域: (x, y, w, h) —— MaaFW 用的格式"""
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
    """把 [x1, y1, x2, y2] 转成 MaaFW 的 (x, y, w, h)"""
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


class MAAAdapter:
    """
    MaaFramework 适配器
    封装所有对 maa 模块的调用
    """

    # MAA 任务限时（秒）：本地识别正常只要零点几秒到几秒，30s 已经非常宽
    RECOGNIZE_TIMEOUT = 30.0
    CONNECT_TIMEOUT = 30.0
    BUNDLE_TIMEOUT = 120.0
    # 连续识别超时几次就认定 MAA 死了
    MAX_CONSECUTIVE_TIMEOUTS = 3
    # MAA 内核暴毙抢救上限：一场运行最多抢救几次，救不回来再按章程了断
    MAX_REVIVE_ATTEMPTS = 2

    def __init__(self, adb_path: str, adb_address: str, resource_dir: str,
                 project_root: Optional[str] = None,
                 manager_path: Optional[str] = None,
                 emulator_instance: int = 0):
        """
        初始化 MAA 适配器

        Args:
            adb_path: ADB 可执行文件路径
            adb_address: ADB 设备地址，如 "127.0.0.1:16384"
            resource_dir: 资源包目录路径（如 "resource/base"）
            project_root: 项目根目录（用于设置日志目录）
            manager_path: MuMuManager.exe 路径（给了就支持模拟器自启动）
            emulator_instance: MuMu 实例编号
        """
        if not MAAFW_AVAILABLE:
            raise RuntimeError("maa 模块未安装，请先执行 `uv sync`")

        self.adb_path = adb_path
        self.adb_address = adb_address
        self.resource_dir = resource_dir
        self.project_root = project_root or "."
        self.manager_path = manager_path
        self.emulator_instance = emulator_instance

        self.resource: Optional[Resource] = None
        self.controller: Optional[AdbController] = None
        self.tasker: Optional[Tasker] = None
        self._last_image: Optional[Any] = None  # 缓存上次截图
        self._mumu_screen = None  # MuMu 显存截图通道（touken/mumu_screen.py）
        self._mumu_tried = False  # 通道只尝试建一次，建不起来就一辈子 ADB
        self._maa_timeouts = 0  # 连续识别超时计数
        self._revive_attempts = 0  # 本场运行已抢救次数

        # 模板匹配引擎：maa（默认，现状）/ cv2（自研替换件）/ ab（双跑对比，
        # 以 MAA 结果为准，日志并排记录两边结果供阈值校准）。
        # 拆 MAA 过渡期专用，迁完删掉。
        engine = os.environ.get('MAAMARU_TEMPLATE_ENGINE', 'maa').strip().lower()
        self._template_engine = engine if engine in ('maa', 'cv2', 'ab') else 'maa'

        self._initialized = False

    # ---------- MAA 任务限时等待（同线程轮询，不开新线程） ----------

    def _wait_job(self, job, timeout: float, label: str):
        """
        同线程轮询等 MAA 任务完成，超时返回 None。

        为什么不用 job.wait()：wait() 是无超时硬阻塞，MAA 内部一旦卡住
        线程永久死锁（8/5 卡死根因）。轮询不开新线程、不并发碰 MAA，
        不踩「线程包 MAA 调用」的血泪红线。
        """
        deadline = time.time() + timeout
        tasker = self.tasker if self._initialized else None
        while True:
            try:
                if job.done:
                    return job
            except Exception as exc:
                _safe_print(f"[MAA 错误] {label} 状态查询失败: {exc}")
                return None
            # MAA 内核猝死（ADB 一断 Tasker 就地暴毙，8-25 两次实测）时
            # job 永远不会 done——直接查 inited 标志秒杀，不傻等到超时。
            # init 期间（self._initialized=False）Tasker 没绑好是正常的，不查。
            if tasker is not None and not getattr(tasker, "inited", True):
                _safe_print(f"[MAA] Tasker 已掉线（{label}），按 MAA 已死处理",
                            flush=True)
                self._maa_timeouts = self.MAX_CONSECUTIVE_TIMEOUTS
                return None
            if time.time() >= deadline:
                _safe_print(f"[MAA 超时] {label} 超过 {timeout:.0f}s 没完成，按失败处理（MAA 疑似卡死）",
                            flush=True)
                return None
            time.sleep(0.05)

    def _note_recognize_timeout(self):
        """连续识别超时 = MAA 大概率已经死了，再跑下去也是空转。
        工人子进程里先抢救（重启 ADB + 重绑 Tasker），救不回来再自我了断
        （退出码 43），让面板报出明确死因；
        非工人环境（test_*.py 手动调试）只计数不抢救不自杀。"""
        self._maa_timeouts += 1
        if (self._maa_timeouts >= self.MAX_CONSECUTIVE_TIMEOUTS
                and os.environ.get("MAAMARU_WORKER")):
            if (self._revive_attempts < self.MAX_REVIVE_ATTEMPTS
                    and self._try_revive()):
                self._maa_timeouts = 0
                return
            _safe_print(f"[MAA] 连续 {self._maa_timeouts} 次识别超时，MAA 已死，"
                        "工人进程自我了断（退出码 43）", flush=True)
            os._exit(43)

    def _try_revive(self) -> bool:
        """MAA 内核暴毙抢救（MAA 本家同款思路：连接死了就重启 ADB 再重连）。

        2026-08-25 凌晨实测：MuMu 截图通道抽风吐出半张图，同一毫秒 MAA
        原生层 Tasker 猝死（Tasker not inited），此后所有识别全是超时。
        旧 Tasker/Controller 的原生状态已不可信，不碰不销毁，直接弃置换新
        （工人进程是消耗品，漏一个原生对象比碰一个坏对象安全）。
        """
        self._revive_attempts += 1
        _safe_print(f"[MAA] 连续 {self._maa_timeouts} 次识别超时，"
                    f"第 {self._revive_attempts} 次抢救：重启 ADB 并重绑 Tasker",
                    flush=True)
        try:
            # 重启 ADB 服务：MuMu 的 ADB 通道抽风时，旧坏连接一起陪葬
            self._adb_run(["kill-server"], timeout=10.0)
            self._adb_run(["start-server"], timeout=10.0)
            self._adb_run(["connect", self.adb_address], timeout=10.0)

            # 先弃置旧 Tasker：它已是尸体，留着会让抢救自己的 _wait_job
            # 误判「Tasker 又掉了」直接放弃
            self.tasker = None
            controller = AdbController(adb_path=self.adb_path,
                                       address=self.adb_address)
            conn = self._wait_job(controller.post_connection(),
                                  timeout=self.CONNECT_TIMEOUT,
                                  label="ADB 抢救重连")
            if conn is None or not conn.succeeded:
                _safe_print("[MAA] 抢救失败：ADB 重连没成", flush=True)
                return False
            tasker = Tasker()
            if not tasker.bind(self.resource, controller) or not tasker.inited:
                _safe_print("[MAA] 抢救失败：Tasker 重绑没成", flush=True)
                return False
            self.controller = controller
            self.tasker = tasker
            self._last_image = None  # 缓存截图是暴毙前的现场，作废
            _safe_print("[MAA] ✓ 抢救成功，接着跑", flush=True)
            return True
        except Exception as exc:
            _safe_print(f"[MAA] 抢救失败: {exc}", flush=True)
            return False

    def init(self) -> bool:
        """初始化资源、连接 ADB、绑定 Tasker"""
        if self._initialized:
            return True

        # 1. 设置日志目录
        log_dir = Path(self.project_root) / "debug"
        log_dir.mkdir(parents=True, exist_ok=True)
        Tasker.set_log_dir(str(log_dir))

        # 2. 加载资源
        print(f"[MAA] 加载资源: {self.resource_dir}")
        self.resource = Resource()
        job = self._wait_job(self.resource.post_bundle(self.resource_dir),
                             timeout=self.BUNDLE_TIMEOUT, label="资源加载")
        if job is None or not job.succeeded:
            print("[MAA 错误] 资源加载失败/超时，请检查资源目录是否完整")
            return False
        print("[MAA] 资源加载成功")

        # 3. 连接 ADB（配置的地址拒连时先找备胎：标准端口自愈，无头 MuMu 只开 emulator-5554）
        from .emulator import adb_alive, resolve_adb_address
        if not adb_alive(self.adb_path, self.adb_address):
            self.adb_address = resolve_adb_address(self.adb_path,
                                                   self.adb_address,
                                                   instance=self.emulator_instance)
        print(f"[MAA] 连接 ADB: {self.adb_address}")
        if not Path(self.adb_path).exists():
            print(f"[MAA 错误] 找不到 adb: {self.adb_path}")
            return False

        self.controller = AdbController(adb_path=self.adb_path, address=self.adb_address)
        conn = self._wait_job(self.controller.post_connection(),
                              timeout=self.CONNECT_TIMEOUT, label="ADB 连接")
        if conn is None or not conn.succeeded:
            # 连不上就试试把模拟器拉起来再连一次（配了 MuMuManager 才会）
            if self.manager_path:
                from .emulator import ensure_emulator
                if ensure_emulator(self.adb_path, self.adb_address,
                                   manager_path=self.manager_path,
                                   instance=self.emulator_instance):
                    self.controller = AdbController(
                        adb_path=self.adb_path, address=self.adb_address)
                    conn = self._wait_job(self.controller.post_connection(),
                                          timeout=self.CONNECT_TIMEOUT,
                                          label="ADB 重连")
                    if conn is not None and conn.succeeded:
                        print("[MAA] ADB 连接成功（模拟器自启动）")
                        return self._bind_tasker()
            print("[MAA 错误] ADB 连接失败/超时，请确认模拟器已启动")
            return False
        print("[MAA] ADB 连接成功")

        return self._bind_tasker()

    def _bind_tasker(self) -> bool:
        """绑定 Tasker（init 的收尾，自启动重连也走这）"""
        self.tasker = Tasker()
        if not self.tasker.bind(self.resource, self.controller) or not self.tasker.inited:
            print("[MAA 错误] Tasker 绑定失败")
            return False

        self._initialized = True
        print("[MAA] 初始化完成")
        return True

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
    # MAA 的 post_*().wait() 是硬阻塞（无 timeout），ADB socket 一旦卡住
    # 线程就永久死锁——2026-08-05 南瓜 battle_loop 两次卡死的根因。
    # 截图/点击/滑动改走裸 adb 子进程：subprocess 真超时，挂了直接 kill，
    # 不传染、不堵 MAA。OCR/模板匹配仍走 MAA（本地推理，不碰 ADB）。

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

    def screenshot(self, force: bool = False) -> Optional[Any]:
        """
        截图并返回图像对象

        Args:
            force: 是否强制重新截图（否则用缓存）

        Returns:
            numpy 数组（BGR 格式）或 None
        """
        if not self._initialized:
            print("[MAA 错误] 未初始化，请先调用 init()")
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
                self._mumu_screen = MumuScreen.from_adb(self.adb_path,
                                                        self.adb_address)
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

        # 裸 adb 截图：exec-out screencap -p → PNG bytes → numpy BGR
        png = self._adb_run(["exec-out", "screencap", "-p"], timeout=15.0, binary=True)
        if not png:
            print("[ADB] 截图失败/超时")
            return None
        try:
            import numpy as np
            from PIL import Image
            import io
            img = Image.open(io.BytesIO(png)).convert("RGB")
            # PIL 是 RGB，MAA 的 post_recognition 要 BGR → 反转通道
            arr = np.array(img)[:, :, ::-1].copy()
            self._last_image = arr
            return arr
        except Exception as exc:
            print(f"[ADB] 截图解析失败: {exc}")
            return None

    def save_screenshot(self, path: str, force: bool = True) -> bool:
        """截图存盘（调试用）。BGR numpy → RGB → PNG"""
        image = self.screenshot(force=force)
        if image is None:
            return False
        try:
            from PIL import Image as _PILImage
            _PILImage.fromarray(image[:, :, ::-1]).save(path)
            print(f"[MAA] 截图已存: {path}")
            return True
        except Exception as exc:
            print(f"[MAA 错误] 截图存盘异常: {exc}")
            return False

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400) -> bool:
        """滑动（用于列表翻页）。裸 adb 子进程，真超时可杀。"""
        if not self._initialized:
            print("[MAA 错误] 未初始化")
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
        """MAA 内核触摸注入的滑动（终点传位移）。

        裸 adb input swipe 在部分游戏 UI 上根本不被识别——刀帐图鉴的
        竖向网格、一览的横向翻页都实测只有内核注入有效；而 input tap
        哪里都好使。两种通道并存，按页面选。"""
        if not self._initialized:
            print("[MAA 错误] 未初始化")
            return False
        try:
            self.controller.post_swipe(x1, y1, x2 - x1, y2 - y1, duration_ms)
            return True
        except Exception as exc:
            print(f"[MAA] 内核滑动 ({x1},{y1}) → ({x2},{y2}) 失败: {exc}")
            return False

    def click(self, target: Point) -> bool:
        """
        点击坐标

        Args:
            target: 点击位置

        Returns:
            是否成功
        """
        if not self._initialized:
            print("[MAA 错误] 未初始化")
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
        """
        OCR 识别文字

        Args:
            expected: 期望识别的文字
            roi: 识别区域
            match_mode: 匹配模式
                - "exact": 精确匹配整个文字
                - "contains": 识别结果包含 expected 即可（逐字匹配）

        Returns:
            识别成功返回区域中心坐标，失败返回 None
        """
        image = self.screenshot()
        if image is None:
            self._record_ocr("match", roi, [], expected, match_mode, False,
                             "screenshot_unavailable")
            return None

        try:
            ocr_job = self._wait_job(
                self.tasker.post_recognition(
                    JRecognitionType.OCR,
                    JOCR(roi=roi.to_tuple()),
                    image,
                ),
                timeout=self.RECOGNIZE_TIMEOUT, label=f'OCR "{expected}"')
            if ocr_job is None:
                self._note_recognize_timeout()
                self._record_ocr("match", roi, [], expected, match_mode, False,
                                 "recognition_timeout")
                return None
            self._maa_timeouts = 0
            ocr_detail = ocr_job.get()
            ocr_reco = ocr_detail.nodes[0].recognition if ocr_detail and ocr_detail.nodes else None

            if ocr_reco is None or not ocr_reco.hit:
                print(f"[MAA] OCR 未命中: expected=\"{expected}\" in {roi.to_list()}")
                self._record_ocr("match", roi, [], expected, match_mode, False)
                return None

            # 检查所有识别结果，不只是 best_result
            all_results = ocr_reco.all_results if hasattr(ocr_reco, 'all_results') else []
            if ocr_reco.best_result:
                all_results = [ocr_reco.best_result] + [r for r in all_results if r != ocr_reco.best_result]
            structured = self._structured_ocr_tokens(all_results, roi)

            # 遍历所有结果，找包含目标文字的
            for result in all_results:
                text = result.text if hasattr(result, 'text') else str(result)
                score = result.score if hasattr(result, 'score') else 0.0

                matched = _ocr_text_matches(text, expected, match_mode)

                if matched:
                    # 获取位置（兼容对象和列表两种格式）
                    center = roi.center  # 默认用 ROI 中心
                    if hasattr(result, 'box') and result.box:
                        box = result.box
                        # box 可能是对象（有 .x .y .w .h）或列表 [x, y, w, h]
                        if hasattr(box, 'x'):
                            center = Point(box.x + box.w // 2, box.y + box.h // 2)
                        elif isinstance(box, (list, tuple)) and len(box) >= 4:
                            center = Point(box[0] + box[2] // 2, box[1] + box[3] // 2)

                    _safe_print(f'[MAA] OCR 命中: "{text}" (目标: "{expected}") '
                                f'at ({center.x}, {center.y}), score={score:.3f}')
                    self._record_ocr("match", roi, structured, expected,
                                     match_mode, True)
                    return center

            # 没有匹配到
            texts = [r.text if hasattr(r, 'text') else str(r) for r in all_results]
            _safe_print(f"[MAA] OCR 识别到文字但未匹配: {texts}, expected=\"{expected}\"")
            self._record_ocr("match", roi, structured, expected,
                             match_mode, False)
            return None

        except Exception as exc:
            _safe_print(f"[MAA 错误] OCR 异常: {exc}")
            self._record_ocr("match", roi, [], expected, match_mode, False,
                             type(exc).__name__)
            return None

    def ocr_all(self, roi: Region, image=None) -> list:
        """
        识别区域内所有文字（不筛选，全都要）

        用途：读取界面上的信息文本（第几部队、地图名、刀名），
        拿到文字后由上层自己做解析判断。

        Args:
            roi: 识别区域
            image: 可选，直接对这张预处理过的图（如放大后的裁剪块）识别，
                   此时 roi 是相对该图的坐标。不给就实时截图。

        Returns:
            [(文本, 中心点Point), ...]，识别失败返回 []
        """
        if image is None:
            image = self.screenshot()
        if image is None:
            self._record_ocr("all", roi, [], error="screenshot_unavailable")
            return []

        try:
            ocr_job = self._wait_job(
                self.tasker.post_recognition(
                    JRecognitionType.OCR,
                    JOCR(roi=roi.to_tuple()),
                    image,
                ),
                timeout=self.RECOGNIZE_TIMEOUT, label="OCR 全量识别")
            if ocr_job is None:
                self._note_recognize_timeout()
                self._record_ocr("all", roi, [], error="recognition_timeout")
                return []
            self._maa_timeouts = 0
            ocr_detail = ocr_job.get()
            ocr_reco = ocr_detail.nodes[0].recognition if ocr_detail and ocr_detail.nodes else None

            if ocr_reco is None or not ocr_reco.hit:
                self._record_ocr("all", roi, [])
                return []

            all_results = ocr_reco.all_results if hasattr(ocr_reco, 'all_results') else []
            if ocr_reco.best_result:
                all_results = [ocr_reco.best_result] + [r for r in all_results if r != ocr_reco.best_result]

            out = []
            for result in all_results:
                text = result.text if hasattr(result, 'text') else str(result)
                center = roi.center
                if hasattr(result, 'box') and result.box:
                    box = result.box
                    if hasattr(box, 'x'):
                        center = Point(box.x + box.w // 2, box.y + box.h // 2)
                    elif isinstance(box, (list, tuple)) and len(box) >= 4:
                        center = Point(box[0] + box[2] // 2, box[1] + box[3] // 2)
                out.append((text, center))

            _safe_print(f"[MAA] OCR 全量识别: {[t for t, _ in out]}")
            self._record_ocr("all", roi,
                             self._structured_ocr_tokens(all_results, roi))
            return out

        except Exception as exc:
            _safe_print(f"[MAA 错误] OCR 全量识别异常: {exc}")
            self._record_ocr("all", roi, [], error=type(exc).__name__)
            return []

    @staticmethod
    def _structured_ocr_tokens(results, roi: Region) -> list[dict]:
        """Convert Maa result objects to the stable, JSON-safe observation shape."""
        tokens = []
        for result in results:
            text = result.text if hasattr(result, "text") else str(result)
            score = float(result.score) if hasattr(result, "score") else None
            center = roi.center
            box_value = None
            box = getattr(result, "box", None)
            if box:
                if hasattr(box, "x"):
                    box_value = [int(box.x), int(box.y), int(box.w), int(box.h)]
                    center = Point(box.x + box.w // 2, box.y + box.h // 2)
                elif isinstance(box, (list, tuple)) and len(box) >= 4:
                    box_value = [int(v) for v in box[:4]]
                    center = Point(box[0] + box[2] // 2, box[1] + box[3] // 2)
            tokens.append({"text": str(text), "score": score,
                           "center": [int(center.x), int(center.y)],
                           "box": box_value})
        return tokens

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

    def _template_match_cv2(self, template: str, roi: Optional[Region],
                            threshold: float):
        """模板匹配（自研 cv2 引擎）；返回 (x, y, score) 或 None。"""
        hit = self._template_match_cv2_raw(template, roi)
        if hit is None or hit[2] < threshold:
            return None
        return hit

    def _template_match_cv2_raw(self, template: str, roi: Optional[Region]):
        """cv2 引擎不过阈值的最佳匹配 (x, y, score)；ab 诊断要原始分。"""
        from . import vision_cv2
        image = self.screenshot()
        if image is None:
            return None
        path = Path(self.resource_dir) / 'image' / template
        try:
            return vision_cv2.best(
                image, path, roi.to_tuple() if roi else None)
        except FileNotFoundError:
            print(f"[cv2模板] 模板不存在: {template}")
            return None
        except Exception as exc:
            print(f"[cv2模板] 匹配异常: {exc}")
            return None

    def template_match(self, template: str, roi: Optional[Region] = None,
                       threshold: float = 0.7) -> Optional[Point]:
        """模板匹配入口：按 _template_engine 路由（maa/cv2/ab 双跑）。"""
        engine = self._template_engine
        if engine == 'cv2':
            hit = self._template_match_cv2(template, roi, threshold)
            if hit is None:
                print(f"[cv2] 模板未命中: {template}, threshold={threshold}")
                return None
            print(f"[cv2] 模板命中: {template} at ({hit[0]}, {hit[1]}), "
                  f"score={hit[2]:.3f}")
            return Point(hit[0], hit[1])
        if engine == 'ab':
            maa_point, maa_score = self._template_match_maa_detail(
                template, roi, threshold)
            raw = self._template_match_cv2_raw(template, roi)
            cv2_score = raw[2] if raw else None
            cv2_hit = (cv2_score is not None and cv2_score >= threshold)
            cv2_desc = (f'命中({raw[0]},{raw[1]})' if cv2_hit else '未命中')
            if cv2_score is not None:
                cv2_desc += f' s={cv2_score:.3f}'
            maa_desc = (f'命中({maa_point.x},{maa_point.y})'
                        if maa_point else '未命中')
            if maa_score is not None:
                maa_desc += f' s={maa_score:.3f}'
            print(f"[AB模板] {template}: maa={maa_desc} cv2={cv2_desc}")
            return maa_point
        return self._template_match_maa(template, roi, threshold)

    def _template_match_maa_detail(self, template: str, roi: Optional[Region],
                                   threshold: float):
        """ab 诊断用：MAA 任务阈值压到 0.3 拿原始分，命中判定按调用阈值在
        Python 侧做。返回 (Point|None, score|None)。"""
        image = self.screenshot()
        if image is None:
            return None, None
        roi_tuple = roi.to_tuple() if roi else (0, 0, 0, 0)
        try:
            tm_job = self._wait_job(
                self.tasker.post_recognition(
                    JRecognitionType.TemplateMatch,
                    JTemplateMatch(
                        template=[template],
                        roi=roi_tuple,
                        threshold=[min(threshold, 0.3)]
                    ),
                    image,
                ),
                timeout=self.RECOGNIZE_TIMEOUT, label=f"模板匹配 {template}")
            if tm_job is None:
                self._note_recognize_timeout()
                return None, None
            self._maa_timeouts = 0
            tm_detail = tm_job.get()
            tm_reco = tm_detail.nodes[0].recognition if tm_detail and tm_detail.nodes else None
            if tm_reco is None or tm_reco.best_result is None:
                return None, None
            score = float(tm_reco.best_result.score)
            box = tm_reco.box or getattr(tm_reco.best_result, "box", None)
            if score < threshold or box is None:
                return None, score
            return Point(box.x + box.w // 2, box.y + box.h // 2), score
        except Exception as exc:
            print(f"[MAA 错误] 模板匹配异常: {exc}")
            return None, None

    def _template_match_maa(self, template: str, roi: Optional[Region] = None,
                            threshold: float = 0.7) -> Optional[Point]:
        """
        模板匹配（MAA 引擎）

        Args:
            template: 模板图片名（相对 resource/base/image 目录）
            roi: 搜索区域，None 表示全屏
            threshold: 匹配阈值

        Returns:
            匹配成功返回中心坐标，失败返回 None
        """
        image = self.screenshot()
        if image is None:
            return None

        roi_tuple = roi.to_tuple() if roi else (0, 0, 0, 0)

        try:
            tm_job = self._wait_job(
                self.tasker.post_recognition(
                    JRecognitionType.TemplateMatch,
                    JTemplateMatch(
                        template=[template],
                        roi=roi_tuple,
                        threshold=[threshold]
                    ),
                    image,
                ),
                timeout=self.RECOGNIZE_TIMEOUT, label=f"模板匹配 {template}")
            if tm_job is None:
                self._note_recognize_timeout()
                return None
            self._maa_timeouts = 0
            tm_detail = tm_job.get()
            tm_reco = tm_detail.nodes[0].recognition if tm_detail and tm_detail.nodes else None

            if tm_reco is None or not tm_reco.hit or tm_reco.box is None:
                print(f"[MAA] 模板未命中: {template} in {roi.to_list() if roi else 'full'}, threshold={threshold}")
                return None

            box = tm_reco.box
            center = Point(box.x + box.w // 2, box.y + box.h // 2)
            score = tm_reco.best_result.score if tm_reco.best_result else 0.0
            print(f"[MAA] 模板命中: {template} at ({center.x}, {center.y}), score={score:.3f}")
            return center

        except Exception as exc:
            print(f"[MAA 错误] 模板匹配异常: {exc}")
            return None

    def template_match_score(self, template: str, roi: Optional[Region] = None,
                             threshold: float = 0.5) -> float:
        """模板匹配只取分数（编队页伤势章"比分数选最佳"分类用）。

        以较低阈值获取候选的真实分；未识别出结果或超时返回 0.0，
        调用方自行做阈值与区分度判定。
        """
        engine = self._template_engine
        if engine == 'cv2':
            from . import vision_cv2
            image = self.screenshot()
            if image is None:
                return 0.0
            path = Path(self.resource_dir) / 'image' / template
            try:
                return vision_cv2.match_score(
                    image, path, roi.to_tuple() if roi else None, threshold)
            except Exception as exc:
                print(f"[cv2模板] 分数异常: {exc}")
                return 0.0
        if engine == 'ab':
            maa_score = self._template_match_score_maa(template, roi, threshold)
            raw = self._template_match_cv2_raw(template, roi)
            print(f"[AB模板分] {template}: maa={maa_score:.3f} "
                  f"cv2={raw[2] if raw else 0.0:.3f}")
            return maa_score
        return self._template_match_score_maa(template, roi, threshold)

    def _template_match_score_maa(self, template: str, roi: Optional[Region] = None,
                                  threshold: float = 0.5) -> float:
        """模板匹配只取分数（MAA 引擎）。"""
        image = self.screenshot()
        if image is None:
            return 0.0
        roi_tuple = roi.to_tuple() if roi else (0, 0, 0, 0)
        try:
            tm_job = self._wait_job(
                self.tasker.post_recognition(
                    JRecognitionType.TemplateMatch,
                    JTemplateMatch(
                        template=[template],
                        roi=roi_tuple,
                        threshold=[threshold]
                    ),
                    image,
                ),
                timeout=self.RECOGNIZE_TIMEOUT, label=f"模板分数 {template}")
            if tm_job is None:
                self._note_recognize_timeout()
                return 0.0
            self._maa_timeouts = 0
            tm_detail = tm_job.get()
            tm_reco = tm_detail.nodes[0].recognition if tm_detail and tm_detail.nodes else None
            if tm_reco is None or not tm_reco.best_result:
                return 0.0
            return float(tm_reco.best_result.score)
        except Exception as exc:
            print(f"[MAA 错误] 模板分数异常: {exc}")
            return 0.0

    def exists(self, template: str, roi: Optional[Region] = None,
               threshold: float = 0.7) -> bool:
        """判断模板是否存在"""
        result = self.template_match(template, roi, threshold)
        return result is not None
