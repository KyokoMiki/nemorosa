import difflib
import posixpath
import re
from collections import defaultdict
from itertools import groupby
from typing import TYPE_CHECKING

from . import config, logger

if TYPE_CHECKING:
    from .clients import ClientTorrentInfo


def is_music_file(filename: str) -> bool:
    """Check if a file is a music file based on its extension.

    Args:
        filename (str): The filename to check.

    Returns:
        bool: True if the file is a music file, False otherwise.
    """
    return posixpath.splitext(filename)[1].lower() in [".flac", ".mp3", ".dsf", ".dff", ".m4a"]


def make_filename_query(filename: str) -> str:
    """Generate cleaned search query string from filename.

    Features:
    1. Remove path part, keep only filename
    2. Replace garbled characters with equal-length spaces
    3. Merge consecutive spaces into single space

    Args:
        filename (str): Original filename.

    Returns:
        str: Cleaned filename.
    """

    # Remove path part, keep only filename
    base_filename = posixpath.basename(filename)

    # Replace common garbled characters and special symbols with equal-length spaces
    # Including: question marks, Chinese question marks, consecutive underscores, brackets, etc.
    sanitized_name = base_filename

    # Replace common garbled characters and invisible characters with equal-length spaces
    # Including zero-width spaces, control characters, and other invisible Unicode characters
    sanitized_name = re.sub(
        r'[?？�_\-\.·~`!@#$%^&*+=|\\:";\'<>?,/\u200b\u200c\u200d\u2060\ufeff\u00a0\u180e\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\u0000-\u001f\u007f-\u009f]',
        " ",
        sanitized_name,
    )

    # Finally merge consecutive multiple spaces into single space
    sanitized_name = re.sub(r"\s+", " ", sanitized_name).strip()

    return sanitized_name


class DiffResult:
    """Store diff analysis result."""

    def __init__(self, prefix: str, suffix: str):
        self.prefix = prefix
        self.suffix = suffix

    def __str__(self):
        return f"DiffResult(prefix='{self.prefix}', suffix='{self.suffix}')"


def find_common_prefix(a: str, b: str) -> str:
    """Find common prefix of two strings and remove trailing digits."""
    min_length = min(len(a), len(b))
    prefix = a[:min_length]

    for i in range(min_length):
        if a[i].lower() != b[i].lower():
            prefix = a[:i]
            break

    # Remove trailing digits
    prefix = re.sub(r"\d+$", "", prefix)
    return prefix


def find_common_suffix(a: str, b: str) -> str:
    """Find common suffix of two strings, skipping ASCII letters and digits."""
    i = 0
    j = 0

    while i < len(a) and j < len(b):
        # Skip ASCII letters and digits
        while i < len(a) and a[i].isascii() and (a[i].isalpha() or a[i].isdigit()):
            i += 1
        while j < len(b) and b[j].isascii() and (b[j].isalpha() or b[j].isdigit()):
            j += 1

        # If both are still in valid range, compare current character
        if i < len(a) and j < len(b) and a[i].lower() == b[j].lower():
            return a[i]

        i += 1
        j += 1

    return ""


def get_diff_result(names: list[str]) -> DiffResult | None:
    """Analyze filename list to find common prefix and suffix patterns."""
    names = list(set(names))  # Remove duplicates
    if len(names) < 2:
        return None

    for i in range(len(names) - 1):
        for j in range(len(names) - 1, i, -1):  # Start from end to avoid two names too similar
            prefix = find_common_prefix(names[i], names[j])
            suffix = find_common_suffix(names[i][len(prefix) :], names[j][len(prefix) :])

            if prefix:  # If common prefix found
                return DiffResult(prefix, suffix)

    return None


def extract_match_key_by_diff(diff: DiffResult | None, filename: str) -> str:
    """Extract match key from filename based on diff analysis result."""
    if diff is None:
        # If no diff analysis result, use simple number matching
        pattern = r"(\d+)(?!.*\d)"  # Match last number
    else:
        if not diff.suffix:
            pattern = f"{re.escape(diff.prefix)}(\\d+)"
        else:
            pattern = f"{re.escape(diff.prefix)}(.+?){re.escape(diff.suffix)}"

    match = re.search(pattern, filename, re.IGNORECASE)
    if match and match.groups():
        key = match.group(1).strip()
        # If pure number, remove leading zeros
        if key.isdigit():
            key = str(int(key))
        return key

    return ""


def calculate_file_keys(files: list[str]) -> dict:
    """Calculate match keys for file list."""
    result = {}

    # Get filenames without extensions
    filenames = []
    for file in files:
        name = file.rsplit(".", 1)[0] if "." in file else file
        if name:
            filenames.append(name)

    if not filenames:
        return result

    # Analyze filename differences
    diff = get_diff_result(filenames)

    # Extract match key for each file
    for file in files:
        name = file.rsplit(".", 1)[0] if "." in file else file
        result[file] = extract_match_key_by_diff(diff, name)

    return result


def filename_match(torrent_name: str, local_names: list[str]) -> str | None:
    """Use pattern-based algorithm to match filenames, replacing simple similarity comparison."""
    if not local_names:
        return None

    if len(local_names) == 1:
        return local_names[0]

    # Calculate match keys for all files
    all_files = [torrent_name] + local_names
    file_keys = calculate_file_keys(all_files)

    torrent_key = file_keys.get(torrent_name, "")

    # If torrent file has key, try to match local files with same key
    if torrent_key:
        for local_name in local_names:
            local_key = file_keys.get(local_name, "")
            if local_key == torrent_key:
                return local_name

    # If no key match, fallback to similarity comparison
    best_match = None
    best_similarity = -1

    for local_name in local_names:
        similarity = difflib.SequenceMatcher(None, local_name, torrent_name).ratio()
        if similarity > best_similarity:
            best_similarity = similarity
            best_match = local_name

    return best_match


