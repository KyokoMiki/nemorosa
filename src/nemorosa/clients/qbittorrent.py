"""
qBittorrent client implementation.
Provides integration with qBittorrent via its Web API.
"""

import os
import posixpath
import time

import qbittorrentapi
from anyio import Path
from asyncer import asyncify
from torf import Torrent

from .. import config, logger
from .client_common import (
    ClientTorrentFile,
    ClientTorrentInfo,
    FieldSpec,
    TorrentClient,
    TorrentConflictError,
    TorrentState,
    parse_libtc_url,
)

# Category suffix for duplicate categories feature
CATEGORY_SUFFIX = ".nemorosa"

# State mapping for qBittorrent torrent client
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

# Field extractors for qBittorrent torrent client
_QBITTORRENT_FIELD_SPECS = {
    "hash": FieldSpec(_request_arguments=None, extractor=lambda t: t.hash),
    "name": FieldSpec(_request_arguments=None, extractor=lambda t: t.name),
    "progress": FieldSpec(_request_arguments=None, extractor=lambda t: t.progress),
    "total_size": FieldSpec(_request_arguments=None, extractor=lambda t: t.size),
    "files": FieldSpec(
        _request_arguments=None,
        extractor=lambda t: [
            ClientTorrentFile(name=f.name, size=f.size, progress=f.progress)
            for f in t.files
        ],
    ),
    "trackers": FieldSpec(
        _request_arguments=None,
        extractor=lambda t: (
            [t.tracker]
            if t.trackers_count == 1
            else [
                tracker.url
                for tracker in t.trackers
                if tracker.url not in ("** [DHT] **", "** [PeX] **", "** [LSD] **")
            ]
        ),
    ),
    "download_dir": FieldSpec(_request_arguments=None, extractor=lambda t: t.save_path),
    "state": FieldSpec(
        _request_arguments=None,
        extractor=lambda t: QBITTORRENT_STATE_MAPPING.get(
            t.state, TorrentState.UNKNOWN
        ),
    ),
    "piece_progress": FieldSpec(
        _request_arguments=None,
        extractor=lambda t: (
            [piece == 2 for piece in t.pieceStates] if t.pieceStates else []
        ),
    ),
}


