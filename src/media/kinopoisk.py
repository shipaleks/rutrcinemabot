"""Kinopoisk Unofficial API client.

Provides async functionality for searching and retrieving movie and TV show
metadata from Kinopoisk. Includes caching and graceful degradation.

API Documentation: https://kinopoiskapiunofficial.tech/documentation/api/

Note: This uses an unofficial API that may have rate limits and availability issues.
All methods implement graceful degradation - they return None or empty results
instead of raising exceptions when the API is unavailable.
"""

import time
from enum import Enum
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

from src.config import settings

logger = structlog.get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

KINOPOISK_BASE_URL = "https://kinopoiskapiunofficial.tech/api"

# Request timeout in seconds (shorter than TMDB due to potential instability)
REQUEST_TIMEOUT = 10.0

# API version endpoints
API_V22 = "/v2.2"
API_V21 = "/v2.1"

# Max results to return
MAX_RESULTS = 20


# =============================================================================
# Enums
# =============================================================================


class KinopoiskMediaType(str, Enum):
    """Type of media content in Kinopoisk."""

    FILM = "FILM"
    TV_SHOW = "TV_SHOW"
    TV_SERIES = "TV_SERIES"
    MINI_SERIES = "MINI_SERIES"
    ALL = "ALL"


class KinopoiskOrder(str, Enum):
    """Sort order for search results."""

    RATING = "RATING"
    NUM_VOTE = "NUM_VOTE"
    YEAR = "YEAR"


# =============================================================================
# Exceptions
# =============================================================================


class KinopoiskError(Exception):
    """Base exception for Kinopoisk API errors."""

    pass


class KinopoiskNotFoundError(KinopoiskError):
    """Raised when a resource is not found on Kinopoisk."""

    pass


class KinopoiskRateLimitError(KinopoiskError):
    """Raised when Kinopoisk rate limit is exceeded."""

    def __init__(self, retry_after: int = 1):
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded. Retry after {retry_after} seconds.")


class KinopoiskAuthError(KinopoiskError):
    """Raised when Kinopoisk API token is invalid."""

    pass


class KinopoiskUnavailableError(KinopoiskError):
    """Raised when Kinopoisk API is temporarily unavailable."""

    pass


# =============================================================================
# Data Models
# =============================================================================


class KinopoiskCountry(BaseModel):
    """Country information from Kinopoisk."""

    country: str


class KinopoiskGenre(BaseModel):
    """Genre information from Kinopoisk."""

    genre: str


class KinopoiskFilm(BaseModel):
    """Film information from Kinopoisk.

    Contains metadata including Russian title, description, and Kinopoisk rating.
    """

    kinopoisk_id: int = Field(..., alias="kinopoiskId")
    imdb_id: str | None = Field(default=None, alias="imdbId")
    name_ru: str | None = Field(default=None, alias="nameRu")
    name_en: str | None = Field(default=None, alias="nameEn")
    name_original: str | None = Field(default=None, alias="nameOriginal")
    poster_url: str | None = Field(default=None, alias="posterUrl")
    poster_url_preview: str | None = Field(default=None, alias="posterUrlPreview")
    cover_url: str | None = Field(default=None, alias="coverUrl")
    logo_url: str | None = Field(default=None, alias="logoUrl")
    rating_kinopoisk: float | None = Field(default=None, alias="ratingKinopoisk")
    rating_imdb: float | None = Field(default=None, alias="ratingImdb")
    rating_kinopoisk_vote_count: int | None = Field(default=None, alias="ratingKinopoiskVoteCount")
    rating_imdb_vote_count: int | None = Field(default=None, alias="ratingImdbVoteCount")
    year: int | None = None
    film_length: int | None = Field(default=None, alias="filmLength")
    slogan: str | None = None
    description: str | None = None
    short_description: str | None = Field(default=None, alias="shortDescription")
    type: str | None = None
    rating_mpaa: str | None = Field(default=None, alias="ratingMpaa")
    rating_age_limits: str | None = Field(default=None, alias="ratingAgeLimits")
    start_year: int | None = Field(default=None, alias="startYear")
    end_year: int | None = Field(default=None, alias="endYear")
    serial: bool | None = None
    completed: bool | None = None
    countries: list[KinopoiskCountry] = Field(default_factory=list)
    genres: list[KinopoiskGenre] = Field(default_factory=list)
    web_url: str | None = Field(default=None, alias="webUrl")

    model_config = {"populate_by_name": True}

    def get_title(self) -> str:
        """Get the best available title.

        Returns Russian title if available, otherwise English or original.

        Returns:
            Film title string
        """
        return self.name_ru or self.name_en or self.name_original or "Unknown"

    def get_english_title(self) -> str | None:
        """Get English or original title.

        Returns:
            English/original title or None
        """
        return self.name_en or self.name_original

    def get_genre_names(self) -> list[str]:
        """Get list of genre names.

        Returns:
            List of genre names in Russian
        """
        return [g.genre for g in self.genres]

    def get_country_names(self) -> list[str]:
        """Get list of country names.

        Returns:
            List of country names
        """
        return [c.country for c in self.countries]

    def is_tv_series(self) -> bool:
        """Check if this is a TV series.

        Returns:
            True if TV series, mini-series, or TV show
        """
        if self.serial:
            return True
        if self.type:
            return self.type in ("TV_SERIES", "TV_SHOW", "MINI_SERIES")
        return False


