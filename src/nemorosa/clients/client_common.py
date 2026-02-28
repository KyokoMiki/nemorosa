"""
Common torrent client functionality.

Provides base classes, utilities and shared logic for all torrent client
implementations.
"""

import logging
import posixpath
import shutil
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import groupby
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlparse

import anyio
import msgspec
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from asyncer import asyncify
from tenacity import before_sleep_log, retry, stop_after_attempt, wait_fixed
from torf import Torrent

from .. import config, filecompare, logger

if TYPE_CHECKING:
    from ..db import NemorosaDatabase
    from ..notifier import Notifier


def decode_bitfield_bytes(bitfield_data: bytes, piece_count: int) -> list[bool]:
    """Decode bitfield bytes data to get piece download status.

    This is a common utility function used by different torrent clients to decode
    bitfield data where each bit represents whether a piece has been downloaded.

    Args:
        bitfield_data: Raw bytes representing the bitfield
        piece_count: Total number of pieces in the torrent

    Returns:
        List of boolean values indicating piece download status
    """
    piece_progress = [False] * piece_count

    for byte_index in range(min(len(bitfield_data), (piece_count + 7) // 8)):
        byte_value = bitfield_data[byte_index]
        start_piece = byte_index * 8
        end_piece = min(start_piece + 8, piece_count)

        for bit_offset in range(end_piece - start_piece):
            bit_index = 7 - bit_offset
            piece_progress[start_piece + bit_offset] = bool(
                byte_value & (1 << bit_index)
            )

    return piece_progress


class PostProcessStatus(StrEnum):
    """Status of post-processing an injected torrent."""

    COMPLETED = "completed"
    PARTIAL_KEPT = "partial_kept"
    PARTIAL_REMOVED = "partial_removed"
    NOT_FOUND = "not_found"
    CHECKING = "checking"
    ERROR = "error"


class PostProcessResult(msgspec.Struct):
    """Result of processing a single injected torrent."""

    status: PostProcessStatus = PostProcessStatus.NOT_FOUND
    started_downloading: bool = False
    error_message: str | None = None


class FieldSpec(msgspec.Struct):
    """Base field specification for torrent client field extraction."""

    _request_arguments: str | set[str] | None
    extractor: Callable[[Any], Any]

    @property
    def request_arguments(self) -> set[str]:
        """Get request arguments as a set, converting string to set if needed."""
        if isinstance(self._request_arguments, str):
            return {self._request_arguments}
        elif isinstance(self._request_arguments, set):
            return self._request_arguments
        else:
            return set()


class TorrentState(StrEnum):
    """Torrent download state enumeration."""

    UNKNOWN = "unknown"
    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    PAUSED = "paused"
    COMPLETED = "completed"
    CHECKING = "checking"
    ERROR = "error"
    QUEUED = "queued"
    MOVING = "moving"
    ALLOCATING = "allocating"
    METADATA_DOWNLOADING = "metadata_downloading"


class TorrentConflictError(Exception):
    """Exception raised when torrent cannot coexist with local torrent."""


class ClientTorrentFile(msgspec.Struct):
    """Represents a file within a torrent from torrent client."""

    name: str
    size: int
    progress: float  # File download progress (0.0 to 1.0)


class ClientTorrentInfo(msgspec.Struct):
    """Represents a torrent with all its information from torrent client."""

    hash: str
    name: str = ""
    progress: float = 0.0
    total_size: int = 0
    files: list[ClientTorrentFile] = msgspec.field(default_factory=list)
    trackers: list[str] = msgspec.field(default_factory=list)
    download_dir: str = ""
    state: TorrentState = TorrentState.UNKNOWN  # Torrent state
    existing_target_trackers: set[str] = msgspec.field(default_factory=set)
    piece_progress: list[bool] = msgspec.field(
        default_factory=list
    )  # Piece download status

    @property
    def fdict(self) -> dict[str, int]:
        """Generate file dictionary mapping relative file path to file size.

        Returns:
            dict[str, int]: Dictionary mapping relative file path to file size.
        """
        if not self.files or not self.name:
            return {}
        return {posixpath.relpath(f.name, self.name): f.size for f in self.files}


class TorrentClient(ABC):
    """Abstract base class for torrent clients."""

    # Class attribute: whether client supports specifying final directory on add
    supports_final_directory = False
    # Class attribute: whether client supports fast resume
    # (skip verification on hash mismatch)
    supports_fast_resume = False

    def __init__(
        self,
        database: "NemorosaDatabase",
        scheduler: AsyncIOScheduler,
        notifier: "Notifier | None" = None,
    ) -> None:
        # Injected dependencies
        self.database = database
        self.scheduler = scheduler
        self.notifier = notifier

        # Monitoring state
        self.monitoring = False
        # key: torrent_hash, value: is_verifying (False=delayed, True=verifying)
        self._tracked_torrents: dict[str, bool] = {}
        self._monitor_condition = anyio.Condition()

        # Job configuration
        self._monitor_job_id = "torrent_monitor"

        # Field configuration - to be set by subclasses
        self.field_config: dict[str, FieldSpec] = {}

    # region Abstract Public

    @abstractmethod
    async def get_torrents(
        self, torrent_hashes: list[str] | None = None, fields: list[str] | None = None
    ) -> list[ClientTorrentInfo]:
        """Get all torrents from client.

        Args:
            torrent_hashes (list[str] | None): Optional list of torrent hashes
                to filter. If None, all torrents will be returned.
            fields (list[str] | None): List of field names to include in the
                result. If None, all available fields will be included.
                Available fields:
                - hash, name, progress, total_size, files, trackers,
                  download_dir, state, piece_progress

        Returns:
            list[ClientTorrentInfo]: List of torrent information objects.
        """

    @abstractmethod
    async def get_torrent_info(
        self, torrent_hash: str, fields: list[str] | None
    ) -> ClientTorrentInfo | None:
        """Get torrent information.

        Args:
            torrent_hash (str): Torrent hash.
            fields (list[str] | None): List of field names to include in the result.
                If None, all available fields will be included.
                Available fields:
                - hash, name, progress, total_size, files, trackers,
                  download_dir, state, piece_progress

        Returns:
            ClientTorrentInfo | None: Torrent information object, or None if not found.
        """

    @abstractmethod
    async def get_torrents_for_monitoring(
        self, torrent_hashes: set[str]
    ) -> dict[str, TorrentState]:
        """Get torrent states for monitoring (optimized for specific torrents).

        This method is optimized for monitoring specific torrents and should only
        return the minimal required information (hash and state) for efficiency.

        Args:
            torrent_hashes (set[str]): Set of torrent hashes to monitor.

        Returns:
            dict[str, TorrentState]: Mapping of torrent hash to current state.
        """

    # endregion

    # region Abstract Internal

    @abstractmethod
    async def _add_torrent(
        self,
        torrent_data: bytes,
        download_dir: str,
        hash_match: bool,
        local_torrent_hash: str = "",
    ) -> str:
        """Add torrent to client, return torrent hash.

        Args:
            torrent_data (bytes): Torrent file data.
            download_dir (str): Download directory.
            hash_match (bool): Whether this is a hash match, if True, skip verification.
            local_torrent_hash (str): Hash of the original local torrent.
                Used for duplicate_categories feature to fetch category on demand.

        Returns:
            str: Torrent hash.
        """

    @abstractmethod
    async def _remove_torrent(self, torrent_hash: str) -> None:
        """Remove torrent from client.

        Args:
            torrent_hash (str): Torrent hash.
        """

    @abstractmethod
    async def _rename_torrent(
        self, torrent_hash: str, old_name: str, new_name: str
    ) -> None:
        """Rename entire torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_name (str): Old torrent name.
            new_name (str): New torrent name.
        """

    @abstractmethod
    async def _rename_file(
        self, torrent_hash: str, old_path: str, new_name: str
    ) -> None:
        """Rename file within torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_path (str): Old file path.
            new_name (str): New file name.
        """

    @abstractmethod
    async def _verify_torrent(self, torrent_hash: str) -> None:
        """Verify torrent integrity.

        Args:
            torrent_hash (str): Torrent hash.
        """

    @abstractmethod
    async def _process_rename_map(
        self, torrent_hash: str, base_path: str, rename_map: dict
    ) -> dict:
        """Process rename mapping to adapt to specific torrent client.

        Args:
            torrent_hash (str): Torrent hash.
            base_path (str): Base path for files.
            rename_map (dict): Original rename mapping.

        Returns:
            dict: Processed rename mapping.
        """

    @abstractmethod
    async def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from client.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bytes | None: Torrent file data, or None if not available.
        """

    @abstractmethod
    async def _resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bool: True if successful, False otherwise.
        """

    # endregion

    # region Torrent Retrieval

    async def rebuild_client_torrents_cache(self, torrents: list[ClientTorrentInfo]):
        """Clear and rebuild database cache with provided torrents.

        This method clears all existing cache entries and repopulates the database cache
        with the provided torrent list.

        Args:
            torrents (list[ClientTorrentInfo]): List of torrents to cache.
        """
        try:
            await self.database.clear_client_torrents_cache()

            if not torrents:
                logger.debug("No torrents provided for cache rebuild")
                return

            # Batch save to database
            await self.database.batch_save_client_torrents(torrents)
            logger.success("Cached %d torrents to database", len(torrents))

        except Exception as e:
            logger.warning("Failed to rebuild cache: %s", e)

    async def rebuild_client_torrents_cache_incremental(
        self, torrents: list[ClientTorrentInfo]
    ):
        """Rebuild database cache with provided torrents incrementally (one by one).

        This method provides the same functionality as rebuild_client_torrents_cache,
        but is designed for background execution with better async performance.
        It first batch deletes torrents that no longer exist, then updates/inserts
        torrents one by one to allow other async operations to interleave.

        Args:
            torrents (list[ClientTorrentInfo]): List of torrents to rebuild cache with.
        """
        try:
            if not torrents:
                logger.debug("No torrents provided for cache sync")
                # Clear all cached torrents if the new list is empty
                with anyio.CancelScope(shield=True):
                    await self.database.clear_client_torrents_cache()
                return

            # Get current cached torrent hashes
            cached_hashes: set[str] = set()
            with anyio.CancelScope(shield=True):
                cached_hashes = await self.database.get_all_cached_torrent_hashes()
            new_hashes = {torrent.hash for torrent in torrents}

            # Step 1: Batch delete torrents that no longer exist in client
            torrents_to_delete = cached_hashes - new_hashes
            if torrents_to_delete:
                with anyio.CancelScope(shield=True):
                    await self.database.delete_client_torrents(torrents_to_delete)
                logger.debug("Deleted %d torrents from cache", len(torrents_to_delete))

            # Step 2: Update/insert torrents one by one
            for torrent in torrents:
                with anyio.CancelScope(shield=True):
                    await self.database.save_client_torrent_info(torrent)
                await anyio.sleep(0)  # Yield control to allow other async tasks to run

            logger.success("Synced %d torrents to database cache", len(torrents))

        except anyio.get_cancelled_exc_class():
            # During shutdown, exit gracefully - the sync will continue on the next run
            logger.debug("Cache sync cancelled during shutdown (expected)")
            return
        except Exception as e:
            logger.warning("Failed to sync cache: %s", e)

    async def refresh_client_torrents_cache(self) -> None:
        """Refresh local client_torrents database cache with incremental updates.

        This method updates the client_torrents table in database with torrent
        information:
        1. Get basic info for all torrents (hash, name, download_dir)
        2. Get all cached torrents from database
        3. Compare in Python to find modified torrents and deleted torrents
        4. Fetch only modified torrents from client API
        5. Update client_torrents cache in database with modified torrents
        6. Delete torrents that no longer exist in client from database
        """
        try:
            # Step 1: Get basic info for all torrents (minimal API call)
            basic_torrents = await self.get_torrents(
                fields=["hash", "name", "download_dir"]
            )
            if not basic_torrents:
                logger.debug("No torrents found in client")
                return

            # Step 2: Get all cached torrents in one query (batch optimized)
            cached_torrents = await self.database.get_all_client_torrents_basic()

            # Step 3: Check which torrents need to be updated
            torrents_to_fetch = [
                torrent.hash
                for torrent in basic_torrents
                if cached_torrents.get(torrent.hash)
                != (torrent.name, torrent.download_dir)
            ]

            # Find torrents that exist in cache but not in client
            torrents_to_delete = cached_torrents.keys() - {
                torrent.hash for torrent in basic_torrents
            }

            # Step 4: Fetch modified/new torrents from client API
            if torrents_to_fetch:
                # Get full info for modified torrents only
                modified_torrents = await self.get_torrents(
                    torrent_hashes=torrents_to_fetch,
                    fields=[
                        "hash",
                        "name",
                        "total_size",
                        "files",
                        "trackers",
                        "download_dir",
                    ],
                )

                # Step 5: Update database
                if modified_torrents:
                    await self.database.batch_save_client_torrents(modified_torrents)
                    logger.debug(
                        "Updated %s modified torrents in database cache",
                        len(modified_torrents),
                    )
            else:
                logger.debug("No modified torrents found, database cache is up to date")

            # Step 6: Delete torrents that no longer exist in client
            if torrents_to_delete:
                await self.database.delete_client_torrents(torrents_to_delete)
                logger.debug(
                    "Deleted %s torrents from database cache (removed from client)",
                    len(torrents_to_delete),
                )

        except Exception as e:
            logger.error("Error refreshing database cache: %s", e)

    async def get_file_matched_torrents(
        self, target_file_size: int, fname_keywords: list[str]
    ) -> list[ClientTorrentInfo]:
        """Get torrents matching file size and name keywords.

        This is a wrapper around db.search_torrent_by_file_match that
        processes the raw database rows into ClientTorrentInfo objects.

        Args:
            target_file_size: Target file size to match.
            fname_keywords: List of keywords that should appear in file path.

        Returns:
            List of ClientTorrentInfo objects.
        """
        rows = await self.database.search_torrent_by_file_match(
            target_file_size, fname_keywords
        )

        # Group rows by torrent hash (rows are already ordered by hash from the query)
        torrents = []

        for _, group_rows in groupby(rows, key=lambda row: row.hash):
            # Convert group iterator to list for processing
            group_list = list(group_rows)
            if not group_list:
                continue

            # Get torrent metadata from first row using attribute access
            first_row = group_list[0]

            # Decode trackers once
            decoded_trackers = (
                msgspec.json.decode(first_row.trackers) if first_row.trackers else []
            )

            # Build files list efficiently using attribute access
            files = [
                ClientTorrentFile(
                    name=row.file_path,
                    size=row.file_size,
                    progress=1.0,  # Assume complete for cached torrents
                )
                for row in group_list
            ]

            # Create ClientTorrentInfo object using attribute access
            torrents.append(
                ClientTorrentInfo(
                    hash=first_row.hash,
                    name=first_row.name,
                    download_dir=first_row.download_dir,
                    total_size=first_row.total_size,
                    trackers=decoded_trackers,
                    files=files,
                )
            )

        return torrents

    async def get_single_torrent(
        self, infohash: str, target_trackers: set[str]
    ) -> ClientTorrentInfo | None:
        """Get single torrent by infohash with existing trackers information.

        This method follows the same logic as get_filtered_torrents but for a single
        torrent. It finds the torrent by infohash and determines which target trackers
        this content already exists on by checking all torrents with the same content
        name.

        Args:
            infohash (str): Torrent infohash.
            target_trackers (list[str]): List of target tracker names.

        Returns:
            ClientTorrentInfo | None: Torrent information with existing_trackers,
                or None if not found.
        """
        try:
            # Find torrent by infohash
            target_torrent = await self.get_torrent_info(
                infohash,
                fields=[
                    "hash",
                    "name",
                    "total_size",
                    "files",
                    "trackers",
                    "download_dir",
                ],
            )

            if not target_torrent:
                logger.debug(
                    "Torrent with infohash %s not found in client torrent list",
                    infohash,
                )
                return None

            logger.debug("Found torrent: %s (%s)", target_torrent.name, infohash)

            # Check if torrent meets basic conditions
            if not self._should_include_torrent(target_torrent):
                return None

            # Collect which target trackers this content already exists on
            # (by checking all torrents with the same content name)
            all_torrents = await self.get_torrents(fields=["name", "trackers"])
            existing_trackers = {
                target_tracker
                for torrent in all_torrents
                if torrent.name == target_torrent.name
                for target_tracker in target_trackers
                for tracker_url in torrent.trackers
                if target_tracker in tracker_url
            }

            # Return torrent info with existing_trackers
            return ClientTorrentInfo(
                hash=target_torrent.hash,
                name=target_torrent.name,
                total_size=target_torrent.total_size,
                files=target_torrent.files,
                trackers=target_torrent.trackers,
                download_dir=target_torrent.download_dir,
                existing_target_trackers=existing_trackers,
            )

        except Exception as e:
            logger.error("Error retrieving single torrent: %s", e)
            return None

    def _should_include_torrent(self, torrent: ClientTorrentInfo) -> bool:
        """Check if a torrent meets all filter conditions.

        Evaluates check_trackers, exclude_mp3, and check_music_only configuration
        to determine whether a torrent should be included in filtered results.

        Args:
            torrent: Torrent information to check.

        Returns:
            True if the torrent passes all filters, False otherwise.
        """
        # Check tracker filter
        check_trackers_list = config.cfg.global_config.check_trackers
        if check_trackers_list and not any(
            any(check_str in url for check_str in check_trackers_list)
            for url in torrent.trackers
        ):
            logger.debug(
                "Torrent %s filtered out: tracker not in check_trackers list",
                torrent.name,
            )
            logger.debug("Torrent trackers: %s", torrent.trackers)
            logger.debug("Required trackers: %s", check_trackers_list)
            return False

        # Check MP3 exclusion filter
        if config.cfg.global_config.exclude_mp3:
            has_mp3 = any(
                posixpath.splitext(file.name)[1].lower() == ".mp3"
                for file in torrent.files
            )
            if has_mp3:
                logger.debug(
                    "Torrent %s filtered out: contains MP3 files (exclude_mp3=true)",
                    torrent.name,
                )
                return False

        # Check music only filter
        if config.cfg.global_config.check_music_only:
            has_music = any(
                filecompare.is_music_file(file.name) for file in torrent.files
            )
            if not has_music:
                logger.debug(
                    "Torrent %s filtered out: no music files found "
                    "(check_music_only=true)",
                    torrent.name,
                )
                file_extensions = [
                    posixpath.splitext(f.name)[1].lower() for f in torrent.files
                ]
                logger.debug("File extensions in torrent: %s", file_extensions)
                return False

        return True

    def _group_torrents_by_content(
        self,
        torrents: list[ClientTorrentInfo],
        target_trackers: list[str],
    ) -> tuple[dict[str, set[str]], dict[str, ClientTorrentInfo]]:
        """Group torrents by content name and collect tracker mappings.

        This method filters torrents using _should_include_torrent, groups them
        by content name (torrent.name), and tracks which target trackers each
        content already exists on.

        Args:
            torrents: List of torrents to process.
            target_trackers: List of target tracker names to check.

        Returns:
            A tuple of (content_tracker_mapping, valid_torrents) where:
                - content_tracker_mapping: Maps content name to set of target
                    trackers it exists on
                - valid_torrents: Maps content name to the best torrent for
                    that content
        """
        content_tracker_mapping: dict[str, set[str]] = {}
        valid_torrents: dict[str, ClientTorrentInfo] = {}

        for torrent in torrents:
            # Apply all filter conditions
            if not self._should_include_torrent(torrent):
                continue

            content_name = torrent.name

            # Record which trackers this content exists on
            if content_name not in content_tracker_mapping:
                content_tracker_mapping[content_name] = set()

            for tracker_url in torrent.trackers:
                for target_tracker in target_trackers:
                    if target_tracker in tracker_url:
                        content_tracker_mapping[content_name].add(target_tracker)

            # Save torrent info (if duplicated, choose better version)
            if content_name not in valid_torrents:
                valid_torrents[content_name] = torrent
            else:
                # Choose version with fewer files or smaller size
                existing = valid_torrents[content_name]
                if len(torrent.files) < len(existing.files) or (
                    len(torrent.files) == len(existing.files)
                    and torrent.total_size < existing.total_size
                ):
                    valid_torrents[content_name] = torrent

        return content_tracker_mapping, valid_torrents

    async def get_filtered_torrents(
        self, target_trackers: list[str]
    ) -> dict[str, ClientTorrentInfo]:
        """Get filtered torrent list and rebuild cache.

        This method performs the following operations:
        1. Get all torrents from client with static fields
        2. Rebuild cache with all torrents (clear and repopulate)
        3. Group by torrent content (same name considered same content)
        4. Check which target trackers each content already exists on
        5. Only return content that doesn't exist on all target trackers

        Args:
            target_trackers (list[str]): List of target tracker names.

        Returns:
            dict[str, ClientTorrentInfo]: Dictionary mapping torrent name to
                torrent info.
        """
        try:
            # Get all torrents with required fields
            torrents = list(
                await self.get_torrents(
                    fields=[
                        "hash",
                        "name",
                        "total_size",
                        "files",
                        "trackers",
                        "download_dir",
                    ]
                )
            )

            # Rebuild cache with all torrents (run in background)
            self.scheduler.add_job(
                self.rebuild_client_torrents_cache_incremental,
                trigger=DateTrigger(),
                args=[torrents],
                id="rebuild_cache",
                misfire_grace_time=None,
                replace_existing=True,
                max_instances=1,
            )

            # Group by content name, collect which trackers each content exists on
            content_tracker_mapping, valid_torrents = self._group_torrents_by_content(
                torrents, target_trackers
            )

            # Step 2: Filter out content that already exists on all target trackers
            filtered_torrents = {}
            target_tracker_set = set(target_trackers)

            for content_name, torrent in valid_torrents.items():
                existing_trackers = content_tracker_mapping.get(content_name, set())

                # If this content already exists on all target trackers, skip
                if target_tracker_set.issubset(existing_trackers):
                    logger.debug(
                        "Skipping %s: already exists on all target trackers %s",
                        content_name,
                        existing_trackers,
                    )
                    continue

                # Otherwise include in results
                filtered_torrents[content_name] = ClientTorrentInfo(
                    hash=torrent.hash,
                    name=content_name,
                    total_size=torrent.total_size,
                    files=torrent.files,
                    trackers=torrent.trackers,
                    download_dir=torrent.download_dir,
                    existing_target_trackers=existing_trackers,
                )

            return filtered_torrents

        except Exception as e:
            logger.error("Error retrieving torrents: %s", e)
            return {}

    async def get_torrent_object(self, torrent_hash: str) -> Torrent | None:
        """Get torrent object from client by hash.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            Torrent | None: Torrent object, or None if not available.
        """
        try:
            torrent_data = await self._get_torrent_data(torrent_hash)
            if torrent_data:
                return Torrent.read_stream(torrent_data)
            return None
        except Exception as e:
            logger.error(
                "Error getting torrent object for hash %s: %s", torrent_hash, e
            )
            return None

    def _get_field_config_and_arguments(
        self, fields: list[str] | None
    ) -> tuple[dict[str, FieldSpec], list[str]]:
        """Get filtered field configuration and required arguments.

        This helper method eliminates code duplication across client implementations
        by providing a common way to filter field configurations and extract
        required arguments for client API calls.

        Args:
            fields: List of field names to include, or None for all fields.

        Returns:
            tuple[dict[str, FieldSpec], list[str]]: (field_config, arguments)
                where:
                - field_config: Filtered field configuration dict mapping field
                    names to FieldSpec objects
                - arguments: List of required argument names for the client API
        """
        # Get requested fields (always include hash)
        field_config = (
            {k: v for k, v in self.field_config.items() if k in fields or k == "hash"}
            if fields
            else self.field_config
        )

        # Get required arguments from field_config
        arguments = list(
            set().union(*[spec.request_arguments for spec in field_config.values()])
        )

        return field_config, arguments

    # endregion

    # region Torrent Injection

    async def inject_torrent(
        self,
        torrent_object: Torrent,
        download_dir: str,
        local_torrent_name: str,
        rename_map: dict,
        hash_match: bool,
        local_torrent_hash: str = "",
    ) -> tuple[bool, bool]:
        """Inject torrent into client (includes complete logic).

        Derived classes only need to implement specific client operation methods.

        Args:
            torrent_object: Torrent object.
            download_dir (str): Download directory.
            local_torrent_name (str): Local torrent name.
            rename_map (dict): File rename mapping.
            hash_match (bool): Whether this is a hash match, if True, skip verification.
            local_torrent_hash (str): Hash of the original local torrent.
                Used for duplicate_categories feature.

        Returns:
            tuple[bool, bool]: (success, verified) where:
                - success: True if injection successful, False otherwise
                - verified: True if verification was performed, False
                    otherwise
        """
        current_name = str(torrent_object.name)
        name_differs = current_name != local_torrent_name
        torrent_url = torrent_object.comment or ""

        if self.supports_final_directory:
            # rTorrent supports specifying final directory level when adding
            final_download_dir = posixpath.join(download_dir, local_torrent_name)
            if name_differs:
                original_download_dir = posixpath.join(download_dir, current_name)
                try:
                    await asyncify(shutil.move)(
                        original_download_dir, final_download_dir
                    )
                except FileExistsError as e:
                    logger.warning(
                        "Download directory already exists, skipping rename: %s", e
                    )
                except OSError as e:
                    logger.error(
                        "Failed to rename directory from %s to %s: %s",
                        current_name,
                        local_torrent_name,
                        e,
                    )
                    raise
                current_name = local_torrent_name
            download_dir = final_download_dir

        # Add torrent to client
        try:
            torrent_hash = await self._add_torrent(
                torrent_object.dump(),
                download_dir,
                hash_match,
                local_torrent_hash=local_torrent_hash,
            )
        except TorrentConflictError as e:
            logger.error("Torrent injection failed due to conflict: %s", e)
            logger.error(
                "This usually happens because the source flag of the torrent "
                "to be injected is incorrect, which generally occurs on "
                "trackers that do not enforce source flag requirements."
            )
            raise

        try:
            should_verify = await self._perform_torrent_rename_and_verify(
                torrent_hash=torrent_hash,
                current_name=current_name,
                local_torrent_name=local_torrent_name,
                rename_map=rename_map,
                name_differs=name_differs,
                hash_match=hash_match,
            )
            logger.success("Torrent injected successfully")
            return True, should_verify
        except Exception as e:
            logger.error("Failed to inject torrent after retries: %s", e)

            # Send failure notification if configured
            if self.notifier:
                await self.notifier.send_inject_failure(
                    torrent_name=local_torrent_name,
                    torrent_url=torrent_url,
                    reason=str(e),
                )

            return False, False

    @retry(
        stop=stop_after_attempt(8),
        wait=wait_fixed(2),
        before_sleep=before_sleep_log(logging.getLogger("nemorosa"), logging.DEBUG),
        reraise=True,
    )
    async def _perform_torrent_rename_and_verify(
        self,
        torrent_hash: str,
        current_name: str,
        local_torrent_name: str,
        rename_map: dict[str, str],
        name_differs: bool,
        hash_match: bool,
    ) -> bool:
        """Perform torrent rename and verification with retry logic.

        This method handles renaming the torrent and its files, then verifies
        the torrent if needed. Uses tenacity for automatic retries.

        Args:
            torrent_hash: Hash of the added torrent.
            current_name: Current name of the torrent.
            local_torrent_name: Target name for the torrent.
            rename_map: File rename mapping.
            name_differs: Whether the torrent name differs from local name.
            hash_match: Whether this is a hash match.

        Returns:
            True if verification was performed, False otherwise.

        Raises:
            Exception: If all retry attempts fail.
        """
        # Rename entire torrent
        if current_name != local_torrent_name:
            await self._rename_torrent(torrent_hash, current_name, local_torrent_name)
            logger.debug(
                "Renamed torrent from %s to %s", current_name, local_torrent_name
            )

        if not config.cfg.linking.enable_linking:
            # Process rename map
            rename_map = await self._process_rename_map(
                torrent_hash=torrent_hash,
                base_path=local_torrent_name,
                rename_map=rename_map,
            )

            # Rename files
            if rename_map:
                for torrent_file_name, local_file_name in rename_map.items():
                    await self._rename_file(
                        torrent_hash,
                        torrent_file_name,
                        local_file_name,
                    )
                    logger.debug(
                        "Renamed torrent file %s to %s",
                        torrent_file_name,
                        local_file_name,
                    )

        # Verify torrent (if renaming was performed or not hash match for
        # clients without fast resume)
        should_verify = (
            name_differs
            or bool(rename_map)
            or (not hash_match and not self.supports_fast_resume)
        )
        if should_verify:
            logger.debug("Verifying torrent after renaming")
            await anyio.sleep(1)
            await self._verify_torrent(torrent_hash)

        return should_verify

    async def reverse_inject_torrent(
        self,
        matched_torrents: list[ClientTorrentInfo],
        new_name: str,
        reverse_rename_map: dict,
    ) -> dict[str, bool]:
        """Reverse inject logic: rename local torrents to match incoming format.

        Args:
            matched_torrents (list[ClientTorrentInfo]): List of local torrents
                to rename.
            new_name (str): New torrent name to match incoming torrent.
            reverse_rename_map (dict): File rename mapping from local to
                incoming format.

        Returns:
            dict[str, bool]: Dictionary mapping torrent hash to success status.
        """
        results = {}

        for matched_torrent in matched_torrents:
            torrent_hash = matched_torrent.hash
            try:
                # Get current torrent name
                torrent_info = await self.get_torrent_info(torrent_hash, ["name"])
                if torrent_info is None or torrent_info.name is None:
                    logger.warning(
                        "Failed to get torrent info for %s, skipping", torrent_hash
                    )
                    continue
                current_name = torrent_info.name

                # Rename entire torrent
                if current_name != new_name:
                    await self._rename_torrent(torrent_hash, current_name, new_name)
                    logger.debug(
                        "Renamed torrent %s from %s to %s",
                        torrent_hash,
                        current_name,
                        new_name,
                    )

                # Rename files according to reverse rename map
                if reverse_rename_map:
                    for (
                        local_file_name,
                        incoming_file_name,
                    ) in reverse_rename_map.items():
                        await self._rename_file(
                            torrent_hash,
                            local_file_name,
                            incoming_file_name,
                        )
                        logger.debug(
                            "Renamed file %s to %s in torrent %s",
                            local_file_name,
                            incoming_file_name,
                            torrent_hash,
                        )

                # Verify torrent after renaming
                if current_name != new_name or reverse_rename_map:
                    logger.debug(
                        "Verifying torrent %s after reverse renaming", torrent_hash
                    )
                    await self._verify_torrent(torrent_hash)

                results[str(torrent_hash)] = True
                logger.success(
                    "Reverse injection completed successfully for torrent %s",
                    torrent_hash,
                )

            except Exception as e:
                results[str(torrent_hash)] = False
                logger.error("Failed to reverse inject torrent %s: %s", torrent_hash, e)

        return results

    # endregion

    # region Post-Processing

    async def post_process_single_injected_torrent(
        self, matched_torrent_hash: str
    ) -> PostProcessResult:
        """Post-process a single injected torrent to determine status and action.

        Args:
            matched_torrent_hash: The hash of the matched torrent to process

        Returns:
            PostProcessResult: Processing result with status,
                started_downloading flag, and error_message
        """
        result = PostProcessResult()

        try:
            logger.debug("Checking matched torrent: %s", matched_torrent_hash)

            # Check if matched torrent exists in client
            matched_torrent = await self.get_torrent_info(
                matched_torrent_hash,
                ["state", "name", "progress", "files", "piece_progress"],
            )
            if not matched_torrent:
                logger.debug(
                    "Matched torrent %s not found in client, skipping",
                    matched_torrent_hash,
                )
                result.status = PostProcessStatus.NOT_FOUND
                return result

            # Skip if matched torrent is checking
            if matched_torrent.state == TorrentState.CHECKING:
                logger.debug(
                    "Matched torrent %s is checking, skipping", matched_torrent.name
                )
                result.status = PostProcessStatus.CHECKING
                return result

            # If matched torrent is 100% complete, start downloading
            if matched_torrent.progress == 1.0:
                logger.info(
                    "Matched torrent %s is 100%% complete", matched_torrent.name
                )
                # Check if auto-start is enabled
                if config.cfg.global_config.auto_start_torrents:
                    # Start downloading the matched torrent
                    await self._resume_torrent(matched_torrent.hash)
                    logger.success(
                        "Started downloading matched torrent: %s", matched_torrent.name
                    )
                    result.started_downloading = True
                else:
                    logger.info("Auto-start disabled, torrent will remain paused")
                    result.started_downloading = False
                # Mark as checked since it's 100% complete
                await self.database.update_scan_result_checked(
                    matched_torrent_hash, True
                )
                result.status = PostProcessStatus.COMPLETED
            # If matched torrent is not 100% complete, check file progress patterns
            else:
                logger.debug(
                    "Matched torrent %s not 100%% complete (%.1f%%), "
                    "checking file patterns",
                    matched_torrent.name,
                    matched_torrent.progress * 100,
                )

                # Analyze file progress patterns
                if filecompare.should_keep_partial_torrent(matched_torrent):
                    logger.debug(
                        "Keeping partial torrent %s - valid pattern",
                        matched_torrent.name,
                    )
                    # Mark as checked since we're keeping the partial torrent
                    await self.database.update_scan_result_checked(
                        matched_torrent_hash, True
                    )
                    result.status = PostProcessStatus.PARTIAL_KEPT
                else:
                    if config.cfg.linking.link_type in (
                        config.LinkType.REFLINK,
                        config.LinkType.REFLINK_OR_COPY,
                    ):
                        # Keep partial torrent explicitly due to reflink being enabled
                        logger.info(
                            "Keeping partial torrent %s - kept due to reflink enabled",
                            matched_torrent.name,
                        )
                        await self.database.update_scan_result_checked(
                            matched_torrent_hash, True
                        )
                        result.status = PostProcessStatus.PARTIAL_KEPT
                    else:
                        logger.warning(
                            "Removing torrent %s - failed validation",
                            matched_torrent.name,
                        )
                        await self._remove_torrent(matched_torrent.hash)
                        # Clear matched torrent information from database
                        await self.database.clear_matched_torrent_info(
                            matched_torrent_hash
                        )
                        result.status = PostProcessStatus.PARTIAL_REMOVED

            # Send success notification if configured
            if self.notifier and result.status in (
                PostProcessStatus.COMPLETED,
                PostProcessStatus.PARTIAL_KEPT,
            ):
                await self.notifier.send_inject_success(
                    torrent_name=matched_torrent.name,
                    torrent_hash=matched_torrent.hash,
                    progress=matched_torrent.progress,
                )

        except Exception as e:
            logger.error("Error processing torrent %s: %s", matched_torrent_hash, e)
            result.status = PostProcessStatus.ERROR
            result.error_message = str(e)

        return result

    # endregion

    # region Monitoring

    async def start_monitoring(self) -> None:
        """Start the background monitoring service."""
        if not self.monitoring:
            self.monitoring = True

            # Add scheduled job for monitoring to the global scheduler
            self.scheduler.add_job(
                self._check_tracked_torrents,
                trigger=IntervalTrigger(seconds=1),
                id=self._monitor_job_id,
                name="Torrent Monitor",
                misfire_grace_time=None,
                max_instances=1,  # Prevent overlapping executions
                coalesce=True,
                replace_existing=True,
            )

            logger.info("Torrent monitoring started")

    async def wait_for_monitoring_completion(self) -> None:
        """Wait for monitoring to complete and tracked torrents to finish.

        This method waits up to 30 seconds for all tracked torrents to complete
        verification. If timeout occurs, remaining torrents are logged and the
        method returns.
        """
        if not self.monitoring:
            return

        self.monitoring = False

        try:
            # Wait for all tracked torrents to be processed with 30s timeout
            with anyio.move_on_after(30.0) as cancel_scope:
                async with self._monitor_condition:
                    if self._tracked_torrents:
                        logger.info(
                            "Waiting for %s tracked torrents to complete...",
                            len(self._tracked_torrents),
                        )
                    # Wait until all torrents are processed (condition loop pattern)
                    while self._tracked_torrents:
                        await self._monitor_condition.wait()

            if cancel_scope.cancelled_caught:
                async with self._monitor_condition:
                    remaining = len(self._tracked_torrents)
                logger.warning("Timeout waiting for %d torrents to complete", remaining)
            else:
                logger.info("All tracked torrents completed")

        except anyio.get_cancelled_exc_class():
            # Handle cancellation during shutdown gracefully
            logger.debug("Monitoring wait cancelled during shutdown")
            return

        logger.info("Torrent monitoring completed and stopped")

    async def _check_tracked_torrents(self) -> None:
        """Check tracked torrents for verification completion.

        This method is called by APScheduler at regular intervals.
        """
        if not self._tracked_torrents:
            return

        try:
            # Only check torrents that are in verifying state (True)
            verifying_torrents = {
                th
                for th, is_verifying in self._tracked_torrents.items()
                if is_verifying
            }
            if not verifying_torrents:
                return

            # Get current torrent states using optimized monitoring method
            current_states = await self.get_torrents_for_monitoring(verifying_torrents)
            completed_torrents = set()

            for torrent_hash in verifying_torrents:
                current_state = current_states.get(torrent_hash)

                # Check if verification is no longer in progress
                # (not checking, allocating, or moving)
                if current_state in (
                    TorrentState.PAUSED,
                    TorrentState.COMPLETED,
                    TorrentState.SEEDING,
                ):
                    logger.info("Verification completed for torrent %s", torrent_hash)

                    # Call post_process_single_injected_torrent from torrent client
                    try:
                        await self.post_process_single_injected_torrent(torrent_hash)
                    except Exception as e:
                        logger.error("Error processing torrent %s: %s", torrent_hash, e)

                    # Remove from tracking
                    completed_torrents.add(torrent_hash)

            # Update tracked torrents and notify waiters if all done
            async with self._monitor_condition:
                # Remove completed torrents from tracking
                for torrent_hash in completed_torrents:
                    self._tracked_torrents.pop(torrent_hash, None)

                # If no more tracked torrents, notify waiters and stop monitoring
                if not self._tracked_torrents:
                    self._monitor_condition.notify_all()
                    self.monitoring = False
                    # Remove the job from the global scheduler
                    try:
                        self.scheduler.remove_job(self._monitor_job_id)
                        logger.debug("Torrent monitor job removed")
                    except Exception as e:
                        logger.warning("Error removing torrent monitor job: %s", e)

        except Exception as e:
            logger.error("Error checking tracked torrents: %s", e)

    async def track_verification(self, torrent_hash: str) -> None:
        """Start tracking a torrent for verification completion."""
        async with self._monitor_condition:
            # Lazy start monitoring if not already started
            if not self.monitoring:
                await self.start_monitoring()

            # Add to tracked torrents as delayed (False)
            self._tracked_torrents[torrent_hash] = False

        # Start a background task to add torrent after 5 seconds delay
        self.scheduler.add_job(
            self._delayed_add_torrent,
            trigger=DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=5)),
            args=[torrent_hash],
            id=f"delayed_add_{torrent_hash}",
            misfire_grace_time=None,
            replace_existing=True,
        )
        logger.debug("Scheduled tracking verification for torrent %s", torrent_hash)

    async def _delayed_add_torrent(self, torrent_hash: str) -> None:
        """Add torrent to tracking list after 5 seconds delay.

        Note: The 5-second delay is necessary for qBittorrent. After calling
        self._verify_torrent(torrent_hash), qBittorrent doesn't immediately
        start verification. It needs processing time to begin the actual
        verification process, and this processing time cannot be queried.
        The delay is now handled by APScheduler's DateTrigger.
        """
        async with self._monitor_condition:
            # Update status to verifying (True)
            if torrent_hash in self._tracked_torrents:
                self._tracked_torrents[torrent_hash] = True
                logger.debug(
                    "Started tracking verification for torrent %s", torrent_hash
                )

    async def is_tracking(self, torrent_hash: str) -> bool:
        """Check if a torrent is being tracked."""
        async with self._monitor_condition:
            return torrent_hash in self._tracked_torrents

    async def get_tracked_count(self) -> int:
        """Get the number of torrents being tracked."""
        async with self._monitor_condition:
            return len(self._tracked_torrents)

    # endregion


