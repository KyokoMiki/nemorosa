"""Torrent injection logic for nemorosa."""

from collections.abc import Callable
from typing import Any

from asyncer import asyncify
from torf import Torrent

from .. import config, logger
from ..clients import ClientTorrentInfo, TorrentClient
from ..filecompare import generate_link_map, generate_rename_map
from ..filelinking import create_file_links_for_torrent

# Type alias for the link function signature
LinkFn = Callable[[Torrent, str, str, dict[str, Any]], str | None]


class TorrentInjector:
    """Handles torrent injection into client with file linking and renaming.

    Args:
        torrent_client: TorrentClient instance for injection operations.
        link_fn: Optional file linking function. Defaults to
            create_file_links_for_torrent. Accepts (torrent_object,
            download_dir, torrent_name, file_mapping) and returns
            linked directory path or None.
    """

    def __init__(
        self,
        torrent_client: TorrentClient,
        link_fn: LinkFn = create_file_links_for_torrent,
    ) -> None:
        """Initialize the torrent injector.

        Args:
            torrent_client: TorrentClient instance for injection operations.
            link_fn: File linking function. Defaults to
                create_file_links_for_torrent.
        """
        self.torrent_client = torrent_client
        self.link_fn = link_fn

    async def prepare_linked_download_dir(
        self,
        torrent_object: Torrent,
        local_fdict: dict[str, int],
        matched_fdict: dict[str, int],
        download_dir: str,
        torrent_name: str,
    ) -> str:
        """Prepare download directory with file linking if enabled.

        Args:
            torrent_object: Torrent object to create links for.
            local_fdict: Local file dictionary.
            matched_fdict: Matched torrent file dictionary.
            download_dir: Original download directory.
            torrent_name: Name of the torrent.

        Returns:
            Final download directory path (linked or original).
        """
        if not config.cfg.linking.enable_linking:
            return download_dir

        file_mapping = generate_link_map(local_fdict, matched_fdict)
        final_dir = await asyncify(self.link_fn)(
            torrent_object, download_dir, torrent_name, file_mapping
        )
        if final_dir is None:
            logger.error(
                "Failed to create file links, falling back to original directory"
            )
            return download_dir
        return final_dir

    async def inject_matched_torrent(
        self,
        matched_torrent: Torrent,
        local_torrent_info: ClientTorrentInfo,
        hash_match: bool = False,
    ) -> bool:
        """Inject matched torrent into client with file linking and renaming.

        Args:
            matched_torrent: The matched torrent to inject.
            local_torrent_info: Local torrent information.
            hash_match: Whether this is a hash match (skip verification).

        Returns:
            True if injection was successful, False otherwise.
        """
        matched_fdict = {"/".join(f.parts[1:]): f.size for f in matched_torrent.files}
        rename_map = generate_rename_map(local_torrent_info.fdict, matched_fdict)

        final_download_dir = await self.prepare_linked_download_dir(
            matched_torrent,
            local_torrent_info.fdict,
            matched_fdict,
            local_torrent_info.download_dir,
            local_torrent_info.name,
        )

        logger.debug("Attempting to inject torrent: %s", local_torrent_info.name)
        logger.debug("Download directory: %s", final_download_dir)
        logger.debug("Rename map: %s", rename_map)

        success, _ = await self.torrent_client.inject_torrent(
            matched_torrent,
            final_download_dir,
            local_torrent_info.name,
            rename_map,
            hash_match,
            local_torrent_info.hash,
        )
        return success
