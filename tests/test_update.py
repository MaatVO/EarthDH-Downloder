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
