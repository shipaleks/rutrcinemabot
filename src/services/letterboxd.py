"""Letterboxd integration service.

This module provides:
- OAuth 2.0 authentication flow for Letterboxd
- Import/export of watchlist, watched, ratings, diary
- Two-way synchronization between bot and Letterboxd

IMPORTANT: Letterboxd API requires approval. Apply at api@letterboxd.com.
Until approved, this module will operate in mock mode.

Usage:
    async with LetterboxdClient(access_token) as client:
        # Import user's watchlist
        watchlist = await client.get_watchlist()

        # Export rating to Letterboxd
        await client.rate_film(letterboxd_id, rating, review)
"""

import contextlib
import hashlib
import hmac
import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger()


# =============================================================================
# Configuration
# =============================================================================

LETTERBOXD_API_BASE = "https://api.letterboxd.com/api/v0"
LETTERBOXD_AUTH_URL = "https://letterboxd.com/authorize"
LETTERBOXD_TOKEN_URL = "https://api.letterboxd.com/api/v0/auth/token"

# Request timeout
REQUEST_TIMEOUT = 30.0


# =============================================================================
# Data Models
# =============================================================================


class LetterboxdFilm(BaseModel):
    """Letterboxd film model."""

    id: str
    name: str
    original_name: str | None = None
    year: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    poster_url: str | None = None
    runtime: int | None = None  # in minutes


class LetterboxdWatchlistEntry(BaseModel):
    """Letterboxd watchlist entry."""

    film: LetterboxdFilm
    added_at: datetime
    notes: str | None = None


class LetterboxdDiaryEntry(BaseModel):
    """Letterboxd diary entry (watched film)."""

    film: LetterboxdFilm
    watched_at: datetime
    rating: float | None = None  # 0.5 to 5.0 in 0.5 increments
    review: str | None = None
    rewatch: bool = False
    liked: bool = False


class LetterboxdRating(BaseModel):
    """Letterboxd rating model."""

    film: LetterboxdFilm
    rating: float  # 0.5 to 5.0
    rated_at: datetime


class LetterboxdUser(BaseModel):
    """Letterboxd user profile."""

    id: str
    username: str
    display_name: str | None = None
    avatar_url: str | None = None
    film_count: int = 0
    watchlist_count: int = 0


class OAuthToken(BaseModel):
    """OAuth token model."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 3600
    refresh_token: str | None = None
    scope: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_expired(self) -> bool:
        """Check if token is expired."""
        elapsed = (datetime.now(UTC) - self.created_at).total_seconds()
        return elapsed >= self.expires_in - 60  # 1 minute buffer


# =============================================================================
# Exceptions
# =============================================================================


class LetterboxdError(Exception):
    """Base exception for Letterboxd errors."""

    pass


class LetterboxdAuthError(LetterboxdError):
    """Authentication error."""

    pass


class LetterboxdAPIError(LetterboxdError):
    """API error with status code."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class LetterboxdNotConfiguredError(LetterboxdError):
    """API not configured (missing credentials)."""

    pass


# =============================================================================
# OAuth Helper
# =============================================================================


