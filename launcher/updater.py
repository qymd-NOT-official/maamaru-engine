"""Download release installers into the user data area without installing them."""

from __future__ import annotations

import hashlib
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from touken.runtime_paths import UPDATES_DIR


REPOSITORY = "qymd-NOT-official/maamaru-engine"
_SHA256 = re.compile(r"sha256:([0-9a-fA-F]{64})\Z")


class UpdateError(RuntimeError):
    """A release is unsuitable or could not be downloaded safely."""


def select_installer(release: dict) -> dict:
    """Return the signed-by-GitHub metadata for this release's Windows installer."""
    version = str(release.get("tag_name") or "").removeprefix("v")
    return _select_asset(release, f"maamaru-setup-v{version}.exe")


def _select_asset(release: dict, expected_name: str) -> dict:
    tag = str(release.get("tag_name") or "")
    version = tag.removeprefix("v")
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag) or release.get("draft") or release.get("prerelease"):
        raise UpdateError("这个版本不是可用的正式更新")
    for asset in release.get("assets") or []:
        if asset.get("name") != expected_name:
            continue
        digest = str(asset.get("digest") or "")
        url = str(asset.get("browser_download_url") or "")
        size = asset.get("size")
        parsed = urllib.parse.urlparse(url)
        expected_path = f"/{REPOSITORY}/releases/download/{tag}/{expected_name}"
        if not _SHA256.fullmatch(digest):
            raise UpdateError("GitHub 没有提供可核对的安装包指纹")
        if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.path != expected_path
                or parsed.query or parsed.fragment):
            raise UpdateError("安装包下载地址不是まあ丸官方仓库")
        if not isinstance(size, int) or size <= 0:
            raise UpdateError("安装包大小信息无效")
        return {"tag": tag, "version": version, "name": expected_name, "digest": digest, "url": url, "size": size}
    raise UpdateError("这个版本没有找到 Windows 安装包")


def select_update(release: dict, program_dir: Path) -> dict:
    from .light_update import installed_manifest, verify_files
    current = installed_manifest(program_dir)
    if current:
        try:
            verify_files(program_dir, current, "runtime")
            version = str(release.get("tag_name", "")).removeprefix("v")
            name = f"maamaru-update-v{version}-{current['runtime']}.zip"
            # Reuse the official source/size/digest checks from installer selection.
            for asset in release.get("assets") or []:
                if asset.get("name") == name:
                    selected = _select_asset(release, name)
                    selected.update(name=name, kind="light")
                    return selected
        except (OSError, ValueError, KeyError, UpdateError):
            pass
    return select_installer(release)


def download_installer(asset: dict, updates_dir: Path = UPDATES_DIR, progress=None) -> dict:
    """Download and verify an installer, atomically exposing only a complete file.

    ``progress`` is an optional callback invoked as ``progress(downloaded, total)``
    after each chunk so callers can render a live progress bar."""
    digest_match = _SHA256.fullmatch(str(asset.get("digest") or ""))
    if not digest_match:
        raise UpdateError("安装包指纹无效")
    expected_hash = digest_match.group(1).lower()
    expected_size = int(asset["size"])
    version = str(asset["version"])
    name = Path(str(asset["name"])).name
    if (name != asset["name"] or not re.fullmatch(r"\d+\.\d+\.\d+", version)
            or (asset.get("kind") == "light" and not re.fullmatch(
                rf"maamaru-update-v{re.escape(version)}-[0-9a-f]{{64}}\.zip", name))
            or (asset.get("kind") != "light" and name != f"maamaru-setup-v{version}.exe")):
        raise UpdateError("安装包文件名无效")

    target_dir = Path(updates_dir) / version
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / name
    partial = target.with_suffix(target.suffix + ".part")

    if target.is_file() and target.stat().st_size == expected_size and _file_sha256(target) == expected_hash:
        return {"path": str(target), "size": expected_size, "sha256": expected_hash, "reused": True}

    partial.unlink(missing_ok=True)
    try:
        request = urllib.request.Request(asset["url"], headers={"User-Agent": "MaamaruLauncher/0.1"})
        hasher = hashlib.sha256()
        downloaded = 0
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                downloaded += len(chunk)
                if downloaded > expected_size:
                    raise UpdateError("安装包大小与 GitHub 记录不一致")
                hasher.update(chunk)
                output.write(chunk)
                if progress is not None:
                    progress(downloaded, expected_size)
        if downloaded != expected_size or hasher.hexdigest() != expected_hash:
            raise UpdateError("安装包校验失败，未保留这次下载")
        partial.replace(target)
        _write_metadata(target_dir / "download.json", asset, expected_hash)
        return {"path": str(target), "size": downloaded, "sha256": expected_hash, "reused": False}
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_metadata(path: Path, asset: dict, digest: str) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({
        "version": asset["version"],
        "asset": asset["name"],
        "size": asset["size"],
        "sha256": digest,
        "source": asset["url"],
        "verified": True,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
