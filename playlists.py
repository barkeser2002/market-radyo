"""Playlist kütüphanesi.

- data/playlists.json: playlist listesi [{id,url,title,thumbnail,count,genre,enabled}]
- data/cache/tracks.json: her playlist için şarkı önbelleği (yt-dlp flat playlist, cookie yok)
- data/parts/*.json: ek kaynaklardan gelen playlist listeleri (aynı biçim), açılışta birleşir
- data/dismissed.json: kullanıcının sildiği playlist id'leri (güncellemede geri gelmez)
"""

import json
import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import yt_dlp

log = logging.getLogger("radyo.playlists")

FLAT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    "ignoreerrors": True,
}
PLAYLIST_URL = "https://www.youtube.com/playlist?list={}"
THUMB_URL = "https://i.ytimg.com/vi/{}/mqdefault.jpg"
BG_INTERVAL = 1800  # arka planda 30 dakikada bir eski önbellekleri kontrol et


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    playlist_id: str


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def download_image(url, dest):
    """Resmi dest'e indirir. Dosya zaten varsa True döner."""
    dest = Path(dest)
    if dest.exists():
        return True
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(r.content)
        os.replace(tmp, dest)
        return True
    except Exception as e:
        log.debug("resim indirilemedi %s: %s", url, e)
        return False