class TorrentClientConfig(msgspec.Struct):
    """Configuration for torrent client connection."""

    # Common fields
    username: str | None = None
    password: str | None = None
    torrents_dir: str | None = None

    # For qBittorrent and rutorrent
    url: str | None = None

    # For Transmission and Deluge
    scheme: str | None = None
    host: str | None = None
    port: int | None = None
    path: str | None = None


def parse_libtc_url(url: str) -> TorrentClientConfig:
    """Parse torrent client URL and extract connection parameters.

    Supported URL formats:
    - transmission+http://127.0.0.1:9091/?torrents_dir=/path
    - rtorrent+http://RUTORRENT_ADDRESS:9380/plugins/rpc/rpc.php
    - deluge://username:password@127.0.0.1:58664
    - qbittorrent+http://username:password@127.0.0.1:8080

    Args:
        url: The torrent client URL to parse

    Returns:
        TorrentClientConfig: Structured configuration object

    Raises:
        ValueError: If the URL scheme is not supported or URL is malformed
    """
    if not url:
        raise ValueError("URL cannot be empty")

    parsed = urlparse(url)
    if not parsed.scheme:
        raise ValueError("URL must have a scheme")

    scheme = parsed.scheme.split("+")

    client = scheme[0]
    torrents_dir = parse_qs(parsed.query).get("torrents_dir", [None])[0]

    # Validate supported client types
    supported_clients = ("transmission", "qbittorrent", "deluge", "rtorrent")
    if client not in supported_clients:
        raise ValueError(
            f"Unsupported client type: {client}. "
            f"Supported clients: {', '.join(supported_clients)}"
        )

    if client == "qbittorrent":
        # qBittorrent: separate auth from URL (uses hostname:port only)
        netloc = (
            f"{parsed.hostname}:{parsed.port}"
            if parsed.port
            else (parsed.hostname or "")
        )
        client_url = f"{scheme[-1]}://{netloc}{parsed.path}"
        return TorrentClientConfig(
            username=unquote(parsed.username) if parsed.username else None,
            password=unquote(parsed.password) if parsed.password else None,
            url=client_url,
            torrents_dir=torrents_dir,
        )
    elif client == "rtorrent":
        # rTorrent: include auth in URL via netloc (user:pass@host:port format)
        client_url = f"{scheme[-1]}://{parsed.netloc}{parsed.path}"
        return TorrentClientConfig(
            url=client_url,
            torrents_dir=torrents_dir,
        )
    else:
        return TorrentClientConfig(
            username=unquote(parsed.username) if parsed.username else None,
            password=unquote(parsed.password) if parsed.password else None,
            scheme=scheme[-1],
            host=parsed.hostname,
            port=parsed.port,
            path=parsed.path,
            torrents_dir=torrents_dir,
        )
