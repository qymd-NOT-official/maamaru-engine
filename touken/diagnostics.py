"""Create a small, privacy-conscious bundle for user bug reports."""

from __future__ import annotations

import io
import json
import os
import platform
import re
import sqlite3
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from launcher.version import CURRENT_VERSION
from touken.runtime_paths import BUNDLE_ROOT, DATA_ROOT, DEBUG_DIR, LOG_DIR


_TEXT_TAIL_BYTES = 512 * 1024
_DEBUG_FILE_LIMIT = 6
_PANEL_LOG_LIMIT = 1000
_SECRET_PATTERN = re.compile(
    r"(?i)(token|secret|password|passwd|api[_-]?key|access[_-]?key)"
    r"(\s*[\"']?\s*[:=]\s*[\"']?)([^\s,;\"']+)"
)
_BEARER_PATTERN = re.compile(r"(?i)(authorization\s*:\s*bearer\s+)([^\s,;\"']+)")
_TOKEN_SHAPE_PATTERN = re.compile(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{12,}\b")
_WINDOWS_USER_PATTERN = re.compile(r"(?i)[A-Z]:\\Users\\[^\\\r\n]+")


@dataclass(frozen=True)
class DiagnosticBundle:
    filename: str
    content: bytes


def _replacement_paths(data_root: Path, bundle_root: Path) -> list[tuple[str, str]]:
    values = [
        (str(data_root.resolve()), "<DATA_ROOT>"),
        (str(bundle_root.resolve()), "<PROGRAM_ROOT>"),
        (str(Path.home().resolve()), "<USER_HOME>"),
    ]
    # Longest first so a child data path is not partially replaced by the home path.
    return sorted(set(values), key=lambda item: len(item[0]), reverse=True)


def sanitize_text(text: str, *, data_root: Path = DATA_ROOT, bundle_root: Path = BUNDLE_ROOT) -> str:
    """Hide common local identity paths and accidental credentials in text logs."""
    sanitized = str(text)
    for original, replacement in _replacement_paths(Path(data_root), Path(bundle_root)):
        sanitized = sanitized.replace(original, replacement)
        sanitized = sanitized.replace(original.replace("\\", "/"), replacement)
    sanitized = _WINDOWS_USER_PATTERN.sub("<USER_HOME>", sanitized)
    sanitized = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}<REDACTED>", sanitized)
    sanitized = _BEARER_PATTERN.sub(lambda match: f"{match.group(1)}<REDACTED>", sanitized)
    sanitized = _TOKEN_SHAPE_PATTERN.sub("<REDACTED>", sanitized)
    return sanitized


def _tail_text(path: Path, limit: int = _TEXT_TAIL_BYTES) -> str:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - limit))
        payload = stream.read(limit)
    text = payload.decode("utf-8", errors="replace")
    if size > limit:
        text = "[Earlier content omitted]\n" + text
    return text


def _recent_panel_logs(database: Path, limit: int = _PANEL_LOG_LIMIT) -> str:
    if not database.is_file():
        return "No panel log database was found.\n"
    try:
        with closing(sqlite3.connect(str(database), timeout=1)) as connection:
            rows = connection.execute(
                "SELECT ts, run_id, script, message FROM logs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return f"Panel logs could not be read: {type(exc).__name__}: {exc}\n"
    rows.reverse()
    if not rows:
        return "No panel logs have been recorded yet.\n"
    lines = []
    for timestamp, run_id, script, message in rows:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(timestamp)))
        lines.append(f"[{stamp}] [{script}] [{run_id}] {message}")
    return "\n".join(lines) + "\n"


def _debug_logs(debug_dir: Path) -> list[tuple[str, str]]:
    if not debug_dir.is_dir():
        return []
    candidates = [
        path for path in debug_dir.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in {".log", ".txt"}
    ]
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    output = []
    for index, path in enumerate(candidates[:_DEBUG_FILE_LIMIT], start=1):
        try:
            relative = path.relative_to(debug_dir).as_posix()
            content = f"Source file: {relative}\n\n{_tail_text(path)}"
            output.append((f"debug/{index:02d}.log", content))
        except OSError:
            continue
    return output