class KinopoiskSearchResult(BaseModel):
    """Search result from Kinopoisk.

    Simplified model for search results with basic info.
    """

    kinopoisk_id: int = Field(..., alias="kinopoiskId")
    imdb_id: str | None = Field(default=None, alias="imdbId")
    name_ru: str | None = Field(default=None, alias="nameRu")
    name_en: str | None = Field(default=None, alias="nameEn")
    name_original: str | None = Field(default=None, alias="nameOriginal")
    poster_url: str | None = Field(default=None, alias="posterUrl")
    poster_url_preview: str | None = Field(default=None, alias="posterUrlPreview")
    rating_kinopoisk: float | None = Field(default=None, alias="ratingKinopoisk")
    rating_imdb: float | None = Field(default=None, alias="ratingImdb")
    year: int | None = None
    type: str | None = None
    countries: list[KinopoiskCountry] = Field(default_factory=list)
    genres: list[KinopoiskGenre] = Field(default_factory=list)

    model_config = {"populate_by_name": True}

    def get_title(self) -> str:
        """Get the best available title."""
        return self.name_ru or self.name_en or self.name_original or "Unknown"

    def get_english_title(self) -> str | None:
        """Get English or original title."""
        return self.name_en or self.name_original


# =============================================================================
# Cache Implementation
# =============================================================================


class SimpleCache:
    """Simple in-memory cache with TTL support.

    Thread-safe for single-threaded async use (no locking needed).
    Copied from tmdb.py for consistency.
    """

    def __init__(self, ttl: int | None = None):
        """Initialize cache.

        Args:
            ttl: Time-to-live in seconds. Uses settings.cache_ttl if None.
        """
        self._cache: dict[str, tuple[Any, float]] = {}
        self._ttl = ttl if ttl is not None else settings.cache_ttl

    def get(self, key: str) -> Any | None:
        """Get value from cache if not expired.

        Args:
            key: Cache key

        Returns:
            Cached value or None if not found/expired
        """
        if key not in self._cache:
            return None

        value, timestamp = self._cache[key]
        if time.time() - timestamp > self._ttl:
            del self._cache[key]
            return None

        return value

    def set(self, key: str, value: Any) -> None:
        """Set value in cache.

        Args:
            key: Cache key
            value: Value to cache
        """
        self._cache[key] = (value, time.time())

    def clear(self) -> None:
        """Clear all cached values."""
        self._cache.clear()

    def cleanup_expired(self) -> int:
        """Remove expired entries from cache.

        Returns:
            Number of entries removed
        """
        now = time.time()
        expired = [k for k, (_, t) in self._cache.items() if now - t > self._ttl]
        for key in expired:
            del self._cache[key]
        return len(expired)


# =============================================================================
# Kinopoisk Client
# =============================================================================


