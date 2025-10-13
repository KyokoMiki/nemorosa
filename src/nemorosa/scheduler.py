"""Scheduler module for nemorosa."""

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
        self.logger = logger.get_logger()
        self.database = db.get_database()
        # Track running jobs
        self._running_jobs = set()

    async def start_scheduler(self):
        """Start the scheduler and add configured periodic jobs.

        This method must be called in an async context to properly initialize
        the AsyncIOScheduler with the running event loop.
        """
        # Start scheduler (requires running event loop)
        if not self.scheduler.running:
            self.scheduler.start()

    def add_scheduled_jobs(self):
        """Add configured periodic jobs to the scheduler."""

        # Add search job if configured
        if config.cfg.server.search_cadence_seconds:
            self._add_search_job()

        # Add cleanup job
        self._add_cleanup_job()

        self.logger.info("Scheduled jobs added successfully")

    def _add_search_job(self):
        """Add search job to scheduler."""
        try:
            interval = config.cfg.server.search_cadence_seconds

            self.scheduler.add_job(
                self._run_search_job,
                trigger=IntervalTrigger(seconds=interval),
                id=JobType.SEARCH.value,
                name="Search Job",
                max_instances=1,
                misfire_grace_time=60,
                coalesce=True,
                replace_existing=True,
            )
            self.logger.debug(f"Added search job with cadence: {config.cfg.server.search_cadence}")
        except Exception as e:
            self.logger.error(f"Failed to add search job: {e}")

    def _add_cleanup_job(self):
        """Add cleanup job to scheduler."""
        try:
            interval = config.cfg.server.cleanup_cadence_seconds

            self.scheduler.add_job(
                self._run_cleanup_job,
                trigger=IntervalTrigger(seconds=interval),
                id=JobType.CLEANUP.value,
                name="Cleanup Job",
                max_instances=1,
                misfire_grace_time=60,
                coalesce=True,
                replace_existing=True,
            )
            self.logger.debug(f"Added cleanup job with cadence: {config.cfg.server.cleanup_cadence}")
        except Exception as e:
            self.logger.error(f"Failed to add cleanup job: {e}")

    async def _run_search_job(self):
        """Run search job."""
        job_name = JobType.SEARCH.value

        self.logger.debug(f"Starting {job_name} job")

        # Mark job as running
        self._running_jobs.add(job_name)

        try:
            # Record job start
            start_time = datetime.now(UTC)

            # Get next run time from APScheduler
            next_run_time = None
            job = self.scheduler.get_job(JobType.SEARCH.value)
            if job and job.next_run_time:
                next_run_time = job.next_run_time

            await self.database.update_job_run(job_name, start_time, next_run_time)

            # Run the actual search process
            from .core import NemorosaCore

            processor = NemorosaCore()
            await processor.process_torrents()

            client = processor.torrent_client
            if client and client.monitoring:
                self.logger.debug("Stopping torrent monitoring and waiting for tracked torrents to complete...")
                await client.wait_for_monitoring_completion()

            # Record successful completion
            end_time = datetime.now(UTC)
            duration = (end_time - start_time).total_seconds()
            self.logger.debug(f"Completed {job_name} job in {duration:.2f} seconds")

        except Exception as e:
            self.logger.error(f"Error in {job_name} job: {e}")
        finally:
            # Mark job as not running
            self._running_jobs.discard(job_name)

    async def _run_cleanup_job(self):
        """Run cleanup job."""
        job_name = JobType.CLEANUP.value
        self.logger.debug(f"Starting {job_name} job")

        # Mark job as running
        self._running_jobs.add(job_name)

        try:
            # Record job start
            start_time = datetime.now(UTC)

            # Get next run time from APScheduler
            next_run_time = None
            job = self.scheduler.get_job(JobType.CLEANUP.value)
            if job and job.next_run_time:
                next_run_time = job.next_run_time

            await self.database.update_job_run(job_name, start_time, next_run_time)

            # Run cleanup process
            from .core import NemorosaCore

            processor = NemorosaCore()
            await processor.retry_undownloaded_torrents()

            # Then post-process injected torrents
            await processor.post_process_injected_torrents()

            # Record successful completion
            end_time = datetime.now(UTC)
            duration = (end_time - start_time).total_seconds()
            self.logger.debug(f"Completed {job_name} job in {duration:.2f} seconds")

        except Exception as e:
            self.logger.error(f"Error in {job_name} job: {e}")
        finally:
            # Mark job as not running
            self._running_jobs.discard(job_name)

    async def trigger_job_early(self, job_type: JobType) -> JobResponse:
        """Trigger a job to run early.

        Args:
            job_type: Type of job to trigger.

        Returns:
            JobResponse: Job trigger result.
        """
        job_name = job_type.value
        self.logger.debug(f"Triggering {job_name} job early")

        try:
            # Check if job exists and is enabled
            job = self.scheduler.get_job(job_name)
            if not job:
                self.logger.warning(f"Job {job_name} not found or not enabled")
                return JobResponse(
                    status="not_found",
                    message=f"Job {job_name} not found or not enabled",
                    job_name=job_name,
                )

            # Check if job is already running
            if job_name in self._running_jobs:
                self.logger.warning(f"Job {job_name} is already running")
                return JobResponse(
                    status="conflict",
                    message=f"Job {job_name} is currently running",
                    job_name=job_name,
                )

            self.scheduler.modify_job(job_name, next_run_time=datetime.now(UTC))

            self.logger.debug(f"Successfully triggered {job_name} job")
            result = JobResponse(
                status="success",
                message=f"Job {job_name} triggered successfully",
                job_name=job_name,
            )

            return result

        except Exception as e:
            self.logger.error(f"Error triggering {job_name} job: {e}")
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
        self.logger.info("Scheduler stopped")


# Global job manager instance
job_manager: JobManager | None = None


def get_job_manager() -> JobManager:
    """Get global job manager instance.

    Returns:
        JobManager instance.
    """
    global job_manager
    if job_manager is None:
        job_manager = JobManager()
    return job_manager
