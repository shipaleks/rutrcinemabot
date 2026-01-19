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
        logger.info(
            "checking_monitor",
            monitor_id=monitor.id,
            title=monitor.title,
            quality=monitor.quality,
        )

        # Try Rutracker first
        result = await self._search_rutracker(
            monitor.title,
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
                source="rutracker",
            )

        # Fallback to PirateBay
        result = await self._search_piratebay(
            monitor.title,
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
    ) -> dict[str, Any] | None:
        """Search Rutracker for a release.

        Args:
            title: Title to search for
            quality: Desired quality (1080p, 4K, etc.)

        Returns:
            Best matching result or None
        """
        await self._rate_limit()

        try:
            async with RutrackerClient(
                username=self._rutracker_username,
                password=self._rutracker_password,
            ) as client:
                results = await client.search(
                    title,
                    quality=quality,
                    category="movie",
                )

                if not results:
                    return None

                # Filter by quality and seeds
                valid_results = [
                    r
                    for r in results
                    if r.seeds >= self._min_seeds
                    and (not r.quality or r.quality == quality or quality in r.title)
                ]

                if not valid_results:
                    return None

                # Return best result (most seeds)
                best = max(valid_results, key=lambda r: r.seeds)

                logger.info(
                    "rutracker_match_found",
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

                # Filter for best match
                valid_results = [r for r in results if r.seeds >= self._min_seeds]

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
