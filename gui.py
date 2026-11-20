"""Tkinter arayüzü: Radyo ve Playlistler sekmeleri."""

import logging
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageOps, ImageTk

from ads import AnnouncementPicker, list_announcements
from player import RadioEngine
from playlists import THUMB_URL, download_image

log = logging.getLogger("radyo.gui")

COVER_SIZE = (320, 180)
ROW_THUMB_SIZE = (64, 36)
CHECK_ON, CHECK_OFF = "☑", "☐"


def load_photo(path, size):
    im = Image.open(path).convert("RGB")
    return ImageTk.PhotoImage(ImageOps.fit(im, size))


class App:
    def __init__(self, root, base, cfg, lib):
        self.root = root
        self.base = Path(base)
        self.cfg = cfg
        self.lib = lib
        self.q = queue.Queue()
        self.cover_key = None
        self.cover_id = None
        self.cover_img = None
        self.row_imgs = {}
        self.thumb_busy = set()
        self.ad_labels = []
        self.announcer = AnnouncementPicker(list_announcements(self.base / "anonslar"))
        self.engine = RadioEngine(cfg, lib, self.announcer)  # VLC yoksa burada hata verir

        root.title("Market Radyo")
        root.geometry("680x800")
        root.minsize(620, 680)
        self._build()
        self.engine.start()
        lib.start_background(on_change=lambda: self.post(self.refresh_playlists))
        self.refresh_playlists()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self._pump)
        root.after(300, self._tick)

    # ----- kurulum ----------------------------------------------------
    def _build(self):
        nb = ttk.Notebook(self.root)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        radio = ttk.Frame(nb, padding=12)
        plist = ttk.Frame(nb, padding=12)
        nb.add(radio, text="  Radyo  ")
        nb.add(plist, text="  Playlistler  ")
        self._build_radio(radio)
        self._build_playlists(plist)

    def _build_radio(self, f):
        self.cover = tk.Label(f, text="Kapak yok", width=44, height=9, bg="#202020", fg="#aaaaaa")
        self.cover.pack(pady=(0, 10))
        self.title_var = tk.StringVar(value="Hazırlanıyor...")
        tk.Label(f, textvariable=self.title_var, font=("Segoe UI", 15, "bold"),
                 wraplength=620, justify="center").pack(fill="x")
        self.status_var = tk.StringVar()
        tk.Label(f, textvariable=self.status_var, fg="#777777", wraplength=620).pack(pady=(4, 12))

        row = ttk.Frame(f)
        row.pack(pady=4)
        self.btn_toggle = tk.Button(row, text="Duraklat", font=("Segoe UI", 14, "bold"),
                                    width=12, height=2, command=self.on_toggle)
        self.btn_toggle.grid(row=0, column=0, padx=8)
        tk.Button(row, text="Sonraki", font=("Segoe UI", 14, "bold"),
                  width=12, height=2, command=self.on_next).grid(row=0, column=1, padx=8)

        self.vol = tk.Scale(f, from_=0, to=100, orient="horizontal", label="Ses",
                            length=560, command=self.on_volume)
        self.vol.set(int(self.cfg.get("volume", 70)))
        self.vol.pack(pady=(12, 0))

        self.every = tk.Scale(f, from_=1, to=20, orient="horizontal",
                              label="Anons aralığı (her kaç şarkıda bir)",
                              length=560, command=self.on_every)
        self.every.set(self.engine.announce_every)
        self.every.pack(pady=(8, 0))

        self.ad_on = tk.BooleanVar(value=self.engine.announce_enabled)
        ttk.Checkbutton(f, text="Anonslar açık", variable=self.ad_on,
                        command=self.on_ad_toggle).pack(pady=6)

        lf = ttk.LabelFrame(f, text="Anons olasılıkları (şu an)", padding=8)
        lf.pack(fill="x", pady=6)
        if not self.announcer.files:
            ttk.Label(lf, text="anonslar klasöründe .mp3 yok").pack(anchor="w")
        for path in self.announcer.files:
            lbl = ttk.Label(lf, text=Path(path).name, font=("Consolas", 10))
            lbl.pack(anchor="w")
            self.ad_labels.append((path, lbl))

    def _build_playlists(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="YouTube playlist URL:").pack(side="left")
        self.url_var = tk.StringVar()
        ent = ttk.Entry(top, textvariable=self.url_var)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ent.bind("<Return>", lambda e: self.on_add())
        self.add_btn = ttk.Button(top, text="Ekle", command=self.on_add)
        self.add_btn.pack(side="left")

        style = ttk.Style()
        style.configure("Treeview", rowheight=44)
        tv = ttk.Treeview(f, columns=("on", "title", "count", "genre"),
                          show="tree headings", selectmode="browse")
        tv.heading("#0", text="Kapak")
        tv.heading("on", text="Açık")
        tv.heading("title", text="Başlık")
        tv.heading("count", text="Şarkı")
        tv.heading("genre", text="Tür")
        tv.column("#0", width=84, stretch=False)
        tv.column("on", width=50, anchor="center", stretch=False)
        tv.column("title", width=300)
        tv.column("count", width=70, anchor="e", stretch=False)
        tv.column("genre", width=110)
        tv.pack(fill="both", expand=True, pady=8)
        tv.bind("<Button-1>", self.on_tree_click)
        self.tv = tv

        bf = ttk.Frame(f)
        bf.pack(fill="x")
        ttk.Button(bf, text="Tür değiştir", command=self.on_genre).pack(side="left")
        ttk.Button(bf, text="Sil", command=self.on_delete).pack(side="left", padx=6)
        ttk.Button(bf, text="Şimdi yenile", command=self.on_refresh).pack(side="left")
        self.pl_status = tk.StringVar()
        ttk.Label(f, textvariable=self.pl_status, foreground="#666666").pack(anchor="w", pady=4)

    # ----- radyo olayları --------------------------------------------
    def on_toggle(self):
        self.engine.toggle_pause()

    def on_next(self):
        self.engine.skip()

    def on_volume(self, v):
        self.engine.set_volume(int(float(v)))

    def on_every(self, v):
        self.engine.set_announce_every(int(float(v)))

    def on_ad_toggle(self):
        self.engine.set_announce_enabled(self.ad_on.get())

    def _tick(self):
        try:
            s = self.engine.snapshot()
            self.title_var.set(s["title"] or "-")
            left = ""
            if s["until_ad"] is not None:
                left = "   |   Sonraki anonsa: %d şarkı" % s["until_ad"]
            self.status_var.set("%s   |   Havuz: %d şarkı%s"
                                % (s["status"], self.lib.count(), left))
            self.btn_toggle.config(text="Devam Et" if s["paused"] else "Duraklat")
            key = "%s:%s" % (s["kind"], s["id"])
            if key != self.cover_key:
                self.cover_key = key
                self.cover_id = s["id"]
                self._load_cover(s["id"])
            self._update_ads()
        except Exception:
            log.exception("arayüz güncelleme hatası")
        self.root.after(500, self._tick)

    def _update_ads(self):
        probs = {p: (pct, last) for p, pct, last in self.announcer.probabilities()}
        for path, lbl in self.ad_labels:
            pct, last = probs.get(path, (0.0, False))
            tag = "   (son çalan, bu sefer çalmaz)" if last else ""
            lbl.config(text="%-14s %%%5.1f%s" % (Path(path).name, pct, tag))

    def _load_cover(self, vid):
        if not vid:
            self.cover_img = None
            self.cover.config(image="", text="Anons")
            return
        path = self.lib.thumb_for_track(vid)

        def work():
            ok = download_image(THUMB_URL.format(vid), path)
            self.post(lambda: self._show_cover(vid, path if ok else None))

        threading.Thread(target=work, daemon=True).start()

    def _show_cover(self, vid, path):
        if vid != self.cover_id:
            return
        if path is None:
            self.cover.config(image="", text="Kapak yüklenemedi")
            return
        try:
            self.cover_img = load_photo(path, COVER_SIZE)
            self.cover.config(image=self.cover_img, text="")
        except Exception:
            self.cover.config(image="", text="Kapak okunamadı")

    # ----- playlist sekmesi ------------------------------------------
    def refresh_playlists(self):
        tv = self.tv
        tv.delete(*tv.get_children())
        playlists = self.lib.snapshot_playlists()
        for p in playlists:
            img = self._row_thumb(p)
            tv.insert("", "end", iid=p["id"], text="", image=img or "",
                      values=(CHECK_ON if p["enabled"] else CHECK_OFF,
                              p["title"], p["count"], p["genre"]))
        self.pl_status.set("%d playlist   |   toplam %d şarkı (açık olanlar)"
                           % (len(playlists), self.lib.count()))

    def _row_thumb(self, p):
        pid = p["id"]
        path = self.lib.thumb_for_playlist(pid)
        if path.exists():
            if pid not in self.row_imgs:
                try:
                    self.row_imgs[pid] = load_photo(path, ROW_THUMB_SIZE)
                except Exception:
                    path.unlink(missing_ok=True)
            return self.row_imgs.get(pid)
        if p.get("thumbnail") and pid not in self.thumb_busy:
            self.thumb_busy.add(pid)
            url = p["thumbnail"]

            def work():
                ok = download_image(url, path)
                self.post(lambda: self._thumb_done(pid, ok))

            threading.Thread(target=work, daemon=True).start()
        return None

    def _thumb_done(self, pid, ok):
        self.thumb_busy.discard(pid)
        if ok:
            self.refresh_playlists()

    def on_tree_click(self, e):
        if self.tv.identify_region(e.x, e.y) != "cell":
            return
        if self.tv.identify_column(e.x) != "#1":
            return
        pid = self.tv.identify_row(e.y)
        if not pid:
            return
        p = self.lib.get(pid)
        if p:
            self.lib.set_enabled(pid, not p["enabled"])

    def on_add(self):
        url = self.url_var.get().strip()
        if not url:
            return
        self.add_btn.config(state="disabled")
        self.pl_status.set("Playlist okunuyor, lütfen bekleyin...")

        def work():
            try:
                e = self.lib.add_playlist(url)
                msg = "Eklendi: %s (%d şarkı)" % (e["title"], e["count"])
                self.post(lambda: (self.url_var.set(""), self.pl_status.set(msg)))
            except Exception as ex:
                err = "Eklenemedi: %s" % str(ex)[:200]
                self.post(lambda: self.pl_status.set(err))
            finally:
                self.post(lambda: self.add_btn.config(state="normal"))

        threading.Thread(target=work, daemon=True).start()

    def on_delete(self):
        sel = self.tv.selection()
        if not sel:
            self.pl_status.set("Önce bir playlist seçin")
            return
        pid = sel[0]
        p = self.lib.get(pid)
        if not p:
            return
        if messagebox.askyesno("Sil", "Bu playlist listeden silinsin mi?\n\n%s" % p["title"]):
            self.lib.remove_playlist(pid)
            self.refresh_playlists()

    def on_genre(self):
        sel = self.tv.selection()
        if not sel:
            self.pl_status.set("Önce bir playlist seçin")
            return
        p = self.lib.get(sel[0])
        if not p:
            return
        new = simpledialog.askstring("Tür", "Yeni tür:", initialvalue=p["genre"], parent=self.root)
        if new is not None:
            self.lib.set_genre(p["id"], new.strip())
            self.refresh_playlists()

    def on_refresh(self):
        self.pl_status.set("Şarkı listeleri yenileniyor...")

        def work():
            try:
                self.lib.refresh_stale(force=True)
                self.post(lambda: self.pl_status.set("Yenileme bitti"))
            except Exception as ex:
                err = "Yenileme hatası: %s" % str(ex)[:200]
                self.post(lambda: self.pl_status.set(err))

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
        self.root.destroy()


def run_app(base, cfg, lib):
    root = tk.Tk()
    try:
        App(root, base, cfg, lib)
    except Exception as e:
        log.exception("başlatma hatası")
        messagebox.showerror("Market Radyo",
                             "Program başlatılamadı:\n\n%s\n\n"
                             "64-bit VLC Media Player kurulu olmalı." % e)
        root.destroy()
        return
    root.mainloop()
