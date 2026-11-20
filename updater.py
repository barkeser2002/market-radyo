"""Güncelleme: GitHub Releases üzerinden yeni sürümü bulur ve kurar.

- Sürüm etiketi vX.Y.Z biçimindedir (GitHub Releases, token gerektirmez).
- Açılışta ve her 6 saatte bir arka planda denetlenir.
- Derlenmiş exe (sys.frozen) ise MarketRadyo.exe ve MarketRadyo.exe.sha256 indirilir,
  sha256 doğrulanır ve exe yanına MarketRadyo.exe.new olarak yazılır. Kurulum
  apply_pending() ile yapılır: data\\update.bat eski süreç çıkana kadar bekler, exe'yi
  değiştirir (başarısızsa eskisini geri koyar), yeniden başlatır ve kendini siler.
  Kurulum, radyo bir sonraki parçaya geçerken istenir; çalma sırasında anında kapanmaz.
- Kaynak (python) kurulumda yalnızca yeni sürüm olduğu bildirilir.
- data/playlists.json uzak kopyası (raw) okunup yerel listeye eklenir; yerel kayıtlar
  (eklenenler, açık/kapalı tercihleri, türler) korunur.
"""

import hashlib
import logging
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import requests

log = logging.getLogger("radyo.updater")

API_URL = "https://api.github.com/repos/%s/releases/latest"
RAW_URL = "https://raw.githubusercontent.com/%s/%s/%s"
PLAYLISTS_PATH = "data/playlists.json"
EXE_NAME = "MarketRadyo.exe"
SHA_NAME = "MarketRadyo.exe.sha256"
TIMEOUT = 15
CHECK_INTERVAL = 6 * 3600
CHUNK = 1024 * 1024
CREATE_NO_WINDOW = 0x08000000
HEADERS = {"User-Agent": "MarketRadyo-Updater", "Accept": "application/vnd.github+json"}

_VERSION_RE = re.compile(r"\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*", re.IGNORECASE)
_SHA_RE = re.compile(r"\b([0-9a-fA-F]{64})\b")


def is_frozen():
    return bool(getattr(sys, "frozen", False))


# ----- saf yardımcılar (testlenebilir) ---------------------------------
def parse_version(text):
    """'v1.2.3' / '1.2.3' -> (1, 2, 3). Okunamazsa None."""
    m = _VERSION_RE.fullmatch(str(text or ""))
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())


def is_newer(remote, local):
    r, l = parse_version(remote), parse_version(local)
    return r is not None and l is not None and r > l


def parse_sha256(text):
    """'<64 hex>  dosyaadi' ya da yalnızca '<64 hex>' biçimindeki metinden hash'i alır."""
    m = _SHA_RE.search(str(text or ""))
    return m.group(1).lower() if m else None


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_sha256(path, sha_text):
    """Dosyanın sha256 özeti sha_text içindeki hash ile aynıysa True."""
    expected = parse_sha256(sha_text)
    if expected is None:
        return False
    try:
        return sha256_file(path) == expected
    except OSError:
        return False


def build_update_script(pid, exe, new, old):
    """Kurulum betiği (update.bat) metni. Testte doğrudan üretilip incelenebilir."""
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        'set "PID=%d"' % int(pid),
        'set "EXE=%s"' % exe,
        'set "NEW=%s"' % new,
        'set "OLD=%s"' % old,
        "set /a TRIES=0",
        ":wait",
        'tasklist /FI "PID eq %PID%" /NH 2>nul | find "%PID%" >nul',
        "if not errorlevel 1 (",
        "  ping -n 2 127.0.0.1 >nul",
        "  goto wait",
        ")",
        ":replace",
        "set /a TRIES+=1",
        'move /Y "%EXE%" "%OLD%" >nul 2>&1',
        "if errorlevel 1 (",
        "  if %TRIES% GEQ 30 goto fail",
        "  ping -n 2 127.0.0.1 >nul",
        "  goto replace",
        ")",
        'move /Y "%NEW%" "%EXE%" >nul 2>&1',
        "if errorlevel 1 (",
        '  move /Y "%OLD%" "%EXE%" >nul 2>&1',
        "  goto fail",
        ")",
        'del /F /Q "%OLD%" >nul 2>&1',
        'start "" "%EXE%"',
        "goto cleanup",
        ":fail",
        'del /F /Q "%NEW%" >nul 2>&1',
        'if exist "%EXE%" start "" "%EXE%"',
        ":cleanup",
        'del /F /Q "%~f0" >nul 2>&1',
        "exit /b 0",
    ]
    return "\r\n".join(lines) + "\r\n"


# ----- güncelleyici ----------------------------------------------------
@dataclass
class CheckResult:
    status: str          # disabled | error | uptodate | available | staged
    version: str = ""    # uzak sürüm, 'v' olmadan (ör. 1.0.1)
    message: str = ""


