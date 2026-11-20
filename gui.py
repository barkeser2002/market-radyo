"""Market Radyo arayüzü (customtkinter, koyu tema).

Kenar menüsü: Radyo, Playlistler, Ayarlar, Hakkında. Motor (player.RadioEngine),
kütüphane (playlists.Library), anons seçimi (ads) ve güncelleyici (updater) olduğu
gibi kullanılır; bu modül yalnızca arayüzdür.

İş parçacığı kuralı: arka plan iş parçacıkları arayüze dokunmaz. Sonuçlarını
self.post() ile kuyruğa koyarlar; ana iş parçacığındaki _pump() bunları uygular.
"""

import logging
import os
import queue
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import messagebox, simpledialog

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont, ImageOps

from ads import AnnouncementPicker, prepare_announcements
from config import save_setting
from istek import PLAYLIST_WATCH, parse_link, search_all, video_info
from player import RadioEngine, internet_ok
from playlists import THUMB_URL, download_image, fetch_playlist

log = logging.getLogger("radyo.gui")

APP_NAME = "Market Radyo"
DEVELOPER = "Barış Keser"
CONTACT_EMAIL = "bariskeser@bariskeser.com"
GITHUB_URL = "https://github.com/barkeser2002/market-radyo"
FEATURES = [
    "Kesintisiz radyo: şarkılar arasında sessizlik olmadan çalar",
    "3000-10000 şarkılık geniş havuz: playlist'lerdeki şarkılardan rastgele seçim",
    "Sadece ses akışı: videolar indirilmez, yalnızca ses akışı çözülür",
    "Crossfade: parçalar eşit güç eğrisiyle üst üste bindirilerek geçilir (0-10 sn)",
    "Dakika bazlı anonslar: her 1-60 dakikada bir (varsayılan 10); müzik çalarken süre sayılır, duraklatmada durur",
    "Anons modu 'müzik altta': süre dolunca şarkı kesilmez; müzik %15'e kısılır, anons üstüne bindirilir, bitince müzik geri gelir",
    "Anons modu 'şarkı dursun': şarkı geçişle kısılıp duraklar, anons tam sesle çalar, şarkı kaldığı saniyeden devam eder",
    "Geçiş süresi ayarlanabilir (varsayılan 2 sn, en fazla 3 sn); bağlantı koparsa şarkı kaldığı yerden yeniden açılır",
    "Anonsu şimdi çal: istediğin an anons çalınır; anons sırasında müzik seviyesi ayarlanabilir",
    "Akıllı anons rotasyonu: anonslar ağırlıklı rastgele seçilir, art arda aynısı çalmaz",
    "Otomatik format dönüştürme: mp3 olmayan anonslar açılışta mp3'e çevrilir",
    "Özel istek: YouTube'da şarkı ya da playlist ara, bağlantı yapıştır; istekler kuyrukta sırayla çalar",
    "Playlist ekleme: YouTube playlist URL'si ile ekleme, tür ve açık/kapalı yönetimi",
    "Otomatik güncelleme: derlenmiş programda yeni sürüm GitHub'dan indirilir ve bir sonraki parça geçişinde kurulur",
]

# ----- tasarım --------------------------------------------------------
BG = "#121212"
SIDEBAR = "#0B0B0B"
CARD = "#1C1C1C"
CARD_HOVER = "#262626"
SECOND = "#2A2A2A"        # ikincil düğme zemini
TRACK = "#2E2E2E"         # kaydırıcı ve ilerleme çubuğu zemini
TEXT = "#F2F2F2"
MUTED = "#9A9A9A"
ACCENT = "#FF7A1A"        # tek vurgu rengi (turuncu)
ACCENT_HOVER = "#FF9447"
ACCENT_TEXT = "#141414"
DANGER = "#E5484D"
FONT = "Segoe UI"
NAV = [
    ("radio", "♫", "Radyo"),          # ♫
    ("playlists", "☰", "Playlistler"),  # ☰
    ("settings", "⚙", "Ayarlar"),      # ⚙
    ("about", "ℹ", "Hakkında"),        # ℹ
]
ANN_ICON = "\U0001F4E2"                     # 📢

COVER_PX = 300
COVER_RADIUS = 22
THUMB_PX = 56
ROW_H = 80
TICK_MS = 250
NET_EVERY_MS = 60_000
THUMB_RETRY_SEC = 600     # başarısız kapak indirmesi bu süre dolmadan yeniden denenmez
THUMB_PARALLEL = 4        # aynı anda en fazla bu kadar kapak indirilir


def F(size, bold=False):
    return ctk.CTkFont(family=FONT, size=size, weight="bold" if bold else "normal")


def mmss(sec):
    sec = max(0, int(round(sec)))
    return "%d:%02d" % (sec // 60, sec % 60)


def _short(text, limit):
    text = str(text or "")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _glyph_font(size):
    for name in ("seguisym.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def rounded(im, size, radius):
    """Görseli size boyutuna ortalayarak kırpar, köşeleri yuvarlar (RGBA)."""
    fitted = ImageOps.fit(im.convert("RGB"), size).convert("RGBA")
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1),
                                           radius=radius, fill=255)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.paste(fitted, (0, 0), mask)
    return out


def placeholder(size, radius, glyph):
    """Kapak yokken gösterilen, turuncudan koyuya geçen yuvarlak kare."""
    w, h = size
    top, bot = (96, 48, 14), (24, 24, 24)
    im = Image.new("RGB", size, bot)
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    d.text((w / 2, h / 2), glyph, font=_glyph_font(int(min(w, h) * 0.42)),
           fill=(255, 255, 255), anchor="mm")
    return rounded(im, size, radius)


def ctk_image(pil, size):
    return ctk.CTkImage(light_image=pil, dark_image=pil, size=size)


def card(parent, title=None):
    c = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16)
    c.pack(fill="x", pady=(0, 14), padx=4)
    if title:
        ctk.CTkLabel(c, text=title, font=F(14, True), text_color=TEXT,
                     anchor="w").pack(fill="x", padx=20, pady=(16, 2))
    return c


def setting_slider(parent, label, lo, hi, steps, value, fmt, command):
    """Etiketli kaydırıcı satırı. (kaydırıcı, değer etiketi) döndürür."""
    row = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
    row.pack(fill="x", padx=20, pady=8)
    head = ctk.CTkFrame(row, fg_color="transparent", corner_radius=0)
    head.pack(fill="x")
    ctk.CTkLabel(head, text=label, font=F(13), text_color=TEXT, anchor="w").pack(side="left")
    val = ctk.CTkLabel(head, text=fmt(value), font=F(13, True), text_color=ACCENT, anchor="e")
    val.pack(side="right")
    sl = ctk.CTkSlider(row, from_=lo, to=hi, number_of_steps=steps, height=16,
                       progress_color=ACCENT, button_color=TEXT, button_hover_color="#FFFFFF",
                       fg_color=TRACK, command=command)
    sl.set(value)
    sl.pack(fill="x", pady=(8, 0))
    return sl, val


