"""File linking system for nemorosa.

This module provides file linking functionality similar to cross-seed,
allowing files to be linked instead of copied to avoid duplicate storage.
"""

import errno
import os
import shutil
from enum import Enum
from urllib.parse import urlparse

import torf
from reflink_copy import reflink, reflink_or_copy

from . import config, logger


def _safe_stat_dev(path: str) -> int | None:
    """Safely get st_dev for a path, return None on error."""
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


class LinkType(Enum):
    """File linking types."""

    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    REFLINK = "reflink"
    REFLINK_OR_COPY = "reflink_or_copy"


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

    log = logger.get_logger()

    try:
        source_stat = os.stat(source_path)
        source_dev = source_stat.st_dev

        # Strategy 1: Try device-based matching (like cross-seed)
        # On Windows, st_dev always returns 0, so this will be skipped
        if source_dev != 0:
            # Build device to directory mapping, abort if duplicates found
            dev_to_dir = {}
            for link_dir in config.cfg.linking.link_dirs:
                if st_dev := _safe_stat_dev(link_dir):
                    if st_dev in dev_to_dir:
                        # Duplicate device found, cannot use device matching
                        dev_to_dir = {}
                        break
                    dev_to_dir[st_dev] = link_dir

            # Try to find matching device
            if source_dev in dev_to_dir:
                return dev_to_dir[source_dev]

        # Strategy 2: Test actual linking capability in each directory
        # This works for Docker mounts, Windows, and other cases where st_dev is not reliable
        for link_dir in config.cfg.linking.link_dirs:
            if _test_linking_in_directory(source_path, link_dir):
                return link_dir

        # If symlinks are allowed, we can use any directory
        if config.cfg.linking.link_type == LinkType.SYMLINK.value:
            return config.cfg.linking.link_dirs[0]

        log.warning(
            f"No suitable link directory found for {source_path}. Linking may fail for {config.cfg.linking.link_type}"
        )
        return None

    except OSError as e:
        log.error(f"Error determining link directory for {source_path}: {e}")
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


def create_file_link(source_path: str, dest_path: str, link_type: LinkType | None = None) -> bool:
    """Create a file link from source to destination.

    Args:
        source_path: Path to the source file
        dest_path: Path to the destination link
        link_type: Type of link to create (uses config default if None)

    Returns:
        True if link was created successfully, False otherwise
    """
    if link_type is None:
        link_type = LinkType(config.cfg.linking.link_type)

    log = logger.get_logger()
    try:
        # Ensure destination directory exists
        dest_dir = os.path.dirname(dest_path)
        os.makedirs(dest_dir, exist_ok=True)

        # Check if destination already exists
        if os.path.exists(dest_path):
            log.debug(f"Link already exists: {dest_path}")
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

        log.debug(f"Created {link_type.value} link: {source_path} -> {dest_path}")
        return True

    except OSError as e:
        if e.errno == errno.EEXIST:
            return True  # File already exists
        log.error(f"Failed to create {link_type.value} link {source_path} -> {dest_path}: {e}")
        return False
    except Exception as e:
        log.error(f"Unexpected error creating link {source_path} -> {dest_path}: {e}")
        return False


def create_directory_links(source_dir: str, dest_dir: str, file_mapping: dict[str, str]) -> dict[str, bool]:
    """Create links for multiple files in a directory structure.

    Args:
        source_dir: Source directory path
        dest_dir: Destination directory path
        file_mapping: Mapping of relative file paths to new names

    Returns:
        Dictionary mapping file paths to success status
    """
    log = logger.get_logger()
    results = {}

    for rel_path, new_name in file_mapping.items():
        source_path = os.path.join(source_dir, rel_path)
        dest_path = os.path.join(dest_dir, new_name)

        if os.path.exists(source_path):
            success = create_file_link(source_path, dest_path)
            results[rel_path] = success
        else:
            log.warning(f"Source file not found: {source_path}")
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
    log = logger.get_logger()
    try:
        torrent_link_path = os.path.join(link_dir, torrent_name)
        if os.path.exists(torrent_link_path):
            if os.path.isdir(torrent_link_path):
                shutil.rmtree(torrent_link_path)
            else:
                os.unlink(torrent_link_path)
            log.debug(f"Removed links for torrent: {torrent_name}")
            return True
        return False
    except OSError as e:
        log.error(f"Failed to remove links for {torrent_name}: {e}")
        return False


def test_linking_capability(test_dir: str) -> bool:
    """Test if linking is possible in a directory.

    Args:
        test_dir: Directory to test

    Returns:
        True if linking is possible, False otherwise
    """
    # If linking is not enabled, return False
    if not config.cfg.linking.enable_linking:
        return False

    log = logger.get_logger()
    try:
        # Create a test file
        test_file = os.path.join(test_dir, "test_linking.tmp")
        with open(test_file, "w") as f:
            f.write("test")

        # Try to create a link in each link directory
        for link_dir in config.cfg.linking.link_dirs:
            test_link = os.path.join(link_dir, "test_link.tmp")
            try:
                if create_file_link(test_file, test_link):
                    os.unlink(test_link)
                    os.unlink(test_file)
                    return True
            except OSError:
                continue

        # Clean up
        if os.path.exists(test_file):
            os.unlink(test_file)

        return False

    except Exception as e:
        log.error(f"Error testing linking capability: {e}")
        return False


def create_file_links_for_torrent(
    torrent_object: torf.Torrent, local_download_dir: str, local_torrent_name: str, file_mapping: dict
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
    log = logger.get_logger()
    try:
        # Extract tracker name from torrent data
        if not torrent_object.trackers or not torrent_object.trackers.flat:
            log.warning("No trackers found in torrent")
            return None
        tracker = torrent_object.trackers.flat[0]
        tracker_name = urlparse(tracker).hostname or "unknown"

        original_download_dir = os.path.join(local_download_dir, local_torrent_name)

        # Get the link directory
        link_dir = get_link_directory(original_download_dir)
        if not link_dir:
            log.warning(f"No suitable link directory found for {original_download_dir}")
            return None

        if torrent_object.name is None:
            log.warning("No torrent name found")
            return None

        # Create torrent-specific directory (following cross-seed structure)
        final_download_dir = os.path.join(link_dir, tracker_name)
        torrent_link_dir = os.path.join(final_download_dir, torrent_object.name)

        # Create directory links
        results = create_directory_links(original_download_dir, torrent_link_dir, file_mapping)

        # Check results
        successful_links = sum(1 for success in results.values() if success)
        total_files = len(results)

        if successful_links == total_files:
            log.info(f"Successfully created all {total_files} file links for {local_torrent_name}")
            return final_download_dir  # Return the linked directory path
        elif successful_links > 0:
            log.warning(f"Partially created file links: {successful_links}/{total_files} for {local_torrent_name}")
            return final_download_dir  # Return the linked directory path even if partial
        else:
            log.error(f"Failed to create any file links for {local_torrent_name}")
            return None

    except Exception as e:
        log.error(f"Error creating file links for {local_torrent_name}: {e}")
        return None
