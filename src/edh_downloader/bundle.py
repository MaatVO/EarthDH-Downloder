"""The app's Python code as one small zip, to run the agent on a server (``PYTHONPATH=agent.zip``).

From source it is built on the fly; in the packaged executable it is read from the copy added at build time
(``python -m edh_downloader.bundle <file>`` writes it). The build id is a hash of the content: the server keeps one
zip per build and uploads only when this computer's code differs.
"""

import hashlib
import io
import sys
import zipfile
from pathlib import Path

SKIP_DIRS = {"__pycache__"}
KEEP_GUI = {"structure.py"}  # the only UI module the agent needs (describing stores and plans)


def _files(root: Path):
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root.parent)
        if SKIP_DIRS & set(rel.parts):
            continue
        if "gui" in rel.parts and p.name not in KEEP_GUI:
            continue
        yield p, rel.as_posix()


def build() -> tuple[bytes, str]:
    """(zip bytes, build id)."""
    frozen = Path(getattr(sys, "_MEIPASS", "")) / "agent.zip"
    if getattr(sys, "frozen", False) and frozen.exists():
        data = frozen.read_bytes()
    else:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p, rel in _files(Path(__file__).parent):
                z.writestr(zipfile.ZipInfo(rel, (2020, 1, 1, 0, 0, 0)), p.read_bytes(), zipfile.ZIP_DEFLATED)
            z.writestr(zipfile.ZipInfo("edh_downloader/gui/__init__.py", (2020, 1, 1, 0, 0, 0)), "")
        data = buf.getvalue()
    return data, hashlib.sha256(data).hexdigest()[:12]


if __name__ == "__main__":
    Path(sys.argv[1]).write_bytes(build()[0])
