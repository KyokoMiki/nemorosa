import base64
import os
import posixpath
import re

import deluge_client

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

# State mapping for Deluge torrent client
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

# Field specifications for Deluge torrent client
_DELUGE_FIELD_SPECS = {
    "hash": FieldSpec(_request_arguments="hash", extractor=lambda t: t["hash"]),
    "name": FieldSpec(_request_arguments="name", extractor=lambda t: t["name"]),
    "progress": FieldSpec(_request_arguments="progress", extractor=lambda t: t["progress"] / 100.0),
    "total_size": FieldSpec(_request_arguments="total_size", extractor=lambda t: t["total_size"]),
    "files": FieldSpec(
        _request_arguments={"files", "file_progress"},
        extractor=lambda t: [
            ClientTorrentFile(name=f["path"], size=f["size"], progress=t["file_progress"][f["index"]])
            for f in t["files"]
        ],
    ),
    "trackers": FieldSpec(
        _request_arguments="trackers", extractor=lambda t: [tracker["url"] for tracker in t["trackers"]]
    ),
    "download_dir": FieldSpec(_request_arguments="save_path", extractor=lambda t: t["save_path"]),
    "state": FieldSpec(
        _request_arguments="state",
        extractor=lambda t: DELUGE_STATE_MAPPING.get(t["state"], TorrentState.UNKNOWN),
    ),
    "piece_progress": FieldSpec(
        _request_arguments={"pieces", "num_pieces"},
        extractor=lambda t: (
            [True] * t["num_pieces"] if t["progress"] == 100.0 else [piece == 3 for piece in t["pieces"]]
        ),
    ),
}


class DelugeClient(TorrentClient):
    """Deluge torrent client implementation."""

    def __init__(self, url: str):
        super().__init__()
        client_config = parse_libtc_url(url)
        self.torrents_dir = client_config.torrents_dir or ""
        self.client = deluge_client.DelugeRPCClient(
            host=client_config.host or "localhost",
            port=client_config.port or 58846,
            username=client_config.username or "",
            password=client_config.password or "",
            decode_utf8=True,
            timeout=60,
        )
        # Connect to Deluge daemon
        self.client.connect()

        self.field_config = _DELUGE_FIELD_SPECS

    # region Abstract Methods - Public Operations

    def get_torrents(
        self, torrent_hashes: list[str] | None = None, fields: list[str] | None = None
    ) -> list[ClientTorrentInfo]:
        """Get all torrents from Deluge.

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

            # Get required Deluge properties based on requested fields
            arguments = list(set().union(*[spec.request_arguments for spec in field_config.values()]))

            # Get torrents from Deluge (filtered by hashes if provided)
            torrent_details = self.client.call(
                "core.get_torrents_status", {"id": torrent_hashes} if torrent_hashes else {}, arguments
            )

            if not torrent_details:
                return []

            # Build ClientTorrentInfo objects
            result = []
            for torrent in torrent_details.values():
                values = {field_name: spec.extractor(torrent) for field_name, spec in field_config.items()}
                torrent_info = ClientTorrentInfo(**values)
                result.append(torrent_info)

            return result

        except Exception as e:
            logger.error("Error retrieving torrents from Deluge: %s", e)
            return []

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
            logger.error(f"Error getting torrent states for monitoring from Deluge: {e}")
            return {}

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
            arguments = list(set().union(*[spec.request_arguments for spec in field_config.values()]))

            torrent_info = self.client.call(
                "core.get_torrent_status",
                torrent_hash,
                arguments,
            )

            if torrent_info is None:
                return None

            # Build ClientTorrentInfo using field_config
            return ClientTorrentInfo(
                **{field_name: spec.extractor(torrent_info) for field_name, spec in field_config.items()}
            )
        except Exception as e:
            logger.error("Error retrieving torrent info from Deluge: %s", e)
            return None

    # endregion

    # region Abstract Methods - Internal Operations

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
                    logger.error(error_msg)
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

    def _rename_torrent(self, torrent_hash: str, old_name: str, new_name: str):
        """Rename entire torrent."""
        self.client.call("core.rename_folder", torrent_hash, old_name + "/", new_name + "/")

    def _rename_file(self, torrent_hash: str, old_path: str, new_name: str):
        """Rename file within torrent."""
        try:
            self.client.call("core.rename_files", torrent_hash, [(old_path, new_name)])
        except Exception as e:
            logger.warning(f"Failed to rename file in Deluge: {e}")

    def _verify_torrent(self, torrent_hash: str):
        """Verify torrent integrity."""
        self.client.call("core.force_recheck", [torrent_hash])

    def _process_rename_map(self, torrent_hash: str, base_path: str, rename_map: dict) -> dict:
        """
        Deluge needs to use index to rename files
        """
        torrent_info = self.client.call("core.get_torrent_status", torrent_hash, ["files"])
        if torrent_info is None:
            return {}
        files = torrent_info.get("files", [])
        new_rename_map = {
            file["index"]: posixpath.join(base_path, rename_map[relpath])
            for file in files
            if (relpath := posixpath.relpath(file["path"], base_path)) in rename_map
        }
        return new_rename_map

    def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from Deluge."""
        try:
            torrent_path = posixpath.join(self.torrents_dir, torrent_hash + ".torrent")
            with open(torrent_path, "rb") as f:
                return f.read()
        except Exception as e:
            logger.error(f"Error getting torrent data from Deluge: {e}")
            return None

    def _resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent in Deluge."""
        try:
            self.client.call("core.resume_torrent", [torrent_hash])
            return True
        except Exception as e:
            logger.error(f"Failed to resume torrent {torrent_hash}: {e}")
            return False

    # endregion