def _expedition_schedule_summary(data_root: Path, now: float | None = None) -> str:
    """排班开关、时刻表与各队派遣记录快照，用来隔空定位「排班没接管」。

    只含开关、时刻、地图编号和归来预估，不含任何敏感信息。
    """
    now = time.time() if now is None else now
    now_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    lines = ["远征排班状态摘要", f"生成时间: {now_text}", ""]

    try:
        raw_cfg = json.loads((Path(data_root) / "config" / "expedition.json").read_text(encoding="utf-8"))
        cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
    except (OSError, ValueError):
        cfg = {}
        lines.append("- 未找到排班配置文件：从未保存过排班设置，自动排班按默认关闭")

    auto = cfg.get("automation")
    auto = auto if isinstance(auto, dict) else {}
    enabled = bool(auto.get("enabled", False))
    lines.append(f"- 自动排班总开关: {'开' if enabled else '关 —— 今日单独安排的远征仍按安排执行；暂停可停止全部接管'}")
    paused_until = str(auto.get("paused_until", "") or "").strip()
    if paused_until:
        state = "暂停生效中" if paused_until > now_text else "暂停已过期"
        lines.append(f"- 暂停: {paused_until}（{state}）")
    if str(auto.get("mode", "preset")) == "custom":
        entries = [e for e in cfg.get("entries", []) if isinstance(e, dict)]
        active = [e for e in entries if e.get("enabled", True)]
        lines.append(f"- 模式: 自定义时刻表，共 {len(entries)} 条（启用 {len(active)} 条）")
        for entry in entries[:10]:
            mark = "" if entry.get("enabled", True) else "（停用）"
            lines.append(f"  · {entry.get('time', '?')} 部队{entry.get('team_no', '?')} → {entry.get('map_code', '?')}{mark}")
    else:
        teams = "、".join(f"部队{t}" for t in auto.get("teams", [2, 3, 4]))
        capitalist = "开" if auto.get("capitalist") else "关"
        lines.append(f"- 模式: 攻略预设「{auto.get('preset', '小判')}」，开始 {auto.get('start_time', '08:00')}，"
                     f"部队 {teams}，资本家模式 {capitalist}")
    common = [c for c in cfg.get("common_plan", []) if isinstance(c, dict)]
    if common:
        rows = "、".join(
            f"部队{c.get('team_no', '?')}→{c.get('map_code', '?')}{'（启用）' if c.get('enabled') else '（停用）'}"
            for c in common
        )
        lines.append(f"- 常用安排（日课补派用）: {rows}")
    lines.append(f"- 已记录的排班完成次数: {len(auto.get('last_runs', {}) or {})}")

    records = {}
    try:
        raw_records = json.loads((Path(data_root) / "state" / "expeditions.json").read_text(encoding="utf-8"))
        if isinstance(raw_records, dict):
            records = raw_records
    except (OSError, ValueError):
        pass
    lines.append("")
    lines.append("派遣记录（仅估计归来时间，实际状态以游戏为准）:")
    if not records:
        lines.append("- （无派遣记录，无法估计归来时间）")

    def _sort_key(key):
        text = str(key)
        return (0, int(text)) if text.isdigit() else (1, text)

    for key in sorted(records, key=_sort_key):
        record = records[key]
        if not isinstance(record, dict):
            continue
        label = f"部队{key}"
        dispatched_at = record.get("dispatched_at")
        map_code = record.get("map_code")
        duration = record.get("duration_min")
        try:
            start_ts = time.mktime(time.strptime(str(dispatched_at), "%Y-%m-%d %H:%M:%S"))
            end_ts = start_ts + int(duration) * 60
            remaining = int((end_ts - now) / 60)
            back = time.strftime("%m-%d %H:%M", time.localtime(end_ts))
            if remaining > 0:
                lines.append(f"- {label}: {map_code}，派出于 {dispatched_at}，预计 {back} 归来（还剩约 {remaining} 分钟）")
            else:
                lines.append(f"- {label}: {map_code}，派出于 {dispatched_at}，预计已归来（以游戏为准）")
        except (TypeError, ValueError):
            lines.append(f"- {label}: 记录不完整（派出于 {dispatched_at}，地图 {map_code}，时长 {duration}），无法估计归来时间")
    return "\n".join(lines) + "\n"


