# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_submodules

maa_datas, maa_binaries, maa_hidden = collect_all('maa')
webview_datas, webview_binaries, webview_hidden = collect_all('webview')

datas = [
    ('panel/static', 'panel/static'),
    ('panel/panel_config.example.json', 'panel'),
    ('panel/expedition_schedule.json', 'panel'),
    ('touken_config.example.json', '.'),
    ('manifest.json', '.'),
    ('launcher/assets/maamaru-launcher.ico', 'launcher/assets'),
    ('launcher/assets/maamaru-launcher.png', 'launcher/assets'),
    ('launcher/assets/maamaru-launcher-header.png', 'launcher/assets'),
    ('launcher/assets/honmaru_rain_garden.png', 'launcher/assets'),
    ('launcher/assets/honmaru_garden_after_rain.png', 'launcher/assets'),
    ('launcher/assets/fox_idle.png', 'launcher/assets'),
    ('launcher/assets/fox_run_1_alpha.png', 'launcher/assets'),
    ('launcher/assets/fox_run_2_alpha.png', 'launcher/assets'),
    ('touken/data', 'touken/data'),
    ('resource', 'resource'),
    ('profiles', 'profiles'),
] + maa_datas + webview_datas

binaries = maa_binaries + webview_binaries

hiddenimports = [
    'uvicorn.logging',
    'uvicorn.loops.auto',
    'uvicorn.protocols.http.auto',
    'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan.on',
    'panel.worker',
] + collect_submodules('uvicorn') + maa_hidden + webview_hidden

a = Analysis(
    ['maamaru_launcher.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

# The third-party PyInstaller hook named ``panel`` also sees our local panel
# package and may collect ignored runtime files from a developer checkout.
# Release artifacts must contain templates only, never real user configuration.
_private_bundle_paths = {
    'panel/panel_config.json',
    'panel_config.json',
    'touken_config.json',
    'expedition_schedule.json',
}
a.datas = [
    item for item in a.datas
    if item[0].replace('\\', '/') not in _private_bundle_paths
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='まあ丸启动器',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='launcher/assets/maamaru-launcher.ico',
)

coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='まあ丸启动器')
