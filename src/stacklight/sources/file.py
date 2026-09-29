"""Replay frames from a directory of images, a single image, or a video file.

Used for regression tests against real captures: the calibration UI's "capture sample"
button writes into a directory, and that directory becomes a fixture that pins down
detection behaviour against light the real machine actually produced.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image

from ..clock import Clock
from ..errors import SourceError
from .base import CameraControls, FrameSource

log = logging.getLogger(__name__)

IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".bmp", ".ppm", ".tif", ".tiff"})
VIDEO_SUFFIXES = frozenset({".mp4", ".mkv", ".avi", ".mov", ".m4v", ".webm"})


class FileSource(FrameSource):
    """Replays images or video on a loop at the configured frame rate."""

    kind = "file"

    def __init__(
        self,
        path: str | Path,
        resolution: tuple[int, int],
        fps: float,
        *,
        loop: bool = True,
        pattern: str = "*",
        controls: CameraControls | None = None,
        clock: Clock | None = None,
    ) -> None:
        super().__init__(resolution, fps, controls=controls, clock=clock)
        self._path = Path(path).expanduser()
        self._loop = loop
        self._pattern = pattern
        self._files: tuple[Path, ...] = ()
        self._index = 0
        self._video: object | None = None
        self._exhausted = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def files(self) -> tuple[Path, ...]:
        return self._files

    @property
    def exhausted(self) -> bool:
        """True when a non-looping source has run out of frames."""
        return self._exhausted

    def _on_start(self) -> None:
        self._index = 0
        self._exhausted = False
        if not self._path.exists():
            raise SourceError(f"file source path does not exist: {self._path}")

        if self._path.is_dir():
            self._files = tuple(
                sorted(
                    p
                    for p in self._path.glob(self._pattern)
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
                )
            )
            if not self._files:
                raise SourceError(
                    f"no images matching {self._pattern!r} in {self._path} "
                    f"(looked for {sorted(IMAGE_SUFFIXES)})"
                )
            log.info(
                "replaying image directory",
                extra={"path": str(self._path), "frames": len(self._files)},
            )
        elif self._path.suffix.lower() in VIDEO_SUFFIXES:
            self._video = self._open_video()
        elif self._path.suffix.lower() in IMAGE_SUFFIXES:
            self._files = (self._path,)
        else:
            raise SourceError(
                f"unsupported file source {self._path}: expected a directory, an image "
                f"{sorted(IMAGE_SUFFIXES)}, or a video {sorted(VIDEO_SUFFIXES)}"
            )

    def _on_stop(self) -> None:
        if self._video is not None:
            self._video.release()  # type: ignore[attr-defined]
            self._video = None

    def _open_video(self) -> object:
        # OpenCV is an optional extra; nothing else in the hot path needs it.
        try:
            import cv2
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise SourceError(
                f"reading {self._path.name} needs OpenCV. Install the optional extra: "
                "pip install -e '.[opencv]' -- or point camera.file.path at a directory "
                "of images instead."
            ) from exc
        capture = cv2.VideoCapture(str(self._path))
        if not capture.isOpened():
            raise SourceError(f"could not open video {self._path}")
        log.info("replaying video", extra={"path": str(self._path)})
        return capture

    def _capture(self, t: float) -> np.ndarray:
        if self._video is not None:
            return self._next_video_frame()
        return self._next_image_frame()

    def _next_image_frame(self) -> np.ndarray:
        if self._index >= len(self._files):
            if not self._loop:
                self._exhausted = True
                raise SourceError(f"file source exhausted after {len(self._files)} frame(s)")
            self._index = 0
        frame_path = self._files[self._index]
        self._index += 1
        try:
            with Image.open(frame_path) as img:
                return self._to_array(img)
        except OSError as exc:
            raise SourceError(f"cannot read frame {frame_path}: {exc}") from exc

    def _next_video_frame(self) -> np.ndarray:
        import cv2

        ok, frame = self._video.read()  # type: ignore[attr-defined]
        if not ok:
            if not self._loop:
                self._exhausted = True
                raise SourceError(f"video {self._path} exhausted")
            self._video.set(cv2.CAP_PROP_POS_FRAMES, 0)  # type: ignore[attr-defined]
            ok, frame = self._video.read()  # type: ignore[attr-defined]
            if not ok:
                raise SourceError(f"video {self._path} produced no frames")
        rgb = frame[:, :, ::-1]
        return self._to_array(Image.fromarray(rgb))

    def _to_array(self, img: Image.Image) -> np.ndarray:
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.size != self._resolution:
            log.debug(
                "resizing replayed frame",
                extra={"from": img.size, "to": self._resolution},
            )
            img = img.resize(self._resolution, Image.Resampling.BILINEAR)
        return np.asarray(img, dtype=np.uint8)


def frame_paths(directory: Path, pattern: str = "*") -> Sequence[Path]:
    """List the image files ``FileSource`` would replay from ``directory``."""
    return sorted(
        p
        for p in Path(directory).glob(pattern)
        if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES
    )
