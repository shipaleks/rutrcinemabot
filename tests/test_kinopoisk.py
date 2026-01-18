"""Tests for Kinopoisk Unofficial API client.

Tests cover:
- Data models and their methods
- Cache behavior
- Client operations (search, get film)
- Error handling and graceful degradation
- Convenience functions
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.media.kinopoisk import (
    KinopoiskAuthError,
    KinopoiskClient,
    KinopoiskCountry,
    KinopoiskError,
    KinopoiskFilm,
    KinopoiskGenre,
    KinopoiskMediaType,
    KinopoiskNotFoundError,
    KinopoiskRateLimitError,
    KinopoiskSearchResult,
    KinopoiskUnavailableError,
    SimpleCache,
    get_kinopoisk_film,
    get_kinopoisk_rating,
    search_kinopoisk,
)

# =============================================================================
# Test Data
# =============================================================================

SAMPLE_FILM_DATA = {
    "kinopoiskId": 41519,
    "imdbId": "tt0120689",
    "nameRu": "Зелёная миля",
    "nameEn": "The Green Mile",
    "nameOriginal": "The Green Mile",
    "posterUrl": "https://kinopoiskapiunofficial.tech/images/posters/kp/41519.jpg",
    "posterUrlPreview": "https://kinopoiskapiunofficial.tech/images/posters/kp_small/41519.jpg",
    "coverUrl": "https://avatars.mds.yandex.net/get-ott/cover.jpg",
    "logoUrl": None,
    "ratingKinopoisk": 9.1,
    "ratingImdb": 8.6,
    "ratingKinopoiskVoteCount": 823456,
    "ratingImdbVoteCount": 1234567,
    "year": 1999,
    "filmLength": 189,
    "slogan": "Пол Эджкомб не верил в чудеса. Пока не столкнулся с одним из них.",
    "description": "Обвиняемый в страшном преступлении, Джон Коффи оказывается в блоке смертников тюрьмы.",
    "shortDescription": "Чудеса случаются.",
    "type": "FILM",
    "ratingMpaa": "r",
    "ratingAgeLimits": "age16",
    "startYear": None,
    "endYear": None,
    "serial": False,
    "completed": None,
    "countries": [{"country": "США"}],
    "genres": [{"genre": "драма"}, {"genre": "фэнтези"}],
    "webUrl": "https://www.kinopoisk.ru/film/41519/",
}

SAMPLE_SEARCH_RESULT = {
    "kinopoiskId": 41519,
    "imdbId": "tt0120689",
    "nameRu": "Зелёная миля",
    "nameEn": "The Green Mile",
    "nameOriginal": "The Green Mile",
    "posterUrl": "https://kinopoiskapiunofficial.tech/images/posters/kp/41519.jpg",
    "posterUrlPreview": "https://kinopoiskapiunofficial.tech/images/posters/kp_small/41519.jpg",
    "ratingKinopoisk": 9.1,
    "ratingImdb": 8.6,
    "year": 1999,
    "type": "FILM",
    "countries": [{"country": "США"}],
    "genres": [{"genre": "драма"}],
}

SAMPLE_TV_SERIES_DATA = {
    "kinopoiskId": 77044,
    "nameRu": "Во все тяжкие",
    "nameEn": "Breaking Bad",
    "nameOriginal": "Breaking Bad",
    "ratingKinopoisk": 9.5,
    "year": 2008,
    "type": "TV_SERIES",
    "serial": True,
    "startYear": 2008,
    "endYear": 2013,
    "countries": [{"country": "США"}],
    "genres": [{"genre": "триллер"}, {"genre": "драма"}],
}

SAMPLE_SEARCH_RESPONSE = {
    "total": 1,
    "totalPages": 1,
    "items": [SAMPLE_SEARCH_RESULT],
}


# =============================================================================
# Tests: Data Models
# =============================================================================


class TestKinopoiskCountry:
    """Tests for KinopoiskCountry model."""

    def test_country_creation(self):
        """Test country model creation."""
        country = KinopoiskCountry(country="США")
        assert country.country == "США"


class TestKinopoiskGenre:
    """Tests for KinopoiskGenre model."""

    def test_genre_creation(self):
        """Test genre model creation."""
        genre = KinopoiskGenre(genre="драма")
        assert genre.genre == "драма"


class TestKinopoiskSearchResult:
    """Tests for KinopoiskSearchResult model."""

    def test_search_result_creation(self):
        """Test search result model creation."""
        result = KinopoiskSearchResult.model_validate(SAMPLE_SEARCH_RESULT)
        assert result.kinopoisk_id == 41519
        assert result.name_ru == "Зелёная миля"
        assert result.name_en == "The Green Mile"
        assert result.rating_kinopoisk == 9.1
        assert result.year == 1999

    def test_search_result_get_title_russian(self):
        """Test get_title returns Russian title when available."""
        result = KinopoiskSearchResult.model_validate(SAMPLE_SEARCH_RESULT)
        assert result.get_title() == "Зелёная миля"

    def test_search_result_get_title_english_fallback(self):
        """Test get_title falls back to English when no Russian."""
        data = SAMPLE_SEARCH_RESULT.copy()
        data["nameRu"] = None
        result = KinopoiskSearchResult.model_validate(data)
        assert result.get_title() == "The Green Mile"

    def test_search_result_get_english_title(self):
        """Test get_english_title."""
        result = KinopoiskSearchResult.model_validate(SAMPLE_SEARCH_RESULT)
        assert result.get_english_title() == "The Green Mile"

    def test_search_result_alias_mapping(self):
        """Test that aliases map correctly."""
        result = KinopoiskSearchResult.model_validate(SAMPLE_SEARCH_RESULT)
        assert result.poster_url is not None
        assert result.poster_url_preview is not None


class TestKinopoiskFilm:
    """Tests for KinopoiskFilm model."""

    def test_film_creation(self):
        """Test film model creation with full data."""
        film = KinopoiskFilm.model_validate(SAMPLE_FILM_DATA)
        assert film.kinopoisk_id == 41519
        assert film.name_ru == "Зелёная миля"
        assert film.rating_kinopoisk == 9.1
        assert film.year == 1999
        assert film.film_length == 189
        assert film.description is not None

    def test_film_get_title(self):
        """Test get_title method."""
        film = KinopoiskFilm.model_validate(SAMPLE_FILM_DATA)
        assert film.get_title() == "Зелёная миля"

    def test_film_get_english_title(self):
        """Test get_english_title method."""
        film = KinopoiskFilm.model_validate(SAMPLE_FILM_DATA)
        assert film.get_english_title() == "The Green Mile"

    def test_film_get_genre_names(self):
        """Test get_genre_names method."""
        film = KinopoiskFilm.model_validate(SAMPLE_FILM_DATA)
        genres = film.get_genre_names()
        assert "драма" in genres
        assert "фэнтези" in genres

    def test_film_get_country_names(self):
        """Test get_country_names method."""
        film = KinopoiskFilm.model_validate(SAMPLE_FILM_DATA)
        countries = film.get_country_names()
        assert "США" in countries

    def test_film_is_tv_series_false(self):
        """Test is_tv_series returns False for films."""
        film = KinopoiskFilm.model_validate(SAMPLE_FILM_DATA)
        assert film.is_tv_series() is False

    def test_film_is_tv_series_true(self):
        """Test is_tv_series returns True for TV series."""
        film = KinopoiskFilm.model_validate(SAMPLE_TV_SERIES_DATA)
        assert film.is_tv_series() is True

    def test_film_is_tv_series_by_type(self):
        """Test is_tv_series based on type field."""
        data = SAMPLE_FILM_DATA.copy()
        data["type"] = "TV_SERIES"
        data["serial"] = None
        film = KinopoiskFilm.model_validate(data)
        assert film.is_tv_series() is True

    def test_film_minimal_data(self):
        """Test film creation with minimal data."""
        minimal = {"kinopoiskId": 123}
        film = KinopoiskFilm.model_validate(minimal)
        assert film.kinopoisk_id == 123
        assert film.get_title() == "Unknown"
        assert film.rating_kinopoisk is None


# =============================================================================
# Tests: Cache
# =============================================================================


class TestSimpleCache:
    """Tests for SimpleCache."""

    def test_cache_set_and_get(self):
        """Test basic set and get operations."""
        cache = SimpleCache(ttl=60)
        cache.set("key1", {"data": "value"})
        assert cache.get("key1") == {"data": "value"}

    def test_cache_miss(self):
        """Test cache miss returns None."""
        cache = SimpleCache(ttl=60)
        assert cache.get("nonexistent") is None

    def test_cache_expiration(self):
        """Test cache entries expire after TTL."""
        cache = SimpleCache(ttl=1)
        cache.set("key", "value")
        assert cache.get("key") == "value"

        # Simulate time passing
        cache._cache["key"] = ("value", time.time() - 2)
        assert cache.get("key") is None

    def test_cache_clear(self):
        """Test cache clear removes all entries."""
        cache = SimpleCache(ttl=60)
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.clear()
        assert cache.get("key1") is None
        assert cache.get("key2") is None

    def test_cache_cleanup_expired(self):
        """Test cleanup_expired removes old entries."""
        cache = SimpleCache(ttl=1)
        cache.set("old", "value")
        cache.set("new", "value")

        # Make one entry old
        cache._cache["old"] = ("value", time.time() - 2)

        removed = cache.cleanup_expired()
        assert removed == 1
        assert cache.get("old") is None
        assert cache.get("new") == "value"


# =============================================================================
# Tests: KinopoiskClient
# =============================================================================


class TestKinopoiskClientInit:
    """Tests for KinopoiskClient initialization."""

    @patch("src.media.kinopoisk.settings")
    def test_client_init_with_settings(self, mock_settings):
        """Test client initialization uses settings."""
        mock_settings.kinopoisk_api_token.get_secret_value.return_value = "test_token"
        mock_settings.cache_ttl = 3600

        client = KinopoiskClient()
        assert client._api_token == "test_token"

    def test_client_init_with_custom_token(self):
        """Test client initialization with custom token."""
        client = KinopoiskClient(api_token="custom_token")
        assert client._api_token == "custom_token"

    @pytest.mark.asyncio
    async def test_client_context_manager(self):
        """Test client as context manager."""
        client = KinopoiskClient(api_token="test")
        async with client:
            assert client._client is not None
        assert client._client is None

    def test_client_property_raises_outside_context(self):
        """Test client property raises when not in context."""
        client = KinopoiskClient(api_token="test")
        with pytest.raises(RuntimeError, match="must be used as async context manager"):
            _ = client.client


class TestKinopoiskClientSearch:
    """Tests for KinopoiskClient search methods."""

    @pytest.mark.asyncio
    async def test_search_success(self):
        """Test successful search."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_SEARCH_RESPONSE

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                results = await client.search("Зелёная миля")

                assert len(results) == 1
                assert results[0].kinopoisk_id == 41519
                assert results[0].name_ru == "Зелёная миля"

    @pytest.mark.asyncio
    async def test_search_with_media_type_filter(self):
        """Test search with media type filter."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_SEARCH_RESPONSE

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                await client.search("test", media_type=KinopoiskMediaType.FILM)

                # Verify type parameter was passed
                call_kwargs = mock_get.call_args[1]
                assert call_kwargs["params"]["type"] == "FILM"

    @pytest.mark.asyncio
    async def test_search_safe_returns_empty_on_error(self):
        """Test search_safe returns empty list on error."""
        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = Exception("API Error")

            async with KinopoiskClient(api_token="test") as client:
                results = await client.search_safe("test")
                assert results == []

    @pytest.mark.asyncio
    async def test_search_by_keyword(self):
        """Test search_by_keyword v2.1 endpoint."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "films": [
                {
                    "filmId": 41519,
                    "nameRu": "Зелёная миля",
                    "nameEn": "The Green Mile",
                    "year": "1999",
                    "rating": "9.1",
                    "countries": [{"country": "США"}],
                    "genres": [{"genre": "драма"}],
                }
            ]
        }

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                results = await client.search_by_keyword("Зелёная миля")

                assert len(results) == 1
                assert results[0].kinopoisk_id == 41519


