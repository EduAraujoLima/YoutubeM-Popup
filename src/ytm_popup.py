#!/usr/bin/env python3
"""ytm-popup - mini player popup para YouTube Music (sem login).

Busca via ytmusicapi (modo anônimo), toca via mpv + yt-dlp.
Executar de novo o comando alterna (mostra/esconde) a janela: é isso que o
atalho de teclado faz. A música continua tocando com a janela escondida.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

APP_ID = "io.github.ytmpopup"
CONFIG_DIR = os.path.join(GLib.get_user_config_dir(), "ytm-popup")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_CONFIG = {
    "volume": 70,
    "hide_on_focus_out": True,
    # "auto" -> se você estiver logado no YouTube no Firefox, usa essa sessão
    # (Premium, recomendações); senão toca em modo anônimo.
    # Também aceita "firefox", "chrome", "chromium", "brave"... ou "none".
    "cookies_from_browser": "auto",
    "language": "pt",
}
ART_SIZE = 96
SEEK_STEP = 10


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError:
        pass


def fmt_time(sec):
    if sec is None or sec < 0:
        return "0:00"
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}" if sec < 3600 else \
        f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def run_bg(func, callback, *args):
    """Executa func(*args) numa thread e entrega (resultado, erro) no loop GTK."""
    def worker():
        try:
            res, err = func(*args), None
        except Exception as e:  # noqa: BLE001
            res, err = None, e
        GLib.idle_add(callback, res, err)
    threading.Thread(target=worker, daemon=True).start()


# ------------------------------------------------------------------ login
LOGIN_COOKIES = {"SAPISID", "__Secure-3PAPISID"}
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"


def detect_login(setting):
    """Reaproveita o login do navegador (nenhuma senha é pedida/digitada).

    Lê os cookies do YouTube do perfil do navegador, salva só os do
    YouTube/Google num arquivo privado (0600) para o yt-dlp e monta os
    cabeçalhos para o ytmusicapi. Retorna None se não houver login.
    """
    setting = (setting or "auto").lower()
    if setting == "none":
        return None
    browsers = ["firefox"] if setting == "auto" else [setting]
    from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser

    class _Quiet:
        def debug(self, *a, **k): pass
        info = warning = error = debug
        def progress_bar(self): return None

    for browser in browsers:
        try:
            jar = extract_cookies_from_browser(browser, logger=_Quiet())
        except Exception:  # navegador/perfil inexistente
            continue
        keep = [c for c in jar if c.domain.endswith(("youtube.com", "google.com"))]
        yt = [c for c in keep if c.domain.endswith("youtube.com")]
        if not LOGIN_COOKIES & {c.name for c in yt}:
            continue
        runtime = GLib.get_user_runtime_dir() or "/tmp"
        path = os.path.join(runtime, f"ytm-popup-{os.getuid()}.cookies")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.close(fd)
        out = YoutubeDLCookieJar(path)
        for c in keep:
            out.set_cookie(c)
        out.save(ignore_discard=True, ignore_expires=True)
        seen, parts = set(), []
        for c in yt:
            if c.name not in seen:
                seen.add(c.name)
                parts.append(f"{c.name}={c.value}")
        headers = {
            "cookie": "; ".join(parts),
            "x-goog-authuser": "0",
            # valor real é recalculado pelo ytmusicapi a cada requisição
            "authorization": "SAPISIDHASH 0_0",
            "origin": "https://music.youtube.com",
            "user-agent": UA,
            "accept": "*/*",
            "content-type": "application/json",
        }
        return {"browser": browser, "cookie_file": path, "headers": headers}
    return None


# --------------------------------------------------------------------------- mpv
class Mpv:
    """Controle do mpv via socket JSON IPC."""
    YTDL_OPTS = "format=bestaudio/best"

    def use_cookies(self, path):
        opts = self.YTDL_OPTS + (f",cookies={path}" if path else "")
        self.set("ytdl-raw-options", opts)

    def __init__(self, on_event, volume):
        self.on_event = on_event
        self.lock = threading.Lock()
        runtime = GLib.get_user_runtime_dir() or "/tmp"
        self.sock_path = os.path.join(runtime, f"ytm-popup-{os.getuid()}.sock")
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass
        ytdl_opts = self.YTDL_OPTS
        cmd = [
            "mpv", "--idle=yes", "--no-video", "--no-terminal",
            "--input-ipc-server=" + self.sock_path,
            "--ytdl-raw-options=" + ytdl_opts,
            f"--volume={volume}", "--volume-max=100",
        ]
        ytdlp = os.path.join(os.path.dirname(sys.executable), "yt-dlp")
        if os.path.exists(ytdlp):
            cmd.append("--script-opts=ytdl_hook-ytdl_path=" + ytdlp)
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        for _ in range(100):
            try:
                self.sock.connect(self.sock_path)
                break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("não foi possível iniciar o mpv")
        threading.Thread(target=self._reader, daemon=True).start()
        for i, prop in enumerate(("time-pos", "duration", "pause",
                                  "core-idle", "paused-for-cache"), 1):
            self.command("observe_property", i, prop)

    def command(self, *args):
        data = (json.dumps({"command": list(args)}) + "\n").encode()
        with self.lock:
            try:
                self.sock.sendall(data)
            except OSError:
                pass

    def set(self, prop, value):
        self.command("set_property", prop, value)

    def _reader(self):
        buf = b""
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "event" in msg:
                    GLib.idle_add(self.on_event, msg)

    def quit(self):
        self.command("quit")
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# --------------------------------------------------------------- YouTube Music
class Music:
    """Busca no YouTube Music: com a sessão do navegador se houver,
    senão anônima (nenhuma conta necessária)."""

    def __init__(self, language):
        self.language = language
        self._yt = None
        self.headers = None
        self.lock = threading.Lock()

    def set_auth(self, headers):
        with self.lock:
            self.headers = headers
            self._yt = None

    def _make(self, headers):
        from ytmusicapi import YTMusic
        try:
            return YTMusic(auth=headers, language=self.language)
        except Exception:  # idioma não suportado
            return YTMusic(auth=headers)

    @property
    def yt(self):
        with self.lock:
            if self._yt is None:
                try:
                    self._yt = self._make(self.headers)
                except Exception:  # login inválido -> anônimo
                    self.headers = None
                    self._yt = self._make(None)
            return self._yt

    def _call(self, fn):
        """Executa fn(yt); se a sessão logada falhar, tenta anônimo."""
        try:
            return fn(self.yt)
        except Exception:
            if self.headers is None:
                raise
            return fn(self._make(None))

    @staticmethod
    def _track(item):
        thumbs = item.get("thumbnails") or item.get("thumbnail") or []
        artists = ", ".join(a["name"] for a in item.get("artists") or []
                            if a.get("name"))
        return {
            "videoId": item["videoId"],
            "title": item.get("title") or "?",
            "artist": artists,
            "duration": item.get("duration") or item.get("length") or "",
            "thumb": thumbs[-1]["url"] if thumbs else None,
        }

    def search(self, query):
        res = []
        try:  # tenta primeiro só "músicas"
            res = self._call(lambda yt: yt.search(query, filter="songs", limit=10))
        except Exception:  # noqa: BLE001
            pass
        if not res:
            res = self._call(lambda yt: yt.search(query, limit=20))
        out, seen = [], set()
        for item in res:
            vid = item.get("videoId")
            if vid and vid not in seen and \
                    item.get("resultType", "song") in ("song", "video"):
                seen.add(vid)
                out.append(self._track(item))
        return out[:10]

    def radio(self, video_id):
        """Fila automática ('rádio') a partir de uma música."""
        wp = self._call(lambda yt: yt.get_watch_playlist(videoId=video_id, limit=30))
        return [self._track(t) for t in wp.get("tracks", []) if t.get("videoId")]


# ---------------------------------------------------------------------- UI
CSS = b"""
#ytm-popup { background: #1b1b1f; border: 1px solid #33333a; border-radius: 10px; }
#ytm-popup label { color: #eeeeee; }
#ytm-popup .dim { color: #9a9aa3; font-size: 9pt; }
#ytm-popup .title { font-weight: bold; font-size: 10.5pt; }
#ytm-popup .brand { color: #ff4e45; font-weight: bold; font-size: 8.5pt; }
#ytm-popup button { background: transparent; border: none; box-shadow: none;
                    color: #eeeeee; padding: 4px; min-height: 0; min-width: 0; }
