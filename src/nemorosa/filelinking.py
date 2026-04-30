"""File linking system for nemorosa.

This module provides file linking functionality similar to cross-seed,
allowing files to be linked instead of copied to avoid duplicate storage.
"""

import errno
import shutil
import uuid
from pathlib import Path
from urllib.parse import urlparse

from reflink_copy import reflink, reflink_or_copy
from torf import Torrent

from . import config, logger
from .config import LinkType


def _safe_stat_dev(path: Path) -> int | None:
    """Safely get st_dev for a path, return None on error."""
    try:
        return path.stat().st_dev
    except OSError:
        return None


def _find_link_dir_by_device(source_dev: int, link_dirs: list[str]) -> Path | None:
    """Find link directory by matching device ID.

    On Windows, st_dev always returns 0, so this method will not find a match.
    If duplicate devices are found among link directories, returns None.

    Args:
        source_dev: Device ID of the source path.
        link_dirs: List of link directories to check.

    Returns:
        Matching link directory path, or None if no match found.
    """
    if source_dev == 0:
        return None

    # Build device to directory mapping, abort if duplicates found
    dev_to_dir: dict[int, Path] = {}
    for link_dir in link_dirs:
        path = Path(link_dir)
        if st_dev := _safe_stat_dev(path):
            if st_dev in dev_to_dir:
                # Duplicate device found, cannot use device matching
                return None
            dev_to_dir[st_dev] = path

    return dev_to_dir.get(source_dev)


def get_link_directory(source_path: Path) -> Path | None:
    """Get the appropriate link directory for a source path.

    Uses a strategy similar to cross-seed:
    1. Try device-based matching first (if st_dev is meaningful)
    2. Fall back to testing actual linking capability in each directory

    Args:
        source_path: Path to the source file.

    Returns:
        Link directory path or None if no suitable directory found.
    """
    # If linking is not enabled, return None
    if not config.cfg.linking.enable_linking:
        return None

    try:
        source_dev = source_path.stat().st_dev

        # Strategy 1: Try device-based matching (like cross-seed)
        if link_dir := _find_link_dir_by_device(
            source_dev, config.cfg.linking.link_dirs
        ):
            return link_dir

        # Strategy 2: Test actual linking capability in each directory
        # This works for Docker mounts, Windows, and other cases where
        # st_dev is not reliable
        for link_dir_str in config.cfg.linking.link_dirs:
            link_dir_path = Path(link_dir_str)
            if _test_linking_in_directory(source_path, link_dir_path):
                return link_dir_path

        # If symlinks are allowed, we can use any directory
        if (
            config.cfg.linking.link_type == LinkType.SYMLINK
            and config.cfg.linking.link_dirs
        ):
            return Path(config.cfg.linking.link_dirs[0])

        logger.warning(
            "No suitable link directory found for %s. Linking may fail for %s",
            source_path,
            config.cfg.linking.link_type,
        )
        return None

    except OSError as e:
        logger.error("Error determining link directory for %s: %s", source_path, e)
        return None


def _test_linking_in_directory(source_path: Path, link_dir: Path) -> bool:
    """Test if linking is possible between source and link directory.

    Args:
        source_path: Path to the source file.
        link_dir: Link directory to test.

    Returns:
        True if linking is possible, False otherwise.
    """
    try:
        test_file = link_dir / f"test_linking_{uuid.uuid4()}.tmp"

        # Try to create a link (hardlink for testing)
        if create_file_link(source_path, test_file, LinkType.HARDLINK):
            test_file.unlink()
            return True
        return False
    except Exception:
        return False


def create_file_link(
    source_path: Path, dest_path: Path, link_type: LinkType | None = None
) -> bool:
    """Create a file link from source to destination.

    Args:
        source_path: Path to the source file.
        dest_path: Path to the destination link.
        link_type: Type of link to create (uses config default if None).

    Returns:
        True if link was created successfully, False otherwise.
    """
    if link_type is None:
        link_type = config.cfg.linking.link_type

    try:
        # Ensure destination directory exists
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Check if destination already exists
        if dest_path.exists():
            logger.debug("Link already exists: %s", dest_path)
            return True

        # Resolve source path (unwind symlinks)
        resolved_source = source_path.resolve()

        # Create the appropriate type of link
        if link_type == LinkType.HARDLINK:
            dest_path.hardlink_to(resolved_source)
        elif link_type == LinkType.SYMLINK:
            # Use absolute path for symlinks
            dest_path.symlink_to(resolved_source)
        elif link_type == LinkType.REFLINK:
            # Strict reflink: raise if not supported
            reflink(resolved_source, dest_path)
        elif link_type == LinkType.REFLINK_OR_COPY:
            # Reflink with fallback to copy
            reflink_or_copy(resolved_source, dest_path)

        logger.debug("Created %s link: %s -> %s", link_type, source_path, dest_path)
        return True

    except OSError as e:
        if e.errno == errno.EEXIST:
            return True  # File already exists
        logger.error(
            "Failed to create %s link %s -> %s: %s",
            link_type,
            source_path,
            dest_path,
            e,
        )
        return False
    except Exception as e:
        logger.error(
            "Unexpected error creating link %s -> %s: %s", source_path, dest_path, e
        )
        return False


