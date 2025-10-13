import asyncio
import posixpath
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlparse

import msgspec
import torf
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .. import config, db, filecompare, logger, scheduler


class PostProcessResult(msgspec.Struct, frozen=False):
    """Result of processing a single injected torrent."""

    status: str = "not_found"  # 'completed', 'partial_kept', 'partial_removed', 'not_found', 'checking', 'error'
    started_downloading: bool = False
    error_message: str | None = None


class FieldSpec(msgspec.Struct):
    """Base field specification for torrent client field extraction."""

    _request_arguments: str | set[str]
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


class TorrentState(Enum):
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

    def __bool__(self) -> bool:
        """Make UNKNOWN state falsy for boolean checks."""
        return self != TorrentState.UNKNOWN


class TorrentConflictError(Exception):
    """Exception raised when torrent cannot coexist with local torrent due to source flag issues."""

    pass


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
    files: list[ClientTorrentFile] = []
    trackers: list[str] = []
    download_dir: str = ""
    state: TorrentState = TorrentState.UNKNOWN  # Torrent state
    existing_target_trackers: list[str] = []
    piece_progress: list[bool] = []  # Piece download status

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

    def __init__(self):
        self.logger = logger.get_logger()

        # Monitoring state
        self.monitoring = False
        # key: torrent_hash, value: is_verifying (False=delayed, True=verifying)
        self._tracked_torrents: dict[str, bool] = {}
        self._monitor_lock = threading.Lock()
        self._torrents_processed_event = asyncio.Event()  # Event to signal when all torrents are processed

        # Job configuration
        self._monitor_job_id = "torrent_monitor"

        # Get global job manager
        self.job_manager = scheduler.get_job_manager()

    # region Abstract Public

    @abstractmethod
    def get_torrents(
        self, torrent_hashes: list[str] | None = None, fields: list[str] | None = None
    ) -> list[ClientTorrentInfo]:
        """Get all torrents from client.

        Args:
            torrent_hashes (list[str] | None): Optional list of torrent hashes to filter.
                If None, all torrents will be returned.
            fields (list[str] | None): List of field names to include in the result.
                If None, all available fields will be included.
                Available fields:
                - hash, name, progress, total_size, files, trackers,
                  download_dir, state, piece_progress

        Returns:
            list[ClientTorrentInfo]: List of torrent information objects.
        """
        pass

    @abstractmethod
    def get_torrent_info(self, torrent_hash: str, fields: list[str] | None) -> ClientTorrentInfo | None:
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
        pass

    @abstractmethod
    def get_torrents_for_monitoring(self, torrent_hashes: set[str]) -> dict[str, TorrentState]:
        """Get torrent states for monitoring (optimized for specific torrents).

        This method is optimized for monitoring specific torrents and should only
        return the minimal required information (hash and state) for efficiency.

        Args:
            torrent_hashes (set[str]): Set of torrent hashes to monitor.

        Returns:
            dict[str, TorrentState]: Mapping of torrent hash to current state.
        """
        pass

    # endregion

    # region Abstract Internal

    @abstractmethod
    def _add_torrent(self, torrent_data, download_dir: str, hash_match: bool) -> str:
        """Add torrent to client, return torrent hash.

        Args:
            torrent_data: Torrent file data.
            download_dir (str): Download directory.
            hash_match (bool): Whether this is a hash match, if True, skip verification.

        Returns:
            str: Torrent hash.
        """
        pass

    @abstractmethod
    def _remove_torrent(self, torrent_hash: str):
        """Remove torrent from client.

        Args:
            torrent_hash (str): Torrent hash.
        """
        pass

    @abstractmethod
    def _rename_torrent(self, torrent_hash: str, old_name: str, new_name: str):
        """Rename entire torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_name (str): Old torrent name.
            new_name (str): New torrent name.
        """
        pass

    @abstractmethod
    def _rename_file(self, torrent_hash: str, old_path: str, new_name: str):
        """Rename file within torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_path (str): Old file path.
            new_name (str): New file name.
        """
        pass

    @abstractmethod
    def _verify_torrent(self, torrent_hash: str):
        """Verify torrent integrity.

        Args:
            torrent_hash (str): Torrent hash.
        """
        pass

    @abstractmethod
    def _process_rename_map(self, torrent_hash: str, base_path: str, rename_map: dict) -> dict:
        """Process rename mapping to adapt to specific torrent client.

        Args:
            torrent_hash (str): Torrent hash.
            base_path (str): Base path for files.
            rename_map (dict): Original rename mapping.

        Returns:
            dict: Processed rename mapping.
        """
        pass

    @abstractmethod
    def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from client.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bytes | None: Torrent file data, or None if not available.
        """
        pass

    @abstractmethod
    def _resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bool: True if successful, False otherwise.
        """
        pass

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
            database = db.get_database()
            await database.clear_client_torrents_cache()

            if not torrents:
                self.logger.debug("No torrents provided for cache rebuild")
                return

            # Batch save to database
            await database.batch_save_client_torrents(torrents)
            self.logger.success(f"Cached {len(torrents)} torrents to database")

        except Exception as e:
            self.logger.warning(f"Failed to rebuild cache: {e}")

    async def rebuild_client_torrents_cache_incremental(self, torrents: list[ClientTorrentInfo]):
        """Rebuild database cache with provided torrents incrementally (one by one).

        This method provides the same functionality as rebuild_client_torrents_cache,
        but is designed for background execution with better async performance.
        It first batch deletes torrents that no longer exist, then updates/inserts
        torrents one by one to allow other async operations to interleave.

        Args:
            torrents (list[ClientTorrentInfo]): List of torrents to rebuild cache with.
        """
        try:
            database = db.get_database()

            if not torrents:
                self.logger.debug("No torrents provided for cache sync")
                # Clear all cached torrents if the new list is empty
                await database.clear_client_torrents_cache()
                return

            # Get current cached torrent hashes
            cached_hashes = await database.get_all_cached_torrent_hashes()
            new_hashes = {torrent.hash for torrent in torrents}

            # Step 1: Batch delete torrents that no longer exist in client
            torrents_to_delete = cached_hashes - new_hashes
            if torrents_to_delete:
                await database.delete_client_torrents(torrents_to_delete)
                self.logger.debug(f"Deleted {len(torrents_to_delete)} torrents from cache")

            # Step 2: Update/insert torrents one by one
            for torrent in torrents:
                await database.save_client_torrent_info(torrent)

            self.logger.success(f"Synced {len(torrents)} torrents to database cache")

        except Exception as e:
            self.logger.warning(f"Failed to sync cache: {e}")

    async def refresh_client_torrents_cache(self) -> None:
        """Refresh local client_torrents database cache with incremental updates.

        This method updates the client_torrents table in database with torrent information:
        1. Get basic info for all torrents (hash, name, download_dir)
        2. Get all cached torrents from database
        3. Compare in Python to find modified torrents and deleted torrents
        4. Fetch only modified torrents from client API
        5. Update client_torrents cache in database with modified torrents
        6. Delete torrents that no longer exist in client from database
        """
        try:
            database = db.get_database()

            # Step 1: Get basic info for all torrents (minimal API call)
            basic_torrents = self.get_torrents(fields=["hash", "name", "download_dir"])
            if not basic_torrents:
                self.logger.debug("No torrents found in client")
                return

            # Step 2: Get all cached torrents in one query (optimized for batch comparison)
            cached_torrents = await database.get_all_client_torrents_basic()

            # Step 3: Check which torrents need to be updated
            torrents_to_fetch = [
                torrent.hash
                for torrent in basic_torrents
                if cached_torrents.get(torrent.hash) != (torrent.name, torrent.download_dir)
            ]

            # Find torrents that exist in cache but not in client
            torrents_to_delete = cached_torrents.keys() - {torrent.hash for torrent in basic_torrents}

            # Step 4: Fetch modified/new torrents from client API
            if torrents_to_fetch:
                # Get full info for modified torrents only
                modified_torrents = self.get_torrents(
                    torrent_hashes=torrents_to_fetch,
                    fields=["hash", "name", "total_size", "files", "trackers", "download_dir"],
                )

                # Step 5: Update database
                if modified_torrents:
                    await database.batch_save_client_torrents(modified_torrents)
                    self.logger.debug(f"Updated {len(modified_torrents)} modified torrents in database cache")
            else:
                self.logger.debug("No modified torrents found, database cache is up to date")

            # Step 6: Delete torrents that no longer exist in client
            if torrents_to_delete:
                await database.delete_client_torrents(torrents_to_delete)
                self.logger.debug(
                    f"Deleted {len(torrents_to_delete)} torrents from database cache (removed from client)"
                )

        except Exception as e:
            self.logger.error(f"Error refreshing database cache: {e}")

    @staticmethod
    async def get_file_matched_torrents(target_file_size: int, fname_keywords: list[str]) -> list[ClientTorrentInfo]:
        """Get torrents matching file size and name keywords, return ClientTorrentInfo objects.

        This is a wrapper around db.search_torrent_by_file_match that processes the raw
        database rows into ClientTorrentInfo objects.

        Args:
            target_file_size: Target file size to match.
            fname_keywords: List of keywords that should appear in file path.

        Returns:
            List of ClientTorrentInfo objects.
        """
        database = db.get_database()
        rows = await database.search_torrent_by_file_match(target_file_size, fname_keywords)

        # Group results by torrent hash and build ClientTorrentInfo directly
        torrents_dict = {}
        for row in rows:
            torrent_hash = row["hash"]

            # Initialize ClientTorrentInfo if first time seeing this hash
            if torrent_hash not in torrents_dict:
                torrents_dict[torrent_hash] = ClientTorrentInfo(
                    hash=torrent_hash,
                    name=row["name"],
                    download_dir=row["download_dir"],
                    total_size=row["total_size"],
                    trackers=msgspec.json.decode(row["trackers"]) if row["trackers"] else [],
                    files=[],
                )

            # Add file to this torrent's file list
            torrents_dict[torrent_hash].files.append(
                ClientTorrentFile(
                    name=row["file_path"],
                    size=row["file_size"],
                    progress=1.0,  # Assume complete for cached torrents
                )
            )

        return list(torrents_dict.values())

    def get_single_torrent(self, infohash: str, target_trackers: list[str]) -> ClientTorrentInfo | None:
        """Get single torrent by infohash with existing trackers information.

        This method follows the same logic as get_filtered_torrents but for a single torrent.
        It finds the torrent by infohash and determines which target trackers this content
        already exists on by checking all torrents with the same content name.

        Args:
            infohash (str): Torrent infohash.
            target_trackers (list[str]): List of target tracker names.

        Returns:
            ClientTorrentInfo | None: Torrent information with existing_trackers, or None if not found.
        """
        try:
            # Find torrent by infohash
            target_torrent = self.get_torrent_info(
                infohash,
                fields=["hash", "name", "total_size", "files", "trackers", "download_dir"],
            )

            if not target_torrent:
                self.logger.debug(f"Torrent with infohash {infohash} not found in client torrent list")
                return None

            self.logger.debug(f"Found torrent: {target_torrent.name} ({infohash})")

            # Check if torrent meets basic conditions (same as get_filtered_torrents)
            check_trackers_list = config.cfg.global_config.check_trackers
            if check_trackers_list and not any(
                any(check_str in url for check_str in check_trackers_list) for url in target_torrent.trackers
            ):
                self.logger.debug(f"Torrent {target_torrent.name} filtered out: tracker not in check_trackers list")
                self.logger.debug(f"Torrent trackers: {target_torrent.trackers}")
                self.logger.debug(f"Required trackers: {check_trackers_list}")
                return None

            # Filter MP3 files (based on configuration)
            if config.cfg.global_config.exclude_mp3:
                has_mp3 = any(posixpath.splitext(file.name)[1].lower() == ".mp3" for file in target_torrent.files)
                if has_mp3:
                    self.logger.debug(
                        f"Torrent {target_torrent.name} filtered out: contains MP3 files (exclude_mp3=true)"
                    )
                    return None

            # Check if torrent contains music files (if check_music_only is enabled)
            if config.cfg.global_config.check_music_only:
                has_music = any(filecompare.is_music_file(file.name) for file in target_torrent.files)
                if not has_music:
                    self.logger.debug(
                        f"Torrent {target_torrent.name} filtered out: no music files found (check_music_only=true)"
                    )
                    file_extensions = [posixpath.splitext(f.name)[1].lower() for f in target_torrent.files]
                    self.logger.debug(f"File extensions in torrent: {file_extensions}")
                    return None

            # Collect which target trackers this content already exists on
            # (by checking all torrents with the same content name)
            existing_trackers = set()
            for torrent in self.get_torrents(fields=["name", "trackers"]):
                if torrent.name == target_torrent.name:
                    for tracker_url in torrent.trackers:
                        for target_tracker in target_trackers:
                            if target_tracker in tracker_url:
                                existing_trackers.add(target_tracker)

            # Return torrent info with existing_trackers
            return ClientTorrentInfo(
                hash=target_torrent.hash,
                name=target_torrent.name,
                total_size=target_torrent.total_size,
                files=target_torrent.files,
                trackers=target_torrent.trackers,
                download_dir=target_torrent.download_dir,
                existing_target_trackers=list(existing_trackers),
            )

        except Exception as e:
            self.logger.error("Error retrieving single torrent: %s", e)
            return None

    async def get_filtered_torrents(self, target_trackers: list[str]) -> dict[str, ClientTorrentInfo]:
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
            dict[str, ClientTorrentInfo]: Dictionary mapping torrent name to torrent info.
        """
        try:
            # Get all torrents with required fields
            torrents = list(
                self.get_torrents(fields=["hash", "name", "total_size", "files", "trackers", "download_dir"])
            )

            # Rebuild cache with all torrents (run in background)
            self.job_manager.scheduler.add_job(
                self.rebuild_client_torrents_cache_incremental,
                trigger=DateTrigger(),
                args=[torrents],
                id="rebuild_cache",
                replace_existing=True,
                max_instances=1,
            )

            # Step 1: Group by content name, collect which trackers each content exists on
            content_tracker_mapping = {}  # {content_name: set(trackers)}
            valid_torrents: dict[str, ClientTorrentInfo] = {}  # Torrents that meet basic conditions

            for torrent in torrents:
                # Only process torrents that meet CHECK_TRACKERS conditions
                check_trackers_list = config.cfg.global_config.check_trackers
                if check_trackers_list and not any(
                    any(check_str in url for check_str in check_trackers_list) for url in torrent.trackers
                ):
                    continue

                # Filter MP3 files (based on configuration)
                if config.cfg.global_config.exclude_mp3:
                    has_mp3 = any(posixpath.splitext(file.name)[1].lower() == ".mp3" for file in torrent.files)
                    if has_mp3:
                        continue

                # Check if torrent contains music files (if check_music_only is enabled)
                if config.cfg.global_config.check_music_only:
                    has_music = any(filecompare.is_music_file(file.name) for file in torrent.files)
                    if not has_music:
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
                        len(torrent.files) == len(existing.files) and torrent.total_size < existing.total_size
                    ):
                        valid_torrents[content_name] = torrent

            # Step 2: Filter out content that already exists on all target trackers
            filtered_torrents = {}
            target_tracker_set = set(target_trackers)

            for content_name, torrent in valid_torrents.items():
                existing_trackers = content_tracker_mapping.get(content_name, set())

                # If this content already exists on all target trackers, skip
                if target_tracker_set.issubset(existing_trackers):
                    self.logger.debug(
                        f"Skipping {content_name}: already exists on all target trackers {existing_trackers}"
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
                    existing_target_trackers=list(existing_trackers),  # Record existing target trackers
                )

            return filtered_torrents

        except Exception as e:
            self.logger.error("Error retrieving torrents: %s", e)
            return {}

    def get_torrent_object(self, torrent_hash: str) -> torf.Torrent | None:
        """Get torrent object from client by hash.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            torf.Torrent | None: Torrent object, or None if not available.
        """
        try:
            torrent_data = self._get_torrent_data(torrent_hash)
            if torrent_data:
                return torf.Torrent.read_stream(torrent_data)
            return None
        except Exception as e:
            self.logger.error(f"Error getting torrent object for hash {torrent_hash}: {e}")
            return None

    # endregion

    # region Torrent Injection

    def inject_torrent(
        self, torrent_data, download_dir: str, local_torrent_name: str, rename_map: dict, hash_match: bool
    ) -> tuple[bool, bool]:
        """Inject torrent into client (includes complete logic).

        Derived classes only need to implement specific client operation methods.

        Args:
            torrent_data: Torrent file data.
            download_dir (str): Download directory.
            local_torrent_name (str): Local torrent name.
            rename_map (dict): File rename mapping.
            hash_match (bool): Whether this is a hash match, if True, skip verification.

        Returns:
            tuple[bool, bool]: (success, verified) where:
                - success: True if injection successful, False otherwise
                - verified: True if verification was performed, False otherwise
        """
        # Flag to track if rename map has been processed
        rename_map_processed = False

        # Add torrent to client
        try:
            torrent_hash = self._add_torrent(torrent_data, download_dir, hash_match)
        except TorrentConflictError as e:
            self.logger.error(f"Torrent injection failed due to conflict: {e}")
            self.logger.error(
                "This usually happens because the source flag of the torrent to be injected is incorrect, "
                "which generally occurs on trackers that do not enforce source flag requirements."
            )
            raise

        max_retries = 8
        for attempt in range(max_retries):
            try:
                # Get current torrent name
                torrent_info = self.get_torrent_info(torrent_hash, ["name"])
                if torrent_info is None or torrent_info.name is None:
                    self.logger.warning(f"Failed to get torrent info for {torrent_hash}, skipping")
                    continue
                current_name = torrent_info.name

                # Rename entire torrent
                if current_name != local_torrent_name:
                    self._rename_torrent(torrent_hash, current_name, local_torrent_name)
                    self.logger.debug(f"Renamed torrent from {current_name} to {local_torrent_name}")

                if not config.cfg.linking.enable_linking:
                    # Process rename map only once
                    if not rename_map_processed:
                        rename_map = self._process_rename_map(
                            torrent_hash=torrent_hash, base_path=local_torrent_name, rename_map=rename_map
                        )
                        rename_map_processed = True

                    # Rename files
                    if rename_map:
                        for torrent_file_name, local_file_name in rename_map.items():
                            self._rename_file(
                                torrent_hash,
                                torrent_file_name,
                                local_file_name,
                            )
                            self.logger.debug(f"Renamed torrent file {torrent_file_name} to {local_file_name}")

                # Verify torrent (if renaming was performed or not hash match for non-Transmission clients)
                should_verify = (
                    current_name != local_torrent_name
                    or bool(rename_map)
                    or (not hash_match and self.__class__.__name__ != "TransmissionClient")
                )
                if should_verify:
                    self.logger.debug("Verifying torrent after renaming")
                    time.sleep(1)
                    self._verify_torrent(torrent_hash)

                self.logger.success("Torrent injected successfully")
                return True, should_verify
            except Exception as e:
                if attempt < max_retries - 1:
                    self.logger.debug(f"Error injecting torrent: {e}, retrying ({attempt + 1}/{max_retries})...")
                    time.sleep(2)
                else:
                    self.logger.error(f"Failed to inject torrent after {max_retries} attempts: {e}")
                    return False, False

        # This should never be reached, but just in case
        return False, False

    def reverse_inject_torrent(
        self, matched_torrents: list[ClientTorrentInfo], new_name: str, reverse_rename_map: dict
    ) -> dict[str, bool]:
        """Reverse inject logic: rename all local torrents to match incoming torrent format.

        Args:
            matched_torrents (list[ClientTorrentInfo]): List of local torrents to rename.
            new_name (str): New torrent name to match incoming torrent.
            reverse_rename_map (dict): File rename mapping from local to incoming format.

        Returns:
            dict[str, bool]: Dictionary mapping torrent hash to success status.
        """
        results = {}

        for matched_torrent in matched_torrents:
            torrent_hash = matched_torrent.hash
            try:
                # Get current torrent name
                torrent_info = self.get_torrent_info(torrent_hash, ["name"])
                if torrent_info is None or torrent_info.name is None:
                    self.logger.warning(f"Failed to get torrent info for {torrent_hash}, skipping")
                    continue
                current_name = torrent_info.name

                # Rename entire torrent
                if current_name != new_name:
                    self._rename_torrent(torrent_hash, current_name, new_name)
                    self.logger.debug(f"Renamed torrent {torrent_hash} from {current_name} to {new_name}")

                # Rename files according to reverse rename map
                if reverse_rename_map:
                    for local_file_name, incoming_file_name in reverse_rename_map.items():
                        self._rename_file(
                            torrent_hash,
                            local_file_name,
                            incoming_file_name,
                        )
                        self.logger.debug(
                            f"Renamed file {local_file_name} to {incoming_file_name} in torrent {torrent_hash}"
                        )

                # Verify torrent after renaming
                if current_name != new_name or reverse_rename_map:
                    self.logger.debug(f"Verifying torrent {torrent_hash} after reverse renaming")
                    self._verify_torrent(torrent_hash)

                results[str(torrent_hash)] = True
                self.logger.success(f"Reverse injection completed successfully for torrent {torrent_hash}")

            except Exception as e:
                results[str(torrent_hash)] = False
                self.logger.error(f"Failed to reverse inject torrent {torrent_hash}: {e}")

        return results

    # endregion

    # region Post-Processing

    async def post_process_single_injected_torrent(self, matched_torrent_hash: str) -> PostProcessResult:
        """Post-process a single injected torrent to determine its status and take appropriate action.

        Args:
            matched_torrent_hash: The hash of the matched torrent to process

        Returns:
            PostProcessResult: Processing result with status, started_downloading flag, and error_message
        """
        result = PostProcessResult()

        try:
            database = db.get_database()

            self.logger.debug(f"Checking matched torrent: {matched_torrent_hash}")

            # Check if matched torrent exists in client
            matched_torrent = self.get_torrent_info(
                matched_torrent_hash, ["state", "name", "progress", "files", "piece_progress"]
            )
            if not matched_torrent:
                self.logger.debug(f"Matched torrent {matched_torrent_hash} not found in client, skipping")
                result.status = "not_found"
                return result

            # Skip if matched torrent is checking
            if matched_torrent.state == TorrentState.CHECKING:
                self.logger.debug(f"Matched torrent {matched_torrent.name} is checking, skipping")
                result.status = "checking"
                return result

            # If matched torrent is 100% complete, start downloading
            if matched_torrent.progress == 1.0:
                self.logger.info(f"Matched torrent {matched_torrent.name} is 100% complete")
                # Check if auto-start is enabled
                if config.cfg.global_config.auto_start_torrents:
                    # Start downloading the matched torrent
                    self._resume_torrent(matched_torrent.hash)
                    self.logger.success(f"Started downloading matched torrent: {matched_torrent.name}")
                    result.started_downloading = True
                else:
                    self.logger.info("Auto-start disabled, torrent will remain paused")
                    result.started_downloading = False
                # Mark as checked since it's 100% complete
                await database.update_scan_result_checked(matched_torrent_hash, True)
                result.status = "completed"
            # If matched torrent is not 100% complete, check file progress patterns
            else:
                self.logger.debug(
                    f"Matched torrent {matched_torrent.name} not 100% complete "
                    f"({matched_torrent.progress * 100:.1f}%), checking file patterns"
                )

                # Analyze file progress patterns
                if filecompare.should_keep_partial_torrent(matched_torrent):
                    self.logger.debug(f"Keeping partial torrent {matched_torrent.name} - valid pattern")
                    # Mark as checked since we're keeping the partial torrent
                    await database.update_scan_result_checked(matched_torrent_hash, True)
                    result.status = "partial_kept"
                else:
                    if config.cfg.linking.link_type in ["reflink", "reflink_or_copy"]:
                        # Keep partial torrent explicitly due to reflink being enabled
                        self.logger.info(
                            f"Keeping partial torrent {matched_torrent.name} - kept due to reflink enabled"
                        )
                        await database.update_scan_result_checked(matched_torrent_hash, True)
                        result.status = "partial_kept"
                    else:
                        self.logger.warning(f"Removing torrent {matched_torrent.name} - failed validation")
                        self._remove_torrent(matched_torrent.hash)
                        # Clear matched torrent information from database
                        await database.clear_matched_torrent_info(matched_torrent_hash)
                        result.status = "partial_removed"

        except Exception as e:
            self.logger.error(f"Error processing torrent {matched_torrent_hash}: {e}")
            result.status = "error"
            result.error_message = str(e)

        return result

    # endregion

    # region Monitoring

    async def start_monitoring(self) -> None:
        """Start the background monitoring service."""
        if not self.monitoring:
            self.monitoring = True

            # Add scheduled job for monitoring to the global scheduler
            self.job_manager.scheduler.add_job(
                self._check_tracked_torrents,
                trigger=IntervalTrigger(seconds=1),
                id=self._monitor_job_id,
                name="Torrent Monitor",
                max_instances=1,  # Prevent overlapping executions
                misfire_grace_time=60,
                coalesce=True,
                replace_existing=True,
            )

            self.logger.info("Torrent monitoring started")

    async def wait_for_monitoring_completion(self) -> None:
        """Wait for monitoring to complete and all tracked torrents to finish processing."""
        if not self.monitoring:
            return

        self.monitoring = False

        # Wait for all tracked torrents to be processed
        if self._tracked_torrents:
            self.logger.info(f"Waiting for {len(self._tracked_torrents)} tracked torrents to complete...")

            # Clear the event to ensure we wait for current torrents
            self._torrents_processed_event.clear()

            # Check if all tasks are already empty after clearing the event
            if not self._tracked_torrents:
                self._torrents_processed_event.set()
                self.logger.info("All tracked torrents already completed")
            else:
                try:
                    # Wait for the event to be set (all torrents processed) with timeout
                    await asyncio.wait_for(self._torrents_processed_event.wait(), timeout=30.0)
                    self.logger.info("All tracked torrents completed")
                except TimeoutError:
                    self.logger.warning(f"Timeout waiting for {len(self._tracked_torrents)} torrents to complete")

        self.logger.info("Torrent monitoring stopped")

    async def _check_tracked_torrents(self) -> None:
        """Check tracked torrents for verification completion.

        This method is called by APScheduler at regular intervals.
        """
        if not self._tracked_torrents:
            return

        try:
            # Only check torrents that are in verifying state (True)
            verifying_torrents = {th for th, is_verifying in self._tracked_torrents.items() if is_verifying}
            if not verifying_torrents:
                return

            # Get current torrent states using optimized monitoring method
            current_states = self.get_torrents_for_monitoring(verifying_torrents)
            completed_torrents = set()

            for torrent_hash in self._tracked_torrents:
                current_state = current_states.get(torrent_hash)

                # Check if verification is no longer in progress
                # (not checking, allocating, or moving)
                if current_state in [
                    TorrentState.PAUSED,
                    TorrentState.COMPLETED,
                ]:
                    self.logger.info(f"Verification completed for torrent {torrent_hash}")

                    # Call post_process_single_injected_torrent from torrent client
                    try:
                        await self.post_process_single_injected_torrent(torrent_hash)
                    except Exception as e:
                        self.logger.error(f"Error processing torrent {torrent_hash}: {e}")

                    # Remove from tracking
                    completed_torrents.add(torrent_hash)

            # Check tracked torrents for completion
            with self._monitor_lock:
                # Remove completed torrents from tracking
                for torrent_hash in completed_torrents:
                    self._tracked_torrents.pop(torrent_hash, None)

                # If no more tracked torrents, set the event
                if not self._tracked_torrents:
                    self._torrents_processed_event.set()
                    self.monitoring = False
                    # Remove the job from the global scheduler
                    try:
                        self.job_manager.scheduler.remove_job(self._monitor_job_id)
                        self.logger.info("Torrent monitoring stopped")
                    except Exception as e:
                        self.logger.warning(f"Error removing torrent monitor job: {e}")

        except Exception as e:
            self.logger.error(f"Error checking tracked torrents: {e}")

    async def track_verification(self, torrent_hash: str) -> None:
        """Start tracking a torrent for verification completion."""
        with self._monitor_lock:
            # Lazy start monitoring if not already started
            if not self.monitoring:
                await self.start_monitoring()

            # Add to tracked torrents as delayed (False)
            self._tracked_torrents[torrent_hash] = False

        # Start a background task to add torrent after 5 seconds delay
        self.job_manager.scheduler.add_job(
            self._delayed_add_torrent,
            trigger=DateTrigger(run_date=datetime.now(UTC) + timedelta(seconds=5)),
            args=[torrent_hash],
            id=f"delayed_add_{torrent_hash}",
            replace_existing=True,
        )
        self.logger.debug(f"Scheduled tracking verification for torrent {torrent_hash}")

    async def _delayed_add_torrent(self, torrent_hash: str) -> None:
        """Add torrent to tracking list after 5 seconds delay.

        Note: The 5-second delay is necessary for qBittorrent. After calling
        self._verify_torrent(torrent_hash), qBittorrent doesn't immediately start verification.
        It needs processing time to begin the actual verification process, and this processing
        time cannot be queried. The delay is now handled by APScheduler's DateTrigger.
        """
        with self._monitor_lock:
            # Update status to verifying (True)
            if torrent_hash in self._tracked_torrents:
                self._tracked_torrents[torrent_hash] = True
                self.logger.debug(f"Started tracking verification for torrent {torrent_hash}")

    def stop_tracking(self, torrent_hash: str) -> None:
        """Stop tracking a torrent."""
        with self._monitor_lock:
            self._tracked_torrents.pop(torrent_hash, None)
            self.logger.debug(f"Stopped tracking torrent {torrent_hash}")

    def is_tracking(self, torrent_hash: str) -> bool:
        """Check if a torrent is being tracked."""
        with self._monitor_lock:
            return torrent_hash in self._tracked_torrents

    def get_tracked_count(self) -> int:
        """Get the number of torrents being tracked."""
        with self._monitor_lock:
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


def parse_libtc_url(url: str) -> TorrentClientConfig:
    """Parse torrent client URL and extract connection parameters.

    Supported URL formats:
    - transmission+http://127.0.0.1:9091/?torrents_dir=/path
    - rutorrent+http://RUTORRENT_ADDRESS:9380/plugins/rpc/rpc.php
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
    netloc = parsed.netloc

    # Extract username and password if present
    username = None
    password = None
    if "@" in netloc:
        auth, netloc = netloc.rsplit("@", 1)
        username, password = auth.split(":", 1)

    client = scheme[0]

    # Validate supported client types
    supported_clients = ["transmission", "qbittorrent", "deluge", "rutorrent"]
    if client not in supported_clients:
        raise ValueError(f"Unsupported client type: {client}. Supported clients: {', '.join(supported_clients)}")

    if client in ["qbittorrent", "rutorrent"]:
        # For qBittorrent and rutorrent, use URL format
        client_url = f"{scheme[1]}://{netloc}{parsed.path}"
        return TorrentClientConfig(
            username=username,
            password=password,
            url=client_url,
            torrents_dir=dict(parse_qsl(parsed.query)).get("torrents_dir"),
        )
    else:
        # For Transmission and Deluge, use host:port format
        host, port_str = netloc.split(":")
        port = int(port_str)

        # Extract additional query parameters
        query_params = dict(parse_qsl(parsed.query))

        return TorrentClientConfig(
            username=username,
            password=password,
            scheme=scheme[-1],
            host=host,
            port=port,
            torrents_dir=query_params.get("torrents_dir"),
        )
