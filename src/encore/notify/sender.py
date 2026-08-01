"""The Apprise adapter — the one module allowed to import ``apprise``.

Apprise is the whole of F4's outbound reach: ~90 services (ntfy, email,
Discord, Telegram, Pushover, and the generic webhook that is the published,
boundary-preserving answer to "integrate it with my downloader" — README
non-goals, roadmap risk R4) behind one dependency and one URL grammar.

The `NotificationSender` protocol is the seam the delivery engine talks to,
so the engine's retry/backoff/digest logic is testable without a network and
a second implementation (a webhook-only sender, say) needs no engine change.

Privacy: an Apprise URL is a credential. It arrives here decrypted, is used,
and is never logged, never echoed into an exception message, and never
repr'd — `DeliveryError` messages are built from the *failure*, not from the
destination. Enforced by a ``no_secrets_in_logs`` marker test.
"""

from __future__ import annotations

import logging
from typing import Protocol

from encore.notify.render import RenderedNotification

__all__ = ["AppriseSender", "DeliveryError", "NotificationSender"]

logger = logging.getLogger(__name__)


class DeliveryError(Exception):
    """A notification could not be delivered to a channel.

    The message describes the failure only. It must never contain the
    channel URL, the artist, or the release — it is written to the channel's
    ``last_error`` column, which the CLI and (later) the UI display.
    """


class NotificationSender(Protocol):
    """Sends a rendered notification to one Apprise-style destination URL."""

    def send(self, url: str, notification: RenderedNotification) -> None:
        """Deliver ``notification`` to ``url``.

        Raises:
            DeliveryError: the destination rejected the message, was
                unreachable, or the URL was not a usable Apprise target.
        """
        ...  # pragma: no cover - protocol declaration


class AppriseSender:
    """The real sender: hands the message to Apprise for the configured service."""

    def send(self, url: str, notification: RenderedNotification) -> None:
        """Deliver one notification through Apprise.

        Raises:
            DeliveryError: Apprise rejected the URL, or reported that no
                configured service accepted the message.
        """
        # Imported lazily so the import cost (Apprise loads ~90 service
        # plugins at import time) is paid by processes that actually deliver,
        # not by `encore sync` or a test that only renders.
        import apprise

        target = apprise.Apprise()
        # Broad excepts on purpose: these call into ~90 third-party service
        # plugins, and every way they can fail is, from here, one thing — the
        # notification did not go out. The exception *type* is reported; its
        # message is not, because a plugin is free to echo the URL into it.
        try:
            accepted = bool(target.add(url))
        except Exception as exc:
            raise DeliveryError(
                f"Apprise could not parse the channel URL ({type(exc).__name__})"
            ) from exc
        if not accepted:
            raise DeliveryError(
                "Apprise did not recognize the channel URL — check the service prefix "
                "(see https://github.com/caronc/apprise#supported-notifications)"
            )
        try:
            delivered = bool(target.notify(title=notification.title, body=notification.body))
        except Exception as exc:
            raise DeliveryError(f"the notification service raised {type(exc).__name__}") from exc
        if not delivered:
            raise DeliveryError("the notification service rejected or dropped the message")
