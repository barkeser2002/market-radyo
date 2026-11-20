"""Radyo motoru.

- yt-dlp ile videonun yalnızca ses akış adresi çözülür (indirme yok, cookie yok, video yok).
- ffmpeg ses akışını 44.1 kHz stereo float32 PCM'e çevirir; audio.Mixer sounddevice ile çalar.
- Parçalar crossfade_sec saniye (varsayılan 4, 0-10) üst üste bindirilerek geçilir.
  Sonraki şarkı önceden çözülür ve tamponlanır; geçiş equal-power.
- Anonslar dakika bazlıdır (announce_every_min, varsayılan 10): müzik çalarken süre
  sayılır, duraklatmada ve anons sırasında durur.
- Anons modu (announce_mode):
  * "duck" (varsayılan): süre dolunca şarkı kesilmez; müzik duck_level'a (varsayılan 0.15)
    kısılır, anons ayrı kanalda üstüne bindirilir, bitince müzik eski sesine döner.
  * "pause": müzik announce_fade_sec (varsayılan 2) içinde kısılıp duraklar, anons tam sesle
    çalar, bitince şarkı kaldığı saniyeden equal-power ile devam eder. Duraklatılmış akış
    açık tutulur; bozulursa aynı adresten -ss ile kaldığı yerden yeniden açılır.
- Anons seçimi: ağırlıklı rastgele (ads.py).
- Özel istek (istek.py): kullanıcı şarkı ya da playlist ekler; istekler kuyruğun sonundan
  sırayla çalınır, kuyruk boşalınca radyo havuzdan devam eder. Kuyruk değişiklikleri motor
  iş parçacığında, komut kuyruğu üzerinden uygulanır.
"""

import logging
import os
import socket
import threading
import time
from collections import deque

import yt_dlp

from audio import DUCK_DEFAULT, SR, Mixer, PcmStream
from ffmpeg_util import ffmpeg_path
from istek import RequestQueue

log = logging.getLogger("radyo.player")

STREAM_OPTS = {
    "format": "bestaudio[ext=m4a]/bestaudio/best[height<=144]",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "skip_download": True,
    "no_color": True,
}
WATCH_URL = "https://www.youtube.com/watch?v={}"
STREAM_MAX_AGE = 90 * 60   # akış adresi bu süreden eskiyse yeniden çözülür
STALL_TIMEOUT = 45         # tampon bu kadar süre boş kalırsa akış koptu sayılır
STREAM_CACHE_MAX = 200
TICK = 0.1                 # ana döngü aralığı (sn)
FADE_IN_SEC = 0.5          # sıfırdan başlatma
SKIP_FADE_SEC = 0.4        # "Sonraki" düğmesi
PRIME_SEC = 5.0            # geçişten bu kadar önce sonraki akış açılır
ARMED_WAIT = 3.0           # önceden açılan akış hazır olmasa bile bu süreden sonra geçilir
READY_FRAMES = SR // 2     # geçişten önce en az bu kadar kare tamponlu olsun
CROSSFADE_MAX = 10.0
CROSSFADE_DEFAULT = 4.0
ANNOUNCE_MIN_MAX = 60      # anons aralığı dakika cinsinden (1-60)
ANNOUNCE_MIN_DEFAULT = 10
ANN_READY_FRAMES = SR // 4  # anons kanala alınmadan önce bu kadar kare tamponlu olsun
ANN_WAIT = 3.0             # anons akışı bu sürede hazır olmazsa yine de başlatılır
ANNOUNCE_MODES = ("duck", "pause")
FADE_DEFAULT = 2.0         # duraklatma modunda şarkının kısılma / geri gelme süresi (sn)
FADE_MAX = 3.0
RETRY_MAX = 3              # duraklatılmış akış yeniden açılırken ağ hatasında deneme sayısı
AUDIO_RETRY_SEC = 5.0      # ses aygıtı yokken ya da koptuğunda yeniden deneme aralığı (sn)


