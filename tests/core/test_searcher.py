"""Unit tests for TorrentSearcher."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from torf import Torrent

from nemorosa.clients.client_common import ClientTorrentInfo
from nemorosa.core.searcher import TorrentSearcher
from nemorosa.trackers import TorrentSearchResult

pytestmark = pytest.mark.anyio


# --- Fixtures ---


@pytest.fixture
def searcher() -> TorrentSearcher:
    """Create a TorrentSearcher instance."""
    return TorrentSearcher()


@pytest.fixture
def mock_api() -> MagicMock:
    """Create a mock API instance with common attributes."""
    api = MagicMock()
    api.source_flag = "RED"
    api.server = "https://redacted.sh"
    api.site_host = "redacted.sh"
    api.announce = "https://flacsfor.me/testpasskey/announce"
    api.search_torrent_by_hash = AsyncMock(return_value=None)
    api.search_torrent_by_filename = AsyncMock(return_value=[])
    api.download_torrent = AsyncMock()
    api.torrent = AsyncMock(return_value={})
    api.get_torrent_url = MagicMock(
        side_effect=lambda tid: f"https://redacted.sh/torrents.php?torrentid={tid}"
    )
    return api


# --- Tests for hash_based_search ---


class TestHashBasedSearch:
    """Tests for TorrentSearcher.hash_based_search."""

    async def test_found_by_hash_with_source_flag(
        self, searcher: TorrentSearcher, mock_api: MagicMock, sample_torrent: Torrent
    ) -> None:
        """Should return torrent ID when hash search succeeds."""
        mock_api.search_torrent_by_hash = AsyncMock(
            return_value={
                "response": {"torrent": {"id": 42}},
            }
        )

        tid, matched = await searcher.hash_based_search(
            torrent_object=sample_torrent, api=mock_api
        )

        assert tid == 42
        assert matched is not None
        assert matched.comment == "https://redacted.sh/torrents.php?torrentid=42"

    async def test_not_found_returns_none(
        self, searcher: TorrentSearcher, mock_api: MagicMock, sample_torrent: Torrent
    ) -> None:
        """Should return (None, None) when no hash matches."""
        mock_api.search_torrent_by_hash = AsyncMock(return_value=None)

        tid, matched = await searcher.hash_based_search(
            torrent_object=sample_torrent, api=mock_api
        )

        assert tid is None
        assert matched is None

    async def test_tries_alternate_source_flags_for_red(
        self, searcher: TorrentSearcher, mock_api: MagicMock, sample_torrent: Torrent
    ) -> None:
        """Should try RED, empty, and PTH source flags for RED tracker."""
        call_count = 0

        async def side_effect(torrent_hash: str):
            nonlocal call_count
            call_count += 1
            if call_count == 3:  # Third call (PTH flag)
                return {"response": {"torrent": {"id": 99}}}
            return None

        mock_api.search_torrent_by_hash = AsyncMock(side_effect=side_effect)

        tid, matched = await searcher.hash_based_search(
            torrent_object=sample_torrent, api=mock_api
        )

        assert tid == 99
        assert call_count == 3

    async def test_tries_alternate_source_flags_for_ops(
        self, searcher: TorrentSearcher, mock_api: MagicMock, sample_torrent: Torrent
    ) -> None:
        """Should try OPS, empty, and APL source flags for OPS tracker."""
        mock_api.source_flag = "OPS"
        call_count = 0

        async def side_effect(torrent_hash: str):
            nonlocal call_count
            call_count += 1
            if call_count == 3:  # Third call (APL flag)
                return {"response": {"torrent": {"id": 77}}}
            return None

        mock_api.search_torrent_by_hash = AsyncMock(side_effect=side_effect)

        tid, matched = await searcher.hash_based_search(
            torrent_object=sample_torrent, api=mock_api
        )

        assert tid == 77
        assert call_count == 3

    async def test_exception_during_hash_search_continues(
        self, searcher: TorrentSearcher, mock_api: MagicMock, sample_torrent: Torrent
    ) -> None:
        """Should continue trying other flags when one raises an exception."""
        call_count = 0

        async def side_effect(torrent_hash: str):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Network error")
            if call_count == 2:
                return {"response": {"torrent": {"id": 55}}}
            return None

        mock_api.search_torrent_by_hash = AsyncMock(side_effect=side_effect)

        tid, matched = await searcher.hash_based_search(
            torrent_object=sample_torrent, api=mock_api
        )

        assert tid == 55
        assert call_count == 2


# --- Tests for filename_search ---


class TestFilenameSearch:
    """Tests for TorrentSearcher.filename_search."""

    async def test_found_by_size_match(
        self, searcher: TorrentSearcher, mock_api: MagicMock
    ) -> None:
        """Should return torrent ID when size matches."""
        fdict = {"01 - Track One.flac": 30000000, "02 - Track Two.flac": 20000000}
        tsize = 50000000

        mock_api.search_torrent_by_filename = AsyncMock(
            return_value=[
                TorrentSearchResult(torrent_id=101, size=50000000, title="Match"),
            ]
        )
        mock_torrent = MagicMock(spec=Torrent)
        mock_api.download_torrent = AsyncMock(return_value=mock_torrent)

        tid, matched = await searcher.filename_search(
            fdict=fdict, tsize=tsize, api=mock_api
        )

        assert tid == 101
        assert matched is mock_torrent

    async def test_not_found_returns_none(
        self, searcher: TorrentSearcher, mock_api: MagicMock
    ) -> None:
        """Should return (None, None) when no matches found."""
        fdict = {"01 - Track One.flac": 30000000}
        tsize = 50000000

        mock_api.search_torrent_by_filename = AsyncMock(return_value=[])

        tid, matched = await searcher.filename_search(
            fdict=fdict, tsize=tsize, api=mock_api
        )

        assert tid is None
        assert matched is None

    async def test_download_failure_returns_tid_with_none_torrent(
        self, searcher: TorrentSearcher, mock_api: MagicMock
    ) -> None:
        """Should return (tid, None) when download fails after finding match."""
        fdict = {"01 - Track One.flac": 30000000}
        tsize = 50000000

        mock_api.search_torrent_by_filename = AsyncMock(
            return_value=[
                TorrentSearchResult(torrent_id=101, size=50000000, title="Match"),
            ]
        )
        mock_api.download_torrent = AsyncMock(side_effect=Exception("Download failed"))

        tid, matched = await searcher.filename_search(
            fdict=fdict, tsize=tsize, api=mock_api
        )

        assert tid == 101
        assert matched is None


# --- Tests for match_by_file_content ---


class TestMatchByFileContent:
    """Tests for TorrentSearcher.match_by_file_content."""

    async def test_match_found_by_file_size(
        self, searcher: TorrentSearcher, mock_api: MagicMock
    ) -> None:
        """Should return torrent ID when file size matches."""
        torrents = [
            TorrentSearchResult(torrent_id=201, size=50000000, title="Torrent A"),
        ]
        fdict = {"01 - Track.flac": 30000000}
        scan_querys = ["01 - Track.flac"]

        mock_api.torrent = AsyncMock(
            return_value={"fileList": {"01 - Track.flac": 30000000}}
        )

        with patch("nemorosa.core.searcher.config") as mock_config:
            mock_config.cfg.linking.enable_linking = False

            tid = await searcher.match_by_file_content(
                torrents=torrents,
                fname="01 - Track.flac",
                fdict=fdict,
                scan_querys=scan_querys,
                api=mock_api,
            )

        assert tid == 201

    async def test_no_match_returns_none(
        self, searcher: TorrentSearcher, mock_api: MagicMock
    ) -> None:
        """Should return None when no file content matches."""
        torrents = [
            TorrentSearchResult(torrent_id=201, size=50000000, title="Torrent A"),
        ]
        fdict = {"01 - Track.flac": 30000000}
        scan_querys = ["01 - Track.flac"]

        mock_api.torrent = AsyncMock(
            return_value={"fileList": {"01 - Track.flac": 99999999}}
        )

        with patch("nemorosa.core.searcher.config") as mock_config:
            mock_config.cfg.linking.enable_linking = False

            tid = await searcher.match_by_file_content(
                torrents=torrents,
                fname="01 - Track.flac",
                fdict=fdict,
                scan_querys=scan_querys,
                api=mock_api,
            )

        assert tid is None

    async def test_conflict_detected_returns_none(
        self, searcher: TorrentSearcher, mock_api: MagicMock
    ) -> None:
        """Should return None when conflict is detected and linking disabled."""
        torrents = [
            TorrentSearchResult(torrent_id=201, size=50000000, title="Torrent A"),
        ]
        fdict = {
            "01 - Track.flac": 30000000,
            "cover.jpg": 5000,
        }
        scan_querys = ["01 - Track.flac"]

        # File size matches for the music file, but cover.jpg has conflict
        mock_api.torrent = AsyncMock(
            return_value={
                "fileList": {
                    "01 - Track.flac": 30000000,
                    "cover.jpg": 9999,
                }
            }
        )

        with patch("nemorosa.core.searcher.config") as mock_config:
            mock_config.cfg.linking.enable_linking = False

            tid = await searcher.match_by_file_content(
                torrents=torrents,
                fname="01 - Track.flac",
                fdict=fdict,
                scan_querys=scan_querys,
                api=mock_api,
            )

        assert tid is None

    async def test_conflict_allowed_with_linking_enabled(
        self, searcher: TorrentSearcher, mock_api: MagicMock
    ) -> None:
        """Should return torrent ID when linking is enabled despite conflicts."""
        torrents = [
            TorrentSearchResult(torrent_id=201, size=50000000, title="Torrent A"),
        ]
        fdict = {
            "01 - Track.flac": 30000000,
            "cover.jpg": 5000,
        }
        scan_querys = ["01 - Track.flac"]

        mock_api.torrent = AsyncMock(
            return_value={
                "fileList": {
                    "01 - Track.flac": 30000000,
                    "cover.jpg": 9999,
                }
            }
        )

        with patch("nemorosa.core.searcher.config") as mock_config:
            mock_config.cfg.linking.enable_linking = True

            tid = await searcher.match_by_file_content(
                torrents=torrents,
                fname="01 - Track.flac",
                fdict=fdict,
                scan_querys=scan_querys,
                api=mock_api,
            )

        assert tid == 201

    async def test_api_error_continues_to_next_torrent(
        self, searcher: TorrentSearcher, mock_api: MagicMock
    ) -> None:
        """Should continue checking next torrent when API call fails."""
        torrents = [
            TorrentSearchResult(torrent_id=201, size=50000000, title="Torrent A"),
            TorrentSearchResult(torrent_id=202, size=50000000, title="Torrent B"),
        ]
        fdict = {"01 - Track.flac": 30000000}
        scan_querys = ["01 - Track.flac"]

        mock_api.torrent = AsyncMock(
            side_effect=[
                Exception("API error"),
                {"fileList": {"01 - Track.flac": 30000000}},
            ]
        )

        with patch("nemorosa.core.searcher.config") as mock_config:
            mock_config.cfg.linking.enable_linking = False

            tid = await searcher.match_by_file_content(
                torrents=torrents,
                fname="01 - Track.flac",
                fdict=fdict,
                scan_querys=scan_querys,
                api=mock_api,
            )

        assert tid == 202


# --- Tests for search_torrent_match ---


class TestSearchTorrentMatch:
    """Tests for TorrentSearcher.search_torrent_match."""

    async def test_hash_match_found(
        self,
        searcher: TorrentSearcher,
        mock_api: MagicMock,
        sample_torrent: Torrent,
        sample_torrent_details: ClientTorrentInfo,
    ) -> None:
        """Should return hash match result when hash search succeeds."""
        mock_torrent = MagicMock(spec=Torrent)
        mock_api.search_torrent_by_hash = AsyncMock(
            return_value={"response": {"torrent": {"id": 42}}}
        )

        # Patch hash_based_search to return directly
        searcher.hash_based_search = AsyncMock(return_value=(42, mock_torrent))

        result = await searcher.search_torrent_match(
            sample_torrent_details, mock_api, sample_torrent
        )

        assert result.torrent_id == 42
        assert result.matched_torrent is mock_torrent
        assert result.hash_match is True
        assert result.search_success is True

    async def test_fallback_to_filename_search(
        self,
        searcher: TorrentSearcher,
        mock_api: MagicMock,
        sample_torrent: Torrent,
        sample_torrent_details: ClientTorrentInfo,
    ) -> None:
        """Should fall back to filename search when hash search finds nothing."""
        mock_torrent = MagicMock(spec=Torrent)
        searcher.hash_based_search = AsyncMock(return_value=(None, None))
        searcher.filename_search = AsyncMock(return_value=(101, mock_torrent))

        result = await searcher.search_torrent_match(
            sample_torrent_details, mock_api, sample_torrent
        )

        assert result.torrent_id == 101
        assert result.matched_torrent is mock_torrent
        assert result.hash_match is False
        assert result.search_success is True

    async def test_no_torrent_object_skips_hash_search(
        self,
        searcher: TorrentSearcher,
        mock_api: MagicMock,
        sample_torrent_details: ClientTorrentInfo,
    ) -> None:
        """Should skip hash search when torrent_object is None."""
        mock_torrent = MagicMock(spec=Torrent)
        searcher.filename_search = AsyncMock(return_value=(101, mock_torrent))

        result = await searcher.search_torrent_match(
            sample_torrent_details, mock_api, None
        )

        assert result.torrent_id == 101
        assert result.hash_match is False
        assert result.search_success is True

    async def test_both_searches_fail(
        self,
        searcher: TorrentSearcher,
        mock_api: MagicMock,
        sample_torrent: Torrent,
        sample_torrent_details: ClientTorrentInfo,
    ) -> None:
        """Should return failure when both searches raise exceptions."""
        searcher.hash_based_search = AsyncMock(
            side_effect=Exception("Hash search error")
        )
        searcher.filename_search = AsyncMock(
            side_effect=Exception("Filename search error")
        )

        result = await searcher.search_torrent_match(
            sample_torrent_details, mock_api, sample_torrent
        )

        assert result.torrent_id is None
        assert result.matched_torrent is None
        assert result.search_success is False

    async def test_hash_fails_filename_succeeds(
        self,
        searcher: TorrentSearcher,
        mock_api: MagicMock,
        sample_torrent: Torrent,
        sample_torrent_details: ClientTorrentInfo,
    ) -> None:
        """Should find via filename but search_success stays False from hash error."""
        mock_torrent = MagicMock(spec=Torrent)
        searcher.hash_based_search = AsyncMock(
            side_effect=Exception("Hash search error")
        )
        searcher.filename_search = AsyncMock(return_value=(101, mock_torrent))

        result = await searcher.search_torrent_match(
            sample_torrent_details, mock_api, sample_torrent
        )

        assert result.torrent_id == 101
        assert result.matched_torrent is mock_torrent
        assert result.hash_match is False
        # search_success remains False because hash search failed
        # (current implementation does not reset it on filename success)
        assert result.search_success is False
