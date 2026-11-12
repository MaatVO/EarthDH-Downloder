import hashlib
import sys

import pytest

from edh_downloader import update


def test_parse_and_newer():
    assert update.parse("v0.2.13") == (0, 2, 13)
    assert update.is_newer("0.2.13", "0.2.9")
    assert not update.is_newer("0.2.2", "0.2.2")
    assert not update.is_newer("junk", "0.2.0")


def test_install_verifies_checksum(tmp_path, monkeypatch):
    payload = b"new-binary"
    exe = tmp_path / "edh-dl"
    exe.write_bytes(b"old")
    served = {"https://x/bin": payload, "https://x/bin.sha256": f"{hashlib.sha256(payload).hexdigest()} bin".encode()}
    monkeypatch.setattr(update, "_get", lambda url, timeout: served[url])

    class R:
        def __init__(s, b): s.b = b; s.i = 0
        def read(s, n): d, s.b = s.b[:n], s.b[n:]; return d
        def __enter__(s): return s
        def __exit__(s, *a): pass

    monkeypatch.setattr(update.urllib.request, "urlopen", lambda req, timeout: R(served[req.full_url]))
    info = {"url": "https://x/bin", "sha_url": "https://x/bin.sha256"}
    if sys.platform != "win32":
        update.install(info, exe)
        assert exe.read_bytes() == payload
    served["https://x/bin.sha256"] = b"0" * 64 + b" bin"
    exe.write_bytes(b"old")
    with pytest.raises(ValueError, match="checksum"):
        update.install(info, exe)
    assert exe.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["edh-dl"]  # no temp leftovers
    with pytest.raises(ValueError, match="https"):
        update.install({"url": "http://x/bin", "sha_url": "https://x/bin.sha256"}, exe)


def test_latest_picks_platform_asset(monkeypatch):
    import json
    monkeypatch.setattr(update, "platform_tag", lambda: "linux-x86_64")
    rel = {"tag_name": "v0.2.9", "assets": [
        {"name": n, "browser_download_url": f"https://g/{n}"} for n in (
            "edh-dl-v0.2.9-linux-x86_64", "edh-dl-v0.2.9-linux-x86_64.sha256", "edh-dl-v0.2.9-macos-arm64")]}
    monkeypatch.setattr(update, "_get", lambda url, timeout: json.dumps(rel).encode())
    assert update.latest()["url"] == "https://g/edh-dl-v0.2.9-linux-x86_64"
    assert update.latest()["version"] == "0.2.9"


class _Threads:
    def __init__(self):
        self.started = []

    def __call__(self, target, **kw):
        self.started.append(target)
        return self

    def start(self):
        pass


def _startup(monkeypatch, tmp_path, version):
    monkeypatch.setattr(update, "__version__", version)
    monkeypatch.setattr(update, "can_self_update", lambda: True)
    monkeypatch.setattr(update.sys, "executable", str(tmp_path / "edh-dl"))
    monkeypatch.delenv("EDH_DL_NO_UPDATE", raising=False)
    threads = _Threads()
    monkeypatch.setattr(update.threading, "Thread", threads)
    update.auto_update()
    return threads.started


def test_local_build_never_replaces_itself_at_startup(monkeypatch, tmp_path):
    """A build from packaging/build.sh without a release number must not swap itself for the latest release."""
    assert _startup(monkeypatch, tmp_path, "0.2.0+local") == []
    assert update.is_local_build("0.2.0+local") and not update.is_local_build("0.2.17")
    assert update.is_newer("0.2.5", "0.2.0+local")  # an explicit `edh-dl update` still sees releases as newer


def test_release_build_checks_in_the_background_without_delaying_the_start(monkeypatch, tmp_path):
    (tmp_path / ".edh-dl-new-abandoned").write_bytes(b"x")
    import os
    os.utime(tmp_path / ".edh-dl-new-abandoned", (0, 0))  # an update interrupted long ago
    (tmp_path / ".edh-dl-new-current").write_bytes(b"x")  # another window downloading right now
    assert _startup(monkeypatch, tmp_path, "0.2.3") == [update._update_in_background]
    assert not (tmp_path / ".edh-dl-new-abandoned").exists()
    assert (tmp_path / ".edh-dl-new-current").exists()


def test_background_update_installs_for_the_next_start(monkeypatch, tmp_path):
    exe = tmp_path / "edh-dl"
    monkeypatch.setattr(update, "__version__", "0.2.3")
    monkeypatch.setattr(update.sys, "executable", str(exe))
    monkeypatch.setattr(update, "UPDATED", None)
    monkeypatch.setattr(update, "latest", lambda timeout: {"version": "0.2.9", "url": "u", "sha_url": "s"})
    got = []
    monkeypatch.setattr(update, "install", lambda info, path: got.append((info["version"], path)))
    update._update_in_background()
    assert got == [("0.2.9", exe)] and update.UPDATED == "0.2.9"

    monkeypatch.setattr(update, "latest", lambda timeout: (_ for _ in ()).throw(OSError("offline")))
    update._update_in_background()  # offline: says so, changes nothing, raises nothing
