"""Özel istek: YouTube araması, bağlantı çözümleme ve istek kuyruğu.

- search_all(): "ytsearch5:" ile şarkılar + YouTube sonuç sayfasından ilk 5 playlist.
  yt-dlp flat modunda, cookie kullanmadan çalışır; indirme yapmaz.
- parse_link(): YouTube video / playlist (list=) bağlantılarını tanır.
- RequestQueue: istek kuyruğu. Öğe ya tek şarkıdır ya da bir playlist (sırayla/karışık);
  playlist öğesi kendi içinde ilerler. Kuyruk boşalınca radyo normal havuzdan devam eder.
"""

import random
import re
import threading
import urllib.parse
from collections import deque
from dataclasses import dataclass, field

import yt_dlp

from playlists import Track

SEARCH_SONGS = 5
SEARCH_PLAYLISTS = 5
SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "skip_download": True,
    "extract_flat": "in_playlist",
    "ignoreerrors": True,
    "noplaylist": True,
}
SONG_QUERY = "ytsearch%d:%s"
PLAYLIST_RESULTS = "https://www.youtube.com/results?search_query=%s&sp=EgIQAw%%3D%%3D"
VIDEO_RE = re.compile(r"(?:[?&]v=|youtu\.be/|/shorts/|/embed/)([A-Za-z0-9_-]{11})")
LIST_RE = re.compile(r"[?&]list=([A-Za-z0-9_-]+)")
PLAYLIST_WATCH = "https://www.youtube.com/playlist?list=%s"


def parse_link(text):
    """Metni YouTube bağlantısı olarak çözer.

    ("playlist", pid) | ("video", vid) | None. list= içeren bağlantı playlist sayılır.
    """
    text = (text or "").strip()
    m = LIST_RE.search(text)
    if m and ("youtube" in text or "youtu.be" in text or text.startswith("PL")):
        return ("playlist", m.group(1))
    if "youtube" in text or "youtu.be" in text:
        m = VIDEO_RE.search(text)
        if m:
            return ("video", m.group(1))
    return None


def _thumb_of(entry):
    t = entry.get("thumbnail")
    if t:
        return t
    thumbs = entry.get("thumbnails") or []
    return thumbs[-1].get("url", "") if thumbs else ""


def _song_row(e):
    vid = str(e.get("id") or "")
    if not vid:
        return None
    return {
        "id": vid,
        "title": e.get("title") or vid,
        "channel": e.get("channel") or e.get("uploader") or "",
        "duration": e.get("duration"),
    }


def video_info(vid):
    """Tek bir video için başlık ve kanal bilgisi (indirme yok)."""
    opts = dict(SEARCH_OPTS, extract_flat=False)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info("https://www.youtube.com/watch?v=" + vid, download=False,
                                process=False)
    row = _song_row(info or {"id": vid})
    if row is None:
        raise ValueError("Video bilgisi alınamadı")
    return row


def search_songs(query, n=SEARCH_SONGS):
    with yt_dlp.YoutubeDL(SEARCH_OPTS) as ydl:
        info = ydl.extract_info(SONG_QUERY % (n, query), download=False)
    rows, seen = [], set()
    for e in (info or {}).get("entries") or []:
        row = _song_row(e or {})
        if row and row["id"] not in seen:
            seen.add(row["id"])
            rows.append(row)
    return rows[:n]


def search_playlists(query, n=SEARCH_PLAYLISTS):
    url = PLAYLIST_RESULTS % urllib.parse.quote_plus(query)
    with yt_dlp.YoutubeDL(SEARCH_OPTS) as ydl:
        info = ydl.extract_info(url, download=False)
    rows, seen = [], set()
    for e in (info or {}).get("entries") or []:
        if not e:
            continue
        link = e.get("url") or ""
        m = LIST_RE.search(link)
        pid = m.group(1) if m else ""
        if not pid or pid in seen:
            continue
        seen.add(pid)
        rows.append({
            "id": pid,
            "title": e.get("title") or pid,
            "thumbnail": _thumb_of(e),
            "count": None,   # flat aramada şarkı sayısı gelmez; eklenince bilinir
        })
        if len(rows) >= n:
            break
    return rows


