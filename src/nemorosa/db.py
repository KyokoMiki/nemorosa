"""
Database operation module.
Provides SQLite storage functionality for torrent scan history, result mapping, URL records and other data.
"""

import os
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any

import msgspec
from platformdirs import user_config_dir
from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, delete, func, select, text, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from . import config

if TYPE_CHECKING:
    from .clients.client_common import ClientTorrentInfo


# Define declarative base using SQLAlchemy 2.0 style
class Base(DeclarativeBase):
    """Base class for all ORM models."""

    pass


# ORM Models using SQLAlchemy 2.0 Mapped and mapped_column
class ScanResult(Base):
    """Scan results table - merge original scan_history, torrent_mapping, torrent_results."""

    __tablename__ = "scan_results"

    local_torrent_hash: Mapped[str] = mapped_column(String, primary_key=True)
    site_host: Mapped[str] = mapped_column(String, primary_key=True, server_default="default")
    local_torrent_name: Mapped[str | None] = mapped_column(String)
    matched_torrent_id: Mapped[str | None] = mapped_column(String)
    matched_torrent_hash: Mapped[str | None] = mapped_column(String)
    checked: Mapped[bool] = mapped_column(Boolean, server_default="0")
    scanned_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("idx_scan_results_matched_checked", "matched_torrent_hash", "checked"),)


class UndownloadedTorrent(Base):
    """Undownloaded torrents table - record detailed information of undownloaded torrents."""

    __tablename__ = "undownloaded_torrents"

    torrent_id: Mapped[str] = mapped_column(String, primary_key=True)
    site_host: Mapped[str] = mapped_column(String, primary_key=True, server_default="default")
    download_dir: Mapped[str | None] = mapped_column(String)
    local_torrent_name: Mapped[str | None] = mapped_column(String)
    rename_map: Mapped[str | None] = mapped_column(String)  # JSON format
    added_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (Index("idx_undownloaded_site_host", "site_host"),)


class JobLog(Base):
    """Job log table - for scheduler job tracking."""

    __tablename__ = "job_log"

    job_name: Mapped[str] = mapped_column(String, primary_key=True)
    last_run: Mapped[datetime | None] = mapped_column()
    next_run: Mapped[datetime | None] = mapped_column()
    run_count: Mapped[int] = mapped_column(Integer, server_default="1")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ClientTorrent(Base):
    """Client torrents cache table - cache static torrent information from client."""

    __tablename__ = "client_torrents"

    hash: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    total_size: Mapped[int] = mapped_column(Integer, nullable=False)
    download_dir: Mapped[str | None] = mapped_column(String)
    trackers: Mapped[str | None] = mapped_column(String)  # JSON array

    @classmethod
    def from_client_info(cls, info: "ClientTorrentInfo") -> "ClientTorrent":
        """Alternative constructor: Create ClientTorrent from ClientTorrentInfo.

        This is a factory method that provides a convenient way to create a ClientTorrent
        ORM object from a ClientTorrentInfo business object.

        Args:
            info: ClientTorrentInfo object from torrent client.

        Returns:
            ClientTorrent ORM object ready for database persistence.
        """
        return cls(
            hash=info.hash,
            name=info.name,
            total_size=info.total_size,
            download_dir=info.download_dir or None,
            trackers=msgspec.json.encode(info.trackers).decode() if info.trackers else None,
        )


class TorrentFile(Base):
    """Torrent files table - file index for fast searching."""

    __tablename__ = "torrent_files"

    torrent_hash: Mapped[str] = mapped_column(
        String, ForeignKey("client_torrents.hash", ondelete="CASCADE"), primary_key=True
    )
    file_path: Mapped[str] = mapped_column(String, primary_key=True)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("idx_torrent_files_size", "file_size"),)


class Metadata(Base):
    """Metadata table - stores key-value pairs for application metadata."""

    __tablename__ = "metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(String)


