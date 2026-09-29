from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest

from stacklight.config import NodeConfig, parse_config
from stacklight.detector import Detector, Reading
from stacklight.sources import create_source
from stacklight.sources.synthetic import ScriptStep

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


# --------------------------------------------------------------------------------------
# Driving the detection engine from scripted synthetic light
# --------------------------------------------------------------------------------------


def run_script(
    config: NodeConfig,
    steps: Sequence[ScriptStep],
    *,
    seconds: float | None = None,
    loop: bool = False,
) -> list[Reading]:
    """Play a light script through a synthetic camera and the detector.

    The synthetic source runs on a virtual clock, so ``seconds`` of light behaviour
    costs no real time and the same frames come out every run.
    """
    if seconds is None:
        seconds = sum(step.duration_s for step in steps)
    source = create_source(config, override="synthetic")
    source.set_script(steps, loop=loop)
    detector = Detector(config)
    readings: list[Reading] = []
    with source:
        for frame in source.frames(limit=round(seconds * config.camera.fps)):
            readings.append(detector.update(frame))
    return readings


def commits(readings: Iterable[Reading]) -> list[tuple[str, float]]:
    """``(state, time)`` for every frame that committed a new state."""
    return [(r.state, round(r.t, 3)) for r in readings if r.changed]


def states(readings: Iterable[Reading]) -> list[str]:
    return [state for state, _ in commits(readings)]


def at(readings: Sequence[Reading], t: float) -> Reading:
    """The last reading at or before source time ``t``."""
    candidates = [r for r in readings if r.t <= t]
    if not candidates:
        raise AssertionError(f"no reading at or before t={t}")
    return candidates[-1]


@pytest.fixture
def dark_ref_config_dict(config_dict: dict) -> dict:
    """Config whose synthetic source can also light the dark reference region.

    The dark reference is not a detection segment, so the synthetic mirror does not
    draw it. Naming it explicitly lets a test simulate a bumped camera.
    """
    config_dict["camera"]["synthetic"]["segments"] = [
        {"name": "red", "roi": [20, 10, 20, 20], "color": [255, 0, 0]},
        {"name": "green", "roi": [20, 40, 20, 20], "color": [0, 255, 0]},
        {"name": "stray_light", "roi": [100, 80, 20, 20], "color": [255, 255, 255]},
    ]
    return config_dict


@pytest.fixture
def fast_window_config_dict(config_dict: dict) -> dict:
    """Short flash window, long debounce.

    Segment modes settle in a third of a second, so any state flapping that survives
    is the debounce being exercised rather than the flash classifier.
    """
    config_dict["detection"]["flash_window_s"] = 0.4
    config_dict["detection"]["debounce_s"] = 2.0
    return config_dict
