"""Core processing functions for nemorosa."""

import posixpath
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import anyio
import msgspec
from asyncer import asyncify
from pydantic import BaseModel, Field
from torf import Torrent

from . import config, logger
from .api import get_api_by_site_host, get_api_by_tracker, get_target_apis
from .client_instance import get_torrent_client
from .clients import ClientTorrentInfo, PostProcessStatus, TorrentConflictError
from .db import get_database
from .filecompare import (
    check_conflicts,
    generate_link_map,
    generate_rename_map,
    is_music_file,
    make_search_query,
    select_search_filenames,
)
from .filelinking import create_file_links_for_torrent
from .notifier import get_notifier

if TYPE_CHECKING:
    from .api import GazelleGamesNet, GazelleJSONAPI, GazelleParser, TorrentSearchResult

# Constants
MAX_SEARCH_RESULTS = 20


class ProcessStatus(StrEnum):
    """Status enumeration for process operations."""

    SUCCESS = "success"
    NOT_FOUND = "not_found"
    ERROR = "error"
    SKIPPED = "skipped"
    SKIPPED_POTENTIAL_TRUMP = "skipped_potential_trump"


class ProcessorStats(msgspec.Struct):
    """Statistics for torrent processing session."""

    found: int = 0
    downloaded: int = 0
    scanned: int = 0
    cnt_dl_fail: int = 0
    attempted: int = 0
    successful: int = 0
    failed: int = 0
    removed: int = 0


class PostProcessStats(msgspec.Struct):
    """Statistics for post-processing injected torrents."""

    matches_checked: int = 0
    matches_completed: int = 0
    matches_started_downloading: int = 0
    matches_failed: int = 0


class ProcessResponse(BaseModel):
    """Response model for process operations."""

    status: ProcessStatus = Field(..., description="Processing status")
    message: str = Field(..., description="Status message")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "success",
                    "message": "Successfully processed torrent: name (infohash)",
                },
                {
                    "status": "skipped",
                    "message": (
                        "Torrent already exists on all target trackers: "
                        "[tracker1, tracker2]"
                    ),
                },
            ]
        }
    }


