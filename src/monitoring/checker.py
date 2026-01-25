"""Release availability checker.

This module handles checking torrent trackers for monitored releases
and processing found results.

Usage:
    checker = ReleaseChecker()
    found = await checker.check_monitor(monitor)
"""

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog

from src.search.piratebay import PirateBayClient, PirateBayError
from src.search.rutracker import RutrackerClient, RutrackerError
from src.search.torapi import TorAPIClient, TorAPIProvider

logger = structlog.get_logger(__name__)


@dataclass
class FoundRelease:
    """Information about a found release."""

    monitor_id: int
    user_id: int
    title: str
    torrent_title: str
    quality: str
    size: str
    seeds: int
    magnet: str
    source: str  # "rutracker" or "piratebay"


def normalize_quality(quality: str | None) -> str | None:
    """Normalize quality string for comparison.

    Maps 2160p/UHD to 4K for unified matching.

    Args:
        quality: Quality string to normalize

    Returns:
        Normalized quality string or None
    """
    if not quality:
        return None

    quality_upper = quality.upper()

    # Unify 4K/2160p variants
    if any(q in quality_upper for q in ["2160", "UHD", "4K", "ULTRA HD"]):
        return "4K"

    # Standard mappings
    if "1080" in quality_upper:
        return "1080p"
    if "720" in quality_upper:
        return "720p"
    if "HDR" in quality_upper:
        return "HDR"

    return quality


def quality_matches(result_quality: str | None, target_quality: str, title: str) -> bool:
    """Check if result quality matches target quality.

    Args:
        result_quality: Quality detected from the result
        target_quality: Quality the user is looking for
        title: Torrent title (for fallback matching)

    Returns:
        True if quality matches
    """
    target_normalized = normalize_quality(target_quality)
    result_normalized = normalize_quality(result_quality)

    # Direct match after normalization
    if result_normalized and result_normalized == target_normalized:
        return True

    # Also check if target quality string appears in title
    # (handles cases where quality detection failed)
    title_upper = title.upper()
    target_upper = target_quality.upper()

    if target_upper in title_upper:
        return True

    # For 4K targets, also accept 2160p in title
    return bool(target_normalized == "4K" and ("2160" in title_upper or "UHD" in title_upper))


