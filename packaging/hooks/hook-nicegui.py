"""NiceGUI's browser files, without the libraries of elements this app never shows.

Replaces the hook from pyinstaller-hooks-contrib (which takes them all). The single-file executable unpacks every
file at each start, so leaving these out (~17 MB, ~400 files) makes the app start sooner. NiceGUI loads them only
when such an element is on a page; if the app ever uses one (ui.plotly, ui.echart, ui.aggrid, ui.codemirror,
ui.json_editor, ui.scene, ui.mermaid or markdown with the mermaid extra, ui.xterm, ui.add_sass), remove it here.
"""

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

UNUSED = ("plotly", "echart", "aggrid", "codemirror", "json_editor", "scene", "mermaid", "xterm")

datas = collect_data_files("nicegui", excludes=[f"elements/{name}/**" for name in UNUSED]
                           + ["static/sass.dart.js", "**/__pycache__/**"])
hiddenimports = collect_submodules("nicegui")
