"""Unit tests for JobManager callback-based scheduling."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from nemorosa.clients.client_common import TorrentClient
from nemorosa.core.processor import NemorosaCore
from nemorosa.db import NemorosaDatabase
from nemorosa.scheduler import JobManager, JobType

pytestmark = pytest.mark.anyio


# --- Fixtures ---


@pytest.fixture
def mock_database() -> MagicMock:
    """Create a mock NemorosaDatabase."""
    db = MagicMock(spec=NemorosaDatabase)
    db.update_job_run = AsyncMock()
    db.get_job_last_run = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_scheduler() -> MagicMock:
    """Create a mock AsyncIOScheduler."""
    scheduler = MagicMock(spec=AsyncIOScheduler)
    scheduler.running = True
    scheduler.get_job = MagicMock(return_value=None)
    return scheduler


@pytest.fixture
def mock_core() -> MagicMock:
    """Create a mock NemorosaCore."""
    core = MagicMock(spec=NemorosaCore)
    core.process_torrents = AsyncMock()
    core.retry_undownloaded_torrents = AsyncMock()
    core.post_process_injected_torrents = AsyncMock()
    return core


@pytest.fixture
def mock_torrent_client() -> MagicMock:
    """Create a mock TorrentClient."""
    client = MagicMock(spec=TorrentClient)
    client.monitoring = False
    client.wait_for_monitoring_completion = AsyncMock()
    return client


@pytest.fixture
def job_manager(
    mock_database: MagicMock,
    mock_scheduler: MagicMock,
    mock_core: MagicMock,
    mock_torrent_client: MagicMock,
) -> JobManager:
    """Create a JobManager with mock dependencies."""
    return JobManager(
        scheduler=mock_scheduler,
        database=mock_database,
        core=mock_core,
        torrent_client=mock_torrent_client,
    )


# --- Tests for callback invocation ---


class TestRunSearchJob:
    """Tests for JobManager._run_search_job callback pattern."""

    async def test_search_handler_called(
        self,
        job_manager: JobManager,
        mock_core: MagicMock,
        mock_database: MagicMock,
    ) -> None:
        """Should call core.process_torrents when running search job."""
        await job_manager._run_search_job()

        mock_core.process_torrents.assert_awaited_once()
        mock_database.update_job_run.assert_awaited_once()

    async def test_search_job_marks_running_state(
        self,
        job_manager: JobManager,
        mock_core: MagicMock,
    ) -> None:
        """Should track running state during job execution."""
        was_running = False

        async def check_running() -> None:
            nonlocal was_running
            was_running = JobType.SEARCH in job_manager._running_jobs

        mock_core.process_torrents.side_effect = check_running

        await job_manager._run_search_job()

        assert was_running is True
        # After completion, should no longer be running
        assert JobType.SEARCH not in job_manager._running_jobs

    async def test_search_handler_waits_for_monitoring(
        self,
        mock_database: MagicMock,
        mock_scheduler: MagicMock,
        mock_core: MagicMock,
        mock_torrent_client: MagicMock,
    ) -> None:
        """Should wait for monitoring completion if monitoring is active."""
        mock_torrent_client.monitoring = True
        manager = JobManager(
            scheduler=mock_scheduler,
            database=mock_database,
            core=mock_core,
            torrent_client=mock_torrent_client,
        )

        await manager._run_search_job()

        mock_core.process_torrents.assert_awaited_once()
        mock_torrent_client.wait_for_monitoring_completion.assert_awaited_once()


class TestRunCleanupJob:
    """Tests for JobManager._run_cleanup_job callback pattern."""

    async def test_cleanup_handler_called(
        self,
        job_manager: JobManager,
        mock_core: MagicMock,
        mock_database: MagicMock,
    ) -> None:
        """Should call core cleanup methods when running cleanup job."""
        await job_manager._run_cleanup_job()

        mock_core.retry_undownloaded_torrents.assert_awaited_once()
        mock_core.post_process_injected_torrents.assert_awaited_once()
        mock_database.update_job_run.assert_awaited_once()

    async def test_cleanup_job_marks_running_state(
        self,
        job_manager: JobManager,
        mock_core: MagicMock,
    ) -> None:
        """Should track running state during job execution."""
        was_running = False

        async def check_running() -> None:
            nonlocal was_running
            was_running = JobType.CLEANUP in job_manager._running_jobs

        mock_core.retry_undownloaded_torrents.side_effect = check_running

        await job_manager._run_cleanup_job()

        assert was_running is True
        assert JobType.CLEANUP not in job_manager._running_jobs


class TestHandlerErrorIsolation:
    """Tests for error handling in job callbacks."""

    async def test_search_handler_error_does_not_propagate(
        self,
        job_manager: JobManager,
        mock_core: MagicMock,
    ) -> None:
        """Should catch handler errors via _job_execution_context."""
        mock_core.process_torrents.side_effect = RuntimeError("search failed")

        # Should not raise — _job_execution_context catches exceptions
        await job_manager._run_search_job()

        assert JobType.SEARCH not in job_manager._running_jobs

    async def test_cleanup_handler_error_does_not_propagate(
        self,
        job_manager: JobManager,
        mock_core: MagicMock,
    ) -> None:
        """Should catch handler errors via _job_execution_context."""
        mock_core.retry_undownloaded_torrents.side_effect = RuntimeError(
            "cleanup failed"
        )

        await job_manager._run_cleanup_job()

        assert JobType.CLEANUP not in job_manager._running_jobs


# --- Tests for trigger_job_early ---


class TestTriggerJobEarly:
    """Tests for JobManager.trigger_job_early."""

    async def test_not_found_when_job_missing(
        self,
        job_manager: JobManager,
    ) -> None:
        """Should return not_found when job doesn't exist in scheduler."""
        result = await job_manager.trigger_job_early(JobType.SEARCH)

        assert result.status == "not_found"
        assert result.job_name == JobType.SEARCH

    async def test_conflict_when_job_running(
        self,
        job_manager: JobManager,
    ) -> None:
        """Should return conflict when job is already running."""
        # Simulate a running job
        job_manager._running_jobs.add(JobType.SEARCH)
        # Mock scheduler.get_job to return a truthy value
        job_manager.scheduler.get_job = MagicMock(return_value=MagicMock())

        result = await job_manager.trigger_job_early(JobType.SEARCH)

        assert result.status == "conflict"

    async def test_success_when_job_exists_and_idle(
        self,
        job_manager: JobManager,
    ) -> None:
        """Should return success when job exists and is not running."""
        job_manager.scheduler.get_job = MagicMock(return_value=MagicMock())
        job_manager.scheduler.modify_job = MagicMock()

        result = await job_manager.trigger_job_early(JobType.SEARCH)

        assert result.status == "success"
        assert result.job_name == JobType.SEARCH
        job_manager.scheduler.modify_job.assert_called_once()


