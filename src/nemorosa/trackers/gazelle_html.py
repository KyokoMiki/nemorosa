"""Gazelle HTML scraping API implementation for nemorosa."""

from contextlib import suppress
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from aiohttp import ClientSession
from bs4 import BeautifulSoup, Tag
from humanfriendly import InvalidSize, parse_size

from .. import logger
from .api_common import (
    GazelleBase,
    RequestException,
    TorrentSearchResult,
    TrackerSpec,
    _auth_retry,
)


class GazelleParser(GazelleBase):
    def __init__(
        self,
        server: str,
        spec: TrackerSpec,
        cookies: SimpleCookie | None = None,
        api_key: str | None = None,
        session: ClientSession | None = None,
    ) -> None:
        super().__init__(server, spec, session=session)

        if cookies:
            self.client.cookie_jar.update_cookies(cookies)
            logger.debug("Using provided cookies")
        else:
            logger.warning("No cookies provided")

    @_auth_retry
    async def auth(self) -> None:
        """Get authkey and passkey from server by performing a blank search.

        Raises:
            RequestException: If authentication fails.
        """
        try:
            await self.search_torrent_by_filename("")
        except RequestException as e:
            logger.error("Failed to authenticate: %s", e)
            raise

    async def search_torrent_by_filename(
        self, filename: str
    ) -> list[TorrentSearchResult]:
        """Execute search and return torrent list.

        Args:
            filename (str): Filename to search for.

        Returns:
            list[TorrentSearchResult]: List containing torrent information.
        """
        # For HTML parser trackers, use HTML parsing
        params = {"action": "advanced", "filelist": filename}
        logger.debug("Filename search: requesting torrents.php with params: %s", params)
        content = await self.request("torrents.php", params=params)
        torrents = self.parse_search_results(content)
        logger.debug(
            "Filename search for '%s': found %d torrent(s)", filename, len(torrents)
        )
        return torrents

    def parse_search_results(
        self, html_content: bytes | str
    ) -> list[TorrentSearchResult]:
        """Parse search results page.

        Args:
            html_content (bytes | str): HTML content of the search results page.

        Returns:
            list[TorrentSearchResult]: List of parsed torrent information.
        """
        soup = BeautifulSoup(html_content, "lxml")

        # Find all torrents under albums
        torrent_rows = soup.select("tr.group_torrent")
        logger.debug("Found %d torrent row(s) in HTML", len(torrent_rows))

        torrents = [
            torrent
            for torrent_row in torrent_rows
            if (torrent := self.parse_torrent_row(torrent_row))
        ]

        return torrents

    def _parse_torrent_size(self, cells: list[Tag]) -> int:
        """Parse torrent size from table cells.

        Args:
            cells: List of table cells from torrent row.

        Returns:
            Parsed size in bytes, or 0 if parsing fails.
        """
        # Try column 4 first (standard structure)
        if len(cells) > 4:
            with suppress(InvalidSize, ValueError):
                size_text = cells[4].get_text(strip=True).replace(",", "")
                return parse_size(size_text, binary=True)

        # Fallback: search for a cell that parses as size
        time_words = ("ago", "hour", "day", "week", "month", "year", "minute")
        for cell in cells:
            size_text = cell.get_text(strip=True).replace(",", "")
            # Skip cells that look like time/date strings
            if any(time_word in size_text.lower() for time_word in time_words):
                continue
            with suppress(InvalidSize, ValueError):
                return parse_size(size_text, binary=True)

        return 0

    def parse_torrent_row(self, row: Tag) -> TorrentSearchResult | None:
        """Parse single torrent row.

        Args:
            row (Tag): BeautifulSoup Tag object representing a torrent row.

        Returns:
            TorrentSearchResult | None: Parsed torrent info, or None if
                parsing fails.
        """
        try:
            # Get download link
            download_link = row.select_one(
                'a[href^="torrents.php?action=download&id="]'
            )
            if not download_link:
                return None

            # Ensure href is a string
            href = download_link.get("href")
            if not href or not isinstance(href, str):
                return None

            parsed_url = urlparse(href)
            query_params = parse_qs(parsed_url.query)
            torrent_id = query_params.get("id", [None])[0]
            if not torrent_id:
                return None

            self.authkey = query_params.get("authkey", [None])[0]
            self.passkey = query_params.get("torrent_pass", [None])[0]

            # Get all table cells and parse size
            cells = row.select("td")
            title = f"Torrent {torrent_id}"
            size = self._parse_torrent_size(cells)

            if size == 0:
                logger.warning(
                    "Could not parse size for torrent %s, setting to 0", torrent_id
                )

            return TorrentSearchResult(
                torrent_id=int(torrent_id),
                size=size,
                title=title,
            )
        except Exception as e:
            logger.error("Error parsing torrent row: %s", e)
            return None
