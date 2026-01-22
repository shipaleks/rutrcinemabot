"""Letterboxd RSS feed parser.

This module provides RSS-based sync with Letterboxd (read-only).
No API key required - works immediately with any public Letterboxd profile.

Usage:
    client = LetterboxdRSS("username")
    watchlist = await client.get_watchlist()
    diary = await client.get_diary()
"""

import re
from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Letterboxd RSS URLs
LETTERBOXD_BASE = "https://letterboxd.com"
RSS_DIARY = "{base}/{username}/rss/"
RSS_WATCHLIST = "{base}/{username}/rss/watchlist/"

# Request settings
REQUEST_TIMEOUT = 30.0
USER_AGENT = "MediaConciergeBot/1.0"


@dataclass
class LetterboxdRSSFilm:
    """Film from Letterboxd RSS feed."""

    title: str
    year: int | None
    letterboxd_url: str
    poster_url: str | None = None


@dataclass
class LetterboxdRSSWatchlistItem:
    """Watchlist item from RSS."""

    film: LetterboxdRSSFilm
    added_at: datetime


@dataclass
class LetterboxdRSSDiaryEntry:
    """Diary entry from RSS (watched film)."""

    film: LetterboxdRSSFilm
    watched_at: datetime
    rating: float | None  # 0.5 to 5.0
    rewatch: bool
    review_snippet: str | None


class LetterboxdRSSError(Exception):
    """Error fetching or parsing Letterboxd RSS."""

    pass


