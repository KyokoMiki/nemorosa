"""Unit tests for NemorosaCore orchestration logic."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from torf import Torrent

from nemorosa.clients.client_common import ClientTorrentInfo
from nemorosa.core.injector import TorrentInjector
from nemorosa.core.models import SearchMatchResult
from nemorosa.core.processor import NemorosaCore
from nemorosa.core.searcher import TorrentSearcher
from nemorosa.db import NemorosaDatabase

pytestmark = pytest.mark.anyio


# --- Fixtures ---


@pytest.fixture
def mock_searcher() -> MagicMock:
    """Create a mock TorrentSearcher."""
    searcher = MagicMock(spec=TorrentSearcher)
    searcher.search_torrent_match = AsyncMock(return_value=SearchMatchResult())
    return searcher


@pytest.fixture
def mock_injector() -> MagicMock:
    """Create a mock TorrentInjector."""
    injector = MagicMock(spec=TorrentInjector)
    injector.inject_matched_torrent = AsyncMock(return_value=True)
    injector.prepare_linked_download_dir = AsyncMock(return_value="/downloads")
    return injector


@pytest.fixture
def mock_database() -> MagicMock:
    """Create a mock NemorosaDatabase."""
    db = MagicMock(spec=NemorosaDatabase)
    db.is_hash_scanned = AsyncMock(return_value=False)
    db.add_scan_result = AsyncMock()
    return db


@pytest.fixture
def mock_api() -> MagicMock:
    """Create a mock API instance."""
    api = MagicMock()
    api.server = "https://redacted.sh"
    api.site_host = "redacted.sh"
    api.tracker_query = "flacsfor.me"
    api.source_flag = "RED"
    api.download_torrent = AsyncMock()
    return api


@pytest.fixture
def core(
    mock_torrent_client: MagicMock,
    mock_database: MagicMock,
    mock_searcher: MagicMock,
    mock_injector: MagicMock,
) -> NemorosaCore:
    """Create a NemorosaCore with all mock dependencies."""
    return NemorosaCore(
        torrent_client=mock_torrent_client,
        database=mock_database,
        searcher=mock_searcher,
        injector=mock_injector,
        target_apis=[],
    )


# --- Tests for process_torrent_search ---


class TestProcessTorrentSearch:
    """Tests for NemorosaCore.process_torrent_search."""

    async def test_no_match_found(
        self,
        core: NemorosaCore,
        mock_searcher: MagicMock,
        mock_api: MagicMock,
        sample_torrent_details: ClientTorrentInfo,
    ) -> None:
        """Should return success with no match when searcher finds nothing."""
        mock_searcher.search_torrent_match = AsyncMock(return_value=SearchMatchResult())

        success, tid, thash = await core.process_torrent_search(
            torrent_details=sample_torrent_details,
            api=mock_api,
        )

        assert success is True
        assert tid is None
        assert thash is None
        assert core.stats.scanned == 1

    async def test_match_found_and_injected(
        self,
        core: NemorosaCore,
        mock_searcher: MagicMock,
        mock_injector: MagicMock,
        mock_api: MagicMock,
        sample_torrent_details: ClientTorrentInfo,
    ) -> None:
        """Should inject torrent when match is found."""
        mock_torrent = MagicMock(spec=Torrent)
        mock_torrent.infohash = "matched_hash_123"
        mock_searcher.search_torrent_match = AsyncMock(
            return_value=SearchMatchResult(
                torrent_id=42,
                matched_torrent=mock_torrent,
                hash_match=True,
                search_success=True,
            )
        )
        mock_injector.inject_matched_torrent = AsyncMock(return_value=True)

        with patch("nemorosa.core.processor.config") as mock_config:
            mock_config.cfg.global_config.no_download = False

            success, tid, thash = await core.process_torrent_search(
                torrent_details=sample_torrent_details,
                api=mock_api,
            )

        assert success is True
        assert tid == "42"
        assert thash == "matched_hash_123"
        assert core.stats.found == 1
        assert core.stats.downloaded == 1

    async def test_match_found_no_download_mode(
        self,
        core: NemorosaCore,
        mock_searcher: MagicMock,
        mock_injector: MagicMock,
        mock_api: MagicMock,
        sample_torrent_details: ClientTorrentInfo,
    ) -> None:
        """Should skip injection when no_download is True."""
        mock_torrent = MagicMock(spec=Torrent)
        mock_searcher.search_torrent_match = AsyncMock(
            return_value=SearchMatchResult(
                torrent_id=42,
                matched_torrent=mock_torrent,
                hash_match=True,
                search_success=True,
            )
        )

        with patch("nemorosa.core.processor.config") as mock_config:
            mock_config.cfg.global_config.no_download = True

            success, tid, thash = await core.process_torrent_search(
                torrent_details=sample_torrent_details,
                api=mock_api,
            )

        assert success is True
        assert tid == "42"
        assert thash is None
        mock_injector.inject_matched_torrent.assert_not_called()

    async def test_injection_failure(
        self,
        core: NemorosaCore,
        mock_searcher: MagicMock,
        mock_injector: MagicMock,
        mock_api: MagicMock,
        sample_torrent_details: ClientTorrentInfo,
    ) -> None:
        """Should track failure when injection fails."""
        mock_torrent = MagicMock(spec=Torrent)
        mock_searcher.search_torrent_match = AsyncMock(
            return_value=SearchMatchResult(
                torrent_id=42,
                matched_torrent=mock_torrent,
                hash_match=False,
                search_success=True,
            )
        )
        mock_injector.inject_matched_torrent = AsyncMock(return_value=False)

        with patch("nemorosa.core.processor.config") as mock_config:
            mock_config.cfg.global_config.no_download = False

            success, tid, thash = await core.process_torrent_search(
                torrent_details=sample_torrent_details,
                api=mock_api,
            )

        assert success is True
        assert tid == "42"
        assert thash is None
        assert core.stats.cnt_dl_fail == 1

    async def test_search_failure(
        self,
        core: NemorosaCore,
        mock_searcher: MagicMock,
        mock_api: MagicMock,
        sample_torrent_details: ClientTorrentInfo,
    ) -> None:
        """Should return search_success=False when search fails."""
        mock_searcher.search_torrent_match = AsyncMock(
            return_value=SearchMatchResult(search_success=False)
        )

        success, tid, thash = await core.process_torrent_search(
            torrent_details=sample_torrent_details,
            api=mock_api,
        )

        assert success is False
        assert tid is None
