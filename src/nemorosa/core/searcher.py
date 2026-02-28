"""Torrent search logic for nemorosa."""

import posixpath
from typing import TYPE_CHECKING

from torf import Torrent

from .. import config, logger
from ..filecompare import (
    check_conflicts,
    is_music_file,
    make_search_query,
    select_search_filenames,
)
from .models import SearchMatchResult

if TYPE_CHECKING:
    from ..clients import ClientTorrentInfo
    from ..trackers import (
        GazelleAPI,
        TorrentSearchResult,
    )

# Constants
MAX_SEARCH_RESULTS = 20


class TorrentSearcher:
    """Stateless torrent search coordinator.

    All external dependencies (api) are passed via method parameters.
    No instance state is required.
    """

    async def hash_based_search(
        self,
        *,
        torrent_object: Torrent,
        api: "GazelleAPI",
    ) -> tuple[int | None, Torrent | None]:
        """Search for torrent using hash-based search.

        Args:
            torrent_object: Torrent object for hash calculation.
            api: API instance for the target site.

        Returns:
            Torrent ID and matched torrent if found, (None, None) otherwise.
        """
        torrent_copy = Torrent.copy(torrent_object)
        target_source_flag = api.source_flag

        source_flags = [target_source_flag, ""]
        if target_source_flag == "RED":
            source_flags.append("PTH")
        elif target_source_flag == "OPS":
            source_flags.append("APL")

        for flag in source_flags:
            try:
                torrent_copy.source = flag
                torrent_hash = torrent_copy.infohash

                search_result = await api.search_torrent_by_hash(torrent_hash)
                if search_result:
                    logger.success("Found torrent by hash! Hash: %s", torrent_hash)
                    tid = int(search_result["response"]["torrent"]["id"])
                    logger.success("Found match! Torrent ID: %d", tid)
                    torrent_copy.comment = api.get_torrent_url(tid)
                    torrent_copy.trackers = [api.announce]
                    return tid, torrent_copy
            except Exception as e:
                logger.debug("Hash search failed for source '%s': %s", flag, e)

        return None, None

    async def _search_single_filename(
        self,
        fname: str,
        fdict: dict[str, int],
        tsize: int,
        scan_querys: list[str],
        api: "GazelleAPI",
    ) -> tuple[int | None, bool]:
        """Search for torrent match using a single filename.

        Args:
            fname: Filename to search for.
            fdict: File dictionary mapping filename to size.
            tsize: Total size of the torrent.
            scan_querys: List of all search query filenames.
            api: API instance for the target site.

        Returns:
            Tuple of (torrent_id, should_continue_loop).
        """
        logger.debug("Searching for file: %s", fname)
        fname_query = fname
        try:
            torrents = await api.search_torrent_by_filename(fname_query)
        except Exception as e:
            logger.error("Error searching for file '%s': %s", fname_query, e)
            raise

        logger.debug(
            "Found %s potential matches for file '%s'", len(torrents), fname_query
        )

        # If no results found and it's a music file, try fallback search
        if len(torrents) == 0 and is_music_file(fname):
            fname_query = make_search_query(posixpath.basename(fname))
            if fname_query != fname:
                logger.debug(
                    "No results found for '%s', trying fallback search "
                    "with basename: '%s'",
                    fname,
                    fname_query,
                )
                try:
                    fallback_torrents = await api.search_torrent_by_filename(
                        fname_query
                    )
                    if fallback_torrents:
                        torrents = fallback_torrents
                        logger.debug(
                            "Fallback search found %s potential matches for '%s'",
                            len(torrents),
                            fname_query,
                        )
                    else:
                        logger.debug(
                            "Fallback search also found no results for '%s'",
                            fname_query,
                        )
                except Exception as e:
                    logger.error(
                        "Error in fallback search for file basename '%s': %s",
                        fname_query,
                        e,
                    )
                    raise

        # Match by total size
        tid = next((t.torrent_id for t in torrents if t.size == tsize), None)
        if tid is not None:
            logger.success("Size match found! Torrent ID: %d (Size: %d)", tid, tsize)
            return tid, False

        # Handle cases with too many results
        if len(torrents) > MAX_SEARCH_RESULTS:
            logger.warning(
                "Too many results found for file '%s' (%s). Skipping.",
                fname_query,
                len(torrents),
            )
            return None, True

        # Match by file content
        logger.debug(
            "No size match found. Checking file contents for '%s'", fname_query
        )
        tid = await self.match_by_file_content(
            torrents=torrents,
            fname=fname,
            fdict=fdict,
            scan_querys=scan_querys,
            api=api,
        )

        if tid is not None:
            logger.debug("Match found with file '%s'. Stopping search.", fname)
            return tid, False

        logger.debug("No more results for file '%s'", fname)
        if is_music_file(fname):
            logger.debug("Stopping search as music file match is not found")
            return None, False

        return None, True

    async def filename_search(
        self,
        *,
        fdict: dict[str, int],
        tsize: int,
        api: "GazelleAPI",
    ) -> tuple[int | None, Torrent | None]:
        """Search for torrent using filename-based search.

        Args:
            fdict: File dictionary mapping filename to size.
            tsize: Total size of the torrent.
            api: API instance for the target site.

        Returns:
            Torrent ID and matched torrent if found.
        """
        tid = None
        scan_querys = select_search_filenames(fdict.keys())

        for fname in scan_querys:
            tid, should_continue = await self._search_single_filename(
                fname, fdict, tsize, scan_querys, api
            )
            if tid is not None or not should_continue:
                break

        if tid is None:
            return tid, None

        try:
            matched_torrent = await api.download_torrent(tid)
            return tid, matched_torrent
        except Exception as e:
            logger.error(
                "Failed to download torrent data for torrent ID: %d: %s", tid, e
            )
            return tid, None

    async def match_by_file_content(
        self,
        *,
        torrents: list["TorrentSearchResult"],
        fname: str,
        fdict: dict,
        scan_querys: list[str],
        api: "GazelleAPI",
    ) -> int | None:
        """Match torrents by file content.

        Args:
            torrents: List of torrents to check.
            fname: Original filename.
            fdict: File dictionary mapping filename to size.
            scan_querys: List of scan queries.
            api: API instance for the target site.

        Returns:
            Torrent ID if found, None otherwise.
        """
        for t_index, t in enumerate(torrents, 1):
            logger.debug(
                "Checking torrent #%s/%s: ID %s", t_index, len(torrents), t.torrent_id
            )

            try:
                resp = await api.torrent(t.torrent_id)
                resp_files = resp.get("fileList", {})
            except Exception as e:
                torrent_id = t.torrent_id
                logger.exception(
                    "Failed to get torrent data for ID %s: %s. "
                    "Continuing with next torrent.",
                    torrent_id,
                    e,
                )
                continue

            check_music_file = (
                fname
                if is_music_file(fname)
                else (scan_querys[-1] if scan_querys else fname)
            )

            check_size = fdict.get(check_music_file)
            if check_size is None:
                logger.debug(
                    "Key '%s' not found in fdict, skipping torrent %s",
                    check_music_file,
                    t.torrent_id,
                )
                continue

            if check_size in resp_files.values():
                if config.cfg.linking.enable_linking or not check_conflicts(
                    fdict, resp_files
                ):
                    logger.success(
                        "File match found! Torrent ID: %s (File: %s)",
                        t.torrent_id,
                        check_music_file,
                    )
                    return t.torrent_id
                else:
                    logger.debug("Conflict detected. Skipping this torrent.")
                    return None

        return None

    async def search_torrent_match(
        self,
        torrent_details: "ClientTorrentInfo",
        api: "GazelleAPI",
        torrent_object: Torrent | None,
    ) -> SearchMatchResult:
        """Search for matching torrent using hash and filename search.

        Args:
            torrent_details: Torrent details from client.
            api: API instance for the target site.
            torrent_object: Original torrent object for hash search.

        Returns:
            SearchMatchResult with match details.
        """
        tid = None
        matched_torrent = None
        hash_match = True
        search_success = True

        if torrent_object:
            try:
                logger.debug("Trying hash-based search first")
                tid, matched_torrent = await self.hash_based_search(
                    torrent_object=torrent_object, api=api
                )
            except Exception as e:
                logger.error("Hash-based search failed: %s", e)
                search_success = False

        if tid is None:
            try:
                logger.debug(
                    "No torrent found by hash, falling back to filename search"
                )
                tid, matched_torrent = await self.filename_search(
                    fdict=torrent_details.fdict,
                    tsize=torrent_details.total_size,
                    api=api,
                )
                hash_match = False
            except Exception as e:
                logger.error("Filename search failed: %s", e)
                search_success = False

        return SearchMatchResult(
            torrent_id=tid,
            matched_torrent=matched_torrent,
            hash_match=hash_match,
            search_success=search_success,
        )