class NemorosaCore:
    """Main class for processing torrents and cross-seeding operations."""

    def __init__(self):
        """Initialize the torrent processor."""
        self.torrent_client = get_torrent_client()
        self.database = get_database()
        self.stats = ProcessorStats()

    async def hash_based_search(
        self,
        *,
        torrent_object: Torrent,
        api: "GazelleJSONAPI | GazelleParser | GazelleGamesNet",
    ) -> tuple[int | None, Torrent | None]:
        """Search for torrent using hash-based search.

        Args:
            torrent_object (Torrent): Torrent object for hash calculation.
            api: API instance for the target site.

        Returns:
            tuple[int | None, Torrent | None]: Torrent ID and matched torrent
                if found, (None, None) otherwise.
        """
        torrent_copy = Torrent.copy(torrent_object)

        # Get target source flag from API
        target_source_flag = api.source_flag

        source_flags = [target_source_flag, ""]

        # Define possible source flags for the target tracker
        # This should match the logic in fertilizer
        if target_source_flag == "RED":
            source_flags.append("PTH")
        elif target_source_flag == "OPS":
            source_flags.append("APL")

        # Create a copy of the torrent and try different source flags
        for flag in source_flags:
            try:
                torrent_copy.source = flag

                # Calculate hash
                torrent_hash = torrent_copy.infohash

                # Search torrent by hash
                search_result = await api.search_torrent_by_hash(torrent_hash)
                if search_result:
                    logger.success("Found torrent by hash! Hash: %s", torrent_hash)

                    # Get torrent ID from search result
                    torrent_id = search_result["response"]["torrent"]["id"]
                    if torrent_id:
                        tid = int(torrent_id)
                        logger.success("Found match! Torrent ID: %d", tid)
                        torrent_copy.comment = api.get_torrent_url(tid)
                        torrent_copy.trackers = [api.announce]
                        return tid, torrent_copy
            except Exception as e:
                logger.debug("Hash search failed for source '%s': %s", flag, e)

        return None, None

    async def _prepare_linked_download_dir(
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
        final_dir = await asyncify(create_file_links_for_torrent)(
            torrent_object, download_dir, torrent_name, file_mapping
        )
        if final_dir is None:
            logger.error(
                "Failed to create file links, falling back to original directory"
            )
            return download_dir
        return final_dir

    async def _search_single_filename(
        self,
        fname: str,
        fdict: dict[str, int],
        tsize: int,
        scan_querys: list[str],
        api: "GazelleJSONAPI | GazelleParser | GazelleGamesNet",
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
            - torrent_id: Found torrent ID or None.
            - should_continue_loop: False if search should stop
                (match found or music file).
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
            return tid, False  # Found match, stop loop

        # Handle cases with too many results
        if len(torrents) > MAX_SEARCH_RESULTS:
            logger.warning(
                "Too many results found for file '%s' (%s). Skipping.",
                fname_query,
                len(torrents),
            )
            return None, True  # Continue to next filename

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
            return tid, False  # Found match, stop loop

        logger.debug("No more results for file '%s'", fname)
        if is_music_file(fname):
            logger.debug("Stopping search as music file match is not found")
            return None, False  # Music file not found, stop loop

        return None, True  # Continue to next filename

    async def filename_search(
        self,
        *,
        fdict: dict[str, int],
        tsize: int,
        api: "GazelleJSONAPI | GazelleParser | GazelleGamesNet",
    ) -> tuple[int | None, Torrent | None]:
        """Search for torrent using filename-based search.

        Args:
            fdict (dict): File dictionary mapping filename to size.
            tsize (int): Total size of the torrent.
            api: API instance for the target site.

        Returns:
            tuple[int | None, Torrent | None]: Torrent ID and matched torrent if found.
        """
        # search for the files with top 5 longest name
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

        # Download torrent data and create torrent object
        try:
            matched_torrent = await api.download_torrent(tid)
            return tid, matched_torrent
        except Exception as e:
            logger.error(
                "Failed to download torrent data for torrent ID: %d: %s", tid, e
            )
            return tid, None

    async def _search_torrent_by_filename_in_client(
        self, torrent_fdict: dict[str, int]
    ) -> list[ClientTorrentInfo]:
        """Search for matching torrents in client by filename using database.

        Args:
            torrent_fdict (dict): File dictionary of the incoming torrent.

        Returns:
            list: List of matching ClientTorrentInfo objects.
        """
        try:
            matched_torrents = []

            # Select top 5 longest filenames for search (sorted by length)
            scan_queries = select_search_filenames(torrent_fdict.keys())

            logger.debug(
                "Searching with %s file queries: %s", len(scan_queries), scan_queries
            )

            for fname in scan_queries:
                logger.debug("Searching for file: %s", fname)

                # Get the file size to match
                target_file_size = torrent_fdict[fname]

                # Use make_search_query to process filename
                fname_query = make_search_query(posixpath.basename(fname))
                if not fname_query:
                    continue

                fname_query_words = fname_query.split()

                # Get matching torrents from client's database cache
                candidate_torrents = (
                    await self.torrent_client.get_file_matched_torrents(
                        target_file_size=target_file_size,
                        fname_keywords=fname_query_words,
                    )
                )

                logger.debug("Found %d candidate torrents", len(candidate_torrents))

                # Verify each candidate for conflicts
                for candidate in candidate_torrents:
                    logger.debug("Verifying candidate torrent: %s", candidate.name)

                    # Database query ensured size and name match, only check conflicts
                    if config.cfg.linking.enable_linking or not check_conflicts(
                        candidate.fdict, torrent_fdict
                    ):
                        logger.success(
                            "Complete torrent match found: %s", candidate.name
                        )
                        matched_torrents.append(candidate)
                    else:
                        logger.debug(
                            "Match found but has conflicts: %s", candidate.name
                        )

                # If matching torrent found, can return early
                if matched_torrents:
                    break

                # If music file and no match found, stop searching
                if is_music_file(fname):
                    logger.debug("Stopping search as music file match is not found")
                    break

            return matched_torrents

        except Exception as e:
            logger.error("Error searching torrent by filename in client: %s", e)
            return []

    async def match_by_file_content(
        self,
        *,
        torrents: list["TorrentSearchResult"],
        fname: str,
        fdict: dict,
        scan_querys: list[str],
        api: "GazelleJSONAPI | GazelleParser | GazelleGamesNet",
    ) -> int | None:
        """Match torrents by file content.

        Args:
            torrents (list[TorrentSearchResult]): List of torrents to check.
            fname (str): Original filename.
            fdict (dict): File dictionary mapping filename to size.
            scan_querys (list[str]): List of scan queries.
            api: API instance for the target site.

        Returns:
            int | None: Torrent ID if found, None otherwise.
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

            check_music_file = fname if is_music_file(fname) else scan_querys[-1]

            # For music files, byte-level size comparison is sufficient for
            # identical matching, as it provides reliable file identification
            # without requiring full content comparison
            if fdict[check_music_file] in resp_files.values():
                # Check file conflicts
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
        # Generate file dictionary and rename map
        matched_fdict = {"/".join(f.parts[1:]): f.size for f in matched_torrent.files}
        rename_map = generate_rename_map(local_torrent_info.fdict, matched_fdict)

        # Handle file linking and rename map based on configuration
        final_download_dir = await self._prepare_linked_download_dir(
            matched_torrent,
            local_torrent_info.fdict,
            matched_fdict,
            local_torrent_info.download_dir,
            local_torrent_info.name,
        )

        logger.debug("Attempting to inject torrent: %s", local_torrent_info.name)
        logger.debug("Download directory: %s", final_download_dir)
        logger.debug("Rename map: %s", rename_map)

        # Inject torrent and handle renaming
        # Pass local_torrent_info.hash for duplicate_categories feature
        success, _ = await self.torrent_client.inject_torrent(
            matched_torrent,
            final_download_dir,
            local_torrent_info.name,
            rename_map,
            hash_match,
            local_torrent_info.hash,
        )
        return success

    async def _search_torrent_match(
        self,
        torrent_details: ClientTorrentInfo,
        api: "GazelleJSONAPI | GazelleParser | GazelleGamesNet",
        torrent_object: Torrent | None,
    ) -> tuple[int | None, Torrent | None, bool, bool]:
        """Search for matching torrent using hash and filename search.

        Args:
            torrent_details: Torrent details from client.
            api: API instance for the target site.
            torrent_object: Original torrent object for hash search.

        Returns:
            Tuple of (torrent_id, matched_torrent, hash_match, search_success).
        """
        tid = None
        matched_torrent = None
        hash_match = True
        search_success = True

        # Try hash-based search first if torrent object is available
        if torrent_object:
            try:
                logger.debug("Trying hash-based search first")
                tid, matched_torrent = await self.hash_based_search(
                    torrent_object=torrent_object, api=api
                )
            except Exception as e:
                logger.error("Hash-based search failed: %s", e)
                search_success = False

        # If hash search didn't find anything, try filename search
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

        return tid, matched_torrent, hash_match, search_success

    async def process_torrent_search(
        self,
        *,
        torrent_details: ClientTorrentInfo,
        api: "GazelleJSONAPI | GazelleParser | GazelleGamesNet",
        torrent_object: Torrent | None = None,
    ) -> tuple[bool, str | None, str | None]:
        """Process torrent search and injection.

        Args:
            torrent_details (ClientTorrentInfo): Torrent details from client.
            api: API instance for the target site.
            torrent_object (Torrent, optional): Original torrent object for hash search.

        Returns:
            tuple[bool, str | None, str | None]:
                (search_success, matched_torrent_id, matched_torrent_hash).
                - search_success: True if search completed without errors,
                  False if errors occurred.
                - matched_torrent_id: Torrent ID if found, None otherwise.
                - matched_torrent_hash: Torrent hash if injected, None otherwise.
        """
        self.stats.scanned += 1

        # Search for matching torrent
        (
            tid,
            matched_torrent,
            hash_match,
            search_success,
        ) = await self._search_torrent_match(torrent_details, api, torrent_object)

        # Handle no match found case
        if tid is None:
            logger.header("No matching torrent found")
            return search_success, None, None

        # Found a match
        self.stats.found += 1
        logger.success("Found match! Torrent ID: %s", tid)

        # Inject torrent and handle renaming
        if config.cfg.global_config.no_download:
            return search_success, str(tid), None

        matched_torrent_hash = None
        injection_success = False

        # Check if matched_torrent is None (download failed)
        if matched_torrent is None:
            logger.error("Failed to inject torrent: %s (matched_torrent is None)", tid)
        else:
            # Try to inject torrent
            try:
                injection_success = await self.inject_matched_torrent(
                    matched_torrent, torrent_details, hash_match
                )
                if injection_success:
                    self.stats.downloaded += 1
                    matched_torrent_hash = matched_torrent.infohash
                    logger.success("Torrent injected successfully")
                else:
                    logger.error("Failed to inject torrent: %s", tid)
            except TorrentConflictError as e:
                # Torrent conflict - treat as no match found
                logger.debug("Torrent conflict detected: %s", e)
                return search_success, None, None
            except Exception as e:
                # Other errors during injection - skip this torrent
                logger.exception("Error during torrent injection: %s", e)

        # Log download/injection failure
        if not injection_success:
            self.stats.cnt_dl_fail += 1
            if self.stats.cnt_dl_fail <= 10:
                logger.error(
                    "It might because the torrent id %s has reached the limitation of "
                    "non-browser downloading of %s. The failed download info will be "
                    "saved to database. You can download it from your own browser.",
                    tid,
                    api.server,
                )
                if self.stats.cnt_dl_fail == 10:
                    logger.debug(
                        "Suppressing further hinting for .torrent file "
                        "downloading failures"
                    )

        return search_success, str(tid), matched_torrent_hash

    async def process_single_torrent_from_client(
        self,
        torrent_details: ClientTorrentInfo,
        skip_scanned_check: bool = False,
    ) -> bool:
        """Process a single torrent from client torrent list.

        Args:
            torrent_details (ClientTorrentInfo): Torrent details from client.
            skip_scanned_check (bool): If True, skip already-scanned check.

        Returns:
            bool: True if any target site was successful, False otherwise.
        """

        # Try to get torrent data from torrent client for hash search
        torrent_object = await self.torrent_client.get_torrent_object(
            torrent_details.hash
        )

        # Scan and match for each target site
        any_success = False

        for api_instance in get_target_apis():
            # Check if torrent has been scanned on this site
            # (unless skip_scanned_check is True)
            if not skip_scanned_check and await self.database.is_hash_scanned(
                local_torrent_hash=torrent_details.hash,
                site_host=api_instance.site_host,
            ):
                logger.debug(
                    "Skipping already scanned torrent on %s: %s (%s)",
                    api_instance.site_host,
                    torrent_details.name,
                    torrent_details.hash,
                )
                continue
            logger.debug(
                "Trying target site: %s (tracker: %s)",
                api_instance.server,
                api_instance.tracker_query,
            )

            # Check if this content already exists on current target tracker
            if api_instance.tracker_query in torrent_details.existing_target_trackers:
                logger.debug(
                    "Content already exists on %s, skipping", api_instance.tracker_query
                )
                continue

            try:
                # Scan and match
                (
                    search_success,
                    matched_torrent_id,
                    matched_torrent_hash,
                ) = await self.process_torrent_search(
                    torrent_details=torrent_details,
                    api=api_instance,
                    torrent_object=torrent_object,  # For hash search
                )

                # Record scan result to database if search completed without errors
                if search_success:
                    await self.database.add_scan_result(
                        local_torrent_hash=torrent_details.hash,
                        site_host=api_instance.site_host,
                        local_torrent_name=torrent_details.name,
                        matched_torrent_id=matched_torrent_id,
                        matched_torrent_hash=matched_torrent_hash,
                    )

                if matched_torrent_hash is not None:
                    any_success = True
                    # Start tracking verification for the matched torrent
                    await self.torrent_client.track_verification(matched_torrent_hash)
                    logger.success("Successfully processed on %s", api_instance.server)

            except Exception as e:
                logger.error(
                    "Error processing torrent on %s: %s", api_instance.server, e
                )
                continue

        return any_success

    async def process_torrents(self):
        """Process torrents in client, supporting multiple target sites."""
        logger.section("===== Processing Torrents =====")

        # Extract target_trackers from target_apis
        target_trackers = [
            api_instance.tracker_query for api_instance in get_target_apis()
        ]

        # Reset stats for this processing session
        self.stats = ProcessorStats()

        try:
            # Get filtered torrent list
            torrents = await self.torrent_client.get_filtered_torrents(target_trackers)
            logger.debug(
                "Found %d torrents in client matching the criteria", len(torrents)
            )

            for i, (torrent_name, torrent_details) in enumerate(torrents.items()):
                logger.header(
                    "Processing %d/%d: %s (%s)",
                    i + 1,
                    len(torrents),
                    torrent_name,
                    torrent_details.hash,
                )

                # Process single torrent
                any_success = await self.process_single_torrent_from_client(
                    torrent_details=torrent_details,
                )

                # Record processed torrents (scan history handled inside scan function)
                if any_success:
                    logger.success("Torrent processed successfully")

        except Exception as e:
            logger.exception("Error processing torrents: %s", e)
        finally:
            # Send scan complete notification if configured
            if config.cfg.global_config.notification_urls and self.stats.scanned > 0:
                await get_notifier().send_scan_complete(self.stats)

            logger.success("Torrent processing summary:")
            logger.success("Torrents scanned: %d", self.stats.scanned)
            logger.success("Matches found: %d", self.stats.found)
            logger.success(".torrent files downloaded: %d", self.stats.downloaded)
            logger.section("===== Torrent Processing Complete =====")

    async def retry_undownloaded_torrents(self):
        """Re-download undownloaded torrents."""
        logger.section("===== Retrying Undownloaded Torrents =====")

        # Reset retry stats
        retry_stats = ProcessorStats()

        try:
            # Get all undownloaded torrents
            undownloaded_torrents = await self.database.load_undownloaded_torrents()

            if not undownloaded_torrents:
                logger.info("No undownloaded torrents found")
                return

            logger.info("Found %d undownloaded torrents", len(undownloaded_torrents))

            for undownloaded in undownloaded_torrents:
                retry_stats.attempted += 1
                total = len(undownloaded_torrents)
                torrent_id = str(undownloaded.matched_torrent_id)
                logger.header(
                    "Retrying torrent ID: %s (%s/%s)",
                    torrent_id,
                    retry_stats.attempted,
                    total,
                )

                try:
                    # Get API instance by site_host
                    api_instance = get_api_by_site_host(undownloaded.site_host)
                    if not api_instance:
                        logger.warning(
                            "Could not find API instance for site_host %s, "
                            "skipping torrent %s",
                            undownloaded.site_host,
                            torrent_id,
                        )
                        retry_stats.failed += 1
                        continue

                    logger.debug("Using API instance: %s", api_instance.server)

                    # Get local torrent information from client
                    local_torrent_info = await self.torrent_client.get_torrent_info(
                        undownloaded.local_torrent_hash,
                        fields=["name", "download_dir", "files"],
                    )
                    if not local_torrent_info:
                        logger.warning(
                            "Local torrent %s not found in client, "
                            "deleting from database",
                            undownloaded.local_torrent_hash,
                        )
                        await self.database.delete_scan_result(
                            undownloaded.local_torrent_hash, undownloaded.site_host
                        )
                        retry_stats.removed += 1
                        continue

                    # Download torrent data
                    matched_torrent = await api_instance.download_torrent(torrent_id)

                    # Try to inject torrent into client
                    success = await self.inject_matched_torrent(
                        matched_torrent, local_torrent_info, hash_match=False
                    )
                    if success:
                        retry_stats.successful += 1
                        retry_stats.removed += 1

                        # Injection successful, update scan_result with
                        # matched_torrent_hash
                        await self.database.mark_torrent_downloaded(
                            undownloaded.local_torrent_hash,
                            undownloaded.site_host,
                            matched_torrent.infohash,
                        )
                        logger.success(
                            "Successfully downloaded and injected torrent %s",
                            torrent_id,
                        )
                        logger.success("Updated scan result for torrent %s", torrent_id)
                    else:
                        retry_stats.failed += 1
                        logger.error("Failed to inject torrent %s", torrent_id)

                except Exception as e:
                    retry_stats.failed += 1
                    logger.error("Error processing torrent %s: %s", torrent_id, e)
                    continue

        except Exception as e:
            logger.exception("Error retrying undownloaded torrents: %s", e)
        finally:
            logger.success("Retry undownloaded torrents summary:")
            logger.success("Torrents attempted: %d", retry_stats.attempted)
            logger.success("Successfully downloaded: %d", retry_stats.successful)
            logger.success("Failed downloads: %d", retry_stats.failed)
            logger.success("Removed from undownloaded list: %d", retry_stats.removed)
            logger.section("===== Retry Undownloaded Torrents Complete =====")

    async def post_process_injected_torrents(self):
        """Post-process injected torrents to start downloading completed ones.

        This function checks previously found cross-seed matches in scan_results,
        verifies if local torrents are 100% complete, and starts downloading
        the matched torrents for cross-seeding. The matched torrents are
        already added to the client, we just need to start downloading them
        when the local torrents reach 100% completion.
        """
        logger.section("===== Post-Processing Injected Torrents =====")

        # Reset stats for injected torrents processing
        stats = PostProcessStats()

        try:
            # Get all matched scan results
            matched_results = await self.database.get_matched_scan_results()
            if not matched_results:
                logger.debug("No matched torrents found")
                return

            logger.info("Found %d matched torrents", len(matched_results))

            # Process all matched results
            for matched_torrent_hash in matched_results:
                stats.matches_checked += 1

                # Process single torrent
                result = await self.torrent_client.post_process_single_injected_torrent(
                    matched_torrent_hash
                )

                # Update stats based on result
                if result.status == PostProcessStatus.COMPLETED:
                    stats.matches_completed += 1
                    if result.started_downloading:
                        stats.matches_started_downloading += 1
                elif result.status == PostProcessStatus.PARTIAL_KEPT:
                    # Partial torrent kept, no action needed
                    pass
                elif result.status in (
                    PostProcessStatus.PARTIAL_REMOVED,
                    PostProcessStatus.ERROR,
                ):
                    stats.matches_failed += 1
                # For "not_found" and "checking" status, no stats update needed

        except Exception as e:
            logger.exception("Error processing injected torrents: %s", e)
        finally:
            logger.success("Injected torrents post-processing summary:")
            logger.success("Matches checked: %d", stats.matches_checked)
            logger.success("Matches completed: %d", stats.matches_completed)
            logger.success(
                "Matches started downloading: %d", stats.matches_started_downloading
            )
            logger.success("Matches failed: %d", stats.matches_failed)
            logger.section("===== Injected Torrents Post-Processing Complete =====")

    async def process_single_torrent(
        self,
        infohash: str,
    ) -> ProcessResponse:
        """Process a single torrent by infohash from torrent client.

        Args:
            infohash (str): Infohash of the torrent to process.

        Returns:
            ProcessResponse: Processing result with status and details.
        """

        try:
            # Extract target_trackers from target_apis
            target_trackers = {
                api_instance.tracker_query for api_instance in get_target_apis()
            }

            # Get torrent details from torrent client with existing trackers info
            torrent_info = await self.torrent_client.get_single_torrent(
                infohash, target_trackers
            )

            if not torrent_info:
                return ProcessResponse(
                    status=ProcessStatus.ERROR,
                    message=(
                        f"Torrent with infohash {infohash} not found in client "
                        f"or was filtered out"
                    ),
                )

            if target_trackers.issubset(torrent_info.existing_target_trackers):
                return ProcessResponse(
                    status=ProcessStatus.SKIPPED,
                    message=(
                        f"Torrent already exists on all target trackers: "
                        f"{torrent_info.existing_target_trackers}"
                    ),
                )

            # Process the torrent using the same logic as
            # process_single_torrent_from_client.
            # Skip the scanned check for webhook calls
            any_success = await self.process_single_torrent_from_client(
                torrent_details=torrent_info,
                skip_scanned_check=True,
            )

            if any_success:
                return ProcessResponse(
                    status=ProcessStatus.SUCCESS,
                    message=(
                        f"Successfully processed torrent: {torrent_info.name} "
                        f"({infohash})"
                    ),
                )
            else:
                return ProcessResponse(
                    status=ProcessStatus.NOT_FOUND,
                    message=(
                        f"No matching torrents found for: {torrent_info.name} "
                        f"({infohash})"
                    ),
                )

        except Exception as e:
            logger.error("Error processing single torrent %s: %s", infohash, str(e))
            return ProcessResponse(
                status=ProcessStatus.ERROR,
                message=f"Error processing torrent: {str(e)}",
            )

    async def _get_torrent_api_and_info(
        self,
        torrent_link: str,
        tid: str,
    ) -> (
        tuple["GazelleJSONAPI | GazelleParser | GazelleGamesNet", dict[str, int]]
        | ProcessResponse
    ):
        """Get torrent API instance and torrent info from link.

        Args:
            torrent_link: Torrent link containing site information.
            tid: Torrent ID to fetch info for.

        Returns:
            Tuple of (torrent_api, fdict_torrent) on success,
            or ProcessResponse on failure.
        """
        parsed_link = urlparse(torrent_link)
        site_host = parsed_link.hostname
        if not site_host:
            return ProcessResponse(
                status=ProcessStatus.ERROR,
                message=f"Invalid torrent link (missing hostname): {torrent_link}",
            )
        torrent_api = get_api_by_site_host(site_host)
        if not torrent_api:
            return ProcessResponse(
                status=ProcessStatus.ERROR,
                message=f"Could not find API instance for site: {site_host}",
            )

        try:
            torrent_info = await torrent_api.torrent(tid)
            if not torrent_info:
                return ProcessResponse(
                    status=ProcessStatus.ERROR,
                    message=f"Failed to get torrent info for ID: {tid}",
                )
        except Exception as e:
            logger.error("Failed to get torrent info: %s", e)
            return ProcessResponse(
                status=ProcessStatus.ERROR,
                message=f"Failed to get torrent info: {str(e)}",
            )

        fdict_torrent = torrent_info.get("fileList", {})
        return torrent_api, fdict_torrent

    async def _find_reverse_announce_match(
        self,
        torrent_name: str,
        fdict_torrent: dict[str, int],
        torrent_api: "GazelleJSONAPI | GazelleParser | GazelleGamesNet",
        tid: str,
    ) -> ClientTorrentInfo | ProcessResponse:
        """Find matching torrent for reverse announce and validate against trump.

        Args:
            torrent_name: Name of the torrent for logging.
            fdict_torrent: File dictionary from the incoming torrent.
            torrent_api: API instance for the incoming torrent.
            tid: Torrent ID for logging.

        Returns:
            ClientTorrentInfo on success, or ProcessResponse on failure.
        """
        # Search for matching torrents
        matched_torrents = await self._search_torrent_by_filename_in_client(
            fdict_torrent
        )

        if not matched_torrents:
            return ProcessResponse(
                status=ProcessStatus.NOT_FOUND,
                message=f"No matching torrent found in client for: {torrent_name}",
            )

        # Check if incoming torrent may trump local torrent
        for matched_torrent in matched_torrents:
            matched_api = get_api_by_tracker(matched_torrent.trackers)
            if matched_api is not None and matched_api == torrent_api:
                logger.warning(
                    "Incoming torrent %s may trump local torrent %s, "
                    "skipping processing",
                    tid,
                    matched_torrent.hash,
                )
                return ProcessResponse(
                    status=ProcessStatus.SKIPPED_POTENTIAL_TRUMP,
                    message=(
                        f"Local torrent {matched_torrent.hash} may be trumped, "
                        f"skipping processing"
                    ),
                )

        # Select the torrent with the most files
        matched_torrent = max(matched_torrents, key=lambda x: len(x.files))
        logger.success("Found matching torrent in client: %s", matched_torrent.name)
        return matched_torrent

    async def process_reverse_announce_torrent(
        self,
        torrent_name: str,
        torrent_link: str,
        album_name: str,
    ) -> ProcessResponse:
        """Process a single announce torrent for cross-seeding.

        Args:
            torrent_name (str): Name of the torrent.
            torrent_link (str): Torrent link containing the torrent ID.
            album_name (str): Album name for searching.

        Returns:
            ProcessResponse: Processing result with status and details.
        """
        try:
            # Extract torrent ID from torrent_link
            parsed_link = urlparse(torrent_link)
            query_params = parse_qs(parsed_link.query)
            if "id" not in query_params or not query_params["id"]:
                raise ValueError(
                    f"Missing 'id' parameter in torrent link: {torrent_link}"
                )
            tid = query_params["id"][0]

            logger.debug("Extracted torrent ID: %s from link: %s", tid, torrent_link)

            # Validate album keywords
            album_keywords = make_search_query(album_name).split()
            logger.debug(
                "Searching for album: %s with keywords: %s", album_name, album_keywords
            )

            if len(album_keywords) == 0:
                logger.debug(
                    "No valid album keywords extracted for album: %s", album_name
                )
                return ProcessResponse(
                    status=ProcessStatus.NOT_FOUND,
                    message=(
                        f"No valid album keywords extracted for album: {album_name}"
                    ),
                )

            # Refresh client torrent cache and search
            await self.torrent_client.refresh_client_torrents_cache()
            has_album_match = await self.database.search_torrent_by_album_name(
                album_keywords
            )
            if not has_album_match:
                return ProcessResponse(
                    status=ProcessStatus.NOT_FOUND,
                    message=(
                        f"No matching torrent found in client for album: {album_name}"
                    ),
                )

            # Get API instance and torrent info
            api_result = await self._get_torrent_api_and_info(torrent_link, tid)
            if isinstance(api_result, ProcessResponse):
                return api_result
            torrent_api, fdict_torrent = api_result

            # Find matching torrent with trump validation
            match_result = await self._find_reverse_announce_match(
                torrent_name, fdict_torrent, torrent_api, tid
            )
            if isinstance(match_result, ProcessResponse):
                return match_result
            matched_torrent = match_result

            # Download torrent data from API (only after finding a match)
            try:
                torrent_object = await torrent_api.download_torrent(tid)
            except Exception as e:
                logger.error("Failed to download torrent data: %s", e)
                return ProcessResponse(
                    status=ProcessStatus.ERROR,
                    message=f"Failed to download torrent data: {str(e)}",
                )

            # Use client's file dictionary
            rename_map = generate_rename_map(matched_torrent.fdict, fdict_torrent)

            # Handle file linking and prepare download directory
            final_download_dir = await self._prepare_linked_download_dir(
                torrent_object,
                matched_torrent.fdict,
                fdict_torrent,
                matched_torrent.download_dir,
                matched_torrent.name,
            )

            success, _ = await self.torrent_client.inject_torrent(
                torrent_object,
                final_download_dir,
                matched_torrent.name,
                rename_map,
                False,
                matched_torrent.hash,
            )
            if success:
                logger.success("Torrent injected successfully")
                await self.torrent_client.track_verification(torrent_object.infohash)

                # Send announce success notification if configured
                if config.cfg.global_config.notification_urls:
                    await get_notifier().send_announce_success(
                        torrent_name=torrent_name,
                        torrent_url=torrent_link,
                    )

                return ProcessResponse(
                    status=ProcessStatus.SUCCESS,
                    message=(
                        f"Successfully processed reverse announce torrent: "
                        f"{torrent_name}"
                    ),
                )
            else:
                logger.error("Failed to inject torrent: %s", tid)
                return ProcessResponse(
                    status=ProcessStatus.ERROR,
                    message=f"Failed to inject torrent: {tid}",
                )

        except Exception as e:
            logger.error(
                "Error processing reverse announce torrent %s: %s", torrent_name, str(e)
            )
            return ProcessResponse(
                status=ProcessStatus.ERROR,
                message=f"Error processing torrent: {str(e)}",
            )


# Global core instance
_core_instance: NemorosaCore | None = None
_core_lock = anyio.Lock()


async def init_core() -> None:
    """Initialize global core instance.

    Should be called once during application startup.

    Raises:
        RuntimeError: If already initialized.
    """
    global _core_instance
    async with _core_lock:
        if _core_instance is not None:
            raise RuntimeError("Core already initialized.")

        _core_instance = NemorosaCore()


def get_core() -> NemorosaCore:
    """Get global core instance.

    Must be called after init_core() has been invoked.

    Returns:
        NemorosaCore: Core instance.

    Raises:
        RuntimeError: If core has not been initialized.
    """
    if _core_instance is None:
        raise RuntimeError("Core not initialized. Call init_core() first.")
    return _core_instance
