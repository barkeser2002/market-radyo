"""ffmpeg ikili dosyasını bulur ve konsol penceresi açmadan çalıştırır.

Arama sırası:
1. PyInstaller ile paketlenmişse: sys._MEIPASS/ffmpeg.exe
2. Proje klasörü: bin/ffmpeg.exe
3. Sistem PATH'i
"""

import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def ffmpeg_path():
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "ffmpeg.exe"
        if p.exists():
            return str(p)
    p = BASE / "bin" / "ffmpeg.exe"
    if p.exists():
        return str(p)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise FileNotFoundError("ffmpeg bulunamadı: bin\\ffmpeg.exe yok ve PATH'te de yok")


def hidden_window_kwargs():
    """Windows'ta alt süreç için konsol penceresi açılmasını engeller."""
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
