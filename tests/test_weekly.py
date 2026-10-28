"""Tests for the disk-space check in ops/weekly.py, at the thresholds in config/features.yaml."""

import pytest
import yaml

from ops.weekly import ROOT, disk_check

CFG = yaml.safe_load((ROOT / "config" / "features.yaml").read_text())["weekly"]
WARN, STOP = CFG["disk_warn_free_gb"], CFG["disk_stop_free_gb"]


@pytest.mark.parametrize("free_gb, level", [
    (STOP - 0.1, "stop"),
    (STOP, "warn"),
    (WARN - 0.1, "warn"),
    (WARN, "ok"),
    (50, "ok"),
])
def test_disk_check_levels(free_gb, level):
    assert disk_check(free_gb, 4.8, WARN, STOP)[0] == level


def test_disk_check_message_names_space_and_image():
    _, msg = disk_check(4.4, 5.2, WARN, STOP)
    assert "4.4 GB free" in msg and "5.2 GB" in msg and "compact" in msg
