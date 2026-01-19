"""
Deluge client implementation.
Provides integration with Deluge via its RPC interface.
"""

import base64
import posixpath
import re

import deluge_client
from anyio import Path
from asyncer import asyncify

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
            [True] * t["num_pieces"]
            if t["progress"] == 100.0
            else [piece == 3 for piece in t["pieces"]]  # 3 = Completed
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

    async def get_torrents(
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
            # Get field configuration and required arguments
            field_config, arguments = self._get_field_config_and_arguments(fields)

            # Get torrents from Deluge (filtered by hashes if provided)
            torrent_details = await asyncify(self.client.call)(
                "core.get_torrents_status",
                {"id": torrent_hashes} if torrent_hashes else {},
                arguments,
            )

            if not isinstance(torrent_details, dict):
                logger.error("Invalid torrents details from Deluge: %s", torrent_details)
                return []

            # Build ClientTorrentInfo objects
            return [
                ClientTorrentInfo(**{field_name: spec.extractor(torrent) for field_name, spec in field_config.items()})
                for torrent in torrent_details.values()
            ]

        except Exception as e:
            logger.error("Error retrieving torrents from Deluge: %s", e)
            return []

    async def get_torrents_for_monitoring(self, torrent_hashes: set[str]) -> dict[str, TorrentState]:
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
            torrents_status = await asyncify(self.client.call)(
                "core.get_torrents_status",
                {"id": list(torrent_hashes)},
                ["state"],
            )

            if not isinstance(torrents_status, dict):
                logger.error("Invalid torrents status from Deluge: %s", torrents_status)
                return {}

            return {
                torrent_hash: DELUGE_STATE_MAPPING.get(status.get("state"), TorrentState.UNKNOWN)
                for torrent_hash, status in torrents_status.items()
            }

        except Exception as e:
            logger.error(f"Error getting torrent states for monitoring from Deluge: {e}")
            return {}

    async def get_torrent_info(self, torrent_hash: str, fields: list[str] | None) -> ClientTorrentInfo | None:
        """Get torrent information.

        Args:
            torrent_hash (str): Torrent hash.
            fields (list[str] | None): List of field names to include in the result.

        Returns:
            ClientTorrentInfo | None: Torrent information, or None if not found.
        """
        try:
            # Get field configuration and required arguments
            field_config, arguments = self._get_field_config_and_arguments(fields)

            torrent_info = await asyncify(self.client.call)(
                "core.get_torrent_status",
                torrent_hash,
                arguments,
            )

            if not isinstance(torrent_info, dict):
                logger.debug("Invalid torrent info from Deluge: %s", torrent_info)
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

    async def _add_torrent(self, torrent_data: bytes, download_dir: str, hash_match: bool) -> str:
        """Add torrent to Deluge.

        Args:
            torrent_data (bytes): Torrent file data.
            download_dir (str): Download directory.
            hash_match (bool): Whether this is a hash match, if True, skip verification.

        Returns:
            str: Torrent hash.
        """
        torrent_b64 = base64.b64encode(torrent_data).decode()
        try:
            torrent_hash = await asyncify(self.client.call)(
                "core.add_torrent_file",
                None,
                torrent_b64,
                {
                    "download_location": download_dir,
                    "add_paused": True,
                    "seed_mode": hash_match,
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
                await asyncify(self.client.call)("label.set_torrent", torrent_hash, label)
            except Exception as label_error:
                # If setting label fails, try creating label first
                if "Unknown Label" in str(label_error) or "label does not exist" in str(label_error).lower():
                    try:
                        await asyncify(self.client.call)("label.add", label)
                        await asyncify(self.client.call)("label.set_torrent", torrent_hash, label)
                    except Exception as retry_error:
                        logger.warning(f"Failed to set label after creating it: {retry_error}")

        return str(torrent_hash)

    async def _remove_torrent(self, torrent_hash: str) -> None:
        """Remove torrent from Deluge.

        Args:
            torrent_hash (str): Torrent hash.
        """
        await asyncify(self.client.call)("core.remove_torrent", torrent_hash, False)

    async def _rename_torrent(self, torrent_hash: str, old_name: str, new_name: str) -> None:
        """Rename entire torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_name (str): Old torrent name.
            new_name (str): New torrent name.
        """
        await asyncify(self.client.call)(
            "core.rename_folder",
            torrent_hash,
            old_name + "/",
            new_name + "/",
        )

    async def _rename_file(self, torrent_hash: str, old_path: str, new_name: str) -> None:
        """Rename file within torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_path (str): Old file path.
            new_name (str): New file name.
        """
        try:
            await asyncify(self.client.call)(
                "core.rename_files",
                torrent_hash,
                [(old_path, new_name)],
            )
        except Exception as e:
            logger.warning(f"Failed to rename file in Deluge: {e}")

    async def _verify_torrent(self, torrent_hash: str) -> None:
        """Verify torrent integrity.

        Args:
            torrent_hash (str): Torrent hash.
        """
        await asyncify(self.client.call)("core.force_recheck", [torrent_hash])

    async def _process_rename_map(self, torrent_hash: str, base_path: str, rename_map: dict) -> dict:
        """Process rename mapping to adapt to Deluge.

        Deluge needs to use index to rename files.

        Args:
            torrent_hash (str): Torrent hash.
            base_path (str): Base path for files.
            rename_map (dict): Original rename mapping.

        Returns:
            dict: Processed rename mapping with file indices as keys.
        """
        torrent_info = await asyncify(self.client.call)("core.get_torrent_status", torrent_hash, ["files"])
        if not isinstance(torrent_info, dict):
            logger.debug("Invalid torrent info from Deluge: %s", torrent_info)
            return {}
        files = torrent_info.get("files", [])
        new_rename_map = {
            file["index"]: posixpath.join(base_path, rename_map[relpath])
            for file in files
            if (relpath := posixpath.relpath(file["path"], base_path)) in rename_map
        }
        return new_rename_map

    async def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from Deluge.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bytes | None: Torrent file data, or None if not available.
        """
        try:
            torrent_path = Path(self.torrents_dir) / f"{torrent_hash}.torrent"
            return await torrent_path.read_bytes()
        except Exception as e:
            logger.error(f"Error getting torrent data from Deluge: {e}")
            return None

    async def _resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent in Deluge.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            await asyncify(self.client.call)("core.resume_torrent", [torrent_hash])
            return True
        except Exception as e:
            logger.error(f"Failed to resume torrent {torrent_hash}: {e}")
            return False

    # endregion
