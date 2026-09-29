from __future__ import annotations

import copy

import pytest

from stacklight.config import (
    Roi,
    find_ambiguous_patterns,
    load_config,
    parse_config,
    resolve_state,
)
from stacklight.errors import ConfigError
from stacklight.types import UNKNOWN_STATE, SegmentMode

from .conftest import CONFIG_DIR

OFF, STEADY, FLASHING = SegmentMode.OFF, SegmentMode.STEADY, SegmentMode.FLASHING


def parse_with(config_dict: dict, **changes) -> None:
    data = copy.deepcopy(config_dict)
    data.update(changes)
    parse_config(data)


# -- shipped configs ---------------------------------------------------------------


@pytest.mark.parametrize("name", ["machine1.yaml", "dev-synthetic.yaml"])
def test_shipped_configs_are_valid(name):
    config = load_config(CONFIG_DIR / name)
    assert config.segment_names == ("red", "green")
    assert config.dark_reference is not None


def test_topics_are_namespaced_per_node(config):
    assert config.state_topic == "shop/lights/testnode/state"
    assert config.health_topic == "shop/lights/testnode/health"
    assert config.snapshot_topic == "shop/lights/testnode/snapshot"


def test_defaults_fill_in_optional_sections(config):
    assert config.mqtt.host == "localhost"
    assert config.publish.heartbeat_s == 15.0
    assert config.logging.level == "INFO"
    # "value" (max channel) is the default metric: a saturated red LED reads ~255
    # rather than the ~85 a plain RGB average would give.
    assert config.detection.metric == "value"


# -- structural validation ---------------------------------------------------------


def test_unknown_top_level_key_is_rejected(config_dict):
    config_dict["segmnets"] = []
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config(config_dict)


def test_unknown_nested_key_is_rejected(config_dict):
    config_dict["detection"]["debounce"] = 2
    with pytest.raises(ConfigError, match=r"detection: unknown key"):
        parse_config(config_dict)


def test_missing_required_key_is_rejected(config_dict):
    del config_dict["segments"]
    with pytest.raises(ConfigError, match="missing required key 'segments'"):
        parse_config(config_dict)


@pytest.mark.parametrize("node_id", ["machine", "af 100", "af/100", "", "-af"])
def test_node_id_must_be_topic_safe(config_dict, node_id):
    config_dict["node_id"] = node_id
    with pytest.raises(ConfigError, match="node_id"):
        parse_config(config_dict)


def test_base_topic_rejects_wildcards(config_dict):
    config_dict["mqtt"] = {"base_topic": "shop/+/lights"}
    with pytest.raises(ConfigError, match="wildcard"):
        parse_config(config_dict)


# -- ROIs --------------------------------------------------------------------------


def test_roi_outside_frame_is_rejected(config_dict):
    config_dict["segments"][0]["roi"] = [150, 10, 40, 20]  # frame is 160x120
    with pytest.raises(ConfigError, match="extends past the 160x120 frame"):
        parse_config(config_dict)


def test_dark_reference_roi_is_bounds_checked(config_dict):
    config_dict["dark_reference"]["roi"] = [0, 110, 20, 20]
    with pytest.raises(ConfigError, match=r"dark_reference\.roi"):
        parse_config(config_dict)


def test_zero_size_roi_is_rejected(config_dict):
    config_dict["segments"][0]["roi"] = [10, 10, 0, 20]
    with pytest.raises(ConfigError, match="must be positive"):
        parse_config(config_dict)


def test_roi_slices_index_a_frame_correctly():
    roi = Roi(3, 5, 4, 6)
    rows, cols = roi.slices
    assert (rows.start, rows.stop) == (5, 11)
    assert (cols.start, cols.stop) == (3, 7)
    assert roi.area == 24


def test_dark_reference_is_optional(config_dict):
    del config_dict["dark_reference"]
    assert parse_config(config_dict).dark_reference is None


# -- segments ----------------------------------------------------------------------


def test_duplicate_segment_names_are_rejected(config_dict):
    config_dict["segments"][1]["name"] = "red"
    with pytest.raises(ConfigError, match="duplicate segment name"):
        parse_config(config_dict)


def test_empty_segment_list_is_rejected(config_dict):
    config_dict["segments"] = []
    with pytest.raises(ConfigError, match="at least one segment"):
        parse_config(config_dict)


def test_threshold_must_be_a_pixel_value(config_dict):
    config_dict["segments"][0]["threshold"] = 300
    with pytest.raises(ConfigError, match="must be <= 255"):
        parse_config(config_dict)


def test_segment_render_color_defaults_from_name(config):
    assert config.segment("red").render_color == (255, 40, 30)
    assert config.segment("green").render_color == (40, 255, 70)


def test_explicit_segment_color_wins(config_dict):
    config_dict["segments"][0]["color"] = [200, 0, 0]
    assert parse_config(config_dict).segment("red").render_color == (200, 0, 0)


# -- detection ---------------------------------------------------------------------