class TestKinopoiskClientGetFilm:
    """Tests for KinopoiskClient get_film methods."""

    @pytest.mark.asyncio
    async def test_get_film_success(self):
        """Test successful get_film."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_FILM_DATA

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                film = await client.get_film(41519)

                assert film.kinopoisk_id == 41519
                assert film.name_ru == "Зелёная миля"
                assert film.rating_kinopoisk == 9.1
                assert film.description is not None

    @pytest.mark.asyncio
    async def test_get_film_not_found(self):
        """Test get_film raises on 404."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not found"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                with pytest.raises(KinopoiskNotFoundError):
                    await client.get_film(999999)

    @pytest.mark.asyncio
    async def test_get_film_safe_returns_none_on_error(self):
        """Test get_film_safe returns None on error."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.text = "Not found"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                film = await client.get_film_safe(999999)
                assert film is None

    @pytest.mark.asyncio
    async def test_get_rating(self):
        """Test get_rating convenience method."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_FILM_DATA

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                rating = await client.get_rating(41519)
                assert rating == 9.1

    @pytest.mark.asyncio
    async def test_get_description_ru(self):
        """Test get_description_ru convenience method."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_FILM_DATA

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                description = await client.get_description_ru(41519)
                assert description is not None
                assert "Джон Коффи" in description


class TestKinopoiskClientFindFilm:
    """Tests for find_film_by_title method."""

    @pytest.mark.asyncio
    async def test_find_film_by_title(self):
        """Test find_film_by_title returns full film details."""
        search_response = MagicMock()
        search_response.status_code = 200
        search_response.json.return_value = SAMPLE_SEARCH_RESPONSE

        film_response = MagicMock()
        film_response.status_code = 200
        film_response.json.return_value = SAMPLE_FILM_DATA

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [search_response, film_response]

            async with KinopoiskClient(api_token="test") as client:
                film = await client.find_film_by_title("Зелёная миля")

                assert film is not None
                assert film.kinopoisk_id == 41519
                assert film.description is not None

    @pytest.mark.asyncio
    async def test_find_film_by_title_with_year(self):
        """Test find_film_by_title with year filter."""
        search_response = MagicMock()
        search_response.status_code = 200
        search_response.json.return_value = {
            "total": 2,
            "items": [
                {**SAMPLE_SEARCH_RESULT, "year": 2020},
                {**SAMPLE_SEARCH_RESULT, "year": 1999},
            ],
        }

        film_response = MagicMock()
        film_response.status_code = 200
        film_response.json.return_value = SAMPLE_FILM_DATA

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = [search_response, film_response]

            async with KinopoiskClient(api_token="test") as client:
                film = await client.find_film_by_title("Зелёная миля", year=1999)

                assert film is not None

    @pytest.mark.asyncio
    async def test_find_film_by_title_not_found(self):
        """Test find_film_by_title returns None when not found."""
        search_response = MagicMock()
        search_response.status_code = 200
        search_response.json.return_value = {"total": 0, "items": []}

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = search_response

            async with KinopoiskClient(api_token="test") as client:
                film = await client.find_film_by_title("NonexistentFilm12345")
                assert film is None


class TestKinopoiskClientErrors:
    """Tests for error handling."""

    @pytest.mark.asyncio
    async def test_auth_error(self):
        """Test 401 raises KinopoiskAuthError."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="invalid") as client:
                with pytest.raises(KinopoiskAuthError):
                    await client.search("test")

    @pytest.mark.asyncio
    async def test_rate_limit_error(self):
        """Test 402 raises KinopoiskRateLimitError."""
        mock_response = MagicMock()
        mock_response.status_code = 402
        mock_response.text = "Payment required"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                with pytest.raises(KinopoiskRateLimitError) as exc_info:
                    await client.search("test")
                assert exc_info.value.retry_after == 60

    @pytest.mark.asyncio
    async def test_server_error(self):
        """Test 5xx raises KinopoiskUnavailableError."""
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_response.text = "Service unavailable"

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                with pytest.raises(KinopoiskUnavailableError):
                    await client.search("test")

    @pytest.mark.asyncio
    async def test_timeout_error(self):
        """Test timeout raises KinopoiskUnavailableError."""
        import httpx

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.side_effect = httpx.TimeoutException("Timeout")

            async with KinopoiskClient(api_token="test") as client:
                with pytest.raises(KinopoiskUnavailableError, match="timeout"):
                    await client.search("test")


