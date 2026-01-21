"""
Gazelle API module for nemorosa.

Provides API implementations for Gazelle-based torrent sites, including JSON
API and HTML parsing.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Collection
from contextlib import suppress
from enum import StrEnum
from html import unescape
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import anyio
import msgspec
from aiohttp import ClientSession, ClientTimeout, CookieJar, TooManyRedirects
from aiolimiter import AsyncLimiter
from bs4 import BeautifulSoup, Tag
from humanfriendly import InvalidSize, parse_size
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from torf import Torrent

from . import logger
from .config import TargetSiteConfig


class AuthMethod(StrEnum):
    """Authentication method enumeration."""

    COOKIES = "cookies"
    API_KEY = "api_key"


class InvalidCredentialsException(Exception):
    pass


class RequestException(Exception):
    pass


# Reusable retry decorator for API calls
_auth_retry = retry(
    stop=stop_after_attempt(8),
    wait=wait_exponential(multiplier=0.4, max=60),
    before_sleep=before_sleep_log(logging.getLogger("nemorosa"), logging.WARNING),
    retry=retry_if_exception_type(RequestException),
    reraise=True,
)


class TorrentSearchResult(msgspec.Struct):
    """Standardized torrent search result structure.

    Attributes:
        torrent_id: Unique identifier for the torrent.
        size: Size of the torrent in bytes.
        title: Display title of the torrent.
    """

    torrent_id: int
    size: int
    title: str


class GazelleBase(ABC):
    """Base class for Gazelle API, containing common attributes and methods."""

    def __init__(self, server: str) -> None:
        timeout = ClientTimeout(total=60.0, connect=30.0, sock_read=60.0)

        headers = {
            "Accept-Charset": "utf-8",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0"
            ),
        }
        self.cookie_jar = CookieJar()
        self.client = ClientSession(
            timeout=timeout, headers=headers, cookie_jar=self.cookie_jar
        )
        self.server = server
        self.authkey = None
        self.passkey = None
        self.auth_method = AuthMethod.COOKIES  # Default authentication method

        # API configuration - subclasses can override these
        self._api_endpoint = "/ajax.php"
        self._api_action_key = "action"
        self._auth_action = "index"  # Action name for authentication API call

        spec = TRACKER_SPECS[server]
        self._rate_limiter = None
        self.max_requests_per_10s = spec.max_requests_per_10s
        self.source_flag = spec.source_flag
        self.tracker_url = spec.tracker_url
        self.tracker_query = spec.tracker_query

    @property
    def rate_limiter(self) -> AsyncLimiter:
        """Get rate limiter for current event loop."""
        if self._rate_limiter is None:
            self._rate_limiter = AsyncLimiter(self.max_requests_per_10s, 10)
        return self._rate_limiter

    @property
    def announce(self) -> str:
        return f"{self.tracker_url}/{self.passkey}/announce"

    @property
    def site_host(self) -> str:
        return str(urlparse(self.server).hostname)

    async def close(self) -> None:
        """Close the aiohttp ClientSession."""
        await self.client.close()

    async def torrent(self, torrent_id: int | str) -> dict[str, Any]:
        """Get torrent object - subclasses need to implement specific request logic.

        Args:
            torrent_id (int | str): The ID of the torrent to retrieve.

        Returns:
            dict[str, Any]: Torrent object data, empty dict on error.
        """
        torrent_object = {}
        logger.debug("Getting torrent by id: %s", torrent_id)
        try:
            torrent_lookup = await self._get_torrent_data(torrent_id)
        except Exception as e:
            logger.error("Failed to get torrent by id %s. Error: %s", torrent_id, e)
            return torrent_object  # return empty dict on error

        torrent_lookup_status = torrent_lookup.get("status", None)
        if torrent_lookup_status == "success":
            logger.debug("Torrent lookup successful for id: %s", torrent_id)
            torrent_object = torrent_lookup["response"]["torrent"]
            torrent_object["fileList"] = self.parse_file_list(
                torrent_object.get("fileList", "")
            )
        else:
            logger.error(
                "Torrent lookup failed for id: %s. Status: %s",
                torrent_id,
                torrent_lookup_status,
            )
        return torrent_object

    async def _get_torrent_data(self, torrent_id: int | str) -> dict[str, Any]:
        """Get torrent data using the Gazelle API.

        Args:
            torrent_id (int | str): The ID of the torrent to retrieve.

        Returns:
            dict[str, Any]: Response data from the API.
        """
        return await self.api("torrent", id=torrent_id)

    def parse_file_list(self, file_list_str: Any) -> dict[str, int]:
        """Parse the file list from a torrent object.

        Default implementation expects a string with entries separated by '|||'.
        Each entry is in the format 'filename{{{filesize}}}'.
        Subclasses may override to handle different formats.

        Args:
            file_list_str: Raw file list data from torrent object.

        Returns:
            dict[str, int]: Dictionary mapping filename to file size.
        """
        if not file_list_str:
            logger.warning("File list is empty or None")
            return {}

        logger.debug("Parsing file list")
        # split the string into individual entries
        entries = file_list_str.split("|||")
        file_list = {}
        for entry in entries:
            # split filename and filesize
            parts = entry.split("{{{")
            if len(parts) == 2:
                filename = unescape(parts[0].strip())
                filesize = parts[1].removesuffix("}}}").strip()
                file_list[filename] = int(filesize)
            else:
                logger.warning("Malformed entry in file list: %s", entry)

        return file_list

    async def download_torrent(self, torrent_id: int | str) -> Torrent:
        """Download a torrent by its ID and parse it using torf.

        Args:
            torrent_id (int | str): The ID of the torrent to download.

        Returns:
            Torrent: The parsed torrent object.
        """
        if self.auth_method == AuthMethod.API_KEY:
            ajaxpage = self.server + "/ajax.php"
            response = await self.request(
                ajaxpage, params={"action": "download", "id": torrent_id}
            )
        else:
            torrent_link = self.get_torrent_link(torrent_id)
            response = await self.request(torrent_link)

        logger.debug("Torrent %s downloaded successfully", torrent_id)
        return Torrent.read_stream(response)

    def get_torrent_url(self, torrent_id: int | str) -> str:
        """Get the permalink for a torrent by its ID.

        Args:
            torrent_id (int | str): The ID of the torrent.

        Returns:
            str: Torrent permalink.
        """
        return f"{self.server}/torrents.php?torrentid={torrent_id}"

    def get_torrent_link(self, torrent_id: int | str) -> str:
        """Get the direct download link for a torrent by its ID.

        Args:
            torrent_id (int | str): The ID of the torrent.

        Returns:
            str: Direct download URL for the torrent.
        """
        return (
            f"{self.server}/torrents.php?action=download"
            f"&id={torrent_id}"
            f"&authkey={self.authkey}"
            f"&torrent_pass={self.passkey}"
        )

    @abstractmethod
    async def search_torrent_by_filename(
        self, filename: str
    ) -> list[TorrentSearchResult]:
        """Search torrents by filename - subclasses must implement specific logic.

        Args:
            filename (str): Filename to search for.

        Returns:
            list[TorrentSearchResult]: List containing torrent information.
        """

    async def search_torrent_by_hash(self, torrent_hash: str) -> dict[str, Any] | None:
        """Search torrent by hash using the Gazelle API.

        Args:
            torrent_hash (str): Torrent hash to search for.

        Returns:
            dict[str, Any] | None: Search result with torrent info, or None
                if not found.
        """
        try:
            response = await self.api("torrent", hash=torrent_hash)
            if response.get("status") == "success":
                logger.debug("Hash search successful for hash '%s'", torrent_hash)
                return response
            else:
                if response.get("error") in ("bad parameters", "bad hash parameter"):
                    logger.debug("No torrent found matching hash '%s'", torrent_hash)
                    return None
                else:
                    logger.error(
                        "Error searching for torrent by hash '%s': %s",
                        torrent_hash,
                        response.get("error"),
                    )
                    raise RequestException(
                        f"Error searching for torrent by hash "
                        f"'{torrent_hash}': {response.get('error')}"
                    )
        except Exception as e:
            logger.error(
                "Error searching for torrent by hash '%s': %s", torrent_hash, e
            )
            raise

    async def api(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Make an API request at a given endpoint.

        Args:
            action (str): The action to perform.
            **kwargs (Any): Additional parameters for the request.

        Returns:
            dict[str, Any]: JSON response from the server.

        Raises:
            RequestException: If the request fails.
        """
        apipage = self.server + self._api_endpoint
        params = {self._api_action_key: action}
        if self.auth_method != AuthMethod.API_KEY and self.authkey:
            params["auth"] = self.authkey
        params.update(kwargs)

        try:
            # Do not check status code, because Gazelle API may return valid
            # JSON on error
            content = await self.request(
                apipage, params=params, check_status_code=False
            )
            return msgspec.json.decode(content)
        except (ValueError, msgspec.DecodeError) as e:
            raise RequestException from e

    async def request(
        self,
        url: str,
        params: dict[str, Any] | None = None,
        check_status_code: bool = True,
    ) -> bytes:
        """Send HTTP GET request and return response content.

        Args:
            url (str): Request URL.
            params (dict[str, Any] | None, optional): Query parameters.
            check_status_code (bool): If True, raise exception for non-200
                status. Defaults to True.

        Returns:
            bytes: Response content.

        Raises:
            RequestException: If request fails and check_status_code is True.
        """
        async with self.rate_limiter:
            full_url = urljoin(self.server, url)

            try:
                async with self.client.get(full_url, params=params) as aio_response:
                    # Read response efficiently
                    chunks = []
                    async for chunk in aio_response.content.iter_chunked(8192):
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    status = aio_response.status
            except TooManyRedirects as e:
                # Too many redirects usually means invalid/expired cookies
                raise InvalidCredentialsException(
                    f"Too many redirects (likely invalid cookies): {e}"
                ) from e
            except Exception as e:
                raise RequestException(f"Request error: {e}") from e

            if status != 200 and check_status_code:
                logger.debug("Status of request is %s. Aborting...", status)
                logger.debug("Response content (first 500 bytes): %s", content[:500])
                raise RequestException(f"HTTP {status}: {content[:500]}")

            return content

    @_auth_retry
    async def auth(self) -> None:
        """Authenticate with the server and get authkey/passkey.

        Raises:
            InvalidCredentialsException: If credentials are invalid.
            RequestException: If authentication fails for other reasons.
        """
        accountinfo = await self.api(self._auth_action)
        if accountinfo.get("status") != "success":
            error = accountinfo.get("error", "unknown error")
            if error in ("bad credentials", "invalid token", "APIKey is not valid."):
                raise InvalidCredentialsException(f"Invalid credentials: {error}")
            raise RequestException(f"Authentication failed: {error}")
        try:
            self.authkey = accountinfo["response"]["authkey"]
            self.passkey = accountinfo["response"]["passkey"]
        except KeyError as e:
            raise RequestException(f"Invalid response format: missing {e}") from e


