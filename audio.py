"""PCM akışı ve yazılımsal karıştırıcı (equal-power crossfade).

- PcmStream: bir ffmpeg süreci (ya da bellekteki PCM) çıktısını float32 stereo,
  44.1 kHz olarak tamponlar. Tampon CAP_SEC saniyeyi aşınca okuma durur (geri basınç).
  start: kaynağın kaçıncı saniyeden başladığı (ffmpeg -ss ile açılmışsa); konum buna göre hesaplanır.
- Mixer: tek bir ses çıkışını (sounddevice) yönetir. İki kanal vardır:
  * müzik: en fazla iki akış, equal-power crossfade (eski cos, yeni sin kazancı);
  * anons: ayrı bir akış. İki anons modu vardır:
    - "duck": anons başlayınca müzik duck_level kazancına yumuşakça iner (DUCK_DOWN_SEC),
      anons tam sesle üstüne bindirilir; anons bitince müzik DUCK_UP_SEC içinde eski seviyesine döner.
    - "pause": müzik fade_sec içinde kısılıp DURAKLATILIR (akış kapatılmaz, 'held' olarak tutulur);
      anons equal-power ile fade_sec içinde tam sesle girer. Anons bitince müzik, kaldığı
      kareden equal-power ile fade_sec içinde geri döner. Duraklatılmış akış bozulmuşsa
      (resumable() False) Mixer.resume_wanted() True olur; motor aynı akışı kaldığı konumdan
      yeniden açıp resume_held() ile verir.
  Toplam yumuşak limitlenir.
"""

import logging
import math
import subprocess
import threading
import time
from collections import deque

import numpy as np

from ffmpeg_util import hidden_window_kwargs

log = logging.getLogger("radyo.audio")

SR = 44100
CH = 2
FRAME_BYTES = CH * 4      # float32 stereo kare = 8 bayt
CAP_SEC = 12.0            # bir akıştan en fazla bu kadar saniye tamponlanır
CHUNK_BYTES = 32768       # ffmpeg'den tek seferde okunan bayt (8'in katı)
DUCK_DEFAULT = 0.15       # anons sırasında müzik kazancı
DUCK_DOWN_SEC = 0.5       # müziğin kısılma süresi
DUCK_UP_SEC = 1.0         # müziğin eski sesine dönme süresi
LIMIT_KNEE = 0.9          # bu genliğin üstünde yumuşak sıkıştırma başlar
RESUME_TAIL_SEC = 2.0     # duraklatılmış akış bu kadar saniyeden fazla eksikse "yarım" sayılır


def equal_power_gains(pos, length, n):
    """Geçişin pos. karesinden itibaren n kare için (eski, yeni) kazançları.

    eski = cos(r * pi/2), yeni = sin(r * pi/2), r = 0..1. Her an eski^2 + yeni^2 = 1.
    """
    r = np.clip((pos + np.arange(n)) / float(length), 0.0, 1.0)[:, None]
    return np.cos(r * math.pi / 2.0), np.sin(r * math.pi / 2.0)


def eqp_gains(pos, length, n, g_from, g_to):
    """Tek kanal için eşit güçlü kazanç eğrisi: g_from -> g_to, length kare boyunca.

    Düşüşte cos, yükselişte sin şekli kullanılır (1 -> 0 cos, 0 -> 1 sin).
    length <= 0 ise doğrudan g_to döner.
    """
    if length <= 0:
        return np.full(n, float(g_to))
    r = np.clip((pos + np.arange(n)) / float(length), 0.0, 1.0)
    if g_to < g_from:
        return g_to + (g_from - g_to) * np.cos(r * math.pi / 2.0)
    return g_from + (g_to - g_from) * np.sin(r * math.pi / 2.0)


def duck_gains(pos, length, n, g_from, g_to):
    """Duck kazanç eğrisi: pos. karesinden n kare için müzik kazancı.

    length kare boyunca g_from'dan g_to'ya kosinüs yumuşatmalı (S biçimli) geçer;
    length <= 0 ise doğrudan g_to döner.
    """
    if length <= 0:
        return np.full(n, float(g_to))
    r = np.clip((pos + np.arange(n)) / float(length), 0.0, 1.0)
    ease = 0.5 - 0.5 * np.cos(r * math.pi)
    return g_from + (g_to - g_from) * ease