class ReleaseChecker:
    """Checks torrent trackers for monitored releases.

    This class handles:
    - Searching Rutracker and PirateBay for releases
    - Quality filtering based on monitor settings
    - Rate limiting to avoid hammering trackers
    """

    def __init__(
        self,
        rutracker_username: str | None = None,
        rutracker_password: str | None = None,
        min_seeds: int = 5,
        rate_limit_seconds: float = 2.0,
    ):
        """Initialize release checker.

        Args:
            rutracker_username: Rutracker credentials
            rutracker_password: Rutracker password
            min_seeds: Minimum seeds for a valid result
            rate_limit_seconds: Delay between tracker requests
        """
        self._rutracker_username = rutracker_username
        self._rutracker_password = rutracker_password
        self._min_seeds = min_seeds
        self._rate_limit_seconds = rate_limit_seconds
        self._last_request_time: float = 0

    async def _rate_limit(self) -> None:
        """Apply rate limiting between requests."""
        import time

        current_time = time.time()
        elapsed = current_time - self._last_request_time

        if elapsed < self._rate_limit_seconds:
            await asyncio.sleep(self._rate_limit_seconds - elapsed)

        self._last_request_time = time.time()

    def _build_tv_search_query(self, monitor: Any) -> str:
        """Build search query for TV series based on tracking mode.

        Args:
            monitor: Monitor object with title, tracking_mode, season/episode numbers

        Returns:
            Search query string
        """
        title = monitor.title
        tracking_mode = getattr(monitor, "tracking_mode", "season")
        season_number = getattr(monitor, "season_number", None)
        episode_number = getattr(monitor, "episode_number", None)

        if tracking_mode == "episode" and season_number and episode_number:
            # Specific episode: "Breaking Bad S01E05"
            return f"{title} S{season_number:02d}E{episode_number:02d}"
        if season_number:
            # Specific season: "Breaking Bad S01" or "Breaking Bad Season 1"
            # Try both formats for better matching
            return f"{title} S{season_number:02d}"
        # Whole series - just the title
        return title

    async def check_monitor(
        self,
        monitor: Any,  # Monitor model from storage
    ) -> FoundRelease | None:
        """Check if a monitored release is available.

        Args:
            monitor: Monitor object with title, quality, user_id, etc.

        Returns:
            FoundRelease if found, None otherwise
        """
        tracking_mode = getattr(monitor, "tracking_mode", "season")
        season_number = getattr(monitor, "season_number", None)
        episode_number = getattr(monitor, "episode_number", None)

        logger.info(
            "checking_monitor",
            monitor_id=monitor.id,
            title=monitor.title,
            quality=monitor.quality,
            media_type=monitor.media_type,
            tracking_mode=tracking_mode,
            season=season_number,
            episode=episode_number,
        )

        # Determine category based on media_type
        category = "tv_show" if monitor.media_type == "tv" else "movie"

        # Build search query - for TV series, include season/episode info
        if monitor.media_type == "tv":
            search_title = self._build_tv_search_query(monitor)
        else:
            search_title = monitor.title

        # Try Rutracker first
        result = await self._search_rutracker(
            search_title,
            monitor.quality,
            category=category,
        )

        if result:
            return FoundRelease(
                monitor_id=monitor.id,
                user_id=monitor.user_id,
                title=monitor.title,
                torrent_title=result["title"],
                quality=result.get("quality", monitor.quality),
                size=result["size"],
                seeds=result["seeds"],
                magnet=result["magnet"],
                source="rutracker",
            )

        # Fallback to PirateBay
        result = await self._search_piratebay(
            search_title,
            monitor.quality,
        )

        if result:
            return FoundRelease(
                monitor_id=monitor.id,
                user_id=monitor.user_id,
                title=monitor.title,
                torrent_title=result["title"],
                quality=result.get("quality", monitor.quality),
                size=result["size"],
                seeds=result["seeds"],
                magnet=result["magnet"],
                source="piratebay",
            )

        return None

    async def _search_rutracker(
        self,
        title: str,
        quality: str,
        category: str = "movie",
    ) -> dict[str, Any] | None:
        """Search Rutracker for a release.

        Tries TorAPI first (more reliable), then falls back to direct scraping.

        Args:
            title: Title to search for
            quality: Desired quality (1080p, 4K, etc.)
            category: Content category (movie, tv_show)

        Returns:
            Best matching result or None
        """
        await self._rate_limit()

        # Try TorAPI first (more reliable, no auth needed)
        try:
            async with TorAPIClient() as torapi:
                results = await torapi.search(title, TorAPIProvider.RUTRACKER, quality)

                if results:
                    # Filter by seeds
                    valid_results = [r for r in results if r.seeds >= self._min_seeds]

                    if valid_results:
                        best = max(valid_results, key=lambda r: r.seeds)

                        logger.info(
                            "torapi_monitor_match_found",
                            title=title,
                            torrent_title=best.name,
                            seeds=best.seeds,
                        )

                        return {
                            "title": best.name,
                            "size": best.size,
                            "seeds": best.seeds,
                            "quality": best.quality,
                            "magnet": best.magnet,
                        }
        except Exception as e:
            logger.warning("torapi_search_error", title=title, error=str(e))

        # Fallback to direct Rutracker client
        try:
            async with RutrackerClient(
                username=self._rutracker_username,
                password=self._rutracker_password,
            ) as client:
                results = await client.search(
                    title,
                    quality=quality,
                    category=category,
                )

                if not results:
                    return None

                # Filter by quality and seeds
                valid_results = [
                    r
                    for r in results
                    if r.seeds >= self._min_seeds and quality_matches(r.quality, quality, r.title)
                ]

                if not valid_results:
                    return None

                # Return best result (most seeds)
                best = max(valid_results, key=lambda r: r.seeds)

                logger.info(
                    "rutracker_monitor_match_found",
                    title=title,
                    torrent_title=best.title,
                    seeds=best.seeds,
                )

                return {
                    "title": best.title,
                    "size": best.size,
                    "seeds": best.seeds,
                    "quality": best.quality,
                    "magnet": best.magnet,
                }

        except RutrackerError as e:
            logger.warning("rutracker_search_error", title=title, error=str(e))
            return None

    async def _search_piratebay(
        self,
        title: str,
        quality: str,
    ) -> dict[str, Any] | None:
        """Search PirateBay for a release.

        Args:
            title: Title to search for
            quality: Desired quality

        Returns:
            Best matching result or None
        """
        await self._rate_limit()

        # Build search query with quality
        query = f"{title} {quality}"

        try:
            async with PirateBayClient() as client:
                results = await client.search(query, min_seeds=self._min_seeds)

                if not results:
                    return None

                # Filter for best match by seeds and quality
                valid_results = [
                    r
                    for r in results
                    if r.seeds >= self._min_seeds and quality_matches(r.quality, quality, r.title)
                ]

                if not valid_results:
                    return None

                # Return best result (most seeds)
                best = max(valid_results, key=lambda r: r.seeds)

                logger.info(
                    "piratebay_match_found",
                    title=title,
                    torrent_title=best.title,
                    seeds=best.seeds,
                )

                return {
                    "title": best.title,
                    "size": best.size,
                    "seeds": best.seeds,
                    "quality": best.quality,
                    "magnet": best.magnet,
                }

        except PirateBayError as e:
            logger.warning("piratebay_search_error", title=title, error=str(e))
            return None

    async def check_all_monitors(
        self,
        monitors: list[Any],
    ) -> list[FoundRelease]:
        """Check multiple monitors for availability.

        Args:
            monitors: List of Monitor objects to check

        Returns:
            List of FoundRelease for monitors that were found
        """
        found_releases = []

        for monitor in monitors:
            try:
                result = await self.check_monitor(monitor)
                if result:
                    found_releases.append(result)
            except Exception as e:
                logger.error(
                    "monitor_check_failed",
                    monitor_id=monitor.id,
                    error=str(e),
                )

        return found_releases
