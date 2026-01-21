"""Notification module for nemorosa using Apprise."""

from typing import TYPE_CHECKING

import apprise

from . import logger

if TYPE_CHECKING:
    from .core import ProcessorStats


class Notifier:
    """Push notification handler using Apprise."""

    def __init__(self, urls: list[str]):
        """Initialize the notifier with Apprise URLs.

        Args:
            urls: List of Apprise notification URLs.

        Raises:
            ValueError: If any URL is invalid.
        """
        self.apprise = apprise.Apprise()

        for url in urls:
            if not self.apprise.add(url):
                raise ValueError(
                    f"Invalid notification URL: {logger.redact_url_password(url)}"
                )
            logger.debug("Added notification URL: %s", logger.redact_url_password(url))

        logger.info(
            "Notifier initialized with %d notification service(s)", len(self.apprise)
        )

    async def notify(
        self,
        title: str,
        body: str,
        notify_type: apprise.NotifyType = apprise.NotifyType.INFO,
    ) -> bool:
        """Send notification to all configured services.

        Args:
            title: Notification title.
            body: Notification body/message.
            notify_type: Type of notification (INFO, SUCCESS, WARNING, FAILURE).

        Returns:
            True if at least one notification was sent successfully.
            False if notifications failed to send OR if no notification
            services are configured/filtered.
        """
        try:
            result = await self.apprise.async_notify(
                title=title,
                body=body,
                notify_type=notify_type,
            )
            if result:
                logger.debug("Notification sent: %s", title)
            else:
                logger.warning("Failed to send notification: %s", title)
            return bool(result)
        except Exception as e:
            logger.error("Error sending notification: %s", e)
            return False

    async def send_test(self) -> bool:
        """Send a test notification.

        Returns:
            True if test notification was sent successfully.
        """
        return await self.notify(
            title="nemorosa",
            body="Test notification - nemorosa is configured correctly!",
            notify_type=apprise.NotifyType.INFO,
        )

    async def send_inject_success(
        self,
        torrent_name: str,
        torrent_hash: str,
        progress: float,
    ) -> bool:
        """Send notification for successful torrent injection.

        Args:
            torrent_name: Name of the injected torrent.
            torrent_hash: Hash of the injected torrent.
            progress: Download progress of the torrent (0.0 to 1.0).

        Returns:
            True if notification was sent successfully.
        """
        progress_pct = f"{progress * 100:.1f}%"
        return await self.notify(
            title="nemorosa - Torrent Injected",
            body=(
                f"✅ Injected: {torrent_name}\n"
                f"Hash: {torrent_hash}\n"
                f"Progress: {progress_pct}"
            ),
            notify_type=apprise.NotifyType.SUCCESS,
        )

    async def send_inject_failure(
        self,
        torrent_name: str,
        torrent_url: str,
        reason: str,
    ) -> bool:
        """Send notification for failed torrent injection.

        Args:
            torrent_name: Name of the torrent that failed to inject.
            torrent_url: Permalink URL to the torrent on target tracker.
            reason: Reason for failure.

        Returns:
            True if notification was sent successfully.
        """
        return await self.notify(
            title="nemorosa - Injection Failed",
            body=(
                f"❌ Failed: {torrent_name}\nPermalink: {torrent_url}\nReason: {reason}"
            ),
            notify_type=apprise.NotifyType.FAILURE,
        )

    async def send_scan_complete(self, stats: "ProcessorStats") -> bool:
        """Send notification for scan completion summary.

        Args:
            stats: Processing statistics from the scan session.

        Returns:
            True if notification was sent successfully.
        """
        return await self.notify(
            title="nemorosa - Scan Complete",
            body=f"📊 Scan Summary\n"
            f"Scanned: {stats.scanned}\n"
            f"Matches Found: {stats.found}\n"
            f"Injected: {stats.downloaded}",
            notify_type=apprise.NotifyType.INFO,
        )

    async def send_announce_success(
        self,
        torrent_name: str,
        torrent_url: str,
    ) -> bool:
        """Send notification for successful announce processing.

        Args:
            torrent_name: Name of the announced torrent.
            torrent_url: Permalink URL to the torrent.

        Returns:
            True if notification was sent successfully.
        """
        return await self.notify(
            title="nemorosa - Announce Processed",
            body=f"✅ Announce: {torrent_name}\nPermalink: {torrent_url}",
            notify_type=apprise.NotifyType.SUCCESS,
        )


# Global notifier instance
_notifier_instance: Notifier | None = None


def init_notifier(urls: list[str]) -> None:
    """Initialize global notifier instance.

    Should be called once during application startup.

    Args:
        urls: List of Apprise notification URLs.

    Raises:
        RuntimeError: If already initialized.
        ValueError: If any URL is invalid.
    """
    global _notifier_instance
    if _notifier_instance is not None:
        raise RuntimeError("Notifier already initialized.")

    _notifier_instance = Notifier(urls)


def get_notifier() -> Notifier:
    """Get global notifier instance.

    Must be called after init_notifier() has been invoked and only when
    notification URLs are configured.

    Returns:
        Notifier instance.

    Raises:
        RuntimeError: If notifier has not been initialized.
    """
    if _notifier_instance is None:
        raise RuntimeError("Notifier not initialized. Call init_notifier() first.")
    return _notifier_instance
