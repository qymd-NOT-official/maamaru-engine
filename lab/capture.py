# -*- coding: utf-8 -*-
"""
地图实验室 · 抓图工具

用法（项目根目录，用项目虚拟环境）：
    .venv/Scripts/python.exe -m lab.capture <名字>     # 抓当前屏幕存到 lab/samples/<名字>.png
    .venv/Scripts/python.exe -m lab.capture            # 不带名字就按时间戳命名

只截图，不点任何东西。采集地图素材的第一步。
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from touken import DeviceAdapter  # noqa: E402

SAMPLES_DIR = ROOT / "lab" / "samples"


def make_maa() -> DeviceAdapter:
    cfg = json.loads((ROOT / "touken_config.json").read_text(encoding="utf-8"))
    device = DeviceAdapter(
        adb_path=cfg["adb_path"],
        adb_address=cfg["adb_address"],
        resource_dir=str(ROOT / "resource" / "base"),
        project_root=str(ROOT),
        manager_path=cfg.get("emulator_manager"),
        emulator_instance=int(cfg.get("emulator_instance", 0)),
    )
    if not device.init():
        raise RuntimeError("设备初始化失败：检查模拟器开没开、ADB 通不通")
    return device


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y%m%d_%H%M%S")
    if not name.endswith(".png"):
        name += ".png"
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    out = SAMPLES_DIR / name

    device = make_maa()
    if device.save_screenshot(str(out), force=True):
        print(f"OK {out}")
    else:
        print("FAIL 截图没存下来")
        sys.exit(1)


if __name__ == "__main__":
    main()
