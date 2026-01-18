"""Search module for torrent trackers.

This module provides async clients for searching movies and TV shows
on various torrent trackers including Rutracker and PirateBay.
"""

from src.search.rutracker import (
    RutrackerBlockedError,
    RutrackerCaptchaError,
    RutrackerClient,
    RutrackerError,
    SearchResult,
    search_rutracker,
)

__all__ = [
    "RutrackerClient",
    "RutrackerError",
    "RutrackerBlockedError",
    "RutrackerCaptchaError",
    "SearchResult",
    "search_rutracker",
]
