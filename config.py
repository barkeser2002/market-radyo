"""Ayar dosyası (config.json) okuma ve tek bir ayarı kaydetme."""

import json
import os
from pathlib import Path

DEFAULT_CONFIG = {
    "update_repo": "OWNER/market-radyo",
    "update_branch": "main",
    "volume": 70,
    "announce_every_min": 10,   # anons aralığı (dakika, 1-60); eski "announce_every" anahtarı yok sayılır
    "announce_enabled": True,
    "announce_mode": "duck",    # "duck": müzik altta çalar; "pause": şarkı anons süresince duraklar
    "announce_fade_sec": 2,     # pause modunda şarkının kısılma / geri gelme süresi (0-3 sn)
    "duck_level": 0.15,         # anons sırasında müzik kazancı (0-1)
    "cache_hours": 24,
    "recent_window": 200,
    "crossfade_sec": 4,
}


def config_path(base):
    return Path(base) / "config.json"


def load_config(base):
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(config_path(base).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save_setting(base, key, value):
    """config.json içinde yalnızca key'i günceller; diğer ayarlar olduğu gibi kalır.

    Dosya var ama okunamıyorsa (bozuk JSON) hata verir; kullanıcının ayarları silinmez.
    """
    path = config_path(base)
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("config.json bir sözlük değil")
    else:
        data = {}
    data[key] = value
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
