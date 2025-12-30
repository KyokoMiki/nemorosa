"""
rTorrent client implementation.
Provides integration with rTorrent via XML-RPC interface using SCGI transport.
"""

import asyncio
import posixpath
import xmlrpc.client  # nosec B411
from pathlib import Path
from urllib.parse import urlsplit

import aiofiles
import aiofiles.os
import defusedxml.xmlrpc
from torf import Torrent

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
from .scgitransport import SCGITransport


def _get_rtorrent_state(is_active, is_open, complete, hashing) -> TorrentState:
    """Get torrent state from rTorrent status flags.

    Args:
        is_active: Whether torrent is active (downloading/seeding)
        is_open: Whether torrent is open
        complete: Whether torrent is complete
        hashing: Whether torrent is hashing

    Returns:
        TorrentState: Mapped torrent state
    """
    if hashing:
        return TorrentState.CHECKING
    if not is_open:
        return TorrentState.PAUSED
    if complete:
        return TorrentState.SEEDING if is_active else TorrentState.COMPLETED
    return TorrentState.DOWNLOADING if is_active else TorrentState.PAUSED


# Field specifications for rTorrent torrent client (excluding files and trackers)
_RTORRENT_FIELD_SPECS = {
    "hash": FieldSpec(_request_arguments="d.hash", extractor=lambda t: t.get("d.hash", "")),
    # For rTorrent, use d.directory instead of d.name as the torrent name, because in the current
    # nemorosa design, the name is used to identify whether two torrents are the same, and rTorrent
    # doesn't support renaming, only changing the save directory
    "name": FieldSpec(
        _request_arguments="d.directory", extractor=lambda t: posixpath.basename(t.get("d.directory", ""))
    ),
    "progress": FieldSpec(
        _request_arguments={"d.completed_bytes", "d.size_bytes"},
        extractor=lambda t: t.get("d.completed_bytes", 0) / t.get("d.size_bytes", 1)
        if t.get("d.size_bytes", 0) > 0
        else 0.0,
    ),
    "total_size": FieldSpec(_request_arguments="d.size_bytes", extractor=lambda t: t.get("d.size_bytes", 0)),
    "download_dir": FieldSpec(
        _request_arguments="d.directory", extractor=lambda t: posixpath.dirname(t.get("d.directory", ""))
    ),
    "state": FieldSpec(
        _request_arguments={"d.is_active", "d.is_open", "d.complete", "d.hashing"},
        extractor=lambda t: _get_rtorrent_state(
            t.get("d.is_active", 0), t.get("d.is_open", 0), t.get("d.complete", 0), t.get("d.hashing", 0)
        ),
    ),
    "piece_progress": FieldSpec(
        _request_arguments={"d.bitfield", "d.size_chunks", "d.completed_bytes", "d.size_bytes"},
        extractor=lambda t: _decode_bitfield(
            t.get("d.bitfield", ""),
            t.get("d.size_chunks", 0),
            t.get("d.completed_bytes", 0) / t.get("d.size_bytes", 1) if t.get("d.size_bytes", 0) > 0 else 0.0,
        ),
    ),
}


def _decode_bitfield(bitfield_hex: str, piece_count: int, progress: float = 0.0) -> list[bool]:
    """Decode hexadecimal bitfield data to get piece download status.

    Args:
        bitfield_hex: Hexadecimal encoded bitfield data from rTorrent
        piece_count: Total number of pieces in the torrent
        progress: Download progress (0.0 to 1.0)

    Returns:
        List of boolean values indicating piece download status
    """
    if progress == 1.0:
        return [True] * piece_count

    if not bitfield_hex:
        return [False] * piece_count

    bitfield_data = bytes.fromhex(bitfield_hex)
    return decode_bitfield_bytes(bitfield_data, piece_count)


def create_proxy(url: str) -> xmlrpc.client.ServerProxy:
    """Create XML-RPC proxy with SCGI support.

    Args:
        url: URL in format scgi://host:port or scgi:///path/to/socket or http://host:port/path

    Returns:
        ServerProxy instance
    """
    parsed = urlsplit(url)
    proto = url.split(":")[0].lower()
    if proto == "scgi":
        if parsed.netloc:
            proxy_url = f"http://{parsed.netloc}"
            return xmlrpc.client.ServerProxy(proxy_url, transport=SCGITransport())
        else:
            path = parsed.path
            return xmlrpc.client.ServerProxy("http://1", transport=SCGITransport(socket_path=path))
    else:
        return xmlrpc.client.ServerProxy(url)


