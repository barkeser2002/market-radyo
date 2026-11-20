"""Açılışta otomatik güncelleme kontrolü.

- GitHub raw üzerinden version.txt okunur. Yeni sürüm varsa APP_FILES indirilip
  kurulur ve program yeniden başlatılır. config.json ve data/ klasörüne dokunulmaz.
- data/playlists.json uzak kaynaktan okunup yerel listeye eklenir (yerel kayıtlar korunur).
- Depo ayarlı değilse (config.json'da OWNER kalmışsa) kontrol atlanır.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import requests

log = logging.getLogger("radyo.updater")

APP_FILES = [
    "main.py", "player.py", "ads.py", "playlists.py", "updater.py", "gui.py",
    "version.txt", "requirements.txt", "calistir.bat",
]
TIMEOUT = 8


def parse_version(text):
    nums = [int(x) for x in re.findall(r"\d+", text)][:3]
    return tuple(nums + [0] * (3 - len(nums)))


class Updater:
    def __init__(self, base, cfg):
        self.base = Path(base)
        repo = str(cfg.get("update_repo", "")).strip()
        branch = str(cfg.get("update_branch", "main")).strip() or "main"
        self.enabled = "/" in repo and "OWNER" not in repo.upper()
        self.raw = "https://raw.githubusercontent.com/%s/%s/" % (repo, branch)

    def _get(self, rel):
        r = requests.get(self.raw + rel, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content

    def local_version(self):
        p = self.base / "version.txt"
        try:
            return p.read_text(encoding="utf-8").strip()
        except OSError:
            return "0.0.0"

    def check_app(self):
        """Yeni sürüm kurulduysa True döner (yeniden başlatma gerekir)."""
        if not self.enabled:
            log.info("güncelleme deposu ayarlı değil, atlanıyor")
            return False
        try:
            remote = self._get("version.txt").decode("utf-8", "replace").strip()
        except Exception as e:
            log.info("sürüm kontrolü yapılamadı: %s", e)
            return False
        if parse_version(remote) <= parse_version(self.local_version()):
            return False

        staged = {}
        for name in APP_FILES:
            try:
                staged[name] = self._get(name)
            except Exception as e:
                log.warning("güncelleme dosyası indirilemedi (%s): %s", name, e)
                return False
        for name, data in staged.items():
            if name.endswith(".py"):
                try:
                    compile(data.decode("utf-8"), name, "exec")
                except (SyntaxError, UnicodeDecodeError) as e:
                    log.error("indirilen %s geçersiz, güncelleme iptal: %s", name, e)
                    return False

        backup = self.base / "data" / "update_backup"
        backup.mkdir(parents=True, exist_ok=True)
        for name, data in staged.items():
            dest = self.base / name
            if dest.exists():
                shutil.copy2(dest, backup / name)
            tmp = dest.with_name(dest.name + ".new")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        log.info("yeni sürüm kuruldu: %s", remote)
        return True

    def check_playlists(self, library):
        if not self.enabled:
            return 0
        try:
            data = json.loads(self._get("data/playlists.json").decode("utf-8"))
        except Exception as e:
            log.info("uzak playlist listesi alınamadı: %s", e)
            return 0
        return library.merge_remote(data)

    def restart(self):
        subprocess.Popen([sys.executable, str(self.base / "main.py")], cwd=str(self.base))
        sys.exit(0)
