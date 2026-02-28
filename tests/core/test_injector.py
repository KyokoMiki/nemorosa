"""Unit tests for TorrentInjector."""

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from torf import Torrent

from nemorosa.clients.client_common import ClientTorrentInfo, TorrentClient
from nemorosa.core.injector import TorrentInjector

pytestmark = pytest.mark.anyio


# --- Fixtures ---


@pytest.fixture
def injector(mock_torrent_client: MagicMock) -> TorrentInjector:
    """Create a TorrentInjector with mock client."""
    return TorrentInjector(mock_torrent_client)


# --- Tests for prepare_linked_download_dir ---


class TestPrepareLinkedDownloadDir:
    """Tests for TorrentInjector.prepare_linked_download_dir."""

    async def test_returns_original_dir_when_linking_disabled(
        self, injector: TorrentInjector, sample_torrent: Torrent
    ) -> None:
        """Should return original download_dir when linking is disabled."""
        with patch("nemorosa.core.injector.config") as mock_config:
            mock_config.cfg.linking.enable_linking = False

            result = await injector.prepare_linked_download_dir(
                torrent_object=sample_torrent,
                local_fdict={"01 - Track.flac": 30000000},
                matched_fdict={"01 - Track.flac": 30000000},
                download_dir="/downloads",
                torrent_name="Test Album",
            )

        assert result == "/downloads"

    async def test_returns_linked_dir_when_linking_enabled(
        self, injector: TorrentInjector, sample_torrent: Torrent
    ) -> None:
        """Should return linked directory when linking succeeds."""
        with (
            patch("nemorosa.core.injector.config") as mock_config,
            patch("nemorosa.core.injector.asyncify") as mock_asyncify,
        ):
            mock_config.cfg.linking.enable_linking = True
            mock_async_fn = AsyncMock(return_value="/linked/dir")
            mock_asyncify.return_value = mock_async_fn

            result = await injector.prepare_linked_download_dir(
                torrent_object=sample_torrent,
                local_fdict={"01 - Track.flac": 30000000},
                matched_fdict={"01 - Track.flac": 30000000},
                download_dir="/downloads",
                torrent_name="Test Album",
            )

        assert result == "/linked/dir"

    async def test_falls_back_when_linking_fails(
        self, injector: TorrentInjector, sample_torrent: Torrent
    ) -> None:
        """Should fall back to original dir when linking returns None."""
        with (
            patch("nemorosa.core.injector.config") as mock_config,
            patch("nemorosa.core.injector.asyncify") as mock_asyncify,
        ):
            mock_config.cfg.linking.enable_linking = True
            mock_async_fn = AsyncMock(return_value=None)
            mock_asyncify.return_value = mock_async_fn

            result = await injector.prepare_linked_download_dir(
                torrent_object=sample_torrent,
                local_fdict={"01 - Track.flac": 30000000},
                matched_fdict={"01 - Track.flac": 30000000},
                download_dir="/downloads",
                torrent_name="Test Album",
            )

        assert result == "/downloads"


# --- Tests for inject_matched_torrent ---


