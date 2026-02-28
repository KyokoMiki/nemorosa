"""Shared fixtures for core tests."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from torf import Torrent

from nemorosa.clients.client_common import (
    ClientTorrentFile,
    ClientTorrentInfo,
    TorrentClient,
)


@pytest.fixture
def mock_torrent_client() -> MagicMock:
    """Create a mock TorrentClient."""
    client = MagicMock(spec=TorrentClient)
    client.inject_torrent = AsyncMock(return_value=(True, "hash123"))
    client.get_torrent_object = AsyncMock(return_value=None)
    client.get_filtered_torrents = AsyncMock(return_value={})
    client.track_verification = AsyncMock()
    return client


@pytest.fixture
def sample_torrent(tmp_path) -> Torrent:
    """Create a sample Torrent object for testing."""
    content_dir = tmp_path / "test_album"
    content_dir.mkdir()
    (content_dir / "01 - Track.flac").write_bytes(b"\x00" * 1024)
    t = Torrent(path=str(content_dir))
    t.generate()
    return t


@pytest.fixture
def sample_local_info() -> ClientTorrentInfo:
    """Create sample ClientTorrentInfo for testing."""
    return ClientTorrentInfo(
        hash="abc123",
        name="Test Album",
        total_size=30000000,
        files=[
            ClientTorrentFile(
                name="Test Album/01 - Track.flac", size=30000000, progress=1.0
            ),
        ],
        download_dir="/downloads",
    )


@pytest.fixture
def sample_torrent_details() -> ClientTorrentInfo:
    """Create sample ClientTorrentInfo with multiple files."""
    return ClientTorrentInfo(
        hash="abc123def456",
        name="Test Album",
        total_size=50000000,
        files=[
            ClientTorrentFile(
                name="Test Album/01 - Track One.flac", size=30000000, progress=1.0
            ),
            ClientTorrentFile(
                name="Test Album/02 - Track Two.flac", size=20000000, progress=1.0
            ),
        ],
        download_dir="/downloads",
    )
