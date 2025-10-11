"""
Database operation module - replacing JSON file storage.
Provides SQLite storage functionality for torrent scan history, result mapping, URL records and other data.
"""

import os
import sqlite3
import threading
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any

import msgspec

from . import config

if TYPE_CHECKING:
    from .clients.client_common import ClientTorrentInfo


class TorrentDatabase:
    """Torrent database management class."""

    def __init__(self, db_path: str | None = None):
        """Initialize database connection.

        Args:
            db_path: Database file path, if None uses config directory.
        """
        if db_path is None:
            config_dir = config.get_config_dir()
            db_path = os.path.join(config_dir, "nemorosa.db")

        self.db_path = db_path
        self._local = threading.local()

        # Ensure database directory exists
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        # Initialize database tables
        self._init_database()

    @property
    def connection(self):
        """Get thread-local database connection.

        Returns:
            sqlite3.Connection: Thread-local database connection.
        """
        if not hasattr(self._local, "connection"):
            self._local.connection = sqlite3.connect(self.db_path)
            self._local.connection.row_factory = sqlite3.Row
        return self._local.connection

    @contextmanager
    def transaction(self):
        """Database transaction context manager.

        Yields:
            sqlite3.Connection: Database connection within transaction.
        """
        conn = self.connection
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _init_database(self):
        """Initialize database table structure."""
        with self.transaction() as conn:
            # Scan results table - merge original scan_history, torrent_mapping, torrent_results
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scan_results (
                    local_torrent_hash TEXT NOT NULL,
                    local_torrent_name TEXT,
                    site_host TEXT DEFAULT 'default',
                    matched_torrent_id TEXT,
                    matched_torrent_hash TEXT,
                    checked BOOLEAN NOT NULL DEFAULT FALSE,
                    scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (local_torrent_hash, site_host)
                )
            """)

            # Undownloaded torrents table - record detailed information of undownloaded torrents
            conn.execute("""
                CREATE TABLE IF NOT EXISTS undownloaded_torrents (
                    torrent_id TEXT NOT NULL,
                    site_host TEXT DEFAULT 'default',
                    download_dir TEXT,
                    local_torrent_name TEXT,
                    rename_map TEXT,  -- JSON format storage for rename mapping
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (torrent_id, site_host)
                )
            """)

            # Job log table - for scheduler job tracking
            conn.execute("""
                CREATE TABLE IF NOT EXISTS job_log (
                    job_name TEXT PRIMARY KEY,
                    last_run TIMESTAMP,
                    next_run TIMESTAMP,
                    run_count INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Client torrents cache table - cache static torrent information from client
            conn.execute("""
                CREATE TABLE IF NOT EXISTS client_torrents (
                    hash TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    total_size INTEGER NOT NULL,
                    download_dir TEXT,
                    trackers TEXT,  -- JSON array of tracker URLs
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Torrent files table - file index for fast searching
            conn.execute("""
                CREATE TABLE IF NOT EXISTS torrent_files (
                    torrent_hash TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    PRIMARY KEY (torrent_hash, file_path),
                    FOREIGN KEY (torrent_hash) REFERENCES client_torrents(hash) ON DELETE CASCADE
                )
            """)

            # Create indexes to improve query performance
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_scan_results_matched_checked "
                "ON scan_results(matched_torrent_hash, checked)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_undownloaded_site_host ON undownloaded_torrents(site_host)")

            # Indexes for client torrents cache
            conn.execute("CREATE INDEX IF NOT EXISTS idx_torrent_files_size ON torrent_files(file_size)")

    # region Scan results

    def add_scan_result(
        self,
        local_torrent_hash: str,
        local_torrent_name: str | None = None,
        matched_torrent_id: str | None = None,
        site_host: str = "default",
        matched_torrent_hash: str | None = None,
    ):
        """Add scan result record.

        Args:
            local_torrent_hash (str): Local torrent hash.
            local_torrent_name (str, optional): Local torrent name.
            matched_torrent_id (str, optional): Matched torrent ID (can be None to indicate not found).
            site_host (str): Site hostname.
            matched_torrent_hash (str, optional): Matched torrent hash.
        """
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO scan_results "
                "(local_torrent_hash, local_torrent_name, matched_torrent_id, site_host, "
                "matched_torrent_hash) VALUES (?, ?, ?, ?, ?)",
                (local_torrent_hash, local_torrent_name, matched_torrent_id, site_host, matched_torrent_hash),
            )

    def is_hash_scanned(self, local_torrent_hash: str, site_host: str) -> bool:
        """Check if specified local torrent hash has been scanned on specific site.

        Args:
            local_torrent_hash (str): Local torrent hash.
            site_host (str): Site hostname.

        Returns:
            bool: True if scanned on the specific site, False otherwise.
        """
        with self.connection as conn:
            cursor = conn.execute(
                "SELECT 1 FROM scan_results WHERE local_torrent_hash = ? AND site_host = ? LIMIT 1",
                (local_torrent_hash, site_host),
            )
            return cursor.fetchone() is not None

    # endregion

    # region Undownloaded torrents

    def load_undownloaded_torrents(self, site_host: str = "default") -> dict[str, dict[str, Any]]:
        """Load undownloaded torrent information for specified site.

        Args:
            site_host (str): Site hostname, defaults to 'default'.

        Returns:
            dict[str, dict[str, Any]]: Mapping dictionary from torrent ID to detailed information.
        """
        with self.connection as conn:
            cursor = conn.execute(
                "SELECT torrent_id, download_dir, local_torrent_name, rename_map "
                "FROM undownloaded_torrents WHERE site_host = ?",
                (site_host,),
            )
            result = {}
            for row in cursor.fetchall():
                result[row["torrent_id"]] = {
                    "download_dir": row["download_dir"],
                    "local_torrent_name": row["local_torrent_name"],
                    "rename_map": msgspec.json.decode(row["rename_map"]) if row["rename_map"] else {},
                }
            return result

    def add_undownloaded_torrent(self, torrent_id: str, torrent_info: dict, site_host: str = "default"):
        """Add undownloaded torrent information.

        Args:
            torrent_id (str): Torrent ID.
            torrent_info (dict): Dictionary containing download_dir, local_torrent_name, rename_map.
            site_host (str): Site hostname.
        """
        with self.transaction() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO undownloaded_torrents 
                   (torrent_id, site_host, download_dir, local_torrent_name, rename_map) 
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    torrent_id,
                    site_host,
                    torrent_info.get("download_dir"),
                    torrent_info.get("local_torrent_name"),
                    msgspec.json.encode(torrent_info.get("rename_map", {})).decode(),
                ),
            )

    def remove_undownloaded_torrent(self, torrent_id: str, site_host: str = "default"):
        """Remove specified torrent from undownloaded torrents table.

        Args:
            torrent_id (str): Torrent ID.
            site_host (str): Site hostname.
        """
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM undownloaded_torrents WHERE torrent_id = ? AND site_host = ?",
                (torrent_id, site_host),
            )

    def get_matched_scan_results(self) -> dict[str, dict[str, Any]]:
        """Get scan results with matched torrent hash for all sites that haven't been checked.

        Returns:
            dict[str, dict[str, Any]]: Dictionary mapping matched_torrent_hash to scan result information.
        """
        with self.connection as conn:
            cursor = conn.execute(
                "SELECT local_torrent_hash, local_torrent_name, matched_torrent_id, matched_torrent_hash, site_host "
                "FROM scan_results WHERE matched_torrent_hash IS NOT NULL AND checked = FALSE"
            )
            result = {}
            for row in cursor.fetchall():
                result[row["matched_torrent_hash"]] = {
                    "local_torrent_hash": row["local_torrent_hash"],
                    "local_torrent_name": row["local_torrent_name"],
                    "matched_torrent_id": row["matched_torrent_id"],
                    "site_host": row["site_host"],
                }
            return result

    def update_scan_result_checked(self, matched_torrent_hash: str, checked: bool):
        """Update checked status for a scan result.

        Args:
            matched_torrent_hash (str): Matched torrent hash.
            checked (bool): Checked status.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE scan_results SET checked = ? WHERE matched_torrent_hash = ?",
                (checked, matched_torrent_hash),
            )

    def clear_matched_torrent_info(self, matched_torrent_hash: str):
        """Clear matched torrent information for a scan result.

        Args:
            matched_torrent_hash (str): Matched torrent hash.
        """
        with self.transaction() as conn:
            conn.execute(
                "UPDATE scan_results SET matched_torrent_id = NULL, matched_torrent_hash = NULL "
                "WHERE matched_torrent_hash = ?",
                (matched_torrent_hash,),
            )

    # endregion

    # region Job log

    def get_job_last_run(self, job_name: str) -> int | None:
        """Get last run timestamp for a job.

        Args:
            job_name (str): Name of the job.

        Returns:
            int | None: Last run timestamp in seconds since epoch, or None if never run.
        """
        with self.connection as conn:
            cursor = conn.execute("SELECT last_run FROM job_log WHERE job_name = ?", (job_name,))
            result = cursor.fetchone()
            return result[0] if result else None

    def update_job_run(self, job_name: str, last_run: int, next_run: int | None = None):
        """Update job run information.

        Args:
            job_name (str): Name of the job.
            last_run (int): Last run timestamp in seconds since epoch.
            next_run (int | None): Next run timestamp in seconds since epoch, or None.
        """
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO job_log (job_name, last_run, next_run, run_count)
                VALUES (?, ?, ?, COALESCE((SELECT run_count FROM job_log WHERE job_name = ?), 0) + 1)
                """,
                (job_name, last_run, next_run, job_name),
            )

    def get_job_run_count(self, job_name: str) -> int:
        """Get run count for a job.

        Args:
            job_name (str): Name of the job.

        Returns:
            int: Number of times the job has run.
        """
        with self.connection as conn:
            cursor = conn.execute("SELECT run_count FROM job_log WHERE job_name = ?", (job_name,))
            result = cursor.fetchone()
            return result[0] if result else 0

    # endregion

    # region Client torrents cache

    def save_client_torrent_info(self, torrent_info: "ClientTorrentInfo"):
        """Save ClientTorrentInfo to database.

        Args:
            torrent_info: ClientTorrentInfo object from clients.client_common.
        """
        with self.transaction() as conn:
            # Save torrent basic info (1 record)
            conn.execute(
                """INSERT OR REPLACE INTO client_torrents 
                   (hash, name, total_size, download_dir, trackers, updated_at) 
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (
                    torrent_info.hash,
                    torrent_info.name,
                    torrent_info.total_size,
                    torrent_info.download_dir,
                    msgspec.json.encode(torrent_info.trackers).decode(),
                ),
            )

            # Delete old file records for this torrent
            conn.execute("DELETE FROM torrent_files WHERE torrent_hash = ?", (torrent_info.hash,))

            # Insert file records (N records for N files)
            if torrent_info.files:
                file_records = [(torrent_info.hash, file_obj.name, file_obj.size) for file_obj in torrent_info.files]

                conn.executemany(
                    """INSERT INTO torrent_files 
                       (torrent_hash, file_path, file_size) 
                       VALUES (?, ?, ?)""",
                    file_records,
                )

    def get_all_cached_torrent_hashes(self) -> set[str]:
        """Get all cached torrent hashes.

        Returns:
            set[str]: Set of all torrent hashes in cache.
        """
        with self.connection as conn:
            cursor = conn.execute("SELECT hash FROM client_torrents")
            return {row["hash"] for row in cursor.fetchall()}

    def delete_client_torrent(self, torrent_hash: str):
        """Delete torrent and its files from cache.

        Args:
            torrent_hash (str): Torrent hash to delete.
        """
        with self.transaction() as conn:
            conn.execute("DELETE FROM client_torrents WHERE hash = ?", (torrent_hash,))

    def clear_client_torrents_cache(self):
        """Clear all cached client torrent information."""
        with self.transaction() as conn:
            conn.execute("DELETE FROM torrent_files")
            conn.execute("DELETE FROM client_torrents")

    def batch_save_client_torrents(self, torrents: list["ClientTorrentInfo"]):
        """Batch save multiple torrents to database.

        Args:
            torrents: List of ClientTorrentInfo objects.
        """
        with self.transaction() as conn:
            # Batch insert torrents
            torrent_records = [
                (
                    t.hash,
                    t.name,
                    t.total_size,
                    t.download_dir,
                    msgspec.json.encode(t.trackers).decode(),
                )
                for t in torrents
            ]

            conn.executemany(
                """INSERT OR REPLACE INTO client_torrents 
                   (hash, name, total_size, download_dir, trackers, updated_at) 
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                torrent_records,
            )

            # Batch insert files
            all_file_records = []
            for t in torrents:
                if t.files:
                    for file in t.files:
                        all_file_records.append((t.hash, file.name, file.size))

            if all_file_records:
                # Delete old files for all torrents
                hashes = [t.hash for t in torrents]
                placeholders = ",".join("?" * len(hashes))
                conn.execute(f"DELETE FROM torrent_files WHERE torrent_hash IN ({placeholders})", hashes)

                # Insert new files
                conn.executemany(
                    """INSERT INTO torrent_files 
                       (torrent_hash, file_path, file_size) 
                       VALUES (?, ?, ?)""",
                    all_file_records,
                )

    def get_all_client_torrents_basic(self) -> dict[str, tuple[str, str]]:
        """Get basic info (name, download_dir) for all cached torrents.

        Returns:
            dict[str, tuple[str, str]]: Mapping from hash to (name, download_dir).
        """
        with self.connection as conn:
            cursor = conn.execute("SELECT hash, name, download_dir FROM client_torrents")
            return {row["hash"]: (row["name"], row["download_dir"]) for row in cursor.fetchall()}

    def search_torrent_by_file_match(self, target_file_size: int, fname_keywords: list[str]) -> list[sqlite3.Row]:
        """Search torrents by file size and name keywords, return raw database rows.

        Args:
            target_file_size: Target file size to match.
            fname_keywords: List of keywords that should appear in file path.

        Returns:
            List of database Row objects containing torrent and file information.
        """
        with self.connection as conn:
            # Build conditions for subquery
            conditions = ["tf2.file_size = ?"]
            params = [target_file_size]

            for keyword in fname_keywords:
                conditions.append("LOWER(tf2.file_path) LIKE ?")
                params.append(f"%{keyword.lower()}%")  # type: ignore

            # Use subquery to first get matching torrent hashes, then get all files for those torrents
            query = f"""
                SELECT 
                    ct.hash, ct.name, ct.download_dir, ct.total_size, ct.trackers,
                    tf.file_path, tf.file_size
                FROM client_torrents ct
                JOIN torrent_files tf ON ct.hash = tf.torrent_hash
                WHERE ct.hash IN (
                    SELECT DISTINCT tf2.torrent_hash
                    FROM torrent_files tf2
                    WHERE {" AND ".join(conditions)}
                )
                ORDER BY ct.hash, tf.file_path
            """

            cursor = conn.execute(query, params)
            return cursor.fetchall()

    # endregion

    def close(self):
        """Close database connection."""
        if hasattr(self._local, "connection"):
            with suppress(Exception):
                self._local.connection.close()
            delattr(self._local, "connection")


# Global database instance
_db_instance: TorrentDatabase | None = None
_db_lock = threading.Lock()


def cleanup_database():
    """Cleanup global database instance."""
    global _db_instance
    with _db_lock:
        if _db_instance is not None:
            _db_instance.close()
            _db_instance = None


def get_database(db_path: str | None = None) -> TorrentDatabase:
    """Get global database instance.

    Args:
        db_path (str, optional): Database file path, if None uses nemorosa.db in config directory.

    Returns:
        TorrentDatabase: Database instance.
    """
    global _db_instance
    with _db_lock:
        if _db_instance is None:
            if db_path is None:
                # Use database file in config directory
                config_dir = config.get_config_dir()
                db_path = os.path.join(config_dir, "nemorosa.db")
            _db_instance = TorrentDatabase(db_path)
        return _db_instance
