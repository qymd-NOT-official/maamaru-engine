"""Build and apply official, runtime-compatible program update packages."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
import zipfile
from pathlib import Path, PurePosixPath

EXE = "まあ丸启动器.exe"
MANIFEST = "update-manifest.json"
FORMAT = 1
APP_ROOTS = ("panel", "touken/data", "resource", "profiles", "launcher/assets")


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def safe_path(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or any(ord(char) < 32 or char in '\\:*?"<>|' for char in name) or path.is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))
            or any(part.rstrip(" .") != part for part in path.parts)
            or any(re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part)
                   for part in path.parts)):
        raise ValueError("更新包文件路径无效")
    return name


def app_file(name: str) -> bool:
    relative = name.removeprefix("_internal/")
    if relative.startswith("resource/base/model/"):
        return False
    return (name in (EXE, "manifest.json")
            or relative in ("manifest.json", "touken_config.example.json", "panel/panel_config.example.json",
                            "panel/expedition_schedule.json")
            or any(relative.startswith(root + "/") for root in APP_ROOTS))


def validate_manifest(data: dict) -> dict:
    if (data.get("format") != FORMAT or data.get("product") != "Maamaru"
            or data.get("data_schema") != 1 or not re.fullmatch(r"[0-9a-f]{64}", data.get("runtime", ""))
            or not re.fullmatch(r"\d+\.\d+\.\d+", data.get("version", ""))):
        raise ValueError("更新包版本或兼容信息无效")
    seen = set()
    for row in data["files"]:
        name = safe_path(row["path"])
        private = name.removeprefix("_internal/")
        if (private in ("touken_config.json", "panel_config.json", "panel/panel_config.json",
                        "expedition_schedule.json") or any(part in ("Netease", "status", "debug")
                                                             for part in PurePosixPath(private).parts)):
            raise ValueError("更新包包含用户数据")
        if name.casefold() in seen or name == MANIFEST:
            raise ValueError("更新清单包含重复文件")
        seen.add(name.casefold())
        if (not isinstance(row["size"], int) or row["size"] < 0
                or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"])
                or row["kind"] not in ("app", "runtime")):
            raise ValueError("更新清单文件信息无效")
        # Packages may only manage the frozen executable and its bundle.
        if name not in (EXE, "manifest.json") and not name.startswith("_internal/"):
            raise ValueError("更新包包含程序范围以外的文件")
        if row["kind"] != ("app" if app_file(name) else "runtime"):
            raise ValueError("更新清单文件分类无效")
    if EXE.casefold() not in seen or "manifest.json" not in seen:
        raise ValueError("更新清单缺少启动器")
    runtime_rows = [r for r in data["files"] if r["kind"] == "runtime"]
    expected = hashlib.sha256(json.dumps(runtime_rows, sort_keys=True).encode()).hexdigest()
    if expected != data["runtime"] or not runtime_rows:
        raise ValueError("运行环境指纹无效")
    return data


def build_package(program: Path, output: Path, version: str) -> Path:
    # PyInstaller's base library member order can vary between builds. Make the
    # archive deterministic so an unchanged runtime keeps the same fingerprint.
    library = program / "_internal/base_library.zip"
    if library.is_file():
        with zipfile.ZipFile(library) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        temporary = library.with_suffix(".zip.tmp")
        with zipfile.ZipFile(temporary, "w") as archive:
            for name, payload in sorted(entries.items()):
                archive.writestr(zipfile.ZipInfo(name), payload)
        temporary.replace(library)
    rows = []
    for path in sorted(program.rglob("*")):
        if not path.is_file() or path.name == MANIFEST:
            continue
        name = path.relative_to(program).as_posix()
        rows.append({"path": name, "size": path.stat().st_size, "sha256": digest(path),
                     "kind": "app" if app_file(name) else "runtime"})
    runtime_rows = [r for r in rows if r["kind"] == "runtime"]
    runtime = hashlib.sha256(json.dumps(runtime_rows, sort_keys=True).encode()).hexdigest()
    data = validate_manifest({"format": FORMAT, "product": "Maamaru", "data_schema": 1,
                              "version": version, "runtime": runtime, "files": rows})
    (program / MANIFEST).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    output.mkdir(parents=True, exist_ok=True)
    target = output / f"maamaru-update-v{version}-{runtime}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(program / MANIFEST, MANIFEST)
        for row in rows:
            if row["kind"] == "app":
                archive.write(program / row["path"], row["path"])
    return target


def installed_manifest(program: Path) -> dict | None:
    try:
        return validate_manifest(json.loads((program / MANIFEST).read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def verify_files(program: Path, data: dict, kind: str | None = None) -> None:
    for row in data["files"]:
        if kind and row["kind"] != kind:
            continue
        path = program / row["path"]
        if (not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(program.resolve())
                or path.stat().st_size != row["size"] or digest(path) != row["sha256"]):
            raise ValueError(f"程序文件校验失败：{row['path']}")


def stage(package: Path, program: Path, candidate: Path, version: str) -> dict:
    previous = installed_manifest(program)
    if previous is None:
        raise ValueError("旧版不支持轻量更新，请使用完整安装包")
    with zipfile.ZipFile(package) as archive:
        infos = archive.infolist()
        names = [safe_path(info.filename) for info in infos]
        if len({n.casefold() for n in names}) != len(names):
            raise ValueError("更新包包含重复文件")
        info = archive.getinfo(MANIFEST)
        if info.file_size > 8 * 1024 * 1024:
            raise ValueError("更新清单过大")
        data = validate_manifest(json.loads(archive.read(MANIFEST)))
        if data["version"] != version or data["runtime"] != previous["runtime"]:
            raise ValueError("更新包与当前运行环境不兼容，请使用完整安装包")
        expected = {r["path"]: r for r in data["files"] if r["kind"] == "app"}
        if set(names) != set(expected) | {MANIFEST}:
            raise ValueError("更新包文件与清单不一致")
        verify_files(program, data, "runtime")
        # A fresh candidate contains only managed files plus installer bookkeeping.
        candidate.mkdir()
        for row in data["files"]:
            if row["kind"] == "runtime":
                dest = candidate / row["path"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(program / row["path"], dest)
        for path in program.glob("unins*.*"):
            if path.is_file() and not path.is_symlink():
                shutil.copy2(path, candidate / path.name)
        for name, row in expected.items():
            info = archive.getinfo(name)
            if info.file_size != row["size"] or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("更新包文件大小或类型无效")
            dest = candidate / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, dest.open("wb") as output:
                shutil.copyfileobj(source, output)
        (candidate / MANIFEST).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    verify_files(candidate, data)
    return data


def probe(program: Path) -> None:
    # Import the actual frozen app without opening UI or touching user data.
    result = subprocess.run([str(program / EXE), "--update-probe"], timeout=45,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
    if result.returncode:
        raise ValueError("新版启动检查失败")


def apply_package(package: Path, program: Path, backup: Path, version: str) -> None:
    program = program.resolve()
    if os.name == "nt":
        environment = os.environ.copy()
        environment["MAAMARU_UPDATE_TARGET"] = str(program / EXE)
        result = subprocess.run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference='Stop'; @((Get-CimInstance Win32_Process) | "
            "Where-Object { $_.ExecutablePath -and "
            "$_.ExecutablePath -ieq $env:MAAMARU_UPDATE_TARGET }).Count",
        ], env=environment, capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        if result.returncode or result.stdout.strip() != "0":
            raise ValueError("旧版面板或任务还在运行，请关闭后再更新")
    candidate = program.with_name(program.name + ".update-" + uuid.uuid4().hex)
    stage(package, program, candidate, version)
    probe(candidate)
    # Rename on the same volume; keep the old directory until new startup succeeds.
    old = program.with_name(program.name + ".previous-" + uuid.uuid4().hex)
    program.rename(old)
    try:
        candidate.rename(program)
        probe(program)
    except Exception:
        if program.exists():
            program.rename(candidate.with_name(candidate.name + ".failed"))
        old.rename(program)
        raise
    # Preserve rollback snapshot in the user update area, even across drives.
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(old, backup)
    except OSError:
        # The intact adjacent snapshot still exists; update itself is successful.
        pass