class Updater:
    def __init__(self, base, cfg, exe=None):
        self.base = Path(base)
        repo = str(cfg.get("update_repo", "")).strip()
        self.repo = repo
        self.branch = str(cfg.get("update_branch", "main")).strip() or "main"
        self.enabled = "/" in repo and "OWNER" not in repo.upper()
        self.exe = Path(exe) if exe else Path(sys.executable)
        self._check_lock = threading.Lock()   # aynı anda iki denetim/indirme olmasın
        self._state_lock = threading.Lock()
        self._staged_version = None
        self._staged_path = None
        self._stop = threading.Event()
        self._library = None
        self._on_result = None

    # ----- sürüm -----------------------------------------------------
    def local_version(self):
        """Derlenmişse exe içine gömülü version.txt, değilse proje klasöründeki version.txt."""
        candidates = []
        meipass = getattr(sys, "_MEIPASS", None)
        if is_frozen() and meipass:
            candidates.append(Path(meipass) / "version.txt")
        candidates.append(self.base / "version.txt")
        for p in candidates:
            try:
                v = p.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if v:
                return v
        return "0.0.0"

    def pending_version(self):
        """İndirilmiş ve doğrulanmış, henüz kurulmamış sürüm (yoksa None)."""
        with self._state_lock:
            return self._staged_version

    # ----- denetim ---------------------------------------------------
    def _latest_release(self):
        r = requests.get(API_URL % self.repo, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()

    def _get_text(self, url):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        try:
            r.raise_for_status()
            return r.text
        finally:
            r.close()

    def _download(self, url, dest):
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
        try:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(CHUNK):
                    if chunk:
                        f.write(chunk)
        finally:
            r.close()

    def _download_verified(self, exe_url, sha_url):
        """exe'yi indirir, sha256'sını doğrular ve exe.new olarak yerleştirir."""
        dest = self.exe.with_name(self.exe.name + ".new")
        part = dest.with_name(dest.name + ".part")
        sha_text = self._get_text(sha_url)
        try:
            self._download(exe_url, part)
            if not verify_sha256(part, sha_text):
                raise ValueError("sha256 doğrulanamadı; dosya bozuk ya da değiştirilmiş")
            os.replace(part, dest)
        except Exception:
            part.unlink(missing_ok=True)
            raise
        return dest

    def check(self):
        """Yeni sürüm var mı denetler. Derlenmiş programda yeni sürümü indirip doğrular (kurmaz)."""
        if not self.enabled:
            return CheckResult("disabled", message="Güncelleme deposu ayarlı değil (config.json: update_repo).")
        with self._check_lock:
            try:
                rel = self._latest_release()
            except Exception as e:
                log.info("sürüm kontrolü yapılamadı: %s", e)
                return CheckResult("error", message="Sürüm kontrolü yapılamadı: %s" % e)
            tag = str(rel.get("tag_name") or "")
            parsed = parse_version(tag)
            if parsed is None:
                return CheckResult("error", message="Geçersiz sürüm etiketi: %r" % tag)
            ver = ".".join(str(x) for x in parsed)
            local = self.local_version()
            if not is_newer(tag, local):
                return CheckResult("uptodate", ver, "Güncel: v%s" % local)
            if not is_frozen():
                return CheckResult("available", ver,
                                   "Yeni sürüm v%s var. Kaynak kurulumda güncellemeyi elle alın." % ver)
            if self.pending_version() == ver:
                return CheckResult("staged", ver, "Sürüm v%s zaten indirildi." % ver)

            assets = {a.get("name"): a.get("browser_download_url") for a in rel.get("assets") or []}
            exe_url, sha_url = assets.get(EXE_NAME), assets.get(SHA_NAME)
            if not exe_url or not sha_url:
                return CheckResult("error", ver, "Sürüm %s içinde %s ya da %s yok."
                                   % (tag, EXE_NAME, SHA_NAME))
            try:
                staged = self._download_verified(exe_url, sha_url)
            except Exception as e:
                log.warning("güncelleme indirilemedi (%s): %s", tag, e)
                return CheckResult("error", ver, "Güncelleme indirilemedi: %s" % e)
            with self._state_lock:
                self._staged_version = ver
                self._staged_path = staged
            log.info("yeni sürüm indirildi ve doğrulandı: %s", ver)
            return CheckResult("staged", ver, "Sürüm v%s indirildi." % ver)

    def sync_playlists(self):
        """Uzak data/playlists.json yeni kayıtlarını yerel listeye ekler. Eklenen sayısını döndürür."""
        if not self.enabled or self._library is None:
            return 0
        url = RAW_URL % (self.repo, self.branch, PLAYLISTS_PATH)
        r = requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=TIMEOUT)
        r.raise_for_status()
        return self._library.merge_remote(r.json())

    # ----- kurulum ---------------------------------------------------
    def apply_pending(self):
        """Hazır sürümü kurar: update.bat'ı görünmez başlatır. True ise çağıran hemen çıkmalı."""
        with self._state_lock:
            ver, staged = self._staged_version, self._staged_path
        if not ver or staged is None or not is_frozen():
            return False
        exe = self.exe
        old = exe.with_name(exe.name + ".old")
        bat = self.base / "data" / "update.bat"
        bat.parent.mkdir(parents=True, exist_ok=True)
        script = build_update_script(os.getpid(), exe, Path(staged), old)
        with open(bat, "w", encoding="utf-8", newline="") as f:
            f.write(script)
        subprocess.Popen(["cmd.exe", "/d", "/c", str(bat)], cwd=str(self.base),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW,
                         close_fds=True)
        log.info("güncelleme kurulumu başlatıldı: v%s", ver)
        return True

    # ----- arka plan -------------------------------------------------
    def start_background(self, library=None, on_result=None):
        """Açılışta ve her CHECK_INTERVAL'de: playlist senkronu + sürüm denetimi.

        on_result(CheckResult) yalnızca 'staged' ya da 'available' durumunda çağrılır.
        """
        self._library = library
        self._on_result = on_result
        threading.Thread(target=self._bg_loop, name="guncelleme", daemon=True).start()

    def _bg_loop(self):
        while not self._stop.is_set():
            try:
                added = self.sync_playlists()
                if added:
                    log.info("uzak playlist listesinden %d yeni kayıt eklendi", added)
            except Exception as e:
                log.info("uzak playlist listesi alınamadı: %s", e)
            try:
                res = self.check()
                if res.status in ("staged", "available") and self._on_result:
                    self._on_result(res)
            except Exception:
                log.exception("güncelleme denetimi başarısız")
            self._stop.wait(CHECK_INTERVAL)

    def stop(self):
        self._stop.set()
