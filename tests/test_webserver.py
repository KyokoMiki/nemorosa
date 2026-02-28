"""Unit tests for webserver endpoints using FastAPI dependency overrides."""

from collections.abc import Generator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from nemorosa.core.models import ProcessResponse, ProcessStatus
from nemorosa.core.processor import NemorosaCore
from nemorosa.scheduler import JobManager, JobResponse, JobType
from nemorosa.webserver import app, get_core, get_job_manager, verify_api_key

# --- Fixtures ---


@pytest.fixture
def mock_core() -> MagicMock:
    """Create a mock NemorosaCore."""
    core = MagicMock(spec=NemorosaCore)
    core.process_single_torrent = AsyncMock()
    core.process_reverse_announce_torrent = AsyncMock()
    return core


@pytest.fixture
def mock_job_manager() -> MagicMock:
    """Create a mock JobManager."""
    jm = MagicMock(spec=JobManager)
    jm.trigger_job_early = AsyncMock()
    jm.get_job_status = AsyncMock()
    return jm


@pytest.fixture
def client(
    mock_core: MagicMock, mock_job_manager: MagicMock
) -> Generator[TestClient, None, None]:
    """Create a TestClient with dependency overrides and no-op lifespan."""

    # Replace lifespan to avoid initializing the full application
    @asynccontextmanager
    async def _noop_lifespan(_app):
        yield

    original_router_lifespan = app.router.lifespan_context
    app.router.lifespan_context = _noop_lifespan

    app.dependency_overrides[get_core] = lambda: mock_core
    app.dependency_overrides[get_job_manager] = lambda: mock_job_manager
    app.dependency_overrides[verify_api_key] = lambda: True

    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

    app.dependency_overrides.clear()
    app.router.lifespan_context = original_router_lifespan


# --- Tests for webhook endpoint ---


class TestWebhookEndpoint:
    """Tests for POST /api/webhook."""

    def test_webhook_success(self, client: TestClient, mock_core: MagicMock) -> None:
        """Should return 200 when torrent is successfully processed."""
        mock_core.process_single_torrent.return_value = ProcessResponse(
            status=ProcessStatus.SUCCESS,
            message="Injected torrent abc123",
        )

        resp = client.post("/api/webhook?infohash=abc123")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "success"
        mock_core.process_single_torrent.assert_awaited_once_with("abc123")

    def test_webhook_not_found(self, client: TestClient, mock_core: MagicMock) -> None:
        """Should return 204 when no match is found."""
        mock_core.process_single_torrent.return_value = ProcessResponse(
            status=ProcessStatus.NOT_FOUND,
            message="No match",
        )

        resp = client.post("/api/webhook?infohash=abc123")

        assert resp.status_code == 204

    def test_webhook_error(self, client: TestClient, mock_core: MagicMock) -> None:
        """Should return 500 when processing error occurs."""
        mock_core.process_single_torrent.return_value = ProcessResponse(
            status=ProcessStatus.ERROR,
            message="Something broke",
        )

        resp = client.post("/api/webhook?infohash=abc123")

        assert resp.status_code == 500

    def test_webhook_skipped(self, client: TestClient, mock_core: MagicMock) -> None:
        """Should return 200 for skipped status."""
        mock_core.process_single_torrent.return_value = ProcessResponse(
            status=ProcessStatus.SKIPPED,
            message="Already exists",
        )

        resp = client.post("/api/webhook?infohash=abc123")

        assert resp.status_code == 200

    def test_webhook_missing_infohash(self, client: TestClient) -> None:
        """Should return 422 when infohash is missing."""
        resp = client.post("/api/webhook")

        assert resp.status_code == 422

    def test_webhook_exception(self, client: TestClient, mock_core: MagicMock) -> None:
        """Should return 500 when an unexpected exception occurs."""
        mock_core.process_single_torrent.side_effect = RuntimeError("boom")

        resp = client.post("/api/webhook?infohash=abc123")

        assert resp.status_code == 500


# --- Tests for announce endpoint ---


