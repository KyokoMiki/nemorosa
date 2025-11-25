"""
Gazelle API module for nemorosa.
Provides API implementations for Gazelle-based torrent sites, including JSON API and HTML parsing.
"""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Collection
from html import unescape
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import msgspec
from aiohttp import ClientSession, ClientTimeout, CookieJar
from aiolimiter import AsyncLimiter
from bs4 import BeautifulSoup, Tag
from humanfriendly import InvalidSize, parse_size
from torf import Torrent

from . import logger
from .config import TargetSiteConfig


class LoginException(Exception):
    pass


class RequestException(Exception):
    pass


class GazelleBase(ABC):
    """Base class for Gazelle API, containing common attributes and methods."""

    def __init__(self, server: str) -> None:
        timeout = ClientTimeout(total=60.0, connect=30.0, sock_read=60.0)

        headers = {
            "Accept-Charset": "utf-8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0",
        }
        self.cookie_jar = CookieJar()
        self.client = ClientSession(timeout=timeout, headers=headers, cookie_jar=self.cookie_jar)
        self.server = server
        self.authkey = None
        self.passkey = None
        self.auth_method = "cookies"  # Default authentication method

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

    @property
    def is_ggn(self) -> bool:
        """Check if this is a GazelleGames.net tracker."""
        return "gazellegames.net" in self.server

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
        logger.debug(f"Getting torrent by id: {torrent_id}")
        try:
            torrent_lookup = await self._get_torrent_data(torrent_id)
        except Exception as e:
            logger.error(f"Failed to get torrent by id {torrent_id}. Error: {e}")
            return torrent_object  # return empty dict on error

        torrent_lookup_status = torrent_lookup.get("status", None)
        if torrent_lookup_status == "success":
            logger.debug(f"Torrent lookup successful for id: {torrent_id}")
            torrent_object = torrent_lookup["response"]["torrent"]
            torrent_object["fileList"] = self.parse_file_list(torrent_object.get("fileList", ""))
        else:
            logger.error(f"Torrent lookup failed for id: {torrent_id}. Status: {torrent_lookup_status}")
        return torrent_object

    async def _get_torrent_data(self, torrent_id: int | str) -> dict[str, Any]:
        """Get torrent data using the Gazelle API.

        Args:
            torrent_id (int | str): The ID of the torrent to retrieve.

        Returns:
            dict[str, Any]: Response data from the API.
        """
        # GGN uses api.php instead of ajax.php
        if self.is_ggn:
            apipage = self.server + "/api.php"
            params = {"request": "torrent", "id": str(torrent_id)}
            content = await self.request(apipage, params=params, check_status_code=False)
            return msgspec.json.decode(content)
        else:
            return await self.ajax("torrent", id=torrent_id)

    def parse_file_list(self, file_list_data: str | list | dict) -> dict[str, int]:
        """Parse the file list from a torrent object.

        The file list can be:
        - A string with entries separated by '|||', each entry in format 'filename{{{filesize}}}'
        - A list of dicts with 'name' and 'size' keys (GGN format)
        - A dict (already parsed, return as-is)

        Args:
            file_list_data (str | list | dict): Raw file list data from torrent object.

        Returns:
            dict[str, int]: Dictionary mapping filename to file size.
        """
        if not file_list_data:
            logger.debug("File list is empty or None")
            return {}
        
        # If it's already a dict, return it
        if isinstance(file_list_data, dict):
            return file_list_data
        
        # If it's a list (GGN format: could be list of dicts or list of strings)
        if isinstance(file_list_data, list):
            logger.debug("Parsing file list from list format")
            file_list = {}
            for item in file_list_data:
                if isinstance(item, dict):
                    # Format: [{"name": "...", "size": ...}, ...]
                    name = item.get("name", "")
                    size = item.get("size", 0)
                    if name:
                        file_list[name] = int(size)
                elif isinstance(item, str):
                    # Format: ["filename{{{size}}}", ...] - parse as string entries
                    parts = item.split("{{{")
                    if len(parts) == 2:
                        filename = unescape(parts[0].strip())
                        filesize = parts[1].removesuffix("}}}").strip()
                        file_list[filename] = int(filesize)
                    else:
                        logger.warning(f"Malformed file list entry: {item}")
                else:
                    logger.warning(f"Unexpected file list item format: {item} (type: {type(item)})")
            return file_list

        # Otherwise, treat it as a string
        logger.debug("Parsing file list from string format")
        # split the string into individual entries
        entries = file_list_data.split("|||")
        file_list = {}
        for entry in entries:
            # split filename and filesize
            parts = entry.split("{{{")
            if len(parts) == 2:
                filename = unescape(parts[0].strip())
                filesize = parts[1].removesuffix("}}}").strip()
                file_list[filename] = int(filesize)
            else:
                logger.warning(f"Malformed entry in file list: {entry}")

        return file_list

    async def download_torrent(self, torrent_id: int | str) -> Torrent:
        """Download a torrent by its ID and parse it using torf.

        Args:
            torrent_id (int | str): The ID of the torrent to download.

        Returns:
            Torrent: The parsed torrent object.
        """
        # GGN uses torrents.php?action=download instead of ajax.php
        if self.is_ggn:
            # GGN requires authkey and passkey for download
            if not self.authkey or not self.passkey:
                raise RequestException("GGN requires authkey and passkey for torrent download (should be set during auth)")
            download_url = f"{self.server}/torrents.php?action=download&id={torrent_id}&authkey={self.authkey}&torrent_pass={self.passkey}"
            response = await self.request(download_url)
        elif self.auth_method == "api_key":
            ajaxpage = self.server + "/ajax.php"
            response = await self.request(ajaxpage, params={"action": "download", "id": torrent_id})
        else:
            torrent_link = self.get_torrent_link(torrent_id)
            response = await self.request(torrent_link)

        logger.debug(f"Torrent {torrent_id} downloaded successfully")
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
    async def search_torrent_by_filename(self, filename: str) -> list[dict[str, Any]]:
        """Search torrents by filename - subclasses must implement specific logic.

        Args:
            filename (str): Filename to search for.

        Returns:
            list[dict[str, Any]]: List of matching torrent objects.
        """

    async def search_torrent_by_hash(self, torrent_hash: str) -> dict[str, Any] | None:
        """Search torrent by hash using the Gazelle API.

        Args:
            torrent_hash (str): Torrent hash to search for.

        Returns:
            dict[str, Any] | None: Search result with torrent information, or None if not found.
        """
        try:
            response = await self.ajax("torrent", hash=torrent_hash)
            if response.get("status") == "success":
                logger.debug(f"Hash search successful for hash '{torrent_hash}'")
                return response
            else:
                if response.get("error") in ("bad parameters", "bad hash parameter"):
                    logger.debug(f"No torrent found matching hash '{torrent_hash}'")
                    return None
                else:
                    logger.error(f"Error searching for torrent by hash '{torrent_hash}': {response.get('error')}")
                    raise RequestException(
                        f"Error searching for torrent by hash '{torrent_hash}': {response.get('error')}"
                    )
        except Exception as e:
            logger.error(f"Error searching for torrent by hash '{torrent_hash}': {e}")
            raise

    async def ajax(self, action: str, **kwargs: Any) -> dict[str, Any]:
        """Make an AJAX request at a given action page.

        Args:
            action (str): The action to perform.
            **kwargs (Any): Additional parameters for the request.

        Returns:
            dict[str, Any]: JSON response from the server.

        Raises:
            RequestException: If the request fails.
        """
        ajaxpage = self.server + "/ajax.php"
        params = {"action": action}
        if self.authkey:
            params["auth"] = self.authkey
        params.update(kwargs)

        try:
            # For AJAX requests, do not check status code, because Gazelle API may return valid JSON on error
            content = await self.request(ajaxpage, params=params, check_status_code=False)
            json_response = msgspec.json.decode(content)
            return json_response
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
            check_status_code (bool): If True, raise exception for non-200 status. Defaults to True.

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
            except Exception as e:
                raise RequestException(f"Request error: {e}") from e

            if status != 200 and check_status_code:
                logger.debug(f"Status of request is {status}. Aborting...")
                logger.debug(f"Response content (first 500 bytes): {content[:500]}")
                raise RequestException(f"HTTP {status}: {content[:500]}")

            return content

    @abstractmethod
    async def auth(self) -> None:
        """Authenticate with the server - subclasses must implement specific logic.

        Raises:
            RequestException: If authentication fails.
        """


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
            self.auth_method = "api_key"

        if api_key is None and cookies:
            self.cookie_jar.update_cookies(cookies)

    async def auth(self) -> None:
        """Get auth key from server.

        Raises:
            RequestException: If authentication fails.
        """
        try:
            accountinfo = await self.ajax("index")
            self.authkey = accountinfo["response"]["authkey"]
            self.passkey = accountinfo["response"]["passkey"]
        except Exception as e:
            logger.error(f"Authentication failed: {e}")
            raise

    async def logout(self) -> None:
        """Log out user."""
        logoutpage = self.server + "/logout.php"
        params = {"auth": self.authkey}
        await self.request(logoutpage, params=params)

    async def search_torrent_by_filename(self, filename: str) -> list[dict[str, Any]]:
        params = {"filelist": filename}
        try:
            response = await self.ajax("browse", **params)
            # Log API response status
            if response.get("status") != "success":
                logger.warning(f"API failure for file '{filename}': {msgspec.json.encode(response).decode()}")
                return []
            else:
                logger.debug(f"API search successful for file '{filename}'")

            # Process search results
            torrents = [
                torrent
                for group in response["response"]["results"]
                if "torrents" in group
                for torrent in group["torrents"]
            ]

            return torrents
        except Exception as e:
            logger.error(f"Error searching for torrent by filename '{filename}': {e}")
            raise


