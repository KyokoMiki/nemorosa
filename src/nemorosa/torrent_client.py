"""
Torrent Client Module

This module provides a unified interface for different torrent clients including
Transmission, qBittorrent, and Deluge.
"""

import asyncio
import base64
import json
import os
import posixpath
import re
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from enum import Enum
from typing import Any
from urllib.parse import parse_qsl, urlparse

import deluge_client
import msgspec
import qbittorrentapi
import torf
import transmission_rpc
from apscheduler.triggers.interval import IntervalTrigger
from transmission_rpc.constants import RpcMethod

from . import config, db, filecompare, logger, scheduler


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


# State mapping tables for different torrent clients
TRANSMISSION_STATE_MAPPING = {
    "stopped": TorrentState.PAUSED,
    "check pending": TorrentState.CHECKING,
    "checking": TorrentState.CHECKING,
    "download pending": TorrentState.QUEUED,
    "downloading": TorrentState.DOWNLOADING,
    "seed pending": TorrentState.QUEUED,
    "seeding": TorrentState.SEEDING,
}

QBITTORRENT_STATE_MAPPING = {
    "error": TorrentState.ERROR,
    "missingFiles": TorrentState.ERROR,
    "uploading": TorrentState.SEEDING,
    "pausedUP": TorrentState.PAUSED,
    "stoppedUP": TorrentState.PAUSED,
    "queuedUP": TorrentState.QUEUED,
    "stalledUP": TorrentState.SEEDING,
    "checkingUP": TorrentState.CHECKING,
    "forcedUP": TorrentState.SEEDING,
    "allocating": TorrentState.ALLOCATING,
    "downloading": TorrentState.DOWNLOADING,
    "metaDL": TorrentState.METADATA_DOWNLOADING,
    "forcedMetaDL": TorrentState.METADATA_DOWNLOADING,
    "pausedDL": TorrentState.PAUSED,
    "stoppedDL": TorrentState.PAUSED,
    "queuedDL": TorrentState.QUEUED,
    "forcedDL": TorrentState.DOWNLOADING,
    "stalledDL": TorrentState.DOWNLOADING,
    "checkingDL": TorrentState.CHECKING,
    "checkingResumeData": TorrentState.CHECKING,
    "moving": TorrentState.MOVING,
    "unknown": TorrentState.UNKNOWN,
}

