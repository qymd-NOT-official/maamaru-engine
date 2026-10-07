"""Program resources, writable user data, and legacy-data migration."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path


DATA_SCHEMA_VERSION = 1
BUNDLE_ROOT = Path(__file__).resolve().parent.parent
DATA_LOCATION_REGISTRY_KEY = r"Software\Maamaru"
DATA_LOCATION_VALUE = "DataRoot"
PREVIOUS_DATA_LOCATION_VALUE = "PreviousDataRoot"


def _local_app_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))


def _base_data_root() -> Path:
    name = "Maamaru" if getattr(sys, "frozen", False) else "Maamaru-Dev"
    return (_local_app_data() / name).resolve()


DEFAULT_DATA_ROOT = _base_data_root()


def _read_registry_path(value_name: str) -> Path | None:
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, DATA_LOCATION_REGISTRY_KEY) as key:
            value, value_type = winreg.QueryValueEx(key, value_name)
        if value_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
            return None
        expanded = os.path.expandvars(str(value).strip())
        path = Path(expanded).expanduser()
        if not path.is_absolute() or path == Path(path.anchor):
            return None
        return path.resolve()
    except (OSError, ValueError, TypeError):
        return None


def set_registered_data_root(data_root: Path, previous_root: Path) -> None:
    """Persist a packaged-install data location after a verified copy."""
    if os.name != "nt":
        raise OSError("用户数据目录迁移目前只支持 Windows")
    import winreg
    with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, DATA_LOCATION_REGISTRY_KEY,
            0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, DATA_LOCATION_VALUE, 0, winreg.REG_SZ,
                          str(Path(data_root).resolve()))
        winreg.SetValueEx(key, PREVIOUS_DATA_LOCATION_VALUE, 0, winreg.REG_SZ,
                          str(Path(previous_root).resolve()))


def clear_previous_data_root() -> None:
    if os.name != "nt":
        return
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, DATA_LOCATION_REGISTRY_KEY,
                0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, PREVIOUS_DATA_LOCATION_VALUE)
    except OSError:
        pass


def registered_previous_data_root() -> Path | None:
    return _read_registry_path(PREVIOUS_DATA_LOCATION_VALUE)


def _default_data_root() -> Path:
    override = os.environ.get("MAAMARU_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    configured = _read_registry_path(DATA_LOCATION_VALUE)
    return configured or DEFAULT_DATA_ROOT


DATA_ROOT = _default_data_root()
CONFIG_DIR = DATA_ROOT / "config"
STATE_DIR = DATA_ROOT / "state"
LOG_DIR = DATA_ROOT / "logs"
DEBUG_DIR = DATA_ROOT / "debug"
BACKUP_DIR = DATA_ROOT / "backups"
UPDATES_DIR = DATA_ROOT / "updates"
USER_PROFILES_DIR = DATA_ROOT / "profiles" / "overrides"
# 日服账号的独立数据根（账房库、状态、配置都收在这里，与国服互不可见）
JP_DATA_DIR = DATA_ROOT / "jp"

# Kept as an API alias while callers move to the clearer STATE_DIR name.
STATUS_DIR = STATE_DIR
CONFIG_PATH = CONFIG_DIR / "touken.json"
PANEL_CONFIG_PATH = CONFIG_DIR / "panel.json"
SCHEDULE_PATH = CONFIG_DIR / "expedition.json"
MIGRATION_PATH = DATA_ROOT / "migration.json"
DATA_VERSION_PATH = DATA_ROOT / "data-version.json"

RESOURCE_DIR = BUNDLE_ROOT / "resource" / "base"
BUNDLED_PROFILES_DIR = BUNDLE_ROOT / "profiles"
PROFILES_DIR = USER_PROFILES_DIR


@dataclass(frozen=True)
class _Layout:
    root: Path
    config: Path
    state: Path
    logs: Path
    debug: Path
    backups: Path
    updates: Path
    profiles: Path


def _layout(root: Path) -> _Layout:
    return _Layout(
        root=root,
        config=root / "config",
        state=root / "state",
        logs=root / "logs",
        debug=root / "debug",
        backups=root / "backups",
        updates=root / "updates",
        profiles=root / "profiles" / "overrides",
    )


def _write_json(path: Path, payload: dict) -> None:
    """Replace a small metadata file without exposing a half-written JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _ensure_data_version(path: Path) -> None:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if int(current.get("data_schema", 0)) == DATA_SCHEMA_VERSION:
            return
    except (OSError, ValueError, TypeError):
        pass
    _write_json(path, {
        "data_schema": DATA_SCHEMA_VERSION,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })


def _copy_missing_file(source: Path, target: Path, backup: Path | None = None) -> bool:
    if not source.is_file() or target.exists() or source.resolve() == target.resolve():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if backup is not None and not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup)
    shutil.copy2(source, target)
    return True


def _copy_missing_tree(
    source: Path,
    target: Path,
    backup: Path | None = None,
    skip_names: set[str] | None = None,
) -> list[str]:
    copied = []
    if not source.is_dir() or source.resolve() == target.resolve():
        return copied
    target_resolved = target.resolve()
    for item in source.rglob("*"):
        if not item.is_file():
            continue
        if skip_names and item.name in skip_names:
            continue
        if item.resolve().is_relative_to(target_resolved):
            continue
        relative = item.relative_to(source)
        destination = target / relative
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if backup is not None:
            backup_file = backup / relative
            backup_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, backup_file)
        shutil.copy2(item, destination)
        copied.append(relative.as_posix())
    return copied


