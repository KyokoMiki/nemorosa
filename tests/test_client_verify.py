"""Tests for the post-inject verify decision in TorrentClient."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nemorosa.clients.client_common import TorrentClient
from nemorosa.clients.qbittorrent import QBittorrentClient
from nemorosa.clients.transmission import TransmissionClient

pytestmark = pytest.mark.anyio


def _fake_client(cls: type[TorrentClient]) -> MagicMock:
    client = MagicMock(spec=cls)
    client.supports_fast_resume = cls.supports_fast_resume
    client.skip_check_marks_complete = getattr(cls, "skip_check_marks_complete", True)
    client._rename_torrent = AsyncMock()
    client._rename_file = AsyncMock()
    client._verify_torrent = AsyncMock()
    return client


async def _run(client: MagicMock, hash_match: bool) -> bool:
    with (
        patch("nemorosa.clients.client_common.config") as mock_config,
        patch("nemorosa.clients.client_common.anyio.sleep", new=AsyncMock()),
    ):
        mock_config.cfg.linking.enable_linking = True
        return await TorrentClient._perform_torrent_rename_and_verify(
            client,
            torrent_hash="abc",
            current_name="Album",
            local_torrent_name="Album",
            rename_map={},
            name_differs=False,
            hash_match=hash_match,
        )


class TestVerifyHashMatch:
    async def test_qbittorrent_rechecks_hash_match(self) -> None:
        """qBittorrent adds stopped: a skip-checked hash match stays at 0%
        until rechecked."""
        client = _fake_client(QBittorrentClient)
        assert await _run(client, hash_match=True) is True  # nosec B101
        client._verify_torrent.assert_awaited_once_with("abc")

    async def test_qbittorrent_still_rechecks_non_hash_match(self) -> None:
        client = _fake_client(QBittorrentClient)
        assert await _run(client, hash_match=False) is True  # nosec B101
        client._verify_torrent.assert_awaited_once_with("abc")

    async def test_transmission_hash_match_unchanged(self) -> None:
        client = _fake_client(TransmissionClient)
        assert await _run(client, hash_match=True) is False  # nosec B101
        client._verify_torrent.assert_not_awaited()