class GazelleParser(GazelleBase):
    def __init__(
        self,
        server: str,
        cookies: SimpleCookie | None = None,
        api_key: str | None = None,
    ) -> None:
        super().__init__(server)

        # GGN uses API keys, not cookies
        if "gazellegames.net" in server:
            if api_key:
                # GGN API documentation specifies X-API-Key header (not Authorization)
                self.client.headers["X-API-Key"] = api_key
                self.auth_method = "api_key"
                logger.debug("Using API key for GGN authentication (X-API-Key header)")
            else:
                logger.warning("No API key provided for GGN")
        elif cookies:
            self.cookie_jar.update_cookies(cookies)
            logger.debug("Using provided cookies")
        else:
            logger.warning("No cookies provided")

    async def auth(self) -> None:
        """Get authkey and passkey from server by performing a blank search.

        Raises:
            RequestException: If authentication fails.
        """
        # For GGN, get authkey and passkey from quick_user endpoint
        if self.is_ggn:
            try:
                # GGN has quick_user endpoint that returns authkey and passkey
                apipage = self.server + "/api.php"
                params = {"request": "quick_user"}
                
                content = await self.request(apipage, params=params, check_status_code=False)
                
                if len(content) == 0:
                    logger.warning("GGN quick_user returned empty response - API key may be invalid")
                    raise RequestException("GGN API returned empty response")
                
                json_response = msgspec.json.decode(content)
                
                if json_response.get("status") == "success":
                    user_data = json_response.get("response", {})
                    self.authkey = user_data.get("authkey")
                    self.passkey = user_data.get("passkey")
                else:
                    error_msg = json_response.get("error", "unknown error")
                    logger.error(f"GGN API authentication failed: {error_msg}")
                    raise RequestException(f"GGN API authentication failed: {error_msg}")
            except RequestException:
                raise
            except Exception as e:
                logger.error(f"Failed to authenticate with GGN API: {e}")
                raise RequestException(f"GGN API authentication error: {e}") from e
        else:
            # For other trackers, use blank search
            try:
                await self.search_torrent_by_filename("")
            except RequestException as e:
                logger.error(f"Failed to authenticate: {e}")
                raise

    async def search_torrent_by_hash(self, torrent_hash: str) -> dict[str, Any] | None:
        """Search torrent by hash - override for trackers that may not support hash search.

        For trackers using HTML parsing (GazelleParser), hash search may not be available.
        GGN doesn't support hash search - both the torrent and search endpoints fail.
        This method gracefully returns None if hash search is not supported,
        allowing the system to fall back to filename-based search.

        Args:
            torrent_hash (str): Torrent hash to search for.

        Returns:
            dict[str, Any] | None: Search result with torrent information, or None if not found/not supported.
        """
        # GGN doesn't support hash search - the hash parameter on search endpoint doesn't actually filter by hash
        # The torrent endpoint with hash parameter returns "bad hash parameter" error
        # So we skip hash search for GGN and rely on filename search fallback
        if self.is_ggn:
            return None
        
        # For other trackers, try the standard ajax.php approach
        try:
            response = await self.ajax("torrent", hash=torrent_hash)
            if response.get("status") == "success":
                logger.debug(f"Hash search successful for hash '{torrent_hash}'")
                return response
            else:
                if response.get("error") in ("bad parameters", "bad hash parameter"):
                    logger.debug(f"No torrent found matching hash '{torrent_hash}'")
                    return None
                else:
                    # For HTML parser trackers, hash search might not be supported
                    # Log at debug level and return None to allow fallback to filename search
                    logger.debug(
                        f"Hash search not available or failed for hash '{torrent_hash}': "
                        f"{response.get('error', 'unknown error')}. Will fall back to filename search."
                    )
                    return None
        except RequestException as e:
            # For HTML parser trackers, hash search endpoint might not exist
            # Log at debug level and return None to allow fallback to filename search
            logger.debug(f"Hash search endpoint not available: {e}. Will fall back to filename search.")
            return None
        except Exception as e:
            # Any other error - log and return None to allow fallback
            logger.debug(f"Hash search error for hash '{torrent_hash}': {e}. Will fall back to filename search.")
            return None

    async def search_torrent_by_filename(self, filename: str) -> list[dict[str, Any]]:
        """Execute search and return torrent list.

        Args:
            filename (str): Filename to search for.

        Returns:
            list[dict[str, Any]]: List containing torrent information.
        """
        # GGN uses JSON API instead of HTML parsing
        if self.is_ggn:
            apipage = self.server + "/api.php"
            params = {
                "request": "search",
                "search_type": "torrents",
                "filelist": filename,
                "filter_cat[4]": "1"
            }
            
            try:
                content = await self.request(apipage, params=params, check_status_code=False)
                json_response = msgspec.json.decode(content)
                
                if json_response.get("status") != "success":
                    error_msg = json_response.get("error", "unknown error")
                    logger.warning(f"GGN API search failed for '{filename}': {error_msg}")
                    return []
                
                response_data = json_response.get("response", {})
                torrents = []
                
                if isinstance(response_data, list) or not response_data:
                    return []
                
                for group_id, group_data in response_data.items():
                    group_torrents = group_data.get("Torrents", {})
                    for torrent_id, torrent_data in group_torrents.items():
                        torrent_info = {
                            "torrentId": torrent_id,
                            "size": int(torrent_data.get("Size", 0)),
                            "title": group_data.get("Name", f"Torrent {torrent_id}"),
                            "formats": [],
                            "info": torrent_data.get("ReleaseTitle", ""),
                        }
                        torrents.append(torrent_info)
                
                logger.debug(f"Filename search for '{filename}': found {len(torrents)} torrent(s)")
                return torrents
            except (RequestException, ValueError, msgspec.DecodeError) as e:
                logger.error(f"GGN API search error for '{filename}': {e}")
                return []
            except Exception as e:
                logger.error(f"GGN API search unexpected error for '{filename}': {e}")
                return []
        
        # For other trackers, use HTML parsing
        params = {"action": "advanced", "filelist": filename}
        logger.debug(f"Filename search: requesting torrents.php with params: {params}")
        content = await self.request("torrents.php", params=params)
        torrents = self.parse_search_results(content)
        logger.debug(f"Filename search for '{filename}': found {len(torrents)} torrent(s)")
        return torrents

    def parse_search_results(self, html_content: bytes | str) -> list[dict[str, Any]]:
        """Parse search results page.

        Args:
            html_content (bytes | str): HTML content of the search results page.

        Returns:
            list[dict[str, Any]]: List of parsed torrent information.
        """
        # Convert bytes to string if needed
        if isinstance(html_content, bytes):
            html_content = html_content.decode('utf-8', errors='ignore')
        
        soup = BeautifulSoup(html_content, "lxml")

        # Find all torrents under albums
        torrent_rows = soup.select("tr.group_torrent")
        logger.debug(f"Found {len(torrent_rows)} torrent row(s) in HTML")
        
        torrents = [torrent for torrent_row in torrent_rows if (torrent := self.parse_torrent_row(torrent_row))]
        
        return torrents

    def parse_torrent_row(self, row: Tag) -> dict[str, Any] | None:
        """Parse single torrent row.

        Args:
            row (Tag): BeautifulSoup Tag object representing a torrent row.

        Returns:
            dict[str, Any] | None: Parsed torrent information, or None if parsing fails.
        """
        try:
            # Get download link
            download_link = row.select_one('a[href^="torrents.php?action=download&id="]')
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

            full_download_url = urljoin(self.server, href)

            # Get all table cells
            cells = row.select("td")
            
            # Get torrent size - try column 3 (index 3) first, then search if needed
            size = None
            if len(cells) > 3:
                try:
                    size_text = cells[3].get_text(strip=True).replace(",", "")
                    size = parse_size(size_text, binary=True)
                except (InvalidSize, ValueError):
                    pass
            
            # If column 3 didn't work, search for a cell that parses as size
            if size is None:
                for cell in cells:
                    size_text = cell.get_text(strip=True).replace(",", "")
                    # Skip cells that look like time/date strings
                    if any(time_word in size_text.lower() for time_word in ["ago", "hour", "day", "week", "month", "year", "minute"]):
                        continue
                    try:
                        size = parse_size(size_text, binary=True)
                        break
                    except (InvalidSize, ValueError):
                        continue
            
            # If we still couldn't find a valid size, log a warning and skip this torrent
            if size is None:
                logger.warning(f"Could not parse size for torrent {torrent_id}, skipping")
                return None

            # Get title
            title = None
            title_node = row.select_one("td:nth-child(2) > div > a")
            if title_node:
                title = title_node.get_text(strip=True)
            
            if not title:
                title = f"Torrent {torrent_id}"

            # Get torrent formats
            formats = []
            format_nodes = row.select("td:nth-child(2) > div > span")
            if format_nodes:
                formats = [span.get_text(strip=True) for span in format_nodes]

            # Get torrent info
            info_nodes = row.select("td:nth-child(2) > div > span.small")
            info_text = info_nodes[0].get_text(strip=True) if info_nodes else ""

            return {
                "torrentId": torrent_id,
                "download_link": full_download_url,
                "size": size,
                "title": title,
                "formats": formats,
                "info": info_text,
            }
        except Exception as e:
            logger.error(f"Error parsing torrent row: {e}")
            return None