def build_diagnostic_bundle(
    *,
    data_root: Path = DATA_ROOT,
    log_dir: Path = LOG_DIR,
    debug_dir: Path = DEBUG_DIR,
    bundle_root: Path = BUNDLE_ROOT,
    version: str = CURRENT_VERSION,
    frozen: bool | None = None,
) -> DiagnosticBundle:
    """Build an in-memory zip without copying configuration, screenshots, or raw databases."""
    data_root = Path(data_root)
    log_dir = Path(log_dir)
    debug_dir = Path(debug_dir)
    bundle_root = Path(bundle_root)
    generated = time.strftime("%Y-%m-%d %H:%M:%S %z")
    filename = f"maamaru-feedback-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    packaged = getattr(sys, "frozen", False) if frozen is None else frozen

    files: list[tuple[str, str]] = []
    launcher_log = log_dir / "launcher.log"
    if launcher_log.is_file():
        try:
            files.append(("launcher.log", _tail_text(launcher_log)))
        except OSError as exc:
            files.append(("launcher.log", f"Launcher log could not be read: {exc}\n"))
    else:
        files.append(("launcher.log", "No launcher failure has been recorded.\n"))
    files.append(("recent-panel-logs.txt", _recent_panel_logs(log_dir / "maamaru_logs.db")))
    files.append(("expedition-schedule.txt", _expedition_schedule_summary(data_root)))
    files.extend(_debug_logs(debug_dir))

    included = "\n".join(f"- {name}" for name, _ in files)
    summary = f"""まあ丸错误反馈包

Generated: {generated}
Maamaru version: {version}
Run mode: {'packaged installer' if packaged else 'source/development'}
Operating system: {platform.platform()}
Architecture: {platform.machine() or 'unknown'}
Python: {platform.python_version()}

Included files:
{included}

Privacy boundary:
- Includes text-only startup, recent task, and MaaFramework diagnostic logs, plus an expedition schedule digest (switches, times, map codes, return estimates).
- Does not include full configuration files, API keys, chat history, screenshots, inventory data, or raw databases.
- Local user paths and common credential fields are replaced before export.
"""
    files.insert(0, ("diagnostic-summary.txt", summary))

    replacements = {"data_root": data_root, "bundle_root": bundle_root}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, sanitize_text(content, **replacements).encode("utf-8"))
    return DiagnosticBundle(filename=filename, content=stream.getvalue())


def reveal_file_in_explorer(path: Path) -> bool:
    """在资源管理器里打开所在文件夹并选中文件；打不开退而打开文件夹。不抛异常。"""
    import subprocess
    path = Path(path)
    try:
        subprocess.Popen(
            ["explorer.exe", "/select,", str(path)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return True
    except OSError:
        startfile = getattr(os, "startfile", None)
        if startfile is None:
            return False
        try:
            startfile(path.parent)
            return True
        except OSError:
            return False


def create_diagnostic_bundle(destination_dir: Path | None = None, **kwargs) -> Path:
    """Write the bundle atomically and return a path suitable for Explorer selection."""
    target_dir = Path(destination_dir or (DATA_ROOT / "diagnostics"))
    target_dir.mkdir(parents=True, exist_ok=True)
    bundle = build_diagnostic_bundle(**kwargs)
    target = target_dir / bundle.filename
    suffix = 2
    while target.exists():
        target = target_dir / bundle.filename.replace(".zip", f"-{suffix}.zip")
        suffix += 1
    with tempfile.NamedTemporaryFile(dir=target_dir, suffix=".tmp", delete=False) as temporary:
        temporary.write(bundle.content)
        temporary_path = Path(temporary.name)
    temporary_path.replace(target)
    return target
