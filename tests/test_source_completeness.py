"""Tests that a torrent still downloading is never used as a cross-seed source."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from nemorosa.clients.client_common import (
    ClientTorrentFile,
    ClientTorrentInfo,
    TorrentClient,
)
from nemorosa.clients.qbittorrent import QBittorrentClient

pytestmark = pytest.mark.anyio

OPS = "https://home.opsfet.ch/announce"
RED = "https://flacsfor.me/announce"


def _torrent(
    infohash: str, progress: float, tracker: str, files: int = 2
) -> ClientTorrentInfo:
    return ClientTorrentInfo(
        hash=infohash,
        name="Album",
        progress=progress,
        total_size=100 * files,
        files=[
            ClientTorrentFile(name=f"Album/{i}.flac", size=100, progress=progress)
            for i in range(files)
        ],
        trackers=[tracker],
        download_dir="/data",
    )


def _fake_client() -> MagicMock:
    client = MagicMock(spec=QBittorrentClient)
    client._should_include_torrent = MagicMock(return_value=True)
    return client


class TestGroupTorrentsByContent:
    def test_incomplete_torrent_is_not_a_source(self) -> None:
        mapping, valid = TorrentClient._group_torrents_by_content(
            _fake_client(), [_torrent("a", 0.4, RED)], ["flacsfor.me", "opsfet.ch"]
        )
        assert "Album" not in valid  # nosec B101
        # ...but it still counts as present on its tracker
        assert mapping["Album"] == {"flacsfor.me"}  # nosec B101

    def test_complete_copy_wins_over_smaller_incomplete_one(self) -> None:
        complete = _torrent("a", 1.0, RED, files=3)
        downloading = _torrent("b", 0.4, OPS, files=1)  # fewer files: would win
        mapping, valid = TorrentClient._group_torrents_by_content(
            _fake_client(), [complete, downloading], ["flacsfor.me", "opsfet.ch"]
        )
        assert valid["Album"].hash == "a"  # nosec B101
        assert mapping["Album"] == {"flacsfor.me", "opsfet.ch"}  # nosec B101


class TestGetSingleTorrent:
    async def test_incomplete_torrent_is_filtered_out(self) -> None:
        client = _fake_client()
        client.get_torrent_info = AsyncMock(return_value=_torrent("a", 0.4, RED))
        client.get_torrents = AsyncMock(return_value=[])
        result = await TorrentClient.get_single_torrent(client, "a", {"opsfet.ch"})
        assert result is None  # nosec B101
        client.get_torrents.assert_not_awaited()

    async def test_complete_torrent_is_returned(self) -> None:
        client = _fake_client()
        client.get_torrent_info = AsyncMock(return_value=_torrent("a", 1.0, RED))
        client.get_torrents = AsyncMock(return_value=[_torrent("a", 1.0, RED)])
        result = await TorrentClient.get_single_torrent(client, "a", {"flacsfor.me"})
        assert result is not None  # nosec B101
        assert result.existing_target_trackers == {"flacsfor.me"}  # nosec B101
