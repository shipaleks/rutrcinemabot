"""TMDB (The Movie Database) API client.

Provides async functionality for searching and retrieving movie and TV show
metadata from TMDB. Includes caching for performance optimization.

API Documentation: https://developers.themoviedb.org/3
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

TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p"

# Request timeout in seconds
REQUEST_TIMEOUT = 15.0

# Default language for requests
DEFAULT_LANGUAGE = "ru-RU"

# Image sizes available from TMDB
POSTER_SIZES = ["w92", "w154", "w185", "w342", "w500", "w780", "original"]
BACKDROP_SIZES = ["w300", "w780", "w1280", "original"]
PROFILE_SIZES = ["w45", "w185", "h632", "original"]


# =============================================================================
# Enums
# =============================================================================


class MediaType(str, Enum):
    """Type of media content."""

    MOVIE = "movie"
    TV = "tv"
    PERSON = "person"


# =============================================================================
# Exceptions
# =============================================================================


class TMDBError(Exception):
    """Base exception for TMDB API errors."""

    pass


class TMDBNotFoundError(TMDBError):
    """Raised when a resource is not found on TMDB."""

    pass


class TMDBRateLimitError(TMDBError):
    """Raised when TMDB rate limit is exceeded."""

    def __init__(self, retry_after: int = 1):
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded. Retry after {retry_after} seconds.")


class TMDBAuthError(TMDBError):
    """Raised when TMDB API key is invalid."""

    pass


# =============================================================================
# Data Models
# =============================================================================


class Genre(BaseModel):
    """Movie or TV show genre."""

    id: int
    name: str


class ProductionCompany(BaseModel):
    """Production company information."""

    id: int
    name: str
    logo_path: str | None = None
    origin_country: str = ""


class Person(BaseModel):
    """Person (actor, director, etc.) information."""

    id: int
    name: str
    profile_path: str | None = None
    character: str | None = None  # For cast members
    job: str | None = None  # For crew members
    department: str | None = None
    known_for_department: str | None = None
    popularity: float = 0.0

    def get_profile_url(self, size: str = "w185") -> str | None:
        """Get full URL for profile image.

        Args:
            size: Image size (w45, w185, h632, original)

        Returns:
            Full URL or None if no profile image
        """
        if not self.profile_path:
            return None
        return f"{TMDB_IMAGE_BASE_URL}/{size}{self.profile_path}"


class Credits(BaseModel):
    """Movie or TV show credits (cast and crew)."""

    cast: list[Person] = Field(default_factory=list)
    crew: list[Person] = Field(default_factory=list)

    def get_directors(self) -> list[Person]:
        """Get all directors from crew."""
        return [p for p in self.crew if p.job == "Director"]

    def get_writers(self) -> list[Person]:
        """Get all writers from crew."""
        return [p for p in self.crew if p.department == "Writing" or p.job == "Screenplay"]

    def get_top_cast(self, limit: int = 10) -> list[Person]:
        """Get top billed cast members.

        Args:
            limit: Maximum number of cast members to return

        Returns:
            List of top cast members
        """
        return self.cast[:limit]


class Movie(BaseModel):
    """Movie information from TMDB."""

    id: int
    title: str
    original_title: str = ""
    overview: str = ""
    release_date: str = ""
    poster_path: str | None = None
    backdrop_path: str | None = None
    vote_average: float = 0.0
    vote_count: int = 0
    popularity: float = 0.0
    genres: list[Genre] = Field(default_factory=list)
    runtime: int | None = None
    status: str = ""
    tagline: str = ""
    budget: int = 0
    revenue: int = 0
    production_companies: list[ProductionCompany] = Field(default_factory=list)
    imdb_id: str | None = None

    def get_poster_url(self, size: str = "w500") -> str | None:
        """Get full URL for poster image.

        Args:
            size: Image size (w92, w154, w185, w342, w500, w780, original)

        Returns:
            Full URL or None if no poster
        """
        if not self.poster_path:
            return None
        return f"{TMDB_IMAGE_BASE_URL}/{size}{self.poster_path}"

    def get_backdrop_url(self, size: str = "w1280") -> str | None:
        """Get full URL for backdrop image.

        Args:
            size: Image size (w300, w780, w1280, original)

        Returns:
            Full URL or None if no backdrop
        """
        if not self.backdrop_path:
            return None
        return f"{TMDB_IMAGE_BASE_URL}/{size}{self.backdrop_path}"

    def get_year(self) -> int | None:
        """Extract year from release date.

        Returns:
            Year as integer or None if no release date
        """
        if self.release_date and len(self.release_date) >= 4:
            try:
                return int(self.release_date[:4])
            except ValueError:
                return None
        return None

    def get_genre_names(self) -> list[str]:
        """Get list of genre names.

        Returns:
            List of genre names
        """
        return [g.name for g in self.genres]


class NextEpisode(BaseModel):
    """Information about next episode to air."""

    air_date: str = ""
    episode_number: int = 0
    season_number: int = 0
    name: str = ""


class TVShow(BaseModel):
    """TV show information from TMDB."""

    id: int
    name: str
    original_name: str = ""
    overview: str = ""
    first_air_date: str = ""
    last_air_date: str = ""
    poster_path: str | None = None
    backdrop_path: str | None = None
    vote_average: float = 0.0
    vote_count: int = 0
    popularity: float = 0.0
    genres: list[Genre] = Field(default_factory=list)
    episode_run_time: list[int] = Field(default_factory=list)
    status: str = ""
    tagline: str = ""
    number_of_seasons: int = 0
    number_of_episodes: int = 0
    in_production: bool = False
    production_companies: list[ProductionCompany] = Field(default_factory=list)
    next_episode_to_air: NextEpisode | None = None
    last_episode_to_air: NextEpisode | None = None

    def get_poster_url(self, size: str = "w500") -> str | None:
        """Get full URL for poster image."""
        if not self.poster_path:
            return None
        return f"{TMDB_IMAGE_BASE_URL}/{size}{self.poster_path}"

    def get_backdrop_url(self, size: str = "w1280") -> str | None:
        """Get full URL for backdrop image."""
        if not self.backdrop_path:
            return None
        return f"{TMDB_IMAGE_BASE_URL}/{size}{self.backdrop_path}"

    def get_year(self) -> int | None:
        """Extract year from first air date."""
        if self.first_air_date and len(self.first_air_date) >= 4:
            try:
                return int(self.first_air_date[:4])
            except ValueError:
                return None
        return None

    def get_genre_names(self) -> list[str]:
        """Get list of genre names."""
        return [g.name for g in self.genres]


class SearchResult(BaseModel):
    """Search result that can be either a movie or TV show."""

    id: int
    media_type: MediaType
    title: str  # Will be 'title' for movies, 'name' for TV
    original_title: str = ""
    overview: str = ""
    release_date: str = ""  # release_date for movies, first_air_date for TV
    poster_path: str | None = None
    vote_average: float = 0.0
    popularity: float = 0.0

    def get_poster_url(self, size: str = "w342") -> str | None:
        """Get full URL for poster image."""
        if not self.poster_path:
            return None
        return f"{TMDB_IMAGE_BASE_URL}/{size}{self.poster_path}"

    def get_year(self) -> int | None:
        """Extract year from release date."""
        if self.release_date and len(self.release_date) >= 4:
            try:
                return int(self.release_date[:4])
            except ValueError:
                return None
        return None


# =============================================================================
# Cache Implementation
# =============================================================================


class SimpleCache:
    """Simple in-memory cache with TTL support.

    Thread-safe for single-threaded async use (no locking needed).
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
# TMDB Client
# =============================================================================


