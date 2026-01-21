"""
Nemorosa - Cross-seeding tool specifically designed for Gazelle-based music trackers.
"""

__version__ = "0.4.2"
__author__ = "KyokoMiki"
__description__ = (
    "A specialized cross-seeding tool designed for music torrents, featuring "
    "automatic file mapping, partial matching, and seamless torrent injection"
)

from .cli import main

__all__ = ["main", "__version__", "__author__", "__description__"]
