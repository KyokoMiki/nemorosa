import base64
import posixpath

import msgspec
import transmission_rpc
from transmission_rpc.constants import RpcMethod

from .. import config
from .client_common import (
    ClientTorrentFile,
    ClientTorrentInfo,
    FieldSpec,
    TorrentClient,
    TorrentConflictError,
    TorrentState,
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
    "hash": FieldSpec(_request_arguments="hashString", extractor=lambda t: t.hash_string),
    "name": FieldSpec(_request_arguments="name", extractor=lambda t: t.name),
    "progress": FieldSpec(_request_arguments="percentDone", extractor=lambda t: t.percent_done),
    "total_size": FieldSpec(_request_arguments="totalSize", extractor=lambda t: t.total_size),
    "files": FieldSpec(
        _request_arguments="files",
        extractor=lambda t: [
            ClientTorrentFile(
                name=f["name"],
                size=f["length"],
                progress=f.get("bytesCompleted", 0) / f["length"] if f["length"] > 0 else 0.0,
            )
            for f in t.fields["files"]
        ],
    ),
    "trackers": FieldSpec(_request_arguments="trackerList", extractor=lambda t: t.tracker_list),
    "download_dir": FieldSpec(_request_arguments="downloadDir", extractor=lambda t: t.download_dir),
    "state": FieldSpec(
        _request_arguments="status",
        extractor=lambda t: TRANSMISSION_STATE_MAPPING.get(t.status.value, TorrentState.UNKNOWN),
    ),
    "piece_progress": FieldSpec(
        _request_arguments={"pieces", "pieceCount"},
        extractor=lambda t: _decode_piece_progress(t.pieces, t.piece_count),
    ),
}


def _decode_piece_progress(pieces_b64: str, piece_count: int) -> list[bool]:
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


class TransmissionClient(TorrentClient):
    """Transmission torrent client implementation."""

    def __init__(self, url: str):
        super().__init__()
        client_config = parse_libtc_url(url)
        self.torrents_dir = client_config.torrents_dir or "/config/torrents"

        self.client = transmission_rpc.Client(
            host=client_config.host or "localhost",
            port=client_config.port or 9091,
            username=client_config.username,
            password=client_config.password,
        )

        # Use the field specifications constant
        self.field_config = _TRANSMISSION_FIELD_SPECS

    # region Abstract Methods - Public Operations

    def get_torrents(
        self, torrent_hashes: list[str] | None = None, fields: list[str] | None = None
    ) -> list[ClientTorrentInfo]:
        """Get all torrents from Transmission.

        Args:
            torrent_hashes (list[str] | None): Optional list of torrent hashes to filter.
                If None, all torrents will be returned.
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

            # Get required Transmission properties based on requested fields
            arguments = list(set().union(*[spec.request_arguments for spec in field_config.values()]))

            # Get torrents from Transmission (filtered by hashes if provided)
            torrents = self.client.get_torrents(ids=torrent_hashes, arguments=arguments)  # type: ignore[arg-type]

            # Build ClientTorrentInfo objects
            result = []
            for torrent in torrents:
                values = {field_name: spec.extractor(torrent) for field_name, spec in field_config.items()}
                torrent_info = ClientTorrentInfo(**values)
                result.append(torrent_info)

            return result

        except Exception as e:
            self.logger.error("Error retrieving torrents from Transmission: %s", e)
            return []

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

            torrent = self.client.get_torrent(torrent_hash, arguments=arguments)

            # Build ClientTorrentInfo using field_config
            return ClientTorrentInfo(
                **{field_name: spec.extractor(torrent) for field_name, spec in field_config.items()}
            )
        except Exception as e:
            self.logger.error("Error retrieving torrent info from Transmission: %s", e)
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
                torrent.hash_string: TRANSMISSION_STATE_MAPPING.get(torrent.status.value, TorrentState.UNKNOWN)
                for torrent in torrents
            }

            return result

        except Exception as e:
            self.logger.error(f"Error getting torrent states for monitoring from Transmission: {e}")
            return {}

    # endregion

    # region Abstract Methods - Internal Operations

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
            data = msgspec.json.decode(http_data)
        except msgspec.DecodeError as error:
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

    def _rename_torrent(self, torrent_hash: str, old_name: str, new_name: str):
        """Rename entire torrent."""
        self.client.rename_torrent_path(torrent_hash, location=old_name, name=new_name)

    def _rename_file(self, torrent_hash: str, old_path: str, new_name: str):
        """Rename file within torrent."""
        self.client.rename_torrent_path(torrent_hash, location=old_path, name=new_name)

    def _verify_torrent(self, torrent_hash: str):
        """Verify torrent integrity."""
        self.client.verify_torrent(torrent_hash)

    def _process_rename_map(self, torrent_hash: str, base_path: str, rename_map: dict) -> dict:
        """Process rename mapping to adapt to Transmission."""
        temp_map = {}
        for torrent_name, local_name in rename_map.items():
            torrent_name_list = torrent_name.split("/")
            local_name_list = local_name.split("/")
            # Transmission cannot complete non-same-level moves
            if len(torrent_name_list) == len(local_name_list):
                for i in range(len(torrent_name_list)):
                    if torrent_name_list[i] != local_name_list[i]:
                        temp_map[("/".join(torrent_name_list[: i + 1]), local_name_list[i])] = i

        transmission_map = {
            posixpath.join(base_path, key): value
            for (key, value), _priority in sorted(temp_map.items(), key=lambda item: item[1], reverse=True)
        }

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

    def _resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent in Transmission."""
        try:
            self.client.start_torrent(torrent_hash)
            return True
        except Exception as e:
            self.logger.error(f"Failed to resume torrent {torrent_hash}: {e}")
            return False

    # endregion
