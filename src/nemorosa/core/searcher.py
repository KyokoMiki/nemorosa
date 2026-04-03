"""Torrent search logic for nemorosa."""

import posixpath
from typing import TYPE_CHECKING

from torf import Torrent

from .. import config, logger
from ..filecompare import (
    check_conflicts,
    is_music_file,
    is_size_approx_equal,
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
        target_source_flag = api.spec.source_flag

        source_flags = {target_source_flag, ""}
        if target_source_flag == "RED":
            source_flags.add("PTH")
        elif target_source_flag == "OPS":
            source_flags.add("APL")

        for flag in source_flags:
            try:
                torrent_copy.source = flag
                torrent_hash = torrent_copy.infohash

                result = await api.search_torrent_by_hash(torrent_hash)
                if result is not None:
                    logger.success("Found torrent by hash! Hash: %s", torrent_hash)
                    logger.success("Found match! Torrent ID: %d", result.torrent_id)
                    torrent_copy.comment = api.get_torrent_url(result.torrent_id)
                    torrent_copy.trackers = [api.announce]
                    return result.torrent_id, torrent_copy
            except Exception as e:
                logger.debug("Hash search failed for source '%s': %s", flag, e)

        return None, None

    async def _search_with_fallback(
        self,
        fname: str,
        api: "GazelleAPI",
    ) -> list["TorrentSearchResult"]:
        """Search by filename, falling back to basename for music files.

        Args:
            fname: Filename to search for.
            api: API instance for the target site.

        Returns:
            List of search results.
        """
        try:
            torrents = await api.search_torrent_by_filename(fname)
        except Exception as e:
            logger.error("Error searching for file '%s': %s", fname, e)
            raise

        logger.debug("Found %s potential matches for file '%s'", len(torrents), fname)

        if len(torrents) == 0 and is_music_file(fname):
            basename_query = make_search_query(posixpath.basename(fname))
            if basename_query != fname:
                logger.debug(
                    "No results for '%s', trying basename: '%s'",
                    fname,
                    basename_query,
                )
                try:
                    fallback = await api.search_torrent_by_filename(basename_query)
                    if fallback:
                        logger.debug(
                            "Fallback found %s matches for '%s'",
                            len(fallback),
                            basename_query,
                        )
                        return fallback
                    logger.debug(
                        "Fallback also found no results for '%s'",
                        basename_query,
                    )
                except Exception as e:
                    logger.error(
                        "Error in fallback search for '%s': %s",
                        basename_query,
                        e,
                    )
                    raise

        return torrents

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
        torrents = await self._search_with_fallback(fname, api)

        # Match by total size
        tid = next((t.torrent_id for t in torrents if t.size == tsize), None)
        if tid is not None:
            logger.success("Size match found! Torrent ID: %d (Size: %d)", tid, tsize)
            return tid, False

        # Handle cases with too many results
        if len(torrents) > MAX_SEARCH_RESULTS:
            logger.warning(
                "Too many results found for file '%s' (%s). Skipping.",
                fname,
                len(torrents),
            )
            return None, True

        # Determine the file to check and its expected size
        check_file = self._select_check_file(fname, scan_querys)
        check_size = fdict.get(check_file)
        if check_size is None:
            logger.debug("Key '%s' not found in fdict, skipping", check_file)
            return None, True

        # Match by file content — choose strategy based on size precision
        logger.debug("No size match found. Checking file contents for '%s'", fname)
        if api.has_precise_sizes:
            tid = await self._match_by_file_content(
                torrents=torrents,
                check_size=check_size,
                check_file=check_file,
                fdict=fdict,
                api=api,
            )
        else:
            tid = await self._match_by_download_verify(
                torrents=torrents,
                check_size=check_size,
                check_file=check_file,
                fdict=fdict,
                tsize=tsize,
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

    def _select_check_file(
        self,
        fname: str,
        scan_querys: list[str],
    ) -> str:
        """Select the file to use for size checking.

        Args:
            fname: Original filename.
            scan_querys: List of scan queries.

        Returns:
            Filename to use for size comparison.
        """
        if is_music_file(fname):
            return fname
        return scan_querys[-1] if scan_querys else fname

    def _check_file_match(
        self,
        check_size: int,
        fdict: dict[str, int],
        resp_files: dict[str, int],
        torrent_id: int,
        check_file: str,
    ) -> int | None:
        """Check if check_size matches and no conflicts exist.

        Args:
            check_size: Expected file size in bytes.
            fdict: Local file dictionary.
            resp_files: Remote file dictionary.
            torrent_id: Torrent ID for logging.
            check_file: Filename for logging.

        Returns:
            torrent_id if matched, None otherwise.
        """
        if check_size not in resp_files.values():
            return None

        if config.cfg.linking.enable_linking or not check_conflicts(fdict, resp_files):
            logger.success(
                "File match found! Torrent ID: %s (File: %s)",
                torrent_id,
                check_file,
            )
            return torrent_id

        logger.debug("Conflict detected. Skipping this torrent.")
        return None

    async def _match_by_file_content(
        self,
        *,
        torrents: list["TorrentSearchResult"],
        check_size: int,
        check_file: str,
        fdict: dict[str, int],
        api: "GazelleAPI",
    ) -> int | None:
        """Match torrents by exact file size comparison.

        Used for trackers that provide precise byte-level sizes.

        Args:
            torrents: List of torrents to check.
            check_size: Expected file size in bytes.
            check_file: Filename for logging.
            fdict: File dictionary mapping filename to size.
            api: API instance for the target site.

        Returns:
            Torrent ID if found, None otherwise.
        """
        for t_index, t in enumerate(torrents, 1):
            logger.debug(
                "Checking torrent #%s/%s: ID %s",
                t_index,
                len(torrents),
                t.torrent_id,
            )

            try:
                resp_files = await api.get_torrent_fdict(t.torrent_id)
            except Exception as e:
                logger.exception(
                    "Failed to get torrent data for ID %s: %s. "
                    "Continuing with next torrent.",
                    t.torrent_id,
                    e,
                )
                continue

            result = self._check_file_match(
                check_size, fdict, resp_files, t.torrent_id, check_file
            )
            if result is not None:
                return result

        return None

    async def _match_by_download_verify(
        self,
        *,
        torrents: list["TorrentSearchResult"],
        check_size: int,
        check_file: str,
        fdict: dict[str, int],
        tsize: int,
        api: "GazelleAPI",
    ) -> int | None:
        """Match torrents using approximate sizes, then verify via download.

        Used for trackers that only provide human-readable sizes (e.g.
        "154.66 MB") which cannot be compared exactly. Candidates are
        sorted by total size similarity first. Then for each candidate,
        filters by approximate file size and downloads the .torrent file
        to obtain exact sizes for final verification.

        Args:
            torrents: List of torrents to check.
            check_size: Expected file size in bytes.
            check_file: Filename for logging.
            fdict: File dictionary mapping filename to size.
            tsize: Total size of the local torrent for similarity sorting.
            api: API instance for the target site.

        Returns:
            Torrent ID if found, None otherwise.
        """
        # Sort by total size similarity — closer to tsize first
        sorted_torrents = sorted(torrents, key=lambda t: abs(t.size - tsize))

        for t_index, t in enumerate(sorted_torrents, 1):
            logger.debug(
                "Fuzzy checking torrent #%s/%s: ID %s",
                t_index,
                len(sorted_torrents),
                t.torrent_id,
            )

            # Stage 1: approximate match using HTML-parsed fdict
            try:
                approx_files = await api.get_torrent_fdict(t.torrent_id)
            except Exception as e:
                logger.exception(
                    "Failed to get torrent data for ID %s: %s. "
                    "Continuing with next torrent.",
                    t.torrent_id,
                    e,
                )
                continue

            approx_size = approx_files.get(check_file)
            if approx_size is None or not is_size_approx_equal(check_size, approx_size):
                continue

            logger.debug(
                "Approximate file size match for torrent %s, "
                "downloading for exact verification",
                t.torrent_id,
            )

            # Stage 2: download .torrent for exact sizes
            try:
                torrent_obj = await api.download_torrent(t.torrent_id)
                exact_files = {"/".join(f.parts[1:]): f.size for f in torrent_obj.files}
            except Exception as e:
                logger.error(
                    "Failed to download torrent %s for verification: %s",
                    t.torrent_id,
                    e,
                )
                continue

            result = self._check_file_match(
                check_size, fdict, exact_files, t.torrent_id, check_file
            )
            if result is not None:
                return result

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
