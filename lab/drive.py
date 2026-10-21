# -*- coding: utf-8 -*-
"""
地图实验室 · 手动驾驶工具

一步步开游戏，每步之间可以抓图看画面，不搞自动连点。
所有步骤都带识别验证，认不准就停下报告，绝不盲点。

用法（项目根目录，项目虚拟环境）：
    .venv/Scripts/python.exe -m lab.drive probe              # 当前状态报告 + 抓图
    .venv/Scripts/python.exe -m lab.drive nav 出阵           # 导航到某界面
    .venv/Scripts/python.exe -m lab.drive chapter 1          # 合战场选第N章（含决定）
    .venv/Scripts/python.exe -m lab.drive map 1              # 选小图（会直接进部队选择）
    .venv/Scripts/python.exe -m lab.drive team 2             # 选第N部队
    .venv/Scripts/python.exe -m lab.drive automarch          # 只报告委托状态，不改
    .venv/Scripts/python.exe -m lab.drive automarch_dialog   # 点开自动行军弹窗并抓图（不关不选）
    .venv/Scripts/python.exe -m lab.drive depart             # 点即刻出阵
    .venv/Scripts/python.exe -m lab.drive shot <名字>        # 抓图到 lab/samples/
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from touken import DeviceAdapter, ToukenAgent  # noqa: E402
from touken.device_adapter import roi_4to4  # noqa: E402

SAMPLES = ROOT / "lab" / "samples"


def make_agent() -> ToukenAgent:
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
        raise RuntimeError("设备初始化失败")
    return ToukenAgent(str(ROOT / "touken_config.json"), device)


def shot(agent, name=None):
    SAMPLES.mkdir(parents=True, exist_ok=True)
    name = name or time.strftime("%Y%m%d_%H%M%S")
    if not name.endswith(".png"):
        name += ".png"
    out = SAMPLES / name
    ok = agent.device.save_screenshot(str(out), force=True)
    print(f"[LAB] 抓图 {'OK' if ok else 'FAIL'}: {out.name}")
    return ok


def delegated_status(agent) -> bool:
    mc = agent.config["team_select"]["auto_march"]["check_delegated"]
    r = mc["roi"]
    roi = roi_4to4(r[0], r[1], r[2], r[3])
    agent.device.screenshot(force=True)
    return bool(agent.device.exists(mc["template"], roi))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    agent = make_agent()

    if cmd == "probe":
        shot(agent, "probe")
        try:
            print(f"[LAB] 委托状态: {'委托中' if delegated_status(agent) else '未委托（或不在部队选择界面）'}")
        except Exception as e:
            print(f"[LAB] 委托状态读不出来: {e}")

    elif cmd == "nav":
        target = sys.argv[2]
        for msg in agent.navigate_to_stream(target):
            print(msg)
        print(f"[LAB] current_location = {agent.current_location}")
        shot(agent, f"nav_{target}")

    elif cmd == "chapter":
        n = sys.argv[2]
        ms = agent.config["map_select"]["合战场"]
        pt = ms["chapters"].get(n)
        if not pt:
            print(f"[LAB] 没有章节{n}的坐标")
            return
        agent._click_point(pt)
        time.sleep(0.5)
        agent.device.screenshot(force=True)
        decide = agent.device.template_match(agent.config["sortie"]["decide_button"]["template"])
        if decide:
            agent.device.click(decide)
            print(f"[LAB] 已选章节{n}并点决定")
            time.sleep(1.5)
        else:
            print(f"[LAB] 点了章节{n}但没看到决定按钮，抓图看看")
        shot(agent, f"chapter_{n}")

    elif cmd == "map":
        n = sys.argv[2]
        ms = agent.config["map_select"]["合战场"]
        pt = ms["maps"].get(n)
        if not pt:
            print(f"[LAB] 没有小图{n}的坐标")
            return
        # 先确认在小图页
        agent.device.screenshot(force=True)
        if not agent.device.template_match(agent.config["sortie"]["area_select_ui"]["template"]):
            print("[LAB] 不在小图页（没看到部队选择提示），先抓图")
            shot(agent, "map_not_on_page")
            return
        agent._click_point(pt)
        print(f"[LAB] 已点小图{n}，等部队选择界面...")
        ok = agent._wait_for_team_select(agent.config["sortie"], attempts=12, open_after=2)
        print(f"[LAB] 部队选择界面: {'到了' if ok else '没到'}")
        shot(agent, f"map_{n}_team_select")

    elif cmd == "team":
        n = int(sys.argv[2])
        ok = agent._pick_team(n)
        print(f"[LAB] 选部队{n}: {'OK' if ok else '失败'}")
        shot(agent, f"team_{n}")

    elif cmd == "automarch":
        print(f"[LAB] 委托状态: {'委托中' if delegated_status(agent) else '未委托'}")

    elif cmd == "automarch_dialog":
        mc = agent.config["team_select"]["auto_march"]
        if agent._click_template_config(mc["enable_button"]):
            time.sleep(1.2)
            shot(agent, "automarch_dialog")
            print("[LAB] 弹窗已开并抓图（没点里面任何东西）")
        else:
            print("[LAB] 没找到自动行军按钮")

    elif cmd == "depart":
        cfg = agent.config["sortie"]
        agent.device.screenshot(force=True)
        depart = agent.device.template_match(cfg["depart_button"]["template"])
        if depart:
            agent.device.click(depart)
            print("[LAB] 已点即刻出阵")
            time.sleep(2.0)
            shot(agent, "departed")
        else:
            print("[LAB] 没找到即刻出阵按钮")
            shot(agent, "depart_missing")

    elif cmd == "formation":
        result = agent.choose_formation(
            strategy="advantage", formation_name="逆行阵", enable_auto=False)
        print(f"[LAB] 阵形选择结果: {result}")
        time.sleep(2.0)
        shot(agent, "formation_done")

    elif cmd == "march":
        # 地图决策屏点"行军"继续（模板认，认不到就抓图报告）
        cfg = agent.config["sortie"]
        agent.device.screenshot(force=True)
        pt = agent.device.template_match(cfg["march_continue_button"]["template"])
        if pt:
            agent.device.click(pt)
            print("[LAB] 已点行军")
            time.sleep(2.5)
            shot(agent, f"march_{time.strftime('%H%M%S')}")
        else:
            print("[LAB] 没找到行军按钮，抓图看看")
            shot(agent, "march_missing")

    elif cmd == "skip":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        agent.skip_safe(n)
        shot(agent, f"skip_{time.strftime('%H%M%S')}")

    elif cmd == "shot":
        shot(agent, sys.argv[2] if len(sys.argv) > 2 else None)

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
