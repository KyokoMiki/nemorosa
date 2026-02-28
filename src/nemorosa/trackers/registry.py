"""Gazelle API registry and global instance management for nemorosa."""

from collections.abc import Collection
from contextlib import suppress
from http.cookies import SimpleCookie
from typing import TypeAlias

import anyio

from .. import logger
from ..config import TargetSiteConfig
from .api_common import TrackerSpec
from .gazelle_games import GazelleGamesNet
from .gazelle_html import GazelleParser
from .gazelle_json import GazelleJSONAPI

# Type alias for API instance types
GazelleAPI: TypeAlias = GazelleJSONAPI | GazelleParser | GazelleGamesNet

TRACKER_REGISTRY: dict[str, tuple[type[GazelleAPI], TrackerSpec]] = {
    "https://redacted.sh": (
        GazelleJSONAPI,
        TrackerSpec(
            rate_limit_max_requests=10,
            rate_limit_period=10.0,
            source_flag="RED",
            tracker_url="https://flacsfor.me",
            tracker_query="flacsfor.me",
        ),
    ),
    "https://orpheus.network": (
        GazelleJSONAPI,
        TrackerSpec(
            rate_limit_max_requests=5,
            rate_limit_period=10.0,
            source_flag="OPS",
            tracker_url="https://home.opsfet.ch",
            tracker_query="home.opsfet.ch",
        ),
    ),
    "https://dicmusic.com": (
        GazelleJSONAPI,
        TrackerSpec(
            rate_limit_max_requests=5,
            rate_limit_period=10.0,
            source_flag="DICMusic",
            tracker_url="https://tracker.52dic.vip",
            tracker_query="tracker.52dic.vip",
        ),
    ),
    "https://libble.me": (
        GazelleParser,
        TrackerSpec(
            rate_limit_max_requests=2,
            rate_limit_period=10.0,
            source_flag="LENNY",
            tracker_url="https://tracker.libble.me:34443",
            tracker_query="tracker.libble.me",
        ),
    ),
    "https://lztr.me": (
        GazelleParser,
        TrackerSpec(
            rate_limit_max_requests=2,
            rate_limit_period=10.0,
            source_flag="LZTR",
            tracker_url="https://tracker.lztr.me:34443",
            tracker_query="tracker.lztr.me",
        ),
    ),
    "https://gazellegames.net": (
        GazelleGamesNet,
        TrackerSpec(
            # GGn officially allows 5 requests per 10 seconds, but does not support
            # burst behavior from AsyncLimiter's leaky bucket algorithm. Using 1/2s
            # (equivalent to 5/10s without burst) to avoid rate limit errors.
            rate_limit_max_requests=1,
            rate_limit_period=2.0,
            source_flag="GGn",
            tracker_url="https://tracker.gazellegames.net",
            tracker_query="tracker.gazellegames.net",
        ),
    ),
}


def get_api_instance(
    server: str,
    cookies: SimpleCookie | None = None,
    api_key: str | None = None,
) -> GazelleAPI:
    """Get appropriate API instance based on server address.

    Args:
        server: Server address.
        cookies: Optional cookies.
        api_key: Optional API key.

    Returns:
        API instance for the given server.

    Raises:
        ValueError: If unsupported server is provided.
    """
    if server not in TRACKER_REGISTRY:
        raise ValueError(f"Unsupported server: {server}")

    api_class, spec = TRACKER_REGISTRY[server]
    return api_class(server=server, spec=spec, cookies=cookies, api_key=api_key)


# Global target_apis instance
_target_apis_instance: list[GazelleAPI] = []
_target_apis_lock = anyio.Lock()


