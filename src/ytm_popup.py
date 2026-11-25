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


# ------------------------------------------------------------------ log
LOG_FILE = os.path.join(GLib.get_user_data_dir(), "ytm-popup", "player.log")


def log(msg):
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 512 * 1024:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except OSError:
        pass


class _YdlLogger:
    """Manda avisos/erros do yt-dlp para o player.log."""
    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        log("yt-dlp aviso: " + msg)

    def error(self, msg):
        log("yt-dlp erro: " + msg)


def _short_error(err):
    msg = str(err).replace("ERROR: ", "")
    if "] " in msg[:40]:
        msg = msg.split("] ", 1)[1]
    if ": " in msg[:20]:  # remove o "videoId: " do começo
        msg = msg.split(": ", 1)[1]
    return msg.split(". ")[0][:140]


def resolve_stream(video_id, cookie_file):
    """Obtém a URL direta do áudio com yt-dlp.

    Tenta primeiro com a sessão do navegador (Premium) e, se falhar,
    sem login. Retorna dict com url, headers e se usou a conta.
    """
    import yt_dlp
    page = f"https://music.youtube.com/watch?v={video_id}"
    attempts = [cookie_file, None] if cookie_file else [None]
    last = None
    for cf in attempts:
        opts = {"format": "bestaudio/best", "quiet": True, "no_warnings": False,
                "noplaylist": True, "logger": _YdlLogger()}
        if cf:
            opts["cookiefile"] = cf
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(page, download=False)
            if info.get("url"):
                log(f"ok {video_id} formato={info.get('format_id')} "
                    f"conta={'sim' if cf else 'não'}")
                return {"url": info["url"], "logged": bool(cf),
                        "headers": info.get("http_headers") or {}}
            last = "nenhum formato de áudio disponível"
        except Exception as e:  # noqa: BLE001
            last = e
        log(f"falha {video_id} conta={'sim' if cf else 'não'}: {last}")
    raise RuntimeError(_short_error(last))


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
    def load_stream(self, stream):
        headers = dict(stream.get("headers") or {})
        ua = headers.pop("User-Agent", None)
        if ua:
            self.set("user-agent", ua)
        self.set("http-header-fields", [f"{k}: {v}" for k, v in headers.items()])
        self.command("loadfile", stream["url"], "replace")
        self.set("pause", False)

    def __init__(self, on_event, volume):
        self.on_event = on_event
        self.lock = threading.Lock()
        runtime = GLib.get_user_runtime_dir() or "/tmp"
        self.sock_path = os.path.join(runtime, f"ytm-popup-{os.getuid()}.sock")
        try:
            os.unlink(self.sock_path)
        except OSError:
            pass
        cmd = [
            "mpv", "--idle=yes", "--no-video", "--no-terminal", "--ytdl=no",
            "--input-ipc-server=" + self.sock_path,
            f"--volume={volume}", "--volume-max=100",
            "--cache=yes", "--demuxer-max-bytes=50MiB",
        ]
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
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
        # O Marco (gerenciador de janelas do MATE) bloqueia "roubo de foco":
        # marcamos a janela com o horário atual do servidor X para que ele
        # entenda que ela foi aberta agora pelo usuário (via atalho).
        self.realize()
        ts = Gtk.get_current_event_time()
        x11 = None
        try:
            gi.require_version("GdkX11", "3.0")
            from gi.repository import GdkX11
            gw = self.get_window()
            if isinstance(gw, GdkX11.X11Window):
                x11 = gw
                ts = GdkX11.x11_get_server_time(gw)
                gw.set_user_time(ts)
        except (ValueError, ImportError):
            pass
        self.show()
        self.present_with_time(ts)
        if x11 is not None:
            x11.focus(ts)
        self.entry.grab_focus()

    # -- eventos
    def on_key(self, _w, ev):
        k = ev.keyval
        ctrl = ev.state & Gdk.ModifierType.CONTROL_MASK
        alt = ev.state & Gdk.ModifierType.MOD1_MASK
        typing = self.entry.has_focus() and self.entry.get_text()
        if k == Gdk.KEY_Escape:
            self.hide()
        elif k == Gdk.KEY_space and not typing:
            self.app.toggle_pause()
        elif ctrl and k == Gdk.KEY_Right or k == Gdk.KEY_AudioNext:
            self.app.next()
        elif ctrl and k == Gdk.KEY_Left or k == Gdk.KEY_AudioPrev:
            self.app.prev()
        elif alt and k == Gdk.KEY_Right:
            self.app.seek_rel(SEEK_STEP)
        elif alt and k == Gdk.KEY_Left:
            self.app.seek_rel(-SEEK_STEP)
        elif ctrl and k == Gdk.KEY_Up or k == Gdk.KEY_AudioRaiseVolume:
            self.app.change_volume(+5)
        elif ctrl and k == Gdk.KEY_Down or k == Gdk.KEY_AudioLowerVolume:
            self.app.change_volume(-5)
        elif k == Gdk.KEY_AudioPlay:
            self.app.toggle_pause()
        else:
            return False
        return True

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


