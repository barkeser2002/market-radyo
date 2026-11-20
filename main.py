"""Market Radyo giriş noktası: güncelleme kontrolü, kütüphane ve arayüz."""

import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE = Path(__file__).resolve().parent

DEFAULT_CONFIG = {
    "update_repo": "OWNER/market-radyo",
    "update_branch": "main",
    "volume": 70,
    "announce_every": 4,
    "announce_enabled": True,
    "cache_hours": 24,
    "recent_window": 200,
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads((BASE / "config.json").read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def setup_logging():
    log_dir = BASE / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(log_dir / "radyo.log", maxBytes=1_000_000,
                                  backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main():
    setup_logging()
    cfg = load_config()
    from playlists import Library
    from updater import Updater

    lib = Library(BASE, cfg)
    upd = Updater(BASE, cfg)
    try:
        if upd.check_app():
            upd.restart()  # yeni sürüm kuruldu: yeni süreci başlatıp çıkar
        upd.check_playlists(lib)
    except Exception:
        logging.exception("güncelleme kontrolü başarısız, mevcut sürümle devam")

    from gui import run_app
    run_app(BASE, cfg, lib)


if __name__ == "__main__":
    sys.exit(main())
