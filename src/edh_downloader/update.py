"""Self-update of the packaged executable from the latest GitHub release.

Every start of the app (``edh-dl`` / ``edh-dl app``) asks GitHub for the latest release. If it is newer than this
build, the matching executable is downloaded, checked against the release's ``.sha256`` file, swapped in place of
the running one and started again with the same arguments. Anything that goes wrong (offline, no permission to
write next to the executable, unknown platform, ...) is reported in one line and the current version just starts.

Only the packaged executable updates itself; running from source / ``uv tool install`` never does.
Opt out with ``--no-update`` or ``EDH_DL_NO_UPDATE=1``. ``edh-dl update`` does the same on demand.
"""

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from . import __version__

REPO = "MaatVO/EarthDH-Downloder"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RESTARTED = "EDH_DL_UPDATED"  # set for the restarted process: never update twice in a row


def parse(v: str) -> tuple[int, ...]:
    """'v0.2.13' -> (0, 2, 13); unparsable -> ()."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", v.strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def platform_tag() -> str | None:
    m = platform.machine().lower()
    arch = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "arm64", "aarch64": "arm64"}.get(m)
    os_ = {"linux": "linux", "darwin": "macos", "win32": "windows"}.get(sys.platform)
    tag = f"{os_}-{arch}" if os_ and arch else None
    return tag if tag in ("linux-x86_64", "macos-arm64", "windows-x86_64") else None


def can_self_update() -> bool:
    return bool(getattr(sys, "frozen", False)) and not os.environ.get("APPIMAGE") and platform_tag() is not None


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "edh-downloader-updater",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest(timeout: float = 4.0) -> dict | None:
    """{'version': '0.2.5', 'url': ..., 'sha_url': ...} for this platform, or None."""
    rel = json.loads(_get(API, timeout))
    if rel.get("draft") or rel.get("prerelease"):
        return None
    tag = platform_tag()
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    for name, url in assets.items():
        if f"-{tag}" in name and not name.endswith(".sha256") and f"{name}.sha256" in assets:
            return {"version": rel["tag_name"].lstrip("v"), "url": url, "sha_url": assets[f"{name}.sha256"]}
    return None


def is_newer(remote: str, local: str = __version__) -> bool:
    r, l = parse(remote), parse(local)
    return bool(r) and r > l


def install(info: dict, exe: Path, timeout: float = 120.0) -> None:
    """Download, verify and swap in the new executable (raises on any problem; the old one stays usable)."""
    for u in (info["url"], info["sha_url"]):
        if not u.startswith("https://"):
            raise ValueError(f"refusing non-https url {u}")
    want = _get(info["sha_url"], 15).decode().split()[0].lower()
    fd, tmp_name = tempfile.mkstemp(prefix=".edh-dl-new-", dir=exe.parent)  # same folder: atomic rename
    tmp = Path(tmp_name)
    try:
        h = hashlib.sha256()
        req = urllib.request.Request(info["url"], headers={"User-Agent": "edh-downloader-updater"})
        with os.fdopen(fd, "wb") as f, urllib.request.urlopen(req, timeout=timeout) as r:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != want:
            raise ValueError("checksum mismatch, update discarded")
        tmp.chmod(0o755)
        if sys.platform == "win32":  # a running .exe can be renamed but not overwritten
            old = exe.with_name(exe.name + ".old")
            old.unlink(missing_ok=True)
            os.replace(exe, old)
            try:
                os.replace(tmp, exe)
            except OSError:
                os.replace(old, exe)
                raise
        else:
            os.replace(tmp, exe)
    finally:
        tmp.unlink(missing_ok=True)


def check_and_update(force_notice: bool = False) -> bool:
    """Update if a newer release exists. True when the executable was replaced."""
    try:
        info = latest()
    except Exception as e:
        if force_notice:
            print(f"could not check for updates: {e}")
        return False
    if not info or not is_newer(info["version"]):
        if force_notice:
            print(f"edh-dl {__version__} is up to date")
        return False
    print(f"Updating edh-dl {__version__} → {info['version']} …", flush=True)
    try:
        install(info, Path(sys.executable))
    except Exception as e:
        print(f"update failed ({e}); starting {__version__}", flush=True)
        return False
    return True


def auto_update(argv: list[str]) -> None:
    """Called at startup. If an update was installed, runs the new version and exits with its status."""
    if os.environ.get("EDH_DL_NO_UPDATE") or os.environ.get(RESTARTED) or not can_self_update():
        return
    if sys.platform == "win32":
        Path(sys.executable).with_name(Path(sys.executable).name + ".old").unlink(missing_ok=True)
    if not check_and_update():
        return
    env = dict(os.environ, **{RESTARTED: "1", "PYINSTALLER_RESET_ENVIRONMENT": "1"})  # fresh onefile unpack
    try:
        rc = subprocess.call([sys.executable, *argv], env=env)
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
