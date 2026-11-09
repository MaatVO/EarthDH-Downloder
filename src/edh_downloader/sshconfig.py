"""Read ``~/.ssh/config`` (and its ``Include`` files): the host aliases to offer as presets, and what they resolve to.

Only for display and for known_hosts entries: the connection itself is made by asyncssh, which applies the whole
config (ProxyJump, IdentityFile, certificates …) to an alias by itself. ``Match`` blocks are skipped here.
"""

import glob
import os
import shlex
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

SSH_DIR = Path("~/.ssh")


@dataclass
class Entry:
    alias: str
    hostname: str = ""
    user: str = ""
    port: int = 0
    proxy: str = ""
    known_hosts: str = ""  # UserKnownHostsFile (space-separated), empty: default
    strict: str = ""  # StrictHostKeyChecking, lower case


def _lines(path: Path, depth: int = 0):
    """(keyword, args) of a config file, with Include expanded in place."""
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line.split(None, 1)[0]:
            line = line.replace("=", " ", 1)
        try:
            parts = shlex.split(line, comments=True)
        except ValueError:
            continue
        if not parts:
            continue
        key, args = parts[0].lower(), parts[1:]
        if key == "include" and depth < 8:
            for pat in args:
                pat = os.path.expanduser(pat)
                if not os.path.isabs(pat):
                    pat = str(SSH_DIR.expanduser() / pat)
                for f in sorted(glob.glob(pat)):
                    yield from _lines(Path(f), depth + 1)
        else:
            yield key, args


def _blocks(path: Path | None = None):
    """[(patterns or None for Match, {keyword: first value})] in file order; the first block holds global options."""
    blocks: list[tuple[list[str] | None, dict[str, str]]] = [(["*"], {})]
    for key, args in _lines(path or SSH_DIR.expanduser() / "config"):
        if key == "host":
            blocks.append((args, {}))
        elif key == "match":
            blocks.append((None, {}))
        elif args:
            blocks[-1][1].setdefault(key, " ".join(args))
    return blocks


def _matches(patterns: list[str], host: str) -> bool:
    ok = False
    for p in patterns:
        if p.startswith("!"):
            if fnmatchcase(host, p[1:]):
                return False
        elif fnmatchcase(host, p):
            ok = True
    return ok


def lookup(alias: str, path: Path | None = None) -> Entry:
    """What ssh would use for ``alias`` (first value wins, as in ssh)."""
    opts: dict[str, str] = {}
    for patterns, values in _blocks(path):
        if patterns is not None and _matches(patterns, alias):
            for k, v in values.items():
                opts.setdefault(k, v)
    try:
        port = int(opts.get("port", 0))
    except ValueError:
        port = 0
    hostname = opts.get("hostname", "").replace("%h", alias)
    return Entry(alias, hostname, opts.get("user", ""), port, opts.get("proxyjump", ""),
                 opts.get("userknownhostsfile", ""), opts.get("stricthostkeychecking", "").lower())


def hosts(path: Path | None = None) -> list[Entry]:
    """Concrete aliases (no wildcards), in file order."""
    seen: list[str] = []
    for patterns, _ in _blocks(path):
        for p in patterns or []:
            if not any(c in p for c in "*?!") and p not in seen:
                seen.append(p)
    return [lookup(a, path) for a in seen]