class TestKinopoiskClientCache:
    """Tests for caching behavior."""

    @pytest.mark.asyncio
    async def test_cache_hit(self):
        """Test cache hit returns cached data."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_SEARCH_RESPONSE

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                # First call
                results1 = await client.search("test")
                # Second call (should be cached)
                results2 = await client.search("test")

                assert results1 == results2
                # Only one HTTP call should be made
                assert mock_get.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_clear(self):
        """Test cache clear works."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_SEARCH_RESPONSE

        with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_response

            async with KinopoiskClient(api_token="test") as client:
                await client.search("test")
                client.clear_cache()
                await client.search("test")

                # Two HTTP calls after cache clear
                assert mock_get.call_count == 2


# =============================================================================
# Tests: Convenience Functions
# =============================================================================


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    @pytest.mark.asyncio
    async def test_search_kinopoisk(self):
        """Test search_kinopoisk function."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_SEARCH_RESPONSE

        with (
            patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
            patch("src.media.kinopoisk.settings") as mock_settings,
        ):
            mock_get.return_value = mock_response
            mock_settings.kinopoisk_api_token.get_secret_value.return_value = "test"
            mock_settings.cache_ttl = 3600

            results = await search_kinopoisk("Зелёная миля")

            assert len(results) == 1
            assert results[0].name_ru == "Зелёная миля"

    @pytest.mark.asyncio
    async def test_get_kinopoisk_film(self):
        """Test get_kinopoisk_film function."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_FILM_DATA

        with (
            patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
            patch("src.media.kinopoisk.settings") as mock_settings,
        ):
            mock_get.return_value = mock_response
            mock_settings.kinopoisk_api_token.get_secret_value.return_value = "test"
            mock_settings.cache_ttl = 3600

            film = await get_kinopoisk_film(41519)

            assert film is not None
            assert film.kinopoisk_id == 41519

    @pytest.mark.asyncio
    async def test_get_kinopoisk_rating(self):
        """Test get_kinopoisk_rating function."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = SAMPLE_FILM_DATA

        with (
            patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
            patch("src.media.kinopoisk.settings") as mock_settings,
        ):
            mock_get.return_value = mock_response
            mock_settings.kinopoisk_api_token.get_secret_value.return_value = "test"
            mock_settings.cache_ttl = 3600

            rating = await get_kinopoisk_rating(41519)

            assert rating == 9.1

    @pytest.mark.asyncio
    async def test_convenience_functions_graceful_degradation(self):
        """Test convenience functions return None/empty on error."""
        with (
            patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
            patch("src.media.kinopoisk.settings") as mock_settings,
        ):
            mock_get.side_effect = Exception("API Error")
            mock_settings.kinopoisk_api_token.get_secret_value.return_value = "test"
            mock_settings.cache_ttl = 3600

            # Should return empty list, not raise
            results = await search_kinopoisk("test")
            assert results == []

            # Should return None, not raise
            film = await get_kinopoisk_film(123)
            assert film is None

            rating = await get_kinopoisk_rating(123)
            assert rating is None


# =============================================================================
# Tests: Enums
# =============================================================================


class TestKinopoiskEnums:
    """Tests for Kinopoisk enums."""

    def test_media_type_values(self):
        """Test KinopoiskMediaType enum values."""
        assert KinopoiskMediaType.FILM.value == "FILM"
        assert KinopoiskMediaType.TV_SHOW.value == "TV_SHOW"
        assert KinopoiskMediaType.TV_SERIES.value == "TV_SERIES"
        assert KinopoiskMediaType.MINI_SERIES.value == "MINI_SERIES"
        assert KinopoiskMediaType.ALL.value == "ALL"


# =============================================================================
# Tests: Exceptions
# =============================================================================


class TestKinopoiskExceptions:
    """Tests for Kinopoisk exceptions."""

    def test_base_error(self):
        """Test KinopoiskError base exception."""
        error = KinopoiskError("Test error")
        assert str(error) == "Test error"

    def test_not_found_error(self):
        """Test KinopoiskNotFoundError."""
        error = KinopoiskNotFoundError("Film not found")
        assert isinstance(error, KinopoiskError)

    def test_rate_limit_error(self):
        """Test KinopoiskRateLimitError with retry_after."""
        error = KinopoiskRateLimitError(retry_after=30)
        assert error.retry_after == 30
        assert "30 seconds" in str(error)

    def test_auth_error(self):
        """Test KinopoiskAuthError."""
        error = KinopoiskAuthError("Invalid token")
        assert isinstance(error, KinopoiskError)

    def test_unavailable_error(self):
        """Test KinopoiskUnavailableError."""
        error = KinopoiskUnavailableError("Service down")
        assert isinstance(error, KinopoiskError)