def check_conflicts(fdict_local, fdict_torrent):
    """Detect conflicts of same-name files with different sizes.

    Args:
        fdict_local (dict): Local file dictionary.
        fdict_torrent (dict): Torrent file dictionary.

    Returns:
        bool: True if conflicts exist, False otherwise.
    """
    for name, size in fdict_torrent.items():
        if name in fdict_local and fdict_local[name] != size:
            logger.get_logger().error(
                f"File conflict detected! File: {name}, Local size: {fdict_local[name]}, Torrent size: {size}"
            )
            return True
    return False


def generate_rename_map(fdict_local, fdict_torrent):
    """Generate rename mapping from fdict_local to fdict_torrent.

    Args:
        fdict_local (dict): Local file dictionary.
        fdict_torrent (dict): Remote torrent file dictionary.

    Returns:
        dict: Rename mapping dictionary, format like {"1.flac": "1-1.flac", "2.flac": "1-2.flac"}.
    """
    # Step 1: Create remaining file dictionary (remove same-name files)
    remaining_local = fdict_local.copy()
    remaining_torrent = fdict_torrent.copy()

    # Remove same-name files (regardless of size)
    for name in fdict_torrent:
        if name in fdict_local:
            del remaining_local[name]
            del remaining_torrent[name]

    # Group local files by file size
    size_map_local = defaultdict(list)
    for name, size in remaining_local.items():
        size_map_local[size].append(name)

    # Initialize rename mapping
    rename_map = {}

    # Traverse torrent file dictionary
    for remote_filename, remote_filesize in remaining_torrent.items():
        # Check if there are local files with same size
        if remote_filesize in size_map_local:
            local_names = size_map_local[remote_filesize]

            if len(local_names) == 1:
                # Unique match: directly establish mapping
                local_name = local_names[0]
                rename_map[remote_filename] = local_name

            elif len(local_names) > 1:
                # Multiple files with same size: use pattern-based filename matching
                best_match = filename_match(remote_filename, local_names)

                # If match found
                if best_match:
                    rename_map[remote_filename] = best_match

                    # Remove matched files
                    size_map_local[remote_filesize].remove(best_match)

                    # If this size group is empty, remove entire group
                    if not size_map_local[remote_filesize]:
                        del size_map_local[remote_filesize]

    return rename_map


def should_keep_partial_torrent(torrent: "ClientTorrentInfo") -> bool:
    """Check if a partial torrent should be kept based on piece and file progress patterns.

    Compares the number of continuous undownloaded blocks with the number of files
    that have zero progress. If there are more undownloaded blocks than zero-progress
    files, it indicates a conflict.

    Args:
        torrent: The torrent to analyze.

    Returns:
        bool: True if torrent should be kept, False if it should be removed.
    """
    if not torrent.piece_progress or not torrent.files:
        return False

    # Count continuous blocks of undownloaded pieces (False values)
    undownloaded_blocks_count = sum(1 for value, _ in groupby(torrent.piece_progress) if not value)

    # Count continuous blocks of files with zero progress (completely undownloaded)
    zero_progress_count = sum(1 for value, _ in groupby(torrent.files, key=lambda f: f.progress == 0.0) if value)

    # Check for conflicts: number of continuous undownloaded blocks should not exceed
    # the number of files with zero progress
    return undownloaded_blocks_count <= zero_progress_count


def generate_link_map(fdict_local: dict, fdict_torrent: dict) -> dict:
    """Generate link mapping from local files to torrent files.

    Args:
        fdict_local (dict): Local file dictionary.
        fdict_torrent (dict): Remote torrent file dictionary.

    Returns:
        dict: Link mapping dictionary, format like {"local.flac": "remote.flac"}.
    """
    link_map = {}

    # Step 1: Create remaining file dictionary (remove same-name files)
    remaining_local = fdict_local.copy()
    remaining_torrent = fdict_torrent.copy()

    # When using reflink, allow same-name files with different sizes to be treated as matches
    if config.cfg.linking.link_type in ["reflink", "reflink_or_copy"]:
        for name in fdict_torrent:
            if name in fdict_local:
                link_map[name] = name
                del remaining_local[name]
                del remaining_torrent[name]

    # Group remote files by file size
    size_map_remote = defaultdict(list)
    for name, size in remaining_torrent.items():
        size_map_remote[size].append(name)

    # Traverse local file dictionary
    for local_filename, local_filesize in remaining_local.items():
        if local_filesize in size_map_remote:
            remote_names = size_map_remote[local_filesize]

            if len(remote_names) == 1:
                remote_name = remote_names[0]
                link_map[local_filename] = remote_name

            elif len(remote_names) > 1:
                best_match = filename_match(local_filename, remote_names)
                if best_match:
                    link_map[local_filename] = best_match

                    # Remove matched files
                    size_map_remote[local_filesize].remove(best_match)

                    # If this size group is empty, remove entire group
                    if not size_map_remote[local_filesize]:
                        del size_map_remote[local_filesize]

    return link_map
