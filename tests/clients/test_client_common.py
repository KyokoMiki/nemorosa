"""Unit tests for ``TorrentClient.post_process_single_injected_torrent``.

Focused on the ``keep_partial_torrents`` opt-in (PR #190): a partial match that
fails ``should_keep_partial_torrent`` should be kept paused for manual review
when the user opts in, and removed (today's default behaviour) otherwise.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nemorosa.clients.client_common import (
    ClientTorrentInfo,
    PostProcessStatus,
    TorrentClient,
    TorrentState,
)

pytestmark = pytest.mark.anyio

MATCHED_HASH = "deadbeef"


class _StubClient(TorrentClient):
    """Concrete ``TorrentClient`` whose abstract methods are inert.

    Only the members exercised by ``post_process_single_injected_torrent`` are
    replaced with mocks in the ``client`` fixture; the remaining abstract
    methods exist purely so the base class can be instantiated.
    """

    async def get_torrents(self, torrent_hashes=None, fields=None):
        raise NotImplementedError

    async def get_torrent_info(self, torrent_hash, fields):
        raise NotImplementedError

    async def get_torrents_for_monitoring(self, torrent_hashes):
        raise NotImplementedError

    async def _add_torrent(
        self, torrent_data, download_dir, hash_match, local_torrent_hash=""
    ):
        raise NotImplementedError

    async def _remove_torrent(self, torrent_hash):
        raise NotImplementedError

    async def _rename_torrent(self, torrent_hash, old_name, new_name):
        raise NotImplementedError

    async def _rename_file(self, torrent_hash, old_path, new_name):
        raise NotImplementedError

    async def _verify_torrent(self, torrent_hash):
        raise NotImplementedError

    async def _process_rename_map(self, torrent_hash, base_path, rename_map):
        raise NotImplementedError

    async def _get_torrent_data(self, torrent_hash):
        raise NotImplementedError

    async def _resume_torrent(self, torrent_hash):
        raise NotImplementedError


@pytest.fixture
def client() -> _StubClient:
    """Stub client with a mocked database and a mocked ``_remove_torrent``."""
    downloader_config = MagicMock()
    downloader_config.client_key = "test-client"

    database = MagicMock()
    database.update_scan_result_checked = AsyncMock()
    database.clear_matched_torrent_info = AsyncMock()

    stub = _StubClient(downloader_config, database, MagicMock(), notifier=None)
    stub._remove_torrent = AsyncMock()
    return stub


@pytest.fixture
def partial_torrent() -> ClientTorrentInfo:
    """A partial (50%) match that is not currently being checked."""
    return ClientTorrentInfo(
        hash=MATCHED_HASH,
        name="Some Artist - Some Album",
        progress=0.5,
        state=TorrentState.PAUSED,
    )


async def test_partial_kept_when_opted_in(
    client: _StubClient, partial_torrent: ClientTorrentInfo
) -> None:
    """keep_partial_torrents=True keeps a failed-validation partial paused."""
    client.get_torrent_info = AsyncMock(return_value=partial_torrent)

    with (
        patch("nemorosa.clients.client_common.config") as mock_config,
        patch(
            "nemorosa.clients.client_common.filecompare.should_keep_partial_torrent",
            return_value=False,
        ),
    ):
        mock_config.cfg.linking.enable_linking = False
        mock_config.cfg.global_config.keep_partial_torrents = True

        result = await client.post_process_single_injected_torrent(MATCHED_HASH)

    assert result.status == PostProcessStatus.PARTIAL_KEPT
    client._remove_torrent.assert_not_awaited()
    client.database.update_scan_result_checked.assert_awaited_once_with(
        client.client_key, MATCHED_HASH, True
    )


async def test_partial_removed_when_not_opted_in(
    client: _StubClient, partial_torrent: ClientTorrentInfo
) -> None:
    """Default (opt-out, no reflink) removes the failed-validation partial."""
    client.get_torrent_info = AsyncMock(return_value=partial_torrent)

    with (
        patch("nemorosa.clients.client_common.config") as mock_config,
        patch(
            "nemorosa.clients.client_common.filecompare.should_keep_partial_torrent",
            return_value=False,
        ),
    ):
        mock_config.cfg.linking.enable_linking = False
        mock_config.cfg.global_config.keep_partial_torrents = False

        result = await client.post_process_single_injected_torrent(MATCHED_HASH)

    assert result.status == PostProcessStatus.PARTIAL_REMOVED
    client._remove_torrent.assert_awaited_once_with(MATCHED_HASH)
    client.database.clear_matched_torrent_info.assert_awaited_once_with(
        client.client_key, MATCHED_HASH
    )
    client.database.update_scan_result_checked.assert_not_awaited()