class GazelleJSONAPI(GazelleBase):
    def __init__(
        self,
        server: str,
        api_key: str | None = None,
        cookies: SimpleCookie | None = None,
    ) -> None:
        super().__init__(server)

        # Add API key to headers (if provided)
        if api_key:
            self.client.headers["Authorization"] = api_key
            self.auth_method = AuthMethod.API_KEY

        if api_key is None and cookies:
            self.cookie_jar.update_cookies(cookies)

    async def logout(self) -> None:
        """Log out user."""
        logoutpage = self.server + "/logout.php"
        params = {"auth": self.authkey}
        await self.request(logoutpage, params=params)

    async def search_torrent_by_filename(
        self, filename: str
    ) -> list[TorrentSearchResult]:
        params = {"filelist": filename}
        try:
            response = await self.api("browse", **params)
            # Log API response status
            if response.get("status") != "success":
                logger.warning(
                    "API failure for file '%s': %s",
                    filename,
                    msgspec.json.encode(response).decode(),
                )
                return []
            else:
                logger.debug("API search successful for file '%s'", filename)

            # Process search results
            results = []
            for group in response["response"]["results"]:
                if "torrents" not in group:
                    continue
                group_name = group.get("groupName", "")
                for torrent in group["torrents"]:
                    torrent_id = torrent.get("torrentId", "")
                    if not torrent_id:
                        continue

                    size = int(torrent.get("size", 0))
                    title = f"{group_name} - {torrent.get('remasterTitle', '')}".strip(
                        " -"
                    )

                    results.append(
                        TorrentSearchResult(
                            torrent_id=int(torrent_id),
                            size=size,
                            title=title or f"Torrent {torrent_id}",
                        )
                    )

            return results
        except Exception as e:
            logger.error(
                "Error searching for torrent by filename '%s': %s", filename, e
            )
            raise