DELUGE_STATE_MAPPING = {
    "Error": TorrentState.ERROR,
    "Paused": TorrentState.PAUSED,
    "Queued": TorrentState.QUEUED,
    "Checking": TorrentState.CHECKING,
    "Downloading": TorrentState.DOWNLOADING,
    "Downloading Metadata": TorrentState.METADATA_DOWNLOADING,
    "Finished": TorrentState.COMPLETED,
    "Seeding": TorrentState.SEEDING,
    "Allocating": TorrentState.ALLOCATING,
    "Moving": TorrentState.MOVING,
    "Active": TorrentState.SEEDING,
    "Inactive": TorrentState.PAUSED,
}


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
        self._monitoring = False
        # key: torrent_hash, value: is_verifying (False=delayed, True=verifying)
        self._tracked_torrents: dict[str, bool] = {}
        self._monitor_lock = threading.Lock()
        self._torrents_processed_event = asyncio.Event()  # Event to signal when all torrents are processed

        # Job configuration
        self._monitor_job_id = "torrent_monitor"

        # Get global job manager
        self.job_manager = scheduler.get_job_manager()

    @abstractmethod
    def get_torrents(self, fields: list[str] | None) -> list[ClientTorrentInfo]:
        """Get all torrents from client.

        Args:
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

    @abstractmethod
    def resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bool: True if successful, False otherwise.
        """
        pass

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
                fields=["hash", "name", "progress", "total_size", "files", "trackers", "download_dir", "state"],
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
                progress=target_torrent.progress,
                total_size=target_torrent.total_size,
                files=target_torrent.files,
                trackers=target_torrent.trackers,
                download_dir=target_torrent.download_dir,
                state=target_torrent.state,
                existing_target_trackers=list(existing_trackers),
            )

        except Exception as e:
            self.logger.error("Error retrieving single torrent: %s", e)
            return None

    def get_filtered_torrents(self, target_trackers: list[str]) -> dict[str, ClientTorrentInfo]:
        """Get filtered torrent list.

        This method contains common filtering logic, derived classes only need to implement get_torrents().

        New logic:
        1. Group by torrent content (same name considered same content)
        2. Check which target trackers each content already exists on
        3. Only return content that doesn't exist on all target trackers

        Args:
            target_trackers (list[str]): List of target tracker names.

        Returns:
            dict[str, dict]: Dictionary mapping torrent name to torrent info.
        """
        try:
            # Get all torrents with required fields
            torrents = list(
                self.get_torrents(
                    fields=["hash", "name", "progress", "total_size", "files", "trackers", "download_dir", "state"]
                )
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
                    progress=torrent.progress,
                    total_size=torrent.total_size,
                    files=torrent.files,
                    trackers=torrent.trackers,
                    download_dir=torrent.download_dir,
                    state=torrent.state,
                    existing_target_trackers=list(existing_trackers),  # Record existing target trackers
                )

            return filtered_torrents

        except Exception as e:
            self.logger.error("Error retrieving torrents: %s", e)
            return {}

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
                    or (not hash_match and not isinstance(self, TransmissionClient))
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

    # ===== The following methods need to be implemented by derived classes =====

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

    def get_torrent_object(self, torrent_hash: str) -> "torf.Torrent | None":
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

    def process_single_injected_torrent(self, matched_torrent_hash: str) -> dict:
        """Process a single injected torrent to determine its status and take appropriate action.

        Args:
            matched_torrent_hash: The hash of the matched torrent to process

        Returns:
            dict: Statistics about the processing result with keys:
                - status: 'completed', 'partial_kept', 'partial_removed', 'not_found', 'checking', 'error'
                - started_downloading: bool indicating if download was started
                - error_message: str containing error details if status is 'error'
        """
        stats = {"status": "not_found", "started_downloading": False, "error_message": None}

        try:
            database = db.get_database()

            self.logger.debug(f"Checking matched torrent: {matched_torrent_hash}")

            # Check if matched torrent exists in client
            matched_torrent = self.get_torrent_info(
                matched_torrent_hash, ["state", "name", "progress", "files", "piece_progress"]
            )
            if not matched_torrent:
                self.logger.debug(f"Matched torrent {matched_torrent_hash} not found in client, skipping")
                stats["status"] = "not_found"
                return stats

            # Skip if matched torrent is checking
            if matched_torrent.state == TorrentState.CHECKING:
                self.logger.debug(f"Matched torrent {matched_torrent.name} is checking, skipping")
                stats["status"] = "checking"
                return stats

            # If matched torrent is 100% complete, start downloading
            if matched_torrent.progress == 1.0:
                self.logger.info(f"Matched torrent {matched_torrent.name} is 100% complete, starting download")
                # Start downloading the matched torrent
                self.resume_torrent(matched_torrent.hash)
                self.logger.success(f"Started downloading matched torrent: {matched_torrent.name}")
                # Mark as checked since it's 100% complete
                database.update_scan_result_checked(matched_torrent_hash, True)
                stats["status"] = "completed"
                stats["started_downloading"] = True
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
                    database.update_scan_result_checked(matched_torrent_hash, True)
                    stats["status"] = "partial_kept"
                else:
                    self.logger.warning(f"Removing torrent {matched_torrent.name} - failed validation")
                    self._remove_torrent(matched_torrent.hash)
                    # Clear matched torrent information from database
                    database.clear_matched_torrent_info(matched_torrent_hash)
                    stats["status"] = "partial_removed"

        except Exception as e:
            self.logger.error(f"Error processing torrent {matched_torrent_hash}: {e}")
            stats["status"] = "error"
            stats["error_message"] = str(e)

        return stats

    @abstractmethod
    def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from client.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bytes | None: Torrent file data, or None if not available.
        """
        pass

    # ===== Monitoring Methods =====

    async def start_monitoring(self) -> None:
        """Start the background monitoring service."""
        if not self._monitoring:
            self._monitoring = True

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

            # Ensure scheduler is running
            if not self.job_manager.scheduler.running:
                self.job_manager.scheduler.start()
                self.logger.debug("Started global scheduler for torrent monitoring")

            self.logger.info("Torrent monitoring started with global scheduler")

    async def stop_monitoring(self) -> None:
        """Stop the background monitoring service and wait for all tracked torrents to complete."""
        if not self._monitoring:
            return

        self._monitoring = False

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

        # Remove the job from the global scheduler
        try:
            self.job_manager.scheduler.remove_job(self._monitor_job_id)
            self.logger.info("Torrent monitoring stopped")
        except Exception as e:
            self.logger.warning(f"Error removing torrent monitor job: {e}")

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

            # Check tracked torrents for completion
            with self._monitor_lock:
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

                        # Call process_single_injected_torrent from torrent client
                        try:
                            self.process_single_injected_torrent(torrent_hash)
                        except Exception as e:
                            self.logger.error(f"Error processing torrent {torrent_hash}: {e}")

                        # Remove from tracking
                        completed_torrents.add(torrent_hash)

                # Remove completed torrents from tracking
                for torrent_hash in completed_torrents:
                    self._tracked_torrents.pop(torrent_hash, None)

                # If no more tracked torrents, set the event
                if not self._tracked_torrents:
                    self._torrents_processed_event.set()

        except Exception as e:
            self.logger.error(f"Error checking tracked torrents: {e}")

    async def track_verification(self, torrent_hash: str) -> None:
        """Start tracking a torrent for verification completion."""
        with self._monitor_lock:
            # Lazy start monitoring if not already started
            if not self._monitoring:
                await self.start_monitoring()

            # Add to tracked torrents as delayed (False)
            self._tracked_torrents[torrent_hash] = False

        # Start a background task to add torrent after 5 seconds delay
        asyncio.create_task(self._delayed_add_torrent(torrent_hash))
        self.logger.debug(f"Scheduled tracking verification for torrent {torrent_hash}")

    async def _delayed_add_torrent(self, torrent_hash: str) -> None:
        """Add torrent to tracking list after 5 seconds delay."""
        # Wait for 5 seconds - this delay is necessary for qBittorrent:
        # After calling self._verify_torrent(torrent_hash), qBittorrent doesn't immediately
        # start verification. It needs processing time to begin the actual verification
        # process, and this processing time cannot be queried. Therefore, we hard-code
        # a 5-second wait to ensure the verification has started before we begin monitoring.
        # The "Verifying torrent after renaming" wait is also added for qBittorrent compatibility.
        await asyncio.sleep(5)
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


class TransmissionClient(TorrentClient):
    """Transmission torrent client implementation."""

    def __init__(self, url: str):
        super().__init__()
        config = parse_libtc_url(url)
        self.torrents_dir = config.torrents_dir or "/config/torrents"

        self.client = transmission_rpc.Client(
            host=config.host or "localhost",
            port=config.port or 9091,
            username=config.username,
            password=config.password,
        )

        # Field configuration: field_name -> (Transmission arguments, extractor function)
        self.field_config: dict[str, tuple[set[str], Callable[[Any], Any]]] = {
            "hash": ({"hashString"}, lambda t: t.hash_string),
            "name": ({"name"}, lambda t: t.name),
            "progress": ({"percentDone"}, lambda t: t.percent_done),
            "total_size": ({"totalSize"}, lambda t: t.total_size),
            "files": (
                {"files"},
                lambda t: [
                    ClientTorrentFile(
                        name=f["name"],
                        size=f["length"],
                        progress=f.get("bytesCompleted", 0) / f["length"] if f["length"] > 0 else 0.0,
                    )
                    for f in t.fields["files"]
                ],
            ),
            "trackers": ({"trackerList"}, lambda t: t.tracker_list),
            "download_dir": ({"downloadDir"}, lambda t: t.download_dir),
            "state": ({"status"}, lambda t: TRANSMISSION_STATE_MAPPING.get(t.status.value, TorrentState.UNKNOWN)),
            "piece_progress": (
                {"pieces", "pieceCount"},
                lambda t: self._decode_piece_progress(t.pieces, t.piece_count),
            ),
        }

    def _decode_piece_progress(self, pieces_b64: str, piece_count: int) -> list[bool]:
        """Decode base64 pieces data to get piece download status.

        Args:
            pieces_b64: Base64 encoded pieces data from Transmission
            piece_count: Total number of pieces in the torrent

        Returns:
            List of boolean values indicating piece download status
        """
        pieces_data = base64.b64decode(pieces_b64)
        piece_progress = [False] * piece_count

        for byte_index in range(min(len(pieces_data), (piece_count + 7) // 8)):
            byte_value = pieces_data[byte_index]
            start_piece = byte_index * 8
            end_piece = min(start_piece + 8, piece_count)

            for bit_offset in range(end_piece - start_piece):
                bit_index = 7 - bit_offset
                piece_progress[start_piece + bit_offset] = bool(byte_value & (1 << bit_index))

        return piece_progress

    def get_torrents(self, fields: list[str] | None) -> list[ClientTorrentInfo]:
        """Get all torrents from Transmission.

        Args:
            fields (list[str] | None): List of field names to include in the result.
                If None, all available fields will be included.

        Returns:
            list[ClientTorrentInfo]: List of torrent information.
        """
        try:
            # Get required arguments based on requested fields (always include hash)
            field_config = (
                {k: v for k, v in self.field_config.items() if k in fields or k == "hash"}
                if fields
                else self.field_config
            )

            # Union all argument sets
            arguments = list(set().union(*[arg for arg, _ in field_config.values()]))

            torrents = self.client.get_torrents(arguments=arguments)

            # Build torrent data with only requested fields using list comprehension
            result = [
                ClientTorrentInfo(
                    **{field_name: extractor(torrent) for field_name, (_, extractor) in field_config.items()}
                )
                for torrent in torrents
            ]

            return result

        except Exception as e:
            self.logger.error("Error retrieving torrents from Transmission: %s", e)
            return []

    def _add_torrent(self, torrent_data, download_dir: str, hash_match: bool) -> str:
        """Add torrent to Transmission.

        Args:
            torrent_data: Torrent file data.
            download_dir (str): Download directory.
            hash_match (bool): Not used for Transmission (has fast verification by default).

        Returns:
            str: Torrent hash string.
        """
        # Note: We reimplement this method instead of using client.add_torrent()
        # because we need access to the raw response data to detect torrent-duplicate
        # and handle it appropriately in the injection logic.

        # Get torrent data for RPC call
        torrent_data_b64 = base64.b64encode(torrent_data).decode()

        # Prepare arguments
        kwargs = {
            "download-dir": download_dir,
            "paused": True,
            "metainfo": torrent_data_b64,
            "labels": [config.cfg.downloader.label],
        }

        # Make direct RPC call to get raw response
        query = {"method": RpcMethod.TorrentAdd, "arguments": kwargs}
        http_data = self.client._http_query(query)

        # Parse JSON response
        try:
            data = json.loads(http_data)
        except json.JSONDecodeError as error:
            raise ValueError("failed to parse response as json", query, http_data) from error

        if "result" not in data:
            raise ValueError("Query failed, response data missing without result.", query, data, http_data)

        if data["result"] != "success":
            raise ValueError(f'Query failed with result "{data["result"]}".', query, data, http_data)

        # Extract torrent info from arguments
        res = data["arguments"]
        torrent_info = None
        if "torrent-added" in res:
            torrent_info = res["torrent-added"]
        elif "torrent-duplicate" in res:
            torrent_info = res["torrent-duplicate"]
            error_msg = f"The torrent to be injected cannot coexist with local torrent {torrent_info['hashString']}"
            self.logger.error(error_msg)
            raise TorrentConflictError(error_msg)

        if not torrent_info:
            raise ValueError("Invalid torrent-add response")

        return torrent_info["hashString"]

    def _remove_torrent(self, torrent_hash: str):
        """Remove torrent from Transmission.

        Args:
            torrent_hash (str): Torrent hash.
        """
        self.client.remove_torrent(torrent_hash, delete_data=False)

    def get_torrent_info(self, torrent_hash: str, fields: list[str] | None) -> ClientTorrentInfo | None:
        """Get torrent information."""
        try:
            # Get requested fields (always include hash)
            field_config = (
                {k: v for k, v in self.field_config.items() if k in fields or k == "hash"}
                if fields
                else self.field_config
            )

            # Get required arguments from field_config
            arguments = list(set().union(*[arg for arg, _ in field_config.values()]))

            torrent = self.client.get_torrent(torrent_hash, arguments=arguments)

            # Build ClientTorrentInfo using field_config
            return ClientTorrentInfo(
                **{field_name: extractor(torrent) for field_name, (_, extractor) in field_config.items()}
            )
        except Exception as e:
            self.logger.error("Error retrieving torrent info from Transmission: %s", e)
            return None

    def _rename_torrent(self, torrent_hash: str, old_name: str, new_name: str):
        """Rename entire torrent."""
        self.client.rename_torrent_path(torrent_hash, location=old_name, name=new_name)

    def _rename_file(self, torrent_hash: str, old_path: str, new_name: str):
        """Rename file within torrent."""
        self.client.rename_torrent_path(torrent_hash, location=old_path, name=new_name)

    def _verify_torrent(self, torrent_hash: str):
        """Verify torrent integrity."""
        self.client.verify_torrent(torrent_hash)

    def resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent in Transmission."""
        try:
            self.client.start_torrent(torrent_hash)
            return True
        except Exception as e:
            self.logger.error(f"Failed to resume torrent {torrent_hash}: {e}")
            return False

    def _process_rename_map(self, torrent_hash: str, base_path: str, rename_map: dict) -> dict:
        """Process rename mapping to adapt to Transmission."""
        transmission_map = {}
        temp_map = {}
        for torrent_name, local_name in rename_map.items():
            torrent_name_list = torrent_name.split("/")
            local_name_list = local_name.split("/")
            # Transmission cannot complete non-same-level moves
            if len(torrent_name_list) == len(local_name_list):
                for i in range(len(torrent_name_list)):
                    if torrent_name_list[i] != local_name_list[i]:
                        temp_map[("/".join(torrent_name_list[: i + 1]), local_name_list[i])] = i

        for (key, value), _priority in sorted(temp_map.items(), key=lambda item: item[1], reverse=True):
            transmission_map[posixpath.join(base_path, key)] = value

        return transmission_map

    def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from Transmission."""
        try:
            torrent_path = posixpath.join(self.torrents_dir, torrent_hash + ".torrent")
            with open(torrent_path, "rb") as f:
                return f.read()
        except Exception as e:
            self.logger.error(f"Error getting torrent data from Transmission: {e}")
            return None

    def get_torrents_for_monitoring(self, torrent_hashes: set[str]) -> dict[str, TorrentState]:
        """Get torrent states for monitoring (optimized for Transmission).

        Uses Transmission's get_torrents with minimal fields to get only
        the required state information for monitoring.

        Args:
            torrent_hashes (set[str]): Set of torrent hashes to monitor.

        Returns:
            dict[str, TorrentState]: Mapping of torrent hash to current state.
        """
        if not torrent_hashes:
            return {}

        try:
            # Get minimal torrent info - only hash and status
            torrents = self.client.get_torrents(
                ids=list(torrent_hashes),
                arguments=["hashString", "status"],  # Only get hash and status for efficiency
            )

            result = {
                torrent.hash_string: TRANSMISSION_STATE_MAPPING.get(torrent.status, TorrentState.UNKNOWN)
                for torrent in torrents
            }

            return result

        except Exception as e:
            self.logger.error(f"Error getting torrent states for monitoring from Transmission: {e}")
            return {}


class QBittorrentClient(TorrentClient):
    """qBittorrent torrent client implementation."""

    def __init__(self, url: str):
        super().__init__()
        config = parse_libtc_url(url)
        self.torrents_dir = config.torrents_dir or ""
        self.client = qbittorrentapi.Client(
            host=config.url or "http://localhost:8080",
            username=config.username,
            password=config.password,
        )
        # Authenticate with qBittorrent
        self.client.auth_log_in()

        # Initialize sync state for incremental updates
        self._last_rid = 0
        self._torrent_states_cache: dict[str, TorrentState] = {}

        # Field configuration: field_name -> extractor function
        self.field_config = {
            "hash": lambda t: t.hash,
            "name": lambda t: t.name,
            "progress": lambda t: t.progress,
            "total_size": lambda t: t.size,
            "files": lambda t: [ClientTorrentFile(name=f.name, size=f.size, progress=f.progress) for f in t.files],
            "trackers": lambda t: [
                tracker.url
                for tracker in t.trackers
                if tracker.url not in ("** [DHT] **", "** [PeX] **", "** [LSD] **")
            ],
            "download_dir": lambda t: t.save_path,
            "state": lambda t: QBITTORRENT_STATE_MAPPING.get(t.state, TorrentState.UNKNOWN),
            "piece_progress": lambda t: [piece == 2 for piece in t.pieceStates] if t.pieceStates else [],
        }

    def get_torrents(self, fields: list[str] | None) -> list[ClientTorrentInfo]:
        """Get all torrents from qBittorrent.

        Args:
            fields (list[str] | None): List of field names to include in the result.
                If None, all available fields will be included.

        Returns:
            list[ClientTorrentInfo]: List of torrent information.
        """
        try:
            # Get requested fields (always include hash)
            field_config = (
                {k: v for k, v in self.field_config.items() if k in fields or k == "hash"}
                if fields
                else self.field_config
            )

            torrents = self.client.torrents_info()

            # Build torrent data with only requested fields using list comprehension
            result = [
                ClientTorrentInfo(**{field_name: extractor(torrent) for field_name, extractor in field_config.items()})
                for torrent in torrents
            ]

            return result

        except Exception as e:
            self.logger.error("Error retrieving torrents from qBittorrent: %s", e)
            return []

    def _add_torrent(self, torrent_data, download_dir: str, hash_match: bool) -> str:
        """Add torrent to qBittorrent."""

        # qBittorrent doesn't return the hash directly, we need to decode it
        torrent_obj = torf.Torrent.read_stream(torrent_data)
        info_hash = torrent_obj.infohash

        current_time = time.time()

        result = self.client.torrents_add(
            torrent_files=torrent_data,
            save_path=download_dir,
            is_paused=True,
            category=config.cfg.downloader.label,
            use_auto_torrent_management=False,
            is_skip_checking=hash_match,  # Skip hash checking if hash match
        )

        # qBittorrent returns "Ok." for success and "Fails." for failure
        if result != "Ok.":
            # Check if torrent already exists by comparing add time
            try:
                torrent_info = self.client.torrents_info(torrent_hashes=info_hash)
                if torrent_info:
                    # Get the first (and should be only) torrent with this hash
                    existing_torrent = torrent_info[0]
                    # Convert add time to unix timestamp
                    add_time = existing_torrent.added_on
                    if add_time < current_time:
                        raise TorrentConflictError(existing_torrent.hash)
                    # Check if tracker is correct
                    target_tracker = torrent_obj.trackers.flat[0] if torrent_obj.trackers else ""
                    if existing_torrent.tracker != target_tracker:
                        raise TorrentConflictError(existing_torrent.hash)

            except TorrentConflictError as e:
                error_msg = f"The torrent to be injected cannot coexist with local torrent {e}"
                self.logger.error(error_msg)
                raise TorrentConflictError(error_msg) from e
            except Exception as e:
                raise ValueError(f"Failed to add torrent to qBittorrent: {e}") from e

        return info_hash

    def _remove_torrent(self, torrent_hash: str):
        """Remove torrent from qBittorrent."""
        self.client.torrents_delete(torrent_hashes=torrent_hash, delete_files=False)

    def get_torrent_info(self, torrent_hash: str, fields: list[str] | None) -> ClientTorrentInfo | None:
        """Get torrent information."""
        try:
            torrent_info = self.client.torrents_info(torrent_hashes=torrent_hash)
            if not torrent_info:
                return None

            torrent = torrent_info[0]

            # Get requested fields (always include hash)
            field_config = (
                {k: v for k, v in self.field_config.items() if k in fields or k == "hash"}
                if fields
                else self.field_config
            )

            # Build ClientTorrentInfo using field_config
            return ClientTorrentInfo(
                **{field_name: extractor(torrent) for field_name, extractor in field_config.items()}
            )
        except Exception as e:
            self.logger.error("Error retrieving torrent info from qBittorrent: %s", e)
            return None

    def _rename_torrent(self, torrent_hash: str, old_name: str, new_name: str):
        """Rename entire torrent."""
        self.client.torrents_rename(torrent_hash=torrent_hash, new_torrent_name=new_name)
        self.client.torrents_rename_folder(torrent_hash=torrent_hash, old_path=old_name, new_path=new_name)

    def _rename_file(self, torrent_hash: str, old_path: str, new_name: str):
        """Rename file within torrent."""
        self.client.torrents_rename_file(torrent_hash=torrent_hash, old_path=old_path, new_path=new_name)

    def _verify_torrent(self, torrent_hash: str):
        """Verify torrent integrity."""
        self.client.torrents_recheck(torrent_hashes=torrent_hash)

    def resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent in qBittorrent."""
        try:
            self.client.torrents_resume(torrent_hashes=torrent_hash)
            return True
        except Exception as e:
            self.logger.error(f"Failed to resume torrent {torrent_hash}: {e}")
            return False

    def _process_rename_map(self, torrent_hash: str, base_path: str, rename_map: dict) -> dict:
        """
        qBittorrent needs to prepend the root directory
        """
        new_rename_map = {}
        for key, value in rename_map.items():
            new_rename_map[posixpath.join(base_path, key)] = posixpath.join(base_path, value)
        return new_rename_map

    def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from qBittorrent."""
        try:
            torrent_data = self.client.torrents_export(torrent_hash=torrent_hash)
            if torrent_data is None:
                torrent_path = posixpath.join(self.torrents_dir, torrent_hash + ".torrent")
                with open(torrent_path, "rb") as f:
                    return f.read()
            return torrent_data
        except Exception as e:
            self.logger.error(f"Error getting torrent data from qBittorrent: {e}")
            return None

    def get_torrents_for_monitoring(self, torrent_hashes: set[str]) -> dict[str, TorrentState]:
        """Get torrent states for monitoring (optimized for qBittorrent).

        Uses qBittorrent's efficient sync/maindata API to get only the required
        state information for monitoring specific torrents. This method implements
        incremental sync using RID (Response ID) to only fetch changes since last call.

        Args:
            torrent_hashes (set[str]): Set of torrent hashes to monitor.

        Returns:
            dict[str, TorrentState]: Mapping of torrent hash to current state.
        """
        if not torrent_hashes:
            return {}

        try:
            # Use qBittorrent's sync API for efficient monitoring
            # This returns only changed data since last request using RID
            maindata = self.client.sync_maindata(rid=self._last_rid)

            # Update RID for next incremental request
            new_rid = maindata.get("rid", self._last_rid)
            if new_rid is not None:
                self._last_rid = int(new_rid)

            # Extract torrents data from sync response
            torrents_data = maindata.get("torrents", {})

            # Ensure torrents_data is a dictionary
            if not isinstance(torrents_data, dict):
                self.logger.warning("Unexpected torrents data format from qBittorrent sync API")
                return {}

            # Update cache with new data from torrents_data
            for torrent_hash, torrent_info in torrents_data.items():
                if isinstance(torrent_info, dict):
                    state_str = torrent_info.get("state", "unknown")
                    if isinstance(state_str, str):
                        state = QBITTORRENT_STATE_MAPPING.get(state_str, TorrentState.UNKNOWN)
                        self._torrent_states_cache[torrent_hash] = state

            # Return cached states for requested torrents
            return self._torrent_states_cache

        except Exception as e:
            self.logger.error(f"Error getting torrent states for monitoring from qBittorrent: {e}")
            # On error, fall back to cached states for requested torrents
            return self._torrent_states_cache

    def reset_sync_state(self) -> None:
        """Reset sync state for incremental updates.

        This will cause the next sync request to return all data instead of just changes.
        Useful when the sync state gets out of sync or when starting fresh monitoring.
        """
        self._last_rid = 0
        self._torrent_states_cache.clear()
        self.logger.debug("Reset qBittorrent sync state")


class DelugeClient(TorrentClient):
    """Deluge torrent client implementation."""

    def __init__(self, url: str):
        super().__init__()
        config = parse_libtc_url(url)
        self.torrents_dir = config.torrents_dir or ""
        self.client = deluge_client.DelugeRPCClient(
            host=config.host or "localhost",
            port=config.port or 58846,
            username=config.username or "",
            password=config.password or "",
            decode_utf8=True,
        )
        # Connect to Deluge daemon
        self.client.connect()

        # Field configuration: field_name -> (Deluge properties, extractor function)
        self.field_config = {
            "hash": ({"hash"}, lambda t: t["hash"]),
            "name": ({"name"}, lambda t: t["name"]),
            "progress": ({"progress"}, lambda t: t["progress"] / 100.0),
            "total_size": ({"total_size"}, lambda t: t["total_size"]),
            "files": (
                {"files", "file_progress"},
                lambda t: [
                    ClientTorrentFile(
                        name=f["path"],
                        size=f["size"],
                        progress=t["file_progress"][f["index"]],
                    )
                    for f in t["files"]
                ],
            ),
            "trackers": ({"trackers"}, lambda t: [tracker["url"] for tracker in t["trackers"]]),
            "download_dir": ({"save_path"}, lambda t: t["save_path"]),
            "state": (
                {"state"},
                lambda t: DELUGE_STATE_MAPPING.get(t["state"], TorrentState.UNKNOWN),
            ),
            "piece_progress": (
                {"pieces", "num_pieces"},
                lambda t: (
                    [True] * t["num_pieces"] if t["progress"] == 100.0 else [piece == 3 for piece in t["pieces"]]
                ),
            ),
        }

    def get_torrents(self, fields: list[str] | None) -> list[ClientTorrentInfo]:
        """Get all torrents from Deluge.

        Args:
            fields (list[str] | None): List of field names to include in the result.
                If None, all available fields will be included.

        Returns:
            list[ClientTorrentInfo]: List of torrent information.
        """
        try:
            # Get requested fields (always include hash)
            field_config = (
                {k: v for k, v in self.field_config.items() if k in fields or k == "hash"}
                if fields
                else self.field_config
            )

            # Get required Deluge properties based on requested fields
            arguments = list(set().union(*[arg for arg, _ in field_config.values()]))

            torrent_details = self.client.call(
                "core.get_torrents_status",
                {},
                arguments,
            )
            if torrent_details is None:
                return []

            # Build torrent data with only requested fields using list comprehension
            result = [
                ClientTorrentInfo(
                    **{field_name: extractor(torrent) for field_name, (_, extractor) in field_config.items()}
                )
                for torrent in torrent_details.values()
            ]

            return result

        except Exception as e:
            self.logger.error("Error retrieving torrents from Deluge: %s", e)
            return []

    def _add_torrent(self, torrent_data, download_dir: str, hash_match: bool) -> str:
        """Add torrent to Deluge."""
        torrent_b64 = base64.b64encode(torrent_data).decode()
        try:
            torrent_hash = self.client.call(
                "core.add_torrent_file",
                f"{os.urandom(16).hex()}.torrent",  # filename
                torrent_b64,
                {
                    "download_location": download_dir,
                    "add_paused": True,
                    "seed_mode": hash_match,  # Skip hash checking if hash match
                },
            )
        except Exception as e:
            if "Torrent already in session" in str(e):
                # Extract torrent hash from error message
                match = re.search(r"\(([a-f0-9]{40})\)", str(e))
                if match:
                    torrent_hash = match.group(1)
                    error_msg = f"The torrent to be injected cannot coexist with local torrent {torrent_hash}"
                    self.logger.error(error_msg)
                    raise TorrentConflictError(error_msg) from e
                else:
                    raise TorrentConflictError(str(e)) from e
            else:
                raise

        # Set label (if provided)
        label = config.cfg.downloader.label
        if label and torrent_hash:
            try:
                self.client.call("label.set_torrent", torrent_hash, label)
            except Exception as label_error:
                # If setting label fails, try creating label first
                if "Unknown Label" in str(label_error) or "label does not exist" in str(label_error).lower():
                    self.client.call("label.add", label)
                    # Try setting label again
                    self.client.call("label.set_torrent", torrent_hash, label)

        return str(torrent_hash)

    def _remove_torrent(self, torrent_hash: str):
        """Remove torrent from Deluge."""
        self.client.call("core.remove_torrent", torrent_hash, False)

    def get_torrent_info(self, torrent_hash: str, fields: list[str] | None) -> ClientTorrentInfo | None:
        """Get torrent information."""
        try:
            # Get requested fields (always include hash)
            field_config = (
                {k: v for k, v in self.field_config.items() if k in fields or k == "hash"}
                if fields
                else self.field_config
            )

            # Get required arguments from field_config
            arguments = list(set().union(*[arg for arg, _ in field_config.values()]))

            torrent_info = self.client.call(
                "core.get_torrent_status",
                torrent_hash,
                arguments,
            )

            if torrent_info is None:
                return None

            # Build ClientTorrentInfo using field_config
            return ClientTorrentInfo(
                **{field_name: extractor(torrent_info) for field_name, (_, extractor) in field_config.items()}
            )
        except Exception as e:
            self.logger.error("Error retrieving torrent info from Deluge: %s", e)
            return None

    def _rename_torrent(self, torrent_hash: str, old_name: str, new_name: str):
        """Rename entire torrent."""
        self.client.call("core.rename_folder", torrent_hash, old_name + "/", new_name + "/")

    def _rename_file(self, torrent_hash: str, old_path: str, new_name: str):
        """Rename file within torrent."""
        try:
            self.client.call("core.rename_files", torrent_hash, [(old_path, new_name)])
        except Exception as e:
            self.logger.warning(f"Failed to rename file in Deluge: {e}")

    def _verify_torrent(self, torrent_hash: str):
        """Verify torrent integrity."""
        self.client.call("core.force_recheck", [torrent_hash])

    def resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent in Deluge."""
        try:
            self.client.call("core.resume_torrent", [torrent_hash])
            return True
        except Exception as e:
            self.logger.error(f"Failed to resume torrent {torrent_hash}: {e}")
            return False

    def _process_rename_map(self, torrent_hash: str, base_path: str, rename_map: dict) -> dict:
        """
        Deluge needs to use index to rename files
        """
        new_rename_map = {}
        torrent_info = self.client.call("core.get_torrent_status", torrent_hash, ["files"])
        if torrent_info is None:
            return {}
        files = torrent_info.get("files", [])
        for file in files:
            relpath = posixpath.relpath(file["path"], base_path)
            if relpath in rename_map:
                new_rename_map[file["index"]] = posixpath.join(base_path, rename_map[relpath])
        return new_rename_map

    def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from Deluge."""
        try:
            torrent_path = posixpath.join(self.torrents_dir, torrent_hash + ".torrent")
            with open(torrent_path, "rb") as f:
                return f.read()
        except Exception as e:
            self.logger.error(f"Error getting torrent data from Deluge: {e}")
            return None

    def get_torrents_for_monitoring(self, torrent_hashes: set[str]) -> dict[str, TorrentState]:
        """Get torrent states for monitoring (optimized for Deluge).

        Uses Deluge's get_torrents_status with minimal fields to get only
        the required state information for monitoring.

        Args:
            torrent_hashes (set[str]): Set of torrent hashes to monitor.

        Returns:
            dict[str, TorrentState]: Mapping of torrent hash to current state.
        """
        if not torrent_hashes:
            return {}

        try:
            # Get minimal torrent status - only state
            torrents_status = self.client.call(
                "core.get_torrents_status",
                {"id": list(torrent_hashes)},
                ["state"],  # Only get state for efficiency
            )

            result = {}
            if torrents_status and isinstance(torrents_status, dict):
                result = {
                    torrent_hash: DELUGE_STATE_MAPPING.get(status.get("state"), TorrentState.UNKNOWN)
                    for torrent_hash, status in torrents_status.items()
                }

            return result

        except Exception as e:
            self.logger.error(f"Error getting torrent states for monitoring from Deluge: {e}")
            return {}


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


# Torrent client factory mapping
TORRENT_CLIENT_MAPPING = {
    "transmission": TransmissionClient,
    "qbittorrent": QBittorrentClient,
    "deluge": DelugeClient,
}


def create_torrent_client(url: str) -> TorrentClient:
    """Create a torrent client instance based on the URL scheme

    Args:
        url: The torrent client URL

    Returns:
        TorrentClient: Configured torrent client instance

    Raises:
        ValueError: If URL is empty or client type is not supported
        TypeError: If URL is None
    """
    if not url.strip():
        raise ValueError("URL cannot be empty")

    parsed = urlparse(url)
    client_type = parsed.scheme.split("+")[0]

    if client_type not in TORRENT_CLIENT_MAPPING:
        raise ValueError(f"Unsupported torrent client type: {client_type}")

    return TORRENT_CLIENT_MAPPING[client_type](url)


# Global torrent client instance
_torrent_client_instance: TorrentClient | None = None
_torrent_client_lock = threading.Lock()


def get_torrent_client() -> TorrentClient:
    """Get global torrent client instance.

    Returns:
        TorrentClient: Torrent client instance.
    """
    global _torrent_client_instance
    with _torrent_client_lock:
        if _torrent_client_instance is None:
            # Get client URL from config
            client_url = config.cfg.downloader.client
            _torrent_client_instance = create_torrent_client(client_url)
        return _torrent_client_instance


def set_torrent_client(torrent_client: TorrentClient) -> None:
    """Set global torrent client instance.

    Args:
        torrent_client: Torrent client instance to set as current.
    """
    global _torrent_client_instance
    with _torrent_client_lock:
        _torrent_client_instance = torrent_client