class LetterboxdRSS:
    """Letterboxd RSS feed client.

    Parses public RSS feeds to import watchlist and diary.
    No authentication required.

    Usage:
        client = LetterboxdRSS("username")
        watchlist = await client.get_watchlist()
        diary = await client.get_diary()  # Gets all available entries
    """

    def __init__(self, username: str):
        """Initialize RSS client.

        Args:
            username: Letterboxd username (from profile URL)
        """
        self.username = username.strip().lower()
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "LetterboxdRSS":
        """Open HTTP client."""
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, *args) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get HTTP client."""
        if self._client is None:
            raise RuntimeError("Client not initialized. Use 'async with' context.")
        return self._client

    async def _fetch_rss(self, url: str, allow_empty: bool = False) -> str | None:
        """Fetch RSS feed content.

        Args:
            url: RSS feed URL
            allow_empty: If True, return None on 404 instead of raising error

        Returns:
            RSS XML content or None if allow_empty and 404

        Raises:
            LetterboxdRSSError: If fetch fails
        """
        try:
            response = await self.client.get(url)

            if response.status_code == 404:
                if allow_empty:
                    logger.info("letterboxd_rss_empty", url=url)
                    return None
                raise LetterboxdRSSError(f"User '{self.username}' not found on Letterboxd")

            response.raise_for_status()
            return response.text

        except httpx.HTTPStatusError as e:
            logger.error("letterboxd_rss_http_error", url=url, status=e.response.status_code)
            raise LetterboxdRSSError(f"HTTP error: {e.response.status_code}") from e
        except httpx.RequestError as e:
            logger.error("letterboxd_rss_request_error", url=url, error=str(e))
            raise LetterboxdRSSError(f"Request failed: {e}") from e

    def _parse_film_title(self, title: str) -> tuple[str, int | None]:
        """Parse film title and year from RSS title.

        RSS titles are like: "Film Name, 2023" or "Film Name, 2023 - ★★★★"

        Args:
            title: Raw title from RSS

        Returns:
            Tuple of (title, year)
        """
        # Remove rating stars if present
        title = re.sub(r"\s*-\s*★+½?\s*$", "", title)
        title = re.sub(r"\s*-\s*\(no rating\)\s*$", "", title)

        # Extract year
        match = re.match(r"^(.+),\s*(\d{4})$", title)
        if match:
            return match.group(1).strip(), int(match.group(2))

        return title.strip(), None

    def _parse_rating(self, title: str) -> float | None:
        """Extract rating from RSS title.

        Ratings appear as: "★★★★" (4 stars) or "★★★½" (3.5 stars)

        Args:
            title: Raw title from RSS

        Returns:
            Rating as float (0.5-5.0) or None
        """
        match = re.search(r"(★+)(½)?", title)
        if match:
            stars = len(match.group(1))
            half = 0.5 if match.group(2) else 0
            return min(5.0, stars + half)
        return None

    def _parse_date(self, date_str: str) -> datetime:
        """Parse RSS date string.

        Args:
            date_str: Date string like "Sat, 18 Jan 2025 12:00:00 +0000"

        Returns:
            Parsed datetime
        """
        try:
            # Standard RSS date format
            return datetime.strptime(date_str, "%a, %d %b %Y %H:%M:%S %z")
        except ValueError:
            # Fallback to now
            return datetime.now()

    async def get_watchlist(self, limit: int = 10000) -> list[LetterboxdRSSWatchlistItem]:
        """Get user's watchlist from RSS.

        Args:
            limit: Maximum items to return (default 10000 - all available)

        Returns:
            List of watchlist items (empty if watchlist is empty/private)
        """
        url = RSS_WATCHLIST.format(base=LETTERBOXD_BASE, username=self.username)
        logger.info("fetching_letterboxd_watchlist", username=self.username)

        xml_content = await self._fetch_rss(url, allow_empty=True)
        items: list[LetterboxdRSSWatchlistItem] = []

        # Empty watchlist returns 404, handle gracefully
        if xml_content is None:
            logger.info("letterboxd_watchlist_empty", username=self.username)
            return items

        try:
            root = ElementTree.fromstring(xml_content)
            channel = root.find("channel")

            if channel is None:
                return items

            for item in channel.findall("item")[:limit]:
                title_elem = item.find("title")
                link_elem = item.find("link")
                pub_date_elem = item.find("pubDate")

                if title_elem is None or title_elem.text is None:
                    continue

                film_title, year = self._parse_film_title(title_elem.text)

                # Try to get poster from description
                poster_url = None
                desc_elem = item.find("description")
                if desc_elem is not None and desc_elem.text:
                    img_match = re.search(r'<img src="([^"]+)"', desc_elem.text)
                    if img_match:
                        poster_url = img_match.group(1)

                film = LetterboxdRSSFilm(
                    title=film_title,
                    year=year,
                    letterboxd_url=link_elem.text
                    if link_elem is not None and link_elem.text
                    else "",
                    poster_url=poster_url,
                )

                added_at = self._parse_date(
                    pub_date_elem.text if pub_date_elem is not None and pub_date_elem.text else ""
                )

                items.append(LetterboxdRSSWatchlistItem(film=film, added_at=added_at))

        except ElementTree.ParseError as e:
            logger.error("letterboxd_rss_parse_error", error=str(e))
            raise LetterboxdRSSError(f"Failed to parse RSS: {e}") from e

        logger.info("letterboxd_watchlist_fetched", count=len(items))
        return items

    async def get_diary(self, limit: int = 10000) -> list[LetterboxdRSSDiaryEntry]:
        """Get user's diary (watch history) from RSS.

        Args:
            limit: Maximum entries to return (default 10000 - all available)

        Returns:
            List of diary entries with ratings
        """
        url = RSS_DIARY.format(base=LETTERBOXD_BASE, username=self.username)
        logger.info("fetching_letterboxd_diary", username=self.username)

        xml_content = await self._fetch_rss(url)
        entries: list[LetterboxdRSSDiaryEntry] = []

        try:
            root = ElementTree.fromstring(xml_content)
            channel = root.find("channel")

            if channel is None:
                return entries

            for item in channel.findall("item")[:limit]:
                title_elem = item.find("title")
                link_elem = item.find("link")
                pub_date_elem = item.find("pubDate")

                if title_elem is None or title_elem.text is None:
                    continue

                raw_title = title_elem.text
                film_title, year = self._parse_film_title(raw_title)
                rating = self._parse_rating(raw_title)

                # Check for rewatch
                rewatch = False
                desc_elem = item.find("description")
                review_snippet = None
                poster_url = None

                if desc_elem is not None and desc_elem.text:
                    desc_text = desc_elem.text
                    rewatch = "Rewatched" in desc_text

                    # Extract poster
                    img_match = re.search(r'<img src="([^"]+)"', desc_text)
                    if img_match:
                        poster_url = img_match.group(1)

                    # Extract review snippet (text after the image/rating)
                    text_match = re.search(r"</p>\s*<p>(.+?)</p>", desc_text, re.DOTALL)
                    if text_match:
                        review_snippet = re.sub(r"<[^>]+>", "", text_match.group(1)).strip()
                        if len(review_snippet) > 200:
                            review_snippet = review_snippet[:200] + "..."

                film = LetterboxdRSSFilm(
                    title=film_title,
                    year=year,
                    letterboxd_url=link_elem.text
                    if link_elem is not None and link_elem.text
                    else "",
                    poster_url=poster_url,
                )

                watched_at = self._parse_date(
                    pub_date_elem.text if pub_date_elem is not None and pub_date_elem.text else ""
                )

                entries.append(
                    LetterboxdRSSDiaryEntry(
                        film=film,
                        watched_at=watched_at,
                        rating=rating,
                        rewatch=rewatch,
                        review_snippet=review_snippet,
                    )
                )

        except ElementTree.ParseError as e:
            logger.error("letterboxd_rss_parse_error", error=str(e))
            raise LetterboxdRSSError(f"Failed to parse RSS: {e}") from e

        logger.info("letterboxd_diary_fetched", count=len(entries))
        return entries

    async def check_user_exists(self) -> bool:
        """Check if the Letterboxd user exists.

        Returns:
            True if user exists
        """
        try:
            url = RSS_DIARY.format(base=LETTERBOXD_BASE, username=self.username)
            response = await self.client.head(url)
            return response.status_code == 200
        except Exception:
            return False


async def sync_letterboxd_to_storage(
    username: str,
    storage,  # BaseStorage
    user_id: int,
    sync_watchlist: bool = True,
    sync_diary: bool = True,
    diary_limit: int = 10000,
) -> dict[str, int]:
    """Sync Letterboxd data to bot storage via RSS.

    Args:
        username: Letterboxd username
        storage: Bot storage instance
        user_id: Internal user ID
        sync_watchlist: Whether to import watchlist
        sync_diary: Whether to import diary/watch history
        diary_limit: Max diary entries to import (default 10000 - all)

    Returns:
        Dict with sync results: {"watchlist_imported": N, "diary_imported": N, ...}
    """
    results = {
        "watchlist_imported": 0,
        "watchlist_skipped": 0,
        "diary_imported": 0,
        "diary_skipped": 0,
    }

    async with LetterboxdRSS(username) as client:
        # Check user exists
        if not await client.check_user_exists():
            raise LetterboxdRSSError(f"User '{username}' not found on Letterboxd")

        # Sync watchlist
        if sync_watchlist:
            watchlist = await client.get_watchlist()

            for item in watchlist:
                # Check if already in watchlist (by title+year since we don't have TMDB ID)
                existing = await storage.get_watchlist(user_id, limit=1000)
                already_exists = any(
                    w.title.lower() == item.film.title.lower() and w.year == item.film.year
                    for w in existing
                )

                if already_exists:
                    results["watchlist_skipped"] += 1
                    continue

                await storage.add_to_watchlist(
                    user_id=user_id,
                    tmdb_id=None,  # RSS doesn't provide TMDB ID
                    media_type="movie",
                    title=item.film.title,
                    year=item.film.year,
                    notes=f"Imported from Letterboxd (@{username})",
                )
                results["watchlist_imported"] += 1

        # Sync diary
        if sync_diary:
            diary = await client.get_diary(limit=diary_limit)

            for entry in diary:
                # Check if already watched
                watched = await storage.get_watched(user_id, limit=1000)
                already_watched = any(
                    w.title.lower() == entry.film.title.lower() and w.year == entry.film.year
                    for w in watched
                )

                if already_watched:
                    results["diary_skipped"] += 1
                    continue

                # Convert Letterboxd rating (0.5-5.0) to bot rating (1-10)
                rating = None
                if entry.rating:
                    rating = entry.rating * 2  # 5.0 -> 10

                await storage.add_watched(
                    user_id=user_id,
                    media_type="movie",
                    title=entry.film.title,
                    tmdb_id=None,
                    year=entry.film.year,
                    rating=rating,
                    review=entry.review_snippet,
                    watched_at=entry.watched_at,
                )
                results["diary_imported"] += 1

    logger.info(
        "letterboxd_sync_complete",
        username=username,
        user_id=user_id,
        results=results,
    )

    return results
