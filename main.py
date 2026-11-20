"""Market Radyo giriş noktası: ayarlar, kütüphane, güncelleyici ve arayüz."""

import logging
import os
import shutil
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Derlenmiş exe'de veriler (config.json, data/, anonslar/) exe'nin yanında durur;
# PyInstaller tek dosya modunda __file__ geçici açılma klasörüne işaret eder.
if getattr(sys, "frozen", False):
    BASE = Path(sys.executable).resolve().parent
else:
    BASE = Path(__file__).resolve().parent


def prepare_frozen_dirs():
    """Derlenmiş exe'de çalışma klasörünü exe'nin klasörüne alır ve eksik öğeleri hazırlar.

    - anonslar/ yoksa oluşturulur.
    - data/playlists.json yoksa exe'ye gömülü kopyadan yazılır (ilk açılış).
    """
    os.chdir(BASE)
    (BASE / "anonslar").mkdir(exist_ok=True)
    data = BASE / "data"
    data.mkdir(exist_ok=True)
    bundled = Path(getattr(sys, "_MEIPASS", BASE)) / "data" / "playlists.json"
    target = data / "playlists.json"
    if not target.exists() and bundled.exists():
        shutil.copyfile(bundled, target)


def setup_logging():
    log_dir = BASE / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "radyo.log", maxBytes=1_000_000,
                                  backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    if getattr(sys, "frozen", False):
        prepare_frozen_dirs()
    setup_logging()
    from config import load_config
    from playlists import Library
    from updater import Updater
    from gui import run_app

    cfg = load_config(BASE)
    lib = Library(BASE, cfg)
    upd = Updater(BASE, cfg)  # denetim ve playlist senkronu arayüzle birlikte arka planda başlar
    run_app(BASE, cfg, lib, upd)


if __name__ == "__main__":
    sys.exit(main())
