"""Media metadata module.

Provides clients for fetching movie and TV show metadata from various sources:
- TMDB (The Movie Database) for international content
- Kinopoisk for Russian content

All clients are async and include caching for performance.
"""

from src.media.kinopoisk import (
    KinopoiskClient,
    KinopoiskError,
    KinopoiskFilm,
    KinopoiskMediaType,
    KinopoiskNotFoundError,
    KinopoiskSearchResult,
    KinopoiskUnavailableError,
    get_kinopoisk_film,
    get_kinopoisk_rating,
    search_kinopoisk,
)
from src.media.tmdb import (
    Credits,
    MediaType,
    Movie,
    Person,
    SearchResult,
    TMDBClient,
    TMDBError,
    TMDBNotFoundError,
    TMDBRateLimitError,
    TVShow,
)

__all__ = [
    # TMDB
    "TMDBClient",
    "TMDBError",
    "TMDBNotFoundError",
    "TMDBRateLimitError",
    "Movie",
    "TVShow",
    "MediaType",
    "Person",
    "Credits",
    "SearchResult",
    # Kinopoisk
    "KinopoiskClient",
    "KinopoiskError",
    "KinopoiskNotFoundError",
    "KinopoiskUnavailableError",
    "KinopoiskFilm",
    "KinopoiskSearchResult",
    "KinopoiskMediaType",
    "search_kinopoisk",
    "get_kinopoisk_film",
    "get_kinopoisk_rating",
]
