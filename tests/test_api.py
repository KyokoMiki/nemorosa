"""Unit tests for API parsing logic."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientSession
from bs4 import BeautifulSoup

from nemorosa.trackers import (
    GazelleGamesNet,
    GazelleJSONAPI,
    GazelleParser,
    TrackerSpec,
    get_api_instance,
)

pytestmark = pytest.mark.anyio


# --- Fixtures ---


@pytest.fixture
def mock_session() -> MagicMock:
    """Create a mock ClientSession for injection."""
    session = MagicMock(spec=ClientSession)
    session.headers = {}
    session.get = AsyncMock()
    session.close = AsyncMock()
    return session


@pytest.fixture
def test_spec() -> TrackerSpec:
    """Create a TrackerSpec for testing."""
    return TrackerSpec(
        rate_limit_max_requests=5,
        rate_limit_period=10.0,
        source_flag="TEST",
        tracker_url="https://tracker.example.com",
        tracker_query="tracker.example.com",
    )


@pytest.fixture
def ggn_spec() -> TrackerSpec:
    """Create a TrackerSpec for GGN testing."""
    return TrackerSpec(
        rate_limit_max_requests=1,
        rate_limit_period=2.0,
        source_flag="GGn",
        tracker_url="https://tracker.gazellegames.net",
        tracker_query="tracker.gazellegames.net",
    )


# --- Tests for GazelleBase.parse_file_list ---


class TestGazelleBaseParseFileList:
    """Tests for GazelleBase.parse_file_list (default |||/{{{ format)."""

    def test_standard_file_list(
        self, mock_session: MagicMock, test_spec: TrackerSpec
    ) -> None:
        """Should parse standard Gazelle file list format."""
        api = GazelleJSONAPI(
            "https://redacted.sh", spec=test_spec, session=mock_session
        )
        file_list_str = (
            "01 - Track One.flac{{{30000000}}}|||02 - Track Two.flac{{{20000000}}}"
        )

        result = api.parse_file_list(file_list_str)

        assert result == {
            "01 - Track One.flac": 30000000,
            "02 - Track Two.flac": 20000000,
        }

    def test_single_file(self, mock_session: MagicMock, test_spec: TrackerSpec) -> None:
        """Should parse single file entry."""
        api = GazelleJSONAPI(
            "https://redacted.sh", spec=test_spec, session=mock_session
        )
        file_list_str = "album.flac{{{50000000}}}"

        result = api.parse_file_list(file_list_str)

        assert result == {"album.flac": 50000000}

    def test_empty_file_list(
        self, mock_session: MagicMock, test_spec: TrackerSpec
    ) -> None:
        """Should return empty dict for empty input."""
        api = GazelleJSONAPI(
            "https://redacted.sh", spec=test_spec, session=mock_session
        )

        assert api.parse_file_list("") == {}
        assert api.parse_file_list(None) == {}

    def test_html_entities_unescaped(
        self, mock_session: MagicMock, test_spec: TrackerSpec
    ) -> None:
        """Should unescape HTML entities in filenames."""
        api = GazelleJSONAPI(
            "https://redacted.sh", spec=test_spec, session=mock_session
        )
        file_list_str = "Track &amp; Bass.flac{{{10000}}}"

        result = api.parse_file_list(file_list_str)

        assert "Track & Bass.flac" in result

    def test_malformed_entry_skipped(
        self, mock_session: MagicMock, test_spec: TrackerSpec
    ) -> None:
        """Should skip malformed entries without crashing."""
        api = GazelleJSONAPI(
            "https://redacted.sh", spec=test_spec, session=mock_session
        )
        file_list_str = (
            "good.flac{{{1000}}}|||bad_entry_no_braces|||also_good.flac{{{2000}}}"
        )

        result = api.parse_file_list(file_list_str)

        assert result == {"good.flac": 1000, "also_good.flac": 2000}


# --- Tests for GazelleGamesNet.parse_file_list ---


class TestGGNParseFileList:
    """Tests for GazelleGamesNet.parse_file_list (list of dicts format)."""

    def test_standard_ggn_file_list(
        self, mock_session: MagicMock, ggn_spec: TrackerSpec
    ) -> None:
        """Should parse GGN list-of-dicts format."""
        api = GazelleGamesNet(
            "https://gazellegames.net",
            spec=ggn_spec,
            api_key="test_key",
            session=mock_session,
        )
        file_list = [
            {"name": "game.iso", "size": 700000000},
            {"name": "readme.txt", "size": 1024},
        ]

        result = api.parse_file_list(file_list)

        assert result == {"game.iso": 700000000, "readme.txt": 1024}

    def test_empty_ggn_file_list(
        self, mock_session: MagicMock, ggn_spec: TrackerSpec
    ) -> None:
        """Should return empty dict for empty list."""
        api = GazelleGamesNet(
            "https://gazellegames.net",
            spec=ggn_spec,
            api_key="test_key",
            session=mock_session,
        )

        assert api.parse_file_list([]) == {}
        assert api.parse_file_list(None) == {}  # type: ignore[arg-type]

    def test_html_entities_in_ggn(
        self, mock_session: MagicMock, ggn_spec: TrackerSpec
    ) -> None:
        """Should unescape HTML entities in GGN filenames."""
        api = GazelleGamesNet(
            "https://gazellegames.net",
            spec=ggn_spec,
            api_key="test_key",
            session=mock_session,
        )
        file_list = [{"name": "Game &amp; DLC.zip", "size": 5000}]

        result = api.parse_file_list(file_list)

        assert "Game & DLC.zip" in result


# --- Tests for GazelleParser.parse_search_results ---


class TestGazelleParserParseResults:
    """Tests for GazelleParser HTML parsing logic."""

    def test_parse_torrent_row_with_download_link(
        self, mock_session: MagicMock, test_spec: TrackerSpec
    ) -> None:
        """Should extract torrent ID and size from HTML row."""
        api = GazelleParser(
            "https://libble.me", spec=test_spec, cookies=None, session=mock_session
        )

        html = (
            '<tr class="group_torrent">'
            "<td>"
            '<a href="torrents.php?action=download&id=12345'
            '&authkey=abc&torrent_pass=xyz">DL</a>'
            "</td>"
            "<td>info</td>"
            "<td>info2</td>"
            "<td>info3</td>"
            "<td>150.5 MB</td>"
            "</tr>"
        )
        soup = BeautifulSoup(html, "lxml")
        row = soup.select_one("tr.group_torrent")
        assert row is not None

        result = api.parse_torrent_row(row)

        assert result is not None
        assert result.torrent_id == 12345
        assert result.size > 0
        assert api.authkey == "abc"
        assert api.passkey == "xyz"

    def test_parse_torrent_row_no_download_link(
        self, mock_session: MagicMock, test_spec: TrackerSpec
    ) -> None:
        """Should return None when no download link found."""
        api = GazelleParser(
            "https://libble.me", spec=test_spec, cookies=None, session=mock_session
        )

        html = (
            '<tr class="group_torrent">'
            '<td><a href="other.php">Not a download</a></td>'
            "</tr>"
        )
        soup = BeautifulSoup(html, "lxml")
        row = soup.select_one("tr.group_torrent")
        assert row is not None

        result = api.parse_torrent_row(row)

        assert result is None

    def test_parse_search_results_multiple_rows(
        self, mock_session: MagicMock, test_spec: TrackerSpec
    ) -> None:
        """Should parse multiple torrent rows from HTML."""
        api = GazelleParser(
            "https://libble.me", spec=test_spec, cookies=None, session=mock_session
        )
        html = (
            "<html><body><table>"
            '<tr class="group_torrent">'
            "<td>"
            '<a href="torrents.php?action=download&id=100'
            '&authkey=a&torrent_pass=p">DL</a>'
            "</td>"
            "<td></td><td></td><td></td><td>50 MB</td>"
            "</tr>"
            '<tr class="group_torrent">'
            "<td>"
            '<a href="torrents.php?action=download&id=200'
            '&authkey=a&torrent_pass=p">DL</a>'
            "</td>"
            "<td></td><td></td><td></td><td>100 MB</td>"
            "</tr>"
            "</table></body></html>"
        )

        results = api.parse_search_results(html)

        assert len(results) == 2
        assert results[0].torrent_id == 100
        assert results[1].torrent_id == 200

    def test_parse_search_results_empty_html(
        self, mock_session: MagicMock, test_spec: TrackerSpec
    ) -> None:
        """Should return empty list for HTML with no torrent rows."""
        api = GazelleParser(
            "https://libble.me", spec=test_spec, cookies=None, session=mock_session
        )

        results = api.parse_search_results("<html><body></body></html>")

        assert results == []


# --- Tests for session injection ---


class TestSessionInjection:
    """Tests for session parameter injection in API constructors."""

    def test_gazelle_base_uses_injected_session(
        self, mock_session: MagicMock, test_spec: TrackerSpec
    ) -> None:
        """Should use injected session instead of creating a new one."""
        api = GazelleJSONAPI(
            "https://redacted.sh", spec=test_spec, session=mock_session
        )

        assert api.client is mock_session

    async def test_gazelle_base_creates_session_when_none(
        self, test_spec: TrackerSpec
    ) -> None:
        """Should create its own session when none is provided."""
        api = GazelleJSONAPI("https://redacted.sh", spec=test_spec)

        assert api.client is not None
        assert isinstance(api.client, ClientSession)
        await api.close()

    def test_ggn_uses_injected_session(
        self, mock_session: MagicMock, ggn_spec: TrackerSpec
    ) -> None:
        """Should pass session through to GazelleBase."""
        api = GazelleGamesNet(
            "https://gazellegames.net",
            spec=ggn_spec,
            api_key="test",
            session=mock_session,
        )

        assert api.client is mock_session

    def test_parser_uses_injected_session(
        self, mock_session: MagicMock, test_spec: TrackerSpec
    ) -> None:
        """Should pass session through to GazelleBase."""
        api = GazelleParser("https://libble.me", spec=test_spec, session=mock_session)

        assert api.client is mock_session


# --- Tests for get_api_instance ---


class TestGetApiInstance:
    """Tests for get_api_instance factory function."""

    async def test_returns_json_api_for_red(self) -> None:
        """Should return GazelleJSONAPI for redacted.sh."""
        api = get_api_instance("https://redacted.sh")

        assert isinstance(api, GazelleJSONAPI)
        await api.close()

    async def test_returns_parser_for_libble(self) -> None:
        """Should return GazelleParser for libble.me."""
        api = get_api_instance("https://libble.me")

        assert isinstance(api, GazelleParser)
        await api.close()

    async def test_returns_ggn_for_gazellegames(self) -> None:
        """Should return GazelleGamesNet for gazellegames.net."""
        api = get_api_instance("https://gazellegames.net")

        assert isinstance(api, GazelleGamesNet)
        await api.close()

    def test_raises_for_unsupported_server(self) -> None:
        """Should raise ValueError for unsupported server."""
        with pytest.raises(ValueError, match="Unsupported server"):
            get_api_instance("https://unknown.example.com")
