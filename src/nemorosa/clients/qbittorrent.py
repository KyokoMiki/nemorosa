import posixpath
import time

import qbittorrentapi
import torf

from .. import config
from .client_common import (
    ClientTorrentFile,
    ClientTorrentInfo,
    TorrentClient,
    TorrentConflictError,
    TorrentState,
    parse_libtc_url,
)

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
    "hash": lambda t: t.hash,
    "name": lambda t: t.name,
    "progress": lambda t: t.progress,
    "total_size": lambda t: t.size,
    "files": lambda t: [ClientTorrentFile(name=f.name, size=f.size, progress=f.progress) for f in t.files],
    "trackers": lambda t: [t.tracker]
    if t.trackers_count == 1
    else [tracker.url for tracker in t.trackers if tracker.url not in ("** [DHT] **", "** [PeX] **", "** [LSD] **")],
    "download_dir": lambda t: t.save_path,
    "state": lambda t: QBITTORRENT_STATE_MAPPING.get(t.state, TorrentState.UNKNOWN),
    "piece_progress": lambda t: [piece == 2 for piece in t.pieceStates] if t.pieceStates else [],
}


class QBittorrentClient(TorrentClient):
    """qBittorrent torrent client implementation."""

    def __init__(self, url: str):
        super().__init__()
        client_config = parse_libtc_url(url)
        self.torrents_dir = client_config.torrents_dir or ""
        self.client = qbittorrentapi.Client(
            host=client_config.url or "http://localhost:8080",
            username=client_config.username,
            password=client_config.password,
        )
        # Authenticate with qBittorrent
        self.client.auth_log_in()

        # Initialize sync state for incremental updates
        self._last_rid = 0
        self._torrent_states_cache: dict[str, TorrentState] = {}

        # Use the field specifications constant
        self.field_config = _QBITTORRENT_FIELD_SPECS

        # Initialize database cache on first connection
        self._initialize_cache()

    # region Abstract Methods - Public Operations

    def get_torrents(
        self, torrent_hashes: list[str] | None = None, fields: list[str] | None = None
    ) -> list[ClientTorrentInfo]:
        """Get all torrents from qBittorrent.

        Args:
            torrent_hashes (list[str] | None): Optional list of torrent hashes to filter.
                If None, all torrents will be returned.
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

            # Get torrents from qBittorrent
            torrents = self.client.torrents_info(torrent_hashes=torrent_hashes)

            # Build ClientTorrentInfo objects
            result = []
            for torrent in torrents:
                values = {field_name: extractor(torrent) for field_name, extractor in field_config.items()}
                torrent_info = ClientTorrentInfo(**values)
                result.append(torrent_info)

            return result

        except Exception as e:
            self.logger.error("Error retrieving torrents from qBittorrent: %s", e)
            return []

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

    # endregion

    # region Abstract Methods - Internal Operations

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

    def _resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent in qBittorrent."""
        try:
            self.client.torrents_resume(torrent_hashes=torrent_hash)
            return True
        except Exception as e:
            self.logger.error(f"Failed to resume torrent {torrent_hash}: {e}")
            return False

    # endregion

    # region Monitoring Methods

    def reset_sync_state(self) -> None:
        """Reset sync state for incremental updates.

        This will cause the next sync request to return all data instead of just changes.
        Useful when the sync state gets out of sync or when starting fresh monitoring.
        """
        self._last_rid = 0
        self._torrent_states_cache.clear()
        self.logger.debug("Reset qBittorrent sync state")

    # endregion
