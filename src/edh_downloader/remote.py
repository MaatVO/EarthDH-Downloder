"""Work on a server from your computer over one SSH connection (asyncssh, no ssh binary needed).

The interface runs here; the server runs ``edh-dl agent``, which holds the keys, quota, jobs and data. Nothing big
is uploaded: the server gets a zip with this app's Python code (a few hundred KB, again only when it changes) and
a virtual environment with the numerical packages, installed once. Closing the session stops the agent; download
jobs run detached and keep going.
"""

import asyncio
import json
import os
import shlex
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import asyncssh

from . import __version__, backend, bundle, sshconfig
from .paths import config_dir
from .util import read_json, write_json_atomic

STATE_DIR = "~/.local/share/edh-dl"  # on the server: venv/ and agent/<build>.zip
DEPS = ["aiohttp>=3.9", "icechunk>=2", "numcodecs>=0.15", "numpy>=2", "pandas>=2.2", "xarray>=2024.10", "zarr>=3"]
PROBE = "import aiohttp, icechunk, numcodecs, numpy, pandas, xarray, zarr"


@dataclass
class Server:
    """A remembered server (never a password)."""
    host: str
    user: str = ""
    port: int = 0  # 0: from ~/.ssh/config, else 22
    key_file: str = ""  # empty: agent and default keys (~/.ssh/id_*), as ssh does
    cmd: str = ""  # path of edh-dl on the server; empty: detect
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or (f"{self.user}@{self.host}" if self.user else self.host)


def saved() -> list[Server]:
    return [Server(**s) for s in read_json(config_dir() / "servers.json", []) or []]


def remember(s: Server) -> None:
    lst = [x for x in saved() if (x.host, x.user, x.port) != (s.host, s.user, s.port)]
    write_json_atomic(config_dir() / "servers.json", [asdict(s)] + [asdict(x) for x in lst])
    os.chmod(config_dir() / "servers.json", 0o600)


def forget(s: Server) -> None:
    lst = [x for x in saved() if (x.host, x.user, x.port) != (s.host, s.user, s.port)]
    write_json_atomic(config_dir() / "servers.json", [asdict(x) for x in lst])


class UnknownHost(Exception):
    """The server's host key is not in known_hosts: show ``fingerprint`` and ask."""

    def __init__(self, host, port, key):
        super().__init__(f"unknown host key for {host}")
        self.host, self.port, self.key = host, port, key
        self.fingerprint = key.get_fingerprint("sha256")
        self.algorithm = key.get_algorithm()


class SetupNeeded(Exception):
    """The server has no Python environment for the agent yet; ``setup`` creates it (needs internet there)."""


class SetupFailed(Exception):
    pass


@dataclass
class Session:
    server: Server
    conn: asyncssh.SSHClientConnection
    backend: backend.Remote
    version: str
    started: float = field(default_factory=time.time)

    @property
    def closed(self) -> bool:
        return self.backend.closed

    async def close(self):
        await self.backend.close()
        try:
            self.conn.close()
        except Exception:  # noqa: BLE001
            pass


def trust(host: str, port: int, key) -> None:
    """Append the host key to ~/.ssh/known_hosts (what ssh does when you answer "yes")."""
    kh = Path.home() / ".ssh" / "known_hosts"
    kh.parent.mkdir(mode=0o700, exist_ok=True)
    name = host if port == 22 else f"[{host}]:{port}"
    with open(kh, "a") as f:
        f.write(f"{name} {key.export_public_key().decode().strip()}\n")


async def _connect(s: Server, password: str | None, passphrase: str | None):
    kw = dict(keepalive_interval=30, passphrase=passphrase or None)
    if s.port:  # else ~/.ssh/config or 22
        kw["port"] = s.port
    if s.user:
        kw["username"] = s.user
    if s.key_file:
        kw["client_keys"] = [os.path.expanduser(s.key_file)]
    if password:
        kw["password"] = password
    try:
        return await asyncssh.connect(s.host, **kw)
    except asyncssh.HostKeyNotVerifiable:
        # an alias: known_hosts is keyed by the real name and port (no ProxyJump here: direct reach only)
        e = sshconfig.lookup(s.host)
        host, port = e.hostname or s.host, s.port or e.port or 22
        key = await asyncssh.get_server_host_key(host, port)
        raise UnknownHost(host, port, key) from None


