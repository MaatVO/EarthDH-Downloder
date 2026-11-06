"""The laptop side: remembered servers (no secrets), and the connect page renders without a session."""

import json


def test_remembered_servers(home):
    from edh_downloader import remote

    a = remote.Server(host="hpc.example.org", user="me", key_file="~/.ssh/id_ed25519")
    remote.remember(a)
    remote.remember(remote.Server(host="other", label="lab"))
    remote.remember(a)  # moves to the top, no duplicate
    names = [s.name for s in remote.saved()]
    assert names == ["me@hpc.example.org", "lab"]
    raw = (home / "cfg" / "servers.json").read_text()
    assert "password" not in raw and "passphrase" not in raw and json.loads(raw)[0]["port"] == 0  # 0: ~/.ssh/config, else 22
    remote.forget(a)
    assert [s.name for s in remote.saved()] == ["lab"]
