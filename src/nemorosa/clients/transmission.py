"""
Transmission client implementation.
Provides integration with Transmission via its RPC interface.
"""

import base64
import posixpath

import msgspec
import transmission_rpc
from anyio import Path
from asyncer import asyncify
from transmission_rpc.constants import RpcMethod

from .. import config, logger
from .client_common import (
    ClientTorrentFile,
    ClientTorrentInfo,
    FieldSpec,
    TorrentClient,
    TorrentConflictError,
    TorrentState,
    decode_bitfield_bytes,
    parse_libtc_url,
)

# State mapping for Transmission torrent client
TRANSMISSION_STATE_MAPPING = {
    "stopped": TorrentState.PAUSED,
    "check pending": TorrentState.CHECKING,
    "checking": TorrentState.CHECKING,
    "download pending": TorrentState.QUEUED,
    "downloading": TorrentState.DOWNLOADING,
    "seed pending": TorrentState.QUEUED,
    "seeding": TorrentState.SEEDING,
}

# Field specifications for Transmission torrent client
_TRANSMISSION_FIELD_SPECS = {
    "hash": FieldSpec(
        _request_arguments="hashString", extractor=lambda t: t.hash_string
    ),
    "name": FieldSpec(_request_arguments="name", extractor=lambda t: t.name),
    "progress": FieldSpec(
        _request_arguments="percentDone", extractor=lambda t: t.percent_done
    ),
    "total_size": FieldSpec(
        _request_arguments="totalSize", extractor=lambda t: t.total_size
    ),
    "files": FieldSpec(
        _request_arguments="files",
        extractor=lambda t: [
            ClientTorrentFile(
                name=f["name"],
                size=f["length"],
                progress=f.get("bytesCompleted", 0) / f["length"]
                if f["length"] > 0
                else 0.0,
            )
            for f in t.fields["files"]
        ],
    ),
    "trackers": FieldSpec(
        _request_arguments="trackerList", extractor=lambda t: t.tracker_list
    ),
    "download_dir": FieldSpec(
        _request_arguments="downloadDir", extractor=lambda t: t.download_dir
    ),
    "state": FieldSpec(
        _request_arguments="status",
        extractor=lambda t: TRANSMISSION_STATE_MAPPING.get(
            t.status.value, TorrentState.UNKNOWN
        ),
    ),
    "piece_progress": FieldSpec(
        _request_arguments={"pieces", "pieceCount"},
        extractor=lambda t: decode_bitfield_bytes(
            base64.b64decode(t.pieces), t.piece_count
        ),
    ),
}


