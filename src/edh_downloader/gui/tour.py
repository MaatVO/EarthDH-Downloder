"""Guided tour: a first-launch welcome and a spotlight walk-through, from the SSH connection to the download.

Two parts, one per page: the connect page (SSH form, saved servers, This computer) and the app (API key, the five
sections of a new job, the plan, launching, Jobs). The app part runs in the tab of the server (or This computer) the
user opens after the connect part. Elements are found by ``edh-tour-<name>`` marker classes.

Where the tour stands is kept in ``ui.json`` in the config directory of the computer that shows the interface:
``welcome`` (never offered), ``app`` (the connect part is done, the app part runs in the next app tab opened) or
``done`` (finished or skipped; the Tour button replays it).
"""

import html
import json
from typing import Callable

from nicegui import ui

from ..paths import config_dir
from ..util import read_json, write_json_atomic

WELCOME, APP, DONE = "welcome", "app", "done"
EVENT = "edh_tour"  # emitted by the page when a tour ends: {"tour": name, "result": "done" | "skip"}
START_EVENT = "edh_tour_start"  # the connect shell asks an app tab to run its tour


def _file():
    return config_dir() / "ui.json"


def stage() -> str:
    s = (read_json(_file(), {}, kind=dict) or {}).get("tour")
    return s if s in (WELCOME, APP, DONE) else WELCOME


def set_stage(s: str) -> None:
    d = read_json(_file(), {}, kind=dict) or {}
    d["tour"] = s
    write_json_atomic(_file(), d)


# ------------------------------------------------------------------ steps
# el: a selector, or a list: the first, else the first visible fallback; none = a centred card
# click: clicked before the step is shown (a tab), then el is awaited a few seconds
# optional: left out when its element is not on the page

def connect_steps(apps_open: bool) -> list[dict]:
    last = ({"title": "Now the app itself",
             "body": "The tour continues in the tab you have open: API key, choosing the data, the cost, and the "
                     "download.", "next": "Continue"}
            if apps_open else
            {"title": "Now connect",
             "body": "Connect to a server, or choose <b>This computer</b>. The tour continues inside the app: API "
                     "key, choosing the data, the cost, and the download.", "next": "Got it"})
    return [
        {"el": ".edh-tour-ssh", "title": "Connect to your server",
         "body": "Downloads run on a server you reach over SSH. Jobs, files, API keys and quota live there: this "
                 "window only shows the interface."},
        {"el": ".edh-tour-preset", "optional": True, "title": "Hosts from ~/.ssh/config",
         "body": "Pick an alias: its user, port, key and jump host come with it."},
        {"el": ".edh-tour-host", "title": "Or type the address",
         "body": "The server's name or IP address, your user on it, and the port if it is not 22."},
        {"el": ".edh-tour-auth", "title": "Sign in",
         "body": "With your SSH key or agent (add the passphrase if the key has one), or with a password. Passwords "
                 "and passphrases are never saved."},
        {"el": ".edh-tour-connect", "title": "Connect",
         "body": "The first time, you check the server's key fingerprint. If the server has no download "
                 "environment yet, the app offers to set one up in <code>~/.local/share/edh-dl</code> (about "
                 "300 MB, once)."},
        {"el": ".edh-tour-servers", "title": "Saved servers",
         "body": "Servers you connect to are listed here: connect again in one click. Every open connection gets "
                 "its own tab in the top bar."},
        {"el": ".edh-tour-local", "title": "No server?",
         "body": "Use <b>This computer</b> to download here instead. Jobs, files and keys then stay on this "
                 "computer."},
        last,
    ]


NEW = ".edh-tour-tab-new"  # steps on the New job tab bring it up (also when going back from Jobs)


