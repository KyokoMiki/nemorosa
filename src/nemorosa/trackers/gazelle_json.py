"""Gazelle JSON API implementation for nemorosa."""

from http.cookies import SimpleCookie

import msgspec
from aiohttp import ClientSession

from .. import logger
from .api_common import (
    AuthMethod,
    GazelleBase,
    TorrentSearchResult,
    TrackerSpec,
)


class GazelleJSONAPI(GazelleBase):
    def __init__(
        self,
        server: str,
        spec: TrackerSpec,
        api_key: str | None = None,
        cookies: SimpleCookie | None = None,
        session: ClientSession | None = None,
    ) -> None:
        super().__init__(server, spec, session=session)

        # Add API key to headers (if provided)
        if api_key:
            self.client.headers["Authorization"] = api_key
            self.auth_method = AuthMethod.API_KEY

        if not api_key and cookies:
            self.client.cookie_jar.update_cookies(cookies)

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
