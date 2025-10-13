"""Web server module for nemorosa."""

import base64
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import __version__, api, config, db, logger, scheduler
from .core import NemorosaCore, ProcessResponse, ProcessStatus
from .scheduler import JobResponse


class AnnounceRequest(BaseModel):
    """Announce request model."""

    name: str = Field(..., description="Torrent name")
    link: str = Field(..., description="Torrent download link")
    torrentdata: str = Field(..., description="Base64 encoded torrent data")

    model_config = {
        "json_schema_extra": {
            "example": {
                "name": "Example.Album.2024.FLAC-GROUP",
                "link": "https://tracker.example.com/torrents.php?id=12345",
                "torrentdata": "ZDg6YW5ub3VuY2U0MTpodHRw...",
            }
        }
    }


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Lifespan event handler for FastAPI app."""
    # Get logger and job manager
    app_logger = logger.get_logger()
    job_manager = scheduler.get_job_manager()

    # Initialize core components (torrent client, database, API connections, scheduler)
    from .cli import async_init

    await async_init()

    # Add scheduled jobs if available
    if job_manager and api.get_target_apis():
        job_manager.add_scheduled_jobs()
        app_logger.info("Scheduled jobs configured and added")

    yield

    # Shutdown
    if job_manager:
        job_manager.stop_scheduler()
        app_logger.info("Scheduler stopped")

    # Cleanup database
    await db.cleanup_database()


# Create FastAPI app
app = FastAPI(
    title="Nemorosa Web Server",
    description="Music torrent cross-seeding tool with automatic file mapping and seamless injection",
    version=__version__,
    lifespan=lifespan,
)


def verify_api_key(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
) -> bool:
    """Verify API key."""
    # Check if API key is configured in server config
    api_key = config.cfg.server.api_key
    if not api_key:
        # No API key configured, allow all requests
        return True

    if not credentials or credentials.credentials != api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")

    return True


# Type alias for API key dependency
ApiKeyDep = Annotated[bool, Depends(verify_api_key)]


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all requests."""
    app_logger = logger.get_logger()
    app_logger.debug(f"Incoming request: {request.method} {request.url}")

    response = await call_next(request)

    app_logger.debug(f"Response: {response.status_code}")

    return response


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "Nemorosa Web Server",
        "version": __version__,
        "endpoints": {
            "webhook": "/api/webhook",
            "announce": "/api/announce",
            "job": "/api/job",
            "docs": "/docs",
        },
    }


@app.get("/favicon.ico")
async def favicon():
    """Serve favicon.ico."""
    favicon_path = Path(__file__).parent / "static" / "favicon.ico"
    return FileResponse(favicon_path)


@app.post(
    "/api/webhook",
    response_model=ProcessResponse,
    tags=["webhook"],
    summary="Process torrent via webhook",
    responses={
        200: {"description": "Successfully processed (injected/saved/already exists)"},
        204: {"description": "No matching torrent found (normal case)"},
    },
)
async def webhook(
    infohash: Annotated[str, Query(min_length=1, description="Torrent infohash (40-character hex string)")],
    response: Response,
    _: ApiKeyDep,
) -> ProcessResponse:
    """Process a single torrent via webhook.

    This endpoint triggers cross-seed processing for a specific torrent
    identified by its infohash.

    Returns processing results with appropriate HTTP status codes:

    - **200**: Successfully processed (injected/saved/already exists)
    - **204**: No matching torrent found (this is normal, not an error)

    Args:
        infohash: Torrent infohash from URL parameter
        response: FastAPI Response object for status code manipulation
        _: API key verification

    Returns:
        WebhookResponse: Processing result with detailed information
    """

    app_logger = logger.get_logger()

    try:
        # Process the torrent
        processor = NemorosaCore()
        result = await processor.process_single_torrent(infohash)

        if result.status == ProcessStatus.NOT_FOUND:
            # No matches found
            response.status_code = status.HTTP_204_NO_CONTENT
            app_logger.info(f"No matches found for webhook: {infohash}")
        elif result.status == ProcessStatus.ERROR:
            # Processing error
            app_logger.error(f"Error processing webhook: {infohash} - {result.message}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Processing error: {result.message}"
            )
        else:
            # Successfully processed (SUCCESS, SKIPPED, etc.)
            response.status_code = status.HTTP_200_OK
            app_logger.info(f"Processed webhook: {infohash} (status: 200)")

        # Return the result directly
        return result

    except HTTPException:
        raise
    except Exception as e:
        app_logger.error(f"Error processing torrent {infohash}: {str(e)}")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error: {str(e)}"
        ) from e


