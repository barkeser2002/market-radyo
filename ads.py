"""Anons (reklam) seçimi: ağırlıklı rastgele.

Kurallar:
- En son çalınan anonsun ağırlığı 0. En az 2 dosya varsa art arda aynı anons çalınmaz.
- Diğer anonsların ağırlığı, en son çalındığından beri geçen anons sayısıdır.
  Uzun süredir çalmayan anons daha yüksek olasılıkla seçilir.
- Hiç çalınmamış anonslar, sanki en eski zamanda çalınmış gibi en yüksek ağırlığı alır.
- Tek dosya varsa her zaman o çalınır.
"""

import logging
import random
import subprocess
from pathlib import Path

from ffmpeg_util import ffmpeg_path, hidden_window_kwargs

log = logging.getLogger("radyo.ads")

AUDIO_EXTENSIONS = (".mp3",)
SKIP_NAMES = {"desktop.ini", "thumbs.db"}
TEST_TIMEOUT = 120
CONVERT_TIMEOUT = 600


def list_announcements(folder):
    """Klasördeki anons dosyalarının yollarını (sıralı) döndürür."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted(
        str(p) for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    )


class AnnouncementPicker:
    def __init__(self, files=(), rng=None):
        self.rng = rng or random.Random()
        self.files = []
        self._last_index = {}   # dosya -> en son çalındığı sıra numarası
        self._counter = 0       # şimdiye kadar çalınan toplam anons sayısı
        self._last_file = None  # en son çalınan dosya
        self.set_files(files)

    def set_files(self, files):
        """Dosya listesini günceller; bilinen dosyaların geçmişi korunur."""
        files = list(dict.fromkeys(files))
        n = len(files)
        self._last_index = {f: self._last_index.get(f, -n) for f in files}
        self.files = files
        if self._last_file not in self._last_index:
            self._last_file = None

    def weights(self):
        """Her dosyanın ham ağırlığı (sıra numarası farkına dayalı)."""
        if not self.files:
            return {}
        if len(self.files) == 1:
            return {self.files[0]: 1.0}
        out = {}
        for f in self.files:
            if f == self._last_file:
                out[f] = 0.0
            else:
                out[f] = float(self._counter - self._last_index[f])
        return out

    def probabilities(self):
        """[(dosya, yüzde, son_çalan_mı)] listesi. Yüzdeler toplamı 100 eder."""
        w = self.weights()
        total = sum(w.values())
        result = []
        for f in self.files:
            pct = (w[f] / total * 100.0) if total > 0 else 0.0
            result.append((f, pct, f == self._last_file))
        return result

    def pick(self):
        """Bir sonraki anons dosyasını seçer. Dosya yoksa None."""
        if not self.files:
            return None
        if len(self.files) == 1:
            chosen = self.files[0]
        else:
            w = self.weights()
            chosen = self.rng.choices(self.files, weights=[w[f] for f in self.files])[0]
        self._record(chosen)
        return chosen

    def _record(self, chosen):
        self._last_index[chosen] = self._counter
        self._counter += 1
        self._last_file = chosen


# ----- anons dönüştürme -------------------------------------------------
def _ffmpeg_ok(path):
    """ffmpeg dosyayı baştan sona hatasız çözebiliyorsa True."""
    try:
        r = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-v", "error", "-nostdin",
             "-i", str(path), "-f", "null", "-"],
            capture_output=True, timeout=TEST_TIMEOUT, **hidden_window_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and not r.stderr.strip()


def _free_mp3_name(src):
    """src için klasörde çakışmayan .mp3 adı (x.mp3, x_1.mp3, x_2.mp3 ...)."""
    base = src.with_suffix(".mp3")
    if not base.exists():
        return base
    n = 1
    while True:
        cand = src.parent / ("%s_%d.mp3" % (src.stem, n))
        if not cand.exists():
            return cand
        n += 1


def convert_to_mp3(src):
    """src'yi mp3'e çevirir. Yeni dosya doğrulanınca orijinali siler.

    Başarılı olursa yeni mp3 yolunu, başarısız olursa None döndürür (orijinal kalır).
    """
    src = Path(src)
    dst = _free_mp3_name(src)
    cmd = [ffmpeg_path(), "-hide_banner", "-v", "error", "-nostdin", "-n",
           "-i", str(src), "-map", "0:a:0", "-vn",
           "-codec:a", "libmp3lame", "-b:a", "192k", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=CONVERT_TIMEOUT,
                           **hidden_window_kwargs())
        ok = (r.returncode == 0 and dst.is_file() and dst.stat().st_size > 0
              and _ffmpeg_ok(dst))
        err = r.stderr.decode("utf-8", "replace").strip()[-300:]
    except (OSError, subprocess.TimeoutExpired) as e:
        ok, err = False, str(e)
    if not ok:
        log.warning("anons dönüştürülemedi, orijinal korunuyor: %s (%s)", src.name, err)
        if dst.exists():
            try:
                dst.unlink()
            except OSError:
                pass
        return None
    try:
        if src.resolve() != dst.resolve():
            src.unlink()
    except OSError as e:
        log.warning("orijinal anons silinemedi (%s): %s", src.name, e)
    log.info("anons mp3'e çevrildi: %s -> %s", src.name, dst.name)
    return dst


def prepare_announcements(folder):
    """Klasördeki her dosyayı kontrol eder; mp3 değilse ya da okunamıyorsa mp3'e çevirir.

    Dönüş: kullanılabilir mp3 dosyalarının sıralı listesi (list_announcements gibi).
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []
    try:
        ffmpeg_path()
    except FileNotFoundError as e:
        log.warning("anons kontrolü atlandı: %s", e)
        return list_announcements(folder)
    usable = []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name.lower() in SKIP_NAMES or p.name.startswith("."):
            continue
        if p.suffix.lower() == ".mp3" and _ffmpeg_ok(p):
            usable.append(str(p))
            continue
        new = convert_to_mp3(p)
        if new is not None:
            usable.append(str(new))
    return sorted(usable)