def _legacy_roots(data_root: Path, bundle_root: Path) -> list[Path]:
    candidates = [data_root, data_root / "data", bundle_root, bundle_root / "data"]
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent)
    unique = []
    seen = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def migrate_legacy_data(
    data_root: Path = DATA_ROOT,
    bundle_root: Path = BUNDLE_ROOT,
    legacy_roots: list[Path] | None = None,
) -> dict:
    """Copy known old layouts into the v1 layout; never delete or overwrite user files."""
    layout = _layout(Path(data_root).resolve())
    marker = layout.root / "migration.json"
    if marker.is_file():
        try:
            previous = json.loads(marker.read_text(encoding="utf-8"))
            if int(previous.get("data_schema", 0)) >= DATA_SCHEMA_VERSION:
                return previous
        except (OSError, ValueError, TypeError):
            pass

    roots = legacy_roots or _legacy_roots(layout.root, Path(bundle_root).resolve())
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_root = layout.backups / f"legacy-{stamp}"
    events: list[dict] = []

    file_mappings = (
        ("touken_config.json", layout.config / "touken.json", "config/touken.json"),
        ("panel_config.json", layout.config / "panel.json", "config/panel.json"),
        ("expedition_schedule.json", layout.config / "expedition.json", "config/expedition.json"),
        ("launcher.log", layout.logs / "launcher.log", "logs/launcher.log"),
    )
    for root in roots:
        for old_name, target, backup_name in file_mappings:
            source = root / old_name
            if _copy_missing_file(source, target, backup_root / backup_name):
                events.append({"source": str(source), "target": str(target), "kind": "file"})

        old_status = root / "status"
        if old_status.is_dir():
            database = old_status / "maamaru_logs.db"
            if _copy_missing_file(database, layout.logs / database.name, backup_root / "logs" / database.name):
                events.append({"source": str(database), "target": str(layout.logs / database.name), "kind": "file"})
            state_files = _copy_missing_tree(
                old_status,
                layout.state,
                backup_root / "state",
                skip_names={"maamaru_logs.db"},
            )
            for name in state_files:
                events.append({"source": str(old_status / name), "target": str(layout.state / name), "kind": "state"})

        folders = []
        # A source/program bundle may contain official profiles and developer screenshots;
        # neither belongs in a user's data migration. Flat data roots from old releases do.
        if root.resolve() != Path(bundle_root).resolve():
            folders.extend((
                ("debug", layout.debug, "debug"),
                ("profiles", layout.profiles, "profile-override"),
            ))
        for folder_name, target, kind in folders:
            source = root / folder_name
            copied = _copy_missing_tree(source, target, backup_root / folder_name)
            events.extend(
                {"source": str(source / name), "target": str(target / name), "kind": kind}
                for name in copied
            )

    result = {
        "data_schema": DATA_SCHEMA_VERSION,
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "copied": len(events),
        "backup": str(backup_root) if events else None,
        "events": events,
    }
    _write_json(marker, result)
    return result


def _fill_missing_config_keys(
    example_path: Path,
    target_path: Path,
    backup_dir: Path,
) -> list[str]:
    """给老配置补新版本新增的字段（递归；用户已有的值一概不动）。

    只补缺失键，不覆盖、不删除。写入前把原文件备份到 backup_dir。
    返回补过的字段路径列表；没补就是空列表（也没动文件）。
    """
    if not example_path.is_file() or not target_path.is_file():
        return []
    try:
        template = json.loads(example_path.read_text(encoding="utf-8-sig"))
        current = json.loads(target_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    if not isinstance(template, dict) or not isinstance(current, dict):
        return []

    added: list[str] = []

    def fill(dst: dict, src: dict, prefix: str = "") -> None:
        for key, value in src.items():
            if key not in dst:
                dst[key] = value
                added.append(prefix + key)
            elif isinstance(value, dict) and isinstance(dst.get(key), dict):
                fill(dst[key], value, prefix + key + ".")

    fill(current, template)
    if not added:
        return []

    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"{target_path.stem}-pre-keyfill-{stamp}{target_path.suffix}"
    shutil.copy2(target_path, backup)
    target_path.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    return added


def ensure_runtime_data(
    data_root: Path = DATA_ROOT,
    bundle_root: Path = BUNDLE_ROOT,
    legacy_roots: list[Path] | None = None,
) -> dict:
    """Create the writable layout, migrate old data, then fill missing defaults."""
    layout = _layout(Path(data_root).resolve())
    for directory in (
        layout.root,
        layout.config,
        layout.state,
        layout.logs,
        layout.debug,
        layout.backups,
        layout.updates,
        layout.profiles,
    ):
        directory.mkdir(parents=True, exist_ok=True)

    migration = migrate_legacy_data(layout.root, Path(bundle_root).resolve(), legacy_roots)
    defaults = (
        (Path(bundle_root) / "touken_config.example.json", layout.config / "touken.json"),
        (Path(bundle_root) / "panel" / "panel_config.example.json", layout.config / "panel.json"),
        (Path(bundle_root) / "panel" / "expedition_schedule.json", layout.config / "expedition.json"),
    )
    for source, target in defaults:
        _copy_missing_file(source, target)

    # 老安装升级：配置已存在时 _copy_missing_file 不会动它，新版本新增的
    # 字段（比如异去坐标）永远进不来，出阵直接哑跑还报绿。这里只补缺失键。
    _fill_missing_config_keys(
        Path(bundle_root) / "touken_config.example.json",
        layout.config / "touken.json",
        layout.backups,
    )

    _ensure_data_version(layout.root / "data-version.json")
    return migration
