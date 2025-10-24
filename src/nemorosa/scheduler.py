"""Scheduler module for nemorosa."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import Enum

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import BaseModel, Field

from . import config, db, logger


class JobResponse(BaseModel):
    """Job response model."""

    status: str = Field(..., description="Job status")
    message: str = Field(..., description="Job message")
    job_name: str | None = Field(default=None, description="Job name")
    next_run: str | None = Field(default=None, description="Next scheduled run time")
    last_run: str | None = Field(default=None, description="Last run time")

    model_config = {
        "json_schema_extra": {
            "example": {"status": "success", "message": "Job triggered successfully", "job_name": "search"}
        }
    }


class JobType(Enum):
    """Job type enumeration."""

    SEARCH = "search"
    CLEANUP = "cleanup"


class JobManager:
    """Job manager for handling scheduled tasks."""

    def __init__(self):
        """Initialize job manager."""
        self.scheduler = AsyncIOScheduler()

        self.database = db.get_database()
        # Track running jobs
        self._running_jobs = set()
        self._running_jobs_lock = asyncio.Lock()

    @asynccontextmanager
    async def _job_execution_context(self, job_name: str):
        """Context manager for job execution that handles common setup and teardown.

        Args:
            job_name: Name of the job being executed.

        Yields:
            start_time: The job start time for duration calculation.
        """
        # Mark job as running
        async with self._running_jobs_lock:
            self._running_jobs.add(job_name)
        start_time = datetime.now(UTC)

        try:
            yield start_time
            # Record successful completion
            end_time = datetime.now(UTC)
            duration = (end_time - start_time).total_seconds()
            logger.debug(f"Completed {job_name} job in {duration:.2f} seconds")
        except Exception as e:
            logger.exception(f"Error in {job_name} job: {e}")
        finally:
            # Mark job as not running
            async with self._running_jobs_lock:
                self._running_jobs.discard(job_name)

    async def start_scheduler(self):
        """Start the scheduler.

        This method must be called in an async context to properly initialize
        the AsyncIOScheduler with the running event loop.
        """
        # Start scheduler (requires running event loop)
        if not self.scheduler.running:
            self.scheduler.start()

    def add_scheduled_jobs(self):
        """Add configured periodic jobs to the scheduler."""
        # Add search job
        self._add_search_job()
        # Add cleanup job
        self._add_cleanup_job()
        logger.info("Scheduled jobs added successfully")

    def _add_search_job(self):
        """Add search job to scheduler."""
        try:
            interval = config.cfg.server.search_cadence

            if not interval:
                logger.debug("No search cadence configured, skipping search job")
                return

            self.scheduler.add_job(
                self._run_search_job,
                trigger=IntervalTrigger(seconds=int(interval)),
                id=JobType.SEARCH.value,
                name="Search Job",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            logger.debug(f"Added search job with cadence: {interval}")
        except Exception as e:
            logger.exception(f"Failed to add search job: {e}")

    def _add_cleanup_job(self):
        """Add cleanup job to scheduler."""
        try:
            interval = config.cfg.server.cleanup_cadence

            self.scheduler.add_job(
                self._run_cleanup_job,
                trigger=IntervalTrigger(seconds=int(interval)),
                id=JobType.CLEANUP.value,
                name="Cleanup Job",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
            logger.debug(f"Added cleanup job with cadence: {interval}")
        except Exception as e:
            logger.exception(f"Failed to add cleanup job: {e}")

    async def _run_search_job(self):
        """Run search job."""
        job_name = JobType.SEARCH.value
        logger.debug(f"Starting {job_name} job")

        async with self._job_execution_context(job_name) as start_time:
            # Get next run time from APScheduler
            next_run_time = None
            job = self.scheduler.get_job(JobType.SEARCH.value)
            if job and job.next_run_time:
                next_run_time = job.next_run_time

            await self.database.update_job_run(job_name, start_time, next_run_time)

            # Run the actual search process
            from .core import get_core

            processor = get_core()
            await processor.process_torrents()

            client = processor.torrent_client
            if client and client.monitoring:
                logger.debug("Stopping torrent monitoring and waiting for tracked torrents to complete...")
                await client.wait_for_monitoring_completion()

    async def _run_cleanup_job(self):
        """Run cleanup job."""
        job_name = JobType.CLEANUP.value
        logger.debug(f"Starting {job_name} job")

        async with self._job_execution_context(job_name) as start_time:
            # Get next run time from APScheduler
            next_run_time = None
            job = self.scheduler.get_job(JobType.CLEANUP.value)
            if job and job.next_run_time:
                next_run_time = job.next_run_time

            await self.database.update_job_run(job_name, start_time, next_run_time)

            # Run cleanup process
            from .core import get_core

            processor = get_core()
            await processor.retry_undownloaded_torrents()

            # Then post-process injected torrents
            await processor.post_process_injected_torrents()

    async def trigger_job_early(self, job_type: JobType) -> JobResponse:
        """Trigger a job to run early.

        Args:
            job_type: Type of job to trigger.

        Returns:
            JobResponse: Job trigger result.
        """
        job_name = job_type.value
        logger.debug(f"Triggering {job_name} job early")

        try:
            # Check if job exists and is enabled
            job = self.scheduler.get_job(job_name)
            if not job:
                logger.warning(f"Job {job_name} not found or not enabled")
                return JobResponse(
                    status="not_found",
                    message=f"Job {job_name} not found or not enabled",
                    job_name=job_name,
                )

            # Check if job is already running
            async with self._running_jobs_lock:
                is_running = job_name in self._running_jobs

            if is_running:
                logger.warning(f"Job {job_name} is already running")
                return JobResponse(
                    status="conflict",
                    message=f"Job {job_name} is currently running",
                    job_name=job_name,
                )

            self.scheduler.modify_job(job_name, next_run_time=datetime.now(UTC))

            logger.debug(f"Successfully triggered {job_name} job")
            result = JobResponse(
                status="success",
                message=f"Job {job_name} triggered successfully",
                job_name=job_name,
            )

            return result

        except Exception as e:
            logger.error(f"Error triggering {job_name} job: {e}")
            return JobResponse(
                status="error",
                message=f"Error triggering job: {str(e)}",
                job_name=job_name,
            )

    async def get_job_status(self, job_type: JobType) -> JobResponse:
        """Get status of a job.

        Args:
            job_type: Type of job to get status for.

        Returns:
            JobResponse: Job status information.
        """
        job_name = job_type.value
        job = self.scheduler.get_job(job_name)

        if not job:
            return JobResponse(
                status="not_found",
                message=f"Job {job_name} not found",
                job_name=job_name,
            )

        # Check if job is currently running
        async with self._running_jobs_lock:
            is_running = job_name in self._running_jobs

        # Get last run time from database
        last_run_dt = await self.database.get_job_last_run(job_name)
        last_run = last_run_dt.isoformat() if last_run_dt else None

        # Determine status based on running state
        if is_running:
            status = "running"
            message = f"Job {job_name} is currently running"
        else:
            status = "active"
            message = f"Job {job_name} is active"

        return JobResponse(
            status=status,
            message=message,
            job_name=job_name,
            next_run=job.next_run_time.isoformat() if job.next_run_time else None,
            last_run=last_run,
        )

    def stop_scheduler(self):
        """Stop the scheduler."""
        self.scheduler.shutdown()
        logger.info("Scheduler stopped")


# Global job manager instance
_job_manager_instance: JobManager | None = None
_job_manager_lock = asyncio.Lock()


async def init_job_manager() -> None:
    """Initialize global job manager instance.

    Should be called once during application startup (cli.async_init).

    Raises:
        RuntimeError: If already initialized.
    """
    global _job_manager_instance
    async with _job_manager_lock:
        if _job_manager_instance is not None:
            raise RuntimeError("Job manager already initialized.")

        _job_manager_instance = JobManager()
        await _job_manager_instance.start_scheduler()


def get_job_manager() -> JobManager:
    """Get global job manager instance.

    Must be called after init_job_manager() has been invoked.

    Returns:
        JobManager instance.

    Raises:
        RuntimeError: If job manager has not been initialized.
    """
    if _job_manager_instance is None:
        raise RuntimeError("JobManager not initialized. Call init_job_manager() first.")
    return _job_manager_instance
