# -*- coding: utf-8 -*-
"""
脚本工人进程入口（2026-08-05，卡死修复的一部分）

面板不再在本进程线程里跑脚本，而是每个脚本起一个子进程：
  - 识别/设备通道一旦卡死，看门狗直接 kill 子进程，面板毫发无损
  - 「紧急停止」= 真杀进程，立刻停，不用等 generator 下次 yield
  - 工人崩了只死工人，面板 / 调度器 / QQ/TG 机器人都活着

工人只干一件事：按名字找到注册的脚本，跑 generator，把每条 yield
print 到 stdout（父进程逐行收走，落 SQLite + SSE 广播 + 终端）。

用法：
  源码: python -m panel.worker <script> <config_path>
  exe : まあ丸.exe --worker <script> <config_path>
参数走环境变量 MAAMARU_WORKER_PARAMS（JSON），避开命令行中文/长度坑。
"""

import json
import os
import sys
from pathlib import Path


def _rescue_stdout():
    """PyInstaller windowed 模式 stdout/stderr 是 None，
    可父进程还架着管道等读呢——手动接回 fd 1/2。接不上再退 devnull。"""
    if sys.stdout is None:
        try:
            sys.stdout = os.fdopen(1, "w", encoding="utf-8",
                                   errors="replace", buffering=1)
        except OSError:
            sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        try:
            sys.stderr = os.fdopen(2, "w", encoding="utf-8",
                                   errors="replace", buffering=1)
        except OSError:
            sys.stderr = open(os.devnull, "w", encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def main():
    _rescue_stdout()
    # 保证能 import panel / touken 包（源码模式下 cwd 不一定是项目根）
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    argv = sys.argv[1:]
    if argv and argv[0] == "--worker":
        argv = argv[1:]
    if len(argv) < 2:
        print("[工人] 参数不够：<script> <config_path>", flush=True)
        sys.exit(2)

    script_name, config_path = argv[0], argv[1]
    try:
        params = json.loads(os.environ.get("MAAMARU_WORKER_PARAMS", "{}") or "{}")
    except json.JSONDecodeError:
        params = {}

    # import panel.server 即触发全部 register_script（不会起 HTTP 服务）
    # 注意：_SCRIPTS 是 panel.script_runner 的模块级字典，server 只是往里注册，
    # 所以要从 script_runner 拿，不能 from .server import _SCRIPTS（会 ImportError）
    from . import server as _server  # noqa: F401
    from .script_runner import _SCRIPTS
    from touken.flow_control import FlowAborted
    from .workflow import WorkflowPaused
    info = _SCRIPTS.get(script_name)
    if not info:
        print(f"[工人] 不认识的脚本: {script_name}", flush=True)
        sys.exit(2)

    print(f"[工人] 上岗：{info['label']}（pid {os.getpid()}）", flush=True)
    try:
        for msg in info["fn"](config_path, params):
            print(msg, flush=True)
    except WorkflowPaused as exc:
        print("@@MAAMARU_WORKFLOW_WAIT@@" + json.dumps({"params": params,
            "config_path": config_path, "wake_at": exc.wake_at, "resume": exc.resume}, ensure_ascii=False), flush=True)
        sys.exit(44)
    except FlowAborted as exc:
        print(f"[工人] 安全停止：{exc}", flush=True)
        sys.exit(42)
    except SystemExit:
        raise
    except Exception as exc:
        import traceback
        print(f"[工人] 脚本崩了: {exc}", flush=True)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
