"""Search module for torrent trackers.

This module provides async clients for searching movies and TV shows
on various torrent trackers including Rutracker and PirateBay.
"""

from src.search.piratebay import (
    PirateBayClient,
    PirateBayError,
    PirateBayResult,
    PirateBayUnavailableError,
    search_piratebay,
)
from src.search.piratebay import (
    search_with_fallback as search_piratebay_with_fallback,
)
from src.search.rutracker import (
    RutrackerBlockedError,
    RutrackerCaptchaError,
    RutrackerClient,
    RutrackerError,
    SearchResult,
    search_rutracker,
)

__all__ = [
    # Rutracker
    "RutrackerClient",
    "RutrackerError",
    "RutrackerBlockedError",
    "RutrackerCaptchaError",
    "SearchResult",
    "search_rutracker",
    # PirateBay
    "PirateBayClient",
    "PirateBayError",
    "PirateBayUnavailableError",
    "PirateBayResult",
    "search_piratebay",
    "search_piratebay_with_fallback",
]
