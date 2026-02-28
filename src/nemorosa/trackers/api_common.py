"""
Gazelle API base module for nemorosa.

Provides base class, common types, and exceptions for Gazelle-based
torrent site API implementations.
"""

import logging
from abc import ABC, abstractmethod
from enum import StrEnum
from html import unescape
from typing import Any
from urllib.parse import urljoin, urlparse

import msgspec
from aiohttp import ClientSession, ClientTimeout, CookieJar, TooManyRedirects
from aiolimiter import AsyncLimiter
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from torf import Torrent

from .. import logger


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


class TrackerSpec(msgspec.Struct):
    """Predefined Tracker specification."""

    rate_limit_max_requests: int
    rate_limit_period: float
    source_flag: str
    tracker_url: str
    tracker_query: str


class GazelleBase(ABC):
    """Base class for Gazelle API, containing common attributes and methods."""

    def __init__(
        self,
        server: str,
        spec: TrackerSpec,
        session: ClientSession | None = None,
    ) -> None:
        if session is not None:
            self.client = session
        else:
            cookie_jar = CookieJar()
            timeout = ClientTimeout(total=60.0, connect=30.0, sock_read=60.0)

            headers = {
                "Accept-Charset": "utf-8",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0"
                ),
            }
            self.client = ClientSession(
                timeout=timeout, headers=headers, cookie_jar=cookie_jar
            )

        self.server = server
        self.authkey = None
        self.passkey = None
        self.auth_method = AuthMethod.COOKIES  # Default authentication method

        # API configuration - subclasses can override these
        self._api_endpoint = "/ajax.php"
        self._api_action_key = "action"
        self._auth_action = "index"  # Action name for authentication API call

        self._rate_limiter = None
        self.rate_limit_max_requests = spec.rate_limit_max_requests
        self.rate_limit_period = spec.rate_limit_period
        self.source_flag = spec.source_flag
        self.tracker_url = spec.tracker_url
        self.tracker_query = spec.tracker_query

    @property
    def rate_limiter(self) -> AsyncLimiter:
        """Get rate limiter for current event loop."""
        if self._rate_limiter is None:
            self._rate_limiter = AsyncLimiter(
                self.rate_limit_max_requests, self.rate_limit_period
            )
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
                try:
                    file_list[filename] = int(filesize)
                except ValueError:
                    logger.warning(
                        "Invalid file size '%s' for file: %s", filesize, filename
                    )
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
                    raise RequestException(
                        f"Error searching for torrent by hash "
                        f"'{torrent_hash}': {response.get('error')}"
                    )
        except RequestException:
            raise
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
