"""Release notes for a GitHub release: the commits since the previous tag, grouped by kind, then the downloads.

GitHub's generated notes only list merged pull requests, so direct pushes to main get a bare "Full Changelog"
link. This reads the commit subjects instead (Conventional Commits: ``feat: …``, ``fix(ui): …``) and writes
Markdown to stdout. Standard library only; run from the repository with the full history and tags fetched:

    python packaging/release_notes.py --tag v0.2.18 --repo owner/name [--assets assets/] > notes.md
"""

import argparse
import re
import subprocess
from pathlib import Path

# (heading, commit types) in the order shown; anything unlisted or untyped goes to "Other changes"
SECTIONS = (
    ("New", {"feat"}),
    ("Fixes", {"fix"}),
    ("Improvements", {"perf", "refactor", "style"}),
    ("Documentation", {"docs"}),
    ("Other changes", set()),
    ("Maintenance", {"ci", "build", "chore", "test", "deps"}),
)
SKIP = re.compile(r"^(Merge |Revert \"Revert)|\[skip (ci|actions|notes)\]", re.I)
CC = re.compile(r"^(?P<type>[a-z]+)(\((?P<scope>[^)]+)\))?(?P<bang>!)?:\s*(?P<text>.+)$", re.I)
PLATFORMS = (("linux", "Linux (x86-64)"), ("macos", "macOS (Apple silicon)"), ("windows", "Windows (x86-64)"))


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def previous_tag(tag: str) -> str | None:
    """The newest tag reachable from HEAD other than ``tag`` (which may already exist on a re-run)."""
    for t in git("tag", "--merged", "HEAD", "--sort=-v:refname").split():
        if t != tag:
            return t
    return None


def commits(since: str | None) -> list[tuple[str, str]]:
    rng = f"{since}..HEAD" if since else "HEAD"
    out = git("log", "--no-merges", "--format=%h%x1f%s", rng)
    return [tuple(line.split("\x1f", 1)) for line in out.splitlines() if "\x1f" in line]


def entry(subject: str) -> tuple[str, str, bool]:
    """(type, text, breaking) of a commit subject; untyped subjects have type ""."""
    m = CC.match(subject)
    if not m:
        return "", subject, False
    text = m["text"].strip()
    text = text[0].upper() + text[1:]
    if m["scope"]:
        text = f"**{m['scope']}:** {text}"
    return m["type"].lower(), text, bool(m["bang"])


def notes(tag: str, repo: str, assets: Path | None) -> str:
    prev = previous_tag(tag)
    groups: dict[str, list[str]] = {h: [] for h, _ in SECTIONS}
    breaking = []
    for sha, subject in commits(prev):
        if SKIP.search(subject):
            continue
        kind, text, bang = entry(subject)
        line = f"- {text} ({sha})"
        if bang:
            breaking.append(line)
        heading = next((h for h, kinds in SECTIONS if kind in kinds), "Other changes")
        groups[heading].append(line)

    out = [f"## What's changed in {tag}", ""]
    if breaking:
        out += ["### ⚠️ Breaking changes", *breaking, ""]
    for heading, lines in groups.items():
        if lines:
            out += [f"### {heading}", *lines, ""]
    if not any(groups.values()):
        out += ["No changes beyond a rebuild.", ""]

    if assets and assets.is_dir():
        files = sorted(p.name for p in assets.iterdir() if p.is_file() and not p.name.endswith(".sha256"))
        rows = []
        for key, label in PLATFORMS:
            for name in files:
                if key in name:
                    url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
                    rows.append(f"| {label} | [`{name}`]({url}) |")
        if rows:
            out += ["### Downloads", "", "| Platform | File |", "| --- | --- |", *rows, "",
                    "Each file has a `.sha256` checksum beside it. Already installed? The app downloads "
                    "this version by itself and uses it from the next start.", ""]

    if prev:
        out.append(f"**Full changelog:** https://github.com/{repo}/compare/{prev}...{tag}")
    return "\n".join(out) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tag", required=True, help="the tag of the release being made")
    ap.add_argument("--repo", required=True, help="owner/name on GitHub")
    ap.add_argument("--assets", type=Path, help="directory of the release files, for the download table")
    a = ap.parse_args()
    print(notes(a.tag, a.repo, a.assets), end="")


if __name__ == "__main__":
    main()
