# -*- coding: utf-8 -*-
"""
生产版小地图阅读器（"王点前撤退"的决策原语）

从 lab/ 地图实验室搬过来的已验证逻辑：
  认节点（HSV 填色 + 黑描边校验）→ 推拓扑（法线横带扫弧线 +
  边上躺/绕路规则）→ 认旗标（当前位置）→ BFS 算当前节点到 BOSS 的图距离。

只认"决策屏"（战后部队状态屏）右上角的迷你地图——那里永远干净完整。
阈值全是真机采样调出来的，别瞎改；要改先去 lab/ 里验证。

cv2 是可选依赖：没装 opencv 时所有函数安全返回 None，
调用方按"认不出来"处理（继续正常行军），绝不能因此误判撤退。
"""

from collections import deque
from itertools import combinations

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于运行环境
    cv2 = None
    np = None
    CV2_AVAILABLE = False

# 右上迷你地图的固定区域（1280x720 实测量）
MINIMAP_ROI = (890, 120, 1265, 360)
MINIMAP_SCALE = 3

# 昼/夜图分界：迷你地图 V 通道中位数。昼图实测 140-185（8-4 市街 140），
# 夜战图（6 章三条大桥等）实测 ≈35。阈值 100 卡中间，两侧余量都巨大。
_NIGHT_MEDIAN_V = 100

# 节点填充色 HSV 阈值（真机采样，见 lab/detect_nodes.py 注释）：
#   BOSS 红 H>165/H<10 S>150 V>120；紫点 H124-150 S>100 V>60；
#   白点 S<60 V>180；绿点 H45-75 S>120 V>80（草地 H≈34 不撞）
# 紫点 H 必须收窄：真紫点填色是固定色 H≈132（昼夜六图实测全在 132.2±0.4），
# 而夜战图（7-4 城内）的建筑阴影是 H111-120 的蓝紫色，H 放到 110 会
# 在王点周围圈出一堆幻影节点，亮墙线再把它们连成假边（7-4 因此提前撤退）。
# 旗子约 8x13px，竖的。宽上限放到 24：王点门口地形复杂，旗面白色
# 会和背后的河岸/道路亮色粘连，8-4 实测过 20.7px 宽的"胖旗"（真旗），
# 靠下面的几何闸门兜底防假旗，不靠宽度卡死。
_FLAG_MIN_W, _FLAG_MAX_W = 5, 24
_FLAG_MIN_H, _FLAG_MAX_H = 8, 28
_FLAG_DX, _FLAG_DY_MIN, _FLAG_DY_MAX = 12, 6, 22  # 旗底下 6-22px、±12px 内=当前节点


def _node_masks(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    masks = {
        "boss": ((h > 165) | (h < 10)) & (s > 150) & (v > 120),
        "battle": (h > 124) & (h < 150) & (s > 100) & (v > 60),
        "white": (s < 60) & (v > 180),
        "green": (h > 45) & (h < 75) & (s > 120) & (v > 80),
    }
    # 开运算：核比连线粗（连线约6-8px），细线消失，节点填充留下
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    return {name: cv2.morphologyEx(m.astype(np.uint8) * 255, cv2.MORPH_OPEN, k)
            for name, m in masks.items()}


def _outline_ratio(img, x, y, w, h, band=6):
    """外圈暗像素占比：真节点有一圈黑描边，白墙/建筑碎片没有。"""
    v = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 2]
    H, W = v.shape
    x1, y1 = max(0, x - band), max(0, y - band)
    x2, y2 = min(W, x + w + band), min(H, y + h + band)
    outer = v[y1:y2, x1:x2]
    if outer.size == 0:
        return 0.0
    ring = np.ones(outer.shape, bool)
    ring[y - y1:y - y1 + h, x - x1:x - x1 + w] = False
    ring_px = outer[ring]
    return float((ring_px < 100).mean()) if ring_px.size else 0.0


