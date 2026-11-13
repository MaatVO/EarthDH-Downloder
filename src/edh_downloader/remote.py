"""Work on a server from your computer over one SSH connection (asyncssh, no ssh binary needed).

The interface runs here; the server runs ``edh-dl agent``, which holds the keys, quota, jobs and data. Nothing big
is uploaded: the server gets a zip with this app's Python code (a few hundred KB, again only when it changes) and
a virtual environment with the numerical packages, installed once. Closing the session stops the agent; download
jobs run detached and keep going.
"""

import asyncio
import hashlib
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

STATE_DIR = "~/.local/share/edh-dl"  # on the server: venv-<deps id>/ and agent/<build>.zip
DEPS = ["aiohttp>=3.9", "icechunk>=2", "numcodecs>=0.15", "numpy>=2", "pandas>=2.2", "xarray>=2024.10", "zarr>=3"]
PROBE = "import aiohttp, icechunk, numcodecs, numpy, pandas, xarray, zarr"
# GRIB and NetCDF decoding for Climate Data Store jobs. Optional: their binary wheels need glibc 2.28+ (macOS 15+),
# and a server without them must still run Earth Data Hub jobs, so each is installed when possible (one failing does
# not stop the other) and never part of the PROBE.
OPTIONAL = {"GRIB": ["eccodes>=2.40", "eccodeslib>=2.50.0.34; sys_platform != 'win32'"],
            "NetCDF": ["netCDF4>=1.7"]}
OPTIONAL_DEPS = [d for group in OPTIONAL.values() for d in group]
# Jobs keep running with the code (agent/<build>.zip) and the environment that started them. So nothing a newer app
# version installs may touch them: each build has its own zip, each dependency set its own venv, and an existing
# environment is never rebuilt in place (``--clear`` would pull the packages from under a running download).
DEPS_ID = hashlib.sha256("\n".join(DEPS + OPTIONAL_DEPS).encode()).hexdigest()[:10]
LEGACY_DEPS = [  # the set apps before versioned environments installed in venv/
    "aiohttp>=3.9", "icechunk>=2", "numcodecs>=0.15", "numpy>=2", "pandas>=2.2", "xarray>=2024.10",
               "zarr>=3"]


def venv_dir(state: str) -> str:
    return f"{state}/venv-{DEPS_ID}"


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

    def __init__(self, host, port, key, path=None):
        super().__init__(f"unknown host key for {host}")
        self.host, self.port, self.key, self.path = host, port, key, path
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


def _known_hosts_file(alias: str, host: str) -> Path:
    """Where ssh would record ``host``: the alias's UserKnownHostsFile (if any real file), else ~/.ssh/known_hosts."""
    e = sshconfig.lookup(alias)
    if host == (e.hostname or alias):
        for f in e.known_hosts.split():
            f = os.path.expanduser(f)
            if f != os.devnull:
                return Path(f)
    return Path.home() / ".ssh" / "known_hosts"


def trust(host: str, port: int, key, path: Path | None = None) -> None:
    """Append the host key to known_hosts (what ssh does when you answer "yes")."""
    kh = Path(path) if path else Path.home() / ".ssh" / "known_hosts"
    kh.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = host if port == 22 else f"[{host}]:{port}"
    with open(kh, "a") as f:
        f.write(f"{name} {key.export_public_key().decode().strip()}\n")


# Host keys asyncssh could not verify. Hooked on the base class because ProxyJump hops are opened by asyncssh
# itself with a plain SSHClient: this way the key of whichever hop is unknown is captured, on the very
# connection being made (no separate direct connection, which a jump host setup would not allow).
_unknown: list[tuple[str, int, object]] = []


def _capture(self, host, addr, port, key) -> bool:
    _unknown.append((host, port, key))
    return False


asyncssh.SSHClient.validate_host_public_key = _capture


