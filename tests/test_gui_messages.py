"""Wording helpers for failures and the disabled Launch button."""

from edh_downloader.gui.messages import classify_error, dataset_error, display_version, launch_reason


def test_classify_error():
    assert classify_error("HTTP 401 Unauthorized") == "auth"
    assert classify_error("key rejected (HTTP 403)") == "auth"
    assert classify_error("Cannot connect to host edh.example:443") == "network"
    assert classify_error("Name or service not known") == "network"
    assert classify_error("zarr metadata is broken") == "other"


def test_dataset_error_names_the_key_and_keeps_details():
    e = dataset_error("401 Unauthorized for https://x/y", "work")
    assert e["action"] == "keys" and "'work'" in e["title"] and "401" in e["details"]
    assert dataset_error("timed out", None)["action"] == "retry"
    assert dataset_error("weird", None)["kind"] == "other"


def test_launch_reason_priority():
    assert launch_reason(False, False) == "Add an API key in Keys."
    assert launch_reason(False, True, planning=True) == "Updating the plan…"
    assert launch_reason(False, True, error="No variables selected") == "No variables selected"
    assert launch_reason(False, True, blocked=["Accept the licence"]) == "Accept the licence"
    assert launch_reason(False, True).startswith("Select")
    assert launch_reason(True, True) == ""


def test_display_version_hides_local_suffix():
    assert display_version("0.2.0+local") == "0.2.0"
    assert display_version("0.2.0+local", dev=True) == "0.2.0+local"
    assert display_version("1.0.0") == "1.0.0"
