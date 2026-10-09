"""Single executable entry point for launcher and panel child mode."""

import os
import sys
from pathlib import Path


_WORKER_MODE = "--worker" in sys.argv


def _make_stdio_safe():
    """后台提示中的 emoji 不应因 Windows GBK 控制台而终止程序。"""
    if _WORKER_MODE:
        # 工人进程的 stdout 是父进程架着的管道，是它唯一的汇报通道，
        # 不能往 devnull 扔（worker.py 的 _rescue_stdout 会自己接 fd 1/2）
        return
    # PyInstaller 的 windowed 模式会把二者设为 None；第三方模块仍可能 print。
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (OSError, ValueError):
                pass


_make_stdio_safe()


if getattr(sys, "frozen", False) and "--update-probe" not in sys.argv:
    data_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Maamaru"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        # 受限电脑/便携运行时退回 EXE 旁边，至少保证能够启动。
        data_dir = Path(sys.executable).resolve().parent / "data"
    os.environ.setdefault("MAAMARU_DATA_DIR", str(data_dir))


def main():
    if "--update-probe" in sys.argv:
        import tempfile
        try:
            with tempfile.TemporaryDirectory(prefix="maamaru-probe-", ignore_cleanup_errors=True) as probe_data:
                os.environ["MAAMARU_DATA_DIR"] = probe_data
                os.environ["LOCALAPPDATA"] = probe_data
                from launcher import app
                from panel import server, worker
                from touken.runtime_paths import BUNDLE_ROOT
                if not (BUNDLE_ROOT / "panel/static").is_dir():
                    raise SystemExit(1)
        except Exception:
            raise SystemExit(1)
        raise SystemExit(0)
    elif "--apply-update" in sys.argv:
        from launcher.update_apply import run_plan
        plan_index = sys.argv.index("--apply-update") + 1
        raise SystemExit(run_plan(Path(sys.argv[plan_index])))
    elif "--worker" in sys.argv:
        # 工人子进程：跑脚本、把 yield 逐行 print 给父进程（面板）收
        from panel.worker import main as worker_main
        worker_main()
    elif "--ledger" in sys.argv:
        from maamaru_app import main as panel_main
        panel_main(ledger_mode=True)
    elif "--panel" in sys.argv:
        from maamaru_app import main as panel_main
        panel_main()
    else:
        from launcher.app import main as launcher_main
        launcher_main()


if __name__ == "__main__":
    main()
