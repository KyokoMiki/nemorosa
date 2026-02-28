"""Core processing package for nemorosa."""

from .injector import TorrentInjector
from .models import (
    PostProcessStats,
    ProcessorStats,
    ProcessResponse,
    ProcessStatus,
    SearchMatchResult,
)
from .processor import NemorosaCore
from .registry import get_core, init_core
from .searcher import TorrentSearcher

__all__ = [
    "NemorosaCore",
    "PostProcessStats",
    "ProcessorStats",
    "ProcessResponse",
    "ProcessStatus",
    "SearchMatchResult",
    "TorrentInjector",
    "TorrentSearcher",
    "get_core",
    "init_core",
]
