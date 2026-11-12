import re
from pathlib import Path

from edh_downloader.gui import tour

GUI = Path(tour.__file__).parent


def test_first_launch_offers_the_tour_and_remembers_the_answer(tmp_path, monkeypatch):
    monkeypatch.setenv("EDH_DL_CONFIG_DIR", str(tmp_path))
    assert tour.stage() == tour.WELCOME
    tour.set_stage(tour.APP)
    assert tour.stage() == tour.APP
    tour.set_stage(tour.DONE)
    assert tour.stage() == tour.DONE


def test_unreadable_or_foreign_state_counts_as_first_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("EDH_DL_CONFIG_DIR", str(tmp_path))
    (tmp_path / "ui.json").write_text("[]")
    assert tour.stage() == tour.WELCOME
    (tmp_path / "ui.json").write_text('{"tour": "maybe"}')
    assert tour.stage() == tour.WELCOME
    (tmp_path / "ui.json").write_text('{"other": 1}')
    tour.set_stage(tour.DONE)
    assert '"other": 1' in (tmp_path / "ui.json").read_text()  # keeps what else is stored there


def test_server_names_are_escaped():
    steps = tour.app_steps("<b>evil</b>", 12, "")
    assert "<b>evil</b>" not in steps[0]["title"] + steps[0]["body"]


def test_every_step_points_at_an_element_that_exists():
    """Each marker the tour looks for is set somewhere in the pages (only optional steps may be missing)."""
    source = (GUI / "app.py").read_text() + (GUI / "connect.py").read_text()
    marked = set(re.findall(r"edh-tour-([a-z-]+)", source)) | set(re.findall(r'mark="([a-z-]+)"', source))
    for steps in (tour.connect_steps(False), tour.connect_steps(True), tour.app_steps("x", 12, "")):
        for s in steps:
            for sel in [*([s["el"]] if isinstance(s.get("el"), str) else s.get("el") or []), s.get("click")]:
                if sel:
                    assert sel.removeprefix(".edh-tour-") in marked, sel
