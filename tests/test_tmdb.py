"""Tests for TMDB (The Movie Database) API client."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.media.tmdb import (
    Credits,
    Genre,
    MediaType,
    Movie,
    Person,
    ProductionCompany,
    SearchResult,
    SimpleCache,
    TMDBAuthError,
    TMDBClient,
    TMDBError,
    TMDBNotFoundError,
    TMDBRateLimitError,
    TVShow,
)

# =============================================================================
# Sample API Responses
# =============================================================================

SAMPLE_MOVIE_SEARCH_RESPONSE = {
    "page": 1,
    "results": [
        {
            "id": 27205,
            "title": "Inception",
            "original_title": "Inception",
            "overview": "A skilled thief is given a chance at redemption.",
            "release_date": "2010-07-16",
            "poster_path": "/9gk7adHYeDvHkCSEqAvQNLV5Ber.jpg",
            "vote_average": 8.4,
            "popularity": 100.5,
        },
        {
            "id": 27206,
            "title": "Inception: The Cobol Job",
            "original_title": "Inception: The Cobol Job",
            "overview": "A prequel comic.",
            "release_date": "2010-12-07",
            "poster_path": None,
            "vote_average": 7.2,
            "popularity": 10.0,
        },
    ],
    "total_pages": 1,
    "total_results": 2,
}

SAMPLE_TV_SEARCH_RESPONSE = {
    "page": 1,
    "results": [
        {
            "id": 1396,
            "name": "Breaking Bad",
            "original_name": "Breaking Bad",
            "overview": "A high school chemistry teacher diagnosed with lung cancer.",
            "first_air_date": "2008-01-20",
            "poster_path": "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
            "vote_average": 9.5,
            "popularity": 200.0,
        },
    ],
    "total_pages": 1,
    "total_results": 1,
}

SAMPLE_MULTI_SEARCH_RESPONSE = {
    "page": 1,
    "results": [
        {
            "id": 27205,
            "media_type": "movie",
            "title": "Inception",
            "original_title": "Inception",
            "overview": "A thief...",
            "release_date": "2010-07-16",
            "poster_path": "/9gk7adHYeDvHkCSEqAvQNLV5Ber.jpg",
            "vote_average": 8.4,
            "popularity": 100.0,
        },
        {
            "id": 1396,
            "media_type": "tv",
            "name": "Breaking Bad",
            "original_name": "Breaking Bad",
            "overview": "A teacher...",
            "first_air_date": "2008-01-20",
            "poster_path": "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
            "vote_average": 9.5,
            "popularity": 200.0,
        },
        {
            "id": 12345,
            "media_type": "person",
            "name": "Some Actor",
        },
    ],
    "total_pages": 1,
    "total_results": 3,
}

SAMPLE_MOVIE_DETAILS = {
    "id": 27205,
    "title": "Inception",
    "original_title": "Inception",
    "overview": "A skilled thief is given a chance at redemption.",
    "release_date": "2010-07-16",
    "poster_path": "/9gk7adHYeDvHkCSEqAvQNLV5Ber.jpg",
    "backdrop_path": "/s3TBrRGB1iav7gFOCNx3H31MoES.jpg",
    "vote_average": 8.4,
    "vote_count": 35000,
    "popularity": 100.5,
    "genres": [
        {"id": 28, "name": "Action"},
        {"id": 878, "name": "Science Fiction"},
    ],
    "runtime": 148,
    "status": "Released",
    "tagline": "Your mind is the scene of the crime.",
    "budget": 160000000,
    "revenue": 836800000,
    "production_companies": [
        {
            "id": 923,
            "name": "Legendary Pictures",
            "logo_path": "/8M99Dkt23MjQMTTWukq4m5XsEuo.png",
            "origin_country": "US",
        },
    ],
    "imdb_id": "tt1375666",
}

SAMPLE_TV_DETAILS = {
    "id": 1396,
    "name": "Breaking Bad",
    "original_name": "Breaking Bad",
    "overview": "A high school chemistry teacher diagnosed with lung cancer.",
    "first_air_date": "2008-01-20",
    "last_air_date": "2013-09-29",
    "poster_path": "/ggFHVNu6YYI5L9pCfOacjizRGt.jpg",
    "backdrop_path": "/tsRy63Mu5cu8etL1X7ZLyf7UP1M.jpg",
    "vote_average": 9.5,
    "vote_count": 12000,
    "popularity": 200.0,
    "genres": [
        {"id": 18, "name": "Drama"},
        {"id": 80, "name": "Crime"},
    ],
    "episode_run_time": [45, 47],
    "status": "Ended",
    "tagline": "All Hail the King",
    "number_of_seasons": 5,
    "number_of_episodes": 62,
    "in_production": False,
    "production_companies": [
        {
            "id": 11073,
            "name": "Sony Pictures Television Studios",
            "logo_path": "/aCbASRcI1MI7DXjPbSW9Fcv9uGR.png",
            "origin_country": "US",
        },
    ],
}

SAMPLE_CREDITS = {
    "id": 27205,
    "cast": [
        {
            "id": 6193,
            "name": "Leonardo DiCaprio",
            "profile_path": "/wo2hJpn04vbtmh0B9utCFdsQhxM.jpg",
            "character": "Cobb",
            "known_for_department": "Acting",
            "popularity": 50.0,
        },
        {
            "id": 24045,
            "name": "Joseph Gordon-Levitt",
            "profile_path": "/dhv9v8Fy8UjGzB8YKWxqnfXmwjd.jpg",
            "character": "Arthur",
            "known_for_department": "Acting",
            "popularity": 25.0,
        },
    ],
    "crew": [
        {
            "id": 525,
            "name": "Christopher Nolan",
            "profile_path": "/xuAIuYSmsUzKlUMBFGVZaWsY3DZ.jpg",
            "job": "Director",
            "department": "Directing",
            "known_for_department": "Directing",
            "popularity": 30.0,
        },
        {
            "id": 525,
            "name": "Christopher Nolan",
            "profile_path": "/xuAIuYSmsUzKlUMBFGVZaWsY3DZ.jpg",
            "job": "Screenplay",
            "department": "Writing",
            "known_for_department": "Directing",
            "popularity": 30.0,
        },
    ],
}

SAMPLE_PERSON_DETAILS = {
    "id": 137427,
    "name": "Denis Villeneuve",
    "biography": "Denis Villeneuve is a French Canadian film director and writer.",
    "birthday": "1967-10-03",
    "deathday": None,
    "place_of_birth": "Trois-Rivières, Quebec, Canada",
    "profile_path": "/zdDx9Xs93UIrJFWYApYR28J8M6b.jpg",
    "known_for_department": "Directing",
    "popularity": 25.5,
}

SAMPLE_RECOMMENDATIONS = {
    "page": 1,
    "results": [
        {
            "id": 157336,
            "title": "Interstellar",
            "original_title": "Interstellar",
            "overview": "A space exploration epic.",
            "release_date": "2014-11-05",
            "poster_path": "/gEU2QniE6E77NI6lCU6MxlNBvIx.jpg",
            "vote_average": 8.4,
            "popularity": 95.0,
        },
        {
            "id": 49026,
            "title": "The Dark Knight Rises",
            "original_title": "The Dark Knight Rises",
            "overview": "Batman rises.",
            "release_date": "2012-07-20",
            "poster_path": "/dEYnvnUfXrqvqeRSqvIEtmzhoA8.jpg",
            "vote_average": 7.8,
            "popularity": 80.0,
        },
    ],
    "total_pages": 1,
    "total_results": 2,
}


# =============================================================================
# Data Model Tests
# =============================================================================


class TestGenre:
    """Tests for Genre model."""

    def test_genre_creation(self):
        """Test creating a Genre instance."""
        genre = Genre(id=28, name="Action")
        assert genre.id == 28
        assert genre.name == "Action"


class TestProductionCompany:
    """Tests for ProductionCompany model."""

    def test_production_company_creation(self):
        """Test creating a ProductionCompany instance."""
        company = ProductionCompany(
            id=923,
            name="Legendary Pictures",
            logo_path="/logo.png",
            origin_country="US",
        )
        assert company.id == 923
        assert company.name == "Legendary Pictures"
        assert company.logo_path == "/logo.png"
        assert company.origin_country == "US"

    def test_production_company_defaults(self):
        """Test default values for ProductionCompany."""
        company = ProductionCompany(id=1, name="Test")
        assert company.logo_path is None
        assert company.origin_country == ""


class TestPerson:
    """Tests for Person model."""

    def test_person_creation(self):
        """Test creating a Person instance."""
        person = Person(
            id=6193,
            name="Leonardo DiCaprio",
            profile_path="/profile.jpg",
            character="Cobb",
            popularity=50.0,
        )
        assert person.id == 6193
        assert person.name == "Leonardo DiCaprio"
        assert person.character == "Cobb"

    def test_get_profile_url(self):
        """Test getting profile image URL."""
        person = Person(id=1, name="Test", profile_path="/test.jpg")
        url = person.get_profile_url()
        assert url == "https://image.tmdb.org/t/p/w185/test.jpg"

    def test_get_profile_url_with_size(self):
        """Test getting profile URL with custom size."""
        person = Person(id=1, name="Test", profile_path="/test.jpg")
        url = person.get_profile_url(size="original")
        assert url == "https://image.tmdb.org/t/p/original/test.jpg"

    def test_get_profile_url_no_path(self):
        """Test profile URL when no path exists."""
        person = Person(id=1, name="Test")
        assert person.get_profile_url() is None


class TestCredits:
    """Tests for Credits model."""

    def test_credits_creation(self):
        """Test creating a Credits instance."""
        credits = Credits(
            cast=[Person(id=1, name="Actor 1", character="Char 1")],
            crew=[Person(id=2, name="Director 1", job="Director")],
        )
        assert len(credits.cast) == 1
        assert len(credits.crew) == 1

    def test_get_directors(self):
        """Test getting directors from crew."""
        credits = Credits(
            cast=[],
            crew=[
                Person(id=1, name="Director", job="Director"),
                Person(id=2, name="Producer", job="Producer"),
                Person(id=3, name="Director 2", job="Director"),
            ],
        )
        directors = credits.get_directors()
        assert len(directors) == 2
        assert all(d.job == "Director" for d in directors)

    def test_get_writers(self):
        """Test getting writers from crew."""
        credits = Credits(
            cast=[],
            crew=[
                Person(id=1, name="Writer", job="Screenplay", department="Writing"),
                Person(id=2, name="Director", job="Director", department="Directing"),
            ],
        )
        writers = credits.get_writers()
        assert len(writers) == 1
        assert writers[0].name == "Writer"

    def test_get_top_cast(self):
        """Test getting top cast members."""
        credits = Credits(
            cast=[Person(id=i, name=f"Actor {i}") for i in range(15)],
            crew=[],
        )
        top_cast = credits.get_top_cast(limit=5)
        assert len(top_cast) == 5
        assert top_cast[0].name == "Actor 0"


class TestMovie:
    """Tests for Movie model."""

    def test_movie_creation(self):
        """Test creating a Movie instance."""
        movie = Movie(
            id=27205,
            title="Inception",
            original_title="Inception",
            overview="A thief...",
            release_date="2010-07-16",
            vote_average=8.4,
        )
        assert movie.id == 27205
        assert movie.title == "Inception"
        assert movie.vote_average == 8.4

    def test_get_poster_url(self):
        """Test getting poster URL."""
        movie = Movie(id=1, title="Test", poster_path="/poster.jpg")
        url = movie.get_poster_url()
        assert url == "https://image.tmdb.org/t/p/w500/poster.jpg"

    def test_get_poster_url_no_path(self):
        """Test poster URL when no path exists."""
        movie = Movie(id=1, title="Test")
        assert movie.get_poster_url() is None

    def test_get_backdrop_url(self):
        """Test getting backdrop URL."""
        movie = Movie(id=1, title="Test", backdrop_path="/backdrop.jpg")
        url = movie.get_backdrop_url()
        assert url == "https://image.tmdb.org/t/p/w1280/backdrop.jpg"

    def test_get_year(self):
        """Test extracting year from release date."""
        movie = Movie(id=1, title="Test", release_date="2010-07-16")
        assert movie.get_year() == 2010

    def test_get_year_empty_date(self):
        """Test year extraction with empty date."""
        movie = Movie(id=1, title="Test", release_date="")
        assert movie.get_year() is None

    def test_get_year_invalid_date(self):
        """Test year extraction with invalid date."""
        movie = Movie(id=1, title="Test", release_date="invalid")
        assert movie.get_year() is None

    def test_get_genre_names(self):
        """Test getting genre names."""
        movie = Movie(
            id=1,
            title="Test",
            genres=[
                Genre(id=28, name="Action"),
                Genre(id=878, name="Science Fiction"),
            ],
        )
        names = movie.get_genre_names()
        assert names == ["Action", "Science Fiction"]


class TestTVShow:
    """Tests for TVShow model."""

    def test_tv_show_creation(self):
        """Test creating a TVShow instance."""
        tv = TVShow(
            id=1396,
            name="Breaking Bad",
            first_air_date="2008-01-20",
            number_of_seasons=5,
        )
        assert tv.id == 1396
        assert tv.name == "Breaking Bad"
        assert tv.number_of_seasons == 5

    def test_get_year(self):
        """Test extracting year from first air date."""
        tv = TVShow(id=1, name="Test", first_air_date="2008-01-20")
        assert tv.get_year() == 2008

    def test_get_poster_url(self):
        """Test getting poster URL."""
        tv = TVShow(id=1, name="Test", poster_path="/poster.jpg")
        url = tv.get_poster_url()
        assert url == "https://image.tmdb.org/t/p/w500/poster.jpg"


class TestSearchResult:
    """Tests for SearchResult model."""

    def test_search_result_movie(self):
        """Test creating a movie search result."""
        result = SearchResult(
            id=27205,
            media_type=MediaType.MOVIE,
            title="Inception",
            release_date="2010-07-16",
            vote_average=8.4,
        )
        assert result.id == 27205
        assert result.media_type == MediaType.MOVIE
        assert result.get_year() == 2010

    def test_search_result_tv(self):
        """Test creating a TV search result."""
        result = SearchResult(
            id=1396,
            media_type=MediaType.TV,
            title="Breaking Bad",
            release_date="2008-01-20",
        )
        assert result.media_type == MediaType.TV


# =============================================================================
# Cache Tests
# =============================================================================


class TestSimpleCache:
    """Tests for SimpleCache."""

    def test_cache_set_get(self):
        """Test basic set and get."""
        cache = SimpleCache(ttl=60)
        cache.set("key1", {"data": "value"})
        assert cache.get("key1") == {"data": "value"}

    def test_cache_miss(self):
        """Test cache miss returns None."""
        cache = SimpleCache(ttl=60)
        assert cache.get("nonexistent") is None

    def test_cache_expiry(self):
        """Test cache expiration."""
        cache = SimpleCache(ttl=1)
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

        # Simulate time passing
        cache._cache["key1"] = ("value1", time.time() - 2)
        assert cache.get("key1") is None

    def test_cache_clear(self):
        """Test clearing cache."""
        cache = SimpleCache(ttl=60)
        cache.set("key1", "value1")
        cache.set("key2", "value2")
        cache.clear()
        assert cache.get("key1") is None
        assert cache.get("key2") is None

    def test_cache_cleanup_expired(self):
        """Test cleanup of expired entries."""
        cache = SimpleCache(ttl=1)
        cache.set("key1", "value1")
        cache.set("key2", "value2")

        # Expire one entry
        cache._cache["key1"] = ("value1", time.time() - 2)

        removed = cache.cleanup_expired()
        assert removed == 1
        assert cache.get("key1") is None
        assert cache.get("key2") == "value2"


# =============================================================================
# TMDBClient Tests
# =============================================================================


class TestTMDBClient:
    """Tests for TMDBClient."""

    @pytest.fixture
    def mock_response(self):
        """Create a mock HTTP response."""

        def _create_response(data: dict, status_code: int = 200):
            response = MagicMock(spec=httpx.Response)
            response.status_code = status_code
            response.json.return_value = data
            response.text = str(data)
            response.headers = {}
            return response

        return _create_response

    @pytest.mark.asyncio
    async def test_client_context_manager(self):
        """Test client as context manager."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                assert client._client is not None
            assert client._client is None

    @pytest.mark.asyncio
    async def test_client_not_in_context(self):
        """Test client raises error when not in context manager."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            client = TMDBClient()
            with pytest.raises(RuntimeError, match="must be used as async context"):
                _ = client.client

    @pytest.mark.asyncio
    async def test_search_movie(self, mock_response):
        """Test movie search."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(
                    return_value=mock_response(SAMPLE_MOVIE_SEARCH_RESPONSE)
                )

                results = await client.search_movie("Inception")

                assert len(results) == 2
                assert results[0].id == 27205
                assert results[0].title == "Inception"
                assert results[0].media_type == MediaType.MOVIE
                assert results[0].vote_average == 8.4

    @pytest.mark.asyncio
    async def test_search_movie_with_year(self, mock_response):
        """Test movie search with year filter."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(
                    return_value=mock_response(SAMPLE_MOVIE_SEARCH_RESPONSE)
                )

                results = await client.search_movie("Inception", year=2010)

                assert len(results) == 2
                # Verify year was passed in params
                call_args = client._client.get.call_args
                assert "year" in call_args.kwargs["params"]
                assert call_args.kwargs["params"]["year"] == 2010

    @pytest.mark.asyncio
    async def test_search_tv(self, mock_response):
        """Test TV show search."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(
                    return_value=mock_response(SAMPLE_TV_SEARCH_RESPONSE)
                )

                results = await client.search_tv("Breaking Bad")

                assert len(results) == 1
                assert results[0].id == 1396
                assert results[0].title == "Breaking Bad"
                assert results[0].media_type == MediaType.TV

    @pytest.mark.asyncio
    async def test_search_multi(self, mock_response):
        """Test multi search (movies and TV)."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(
                    return_value=mock_response(SAMPLE_MULTI_SEARCH_RESPONSE)
                )

                results = await client.search_multi("test")

                # Should filter out person results
                assert len(results) == 2
                assert results[0].media_type == MediaType.MOVIE
                assert results[1].media_type == MediaType.TV

    @pytest.mark.asyncio
    async def test_get_movie(self, mock_response):
        """Test getting movie details."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(return_value=mock_response(SAMPLE_MOVIE_DETAILS))

                movie = await client.get_movie(27205)

                assert movie.id == 27205
                assert movie.title == "Inception"
                assert movie.runtime == 148
                assert len(movie.genres) == 2
                assert movie.genres[0].name == "Action"
                assert movie.imdb_id == "tt1375666"

    @pytest.mark.asyncio
    async def test_get_tv_show(self, mock_response):
        """Test getting TV show details."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(return_value=mock_response(SAMPLE_TV_DETAILS))

                tv = await client.get_tv_show(1396)

                assert tv.id == 1396
                assert tv.name == "Breaking Bad"
                assert tv.number_of_seasons == 5
                assert tv.number_of_episodes == 62
                assert not tv.in_production

    @pytest.mark.asyncio
    async def test_get_person(self, mock_response):
        """Test getting person details."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(return_value=mock_response(SAMPLE_PERSON_DETAILS))

                person = await client.get_person(137427)

                assert person["id"] == 137427
                assert person["name"] == "Denis Villeneuve"
                assert person["known_for_department"] == "Directing"
                assert person["birthday"] == "1967-10-03"
                assert person["profile_path"] == "/zdDx9Xs93UIrJFWYApYR28J8M6b.jpg"
                assert "biography" in person

    @pytest.mark.asyncio
    async def test_get_movie_credits(self, mock_response):
        """Test getting movie credits."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(return_value=mock_response(SAMPLE_CREDITS))

                credits = await client.get_movie_credits(27205)

                assert len(credits.cast) == 2
                assert credits.cast[0].name == "Leonardo DiCaprio"
                assert credits.cast[0].character == "Cobb"
                assert len(credits.crew) == 2
                directors = credits.get_directors()
                assert len(directors) == 1
                assert directors[0].name == "Christopher Nolan"

    @pytest.mark.asyncio
    async def test_get_credits_movie(self, mock_response):
        """Test get_credits for movies."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(return_value=mock_response(SAMPLE_CREDITS))

                credits = await client.get_credits(27205, MediaType.MOVIE)
                assert len(credits.cast) == 2

    @pytest.mark.asyncio
    async def test_get_credits_invalid_type(self):
        """Test get_credits with invalid media type."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)

                with pytest.raises(ValueError, match="Invalid media type"):
                    await client.get_credits(1, MediaType.PERSON)

    @pytest.mark.asyncio
    async def test_get_movie_recommendations(self, mock_response):
        """Test getting movie recommendations."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(return_value=mock_response(SAMPLE_RECOMMENDATIONS))

                results = await client.get_movie_recommendations(27205)

                assert len(results) == 2
                assert results[0].id == 157336
                assert results[0].title == "Interstellar"
                assert results[0].media_type == MediaType.MOVIE

    @pytest.mark.asyncio
    async def test_get_recommendations_movie(self, mock_response):
        """Test get_recommendations for movies."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(return_value=mock_response(SAMPLE_RECOMMENDATIONS))

                results = await client.get_recommendations(27205, MediaType.MOVIE)
                assert len(results) == 2

    @pytest.mark.asyncio
    async def test_get_recommendations_invalid_type(self):
        """Test get_recommendations with invalid media type."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)

                with pytest.raises(ValueError, match="Invalid media type"):
                    await client.get_recommendations(1, MediaType.PERSON)

    @pytest.mark.asyncio
    async def test_caching(self, mock_response):
        """Test that responses are cached."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(
                    return_value=mock_response(SAMPLE_MOVIE_SEARCH_RESPONSE)
                )

                # First call
                await client.search_movie("Inception")
                assert client._client.get.call_count == 1

                # Second call should use cache
                await client.search_movie("Inception")
                assert client._client.get.call_count == 1

    @pytest.mark.asyncio
    async def test_cache_clear(self, mock_response):
        """Test clearing cache."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(
                    return_value=mock_response(SAMPLE_MOVIE_SEARCH_RESPONSE)
                )

                await client.search_movie("Inception")
                client.clear_cache()
                await client.search_movie("Inception")

                # After clear, should make new request
                assert client._client.get.call_count == 2