@app.post(
    "/api/announce",
    response_model=ProcessResponse,
    tags=["announce"],
    summary="Process torrent announce",
    responses={
        200: {"description": "Successfully processed (injected/saved/already exists)"},
        204: {"description": "No matching torrent found (normal case)"},
    },
)
async def announce(
    request: AnnounceRequest,
    response: Response,
    _: ApiKeyDep,
) -> ProcessResponse:
    """Process torrent announce from tracker.

    This endpoint receives announce notifications with torrent data
    in JSON format from external systems like autobrr.

    Returns detailed processing results with appropriate HTTP status codes:

    - **200**: Successfully processed (injected/saved/already exists)
    - **204**: No matching torrent found (this is normal, not an error)

    Args:
        request: Announce request containing torrent name, link, and data
        response: FastAPI Response object for status code manipulation
        _: API key verification

    Returns:
        WebhookResponse: Processing result with detailed information
    """

    try:
        try:
            torrent_bytes = base64.b64decode(request.torrentdata)
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid base64 torrent data: {str(e)}"
            ) from e

        # Log the announce
        app_logger = logger.get_logger()
        app_logger.info(f"Received announce for torrent: {request.name} from {request.link}")

        # Process the torrent for cross-seeding using the reverse announce function
        processor = NemorosaCore()
        result = await processor.process_reverse_announce_torrent(
            torrent_name=request.name,
            torrent_link=request.link,
            torrent_data=torrent_bytes,
        )

        if result.status == ProcessStatus.NOT_FOUND:
            # No matches found
            response.status_code = status.HTTP_204_NO_CONTENT
            app_logger.info(f"No matches found for announce: {request.name}")
        elif result.status == ProcessStatus.ERROR:
            # Processing error
            app_logger.error(f"Error processing announce: {request.name} - {result.message}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Processing error: {result.message}"
            )
        elif result.status == ProcessStatus.SUCCESS:
            # Successfully processed
            response.status_code = status.HTTP_200_OK
            app_logger.info(f"Successfully processed announce: {request.name} (status: 200)")
        else:
            # Default case - no specific action was taken (SKIPPED, etc.)
            response.status_code = status.HTTP_204_NO_CONTENT

        # Return the result directly
        return result

    except HTTPException:
        raise
    except Exception as e:
        app_logger = logger.get_logger()
        app_logger.error(f"Error processing torrent announce {request.name}: {str(e)}")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error: {str(e)}"
        ) from e


@app.post(
    "/api/job",
    response_model=JobResponse,
    tags=["jobs"],
    summary="Trigger job execution",
    responses={
        200: {"description": "Job triggered successfully"},
        404: {"description": "Job not found or disabled"},
        409: {"description": "Job already running or not eligible"},
    },
)
async def trigger_job(
    job_type: Annotated[str, Query(description="Job type: 'search' or 'cleanup'")],
    _: ApiKeyDep,
) -> JobResponse:
    """Trigger a scheduled job to run ahead of schedule.

    Allows manual triggering of scheduled jobs for immediate execution.

    Returns appropriate HTTP status codes:
    - **200**: Job triggered successfully
    - **404**: Job not found or disabled
    - **409**: Job already running or not eligible

    Args:
        job_type: Type of job to trigger (search, cleanup)
        _: API key verification

    Returns:
        JobResponse: Job trigger result with status and timing information
    """
    try:
        # Get job manager
        job_mgr = scheduler.get_job_manager()

        # Convert string to JobType enum and validate
        try:
            job_type_enum = scheduler.JobType(job_type)
        except ValueError as e:
            valid_job_types = [job_type.value for job_type in scheduler.JobType]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid job type '{job_type}'. Must be one of: {valid_job_types}",
            ) from e

        # Trigger the job
        result = await job_mgr.trigger_job_early(job_type_enum)

        # Map internal status to HTTP status codes
        if result.status == "not_found":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=result.message)
        elif result.status == "conflict":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=result.message)

        return result

    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        app_logger = logger.get_logger()
        app_logger.error(f"Error triggering job {job_type}: {str(e)}")
        # Let FastAPI handle the 500 error automatically
        raise


@app.get(
    "/api/job/{job_type}",
    response_model=JobResponse,
    tags=["jobs"],
    summary="Get job status",
    responses={
        200: {"description": "Job status retrieved successfully"},
        400: {"description": "Invalid job type"},
        401: {"description": "Unauthorized - Invalid API key"},
        500: {"description": "Internal server error"},
    },
)
async def get_job_status(
    job_type: str,
    _: ApiKeyDep,
) -> JobResponse:
    """Get the current status and schedule of a job.

    Retrieves information about a scheduled job including its status,
    next run time, and last run time.

    Args:
        job_type: Type of job to get status for (search, cleanup)
        _: API key verification

    Returns:
        JobResponse: Job status information including run times
    """
    try:
        # Get job manager
        job_mgr = scheduler.get_job_manager()

        # Convert string to JobType enum and validate
        try:
            job_type_enum = scheduler.JobType(job_type)
        except ValueError as e:
            valid_job_types = [job_type.value for job_type in scheduler.JobType]
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid job type '{job_type}'. Must be one of: {valid_job_types}",
            ) from e

        # Get job status
        result = await job_mgr.get_job_status(job_type_enum)

        return result

    except Exception as e:
        app_logger = logger.get_logger()
        app_logger.error(f"Error getting job status for {job_type}: {str(e)}")

        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Internal server error: {str(e)}"
        ) from e


def run_webserver():
    """Run the web server."""
    # Use config values if not provided
    host = config.cfg.server.host
    port = config.cfg.server.port
    log_level = config.cfg.global_config.loglevel

    # Get logger
    app_logger = logger.get_logger()

    # Log server startup
    app_logger.info(
        f"Starting Nemorosa web server on {host if host is not None else 'all interfaces (IPv4/IPv6)'}:{port}"
    )
    app_logger.info(f"Using torrent client: {config.cfg.downloader.client}")
    app_logger.info(f"Target sites: {len(config.cfg.target_sites)}")

    # Check if API key is configured
    api_key = config.cfg.server.api_key
    if api_key:
        app_logger.info("API key authentication enabled")
    else:
        app_logger.info("API key authentication disabled")

    # Check if scheduler should be initialized
    if any(
        [
            config.cfg.server.search_cadence,
            config.cfg.server.cleanup_cadence,
        ]
    ):
        app_logger.info("Scheduler will be started with configured jobs")
    else:
        app_logger.info("No scheduled jobs configured")

    # Import uvicorn here to avoid import issues
    import uvicorn

    # Run server
    uvicorn.run("nemorosa.webserver:app", host=host, port=port, log_level=log_level, reload=False)  # type: ignore[arg-type]
