"""Self-update of the packaged executable from the latest GitHub release.

Every start of the app (``edh-dl`` / ``edh-dl app``) asks GitHub for the latest release, in the background while
the app starts. If it is newer than this build, the matching executable is downloaded, checked against the
release's ``.sha256`` file and swapped in place of the running one, which carries on; the new version runs from the
next start. Anything that goes wrong (offline, no permission to write next to the executable, unknown platform,
...) is reported in one line and nothing changes.

Only the packaged executable updates itself; running from source / ``uv tool install`` never does.
Opt out with ``--no-update`` or ``EDH_DL_NO_UPDATE=1``. ``edh-dl update`` does the same on demand.
"""

import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import __version__

REPO = "MaatVO/EarthDH-Downloder"
API = f"https://api.github.com/repos/{REPO}/releases/latest"


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


def is_local_build(version: str | None = None) -> bool:
    """A build made on a developer's machine (packaging/build.sh without a release number): '0.2.0+local'."""
    return (version or __version__).endswith("+local")


UPDATED: str | None = None  # the version installed in the background, which runs from the next start


def _clean_leftovers(exe: Path) -> None:
    """Remove what an interrupted update or a replaced Windows executable left next to ``exe``."""
    old = exe.with_name(exe.name + ".old")
    stale = [p for p in exe.parent.glob(".edh-dl-new-*") if time.time() - p.stat().st_mtime > 3600]
    for p in [old, *stale]:
        try:
            p.unlink(missing_ok=True)
        except OSError:  # still running (another window of the old version), or not ours to delete
            pass


def _update_in_background() -> None:
    global UPDATED
    try:
        info = latest(timeout=10)
        if not info or not is_newer(info["version"]):
            return
        install(info, Path(sys.executable))
    except Exception as e:  # offline, rate-limited, read-only folder …: this version just keeps running
        print(f"(update check: {e})", flush=True)
        return
    UPDATED = info["version"]
    print(f"edh-dl {UPDATED} is downloaded: it starts next time you open the app.", flush=True)


def auto_update() -> None:
    """Called at startup: looks for a newer release in the background, so the start is never delayed. A newer
    executable is downloaded, verified and swapped in place of this one (the running program is unaffected); it
    runs from the next start. Local builds never update themselves here (``edh-dl update`` still can)."""
    if os.environ.get("EDH_DL_NO_UPDATE") or not can_self_update() or is_local_build():
        return
    _clean_leftovers(Path(sys.executable))
    threading.Thread(target=_update_in_background, name="edh-dl-update", daemon=True).start()
