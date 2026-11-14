# -*- coding: utf-8 -*-
"""
刮刮乐剪影库 · 从游戏客户端同步

国服刮刮乐的剪影原图随活动资源直接下载在客户端里
（res/game_assets/images/silhouette/<图鉴号>/1.png=头部 2.png=腿部，
彩色立绘裁剪、透明底），把透明通道涂黑就是剪影库素材——
与内置库实测 IoU 0.998+ 同源。新活动换皮/新刀进池后跑一遍即可，
不用进游戏刮板子，也不用截图。

用法（项目根目录，项目虚拟环境，模拟器开着并已 root）：
    .venv/Scripts/python.exe -m lab.pull_silhouettes            # 同步进 profiles/
    .venv/Scripts/python.exe -m lab.pull_silhouettes --dry-run  # 只报告缺哪些，不写

命名规则：编号在名册（touken/data/swords.json）里 → 直接用其名；
编号没有而编号-1 有 → 视为那把的「極」，名字加「極」；
都不认识 → 写 profiles/staging/ 并提示人工命名（不入库）。
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROFILES_DIR = ROOT / "profiles"
STAGING_DIR = PROFILES_DIR / "staging"
GAME_SIL_DIR = ("/data/data/com.youzu.djlw/files/version/res/"
                "game_assets/images/silhouette")


def _adb(adb_path: str, address: str, *args: str) -> str:
    out = subprocess.run(
        [adb_path, "-s", address, *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError(f"adb 失败: {' '.join(args)}: {out.stderr.strip()}")
    return out.stdout


def _sword_names() -> dict[int, str]:
    """图鉴号 → 日文名（名册键 touken_<号>_xxx；号-1 命中则视为其極）"""
    data = json.loads((ROOT / "touken" / "data" / "swords.json")
                      .read_text(encoding="utf-8"))["chars"]
    base: dict[int, str] = {}
    for key, entry in data.items():
        m = re.match(r"touken_(\d+)_", key)
        if m:
            base[int(m.group(1))] = entry["name"]

    def resolve(sid: int) -> str | None:
        if sid in base:
            return base[sid]
        if sid - 1 in base:
            return base[sid - 1] + "極"
        return None

    return resolve  # type: ignore[return-value]


def _convert(src: Path, dst: Path) -> None:
    """彩色立绘 → 300×300 白底黑影（透明通道即轮廓，与内置库同源）"""
    arr = np.array(Image.open(src).convert("RGBA"))
    canvas = np.full(arr.shape[:2], 255, dtype=np.uint8)
    canvas[arr[..., 3] > 127] = 0
    Image.fromarray(canvas).save(dst)


def main() -> None:
    parser = argparse.ArgumentParser(description="从客户端同步刮刮乐剪影素材")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写文件")
    args = parser.parse_args()

    cfg = json.loads((ROOT / "touken_config.json").read_text(encoding="utf-8"))
    adb, addr = cfg["adb_path"], cfg["adb_address"]

    listing = _adb(adb, addr, "shell", f"su 0 ls {GAME_SIL_DIR}")
    ids = sorted(int(x) for x in listing.split() if x.strip().isdigit())
    print(f"客户端剪影目录 {len(ids)} 个编号: {ids}")

    resolve = _sword_names()
    added, skipped, unknown = [], [], []
    with tempfile.TemporaryDirectory() as tmp:
        remote = "/sdcard/sil_sync"
        # 复制时改名 <编号>_<n>.png，否则各目录的 1.png/2.png 互相覆盖
        copy_script = (
            f"rm -rf {remote} && mkdir -p {remote} && "
            f"for d in {GAME_SIL_DIR}/*/; do "
            f"id=$(basename $d); "
            f"for f in $d*.png; do "
            f"[ -f \"$f\" ] && cp \"$f\" {remote}/${{id}}_$(basename $f); "
            f"done; done; chmod -R 644 {remote}")
        _adb(adb, addr, "shell", f"su 0 sh -c '{copy_script}'")
        _adb(adb, addr, "pull", remote, tmp)
        pulled = Path(tmp) / "sil_sync"
        for f in sorted(pulled.glob("*.png")):
            m = re.match(r"(\d+)_(\d+)\.png", f.name)
            if not m:
                continue
            sid, view = int(m.group(1)), ("head" if m.group(2) == "1" else "foot")
            name = resolve(sid)
            if name is None:
                unknown.append(f"{sid:05d}_{view}")
                if not args.dry_run:
                    STAGING_DIR.mkdir(parents=True, exist_ok=True)
                    _convert(f, STAGING_DIR / f"{sid:05d}_未命名_{view}.png")
                continue
            out = PROFILES_DIR / f"{sid:05d}_{name}_{view}.png"
            if out.exists():
                skipped.append(out.name)
                continue
            added.append(out.name)
            if not args.dry_run:
                _convert(f, out)
        _adb(adb, addr, "shell", f"su 0 rm -rf {remote}")

    print(f"新增 {len(added)} 张:")
    for n in added:
        print("  +", n)
    print(f"已有跳过 {len(skipped)} 张")
    if unknown:
        print(f"⚠️ {len(unknown)} 张名册不认识，已存 {STAGING_DIR}（人工命名后挪进 profiles/）:")
        for n in unknown:
            print("  ?", n)
    if args.dry_run:
        print("（dry-run，未写入任何文件）")


if __name__ == "__main__":
    main()
