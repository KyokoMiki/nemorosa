"""Gazelle API package for nemorosa."""

from .api_common import (
    AuthMethod,
    GazelleBase,
    InvalidCredentialsException,
    RequestException,
    TorrentSearchResult,
    TrackerSpec,
)
from .gazelle_games import GazelleGamesNet
from .gazelle_html import GazelleParser
from .gazelle_json import GazelleJSONAPI
from .registry import (
    TRACKER_REGISTRY,
    GazelleAPI,
    cleanup_api,
    find_api_by_site_host,
    find_api_by_tracker,
    get_api_by_site_host,
    get_api_by_tracker,
    get_api_instance,
    get_target_apis,
    init_api,
)

__all__ = [
    "AuthMethod",
    "GazelleAPI",
    "GazelleBase",
    "GazelleGamesNet",
    "GazelleJSONAPI",
    "GazelleParser",
    "InvalidCredentialsException",
    "RequestException",
    "TRACKER_REGISTRY",
    "TorrentSearchResult",
    "TrackerSpec",
    "cleanup_api",
    "find_api_by_site_host",
    "find_api_by_tracker",
    "get_api_by_site_host",
    "get_api_by_tracker",
    "get_api_instance",
    "get_target_apis",
    "init_api",
]
