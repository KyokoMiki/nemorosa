"""Pure utility functions for core processing logic.

All functions in this module are pure: same input always produces same output,
no side effects, no I/O operations.
"""

from collections.abc import Collection
from urllib.parse import parse_qs, urlparse

from ..filecompare import make_search_query


def parse_torrent_id_from_link(torrent_link: str) -> str:
    """Extract torrent ID from a torrent link URL.

    Args:
        torrent_link: URL containing an 'id' query parameter.

    Returns:
        The extracted torrent ID string.

    Raises:
        ValueError: If the 'id' parameter is missing or empty.
    """
    parsed_link = urlparse(torrent_link)
    query_params = parse_qs(parsed_link.query)
    if "id" not in query_params or not query_params["id"]:
        raise ValueError(f"Missing 'id' parameter in torrent link: {torrent_link}")
    return query_params["id"][0]


def should_skip_target_site(
    tracker_query: str, existing_target_trackers: Collection[str]
) -> bool:
    """Determine if a target site should be skipped for a torrent.

    A site should be skipped when the torrent already exists on that tracker.

    Args:
        tracker_query: The tracker query string to check.
        existing_target_trackers: Collection of tracker strings the torrent
            already belongs to.

    Returns:
        True if the site should be skipped, False otherwise.
    """
    return tracker_query in existing_target_trackers


def extract_album_keywords(album_name: str) -> list[str]:
    """Extract search keywords from an album name.

    Sanitizes the album name and splits into keyword tokens.

    Args:
        album_name: Raw album name string.

    Returns:
        List of keyword strings. Empty list if no valid keywords extracted.
    """
    return make_search_query(album_name).split()


def parse_site_host_from_link(torrent_link: str) -> str | None:
    """Extract the hostname from a torrent link URL.

    Args:
        torrent_link: URL to extract hostname from.

    Returns:
        Hostname string, or None if parsing fails.
    """
    parsed_link = urlparse(torrent_link)
    return parsed_link.hostname