async def init_api(target_sites: list[TargetSiteConfig]) -> None:
    """Initialize global target APIs instance.

    Should be called once during application startup.

    Args:
        target_sites (list[TargetSiteConfig]): List of TargetSiteConfig objects.

    Raises:
        RuntimeError: If no API connections were successful or if already initialized.
    """
    global _target_apis_instance
    async with _target_apis_lock:
        if _target_apis_instance:
            raise RuntimeError("API already initialized.")

        logger.section("===== Establishing API Connections =====")
        target_apis = []

        for i, site in enumerate(target_sites):
            # Parse cookie string to SimpleCookie if present
            site_cookies = SimpleCookie(site.cookie) if site.cookie else None

            logger.debug(
                "Connecting to target site %s/%s: %s",
                i + 1,
                len(target_sites),
                site.server,
            )
            api_instance = get_api_instance(
                server=site.server, api_key=site.api_key, cookies=site_cookies
            )
            try:
                await api_instance.auth()
                target_apis.append(api_instance)
                logger.success("API connection established for %s", site.server)
            except Exception as e:
                logger.error("API connection failed for %s: %s", site.server, str(e))
                # Close the failed instance to prevent resource leak
                with suppress(Exception):
                    await api_instance.close()
                # Continue processing other sites, don't exit program

        if not target_apis:
            logger.critical("No API connections were successful")
            raise RuntimeError("Failed to establish any API connections")

        logger.success("Successfully connected to %d target site(s)", len(target_apis))
        _target_apis_instance = target_apis


def get_target_apis() -> list[GazelleAPI]:
    """Get global target APIs instance.

    Must be called after init_api() has been invoked.

    Returns:
        list[GazelleAPI]: Target APIs instance.

    Raises:
        RuntimeError: If target APIs have not been initialized.
    """
    if not _target_apis_instance:
        raise RuntimeError("Target APIs not initialized. Call init_api() first.")
    return _target_apis_instance


def get_api_by_tracker(
    trackers: str | Collection[str],
) -> GazelleAPI | None:
    """Get API instance by matching tracker query string (global wrapper).

    Args:
        trackers (str | Collection[str]): A single tracker URL string or a
            collection of tracker URLs.

    Returns:
        The first matching API instance, or None if no match is found.

    Raises:
        RuntimeError: If target APIs have not been initialized.
    """
    return find_api_by_tracker(get_target_apis(), trackers)


def get_api_by_site_host(
    site_host: str,
) -> GazelleAPI | None:
    """Get API instance by matching site hostname (global wrapper).

    Args:
        site_host (str): Site hostname (e.g., 'redacted.sh', 'orpheus.network').

    Returns:
        The matching API instance, or None if no match is found.

    Raises:
        RuntimeError: If target APIs have not been initialized.
    """
    return find_api_by_site_host(get_target_apis(), site_host)


def find_api_by_tracker(
    apis: list[GazelleAPI],
    trackers: str | Collection[str],
) -> GazelleAPI | None:
    """Find API instance by matching tracker query string.

    Pure function: no global state access.

    Args:
        apis: List of API instances to search.
        trackers: A single tracker URL string or a collection of tracker URLs.

    Returns:
        The first matching API instance, or None if no match is found.
    """
    tracker_list = [trackers] if isinstance(trackers, str) else trackers

    for api_instance in apis:
        for tracker in tracker_list:
            if api_instance.tracker_query in tracker:
                return api_instance

    return None


def find_api_by_site_host(
    apis: list[GazelleAPI],
    site_host: str,
) -> GazelleAPI | None:
    """Find API instance by matching site hostname.

    Pure function: no global state access.

    Args:
        apis: List of API instances to search.
        site_host: Site hostname (e.g., 'redacted.sh', 'orpheus.network').

    Returns:
        The matching API instance, or None if no match is found.
    """
    for api_instance in apis:
        if api_instance.site_host == site_host:
            return api_instance

    return None


async def cleanup_api() -> None:
    """Close all API client sessions and cleanup resources.

    This function should be called during application shutdown to properly
    close all aiohttp ClientSession instances and release resources.
    Errors during cleanup are logged but do not prevent other clients from closing.
    """
    global _target_apis_instance
    async with _target_apis_lock:
        if not _target_apis_instance:
            logger.debug("No API instances to cleanup")
            return

        logger.debug("Cleaning up %d API client(s)...", len(_target_apis_instance))
        cleanup_errors = []

        for api_instance in _target_apis_instance:
            try:
                await api_instance.close()
                logger.debug("Closed API client for %s", api_instance.server)
            except Exception as e:
                error_msg = f"Error closing API client for {api_instance.server}: {e}"
                logger.warning(error_msg)
                cleanup_errors.append(error_msg)

        # Clear the instance list
        _target_apis_instance = []

        if cleanup_errors:
            logger.warning(
                "API cleanup completed with %d error(s)", len(cleanup_errors)
            )
        else:
            logger.debug("All API clients closed successfully")