def search_all(query):
    """{"songs": [...], "playlists": [...]} döndürür. Hata olursa istisna fırlatır."""
    query = (query or "").strip()
    return {"songs": search_songs(query), "playlists": search_playlists(query)}


# ----- istek kuyruğu -----------------------------------------------------
@dataclass
class Request:
    id: int
    kind: str                 # "track" | "playlist"
    title: str
    thumb: str                # track: video id; playlist: playlist id
    tracks: list = field(default_factory=list)   # [Track]
    pos: int = 0              # sıradaki çalınacak şarkının indeksi
    mode: str = "seq"         # "seq" | "shuffle"

    def remaining(self):
        return max(0, len(self.tracks) - self.pos)


class RequestQueue:
    """İstek kuyruğu (iş parçacığı güvenli). Öğeler sırayla, FIFO biçiminde tüketilir."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items = deque()
        self._seq = 0
        self.version = 0          # her değişimde artar; arayüz yalnızca değişince yeniden çizer

    def _next_id(self):
        self._seq += 1
        return self._seq

    # ----- öğe üretimi (kuyruğa eklemeden) ---------------------------
    def make_track(self, vid, title, playlist_id=""):
        with self._lock:
            rid = self._next_id()
        t = Track(str(vid), title or str(vid), playlist_id or "")
        return Request(id=rid, kind="track", title=t.title, thumb=t.id, tracks=[t])

    def make_playlist(self, pid, title, tracks, mode="seq", thumb=""):
        """tracks: [{"id","title"}] ya da [Track]. mode "shuffle" ise sıra karıştırılır."""
        items = []
        for t in tracks or []:
            if isinstance(t, Track):
                items.append(t)
            elif isinstance(t, dict) and t.get("id"):
                items.append(Track(str(t["id"]), t.get("title") or str(t["id"]), str(pid)))
        if not items:
            raise ValueError("Playlist boş")
        if mode == "shuffle":
            random.shuffle(items)
        else:
            mode = "seq"
        with self._lock:
            rid = self._next_id()
        return Request(id=rid, kind="playlist", title=title or str(pid), thumb=thumb or str(pid),
                       tracks=items, mode=mode)

    # ----- kuyruk işlemleri ------------------------------------------
    def add(self, req):
        with self._lock:
            self._items.append(req)
            self.version += 1

    def is_empty(self):
        with self._lock:
            return not self._items

    def __len__(self):
        with self._lock:
            return len(self._items)

    def pop_next(self):
        """Sıradaki şarkıyı (Track, istek_id) olarak verir; kuyruk boşsa None."""
        with self._lock:
            while self._items:
                req = self._items[0]
                if req.pos >= len(req.tracks):
                    self._items.popleft()
                    self.version += 1
                    continue
                track = req.tracks[req.pos]
                req.pos += 1
                if req.pos >= len(req.tracks):
                    self._items.popleft()   # son şarkı seçildi; öğe bitti
                self.version += 1
                return track, req.id
            return None

    def remove(self, rid):
        with self._lock:
            for req in list(self._items):
                if req.id == rid:
                    self._items.remove(req)
                    self.version += 1
                    return True
            return False

    def advance(self, rid):
        """Playlist öğesinde sıradaki bir şarkıyı atlar (kuyruk tarafı). Öğe bittiyse kaldırılır."""
        with self._lock:
            for req in list(self._items):
                if req.id == rid:
                    req.pos += 1
                    if req.pos >= len(req.tracks):
                        self._items.remove(req)
                    self.version += 1
                    return True
            return False

    def snapshot(self):
        """Arayüz için öğeler: id, tür, başlık, kapak kimliği, çalınan/toplam."""
        with self._lock:
            out = []
            for req in self._items:
                out.append({
                    "id": req.id, "kind": req.kind, "title": req.title, "thumb": req.thumb,
                    "done": req.pos, "total": len(req.tracks), "mode": req.mode,
                })
            return out
