# Market Radyo

Markette sürekli çalan radyo programı. YouTube playlist'lerindeki şarkıları
**indirmeden** stream eder. Araya `anonslar/` klasöründeki `.mp3` anonsları
ağırlıklı rastgele olarak karıştırır.

## Gereksinimler

- Windows, Python 3.10+ (64-bit)
- **64-bit VLC Media Player** (zorunlu). Kurulu değilse program açılır ama başlatılamaz.
  VLC kurulu olduğu sürece `python-vlc` onu otomatik bulur. Varsayılan konum:
  `C:\Program Files\VideoLAN\VLC`
- İnternet bağlantısı. YouTube cookie'si **gerekmez**.

## Kurulum ve çalıştırma

1. `calistir.bat` dosyasına çift tıklayın.
   Bağımlılıkları (`yt-dlp`, `python-vlc`, `Pillow`, `requests`) sessizce kurar ve programı açar.
2. Elle çalıştırmak için: `python -m pip install -r requirements.txt` ardından `python main.py`.

## Ekranlar

**Radyo sekmesi**
- Şu an çalan başlık ve kapak (YouTube küçük resmi)
- Duraklat / Devam Et, Sonraki
- Ses kaydırıcısı
- Anons aralığı: her N şarkıda bir anons (varsayılan 4, 1-20 arası)
- Anonslar açık/kapalı kutusu
- Her anonsun o anki çalma olasılığı yüzdesi (son çalan anons %0)

**Playlistler sekmesi**
- Playlist listesi: küçük kapak, başlık, şarkı sayısı, tür, açık/kapalı kutusu
  (kutuya tıklayarak açıp kapatılır)
- URL yapıştırıp **Ekle**: başlık, kapak ve şarkı sayısı yt-dlp ile çekilir ve kaydedilir
- **Tür değiştir**, **Sil** (silinen playlist güncellemeyle geri gelmez), **Şimdi yenile**

## Nasıl çalışır

- **Şarkı listesi:** Açılışta ve sonrasında 24 saatte bir (arka planda) her playlist
  `yt-dlp` ile (flat, cookie yok) okunur. Sonuç `data/cache/tracks.json` dosyasına yazılır.
- **Karıştırma:** Açık playlist'lerin tüm şarkılarından rastgele seçilir.
  Son 200 çalınan şarkı tekrar edilmez.
- **Çalma:** Şarkının ses akış adresi `yt-dlp` ile çözülür, `python-vlc` ile çalınır.
  Bir sonraki şarkının adresi önceden çözülür, böylece geçişler kısa olur.
  Şarkı hata verirse atlanır. İnternet koparsa bağlantı gelene kadar bekleyip şarkıyı tekrar dener.
- **Anons seçimi (`ads.py`):**
  - En son çalan anonsun ağırlığı 0. 2 veya daha fazla dosya varsa art arda aynısı çalmaz.
  - Diğer anonsların ağırlığı, en son çalınmasından bu yana geçen anons sayısıdır.
    Uzun süredir çalmayan anons daha yüksek olasılıkla seçilir.
  - Tek dosya varsa her zaman o çalar.

## Güncelleme nasıl yayınlanır

1. Kodda değişikliği yapın ve test edin.
2. `version.txt` içindeki sürümü artırın (ör. `1.0.0` -> `1.0.1`).
3. Commit + push edin:

```
git add -A
git commit -m "Açıklama"
git push
```

Markettekiler bir sonraki açılışta sürümü görür ve yeni dosyaları otomatik indirir.
`config.json` ve `data/` güncellemede değişmez.

## Otomatik güncelleme

- `config.json` içindeki `update_repo` (varsayılan `OWNER/market-radyo`) ve `update_branch` (`main`)
  kullanılır. Gerçek GitHub deposunu yazmadan güncelleme kontrolü yapılmaz.
- Açılışta GitHub'dan `version.txt` okunur. Yeni sürüm varsa `main.py`, `player.py`,
  `ads.py`, `playlists.py`, `updater.py`, `gui.py`, `version.txt`, `requirements.txt`,
  `calistir.bat` indirilip kurulur. Eski dosyalar `data/update_backup/` içine yedeklenir.
  Program yeniden başlatılır.
- Uzaktaki `data/playlists.json` da her açılışta okunur. Yeni playlist'ler eklenir,
  sizin eklediklerinize ve açık/kapalı ayarlarınıza dokunulmaz.
- `config.json` ve `data/` güncellemede üzerine yazılmaz.
- Yeni bağımlılık eklenen bir sürümde `calistir.bat` kullanın. Bat dosyası her çalıştığında
  `requirements.txt` kurar.

## Klasör yapısı

```
main.py          giriş noktası, güncelleme kontrolü
gui.py           tkinter arayüzü
player.py        yt-dlp akış çözme, python-vlc oynatma, anons/şarkı sırası
playlists.py     playlist kütüphanesi, şarkı önbelleği
ads.py           anons seçimi (ağırlıklı rastgele)
updater.py       GitHub üzerinden güncelleme
config.json      ayarlar (ses, anons aralığı, güncelleme deposu)
version.txt      sürüm numarası
requirements.txt bağımlılıklar
calistir.bat     kur ve çalıştır
anonslar/        çalınacak anons .mp3 dosyaları
data/
  playlists.json   playlist listesi
  parts/*.json     ek playlist listeleri (aynı biçim, açılışta birleşir)
  dismissed.json   silinen playlist id'leri
  cache/           şarkı listesi önbelleği ve kapak resimleri (otomatik oluşur)
  radyo.log        günlük
tests/test_ads.py  anons seçimi testleri
```

## Playlist kaydı biçimi

`data/playlists.json` içinde:

```json
[
  {
    "id": "PLxxxxxxxxxxxxxxxx",
    "url": "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx",
    "title": "Yabancı Pop",
    "thumbnail": "https://i.ytimg.com/...",
    "count": 250,
    "genre": "pop",
    "enabled": true
  }
]
```

## Testler

```
python -m unittest discover -s tests -v
```

## Sınırlar

- Akış adresleri birkaç saat geçerlidir. Program 90 dakikadan eski adresleri yeniler.
- VLC ile iki şarkı arasında tam kesintisiz (gapless) geçiş garanti değildir. Ön yükleme boşluğu azaltır.
- YouTube değişiklikleri veya bölge kısıtları bazı şarkıların çalmamasına yol açabilir. Bu şarkılar atlanır.
