"""Unit tests for core pure utility functions."""

import pytest

from nemorosa.core.utils import (
    extract_album_keywords,
    parse_site_host_from_link,
    parse_torrent_id_from_link,
    should_skip_target_site,
)

# --- Tests for parse_torrent_id_from_link ---


class TestParseTorrentIdFromLink:
    """Tests for parse_torrent_id_from_link."""

    def test_extracts_id_from_standard_link(self) -> None:
        """Should extract torrent ID from a standard torrent link."""
        link = "https://redacted.sh/torrents.php?id=12345&authkey=abc"
        assert parse_torrent_id_from_link(link) == "12345"

    def test_extracts_first_id_when_multiple(self) -> None:
        """Should return the first ID value."""
        link = "https://example.com/torrents.php?id=111&id=222"
        assert parse_torrent_id_from_link(link) == "111"

    def test_raises_on_missing_id(self) -> None:
        """Should raise ValueError when 'id' parameter is missing."""
        link = "https://redacted.sh/torrents.php?action=download"
        with pytest.raises(ValueError, match="Missing 'id' parameter"):
            parse_torrent_id_from_link(link)

    def test_raises_on_empty_id(self) -> None:
        """Should raise ValueError when 'id' parameter is empty."""
        link = "https://redacted.sh/torrents.php?id="
        with pytest.raises(ValueError, match="Missing 'id' parameter"):
            parse_torrent_id_from_link(link)

    def test_extracts_id_with_other_params(self) -> None:
        """Should extract ID regardless of other query parameters."""
        link = "https://orpheus.network/torrents.php?action=view&id=99999&type=music"
        assert parse_torrent_id_from_link(link) == "99999"


# --- Tests for should_skip_target_site ---


class TestShouldSkipTargetSite:
    """Tests for should_skip_target_site."""

    def test_skip_when_tracker_exists(self) -> None:
        """Should return True when tracker is in existing trackers."""
        assert (
            should_skip_target_site("flacsfor.me", {"flacsfor.me", "home.opsfet.ch"})
            is True
        )

    def test_no_skip_when_tracker_absent(self) -> None:
        """Should return False when tracker is not in existing trackers."""
        assert should_skip_target_site("flacsfor.me", {"home.opsfet.ch"}) is False

    def test_no_skip_with_empty_trackers(self) -> None:
        """Should return False when existing trackers is empty."""
        assert should_skip_target_site("flacsfor.me", set()) is False

    def test_works_with_list(self) -> None:
        """Should work with list as well as set."""
        assert should_skip_target_site("flacsfor.me", ["flacsfor.me", "other"]) is True


# --- Tests for extract_album_keywords ---


class TestExtractAlbumKeywords:
    """Tests for extract_album_keywords."""

    def test_splits_normal_album_name(self) -> None:
        """Should split album name into keywords."""
        result = extract_album_keywords("Dark Side of the Moon")
        assert set(result) == {"Dark", "Side", "of", "the", "Moon"}

    def test_removes_special_characters(self) -> None:
        """Should sanitize special characters before splitting."""
        result = extract_album_keywords("OK_Computer-2017.Remaster")
        assert "OK" in result
        assert "Computer" in result
        assert "2017" in result
        assert "Remaster" in result

    def test_empty_string_returns_empty_list(self) -> None:
        """Should return empty list for empty input."""
        assert extract_album_keywords("") == []

    def test_special_chars_only_returns_empty(self) -> None:
        """Should return empty list when only special characters."""
        assert extract_album_keywords("___---...") == []


# --- Tests for parse_site_host_from_link ---


class TestParseSiteHostFromLink:
    """Tests for parse_site_host_from_link."""

    def test_extracts_hostname(self) -> None:
        """Should extract hostname from URL."""
        assert (
            parse_site_host_from_link("https://redacted.sh/torrents.php?id=123")
            == "redacted.sh"
        )

    def test_extracts_hostname_from_different_domain(self) -> None:
        """Should extract full hostname from a URL on a different domain."""
        assert (
            parse_site_host_from_link("https://gazellegames.net/api.php")
            == "gazellegames.net"
        )

    def test_returns_none_for_invalid_url(self) -> None:
        """Should return None for URL without hostname."""
        assert parse_site_host_from_link("not-a-url") is None

    def test_returns_none_for_empty_string(self) -> None:
        """Should return None for empty string."""
        assert parse_site_host_from_link("") is None