class VirtualList(ctk.CTkFrame):
    """Yalnızca görünen satırların widget'larını tutan sanal liste.

    283+ playlist kartını akıcı göstermek için satır widget'ları havuzdan
    yeniden kullanılır; her satır row.set(item) ile doldurulur.
    """

    def __init__(self, master, row_h, row_factory):
        super().__init__(master, fg_color="transparent", corner_radius=0)
        self.row_h = row_h
        self.row_factory = row_factory
        self.items = []
        self.offset = 0
        self.rows = []
        self.viewport = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self.viewport.pack(side="left", fill="both", expand=True)
        self.bar = ctk.CTkScrollbar(self, orientation="vertical", command=self._on_bar,
                                    button_color=SECOND, button_hover_color="#3A3A3A")
        self.bar.pack(side="right", fill="y", padx=(8, 0))
        self.viewport.bind("<Configure>", lambda e: self.reflow())
        # CustomTkinter widget'larinda bind_all yasak; ust duzey Tk penceresine baglanir
        tk.Misc.bind_all(self.winfo_toplevel(), "<MouseWheel>", self._on_wheel, add="+")

    def _viewport_h(self):
        return max(1, self.viewport.winfo_height())

    def _max_offset(self):
        return max(0, len(self.items) * self.row_h - self._viewport_h())

    def set_items(self, items):
        self.items = list(items)
        self.reflow()

    def scroll_by(self, dy):
        self.offset += dy
        self.reflow()

    def _on_bar(self, *args):
        if not args:
            return
        total = max(1, len(self.items) * self.row_h)
        if args[0] == "moveto":
            self.offset = int(float(args[1]) * total)
        elif args[0] == "scroll":
            self.offset += int(args[1]) * self.row_h
        self.reflow()

    def _on_wheel(self, e):
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except Exception:
            return
        if w is None:
            return
        vp = str(self.viewport)
        p = str(w)
        if p != vp and not p.startswith(vp + "."):
            return  # fare bu listenin dışında
        self.scroll_by((-1 if e.delta > 0 else 1) * self.row_h)

    def reflow(self):
        vh = self._viewport_h()
        self.offset = max(0, min(self.offset, self._max_offset()))
        need = vh // self.row_h + 3
        while len(self.rows) < need:
            self.rows.append(self.row_factory(self.viewport))
        first = self.offset // self.row_h
        for i, row in enumerate(self.rows):
            idx = first + i
            if i < need and idx < len(self.items):
                row.set(self.items[idx])
                row.place(x=0, y=idx * self.row_h - self.offset, relwidth=1.0)
            else:
                row.place_forget()
        total = len(self.items) * self.row_h
        if total <= 0:
            self.bar.set(0, 1)
        else:
            self.bar.set(self.offset / total, min(1.0, (self.offset + vh) / total))


class PlaylistRow(ctk.CTkFrame):
    """Playlist kartı: kapak, başlık, şarkı sayısı ve tür, açık/kapalı, Tür ve Sil düğmeleri."""

    def __init__(self, master, app):
        super().__init__(master, fg_color=CARD, corner_radius=14, height=ROW_H - 8)
        self.app = app
        self.pid = None
        self._syncing = False
        self.thumb = ctk.CTkLabel(self, text="", width=THUMB_PX, height=THUMB_PX)
        self.thumb.pack(side="left", padx=(12, 14))
        self.sw = ctk.CTkSwitch(self, text="", width=44, switch_width=44, switch_height=22,
                                progress_color=ACCENT, button_color=TEXT,
                                button_hover_color="#FFFFFF", fg_color=TRACK,
                                command=self._on_switch)
        self.sw.pack(side="right", padx=(6, 16))
        self.btn_del = ctk.CTkButton(self, text="Sil", width=60, height=30, corner_radius=15,
                                     fg_color=SECOND, hover_color=DANGER, text_color=TEXT,
                                     font=F(12), command=self._on_delete)
        self.btn_del.pack(side="right", padx=6)
        self.btn_genre = ctk.CTkButton(self, text="Tür", width=64, height=30, corner_radius=15,
                                       fg_color=SECOND, hover_color=CARD_HOVER, text_color=TEXT,
                                       font=F(12), command=self._on_genre)
        self.btn_genre.pack(side="right", padx=6)
        mid = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        mid.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.title = ctk.CTkLabel(mid, text="", font=F(14, True), text_color=TEXT,
                                  anchor="w", justify="left")
        self.title.pack(anchor="w", pady=(16, 0))
        self.meta = ctk.CTkLabel(mid, text="", font=F(12), text_color=MUTED, anchor="w")
        self.meta.pack(anchor="w")

    def set(self, p):
        self.pid = p["id"]
        self.title.configure(text=_short(p["title"], 60))
        self.meta.configure(text="%d şarkı   ·   %s" % (p["count"], p["genre"]))
        self._syncing = True
        try:
            if p["enabled"]:
                self.sw.select()
            else:
                self.sw.deselect()
        finally:
            self._syncing = False
        self.thumb.configure(image=self.app.thumb_image(p["id"], p.get("thumbnail", "")))

    def _on_switch(self):
        if not self._syncing and self.pid is not None:
            self.app.set_playlist_enabled(self.pid, bool(self.sw.get()))

    def _on_genre(self):
        if self.pid is not None:
            self.app.on_genre(self.pid)

    def _on_delete(self):
        if self.pid is not None:
            self.app.on_delete(self.pid)


