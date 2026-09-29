from __future__ import annotations

from pathlib import Path

import pytest

from stacklight.config import NodeConfig, parse_config

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "config"


def minimal_config_dict() -> dict:
    """A small, valid config. Tests mutate copies of this to probe validation."""
    return {
        "node_id": "testnode",
        "camera": {
            "source": "synthetic",
            "resolution": [160, 120],
            "fps": 20,
            "exposure_us": 800,
            "analogue_gain": 1.0,
            "awb_enable": False,
            "synthetic": {"realtime": False, "noise_sigma": 0.0, "ambient": 0.0},
        },
        "segments": [
            {"name": "red", "roi": [20, 10, 20, 20], "threshold": 90},
            {"name": "green", "roi": [20, 40, 20, 20], "threshold": 90},
        ],
        "dark_reference": {"roi": [100, 80, 20, 20], "threshold": 60},
        "detection": {
            "debounce_s": 1.0,
            "flash_window_s": 2.0,
            "flash_duty_range": [0.2, 0.8],
            "flash_min_transitions": 2,
        },
        "states": {
            "fault": {"red": "steady", "green": "off"},
            "running": {"red": "off", "green": "steady"},
            "attention": {"red": "flashing"},
            "idle": {"red": "off", "green": "off"},
        },
    }


@pytest.fixture
def config_dict() -> dict:
    return minimal_config_dict()


@pytest.fixture
def config(config_dict: dict) -> NodeConfig:
    return parse_config(config_dict)