def _is_night_map(work):
    """迷你地图是否夜战图。夜图连线是亮色、背景深色，多处逻辑要反过来。"""
    v = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)[:, :, 2]
    return float(np.median(v)) < _NIGHT_MEDIAN_V


def _detect_nodes(img, roi, scale, night=False):
    """返回 [{name,cx,cy,w,h,area}]，坐标是原图坐标。"""
    x0, y0, x1, y1 = roi
    sub = img[y0:y1, x0:x1]
    if scale != 1:
        sub = cv2.resize(sub, None, fx=scale, fy=scale,
                         interpolation=cv2.INTER_CUBIC)
    masks = _node_masks(sub)
    nodes = []
    for name, mask in masks.items():
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            if not 200 < area < 4000 * scale * scale:
                continue
            x, y, w, h = cv2.boundingRect(c)
            aspect = w / max(h, 1)
            solidity = area / max(w * h, 1)
            if not (1.1 < aspect < 3.5 and solidity > 0.55):
                continue
            # 真节点黑描边占比≈0.6，白墙碎片≈0.18，多线枢纽≈0.33；阈值0.28卡中间。
            # 夜战图例外：背景本来就黑，描边校验形同虚设；而王点常坐在
            # 桥面/亮色背景上（6-2 实测 outline≈0.15），反倒会被误杀——
            # 夜图只对红色 boss 点放宽到 0.10（红色在夜图里没有干扰源）。
            outline_min = 0.10 if (night and name == "boss") else 0.28
            if _outline_ratio(sub, x, y, w, h, band=6) < outline_min:
                continue
            nodes.append({
                "name": name,
                "cx": x0 + int((x + w / 2) / scale),
                "cy": y0 + int((y + h / 2) / scale),
                "w": int(w / scale), "h": int(h / scale),
                "area": int(area / (scale * scale)),
            })
    # 不同颜色掩码可能圈到同一个点，中心太近的去重（留面积大的）。
    # 半径分档：同色 15px（同色真节点最近 18px：6-2 紫点；8-4 是 24px），
    # 跨色 12px——跨色基本是同点误检或旗面白块骑在有色节点头上
    # （8-1 实测旗面被认成 white 节点，中心偏移 8.9px 压过旧的 8px 档，
    # 把真节点的所有连线触发"边上躺"规则全堵死）；
    # 而 6-2 的王点和隔壁紫点是真的只隔 14.8px，12 杀不到它。
    nodes.sort(key=lambda n: -n["area"])
    deduped = []
    for n in nodes:
        if all((n["cx"] - m["cx"]) ** 2 + (n["cy"] - m["cy"]) ** 2
               > (15 if n["name"] == m["name"] else 12) ** 2
               for m in deduped):
            deduped.append(n)
    return deduped


def _dark_mask(img):
    """纯黑线/描边：V 很低就行。连线是带暖棕底色的死黑，加 S 条件会误杀。"""
    return cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[:, :, 2] < 80