class GazelleParser(GazelleBase):
    def __init__(
        self,
        server: str,
        cookies: SimpleCookie | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(server)

        if cookies:
            self.cookie_jar.update_cookies(cookies)
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


class GazelleGamesNet(GazelleBase):
    """GazelleGames.net (GGN) tracker implementation.

    GGN uses a JSON API with X-API-Key authentication instead of cookies.
    This class implements GGN's specific API endpoints using api.php.
    """

    def __init__(
        self,
        server: str,
        cookies: SimpleCookie | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(server)

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


class TrackerSpec(msgspec.Struct):
    """Predefined Tracker specification."""

    api_type: type[GazelleJSONAPI] | type[GazelleParser] | type[GazelleGamesNet]
    max_requests_per_10s: int
    source_flag: str
    tracker_url: str
    tracker_query: str


TRACKER_SPECS = {
    "https://redacted.sh": TrackerSpec(
        api_type=GazelleJSONAPI,
        max_requests_per_10s=10,
        source_flag="RED",
        tracker_url="https://flacsfor.me",
        tracker_query="flacsfor.me",
    ),
    "https://orpheus.network": TrackerSpec(
        api_type=GazelleJSONAPI,
        max_requests_per_10s=5,
        source_flag="OPS",
        tracker_url="https://home.opsfet.ch",
        tracker_query="home.opsfet.ch",
    ),
    "https://dicmusic.com": TrackerSpec(
        api_type=GazelleJSONAPI,
        max_requests_per_10s=5,
        source_flag="DICMusic",
        tracker_url="https://tracker.52dic.vip",
        tracker_query="tracker.52dic.vip",
    ),
    "https://libble.me": TrackerSpec(
        api_type=GazelleParser,
        max_requests_per_10s=2,
        source_flag="LENNY",
        tracker_url="https://tracker.libble.me:34443",
        tracker_query="tracker.libble.me",
    ),
    "https://lztr.me": TrackerSpec(
        api_type=GazelleParser,
        max_requests_per_10s=2,
        source_flag="LZTR",
        tracker_url="https://tracker.lztr.me:34443",
        tracker_query="tracker.lztr.me",
    ),
    "https://gazellegames.net": TrackerSpec(
        api_type=GazelleGamesNet,
        max_requests_per_10s=4,
        source_flag="GGn",
        tracker_url="https://tracker.gazellegames.net",
        tracker_query="tracker.gazellegames.net",
    ),
}


def get_api_instance(
    server: str,
    cookies: SimpleCookie | None = None,
    api_key: str | None = None,
) -> GazelleJSONAPI | GazelleParser | GazelleGamesNet:
    """Get appropriate API instance based on server address.

    Args:
        server (str): Server address.
        cookies (SimpleCookie | None): Optional cookies.
        api_key (str | None): Optional API key.

    Returns:
        GazelleJSONAPI | GazelleParser: API instance.

    Raises:
        ValueError: If unsupported server is provided.
    """
    if server not in TRACKER_SPECS:
        raise ValueError(f"Unsupported server: {server}")

    api_class = TRACKER_SPECS[server].api_type

    return api_class(server=server, cookies=cookies, api_key=api_key)


# Global target_apis instance
_target_apis_instance: list[GazelleJSONAPI | GazelleParser | GazelleGamesNet] = []
_target_apis_lock = anyio.Lock()


async def init_api(target_sites: list[TargetSiteConfig]) -> None:
    """Initialize global target APIs instance.

    Should be called once during application startup.

    Args:
        target_sites (list[TargetSiteConfig]): List of TargetSiteConfig objects.

    Raises:
        RuntimeError: If no API connections were successful or if already initialized.
    """
    global _target_apis_instance
    async with _target_apis_lock:
        if _target_apis_instance:
            raise RuntimeError("API already initialized.")

        logger.section("===== Establishing API Connections =====")
        target_apis = []

        for i, site in enumerate(target_sites):
            # Parse cookie string to SimpleCookie if present
            site_cookies = SimpleCookie(site.cookie) if site.cookie else None

            logger.debug(
                "Connecting to target site %s/%s: %s",
                i + 1,
                len(target_sites),
                site.server,
            )
            api_instance = get_api_instance(
                server=site.server, api_key=site.api_key, cookies=site_cookies
            )
            try:
                await api_instance.auth()
                target_apis.append(api_instance)
                logger.success("API connection established for %s", site.server)
            except Exception as e:
                logger.error("API connection failed for %s: %s", site.server, str(e))
                # Close the failed instance to prevent resource leak
                with suppress(Exception):
                    await api_instance.close()
                # Continue processing other sites, don't exit program

        if not target_apis:
            logger.critical("No API connections were successful")
            raise RuntimeError("Failed to establish any API connections")

        logger.success("Successfully connected to %d target site(s)", len(target_apis))
        _target_apis_instance = target_apis


def get_target_apis() -> list[GazelleJSONAPI | GazelleParser | GazelleGamesNet]:
    """Get global target APIs instance.

    Must be called after init_api() has been invoked.

    Returns:
        list[GazelleJSONAPI | GazelleParser]: Target APIs instance.

    Raises:
        RuntimeError: If target APIs have not been initialized.
    """
    if not _target_apis_instance:
        raise RuntimeError("Target APIs not initialized. Call init_api() first.")
    return _target_apis_instance


def get_api_by_tracker(
    trackers: str | Collection[str],
) -> GazelleJSONAPI | GazelleParser | GazelleGamesNet | None:
    """Get API instance by matching tracker query string.

    Args:
        trackers (str | Collection[str]): A single tracker URL string or a
            collection of tracker URLs.

    Returns:
        GazelleJSONAPI | GazelleParser | GazelleGamesNet | None: The first matching API
        instance, or None if no match is found.

    Raises:
        RuntimeError: If target APIs have not been initialized.
    """
    if not _target_apis_instance:
        raise RuntimeError("Target APIs not initialized. Call init_api() first.")

    # Convert single tracker to list for uniform processing
    tracker_list = [trackers] if isinstance(trackers, str) else trackers

    # Search for matching API instance
    for api_instance in _target_apis_instance:
        for tracker in tracker_list:
            if api_instance.tracker_query in tracker:
                return api_instance

    return None


def get_api_by_site_host(
    site_host: str,
) -> GazelleJSONAPI | GazelleParser | GazelleGamesNet | None:
    """Get API instance by matching site hostname.

    Args:
        site_host (str): Site hostname (e.g., 'redacted.sh', 'orpheus.network').

    Returns:
        GazelleJSONAPI | GazelleParser | None: The matching API instance, or
            None if no match is found.

    Raises:
        RuntimeError: If target APIs have not been initialized.
    """
    if not _target_apis_instance:
        raise RuntimeError("Target APIs not initialized. Call init_api() first.")

    # Search for matching API instance by site_host
    for api_instance in _target_apis_instance:
        if api_instance.site_host == site_host:
            return api_instance

    return None


async def cleanup_api() -> None:
    """Close all API client sessions and cleanup resources.

    This function should be called during application shutdown to properly
    close all aiohttp ClientSession instances and release resources.
    Errors during cleanup are logged but do not prevent other clients from closing.
    """
    global _target_apis_instance
    async with _target_apis_lock:
        if not _target_apis_instance:
            logger.debug("No API instances to cleanup")
            return

        logger.debug("Cleaning up %d API client(s)...", len(_target_apis_instance))
        cleanup_errors = []

        for api_instance in _target_apis_instance:
            try:
                await api_instance.close()
                logger.debug("Closed API client for %s", api_instance.server)
            except Exception as e:
                error_msg = f"Error closing API client for {api_instance.server}: {e}"
                logger.warning(error_msg)
                cleanup_errors.append(error_msg)

        # Clear the instance list
        _target_apis_instance = []

        if cleanup_errors:
            logger.warning(
                "API cleanup completed with %d error(s)", len(cleanup_errors)
            )
        else:
            logger.debug("All API clients closed successfully")