#ytm-popup button:hover { background: #2c2c33; border-radius: 6px; }
#ytm-popup button.play { background: #ff4e45; border-radius: 99px; padding: 8px; }
#ytm-popup button.play:hover { background: #ff6a62; }
#ytm-popup button.play image { color: #ffffff; }
#ytm-popup image { color: #eeeeee; }
#ytm-popup entry { background: #2a2a31; color: #eeeeee; border: 1px solid #3a3a42;
                   border-radius: 8px; caret-color: #ff4e45; }
#ytm-popup scale trough { background: #3a3a42; min-height: 4px; border-radius: 2px; }
#ytm-popup scale highlight { background: #ff4e45; border-radius: 2px; }
#ytm-popup scale slider { background: #ffffff; min-width: 10px; min-height: 10px;
                          border-radius: 99px; border: none; margin: -4px; }
#ytm-popup list, #ytm-popup row { background: transparent; }
#ytm-popup row:hover { background: #2c2c33; border-radius: 6px; }
#ytm-popup .art { background: #2a2a31; border-radius: 6px; }
"""


def icon_button(icon, tooltip, cb, size=Gtk.IconSize.LARGE_TOOLBAR):
    btn = Gtk.Button()
    img = Gtk.Image.new_from_icon_name(icon, size)
    btn.add(img)
    btn.set_tooltip_text(tooltip)
    btn.set_relief(Gtk.ReliefStyle.NONE)
    btn.set_can_focus(False)
    btn.connect("clicked", lambda *_: cb())
    btn.image = img
    return btn


def load_pixbuf(url, size):
    with urllib.request.urlopen(url, timeout=10) as r:
        data = r.read()
    loader = GdkPixbuf.PixbufLoader()
    loader.write(data)
    loader.close()
    pb = loader.get_pixbuf()
    w, h = pb.get_width(), pb.get_height()
    side = min(w, h)  # recorte quadrado central (thumbs de vídeo são 16:9)
    pb = pb.new_subpixbuf((w - side) // 2, (h - side) // 2, side, side)
    return pb.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)


class Popup(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="YT Music")
        self.app = app
        self.set_name("ytm-popup")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_default_size(360, -1)
        screen = Gdk.Screen.get_default()
        if screen.get_rgba_visual() and screen.is_composited():
            self.set_visual(screen.get_rgba_visual())
        prov = Gtk.CssProvider()
        prov.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.connect("delete-event", lambda *_: self.hide() or True)
        self.connect("key-press-event", self.on_key)
        self.connect("focus-out-event", self.on_focus_out)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_border_width(12)
        self.add(root)

        # topo
        top = Gtk.Box(spacing=4)
        brand = Gtk.Label(label="▶ YT MUSIC", xalign=0)
        brand.get_style_context().add_class("brand")
        top.pack_start(brand, False, False, 0)
        self.account_lbl = Gtk.Label(label="anônimo", xalign=0)
        self.account_lbl.get_style_context().add_class("dim")
        self.account_lbl.set_tooltip_text(
            "Sem login: faça login no YouTube pelo Firefox para usar sua conta")
        top.pack_start(self.account_lbl, True, True, 6)
        top.pack_end(icon_button("system-shutdown-symbolic", "Fechar o player",
                                 app.quit_all, Gtk.IconSize.MENU), False, False, 0)
        top.pack_end(icon_button("window-minimize-symbolic", "Esconder (Esc)",
                                 self.hide, Gtk.IconSize.MENU), False, False, 0)
        root.pack_start(top, False, False, 0)

        # capa + info
        mid = Gtk.Box(spacing=12)
        self.art = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic",
                                                Gtk.IconSize.DIALOG)
        self.art.set_size_request(ART_SIZE, ART_SIZE)
        self.art.get_style_context().add_class("art")
        mid.pack_start(self.art, False, False, 0)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_valign(Gtk.Align.CENTER)
        self.title_lbl = Gtk.Label(label="Nada tocando", xalign=0)
        self.title_lbl.set_ellipsize(3)  # END
        self.title_lbl.set_max_width_chars(28)
        self.title_lbl.get_style_context().add_class("title")
        self.artist_lbl = Gtk.Label(label="Pesquise uma música abaixo", xalign=0)
        self.artist_lbl.set_ellipsize(3)
        self.artist_lbl.set_max_width_chars(30)
        self.artist_lbl.get_style_context().add_class("dim")
        info.pack_start(self.title_lbl, False, False, 0)
        info.pack_start(self.artist_lbl, False, False, 0)

        self.progress = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 1)
        self.progress.set_draw_value(False)
        self.progress.set_can_focus(False)
        self.progress.connect("change-value", self.on_seek)
        info.pack_start(self.progress, False, False, 0)
        times = Gtk.Box()
        self.pos_lbl = Gtk.Label(label="0:00")
        self.dur_lbl = Gtk.Label(label="0:00")
        for lbl in (self.pos_lbl, self.dur_lbl):
            lbl.get_style_context().add_class("dim")
        times.pack_start(self.pos_lbl, False, False, 0)
        times.pack_end(self.dur_lbl, False, False, 0)
        info.pack_start(times, False, False, 0)
        mid.pack_start(info, True, True, 0)
        root.pack_start(mid, False, False, 0)

        # controles
        ctr = Gtk.Box(spacing=2)
        ctr.pack_start(icon_button("media-skip-backward-symbolic", "Anterior",
                                   app.prev), False, False, 0)
        ctr.pack_start(icon_button("media-seek-backward-symbolic",
                                   f"Voltar {SEEK_STEP}s",
                                   lambda: app.seek_rel(-SEEK_STEP)), False, False, 0)
        self.play_btn = icon_button("media-playback-start-symbolic",
                                    "Tocar/Pausar (Espaço)", app.toggle_pause)
        self.play_btn.get_style_context().add_class("play")
        ctr.pack_start(self.play_btn, False, False, 0)
        ctr.pack_start(icon_button("media-seek-forward-symbolic",
                                   f"Avançar {SEEK_STEP}s",
                                   lambda: app.seek_rel(SEEK_STEP)), False, False, 0)
        ctr.pack_start(icon_button("media-skip-forward-symbolic", "Próxima",
                                   app.next), False, False, 0)

        self.vol_icon = Gtk.Image()
        self.volume = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 2)
        self.volume.set_draw_value(False)
        self.volume.set_can_focus(False)
        self.volume.set_size_request(90, -1)
        self.volume.set_value(app.cfg["volume"])
        self.volume.connect("value-changed", self.on_volume)
        self._update_vol_icon()
        ctr.pack_end(self.volume, False, False, 0)
        ctr.pack_end(self.vol_icon, False, False, 4)
        root.pack_start(ctr, False, False, 0)

        # busca
        self.entry = Gtk.SearchEntry()
        self.entry.set_placeholder_text("Pesquisar música, artista ou link…")
        self.entry.connect("activate", self.on_search)
        root.pack_start(self.entry, False, False, 0)

        self.status = Gtk.Label(xalign=0)
        self.status.get_style_context().add_class("dim")
        self.status.set_no_show_all(True)
        self.status.set_line_wrap(True)
        self.status.set_max_width_chars(44)
        root.pack_start(self.status, False, False, 0)

        self.results = Gtk.ListBox()
        self.results.set_selection_mode(Gtk.SelectionMode.NONE)
        self.results.set_activate_on_single_click(True)
        self.results.connect("row-activated", self.on_result)
        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.set_min_content_height(220)
        self.scroll.add(self.results)
        self.scroll.set_no_show_all(True)
        root.pack_start(self.scroll, False, False, 0)

        root.show_all()

    # -- helpers
    def set_account(self, login):
        if login:
            name = login["browser"].capitalize()
            self.account_lbl.set_markup(
                f'<span foreground="#3ecf8e">●</span> conta do {name}')
            self.account_lbl.set_tooltip_text(
                f"Usando o login do YouTube do {name} (Premium, se a conta tiver)")
        else:
            self.account_lbl.set_text("anônimo")
            self.account_lbl.set_tooltip_text(
                "Sem login: faça login no YouTube pelo Firefox para usar sua conta")

    def set_status(self, text):
        self.status.set_text(text or "")
        self.status.set_visible(bool(text))

    def place_near_pointer(self):
        display = Gdk.Display.get_default()
        _, px, py = display.get_default_seat().get_pointer().get_position()[0:3]
        mon = display.get_monitor_at_point(px, py).get_workarea()
        w, h = self.get_size()
        x = min(max(px - w // 2, mon.x + 8), mon.x + mon.width - w - 8)
        y = min(max(py - 20, mon.y + 8), mon.y + mon.height - h - 8)
        self.move(x, y)

    def toggle(self):
        if self.get_visible() and self.is_active():
            self.hide()
            return
        if not self.get_visible():
            self.place_near_pointer()
        self.show()
        ts = Gtk.get_current_event_time()
        try:
            gi.require_version("GdkX11", "3.0")
            from gi.repository import GdkX11
            gw = self.get_window()
            if isinstance(gw, GdkX11.X11Window):
                ts = GdkX11.x11_get_server_time(gw)
        except (ValueError, ImportError):
            pass
        self.present_with_time(ts)
        self.entry.grab_focus()

    # -- eventos
    def on_key(self, _w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.hide()
            return True
        if ev.keyval == Gdk.KEY_space and not self.entry.has_focus():
            self.app.toggle_pause()
            return True
        return False

    def on_focus_out(self, *_):
        if self.app.cfg.get("hide_on_focus_out"):
            GLib.timeout_add(150, lambda: (not self.is_active()) and self.hide())
        return False

    def on_seek(self, _scale, _scroll, value):
        self.app.seek_abs(value)
        return False

    def on_volume(self, scale):
        self.app.set_volume(int(scale.get_value()))
        self._update_vol_icon()

    def _update_vol_icon(self):
        v = self.volume.get_value()
        name = "muted" if v == 0 else "low" if v < 34 else \
            "medium" if v < 67 else "high"
        self.vol_icon.set_from_icon_name(f"audio-volume-{name}-symbolic",
                                         Gtk.IconSize.MENU)

    def on_search(self, entry):
        q = entry.get_text().strip()
        if not q:
            return
        if q.startswith("http://") or q.startswith("https://"):
            self.app.play_url(q)
            return
        self.set_status("Pesquisando…")
        run_bg(self.app.music.search, self.show_results, q)

    def show_results(self, tracks, err):
        for child in self.results.get_children():
            self.results.remove(child)
        if err:
            self.set_status(f"Erro na busca: {err}")
            return
        if not tracks:
            self.set_status("Nada encontrado.")
            return
        self.set_status("")
        for t in tracks:
            row = Gtk.ListBoxRow()
            row.track = t
            box = Gtk.Box(spacing=8)
            box.set_border_width(4)
            img = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic",
                                               Gtk.IconSize.DND)
            img.set_size_request(36, 36)
            box.pack_start(img, False, False, 0)
            txt = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            a = Gtk.Label(label=t["title"], xalign=0)
            a.set_ellipsize(3)
            a.set_max_width_chars(34)
            b = Gtk.Label(label=" · ".join(x for x in (t["artist"], t["duration"]) if x),
                          xalign=0)
            b.set_ellipsize(3)
            b.set_max_width_chars(38)
            b.get_style_context().add_class("dim")
            txt.pack_start(a, False, False, 0)
            txt.pack_start(b, False, False, 0)
            box.pack_start(txt, True, True, 0)
            row.add(box)
            self.results.add(row)
            if t["thumb"]:
                self.app.load_art(t["thumb"], 36, img.set_from_pixbuf)
        self.results.show_all()
        self.scroll.show()
        GLib.idle_add(self.keep_on_screen)

    def keep_on_screen(self):
        """Sobe a janela se ela crescer além da borda inferior da tela."""
        display = Gdk.Display.get_default()
        x, y = self.get_position()
        w, h = self.get_preferred_size()[1].width, self.get_preferred_size()[1].height
        mon = display.get_monitor_at_point(x + w // 2, y).get_workarea()
        ny = min(y, mon.y + mon.height - h - 8)
        if ny != y:
            self.move(x, max(ny, mon.y + 8))
        return False

    def on_result(self, _lb, row):
        self.scroll.hide()
        self.entry.set_text("")
        self.resize(360, 1)
        self.app.play_search_result(row.track)

    # -- atualizações vindas do player
    def show_track(self, t):
        self.title_lbl.set_text(t["title"])
        self.artist_lbl.set_text(t["artist"] or "")
        self.set_tooltip_text(f'{t["title"]} — {t["artist"]}')
        self.art.set_from_icon_name("audio-x-generic-symbolic", Gtk.IconSize.DIALOG)
        if t.get("thumb"):
            self.app.load_art(t["thumb"], ART_SIZE, self.art.set_from_pixbuf)

    def set_paused(self, paused):
        icon = "media-playback-start-symbolic" if paused else \
            "media-playback-pause-symbolic"
        self.play_btn.image.set_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR)

    def set_position(self, pos, dur):
        if dur:
            self.progress.set_range(0, max(dur, 1))
            self.dur_lbl.set_text(fmt_time(dur))
        if pos is not None:
            self.progress.set_value(pos)
            self.pos_lbl.set_text(fmt_time(pos))


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.cfg = load_config()
        self.win = None
        self.mpv = None
        self.music = Music(self.cfg.get("language", "pt"))
        self.queue, self.index = [], -1
        self.pos, self.dur, self.paused = 0, 0, True
        self.art_cache = {}
        self._pending_radio = None
        self.errors = 0

    # Gtk.Application garante instância única: rodar o comando de novo chega
    # aqui na instância que já está aberta.
    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        if self.win is None:
            self.hold()
            self.mpv = Mpv(self.on_mpv, self.cfg["volume"])
            self.win = Popup(self)
            self.refresh_login()
            # cookies do navegador mudam com o tempo: relê a cada 3 h
            GLib.timeout_add_seconds(3 * 3600, self.refresh_login)
        cmd = args[0] if args else "toggle"
        actions = {"toggle": self.win.toggle, "show": self.win.toggle,
                   "play-pause": self.toggle_pause, "next": self.next,
                   "prev": self.prev, "quit": self.quit_all}
        actions.get(cmd, self.win.toggle)()
        return 0

    def refresh_login(self):
        run_bg(detect_login, self._got_login,
               self.cfg.get("cookies_from_browser", "auto"))
        return True

    def _got_login(self, login, err):
        if err:
            login = None
        self.music.set_auth(login["headers"] if login else None)
        self.mpv.use_cookies(login["cookie_file"] if login else None)
        self.win.set_account(login)

    # -- player
    def on_mpv(self, msg):
        ev = msg["event"]
        if ev == "property-change":
            name, data = msg.get("name"), msg.get("data")
            if name == "time-pos":
                self.pos = data or 0
                self.win.set_position(self.pos, None)
            elif name == "duration":
                self.dur = data or 0
                self.win.set_position(None, self.dur)
            elif name == "pause":
                self.paused = bool(data) or self.index < 0
                self.win.set_paused(self.paused)
            elif name == "paused-for-cache" and data:
                self.win.set_status("Carregando…")
        elif ev == "file-loaded":
            self.errors = 0
            self.win.set_status("")
            self.win.set_paused(False)
        elif ev == "end-file":
            reason = msg.get("reason")
            if reason == "eof":
                self.next()
            elif reason == "error":
                self.errors += 1
                if self.errors >= 3:
                    self.win.set_status(
                        "O YouTube recusou a reprodução. Faça login no YouTube "
                        "pelo Firefox (ou rode 'ytm-popup --reset').")
                    self.win.set_paused(True)
                    return
                self.win.set_status("Não foi possível tocar esta faixa; pulando…")
                GLib.timeout_add(1500, lambda: self.next() and False)

    def _play_index(self, i):
        if not (0 <= i < len(self.queue)):
            return
        self.index = i
        t = self.queue[i]
        self.win.show_track(t)
        self.win.set_position(0, 0)
        self.win.set_status("Carregando…")
        self.mpv.command("loadfile",
                         f"https://music.youtube.com/watch?v={t['videoId']}",
                         "replace")
        self.mpv.set("pause", False)

    def play_search_result(self, track):
        self.errors = 0
        self.queue, self.index = [track], -1
        self._play_index(0)
        self._pending_radio = track["videoId"]
        run_bg(self.music.radio, self._got_radio, track["videoId"])

    def _got_radio(self, tracks, err):
        if err or not tracks or not self.queue:
            return
        if self.queue[0]["videoId"] != self._pending_radio:
            return  # o usuário já escolheu outra música
        first = self.queue[0]
        self.queue = [first] + [t for t in tracks if t["videoId"] != first["videoId"]]

    def play_url(self, url):
        vid = None
        if "v=" in url:
            vid = url.split("v=", 1)[1].split("&")[0]
        elif "youtu.be/" in url:
            vid = url.split("youtu.be/", 1)[1].split("?")[0]
        if vid:
            self.play_search_result({"videoId": vid, "title": "Carregando…",
                                     "artist": "", "duration": "", "thumb":
                                     f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"})
            self.win.entry.set_text("")
        else:
            self.win.set_status("Link não reconhecido.")

    def toggle_pause(self):
        if self.index >= 0:
            self.mpv.command("cycle", "pause")

    def next(self):
        if self.index + 1 < len(self.queue):
            self._play_index(self.index + 1)
        return False

    def prev(self):
        if self.pos > 5 or self.index <= 0:
            self.seek_abs(0)
        else:
            self._play_index(self.index - 1)

    def seek_rel(self, s):
        if self.index >= 0:
            self.mpv.command("seek", s, "relative")

    def seek_abs(self, s):
        if self.index >= 0:
            self.mpv.command("seek", max(0, s), "absolute")

    def set_volume(self, v):
        self.cfg["volume"] = v
        self.mpv.set("volume", v)
        GLib.timeout_add(800, self._save_cfg_once)

    def _save_cfg_once(self):
        save_config(self.cfg)
        return False

    def load_art(self, url, size, setter):
        key = (url, size)
        if key in self.art_cache:
            setter(self.art_cache[key])
            return

        def done(pb, err):
            if pb is not None:
                self.art_cache[key] = pb
                setter(pb)
        run_bg(load_pixbuf, done, url, size)

    def quit_all(self):
        save_config(self.cfg)
        if self.mpv:
            self.mpv.quit()
        self.quit()


def main():
    app = App()
    try:
        return app.run(sys.argv)
    finally:
        if app.mpv and app.mpv.proc.poll() is None:
            app.mpv.proc.kill()


if __name__ == "__main__":
    sys.exit(main())
