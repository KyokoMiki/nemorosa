import html
import threading
from abc import ABC, abstractmethod
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urljoin, urlparse

import httpx
import humanfriendly
import msgspec
from aiolimiter import AsyncLimiter
from bs4 import BeautifulSoup, Tag

from . import logger
from .config import TargetSiteConfig


class LoginException(Exception):
    pass


class RequestException(Exception):
    pass


class GazelleBase(ABC):
    """Base class for Gazelle API, containing common attributes and methods."""

    def __init__(self, server):
        timeout = httpx.Timeout(connect=30.0, read=60.0, write=30.0, pool=30.0)

        self.client = httpx.AsyncClient(timeout=timeout)
        self.client.headers = {
            "Accept-Charset": "utf-8",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko)"
            " Chrome/137.0.0.0 Safari/537.36 Edg/137.0.0.0",
        }
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
    def rate_limiter(self):
        """Get rate limiter for current event loop."""
        if self._rate_limiter is None:
            self._rate_limiter = AsyncLimiter(self.max_requests_per_10s, 10)
        return self._rate_limiter

    @property
    def announce(self):
        return f"{self.tracker_url}/{self.passkey}/announce"

    @property
    def site_host(self):
        return str(urlparse(self.server).hostname)

    async def torrent(self, torrent_id):
        """Get torrent object - subclasses need to implement specific request logic.

        Args:
            torrent_id: The ID of the torrent to retrieve.

        Returns:
            dict: Torrent object data, empty dict on error.
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

    async def _get_torrent_data(self, torrent_id):
        """Get torrent data using the Gazelle API.

        Args:
            torrent_id: The ID of the torrent to retrieve.

        Returns:
            dict: Response data from the API.
        """
        return await self.ajax("torrent", id=torrent_id)

    def parse_file_list(self, file_list_str):
        """Parse the file list from a torrent object.

        The file list is expected to be a string with entries separated by '|||'.
        Each entry is in the format 'filename{{{filesize}}}'.

        Args:
            file_list_str (str): Raw file list string from torrent object.

        Returns:
            dict: Dictionary mapping filename to file size.
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
                filename = html.unescape(parts[0].strip())
                filesize = parts[1].removesuffix("}}}").strip()
                file_list[filename] = int(filesize)
            else:
                logger.warning(f"Malformed entry in file list: {entry}")

        return file_list

    async def download_torrent(self, torrent_id):
        """Download a torrent by its ID.

        Args:
            torrent_id: The ID of the torrent to download.

        Returns:
            bytes: The content of the torrent file, or None if download fails.
        """
        if self.auth_method == "api_key":
            ajaxpage = self.server + "/ajax.php"
            response = await self.request(ajaxpage, params={"action": "download", "id": torrent_id})
        else:
            torrent_link = self.get_torrent_link(torrent_id)
            response = await self.request(torrent_link)

        if response.status_code != 200:
            logger.error(f"Status of request is {response.status_code}. Aborting...")
            logger.error(f"Response content: {response.content}")
            raise RequestException(f"HTTP {response.status_code}: {response.content}")

        logger.debug(f"Torrent {torrent_id} downloaded successfully")
        return response.content

    def get_torrent_url(self, torrent_id):
        """Get the permalink for a torrent by its ID.

        Args:
            torrent_id: The ID of the torrent.

        Returns:
            str: Torrent permalink.
        """
        return f"{self.server}/torrents.php?torrentid={torrent_id}"

    def get_torrent_link(self, torrent_id):
        """Get the direct download link for a torrent by its ID.

        Args:
            torrent_id: The ID of the torrent.

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
    async def search_torrent_by_filename(self, filename) -> list:
        """Search torrents by filename - subclasses must implement specific logic.

        Args:
            filename (str): Filename to search for.

        Returns:
            list: List of matching torrent objects.
        """
        pass

    async def search_torrent_by_hash(self, torrent_hash):
        """Search torrent by hash using the Gazelle API.

        Args:
            torrent_hash (str): Torrent hash to search for.

        Returns:
            dict: Search result with torrent information, or None if not found.
        """
        try:
            response = await self.ajax("torrent", hash=torrent_hash)
            if response.get("status") == "success":
                logger.debug(f"Hash search successful for hash '{torrent_hash}'")
                return response
            else:
                if response.get("error") in ["bad parameters", "bad hash parameter"]:
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

    async def ajax(self, action, **kwargs):
        """Make an AJAX request at a given action page.

        Args:
            action (str): The action to perform.
            **kwargs: Additional parameters for the request.

        Returns:
            dict: JSON response from the server.

        Raises:
            RequestException: If the request fails.
        """
        ajaxpage = self.server + "/ajax.php"
        params = {"action": action}
        if self.authkey:
            params["auth"] = self.authkey
        params.update(kwargs)

        try:
            r = await self.request(ajaxpage, params=params)
            json_response = msgspec.json.decode(r.content)
            return json_response
        except (ValueError, msgspec.DecodeError) as e:
            raise RequestException from e

    async def request(self, url, params=None, method="GET", data=None):
        """Send HTTP request and return response.

        Args:
            url (str): Request URL.
            params (dict, optional): Query parameters.
            method (str): HTTP method. Defaults to "GET".
            data (dict, optional): Request body data.

        Returns:
            httpx.Response: HTTP response object.

        Raises:
            ValueError: If unsupported HTTP method is used.
        """
        async with self.rate_limiter:
            full_url = urljoin(self.server, url)

            if method == "GET":
                response = await self.client.get(full_url, params=params)
            elif method == "POST":
                response = await self.client.post(full_url, data=data, params=params)
            else:
                raise ValueError("Unsupported HTTP method")
            return response

    @abstractmethod
    async def auth(self):
        """Authenticate with the server - subclasses must implement specific logic.

        Raises:
            RequestException: If authentication fails.
        """
        pass


class GazelleJSONAPI(GazelleBase):
    def __init__(self, api_key=None, cookies=None, server=None):
        super().__init__(server)

        # Add API key to headers (if provided)
        if api_key:
            self.client.headers["Authorization"] = api_key
            self.auth_method = "api_key"

        if api_key is None and cookies:
            for name, value in cookies.items():
                self.client.cookies.set(name, value)

    async def auth(self):
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

    async def logout(self):
        """Log out user."""
        logoutpage = self.server + "/logout.php"
        params = {"auth": self.authkey}
        await self.request(logoutpage, params=params)

    async def search_torrent_by_filename(self, filename):
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
    def __init__(self, cookies=None, server=None, api_key=None):
        super().__init__(server)

        if cookies:
            for name, value in cookies.items():
                self.client.cookies.set(name, value)
            logger.debug("Using provided cookies")
        else:
            logger.warning("No cookies provided")

    async def auth(self):
        """Get authkey and passkey from server by performing a blank search.

        Raises:
            RequestException: If authentication fails.
        """
        try:
            await self.search_torrent_by_filename("")
        except RequestException as e:
            logger.error(f"Failed to authenticate: {e}")
            raise

    async def search_torrent_by_filename(self, filename):
        """Execute search and return torrent list.

        Args:
            filename (str): Filename to search for.

        Returns:
            list: List containing torrent information.
        """
        params = {"action": "advanced", "filelist": filename}

        response = await self.request("torrents.php", params=params)
        return self.parse_search_results(response.text)

    def parse_search_results(self, html_content):
        """Parse search results page.

        Args:
            html_content (str): HTML content of the search results page.

        Returns:
            list: List of parsed torrent information.
        """
        soup = BeautifulSoup(html_content, "lxml")
        torrents = []

        # Find all torrents under albums
        torrent_rows = soup.select("tr.group_torrent")
        for torrent_row in torrent_rows:
            torrent = self.parse_torrent_row(torrent_row)
            if torrent:
                torrents.append(torrent)

        return torrents

    def parse_torrent_row(self, row: Tag):
        """Parse single torrent row.

        Args:
            row (Tag): BeautifulSoup Tag object representing a torrent row.

        Returns:
            dict: Parsed torrent information, or None if parsing fails.
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

            # Get torrent size
            size_cell = row.select("td")[3]  # Column 4 is size
            size_text = size_cell.get_text(strip=True).replace(",", "")
            size = humanfriendly.parse_size(size_text, binary=True)

            # Get title
            title_node = row.select_one("td:nth-child(2) > div > a")
            title = title_node.get_text(strip=True) if title_node else f"Torrent {torrent_id}"

            # Get torrent type
            format_nodes = row.select("td:nth-child(2) > div > span")
            formats = [span.get_text(strip=True) for span in format_nodes] if format_nodes else []

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
}


