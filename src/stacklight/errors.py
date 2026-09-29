"""Exception types raised by the stack light monitor."""

from __future__ import annotations


class StackLightError(Exception):
    """Base class for every error this package raises deliberately."""


class ConfigError(StackLightError):
    """Configuration file is missing, malformed, or internally inconsistent."""


class SourceError(StackLightError):
    """A frame source failed to produce frames."""


class SourceUnavailableError(SourceError):
    """A frame source cannot be constructed in this environment.

    Typically raised when the Pi camera stack is requested on a machine without
    ``picamera2`` installed.
    """
