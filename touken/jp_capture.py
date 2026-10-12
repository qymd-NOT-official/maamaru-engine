# -*- coding: utf-8 -*-
"""日服画面标定采集：把当前游戏画面存成一帧，供裁剪模板用。

采集帧只落在数据目录 jp/debug/frames/（已随数据目录排除在仓库外），
模板裁好后才进 resource/base/image/jp/——同源铁律：模板必须来自这条
取帧通道，不用任何外部截图。

用法（老大操作游戏到目标画面后跑一条）：

    python -m touken.jp_capture --label home          # 本丸
    python -m touken.jp_capture --label sally-menu    # 出阵菜单
"""
from __future__ import annotations

import argparse
import time

from . import jp_browser, jp_browser_probe as frames, jp_vision


def capture(label: str) -> dict:
    """取一帧存盘；返回 {path, width, height, near_black}。"""
    if not label or any(ch in label for ch in r'\/:*?"<>|'):
        raise ValueError('标签名只能由字母数字和连字符组成')
    with jp_browser.JpBrowser() as browser:
        image = browser.screenshot()
        view = browser.base_view or {}
    if jp_vision.base_size() == jp_vision.FALLBACK_BASE:
        jp_vision.lock_base_size(image.width, image.height)
    directory = frames.JP_DATA_DIR / 'debug' / 'frames'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{label}-{time.strftime('%Y%m%d-%H%M%S')}.png"
    image.save(path)
    return {'path': str(path), 'width': image.width, 'height': image.height,
            'near_black': jp_vision.near_black(image),
            'view': f"{view.get('clientWidth')}x{view.get('clientHeight')}"}


def main() -> None:
    parser = argparse.ArgumentParser(description='日服画面标定采集')
    parser.add_argument('--label', required=True, help='画面标签，如 home / sally-menu / team-select')
    args = parser.parse_args()
    try:
        result = capture(args.label)
    except (ValueError, jp_browser.BrowserError) as error:
        print(f'采集失败：{error}', flush=True)
        raise SystemExit(1)
    print(f"已保存 {result['path']}（{result['width']}x{result['height']}，"
          f"窗口 {result['view']}）", flush=True)
    if result['near_black']:
        print('提醒：这帧接近黑屏，可能没采到有效画面', flush=True)


if __name__ == '__main__':
    main()