def app_steps(where: str, sample_requests: int, replay_hint: str) -> list[dict]:
    where = html.escape(where)
    return [
        {"title": f"Your workspace on {where}",
         "body": f"This is where you set up a download. Everything here runs on {where}: keys, jobs and files "
                 "stay there. A job takes four moves: add an API key, choose the data, check the cost, launch."},
        {"el": ".edh-tour-tabs", "title": "Three tabs",
         "body": "<b>New job</b> sets up a download. <b>Jobs</b> follows the ones you started. <b>Keys</b> holds "
                 "your API keys for Earth Data Hub and the Climate Data Store."},
        {"el": [".edh-tour-keys-add", ".edh-tour-keys"], "click": ".edh-tour-tab-keys", "title": "Add your API key",
         "body": "Paste a key from the Earth Data Hub API keys page, or a Climate Data Store token from your CDS "
                 "profile (a <code>~/.cdsapirc</code> is found by itself). Keys are stored on the machine that runs "
                 "the downloads and never written into jobs or outputs."},
        {"el": ".edh-tour-quota", "title": "Watch the quota",
         "body": "Earth Data Hub allows 500,000 requests a month, and every source chunk downloaded is one "
                 "request. The app counts only its own requests: calibrate it in Keys against the official quota "
                 "page."},
        {"el": ".edh-tour-source", "click": NEW, "title": "Earth Data Hub or Climate Data Store",
         "body": "Choose where the data comes from. Earth Data Hub reads ERA5 chunks directly. The Climate Data "
                 "Store has every Copernicus dataset, but each request waits in a queue: the app packs your "
                 "selection into as few requests as the CDS allows."},
        {"el": ".edh-tour-dataset", "click": NEW, "title": "1 · Dataset",
         "body": "Pick a collection and a dataset. Its variables, levels, grid and time span load from Earth Data "
                 "Hub."},
        {"el": ".edh-tour-variables", "click": NEW, "title": "2 · Variables and levels",
         "body": "Tick the variables you need. On pressure-level datasets, choose the levels too: each one adds "
                 "requests."},
        {"el": ".edh-tour-time", "click": NEW, "title": "3 · Time",
         "body": "First and last day, and the hours. Keep every hour as stored, or aggregate into steps of N hours, "
                 "days, months or years, with statistics per variable (mean, min, max, sum)."},
        {"el": ".edh-tour-area", "click": NEW, "title": "4 · Area",
         "body": "The whole globe, a box (type it, use a preset or draw it on the map) or a list of points. Boxes "
                 "may cross the 180° meridian."},
        {"el": ".edh-tour-output", "click": NEW, "title": "5 · Output",
         "body": "Where the data goes and in which format: Icechunk, Zarr v3, or both. You can also extend an "
                 "existing Icechunk store in time. For Zarr, compare chunk layouts and choose the compression."},
        {"el": ".edh-tour-summary", "click": NEW, "title": "Know the cost first",
         "body": "The summary follows every change: requests, download size, output size and the share of your "
                 "monthly quota. Nothing is downloaded yet."},
        {"el": ".edh-tour-sample", "click": NEW, "title": "Measure on a sample (optional)",
         "body": f"After you confirm, downloads {sample_requests} real chunks ({sample_requests} requests) to "
                 "measure your line speed and the real output size, then suggests the number of connections."},
        {"el": ".edh-tour-settings", "click": NEW, "title": "Connections and limits",
         "body": "<b>Connections</b>, just above, sets parallel downloads and can change while the job runs. "
                 "<b>Download settings</b> hold the temporary directory, the disk and memory limits, and the quota "
                 "limit (90% by default)."},
        {"el": ".edh-tour-launch", "click": NEW, "title": "Launch",
         "body": "Starts the job in the background. Close the browser or disconnect: it keeps running. "
                 "<b>Download job.json</b> saves the setup for <code>edh-dl run</code> on the command line."},
        {"el": ".edh-tour-jobs", "click": ".edh-tour-tab-jobs", "title": "Follow your jobs",
         "body": "Progress and time left for every job. Pause, resume or cancel, change connections, read the log, "
                 "or clone the setup. An interrupted job resumes from its last committed unit, without downloading "
                 "again."},
        {"click": NEW, "title": "You are ready",
         "body": f"Set up your first job in <b>New job</b>. {replay_hint}", "next": "Finish"},
    ]


# ------------------------------------------------------------------ welcome

