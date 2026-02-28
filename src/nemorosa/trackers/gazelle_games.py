"""GazelleGames.net (GGN) tracker API implementation for nemorosa."""

from html import unescape
from http.cookies import SimpleCookie
from typing import Any

import msgspec
from aiohttp import ClientSession
from torf import Torrent

from .. import logger
from .api_common import (
    AuthMethod,
    GazelleBase,
    RequestException,
    TorrentSearchResult,
    TrackerSpec,
)


class GazelleGamesNet(GazelleBase):
    """GazelleGames.net (GGN) tracker implementation.

    GGN uses a JSON API with X-API-Key authentication instead of cookies.
    This class implements GGN's specific API endpoints using api.php.
    """

    def __init__(
        self,
        server: str,
        spec: TrackerSpec,
        cookies: SimpleCookie | None = None,  # kept for tracker interface compatibility
        api_key: str | None = None,
        session: ClientSession | None = None,
    ) -> None:
        super().__init__(server, spec, session=session)

        # GGN uses different API configuration
        self._api_endpoint = "/api.php"
        self._api_action_key = "request"
        self._auth_action = "quick_user"

        if api_key:
            # GGN API documentation specifies X-API-Key header (not Authorization)
            self.client.headers["X-API-Key"] = api_key
            self.auth_method = AuthMethod.API_KEY
        else:
            logger.warning("No API key provided for GGN")

    async def download_torrent(self, torrent_id: int | str) -> Torrent:
        """Download a torrent by its ID using GGN's torrents.php endpoint.

        Args:
            torrent_id (int | str): The ID of the torrent to download.

        Returns:
            Torrent: The parsed torrent object.
        """
        # GGN requires authkey and passkey for download
        if not self.authkey or not self.passkey:
            raise RequestException(
                "GGN requires authkey and passkey for torrent download "
                "(should be set during auth)"
            )
        download_url = self.get_torrent_link(torrent_id)
        response = await self.request(download_url)
        return Torrent.read_stream(response)

    async def search_torrent_by_hash(self, torrent_hash: str) -> dict[str, Any] | None:
        """Search torrent by hash using GGN's api.php endpoint.

        GGN requires uppercase hash for search.

        Args:
            torrent_hash (str): Torrent hash to search for (can be lowercase
                or uppercase).

        Returns:
            dict[str, Any] | None: Search result with torrent info, or None
                if not found.
        """
        return await super().search_torrent_by_hash(torrent_hash.upper())

    async def search_torrent_by_filename(
        self, filename: str
    ) -> list[TorrentSearchResult]:
        """Execute search using GGN's JSON API and return torrent list.

        Args:
            filename (str): Filename to search for.

        Returns:
            list[TorrentSearchResult]: List containing torrent information.
        """
        try:
            json_response = await self.api(
                "search",
                search_type="torrents",
                filelist=filename,
                **{"filter_cat[4]": "1"},
            )

            if json_response.get("status") != "success":
                error_msg = json_response.get("error", "unknown error")
                logger.warning(
                    "GGN API search failed for '%s': %s", filename, error_msg
                )
                return []

            response_data = json_response.get("response", {})
            results = []

            if isinstance(response_data, list) or not response_data:
                return []

            for group_data in response_data.values():
                group_torrents = group_data.get("Torrents", {})
                group_name = group_data.get("Name", "")
                for torrent_id, torrent_data in group_torrents.items():
                    size = int(torrent_data.get("Size", 0))
                    release_title = torrent_data.get("ReleaseTitle", "")
                    title = (
                        f"{group_name} - {release_title}".strip(" -")
                        if release_title
                        else group_name
                    )

                    results.append(
                        TorrentSearchResult(
                            torrent_id=int(torrent_id),
                            size=size,
                            title=title or f"Torrent {torrent_id}",
                        )
                    )

            logger.debug(
                "Filename search for '%s': found %d torrent(s)", filename, len(results)
            )
            return results
        except (RequestException, ValueError, msgspec.DecodeError) as e:
            logger.error("GGN API search error for '%s': %s", filename, e)
            return []
        except Exception as e:
            logger.error("GGN API search unexpected error for '%s': %s", filename, e)
            return []

    def parse_file_list(self, file_list_str: list[dict[str, Any]]) -> dict[str, int]:
        """Parse the file list from a torrent object.

        GGN returns file lists as a list of dicts with 'name' and 'size' keys.

        Args:
            file_list_str: Raw file list data from torrent object.

        Returns:
            dict[str, int]: Dictionary mapping filename to file size.
        """
        if not file_list_str:
            logger.debug("File list is empty or None")
            return {}

        # GGN format: list of dicts
        logger.debug("Parsing file list from GGN list format")
        return {unescape(item["name"]): int(item["size"]) for item in file_list_str}
