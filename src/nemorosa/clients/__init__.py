"""Torrent client implementations for nemorosa."""

from .client_common import (
    ClientTorrentFile,
    ClientTorrentInfo,
    FieldSpec,
    PostProcessResult,
    PostProcessStatus,
    TorrentClient,
    TorrentConflictError,
    TorrentState,
)
from .deluge import DelugeClient
from .qbittorrent import QBittorrentClient
from .registry import (
    create_torrent_client,
    get_torrent_client,
    init_torrent_client,
)
from .rtorrent import RTorrentClient
from .transmission import TransmissionClient

__all__ = [
    "ClientTorrentFile",
    "ClientTorrentInfo",
    "DelugeClient",
    "FieldSpec",
    "PostProcessResult",
    "PostProcessStatus",
    "QBittorrentClient",
    "RTorrentClient",
    "TorrentClient",
    "TorrentConflictError",
    "TorrentState",
    "TransmissionClient",
    "create_torrent_client",
    "get_torrent_client",
    "init_torrent_client",
]