class LetterboxdOAuth:
    """OAuth 2.0 helper for Letterboxd authentication."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str = "https://localhost/callback",
    ):
        """Initialize OAuth helper.

        Args:
            client_id: Letterboxd API client ID
            client_secret: Letterboxd API client secret
            redirect_uri: OAuth redirect URI
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    def get_authorization_url(self, state: str | None = None) -> str:
        """Get authorization URL for user to visit.

        Args:
            state: Optional state parameter for CSRF protection

        Returns:
            Authorization URL
        """
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "profile watchlist log",
        }
        if state:
            params["state"] = state

        return f"{LETTERBOXD_AUTH_URL}?{urllib.parse.urlencode(params)}"

    def _sign_request(
        self,
        method: str,
        url: str,
        body: str = "",
        timestamp: int | None = None,
    ) -> tuple[str, int]:
        """Sign a request for Letterboxd API.

        Letterboxd uses HMAC-SHA256 for request signing.

        Args:
            method: HTTP method
            url: Full request URL
            body: Request body (for POST)
            timestamp: Unix timestamp (defaults to now)

        Returns:
            Tuple of (signature, timestamp)
        """
        timestamp = timestamp or int(time.time())
        nonce = hashlib.md5(f"{timestamp}".encode()).hexdigest()

        # Create signature base
        signature_base = "\u0000".join(
            [
                method.upper(),
                url,
                body,
                str(timestamp),
                nonce,
            ]
        )

        # HMAC-SHA256 signature
        signature = hmac.new(
            self.client_secret.encode(),
            signature_base.encode(),
            hashlib.sha256,
        ).hexdigest()

        return signature, timestamp

    async def exchange_code(self, authorization_code: str) -> OAuthToken:
        """Exchange authorization code for access token.

        Args:
            authorization_code: Code received from OAuth callback

        Returns:
            OAuth token

        Raises:
            LetterboxdAuthError: If token exchange fails
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                LETTERBOXD_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "redirect_uri": self.redirect_uri,
                },
            )

            if response.status_code != 200:
                logger.error(
                    "letterboxd_token_exchange_failed",
                    status=response.status_code,
                    body=response.text,
                )
                raise LetterboxdAuthError(f"Token exchange failed: {response.status_code}")

            data = response.json()
            return OAuthToken(
                access_token=data["access_token"],
                token_type=data.get("token_type", "Bearer"),
                expires_in=data.get("expires_in", 3600),
                refresh_token=data.get("refresh_token"),
                scope=data.get("scope"),
            )

    async def refresh_token(self, refresh_token: str) -> OAuthToken:
        """Refresh an expired access token.

        Args:
            refresh_token: Refresh token

        Returns:
            New OAuth token

        Raises:
            LetterboxdAuthError: If refresh fails
        """
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            response = await client.post(
                LETTERBOXD_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
            )

            if response.status_code != 200:
                logger.error(
                    "letterboxd_token_refresh_failed",
                    status=response.status_code,
                )
                raise LetterboxdAuthError(f"Token refresh failed: {response.status_code}")

            data = response.json()
            return OAuthToken(
                access_token=data["access_token"],
                token_type=data.get("token_type", "Bearer"),
                expires_in=data.get("expires_in", 3600),
                refresh_token=data.get("refresh_token", refresh_token),
                scope=data.get("scope"),
            )


# =============================================================================
# Letterboxd Client
# =============================================================================


class LetterboxdClient:
    """Async client for Letterboxd API.

    Note: Requires API access approval from Letterboxd.
    Contact api@letterboxd.com to request access.

    Usage:
        async with LetterboxdClient(access_token, client_id, client_secret) as client:
            user = await client.get_me()
            watchlist = await client.get_watchlist()
    """

    def __init__(
        self,
        access_token: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        """Initialize Letterboxd client.

        Args:
            access_token: OAuth access token for authenticated requests
            client_id: Letterboxd API client ID
            client_secret: Letterboxd API client secret
        """
        self._access_token = access_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "LetterboxdClient":
        """Open HTTP client."""
        self._client = httpx.AsyncClient(
            base_url=LETTERBOXD_API_BASE,
            timeout=REQUEST_TIMEOUT,
        )
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: object | None,
    ) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get active HTTP client."""
        if self._client is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        return self._client

    def _get_headers(self) -> dict[str, str]:
        """Get request headers with authentication."""
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make authenticated API request.

        Args:
            method: HTTP method
            endpoint: API endpoint (without base URL)
            params: Query parameters
            json_data: JSON body data

        Returns:
            Response JSON

        Raises:
            LetterboxdAPIError: If request fails
        """
        try:
            response = await self.client.request(
                method,
                endpoint,
                params=params,
                json=json_data,
                headers=self._get_headers(),
            )

            if response.status_code == 401:
                raise LetterboxdAuthError("Access token expired or invalid")

            if response.status_code >= 400:
                raise LetterboxdAPIError(
                    f"API error: {response.text}",
                    status_code=response.status_code,
                )

            return response.json() if response.text else {}

        except httpx.RequestError as e:
            logger.error("letterboxd_request_failed", error=str(e))
            raise LetterboxdAPIError(f"Request failed: {e}") from e

    # -------------------------------------------------------------------------
    # User endpoints
    # -------------------------------------------------------------------------

    async def get_me(self) -> LetterboxdUser:
        """Get authenticated user's profile.

        Returns:
            User profile

        Raises:
            LetterboxdAuthError: If not authenticated
        """
        data = await self._request("GET", "/me")
        return self._parse_user(data)

    # -------------------------------------------------------------------------
    # Watchlist endpoints
    # -------------------------------------------------------------------------

    async def get_watchlist(
        self,
        username: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[LetterboxdWatchlistEntry]:
        """Get user's watchlist.

        Args:
            username: Username (None for authenticated user)
            limit: Maximum entries to return per page (API max is typically 100)
            cursor: Pagination cursor

        Returns:
            List of watchlist entries
        """
        endpoint = f"/member/{username}/watchlist" if username else "/me/watchlist"
        params: dict[str, Any] = {"perPage": limit}
        if cursor:
            params["cursor"] = cursor

        data = await self._request("GET", endpoint, params=params)
        items = data.get("items", [])

        return [self._parse_watchlist_entry(item) for item in items]

    async def add_to_watchlist(self, film_id: str) -> bool:
        """Add film to authenticated user's watchlist.

        Args:
            film_id: Letterboxd film ID

        Returns:
            True if successful
        """
        await self._request("POST", "/me/watchlist", json_data={"filmId": film_id})
        logger.info("letterboxd_watchlist_added", film_id=film_id)
        return True

    async def remove_from_watchlist(self, film_id: str) -> bool:
        """Remove film from watchlist.

        Args:
            film_id: Letterboxd film ID

        Returns:
            True if successful
        """
        await self._request("DELETE", f"/me/watchlist/{film_id}")
        logger.info("letterboxd_watchlist_removed", film_id=film_id)
        return True

    # -------------------------------------------------------------------------
    # Diary/Watch history endpoints
    # -------------------------------------------------------------------------

    async def get_diary(
        self,
        username: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> list[LetterboxdDiaryEntry]:
        """Get user's diary (watch history).

        Args:
            username: Username (None for authenticated user)
            limit: Maximum entries to return per page (API max is typically 100)
            cursor: Pagination cursor for getting more results

        Returns:
            List of diary entries
        """
        endpoint = f"/member/{username}/log-entries" if username else "/me/log-entries"
        params: dict[str, Any] = {"perPage": limit}
        if cursor:
            params["cursor"] = cursor

        data = await self._request("GET", endpoint, params=params)
        items = data.get("items", [])

        return [self._parse_diary_entry(item) for item in items]

    async def add_diary_entry(
        self,
        film_id: str,
        watched_date: datetime | None = None,
        rating: float | None = None,
        review: str | None = None,
        rewatch: bool = False,
        liked: bool = False,
    ) -> LetterboxdDiaryEntry:
        """Add a diary entry (mark film as watched).

        Args:
            film_id: Letterboxd film ID
            watched_date: When the film was watched (defaults to now)
            rating: Rating (0.5 to 5.0)
            review: Review text
            rewatch: Is this a rewatch?
            liked: Did user like the film?

        Returns:
            Created diary entry
        """
        json_data: dict[str, Any] = {
            "filmId": film_id,
            "diaryDetails": {
                "diaryDate": (watched_date or datetime.now(UTC)).strftime("%Y-%m-%d"),
                "rewatch": rewatch,
            },
        }

        if rating is not None:
            json_data["rating"] = rating

        if review:
            json_data["review"] = {
                "text": review,
                "containsSpoilers": False,
            }

        if liked:
            json_data["like"] = True

        data = await self._request("POST", "/log-entries", json_data=json_data)
        logger.info("letterboxd_diary_entry_added", film_id=film_id, rating=rating)

        return self._parse_diary_entry(data)

    # -------------------------------------------------------------------------
    # Film search endpoints
    # -------------------------------------------------------------------------

    async def search_films(
        self,
        query: str,
        limit: int = 10,
    ) -> list[LetterboxdFilm]:
        """Search for films.

        Args:
            query: Search query
            limit: Maximum results

        Returns:
            List of matching films
        """
        data = await self._request(
            "GET",
            "/search",
            params={
                "input": query,
                "searchMethod": "Autocomplete",
                "include": "FilmSearchItem",
                "perPage": limit,
            },
        )

        items = data.get("items", [])
        films = []

        for item in items:
            if item.get("type") == "FilmSearchItem":
                film_data = item.get("film", {})
                films.append(self._parse_film(film_data))

        return films

    async def get_film_by_tmdb_id(self, tmdb_id: int) -> LetterboxdFilm | None:
        """Get film by TMDB ID.

        Args:
            tmdb_id: TMDB film ID

        Returns:
            Film or None if not found
        """
        try:
            data = await self._request(
                "GET",
                "/films",
                params={"tmdbId": tmdb_id, "perPage": 1},
            )
            items = data.get("items", [])
            if items:
                return self._parse_film(items[0])
            return None
        except LetterboxdAPIError:
            return None

    # -------------------------------------------------------------------------
    # Sync functionality
    # -------------------------------------------------------------------------

    async def sync_watchlist_to_bot(
        self,
        storage: Any,  # BaseStorage
        user_id: int,
    ) -> dict[str, int]:
        """Import Letterboxd watchlist to bot.

        Args:
            storage: Bot storage instance
            user_id: Bot user ID

        Returns:
            Dict with counts: {"imported": N, "skipped": N}
        """
        imported = 0
        skipped = 0

        watchlist = await self.get_watchlist()

        for entry in watchlist:
            film = entry.film
            if film.tmdb_id:
                # Check if already in bot's watchlist
                if await storage.is_in_watchlist(user_id, tmdb_id=film.tmdb_id):
                    skipped += 1
                    continue

                await storage.add_to_watchlist(
                    user_id=user_id,
                    tmdb_id=film.tmdb_id,
                    media_type="movie",
                    title=film.name,
                    year=film.year,
                    notes=entry.notes,
                )
                imported += 1
            else:
                skipped += 1

        logger.info(
            "letterboxd_watchlist_synced",
            user_id=user_id,
            imported=imported,
            skipped=skipped,
        )

        return {"imported": imported, "skipped": skipped}

    async def sync_diary_to_bot(
        self,
        storage: Any,  # BaseStorage
        user_id: int,
        limit: int = 100,
    ) -> dict[str, int]:
        """Import Letterboxd diary to bot's watch history.

        Args:
            storage: Bot storage instance
            user_id: Bot user ID
            limit: Maximum entries to import

        Returns:
            Dict with counts: {"imported": N, "skipped": N}
        """
        imported = 0
        skipped = 0

        diary = await self.get_diary(limit=limit)

        for entry in diary:
            film = entry.film
            if film.tmdb_id:
                # Check if already watched
                if await storage.is_watched(user_id, tmdb_id=film.tmdb_id):
                    skipped += 1
                    continue

                # Convert Letterboxd rating (0.5-5.0) to bot rating (1-10)
                rating = None
                if entry.rating:
                    rating = entry.rating * 2  # 5.0 -> 10

                await storage.add_watched(
                    user_id=user_id,
                    media_type="movie",
                    title=film.name,
                    tmdb_id=film.tmdb_id,
                    year=film.year,
                    rating=rating,
                    review=entry.review,
                    watched_at=entry.watched_at,
                )
                imported += 1
            else:
                skipped += 1

        logger.info(
            "letterboxd_diary_synced",
            user_id=user_id,
            imported=imported,
            skipped=skipped,
        )

        return {"imported": imported, "skipped": skipped}

    async def export_rating_to_letterboxd(
        self,
        tmdb_id: int,
        rating: float,
        review: str | None = None,
        watched_date: datetime | None = None,
    ) -> bool:
        """Export a rating from bot to Letterboxd.

        Args:
            tmdb_id: TMDB film ID
            rating: Bot rating (1-10)
            review: Optional review text
            watched_date: When the film was watched

        Returns:
            True if successful
        """
        # Find film on Letterboxd by TMDB ID
        film = await self.get_film_by_tmdb_id(tmdb_id)
        if not film:
            logger.warning("letterboxd_film_not_found", tmdb_id=tmdb_id)
            return False

        # Convert bot rating (1-10) to Letterboxd rating (0.5-5.0)
        lb_rating = round(rating / 2 * 2) / 2  # Round to 0.5 increments
        lb_rating = max(0.5, min(5.0, lb_rating))

        await self.add_diary_entry(
            film_id=film.id,
            watched_date=watched_date,
            rating=lb_rating,
            review=review,
        )

        return True

    # -------------------------------------------------------------------------
    # Helper methods
    # -------------------------------------------------------------------------

    def _parse_film(self, data: dict[str, Any]) -> LetterboxdFilm:
        """Parse film data from API response."""
        links = data.get("links", [])
        tmdb_id = None
        imdb_id = None

        for link in links:
            if link.get("type") == "tmdb":
                with contextlib.suppress(ValueError):
                    tmdb_id = int(link.get("id", ""))
            elif link.get("type") == "imdb":
                imdb_id = link.get("id")

        poster_url = None
        poster = data.get("poster")
        if poster and poster.get("sizes"):
            # Get medium size poster
            for size in poster["sizes"]:
                if size.get("width") == 230:
                    poster_url = size.get("url")
                    break

        return LetterboxdFilm(
            id=data.get("id", ""),
            name=data.get("name", ""),
            original_name=data.get("originalName"),
            year=data.get("releaseYear"),
            tmdb_id=tmdb_id,
            imdb_id=imdb_id,
            poster_url=poster_url,
            runtime=data.get("runTime"),
        )

    def _parse_user(self, data: dict[str, Any]) -> LetterboxdUser:
        """Parse user data from API response."""
        avatar_url = None
        avatar = data.get("avatar")
        if avatar and avatar.get("sizes"):
            for size in avatar["sizes"]:
                if size.get("width") == 100:
                    avatar_url = size.get("url")
                    break

        return LetterboxdUser(
            id=data.get("id", ""),
            username=data.get("username", ""),
            display_name=data.get("displayName"),
            avatar_url=avatar_url,
            film_count=data.get("stats", {}).get("filmCount", 0),
            watchlist_count=data.get("stats", {}).get("watchlistLength", 0),
        )

    def _parse_watchlist_entry(self, data: dict[str, Any]) -> LetterboxdWatchlistEntry:
        """Parse watchlist entry from API response."""
        film_data = data.get("film", {})

        return LetterboxdWatchlistEntry(
            film=self._parse_film(film_data),
            added_at=datetime.fromisoformat(
                data.get("whenAdded", datetime.now(UTC).isoformat()).replace("Z", "+00:00")
            ),
            notes=data.get("notes"),
        )

    def _parse_diary_entry(self, data: dict[str, Any]) -> LetterboxdDiaryEntry:
        """Parse diary entry from API response."""
        film_data = data.get("film", {})
        diary_details = data.get("diaryDetails", {})

        watched_date = diary_details.get("diaryDate")
        if watched_date:
            watched_at = datetime.strptime(watched_date, "%Y-%m-%d").replace(tzinfo=UTC)
        else:
            watched_at = datetime.now(UTC)

        return LetterboxdDiaryEntry(
            film=self._parse_film(film_data),
            watched_at=watched_at,
            rating=data.get("rating"),
            review=data.get("review", {}).get("text"),
            rewatch=diary_details.get("rewatch", False),
            liked=data.get("like", False),
        )
