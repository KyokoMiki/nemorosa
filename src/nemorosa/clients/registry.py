"""Torrent client registry and global instance management for nemorosa."""

from typing import TYPE_CHECKING

import anyio
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from ..config import ClientType, DownloaderConfig
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
    ClientType.TRANSMISSION: TransmissionClient,
    ClientType.QBITTORRENT: QBittorrentClient,
    ClientType.DELUGE: DelugeClient,
    ClientType.RTORRENT: RTorrentClient,
}


def create_torrent_client(
    downloader_config: DownloaderConfig,
    database: "NemorosaDatabase",
    scheduler: AsyncIOScheduler,
    notifier: "Notifier | None" = None,
) -> TorrentClient:
    """Create a torrent client instance based on client_type.

    Args:
        downloader_config: DownloaderConfig instance for this client.
        database: NemorosaDatabase instance for persistence.
        scheduler: AsyncIOScheduler instance for scheduling.
        notifier: Optional Notifier instance for push notifications.

    Returns:
        Configured torrent client instance.

    Raises:
        ValueError: If client type is not supported.
    """
    client_type = downloader_config.type

    if client_type not in TORRENT_CLIENT_MAPPING:
        raise ValueError(f"Unsupported torrent client type: {client_type}")

    return TORRENT_CLIENT_MAPPING[client_type](
        downloader_config,
        database,
        scheduler,
        notifier,
    )


# Global torrent client instances
_torrent_clients: list[TorrentClient] = []
_torrent_clients_lock = anyio.Lock()


async def init_torrent_clients(
    downloader_configs: list[DownloaderConfig],
    database: "NemorosaDatabase",
    scheduler: AsyncIOScheduler,
    notifier: "Notifier | None" = None,
) -> None:
    """Initialize global torrent client instances.

    Should be called once during application startup.

    Args:
        downloader_configs: List of DownloaderConfig instances.
        database: NemorosaDatabase instance for persistence.
        scheduler: AsyncIOScheduler instance for scheduling background tasks.
        notifier: Optional Notifier instance for push notifications.

    Raises:
        RuntimeError: If already initialized.
    """
    global _torrent_clients
    async with _torrent_clients_lock:
        if _torrent_clients:
            raise RuntimeError("Torrent clients already initialized.")

        _torrent_clients = [
            create_torrent_client(dl_config, database, scheduler, notifier)
            for dl_config in downloader_configs
        ]


def get_torrent_clients() -> list[TorrentClient]:
    """Get global torrent client instances.

    Must be called after init_torrent_clients() has been invoked.

    Returns:
        List of torrent client instances.

    Raises:
        RuntimeError: If torrent clients have not been initialized.
    """
    if not _torrent_clients:
        raise RuntimeError(
            "Torrent clients not initialized. Call init_torrent_clients() first."
        )
    return _torrent_clients


def find_client_by_key(
    clients: list[TorrentClient],
    client_key: str,
) -> TorrentClient | None:
    """Find torrent client by client key.

    Args:
        clients: List of torrent client instances to search.
        client_key: Client key to match.

    Returns:
        The matching TorrentClient, or None if no match is found.
    """
    return next((c for c in clients if c.client_key == client_key), None)
