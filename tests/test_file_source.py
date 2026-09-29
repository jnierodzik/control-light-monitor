from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from stacklight.clock import VirtualClock
from stacklight.errors import SourceError
from stacklight.sources import FileSource
from stacklight.sources.file import frame_paths


@pytest.fixture
def corpus(tmp_path):
    """Three frames whose brightness identifies them: 0, 100, 200."""
    for i, level in enumerate((0, 100, 200)):
        arr = np.full((48, 64, 3), level, dtype=np.uint8)
        Image.fromarray(arr).save(tmp_path / f"frame_{i:03d}.png")
    (tmp_path / "notes.txt").write_text("ignored")
    return tmp_path


def make(path, **kwargs):
    kwargs.setdefault("clock", VirtualClock())
    return FileSource(path, (64, 48), 10.0, **kwargs)


def test_replays_a_directory_in_sorted_order(corpus):
    with make(corpus) as source:
        assert [int(f.data.mean().round()) for f in source.frames(limit=3)] == [0, 100, 200]


def test_non_image_files_are_ignored(corpus):
    with make(corpus) as source:
        assert len(source.files) == 3
    assert len(frame_paths(corpus)) == 3


def test_loops_by_default(corpus):
    with make(corpus) as source:
        levels = [int(f.data.mean().round()) for f in source.frames(limit=7)]
    assert levels == [0, 100, 200, 0, 100, 200, 0]


def test_non_looping_source_reports_exhaustion(corpus):
    with make(corpus, loop=False) as source:
        list(source.frames(limit=3))
        with pytest.raises(SourceError, match="exhausted"):
            source.read()
        assert source.exhausted


def test_pattern_filters_the_corpus(corpus):
    with make(corpus, pattern="frame_00[01].png") as source:
        assert len(source.files) == 2


def test_a_single_image_is_a_valid_source(corpus):
    with make(corpus / "frame_001.png") as source:
        assert int(source.read().data.mean().round()) == 100


def test_frames_are_resized_to_the_configured_resolution(tmp_path):
    Image.fromarray(np.zeros((100, 200, 3), dtype=np.uint8)).save(tmp_path / "a.png")
    with make(tmp_path) as source:
        assert source.read().data.shape == (48, 64, 3)


def test_grayscale_images_are_converted_to_rgb(tmp_path):
    Image.fromarray(np.full((48, 64), 128, dtype=np.uint8), mode="L").save(tmp_path / "a.png")
    with make(tmp_path) as source:
        frame = source.read()
    assert frame.data.shape == (48, 64, 3)
    assert frame.data[..., 0].mean() == pytest.approx(128, abs=1)


def test_missing_path_is_an_error(tmp_path):
    with pytest.raises(SourceError, match="does not exist"):
        make(tmp_path / "nope").start()


def test_empty_directory_is_an_error(tmp_path):
    with pytest.raises(SourceError, match="no images matching"):
        make(tmp_path).start()


def test_unsupported_file_type_is_an_error(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello")
    with pytest.raises(SourceError, match="unsupported file source"):
        make(path).start()