class KinopoiskClient:
    """Async client for Kinopoisk Unofficial API.

    Provides methods for searching movies and TV shows, retrieving details,
    and getting Kinopoisk ratings. Implements graceful degradation for API
    unavailability.

    All methods return None or empty lists instead of raising exceptions
    when the API is unavailable, allowing the bot to continue functioning.

    Example:
        async with KinopoiskClient() as client:
            results = await client.search("Брат")
            if results:
                film = await client.get_film(results[0].kinopoisk_id)
                if film:
                    print(f"Rating: {film.rating_kinopoisk}")

        # Or with graceful degradation (won't raise on API errors):
        async with KinopoiskClient() as client:
            film = await client.get_film_safe(123)  # Returns None on error
    """

    def __init__(
        self,
        api_token: str | None = None,
        cache_ttl: int | None = None,
    ):
        """Initialize Kinopoisk client.

        Args:
            api_token: Kinopoisk API token. Uses settings.kinopoisk_api_token if None.
            cache_ttl: Cache TTL in seconds. Uses settings.cache_ttl if None.
        """
        self._api_token = api_token or settings.kinopoisk_api_token.get_secret_value()
        self._client: httpx.AsyncClient | None = None
        self._cache = SimpleCache(ttl=cache_ttl)

    async def __aenter__(self) -> "KinopoiskClient":
        """Enter async context manager."""
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={
                "X-API-KEY": self._api_token,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        return self

    async def __aexit__(
        self,
        _exc_type: Any,
        _exc_val: Any,
        _exc_tb: Any,
    ) -> None:
        """Exit async context manager."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get HTTP client, ensuring it's initialized.

        Returns:
            The async HTTP client

        Raises:
            RuntimeError: If client not initialized (not in context manager)
        """
        if self._client is None:
            raise RuntimeError("KinopoiskClient must be used as async context manager")
        return self._client

    def _get_cache_key(self, endpoint: str, params: dict[str, Any]) -> str:
        """Generate cache key from endpoint and parameters.

        Args:
            endpoint: API endpoint
            params: Request parameters

        Returns:
            Cache key string
        """
        param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"kp:{endpoint}?{param_str}"

    async def _request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Make authenticated request to Kinopoisk API.

        Args:
            endpoint: API endpoint (without base URL)
            params: Query parameters
            use_cache: Whether to use cache for this request

        Returns:
            JSON response as dictionary

        Raises:
            KinopoiskNotFoundError: Resource not found (404)
            KinopoiskRateLimitError: Rate limit exceeded (402)
            KinopoiskAuthError: Invalid API key (401)
            KinopoiskUnavailableError: API temporarily unavailable
            KinopoiskError: Other API errors
        """
        # Build params
        full_params = params.copy() if params else {}

        # Check cache
        cache_key = self._get_cache_key(endpoint, full_params)
        if use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug("kinopoisk_cache_hit", endpoint=endpoint)
                return cached

        # Make request
        url = f"{KINOPOISK_BASE_URL}{endpoint}"
        logger.debug("kinopoisk_request", endpoint=endpoint, params=params)

        try:
            response = await self.client.get(url, params=full_params)
        except httpx.TimeoutException as e:
            logger.warning("kinopoisk_timeout", endpoint=endpoint)
            raise KinopoiskUnavailableError(f"Request timeout: {e}") from e
        except httpx.HTTPError as e:
            logger.warning("kinopoisk_http_error", endpoint=endpoint, error=str(e))
            raise KinopoiskUnavailableError(f"HTTP error: {e}") from e

        # Handle response status
        if response.status_code == 200:
            data = response.json()
            if use_cache:
                self._cache.set(cache_key, data)
            return data

        if response.status_code == 401:
            raise KinopoiskAuthError("Invalid Kinopoisk API token")
        if response.status_code == 402:
            # Kinopoisk uses 402 for rate limit/quota exceeded
            raise KinopoiskRateLimitError(retry_after=60)
        if response.status_code == 404:
            raise KinopoiskNotFoundError(f"Resource not found: {endpoint}")
        if response.status_code >= 500:
            raise KinopoiskUnavailableError(f"Kinopoisk API unavailable: {response.status_code}")

        error_msg = response.text[:200] if response.text else "Unknown error"
        raise KinopoiskError(f"Kinopoisk API error {response.status_code}: {error_msg}")

    # =========================================================================
    # Search Methods
    # =========================================================================

    async def search(
        self,
        query: str,
        page: int = 1,
        media_type: KinopoiskMediaType = KinopoiskMediaType.ALL,
    ) -> list[KinopoiskSearchResult]:
        """Search for films and TV shows by keyword.

        Searches both Russian and English titles.

        Args:
            query: Search query (Russian or English)
            page: Page number (1-based)
            media_type: Filter by media type (FILM, TV_SERIES, etc.)

        Returns:
            List of search results

        Raises:
            KinopoiskError: On API errors (use search_safe for graceful degradation)
        """
        params: dict[str, Any] = {
            "keyword": query,
            "page": page,
        }
        if media_type != KinopoiskMediaType.ALL:
            params["type"] = media_type.value

        data = await self._request(f"{API_V22}/films", params)
        results = []

        for item in data.get("items", []):
            try:
                result = KinopoiskSearchResult.model_validate(item)
                results.append(result)
            except Exception as e:
                logger.warning(
                    "kinopoisk_parse_error",
                    item_id=item.get("kinopoiskId"),
                    error=str(e),
                )
                continue

        logger.info(
            "kinopoisk_search",
            query=query,
            media_type=media_type.value,
            results_count=len(results),
            total=data.get("total", 0),
        )
        return results[:MAX_RESULTS]

    async def search_safe(
        self,
        query: str,
        page: int = 1,
        media_type: KinopoiskMediaType = KinopoiskMediaType.ALL,
    ) -> list[KinopoiskSearchResult]:
        """Search for films with graceful degradation.

        Same as search() but returns empty list on any error
        instead of raising exceptions.

        Args:
            query: Search query (Russian or English)
            page: Page number (1-based)
            media_type: Filter by media type

        Returns:
            List of search results, or empty list on error
        """
        try:
            return await self.search(query, page, media_type)
        except Exception as e:
            logger.warning(
                "kinopoisk_search_degraded",
                query=query,
                error=str(e),
                error_type=type(e).__name__,
            )
            return []

    async def search_by_keyword(
        self,
        keyword: str,
        page: int = 1,
    ) -> list[KinopoiskSearchResult]:
        """Search by keyword using v2.1 API.

        Alternative search endpoint that may provide different results.

        Args:
            keyword: Search keyword
            page: Page number (1-based)

        Returns:
            List of search results
        """
        params: dict[str, Any] = {
            "keyword": keyword,
            "page": page,
        }

        data = await self._request(f"{API_V21}/films/search-by-keyword", params)
        results = []

        for item in data.get("films", []):
            # Map v2.1 response to our model
            try:
                mapped = {
                    "kinopoiskId": item.get("filmId"),
                    "nameRu": item.get("nameRu"),
                    "nameEn": item.get("nameEn"),
                    "year": item.get("year"),
                    "posterUrl": item.get("posterUrl"),
                    "posterUrlPreview": item.get("posterUrlPreview"),
                    "ratingKinopoisk": item.get("rating"),
                    "countries": item.get("countries", []),
                    "genres": item.get("genres", []),
                    "type": item.get("type"),
                }
                result = KinopoiskSearchResult.model_validate(mapped)
                results.append(result)
            except Exception as e:
                logger.warning(
                    "kinopoisk_parse_error",
                    item_id=item.get("filmId"),
                    error=str(e),
                )
                continue

        logger.info(
            "kinopoisk_search_by_keyword",
            keyword=keyword,
            results_count=len(results),
        )
        return results[:MAX_RESULTS]

    # =========================================================================
    # Detail Methods
    # =========================================================================

    async def get_film(self, film_id: int) -> KinopoiskFilm:
        """Get detailed film information.

        Args:
            film_id: Kinopoisk film ID

        Returns:
            KinopoiskFilm object with full details including ratings

        Raises:
            KinopoiskNotFoundError: Film not found
            KinopoiskError: On other API errors
        """
        data = await self._request(f"{API_V22}/films/{film_id}")

        film = KinopoiskFilm.model_validate(data)

        logger.info(
            "kinopoisk_get_film",
            film_id=film_id,
            title=film.get_title(),
            rating=film.rating_kinopoisk,
        )
        return film

    async def get_film_safe(self, film_id: int) -> KinopoiskFilm | None:
        """Get film details with graceful degradation.

        Same as get_film() but returns None on any error
        instead of raising exceptions.

        Args:
            film_id: Kinopoisk film ID

        Returns:
            KinopoiskFilm object or None on error
        """
        try:
            return await self.get_film(film_id)
        except Exception as e:
            logger.warning(
                "kinopoisk_get_film_degraded",
                film_id=film_id,
                error=str(e),
                error_type=type(e).__name__,
            )
            return None

    # =========================================================================
    # Convenience Methods
    # =========================================================================

    async def get_rating(self, film_id: int) -> float | None:
        """Get Kinopoisk rating for a film.

        Convenience method that only returns the rating.

        Args:
            film_id: Kinopoisk film ID

        Returns:
            Kinopoisk rating (0-10) or None if unavailable
        """
        film = await self.get_film_safe(film_id)
        if film:
            return film.rating_kinopoisk
        return None

    async def get_description_ru(self, film_id: int) -> str | None:
        """Get Russian description for a film.

        Convenience method that only returns the description.

        Args:
            film_id: Kinopoisk film ID

        Returns:
            Russian description or None if unavailable
        """
        film = await self.get_film_safe(film_id)
        if film:
            return film.description or film.short_description
        return None

    async def find_film_by_title(
        self,
        title: str,
        year: int | None = None,
    ) -> KinopoiskFilm | None:
        """Find a film by title and optionally year.

        Searches for the title and returns the best match.
        Prefers exact year match if provided.

        Args:
            title: Film title (Russian or English)
            year: Optional release year for filtering

        Returns:
            Best matching KinopoiskFilm or None if not found
        """
        results = await self.search_safe(title)
        if not results:
            return None

        # If year provided, try to find exact match
        if year:
            for result in results:
                if result.year == year:
                    return await self.get_film_safe(result.kinopoisk_id)

        # Return first result's full details
        return await self.get_film_safe(results[0].kinopoisk_id)

    # =========================================================================
    # Cache Management
    # =========================================================================

    def clear_cache(self) -> None:
        """Clear all cached responses."""
        self._cache.clear()
        logger.info("kinopoisk_cache_cleared")

    def cleanup_cache(self) -> int:
        """Remove expired cache entries.

        Returns:
            Number of entries removed
        """
        removed = self._cache.cleanup_expired()
        if removed > 0:
            logger.debug("kinopoisk_cache_cleanup", removed=removed)
        return removed


# =============================================================================
# Module-level convenience functions
# =============================================================================


async def search_kinopoisk(
    query: str,
    page: int = 1,
) -> list[KinopoiskSearchResult]:
    """Search Kinopoisk for films (convenience function).

    Creates a client, performs search, and returns results.
    Uses graceful degradation.

    Args:
        query: Search query (Russian or English)
        page: Page number (1-based)

    Returns:
        List of search results, or empty list on error
    """
    async with KinopoiskClient() as client:
        return await client.search_safe(query, page)


async def get_kinopoisk_film(film_id: int) -> KinopoiskFilm | None:
    """Get film details from Kinopoisk (convenience function).

    Creates a client, fetches film, and returns it.
    Uses graceful degradation.

    Args:
        film_id: Kinopoisk film ID

    Returns:
        KinopoiskFilm or None on error
    """
    async with KinopoiskClient() as client:
        return await client.get_film_safe(film_id)


async def get_kinopoisk_rating(film_id: int) -> float | None:
    """Get Kinopoisk rating for a film (convenience function).

    Creates a client and fetches only the rating.

    Args:
        film_id: Kinopoisk film ID

    Returns:
        Rating (0-10) or None on error
    """
    async with KinopoiskClient() as client:
        return await client.get_rating(film_id)