def clamp_announce_min(value):
    try:
        v = int(round(float(value)))
    except (TypeError, ValueError):
        v = ANNOUNCE_MIN_DEFAULT
    return max(1, min(ANNOUNCE_MIN_MAX, v))


def clamp_duck(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = DUCK_DEFAULT
    return max(0.0, min(1.0, v))


def clamp_announce_mode(value):
    return "pause" if str(value or "").strip().lower() == "pause" else "duck"


def clamp_fade(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = FADE_DEFAULT
    return max(0.0, min(FADE_MAX, v))


class AnnounceTimer:
    """Anons zamanlayıcısı: yalnızca 'sayılıyor' iken geçen süreyi toplar.

    update(counting) her tick'te çağrılır; counting False iken (duraklatma, anons,
    şarkı yok) süre birikmez. Toplam interval'e ulaşınca update True döner.
    reset() sayacı sıfırlar. clock enjekte edilebilir (testler sahte saat kullanır).
    """

    def __init__(self, interval_sec, clock=time.monotonic):
        self.interval = float(interval_sec)
        self.elapsed = 0.0
        self._clock = clock
        self._last = None

    def set_interval(self, interval_sec):
        self.interval = float(interval_sec)

    def reset(self):
        self.elapsed = 0.0
        self._last = None

    def update(self, counting):
        now = self._clock()
        if self._last is not None and counting:
            self.elapsed += max(0.0, now - self._last)
        self._last = now
        return self.elapsed >= self.interval

    def remaining(self):
        return max(0.0, self.interval - self.elapsed)


def internet_ok(timeout=3):
    try:
        socket.create_connection(("www.youtube.com", 443), timeout=timeout).close()
        return True
    except OSError:
        return False


def resolve_stream(video_id):
    """(akış_url, http_başlıkları, süre_sn_veya_None) döndürür. Cookie kullanılmaz."""
    with yt_dlp.YoutubeDL(STREAM_OPTS) as ydl:
        info = ydl.extract_info(WATCH_URL.format(video_id), download=False)
    if not info:
        raise RuntimeError("video bilgisi alınamadı: " + video_id)
    url = info.get("url")
    headers = dict(info.get("http_headers") or {})
    duration = info.get("duration")
    if not url:
        fmts = [f for f in (info.get("formats") or [])
                if f.get("url") and f.get("acodec") not in (None, "none")]
        if not fmts:
            raise RuntimeError("ses akışı bulunamadı: " + video_id)
        best = max(fmts, key=lambda f: f.get("abr") or 0)
        url = best["url"]
        headers = dict(best.get("http_headers") or headers)
        duration = best.get("duration") or duration
    return url, headers, (float(duration) if duration else None)


def input_cmd(src, headers=None, start=0.0):
    """ffmpeg komutu: kaynağı çözer, ham float32 stereo 44.1 kHz PCM'i stdout'a yazar.

    start > 0 ise kaynak o saniyeden açılır (ffmpeg -ss, girdiden önce: hızlı arama).
    """
    cmd = [ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-nostdin"]
    if src.lower().startswith("http"):
        hdrs = dict(headers or {})
        ua = hdrs.pop("User-Agent", None)
        cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
        if ua:
            cmd += ["-user_agent", ua]
        if hdrs:
            cmd += ["-headers", "".join("%s: %s\r\n" % (k, v) for k, v in hdrs.items())]
    if start and start > 0:
        cmd += ["-ss", "%.3f" % float(start)]
    cmd += ["-i", src, "-vn", "-f", "f32le", "-ac", "2", "-ar", "44100", "pipe:1"]
    return cmd


def clamp_crossfade(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = CROSSFADE_DEFAULT
    return max(0.0, min(CROSSFADE_MAX, v))


def reset_audio_backend():
    """PortAudio'yu yeniden başlatır ki sonradan takılan ses aygıtı görülsün. Hata yok sayılır."""
    try:
        import sounddevice as sd
        sd._terminate()
        sd._initialize()
    except Exception as e:
        log.debug("ses sürücüsü yeniden başlatılamadı: %s", e)


class RadioEngine:
    def __init__(self, cfg, library, announcer, on_item_start=None):
        self.library = library
        self.announcer = announcer
        self.on_item_start = on_item_start  # her şarkı başladığında item ile çağrılır (radyo iş parçacığında)
        self.mixer = Mixer()
        # Ses aygıtı yoksa motor yine kurulur: uyarı yazılır, _audio_tick belli aralıkla yeniden dener.
        self.mixer.open()
        self._audio_next_try = time.monotonic() + AUDIO_RETRY_SEC
        self.mixer.set_volume(int(cfg.get("volume", 70)) / 100.0)

        self.crossfade_sec = clamp_crossfade(cfg.get("crossfade_sec", CROSSFADE_DEFAULT))
        self.announce_enabled = bool(cfg.get("announce_enabled", True))
        self.announce_every_min = clamp_announce_min(cfg.get("announce_every_min", ANNOUNCE_MIN_DEFAULT))
        self.announce_mode = clamp_announce_mode(cfg.get("announce_mode", "duck"))
        self.announce_fade_sec = clamp_fade(cfg.get("announce_fade_sec", FADE_DEFAULT))
        self.mixer.set_duck_level(clamp_duck(cfg.get("duck_level", DUCK_DEFAULT)))
        self._timer = AnnounceTimer(self.announce_every_min * 60)
        self.recent = deque(maxlen=max(1, int(cfg.get("recent_window", 200))))
        self.bad_ids = set()
        self.requests = RequestQueue()   # özel istek kuyruğu (arayüz yalnızca okur ve komut gönderir)
        self._cmds = deque()             # arayüzden motora komutlar: ("add", Request) | ("remove", id) | ("skip_item", id)
        self._front = deque()      # yeniden denenecek şarkılar (önce bunlar çalınır)
        self._planned = None       # seçilmiş, henüz açılmamış şarkı: ("track"|"request", Track[, istek_id])
        self._armed = None         # (öğe, PcmStream, açılma_zamanı): geçişe hazır akış
        self._cur_item = None      # şu an çalan öğe
        self._skip_fade = None     # bir sonraki "atla" geçişinin süresi (None: SKIP_FADE_SEC)
        self._streams = {}         # video_id -> (url, headers, süre, zaman)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._skip = threading.Event()
        self._announce_now = threading.Event()  # "Anonsu şimdi çal" isteği
        self._ann_pending = None   # (PcmStream, açılma_zamanı): henüz anons kanalına verilmemiş anons
        self._ann_name = ""        # son hazırlanan/çalan anonsun dosya adı
        self._status = "Başlatılıyor"
        self._now = {"kind": "", "title": "", "id": ""}
        self.thread = threading.Thread(target=self._run, name="radyo", daemon=True)

    # ----- dış arayüz -------------------------------------------------
    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._skip.set()
        self._drop_armed()
        pending, self._ann_pending = self._ann_pending, None
        if pending is not None:
            pending[0].close()
        self.mixer.close()

    def toggle_pause(self):
        self.mixer.paused = not self.mixer.paused

    def skip(self):
        self._skip.set()

    def set_volume(self, value):
        self.mixer.set_volume(max(0, min(100, int(value))) / 100.0)

    def set_crossfade(self, value):
        self.crossfade_sec = clamp_crossfade(value)

    def set_announce_every_min(self, minutes):
        self.announce_every_min = clamp_announce_min(minutes)
        self._timer.set_interval(self.announce_every_min * 60)

    def set_announce_enabled(self, on):
        self.announce_enabled = bool(on)
        self._timer.reset()

    def set_duck_level(self, value):
        self.mixer.set_duck_level(clamp_duck(value))

    def set_announce_mode(self, mode):
        """Anons modu: "duck" ya da "pause". Sonraki anonsta geçerlidir."""
        self.announce_mode = clamp_announce_mode(mode)

    def set_announce_fade(self, seconds):
        self.announce_fade_sec = clamp_fade(seconds)

    def announce_now(self):
        """Anonsu aralıktan bağımsız hemen çalar. Zaten anons çalıyorsa istek yok sayılır."""
        self._announce_now.set()

    # ----- özel istek (arayüzden çağrılır; iş yükü motor iş parçacığında) ----
    def add_request(self, req):
        """Hazırlanmış bir istek öğesini (istek.RequestQueue.make_*) kuyruğun sonuna ekler."""
        self._cmds.append(("add", req))

    def remove_request(self, rid):
        self._cmds.append(("remove", rid))

    def skip_request_item(self, rid):
        """Playlist öğesinde 'sonraki şarkı'ya geçer (o playlistten bir sonraki parça)."""
        self._cmds.append(("skip_item", rid))

    def snapshot(self):
        with self._lock:
            now = dict(self._now)
            status = self._status
        announcing = self.mixer.announcement_active()
        if announcing:
            if self.mixer.hold_state() is not None:
                status = "Anons çalıyor: %s (şarkı duraklatıldı)" % self._ann_name
            else:
                status = "Anons çalıyor: %s (müzik kısık)" % self._ann_name
        until = None
        if self.announce_enabled and self.announcer.files:
            until = self._timer.remaining()  # saniye
        return {
            "kind": now["kind"], "title": now["title"], "id": now["id"],
            "status": status, "paused": bool(self.mixer.paused),
            "until_ann_sec": until, "announcing": announcing,
            "hold": self.mixer.hold_state(), "mode": self.announce_mode,
            "audio_ok": self.mixer.audio_ok(),
        }

    # ----- yardımcılar ------------------------------------------------
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

    def _drop_armed(self):
        armed, self._armed = self._armed, None
        if armed is not None:
            armed[1].close()

    # ----- ana döngü -------------------------------------------------
    def _run(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("oynatma döngüsü hatası")
                self._sleep(2)
                continue
            self._sleep(TICK)

    def _announcement_tick(self):
        """Anons zamanlayıcısını işletir. Süre dolunca (ya da istekle) anonsu hazırlar,
        hazır olunca ayrı kanala verir. Duraklatma modunda, önceki duraklatma ya da
        geçiş bitene kadar bekler."""
        pending = self._ann_pending
        if pending is not None:
            stream, t0 = pending
            ready = (stream.error or stream.eof or stream.buffered_frames() >= ANN_READY_FRAMES
                     or time.time() - t0 > ANN_WAIT)
            if not ready:
                return
            if self.mixer.hold_state() is not None or (self.announce_mode == "pause" and self.mixer.fading()):
                return  # önceki duraklatma / geçiş bitince verilecek
            self._ann_pending = None
            if stream.error:
                log.warning("anons açılamadı: %s", self._ann_name)
                stream.close()
                return
            self.mixer.play_announcement(stream, mode=self.announce_mode,
                                         fade_sec=self.announce_fade_sec)
            return
        if self.mixer.announcement_active():
            return  # anons çalarken süre sayılmaz
        counting = (self.announce_enabled and not self.mixer.paused
                    and self.mixer.current() is not None
                    and self._cur_item is not None and self._cur_item[0] in ("track", "request"))
        due = self._timer.update(counting)
        if not (due or self._announce_now.is_set()):
            return
        self._announce_now.clear()
        self._timer.reset()
        if not self.announcer.files:
            return
        path = self.announcer.pick()
        if not path or not os.path.isfile(path):
            log.warning("anons dosyası yok: %s", path)
            return
        self._ann_name = os.path.basename(path)
        self._ann_pending = (PcmStream(input_cmd(path), name=self._ann_name), time.time())

    def _process_commands(self):
        while self._cmds:
            try:
                cmd = self._cmds.popleft()
            except IndexError:
                break
            try:
                kind = cmd[0]
                if kind == "add":
                    self._cmd_add(cmd[1])
                elif kind == "remove":
                    self._cmd_remove(cmd[1])
                elif kind == "skip_item":
                    self._cmd_skip_item(cmd[1])
            except Exception:
                log.exception("istek komutu hatası: %s", cmd[0])

    def _cmd_add(self, req):
        """İsteği kuyruğa ekler. Çalan (ya da sıradaki) şarkı bir istek değilse ve kuyruk boşsa
        çalan şarkıdan hemen geçilir; çalan bir istekse yeni istek onun ardına girer."""
        cur = self._cur_item
        cur_is_request = cur is not None and cur[0] == "request"
        up = self._upcoming()
        up_is_request = up is not None and up[0] == "request"
        idle = self.requests.is_empty() and not up_is_request and not cur_is_request
        self.requests.add(req)
        if idle:
            self._request_switch()
        elif up is not None and not up_is_request:
            # sıradaki havuz şarkısı (henüz başlamamış) yerine kuyruktaki istek seçilsin
            self._drop_armed()
            self._planned = None

    def _request_switch(self):
        """Kuyruk boşken gelen istek: çalan şarkıdan crossfade ile hemen geçilir.

        Hemen geçilemiyorsa (anons ya da duraklatma sürüyorsa) istek, mevcut şarkı
        bittiğinde sıraya girer; bunun için önceden seçilmiş havuz şarkısı bırakılır.
        """
        self._drop_armed()
        self._planned = None
        playing = self.mixer.current() is not None or self.mixer.incoming() is not None
        if not playing or self.mixer.hold_state() is not None or self.mixer.announcement_active():
            return
        self._skip_fade = self.crossfade_sec
        self._skip.set()

    def _upcoming(self):
        """Sıradaki (önceden seçilmiş ya da önceden açılmış) öğe; yoksa None."""
        if self._armed is not None:
            return self._armed[0]
        return self._planned

    def _is_upcoming_request(self, rid):
        up = self._upcoming()
        return up is not None and up[0] == "request" and up[2] == rid

    def _replan(self):
        """Sıradaki öğe iptal edildi: açılmış akış kapatılır, yenisi kuyruktan seçilir."""
        self._drop_armed()
        self._planned = None
        self._planned = self._choose_next()

    def _cmd_remove(self, rid):
        self.requests.remove(rid)
        if self._is_upcoming_request(rid):
            self._replan()

    def _cmd_skip_item(self, rid):
        cur = self._cur_item
        if cur is not None and cur[0] == "request" and cur[2] == rid:
            self._skip_fade = None
            self._skip.set()           # çalan şarkı, aynı playlistten sıradakine geçer
            return
        if self._is_upcoming_request(rid):
            self._replan()             # sıradaki şarkı atlandı: aynı playlistin bir sonrakisi seçilir
            return
        self.requests.advance(rid)     # henüz sırası gelmemiş şarkı atlanır

    def _audio_tick(self):
        """Ses çıkışını izler. Aygıt yoksa ya da koptuysa AUDIO_RETRY_SEC aralıkla yeniden dener.

        Aygıt açıksa True. Aygıt geri geldiğinde çalan şarkı kaldığı yerden sürer.
        """
        m = self.mixer
        if m.audio_ok():
            if m.check_device():
                return True
            self._audio_next_try = time.monotonic() + AUDIO_RETRY_SEC
            self._set_status("Ses çıkışı bulunamadı, bekleniyor…")
            return False
        if time.monotonic() < self._audio_next_try:
            return False
        self._audio_next_try = time.monotonic() + AUDIO_RETRY_SEC
        reset_audio_backend()
        if not m.open():
            self._set_status("Ses çıkışı bulunamadı, bekleniyor…")
            return False
        self._set_status("Ses çıkışı bulundu")
        self._resume_after_outage()
        return True

    def _resume_after_outage(self):
        """Ses geri geldi. Çalan akış sağlamsa tamponundan sürer; bozulmuşsa aynı konumdan (-ss) yeniden açılır."""
        cur = self.mixer.current()
        item = self._cur_item
        if cur is None or item is None or self.mixer.hold_state() is not None:
            return
        if cur.resumable():
            return
        pos = cur.position_sec()
        self._set_status("Ses geri geldi; şarkı kaldığı yerden açılıyor")
        new = self._open_at(item, pos)
        if new is None:
            return  # açılamadı: normal akış kopma yolu (_on_failure / _switch) devreye girer
        self.mixer.transition(new, 0.0)

    def _tick(self):
        self._process_commands()
        if not self._audio_tick():
            return  # ses çıkışı yok: şarkı/anons başlatılmaz, komutlar yine işlenir
        self._announcement_tick()
        if self._skip.is_set():
            self._skip.clear()
            fade, self._skip_fade = self._skip_fade, None
            if self.mixer.hold_state() is not None:
                self.mixer.drop_hold()  # duraklatılmış şarkı iptal; sıradaki anonstan sonra çalar
                return
            if self.mixer.current() is None and self.mixer.announcement_active():
                return
            self._switch(SKIP_FADE_SEC if fade is None else fade)
            return
        if self.mixer.paused or self.mixer.fading():
            return
        if self.mixer.resume_wanted():
            self._reopen_held()
            return
        if self.mixer.hold_state() is not None:
            return  # şarkı anons için duraklatıldı: anons bitene kadar müzik işlemi yok

        cur = self.mixer.current()
        if cur is None:
            if self.mixer.announcement_active():
                return  # anons çalarken yeni şarkı başlamaz
            self._switch(FADE_IN_SEC)
            return

        failed = cur.error or cur.is_stalled(STALL_TIMEOUT)
        if failed or cur.is_finished():
            if failed:
                log.warning("akış koptu, sonraki öğeye geçiliyor: %s", cur.name)
                self._on_failure(self._cur_item)
            if not self._switch(0.0):
                # sıradaki öğe açılamadı: çalan akışı kapat, sonraki tick yeniden dener
                self.mixer.transition(None, 0.0)
            return

        xf = self.crossfade_sec
        rem = cur.remaining_frames() / float(SR)
        if self._armed is None and (rem <= xf + PRIME_SEC or cur.eof):
            self._armed = self._open_planned()
        if rem <= xf:
            if self._armed is None:
                self._armed = self._open_planned()
            if self._armed is not None:
                _, stream, t0 = self._armed
                ready = (stream.error or stream.eof
                         or stream.buffered_frames() >= READY_FRAMES
                         or time.time() - t0 > ARMED_WAIT)
                if ready:
                    self._switch(xf)

    def _reopen_held(self):
        """Duraklatılmış akış bozulmuş: aynı şarkıyı kaldığı konumdan (-ss) yeniden açar."""
        held = self.mixer.held_stream()
        item = self._cur_item
        if held is None or item is None:
            self.mixer.drop_hold()
            return
        pos = held.position_sec()
        self._set_status("Bağlantı koptu; şarkı kaldığı yerden açılıyor")
        new = self._open_at(item, pos)
        if new is None:
            self.mixer.drop_hold()
            self._set_status("Şarkı açılamadı, sıradaki şarkıya geçiliyor")
            return
        if not self.mixer.resume_held(new):
            new.close()

    def _open_at(self, item, pos):
        """item şarkısını pos saniyesinden açar (taze akış adresiyle). Olmazsa None."""
        track = item[1]
        attempts = 0
        while not self._stop.is_set():
            try:
                url, headers, duration = resolve_stream(track.id)
            except Exception as e:
                attempts += 1
                if not internet_ok():
                    self._set_status("İnternet yok, yeniden deneniyor...")
                    self._sleep(min(5 * attempts, 30))
                    continue
                log.warning("akış yeniden çözülemedi (%s): %s", track.title, e)
                if attempts >= RETRY_MAX:
                    return None
                self._sleep(1)
                continue
            with self._lock:
                self._streams[track.id] = (url, headers, duration, time.time())
            return PcmStream(input_cmd(url, headers, start=pos), duration=duration,
                             name=track.title, start=pos)
        return None

    def _on_failure(self, item):
        if item is not None and item[0] in ("track", "request"):
            if not internet_ok():
                self._drop_armed()  # önceden açılmış akış da internet kopmasından etkilenmiş olabilir
                self._front.appendleft(item)
                self._set_status("İnternet koptu; şarkı yeniden denenecek")
                while not self._stop.is_set() and not internet_ok():
                    self._sleep(5)

    def _switch(self, fade):
        """Hazır (önceden açılmış) ya da sıradaki öğeyi fade süreyle çalmaya başlatır."""
        for _ in range(3):
            armed, self._armed = self._armed, None
            if armed is None:
                armed = self._open_planned()
                if armed is None:
                    return False
            item, stream, _ = armed
            if stream.error:
                stream.close()
                continue
            self.mixer.transition(stream, fade)
            self._after_start(item)
            return True
        return False

    def _after_start(self, item):
        self._cur_item = item
        track = item[1]
        self.recent.append(track.id)
        if item[0] == "request":
            self._set_now("request", track.title, track.id, "Özel istek çalıyor")
        else:
            self._set_now("track", track.title, track.id, "Çalıyor")
        self._planned = self._choose_next()
        cb = self.on_item_start
        if cb is not None:
            try:
                cb(item)
            except Exception:
                log.exception("şarkı başlangıç bildirimi hatası")

    # ----- sıra seçimi ------------------------------------------------
    def _choose_next(self):
        # Anonslar bu kuyrukta değildir: anons zamanlayıcısı (_announcement_tick) ayrıca çalar.
        if self._front:
            return self._front.popleft()
        req = self.requests.pop_next()
        if req is not None:
            track, rid = req
            threading.Thread(target=self._prefetch, args=(track,), daemon=True).start()
            return ("request", track, rid)
        track = self.library.pick(self._exclude())
        if track is None:
            return None
        threading.Thread(target=self._prefetch, args=(track,), daemon=True).start()
        return ("track", track)

    def _next_item(self):
        if self._planned is not None:
            item, self._planned = self._planned, None
            return item
        return self._choose_next()

    def _open_planned(self):
        """Sıradaki şarkıyı açar. (öğe, PcmStream, zaman) ya da None döner."""
        item = self._next_item()
        if item is None:
            self._set_status("Kuyruk boş: playlist ekleyin ya da yenileyin")
            return None
        stream = self._open_item(item)
        if stream is None:
            return None
        return (item, stream, time.time())

    def _open_item(self, item):
        track = item[1]
        info = self._get_stream(track)
        if info is None:
            if not self._stop.is_set():
                self.bad_ids.add(track.id)
                log.warning("şarkı atlandı, akış alınamadı: %s (%s)", track.title, track.id)
                self._set_status("Atlandı (akış alınamadı): " + track.title)
            return None
        url, headers, duration = info
        return PcmStream(input_cmd(url, headers), duration=duration, name=track.title)

    # ----- akış çözme ------------------------------------------------
    def _prefetch(self, track):
        try:
            self._get_stream(track, retry=False)
        except Exception:
            log.debug("ön yükleme başarısız: %s", track.id)

    def _get_stream(self, track, retry=True):
        """(url, headers, süre) döndürür; çözülemezse None."""
        with self._lock:
            cached = self._streams.get(track.id)
        if cached and time.time() - cached[3] < STREAM_MAX_AGE:
            return cached[0], cached[1], cached[2]

        attempts = 0
        while not self._stop.is_set():
            try:
                url, headers, duration = resolve_stream(track.id)
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
                self._streams[track.id] = (url, headers, duration, time.time())
                if len(self._streams) > STREAM_CACHE_MAX:
                    oldest = min(self._streams, key=lambda k: self._streams[k][3])
                    self._streams.pop(oldest, None)
            return url, headers, duration
        return None