def soft_limit(x, knee=LIMIT_KNEE):
    """Knee üstündeki genliği tanh ile sıkıştırır; knee altı değişmez, çıkış 1.0'ı aşmaz."""
    a = np.abs(x)
    over = a > knee
    if not np.any(over):
        return x
    room = 1.0 - knee
    y = knee + room * np.tanh((a - knee) / room)
    return np.where(over, np.sign(x) * y, x)


class PcmStream:
    """Bir ses kaynağının tamponlu PCM okuyucusu.

    cmd verilirse ffmpeg süreci başlatılır; frames verilirse bellekteki diziyi
    (n, 2) float32 olarak kullanır (test ve sentetik kaynaklar için).
    duration (saniye) bilinirse kalan süre tahmini buna göre yapılır.
    start (saniye): ffmpeg -ss ile bu konumdan açıldıysa verilir; position_sec() mutlak konumdur.
    """

    def __init__(self, cmd=None, duration=None, name="", frames=None, start=0.0):
        self.name = name
        self.duration = duration
        self.start = float(start or 0.0)
        self.cap = int(CAP_SEC * SR)
        self.cv = threading.Condition()
        self._chunks = deque()
        self._buffered = 0        # tampondaki kare sayısı
        self.consumed = 0         # çalınmış (mixer'ın çektiği) kare sayısı
        self.eof = False          # kaynak bitti (tampondakiler hâlâ çalınabilir)
        self.error = False        # ffmpeg hata koduyla çıktı
        self.closed = False
        self.last_data = time.time()
        self.proc = None
        self.stderr_tail = deque(maxlen=20)

        if frames is not None:
            arr = np.asarray(frames, dtype=np.float32).reshape(-1, CH).copy()
            self._chunks.append(arr)
            self._buffered = len(arr)
            self.eof = True
            return

        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, **hidden_window_kwargs())
        threading.Thread(target=self._read, name="pcm-oku", daemon=True).start()
        threading.Thread(target=self._drain_err, name="pcm-hata", daemon=True).start()

    # ----- arka plan iş parçacıkları ----------------------------------
    def _drain_err(self):
        try:
            for line in self.proc.stderr:
                self.stderr_tail.append(line.decode("utf-8", "replace").strip())
        except Exception:
            pass
        finally:
            try:
                self.proc.stderr.close()
            except Exception:
                pass

    def _read(self):
        carry = b""
        try:
            while True:
                with self.cv:
                    while not self.closed and self._buffered >= self.cap:
                        self.cv.wait(0.5)
                    if self.closed:
                        break
                data = self.proc.stdout.read(CHUNK_BYTES)
                if not data:
                    break
                data = carry + data
                n = len(data) - len(data) % FRAME_BYTES
                carry = data[n:]
                if n == 0:
                    continue
                arr = np.frombuffer(data[:n], dtype="<f4").reshape(-1, CH).copy()
                with self.cv:
                    if self.closed:
                        break
                    self._chunks.append(arr)
                    self._buffered += len(arr)
                    self.last_data = time.time()
                    self.cv.notify_all()
        except Exception as e:
            log.warning("akış okuma hatası (%s): %s", self.name, e)
        finally:
            rc = None
            try:
                rc = self.proc.wait(timeout=10)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            try:
                self.proc.stdout.close()
            except Exception:
                pass
            with self.cv:
                if rc not in (0, None) and not self.closed:
                    self.error = True
                    log.warning("ffmpeg hata kodu %s (%s): %s", rc, self.name,
                                " | ".join(self.stderr_tail)[-400:])
                self.eof = True
                self.cv.notify_all()

    # ----- tüketici arayüzü -------------------------------------------
    def pull(self, n):
        """En fazla n kare çeker. (dizi (n,2), gerçekten alınan kare sayısı) döner.

        Eksik kareler sıfırla doldurulur (tampon boşsa sessizlik).
        """
        out = np.zeros((n, CH), dtype=np.float32)
        got = 0
        with self.cv:
            while got < n and self._chunks:
                head = self._chunks[0]
                take = min(len(head), n - got)
                out[got:got + take] = head[:take]
                if take == len(head):
                    self._chunks.popleft()
                else:
                    self._chunks[0] = head[take:]
                got += take
            if got:
                self._buffered -= got
                self.consumed += got
                self.cv.notify_all()
        return out, got

    def position_sec(self):
        """Kaynaktaki mutlak konum (saniye): başlangıç ofseti + çalınmış kareler."""
        with self.cv:
            return self.start + self.consumed / float(SR)

    def remaining_frames(self):
        """Kalan kare tahmini. Kaynak bittiyse kesin değer; süre biliniyorsa tahmin; yoksa sonsuz."""
        with self.cv:
            if self.eof:
                return float(self._buffered)
            if self.duration is not None:
                left = (self.duration - self.start) * SR - self.consumed
                return float(max(self._buffered, left))
            return float("inf")

    def buffered_frames(self):
        with self.cv:
            return self._buffered

    def is_finished(self):
        with self.cv:
            return self.eof and self._buffered == 0

    def is_stalled(self, timeout):
        """Tampon boş, kaynak bitmemiş ve uzun süredir veri gelmiyorsa True."""
        with self.cv:
            return (not self.eof and self._buffered == 0
                    and time.time() - self.last_data > timeout)

    def resumable(self):
        """Duraklatılmış bir akış kaldığı yerden devam edebilir mi?

        Hata verdiyse ya da kaynak süresinin (duration) büyük kısmı okunmadan bittiyse False.
        Süre bilinmiyorsa, hata yoksa True kabul edilir.
        """
        with self.cv:
            if self.closed or self.error:
                return False
            if self.proc is not None:
                # Tampon doluyken okuma iş parçacığı pipe'ı okumaz; süreç çıkışını doğrudan sorarız.
                rc = self.proc.poll()
                if rc not in (None, 0):
                    self.error = True
                    return False
            if self.eof and self.duration is not None:
                end = self.start + (self.consumed + self._buffered) / float(SR)
                if end < self.duration - RESUME_TAIL_SEC:
                    return False
            return True

    def close(self):
        with self.cv:
            if self.closed:
                return
            self.closed = True
            self._chunks.clear()
            self._buffered = 0
            self.cv.notify_all()
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.kill()
            except OSError:
                pass


