"""
Torrent Client Instance Module

This module manages the global torrent client instance and provides a unified
interface for different torrent clients including Transmission, qBittorrent, Deluge, and rTorrent.
It handles singleton pattern for the torrent client to ensure consistent access
across the application.
"""

import threading
from urllib.parse import urlparse

from . import config
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
        ValueError: If URL is empty or client type is not supported
        TypeError: If URL is None
    """
    if not url.strip():
        raise ValueError("URL cannot be empty")

    parsed = urlparse(url)
    client_type = parsed.scheme.split("+")[0]

    if client_type not in TORRENT_CLIENT_MAPPING:
        raise ValueError(f"Unsupported torrent client type: {client_type}")

    return TORRENT_CLIENT_MAPPING[client_type](url)


# Global torrent client instance
_torrent_client_instance: TorrentClient | None = None
_torrent_client_lock = threading.Lock()


def get_torrent_client() -> TorrentClient:
    """Get global torrent client instance.

    Returns:
        TorrentClient: Torrent client instance.
    """
    global _torrent_client_instance
    with _torrent_client_lock:
        if _torrent_client_instance is None:
            # Get client URL from config
            client_url = config.cfg.downloader.client
            _torrent_client_instance = create_torrent_client(client_url)
        return _torrent_client_instance


def set_torrent_client(torrent_client: TorrentClient) -> None:
    """Set global torrent client instance.

    Args:
        torrent_client: Torrent client instance to set as current.
    """
    global _torrent_client_instance
    with _torrent_client_lock:
        _torrent_client_instance = torrent_client
