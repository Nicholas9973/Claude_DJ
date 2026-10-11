"""Configuration loading and safety-window logic."""
from __future__ import annotations

from datetime import time

import pytest

from music_agent.config import SafetyConfig, load_config


def test_config_file_loads():
    # The owner tunes config.toml, so check it loads and is sane rather than pinning its values.
    cfg = load_config()
    assert cfg.music.service
    assert 0 < cfg.safety.max_volume <= 100
    assert isinstance(cfg.safety.quiet_start, time) and isinstance(cfg.safety.quiet_end, time)
    assert cfg.safety.quiet_mode in ("block", "cap")


@pytest.mark.parametrize("t,quiet", [(time(22, 59), False), (time(23, 0), True), (time(3, 0), True),
                                     (time(7, 59), True), (time(8, 0), False), (time(12, 0), False)])
def test_quiet_hours_cross_midnight(t, quiet):
    assert SafetyConfig().in_quiet_hours(t) is quiet
