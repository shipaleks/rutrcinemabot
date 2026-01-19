"""External services integration.

This module provides integrations with external services:
- Letterboxd: Movie tracking and social features (API + RSS)
"""

from src.services.letterboxd import (
    LetterboxdAPIError,
    LetterboxdAuthError,
    LetterboxdClient,
    LetterboxdDiaryEntry,
    LetterboxdError,
    LetterboxdFilm,
    LetterboxdNotConfiguredError,
    LetterboxdOAuth,
    LetterboxdRating,
    LetterboxdUser,
    LetterboxdWatchlistEntry,
    OAuthToken,
)
from src.services.letterboxd_rss import (
    LetterboxdRSS,
    LetterboxdRSSDiaryEntry,
    LetterboxdRSSError,
    LetterboxdRSSFilm,
    LetterboxdRSSWatchlistItem,
    sync_letterboxd_to_storage,
)

__all__ = [
    # API Client (requires approval)
    "LetterboxdClient",
    "LetterboxdOAuth",
    # API Models
    "LetterboxdFilm",
    "LetterboxdWatchlistEntry",
    "LetterboxdDiaryEntry",
    "LetterboxdRating",
    "LetterboxdUser",
    "OAuthToken",
    # API Exceptions
    "LetterboxdError",
    "LetterboxdAuthError",
    "LetterboxdAPIError",
    "LetterboxdNotConfiguredError",
    # RSS Client (no approval needed)
    "LetterboxdRSS",
    "LetterboxdRSSFilm",
    "LetterboxdRSSWatchlistItem",
    "LetterboxdRSSDiaryEntry",
    "LetterboxdRSSError",
    "sync_letterboxd_to_storage",
]
