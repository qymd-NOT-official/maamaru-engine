"""Hand an already verified installer to a detached updater with rollback."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from touken.runtime_paths import DATA_ROOT, UPDATES_DIR


RESULT_PATH = UPDATES_DIR / "last-result.json"


class ApplyError(RuntimeError):
    """The staged installer or update plan is unsafe to apply."""


def prepare_apply(installer: Path, expected_sha256: str, version: str, kind: str = "installer") -> dict:
    """Recheck a staged installer, create a plan, and launch the detached helper."""
    installer = Path(installer).resolve()
    updates = UPDATES_DIR.resolve()
    if not installer.is_file() or not installer.is_relative_to(updates):
        raise ApplyError("安装包不在まあ丸的更新暂存区")
    if _sha256(installer) != expected_sha256.lower():
        raise ApplyError("安装前复核失败，安装包可能已经发生变化")

    program_dir = _program_dir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = updates / "backups" / f"before-{version}-{stamp}"
    plan_path = updates / f"apply-{version}.json"
    plan = {
        "kind": kind,
        "version": version,
        "installer": str(installer),
        "sha256": expected_sha256.lower(),
        "program_dir": str(program_dir),
        "backup_dir": str(backup_dir),
        "previous_executable": str(Path(sys.executable).resolve()),
        "parent_pid": os.getpid(),
        "data_root": str(DATA_ROOT.resolve()),
    }
    _validate_plan(plan)
    _write_json(plan_path, plan)

    if getattr(sys, "frozen", False):
        # Directory bundles need their native dependencies next to the helper.
        # Never execute the helper from the installation being replaced.
        helper_dir = updates / ("helper-" + str(time.time_ns()))
        helper_dir.mkdir()
        internal = Path(sys.executable).resolve().parent / "_internal"
        if internal.is_dir():
            shutil.copytree(internal, helper_dir / "_internal")
        helper = helper_dir / "maamaru-update-helper.exe"
        shutil.copy2(Path(sys.executable).resolve(), helper)
        command = [str(helper), "--apply-update", str(plan_path)]
    else:
        command = [sys.executable, "-m", "launcher.update_apply", str(plan_path)]
    subprocess.Popen(command, close_fds=True, creationflags=_detached_flags())
    return {"plan": str(plan_path), "program_dir": str(program_dir)}


def run_plan(plan_path: Path) -> int:
    """Wait for the launcher, snapshot its program directory, then run Inno Setup."""
    plan: dict = {}
    try:
        loaded = json.loads(Path(plan_path).read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError("更新计划格式不正确")
        plan = loaded
        _validate_plan(plan)
    except (OSError, ValueError, KeyError, ApplyError) as exc:
        # 校验失败也要留下结果并把旧启动器拉回来，不能裸崩在 PyInstaller 弹窗上。
        _record_result(False, plan, f"更新计划校验失败：{exc}", rolled_back=False)
        previous = plan.get("previous_executable")
        if previous:
            _restart(Path(previous))
        return 3
    installer = Path(plan["installer"])
    program_dir = Path(plan["program_dir"])
    backup_dir = Path(plan["backup_dir"])
    previous_executable = Path(plan["previous_executable"])

    try:
        _wait_for_process(int(plan["parent_pid"]), timeout=30)
    except ApplyError as exc:
        _record_result(False, plan, str(exc), rolled_back=False)
        return 2
    if _sha256(installer) != plan["sha256"]:
        _record_result(False, plan, "安装前复核失败，未运行安装器", rolled_back=False)
        _restart(previous_executable)
        return 2

    if plan.get("kind") == "light":
        from .light_update import apply_package
        before_hash = _sha256(previous_executable)
        try:
            apply_package(installer, program_dir, backup_dir, plan["version"])
        except Exception as exc:
            restored = previous_executable.is_file() and _sha256(previous_executable) == before_hash
            _record_result(False, plan, f"轻量更新未完成：{exc}。可重新检查更新或使用完整安装包。", rolled_back=restored)
            _restart(previous_executable)
            return 1
        _record_result(True, plan, f"已更新到 v{plan['version']}", rolled_back=False)
        _restart(program_dir / "まあ丸启动器.exe")
        return 0

    had_program = program_dir.is_dir()
    if had_program:
        shutil.copytree(program_dir, backup_dir, dirs_exist_ok=False)

    result = subprocess.run([
        str(installer),
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
        f"/DIR={program_dir}",
    ], check=False)
    if result.returncode == 0:
        target = program_dir / "まあ丸启动器.exe"
        _record_result(True, plan, f"已安装 v{plan['version']}", rolled_back=False)
        _restart(target)
        return 0

    rolled_back = False
    if had_program and backup_dir.is_dir():
        shutil.rmtree(program_dir, ignore_errors=True)
        shutil.copytree(backup_dir, program_dir)
        rolled_back = True
    message = f"安装器返回错误码 {result.returncode}"
    _record_result(False, plan, message, rolled_back=rolled_back)
    restart = program_dir / "まあ丸启动器.exe" if rolled_back else previous_executable
    _restart(restart)
    return result.returncode or 1


def consume_result() -> dict | None:
    try:
        result = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
        RESULT_PATH.unlink(missing_ok=True)
        return result
    except (OSError, ValueError, TypeError):
        return None


def _validate_plan(plan: dict) -> None:
    updates = UPDATES_DIR.resolve()
    installer = Path(plan["installer"]).resolve()
    backup = Path(plan["backup_dir"]).resolve()
    data_root = Path(plan["data_root"]).resolve()
    if not installer.is_relative_to(updates) or not backup.is_relative_to(updates / "backups"):
        raise ApplyError("更新计划指向了暂存区以外的文件")
    if data_root != DATA_ROOT.resolve():
        raise ApplyError("更新计划的用户数据目录不匹配")
    if plan.get("kind", "installer") not in ("installer", "light"):
        raise ApplyError("更新方式无效")
    program_dir = Path(plan["program_dir"]).resolve()
    if program_dir != _program_dir().resolve():
        # 更新助手是启动器复制到暂存区的副本，自身旁边没有 manifest.json，
        # 无法从自身位置推回自定义过的安装目录；此时改为要求计划指向
        # 一个真实存在的まあ丸安装目录（安装器总会安放 manifest.json）。
        if _has_local_manifest() or not (program_dir / "manifest.json").is_file():
            raise ApplyError("更新计划的程序目录不匹配")
    if (program_dir == Path(program_dir.anchor) or program_dir.is_relative_to(data_root)
            or data_root.is_relative_to(program_dir)):
        raise ApplyError("程序目录与用户数据目录不能互相包含")


def _program_dir() -> Path:
    if getattr(sys, "frozen", False):
        current = Path(sys.executable).resolve().parent
        if (current / "manifest.json").is_file():
            return current
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    return (local / "Programs" / "Maamaru").resolve()


def _has_local_manifest() -> bool:
    """当前进程自身是否坐在一个真实的安装目录里（更新助手副本为 False）。"""
    if not getattr(sys, "frozen", False):
        return False
    return (Path(sys.executable).resolve().parent / "manifest.json").is_file()


def _wait_for_process(pid: int, timeout: int) -> None:
    if os.name != "nt":
        return
    synchronize = 0x00100000
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(synchronize, False, pid)
    if handle:
        try:
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, timeout * 1000)
            if result != 0:
                raise ApplyError("旧启动器尚未退出，更新未开始")
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)


def _restart(executable: Path) -> None:
    if executable.is_file():
        subprocess.Popen([str(executable)], close_fds=True, creationflags=_detached_flags())


def _detached_flags() -> int:
    return getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _record_result(ok: bool, plan: dict, message: str, rolled_back: bool) -> None:
    backup_dir = str(plan.get("backup_dir", ""))
    _write_json(RESULT_PATH, {
        "ok": ok,
        "kind": plan.get("kind", "installer"),
        "version": plan.get("version", "?"),
        "message": message,
        "rolled_back": rolled_back,
        "backup_dir": backup_dir if backup_dir and Path(backup_dir).is_dir() else None,
    })


if __name__ == "__main__":
    raise SystemExit(run_plan(Path(sys.argv[1])))
