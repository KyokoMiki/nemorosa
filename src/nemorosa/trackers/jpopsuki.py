"""JPopSuki HTML scraping implementation for nemorosa."""

from contextlib import suppress

from bs4 import BeautifulSoup
from humanfriendly import InvalidSize, parse_size

from .. import logger
from .api_common import TorrentSearchResult
from .gazelle_html import GazelleParser


class JPopSuki(GazelleParser):
    # JPopSuki uses disablegrouping=1, so each row is a full torrent row
    # with class like "torrent_redline" etc.
    _torrent_row_selector = "tr.torrent_redline"

    # Size column is at index 5 (category, cover, name, files, added, size)
    _size_column_index = 5

    has_precise_sizes = False

    async def search_torrent_by_filename(
        self, filename: str
    ) -> list[TorrentSearchResult]:
        """Execute search via ajax.php advanced search endpoint.

        Args:
            filename: Filename to search for.

        Returns:
            List of parsed torrent search results.
        """
        params = {
            "section": "torrents",
            "action": "advanced",
            "filelist": filename,
            "disablegrouping": "1",
        }
        logger.debug(
            "JPopSuki filename search: requesting ajax.php with params: %s", params
        )
        content = await self.request("ajax.php", params=params)
        torrents = self.parse_search_results(content)
        logger.debug(
            "JPopSuki filename search for '%s': found %d torrent(s)",
            filename,
            len(torrents),
        )
        return torrents

    async def search_torrent_by_hash(
        self, torrent_hash: str
    ) -> TorrentSearchResult | None:
        """JPopSuki does not support hash-based search.

        Args:
            torrent_hash: Torrent hash (unused).

        Returns:
            Always None.
        """
        return None

    async def get_torrent_fdict(self, torrent_id: int | str) -> dict[str, int]:
        """Get file dictionary by parsing the torrent detail HTML page.

        Args:
            torrent_id: The ID of the torrent to retrieve.

        Returns:
            Mapping of filename to file size in bytes. Empty dict on error.
        """
        logger.debug("Getting torrent file list by id: %s", torrent_id)
        try:
            content = await self.request(
                "torrents.php", params={"torrentid": torrent_id}
            )
        except Exception as e:
            logger.error("Failed to get torrent page for id %s: %s", torrent_id, e)
            return {}

        return self._parse_torrent_file_table(content, torrent_id)

    def _parse_torrent_file_table(
        self, html_content: bytes | str, torrent_id: int | str
    ) -> dict[str, int]:
        """Parse file list table from JPopSuki torrent detail page.

        The file table is inside ``tr#torrent_{id}`` and has rows with
        two columns: Filename and Size.

        Args:
            html_content: HTML content of the torrent detail page.
            torrent_id: Torrent ID used to locate the detail row.

        Returns:
            Mapping of filename to file size in bytes.
        """
        soup = BeautifulSoup(html_content, "lxml")
        detail_row = soup.select_one(f"tr#torrent_{torrent_id}")
        if not detail_row:
            logger.warning("Could not find detail row for torrent %s", torrent_id)
            return {}

        # The file table is a nested <table> inside the detail row;
        # skip the header row (colhead_dark).
        file_list: dict[str, int] = {}
        for row in detail_row.select("table tr"):
            if row.select_one(".colhead_dark"):
                continue
            cells = row.select("td")
            if len(cells) != 2:
                continue

            filename = cells[0].get_text(strip=True)
            size_text = cells[1].get_text(strip=True).replace(",", "")
            with suppress(InvalidSize, ValueError):
                file_list[filename] = parse_size(size_text, binary=True)

        logger.debug("Parsed %d file(s) for torrent %s", len(file_list), torrent_id)
        return file_list