async def _sh(conn, cmd: str, check=False) -> tuple[int, str]:
    r = await conn.run(f"sh -lc {shlex.quote(cmd)}", check=check)
    return r.exit_status or 0, (r.stdout or "").strip()


async def _run(conn, cmd: str, log=None) -> tuple[int, str]:
    """Run a shell command, passing its output lines to ``log`` as they come."""
    proc = await conn.create_process(f"sh -lc {shlex.quote(cmd)} 2>&1", term_type=None)
    lines = []
    async for line in proc.stdout:
        line = line.rstrip()
        lines.append(line)
        if log and line:
            log(line[:200])
    await proc.wait()
    return proc.returncode or 0, "\n".join(lines)


async def _home(conn) -> str:
    _, out = await _sh(conn, 'printf %s "$HOME"')
    return out.strip()


async def _env_ok(conn, py: str) -> bool:
    rc, _ = await _sh(conn, f"{shlex.quote(py)} -c {shlex.quote(PROBE)} 2>/dev/null")
    return rc == 0


async def setup(conn, log=lambda m: None) -> None:
    """Create ``<state>/venv`` on the server and install the packages (uv when there, else python3 -m venv)."""
    home = await _home(conn)
    state = STATE_DIR.replace("~", home, 1)
    venv = f"{state}/venv"
    deps = " ".join(shlex.quote(d) for d in DEPS)
    await _sh(conn, f"mkdir -p {shlex.quote(state)}", check=True)
    rc, _ = await _sh(conn, "command -v uv >/dev/null 2>&1")
    if rc == 0:
        log("creating the environment with uv …")
        steps = [f"uv venv --python 3.12 --clear {shlex.quote(venv)}",
                 f"uv pip install --python {shlex.quote(venv)}/bin/python {deps}"]
    else:
        rc, out = await _sh(conn, "python3 -c 'import sys; print(int(sys.version_info >= (3, 12)))' 2>/dev/null")
        if out.strip() != "1":
            raise SetupFailed("the server needs Python 3.12 or newer, or `uv` (https://docs.astral.sh/uv/): "
                              "install one of them and connect again")
        log("creating the environment with python3 …")
        steps = [f"python3 -m venv --clear {shlex.quote(venv)}",
                 f"{shlex.quote(venv)}/bin/python -m pip install --quiet {deps}"]
    for step in steps:
        rc, out = await _run(conn, step, log)
        if rc:
            raise SetupFailed(f"setup failed on the server:\n{out[-1500:]}")
    if not await _env_ok(conn, f"{venv}/bin/python"):
        raise SetupFailed("the packages were installed but cannot be imported on the server")


async def _upload_bundle(conn, state: str, log) -> str:
    data, build = bundle.build()
    dst = f"{state}/agent/{build}.zip"
    rc, _ = await _sh(conn, f"test -s {shlex.quote(dst)}")
    if rc:
        log(f"uploading the app code ({len(data) // 1024} KB) …")
        await _sh(conn, f"mkdir -p {shlex.quote(state + '/agent')}", check=True)
        async with conn.start_sftp_client() as sftp:
            async with sftp.open(dst + ".part", "wb") as fh:
                await fh.write(data)
            await sftp.posix_rename(dst + ".part", dst)
    return dst


async def open_session(s: Server, password: str | None = None, passphrase: str | None = None,
                       log=lambda m: None, install_ok: bool = False) -> Session:
    log(f"connecting to {s.name} …")
    conn = await _connect(s, password, passphrase)
    try:
        home = await _home(conn)
        state = STATE_DIR.replace("~", home, 1)
        py = f"{state}/venv/bin/python"
        if not await _env_ok(conn, py):
            if not install_ok:
                raise SetupNeeded()
            await setup(conn, log)
        zip_path = await _upload_bundle(conn, state, log)
        log("starting the agent …")
        proc = await conn.create_process(
            f"PYTHONPATH={shlex.quote(zip_path)} PYTHONDONTWRITEBYTECODE=1 exec {shlex.quote(py)} -m edh_downloader agent",
            term_type=None)
        try:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=90)
            hello = json.loads(line)["hello"]
        except Exception:
            err = (await proc.stderr.read())[-1500:] if proc.stderr else ""
            proc.close()
            raise SetupFailed("the agent did not start on the server: " + (err.strip() or "no output")) from None
        be = backend.Remote(s.name, proc, hello)
        log("ready")
        return Session(s, conn, be, hello["version"])
    except BaseException:
        conn.close()
        raise