def _bright_line_mask(img):
    """夜战图的连线：亮薰衣草色（6-2 实测 S≈38-69 V≈113-172），
    背景深蓝（S≈150-200 V≈26-46）。S<120 & V>100 把两者干净分开。
    夜空星星也亮，但稀疏不连续，_edge_score 的 0.7 命中线它们够不着。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    return (hsv[:, :, 1] < 120) & (hsv[:, :, 2] > 100)


def _foggy_line_mask(img):
    """雨雾图（8-3 美浓）的连线掩码。雾气本身 V 就到 120-160，朴素亮掩码
    密度 0.38（正常夜图 0.09-0.17）会把雾当成线，节点两两满分连成毛线团。
    换局部对比：V 要比 21x21 邻域中位高 35——真连线线芯 V≈190-200、
    比雾背景（V≈110-140）高 50-90，扛得住；雾扛不住。核必须 21 起步：
    连线在 3 倍工作图里宽 7-10px，9x9 的中位窗会被线自己灌满，
    减出来约等于 0（实测密度 0.006，什么都剩不下）。
    注意这掩码对普通夜图太狠（6-2 的线 V113-172 会被杀光），
    只能在朴素掩码密度超标时启用，别当默认。"""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    s, v = hsv[:, :, 1], hsv[:, :, 2]
    med = cv2.medianBlur(v, 21)
    return (s < 120) & (v > 100) & (v > med.astype(np.int16) + 35)


def _edge_score(dark, p1, p2, samples=24, margin=8, band=24):
    """两点间黑线得分。连线可能是弧线：每个采样点沿法线 ±band px 扫横带。"""
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    length = float(np.hypot(dx, dy))
    if length < margin * 2 + 4:
        return 0.0
    nx, ny = -dy / length, dx / length
    hits = total = 0
    H, W = dark.shape
    for t in np.linspace(0, 1, samples):
        if min(t, 1 - t) * length < margin:
            continue
        xa, ya = x1 + dx * t, y1 + dy * t
        hit = False
        for off in range(-band, band + 1, 2):
            x = int(round(xa + nx * off))
            y = int(round(ya + ny * off))
            if 0 <= x < W and 0 <= y < H and dark[y, x]:
                hit = True
                break
        total += 1
        hits += int(hit)
    return hits / total if total else 0.0


def _wall_rider(line, p1, p2, samples=24, margin=8, band=30,
                core=8, core_max=0.45, jitter_min=6.0):
    """夜图专用：这条"边"是不是蹭墙假边。

    城内图（7-4）的亮墙线同色系、同样细，宽带采样会在弦附近总能找到
    某段墙——得分和真边一样高。差别在命中点的形态：真连线（哪怕弧线）
    贴着弦走、相邻采样命中位置连续；蹭墙边在不同墙段之间乱跳，且大多
    命中在离弦较远处。两个指标联合：近芯命中率低 + 相邻命中抖动大。
    肘形救援边别走这里——折线的弦先天打不中，必被误杀。"""
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    length = float(np.hypot(dx, dy))
    if length < margin * 2 + 4:
        return False
    nx, ny = -dy / length, dx / length
    H, W = line.shape
    hits = []
    for t in np.linspace(0, 1, samples):
        if min(t, 1 - t) * length < margin:
            continue
        xa, ya = x1 + dx * t, y1 + dy * t
        best = None
        for off in range(0, band + 1, 2):
            for o in ([off] if off == 0 else (off, -off)):
                x = int(round(xa + nx * o))
                y = int(round(ya + ny * o))
                if 0 <= x < W and 0 <= y < H and line[y, x]:
                    best = off
                    break
            if best is not None:
                break
        if best is not None:
            hits.append(best)
    if len(hits) < 5:
        return False
    core_ratio = sum(1 for o in hits if o <= core) / len(hits)
    jitter = float(np.mean([abs(a - b) for a, b in zip(hits, hits[1:])]))
    return core_ratio < core_max and jitter > jitter_min


def _mist_corridor(line, p1, p2, samples=24, margin=8, band=24, fill_max=0.6):
    """夜图专用：这条"边"是不是泡在亮雾/亮地面里的假边。

    雨天图（8-3 美浓）背景雾气本身 V>100，亮线掩码在雾区处处命中，
    节点两两得分 1.00 连成毛线团。差别在横带填充率：真连线是黑底上的
    一条细线（带内亮像素≈0.1-0.4），雾区走廊整条横带都是亮的（≈0.9）。
    实测：8-3 雾区假边 0.87-0.94，六章/7-4 真边最高 0.46，0.6 卡中间。"""
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    length = float(np.hypot(dx, dy))
    if length < margin * 2 + 4:
        return False
    nx, ny = -dy / length, dx / length
    H, W = line.shape
    fills = []
    for t in np.linspace(0, 1, samples):
        if min(t, 1 - t) * length < margin:
            continue
        xa, ya = x1 + dx * t, y1 + dy * t
        cnt = tot = 0
        for off in range(-band, band + 1, 2):
            x = int(round(xa + nx * off))
            y = int(round(ya + ny * off))
            if 0 <= x < W and 0 <= y < H:
                tot += 1
                cnt += int(line[y, x])
        if tot:
            fills.append(cnt / tot)
    return bool(fills) and float(np.mean(fills)) > fill_max


def _elbow_score(line, p1, p2):
    """直角折线得分：室内图（池田屋）的连线沿房间网格走 L 形，
    直线采样会砍掉真边（6-4 王点边直线 0.40，肘形两腿 0.85/0.61）。
    两个拐角各试一次，两腿得分取均值，返回较好的拐角。
    用均值而非 min：真折线常在拐角处斜切，一条腿满血一条腿一般；
    假肘形最多一条腿蹭到线，均值过不了阈值。"""
    if abs(p2[0] - p1[0]) < 4 or abs(p2[1] - p1[1]) < 4:
        return 0.0  # 纯水平/竖直对没有肘形可言
    best = 0.0
    for corner in ((p2[0], p1[1]), (p1[0], p2[1])):
        s = (_edge_score(line, p1, corner)
             + _edge_score(line, corner, p2)) / 2
        best = max(best, s)
    return best


def _build_graph(img, roi, scale, edge_threshold=0.7, night=None):
    """检测节点 + 推连线，返回 (nodes, edges)；丢弃没有连线的假节点。

    night=None 时按迷你地图亮度自动判断昼夜；夜战图连线是亮色，
    换成亮线掩码，其余推边/过滤逻辑昼夜通用。"""
    x0, y0, x1, y1 = roi
    sub = img[y0:y1, x0:x1]
    work = cv2.resize(sub, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_CUBIC)
    if night is None:
        night = _is_night_map(work)
    nodes = _detect_nodes(img, roi=roi, scale=scale, night=night)
    line = _bright_line_mask(work) if night else _dark_mask(work)
    foggy = False
    if night and float(line.mean()) > 0.25:
        # 雨雾图（8-3）：雾本身够亮，朴素掩码密度冲到 0.38（正常夜图
        # 0.09-0.17），节点会被雾连成毛线团。换局部对比的雾掩码。
        # 它对普通夜图太狠（6-2 的暗线会被杀光），只能密度超标时启用。
        line = _foggy_line_mask(work)
        foggy = True
    # 推边 margin：默认 8 工作 px（老行为）。雾图专用 8 原图 px——
    # 8-3 的虚线连线端点跟节点圆盘之间的空隙特别大（实测线头离节点
    # 中心 8.7 原图 px），小 margin 会把空隙里的采样算成未命中。
    # 注意别全局用大 margin：白天图旗子压线时白幡盖黑线，采样全挤到
    # 中段反而误杀（8-1 实测 0.5），小 margin 的近端采样还能打到线头。
    edge_margin = int(8 * scale) if foggy else 8
    pts = [((n["cx"] - x0) * scale, (n["cy"] - y0) * scale) for n in nodes]
    # 自适应边长上限（原图 px）：密集图（8-4 市街，节点间距≈26）里
    # 长边基本是沿建筑群蹭出来的假边（实测假边 94/140px，真边≤78）；
    # 稀疏图（1-1 只有 5 个节点，真边≈100px）上限必须跟着放宽。
    # 规则：最近邻距离中位 ×2.8，地板 75（保住 8-4 绕城堡的 67px 曲线真边），
    # 天花板 200。
    if len(pts) >= 3:
        pa = np.array(pts) / scale
        nn = sorted(min(float(np.hypot(*(pa[i] - pa[j])))
                        for j in range(len(pa)) if j != i)
                    for i in range(len(pa)))
        cap_orig = min(max(float(np.median(nn)) * 2.8, 75.0), 200.0)
    else:
        cap_orig = 200.0
    max_edge_len = cap_orig * scale

    def _collect_edges(isolated_only=None, use_elbow=False):
        """扫所有节点对推边。use_elbow=True 是第二遍的肘形救援（配 isolated_only
        只捞落单节点）；第一遍直线推边绝不开肘形——6-2 的桥身亮纹会让
        肘形乱满分，全图放开就是满地图假捷径。"""
        found = []
        for i, j in combinations(range(len(nodes)), 2):
            if isolated_only is not None and i not in isolated_only \
                    and j not in isolated_only:
                continue
            dist = float(np.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1]))
            if dist > max_edge_len:
                continue
            score = _edge_score(line, pts[i], pts[j], margin=edge_margin)
            if score < edge_threshold:
                # 室内夜战图（池田屋）连线走直角折线，直线打不通时试肘形。
                if not use_elbow:
                    continue
                score = _elbow_score(line, pts[i], pts[j])
                if score < edge_threshold:
                    continue
            # 边上躺规则：线段附近（12 原图 px 内）坐着第三个节点 → 不是直连
            x1, y1 = pts[i]
            x2, y2 = pts[j]
            dx, dy = x2 - x1, y2 - y1
            L2 = dx * dx + dy * dy
            blocked = False
            for k, (xk, yk) in enumerate(pts):
                if k in (i, j):
                    continue
                t = max(0.0, min(1.0,
                                 ((xk - x1) * dx + (yk - y1) * dy) / L2))
                if np.hypot(xk - (x1 + dx * t), yk - (y1 + dy * t)) < 12 * scale:
                    blocked = True
                    break
            if not blocked:
                found.append((i, j, score))
        return found

    edges = _collect_edges()
    if night:
        # 蹭墙/泡雾假边绞杀（只限直线推边产物）。被绞的配对要记下来，
        # 免得下面的肘形救援把它原样捞回来。
        killed = {(i, j) for i, j, _ in edges
                  if _wall_rider(line, pts[i], pts[j])
                  or _mist_corridor(line, pts[i], pts[j])}
        edges = [(i, j, s) for i, j, s in edges if (i, j) not in killed]
        # 第二遍：肘形救援只针对落单节点（典型：池田屋王点，唯一的连线
        # 是直角折线）。6-2 的桥身亮纹会让肘形乱满分，绝不能全图放开。
        connected = {i for i, _, _ in edges} | {j for _, j, _ in edges}
        isolated = set(range(len(nodes))) - connected
        if isolated:
            edges += [(i, j, s) for i, j, s
                      in _collect_edges(isolated_only=isolated, use_elbow=True)
                      if (i, j) not in killed]

    # 绕路规则：ik+kj ≈ ij（比值<1.18）说明 k 躺在 i-j 之间，i→j 是蹭线假边
    def _dist(a, b):
        return float(np.hypot(pts[a][0] - pts[b][0], pts[a][1] - pts[b][1]))
    edge_set = {(i, j) for i, j, _ in edges} | {(j, i) for i, j, _ in edges}
    edges = [(i, j, s) for i, j, s in edges
             if not any(k not in (i, j)
                        and (i, k) in edge_set and (k, j) in edge_set
                        and (_dist(i, k) + _dist(k, j)) < _dist(i, j) * 1.18
                        for k in range(len(nodes)))]

    if edges:
        keep = sorted({i for i, _, _ in edges} | {j for _, j, _ in edges})
        remap = {old: new for new, old in enumerate(keep)}
        nodes = [nodes[old] for old in keep]
        edges = [(remap[i], remap[j], s) for i, j, s in edges]
    return nodes, edges


def _find_flag_candidates(img, roi, scale):
    """白色小旗 = 竖直白色小块。返回所有候选中心（原图坐标），高的在前。

    市街图白天背景下，云/河面/建筑会贡献假的竖白块，而且可能比真旗还高，
    只返回"最高的一个"会被假旗抢走——所以全部返回，
    由调用方用"旗底下必须有节点"的几何闸门来筛。"""
    x0, y0, x1, y1 = roi
    sub = img[y0:y1, x0:x1]
    work = cv2.resize(sub, None, fx=scale, fy=scale,
                      interpolation=cv2.INTER_CUBIC)
    hsv = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)
    white = ((hsv[:, :, 1] < 60) & (hsv[:, :, 2] > 180)).astype(np.uint8) * 255
    contours, _ = cv2.findContours(white, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    cands = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        w0, h0 = w / scale, h / scale
        # 旗面里有红日章（红像素簇）；建筑/云/河面的假白块没有。
        # 7-1 中央建筑的白纸条和红瓦都能仿出零星红像素，但真旗的红
        # 是一整颗圆点——同帧过了几何闸门的候选里，红最多的就是真旗。
        # H 档 <15：8-1 的暗日章实测 H=12，H<10 会把它漏成 0。
        blob = hsv[y:y + h, x:x + w]
        red = int((((blob[:, :, 0] > 165) | (blob[:, :, 0] < 15))
                   & (blob[:, :, 1] > 120)).sum())
        # 宽度闸门对红日章完整的块放宽到 40：8-1 的真旗会和沙地粘成
        # 34x16 的大扁块（红 21），正常上限 24 直接把它挡在门外。
        if not (_FLAG_MIN_W < w0 < (40 if red >= 15 else _FLAG_MAX_W)
                and _FLAG_MIN_H < h0 < _FLAG_MAX_H):
            continue
        # 要竖的，横椭圆是白点（白点长宽比≈2）。上限放到 1.05：
        # 7-1 土黄背景上旗面会和旗杆影粘成近方块（实测 0.95 被 0.9 误杀）。
        # 带完整红日章（>=5 红像素）的放宽到 1.5：8-1 的真旗会和旁边
        # 云絮粘成 19x15 的扁块（AR1.32）被误杀，而假白块根本没有红。
        # 红特别多（>=15，暗日章整颗都在）再放到 2.5：就是上面说的
        # 34x16 沙地粘块（AR2.12），1.5 还是过不去。
        ar_limit = 2.5 if red >= 15 else (1.5 if red >= 5 else 1.05)
        if w0 / max(h0, 1) >= ar_limit:
            continue
        if ar_limit > 1.5:
            # 扁块放行档的防伪闸：真旗的红日章在旗面上半（8-1 实测红心
            # 在块内相对高度 0.38）；栅栏/建筑仿品的红在底边上
            # （1-1 栅栏红裙实测 0.9）。看红像素的质心高度。
            ys, _ = np.where((((blob[:, :, 0] > 165) | (blob[:, :, 0] < 15))
                              & (blob[:, :, 1] > 120)))
            if ys.size and float(ys.mean()) / max(h, 1) > 0.65:
                continue
        cx = x0 + (x + w / 2) / scale
        cy = y0 + (y + h / 2) / scale
        cands.append((cx, cy, h0, red))
    # 红日章先行通道：旗面和亮色背景（8-1 的沙地/云絮）粘成大扁块时，
    # 白块通道会整体阵亡（实测真旗粘成 34x16、AR2.12，连放宽的 1.5 都
    # 过不去），但红圆点不会和背景粘。找小红圆斑、脚下窗口里有白旗面
    # 就合成候选；旗心估计 = 圆点下移 4px（圆点在旗面上半）。红记 20：
    # 压过无红假白块（0），不淹没整面旗的红计数（真旗 8-19）。
    # 注意圆点是暗红色（实测 V≈54），不能加 V 条件；
    # H 档要放到 <15：8-1 圆点实测 H=12（S191 V36），H<10 会漏。
    red_mask = ((((hsv[:, :, 0] > 165) | (hsv[:, :, 0] < 15))
                 & (hsv[:, :, 1] > 120))
                .astype(np.uint8) * 255)
    discs, _ = cv2.findContours(red_mask, cv2.RETR_EXTERNAL,
                                cv2.CHAIN_APPROX_SIMPLE)
    Ww, Hw = work.shape[1], work.shape[0]
    for c in discs:
        x, y, w, h = cv2.boundingRect(c)
        w0, h0 = w / scale, h / scale
        if not (2 <= w0 <= 12 and 2 <= h0 <= 12
                and 0.4 < w0 / max(h0, 1) < 2.5
                and cv2.contourArea(c) / (scale * scale) >= 6):
            continue
        rx, ry = x + w / 2, y + h / 2
        # 白旗面竖向剖面：旗面是有底的白块——圆点下方 +10 原图 px 内白占比
        # 高、+14~+18 处已经收尾（真旗实测 0.00-0.01）；鸟居/建筑的红斑
        # 下面是白墙/沙地，白一路铺到底（8-1 鸟居实测 +14..+18 还有 0.67）。
        def _white_frac(dy_lo, dy_hi):
            wx0 = max(0, int(rx - 8 * scale))
            wx1 = min(Ww, int(rx + 8 * scale))
            wy0 = max(0, int(ry + dy_lo * scale))
            wy1 = min(Hw, int(ry + dy_hi * scale))
            win = white[wy0:wy1, wx0:wx1]
            return float((win > 0).mean()) if win.size else 0.0
        if _white_frac(-2, 10) < 0.25 or _white_frac(14, 18) >= 0.25:
            continue
        cands.append((x0 + rx / scale, y0 + (ry + 4 * scale) / scale,
                      0.0, 20))
    cands.sort(key=lambda c: (-c[3], -c[2]))
    return [(cx, cy) for cx, cy, _, _ in cands]


def _node_under_flag(nodes, flag):
    """旗底下 6-22px、左右 ±12px 内的节点下标；没有就 None。"""
    fx, fy = flag
    for i, n in enumerate(nodes):
        if (abs(n["cx"] - fx) <= _FLAG_DX
                and _FLAG_DY_MIN <= n["cy"] - fy <= _FLAG_DY_MAX):
            return i
    return None


def boss_distance_from_image(img):
    """算当前节点到 BOSS 的图距离。

    Args:
        img: BGR numpy 截图（device.screenshot() 的返回值）

    Returns:
        int: 距王点 N 步（1 = 下一脚就是王点）
        None: cv2 没装 / 图不对 / 节点、旗标或王点没认出来 / 王点不可达。
              调用方必须把 None 当"不知道"，继续正常行军，绝不能当撤退信号。
    """
    if not CV2_AVAILABLE or img is None:
        return None
    try:
        nodes, edges = _build_graph(img, roi=MINIMAP_ROI, scale=MINIMAP_SCALE,
                                    edge_threshold=0.7)
        boss = next((i for i, n in enumerate(nodes) if n["name"] == "boss"),
                    None)
        # _build_graph 丢孤立点会重排下标，拿旗标坐标回认当前节点最稳。
        # 旗标候选已按红日章含量排序，第一个过了几何闸门（底下有节点）
        # 的就是真旗——挡市街图的假旗，也挡 7-1 中央建筑的静止假旗。
        cur = None
        for flag in _find_flag_candidates(img, roi=MINIMAP_ROI,
                                          scale=MINIMAP_SCALE):
            cur = _node_under_flag(nodes, flag)
            if cur is not None:
                break
        if cur is None or boss is None:
            return None

        adj = {i: [] for i in range(len(nodes))}
        for i, j, _ in edges:
            adj[i].append(j)
            adj[j].append(i)
        dist = {cur: 0}
        q = deque([cur])
        while q:
            u = q.popleft()
            for v in adj[u]:
                if v not in dist:
                    dist[v] = dist[u] + 1
                    q.append(v)
        return dist.get(boss)
    except Exception:
        return None
