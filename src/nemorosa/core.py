"""Core processing functions for nemorosa."""

import asyncio
import posixpath
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import msgspec
from pydantic import BaseModel, Field
from torf import Torrent

from . import config, logger
from .api import get_api_by_site_host, get_api_by_tracker, get_target_apis
from .client_instance import get_torrent_client
from .clients import ClientTorrentInfo, TorrentConflictError
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

if TYPE_CHECKING:
    from .api import GazelleGamesNet, GazelleJSONAPI, GazelleParser

# Constants
MAX_SEARCH_RESULTS = 20


class ProcessStatus(Enum):
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
    matches_already_downloading: int = 0
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
                    "message": "Torrent already exists on all target trackers: [tracker1, tracker2]",
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
            tuple[int | None, Torrent | None]: Torrent ID and matched torrent if found, (None, None) otherwise.
        """
        torrent_copy = Torrent.copy(torrent_object)

        # Get target source flag from API
        target_source_flag = api.source_flag

        # For all trackers, try source flag variations
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
                logger.debug(f"Trying hash search with source flag '{flag}', hash: {torrent_hash}")

                # Search torrent by hash
                search_result = await api.search_torrent_by_hash(torrent_hash)
                if search_result:
                    logger.debug(f"Hash search returned result: status={search_result.get('status')}")
                    logger.debug(f"Response structure: {list(search_result.get('response', {}).keys())}")
                    
                    # Get torrent ID from search result
                    torrent_data = search_result.get("response", {}).get("torrent")
                    if torrent_data:
                        torrent_id = torrent_data.get("id")
                        logger.debug(f"Extracted torrent ID: {torrent_id}")
                        if torrent_id:
                            tid = int(torrent_id)
                            logger.success(f"Found torrent by hash! Hash: {torrent_hash}, Torrent ID: {tid}")
                            torrent_copy.comment = api.get_torrent_url(tid)
                            torrent_copy.trackers = [api.announce]
                            return tid, torrent_copy
                        else:
                            logger.warning("Hash search found result but torrent ID is missing or None")
                    else:
                        response_keys = list(search_result.get("response", {}).keys())
                        logger.warning(
                            f"Hash search found result but 'response.torrent' is missing. "
                            f"Response keys: {response_keys}"
                        )
                else:
                    logger.debug(f"Hash search returned None for hash {torrent_hash} with source flag '{flag}'")
            except Exception as e:
                logger.exception(f"Hash search failed for source '{flag}': {e}")

        return None, None

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
            logger.debug(f"Searching for file: {fname}")
            fname_query = fname
            try:
                torrents = await api.search_torrent_by_filename(fname_query)
            except Exception as e:
                logger.error(f"Error searching for file '{fname_query}': {e}")
                raise

            # Record the number of results found
            logger.debug(f"Found {len(torrents)} potential matches for file '{fname_query}'")

            # If no results found and it's a music file, try make search query and search again
            if len(torrents) == 0 and is_music_file(fname):
                fname_query = make_search_query(posixpath.basename(fname))
                if fname_query != fname:
                    logger.debug(
                        f"No results found for '{fname}', trying fallback search with basename: '{fname_query}'"
                    )
                    try:
                        fallback_torrents = await api.search_torrent_by_filename(fname_query)
                        if fallback_torrents:
                            torrents = fallback_torrents
                            logger.debug(f"Fallback search found {len(torrents)} potential matches for '{fname_query}'")
                        else:
                            logger.debug(f"Fallback search also found no results for '{fname_query}'")
                    except Exception as e:
                        logger.error(f"Error in fallback search for file basename '{fname_query}': {e}")
                        raise

            # Match by total size
            size_match_found = False
            for t in torrents:
                if tsize == t["size"]:
                    tid = t["torrentId"]
                    size_match_found = True
                    logger.success(f"Size match found! Torrent ID: {tid} (Size: {tsize})")
                    break

            if size_match_found:
                break

            # Handle cases with too many results
            if len(torrents) > MAX_SEARCH_RESULTS:
                logger.warning(f"Too many results found for file '{fname_query}' ({len(torrents)}). Skipping.")
                continue

            # Match by file content
            if tid is None:
                logger.debug(f"No size match found. Checking file contents for '{fname_query}'")
                tid = await self.match_by_file_content(
                    torrents=torrents,
                    fname=fname,
                    fdict=fdict,
                    scan_querys=scan_querys,
                    api=api,
                )

            # If match found, exit early
            if tid is not None:
                logger.debug(f"Match found with file '{fname}'. Stopping search.")
                break

            logger.debug(f"No more results for file '{fname}'")
            if is_music_file(fname):
                logger.debug("Stopping search as music file match is not found")
                break

        if tid is None:
            return tid, None

        # Download torrent data and create torrent object
        try:
            matched_torrent = await api.download_torrent(tid)
            return tid, matched_torrent
        except Exception as e:
            logger.error(f"Failed to download torrent data for torrent ID: {tid}: {e}")
            return tid, None

    async def _search_torrent_by_filename_in_client(self, torrent_fdict: dict[str, int]) -> list[ClientTorrentInfo]:
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

            logger.debug(f"Searching with {len(scan_queries)} file queries: {scan_queries}")

            for fname in scan_queries:
                logger.debug(f"Searching for file: {fname}")

                # Get the file size to match
                target_file_size = torrent_fdict[fname]

                # Use make_search_query to process filename
                fname_query = make_search_query(posixpath.basename(fname))
                if not fname_query:
                    continue

                fname_query_words = fname_query.split()

                # Get matching torrents from client's database cache
                candidate_torrents = await self.torrent_client.get_file_matched_torrents(
                    target_file_size=target_file_size, fname_keywords=fname_query_words
                )

                logger.debug(f"Found {len(candidate_torrents)} candidate torrents")

                # Verify each candidate for conflicts
                for candidate in candidate_torrents:
                    logger.debug(f"Verifying candidate torrent: {candidate.name}")

                    # Database query already ensured size and name match, only check conflicts
                    if config.cfg.linking.enable_linking or not check_conflicts(candidate.fdict, torrent_fdict):
                        logger.success(f"Complete torrent match found: {candidate.name}")
                        matched_torrents.append(candidate)
                    else:
                        logger.debug(f"Match found but has conflicts: {candidate.name}")

                # If matching torrent found, can return early
                if matched_torrents:
                    break

                # If music file and no match found, stop searching
                if is_music_file(fname):
                    logger.debug("Stopping search as music file match is not found")
                    break

            return matched_torrents

        except Exception as e:
            logger.error(f"Error searching torrent by filename in client: {e}")
            return []

    async def match_by_file_content(
        self,
        *,
        torrents: list[dict],
        fname: str,
        fdict: dict,
        scan_querys: list[str],
        api: "GazelleJSONAPI | GazelleParser | GazelleGamesNet",
    ) -> int | None:
        """Match torrents by file content.

        Args:
            torrents (list[dict]): List of torrents to check.
            fname (str): Original filename.
            fdict (dict): File dictionary mapping filename to size.
            scan_querys (list[str]): List of scan queries.
            api: API instance for the target site.

        Returns:
            int | None: Torrent ID if found, None otherwise.
        """
        for t_index, t in enumerate(torrents, 1):
            logger.debug(f"Checking torrent #{t_index}/{len(torrents)}: ID {t['torrentId']}")

            try:
                resp = await api.torrent(t["torrentId"])
                resp_files = resp.get("fileList", {})
            except Exception as e:
                torrent_id = t["torrentId"]
                logger.exception(
                    f"Failed to get torrent data for ID {torrent_id}: {e}. "
                    f"Continuing with next torrent."
                )
                continue

            check_music_file = fname if is_music_file(fname) else scan_querys[-1]

            # For music files, byte-level size comparison is sufficient for identical matching
            # as it provides reliable file identification without requiring full content comparison
            if fdict[check_music_file] in resp_files.values():
                # Check file conflicts
                if config.cfg.linking.enable_linking or not check_conflicts(fdict, resp_files):
                    logger.success(f"File match found! Torrent ID: {t['torrentId']} (File: {check_music_file})")
                    return t["torrentId"]
                else:
                    logger.debug("Conflict detected. Skipping this torrent.")
                    return None

        return None

    def inject_matched_torrent(
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
        if config.cfg.linking.enable_linking:
            # Generate link map for file linking
            file_mapping = generate_link_map(local_torrent_info.fdict, matched_fdict)
            # File linking mode: create links first, then add torrent with linked directory
            final_download_dir = create_file_links_for_torrent(
                matched_torrent, local_torrent_info.download_dir, local_torrent_info.name, file_mapping
            )
            if final_download_dir is None:
                logger.error("Failed to create file links, falling back to original directory")
                final_download_dir = local_torrent_info.download_dir
        else:
            # Normal mode: generate rename map for file renaming
            final_download_dir = local_torrent_info.download_dir

        logger.debug(f"Attempting to inject torrent: {local_torrent_info.name}")
        logger.debug(f"Download directory: {final_download_dir}")
        logger.debug(f"Rename map: {rename_map}")

        # Inject torrent and handle renaming
        success, _ = self.torrent_client.inject_torrent(
            matched_torrent, final_download_dir, local_torrent_info.name, rename_map, hash_match
        )
        return success

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
            tuple[bool, str | None, str | None]: (search_success, matched_torrent_id, matched_torrent_hash).
                - search_success: True if search completed without errors, False if errors occurred.
                - matched_torrent_id: Torrent ID if found, None otherwise.
                - matched_torrent_hash: Torrent hash if successfully injected, None otherwise.
        """
        self.stats.scanned += 1

        tid = None
        matched_torrent = None
        hash_match = True
        search_success = True  # Track if search completed without errors

        # Try hash-based search first if torrent object is available
        if torrent_object:
            try:
                logger.debug("Trying hash-based search first")
                tid, matched_torrent = await self.hash_based_search(torrent_object=torrent_object, api=api)
            except Exception as e:
                logger.error(f"Hash-based search failed: {e}")
                search_success = False

        # If hash search didn't find anything, try filename search
        if tid is None:
            try:
                logger.debug("No torrent found by hash, falling back to filename search")
                tid, matched_torrent = await self.filename_search(
                    fdict=torrent_details.fdict, tsize=torrent_details.total_size, api=api
                )
                hash_match = False
            except Exception as e:
                logger.error(f"Filename search failed: {e}")
                search_success = False

        # Handle no match found case
        if tid is None:
            logger.header("No matching torrent found")
            return search_success, None, None

        # Found a match
        self.stats.found += 1
        logger.success(f"Found match! Torrent ID: {tid}")

        # Inject torrent and handle renaming
        if config.cfg.global_config.no_download:
            return search_success, str(tid), None

        matched_torrent_hash = None
        injection_success = False

        # Check if matched_torrent is None (download failed)
        if matched_torrent is None:
            logger.error(f"Failed to inject torrent: {tid} (matched_torrent is None)")
        else:
            # Try to inject torrent
            try:
                injection_success = self.inject_matched_torrent(matched_torrent, torrent_details, hash_match)
                if injection_success:
                    self.stats.downloaded += 1
                    matched_torrent_hash = matched_torrent.infohash
                    logger.success("Torrent injected successfully")
                else:
                    logger.error(f"Failed to inject torrent: {tid}")
            except TorrentConflictError as e:
                # Torrent conflict - treat as no match found
                logger.debug(f"Torrent conflict detected: {e}")
                return search_success, None, None
            except Exception as e:
                # Other errors during injection - skip this torrent
                logger.exception(f"Error during torrent injection: {e}")

        # Log download/injection failure
        if not injection_success:
            self.stats.cnt_dl_fail += 1
            if self.stats.cnt_dl_fail <= 10:
                logger.error(
                    f"It might because the torrent id {tid} has reached the "
                    f"limitation of non-browser downloading of {api.server}. "
                    f"The failed download info will be saved to database. "
                    "You can download it from your own browser."
                )
                if self.stats.cnt_dl_fail == 10:
                    logger.debug("Suppressing further hinting for .torrent file downloading failures")

        return search_success, str(tid), matched_torrent_hash

    async def process_single_torrent_from_client(
        self,
        torrent_details: ClientTorrentInfo,
        skip_scanned_check: bool = False,
    ) -> bool:
        """Process a single torrent from client torrent list.

        Args:
            torrent_details (ClientTorrentInfo): Torrent details from client.
            skip_scanned_check (bool): If True, skip the already-scanned check (for webhooks).

        Returns:
            bool: True if any target site was successful, False otherwise.
        """

        # Try to get torrent data from torrent client for hash search
        torrent_object = self.torrent_client.get_torrent_object(torrent_details.hash)

        # Scan and match for each target site
        any_success = False

        for api_instance in get_target_apis():
            # Check if torrent has been scanned on this specific site (unless skip_scanned_check is True)
            if not skip_scanned_check and await self.database.is_hash_scanned(
                local_torrent_hash=torrent_details.hash, site_host=api_instance.site_host
            ):
                logger.debug(
                    "Skipping already scanned torrent on %s: %s (%s)",
                    api_instance.site_host,
                    torrent_details.name,
                    torrent_details.hash,
                )
                continue
            logger.debug(f"Trying target site: {api_instance.server} (tracker: {api_instance.tracker_query})")

            # Check if this content already exists on current target tracker
            if api_instance.tracker_query in torrent_details.existing_target_trackers:
                logger.debug(f"Content already exists on {api_instance.tracker_query}, skipping")
                continue

            try:
                # Scan and match
                search_success, matched_torrent_id, matched_torrent_hash = await self.process_torrent_search(
                    torrent_details=torrent_details,
                    api=api_instance,
                    torrent_object=torrent_object,  # Pass torrent object for hash search
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
                    logger.success(f"Successfully processed on {api_instance.server}")

            except Exception as e:
                logger.error(f"Error processing torrent on {api_instance.server}: {e}")
                continue

        return any_success

    async def process_torrents(self):
        """Process torrents in client, supporting multiple target sites."""
        logger.section("===== Processing Torrents =====")

        # Extract target_trackers from target_apis
        target_trackers = [api_instance.tracker_query for api_instance in get_target_apis()]

        # Reset stats for this processing session
        self.stats = ProcessorStats()

        try:
            # Get filtered torrent list
            torrents = await self.torrent_client.get_filtered_torrents(target_trackers)
            logger.debug("Found %d torrents in client matching the criteria", len(torrents))

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

            logger.info(f"Found {len(undownloaded_torrents)} undownloaded torrents")

            for undownloaded in undownloaded_torrents:
                retry_stats.attempted += 1
                total = len(undownloaded_torrents)
                torrent_id = str(undownloaded.matched_torrent_id)
                logger.header(f"Retrying torrent ID: {torrent_id} ({retry_stats.attempted}/{total})")

                try:
                    # Get API instance by site_host
                    api_instance = get_api_by_site_host(undownloaded.site_host)
                    if not api_instance:
                        logger.warning(
                            f"Could not find API instance for site_host {undownloaded.site_host}, "
                            f"skipping torrent {torrent_id}"
                        )
                        retry_stats.failed += 1
                        continue

                    logger.debug(f"Using API instance: {api_instance.server}")

                    # Get local torrent information from client
                    local_torrent_info = self.torrent_client.get_torrent_info(
                        undownloaded.local_torrent_hash, fields=["name", "download_dir", "files"]
                    )
                    if not local_torrent_info:
                        logger.warning(
                            f"Local torrent {undownloaded.local_torrent_hash} not found in client, "
                            f"deleting from database"
                        )
                        await self.database.delete_scan_result(undownloaded.local_torrent_hash, undownloaded.site_host)
                        retry_stats.removed += 1
                        continue

                    # Download torrent data
                    matched_torrent = await api_instance.download_torrent(torrent_id)

                    # Try to inject torrent into client
                    success = self.inject_matched_torrent(matched_torrent, local_torrent_info, hash_match=False)
                    if success:
                        retry_stats.successful += 1
                        retry_stats.removed += 1

                        # Injection successful, update scan_result with matched_torrent_hash
                        await self.database.mark_torrent_downloaded(
                            undownloaded.local_torrent_hash, undownloaded.site_host, matched_torrent.infohash
                        )
                        logger.success(f"Successfully downloaded and injected torrent {torrent_id}")
                        logger.success(f"Updated scan result for torrent {torrent_id}")
                    else:
                        retry_stats.failed += 1
                        logger.error(f"Failed to inject torrent {torrent_id}")

                except Exception as e:
                    retry_stats.failed += 1
                    logger.error(f"Error processing torrent {torrent_id}: {e}")
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
        """Post-process previously injected torrents to start downloading completed torrents.

        This function checks previously found cross-seed matches in scan_results,
        verifies if local torrents are 100% complete, and starts downloading the matched
        torrents for cross-seeding. The matched torrents are already added to the client,
        we just need to start downloading them when the local torrents reach 100% completion.
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

            logger.info(f"Found {len(matched_results)} matched torrents")

            # Process all matched results
            for matched_torrent_hash in matched_results:
                stats.matches_checked += 1

                # Process single torrent
                result = await self.torrent_client.post_process_single_injected_torrent(matched_torrent_hash)

                # Update stats based on result
                if result.status == "completed":
                    stats.matches_completed += 1
                    if result.started_downloading:
                        stats.matches_started_downloading += 1
                elif result.status == "partial_kept":
                    # Partial torrent kept, no action needed
                    pass
                elif result.status in ("partial_removed", "error"):
                    stats.matches_failed += 1
                # For "not_found" and "checking" status, no stats update needed

        except Exception as e:
            logger.exception("Error processing injected torrents: %s", e)
        finally:
            logger.success("Injected torrents post-processing summary:")
            logger.success("Matches checked: %d", stats.matches_checked)
            logger.success("Matches completed: %d", stats.matches_completed)
            logger.success("Matches started downloading: %d", stats.matches_started_downloading)
            logger.success("Matches already downloading: %d", stats.matches_already_downloading)
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
            target_trackers = {api_instance.tracker_query for api_instance in get_target_apis()}

            # Get torrent details from torrent client with existing trackers info
            torrent_info = self.torrent_client.get_single_torrent(infohash, target_trackers)

            if not torrent_info:
                return ProcessResponse(
                    status=ProcessStatus.ERROR,
                    message=f"Torrent with infohash {infohash} not found in client or was filtered out",
                )

            if target_trackers.issubset(torrent_info.existing_target_trackers):
                return ProcessResponse(
                    status=ProcessStatus.SKIPPED,
                    message=f"Torrent already exists on all target trackers: {torrent_info.existing_target_trackers}",
                )

            # Process the torrent using the same logic as process_single_torrent_from_client
            # Skip the scanned check for webhook calls
            any_success = await self.process_single_torrent_from_client(
                torrent_details=torrent_info,
                skip_scanned_check=True,
            )

            if any_success:
                return ProcessResponse(
                    status=ProcessStatus.SUCCESS,
                    message=f"Successfully processed torrent: {torrent_info.name} ({infohash})",
                )
            else:
                return ProcessResponse(
                    status=ProcessStatus.NOT_FOUND,
                    message=f"No matching torrents found for: {torrent_info.name} ({infohash})",
                )

        except Exception as e:
            logger.error(f"Error processing single torrent {infohash}: {str(e)}")
            return ProcessResponse(status=ProcessStatus.ERROR, message=f"Error processing torrent: {str(e)}")

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
                raise ValueError(f"Missing 'id' parameter in torrent link: {torrent_link}")
            tid = query_params["id"][0]

            logger.debug(f"Extracted torrent ID: {tid} from link: {torrent_link}")

            # First, try to search by album name
            album_keywords = make_search_query(album_name).split()
            logger.debug(f"Searching for album: {album_name} with keywords: {album_keywords}")

            # Validate album keywords to avoid unfiltered query
            if len(album_keywords) == 0:
                logger.debug(f"No valid album keywords extracted for album: {album_name}")
                return ProcessResponse(
                    status=ProcessStatus.NOT_FOUND,
                    message=f"No valid album keywords extracted for album: {album_name}",
                )

            # Refresh client torrent cache
            await self.torrent_client.refresh_client_torrents_cache()

            # Search in database by album name
            has_album_match = await self.database.search_torrent_by_album_name(album_keywords)
            if not has_album_match:
                return ProcessResponse(
                    status=ProcessStatus.NOT_FOUND,
                    message=f"No matching torrent found in client for album: {album_name}",
                )

            # Get API instance from torrent link
            site_host = str(parsed_link.hostname)
            torrent_api = get_api_by_site_host(site_host)

            if not torrent_api:
                return ProcessResponse(
                    status=ProcessStatus.ERROR,
                    message=f"Could not find API instance for site: {site_host}",
                )

            # Get torrent info from API to extract file list
            try:
                torrent_info = await torrent_api.torrent(tid)
                if not torrent_info:
                    return ProcessResponse(
                        status=ProcessStatus.ERROR,
                        message=f"Failed to get torrent info for ID: {tid}",
                    )
            except Exception as e:
                logger.error(f"Failed to get torrent info: {e}")
                return ProcessResponse(
                    status=ProcessStatus.ERROR,
                    message=f"Failed to get torrent info: {str(e)}",
                )

            # Extract file list from torrent info
            fdict_torrent = torrent_info.get("fileList", {})

            # Search for matching torrents using database (no need to load all torrents)
            matched_torrents = await self._search_torrent_by_filename_in_client(fdict_torrent)

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
                        f"Incoming torrent {tid} may trump local torrent {matched_torrent.hash}, skipping processing"
                    )
                    return ProcessResponse(
                        status=ProcessStatus.SKIPPED_POTENTIAL_TRUMP,
                        message=f"Local torrent {matched_torrent.hash} may be trumped, skipping processing",
                    )

            # Select the torrent with the most files (only after checking all don't conflict)
            matched_torrent = max(matched_torrents, key=lambda x: len(x.files))
            logger.success(f"Found matching torrent in client: {matched_torrent.name}")

            # Download torrent data from API (only after finding a match)
            try:
                torrent_object = await torrent_api.download_torrent(tid)
            except Exception as e:
                logger.error(f"Failed to download torrent data: {e}")
                return ProcessResponse(
                    status=ProcessStatus.ERROR,
                    message=f"Failed to download torrent data: {str(e)}",
                )

            # Use client's file dictionary
            rename_map = generate_rename_map(matched_torrent.fdict, fdict_torrent)

            # Handle file linking and rename map based on configuration
            if config.cfg.linking.enable_linking:
                # Generate link map for file linking
                file_mapping = generate_link_map(matched_torrent.fdict, fdict_torrent)
                # File linking mode: create links first, then add torrent with linked directory
                final_download_dir = create_file_links_for_torrent(
                    torrent_object, matched_torrent.download_dir, matched_torrent.name, file_mapping
                )
                if final_download_dir is None:
                    logger.error("Failed to create file links, falling back to original directory")
                    final_download_dir = matched_torrent.download_dir
            else:
                # Normal mode: use original download directory
                final_download_dir = matched_torrent.download_dir

            success, _ = self.torrent_client.inject_torrent(
                torrent_object, final_download_dir, matched_torrent.name, rename_map, False
            )
            if success:
                logger.success("Torrent injected successfully")
                await self.torrent_client.track_verification(torrent_object.infohash)
                return ProcessResponse(
                    status=ProcessStatus.SUCCESS,
                    message=f"Successfully processed reverse announce torrent: {torrent_name}",
                )
            else:
                logger.error(f"Failed to inject torrent: {tid}")
                return ProcessResponse(
                    status=ProcessStatus.ERROR,
                    message=f"Failed to inject torrent: {tid}",
                )

        except Exception as e:
            logger.error(f"Error processing reverse announce torrent {torrent_name}: {str(e)}")
            return ProcessResponse(status=ProcessStatus.ERROR, message=f"Error processing torrent: {str(e)}")


# Global core instance
_core_instance: NemorosaCore | None = None
_core_lock = asyncio.Lock()


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