class TransmissionClient(TorrentClient):
    """Transmission torrent client implementation."""

    supports_fast_resume = True

    def __init__(self, url: str):
        super().__init__()
        client_config = parse_libtc_url(url)
        self.torrents_dir = (
            client_config.torrents_dir or config.cfg.downloader.torrents_dir
        )

        # Ensure protocol is either 'http' or 'https'
        protocol = client_config.scheme
        if protocol not in ("http", "https"):
            protocol = "http"  # Default to http if scheme is None or invalid

        self.client = transmission_rpc.Client(
            protocol=protocol,
            username=client_config.username,
            password=client_config.password,
            host=client_config.host or "localhost",
            port=client_config.port or 9091,
            path=client_config.path or "/transmission/rpc"
        )

        # Use the field specifications constant
        self.field_config = _TRANSMISSION_FIELD_SPECS

    # region Abstract Methods - Public Operations

    async def get_torrents(
        self, torrent_hashes: list[str] | None = None, fields: list[str] | None = None
    ) -> list[ClientTorrentInfo]:
        """Get all torrents from Transmission.

        Args:
            torrent_hashes (list[str] | None): Optional list of hashes to filter.
                If None, all torrents will be returned.
            fields (list[str] | None): List of field names to include in the result.
                If None, all available fields will be included.

        Returns:
            list[ClientTorrentInfo]: List of torrent information.
        """
        try:
            # Get field configuration and required arguments
            field_config, arguments = self._get_field_config_and_arguments(fields)

            # Get torrents from Transmission (filtered by hashes if provided)
            torrents = await asyncify(self.client.get_torrents)(
                ids=torrent_hashes,  # type: ignore[arg-type]
                arguments=arguments,
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
            logger.error("Error retrieving torrents from Transmission: %s", e)
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
            # Get field configuration and required arguments
            field_config, arguments = self._get_field_config_and_arguments(fields)

            torrent = await asyncify(self.client.get_torrent)(
                torrent_hash, arguments=arguments
            )

            # Build ClientTorrentInfo using field_config
            return ClientTorrentInfo(
                **{
                    field_name: spec.extractor(torrent)
                    for field_name, spec in field_config.items()
                }
            )
        except Exception as e:
            logger.error("Error retrieving torrent info from Transmission: %s", e)
            return None

    async def get_torrents_for_monitoring(
        self, torrent_hashes: set[str]
    ) -> dict[str, TorrentState]:
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
            torrents = await asyncify(self.client.get_torrents)(
                ids=list(torrent_hashes),
                arguments=["hashString", "status"],
            )

            return {
                torrent.hash_string: TRANSMISSION_STATE_MAPPING.get(
                    torrent.status.value, TorrentState.UNKNOWN
                )
                for torrent in torrents
            }

        except Exception as e:
            logger.error(
                "Error getting torrent states for monitoring from Transmission: %s", e
            )
            return {}

    # endregion

    # region Abstract Methods - Internal Operations

    async def _add_torrent(
        self,
        torrent_data: bytes,
        download_dir: str,
        hash_match: bool,
        local_torrent_hash: str = "",
    ) -> str:
        """Add torrent to Transmission.

        Note: Transmission does not support duplicate_categories feature.
        The local_torrent_hash parameter is ignored.

        Args:
            torrent_data (bytes): Torrent file data.
            download_dir (str): Download directory.
            hash_match (bool): Not used for Transmission (has fast verification).
            local_torrent_hash (str): Hash of original local torrent (ignored).

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
        }

        # Prepare labels: use tags if provided, otherwise use [label] if not None
        if config.cfg.downloader.tags:
            kwargs["labels"] = config.cfg.downloader.tags
        elif config.cfg.downloader.label:
            kwargs["labels"] = [config.cfg.downloader.label]

        # Make direct RPC call to get raw response
        query = {"method": RpcMethod.TorrentAdd, "arguments": kwargs}

        # Note: Must use private method _http_query to access raw response data.
        # transmission_rpc doesn't provide this capability, which is required to
        # distinguish between torrent-added and torrent-duplicate responses.
        http_data = await asyncify(self.client._http_query)(query)  # noqa: SLF001

        # Parse JSON response
        try:
            data = msgspec.json.decode(http_data)
        except msgspec.DecodeError as error:
            raise ValueError(
                "failed to parse response as json", query, http_data
            ) from error

        if "result" not in data:
            raise ValueError(
                "Query failed, response data missing without result.",
                query,
                data,
                http_data,
            )

        if data["result"] != "success":
            raise ValueError(
                f'Query failed with result "{data["result"]}".',
                query,
                data,
                http_data,
            )

        # Extract torrent info from arguments
        res = data["arguments"]
        torrent_info = None
        if "torrent-added" in res:
            torrent_info = res["torrent-added"]
        elif "torrent-duplicate" in res:
            torrent_info = res["torrent-duplicate"]
            error_msg = (
                f"The torrent to be injected cannot coexist with "
                f"local torrent {torrent_info['hashString']}"
            )
            logger.error(error_msg)
            raise TorrentConflictError(error_msg)

        if not torrent_info:
            raise ValueError("Invalid torrent-add response")

        return torrent_info["hashString"]

    async def _remove_torrent(self, torrent_hash: str) -> None:
        """Remove torrent from Transmission.

        Args:
            torrent_hash (str): Torrent hash.
        """
        await asyncify(self.client.remove_torrent)(torrent_hash, delete_data=False)

    async def _rename_torrent(
        self, torrent_hash: str, old_name: str, new_name: str
    ) -> None:
        """Rename entire torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_name (str): Old torrent name.
            new_name (str): New torrent name.
        """
        await asyncify(self.client.rename_torrent_path)(
            torrent_hash,
            location=old_name,
            name=new_name,
        )

    async def _rename_file(
        self, torrent_hash: str, old_path: str, new_name: str
    ) -> None:
        """Rename file within torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_path (str): Old file path.
            new_name (str): New file name.
        """
        await asyncify(self.client.rename_torrent_path)(
            torrent_hash,
            location=old_path,
            name=new_name,
        )

    async def _verify_torrent(self, torrent_hash: str) -> None:
        """Verify torrent integrity.

        Args:
            torrent_hash (str): Torrent hash.
        """
        await asyncify(self.client.verify_torrent)(torrent_hash)

    async def _process_rename_map(
        self, torrent_hash: str, base_path: str, rename_map: dict
    ) -> dict:
        """Process rename mapping to adapt to Transmission.

        Args:
            torrent_hash (str): Torrent hash.
            base_path (str): Base path for files.
            rename_map (dict): Original rename mapping.

        Returns:
            dict: Processed rename mapping for Transmission.
        """
        temp_map = {}
        for torrent_name, local_name in rename_map.items():
            torrent_name_list = torrent_name.split("/")
            local_name_list = local_name.split("/")
            # Transmission cannot complete non-same-level moves
            if len(torrent_name_list) == len(local_name_list):
                for i, (torrent_part, local_part) in enumerate(
                    zip(torrent_name_list, local_name_list, strict=False)
                ):
                    if torrent_part != local_part:
                        temp_map[("/".join(torrent_name_list[: i + 1]), local_part)] = i

        transmission_map = {
            posixpath.join(base_path, key): value
            for (key, value), _priority in sorted(
                temp_map.items(), key=lambda item: item[1], reverse=True
            )
        }

        return transmission_map

    async def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from Transmission.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bytes | None: Torrent file data, or None if not available.
        """
        try:
            torrent_path = Path(self.torrents_dir) / f"{torrent_hash}.torrent"
            return await torrent_path.read_bytes()
        except Exception as e:
            logger.error("Error getting torrent data from Transmission: %s", e)
            return None

    async def _resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent in Transmission.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            await asyncify(self.client.start_torrent)(torrent_hash)
            return True
        except Exception as e:
            logger.error("Failed to resume torrent %s: %s", torrent_hash, e)
            return False

    # endregion