ROUTE = (("Connect", "to your server over SSH, or use this computer"),
         ("API key", "add your Earth Data Hub key"),
         ("Choose the data", "dataset, variables, time, area and output"),
         ("Check the cost", "requests, sizes and quota, before anything starts"),
         ("Download", "launch the job, then follow it in Jobs"))


def welcome(on_start: Callable[[], None], on_skip: Callable[[], None], connect: bool, replay: str):
    """The first-launch offer: start the tour, or skip it (``replay`` says where it stays available).
    ``connect``: the tour starts on the connect page; otherwise (edh-dl serve) it skips that part."""
    route = ROUTE if connect else ROUTE[1:]
    with ui.dialog().props("persistent") as dlg, ui.card().classes("edh-card gap-4").style(
            "width:min(520px,94vw)").props('role=dialog aria-labelledby=edh-welcome-title'):
        ui.label("Welcome to EDH Downloader").classes("edh-title").props("id=edh-welcome-title")
        ui.label("A short tour walks you through a whole download, one step at a time. About two minutes."
                 ).classes("text-body2")
        with ui.element("ol").classes("edh-tour-route"):
            for i, (name, what) in enumerate(route, 1):
                with ui.element("li"):
                    ui.label(str(i)).classes("edh-tour-route-n")
                    ui.html(f"<b>{name}</b> <span>{what}</span>", sanitize=False)
        ui.label(f"Skip it now and replay it any time from {replay}.").classes("edh-muted text-caption")
        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Skip", on_click=lambda: (dlg.close(), on_skip())).props("flat no-caps")
            ui.button("Start the tour", icon="play_arrow", on_click=lambda: (dlg.close(), on_start())).props(
                "unelevated no-caps").classes("edh-grad-btn")
    dlg.on("hide", lambda: dlg.delete())
    dlg.open()


# ------------------------------------------------------------------ running

def install(on_end: Callable[[str, str], None]):
    """Add the tour engine to this page; ``on_end(tour, result)`` runs when a tour ends ("done" or "skip")."""
    ui.add_head_html(HEAD)
    ui.on(EVENT, lambda e: on_end(str((e.args or {}).get("tour", "")), str((e.args or {}).get("result", ""))))


def run(name: str, steps: list[dict]):
    ui.run_javascript(f"window.edhTour && window.edhTour.start({json.dumps(name)}, {json.dumps(steps)})")


