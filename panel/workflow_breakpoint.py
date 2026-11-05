"""任务流翻车断点：现场失控收摊后落盘，玩家修好环境可从翻车处续跑。

与 workflow_waits（定时等待续跑）不同，断点不自动复活——翻车多半说明
模拟器/游戏现场有毛病，等玩家确认修好后从面板手动续跑。
"""

import json
import shutil
import threading
import time
from pathlib import Path

from touken.runtime_paths import STATE_DIR

PATH = STATE_DIR / "workflow_breakpoint.json"
LOCK = threading.RLock()


def _valid_resume(resume):
    """续跑记录完整性校验；缺胳膊少腿的断点不许续跑（不猜着跑）。"""
    if not isinstance(resume, dict) or resume.get("reason") != "failure":
        return False
    nodes = resume.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False
    index = resume.get("next_index")
    if type(index) is not int or not 0 <= index < len(nodes):
        return False
    report = resume.get("report")
    if not isinstance(report, list) or len(report) != index:
        return False
    return True


def save(run_id, workflow_id, config_path, params, resume, failed_label, path=None):
    if not _valid_resume(resume):
        return False
    # workflow_resume/workflow_wait_root 是上一轮的运行态，存进断点会在续跑时
    # 触发 start() 的等待状态检查或嵌套续跑，必须剔除。
    clean = {key: value for key, value in (params or {}).items()
             if key not in ("workflow_resume", "workflow_wait_root")}
    record = {"version": 1, "breakpoint": {
        "run_id": run_id, "workflow_id": workflow_id,
        "config_path": str(config_path), "params": clean,
        "resume": resume, "failed_label": str(failed_label),
        "saved_at": time.time()}}
    with LOCK:
        target = Path(path or PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        if target.exists():
            shutil.copy2(target, target.with_suffix(".json.bak"))
        temporary.replace(target)
    return True


def load(path=None):
    try:
        raw = json.loads(Path(path or PATH).read_text(encoding="utf-8"))
        if raw.get("version") != 1 or not isinstance(raw.get("breakpoint"), dict):
            return None
        bp = raw["breakpoint"]
        if not _valid_resume(bp.get("resume")):
            return None
        if not isinstance(bp.get("config_path"), str) or not isinstance(bp.get("params"), dict):
            return None
        return bp
    except (OSError, ValueError, AttributeError):
        return None


def clear(workflow_id=None, path=None):
    """驳回断点。带上 workflow_id 时只清同一条任务流的断点，不误伤别家。"""
    with LOCK:
        bp = load(path)
        if bp is None:
            return False
        if workflow_id is not None and bp.get("workflow_id") != workflow_id:
            return False
        try:
            Path(path or PATH).unlink()
        except OSError:
            pass
        return True