class TestInjectMatchedTorrent:
    """Tests for TorrentInjector.inject_matched_torrent."""

    async def test_successful_injection(
        self,
        injector: TorrentInjector,
        mock_torrent_client: MagicMock,
        sample_torrent: Torrent,
        sample_local_info: ClientTorrentInfo,
    ) -> None:
        """Should return True when injection succeeds."""
        with patch("nemorosa.core.injector.config") as mock_config:
            mock_config.cfg.linking.enable_linking = False

            result = await injector.inject_matched_torrent(
                sample_torrent, sample_local_info
            )

        assert result is True
        mock_torrent_client.inject_torrent.assert_called_once()

    async def test_failed_injection(
        self,
        injector: TorrentInjector,
        mock_torrent_client: MagicMock,
        sample_torrent: Torrent,
        sample_local_info: ClientTorrentInfo,
    ) -> None:
        """Should return False when injection fails."""
        mock_torrent_client.inject_torrent = AsyncMock(return_value=(False, None))

        with patch("nemorosa.core.injector.config") as mock_config:
            mock_config.cfg.linking.enable_linking = False

            result = await injector.inject_matched_torrent(
                sample_torrent, sample_local_info
            )

        assert result is False

    async def test_hash_match_passed_to_client(
        self,
        injector: TorrentInjector,
        mock_torrent_client: MagicMock,
        sample_torrent: Torrent,
        sample_local_info: ClientTorrentInfo,
    ) -> None:
        """Should pass hash_match flag to torrent client."""
        with patch("nemorosa.core.injector.config") as mock_config:
            mock_config.cfg.linking.enable_linking = False

            await injector.inject_matched_torrent(
                sample_torrent, sample_local_info, hash_match=True
            )

        call_args = mock_torrent_client.inject_torrent.call_args
        # Bind call args against the real inject_torrent signature to resolve
        # 'hash_match' regardless of positional vs keyword passing.
        sig = inspect.signature(TorrentClient.inject_torrent)
        bound = sig.bind(None, *call_args.args, **call_args.kwargs)
        bound.apply_defaults()
        assert bound.arguments["hash_match"] is True


# --- Tests for injectable link_fn (Phase 8) ---


class TestInjectableLinkFn:
    """Tests for TorrentInjector with injectable link_fn."""

    async def test_custom_link_fn_called_when_linking_enabled(
        self,
        mock_torrent_client: MagicMock,
        sample_torrent: Torrent,
    ) -> None:
        """Should call custom link_fn instead of real file linking."""
        fake_link_fn = MagicMock(return_value="/fake/linked/dir")
        injector = TorrentInjector(mock_torrent_client, link_fn=fake_link_fn)

        with patch("nemorosa.core.injector.config") as mock_config:
            mock_config.cfg.linking.enable_linking = True

            result = await injector.prepare_linked_download_dir(
                torrent_object=sample_torrent,
                local_fdict={"01 - Track.flac": 30000000},
                matched_fdict={"01 - Track.flac": 30000000},
                download_dir="/downloads",
                torrent_name="Test Album",
            )

        assert result == "/fake/linked/dir"
        fake_link_fn.assert_called_once()

    async def test_custom_link_fn_returns_none_falls_back(
        self,
        mock_torrent_client: MagicMock,
        sample_torrent: Torrent,
    ) -> None:
        """Should fall back to original dir when custom link_fn returns None."""
        fake_link_fn = MagicMock(return_value=None)
        injector = TorrentInjector(mock_torrent_client, link_fn=fake_link_fn)

        with patch("nemorosa.core.injector.config") as mock_config:
            mock_config.cfg.linking.enable_linking = True

            result = await injector.prepare_linked_download_dir(
                torrent_object=sample_torrent,
                local_fdict={"01 - Track.flac": 30000000},
                matched_fdict={"01 - Track.flac": 30000000},
                download_dir="/downloads",
                torrent_name="Test Album",
            )

        assert result == "/downloads"
        fake_link_fn.assert_called_once()

    async def test_default_link_fn_is_create_file_links(
        self,
        mock_torrent_client: MagicMock,
    ) -> None:
        """Should default to create_file_links_for_torrent."""
        from nemorosa.filelinking import create_file_links_for_torrent

        injector = TorrentInjector(mock_torrent_client)
        assert injector.link_fn is create_file_links_for_torrent

    async def test_full_injection_with_fake_link_fn(
        self,
        mock_torrent_client: MagicMock,
        sample_torrent: Torrent,
        sample_local_info: ClientTorrentInfo,
    ) -> None:
        """Should complete full injection using fake link_fn without real FS."""
        fake_link_fn = MagicMock(return_value="/fake/linked/dir")
        injector = TorrentInjector(mock_torrent_client, link_fn=fake_link_fn)

        with patch("nemorosa.core.injector.config") as mock_config:
            mock_config.cfg.linking.enable_linking = True

            result = await injector.inject_matched_torrent(
                sample_torrent, sample_local_info
            )

        assert result is True
        fake_link_fn.assert_called_once()
        mock_torrent_client.inject_torrent.assert_called_once()