HEAD = """
<style>
.edh-tour { position: fixed; inset: 0; z-index: 9000; }
.edh-tour-block { position: fixed; inset: 0; }
.edh-tour-spot { position: fixed; border-radius: 8px; pointer-events: none; outline: 2px solid var(--edh-accent);
  outline-offset: 0; box-shadow: 0 0 0 200vmax rgba(0, 0, 0, .62); }
.edh-tour-spot.edh-tour-none { width: 0; height: 0; top: 50%; left: 50%; outline: 0; }
.edh-tour-pop { position: fixed; width: min(360px, calc(100vw - 24px)); background: var(--edh-surface-2);
  color: var(--edh-text); border-radius: 6px; padding: 16px 18px 12px; font-size: 14px; line-height: 1.5;
  box-shadow: 0 8px 28px rgba(0, 0, 0, .45); }
.edh-tour-pop.edh-tour-in { animation: edh-tour-in .16s ease-out; }
@keyframes edh-tour-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
.edh-tour-head { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
.edh-tour-count { font-size: 12px; color: var(--edh-muted); font-variant-numeric: tabular-nums; white-space: nowrap; }
.edh-tour-bar { flex: 1; height: 3px; background: var(--edh-surface); border-radius: 2px; overflow: hidden; }
.edh-tour-bar > div { height: 100%; background: var(--edh-accent); }
.edh-tour-title { font-size: 1rem; font-weight: 500; margin-bottom: 4px; }
.edh-tour-body code { font-family: 'Roboto Mono', monospace; font-size: 12px; background: var(--edh-bg);
  padding: 1px 4px; border-radius: 3px; }
.edh-tour-body b { font-weight: 500; }
.edh-tour-actions { display: flex; align-items: center; gap: 4px; margin-top: 14px; }
.edh-tour-actions .edh-tour-gap { flex: 1; }
.edh-tour-actions button { font: inherit; font-size: 14px; border: 0; border-radius: 3px; padding: 6px 12px;
  cursor: pointer; background: transparent; color: var(--edh-accent); }
.edh-tour-actions button:hover { background: rgba(76, 141, 255, .12); }
.edh-tour-actions .edh-tour-skip { color: var(--edh-muted); padding-left: 0; padding-right: 8px; }
.edh-tour-actions .edh-tour-skip:hover { color: var(--edh-text); background: transparent; }
.edh-tour-actions .edh-tour-next { background: var(--edh-accent); color: var(--edh-bg); font-weight: 500; padding: 6px 16px; }
.edh-tour-actions .edh-tour-next:hover { background: var(--edh-accent); filter: brightness(1.08); }
.edh-tour-actions button:focus-visible { outline: 2px solid var(--edh-accent); outline-offset: 2px; }
.edh-tour-actions button[hidden] { display: none; }
.edh-tour-route { list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; font-size: 14px; }
.edh-tour-route li { display: flex; align-items: baseline; gap: 10px; }
.edh-tour-route b { font-weight: 500; }
.edh-tour-route span { color: var(--edh-muted); }
.edh-tour-route-n { flex: none; width: 20px; height: 20px; border-radius: 50%; background: var(--edh-surface-2);
  color: var(--edh-accent); font-size: 12px; font-weight: 500; display: inline-flex; align-items: center;
  justify-content: center; }
@media (prefers-reduced-motion: reduce) { .edh-tour-pop.edh-tour-in { animation: none; } }
</style>
<script>
window.edhTour = (() => {
  let name = '', steps = [], i = 0, dir = 1, seq = 0, root, spot, pop, target = null, frame = 0, before = null;
  const reduced = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
  const shown = el => { if (!el) return false; const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const find = sel => [].concat(sel || []).map(s => document.querySelector(s)).find(shown) || null;
  const wait = ms => new Promise(r => setTimeout(r, ms));

  function build() {
    root = document.createElement('div');
    root.className = 'edh-tour';
    root.innerHTML = `<div class="edh-tour-block"></div><div class="edh-tour-spot edh-tour-none"></div>
      <div class="edh-tour-pop" role="dialog" aria-modal="true" aria-labelledby="edh-tour-title" aria-describedby="edh-tour-body">
        <div class="edh-tour-head"><div class="edh-tour-count" aria-live="polite"></div><div class="edh-tour-bar"><div></div></div></div>
        <div class="edh-tour-title" id="edh-tour-title"></div>
        <div class="edh-tour-body" id="edh-tour-body"></div>
        <div class="edh-tour-actions">
          <button type="button" class="edh-tour-skip">Skip tour</button><span class="edh-tour-gap"></span>
          <button type="button" class="edh-tour-back">Back</button><button type="button" class="edh-tour-next">Next</button>
        </div></div>`;
    spot = root.querySelector('.edh-tour-spot');
    pop = root.querySelector('.edh-tour-pop');
    root.querySelector('.edh-tour-skip').onclick = () => end('skip');
    root.querySelector('.edh-tour-back').onclick = () => go(i - 1, -1);
    root.querySelector('.edh-tour-next').onclick = () => (i >= steps.length - 1 ? end('done') : go(i + 1, 1));
    document.body.appendChild(root);
    document.addEventListener('keydown', keys, true);
  }

  function keys(e) {
    if (!root) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); end('skip'); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); root.querySelector('.edh-tour-next').click(); }
    else if (e.key === 'ArrowLeft' && i > 0) { e.preventDefault(); go(i - 1, -1); }
    else if (e.key === 'Tab') {  // keep focus inside the card
      const b = [...pop.querySelectorAll('button:not([hidden])')];
      const k = b.indexOf(document.activeElement);
      e.preventDefault();
      b[(k + (e.shiftKey ? -1 : 1) + b.length) % b.length].focus();
    }
  }

  async function go(n, d) {
    if (n < 0 || n >= steps.length) return;
    const my = ++seq;
    i = n; dir = d;
    const s = steps[i];
    if (s.click) { const c = document.querySelector(s.click); if (c) c.click(); }
    const sels = [].concat(s.el || []);
    let el = find(sels[0]);
    if (sels.length && !el && s.click) {  // after a tab switch the content may still be loading from the server
      for (let t = 0; t < 60 && !(el = find(sels[0])); t++) { await wait(50); if (my !== seq) return; }
    }
    if (!el) el = find(sels.slice(1));  // fallbacks, when the preferred element never came
    if (s.el && !el && s.optional) {
      const k = i + dir;
      if (k >= 0 && k < steps.length) return go(k, dir);
      if (k < 0) return go(i + 1, 1);
    }
    target = el;
    if (el) {
      const r = el.getBoundingClientRect();
      el.style.scrollMarginTop = '84px';
      el.scrollIntoView({block: r.height > innerHeight * .6 ? 'start' : 'center', behavior: reduced() ? 'auto' : 'smooth'});
    }
    const total = steps.length;
    root.querySelector('.edh-tour-count').textContent = `${i + 1} of ${total}`;
    root.querySelector('.edh-tour-bar > div').style.width = `${(100 * (i + 1)) / total}%`;
    root.querySelector('.edh-tour-title').textContent = s.title || '';
    root.querySelector('.edh-tour-body').innerHTML = s.body || '';
    root.querySelector('.edh-tour-back').hidden = i === 0;
    root.querySelector('.edh-tour-next').textContent = s.next || (i === total - 1 ? 'Finish' : 'Next');
    pop.classList.remove('edh-tour-in'); void pop.offsetWidth; pop.classList.add('edh-tour-in');
    place();
    root.querySelector('.edh-tour-next').focus({preventScroll: true});
  }

  function place() {
    const vw = innerWidth, vh = innerHeight, m = 12, gap = 12, pad = 6;
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    let x, y;
    if (!target || !target.isConnected || !shown(target)) {
      spot.classList.add('edh-tour-none');
      spot.style.cssText = '';
      x = (vw - pw) / 2; y = (vh - ph) / 2;
    } else {
      const r = target.getBoundingClientRect();
      const t = Math.max(r.top - pad, 0), l = Math.max(r.left - pad, 0);
      const b = Math.min(r.bottom + pad, vh), rt = Math.min(r.right + pad, vw);
      spot.classList.remove('edh-tour-none');
      spot.style.cssText = `top:${t}px;left:${l}px;width:${Math.max(rt - l, 0)}px;height:${Math.max(b - t, 0)}px`;
      const fits = [
        [b + gap + ph <= vh - m, () => [l, b + gap]],
        [t - gap - ph >= m, () => [l, t - gap - ph]],
        [rt + gap + pw <= vw - m, () => [rt + gap, Math.max(t, m)]],
        [l - gap - pw >= m, () => [l - gap - pw, Math.max(t, m)]],
      ].find(([ok]) => ok);
      [x, y] = fits ? fits[1]() : [l, vh - ph - m];  // no room around it: over its lower part
    }
    pop.style.left = `${Math.min(Math.max(x, m), vw - pw - m)}px`;
    pop.style.top = `${Math.min(Math.max(y, m), vh - ph - m)}px`;
  }

  function track() { if (root) { place(); frame = requestAnimationFrame(track); } }

  function end(result) {
    if (!root) return;
    cancelAnimationFrame(frame);
    document.removeEventListener('keydown', keys, true);
    root.remove(); root = null; target = null; seq++;
    if (before && before.isConnected) before.focus({preventScroll: true});
    emitEvent('""" + EVENT + """', {tour: name, result});
  }

  async function start(n, s) {
    if (root) end('skip');
    for (let t = 0; t < 600 && !innerWidth; t++) await wait(100);  // an app tab that is not on screen yet
    name = n; steps = s; before = document.activeElement;
    build();
    track();
    go(0, 1);
  }

  return {start, end, active: () => !!root};
})();
</script>
"""
