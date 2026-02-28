"""Torrent client registry and global instance management for nemorosa."""

from typing import TYPE_CHECKING
from urllib.parse import urlparse

import anyio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .client_common import TorrentClient
from .deluge import DelugeClient
from .qbittorrent import QBittorrentClient
from .rtorrent import RTorrentClient
from .transmission import TransmissionClient

if TYPE_CHECKING:
    from ..db import NemorosaDatabase
    from ..notifier import Notifier


# Torrent client factory mapping
TORRENT_CLIENT_MAPPING = {
    "transmission": TransmissionClient,
    "qbittorrent": QBittorrentClient,
    "deluge": DelugeClient,
    "rtorrent": RTorrentClient,
}


def create_torrent_client(
    url: str,
    database: "NemorosaDatabase",
    scheduler: AsyncIOScheduler,
    notifier: "Notifier | None" = None,
) -> TorrentClient:
    """Create a torrent client instance based on the URL scheme.

    Args:
        url: The torrent client URL.
        database: NemorosaDatabase instance for persistence.
        scheduler: AsyncIOScheduler instance for scheduling background tasks.
        notifier: Optional Notifier instance for push notifications.

    Returns:
        Configured torrent client instance.

    Raises:
        ValueError: If URL is empty, None, or client type is not supported.
    """
    if not url or not url.strip():
        raise ValueError("URL cannot be empty")

    parsed = urlparse(url)
    client_type = parsed.scheme.split("+")[0]

    if client_type not in TORRENT_CLIENT_MAPPING:
        raise ValueError(f"Unsupported torrent client type: {client_type}")

    return TORRENT_CLIENT_MAPPING[client_type](
        url,
        database,
        scheduler,
        notifier,
    )


# Global torrent client instance
_torrent_client_instance: TorrentClient | None = None
_torrent_client_lock = anyio.Lock()


async def init_torrent_client(
    url: str,
    database: "NemorosaDatabase",
    scheduler: AsyncIOScheduler,
    notifier: "Notifier | None" = None,
) -> None:
    """Initialize global torrent client instance.

    Should be called once during application startup.

    Args:
        url: The torrent client URL.
        database: NemorosaDatabase instance for persistence.
        scheduler: AsyncIOScheduler instance for scheduling background tasks.
        notifier: Optional Notifier instance for push notifications.

    Raises:
        RuntimeError: If already initialized.
    """
    global _torrent_client_instance
    async with _torrent_client_lock:
        if _torrent_client_instance is not None:
            raise RuntimeError("Torrent client already initialized.")

        _torrent_client_instance = create_torrent_client(
            url, database, scheduler, notifier
        )


def get_torrent_client() -> TorrentClient:
    """Get global torrent client instance.

    Must be called after init_torrent_client() has been invoked.

    Returns:
        Torrent client instance.

    Raises:
        RuntimeError: If torrent client has not been initialized.
    """
    if _torrent_client_instance is None:
        raise RuntimeError(
            "Torrent client not initialized. Call init_torrent_client() first."
        )
    return _torrent_client_instance