def get_api_instance(server: str, cookies: dict | None = None, api_key: str | None = None):
    """Get appropriate API instance based on server address.

    Args:
        server (str): Server address.
        cookies (dict | None): Optional cookies.
        api_key (str | None): Optional API key.

    Returns:
        GazelleBase: API instance.

    Raises:
        ValueError: If unsupported server is provided.
    """
    if server not in TRACKER_SPECS:
        raise ValueError(f"Unsupported server: {server}")

    api_class = TRACKER_SPECS[server].api_type

    return api_class(cookies=cookies, api_key=api_key, server=server)


# Global target_apis instance
_target_apis_instance: list[GazelleJSONAPI | GazelleParser] = []
_target_apis_lock = threading.Lock()


def get_target_apis() -> list[GazelleJSONAPI | GazelleParser]:
    """Get global target_apis instance.

    Returns:
        list[GazelleJSONAPI | GazelleParser]: Target APIs instance.
    """
    return _target_apis_instance


def set_target_apis(target_apis: list[GazelleJSONAPI | GazelleParser]) -> None:
    """Set global target_apis instance.

    Args:
        target_apis: List of target API connections to set as current.
    """
    global _target_apis_instance
    with _target_apis_lock:
        _target_apis_instance = target_apis


async def setup_api_connections(target_sites: list[TargetSiteConfig]):
    """Establish API connections.

    Args:
        target_sites (list): List of TargetSiteConfig objects.

    Returns:
        list: List of established API connections.
    """
    logger.section("===== Establishing API Connections =====")
    target_apis = []

    for i, site in enumerate(target_sites):
        # Convert cookie string to dict if present
        site_cookies = {key: morsel.value for key, morsel in SimpleCookie(site.cookie).items()} if site.cookie else None

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
        logger.critical("No API connections were successful. Exiting.")
        import sys

        sys.exit(1)

    logger.success(f"Successfully connected to {len(target_apis)} target site(s)")
    return target_apis