class TrackerSpec(msgspec.Struct):
    """Predefined Tracker specification."""

    api_type: type[GazelleJSONAPI] | type[GazelleParser]
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
        api_type=GazelleParser,
        max_requests_per_10s=2,
        source_flag="GGn",
        tracker_url="https://tracker.gazellegames.net",
        tracker_query="tracker.gazellegames.net",
    ),
}


def get_api_instance(
    server: str,
    cookies: SimpleCookie | None = None,
    api_key: str | None = None,
) -> GazelleJSONAPI | GazelleParser:
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
_target_apis_instance: list[GazelleJSONAPI | GazelleParser] = []
_target_apis_lock = asyncio.Lock()


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

            logger.debug(f"Connecting to target site {i + 1}/{len(target_sites)}: {site.server}")
            try:
                api_instance = get_api_instance(server=site.server, api_key=site.api_key, cookies=site_cookies)
                await api_instance.auth()
                target_apis.append(api_instance)
                logger.success(f"API connection established for {site.server}")
            except Exception as e:
                logger.error(f"API connection failed for {site.server}: {str(e)}")
                # Continue processing other sites, don't exit program

        if not target_apis:
            logger.critical("No API connections were successful")
            raise RuntimeError("Failed to establish any API connections")

        logger.success(f"Successfully connected to {len(target_apis)} target site(s)")
        _target_apis_instance = target_apis