# --- Tests for get_job_status ---


class TestGetJobStatus:
    """Tests for JobManager.get_job_status."""

    async def test_not_found_when_job_missing(
        self,
        job_manager: JobManager,
    ) -> None:
        """Should return not_found when job doesn't exist."""
        result = await job_manager.get_job_status(JobType.SEARCH)

        assert result.status == "not_found"

    async def test_active_status_for_idle_job(
        self,
        job_manager: JobManager,
        mock_database: MagicMock,
    ) -> None:
        """Should return active when job exists but is not running."""
        mock_job = MagicMock()
        mock_job.next_run_time = datetime(2026, 2, 25, 12, 0, 0, tzinfo=UTC)
        job_manager.scheduler.get_job = MagicMock(return_value=mock_job)

        result = await job_manager.get_job_status(JobType.CLEANUP)

        assert result.status == "active"
        assert result.job_name == JobType.CLEANUP
        assert result.next_run is not None
        mock_database.get_job_last_run.assert_awaited_once()

    async def test_running_status_for_active_job(
        self,
        job_manager: JobManager,
    ) -> None:
        """Should return running when job is currently executing."""
        job_manager._running_jobs.add(JobType.SEARCH)
        mock_job = MagicMock()
        mock_job.next_run_time = None
        job_manager.scheduler.get_job = MagicMock(return_value=mock_job)

        result = await job_manager.get_job_status(JobType.SEARCH)

        assert result.status == "running"
