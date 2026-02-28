"""Core global instance management for nemorosa."""

import anyio

from .. import config
from ..clients import get_torrent_client
from ..db import get_database
from ..notifier import get_notifier
from ..trackers import get_target_apis
from .injector import TorrentInjector
from .processor import NemorosaCore
from .searcher import TorrentSearcher

# Global core instance
_core_instance: NemorosaCore | None = None
_core_lock = anyio.Lock()


async def init_core() -> None:
    """Initialize global core instance.

    Assembles NemorosaCore from global singletons.
    Should be called once during application startup.

    Raises:
        RuntimeError: If already initialized.
    """
    global _core_instance
    async with _core_lock:
        if _core_instance is not None:
            raise RuntimeError("Core already initialized.")

        torrent_client = get_torrent_client()
        database = get_database()
        target_apis = get_target_apis()
        searcher = TorrentSearcher()
        injector = TorrentInjector(torrent_client)

        # Build notifier only when notification URLs are configured
        notifier = None
        if config.cfg.global_config.notification_urls:
            notifier = get_notifier()

        _core_instance = NemorosaCore(
            torrent_client=torrent_client,
            database=database,
            searcher=searcher,
            injector=injector,
            target_apis=target_apis,
            notifier=notifier,
        )


def get_core() -> NemorosaCore:
    """Get global core instance.

    Must be called after init_core() has been invoked.

    Returns:
        NemorosaCore: Core instance.

    Raises:
        RuntimeError: If core has not been initialized.
    """
    if _core_instance is None:
        raise RuntimeError("Core not initialized. Call init_core() first.")
    return _core_instance
