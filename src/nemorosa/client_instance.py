"""
Torrent Client Instance Module

This module manages the global torrent client instance and provides a unified
interface for different torrent clients including Transmission, qBittorrent, Deluge, and rTorrent.
It handles singleton pattern for the torrent client to ensure consistent access
across the application.
"""

import asyncio
from urllib.parse import urlparse

from .clients import DelugeClient, QBittorrentClient, RTorrentClient, TorrentClient, TransmissionClient

# Torrent client factory mapping
TORRENT_CLIENT_MAPPING = {
    "transmission": TransmissionClient,
    "qbittorrent": QBittorrentClient,
    "deluge": DelugeClient,
    "rtorrent": RTorrentClient,
}


def create_torrent_client(url: str) -> TorrentClient:
    """Create a torrent client instance based on the URL scheme

    Args:
        url: The torrent client URL

    Returns:
        TorrentClient: Configured torrent client instance

    Raises:
        ValueError: If URL is empty, None, or client type is not supported
    """
    if not url or not url.strip():
        raise ValueError("URL cannot be empty")

    parsed = urlparse(url)
    client_type = parsed.scheme.split("+")[0]

    if client_type not in TORRENT_CLIENT_MAPPING:
        raise ValueError(f"Unsupported torrent client type: {client_type}")

    return TORRENT_CLIENT_MAPPING[client_type](url)


# Global torrent client instance
_torrent_client_instance: TorrentClient | None = None
_torrent_client_lock = asyncio.Lock()


async def init_torrent_client(url: str) -> None:
    """Initialize global torrent client instance.

    Should be called once during application startup.

    Args:
        url: The torrent client URL.

    Raises:
        RuntimeError: If already initialized.
    """
    global _torrent_client_instance
    async with _torrent_client_lock:
        if _torrent_client_instance is not None:
            raise RuntimeError("Torrent client already initialized.")

        _torrent_client_instance = create_torrent_client(url)


def get_torrent_client() -> TorrentClient:
    """Get global torrent client instance.

    Must be called after init_torrent_client() has been invoked.

    Returns:
        TorrentClient: Torrent client instance.

    Raises:
        RuntimeError: If torrent client has not been initialized.
    """
    if _torrent_client_instance is None:
        raise RuntimeError("Torrent client not initialized. Call init_torrent_client() first.")
    return _torrent_client_instance
