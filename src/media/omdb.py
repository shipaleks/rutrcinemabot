"""OMDB API client for fetching IMDB and Rotten Tomatoes ratings.

OMDB (Open Movie Database) is a free API that provides movie metadata
including IMDB ratings, Rotten Tomatoes scores, and Metascores.

No API key required for basic usage (1000 requests/day).
For higher limits, get a free key at http://www.omdbapi.com/apikey.aspx
"""

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field

logger = structlog.get_logger(__name__)

OMDB_API_BASE = "http://www.omdbapi.com/"


class OMDBRating(BaseModel):
    """OMDB rating from a specific source."""

    source: str  # "Internet Movie Database", "Rotten Tomatoes", "Metacritic"
    value: str  # "7.8/10", "85%", "72/100"


class OMDBResult(BaseModel):
    """OMDB API response with ratings."""

    title: str = Field(alias="Title")
    year: str = Field(alias="Year")
    imdb_rating: str | None = Field(None, alias="imdbRating")
    imdb_votes: str | None = Field(None, alias="imdbVotes")
    imdb_id: str | None = Field(None, alias="imdbID")
    ratings: list[OMDBRating] = Field(default_factory=list, alias="Ratings")
    metascore: str | None = Field(None, alias="Metascore")
    plot: str | None = Field(None, alias="Plot")
    response: str = Field(alias="Response")  # "True" or "False"
    error: str | None = Field(None, alias="Error")

    model_config = ConfigDict(populate_by_name=True)


class OMDBError(Exception):
    """OMDB API error."""

    pass


class OMDBClient:
    """Async client for OMDB API.

    Usage:
        client = OMDBClient(api_key="optional")
        result = await client.search_by_title("Dune", year=2021)
        result = await client.search_by_imdb_id("tt1160419")
    """

    def __init__(self, api_key: str | None = None):
        """Initialize OMDB client.

        Args:
            api_key: Optional API key for higher rate limits
                    (default: public access with 1000/day limit)
        """
        self.api_key = api_key or "demo"  # Demo key for testing
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "OMDBClient":
        """Async context manager entry."""
        self._client = httpx.AsyncClient(timeout=10.0)
        return self

    async def __aexit__(self, _exc_type, _exc_val, _exc_tb) -> None:
        """Async context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get HTTP client, raise if not in context manager."""
        if not self._client:
            raise OMDBError("OMDBClient must be used as async context manager")
        return self._client

    async def search_by_title(
        self,
        title: str,
        year: int | None = None,
    ) -> OMDBResult:
        """Search movie by title and optional year.

        Args:
            title: Movie title
            year: Release year for disambiguation

        Returns:
            OMDBResult with ratings

        Raises:
            OMDBError: If movie not found or API error
        """
        params = {
            "apikey": self.api_key,
            "t": title,
            "plot": "short",
        }
        if year:
            params["y"] = str(year)

        logger.info("omdb_search_by_title", title=title, year=year)

        try:
            response = await self.client.get(OMDB_API_BASE, params=params)
            response.raise_for_status()
            data = response.json()

            result = OMDBResult(**data)

            if result.response == "False":
                logger.warning("omdb_movie_not_found", title=title, error=result.error)
                raise OMDBError(result.error or "Movie not found")

            logger.info(
                "omdb_search_success",
                title=result.title,
                imdb_rating=result.imdb_rating,
            )
            return result

        except httpx.HTTPStatusError as e:
            logger.error("omdb_http_error", status=e.response.status_code, error=str(e))
            raise OMDBError(f"HTTP error: {e}") from e
        except httpx.RequestError as e:
            logger.error("omdb_request_error", error=str(e))
            raise OMDBError(f"Request failed: {e}") from e
        except Exception as e:
            logger.error("omdb_unexpected_error", error=str(e))
            raise OMDBError(f"Unexpected error: {e}") from e

    async def search_by_imdb_id(self, imdb_id: str) -> OMDBResult:
        """Search movie by IMDB ID.

        Args:
            imdb_id: IMDB ID (e.g., "tt1160419")

        Returns:
            OMDBResult with ratings

        Raises:
            OMDBError: If movie not found or API error
        """
        params = {
            "apikey": self.api_key,
            "i": imdb_id,
            "plot": "short",
        }

        logger.info("omdb_search_by_imdb_id", imdb_id=imdb_id)

        try:
            response = await self.client.get(OMDB_API_BASE, params=params)
            response.raise_for_status()
            data = response.json()

            result = OMDBResult(**data)

            if result.response == "False":
                logger.warning("omdb_movie_not_found", imdb_id=imdb_id, error=result.error)
                raise OMDBError(result.error or "Movie not found")

            logger.info(
                "omdb_search_success",
                title=result.title,
                imdb_rating=result.imdb_rating,
            )
            return result

        except httpx.HTTPStatusError as e:
            logger.error("omdb_http_error", status=e.response.status_code, error=str(e))
            raise OMDBError(f"HTTP error: {e}") from e
        except httpx.RequestError as e:
            logger.error("omdb_request_error", error=str(e))
            raise OMDBError(f"Request failed: {e}") from e
        except Exception as e:
            logger.error("omdb_unexpected_error", error=str(e))
            raise OMDBError(f"Unexpected error: {e}") from e

    def get_rotten_tomatoes_rating(self, result: OMDBResult) -> str | None:
        """Extract Rotten Tomatoes rating from result."""
        for rating in result.ratings:
            if "Rotten Tomatoes" in rating.source:
                return rating.value
        return None

    def get_metacritic_rating(self, result: OMDBResult) -> str | None:
        """Extract Metacritic rating from result."""
        for rating in result.ratings:
            if "Metacritic" in rating.source:
                return rating.value
        return result.metascore


# Convenience functions
async def get_ratings_by_title(title: str, year: int | None = None) -> OMDBResult:
    """Get movie ratings by title.

    Args:
        title: Movie title
        year: Optional release year

    Returns:
        OMDBResult with all available ratings
    """
    async with OMDBClient() as client:
        return await client.search_by_title(title, year)


async def get_ratings_by_imdb_id(imdb_id: str) -> OMDBResult:
    """Get movie ratings by IMDB ID.

    Args:
        imdb_id: IMDB ID (e.g., "tt1160419")

    Returns:
        OMDBResult with all available ratings
    """
    async with OMDBClient() as client:
        return await client.search_by_imdb_id(imdb_id)
