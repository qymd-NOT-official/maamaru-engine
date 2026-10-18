"""Fast, read-only health checks used by the launcher UI."""

import importlib.util
import json
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from touken.runtime_paths import CONFIG_DIR, CONFIG_PATH, DATA_VERSION_PATH, MIGRATION_PATH, RESOURCE_DIR


@dataclass
class Check:
    key: str
    label: str
    state: str
    detail: str


def _port_open(port: int = 8080) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _adb_check(config: dict) -> Check:
    adb = Path(str(config.get("adb_path", "")))
    address = str(config.get("adb_address", "127.0.0.1:16384"))
    if not adb.is_file():
        return Check("emulator", "模拟器", "warn", "未找到配置中的 ADB；启动任务前需要设置")
    try:
        result = subprocess.run(
            [str(adb), "-s", address, "shell", "wm", "size"],
            capture_output=True, timeout=4, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (result.stdout or result.stderr or "").strip()
        if result.returncode == 0 and "size" in output.lower():
            size = output.splitlines()[-1].split(":", 1)[-1].strip()
            return Check("emulator", "模拟器", "ok", f"ADB 已连接 · {size}")
        return Check("emulator", "模拟器", "warn", "已找到 ADB，但模拟器当前未连接")
    except Exception:
        return Check("emulator", "模拟器", "warn", "模拟器连接检查超时")


def run_checks() -> list[Check]:
    config = _config()
    checks = []
    required = [
        CONFIG_PATH,
        RESOURCE_DIR / "model" / "ocr" / "rec.onnx",
        RESOURCE_DIR / "image",
    ]
    missing = [path.name for path in required if not path.exists()]
    checks.append(Check(
        "files", "程序文件", "ok" if not missing else "error",
        "核心配置与识别资源完整" if not missing else "缺少：" + "、".join(missing),
    ))
    migrated = 0
    try:
        migrated = int(json.loads(MIGRATION_PATH.read_text(encoding="utf-8")).get("copied", 0))
    except (OSError, ValueError, TypeError):
        pass
    data_detail = "配置与程序文件已分开保存"
    if migrated:
        data_detail += f"；已安全迁移 {migrated} 项旧数据"
    checks.append(Check(
        "data", "用户数据", "ok" if CONFIG_DIR.is_dir() and DATA_VERSION_PATH.is_file() else "error",
        data_detail if CONFIG_DIR.is_dir() and DATA_VERSION_PATH.is_file()
        else "用户数据目录尚未正确建立",
    ))

    if getattr(sys, "frozen", False):
        checks.append(Check("runtime", "运行环境", "ok", "Python 与依赖已内置"))
    else:
        modules = ("fastapi", "uvicorn", "webview", "maa",
                   "rapidocr_onnxruntime")
        absent = [name for name in modules if importlib.util.find_spec(name) is None]
        checks.append(Check(
            "runtime", "运行环境", "ok" if not absent else "error",
            "开发环境依赖完整" if not absent else "缺少依赖：" + "、".join(absent),
        ))

    checks.append(Check(
        "port", "面板端口", "ok" if not _port_open() else "info",
        "8080 可用" if not _port_open() else "面板已在运行，可直接打开",
    ))
    checks.append(_adb_check(config))

    manager = Path(str(config.get("emulator_manager", "")))
    checks.append(Check(
        "manager", "模拟器管理器", "ok" if manager.is_file() else "warn",
        "可自动启动模拟器" if manager.is_file() else "未找到，暂时只能手动启动模拟器",
    ))
    return checks


def has_blocker(checks: list[Check]) -> bool:
    return any(item.state == "error" for item in checks)