async def _connect(s: Server, password: str | None, passphrase: str | None, _retry: bool = True):
    kw = dict(keepalive_interval=30, passphrase=passphrase or None)
    if s.port:  # else ~/.ssh/config or 22
        kw["port"] = s.port
    if s.user:
        kw["username"] = s.user
    if s.key_file:
        kw["client_keys"] = [os.path.expanduser(s.key_file)]
    if password:
        kw["password"] = password
    entry = sshconfig.lookup(s.host)
    if entry.strict in ("no", "off"):  # as ssh: connect without checking host keys
        kw["known_hosts"] = None
    _unknown.clear()
    try:
        return await asyncssh.connect(s.host, **kw)
    except asyncssh.HostKeyNotVerifiable:
        if not _unknown:
            raise
        host, port, key = _unknown[-1]
        path = _known_hosts_file(s.host, host)
        if entry.strict == "accept-new" and _retry:
            trust(host, port, key, path)
            return await _connect(s, password, passphrase, _retry=False)
        raise UnknownHost(host, port, key, path) from None


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


async def capabilities(conn, py: str) -> dict:
    """What the server's environment can do beyond the required packages, by importing them. Nothing here is needed
    to run the app: a missing capability only stops the jobs that need it (GRIB conversion needs ``eccodes``,
    NetCDF conversion ``netCDF4``)."""
    out = {}
    for cap, module in (("grib", "eccodes"), ("netcdf", "netCDF4")):
        rc, _ = await _sh(conn, f"{shlex.quote(py)} -c 'import {module}' 2>/dev/null")
        out[cap] = rc == 0
    return out


async def setup(conn, log=lambda m: None) -> dict:
    """Create ``<state>/venv-<deps id>`` on the server and install the packages (uv when there, else python3 -m
    venv). A new directory: environments that running jobs use are left alone. An environment whose python exists
    is not cleared (it may be in use): only the packages are installed into it. Returns ``capabilities``."""
    home = await _home(conn)
    state = STATE_DIR.replace("~", home, 1)
    venv = venv_dir(state)
    py = f"{venv}/bin/python"
    deps = " ".join(shlex.quote(d) for d in DEPS)
    await _sh(conn, f"mkdir -p {shlex.quote(state)}", check=True)
    exists = (await _sh(conn, f"test -x {shlex.quote(py)}"))[0] == 0
    rc, _ = await _sh(conn, "command -v uv >/dev/null 2>&1")
    uv = rc == 0
    if uv:
        log("creating the environment with uv …" if not exists else "updating the packages with uv …")
        create = f"uv venv --python 3.12 --clear {shlex.quote(venv)}"
        install = f"uv pip install --python {shlex.quote(py)} {deps}"
    else:
        rc, out = await _sh(conn, "python3 -c 'import sys; print(int(sys.version_info >= (3, 12)))' 2>/dev/null")
        if not exists and out.strip() != "1":
            raise SetupFailed("the server needs Python 3.12 or newer, or `uv` (https://docs.astral.sh/uv/): "
                              "install one of them and connect again")
        log("creating the environment with python3 …" if not exists else "updating the packages with pip …")
        create = f"python3 -m venv --clear {shlex.quote(venv)}"
        install = f"{shlex.quote(py)} -m pip install --quiet {deps}"
    steps = [install] if exists else [create, install]
    for step in steps:
        rc, out = await _run(conn, step, log)
        if rc:
            raise SetupFailed(f"setup failed on the server:\n{out[-1500:]}")
    for what, group in OPTIONAL.items():
        if not group:
            continue
        opt = " ".join(shlex.quote(d) for d in group)
        step = (f"uv pip install --python {shlex.quote(py)} {opt}" if uv else
                f"{shlex.quote(py)} -m pip install --quiet {opt}")
        log(f"adding {what} support (Climate Data Store) …")
        rc, out = await _run(conn, step, log)
        if rc:
            log(f"{what} support could not be installed on this server: Climate Data Store jobs that convert "
                f"{what} will not run here (Earth Data Hub jobs are not affected)")
    if not await _env_ok(conn, py):
        raise SetupFailed("the packages were installed but cannot be imported on the server")
    return await capabilities(conn, py)


async def _python(conn, state: str) -> str | None:
    """The environment for this app's dependency set: its own venv, or the legacy ``venv/`` when that was
    installed with the same set (so existing servers do not reinstall)."""
    py = f"{venv_dir(state)}/bin/python"
    if await _env_ok(conn, py):
        return py
    legacy = f"{state}/venv/bin/python"
    if DEPS == LEGACY_DEPS and not OPTIONAL_DEPS and await _env_ok(conn, legacy):
        return legacy
    return None


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
        py = await _python(conn, state)
        if py is None:
            if not install_ok:
                raise SetupNeeded()
            await setup(conn, log)
            py = f"{venv_dir(state)}/bin/python"
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
