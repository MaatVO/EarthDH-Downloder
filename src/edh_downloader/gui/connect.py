"""The app on your computer: the interface, with one tab per server (work done there over SSH) and "This computer".

Served on 127.0.0.1 of your computer. A bar of tabs on top: one per SSH connection, "This computer" and "+" for the
connect form. Every tab is this same interface (always the version you are running) bound to a backend: the
server's agent over SSH, or this computer. Nothing is stored on your computer except the list of remembered
servers (no passwords). Closing the browser tab quits the app (the browser asks first); download jobs are
separate processes and keep running, here or on the servers, and show up again in Jobs next time.
"""

import asyncio
import json
import importlib
import secrets
import socket
import threading
import time

import asyncssh
from fastapi.responses import PlainTextResponse
from nicegui import Client, app, ui

from .. import __version__, backend, remote, sshconfig, update
from . import logo, tour
from .theme import apply_theme

# one user, one app: the open connections outlive page reloads
STATE: dict = {"sessions": {}, "local": False, "active": "new", "pages": set(), "seq": 0}
QUIT_GRACE = 8.0  # seconds without any open page before the app quits (a reload reconnects well within this)
# the browser's own "Leave page?" prompt; app-initiated navigation sets window.edhQuiet first
LEAVE_JS = """<script>
window.addEventListener('beforeunload', e => { if (!window.edhQuiet) { e.preventDefault(); e.returnValue = ''; } });
</script>"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _preset_label(e: sshconfig.Entry) -> str:
    target = e.hostname if e.hostname and e.hostname != e.alias else ""
    return e.alias + (f"  ·  {target}" if target else "")


def _resolved(e: sshconfig.Entry | None) -> str:
    if not e:
        return ""
    where = f"{e.user + '@' if e.user else ''}{e.hostname or e.alias}{':' + str(e.port) if e.port else ''}"
    return f"~/.ssh/config → {where}" + (f" via {e.proxy}" if e.proxy else "")


def build_connect_page():
    apply_theme()
    ui.add_head_html(LEAVE_JS + "<style>body{overflow:hidden}</style>")
    sessions = STATE["sessions"]
    panes: dict[str, ui.element] = {}
    dialogs = ui.element("div")

    @ui.refreshable
    def tabs():
        def tab(key, icon, label, closable=True, tip=""):
            active = STATE["active"] == key
            with ui.element("div").classes("edh-ctab" + (" edh-ctab-active" if active else "")).on(
                    "click", lambda key=key: select(key)) as t:
                ui.icon(icon, size="16px")
                ui.label(label).classes("ellipsis")
                if closable:
                    ui.button(icon="close", on_click=lambda key=key: ask_close(key)).props(
                        "flat round dense size=xs").tooltip("close")
            if tip:
                t.tooltip(tip)

        if STATE["local"]:
            tab("local", "laptop", "This computer", tip="the UI running on this computer")
        for sid, sess in sessions.items():
            tab(sid, "dns", sess.server.name, tip=f"edh-dl v{sess.version} on {sess.server.name}")
        tab("new", "add", "Connect" if not (sessions or STATE["local"]) else "", closable=False,
            tip="connect to a server, or use this computer")

    with ui.element("div").classes("edh-remote-bar"):
        ui.html(logo.img(small=True), sanitize=False)
        with ui.row().classes("edh-ctabs gap-1 no-wrap items-center"):
            tabs()
        ui.space()
        ui.button("Tour", icon="school", on_click=lambda: start_tour()).props(
            "flat dense no-caps size=sm color=grey-5").tooltip("a guided tour of this page")
        ui.button("Quit", icon="power_settings_new", on_click=lambda: ask_quit()).props(
            "flat dense no-caps size=sm color=grey-5").tooltip(
            "closes the app and its connections; download jobs keep running")

    def frame(key, url):  # under ``frames`` whatever the calling context (a button, a dialog …)
        with frames:
            panes[key] = ui.element("iframe").props(
                f'src="{url}" allow="clipboard-write" id="edh-frame-{key}"').classes("edh-remote-frame")

    with ui.element("div").classes("edh-pane") as form_pane:
        panes["new"] = form_pane
        _connect_form(on_connected=lambda sess: add_session(sess), open_local=lambda: open_local())
    frames = ui.element("div")
    if STATE["local"]:
        frame("local", "/local")
    for sid in sessions:
        frame(sid, f"/ui/{sid}")

    def select(key):
        if key not in panes:
            key = "new"
        STATE["active"] = key
        for k, el in panes.items():
            el.set_visibility(k == key)
        tabs.refresh()

    def add_session(sess):
        STATE["seq"] += 1
        sid = f"s{STATE['seq']}"
        sessions[sid] = sess
        frame(sid, f"/ui/{sid}")
        select(sid)

    def open_local():
        if not STATE["local"]:
            STATE["local"] = True
            frame("local", "/local")
        select("local")

    async def close(key):
        if key == "local":
            STATE["local"] = False
        elif key in sessions:
            await sessions.pop(key).close()
        if key in panes:
            panes.pop(key).delete()
        if STATE["active"] == key:
            STATE["active"] = next(reversed(panes), "new") if len(panes) > 1 else "new"
        select(STATE["active"])

    def confirm(title, text, label, fn, color="negative"):
        with dialogs, ui.dialog() as dlg, ui.card().classes("edh-card gap-3").style("width:min(480px,94vw)"):
            ui.label(title).classes("edh-title")
            ui.markdown(text).classes("text-body2")
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dlg.close).props("flat no-caps")

                async def go():
                    dlg.close()
                    await fn()

                ui.button(label, on_click=go).props(f"unelevated no-caps color={color}")
        dlg.on("hide", lambda: dlg.delete())
        dlg.open()

    def ask_close(key):
        if key == "local":
            confirm("Close This computer?", "Download jobs started here keep running in the background; open "
                    "**This computer** again to see them in Jobs.", "Close", lambda: close(key), "primary")
        elif key in sessions:
            name = sessions[key].server.name
            confirm(f"Disconnect from {name}?", "Closes the SSH connection. Download jobs keep running on the "
                    "server; connect again to see them in Jobs.", "Disconnect", lambda: close(key))

    def ask_quit():
        async def quit_():
            ui.run_javascript("window.edhQuiet = true")
            for el in panes.values():
                el.set_visibility(False)
            with form_pane.clear():
                with ui.column().classes("w-full items-center gap-2 py-16"):
                    ui.label("Weather Dataset Downloader is closed").classes("edh-title")
                    ui.label("You can close this tab. Download jobs keep running.").classes("edh-muted")
            form_pane.set_visibility(True)
            await asyncio.sleep(0.3)
            await _quit()

        n = len(sessions)
        conns = f"its {n} SSH connection{'s' if n != 1 else ''}" if n else "the app"
        confirm("Quit Weather Dataset Downloader?", f"Closes {conns}. Download jobs keep running in the background (here and on "
                "the servers) and show up again in Jobs when you open the app.", "Quit", quit_)

    def watch():  # a connection that dropped (network, server reboot): close its tab and say so
        for sid, sess in list(sessions.items()):
            if sess.closed:
                ui.notify(f"connection to {sess.server.name} closed; download jobs keep running there. "
                          "Connect again to see them.", type="warning", timeout=0, close_button=True)
                asyncio.create_task(close(sid))
        if update.UPDATED and not STATE.get("update_told"):  # downloaded in the background since the start
            STATE["update_told"] = True
            ui.notify(f"Weather Dataset Downloader {update.UPDATED} is downloaded. It starts next time you open the app; "
                      "download jobs are not affected.", type="info", timeout=0, close_button=True)

    ui.timer(2.0, watch)
    select(STATE["active"])

    # ---- guided tour: this page explains connecting; the app tab opened next explains the rest
    def app_tabs():
        return [k for k in panes if k != "new"]

    def start_tour():
        if STATE["active"] != "new":  # on a server's (or This computer's) tab: the tour of that tab
            app_tour(STATE["active"])
            return
        tour.run("connect", tour.connect_steps(apps_open=bool(app_tabs())))

    def app_tour(key):
        select(key)
        ui.run_javascript(f"""(() => {{
            const w = document.getElementById('edh-frame-{key}')?.contentWindow;
            if (w && w.edhRequestTour) w.edhRequestTour();
        }})()""")

    def ended(name, result):
        if name != "connect":
            return
        if result == "done" and app_tabs():
            app_tour(app_tabs()[-1])
        elif result == "done":
            tour.set_stage(tour.APP)  # the next app tab opened carries on
        elif tour.stage() != tour.DONE:
            tour.set_stage(tour.DONE)

    tour.install(ended)
    if tour.stage() == tour.WELCOME:
        tour.welcome(on_start=start_tour, on_skip=lambda: tour.set_stage(tour.DONE), connect=True,
                     replay="Tour in the top bar")


async def _quit():
    for sess in list(STATE["sessions"].values()):
        await sess.close()
    STATE["sessions"].clear()
    app.shutdown()


def _track_page(client):
    """Quit when the last app page is gone for QUIT_GRACE seconds (browser tab closed; reloads come back)."""
    pages = STATE["pages"]

    async def gone():
        pages.discard(client.id)
        await asyncio.sleep(QUIT_GRACE)
        if not pages:
            print("the app page was closed: quitting (download jobs keep running).", flush=True)
            await _quit()

    pages.add(client.id)
    client.on_connect(lambda: pages.add(client.id))
    client.on_disconnect(gone)


def _connect_form(on_connected, open_local):
    host_ = ui.element("div")
    servers = remote.saved()
    first = servers[0] if servers else remote.Server(host="")
    presets = {e.alias: e for e in sshconfig.hosts()}
    form = {"host": first.host, "user": first.user, "port": first.port or None, "key_file": first.key_file,
            "cmd": first.cmd, "label": first.label, "auth": "key", "password": "", "passphrase": "",
            "remember": True}

    with ui.column().classes("w-full max-w-[1100px] mx-auto gap-5 px-4 py-8"):
        with ui.row().classes("items-center gap-3 no-wrap"):
            ui.html(logo.img(), sanitize=False)
            with ui.column().classes("gap-0"):
                ui.label("Weather Dataset Downloader").classes("edh-brand")
                ui.label(f"v{__version__} · connect to the server that downloads and stores your data").classes(
                    "edh-muted text-caption")
        with ui.row().classes("w-full gap-5 items-start no-wrap max-md:flex-wrap"):
            with ui.card().classes("edh-card grow min-w-0 edh-tour-ssh"):
                ui.label("Connect over SSH").classes("edh-title")
                ui.label("Jobs, downloads, files, API keys and quota all live on the server. This window only "
                         "shows its interface.").classes("edh-muted text-body2")
                if presets:
                    def pick(e):
                        if e.value in presets and e.value != form["host"]:  # asyncssh applies the alias's config
                            form.update(host=e.value, user="", port=None, key_file="", label="")

                    ui.select({a: _preset_label(x) for a, x in presets.items()}, label="From ~/.ssh/config",
                              with_input=True, on_change=pick).props("outlined dense options-dense").classes(
                        "w-full edh-tour-preset").bind_value_from(form, "host", lambda h: h if h in presets else None)
                with ui.row().classes("w-full gap-3 no-wrap max-sm:flex-wrap edh-tour-host"):
                    ui.input("Server", placeholder="hpc.example.org or an ~/.ssh/config alias").bind_value(
                        form, "host").classes("grow")
                    ui.input("User", placeholder="from config").bind_value(form, "user").classes("w-40")
                    ui.number("Port", min=1, max=65535, format="%d", placeholder="22").bind_value(
                        form, "port").classes("w-24")
                ui.label().bind_text_from(form, "host", lambda h: _resolved(presets.get((h or "").strip()))).classes(
                    "edh-muted text-caption edh-mono").bind_visibility_from(
                    form, "host", lambda h: (h or "").strip() in presets)
                ui.toggle({"key": "SSH key / agent", "password": "Password"}).bind_value(form, "auth").props(
                    "no-caps unelevated toggle-color=primary").classes("edh-toggle edh-tour-auth")
                with ui.row().classes("w-full gap-3 no-wrap max-sm:flex-wrap") as key_row:
                    ui.input("Key file", placeholder="default: agent, ~/.ssh/id_ed25519, id_rsa …").bind_value(
                        form, "key_file").classes("grow")
                    ui.input("Key passphrase (if any)", password=True, password_toggle_button=True).bind_value(
                        form, "passphrase").classes("w-64")
                key_row.bind_visibility_from(form, "auth", lambda v: v == "key")
                pw = ui.input("Password", password=True, password_toggle_button=True).bind_value(
                    form, "password").classes("w-full")
                pw.bind_visibility_from(form, "auth", lambda v: v == "password")
                with ui.expansion("Advanced").classes("w-full edh-expansion"):
                    with ui.row().classes("w-full gap-3 no-wrap max-sm:flex-wrap"):
                        pass
                        ui.input("Name for this server").bind_value(form, "label").classes("w-56")
                with ui.row().classes("w-full items-center justify-between edh-tour-connect"):
                    ui.switch("Remember this server (never the password)").bind_value(form, "remember")
                    btn = ui.button("Connect", icon="login").props("unelevated no-caps").classes(
                        "edh-grad-btn px-6")
                status = ui.column().classes("w-full gap-0 edh-mono text-caption")

            with ui.column().classes("w-80 max-md:w-full gap-3 shrink-0"):
                with ui.column().classes("w-full gap-3 edh-tour-servers"):
                    ui.label("Servers").classes("edh-subtitle")
                    if not servers:
                        ui.label("Servers you connect to are listed here.").classes("edh-muted text-caption")
                    for s in servers:
                        with ui.card().classes("edh-card w-full gap-1 py-3") as card:
                            with ui.row().classes("w-full items-center justify-between no-wrap"):
                                with ui.column().classes("gap-0 min-w-0"):
                                    ui.label(s.name).classes("edh-mono text-weight-medium ellipsis")
                                    ui.label(f"{s.user + '@' if s.user else ''}{s.host}"
                                             f"{':' + str(s.port) if s.port else ''}").classes(
                                        "edh-muted text-caption ellipsis")
                                with ui.row().classes("gap-0 no-wrap"):
                                    ui.button(icon="login", on_click=lambda s=s: use(s, go=True)).props(
                                        "flat round color=secondary").tooltip("connect")
                                    ui.button(icon="edit", on_click=lambda s=s: use(s)).props("flat round").tooltip(
                                        "fill the form")
                                    ui.button(icon="delete_outline", on_click=lambda s=s, c=card: (
                                        remote.forget(s), c.delete())).props("flat round color=grey-6").tooltip("forget")
                ui.label("Or").classes("edh-subtitle q-mt-sm")
                with ui.card().classes("edh-card w-full gap-2 py-3 edh-tour-local"):
                    with ui.row().classes("w-full items-center justify-between no-wrap"):
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label("This computer").classes("text-weight-medium")
                            ui.label("download here, no server: jobs, files and keys stay on this computer").classes(
                                "edh-muted text-caption")
                        ui.button(icon="laptop", on_click=lambda: open_local()).props(
                            "flat round color=secondary").tooltip("use on this computer")

    def log(msg, cls="edh-muted"):
        with status:
            ui.label(msg).classes(cls)

    def later(fn):  # under host_, not the dialog or card that asked (deleted when it closes)
        with host_:
            ui.timer(0.01, fn, once=True)

    def use(s, go=False):
        form.update(host=s.host, user=s.user, port=s.port or None, key_file=s.key_file, cmd=s.cmd, label=s.label)
        if go:
            later(connect)

    def server() -> remote.Server:
        return remote.Server(host=(form["host"] or "").strip(), user=(form["user"] or "").strip(),
                             port=int(form["port"] or 0), key_file=(form["key_file"] or "").strip(),
                             cmd=(form["cmd"] or "").strip(), label=(form["label"] or "").strip())

    def dialog(title, text, actions, extra=None):
        with host_, ui.dialog() as dlg, ui.card().classes("edh-card gap-3").style("width:min(560px,94vw)"):
            ui.label(title).classes("edh-title")
            ui.markdown(text).classes("text-body2")
            if extra:
                extra()
            with ui.row().classes("w-full justify-end gap-2"):
                ui.button("Cancel", on_click=dlg.close).props("flat no-caps")
                for label, fn in actions:
                    ui.button(label, on_click=lambda fn=fn: (dlg.close(), fn())).props(
                        "unelevated no-caps").classes("edh-grad-btn")
        dlg.on("hide", lambda: dlg.delete())
        dlg.open()

    async def connect(install_ok=False):
        s = server()
        if not s.host:
            ui.notify("enter a server", type="warning")
            return
        status.clear()
        btn.props("loading")
        try:
            sess = await remote.open_session(
                s, password=form["password"] if form["auth"] == "password" else None,
                passphrase=form["passphrase"] if form["auth"] == "key" else None, log=log, install_ok=install_ok)
        except remote.UnknownHost as e:
            def ok(e=e):  # bind: Python unbinds ``e`` when the except block ends
                remote.trust(e.host, e.port, e.key, e.path)
                later(lambda: connect(install_ok))

            dialog("Unknown server", f"First connection to **{e.host}**. Check that its key fingerprint is the "
                   f"expected one:\n\n`{e.algorithm}  {e.fingerprint}`\n\nTrusting it adds it to "
                   f"`{e.path or '~/.ssh/known_hosts'}`, like answering *yes* in ssh.", [("Trust and connect", ok)])
            return
        except remote.SetupNeeded:
            dialog("Set up the server",
                   "The server needs a Python environment for the downloads (numpy, zarr, icechunk …, about "
                   "300 MB from PyPI, once). Only the app's code is sent afterwards (a few hundred KB, when it "
                   "changes). It is created in `~/.local/share/edh-dl` and uses `uv` if the server has it.",
                   [("Set up and connect", lambda: later(lambda: connect(True)))])
            return
        except remote.SetupFailed as e:
            log(str(e), "text-negative")
            return
        except asyncssh.PermissionDenied:
            log("authentication failed: check the user, the key (or its passphrase), or use Password.",
                "text-negative")
            return
        except asyncssh.KeyImportError as e:
            log(f"cannot read the key: {e} (a passphrase-protected key needs its passphrase)", "text-negative")
            return
        except (OSError, asyncssh.Error, RuntimeError, TimeoutError) as e:
            log(f"{type(e).__name__}: {e}", "text-negative")
            return
        finally:
            btn.props(remove="loading")
        if form["remember"]:
            remote.remember(s)
        form["password"] = form["passphrase"] = ""
        status.clear()
        on_connected(sess)

    btn.on_click(lambda: connect())


def _warm_up(delay: float = 1.5):
    """While the connect page is on screen: import the app's interface and the download machinery (pandas, zarr,
    icechunk …, ~1 s), so the first server tab or This computer opens without that wait."""
    time.sleep(delay)  # the connect page first
    for name in ("edh_downloader.gui.app", "edh_downloader.service"):
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - the page that needs it imports it again and reports the problem
            pass


def serve_local(port: int = 0, open_browser: bool = True):
    from .auth import TokenMiddleware

    port = port or _free_port()
    tok = secrets.token_urlsafe(18)
    app.add_middleware(TokenMiddleware, token=tok)

    @ui.page("/")
    def index(client: Client):
        _track_page(client)
        build_connect_page()

    @ui.page("/ui/{sid}")
    def server_ui(sid: str):
        from .app import build_page

        sess = STATE["sessions"].get(sid)
        if not sess or sess.closed:
            ui.label("this connection is closed")
            return
        build_page(sess.backend, embedded=True)

    @ui.page("/local")
    def local():
        from .app import build_page

        build_page(backend.Local(), local=True, embedded=True)

    async def close():
        for sess in list(STATE["sessions"].values()):
            await sess.close()

    app.on_shutdown(close)
    app.on_startup(lambda: threading.Thread(target=_warm_up, name="edh-dl-warm-up", daemon=True).start())
    path = f"/?token={tok}"
    print("Weather Dataset Downloader (connects to a server over SSH)")
    print(f"  http://localhost:{port}{path}")
    print("  Closing the browser tab (or Ctrl+C) quits; download jobs keep running.", flush=True)
    ui.run(host="127.0.0.1", port=port, title="Weather Dataset Downloader", show=path if open_browser else False, reload=False,
           favicon=logo.PNG_DATA_URL, dark=True, show_welcome_message=False, storage_secret=secrets.token_hex(16),
           uvicorn_logging_level="warning")
