"""Radyo motoru.

- yt-dlp ile videonun ses akış adresi çözülür (indirme yok, cookie yok).
- python-vlc ile akış çalınır. Bir sonraki şarkı önceden çözülür (prefetch),
  böylece geçişler kısa sürer.
- Anons sırası: ayarlanabilir aralıkta ağırlıklı rastgele anons çalınır.
"""

import logging
import os
import socket
import threading
import time
from collections import deque

import yt_dlp

log = logging.getLogger("radyo.player")

STREAM_OPTS = {
    "format": "bestaudio/best",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "no_color": True,
}
WATCH_URL = "https://www.youtube.com/watch?v={}"
STREAM_MAX_AGE = 90 * 60   # akış adresi bu süreden eskiyse yeniden çözülür
STALL_TIMEOUT = 45         # bu kadar süre oynamazsa hata sayılır
STREAM_CACHE_MAX = 200


def internet_ok(timeout=3):
    try:
        socket.create_connection(("www.youtube.com", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def resolve_stream(video_id):
    """(akış_url, http_başlıkları) döndürür. Cookie kullanılmaz."""
    with yt_dlp.YoutubeDL(STREAM_OPTS) as ydl:
        info = ydl.extract_info(WATCH_URL.format(video_id), download=False)
    if not info:
        raise RuntimeError("video bilgisi alınamadı: " + video_id)
    url = info.get("url")
    headers = dict(info.get("http_headers") or {})
    if not url:
        fmts = [f for f in (info.get("formats") or [])
                if f.get("url") and f.get("acodec") not in (None, "none")]
        if not fmts:
            raise RuntimeError("ses akışı bulunamadı: " + video_id)
        best = max(fmts, key=lambda f: f.get("abr") or 0)
        url = best["url"]
        headers = dict(best.get("http_headers") or headers)
    return url, headers


class RadioEngine:
    def __init__(self, cfg, library, announcer):
        try:
            import vlc  # python-vlc; libvlc (VLC) kurulu olmalı
        except Exception as e:
            raise RuntimeError(
                "python-vlc yüklenemedi. 64-bit VLC kurulu olmalı. Ayrıntı: %s" % e
            ) from e
        self.vlc = vlc
        self.library = library
        self.announcer = announcer
        self.instance = vlc.Instance("--no-video", "--quiet")
        self.player = self.instance.media_player_new()
        self.player.audio_set_volume(int(cfg.get("volume", 70)))

        self.announce_every = max(1, int(cfg.get("announce_every", 4)))
        self.announce_enabled = bool(cfg.get("announce_enabled", True))
        self.recent = deque(maxlen=max(1, int(cfg.get("recent_window", 200))))
        self.bad_ids = set()
        self.tracks_since_ad = 0
        self._planned = None
        self._retry = None
        self._streams = {}          # video_id -> (url, headers, zaman)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._skip = threading.Event()
        self._status = "Başlatılıyor"
        self._now = {"kind": "", "title": "", "id": ""}
        self.thread = threading.Thread(target=self._run, name="radyo", daemon=True)

    # ----- dış arayüz -------------------------------------------------
    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._skip.set()
        try:
            self.player.stop()
        except Exception:
            pass

    def toggle_pause(self):
        st = self.player.get_state()
        if st == self.vlc.State.Paused:
            self.player.set_pause(0)
        elif st in (self.vlc.State.Playing, self.vlc.State.Buffering, self.vlc.State.Opening):
            self.player.set_pause(1)

    def skip(self):
        self._skip.set()

    def set_volume(self, value):
        self.player.audio_set_volume(max(0, min(100, int(value))))

    def set_announce_every(self, n):
        self.announce_every = max(1, int(n))

    def set_announce_enabled(self, on):
        self.announce_enabled = bool(on)

    def snapshot(self):
        with self._lock:
            now = dict(self._now)
            status = self._status
        paused = self.player.get_state() == self.vlc.State.Paused
        until_ad = None
        if self.announce_enabled and self.announcer.files:
            until_ad = max(0, self.announce_every - self.tracks_since_ad)
        return {
            "kind": now["kind"], "title": now["title"], "id": now["id"],
            "status": status, "paused": paused, "until_ad": until_ad,
        }

    # ----- iç işleyiş -------------------------------------------------
    def _set_status(self, text):
        with self._lock:
            self._status = text

    def _set_now(self, kind, title, vid, status):
        with self._lock:
            self._now = {"kind": kind, "title": title, "id": vid}
            self._status = status

    def _sleep(self, seconds):
        self._stop.wait(seconds)

    def _exclude(self):
        return set(self.recent) | self.bad_ids

    def _run(self):
        while not self._stop.is_set():
            try:
                item = self._next_item()
                if item is None:
                    self._set_now("", "", "", "Kuyruk boş: playlist ekleyin ya da yenileyin")
                    self._sleep(3)
                    continue
                kind, payload = item
                if kind == "ad":
                    self._play_ad(payload)
                else:
                    self._play_track(payload)
            except Exception:
                log.exception("oynatma döngüsü hatası")
                self._sleep(2)

    def _next_item(self):
        if self.announce_enabled and self.tracks_since_ad >= self.announce_every:
            path = self.announcer.pick()
            if path:
                return ("ad", path)
        if self._retry is not None:
            track, self._retry = self._retry, None
            return ("track", track)
        track = self._planned or self.library.pick(self._exclude())
        self._planned = None
        if track is None:
            return None
        self.recent.append(track.id)
        return ("track", track)

    def _plan_next(self, current_id):
        """Bir sonraki şarkıyı seçer ve akış adresini arka planda çözer."""
        track = self.library.pick(self._exclude() | {current_id})
        self._planned = track
        if track is not None:
            threading.Thread(target=self._prefetch, args=(track,), daemon=True).start()

    def _prefetch(self, track):
        try:
            self._get_stream(track, retry=False)
        except Exception:
            log.debug("ön yükleme başarısız: %s", track.id)

    def _get_stream(self, track, retry=True):
        """Akış (url, headers) döndürür; çözülemezse None."""
        with self._lock:
            cached = self._streams.get(track.id)
        if cached and time.time() - cached[2] < STREAM_MAX_AGE:
            return cached[0], cached[1]

        attempts = 0
        while not self._stop.is_set():
            try:
                url, headers = resolve_stream(track.id)
            except Exception as e:
                attempts += 1
                if not internet_ok():
                    if not retry:
                        return None
                    self._set_status("İnternet yok, yeniden deneniyor...")
                    self._sleep(min(5 * attempts, 30))
                    continue
                log.warning("akış çözülemedi (%s): %s", track.title, e)
                if attempts >= 2 or not retry:
                    return None
                self._sleep(1)
                continue
            with self._lock:
                self._streams[track.id] = (url, headers, time.time())
                if len(self._streams) > STREAM_CACHE_MAX:
                    oldest = min(self._streams, key=lambda k: self._streams[k][2])
                    self._streams.pop(oldest, None)
            return url, headers
        return None

    def _play_track(self, track):
        self._set_now("track", track.title, track.id, "Yükleniyor...")
        self._plan_next(track.id)
        stream = self._get_stream(track)
        if stream is None:
            if not self._stop.is_set():
                self.bad_ids.add(track.id)
                self._set_status("Atlandı (akış alınamadı): " + track.title)
            return
        url, headers = stream
        self.tracks_since_ad += 1
        outcome = self._play_media(url, headers, "Çalıyor")
        if outcome != "error":
            return
        self._streams.pop(track.id, None)
        if not internet_ok():
            self._set_status("İnternet koptu; şarkı yeniden denenecek")
            while not self._stop.is_set() and not internet_ok():
                self._sleep(5)
            self._retry = track
        else:
            log.warning("oynatma hatası, atlanıyor: %s", track.title)

    def _play_ad(self, path):
        self._set_now("ad", os.path.basename(path), "", "Anons çalıyor")
        self.tracks_since_ad = 0
        self._play_media(path, {}, "Anons çalıyor")

    def _play_media(self, src, headers, label):
        media = self.instance.media_new(src)
        ua = headers.get("User-Agent")
        if ua:
            media.add_option(":http-user-agent=" + ua)
        ref = headers.get("Referer")
        if ref:
            media.add_option(":http-referrer=" + ref)
        self.player.set_media(media)
        media.release()
        self.player.play()
        self._set_status(label)
        return self._wait_end()

    def _wait_end(self):
        V = self.vlc.State
        last_progress = time.time()
        while not self._stop.is_set():
            if self._skip.is_set():
                self._skip.clear()
                self.player.stop()
                return "skipped"
            st = self.player.get_state()
            if st == V.Ended:
                return "ended"
            if st == V.Error:
                self.player.stop()
                return "error"
            if st in (V.Playing, V.Paused):
                last_progress = time.time()
            elif time.time() - last_progress > STALL_TIMEOUT:
                self.player.stop()
                return "error"
            self._sleep(0.3)
        self.player.stop()
        return "stopped"
