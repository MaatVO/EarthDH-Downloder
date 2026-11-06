from edh_downloader import sshconfig


def test_hosts_and_lookup(tmp_path, monkeypatch):
    monkeypatch.setattr(sshconfig, "SSH_DIR", tmp_path)
    (tmp_path / "conf.d").mkdir()
    (tmp_path / "conf.d" / "work").write_text("Host cluster\n  HostName login.cluster.org\n  ProxyJump gw\n")
    (tmp_path / "config").write_text(
        "User globaluser\nInclude conf.d/*\n\n"
        "Host hpc hpc2\n  HostName=hpc.example.org\n  Port 2222\n  User me\n\n"
        "Host gw\n  HostName gateway.example.org\n\n"
        "Match host foo\n  User nobody\n\n"
        "Host *.example.org !bad.example.org\n  User wild\n\n"
        "Host *\n  Port 22\n  ServerAliveInterval 30\n")
    hs = {e.alias: e for e in sshconfig.hosts()}
    assert list(hs) == ["cluster", "hpc", "hpc2", "gw"]
    assert (hs["hpc"].hostname, hs["hpc"].port, hs["hpc"].user) == ("hpc.example.org", 2222, "globaluser")
    assert (hs["cluster"].hostname, hs["cluster"].proxy, hs["cluster"].port) == ("login.cluster.org", "gw", 22)
    assert sshconfig.lookup("x.example.org").user == "globaluser"  # first value wins: the global one


def test_missing_config(tmp_path, monkeypatch):
    monkeypatch.setattr(sshconfig, "SSH_DIR", tmp_path)
    assert sshconfig.hosts() == []
    assert sshconfig.lookup("a").hostname == ""