class Mixer:
    """Tek ses çıkışı. a: çalan akış, b: geçişte gelen akış.

    b None iken fade_len > 0 ise a yalnızca solar (fade-out) ve bitince kapanır.
    a None iken b gelirse yalnızca yükselir (fade-in).

    Duraklatma modu (play_announcement(mode="pause")): müzik fade_sec içinde kısılır ve
    _a 'held' olarak ayrılır (kapatılmaz). hold_state(): None | "fading" | "held".
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._a = None
        self._b = None
        self._fade_len = 0
        self._fade_pos = 0
        self._ann = None           # anons kanalı (PcmStream) ya da None
        self._mode = "duck"        # aktif anonsun modu: "duck" | "pause"
        self._ann_pos = 0          # anons giriş geçişi (pause modunda) konumu
        self._ann_len = 0          # anons giriş geçişi uzunluğu (kare); 0 ise doğrudan tam ses
        self._hold = None          # None | "fading" (müzik kısılıyor) | "held" (müzik ayrıldı)
        self._held = None          # duraklatılmış müzik akışı (kapatılmaz)
        self._resume_len = 0       # duraklatmadan dönüş geçişi (kare)
        self._resume_wanted = False  # held akış bozuk: motor aynı konumdan yeniden açmalı
        self.duck_level = DUCK_DEFAULT
        self._duck_from = 1.0      # duck ramp: başlangıç kazancı
        self._duck_to = 1.0        # duck ramp: hedef kazanç
        self._duck_pos = 0         # ramp içindeki kare konumu
        self._duck_len = 0         # ramp uzunluğu (kare); 0 ise hedefte sabit
        self._duck_curve = "ease"  # "ease" (duck_gains) ya da "eqp" (eqp_gains)
        self.volume = 1.0
        self.paused = False
        self._out = None

    # ----- aygıt ------------------------------------------------------
    def open(self):
        import sounddevice as sd  # ses aygıtı yoksa burada hata verir
        self._out = sd.OutputStream(samplerate=SR, channels=CH, dtype="float32",
                                    callback=self._callback)
        self._out.start()

    def close(self):
        out, self._out = self._out, None
        if out is not None:
            try:
                out.stop()
                out.close()
            except Exception:
                log.exception("ses çıkışı kapatılamadı")
        with self._lock:
            streams = [self._a, self._b, self._ann, self._held]
            self._a = self._b = self._ann = self._held = None
            self._fade_len = self._fade_pos = 0
            self._duck_len = 0
            self._hold = None
        for s in streams:
            if s is not None:
                s.close()

    def _callback(self, outdata, frames, time_info, status):
        try:
            outdata[:] = self.render(frames)
        except Exception:
            log.exception("ses geri çağırma hatası")
            outdata.fill(0)

    # ----- durum ------------------------------------------------------
    def set_volume(self, v):
        self.volume = max(0.0, min(1.0, float(v)))

    def current(self):
        with self._lock:
            return self._a

    def incoming(self):
        with self._lock:
            return self._b

    def fading(self):
        with self._lock:
            return self._fade_len > 0

    def announcement_active(self):
        with self._lock:
            return self._ann is not None

    def hold_state(self):
        with self._lock:
            return self._hold

    def held_stream(self):
        with self._lock:
            return self._held

    def resume_wanted(self):
        with self._lock:
            return self._resume_wanted

    def set_duck_level(self, v):
        """Anons (duck modunda) sırasında müziğin kazancı (0-1). Anons çalıyorsa hemen uygulanır."""
        with self._lock:
            self.duck_level = max(0.0, min(1.0, float(v)))
            if self._ann is not None and self._mode == "duck":
                self._duck_retarget(self.duck_level, DUCK_DOWN_SEC)

    # ----- anons kanalı -----------------------------------------------
    def play_announcement(self, stream, mode="duck", fade_sec=0.0):
        """Anonsu ayrı kanalda başlatır.

        duck: müzik duck_level'a yumuşakça kısılır, şarkı çalmaya devam eder.
        pause: müzik fade_sec içinde kısılıp ayrılır; anons equal-power ile tam sesle girer.
               Müzik yoksa (ya da bir geçiş sürüyorsa) yalnızca anons çalar.
        """
        L = int(round(max(0.0, float(fade_sec)) * SR))
        with self._lock:
            old, self._ann = self._ann, stream
            self._mode = "pause" if mode == "pause" else "duck"
            if self._mode == "pause":
                self._ann_pos = 0
                self._ann_len = L
                self._resume_len = L
                if self._a is not None and self._hold is None and self._fade_len <= 0:
                    self._hold = "fading"
                    self._duck_retarget(0.0, max(0.0, float(fade_sec)), curve="eqp")
            else:
                self._ann_len = 0
                self._duck_retarget(self.duck_level, DUCK_DOWN_SEC)
        if old is not None:
            old.close()

    def _duck_now(self):
        if self._duck_len <= 0:
            return self._duck_to
        return float(self._ramp(self._duck_pos, self._duck_len, 1,
                                self._duck_from, self._duck_to, self._duck_curve)[0])

    def _duck_retarget(self, target, sec, curve="ease"):
        """Mevcut kazançtan hedefe sec saniyede yeni bir ramp başlatır (kilit altında çağrılır)."""
        self._duck_from = self._duck_now()
        self._duck_curve = curve
        self._duck_to = float(target)
        self._duck_pos = 0
        self._duck_len = int(round(max(0.0, float(sec)) * SR))

    @staticmethod
    def _ramp(pos, length, n, g_from, g_to, curve):
        if curve == "eqp":
            return eqp_gains(pos, length, n, g_from, g_to)
        return duck_gains(pos, length, n, g_from, g_to)

    def _detach_held(self):
        """Müzik tamamen kısıldı: akışı kapatmadan kanaldan ayırır (kilit altında)."""
        if self._a is not None and self._b is None:
            self._held = self._a
            self._a = None
            self._hold = "held"
        else:
            self._hold = None

    def _attach_resume(self, stream):
        """Duraklatılmış (ya da yeniden açılmış) akışı equal-power ile geri bağlar (kilit altında)."""
        self._hold = None
        self._resume_wanted = False
        self._duck_curve = "ease"
        self._duck_from = self._duck_to = 1.0
        self._duck_len = self._duck_pos = 0
        if self._resume_len > 0:
            self._b = stream
            self._fade_len = self._resume_len
            self._fade_pos = 0
        else:
            self._a = stream

    def _announcement_done(self):
        """Anons bitti (kilit altında). Müziğin durumuna göre geri döner ya da kısık kalır."""
        if self._hold == "held" and self._held is not None:
            if self._held.resumable():
                held, self._held = self._held, None
                self._attach_resume(held)
            else:
                self._resume_wanted = True  # motor aynı konumdan yeniden açacak
        elif self._hold == "fading":
            # müzik tam kısılmadan anons bitti: duraklatma iptal, şarkı normal sesle devam eder
            self._hold = None
            self._duck_retarget(1.0, DUCK_UP_SEC)
        else:
            self._duck_retarget(1.0, DUCK_UP_SEC)

    def resume_held(self, stream):
        """Bozulan duraklatılmış akışın yerine, kaldığı konumdan açılmış yeni akışı bağlar.

        True döner ve akışın sahipliği mixer'a geçer; bekleyen duraklatma yoksa False
        (çağıran akışı kapatmalıdır).
        """
        old = None
        with self._lock:
            ok = self._hold == "held" and self._held is not None and stream is not None
            if ok:
                old, self._held = self._held, None
                self._attach_resume(stream)
        if old is not None:
            old.close()
        return ok

    def drop_hold(self):
        """Duraklatmayı iptal eder: ayrılmış ya da kısılan şarkı kapatılır. Anons etkilenmez."""
        to_close = []
        with self._lock:
            if self._hold == "fading":
                if self._a is not None:
                    to_close.append(self._a)
                self._a = None
                self._duck_from = self._duck_to = 1.0
                self._duck_len = self._duck_pos = 0
                self._duck_curve = "ease"
            elif self._hold == "held" and self._held is not None:
                to_close.append(self._held)
                self._held = None
            self._hold = None
            self._resume_wanted = False
        for s in to_close:
            s.close()

    # ----- geçişler ---------------------------------------------------
    def transition(self, new, fade_sec=0.0):
        """new akışına fade_sec saniyelik equal-power geçiş başlatır.

        new None ise mevcut ses fade_sec içinde kapanır. fade_sec 0 ise anında geçilir.
        Devam eden bir geçiş varsa önce hemen tamamlanır.
        """
        with self._lock:
            if self._fade_len > 0:
                self._finish_fade()
            L = int(round(max(0.0, float(fade_sec)) * SR))
            if new is None and self._a is None:
                return
            if L <= 0:
                old = self._a
                self._a = new
                self._b = None
                if old is not None and old is not new:
                    old.close()
                return
            self._b = new
            self._fade_len = L
            self._fade_pos = 0

    def _finish_fade(self):
        old = self._a
        self._a = self._b
        self._b = None
        self._fade_len = 0
        self._fade_pos = 0
        if old is not None and old is not self._a:
            old.close()

    # ----- ses üretimi ------------------------------------------------
    def _render_music(self, frames):
        """Müzik kanalının frames karelik karışımı (float64). Kilit altında çağrılır."""
        if self._a is None and self._b is None:
            return np.zeros((frames, CH), dtype=np.float64)
        if self._fade_len <= 0:
            return self._a.pull(frames)[0].astype(np.float64)
        a_s = self._a.pull(frames)[0] if self._a is not None else None
        b_s = self._b.pull(frames)[0] if self._b is not None else None
        ga, gb = equal_power_gains(self._fade_pos, self._fade_len, frames)
        out = np.zeros((frames, CH), dtype=np.float64)
        if a_s is not None:
            out += a_s * ga
        if b_s is not None:
            out += b_s * gb
        self._fade_pos += frames
        if self._fade_pos >= self._fade_len:
            self._finish_fade()
        return out

    def render(self, frames):
        """frames kare karışık ses üretir (float32, (frames, 2)).

        Müzik (crossfade + duck kazancı) ile anons kanalı toplanır; anons varken
        toplam soft_limit'ten geçer. Ana ses (volume) en sonda hem müziğe hem anonsa uygulanır.
        """
        with self._lock:
            if self.paused or (self._a is None and self._b is None and self._ann is None):
                return np.zeros((frames, CH), dtype=np.float32)
            out = self._render_music(frames)
            if self._duck_len > 0 or self._duck_to != 1.0:
                g = self._ramp(self._duck_pos, self._duck_len, frames,
                               self._duck_from, self._duck_to, self._duck_curve)
                out = out * g[:, None]
                self._duck_pos += frames
                if self._duck_pos >= self._duck_len:
                    self._duck_from = self._duck_to
                    self._duck_len = 0
                    self._duck_pos = 0
            if self._hold == "fading" and self._duck_len <= 0:
                self._detach_held()
            if self._ann is not None:
                ann = self._ann.pull(frames)[0].astype(np.float64)
                if self._ann_len > 0:
                    ann = ann * eqp_gains(self._ann_pos, self._ann_len, frames, 0.0, 1.0)[:, None]
                    self._ann_pos += frames
                out = soft_limit(out + ann)
                if self._ann.error or self._ann.is_finished():
                    old, self._ann = self._ann, None
                    self._announcement_done()
                    old.close()
            if self.volume != 1.0:
                out = out * self.volume
        return np.asarray(out, dtype=np.float32)
