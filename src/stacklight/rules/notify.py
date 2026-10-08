"""Push notifications via ntfy.

Behind an interface, same as the frame sources and the publisher: the engine talks to a
Notifier, so every rule can be tested without sending anything to a phone.

stdlib urllib rather than requests -- this is one POST, and a node that only needs to
make one HTTP call should not drag in a dependency for it.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .config import NtfyConfig

log = logging.getLogger(__name__)


class NotifyError(Exception):
    """A notification could not be delivered."""


@dataclass(frozen=True)
class Notification:
    title: str
    message: str
    priority: str = "default"
    tags: tuple[str, ...] = ()
    #: Free-form, for the history log: "state", "watchdog", "staleness".
    kind: str = "state"
    node_id: str = ""
    state: str | None = None
    escalation: int = 0


class Notifier(ABC):
    @abstractmethod
    def send(self, notification: Notification) -> None:
        """Deliver one notification, or raise :class:`NotifyError`."""


@dataclass
class RecordingNotifier(Notifier):
    """Keeps notifications in a list. For tests and for --dry-run."""

    sent: list[Notification] = field(default_factory=list)
    failing: bool = False

    def send(self, notification: Notification) -> None:
        if self.failing:
            raise NotifyError("recording notifier is set to fail")
        self.sent.append(notification)

    def titles(self) -> list[str]:
        return [n.title for n in self.sent]

    def clear(self) -> None:
        self.sent.clear()


class NtfyNotifier(Notifier):
    """Posts to an ntfy server.

    Note that on ntfy.sh the topic name is the only secret -- there are no accounts, so
    anyone who knows it can both read these notifications and publish convincing fakes.
    Config validation rejects topic names short enough to guess.
    """

    def __init__(self, config: NtfyConfig) -> None:
        self._config = config
        self._url = f"{config.server}/{config.topic}"

    @property
    def url(self) -> str:
        return self._url

    def send(self, notification: Notification) -> None:
        headers = {
            "Title": notification.title,
            "Priority": notification.priority,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if notification.tags:
            headers["Tags"] = ",".join(notification.tags)
        if self._config.token:
            headers["Authorization"] = f"Bearer {self._config.token}"

        request = urllib.request.Request(
            self._url,
            data=notification.message.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_s) as response:
                if response.status >= 400:
                    raise NotifyError(f"ntfy returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            raise NotifyError(f"ntfy returned HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NotifyError(f"could not reach {self._config.server}: {exc}") from exc

        log.info(
            "notification sent",
            extra={
                "title": notification.title,
                "priority": notification.priority,
                "kind": notification.kind,
                "node_id": notification.node_id,
            },
        )
