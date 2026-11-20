# -*- mode: python ; coding: utf-8 -*-
# Market Radyo tek dosya (onefile) derlemesi: python -m PyInstaller MarketRadyo.spec --noconfirm
# Gerekenler: bin/ffmpeg.exe (gomulur), version.txt, data/playlists.json (gomulur), customtkinter varliklari.
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = SPECPATH

a = Analysis(
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[(os.path.join(ROOT, "bin", "ffmpeg.exe"), ".")],
    datas=[
        (os.path.join(ROOT, "version.txt"), "."),
        (os.path.join(ROOT, "data", "playlists.json"), "data"),
    ] + collect_data_files("customtkinter"),  # tema JSON'ları, yazı tipleri ve ikonlar
    hiddenimports=collect_submodules("yt_dlp"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="MarketRadyo",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
