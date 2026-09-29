from __future__ import annotations

import json
import logging

import pytest

from stacklight.logging_setup import configure_logging


def test_json_format_emits_one_object_per_line_with_extras(capsys):
    configure_logging("INFO", "json")
    logging.getLogger("stacklight.test").info("state committed", extra={"state": "running"})
    payload = json.loads(capsys.readouterr().err.strip())
    assert payload["msg"] == "state committed"
    assert payload["state"] == "running"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "stacklight.test"


def test_text_format_appends_extras(capsys):
    configure_logging("DEBUG", "text")
    logging.getLogger("stacklight.test").debug("sampled", extra={"value": 142.3})
    assert "value=142.3" in capsys.readouterr().err


def test_level_is_respected(capsys):
    configure_logging("WARNING", "text")
    logging.getLogger("stacklight.test").info("should not appear")
    assert capsys.readouterr().err == ""


def test_unknown_format_is_rejected():
    with pytest.raises(ValueError, match="unknown log format"):
        configure_logging("INFO", "xml")