MPRIS_XML = """
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/><method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/><method name="Previous"/><method name="Pause"/>
    <method name="PlayPause"/><method name="Stop"/><method name="Play"/>
    <method name="Seek"><arg direction="in" name="Offset" type="x"/></method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri"><arg direction="in" name="Uri" type="s"/></method>
    <signal name="Seeked"><arg name="Position" type="x"/></signal>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
  </interface>
</node>
"""
MPRIS_PATH = "/org/mpris/MediaPlayer2"
MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"


class Mpris:
    """Expõe o player via MPRIS: teclas de mídia do teclado (Play/Pause,
    Próxima, Anterior) e o applet de som do MATE passam a controlá-lo."""

    def __init__(self, app):
        self.app = app
        self.conn = None
        self.node = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
        self._last = {}
        Gio.bus_own_name(Gio.BusType.SESSION, "org.mpris.MediaPlayer2.ytmpopup",
                         Gio.BusNameOwnerFlags.NONE, self._on_bus, None, None)

    def _on_bus(self, conn, _name):
        self.conn = conn
        for iface in self.node.interfaces:
            reg = getattr(conn, "register_object_with_closures", None)
            if reg is None:
                conn.register_object(MPRIS_PATH, iface, self._call,
                                     self._get, self._set)
            else:
                reg(MPRIS_PATH, iface, self._call, self._get, self._set)

    # -- métodos
    def _call(self, _conn, _sender, _path, _iface, method, params, inv):
        a = self.app
        if method in ("PlayPause",):
            a.toggle_pause()
        elif method == "Play" and a.paused:
            a.toggle_pause()
        elif method in ("Pause", "Stop") and not a.paused:
            a.toggle_pause()
        elif method == "Next":
            a.next()
        elif method == "Previous":
            a.prev()
        elif method == "Seek":
            a.seek_rel(params.unpack()[0] / 1e6)
        elif method == "SetPosition":
            a.seek_abs(params.unpack()[1] / 1e6)
        elif method == "Raise":
            a.win.toggle() if not a.win.get_visible() else a.win.present()
        elif method == "Quit":
            GLib.idle_add(a.quit_all)
        elif method == "OpenUri":
            a.play_url(params.unpack()[0])
        inv.return_value(None)

    # -- propriedades
    def _props(self, iface):
        a = self.app
        if iface == "org.mpris.MediaPlayer2":
            return {
                "CanQuit": GLib.Variant("b", True),
                "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False),
                "Identity": GLib.Variant("s", "YT Music Popup"),
                "DesktopEntry": GLib.Variant("s", "ytm-popup"),
                "SupportedUriSchemes": GLib.Variant("as", ["https"]),
                "SupportedMimeTypes": GLib.Variant("as", []),
            }
        playing = a.index >= 0
        status = "Stopped" if not playing else ("Paused" if a.paused else "Playing")
        meta = {"mpris:trackid": GLib.Variant("o", "/org/mpris/MediaPlayer2/NoTrack")}
        if playing:
            t = a.queue[a.index]
            meta = {
                "mpris:trackid": GLib.Variant(
                    "o", "/io/github/ytmpopup/track/" +
                    "".join(c if c.isalnum() else "_" for c in t["videoId"])),
                "xesam:title": GLib.Variant("s", t["title"]),
                "xesam:artist": GLib.Variant("as", [t["artist"] or ""]),
                "xesam:url": GLib.Variant(
                    "s", f"https://music.youtube.com/watch?v={t['videoId']}"),
            }
            if a.dur:
                meta["mpris:length"] = GLib.Variant("x", int(a.dur * 1e6))
            if t.get("thumb"):
                meta["mpris:artUrl"] = GLib.Variant("s", t["thumb"])
        return {
            "PlaybackStatus": GLib.Variant("s", status),
            "Rate": GLib.Variant("d", 1.0),
            "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "Metadata": GLib.Variant("a{sv}", meta),
            "Volume": GLib.Variant("d", a.cfg["volume"] / 100.0),
            "Position": GLib.Variant("x", int((a.pos or 0) * 1e6)),
            "CanGoNext": GLib.Variant("b", True),
            "CanGoPrevious": GLib.Variant("b", True),
            "CanPlay": GLib.Variant("b", True),
            "CanPause": GLib.Variant("b", True),
            "CanSeek": GLib.Variant("b", playing),
            "CanControl": GLib.Variant("b", True),
        }

    def _get(self, _conn, _sender, _path, iface, prop):
        return self._props(iface).get(prop)

    def _set(self, _conn, _sender, _path, _iface, prop, value):
        if prop == "Volume":
            v = int(max(0.0, min(1.0, value.unpack())) * 100)
            self.app.win.volume.set_value(v)
        return True

    def changed(self):
        if not self.conn:
            return
        props = self._props(MPRIS_PLAYER)
        props.pop("Position")
        diff = {k: v for k, v in props.items() if self._last.get(k) != v}
        if not diff:
            return
        self._last.update(diff)
        self.conn.emit_signal(
            None, MPRIS_PATH, "org.freedesktop.DBus.Properties",
            "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (MPRIS_PLAYER, diff, [])))


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
        self.cookie_file = None
        self.load_token = 0
        self.stream_cache = {}  # videoId -> (instante, stream)
        self.mpris = None

    # Gtk.Application garante instância única: rodar o comando de novo chega
    # aqui na instância que já está aberta.
    def do_command_line(self, cmdline):
        args = cmdline.get_arguments()[1:]
        cmd = args[0] if args else "toggle"
        if self.win is None and cmd not in ("toggle", "show"):
            return 0  # player não está aberto: nada a fazer
        if self.win is None:
            self.hold()
            self.mpv = Mpv(self.on_mpv, self.cfg["volume"])
            self.win = Popup(self)
            self.refresh_login()
            # cookies do navegador mudam com o tempo: relê a cada 3 h
            GLib.timeout_add_seconds(3 * 3600, self.refresh_login)
            try:
                self.mpris = Mpris(self)
            except Exception as e:  # noqa: BLE001
                log(f"MPRIS indisponível: {e}")
        actions = {"toggle": self.win.toggle, "show": self.win.toggle,
                   "play-pause": self.toggle_pause, "next": self.next,
                   "prev": self.prev, "quit": self.quit_all,
                   "vol-up": lambda: self.change_volume(+5),
                   "vol-down": lambda: self.change_volume(-5)}
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
        self.cookie_file = login["cookie_file"] if login else None
        self.stream_cache.clear()
        self.win.set_account(login)

    # -- player
    def on_mpv(self, msg):
        ev = msg["event"]
        if ev == "property-change":
            name, data = msg.get("name"), msg.get("data")
            if name == "time-pos":
                self.pos = data or 0
                if self.pos >= 1:
                    self.errors = 0  # está tocando de verdade
                self.win.set_position(self.pos, None)
            elif name == "duration":
                self.dur = data or 0
                self.win.set_position(None, self.dur)
                self.mpris_changed()
            elif name == "pause":
                self.paused = bool(data) or self.index < 0
                self.win.set_paused(self.paused)
                self.mpris_changed()
            elif name == "paused-for-cache" and data:
                self.win.set_status("Carregando…")
        elif ev == "file-loaded":
            self.paused = False
            self.win.set_paused(False)
            self.mpris_changed()
            self._prefetch(self.index + 1)
        elif ev == "end-file":
            reason = msg.get("reason")
            if reason == "eof" and self.pos >= 2:
                self.next()
            elif reason in ("eof", "error"):  # terminou sem tocar nada
                self.stream_cache.pop(self._current_id(), None)
                self._track_failed("o mpv não conseguiu reproduzir o áudio "
                                   f"({msg.get('file_error') or reason})")

    def _current_id(self):
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]["videoId"]
        return None

    def _cached(self, vid):
        hit = self.stream_cache.get(vid)
        if hit and time.time() - hit[0] < 3600:
            return hit[1]
        return None

    def _prefetch(self, i):
        if 0 <= i < len(self.queue):
            vid = self.queue[i]["videoId"]
            if not self._cached(vid):
                def done(stream, err):
                    if stream:
                        self.stream_cache[vid] = (time.time(), stream)
                run_bg(resolve_stream, done, vid, self.cookie_file)

    def _play_index(self, i):
        if not (0 <= i < len(self.queue)):
            return
        self.index = i
        t = self.queue[i]
        self.pos = 0
        self.win.show_track(t)
        self.win.set_position(0, 0)
        self.win.set_status("Carregando…")
        self.mpris_changed()
        self.load_token += 1
        token, vid = self.load_token, t["videoId"]
        self.mpv.command("stop")

        def got(stream, err):
            if token != self.load_token:
                return  # usuário já trocou de música
            if err:
                self._track_failed(str(err))
                return
            self.stream_cache[vid] = (time.time(), stream)
            if self.cookie_file and not stream["logged"]:
                self.win.set_status("Tocando sem a conta (a sessão do navegador "
                                    "falhou; veja o player.log)")
            else:
                self.win.set_status("")
            self.mpv.load_stream(stream)

        cached = self._cached(vid)
        if cached:
            got(cached, None)
        else:
            run_bg(resolve_stream, got, vid, self.cookie_file)

    def _track_failed(self, reason):
        self.errors += 1
        log(f"faixa falhou ({self.errors}x seguidas): {reason}")
        if self.errors >= 3 or self.index + 1 >= len(self.queue):
            self.win.set_status(f"Não foi possível tocar: {reason}. "
                                f"Detalhes em {LOG_FILE}")
            self.win.set_paused(True)
            return
        self.win.set_status(f"Pulando ({reason})")
        token = self.load_token
        GLib.timeout_add(1500, lambda: token == self.load_token and self.next())

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

    def change_volume(self, delta):
        v = max(0, min(100, int(self.win.volume.get_value()) + delta))
        self.win.volume.set_value(v)  # dispara set_volume

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
        self.mpris_changed()
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

    def mpris_changed(self):
        if self.mpris:
            self.mpris.changed()

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