class NemorosaDatabase:
    """Nemorosa database management class using SQLAlchemy async API."""

    def __init__(self, db_path: str | None = None):
        """Initialize database connection.

        Args:
            db_path: Database file path, if None uses config directory.
        """
        self.db_path = (
            os.path.abspath(db_path) if db_path else os.path.join(user_config_dir(config.APPNAME), "nemorosa.db")
        )

        # Ensure database directory exists
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

        # Create async engine with SQLAlchemy 2.0 style
        self.engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{self.db_path}",
            echo=False,
            future=True,
        )

        # Create async session factory
        self.async_session_maker: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def init_database(self):
        """Initialize database table structure asynchronously."""
        async with self.engine.begin() as conn:
            # Enable WAL mode for better concurrency
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            # Create all tables defined in Base metadata
            await conn.run_sync(Base.metadata.create_all)

        async with self.engine.connect() as conn:
            await conn.execute(text("VACUUM"))
            await conn.commit()

    # region Scan results

    async def add_scan_result(
        self,
        local_torrent_hash: str,
        local_torrent_name: str | None = None,
        matched_torrent_id: str | None = None,
        site_host: str = "default",
        matched_torrent_hash: str | None = None,
    ):
        """Add scan result record.

        Args:
            local_torrent_hash: Local torrent hash.
            local_torrent_name: Local torrent name.
            matched_torrent_id: Matched torrent ID (can be None to indicate not found).
            site_host: Site hostname.
            matched_torrent_hash: Matched torrent hash.
        """
        async with self.async_session_maker.begin() as session:
            # Use merge to insert or update
            scan_result = ScanResult(
                local_torrent_hash=local_torrent_hash,
                site_host=site_host,
                local_torrent_name=local_torrent_name,
                matched_torrent_id=matched_torrent_id,
                matched_torrent_hash=matched_torrent_hash,
            )
            await session.merge(scan_result)

    async def is_hash_scanned(self, local_torrent_hash: str, site_host: str) -> bool:
        """Check if specified local torrent hash has been scanned on specific site.

        Args:
            local_torrent_hash: Local torrent hash.
            site_host: Site hostname.

        Returns:
            True if scanned on the specific site, False otherwise.
        """
        async with self.async_session_maker() as session:
            stmt = select(ScanResult).where(
                ScanResult.local_torrent_hash == local_torrent_hash, ScanResult.site_host == site_host
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    # endregion

    # region Undownloaded torrents

    async def load_undownloaded_torrents(self, site_host: str = "default") -> dict[str, dict[str, Any]]:
        """Load undownloaded torrent information for specified site.

        Args:
            site_host: Site hostname, defaults to 'default'.

        Returns:
            Mapping dictionary from torrent ID to detailed information.
        """
        async with self.async_session_maker() as session:
            stmt = select(UndownloadedTorrent).where(UndownloadedTorrent.site_host == site_host)
            result_set = await session.execute(stmt)
            torrents = result_set.scalars().all()

            result = {
                torrent.torrent_id: {
                    "download_dir": torrent.download_dir,
                    "local_torrent_name": torrent.local_torrent_name,
                    "rename_map": msgspec.json.decode(torrent.rename_map) if torrent.rename_map else {},
                }
                for torrent in torrents
            }
            return result

    async def add_undownloaded_torrent(self, torrent_id: str, torrent_info: dict, site_host: str = "default"):
        """Add undownloaded torrent information.

        Args:
            torrent_id: Torrent ID.
            torrent_info: Dictionary containing download_dir, local_torrent_name, rename_map.
            site_host: Site hostname.
        """
        async with self.async_session_maker.begin() as session:
            undownloaded = UndownloadedTorrent(
                torrent_id=torrent_id,
                site_host=site_host,
                download_dir=torrent_info.get("download_dir"),
                local_torrent_name=torrent_info.get("local_torrent_name"),
                rename_map=msgspec.json.encode(torrent_info.get("rename_map", {})).decode(),
            )
            await session.merge(undownloaded)

    async def remove_undownloaded_torrent(self, torrent_id: str, site_host: str = "default"):
        """Remove specified torrent from undownloaded torrents table.

        Args:
            torrent_id: Torrent ID.
            site_host: Site hostname.
        """
        async with self.async_session_maker.begin() as session:
            stmt = delete(UndownloadedTorrent).where(
                UndownloadedTorrent.torrent_id == torrent_id, UndownloadedTorrent.site_host == site_host
            )
            await session.execute(stmt)

    async def get_matched_scan_results(self) -> dict[str, dict[str, Any]]:
        """Get scan results with matched torrent hash for all sites that haven't been checked.

        Returns:
            Dictionary mapping matched_torrent_hash to scan result information.
        """
        async with self.async_session_maker() as session:
            stmt = select(ScanResult).where(
                ScanResult.matched_torrent_hash.is_not(None),
                ScanResult.checked.is_(False),
            )
            result_set = await session.execute(stmt)
            scan_results = result_set.scalars().all()

            result = {
                scan_result.matched_torrent_hash: {
                    "local_torrent_hash": scan_result.local_torrent_hash,
                    "local_torrent_name": scan_result.local_torrent_name,
                    "matched_torrent_id": scan_result.matched_torrent_id,
                    "site_host": scan_result.site_host,
                }
                for scan_result in scan_results
                if scan_result.matched_torrent_hash
            }
            return result

    async def update_scan_result_checked(self, matched_torrent_hash: str, checked: bool):
        """Update checked status for a scan result.

        Args:
            matched_torrent_hash: Matched torrent hash.
            checked: Checked status.
        """
        async with self.async_session_maker.begin() as session:
            stmt = (
                update(ScanResult)
                .where(ScanResult.matched_torrent_hash == matched_torrent_hash)
                .values(checked=checked)
            )
            await session.execute(stmt)

    async def clear_matched_torrent_info(self, matched_torrent_hash: str):
        """Clear matched torrent information for a scan result.

        Args:
            matched_torrent_hash: Matched torrent hash.
        """
        async with self.async_session_maker.begin() as session:
            stmt = (
                update(ScanResult)
                .where(ScanResult.matched_torrent_hash == matched_torrent_hash)
                .values(matched_torrent_id=None, matched_torrent_hash=None)
            )
            await session.execute(stmt)

    # endregion

    # region Job log

    async def get_job_last_run(self, job_name: str) -> datetime | None:
        """Get last run datetime for a job.

        Args:
            job_name: Name of the job.

        Returns:
            Last run datetime, or None if never run.
        """
        async with self.async_session_maker() as session:
            stmt = select(JobLog.last_run).where(JobLog.job_name == job_name)
            result = await session.execute(stmt)
            last_run = result.scalar_one_or_none()
            return last_run

    async def update_job_run(self, job_name: str, last_run: datetime, next_run: datetime | None = None):
        """Update job run information.

        Args:
            job_name: Name of the job.
            last_run: Last run datetime.
            next_run: Next run datetime, or None.
        """
        new_run_count = await self.get_job_run_count(job_name) + 1
        async with self.async_session_maker.begin() as session:
            job_log = JobLog(job_name=job_name, last_run=last_run, next_run=next_run, run_count=new_run_count)
            await session.merge(job_log)

    async def get_job_run_count(self, job_name: str) -> int:
        """Get run count for a job.

        Args:
            job_name: Name of the job.

        Returns:
            Number of times the job has run.
        """
        async with self.async_session_maker() as session:
            stmt = select(JobLog.run_count).where(JobLog.job_name == job_name)
            result = await session.execute(stmt)
            run_count = result.scalar_one_or_none()
            return run_count if run_count is not None else 0

    # endregion

    # region Client torrents cache

    async def save_client_torrent_info(self, torrent_info: "ClientTorrentInfo"):
        """Save ClientTorrentInfo to database.

        Args:
            torrent_info: ClientTorrentInfo object from clients.client_common.
        """
        async with self.async_session_maker.begin() as session:
            # Create ORM object from ClientTorrentInfo
            client_torrent = ClientTorrent.from_client_info(torrent_info)
            await session.merge(client_torrent)

            # Delete old file records for this torrent
            delete_stmt = delete(TorrentFile).where(TorrentFile.torrent_hash == torrent_info.hash)
            await session.execute(delete_stmt)

            # Insert file records
            if torrent_info.files:
                torrent_files = [
                    TorrentFile(torrent_hash=torrent_info.hash, file_path=file_obj.name, file_size=file_obj.size)
                    for file_obj in torrent_info.files
                ]
                for torrent_file in torrent_files:
                    session.add(torrent_file)

    async def get_all_cached_torrent_hashes(self) -> set[str]:
        """Get all cached torrent hashes.

        Returns:
            Set of all torrent hashes in cache.
        """
        async with self.async_session_maker() as session:
            stmt = select(ClientTorrent.hash)
            result = await session.execute(stmt)
            return {hash_val for (hash_val,) in result.all()}

    async def delete_client_torrents(self, torrent_hashes: str | list[str] | set[str]):
        """Delete torrent(s) and their files from cache.

        Args:
            torrent_hashes: Single torrent hash, list of torrent hashes, or set of torrent hashes to delete.
        """
        if isinstance(torrent_hashes, str):
            condition = ClientTorrent.hash == torrent_hashes
        else:
            if not torrent_hashes:
                return
            condition = ClientTorrent.hash.in_(torrent_hashes)

        async with self.async_session_maker.begin() as session:
            # Files will be cascade deleted due to foreign key constraint
            stmt = delete(ClientTorrent).where(condition)
            await session.execute(stmt)

    async def clear_client_torrents_cache(self):
        """Clear all cached client torrent information."""
        async with self.async_session_maker.begin() as session:
            # Delete files first (or rely on cascade)
            await session.execute(delete(TorrentFile))
            await session.execute(delete(ClientTorrent))

    async def batch_save_client_torrents(self, torrents: list["ClientTorrentInfo"]):
        """Batch save multiple torrents to database.

        Args:
            torrents: List of ClientTorrentInfo objects.
        """
        if not torrents:
            return

        # SQLite has a limit on number of SQL variables:
        # - after 3.32.0 (2020-05-22): 32766 variables
        TORRENT_BATCH_SIZE = 6553  # 32765 // 5
        FILE_BATCH_SIZE = 10921  # 32765 // 3

        async with self.async_session_maker.begin() as session:
            # Process torrents in batches
            for i in range(0, len(torrents), TORRENT_BATCH_SIZE):
                batch = torrents[i : i + TORRENT_BATCH_SIZE]

                # Prepare data for batch insert
                torrent_data = [
                    {
                        "hash": t.hash,
                        "name": t.name,
                        "total_size": t.total_size,
                        "download_dir": t.download_dir or None,
                        "trackers": msgspec.json.encode(t.trackers).decode() if t.trackers else None,
                    }
                    for t in batch
                ]

                # Batch upsert torrents using INSERT ... ON CONFLICT
                stmt = insert(ClientTorrent).values(torrent_data)
                # On conflict (duplicate hash), update all fields
                stmt = stmt.on_conflict_do_update(
                    index_elements=["hash"],
                    set_={
                        "name": stmt.excluded.name,
                        "total_size": stmt.excluded.total_size,
                        "download_dir": stmt.excluded.download_dir,
                        "trackers": stmt.excluded.trackers,
                    },
                )
                await session.execute(stmt)

            # Delete old files for all torrents in batches
            all_hashes = [t.hash for t in torrents]
            for i in range(0, len(all_hashes), TORRENT_BATCH_SIZE):
                batch_hashes = all_hashes[i : i + TORRENT_BATCH_SIZE]
                delete_stmt = delete(TorrentFile).where(TorrentFile.torrent_hash.in_(batch_hashes))
                await session.execute(delete_stmt)

            # Prepare file data for batch insert
            file_data = [
                {
                    "torrent_hash": t.hash,
                    "file_path": file.name,
                    "file_size": file.size,
                }
                for t in torrents
                if t.files
                for file in t.files
            ]

            # Batch insert files in chunks to avoid SQL variable limit
            for i in range(0, len(file_data), FILE_BATCH_SIZE):
                batch = file_data[i : i + FILE_BATCH_SIZE]
                file_stmt = insert(TorrentFile).values(batch)
                await session.execute(file_stmt)

    async def get_all_client_torrents_basic(self) -> dict[str, tuple[str, str]]:
        """Get basic info (name, download_dir) for all cached torrents.

        Returns:
            Mapping from hash to (name, download_dir).
        """
        async with self.async_session_maker() as session:
            stmt = select(ClientTorrent.hash, ClientTorrent.name, ClientTorrent.download_dir)
            result = await session.execute(stmt)
            return {hash_val: (name, download_dir) for hash_val, name, download_dir in result.all()}

    async def search_torrent_by_file_match(
        self, target_file_size: int, fname_keywords: list[str]
    ) -> list[dict[str, Any]]:
        """Search torrents by file size and name keywords.

        Args:
            target_file_size: Target file size to match.
            fname_keywords: List of keywords that should appear in file path.

        Returns:
            List of dictionaries containing torrent and file information.
        """
        async with self.async_session_maker() as session:
            # Build conditions for matching files
            conditions = [TorrentFile.file_size == target_file_size]
            for keyword in fname_keywords:
                conditions.append(func.lower(TorrentFile.file_path).like(f"%{keyword.lower()}%"))

            # Subquery to find matching torrent hashes
            subquery = select(TorrentFile.torrent_hash).where(*conditions).distinct().subquery()

            # Main query to get all files for matching torrents
            stmt = (
                select(
                    ClientTorrent.hash,
                    ClientTorrent.name,
                    ClientTorrent.download_dir,
                    ClientTorrent.total_size,
                    ClientTorrent.trackers,
                    TorrentFile.file_path,
                    TorrentFile.file_size,
                )
                .join(TorrentFile, ClientTorrent.hash == TorrentFile.torrent_hash)
                .where(ClientTorrent.hash.in_(select(subquery)))
                .order_by(ClientTorrent.hash, TorrentFile.file_path)
            )

            result = await session.execute(stmt)
            rows = result.all()

            # Convert to list of dicts for compatibility
            return [
                {
                    "hash": row.hash,
                    "name": row.name,
                    "download_dir": row.download_dir,
                    "total_size": row.total_size,
                    "trackers": row.trackers,
                    "file_path": row.file_path,
                    "file_size": row.file_size,
                }
                for row in rows
            ]

    # endregion

    # region Metadata

    async def get_metadata(self, key: str) -> str | None:
        """Get metadata value by key.

        Args:
            key: Metadata key.

        Returns:
            Metadata value, or None if key doesn't exist.
        """
        async with self.async_session_maker() as session:
            stmt = select(Metadata.value).where(Metadata.key == key)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def set_metadata(self, key: str, value: str | None):
        """Set metadata value by key.

        Args:
            key: Metadata key.
            value: Metadata value.
        """
        async with self.async_session_maker.begin() as session:
            metadata = Metadata(key=key, value=value)
            await session.merge(metadata)

    async def delete_metadata(self, key: str):
        """Delete metadata by key.

        Args:
            key: Metadata key to delete.
        """
        async with self.async_session_maker.begin() as session:
            stmt = delete(Metadata).where(Metadata.key == key)
            await session.execute(stmt)

    # endregion

    async def close(self):
        """Close database connection."""
        # Execute checkpoint to merge WAL file into main database
        async with self.engine.connect() as conn:
            # TRUNCATE mode: checkpoint all frames and truncate the WAL file
            await conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
            await conn.commit()

        await self.engine.dispose()


# Global database instance
_db_instance: NemorosaDatabase | None = None
_db_lock = threading.Lock()


async def cleanup_database():
    """Cleanup global database instance."""
    global _db_instance
    with _db_lock:
        db_to_close = _db_instance
        _db_instance = None

    if db_to_close is not None:
        await db_to_close.close()


def get_database(db_path: str | None = None) -> NemorosaDatabase:
    """Get global database instance.

    Args:
        db_path (str, optional): Database file path, if None uses nemorosa.db in config directory.

    Returns:
        NemorosaDatabase: Database instance.
    """
    global _db_instance
    with _db_lock:
        if _db_instance is None:
            _db_instance = NemorosaDatabase(db_path)
        return _db_instance