def normalize_entry(e):
    if not isinstance(e, dict) or not e.get("id"):
        return None
    pid = str(e["id"])
    try:
        count = int(e.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    return {
        "id": pid,
        "url": e.get("url") or PLAYLIST_URL.format(pid),
        "title": e.get("title") or pid,
        "thumbnail": e.get("thumbnail") or "",
        "count": count,
        "genre": e.get("genre") or "karışık",
        "enabled": bool(e.get("enabled", True)),
    }


def fetch_playlist(url):
    """yt-dlp ile playlist bilgisi ve şarkı listesi. Cookie yok, indirme yok."""
    with yt_dlp.YoutubeDL(FLAT_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info or "entries" not in info:
        raise ValueError("Bu bir playlist değil ya da okunamadı")
    pid = str(info.get("id") or "")
    if not pid:
        raise ValueError("Playlist kimliği alınamadı")
    tracks, seen = [], set()
    for e in info.get("entries") or []:
        if not e:
            continue
        vid = e.get("id")
        if not vid or vid in seen:
            continue
        seen.add(vid)
        tracks.append({"id": str(vid), "title": e.get("title") or str(vid)})
    if not tracks:
        raise ValueError("Playlist boş ya da okunamadı")
    thumb = info.get("thumbnail") or ""
    if not thumb and info.get("thumbnails"):
        thumb = info["thumbnails"][-1].get("url", "") or ""
    if not thumb:
        thumb = THUMB_URL.format(tracks[0]["id"])
    return {
        "id": pid,
        "title": info.get("title") or "Playlist",
        "thumbnail": thumb,
        "tracks": tracks,
    }


class Library:
    def __init__(self, base, cfg):
        self.base = Path(base)
        self.data = self.base / "data"
        self.cache_dir = self.data / "cache"
        self.thumb_dir = self.cache_dir / "thumbs"
        self.thumb_dir.mkdir(parents=True, exist_ok=True)
        self.cache_hours = float(cfg.get("cache_hours", 24))
        self.playlists_file = self.data / "playlists.json"
        self.tracks_file = self.cache_dir / "tracks.json"
        self.dismissed_file = self.data / "dismissed.json"
        self.lock = threading.RLock()
        self._stop = threading.Event()
        self._pool = None
        self.on_change = None
        self.dismissed = set(read_json(self.dismissed_file, []))
        self.playlists = self._load_playlists()
        self.cache = read_json(self.tracks_file, {})
        if not isinstance(self.cache, dict):
            self.cache = {}
        self.cache.setdefault("playlists", {})

    # ----- yükleme / kayıt -------------------------------------------
    def _read_parts(self):
        items = []
        parts = self.data / "parts"
        if parts.is_dir():
            for f in sorted(parts.glob("*.json")):
                data = read_json(f, [])
                if isinstance(data, dict):
                    data = data.get("playlists", [])
                if isinstance(data, list):
                    items.extend(data)
        return items

    def _load_playlists(self):
        raw = read_json(self.playlists_file, [])
        if not isinstance(raw, list):
            raw = []
        items, seen = [], set()
        for e in raw + self._read_parts():
            n = normalize_entry(e)
            if n and n["id"] not in seen and n["id"] not in self.dismissed:
                seen.add(n["id"])
                items.append(n)
        return items

    def _save_playlists(self):
        write_json(self.playlists_file, self.playlists)

    def _save_cache(self):
        write_json(self.tracks_file, self.cache)

    def _save_dismissed(self):
        write_json(self.dismissed_file, sorted(self.dismissed))

    def _notify(self):
        cb = self.on_change
        if cb:
            try:
                cb()
            except Exception:
                log.exception("arayüz bildirimi hatası")

    def _find(self, pid):
        for p in self.playlists:
            if p["id"] == pid:
                return p
        return None

    def _apply(self, pid, info):
        """Lock altında çağrılır: önbelleği ve playlist kaydını günceller."""
        entry = self._find(pid)
        self.cache["playlists"][pid] = {"updated": time.time(), "tracks": info["tracks"]}
        entry["count"] = len(info["tracks"])
        entry["title"] = info["title"] or entry["title"]
        if info["thumbnail"]:
            entry["thumbnail"] = info["thumbnail"]
        self._pool = None
        self._save_playlists()
        self._save_cache()

    # ----- dışarıya açık işlemler -------------------------------------
    def snapshot_playlists(self):
        with self.lock:
            return [dict(p) for p in self.playlists]

    def get(self, pid):
        with self.lock:
            p = self._find(pid)
            return dict(p) if p else None

    def add_playlist(self, url, genre="karışık"):
        info = fetch_playlist(url.strip())
        pid = info["id"]
        with self.lock:
            entry = self._find(pid)
            if entry is None:
                entry = normalize_entry({
                    "id": pid, "url": PLAYLIST_URL.format(pid), "title": info["title"],
                    "thumbnail": info["thumbnail"], "genre": genre, "enabled": True,
                })
                self.playlists.append(entry)
            self.dismissed.discard(pid)
            self._save_dismissed()
            self._apply(pid, info)
            result = dict(entry)
        self._notify()
        return result

    def refresh_playlist(self, pid):
        with self.lock:
            entry = self._find(pid)
            if entry is None:
                return False
            url = entry["url"]
        info = fetch_playlist(url)
        with self.lock:
            if self._find(pid) is None:
                return False
            self._apply(pid, info)
        self._notify()
        return True

    def refresh_stale(self, force=False):
        now = time.time()
        with self.lock:
            todo = []
            for p in self.playlists:
                c = self.cache["playlists"].get(p["id"])
                if force or not c or now - c.get("updated", 0) > self.cache_hours * 3600:
                    todo.append(p["id"])
        for pid in todo:
            if self._stop.is_set():
                break
            try:
                self.refresh_playlist(pid)
            except Exception as e:
                log.warning("playlist yenilenemedi %s: %s", pid, e)

    def set_enabled(self, pid, on):
        with self.lock:
            p = self._find(pid)
            if p:
                p["enabled"] = bool(on)
                self._pool = None
                self._save_playlists()
        self._notify()

    def set_genre(self, pid, genre):
        with self.lock:
            p = self._find(pid)
            if p:
                p["genre"] = genre or "karışık"
                self._save_playlists()
        self._notify()

    def remove_playlist(self, pid):
        with self.lock:
            self.playlists = [p for p in self.playlists if p["id"] != pid]
            self.cache["playlists"].pop(pid, None)
            self.dismissed.add(pid)
            self._pool = None
            self._save_playlists()
            self._save_cache()
            self._save_dismissed()
        self._notify()

    def merge_remote(self, remote):
        """Uzak playlist listesindeki yeni kayıtları ekler; yerel kayıtlara dokunmaz."""
        added = 0
        with self.lock:
            have = {p["id"] for p in self.playlists}
            for e in remote if isinstance(remote, list) else []:
                n = normalize_entry(e)
                if n and n["id"] not in have and n["id"] not in self.dismissed:
                    self.playlists.append(n)
                    have.add(n["id"])
                    added += 1
            if added:
                self._pool = None
                self._save_playlists()
        if added:
            self._notify()
        return added

    def pool(self):
        with self.lock:
            if self._pool is None:
                out, seen = [], set()
                for p in self.playlists:
                    if not p.get("enabled"):
                        continue
                    c = self.cache["playlists"].get(p["id"], {})
                    for t in c.get("tracks", []):
                        if t["id"] in seen:
                            continue
                        seen.add(t["id"])
                        out.append(Track(t["id"], t.get("title") or t["id"], p["id"]))
                self._pool = out
            return self._pool

    def count(self):
        return len(self.pool())

    def pick(self, exclude=()):
        """Rastgele bir şarkı. exclude'daki id'ler seçilmez (havuz küçükse kısıt gevşer)."""
        pool = self.pool()
        if not pool:
            return None
        ex = exclude if isinstance(exclude, (set, frozenset)) else set(exclude)
        candidates = [t for t in pool if t.id not in ex] or pool
        return random.choice(candidates)

    def thumb_for_track(self, vid):
        return self.thumb_dir / ("v_%s.jpg" % vid)

    def thumb_for_playlist(self, pid):
        return self.thumb_dir / ("pl_%s.jpg" % pid)

    # ----- arka plan --------------------------------------------------
    def start_background(self, on_change=None):
        self.on_change = on_change
        threading.Thread(target=self._bg_loop, name="playlist-refresh", daemon=True).start()

    def _bg_loop(self):
        while not self._stop.is_set():
            self.refresh_stale()
            self._stop.wait(BG_INTERVAL)

    def stop(self):
        self._stop.set()
