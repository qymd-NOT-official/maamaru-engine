"""游戏自动行军详细设定。ROI 由 1280×720 MuMu 运行帧校准。
勾选模板来自 debug/auto-march-settings-runtime.png（2026-10-01），
同帧选中鱼鳞阵命中 1.000，未选中横队/雁行在 0.85 下不命中。
"""
import time
from .device_adapter import roi_4to4

SWITCHES = {
    'medium': ('中伤时停止', (60, 95, 260, 155), (275, 100, 400, 153)),
    'fatigue': ('重疲劳时停止', (470, 95, 672, 155), (685, 100, 815, 153)),
    'equipment': ('刀装破坏时停止', (880, 95, 1085, 155), (1095, 100, 1225, 153)),
}
FORMATIONS = {
    '鱼鳞阵': (210, 350, 402, 412), '横队阵': (545, 350, 737, 412),
    '雁行阵': (880, 350, 1072, 412), '鹤翼阵': (210, 507, 402, 569),
    '方阵': (545, 507, 737, 569), '逆行阵': (880, 507, 1072, 569),
}

def march_policy(repair_threshold, injury_action, auto_equip, stop_on_fatigue, formation):
    if repair_threshold not in ('medium', 'heavy'):
        return None  # 游戏没有轻伤停止，保留脚本处理。
    if formation not in FORMATIONS:
        raise ValueError('未知兜底阵形')
    return {'medium': repair_threshold == 'medium',
            'equipment': not (auto_equip and injury_action in ('continue', 'repair_stop')),
            'fatigue': bool(stop_on_fatigue), 'formation': formation}


def read_switch(device, key, switches=None):
    label, label_roi, switch_roi = (SWITCHES if switches is None else switches)[key]
    if not device.ocr(label, roi_4to4(*label_roi), match_mode='exact'):
        return None
    states = {text.strip() for text, _ in device.ocr_all(roi_4to4(*switch_roi))}
    if '开' in states and '关' not in states:
        return True
    if '关' in states and '开' not in states:
        return False
    return None


def selected_formation(device, name):
    left, top, _, _ = FORMATIONS[name]
    return bool(device.template_match('team/auto_march_selected.png',
                                  roi_4to4(left - 5, top - 5, left + 45, top + 40),
                                  threshold=0.85))


def apply_details(device, policy, *, switches=None):
    """仅在明确的详细设定页修改，逐项回读；无法确认就停止。"""
    device.screenshot(force=True)
    title_roi = roi_4to4(510, 20, 760, 90)
    if not (device.ocr('详细设定', title_roi, match_mode='exact')
            or device.ocr('详细设置', title_roi, match_mode='exact')):
        return False
    active_switches = SWITCHES if switches is None else switches

    def read(key):
        return (read_switch(device, key) if switches is None
                else read_switch(device, key, switches))

    for key in active_switches:
        state = read(key)
        if state is None:
            return False
        if state != policy[key]:
            _, _, (left, top, right, bottom) = active_switches[key]
            from .device_adapter import Point
            device.click(Point((left + right) // 2, (top + bottom) // 2))
            time.sleep(0.5)
            device.screenshot(force=True)
            if read(key) != policy[key]:
                return False
    name = policy['formation']
    if not selected_formation(device, name):
        point = device.ocr(name, roi_4to4(*FORMATIONS[name]), match_mode='exact')
        if not point:
            return False
        device.click(point)
        time.sleep(0.5)
        device.screenshot(force=True)
        if not selected_formation(device, name):
            return False
    # 修改其余项目后，再一起回读，避免后续点击意外改变前项。
    return all(read(key) == policy[key] for key in active_switches)