def create_directory_links(
    source_dir: Path, dest_dir: Path, file_mapping: dict[str, str]
) -> dict[str, bool]:
    """Create links for multiple files in a directory structure.

    Args:
        source_dir: Source directory path.
        dest_dir: Destination directory path.
        file_mapping: Mapping of relative file paths to new names.

    Returns:
        Dictionary mapping file paths to success status.
    """
    results = {}

    for rel_path, new_name in file_mapping.items():
        source_path = source_dir / rel_path
        dest_path = dest_dir / new_name

        if source_path.exists():
            success = create_file_link(source_path, dest_path)
            results[rel_path] = success
        else:
            logger.warning("Source file not found: %s", source_path)
            results[rel_path] = False

    return results


def remove_links(link_dir: Path, torrent_name: str) -> bool:
    """Remove all links for a specific torrent.

    Args:
        link_dir: Directory containing the links.
        torrent_name: Name of the torrent to remove links for.

    Returns:
        True if removal was successful, False otherwise.
    """
    try:
        torrent_link_path = link_dir / torrent_name
        if torrent_link_path.exists():
            if torrent_link_path.is_dir():
                shutil.rmtree(torrent_link_path)
            else:
                torrent_link_path.unlink()
            logger.debug("Removed links for torrent: %s", torrent_name)
            return True
        return False
    except OSError as e:
        logger.error("Failed to remove links for %s: %s", torrent_name, e)
        return False


def create_file_links_for_torrent(
    torrent_object: Torrent,
    local_download_dir: Path,
    local_torrent_name: str,
    file_mapping: dict,
) -> Path | None:
    """Create file links for a torrent instead of renaming files.

    Args:
        torrent_object: Parsed torrent object.
        local_download_dir: Download directory.
        local_torrent_name: Torrent name.
        file_mapping: File mapping for linking operations.

    Returns:
        Parent directory of the linked torrent (link_dir/tracker), or None if failed.
    """
    try:
        # Extract tracker name from torrent data
        if not torrent_object.trackers or not torrent_object.trackers.flat:
            logger.warning("No trackers found in torrent")
            return None
        tracker = torrent_object.trackers.flat[0]
        hostname = urlparse(tracker).hostname
        tracker_name = (
            config.cfg.linking.tracker_aliases.get(hostname, hostname)
            if hostname
            else "unknown"
        )

        original_download_dir = local_download_dir / local_torrent_name

        # Get the link directory
        link_dir = get_link_directory(original_download_dir)
        if not link_dir:
            logger.warning(
                "No suitable link directory found for %s", original_download_dir
            )
            return None

        if torrent_object.name is None:
            logger.warning("No torrent name found")
            return None

        # Create torrent-specific directory (following cross-seed structure)
        final_download_dir = link_dir / tracker_name
        torrent_link_dir = final_download_dir / torrent_object.name

        # Create directory links
        results = create_directory_links(
            original_download_dir, torrent_link_dir, file_mapping
        )

        # Check results
        successful_links = sum(1 for success in results.values() if success)
        total_files = len(results)

        if successful_links == total_files:
            logger.info(
                "Successfully created all %s file links for %s",
                total_files,
                local_torrent_name,
            )
            return final_download_dir
        elif successful_links > 0:
            logger.warning(
                "Partially created file links: %s/%s for %s",
                successful_links,
                total_files,
                local_torrent_name,
            )
            return final_download_dir
        else:
            logger.error("Failed to create any file links for %s", local_torrent_name)
            return None

    except Exception as e:
        logger.error("Error creating file links for %s: %s", local_torrent_name, e)
        return None
