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


class _FakeConn:
    """Records the shell commands; ``ok`` holds the python paths whose probe import succeeds."""

    def __init__(self, ok):
        self.ok, self.cmds = set(ok), []

    async def run(self, cmd, check=False):
        import shlex
        from types import SimpleNamespace

        inner = shlex.split(cmd)[2]
        self.cmds.append(inner)
        if inner.startswith('printf %s "$HOME"'):
            return SimpleNamespace(exit_status=0, stdout="/home/u")
        if " -c " in inner and "import aiohttp" in inner:
            return SimpleNamespace(exit_status=0 if shlex.split(inner)[0] in self.ok else 1, stdout="")
        if inner.startswith("command -v uv"):
            return SimpleNamespace(exit_status=0, stdout="")
        return SimpleNamespace(exit_status=0, stdout="")

    async def create_process(self, cmd, term_type=None):
        self.cmds.append(cmd)

        class P:
            stdout = self._lines()
            returncode = 0

            async def wait(self):
                pass
        self.ok.add(f"/home/u/.local/share/edh-dl/venv-{__import__('edh_downloader.remote').remote.DEPS_ID}/bin/python")
        return P()

    async def _lines(self):
        if False:
            yield ""


def test_existing_server_keeps_its_environment(monkeypatch):
    import asyncio

    from edh_downloader import remote

    legacy = "/home/u/.local/share/edh-dl/venv/bin/python"
    monkeypatch.setattr(remote, "OPTIONAL_DEPS", [])  # the same packages as before: nothing to install
    assert asyncio.run(remote._python(_FakeConn({legacy}), "/home/u/.local/share/edh-dl")) == legacy


def test_grib_support_gets_a_new_environment_and_may_fail(monkeypatch):
    """Adding the optional GRIB packages builds a new environment once; their failure does not stop the setup."""
    import asyncio

    from edh_downloader import remote

    legacy = "/home/u/.local/share/edh-dl/venv/bin/python"
    conn = _FakeConn({legacy})
    assert asyncio.run(remote._python(conn, "/home/u/.local/share/edh-dl")) is None
    orig = conn.create_process

    async def optional_fails(cmd, term_type=None):
        p = await orig(cmd, term_type)
        if "eccodes" in cmd:
            p.returncode = 1
        return p

    conn.create_process = optional_fails
    said = []
    asyncio.run(remote.setup(conn, log=said.append))
    assert any("eccodes" in c for c in conn.cmds)
    assert any("GRIB support could not be installed" in m for m in said)


def test_new_dependencies_install_beside_the_running_environment(monkeypatch):
    """A newer app needing other packages must not rebuild the environment a running job uses."""
    import asyncio

    from edh_downloader import remote

    monkeypatch.setattr(remote, "DEPS", remote.DEPS + ["somethingnew>=1"])
    monkeypatch.setattr(remote, "DEPS_ID", "newdeps123")
    legacy = "/home/u/.local/share/edh-dl/venv/bin/python"
    conn = _FakeConn({legacy})
    assert asyncio.run(remote._python(conn, "/home/u/.local/share/edh-dl")) is None  # needs setup
    asyncio.run(remote.setup(conn))
    touched = " ".join(conn.cmds)
    assert "venv-newdeps123" in touched
    assert "/edh-dl/venv " not in touched + " " and "--clear /home/u/.local/share/edh-dl/venv'" not in touched
    for c in conn.cmds:
        if "--clear" in c:
            assert "venv-newdeps123" in c, f"rebuilds an environment in use: {c}"
