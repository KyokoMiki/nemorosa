"""Core processing orchestrator for nemorosa."""

import posixpath
from typing import TYPE_CHECKING

from torf import Torrent

from .. import config, logger
from ..clients import (
    ClientTorrentInfo,
    PostProcessStatus,
    TorrentConflictError,
)
from ..filecompare import (
    check_conflicts,
    generate_rename_map,
    is_music_file,
    make_search_query,
    select_search_filenames,
)
from ..trackers import find_api_by_site_host, find_api_by_tracker
from .injector import TorrentInjector
from .models import (
    PostProcessStats,
    ProcessorStats,
    ProcessResponse,
    ProcessStatus,
)
from .searcher import TorrentSearcher
from .utils import (
    extract_album_keywords,
    parse_site_host_from_link,
    parse_torrent_id_from_link,
    should_skip_target_site,
)

if TYPE_CHECKING:
    from ..clients import TorrentClient
    from ..db import NemorosaDatabase
    from ..notifier import Notifier
    from ..trackers import GazelleAPI


class NemorosaCore:
    """Orchestrator for torrent processing and cross-seeding operations."""

    def __init__(
        self,
        torrent_client: "TorrentClient",
        database: "NemorosaDatabase",
        searcher: TorrentSearcher,
        injector: TorrentInjector,
        target_apis: "list[GazelleAPI]",
        notifier: "Notifier | None" = None,
    ) -> None:
        """Initialize the torrent processor.

        Args:
            torrent_client: TorrentClient instance for client operations.
            database: NemorosaDatabase instance for persistence.
            searcher: TorrentSearcher instance for search operations.
            injector: TorrentInjector instance for injection operations.
            target_apis: List of initialized API instances for target sites.
            notifier: Optional Notifier instance for push notifications.
        """
        self.torrent_client = torrent_client
        self.database = database
        self.searcher = searcher
        self.injector = injector
        self.target_apis = target_apis
        self.notifier = notifier
        self.stats = ProcessorStats()

    async def _search_torrent_by_filename_in_client(
        self, torrent_fdict: dict[str, int]
    ) -> list[ClientTorrentInfo]:
        """Search for matching torrents in client by filename using database.

        Args:
            torrent_fdict: File dictionary of the incoming torrent.

        Returns:
            List of matching ClientTorrentInfo objects.
        """
        try:
            matched_torrents: list[ClientTorrentInfo] = []
            scan_queries = select_search_filenames(torrent_fdict.keys())

            logger.debug(
                "Searching with %s file queries: %s", len(scan_queries), scan_queries
            )

            for fname in scan_queries:
                logger.debug("Searching for file: %s", fname)
                target_file_size = torrent_fdict[fname]

                fname_query = make_search_query(posixpath.basename(fname))
                if not fname_query:
                    continue

                fname_query_words = fname_query.split()

                candidate_torrents = (
                    await self.torrent_client.get_file_matched_torrents(
                        target_file_size=target_file_size,
                        fname_keywords=fname_query_words,
                    )
                )

                logger.debug("Found %d candidate torrents", len(candidate_torrents))

                for candidate in candidate_torrents:
                    logger.debug("Verifying candidate torrent: %s", candidate.name)
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

                if matched_torrents:
                    break

                if is_music_file(fname):
                    logger.debug("Stopping search as music file match is not found")
                    break

            return matched_torrents

        except Exception as e:
            logger.error("Error searching torrent by filename in client: %s", e)
            return []

    async def process_torrent_search(
        self,
        *,
        torrent_details: ClientTorrentInfo,
        api: "GazelleAPI",
        torrent_object: Torrent | None = None,
    ) -> tuple[bool, str | None, str | None]:
        """Process torrent search and injection.

        Args:
            torrent_details: Torrent details from client.
            api: API instance for the target site.
            torrent_object: Original torrent object for hash search.

        Returns:
            Tuple of (search_success, matched_torrent_id, matched_torrent_hash).
        """
        self.stats.scanned += 1

        result = await self.searcher.search_torrent_match(
            torrent_details, api, torrent_object
        )

        if result.torrent_id is None:
            logger.header("No matching torrent found")
            return result.search_success, None, None

        self.stats.found += 1
        logger.success("Found match! Torrent ID: %s", result.torrent_id)

        if config.cfg.global_config.no_download:
            return result.search_success, str(result.torrent_id), None

        matched_torrent_hash = None
        injection_success = False

        if result.matched_torrent is None:
            logger.error(
                "Failed to inject torrent: %s (matched_torrent is None)",
                result.torrent_id,
            )
        else:
            try:
                injection_success = await self.injector.inject_matched_torrent(
                    result.matched_torrent, torrent_details, result.hash_match
                )
                if injection_success:
                    self.stats.downloaded += 1
                    matched_torrent_hash = result.matched_torrent.infohash
                    logger.success("Torrent injected successfully")
                else:
                    logger.error("Failed to inject torrent: %s", result.torrent_id)
            except TorrentConflictError as e:
                logger.debug("Torrent conflict detected: %s", e)
                return result.search_success, None, None
            except Exception as e:
                logger.exception("Error during torrent injection: %s", e)

        if not injection_success:
            self.stats.cnt_dl_fail += 1
            if self.stats.cnt_dl_fail <= 10:
                logger.error(
                    "It might because the torrent id %s has reached the limitation of "
                    "non-browser downloading of %s. The failed download info will be "
                    "saved to database. You can download it from your own browser.",
                    result.torrent_id,
                    api.server,
                )
                if self.stats.cnt_dl_fail == 10:
                    logger.debug(
                        "Suppressing further hinting for .torrent file "
                        "downloading failures"
                    )

        return result.search_success, str(result.torrent_id), matched_torrent_hash

    async def process_single_torrent_from_client(
        self,
        torrent_details: ClientTorrentInfo,
        skip_scanned_check: bool = False,
    ) -> bool:
        """Process a single torrent from client torrent list.

        Args:
            torrent_details: Torrent details from client.
            skip_scanned_check: If True, skip already-scanned check.

        Returns:
            True if any target site was successful, False otherwise.
        """
        torrent_object = await self.torrent_client.get_torrent_object(
            torrent_details.hash
        )

        any_success = False

        for api_instance in self.target_apis:
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

            if should_skip_target_site(
                api_instance.tracker_query,
                torrent_details.existing_target_trackers,
            ):
                logger.debug(
                    "Content already exists on %s, skipping", api_instance.tracker_query
                )
                continue

            try:
                (
                    search_success,
                    matched_torrent_id,
                    matched_torrent_hash,
                ) = await self.process_torrent_search(
                    torrent_details=torrent_details,
                    api=api_instance,
                    torrent_object=torrent_object,
                )

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

        target_trackers = [
            api_instance.tracker_query for api_instance in self.target_apis
        ]

        self.stats = ProcessorStats()

        try:
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

                any_success = await self.process_single_torrent_from_client(
                    torrent_details=torrent_details,
                )

                if any_success:
                    logger.success("Torrent processed successfully")

        except Exception as e:
            logger.exception("Error processing torrents: %s", e)
        finally:
            if self.notifier and self.stats.scanned > 0:
                await self.notifier.send_scan_complete(self.stats)

            logger.success("Torrent processing summary:")
            logger.success("Torrents scanned: %d", self.stats.scanned)
            logger.success("Matches found: %d", self.stats.found)
            logger.success(".torrent files downloaded: %d", self.stats.downloaded)
            logger.section("===== Torrent Processing Complete =====")

    async def retry_undownloaded_torrents(self):
        """Re-download undownloaded torrents."""
        logger.section("===== Retrying Undownloaded Torrents =====")

        retry_stats = ProcessorStats()

        try:
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
                    api_instance = find_api_by_site_host(
                        self.target_apis, undownloaded.site_host
                    )
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

                    matched_torrent = await api_instance.download_torrent(torrent_id)

                    success = await self.injector.inject_matched_torrent(
                        matched_torrent, local_torrent_info, hash_match=False
                    )
                    if success:
                        retry_stats.successful += 1
                        retry_stats.removed += 1

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
        """Post-process injected torrents to start downloading completed ones."""
        logger.section("===== Post-Processing Injected Torrents =====")

        stats = PostProcessStats()

        try:
            matched_results = await self.database.get_matched_scan_results()
            if not matched_results:
                logger.debug("No matched torrents found")
                return

            logger.info("Found %d matched torrents", len(matched_results))

            for matched_torrent_hash in matched_results:
                stats.matches_checked += 1

                result = await self.torrent_client.post_process_single_injected_torrent(
                    matched_torrent_hash
                )

                if result.status == PostProcessStatus.COMPLETED:
                    stats.matches_completed += 1
                    if result.started_downloading:
                        stats.matches_started_downloading += 1
                elif result.status == PostProcessStatus.PARTIAL_KEPT:
                    pass
                elif result.status in (
                    PostProcessStatus.PARTIAL_REMOVED,
                    PostProcessStatus.ERROR,
                ):
                    stats.matches_failed += 1

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
            infohash: Infohash of the torrent to process.

        Returns:
            Processing result with status and details.
        """
        try:
            target_trackers = {
                api_instance.tracker_query for api_instance in self.target_apis
            }

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
    ) -> tuple["GazelleAPI", dict[str, int]] | ProcessResponse:
        """Get torrent API instance and torrent info from link.

        Args:
            torrent_link: Torrent link containing site information.
            tid: Torrent ID to fetch info for.

        Returns:
            Tuple of (torrent_api, fdict_torrent) on success,
            or ProcessResponse on failure.
        """
        site_host = parse_site_host_from_link(torrent_link)
        if not site_host:
            return ProcessResponse(
                status=ProcessStatus.ERROR,
                message=f"Invalid torrent link (missing hostname): {torrent_link}",
            )
        torrent_api = find_api_by_site_host(self.target_apis, site_host)
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
        torrent_api: "GazelleAPI",
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
        matched_torrents = await self._search_torrent_by_filename_in_client(
            fdict_torrent
        )

        if not matched_torrents:
            return ProcessResponse(
                status=ProcessStatus.NOT_FOUND,
                message=f"No matching torrent found in client for: {torrent_name}",
            )

        for matched_torrent in matched_torrents:
            matched_api = find_api_by_tracker(
                self.target_apis, matched_torrent.trackers
            )
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
            torrent_name: Name of the torrent.
            torrent_link: Torrent link containing the torrent ID.
            album_name: Album name for searching.

        Returns:
            Processing result with status and details.
        """
        try:
            tid = parse_torrent_id_from_link(torrent_link)

            logger.debug("Extracted torrent ID: %s from link: %s", tid, torrent_link)

            album_keywords = extract_album_keywords(album_name)
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

            api_result = await self._get_torrent_api_and_info(torrent_link, tid)
            if isinstance(api_result, ProcessResponse):
                return api_result
            torrent_api, fdict_torrent = api_result

            match_result = await self._find_reverse_announce_match(
                torrent_name, fdict_torrent, torrent_api, tid
            )
            if isinstance(match_result, ProcessResponse):
                return match_result
            matched_torrent = match_result

            try:
                torrent_object = await torrent_api.download_torrent(tid)
            except Exception as e:
                logger.error("Failed to download torrent data: %s", e)
                return ProcessResponse(
                    status=ProcessStatus.ERROR,
                    message=f"Failed to download torrent data: {str(e)}",
                )

            rename_map = generate_rename_map(matched_torrent.fdict, fdict_torrent)

            final_download_dir = await self.injector.prepare_linked_download_dir(
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

                if self.notifier:
                    await self.notifier.send_announce_success(
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
                "Error processing reverse announce torrent %s: %s",
                torrent_name,
                str(e),
            )
            return ProcessResponse(
                status=ProcessStatus.ERROR,
                message=f"Error processing torrent: {str(e)}",
            )