class TestAnnounceEndpoint:
    """Tests for POST /api/announce."""

    def test_announce_success(self, client: TestClient, mock_core: MagicMock) -> None:
        """Should return 200 when announce is successfully processed."""
        mock_core.process_reverse_announce_torrent.return_value = ProcessResponse(
            status=ProcessStatus.SUCCESS,
            message="Processed announce",
        )

        resp = client.post(
            "/api/announce",
            json={
                "name": "Test.Album.FLAC",
                "link": "https://tracker.example.com/torrents.php?id=123",
                "album": "Test Album",
            },
        )

        assert resp.status_code == 200
        mock_core.process_reverse_announce_torrent.assert_awaited_once_with(
            torrent_name="Test.Album.FLAC",
            torrent_link="https://tracker.example.com/torrents.php?id=123",
            album_name="Test Album",
        )

    def test_announce_not_found(self, client: TestClient, mock_core: MagicMock) -> None:
        """Should return 204 when no match is found."""
        mock_core.process_reverse_announce_torrent.return_value = ProcessResponse(
            status=ProcessStatus.NOT_FOUND,
            message="No match",
        )

        resp = client.post(
            "/api/announce",
            json={
                "name": "Test.Album.FLAC",
                "link": "https://tracker.example.com/torrents.php?id=123",
                "album": "Test Album",
            },
        )

        assert resp.status_code == 204

    def test_announce_error(self, client: TestClient, mock_core: MagicMock) -> None:
        """Should return 500 when processing error occurs."""
        mock_core.process_reverse_announce_torrent.return_value = ProcessResponse(
            status=ProcessStatus.ERROR,
            message="API failure",
        )

        resp = client.post(
            "/api/announce",
            json={
                "name": "Test.Album.FLAC",
                "link": "https://tracker.example.com/torrents.php?id=123",
                "album": "Test Album",
            },
        )

        assert resp.status_code == 500

    def test_announce_skipped_returns_204(
        self, client: TestClient, mock_core: MagicMock
    ) -> None:
        """Should return 204 for skipped status (default case)."""
        mock_core.process_reverse_announce_torrent.return_value = ProcessResponse(
            status=ProcessStatus.SKIPPED,
            message="Already exists",
        )

        resp = client.post(
            "/api/announce",
            json={
                "name": "Test.Album.FLAC",
                "link": "https://tracker.example.com/torrents.php?id=123",
                "album": "Test Album",
            },
        )

        assert resp.status_code == 204

    def test_announce_missing_fields(self, client: TestClient) -> None:
        """Should return 422 when required fields are missing."""
        resp = client.post("/api/announce", json={"name": "Test"})

        assert resp.status_code == 422


# --- Tests for job endpoints ---


class TestTriggerJobEndpoint:
    """Tests for POST /api/job."""

    def test_trigger_job_success(
        self, client: TestClient, mock_job_manager: MagicMock
    ) -> None:
        """Should return 200 when job is triggered successfully."""
        mock_job_manager.trigger_job_early.return_value = JobResponse(
            status="success",
            message="Job search triggered successfully",
            job_name="search",
        )

        resp = client.post("/api/job?job_type=search")

        assert resp.status_code == 200
        assert resp.json()["status"] == "success"
        mock_job_manager.trigger_job_early.assert_awaited_once_with(JobType.SEARCH)

    def test_trigger_job_not_found(
        self, client: TestClient, mock_job_manager: MagicMock
    ) -> None:
        """Should return 404 when job is not found."""
        mock_job_manager.trigger_job_early.return_value = JobResponse(
            status="not_found",
            message="Job search not found",
            job_name="search",
        )

        resp = client.post("/api/job?job_type=search")

        assert resp.status_code == 404

    def test_trigger_job_conflict(
        self, client: TestClient, mock_job_manager: MagicMock
    ) -> None:
        """Should return 409 when job is already running."""
        mock_job_manager.trigger_job_early.return_value = JobResponse(
            status="conflict",
            message="Job search is currently running",
            job_name="search",
        )

        resp = client.post("/api/job?job_type=search")

        assert resp.status_code == 409


class TestGetJobStatusEndpoint:
    """Tests for GET /api/job."""

    def test_get_job_status_active(
        self, client: TestClient, mock_job_manager: MagicMock
    ) -> None:
        """Should return 200 with active status."""
        mock_job_manager.get_job_status.return_value = JobResponse(
            status="active",
            message="Job cleanup is active",
            job_name="cleanup",
            next_run="2026-02-25T12:00:00+00:00",
        )

        resp = client.get("/api/job?job_type=cleanup")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "active"
        assert data["next_run"] is not None
        mock_job_manager.get_job_status.assert_awaited_once_with(JobType.CLEANUP)

    def test_get_job_status_not_found(
        self, client: TestClient, mock_job_manager: MagicMock
    ) -> None:
        """Should return 200 with not_found status (not HTTP 404)."""
        mock_job_manager.get_job_status.return_value = JobResponse(
            status="not_found",
            message="Job search not found",
            job_name="search",
        )

        resp = client.get("/api/job?job_type=search")

        # get_job_status returns the response directly, no HTTP error mapping
        assert resp.status_code == 200
        assert resp.json()["status"] == "not_found"

    def test_get_job_status_exception(
        self, client: TestClient, mock_job_manager: MagicMock
    ) -> None:
        """Should return 500 when an unexpected exception occurs."""
        mock_job_manager.get_job_status.side_effect = RuntimeError("db error")

        resp = client.get("/api/job?job_type=cleanup")

        assert resp.status_code == 500