@pytest.mark.parametrize("duty", [[0.8, 0.2], [0.0, 0.0], [0.2, 1.5], [-0.1, 0.5]])
def test_bad_flash_duty_range_is_rejected(config_dict, duty):
    config_dict["detection"]["flash_duty_range"] = duty
    with pytest.raises(ConfigError, match="flash_duty_range"):
        parse_config(config_dict)


def test_unknown_metric_is_rejected(config_dict):
    config_dict["detection"]["metric"] = "hue"
    with pytest.raises(ConfigError, match="metric"):
        parse_config(config_dict)


# -- states ------------------------------------------------------------------------


def test_state_referencing_unknown_segment_is_rejected(config_dict):
    config_dict["states"]["fault"] = {"amber": "steady"}
    with pytest.raises(ConfigError, match=r"states\.fault: unknown key"):
        parse_config(config_dict)


def test_unknown_segment_mode_is_rejected(config_dict):
    config_dict["states"]["fault"] = {"red": "blinking"}
    with pytest.raises(ConfigError, match="expected one of"):
        parse_config(config_dict)


def test_unknown_is_a_reserved_state_name(config_dict):
    config_dict["states"][UNKNOWN_STATE] = {"red": "steady"}
    with pytest.raises(ConfigError, match="reserved"):
        parse_config(config_dict)


def test_state_with_no_constraints_is_rejected(config_dict):
    config_dict["states"]["everything"] = {}
    with pytest.raises(ConfigError, match="match every observation"):
        parse_config(config_dict)


def test_identical_states_are_rejected(config_dict):
    config_dict["states"]["duplicate"] = {"red": "steady", "green": "off"}
    with pytest.raises(ConfigError, match="identical constraints"):
        parse_config(config_dict)


def test_equally_specific_overlapping_states_are_rejected(config_dict):
    # Both are one-constraint rules, and both match (red=flashing, green=steady).
    config_dict["states"]["attention"] = {"red": "flashing"}
    config_dict["states"]["producing"] = {"green": "steady"}
    with pytest.raises(ConfigError, match="equally specific"):
        parse_config(config_dict)


def test_more_specific_state_beats_wildcard(config_dict):
    # A two-constraint rule and a one-constraint rule may overlap: specificity decides.
    config_dict["states"]["attention_idle"] = {"red": "flashing", "green": "off"}
    config = parse_config(config_dict)
    assert config.resolve_state({"red": FLASHING, "green": OFF}) == "attention_idle"
    assert config.resolve_state({"red": FLASHING, "green": STEADY}) == "attention"


# -- state resolution --------------------------------------------------------------


@pytest.mark.parametrize(
    ("red", "green", "expected"),
    [
        (STEADY, OFF, "fault"),
        (OFF, STEADY, "running"),
        (OFF, OFF, "idle"),
        (FLASHING, OFF, "attention"),
        (FLASHING, STEADY, "attention"),
        (FLASHING, FLASHING, "attention"),
        # Nothing configured covers these, so they must not be guessed at.
        (STEADY, STEADY, UNKNOWN_STATE),
        (STEADY, FLASHING, UNKNOWN_STATE),
        (OFF, FLASHING, UNKNOWN_STATE),
    ],
)
def test_state_resolution_table(config, red, green, expected):
    assert config.resolve_state({"red": red, "green": green}) == expected


def test_unmatched_pattern_is_unknown_not_a_guess(config):
    assert resolve_state(config.states, {"red": STEADY, "green": STEADY}) == UNKNOWN_STATE


def test_ambiguity_check_is_clean_for_the_shipped_config():
    config = load_config(CONFIG_DIR / "machine1.yaml")
    assert find_ambiguous_patterns(config.states, config.segment_names) == []


def test_ambiguity_check_is_skipped_for_absurdly_wide_poles(config):
    # 3 ** 9 > 4096, so the exhaustive walk bails out rather than burning CPU.
    assert find_ambiguous_patterns(config.states, [f"s{i}" for i in range(9)]) == []


# -- loading -----------------------------------------------------------------------


def test_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError, match="cannot read config"):
        load_config(tmp_path / "nope.yaml")


def test_malformed_yaml_raises_config_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("node_id: [unclosed\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_empty_file_raises_config_error(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("")
    with pytest.raises(ConfigError, match="is empty"):
        load_config(path)


def test_relative_paths_resolve_against_the_config_file(tmp_path):
    (tmp_path / "frames").mkdir()
    path = tmp_path / "node.yaml"
    path.write_text(
        "node_id: rel\n"
        "camera:\n"
        "  source: file\n"
        "  resolution: [64, 48]\n"
        "  fps: 5\n"
        "  exposure_us: 800\n"
        "  analogue_gain: 1.0\n"
        "  file:\n"
        "    path: frames\n"
        "segments:\n"
        "  - {name: red, roi: [0, 0, 8, 8], threshold: 90}\n"
        "states:\n"
        "  on_: {red: steady}\n"
    )
    config = load_config(path)
    assert config.camera.file is not None
    assert config.camera.file.path == (tmp_path / "frames").resolve()


def test_file_source_without_a_path_is_rejected(config_dict):
    config_dict["camera"]["source"] = "file"
    with pytest.raises(ConfigError, match=r"no camera\.file\.path"):
        parse_config(config_dict)
