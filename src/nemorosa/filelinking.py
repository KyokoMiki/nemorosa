"""File linking system for nemorosa.

This module provides file linking functionality similar to cross-seed,
allowing files to be linked instead of copied to avoid duplicate storage.
"""

import errno
import os
import shutil
from urllib.parse import urlparse

from reflink_copy import reflink, reflink_or_copy
from torf import Torrent

from . import config, logger
from .config import LinkType


def _safe_stat_dev(path: str) -> int | None:
    """Safely get st_dev for a path, return None on error."""
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def _find_link_dir_by_device(source_dev: int, link_dirs: list[str]) -> str | None:
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
    dev_to_dir: dict[int, str] = {}
    for link_dir in link_dirs:
        if st_dev := _safe_stat_dev(link_dir):
            if st_dev in dev_to_dir:
                # Duplicate device found, cannot use device matching
                return None
            dev_to_dir[st_dev] = link_dir

    return dev_to_dir.get(source_dev)


def get_link_directory(source_path: str) -> str | None:
    """Get the appropriate link directory for a source path.

    Uses a strategy similar to cross-seed:
    1. Try device-based matching first (if st_dev is meaningful)
    2. Fall back to testing actual linking capability in each directory

    Args:
        source_path: Path to the source file

    Returns:
        Link directory path or None if no suitable directory found
    """
    # If linking is not enabled, return None
    if not config.cfg.linking.enable_linking:
        return None

    try:
        source_stat = os.stat(source_path)
        source_dev = source_stat.st_dev

        # Strategy 1: Try device-based matching (like cross-seed)
        if link_dir := _find_link_dir_by_device(
            source_dev, config.cfg.linking.link_dirs
        ):
            return link_dir

        # Strategy 2: Test actual linking capability in each directory
        # This works for Docker mounts, Windows, and other cases where
        # st_dev is not reliable
        for link_dir in config.cfg.linking.link_dirs:
            if _test_linking_in_directory(source_path, link_dir):
                return link_dir

        # If symlinks are allowed, we can use any directory
        if (
            config.cfg.linking.link_type == LinkType.SYMLINK
            and config.cfg.linking.link_dirs
        ):
            return config.cfg.linking.link_dirs[0]

        logger.warning(
            "No suitable link directory found for %s. Linking may fail for %s",
            source_path,
            config.cfg.linking.link_type,
        )
        return None

    except OSError as e:
        logger.error("Error determining link directory for %s: %s", source_path, e)
        return None


def _test_linking_in_directory(source_path: str, link_dir: str) -> bool:
    """Test if linking is possible between source and link directory.

    Args:
        source_path: Path to the source file
        link_dir: Link directory to test

    Returns:
        True if linking is possible, False otherwise
    """
    try:
        # Create a test file in the link directory
        test_file = os.path.join(link_dir, "test_linking.tmp")

        # Try to create a link (hardlink for testing)
        if create_file_link(source_path, test_file, LinkType.HARDLINK):
            # Clean up test file
            os.unlink(test_file)
            return True
        return False
    except Exception:
        return False


def create_file_link(
    source_path: str, dest_path: str, link_type: LinkType | None = None
) -> bool:
    """Create a file link from source to destination.

    Args:
        source_path: Path to the source file
        dest_path: Path to the destination link
        link_type: Type of link to create (uses config default if None)

    Returns:
        True if link was created successfully, False otherwise
    """
    if link_type is None:
        link_type = config.cfg.linking.link_type

    try:
        # Ensure destination directory exists
        dest_dir = os.path.dirname(dest_path)
        os.makedirs(dest_dir, mode=config.cfg.linking.dir_mode, exist_ok=True)

        # Check if destination already exists
        if os.path.exists(dest_path):
            logger.debug("Link already exists: %s", dest_path)
            return True

        # Resolve source path (unwind symlinks)
        resolved_source = os.path.realpath(source_path)

        # Create the appropriate type of link
        if link_type == LinkType.HARDLINK:
            os.link(resolved_source, dest_path)
        elif link_type == LinkType.SYMLINK:
            # Use absolute path for symlinks
            os.symlink(resolved_source, dest_path)
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
    source_dir: str, dest_dir: str, file_mapping: dict[str, str]
) -> dict[str, bool]:
    """Create links for multiple files in a directory structure.

    Args:
        source_dir: Source directory path
        dest_dir: Destination directory path
        file_mapping: Mapping of relative file paths to new names

    Returns:
        Dictionary mapping file paths to success status
    """
    results = {}

    for rel_path, new_name in file_mapping.items():
        source_path = os.path.join(source_dir, rel_path)
        dest_path = os.path.join(dest_dir, new_name)

        if os.path.exists(source_path):
            success = create_file_link(source_path, dest_path)
            results[rel_path] = success
        else:
            logger.warning("Source file not found: %s", source_path)
            results[rel_path] = False

    return results


def remove_links(link_dir: str, torrent_name: str) -> bool:
    """Remove all links for a specific torrent.

    Args:
        link_dir: Directory containing the links
        torrent_name: Name of the torrent to remove links for

    Returns:
        True if removal was successful, False otherwise
    """
    try:
        torrent_link_path = os.path.join(link_dir, torrent_name)
        if os.path.exists(torrent_link_path):
            if os.path.isdir(torrent_link_path):
                shutil.rmtree(torrent_link_path)
            else:
                os.unlink(torrent_link_path)
            logger.debug("Removed links for torrent: %s", torrent_name)
            return True
        return False
    except OSError as e:
        logger.error("Failed to remove links for %s: %s", torrent_name, e)
        return False


def create_file_links_for_torrent(
    torrent_object: Torrent,
    local_download_dir: str,
    local_torrent_name: str,
    file_mapping: dict,
) -> str | None:
    """Create file links for a torrent instead of renaming files.

    Args:
        torrent_object: Parsed torrent object
        local_download_dir: Download directory
        local_torrent_name: Torrent name
        file_mapping: File mapping for linking operations

    Returns:
        str | None: Path to the linked directory, or None if failed
    """
    try:
        # Extract tracker name from torrent data
        if not torrent_object.trackers or not torrent_object.trackers.flat:
            logger.warning("No trackers found in torrent")
            return None
        tracker = torrent_object.trackers.flat[0]
        tracker_name = urlparse(tracker).hostname or "unknown"

        original_download_dir = os.path.join(local_download_dir, local_torrent_name)

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
        final_download_dir = os.path.join(link_dir, tracker_name)
        torrent_link_dir = os.path.join(final_download_dir, torrent_object.name)

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
            return final_download_dir  # Return the linked directory path
        elif successful_links > 0:
            logger.warning(
                "Partially created file links: %s/%s for %s",
                successful_links,
                total_files,
                local_torrent_name,
            )
            return (
                final_download_dir  # Return the linked directory path even if partial
            )
        else:
            logger.error("Failed to create any file links for %s", local_torrent_name)
            return None

    except Exception as e:
        logger.error("Error creating file links for %s: %s", local_torrent_name, e)
        return None