# =============================================================================
# Error Handling Tests
# =============================================================================


class TestTMDBErrors:
    """Tests for TMDB error handling."""

    @pytest.fixture
    def mock_error_response(self):
        """Create a mock error HTTP response."""

        def _create_response(status_code: int, text: str = "Error"):
            response = MagicMock(spec=httpx.Response)
            response.status_code = status_code
            response.text = text
            response.headers = {"Retry-After": "5"}
            return response

        return _create_response

    @pytest.mark.asyncio
    async def test_not_found_error(self, mock_error_response):
        """Test 404 error handling."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(return_value=mock_error_response(404, "Not Found"))

                with pytest.raises(TMDBNotFoundError, match="Resource not found"):
                    await client.get_movie(999999)

    @pytest.mark.asyncio
    async def test_auth_error(self, mock_error_response):
        """Test 401 error handling."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "invalid_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(
                    return_value=mock_error_response(401, "Unauthorized")
                )

                with pytest.raises(TMDBAuthError, match="Invalid TMDB API key"):
                    await client.search_movie("test")

    @pytest.mark.asyncio
    async def test_rate_limit_error(self, mock_error_response):
        """Test 429 rate limit error handling."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(
                    return_value=mock_error_response(429, "Too Many Requests")
                )

                with pytest.raises(TMDBRateLimitError) as exc_info:
                    await client.search_movie("test")

                assert exc_info.value.retry_after == 5

    @pytest.mark.asyncio
    async def test_timeout_error(self):
        """Test timeout error handling."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))

                with pytest.raises(TMDBError, match="Request timeout"):
                    await client.search_movie("test")

    @pytest.mark.asyncio
    async def test_http_error(self):
        """Test generic HTTP error handling."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(side_effect=httpx.HTTPError("Connection failed"))

                with pytest.raises(TMDBError, match="HTTP error"):
                    await client.search_movie("test")

    @pytest.mark.asyncio
    async def test_generic_api_error(self, mock_error_response):
        """Test generic API error handling."""
        with patch("src.media.tmdb.settings") as mock_settings:
            mock_settings.tmdb_api_key.get_secret_value.return_value = "test_key"
            mock_settings.cache_ttl = 3600

            async with TMDBClient() as client:
                client._client = MagicMock(spec=httpx.AsyncClient)
                client._client.get = AsyncMock(
                    return_value=mock_error_response(500, "Internal Server Error")
                )

                with pytest.raises(TMDBError, match="TMDB API error 500"):
                    await client.search_movie("test")


# =============================================================================
# Exception Tests
# =============================================================================


class TestExceptions:
    """Tests for exception classes."""

    def test_tmdb_error(self):
        """Test TMDBError creation."""
        error = TMDBError("Test error")
        assert str(error) == "Test error"

    def test_tmdb_not_found_error(self):
        """Test TMDBNotFoundError is subclass of TMDBError."""
        error = TMDBNotFoundError("Not found")
        assert isinstance(error, TMDBError)

    def test_tmdb_rate_limit_error(self):
        """Test TMDBRateLimitError with retry_after."""
        error = TMDBRateLimitError(retry_after=10)
        assert error.retry_after == 10
        assert "10 seconds" in str(error)

    def test_tmdb_auth_error(self):
        """Test TMDBAuthError is subclass of TMDBError."""
        error = TMDBAuthError("Invalid key")
        assert isinstance(error, TMDBError)