class QBittorrentClient(TorrentClient):
    """qBittorrent torrent client implementation."""

    def __init__(self, url: str):
        super().__init__()
        client_config = parse_libtc_url(url)
        self.torrents_dir = (
            config.cfg.downloader.torrents_dir or client_config.torrents_dir or ""
        )
        self.client = qbittorrentapi.Client(
            host=client_config.url or "http://localhost:8080",
            username=client_config.username,
            password=client_config.password,
        )
        # Authenticate with qBittorrent
        if client_config.username and client_config.password:
            self.client.auth_log_in()

        # Initialize sync state for incremental updates
        self._last_rid = 0
        self._torrent_states_cache: dict[str, TorrentState] = {}

        # Use the field specifications constant
        self.field_config = _QBITTORRENT_FIELD_SPECS

    # region Abstract Methods - Public Operations

    async def get_torrents(
        self, torrent_hashes: list[str] | None = None, fields: list[str] | None = None
    ) -> list[ClientTorrentInfo]:
        """Get all torrents from qBittorrent.

        Args:
            torrent_hashes (list[str] | None): Optional list of torrent hashes
                to filter. If None, all torrents will be returned.
            fields (list[str] | None): List of field names to include in the
                result.
                If None, all available fields will be included.

        Returns:
            list[ClientTorrentInfo]: List of torrent information.
        """
        try:
            # Get field configuration and required arguments
            field_config, _ = self._get_field_config_and_arguments(fields)

            # Get torrents from qBittorrent
            torrents = await asyncify(self.client.torrents_info)(
                torrent_hashes=torrent_hashes
            )

            # Build ClientTorrentInfo objects
            return [
                ClientTorrentInfo(
                    **{
                        field_name: spec.extractor(torrent)
                        for field_name, spec in field_config.items()
                    }
                )
                for torrent in torrents
            ]

        except Exception as e:
            logger.error("Error retrieving torrents from qBittorrent: %s", e)
            return []

    async def get_torrent_info(
        self, torrent_hash: str, fields: list[str] | None
    ) -> ClientTorrentInfo | None:
        """Get torrent information.

        Args:
            torrent_hash (str): Torrent hash.
            fields (list[str] | None): List of field names to include in the result.

        Returns:
            ClientTorrentInfo | None: Torrent information, or None if not found.
        """
        try:
            torrent_info = await asyncify(self.client.torrents_info)(
                torrent_hashes=torrent_hash
            )
            if not torrent_info:
                return None

            torrent = torrent_info[0]

            # Get field configuration and required arguments
            field_config, _ = self._get_field_config_and_arguments(fields)

            # Build ClientTorrentInfo using field_config
            return ClientTorrentInfo(
                **{
                    field_name: spec.extractor(torrent)
                    for field_name, spec in field_config.items()
                }
            )
        except Exception as e:
            logger.error("Error retrieving torrent info from qBittorrent: %s", e)
            return None

    async def get_torrents_for_monitoring(
        self, torrent_hashes: set[str]
    ) -> dict[str, TorrentState]:
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
            maindata = await asyncify(self.client.sync_maindata)(rid=self._last_rid)

            # Update RID for next incremental request
            new_rid = maindata.get("rid", self._last_rid)
            if isinstance(new_rid, int | str):
                self._last_rid = int(new_rid)

            # Extract torrents data from sync response
            torrents_data = maindata.get("torrents", {})

            # Ensure torrents_data is a dictionary
            if not isinstance(torrents_data, dict):
                logger.warning(
                    "Unexpected torrents data format from qBittorrent sync API"
                )
                return {}

            # Update cache with new data from torrents_data
            for torrent_hash, torrent_info in torrents_data.items():
                if isinstance(torrent_info, dict):
                    state_str = torrent_info.get("state", "unknown")
                    if isinstance(state_str, str):
                        state = QBITTORRENT_STATE_MAPPING.get(
                            state_str, TorrentState.UNKNOWN
                        )
                        self._torrent_states_cache[torrent_hash] = state

            # Return cached states for requested torrents
            return {
                h: self._torrent_states_cache[h]
                for h in torrent_hashes
                if h in self._torrent_states_cache
            }

        except Exception as e:
            logger.error(
                "Error getting torrent states for monitoring from qBittorrent: %s", e
            )
            # On error, fall back to cached states for requested torrents
            return {
                h: self._torrent_states_cache[h]
                for h in torrent_hashes
                if h in self._torrent_states_cache
            }

    # endregion

    # region Abstract Methods - Internal Operations

    async def _get_local_torrent_info(
        self, local_torrent_hash: str
    ) -> tuple[str, bool]:
        """Get category and autoTMM setting of the local torrent.

        Args:
            local_torrent_hash: Hash of the local torrent.

        Returns:
            tuple[str, bool]: (category, auto_tmm) of the local torrent.
        """
        if not local_torrent_hash:
            return "", False
        try:
            torrent_info = await asyncify(self.client.torrents_info)(
                torrent_hashes=local_torrent_hash
            )
            if torrent_info:
                return torrent_info[0].category or "", torrent_info[0].auto_tmm
        except Exception as e:
            logger.debug("Failed to get local torrent info: %s", e)
        return "", False

    async def _ensure_category_exists(self, category: str, save_path: str) -> None:
        """Ensure category exists with correct save path for autoTMM.

        Args:
            category: Category name to create/update.
            save_path: Save path to set for the category.
        """
        try:
            # Get all categories
            categories = await asyncify(self.client.torrents_categories)()
            if category in categories:
                # Category exists, check if save path matches
                cat_info = categories[category]
                # qbittorrent-api returns Category object with savePath attribute
                existing_path = getattr(cat_info, "savePath", "") or ""
                # Normalize paths before comparison to avoid trailing-slash mismatches
                normalized_existing = (
                    os.path.normpath(existing_path) if existing_path else ""
                )
                normalized_save = os.path.normpath(save_path) if save_path else ""
                if normalized_existing != normalized_save:
                    # Update category save path
                    await asyncify(self.client.torrents_edit_category)(
                        name=category,
                        save_path=normalized_save,
                    )
                    logger.debug(
                        "Updated category '%s' save path to '%s'",
                        category,
                        normalized_save,
                    )
            else:
                # Create new category
                await asyncify(self.client.torrents_create_category)(
                    name=category,
                    save_path=save_path,
                )
                logger.debug(
                    "Created category '%s' with save path '%s'", category, save_path
                )
        except Exception as e:
            logger.warning("Failed to ensure category '%s' exists: %s", category, e)

    def _calculate_category_and_tags(
        self, local_torrent_category: str, is_linking: bool
    ) -> tuple[str, list[str] | None]:
        """Calculate category and tags for the new torrent.

        Follows cross-seed's logic:
        - Linking mode: use default label as category, optionally add dupe tag
        - Non-linking mode: inherit original category, optionally add suffix

        Args:
            local_torrent_category: Category of the original local torrent.
            is_linking: Whether linking mode is enabled.

        Returns:
            tuple[str, list[str] | None]: (category, tags) to use for the new torrent.
        """
        default_label = config.cfg.downloader.label or ""
        default_tags = config.cfg.downloader.tags

        # Short-circuit: use unified labels directly
        if config.cfg.downloader.use_unified_labels:
            return default_label, default_tags

        if is_linking:
            # Linking mode: always use default label as category
            # If duplicate_categories is enabled, add dupe category to tags
            if (
                config.cfg.downloader.duplicate_categories
                and local_torrent_category
                and local_torrent_category != default_label
            ):
                dupe_category = (
                    local_torrent_category
                    if local_torrent_category.endswith(CATEGORY_SUFFIX)
                    else f"{local_torrent_category}{CATEGORY_SUFFIX}"
                )
                tags = list(default_tags) if default_tags else []
                if dupe_category not in tags:
                    tags.append(dupe_category)
                return default_label, tags
            return default_label, default_tags

        # Non-linking mode: inherit original category
        if not config.cfg.downloader.duplicate_categories:
            # duplicate_categories disabled: use original category as-is
            return local_torrent_category or default_label, default_tags

        # duplicate_categories enabled: add suffix to category
        if not local_torrent_category or local_torrent_category == default_label:
            return default_label, default_tags

        dupe_category = (
            local_torrent_category
            if local_torrent_category.endswith(CATEGORY_SUFFIX)
            else f"{local_torrent_category}{CATEGORY_SUFFIX}"
        )
        return dupe_category, default_tags

    async def _add_torrent(
        self,
        torrent_data: bytes,
        download_dir: str,
        hash_match: bool,
        local_torrent_hash: str = "",
    ) -> str:
        """Add torrent to qBittorrent.

        Args:
            torrent_data (bytes): Torrent file data.
            download_dir (str): Download directory.
            hash_match (bool): Whether this is a hash match, if True, skip verification.
            local_torrent_hash (str): Hash of the original local torrent.
                Used for duplicate_categories and autoTMM features.

        Returns:
            str: Torrent hash.
        """
        # Get local torrent info only when needed:
        # - local_torrent_category: needed only if not using unified labels
        # - local_torrent_auto_tmm: needed only if not linking (to inherit autoTMM)
        local_torrent_category = ""
        local_torrent_auto_tmm = False
        need_local_info = local_torrent_hash and (
            not config.cfg.downloader.use_unified_labels
            or not config.cfg.linking.enable_linking
        )
        if need_local_info:
            (
                local_torrent_category,
                local_torrent_auto_tmm,
            ) = await self._get_local_torrent_info(local_torrent_hash)

        # Determine autoTMM setting:
        # - If linking is enabled, always disable autoTMM (need specific path)
        # - Otherwise, inherit from local torrent
        use_auto_tmm = (
            False if config.cfg.linking.enable_linking else local_torrent_auto_tmm
        )

        # Calculate category and tags based on duplicate_categories and linking settings
        category, tags = self._calculate_category_and_tags(
            local_torrent_category, config.cfg.linking.enable_linking
        )

        # If autoTMM is enabled and we're using a new duplicated category (not default),
        # ensure the category exists with correct save path
        default_label = config.cfg.downloader.label or ""
        if (
            use_auto_tmm
            and config.cfg.downloader.duplicate_categories
            and category
            and category != default_label
        ):
            await self._ensure_category_exists(category, download_dir)

        current_time = time.time()

        result = await asyncify(self.client.torrents_add)(
            torrent_files=torrent_data,
            save_path=download_dir if not use_auto_tmm else None,
            is_paused=True,
            category=category,
            tags=tags,
            use_auto_torrent_management=use_auto_tmm,
            is_skip_checking=hash_match,
        )

        # qBittorrent doesn't return the hash directly, we need to decode it
        torrent_obj = Torrent.read_stream(torrent_data)
        info_hash = torrent_obj.infohash

        # qBittorrent returns "Ok." for success and "Fails." for failure
        if result != "Ok.":
            # Check if torrent already exists by comparing add time
            try:
                torrent_info = await asyncify(self.client.torrents_info)(
                    torrent_hashes=info_hash
                )
                if torrent_info:
                    # Get the first (and should be only) torrent with this hash
                    existing_torrent = torrent_info[0]
                    # Convert add time to unix timestamp
                    add_time = existing_torrent.added_on
                    if add_time < current_time:
                        raise TorrentConflictError(existing_torrent.hash)
                    # Check if tracker is correct
                    target_tracker = (
                        torrent_obj.trackers.flat[0] if torrent_obj.trackers else ""
                    )
                    if existing_torrent.tracker != target_tracker:
                        raise TorrentConflictError(existing_torrent.hash)

            except TorrentConflictError as e:
                error_msg = (
                    f"The torrent to be injected cannot coexist with local torrent {e}"
                )
                logger.error(error_msg)
                raise TorrentConflictError(error_msg) from e
            except Exception as e:
                raise ValueError(f"Failed to add torrent to qBittorrent: {e}") from e

        return info_hash

    async def _remove_torrent(self, torrent_hash: str) -> None:
        """Remove torrent from qBittorrent.

        Args:
            torrent_hash (str): Torrent hash.
        """
        await asyncify(self.client.torrents_delete)(
            torrent_hashes=torrent_hash, delete_files=False
        )

    async def _rename_torrent(
        self, torrent_hash: str, old_name: str, new_name: str
    ) -> None:
        """Rename entire torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_name (str): Old torrent name.
            new_name (str): New torrent name.
        """
        try:
            await asyncify(self.client.torrents_rename)(
                torrent_hash=torrent_hash,
                new_torrent_name=new_name,
            )
            await asyncify(self.client.torrents_rename_folder)(
                torrent_hash=torrent_hash,
                old_path=old_name,
                new_path=new_name,
            )
        except qbittorrentapi.Conflict409Error:
            pass

    async def _rename_file(
        self, torrent_hash: str, old_path: str, new_name: str
    ) -> None:
        """Rename file within torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_path (str): Old file path.
            new_name (str): New file name.
        """
        await asyncify(self.client.torrents_rename_file)(
            torrent_hash=torrent_hash,
            old_path=old_path,
            new_path=new_name,
        )

    async def _verify_torrent(self, torrent_hash: str) -> None:
        """Verify torrent integrity.

        Args:
            torrent_hash (str): Torrent hash.
        """
        await asyncify(self.client.torrents_recheck)(torrent_hashes=torrent_hash)

    async def _process_rename_map(
        self, torrent_hash: str, base_path: str, rename_map: dict
    ) -> dict:
        """Process rename mapping to adapt to qBittorrent.

        qBittorrent needs to prepend the root directory.

        Args:
            torrent_hash (str): Torrent hash.
            base_path (str): Base path for files.
            rename_map (dict): Original rename mapping.

        Returns:
            dict: Processed rename mapping with full paths.
        """
        return {
            posixpath.join(base_path, key): posixpath.join(base_path, value)
            for key, value in rename_map.items()
        }

    async def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from qBittorrent.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bytes | None: Torrent file data, or None if not available.
        """
        try:
            torrent_data = await asyncify(self.client.torrents_export)(
                torrent_hash=torrent_hash
            )
            if torrent_data is None:
                torrent_path = Path(self.torrents_dir) / f"{torrent_hash}.torrent"
                return await torrent_path.read_bytes()
            return torrent_data
        except Exception as e:
            logger.error("Error getting torrent data from qBittorrent: %s", e)
            return None

    async def _resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent in qBittorrent.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            await asyncify(self.client.torrents_resume)(torrent_hashes=torrent_hash)
            return True
        except Exception as e:
            logger.error("Failed to resume torrent %s: %s", torrent_hash, e)
            return False

    # endregion

    # region Monitoring Methods

    def reset_sync_state(self) -> None:
        """Reset sync state for incremental updates.

        This will cause the next sync request to return all data instead of
        just changes. Useful when the sync state gets out of sync or when
        starting fresh monitoring.
        """
        self._last_rid = 0
        self._torrent_states_cache.clear()
        logger.debug("Reset qBittorrent sync state")

    # endregion
