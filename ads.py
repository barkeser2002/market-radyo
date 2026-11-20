"""Anons (reklam) seçimi: ağırlıklı rastgele.

Kurallar:
- En son çalınan anonsun ağırlığı 0. En az 2 dosya varsa art arda aynı anons çalınmaz.
- Diğer anonsların ağırlığı, en son çalındığından beri geçen anons sayısıdır.
  Uzun süredir çalmayan anons daha yüksek olasılıkla seçilir.
- Hiç çalınmamış anonslar, sanki en eski zamanda çalınmış gibi en yüksek ağırlığı alır.
- Tek dosya varsa her zaman o çalınır.
"""

import random
from pathlib import Path

AUDIO_EXTENSIONS = (".mp3",)


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
