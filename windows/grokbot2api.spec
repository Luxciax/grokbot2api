# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for the Windows portable client (onedir).
# Run from repo root:
#   pyinstaller --distpath windows/dist --workpath windows/build windows/grokbot2api.spec
#
# PyInstaller injects SPEC / SPECPATH when evaluating this file.

from pathlib import Path

WINDOWS = Path(SPECPATH).resolve()
ROOT = WINDOWS.parent

gateway_py = [
    "grokbot2api.py",
    "sand_inference.py",
    "api_common.py",
    "messages_api.py",
    "responses_api.py",
    "image_gen.py",
    "model_catalogue.py",
]

datas = []
for name in gateway_py:
    src = ROOT / name
    if src.is_file():
        datas.append((str(src), "."))

media_keep = ROOT / "media" / ".gitkeep"
if media_keep.is_file():
    datas.append((str(media_keep), "media"))

readme = WINDOWS / "README.md"
if readme.is_file():
    datas.append((str(readme), "."))

hiddenimports = [
    "grokbot2api",
    "sand_inference",
    "api_common",
    "messages_api",
    "responses_api",
    "image_gen",
    "model_catalogue",
    "windows",
    "windows.app",
    "windows.credentials",
    "windows.gateway_service",
    "windows.tray_ui",
    "pystray",
    "pystray._win32",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
]

a = Analysis(
    [str(WINDOWS / "main.py")],
    pathex=[str(ROOT), str(WINDOWS)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="grokbot2api",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="grokbot2api",
)
