"""Data models for nemorosa core processing."""

from enum import StrEnum
from typing import TYPE_CHECKING

import msgspec
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from torf import Torrent


class ProcessStatus(StrEnum):
    """Status enumeration for process operations."""

    SUCCESS = "success"
    NOT_FOUND = "not_found"
    ERROR = "error"
    SKIPPED = "skipped"
    SKIPPED_POTENTIAL_TRUMP = "skipped_potential_trump"


class ProcessorStats(msgspec.Struct):
    """Statistics for torrent processing session."""

    found: int = 0
    downloaded: int = 0
    scanned: int = 0
    cnt_dl_fail: int = 0
    attempted: int = 0
    successful: int = 0
    failed: int = 0
    removed: int = 0


class PostProcessStats(msgspec.Struct):
    """Statistics for post-processing injected torrents."""

    matches_checked: int = 0
    matches_completed: int = 0
    matches_started_downloading: int = 0
    matches_failed: int = 0


class SearchMatchResult(msgspec.Struct, frozen=True):
    """Result of a torrent search match operation.

    Attributes:
        torrent_id: Matched torrent ID, or None if not found.
        matched_torrent: Downloaded torrent object, or None.
        hash_match: Whether the match was found via hash search.
        search_success: Whether the search completed without errors.
    """

    torrent_id: int | None = None
    matched_torrent: "Torrent | None" = None
    hash_match: bool = True
    search_success: bool = True


class ProcessResponse(BaseModel):
    """Response model for process operations."""

    status: ProcessStatus = Field(..., description="Processing status")
    message: str = Field(..., description="Status message")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "success",
                    "message": "Successfully processed torrent: name (infohash)",
                },
                {
                    "status": "skipped",
                    "message": (
                        "Torrent already exists on all target trackers: "
                        "[tracker1, tracker2]"
                    ),
                },
            ]
        }
    }