class RTorrentClient(TorrentClient):
    """rTorrent torrent client implementation."""

    supports_final_directory = True

    def __init__(self, url: str):
        super().__init__()
        config = parse_libtc_url(url)
        self.torrents_dir = config.torrents_dir or ""

        # Monkey-patch xmlrpc.client to mitigate XML vulnerabilities
        defusedxml.xmlrpc.monkey_patch()

        # rTorrent uses XML-RPC with optional SCGI support
        self.client = create_proxy(config.url or "http://localhost:80/RPC2")

        # Use the field specifications constant
        self.field_config = _RTORRENT_FIELD_SPECS

    # region Abstract Methods - Public Operations

    async def get_torrents(
        self, torrent_hashes: list[str] | None = None, fields: list[str] | None = None
    ) -> list[ClientTorrentInfo]:
        """Get all torrents from rTorrent.

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

            if torrent_hashes:
                # If specific hashes are requested, use xmlrpc.client's MultiCall
                torrents_data = []
                for torrent_hash in torrent_hashes:
                    try:
                        # Use xmlrpc.client's MultiCall for single torrent info
                        multicall = xmlrpc.client.MultiCall(self.client)

                        # Add all method calls to the multicall
                        for arg in arguments:
                            getattr(multicall, arg)(torrent_hash.upper())

                        # Execute all calls at once
                        results = await asyncio.to_thread(multicall)

                        if results:
                            # Build result similar to d.multicall2 format
                            torrents_data.append(results)
                    except Exception as e:
                        # Skip torrents that don't exist or can't be accessed
                        logger.warning(f"Failed to get torrent {torrent_hash}: {e}")
                        continue
            else:
                # Get all torrents
                torrents_data = await asyncio.to_thread(
                    self.client.d.multicall2,
                    "",
                    "main",
                    *[f"{arg}=" for arg in arguments],
                )

            result = []
            if not isinstance(torrents_data, list):
                return result

            for torrent_data in torrents_data:
                try:
                    # Build torrent info dict from d.multicall2 result
                    torrent_dict = {arg: torrent_data[i] for i, arg in enumerate(arguments)}

                    # Extract values using field specifications
                    values = {field_name: spec.extractor(torrent_dict) for field_name, spec in field_config.items()}

                    # Handle additional multicalls if needed
                    torrent_hash = torrent_dict.get("d.hash", "")
                    directory = posixpath.basename(torrent_dict.get("d.directory", ""))
                    await self._populate_files_and_trackers(values, torrent_hash, directory, fields)

                    # Create ClientTorrentInfo object
                    torrent_info = ClientTorrentInfo(**values)
                    result.append(torrent_info)
                except Exception as e:
                    # Skip torrents that fail to parse
                    logger.warning(f"Failed to parse torrent data: {e}")
                    continue

            return result

        except Exception as e:
            logger.error("Error retrieving torrents from rTorrent: %s", e)
            return []

    async def get_torrents_for_monitoring(self, torrent_hashes: set[str]) -> dict[str, TorrentState]:
        """Get torrent states for monitoring (optimized for rTorrent).

        Uses xmlrpc.client's MultiCall to get only the required state information
        for monitoring.

        Args:
            torrent_hashes (set[str]): Set of torrent hashes to monitor.

        Returns:
            dict[str, TorrentState]: Mapping of torrent hash to current state.
        """
        # Define monitoring field configuration
        monitoring_fields = ["d.is_active", "d.is_open", "d.complete", "d.hashing"]

        result = {}
        for torrent_hash in torrent_hashes:
            try:
                # Use xmlrpc.client's MultiCall for batch operations
                multicall = xmlrpc.client.MultiCall(self.client)

                # Add all method calls to the multicall
                for field in monitoring_fields:
                    getattr(multicall, field)(torrent_hash.upper())

                # Execute all calls at once
                results = await asyncio.to_thread(multicall)

                is_active = results[0]
                is_open = results[1]
                complete = results[2]
                hashing = results[3]

                state = _get_rtorrent_state(is_active, is_open, complete, hashing)
                result[torrent_hash] = state

            except Exception as e:
                logger.warning(f"Error getting state for torrent {torrent_hash}: {e}")
                result[torrent_hash] = TorrentState.UNKNOWN
                continue

        return result

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

            # Use xmlrpc.client's MultiCall for single torrent info
            multicall = xmlrpc.client.MultiCall(self.client)

            # Add all method calls to the multicall
            for arg in arguments:
                # Call the method on the specific torrent hash
                getattr(multicall, arg)(torrent_hash.upper())

            # Execute all calls at once
            results = await asyncio.to_thread(multicall)

            if not results:
                return None

            # Build torrent info dict
            torrent_dict = {arg: results[i] for i, arg in enumerate(arguments)}

            # Build ClientTorrentInfo using field_config
            values = {field_name: spec.extractor(torrent_dict) for field_name, spec in field_config.items()}

            # Handle additional multicalls if needed
            directory = posixpath.basename(str(torrent_dict.get("d.directory", "")))
            await self._populate_files_and_trackers(values, torrent_hash, directory, fields)

            return ClientTorrentInfo(**values)
        except Exception as e:
            logger.error("Error retrieving torrent info from rTorrent: %s", e)
            return None

    async def _populate_files_and_trackers(
        self, values: dict, torrent_hash: str, directory: str, fields: list[str] | None
    ) -> None:
        """Populate files and trackers information in values dict.

        Args:
            values: Dictionary to populate with files and trackers
            torrent_hash: The torrent hash
            directory: The base directory name
            fields: List of field names to include, or None for all fields
        """
        # Handle files multicall if needed
        if fields is None or "files" in fields:
            values["files"] = await self._get_torrent_files(torrent_hash, directory)

        # Handle trackers multicall if needed
        if fields is None or "trackers" in fields:
            values["trackers"] = await self._get_torrent_trackers(torrent_hash)

    async def _get_torrent_files(self, torrent_hash: str, directory: str) -> list[ClientTorrentFile]:
        """Get files information for a torrent.

        Args:
            torrent_hash: The torrent hash
            directory: The base directory name

        Returns:
            List of ClientTorrentFile objects
        """
        try:
            files_data = await asyncio.to_thread(
                self.client.f.multicall,
                torrent_hash.upper(),
                "",
                "f.path=",
                "f.size_bytes=",
                "f.completed_chunks=",
                "f.size_chunks=",
            )
            if not isinstance(files_data, list):
                raise ValueError(f"Expected list of files data, got {type(files_data)}, data: {files_data}")
            return [
                ClientTorrentFile(
                    name=posixpath.join(directory, f[0]),
                    size=f[1],
                    progress=f[2] / f[3] if f[3] > 0 else 0.0,
                )
                for f in files_data
            ]
        except Exception as e:
            logger.error("Error retrieving torrent files: %s", e)
            return []

    async def _get_torrent_trackers(self, torrent_hash: str) -> list[str]:
        """Get tracker URLs for a torrent.

        Args:
            torrent_hash: The torrent hash

        Returns:
            List of tracker URLs
        """
        try:
            trackers_data = await asyncio.to_thread(self.client.t.multicall, torrent_hash.upper(), "", "t.url=")
            if not isinstance(trackers_data, list):
                raise ValueError(f"Expected list of trackers data, got {type(trackers_data)}, data: {trackers_data}")
            return [tracker[0] for tracker in trackers_data]
        except Exception as e:
            logger.error("Error retrieving torrent trackers: %s", e)
            return []

    # endregion

    # region Abstract Methods - Internal Operations

    async def _add_torrent(self, torrent_data: bytes, download_dir: str, hash_match: bool) -> str:
        """Add torrent to rTorrent with optional fast resume support.

        Args:
            torrent_data (bytes): Torrent file data.
            download_dir (str): Download directory.
            hash_match (bool): Whether this is a hash match, if True, skip verification.

        Returns:
            str: Torrent hash.
        """
        # Parse torrent to get hash and info
        torrent_obj = Torrent.read_stream(torrent_data)
        info_hash = torrent_obj.infohash
        torrent_bytes = torrent_data
        torrent_completed = False

        # If hash_match is True, add fast resume data
        if hash_match:
            logger.info("Adding fast resume data for hash-matched torrent")
            torrent_completed = True

            # Build libtorrent_resume data
            resume_files = []
            download_path = Path(download_dir)

            # Iterate through files using torf's filetree
            for torrent_file in torrent_obj.files:
                # Build file path
                file_path = download_path / "/".join(torrent_file.parts[1:])

                # Use async file operations to avoid blocking the event loop
                is_file = await aiofiles.os.path.isfile(file_path)
                if not is_file:
                    logger.warning("Torrent is incomplete, fallback to not using fast resume")
                    torrent_completed = False
                    break

                file_stat = await aiofiles.os.stat(file_path)
                if file_stat.st_size != torrent_file.size:
                    logger.warning("Torrent is incomplete, fallback to not using fast resume")
                    torrent_completed = False
                    break

                resume_files.append(
                    {
                        "priority": 1,
                        "completed": 1,
                        "mtime": int(file_stat.st_mtime),
                    }
                )

            # Set bitfield
            if torrent_completed:
                logger.info("Torrent is complete, setting bitfield to piece count")

                # Add resume data to metainfo
                metainfo = {
                    **torrent_obj.metainfo,
                    "libtorrent_resume": {
                        "files": resume_files,
                        "bitfield": torrent_obj.pieces,
                    },
                }

                # Create new torrent with modified metainfo
                modified_torrent = Torrent()
                modified_torrent._metainfo = metainfo  # type: ignore[attr-defined]
                encoded_torrent = modified_torrent.dump()
                torrent_bytes = encoded_torrent

        try:
            # Build command arguments for load.raw
            cmd = [torrent_bytes, f'd.directory_base.set="{download_dir}"']

            # Set label if provided
            label = config.cfg.downloader.label
            if label:
                cmd.append(f"d.custom1.set={label}")

            # Load torrent in stopped state (paused)
            if torrent_completed:
                # The only way to use fast resume information is to start downloading,
                # so if auto_start_torrents = false is set, we can only start then pause
                await asyncio.to_thread(self.client.load.raw_start, "", *cmd)
                if not config.cfg.global_config.auto_start_torrents:
                    await asyncio.sleep(1)
                    await asyncio.to_thread(self.client.d.pause, info_hash.upper())
            else:
                await asyncio.to_thread(self.client.load.raw, "", *cmd)

            return str(info_hash)

        except Exception as e:
            # Check if torrent already exists
            if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                error_msg = f"The torrent to be injected cannot coexist with local torrent {info_hash}"
                logger.error(error_msg)
                raise TorrentConflictError(error_msg) from e
            else:
                raise

    async def _remove_torrent(self, torrent_hash: str) -> None:
        """Remove torrent from rTorrent.

        Args:
            torrent_hash (str): Torrent hash.
        """
        try:
            # Erase torrent (without deleting files)
            await asyncio.to_thread(self.client.d.erase, torrent_hash.upper())
        except Exception as e:
            logger.error(f"Error removing torrent from rTorrent: {e}")

    async def _rename_torrent(self, torrent_hash: str, old_name: str, new_name: str) -> None:
        """Rename entire torrent.

        Since rTorrent supports specifying the final directory level when adding
        torrents, there is no need to rename after adding the torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_name (str): Old torrent name.
            new_name (str): New torrent name.

        Raises:
            NotImplementedError: rTorrent does not support torrent renaming.
        """
        logger.error(
            f"Torrent renaming should not be called for rTorrent: {torrent_hash} from '{old_name}' to '{new_name}'"
        )
        raise NotImplementedError(
            "rTorrent torrent renaming is not supported - use directory specification during adding torrent instead"
        )

    async def _rename_file(self, torrent_hash: str, old_path: str, new_name: str) -> None:
        """Rename file within torrent.

        rTorrent does not support renaming individual files within a torrent.

        Args:
            torrent_hash (str): Torrent hash.
            old_path (str): Old file path.
            new_name (str): New file name.

        Raises:
            NotImplementedError: rTorrent does not support file renaming.
        """
        logger.error(
            f"rTorrent does not support renaming individual files. "
            f"Attempted to rename {old_path} to {new_name} in torrent {torrent_hash}"
        )
        raise NotImplementedError("rTorrent does not support renaming individual files within a torrent.")

    async def _verify_torrent(self, torrent_hash: str) -> None:
        """Verify torrent integrity.

        Args:
            torrent_hash (str): Torrent hash.
        """
        try:
            await asyncio.to_thread(self.client.d.check_hash, torrent_hash.upper())
        except Exception as e:
            logger.error(f"Error verifying torrent in rTorrent: {e}")

    async def _process_rename_map(self, torrent_hash: str, base_path: str, rename_map: dict) -> dict:
        """Process rename mapping to adapt to rTorrent.

        rTorrent does not support renaming individual files within a torrent.

        Args:
            torrent_hash (str): Torrent hash.
            base_path (str): Base path for files.
            rename_map (dict): Original rename mapping.

        Returns:
            dict: Not implemented (rTorrent doesn't support file renaming).

        Raises:
            NotImplementedError: rTorrent does not support file renaming.
        """
        logger.error(
            "rTorrent does not support renaming individual files within a torrent. "
            "File renaming will be skipped. "
            "Consider enabling linking mode for proper file mapping."
        )
        raise NotImplementedError("rTorrent does not support renaming individual files within a torrent.")

    async def _get_torrent_data(self, torrent_hash: str) -> bytes | None:
        """Get torrent data from rTorrent.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bytes | None: Torrent file data, or None if not available.
        """
        try:
            torrent_path = Path(self.torrents_dir) / f"{torrent_hash}.torrent"
            async with aiofiles.open(torrent_path, "rb") as f:
                return await f.read()
        except Exception as e:
            logger.error(f"Error getting torrent data from rTorrent: {e}")
            return None

    async def _resume_torrent(self, torrent_hash: str) -> bool:
        """Resume downloading a torrent in rTorrent.

        Args:
            torrent_hash (str): Torrent hash.

        Returns:
            bool: True if successful, False otherwise.
        """
        try:
            # Start torrent
            await asyncio.to_thread(self.client.d.start, torrent_hash.upper())
            return True
        except Exception as e:
            logger.error(f"Failed to resume torrent {torrent_hash}: {e}")
            return False

    # endregion