def get_target_apis() -> list[GazelleJSONAPI | GazelleParser]:
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
) -> GazelleJSONAPI | GazelleParser | None:
    """Get API instance by matching tracker query string.

    Args:
        trackers (str | Collection[str]): A single tracker URL string or a collection of tracker URLs.

    Returns:
        GazelleJSONAPI | GazelleParser | None: The first matching API instance, or None if no match is found.

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
) -> GazelleJSONAPI | GazelleParser | None:
    """Get API instance by matching site hostname.

    Args:
        site_host (str): Site hostname (e.g., 'redacted.sh', 'orpheus.network').

    Returns:
        GazelleJSONAPI | GazelleParser | None: The matching API instance, or None if no match is found.

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

        logger.debug(f"Cleaning up {len(_target_apis_instance)} API client(s)...")
        cleanup_errors = []

        for api_instance in _target_apis_instance:
            try:
                await api_instance.close()
                logger.debug(f"Closed API client for {api_instance.server}")
            except Exception as e:
                error_msg = f"Error closing API client for {api_instance.server}: {e}"
                logger.warning(error_msg)
                cleanup_errors.append(error_msg)

        # Clear the instance list
        _target_apis_instance = []

        if cleanup_errors:
            logger.warning(f"API cleanup completed with {len(cleanup_errors)} error(s)")
        else:
            logger.debug("All API clients closed successfully")
