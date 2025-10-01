"""Scheduler module for nemorosa."""

from datetime import datetime
from enum import Enum
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from . import config, db, logger


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
        # Flag to track if search job was manually triggered
        self.search_job_manually_triggered = False

    async def start_scheduler(self):
        """Start the scheduler with configured jobs."""
        # Add search job if configured
        if config.cfg.server.search_cadence_seconds:
            self._add_search_job()

        # Add cleanup job
        self._add_cleanup_job()

        # Start scheduler
        self.scheduler.start()
        self.logger.info("Scheduler started successfully")

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

    async def _run_search_job(self, is_manual_trigger: bool = False):
        """Run search job.

        Args:
            is_manual_trigger: True if triggered manually, False if triggered by scheduler
        """
        job_name = JobType.SEARCH.value

        # Check if job should be skipped due to recent manual trigger (only for scheduled runs)
        if not is_manual_trigger and self.search_job_manually_triggered:
            self.logger.debug(f"Skipping {job_name} job - job was manually triggered recently")
            self.search_job_manually_triggered = False
            return

        self.logger.debug(f"Starting {job_name} job")

        try:
            # Record job start
            start_time = int(datetime.now().timestamp())

            # Get next run time from APScheduler
            next_run_time = None
            job = self.scheduler.get_job(JobType.SEARCH.value)
            if job and job.next_run_time:
                if is_manual_trigger:
                    # For manual trigger, get the time after the next scheduled run
                    # (skip the next scheduled run due to manual trigger flag)
                    next_run_time = int(job.next_run_time.timestamp())
                    # Add one more interval to get the run after the skipped one
                    if config.cfg.server.search_cadence_seconds:
                        cadence_seconds = config.cfg.server.search_cadence_seconds
                        next_run_time += cadence_seconds
                else:
                    # For scheduled run, get the normal next run time
                    next_run_time = int(job.next_run_time.timestamp())

            self.database.update_job_run(job_name, start_time, next_run_time)

            # Run the actual search process
            from .core import NemorosaCore

            processor = NemorosaCore()
            await processor.process_torrents()

            client = processor.torrent_client
            if client and client._monitoring:
                self.logger.debug("Stopping torrent monitoring and waiting for tracked torrents to complete...")
                await client.wait_for_monitoring_completion()

            # Record successful completion
            end_time = int(datetime.now().timestamp())
            self.logger.debug(f"Completed {job_name} job in {end_time - start_time} seconds")

        except Exception as e:
            self.logger.error(f"Error in {job_name} job: {e}")

    async def _run_cleanup_job(self):
        """Run cleanup job."""
        job_name = JobType.CLEANUP.value
        self.logger.debug(f"Starting {job_name} job")

        try:
            # Record job start
            start_time = int(datetime.now().timestamp())

            # Get next run time from APScheduler
            next_run_time = None
            job = self.scheduler.get_job(JobType.CLEANUP.value)
            if job and job.next_run_time:
                next_run_time = int(job.next_run_time.timestamp())

            self.database.update_job_run(job_name, start_time, next_run_time)

            # Run cleanup process
            from .core import NemorosaCore

            processor = NemorosaCore()
            await processor.retry_undownloaded_torrents()

            # Then post-process injected torrents
            processor.post_process_injected_torrents()

            # Record successful completion
            end_time = int(datetime.now().timestamp())
            self.logger.debug(f"Completed {job_name} job in {end_time - start_time} seconds")

        except Exception as e:
            self.logger.error(f"Error in {job_name} job: {e}")

    async def trigger_job_early(self, job_type: JobType) -> dict[str, Any]:
        """Trigger a job to run early.

        Args:
            job_type: Type of job to trigger.

        Returns:
            Dictionary with trigger result.
        """
        job_name = job_type.value
        self.logger.debug(f"Triggering {job_name} job early")

        try:
            # Check if job exists and is enabled
            job = self.scheduler.get_job(job_name)
            if not job:
                self.logger.warning(f"Job {job_name} not found or not enabled")
                return {
                    "status": "not_found",
                    "message": f"Job {job_name} not found or not enabled",
                    "job_name": job_name,
                }

            # Trigger the job directly
            if job_type == JobType.SEARCH:
                # Set flag to skip next scheduled run
                self.search_job_manually_triggered = True
                await self._run_search_job(is_manual_trigger=True)
            elif job_type == JobType.CLEANUP:
                await self._run_cleanup_job()

            self.logger.debug(f"Successfully triggered {job_name} job")
            return {
                "status": "success",
                "message": f"Job {job_name} triggered successfully",
                "job_name": job_name,
            }

        except Exception as e:
            self.logger.error(f"Error triggering {job_name} job: {e}")
            return {
                "status": "error",
                "message": f"Error triggering job: {str(e)}",
                "job_name": job_name,
            }

    def get_job_status(self, job_type: JobType) -> dict[str, Any]:
        """Get status of a job.

        Args:
            job_type: Type of job to get status for.

        Returns:
            Dictionary with job status.
        """
        job_name = job_type.value
        job = self.scheduler.get_job(job_name)

        if not job:
            return {
                "status": "not_found",
                "message": f"Job {job_name} not found",
                "job_name": job_name,
            }

        # Get last run time from database
        last_run_timestamp = self.database.get_job_last_run(job_name)
        last_run = None
        if last_run_timestamp:
            last_run = datetime.fromtimestamp(last_run_timestamp).isoformat()

        return {
            "status": "active",
            "job_name": job_name,
            "next_run": job.next_run_time.isoformat() if job.next_run_time else None,
            "last_run": last_run,
        }

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