class App:
    def __init__(self, root, base, cfg, lib, updater):
        self.root = root
        self.base = Path(base)
        self.cfg = cfg
        self.lib = lib
        self.updater = updater
        self.q = queue.Queue()
        self._version = updater.local_version()
        self._updating = False              # kurulum isteği zaten gönderildi mi
        self._save_jobs = {}                # ayar kaydı için bekleyen zamanlayıcılar (anahtar -> job)
        self._cover_id = None
        self._cover_key = None
        self._cover_img = None
        self._thumbs = {}                   # playlist id -> CTkImage
        self._thumb_busy = set()
        self._thumb_failed = {}             # playlist id -> başarısız denemenin zamanı
        self._thumb_sem = threading.Semaphore(THUMB_PARALLEL)
        self._track_pid = None              # şu an çalan şarkının playlist kimliği
        self._track_meta = ""               # "Playlist: ... · Tür: ..." satırı
        self._badge_shown = False
        self._last = {}                     # değişmeyen metinleri yeniden çizmemek için
        self._pages = {}
        self._nav = {}
        self._page = None
        self._ad_rows = []                  # (dosya, ad etiketi, çubuk, yüzde, etiket)
        self._res_token = 0                 # son arama kimliği (eski sonuçlar yok sayılır)
        self._res_labels = []               # arama satırlarındaki kapak etiketleri (kapak gelince güncellenir)
        self._pl_cache = {}                 # playlist id -> fetch_playlist sonucu
        self._pl_meta = {}                  # playlist id -> arama satırındaki şarkı sayısı etiketi
        self._q_ver = -1                    # çizilen istek kuyruğunun sürümü
        self._track_kind = "track"          # şu an çalan öğenin türü: track | request
        self._req_badge_shown = False
        self.announcer = AnnouncementPicker(prepare_announcements(self.base / "anonslar"))
        # ses çıkışı yoksa motor çökmez; rozet gösterilir ve motor aygıtı bekler
        self.engine = RadioEngine(cfg, lib, self.announcer, on_item_start=self._on_item_start)

        root.title(APP_NAME)
        root.geometry("1120x780")
        root.minsize(1000, 700)
        root.configure(fg_color=BG)
        self._build_menu()
        self._build_shell()
        self._build_radio(self._pages["radio"])
        self._build_playlists(self._pages["playlists"])
        self._build_settings(self._pages["settings"])
        self._build_about(self._pages["about"])
        self.engine.start()
        lib.start_background(on_change=lambda: self.post(self.refresh_playlists))
        self.updater.start_background(
            library=lib, on_result=lambda res: self.post(lambda: self._on_update_result(res)))
        self.refresh_playlists()
        self.select_page("radio")
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._pump)
        root.after(250, self._tick)
        root.after(800, self._net_check)

    # ----- iskelet ----------------------------------------------------
    def _build_menu(self):
        bar = tk.Menu(self.root)
        help_menu = tk.Menu(bar, tearoff=0)
        help_menu.add_command(label="Hakkında", command=lambda: self.select_page("about"))
        help_menu.add_command(label="Güncellemeleri denetle", command=self.on_check_updates)
        bar.add_cascade(label="Yardım", menu=help_menu)
        self.root.config(menu=bar)

    def _build_shell(self):
        root = self.root
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)

        side = ctk.CTkFrame(root, fg_color=SIDEBAR, corner_radius=0, width=92)
        side.grid(row=0, column=0, sticky="ns")
        ctk.CTkLabel(side, text="♪", font=F(26, True), text_color=ACCENT).pack(pady=(18, 14))
        for name, icon, caption in NAV:
            box = ctk.CTkFrame(side, fg_color="transparent", corner_radius=0)
            box.pack(pady=4, padx=10)
            btn = ctk.CTkButton(box, text=icon, width=64, height=50, corner_radius=14,
                                font=F(20), fg_color="transparent", hover_color=CARD_HOVER,
                                text_color=MUTED, command=lambda n=name: self.select_page(n))
            btn.pack()
            cap = ctk.CTkLabel(box, text=caption, font=F(10), text_color=MUTED)
            cap.pack()
            self._nav[name] = (btn, cap)

        self.content = ctk.CTkFrame(root, fg_color=BG, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        for name in ("radio", "playlists", "settings", "about"):
            self._pages[name] = ctk.CTkFrame(self.content, fg_color=BG, corner_radius=0)

        bar = ctk.CTkFrame(root, fg_color=SIDEBAR, corner_radius=0, height=30)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.sb_left = ctk.CTkLabel(bar, text="", font=F(11), text_color=MUTED)
        self.sb_left.pack(side="left", padx=14, pady=4)
        self.net_lbl = ctk.CTkLabel(bar, text="", font=F(11), text_color=MUTED)
        self.net_lbl.pack(side="right", padx=14, pady=4)
        self.sb_mid = ctk.CTkLabel(bar, text="", font=F(11), text_color=MUTED, anchor="w")
        self.sb_mid.pack(side="left", fill="x", expand=True, padx=8, pady=4)

    def select_page(self, name):
        if name == self._page:
            return
        if self._page is not None:
            self._pages[self._page].pack_forget()
        self._pages[name].pack(fill="both", expand=True, padx=28, pady=22)
        for n, (btn, cap) in self._nav.items():
            on = n == name
            btn.configure(fg_color=CARD if on else "transparent", text_color=ACCENT if on else MUTED)
            cap.configure(text_color=TEXT if on else MUTED)
        self._page = name

    # ----- Radyo sekmesi ----------------------------------------------
    def _build_radio(self, f):
        f.grid_columnconfigure(1, weight=1)
        f.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(f, fg_color="transparent", corner_radius=0)
        left.grid(row=0, column=0, sticky="ns", padx=(0, 30))
        self._ph_cover = ctk_image(placeholder((COVER_PX, COVER_PX), COVER_RADIUS, "♫"),
                                   (COVER_PX, COVER_PX))
        self.cover = ctk.CTkLabel(left, text="", width=COVER_PX, height=COVER_PX,
                                  image=self._ph_cover)
        self.cover.pack(anchor="n")
        self._build_queue(left)

        right = ctk.CTkFrame(f, fg_color="transparent", corner_radius=0)
        right.grid(row=0, column=1, sticky="nsew")
        right.bind("<Configure>", lambda e: self._wrap_radio(e.width))
        ctk.CTkLabel(right, text="ŞU AN ÇALIYOR", font=F(11, True), text_color=ACCENT,
                     anchor="w").pack(anchor="w", pady=(0, 2))
        self.title_lbl = ctk.CTkLabel(right, text="Hazırlanıyor...", font=F(28, True),
                                      text_color=TEXT, anchor="w", justify="left",
                                      wraplength=480)
        self.title_lbl.pack(anchor="w", fill="x")
        self.sub_lbl = ctk.CTkLabel(right, text="", font=F(14), text_color=MUTED,
                                    anchor="w", justify="left", wraplength=480)
        self.sub_lbl.pack(anchor="w", fill="x", pady=(4, 0))

        # rozetler: anons ve özel istek; yer her zaman ayrılmış olur (düzen kaymasın)
        slot = ctk.CTkFrame(right, fg_color="transparent", corner_radius=0, height=36)
        slot.pack(anchor="w", fill="x", pady=(6, 0))
        slot.pack_propagate(False)
        self.badge = ctk.CTkLabel(slot, text="", font=F(12, True), text_color=ACCENT_TEXT,
                                  fg_color=ACCENT, corner_radius=14, height=28)
        self.req_badge = ctk.CTkLabel(slot, text="⭐ Özel istek", font=F(12, True),
                                      text_color=ACCENT_TEXT, fg_color=ACCENT_HOVER,
                                      corner_radius=14, height=28)
        self._req_badge_shown = False
        self.audio_badge = ctk.CTkLabel(slot, text="🔇 Ses çıkışı bulunamadı, bekleniyor…",
                                        font=F(12, True), text_color=TEXT,
                                        fg_color=DANGER, corner_radius=14, height=28)
        self._audio_badge_shown = False

        prog = ctk.CTkFrame(right, fg_color="transparent", corner_radius=0)
        prog.pack(fill="x", pady=(6, 0))
        self.progress = ctk.CTkProgressBar(prog, height=8, corner_radius=4, progress_color=ACCENT,
                                           fg_color=TRACK)
        self.progress.set(0)
        self.progress.pack(fill="x")
        times = ctk.CTkFrame(prog, fg_color="transparent", corner_radius=0)
        times.pack(fill="x", pady=(4, 0))
        self.t_elapsed = ctk.CTkLabel(times, text="0:00", font=F(12), text_color=MUTED)
        self.t_elapsed.pack(side="left")
        self.t_left = ctk.CTkLabel(times, text="--:--", font=F(12), text_color=MUTED)
        self.t_left.pack(side="right")

        btns = ctk.CTkFrame(right, fg_color="transparent", corner_radius=0)
        btns.pack(anchor="w", pady=(8, 0))
        self.btn_toggle = self._round_button(btns, 0, "⏯", self.on_toggle, primary=True)
        self.cap_toggle = self._button_caption(btns, 0, "Duraklat")
        self._round_button(btns, 1, "⏭", self.on_next)
        self._button_caption(btns, 1, "Sonraki")
        self._round_button(btns, 2, ANN_ICON, self.on_announce_now)
        self._button_caption(btns, 2, "Anonsu şimdi çal", wrap=130)

        self.status_lbl = ctk.CTkLabel(right, text="", font=F(12), text_color=MUTED,
                                       anchor="w", justify="left", wraplength=480)
        self.status_lbl.pack(anchor="w", fill="x", pady=(8, 0))

        self._build_request(right)

        vol = ctk.CTkFrame(right, fg_color="transparent", corner_radius=0)
        vol.pack(fill="x", pady=(8, 0))
        ctk.CTkLabel(vol, text="Ses", font=F(12), text_color=MUTED, width=40,
                     anchor="w").pack(side="left")
        self.vol_val = ctk.CTkLabel(vol, text="%d%%" % int(self.cfg.get("volume", 70)),
                                    font=F(12, True), text_color=TEXT, width=48, anchor="e")
        self.vol_val.pack(side="right")
        self.vol = ctk.CTkSlider(vol, from_=0, to=100, number_of_steps=100, height=16,
                                 progress_color=ACCENT, button_color=TEXT,
                                 button_hover_color="#FFFFFF", fg_color=TRACK,
                                 command=self.on_volume)
        self.vol.set(int(self.cfg.get("volume", 70)))
        self.vol.pack(side="left", fill="x", expand=True, padx=12)

        self.next_lbl = ctk.CTkLabel(right, text="", font=F(13), text_color=TEXT, anchor="w")
        self.next_lbl.pack(anchor="w", pady=(6, 0))

    def _build_queue(self, parent):
        card_ = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16)
        card_.pack(fill="both", expand=True, pady=(14, 0))
        head = ctk.CTkFrame(card_, fg_color="transparent", corner_radius=0)
        head.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(head, text="İstek kuyruğu", font=F(13, True), text_color=TEXT,
                     anchor="w").pack(side="left")
        self.q_count = ctk.CTkLabel(head, text="", font=F(12), text_color=MUTED, anchor="e")
        self.q_count.pack(side="right")
        self.q_box = ctk.CTkScrollableFrame(card_, fg_color="transparent", corner_radius=0)
        self.q_box.pack(fill="both", expand=True, padx=4, pady=(6, 6))
        self.q_inner = ctk.CTkFrame(self.q_box, fg_color="transparent", corner_radius=0)
        self.q_inner.pack(fill="x")
        self._q_ver = -1

    def _build_request(self, parent):
        ctk.CTkLabel(parent, text="\U0001F3B5 Özel istek", font=F(13, True), text_color=TEXT,
                     anchor="w").pack(anchor="w", pady=(14, 0))
        row = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        row.pack(fill="x", pady=(6, 0))
        self.req_entry = ctk.CTkEntry(row, height=38, corner_radius=12, fg_color=CARD,
                                      border_color=SECOND, text_color=TEXT,
                                      placeholder_text="Şarkı, playlist adı ya da YouTube linki (Enter)",
                                      placeholder_text_color=MUTED, font=F(13))
        self.req_entry.pack(side="left", fill="x", expand=True)
        self.req_entry.bind("<Return>", lambda e: self.on_request_submit())
        ctk.CTkButton(row, text="Ara", width=84, height=38, corner_radius=12,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT,
                      font=F(13, True), command=self.on_request_submit).pack(side="left", padx=(8, 0))
        self.req_note = ctk.CTkLabel(parent, text="", font=F(12), text_color=MUTED,
                                     anchor="w", justify="left", wraplength=560)
        self.req_note.pack(anchor="w", fill="x", pady=(6, 0))
        self.res_box = ctk.CTkScrollableFrame(parent, height=190, corner_radius=14, fg_color=CARD)
        self.res_box.pack(fill="x", pady=(4, 0))
        self.res_inner = ctk.CTkFrame(self.res_box, fg_color="transparent", corner_radius=0)
        self.res_inner.pack(fill="x")
        self._res_labels = []
        self._show_placeholder_results()

    def _wrap_radio(self, width):
        wrap = max(200, width - 12)
        for lbl in (self.title_lbl, self.sub_lbl, self.status_lbl):
            lbl.configure(wraplength=wrap)

    def _round_button(self, parent, col, text, command, primary=False):
        box = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        box.grid(row=0, column=col, padx=(0 if col == 0 else 22, 0), sticky="n")
        if primary:
            fg, hover, tc = ACCENT, ACCENT_HOVER, ACCENT_TEXT
        else:
            fg, hover, tc = SECOND, CARD_HOVER, TEXT
        btn = ctk.CTkButton(box, text=text, width=84, height=84, corner_radius=42, font=F(28),
                            fg_color=fg, hover_color=hover, text_color=tc, command=command)
        btn.pack()
        return btn

    def _button_caption(self, parent, col, text, wrap=100):
        lbl = ctk.CTkLabel(parent, text=text, font=F(12), text_color=MUTED, wraplength=wrap,
                           justify="center")
        lbl.grid(row=1, column=col, padx=(0 if col == 0 else 22, 0), pady=(8, 0), sticky="n")
        return lbl

    # ----- Playlistler sekmesi ----------------------------------------
    def _build_playlists(self, f):
        top = ctk.CTkFrame(f, fg_color="transparent", corner_radius=0)
        top.pack(fill="x")
        self.url_entry = ctk.CTkEntry(top, height=38, corner_radius=12,
                                      fg_color=CARD, border_color=SECOND, text_color=TEXT,
                                      placeholder_text="YouTube playlist URL'si",
                                      placeholder_text_color=MUTED, font=F(13))
        self.url_entry.pack(side="left", fill="x", expand=True)
        self.url_entry.bind("<Return>", lambda e: self.on_add())
        self.add_btn = ctk.CTkButton(top, text="Ekle", width=96, height=38, corner_radius=12,
                                     fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                     text_color=ACCENT_TEXT, font=F(13, True), command=self.on_add)
        self.add_btn.pack(side="left", padx=(10, 0))

        bar = ctk.CTkFrame(f, fg_color="transparent", corner_radius=0)
        bar.pack(fill="x", pady=(12, 0))
        self.search_entry = ctk.CTkEntry(bar, height=34,
                                         corner_radius=12, fg_color=CARD, border_color=SECOND,
                                         text_color=TEXT, placeholder_text="Başlığa göre ara",
                                         placeholder_text_color=MUTED, font=F(13), width=260)
        self.search_entry.pack(side="left")
        self.search_entry.bind("<KeyRelease>", lambda e: self.refresh_playlists())
        ctk.CTkButton(bar, text="Şimdi yenile", width=120, height=34, corner_radius=12,
                      fg_color=SECOND, hover_color=CARD_HOVER, text_color=TEXT, font=F(13),
                      command=self.on_refresh).pack(side="right")

        self.pl_status = ctk.CTkLabel(f, text="", font=F(12), text_color=MUTED, anchor="w")
        self.pl_status.pack(side="bottom", fill="x", pady=(8, 0))
        self.pl_list = VirtualList(f, ROW_H, lambda m: PlaylistRow(m, self))
        self.pl_list.pack(fill="both", expand=True, pady=(12, 0))

    # ----- Ayarlar sekmesi --------------------------------------------
    def _build_settings(self, f):
        scroll = ctk.CTkScrollableFrame(f, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True)

        c = card(scroll, "Anonslar")
        row = ctk.CTkFrame(c, fg_color="transparent", corner_radius=0)
        row.pack(fill="x", padx=20, pady=(4, 8))
        ctk.CTkLabel(row, text="Anonslar açık", font=F(13), text_color=TEXT,
                     anchor="w").pack(side="left")
        self.ad_switch = ctk.CTkSwitch(row, text="", width=44, switch_width=44, switch_height=22,
                                       progress_color=ACCENT, button_color=TEXT,
                                       button_hover_color="#FFFFFF", fg_color=TRACK,
                                       command=self.on_ad_toggle)
        if self.engine.announce_enabled:
            self.ad_switch.select()
        self.ad_switch.pack(side="right")
        self.sl_every, self.lbl_every = setting_slider(
            c, "Anons aralığı", 1, 60, 59, self.engine.announce_every_min,
            lambda m: "%d dk" % m, self.on_every)
        self.sl_duck, self.lbl_duck = setting_slider(
            c, "Anons sırasında müzik sesi", 0, 100, 100,
            int(round(self.engine.mixer.duck_level * 100)),
            lambda v: "%d%%" % v, self.on_duck)

        mode_row = ctk.CTkFrame(c, fg_color="transparent", corner_radius=0)
        mode_row.pack(fill="x", padx=20, pady=(10, 2))
        ctk.CTkLabel(mode_row, text="Anons sırasında şarkı", font=F(13), text_color=TEXT,
                     anchor="w").pack(side="left")
        self.seg_mode = ctk.CTkSegmentedButton(
            c, values=self._mode_values(), command=self.on_announce_mode, height=34,
            corner_radius=12, font=F(12, True), fg_color=TRACK, selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER, unselected_color=SECOND,
            unselected_hover_color=CARD_HOVER, text_color=TEXT, text_color_disabled=MUTED)
        self.seg_mode.pack(fill="x", padx=20, pady=(6, 4))
        self.seg_mode.set(self._mode_label(self.engine.announce_mode))
        self.sl_fade, self.lbl_fade = setting_slider(
            c, "Duraklatma modunda geçiş süresi", 0, 3, 30, self.engine.announce_fade_sec,
            lambda v: "%.1f sn" % v, self.on_fade)
        ctk.CTkLabel(c, text="", height=6).pack()

        c2 = card(scroll, "Müzik")
        self.sl_xf, self.lbl_xf = setting_slider(
            c2, "Crossfade", 0, 10, 20, self.engine.crossfade_sec,
            lambda v: "%.1f sn" % v, self.on_crossfade)
        ctk.CTkLabel(c2, text="", height=6).pack()

        c3 = card(scroll, "Anons listesi (şu anki olasılıklar)")
        ctk.CTkLabel(c3, text="Klasör: %s" % (self.base / "anonslar"), font=F(11),
                     text_color=MUTED, anchor="w", justify="left",
                     wraplength=640).pack(anchor="w", fill="x", padx=20)
        ctk.CTkButton(c3, text="Anonslar klasörünü aç", height=34, corner_radius=12,
                      fg_color=SECOND, hover_color=CARD_HOVER, text_color=TEXT, font=F(13),
                      command=self.on_open_ads).pack(anchor="w", padx=20, pady=(10, 12))
        if not self.announcer.files:
            ctk.CTkLabel(c3, text="anonslar klasöründe .mp3 yok", font=F(12), text_color=MUTED,
                         anchor="w").pack(anchor="w", padx=20, pady=(0, 14))
        for path in self.announcer.files:
            r = ctk.CTkFrame(c3, fg_color="transparent", corner_radius=0)
            r.pack(fill="x", padx=20, pady=4)
            name = ctk.CTkLabel(r, text=Path(path).name, font=F(13), text_color=TEXT,
                                anchor="w", width=190)
            name.pack(side="left")
            tag = ctk.CTkLabel(r, text="", font=F(11), text_color=ACCENT, anchor="w", width=110)
            tag.pack(side="right")
            pct = ctk.CTkLabel(r, text="", font=F(13, True), text_color=TEXT, anchor="e", width=64)
            pct.pack(side="right")
            bar = ctk.CTkProgressBar(r, height=8, corner_radius=4, progress_color=ACCENT,
                                     fg_color=TRACK)
            bar.set(0)
            bar.pack(side="left", fill="x", expand=True, padx=12)
            self._ad_rows.append((path, name, bar, pct, tag))
        ctk.CTkLabel(c3, text="", height=8).pack()

    def _mode_label(self, mode):
        if mode == "pause":
            return "Şarkı dursun"
        return "Müzik altta çalsın %%%d" % int(round(self.engine.mixer.duck_level * 100))

    def _mode_values(self):
        return [self._mode_label("duck"), self._mode_label("pause")]

    def _refresh_mode_labels(self):
        """Müzik seviyesi değişince segment etiketindeki yüzde de güncellenir."""
        self.seg_mode.configure(values=self._mode_values())
        self.seg_mode.set(self._mode_label(self.engine.announce_mode))

    def on_announce_mode(self, value):
        mode = "pause" if value == self._mode_label("pause") else "duck"
        self.engine.set_announce_mode(mode)  # sonraki anonstan itibaren geçerli
        self._save_later("announce_mode", mode)

    def on_fade(self, v):
        value = round(float(v) * 10) / 10
        self.lbl_fade.configure(text="%.1f sn" % value)
        self.engine.set_announce_fade(value)
        self._save_later("announce_fade_sec", value)

    def on_duck(self, v):
        pct = int(round(float(v)))
        self.lbl_duck.configure(text="%d%%" % pct)
        value = round(pct / 100.0, 2)
        self.engine.set_duck_level(value)  # anında uygulanır
        self._save_later("duck_level", value)
        self._refresh_mode_labels()

    # ----- Hakkında sekmesi -------------------------------------------
    def _build_about(self, f):
        scroll = ctk.CTkScrollableFrame(f, fg_color="transparent", corner_radius=0)
        scroll.pack(fill="both", expand=True)

        head = card(scroll)
        ctk.CTkLabel(head, text=APP_NAME, font=F(24, True), text_color=TEXT,
                     anchor="w").pack(anchor="w", padx=20, pady=(16, 0))
        ctk.CTkLabel(head, text="Sürüm %s" % self._version, font=F(13), text_color=MUTED,
                     anchor="w").pack(anchor="w", padx=20)
        ctk.CTkLabel(head, text="Geliştiren: %s" % DEVELOPER, font=F(13), text_color=TEXT,
                     anchor="w").pack(anchor="w", padx=20, pady=(10, 0))
        self._link_row(head, "İletişim: ", CONTACT_EMAIL, "mailto:" + CONTACT_EMAIL)
        self._link_row(head, "GitHub: ", GITHUB_URL, GITHUB_URL)
        upd = ctk.CTkFrame(head, fg_color="transparent", corner_radius=0)
        upd.pack(anchor="w", padx=20, pady=(14, 16))
        ctk.CTkButton(upd, text="Güncellemeleri denetle", height=36, corner_radius=12,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT,
                      font=F(13, True), command=self.on_check_updates).pack(side="left")

        feat = card(scroll, "Özellikler")
        for text in FEATURES:
            ctk.CTkLabel(feat, text="•  " + text, font=F(13), text_color=TEXT, anchor="w",
                         justify="left", wraplength=640).pack(anchor="w", fill="x", padx=20,
                                                               pady=3)
        ctk.CTkLabel(feat, text="", height=8).pack()

    def _link_row(self, parent, label, text, target):
        row = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        row.pack(anchor="w", padx=20, pady=(4, 0))
        ctk.CTkLabel(row, text=label, font=F(13), text_color=TEXT).pack(side="left")
        ctk.CTkButton(row, text=text, height=26, corner_radius=8, fg_color="transparent",
                      hover_color=CARD_HOVER, text_color=ACCENT, font=F(13),
                      command=lambda: self._open_url(target)).pack(side="left")

    def _open_url(self, target):
        try:
            webbrowser.open(target)
        except Exception:
            log.exception("bağlantı açılamadı: %s", target)

    # ----- ortak çizim yardımcıları -----------------------------------
    def _set(self, widget, key, text):
        """Metin değiştiyse yeniden yazar; aynıysa dokunmaz (tick başına gereksiz çizimi önler)."""
        if self._last.get(key) != text:
            self._last[key] = text
            widget.configure(text=text)

    # ----- radyo olayları --------------------------------------------
    def on_toggle(self):
        self.engine.toggle_pause()

    def on_next(self):
        self.engine.skip()

    def on_announce_now(self):
        self.engine.announce_now()

    def on_volume(self, v):
        value = int(round(float(v)))
        self.vol_val.configure(text="%d%%" % value)
        self.engine.set_volume(value)

    def on_every(self, v):
        minutes = int(round(float(v)))
        self.lbl_every.configure(text="%d dk" % minutes)
        self.engine.set_announce_every_min(minutes)  # anında uygulanır
        self._save_later("announce_every_min", minutes)

    def on_crossfade(self, v):
        value = round(float(v) * 2) / 2  # 0,5 sn adım
        self.lbl_xf.configure(text="%.1f sn" % value)
        self.engine.set_crossfade(value)  # anında uygulanır
        self._save_later("crossfade_sec", value)

    def _save_later(self, key, value):
        """Kaydırıcı durunca (600 ms sonra) ayarı config.json'a yazar; sürüklerken tekrar yazmaz."""
        self.cfg[key] = value
        job = self._save_jobs.pop(key, None)
        if job is not None:
            self.root.after_cancel(job)
        self._save_jobs[key] = self.root.after(600, lambda: self._save_now(key))

    def _save_now(self, key):
        self._save_jobs.pop(key, None)
        try:
            save_setting(self.base, key, self.cfg[key])
        except Exception as e:
            log.exception("%s ayarı kaydedilemedi", key)
            self._set_note("Ayar kaydedilemedi: %s" % str(e)[:150])

    def on_ad_toggle(self):
        self.engine.set_announce_enabled(bool(self.ad_switch.get()))

    def on_open_ads(self):
        folder = self.base / "anonslar"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except Exception as e:
            log.exception("anonslar klasörü açılamadı")
            self._set_note("Klasör açılamadı: %s" % str(e)[:150])

    def _on_item_start(self, item):
        """Radyo iş parçacığında: her parça başında çağrılır. Arayüze dokunmaz; ana iş parçacığına bırakır."""
        if item and item[0] in ("track", "request"):
            pid, kind = item[1].playlist_id, item[0]
            self.post(lambda: self._set_track_playlist(pid, kind))
        if self._updating or not self.updater.pending_version():
            return
        self._updating = True
        self.post(self._apply_update)

    def _set_track_playlist(self, pid, kind="track"):
        self._track_pid = pid
        self._track_kind = kind
        self._track_meta = "Özel istek" if (kind == "request" and not pid) else ""

    def _tick(self):
        try:
            self._update_radio()
            self._update_queue()
            self._update_ads()
            self._set(self.sb_left, "left", "Sürüm %s   ·   Havuz: %d şarkı"
                      % (self._version, self.lib.count()))
        except Exception:
            log.exception("arayüz güncelleme hatası")
        self.root.after(TICK_MS, self._tick)

    def _update_radio(self):
        s = self.engine.snapshot()
        self._set(self.title_lbl, "title", s["title"] or "-")

        if self._track_pid and not self._track_meta:
            p = self.lib.get(self._track_pid)
            if p:
                prefix = "Özel istek   ·   " if self._track_kind == "request" else ""
                self._track_meta = prefix + "Playlist: %s   ·   Tür: %s" % (_short(p["title"], 60), p["genre"])
        self._set(self.sub_lbl, "sub", self._track_meta)
        self._set(self.status_lbl, "status", s["status"])

        self._set(self.cap_toggle, "cap", "Devam Et" if s["paused"] else "Duraklat")

        announcing = bool(s["announcing"])
        if announcing:
            if s.get("hold") is not None:
                text = "  %s ANONS — şarkı duraklatıldı  " % ANN_ICON
            elif s.get("mode") == "pause":
                text = "  %s ANONS  " % ANN_ICON
            else:
                duck = int(round(self.engine.mixer.duck_level * 100))
                text = "  %s ANONS — müzik %%%d  " % (ANN_ICON, duck)
            self._set(self.badge, "badge", text)
            if not self._badge_shown:
                self.badge.pack(side="left", pady=4, padx=(0, 8))
                self._badge_shown = True
        elif self._badge_shown:
            self.badge.pack_forget()
            self._badge_shown = False

        req_on = s["kind"] == "request"
        if req_on != self._req_badge_shown:
            if req_on:
                self.req_badge.pack(side="left", pady=4)
            else:
                self.req_badge.pack_forget()
            self._req_badge_shown = req_on

        audio_off = not s.get("audio_ok", True)
        if audio_off != self._audio_badge_shown:
            if audio_off:
                self.audio_badge.pack(side="left", pady=4, padx=(0, 8))
            else:
                self.audio_badge.pack_forget()
            self._audio_badge_shown = audio_off

        # geçiş sürerken başlık yeni şarkıya aittir: ilerleme de gelen akıştan okunur
        inc = self.engine.mixer.incoming()
        cur = inc if inc is not None else self.engine.mixer.current()
        dur = getattr(cur, "duration", None) if cur is not None else None
        if cur is not None and dur:
            elapsed = min(float(dur), cur.position_sec())
            self.progress.set(elapsed / float(dur))
            self._set(self.t_elapsed, "tel", mmss(elapsed))
            self._set(self.t_left, "tleft", "-" + mmss(float(dur) - elapsed))
        else:
            self.progress.set(0)
            self._set(self.t_elapsed, "tel", "0:00")
            self._set(self.t_left, "tleft", "--:--")

        self._set(self.next_lbl, "next", self._next_announcement_text(s))

        key = "%s:%s" % (s["kind"], s["id"])
        if key != self._cover_key:
            self._cover_key = key
            self._load_cover(s["id"])

    def _update_queue(self):
        ver = self.engine.requests.version
        if ver != self._q_ver:
            self._q_ver = ver
            self._render_queue(self.engine.requests.snapshot())

    def _render_queue(self, items):
        self._clear(self.q_inner)
        self.q_count.configure(text=("%d öğe" % len(items)) if items else "")
        if not items:
            ctk.CTkLabel(self.q_inner, text="Kuyruk boş: radyo havuzdan çalıyor.\n"
                         "Şarkı ya da playlist ekleyerek sıraya alabilirsin.",
                         font=F(12), text_color=MUTED, anchor="w", justify="left",
                         wraplength=260).pack(anchor="w", padx=10, pady=8)
            return
        for it in items:
            row = ctk.CTkFrame(self.q_inner, fg_color=SECOND, corner_radius=12)
            row.pack(fill="x", pady=3, padx=4)
            if it["kind"] == "playlist":
                text = "%s — %d/%d" % (_short(it["title"], 24), it["done"], it["total"])
            else:
                text = _short(it["title"], 32)
            ctk.CTkLabel(row, text=text, font=F(12, True), text_color=TEXT, anchor="w",
                         justify="left").pack(side="left", fill="x", expand=True,
                                              padx=(10, 4), pady=8)
            rid = it["id"]
            ctk.CTkButton(row, text="✕", width=28, height=28, corner_radius=14,
                          fg_color="transparent", hover_color=DANGER, text_color=TEXT,
                          font=F(12), command=lambda r=rid: self.on_queue_remove(r)
                          ).pack(side="right", padx=(0, 6))
            if it["kind"] == "playlist":
                ctk.CTkButton(row, text="⏭", width=34, height=28, corner_radius=14,
                              fg_color=CARD_HOVER, hover_color=ACCENT, text_color=TEXT,
                              font=F(12), command=lambda r=rid: self.on_queue_skip(r)
                              ).pack(side="right", padx=2)

    def _clear(self, frame):
        for w in frame.winfo_children():
            w.destroy()

    def _next_announcement_text(self, s):
        if not self.announcer.files:
            return "Sıradaki anons: anonslar klasöründe .mp3 yok"
        if not self.engine.announce_enabled:
            return "Anonslar kapalı"
        if s["until_ann_sec"] is None:
            return "Sıradaki anons: -"
        text = "Sıradaki anons: %s" % mmss(s["until_ann_sec"])
        return text + "  (duraklatıldı)" if s["paused"] else text

    # ----- özel istek: arama ------------------------------------------
    def _set_req_note(self, text):
        self.req_note.configure(text=text)

    def _show_placeholder_results(self, text="Şarkı ya da playlist ara; sonuçlar burada listelenir."):
        self._res_labels = []
        self._clear(self.res_inner)
        ctk.CTkLabel(self.res_inner, text=text, font=F(12), text_color=MUTED, anchor="w",
                     justify="left", wraplength=560).pack(anchor="w", padx=12, pady=12)

    def on_request_submit(self):
        text = self.req_entry.get().strip()
        if not text:
            return
        self._res_token += 1
        token = self._res_token
        self._set_req_note("Aranıyor...")
        link = parse_link(text)

        def work():
            try:
                if link and link[0] == "video":
                    data = {"songs": [video_info(link[1])], "playlists": []}
                elif link and link[0] == "playlist":
                    info = self._playlist_info(link[1])
                    data = {"songs": [], "playlists": [{
                        "id": info["id"], "title": info["title"],
                        "thumbnail": info["thumbnail"], "count": len(info["tracks"])}]}
                else:
                    data = search_all(text)
                self.post(lambda: self._show_results(token, data, None))
            except Exception as ex:
                err = str(ex)[:200] or "bilinmeyen hata"
                self.post(lambda: self._show_results(token, None, err))

        threading.Thread(target=work, daemon=True).start()

    def _show_results(self, token, data, err):
        if token != self._res_token:
            return  # daha yeni bir arama var
        self._res_labels = []
        self._pl_meta = {}
        self._clear(self.res_inner)
        if err is not None:
            self._set_req_note("Arama başarısız: %s" % err)
            self._show_placeholder_results("Sonuç alınamadı. Bağlantıyı kontrol edip yeniden deneyin.")
            return
        songs = data.get("songs") or []
        pls = data.get("playlists") or []
        if not songs and not pls:
            self._set_req_note("Sonuç bulunamadı.")
            self._show_placeholder_results("Bu aramada sonuç yok.")
            return
        if songs:
            ctk.CTkLabel(self.res_inner, text="Şarkılar", font=F(12, True), text_color=ACCENT,
                         anchor="w").pack(anchor="w", padx=8, pady=(4, 2))
            for s in songs:
                self._song_row(s)
        if pls:
            ctk.CTkLabel(self.res_inner, text="Playlistler", font=F(12, True), text_color=ACCENT,
                         anchor="w").pack(anchor="w", padx=8, pady=(8, 2))
            for p in pls:
                self._playlist_row(p)
        if pls:
            self._count_playlists(token, [p["id"] for p in pls if not p.get("count")])
        self._set_req_note("%d şarkı, %d playlist bulundu. Şarkıya ya da playlist düğmesine tıkla."
                           % (len(songs), len(pls)))

    def _song_row(self, s):
        row = ctk.CTkFrame(self.res_inner, fg_color=SECOND, corner_radius=12)
        row.pack(fill="x", padx=4, pady=3)
        th = ctk.CTkLabel(row, text="", width=THUMB_PX, height=THUMB_PX)
        th.pack(side="left", padx=(8, 10), pady=6)
        self._res_thumb(th, ("v", s["id"]), self.lib.thumb_for_track(s["id"]),
                        THUMB_URL.format(s["id"]))
        txt = ctk.CTkFrame(row, fg_color="transparent", corner_radius=0)
        txt.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(txt, text=_short(s["title"], 64), font=F(13, True), text_color=TEXT,
                     anchor="w", justify="left").pack(anchor="w")
        meta = s.get("channel") or ""
        if s.get("duration"):
            meta = (meta + "   ·   " if meta else "") + mmss(s["duration"])
        ctk.CTkLabel(txt, text=_short(meta, 70), font=F(12), text_color=MUTED,
                     anchor="w").pack(anchor="w")
        ctk.CTkButton(row, text="+ Ekle", width=84, height=30, corner_radius=15,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT,
                      font=F(12, True), command=lambda x=s: self.on_add_song(x)
                      ).pack(side="right", padx=10)

    def _playlist_row(self, p):
        row = ctk.CTkFrame(self.res_inner, fg_color=SECOND, corner_radius=12)
        row.pack(fill="x", padx=4, pady=3)
        th = ctk.CTkLabel(row, text="", width=THUMB_PX, height=THUMB_PX)
        th.pack(side="left", padx=(8, 10), pady=6)
        self._res_thumb(th, ("p", p["id"]), self.lib.thumb_for_playlist(p["id"]),
                        p.get("thumbnail") or "")
        txt = ctk.CTkFrame(row, fg_color="transparent", corner_radius=0)
        txt.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(txt, text=_short(p["title"], 46), font=F(13, True), text_color=TEXT,
                     anchor="w", justify="left").pack(anchor="w")
        meta = ("%d şarkı" % p["count"]) if p.get("count") else "Şarkı sayısı alınıyor..."
        count_lbl = ctk.CTkLabel(txt, text=meta, font=F(12), text_color=MUTED, anchor="w")
        count_lbl.pack(anchor="w")
        self._pl_meta[p["id"]] = count_lbl
        btns = ctk.CTkFrame(row, fg_color="transparent", corner_radius=0)
        btns.pack(side="right", padx=8)
        pid = p["id"]
        ctk.CTkButton(btns, text="Sırayla", width=70, height=28, corner_radius=14,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT,
                      font=F(11, True), command=lambda: self.on_playlist_queue(pid, "seq")
                      ).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="Karışık", width=70, height=28, corner_radius=14,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, text_color=ACCENT_TEXT,
                      font=F(11, True), command=lambda: self.on_playlist_queue(pid, "shuffle")
                      ).pack(side="left", padx=2)
        ctk.CTkButton(btns, text="Radyoya ekle", width=104, height=28, corner_radius=14,
                      fg_color=CARD, hover_color=CARD_HOVER, text_color=TEXT,
                      font=F(11), command=lambda: self.on_playlist_keep(pid)
                      ).pack(side="left", padx=2)

    def _playlist_info(self, pid):
        """Playlist bilgisi (şarkı listesi dâhil); aynı playlist tekrar okunmasın diye önbelleklenir."""
        hit = self._pl_cache.get(pid)
        if hit is not None:
            return hit
        info = fetch_playlist(PLAYLIST_WATCH % pid)
        if len(self._pl_cache) >= 20:
            self._pl_cache.pop(next(iter(self._pl_cache)))
        self._pl_cache[pid] = info
        return info

    def _count_playlists(self, token, pids):
        """Arama sonucundaki playlistlerin şarkı sayısını arka planda okur (önbelleğe de alır)."""
        def work():
            for pid in pids:
                try:
                    n = len(self._playlist_info(pid)["tracks"])
                except Exception:
                    continue
                self.post(lambda pid=pid, n=n: self._set_pl_count(token, pid, n))

        threading.Thread(target=work, daemon=True).start()

    def _set_pl_count(self, token, pid, n):
        if token != self._res_token:
            return
        lbl = self._pl_meta.get(pid)
        if lbl is None:
            return
        try:
            lbl.configure(text="%d şarkı" % n)
        except Exception:
            pass  # satır bu arada yeniden çizildi

    def on_add_song(self, s):
        self.engine.add_request(self.engine.requests.make_track(s["id"], s["title"]))
        self._set_req_note("Kuyruğa eklendi: %s" % _short(s["title"], 70))

    def on_playlist_queue(self, pid, mode):
        self._set_req_note("Playlist okunuyor, lütfen bekleyin...")

        def work():
            try:
                info = self._playlist_info(pid)
                req = self.engine.requests.make_playlist(info["id"], info["title"], info["tracks"],
                                                         mode=mode, thumb=info["id"])
                self.engine.add_request(req)
                msg = "Kuyruğa eklendi: %s (%d şarkı, %s)" % (
                    _short(info["title"], 50), len(info["tracks"]),
                    "karışık" if mode == "shuffle" else "sırayla")
            except Exception as ex:
                msg = "Playlist eklenemedi: %s" % str(ex)[:160]
            self.post(lambda: self._set_req_note(msg))

        threading.Thread(target=work, daemon=True).start()

    def on_playlist_keep(self, pid):
        self._set_req_note("Playlist radyoya ekleniyor...")

        def work():
            try:
                e = self.lib.add_playlist(PLAYLIST_WATCH % pid)
                msg = "Radyoya eklendi: %s (%d şarkı)" % (_short(e["title"], 50), e["count"])
            except Exception as ex:
                msg = "Eklenemedi: %s" % str(ex)[:160]
            self.post(lambda: self._set_req_note(msg))

        threading.Thread(target=work, daemon=True).start()

    def on_queue_remove(self, rid):
        self.engine.remove_request(rid)

    def on_queue_skip(self, rid):
        self.engine.skip_request_item(rid)

    # ----- arama sonucu kapakları (önbellekli, tembel) -----------------
    def _res_img(self, key, path):
        """Önbellekteki (ya da dosyadaki) kapak; yoksa None."""
        img = self._thumbs.get(key)
        if img is None and path.exists():
            try:
                img = ctk_image(rounded(Image.open(path), (THUMB_PX, THUMB_PX), 10),
                                (THUMB_PX, THUMB_PX))
                self._thumbs[key] = img
            except Exception:
                path.unlink(missing_ok=True)
                img = None
        return img

    def _res_thumb(self, label, key, path, url):
        img = self._res_img(key, path)
        if img is not None:
            label.configure(image=img)
            return
        label.configure(image=self._ph_thumb())
        if not url:
            return
        self._res_labels.append((key, label, path))
        if key in self._thumb_busy:
            return
        self._thumb_busy.add(key)

        def work():
            with self._thumb_sem:
                ok = download_image(url, path)
            self.post(lambda: self._res_thumb_done(key, ok))

        threading.Thread(target=work, daemon=True).start()

    def _res_thumb_done(self, key, ok):
        self._thumb_busy.discard(key)
        keep = []
        for k, label, path in self._res_labels:
            if k != key:
                keep.append((k, label, path))
                continue
            if not ok:
                continue
            try:
                img = self._res_img(key, path)
                if img is not None:
                    label.configure(image=img)
            except Exception:
                pass  # satır bu arada yeniden çizildi
        self._res_labels = keep

    def _update_ads(self):
        if not self._ad_rows:
            return
        probs = {p: (pct, last) for p, pct, last in self.announcer.probabilities()}
        for path, name, bar, pct_lbl, tag in self._ad_rows:
            pct, last = probs.get(path, (0.0, False))
            bar.set(pct / 100.0)
            self._set(pct_lbl, ("pct", path), "%.1f%%" % pct)
            self._set(tag, ("tag", path), "son çalan, bu sefer çalmaz" if last else "")

    # ----- kapaklar ---------------------------------------------------
    def _load_cover(self, vid):
        self._cover_id = vid
        if not vid:
            self._set_cover(None)
            return
        path = self.lib.thumb_for_track(vid)
        if path.exists():
            self._set_cover(path)
            return
        self._set_cover(None)

        def work():
            ok = download_image(THUMB_URL.format(vid), path)
            self.post(lambda: self._cover_ready(vid, path if ok else None))

        threading.Thread(target=work, daemon=True).start()

    def _cover_ready(self, vid, path):
        if vid == self._cover_id:
            self._set_cover(path)

    def _set_cover(self, path):
        img = None
        if path is not None:
            try:
                img = ctk_image(rounded(Image.open(path), (COVER_PX, COVER_PX), COVER_RADIUS),
                                (COVER_PX, COVER_PX))
            except Exception:
                log.warning("kapak okunamadı: %s", path)
        self._cover_img = img or self._ph_cover
        self.cover.configure(image=self._cover_img)

    def thumb_image(self, pid, url=""):
        """Playlist kapağı (CTkImage). Yoksa yer tutucu döner ve indirmeyi arka planda başlatır."""
        img = self._thumbs.get(pid)
        if img is not None:
            return img
        path = self.lib.thumb_for_playlist(pid)
        if path.exists():
            try:
                img = ctk_image(rounded(Image.open(path), (THUMB_PX, THUMB_PX), 10),
                                (THUMB_PX, THUMB_PX))
                self._thumbs[pid] = img
                return img
            except Exception:
                path.unlink(missing_ok=True)
        self._request_thumb(pid, url)
        return self._ph_thumb()

    def _ph_thumb(self):
        if getattr(self, "_ph_thumb_img", None) is None:
            self._ph_thumb_img = ctk_image(placeholder((THUMB_PX, THUMB_PX), 10, "♫"),
                                           (THUMB_PX, THUMB_PX))
        return self._ph_thumb_img

    def _request_thumb(self, pid, url):
        if not url or pid in self._thumb_busy:
            return
        failed = self._thumb_failed.get(pid)
        if failed is not None and time.monotonic() - failed < THUMB_RETRY_SEC:
            return
        self._thumb_busy.add(pid)
        path = self.lib.thumb_for_playlist(pid)

        def work():
            with self._thumb_sem:
                ok = download_image(url, path)
            self.post(lambda: self._thumb_done(pid, ok))

        threading.Thread(target=work, daemon=True).start()

    def _thumb_done(self, pid, ok):
        self._thumb_busy.discard(pid)
        if ok:
            self._thumb_failed.pop(pid, None)
            self.pl_list.reflow()
        else:
            self._thumb_failed[pid] = time.monotonic()

    # ----- güncelleme -------------------------------------------------
    def _apply_update(self):
        ver = self.updater.pending_version()
        if not ver:
            self._updating = False
            return
        self._set_note("Sürüm v%s kuruluyor, program yeniden başlıyor..." % ver)
        try:
            ok = self.updater.apply_pending()
        except Exception:
            log.exception("güncelleme kurulumu başlatılamadı")
            ok = False
        if not ok:
            self._updating = False
            self._set_note("Güncelleme kurulamadı (data\\radyo.log dosyasına bakın).")
            return
        # update.bat bu sürecin çıkmasını bekler; hemen kapanıyoruz
        self.engine.stop()
        self.lib.stop()
        self.updater.stop()
        self.root.destroy()
        logging.shutdown()
        os._exit(0)

    def _on_update_result(self, res):
        if res.status == "staged":
            self._set_note("Yeni sürüm v%s hazır; bir sonraki parça geçişinde kurulacak." % res.version)
        elif res.status == "available":
            self._set_note(res.message)

    def _set_note(self, text):
        self._set(self.sb_mid, "note", text)

    def on_check_updates(self):
        self.select_page("about")
        self._set_note("Güncellemeler denetleniyor...")

        def work():
            res = self.updater.check()
            self.post(lambda: self._show_check_result(res))

        threading.Thread(target=work, daemon=True).start()

    def _show_check_result(self, res):
        if res.status == "staged":
            self._on_update_result(res)
            messagebox.showinfo("Güncelleme", "Sürüm v%s indirildi ve doğrulandı.\n"
                                "Bir sonraki parça geçişinde kurulacak." % res.version,
                                parent=self.root)
        elif res.status == "available":
            self._on_update_result(res)
            messagebox.showinfo("Güncelleme", res.message, parent=self.root)
        elif res.status == "uptodate":
            self._set_note("")
            messagebox.showinfo("Güncelleme", "Programınız güncel (sürüm %s)."
                                % self.updater.local_version(), parent=self.root)
        elif res.status == "disabled":
            self._set_note("")
            messagebox.showinfo("Güncelleme", res.message, parent=self.root)
        else:
            self._set_note(res.message)
            messagebox.showwarning("Güncelleme", res.message, parent=self.root)

    # ----- internet durumu -------------------------------------------
    def _net_check(self):
        def work():
            ok = internet_ok()
            self.post(lambda: self._set_net(ok))

        threading.Thread(target=work, daemon=True).start()
        self.root.after(NET_EVERY_MS, self._net_check)

    def _set_net(self, ok):
        if ok:
            self._set(self.net_lbl, "net", "● İnternet: var")
            self.net_lbl.configure(text_color=ACCENT)
        else:
            self._set(self.net_lbl, "net", "● İnternet: yok")
            self.net_lbl.configure(text_color=DANGER)

    # ----- playlist sekmesi ------------------------------------------
    def refresh_playlists(self):
        playlists = self.lib.snapshot_playlists()
        q = self.search_entry.get().strip().lower()
        shown = [p for p in playlists if q in p["title"].lower()] if q else playlists
        self.pl_list.set_items(shown)
        msg = "%d playlist   |   toplam %d şarkı (açık olanlar)" % (len(playlists), self.lib.count())
        if q:
            msg += "   |   aramada %d eşleşme" % len(shown)
        self.pl_status.configure(text=msg)

    def set_playlist_enabled(self, pid, on):
        self.lib.set_enabled(pid, on)
        self.refresh_playlists()

    def on_add(self):
        url = self.url_entry.get().strip()
        if not url:
            return
        self.add_btn.configure(state="disabled")
        self.pl_status.configure(text="Playlist okunuyor, lütfen bekleyin...")

        def work():
            try:
                e = self.lib.add_playlist(url)
                msg = "Eklendi: %s (%d şarkı)" % (e["title"], e["count"])
                self.post(lambda: (self.url_entry.delete(0, "end"), self.pl_status.configure(text=msg)))
            except Exception as ex:
                err = "Eklenemedi: %s" % str(ex)[:200]
                self.post(lambda: self.pl_status.configure(text=err))
            finally:
                self.post(lambda: self.add_btn.configure(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    def on_delete(self, pid):
        p = self.lib.get(pid)
        if not p:
            return
        if messagebox.askyesno("Sil", "Bu playlist listeden silinsin mi?\n\n%s" % p["title"],
                               parent=self.root):
            self.lib.remove_playlist(pid)
            self.refresh_playlists()

    def on_genre(self, pid):
        p = self.lib.get(pid)
        if not p:
            return
        new = simpledialog.askstring("Tür", "Yeni tür:", initialvalue=p["genre"], parent=self.root)
        if new is not None:
            self.lib.set_genre(p["id"], new.strip())
            self.refresh_playlists()

    def on_refresh(self):
        self.pl_status.configure(text="Şarkı listeleri yenileniyor...")

        def work():
            try:
                self.lib.refresh_stale(force=True)
                self.post(lambda: self.pl_status.configure(text="Yenileme bitti"))
            except Exception as ex:
                err = "Yenileme hatası: %s" % str(ex)[:200]
                self.post(lambda: self.pl_status.configure(text=err))

        threading.Thread(target=work, daemon=True).start()

    # ----- ana iş parçacığına aktarım --------------------------------
    def post(self, fn):
        self.q.put(fn)

    def _pump(self):
        while True:
            try:
                fn = self.q.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:
                log.exception("arayüz işi hatası")
        self.root.after(100, self._pump)

    def close(self):
        self.engine.stop()
        self.lib.stop()
        self.updater.stop()
        self.root.destroy()


def run_app(base, cfg, lib, updater):
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("dark-blue")
    root = ctk.CTk()
    try:
        App(root, base, cfg, lib, updater)
    except Exception as e:
        log.exception("başlatma hatası")
        messagebox.showerror(APP_NAME,
                             "Program başlatılamadı:\n\n%s\n\n"
                             "Ses çıkışı (ses aygıtı) ve ffmpeg (bin\\ffmpeg.exe veya PATH) gerekir." % e)
        root.destroy()
        return
    root.mainloop()