class TMDBClient:
    """Async client for The Movie Database API.

    Provides methods for searching movies and TV shows, retrieving details,
    credits, and recommendations. Includes automatic caching.

    Example:
        async with TMDBClient() as client:
            results = await client.search_movie("Inception")
            movie = await client.get_movie(results[0].id)
            credits = await client.get_credits(movie.id, MediaType.MOVIE)
    """

    def __init__(
        self,
        api_key: str | None = None,
        language: str = DEFAULT_LANGUAGE,
        cache_ttl: int | None = None,
    ):
        """Initialize TMDB client.

        Args:
            api_key: TMDB API key. Uses settings.tmdb_api_key if None.
            language: Language for results (default: ru-RU)
            cache_ttl: Cache TTL in seconds. Uses settings.cache_ttl if None.
        """
        self._api_key = api_key or settings.tmdb_api_key.get_secret_value()
        self._language = language
        self._client: httpx.AsyncClient | None = None
        self._cache = SimpleCache(ttl=cache_ttl)

    async def __aenter__(self) -> "TMDBClient":
        """Enter async context manager."""
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
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
            raise RuntimeError("TMDBClient must be used as async context manager")
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
        return f"{endpoint}?{param_str}"

    async def _request(
        self,
        endpoint: str,
        params: dict[str, Any] | None = None,
        use_cache: bool = True,
    ) -> dict[str, Any]:
        """Make authenticated request to TMDB API.

        Args:
            endpoint: API endpoint (without base URL)
            params: Additional query parameters
            use_cache: Whether to use cache for this request

        Returns:
            JSON response as dictionary

        Raises:
            TMDBNotFoundError: Resource not found (404)
            TMDBRateLimitError: Rate limit exceeded (429)
            TMDBAuthError: Invalid API key (401)
            TMDBError: Other API errors
        """
        # Build full params with auth and language
        full_params = {
            "api_key": self._api_key,
            "language": self._language,
        }
        if params:
            full_params.update(params)

        # Check cache
        cache_key = self._get_cache_key(endpoint, full_params)
        if use_cache:
            cached = self._cache.get(cache_key)
            if cached is not None:
                logger.debug("cache_hit", endpoint=endpoint)
                return cached

        # Make request
        url = f"{TMDB_BASE_URL}{endpoint}"
        logger.debug("tmdb_request", endpoint=endpoint, params=params)

        try:
            response = await self.client.get(url, params=full_params)
        except httpx.TimeoutException as e:
            logger.warning("tmdb_timeout", endpoint=endpoint)
            raise TMDBError(f"Request timeout: {e}") from e
        except httpx.HTTPError as e:
            logger.warning("tmdb_http_error", endpoint=endpoint, error=str(e))
            raise TMDBError(f"HTTP error: {e}") from e

        # Handle response status
        if response.status_code == 200:
            data = response.json()
            if use_cache:
                self._cache.set(cache_key, data)
            return data

        if response.status_code == 401:
            raise TMDBAuthError("Invalid TMDB API key")
        if response.status_code == 404:
            raise TMDBNotFoundError(f"Resource not found: {endpoint}")
        if response.status_code == 429:
            retry_after = int(response.headers.get("Retry-After", 1))
            raise TMDBRateLimitError(retry_after)

        error_msg = response.text[:200] if response.text else "Unknown error"
        raise TMDBError(f"TMDB API error {response.status_code}: {error_msg}")

    # =========================================================================
    # Search Methods
    # =========================================================================

    async def search_movie(
        self,
        query: str,
        year: int | None = None,
        page: int = 1,
    ) -> list[SearchResult]:
        """Search for movies by title.

        Args:
            query: Movie title to search for
            year: Optional year filter
            page: Page number (1-based)

        Returns:
            List of search results
        """
        params: dict[str, Any] = {"query": query, "page": page}
        if year:
            params["year"] = year

        data = await self._request("/search/movie", params)
        results = []

        for item in data.get("results", []):
            results.append(
                SearchResult(
                    id=item["id"],
                    media_type=MediaType.MOVIE,
                    title=item.get("title", ""),
                    original_title=item.get("original_title", ""),
                    overview=item.get("overview", ""),
                    release_date=item.get("release_date", ""),
                    poster_path=item.get("poster_path"),
                    vote_average=item.get("vote_average", 0.0),
                    popularity=item.get("popularity", 0.0),
                )
            )

        logger.info(
            "tmdb_search_movie",
            query=query,
            year=year,
            results_count=len(results),
        )
        return results

    async def search_tv(
        self,
        query: str,
        year: int | None = None,
        page: int = 1,
    ) -> list[SearchResult]:
        """Search for TV shows by title.

        Args:
            query: TV show title to search for
            year: Optional first air year filter
            page: Page number (1-based)

        Returns:
            List of search results
        """
        params: dict[str, Any] = {"query": query, "page": page}
        if year:
            params["first_air_date_year"] = year

        data = await self._request("/search/tv", params)
        results = []

        for item in data.get("results", []):
            results.append(
                SearchResult(
                    id=item["id"],
                    media_type=MediaType.TV,
                    title=item.get("name", ""),
                    original_title=item.get("original_name", ""),
                    overview=item.get("overview", ""),
                    release_date=item.get("first_air_date", ""),
                    poster_path=item.get("poster_path"),
                    vote_average=item.get("vote_average", 0.0),
                    popularity=item.get("popularity", 0.0),
                )
            )

        logger.info(
            "tmdb_search_tv",
            query=query,
            year=year,
            results_count=len(results),
        )
        return results

    async def search_multi(
        self,
        query: str,
        page: int = 1,
    ) -> list[SearchResult]:
        """Search for movies and TV shows together.

        Args:
            query: Title to search for
            page: Page number (1-based)

        Returns:
            List of search results (movies and TV shows)
        """
        params: dict[str, Any] = {"query": query, "page": page}

        data = await self._request("/search/multi", params)
        results = []

        for item in data.get("results", []):
            media_type_str = item.get("media_type", "")
            if media_type_str not in ("movie", "tv"):
                continue  # Skip persons and other types

            media_type = MediaType(media_type_str)

            # Handle different field names for movie vs TV
            if media_type == MediaType.MOVIE:
                title = item.get("title", "")
                original_title = item.get("original_title", "")
                release_date = item.get("release_date", "")
            else:
                title = item.get("name", "")
                original_title = item.get("original_name", "")
                release_date = item.get("first_air_date", "")

            results.append(
                SearchResult(
                    id=item["id"],
                    media_type=media_type,
                    title=title,
                    original_title=original_title,
                    overview=item.get("overview", ""),
                    release_date=release_date,
                    poster_path=item.get("poster_path"),
                    vote_average=item.get("vote_average", 0.0),
                    popularity=item.get("popularity", 0.0),
                )
            )

        logger.info(
            "tmdb_search_multi",
            query=query,
            results_count=len(results),
        )
        return results

    # =========================================================================
    # Detail Methods
    # =========================================================================

    async def get_movie(self, movie_id: int) -> Movie:
        """Get detailed movie information.

        Args:
            movie_id: TMDB movie ID

        Returns:
            Movie object with full details

        Raises:
            TMDBNotFoundError: Movie not found
        """
        data = await self._request(f"/movie/{movie_id}")

        movie = Movie(
            id=data["id"],
            title=data.get("title", ""),
            original_title=data.get("original_title", ""),
            overview=data.get("overview", ""),
            release_date=data.get("release_date", ""),
            poster_path=data.get("poster_path"),
            backdrop_path=data.get("backdrop_path"),
            vote_average=data.get("vote_average", 0.0),
            vote_count=data.get("vote_count", 0),
            popularity=data.get("popularity", 0.0),
            genres=[Genre(**g) for g in data.get("genres", [])],
            runtime=data.get("runtime"),
            status=data.get("status", ""),
            tagline=data.get("tagline", ""),
            budget=data.get("budget", 0),
            revenue=data.get("revenue", 0),
            production_companies=[
                ProductionCompany(**c) for c in data.get("production_companies", [])
            ],
            imdb_id=data.get("imdb_id"),
        )

        logger.info("tmdb_get_movie", movie_id=movie_id, title=movie.title)
        return movie

    async def get_episode_air_date(
        self,
        tv_id: int,
        season_number: int,
        episode_number: int,
    ) -> str | None:
        """Get air date for a specific episode.

        Args:
            tv_id: TMDB TV show ID
            season_number: Season number (1-based)
            episode_number: Episode number (1-based)

        Returns:
            Air date string (YYYY-MM-DD) or None if not available

        Raises:
            TMDBNotFoundError: Episode not found
        """
        try:
            data = await self._request(
                f"/tv/{tv_id}/season/{season_number}/episode/{episode_number}"
            )
            air_date = data.get("air_date")
            logger.info(
                "tmdb_get_episode_air_date",
                tv_id=tv_id,
                season=season_number,
                episode=episode_number,
                air_date=air_date,
            )
            return air_date
        except TMDBNotFoundError:
            logger.debug(
                "tmdb_episode_not_found",
                tv_id=tv_id,
                season=season_number,
                episode=episode_number,
            )
            return None

    async def get_tv_show(self, tv_id: int) -> TVShow:
        """Get detailed TV show information.

        Args:
            tv_id: TMDB TV show ID

        Returns:
            TVShow object with full details

        Raises:
            TMDBNotFoundError: TV show not found
        """
        data = await self._request(f"/tv/{tv_id}")

        # Parse next/last episode info
        next_ep_data = data.get("next_episode_to_air")
        next_episode = None
        if next_ep_data:
            next_episode = NextEpisode(
                air_date=next_ep_data.get("air_date", ""),
                episode_number=next_ep_data.get("episode_number", 0),
                season_number=next_ep_data.get("season_number", 0),
                name=next_ep_data.get("name", ""),
            )

        last_ep_data = data.get("last_episode_to_air")
        last_episode = None
        if last_ep_data:
            last_episode = NextEpisode(
                air_date=last_ep_data.get("air_date", ""),
                episode_number=last_ep_data.get("episode_number", 0),
                season_number=last_ep_data.get("season_number", 0),
                name=last_ep_data.get("name", ""),
            )

        tv_show = TVShow(
            id=data["id"],
            name=data.get("name", ""),
            original_name=data.get("original_name", ""),
            overview=data.get("overview", ""),
            first_air_date=data.get("first_air_date", ""),
            last_air_date=data.get("last_air_date", ""),
            poster_path=data.get("poster_path"),
            backdrop_path=data.get("backdrop_path"),
            vote_average=data.get("vote_average", 0.0),
            vote_count=data.get("vote_count", 0),
            popularity=data.get("popularity", 0.0),
            genres=[Genre(**g) for g in data.get("genres", [])],
            episode_run_time=data.get("episode_run_time", []),
            status=data.get("status", ""),
            tagline=data.get("tagline", ""),
            number_of_seasons=data.get("number_of_seasons", 0),
            number_of_episodes=data.get("number_of_episodes", 0),
            in_production=data.get("in_production", False),
            production_companies=[
                ProductionCompany(**c) for c in data.get("production_companies", [])
            ],
            next_episode_to_air=next_episode,
            last_episode_to_air=last_episode,
        )

        logger.info("tmdb_get_tv_show", tv_id=tv_id, name=tv_show.name)
        return tv_show

    # =========================================================================
    # Credits Methods
    # =========================================================================

    async def get_movie_credits(self, movie_id: int) -> Credits:
        """Get movie credits (cast and crew).

        Args:
            movie_id: TMDB movie ID

        Returns:
            Credits object with cast and crew lists

        Raises:
            TMDBNotFoundError: Movie not found
        """
        data = await self._request(f"/movie/{movie_id}/credits")

        credits = Credits(
            cast=[
                Person(
                    id=p["id"],
                    name=p.get("name", ""),
                    profile_path=p.get("profile_path"),
                    character=p.get("character"),
                    known_for_department=p.get("known_for_department"),
                    popularity=p.get("popularity", 0.0),
                )
                for p in data.get("cast", [])
            ],
            crew=[
                Person(
                    id=p["id"],
                    name=p.get("name", ""),
                    profile_path=p.get("profile_path"),
                    job=p.get("job"),
                    department=p.get("department"),
                    known_for_department=p.get("known_for_department"),
                    popularity=p.get("popularity", 0.0),
                )
                for p in data.get("crew", [])
            ],
        )

        logger.info(
            "tmdb_get_movie_credits",
            movie_id=movie_id,
            cast_count=len(credits.cast),
            crew_count=len(credits.crew),
        )
        return credits

    async def get_tv_credits(self, tv_id: int) -> Credits:
        """Get TV show credits (cast and crew).

        Args:
            tv_id: TMDB TV show ID

        Returns:
            Credits object with cast and crew lists

        Raises:
            TMDBNotFoundError: TV show not found
        """
        data = await self._request(f"/tv/{tv_id}/credits")

        credits = Credits(
            cast=[
                Person(
                    id=p["id"],
                    name=p.get("name", ""),
                    profile_path=p.get("profile_path"),
                    character=p.get("character"),
                    known_for_department=p.get("known_for_department"),
                    popularity=p.get("popularity", 0.0),
                )
                for p in data.get("cast", [])
            ],
            crew=[
                Person(
                    id=p["id"],
                    name=p.get("name", ""),
                    profile_path=p.get("profile_path"),
                    job=p.get("job"),
                    department=p.get("department"),
                    known_for_department=p.get("known_for_department"),
                    popularity=p.get("popularity", 0.0),
                )
                for p in data.get("crew", [])
            ],
        )

        logger.info(
            "tmdb_get_tv_credits",
            tv_id=tv_id,
            cast_count=len(credits.cast),
            crew_count=len(credits.crew),
        )
        return credits

    async def get_credits(self, media_id: int, media_type: MediaType) -> Credits:
        """Get credits for either movie or TV show.

        Args:
            media_id: TMDB media ID
            media_type: Type of media (movie or tv)

        Returns:
            Credits object with cast and crew lists

        Raises:
            TMDBNotFoundError: Media not found
            ValueError: Invalid media type
        """
        if media_type == MediaType.MOVIE:
            return await self.get_movie_credits(media_id)
        if media_type == MediaType.TV:
            return await self.get_tv_credits(media_id)
        raise ValueError(f"Invalid media type for credits: {media_type}")

    # =========================================================================
    # Recommendations Methods
    # =========================================================================

    async def get_movie_recommendations(
        self,
        movie_id: int,
        page: int = 1,
    ) -> list[SearchResult]:
        """Get movie recommendations based on a movie.

        Args:
            movie_id: TMDB movie ID
            page: Page number (1-based)

        Returns:
            List of recommended movies

        Raises:
            TMDBNotFoundError: Movie not found
        """
        data = await self._request(
            f"/movie/{movie_id}/recommendations",
            params={"page": page},
        )

        results = []
        for item in data.get("results", []):
            results.append(
                SearchResult(
                    id=item["id"],
                    media_type=MediaType.MOVIE,
                    title=item.get("title", ""),
                    original_title=item.get("original_title", ""),
                    overview=item.get("overview", ""),
                    release_date=item.get("release_date", ""),
                    poster_path=item.get("poster_path"),
                    vote_average=item.get("vote_average", 0.0),
                    popularity=item.get("popularity", 0.0),
                )
            )

        logger.info(
            "tmdb_get_movie_recommendations",
            movie_id=movie_id,
            results_count=len(results),
        )
        return results

    async def get_tv_recommendations(
        self,
        tv_id: int,
        page: int = 1,
    ) -> list[SearchResult]:
        """Get TV show recommendations based on a show.

        Args:
            tv_id: TMDB TV show ID
            page: Page number (1-based)

        Returns:
            List of recommended TV shows

        Raises:
            TMDBNotFoundError: TV show not found
        """
        data = await self._request(
            f"/tv/{tv_id}/recommendations",
            params={"page": page},
        )

        results = []
        for item in data.get("results", []):
            results.append(
                SearchResult(
                    id=item["id"],
                    media_type=MediaType.TV,
                    title=item.get("name", ""),
                    original_title=item.get("original_name", ""),
                    overview=item.get("overview", ""),
                    release_date=item.get("first_air_date", ""),
                    poster_path=item.get("poster_path"),
                    vote_average=item.get("vote_average", 0.0),
                    popularity=item.get("popularity", 0.0),
                )
            )

        logger.info(
            "tmdb_get_tv_recommendations",
            tv_id=tv_id,
            results_count=len(results),
        )
        return results

    async def get_recommendations(
        self,
        media_id: int,
        media_type: MediaType,
        page: int = 1,
    ) -> list[SearchResult]:
        """Get recommendations for either movie or TV show.

        Args:
            media_id: TMDB media ID
            media_type: Type of media (movie or tv)
            page: Page number (1-based)

        Returns:
            List of recommended media

        Raises:
            TMDBNotFoundError: Media not found
            ValueError: Invalid media type
        """
        if media_type == MediaType.MOVIE:
            return await self.get_movie_recommendations(media_id, page)
        if media_type == MediaType.TV:
            return await self.get_tv_recommendations(media_id, page)
        raise ValueError(f"Invalid media type for recommendations: {media_type}")

    # =========================================================================
    # Person/Director Methods
    # =========================================================================

    async def search_person(
        self,
        query: str,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Search for a person by name.

        Args:
            query: Person name to search for
            page: Page number (1-based)

        Returns:
            List of person results with id, name, known_for_department, etc.
        """
        data = await self._request(
            "/search/person",
            params={
                "query": query,
                "page": page,
            },
        )

        results = []
        for item in data.get("results", []):
            results.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "known_for_department": item.get("known_for_department"),
                    "profile_path": item.get("profile_path"),
                    "popularity": item.get("popularity", 0),
                }
            )

        logger.info("tmdb_search_person", query=query, results_count=len(results))
        return results

    async def get_person(self, person_id: int) -> dict[str, Any]:
        """Get detailed person information.

        Args:
            person_id: TMDB person ID

        Returns:
            Dict with person details including biography, birthday, profile_path, etc.

        Raises:
            TMDBNotFoundError: Person not found
        """
        data = await self._request(f"/person/{person_id}")

        person = {
            "id": data["id"],
            "name": data.get("name", ""),
            "biography": data.get("biography", ""),
            "birthday": data.get("birthday"),
            "deathday": data.get("deathday"),
            "place_of_birth": data.get("place_of_birth"),
            "profile_path": data.get("profile_path"),
            "known_for_department": data.get("known_for_department"),
            "popularity": data.get("popularity", 0),
        }

        logger.info("tmdb_get_person", person_id=person_id, name=person["name"])
        return person

    async def get_person_movie_credits(
        self,
        person_id: int,
    ) -> dict[str, list[dict[str, Any]]]:
        """Get movie credits for a person (both cast and crew).

        Args:
            person_id: TMDB person ID

        Returns:
            Dict with 'cast' and 'crew' lists containing movie information
        """
        data = await self._request(f"/person/{person_id}/movie_credits")

        cast = []
        for item in data.get("cast", []):
            cast.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "release_date": item.get("release_date"),
                    "character": item.get("character"),
                    "popularity": item.get("popularity", 0),
                    "vote_average": item.get("vote_average", 0),
                }
            )

        crew = []
        for item in data.get("crew", []):
            crew.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "release_date": item.get("release_date"),
                    "job": item.get("job"),
                    "department": item.get("department"),
                    "popularity": item.get("popularity", 0),
                    "vote_average": item.get("vote_average", 0),
                }
            )

        logger.info(
            "tmdb_get_person_movie_credits",
            person_id=person_id,
            cast_count=len(cast),
            crew_count=len(crew),
        )
        return {"cast": cast, "crew": crew}

    async def get_person_upcoming_movies(
        self,
        person_id: int,
        role: str = "Director",
    ) -> list[dict[str, Any]]:
        """Get upcoming movies for a person in a specific role.

        Filters movies to only include those with release dates in the future.

        Args:
            person_id: TMDB person ID
            role: Role to filter by (e.g., "Director", "Writer")

        Returns:
            List of upcoming movies with id, title, release_date, etc.
        """
        from datetime import date

        credits = await self.get_person_movie_credits(person_id)
        today = date.today().isoformat()

        upcoming = []
        for movie in credits.get("crew", []):
            # Filter by role
            if movie.get("job") != role:
                continue

            # Filter for future releases
            release_date = movie.get("release_date")
            if not release_date or release_date <= today:
                continue

            upcoming.append(
                {
                    "id": movie.get("id"),
                    "title": movie.get("title"),
                    "release_date": release_date,
                    "job": movie.get("job"),
                    "popularity": movie.get("popularity", 0),
                }
            )

        # Sort by release date
        upcoming.sort(key=lambda x: x.get("release_date", "9999"))

        logger.info(
            "tmdb_get_person_upcoming_movies",
            person_id=person_id,
            role=role,
            upcoming_count=len(upcoming),
        )
        return upcoming

    # =========================================================================
    # Cache Management
    # =========================================================================

    def clear_cache(self) -> None:
        """Clear all cached responses."""
        self._cache.clear()
        logger.info("tmdb_cache_cleared")

    def cleanup_cache(self) -> int:
        """Remove expired cache entries.

        Returns:
            Number of entries removed
        """
        removed = self._cache.cleanup_expired()
        if removed > 0:
            logger.debug("tmdb_cache_cleanup", removed=removed)
        return removed
