"""Release availability checker.

This module handles checking torrent trackers for monitored releases
and processing found results.

Key features:
- Multi-query search strategies (episode-specific, season pack, title-only)
- Season pack episode range detection (parses "Серии 1-8 из 10" etc.)
- Multi-provider TorAPI search (Rutracker, Kinozal, RuTor)
- Unified quality matching with BDRip/WEB-DL recognition
- Diagnostic logging for debugging missed detections

Usage:
    checker = ReleaseChecker()
    found = await checker.check_monitor(monitor)
"""

import asyncio
import re
from dataclasses import dataclass, field
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
    source: str  # "rutracker", "kinozal", "rutor", "piratebay"
    is_preliminary: bool = False  # True when quality is acceptable but below target


@dataclass
class CheckDiagnostics:
    """Diagnostic info from a monitor check — why it found/missed."""

    queries_tried: list[str] = field(default_factory=list)
    total_results: int = 0
    quality_rejected: int = 0
    seeds_rejected: int = 0
    season_pack_checked: bool = False
    season_pack_episode_range: str | None = None
    providers_tried: list[str] = field(default_factory=list)
    result: str = "no_results"  # no_results, quality_mismatch, seeds_too_low, found

    def to_dict(self) -> dict[str, Any]:
        return {
            "queries_tried": self.queries_tried,
            "total_results": self.total_results,
            "quality_rejected": self.quality_rejected,
            "seeds_rejected": self.seeds_rejected,
            "season_pack_checked": self.season_pack_checked,
            "season_pack_episode_range": self.season_pack_episode_range,
            "providers_tried": self.providers_tried,
            "result": self.result,
        }


# ---------------------------------------------------------------------------
# Episode range parsing — detects which episodes a season pack contains
# ---------------------------------------------------------------------------

# Patterns for episode ranges in torrent titles (Russian and English)
EPISODE_RANGE_PATTERNS = [
    # "Серии: 1-8 из 10" / "Серии: 1-8 of 10" / "Серии: 01-08"
    r"[Сс]ери[ия][\s:]*(\d+)\s*[-–]\s*(\d+)",
    # "Episodes 1-8" / "Episode 1-8"
    r"[Ee]pisodes?\s*(\d+)\s*[-–]\s*(\d+)",
    # "E01-E08" / "E01E08"
    r"E(\d+)\s*[-–]\s*E?(\d+)",
    # "(1-8 из 10)" / "(1-8 of 10)"
    r"\((\d+)\s*[-–]\s*(\d+)\s*(?:из|of)\s*\d+\)",
    # "01-08 из 10" / "01-08 of 10"  (not in parens)
    r"(\d+)\s*[-–]\s*(\d+)\s*(?:из|of)\s*\d+",
    # "Серии: 1,2,3,4,5" (comma-separated)
    r"[Сс]ери[ия][\s:]*(\d+(?:\s*,\s*\d+)+)",
    # "S01E01-E08" or "S01E01-08"
    r"S\d+E(\d+)\s*[-–]\s*E?(\d+)",
]


def extract_episode_range(title: str) -> tuple[int, int] | None:
    """Extract episode range (first, last) from a torrent title.

    Handles common formats from Russian and international trackers:
    - "Серии: 1-8 из 10"
    - "Episodes 1-8"
    - "E01-E08"
    - "(1-8 of 10)"
    - "Серии: 1,2,3,4,5"

    Args:
        title: Torrent title to parse

    Returns:
        Tuple (first_episode, last_episode) or None if no range found
    """
    for pattern in EPISODE_RANGE_PATTERNS:
        match = re.search(pattern, title)
        if match:
            groups = match.groups()
            if len(groups) == 1:
                # Comma-separated list: "1,2,3,4,5"
                episodes = [int(e.strip()) for e in groups[0].split(",")]
                if episodes:
                    return (min(episodes), max(episodes))
            elif len(groups) >= 2:
                try:
                    first = int(groups[0])
                    last = int(groups[1])
                    if first <= last:
                        return (first, last)
                except (ValueError, IndexError):
                    continue
    return None


def title_contains_episode(title: str, target_episode: int) -> bool:
    """Check if a torrent title (season pack) contains the target episode.

    Args:
        title: Torrent title
        target_episode: Episode number to look for

    Returns:
        True if the season pack contains the target episode
    """
    ep_range = extract_episode_range(title)
    if ep_range is None:
        return False
    first, last = ep_range
    return first <= target_episode <= last


def _title_matches_season(title: str, season_number: int) -> bool:
    """Check if a torrent title refers to the correct season.

    Args:
        title: Torrent title
        season_number: Expected season number

    Returns:
        True if the title matches the season
    """
    patterns = [
        rf"S0?{season_number}\b",
        rf"[Сс]езон[\s:]*0?{season_number}\b",
        rf"[Ss]eason[\s:]*0?{season_number}\b",
    ]
    return any(re.search(pattern, title, re.IGNORECASE) for pattern in patterns)


# ---------------------------------------------------------------------------
# Quality matching — unified across all search backends
# ---------------------------------------------------------------------------

# Extended quality patterns (covers BDRip, WEB-DL, etc.)
QUALITY_ALIASES: dict[str, list[str]] = {
    "4K": ["2160p", "2160i", "uhd", "ultra hd", "4k"],
    "1080p": [
        "1080p",
        "1080i",
        "fullhd",
        "full hd",
        "fhd",
        "bdrip 1080",
        "bdrip",
        "bdremux",
        "blu-ray",
        "bluray",
        "web-dl 1080",
        "webdl 1080",
    ],
    "720p": ["720p", "720i", "hdrip", "web-dl 720", "webdl 720"],
    "HDR": ["hdr10+", "hdr10", "hdr", "dolby vision", "dv"],
}


# Quality tier ordering for comparison (higher = better)
# Used to determine if a found release is "acceptable but below target"
QUALITY_TIERS: dict[str, int] = {
    "720p": 1,
    "1080p": 2,
    "4K": 3,
    "HDR": 4,  # HDR is typically 4K+HDR, so highest tier
}

# Minimum acceptable quality tier — never suggest below this
MIN_ACCEPTABLE_TIER = 2  # 1080p


def get_quality_tier(quality: str | None) -> int:
    """Get numeric tier for a quality string.

    Args:
        quality: Raw or normalized quality string

    Returns:
        Tier number (higher = better), 0 if unknown
    """
    if not quality:
        return 0
    normalized = normalize_quality(quality)
    if normalized:
        return QUALITY_TIERS.get(normalized, 0)
    return 0


def is_acceptable_quality(quality: str | None, title: str = "") -> bool:
    """Check if quality is >= 1080p (acceptable for preliminary notification).

    Args:
        quality: Quality string from result
        title: Torrent title for fallback detection

    Returns:
        True if quality is at least 1080p
    """
    tier = get_quality_tier(quality)
    if tier >= MIN_ACCEPTABLE_TIER:
        return True

    # Fallback: check title for quality indicators >= 1080p
    title_lower = title.lower()
    for target_q in ("1080p", "4K", "HDR"):
        if target_q in QUALITY_ALIASES:
            for alias in QUALITY_ALIASES[target_q]:
                if alias in title_lower:
                    return True

    return False


def is_quality_below_target(found_quality: str | None, target_quality: str) -> bool:
    """Check if found quality is acceptable but below target.

    Args:
        found_quality: Quality of the found release
        target_quality: User's target quality

    Returns:
        True if found is acceptable (>= 1080p) but strictly below target
    """
    found_tier = get_quality_tier(found_quality)
    target_tier = get_quality_tier(target_quality)

    if found_tier == 0 or target_tier == 0:
        return False

    return found_tier < target_tier and found_tier >= MIN_ACCEPTABLE_TIER


def normalize_quality(quality: str | None) -> str | None:
    """Normalize quality string for comparison.

    Maps 2160p/UHD to 4K, BDRip to 1080p, etc.

    Args:
        quality: Quality string to normalize

    Returns:
        Normalized quality string or None
    """
    if not quality:
        return None

    quality_lower = quality.lower().strip()

    for normalized, aliases in QUALITY_ALIASES.items():
        for alias in aliases:
            if alias in quality_lower:
                return normalized

    # Keep original if no match
    return quality


def quality_matches(result_quality: str | None, target_quality: str, title: str) -> bool:
    """Check if result quality matches target quality.

    Uses normalized comparison and title-based fallback matching.

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

    # Check title for quality indicators
    title_lower = title.lower()

    # Check all aliases for the target quality
    if target_normalized and target_normalized in QUALITY_ALIASES:
        for alias in QUALITY_ALIASES[target_normalized]:
            if alias in title_lower:
                return True

    # Also check raw target string in title
    return target_quality.lower() in title_lower


class ReleaseChecker:
    """Checks torrent trackers for monitored releases.

    This class handles:
    - Multi-query search strategies for TV series (episode, season pack, title)
    - Multi-provider search via TorAPI (Rutracker, Kinozal, RuTor)
    - Quality filtering based on monitor settings
    - Season pack episode detection
    - Rate limiting to avoid hammering trackers
    - Diagnostic logging for missed detections
    """

    def __init__(
        self,
        rutracker_username: str | None = None,
        rutracker_password: str | None = None,
        min_seeds: int = 3,
        rate_limit_seconds: float = 2.0,
    ):
        """Initialize release checker.

        Args:
            rutracker_username: Rutracker credentials
            rutracker_password: Rutracker password
            min_seeds: Minimum seeds for a valid result (lowered from 5 to 3)
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

    def _build_tv_search_queries(self, monitor: Any) -> list[str]:
        """Build multiple search queries for TV series, ordered by specificity.

        For episode tracking, generates:
        1. Episode-specific: "Title S01E05"
        2. Season-level: "Title S01" / "Title Season 1"
        3. Title-only: "Title" (broadest, catches any format)

        For season tracking, generates:
        1. Season-level: "Title S01"
        2. Title-only: "Title"

        Args:
            monitor: Monitor object with title, tracking_mode, season/episode numbers

        Returns:
            List of search queries to try in order
        """
        title = monitor.title
        tracking_mode = getattr(monitor, "tracking_mode", "season")
        season_number = getattr(monitor, "season_number", None)
        episode_number = getattr(monitor, "episode_number", None)

        queries = []

        if tracking_mode == "episode" and season_number and episode_number:
            # Try episode-specific first (works well on PirateBay/scene releases)
            queries.append(f"{title} S{season_number:02d}E{episode_number:02d}")
            # Then season-level (finds season packs on Russian trackers)
            queries.append(f"{title} S{season_number:02d}")
            # Then title + season in Russian-style format
            queries.append(f"{title} сезон {season_number}")
        elif season_number:
            queries.append(f"{title} S{season_number:02d}")
            queries.append(f"{title} сезон {season_number}")
        # Always include title-only as last resort
        queries.append(title)

        # Deduplicate while preserving order
        seen = set()
        unique = []
        for q in queries:
            q_lower = q.lower()
            if q_lower not in seen:
                seen.add(q_lower)
                unique.append(q)

        return unique

    async def check_monitor(
        self,
        monitor: Any,
    ) -> FoundRelease | None:
        """Check if a monitored release is available.

        For TV series in episode mode, uses a multi-strategy approach:
        1. Search for specific episode (S01E05)
        2. Search for season pack and check if it contains the episode
        3. Search by title only (broadest)

        Args:
            monitor: Monitor object with title, quality, user_id, etc.

        Returns:
            FoundRelease if found, None otherwise
        """
        tracking_mode = getattr(monitor, "tracking_mode", "season")
        season_number = getattr(monitor, "season_number", None)
        episode_number = getattr(monitor, "episode_number", None)

        diag = CheckDiagnostics()

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

        category = "tv_show" if monitor.media_type == "tv" else "movie"

        # Build search queries
        if monitor.media_type == "tv":
            queries = self._build_tv_search_queries(monitor)
        else:
            queries = [monitor.title]

        is_episode_tracking = (
            tracking_mode == "episode" and season_number is not None and episode_number is not None
        )

        # Try each query across all search backends
        for query in queries:
            diag.queries_tried.append(query)

            # Search TorAPI across multiple providers
            result = await self._search_torapi_multi(
                query,
                monitor.quality,
                category,
                diag,
                episode_number=episode_number if is_episode_tracking else None,
                season_number=season_number if is_episode_tracking else None,
            )
            if result:
                diag.result = "found"
                self._log_diagnostics(monitor, diag)
                return FoundRelease(
                    monitor_id=monitor.id,
                    user_id=monitor.user_id,
                    title=monitor.title,
                    torrent_title=result["title"],
                    quality=result.get("quality", monitor.quality),
                    size=result["size"],
                    seeds=result["seeds"],
                    magnet=result["magnet"],
                    source=result.get("source", "rutracker"),
                )

            # Search direct Rutracker
            result = await self._search_rutracker_direct(
                query,
                monitor.quality,
                category,
                diag,
                episode_number=episode_number if is_episode_tracking else None,
                season_number=season_number if is_episode_tracking else None,
            )
            if result:
                diag.result = "found"
                self._log_diagnostics(monitor, diag)
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

            # Search PirateBay (only for episode-specific or non-TV queries)
            # PirateBay handles S01E05 format well
            if not is_episode_tracking or query == queries[0]:
                result = await self._search_piratebay(query, monitor.quality, diag)
                if result:
                    diag.result = "found"
                    self._log_diagnostics(monitor, diag)
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

        # No exact quality match found.
        # If target quality is above 1080p, try a second pass looking for
        # any acceptable quality (>= 1080p) as a preliminary match.
        target_tier = get_quality_tier(monitor.quality)
        if target_tier > MIN_ACCEPTABLE_TIER and diag.quality_rejected > 0:
            logger.info(
                "trying_preliminary_quality_search",
                monitor_id=monitor.id,
                title=monitor.title,
                target_quality=monitor.quality,
            )

            preliminary = await self._search_preliminary(
                queries,
                monitor,
                category,
                is_episode_tracking,
                episode_number,
                season_number,
            )
            if preliminary:
                diag.result = "found_preliminary"
                self._log_diagnostics(monitor, diag)
                return preliminary

        # Log diagnostic info about why nothing was found
        if diag.total_results > 0:
            if diag.quality_rejected > 0:
                diag.result = "quality_mismatch"
            elif diag.seeds_rejected > 0:
                diag.result = "seeds_too_low"
        self._log_diagnostics(monitor, diag)

        return None

    def _log_diagnostics(self, monitor: Any, diag: CheckDiagnostics) -> None:
        """Log diagnostic info about a monitor check."""
        logger.info(
            "monitor_check_diagnostics",
            monitor_id=monitor.id,
            title=monitor.title,
            result=diag.result,
            queries_tried=diag.queries_tried,
            total_results=diag.total_results,
            quality_rejected=diag.quality_rejected,
            seeds_rejected=diag.seeds_rejected,
            season_pack_checked=diag.season_pack_checked,
            season_pack_episode_range=diag.season_pack_episode_range,
            providers_tried=diag.providers_tried,
        )

    async def _search_preliminary(
        self,
        queries: list[str],
        monitor: Any,
        category: str,
        is_episode_tracking: bool,
        episode_number: int | None,
        season_number: int | None,
    ) -> FoundRelease | None:
        """Search for acceptable quality (>= 1080p) when target quality not found.

        This is a second pass with relaxed quality matching.
        Returns a FoundRelease with is_preliminary=True.

        Args:
            queries: Search queries to try
            monitor: Monitor object
            category: Content category
            is_episode_tracking: Whether tracking specific episode
            episode_number: Target episode number
            season_number: Target season number

        Returns:
            FoundRelease with is_preliminary=True, or None
        """
        # Use a fresh diagnostics — we don't want to mix with the main search
        diag = CheckDiagnostics()

        for query in queries:
            diag.queries_tried.append(query)

            # Search TorAPI with relaxed quality (1080p as minimum acceptable)
            result = await self._search_torapi_multi(
                query,
                "1080p",  # Search for 1080p as minimum acceptable
                category,
                diag,
                episode_number=episode_number if is_episode_tracking else None,
                season_number=season_number if is_episode_tracking else None,
            )
            if result and is_acceptable_quality(result.get("quality"), result.get("title", "")):
                found_quality = normalize_quality(result.get("quality")) or "1080p"
                return FoundRelease(
                    monitor_id=monitor.id,
                    user_id=monitor.user_id,
                    title=monitor.title,
                    torrent_title=result["title"],
                    quality=found_quality,
                    size=result["size"],
                    seeds=result["seeds"],
                    magnet=result["magnet"],
                    source=result.get("source", "rutracker"),
                    is_preliminary=True,
                )

            # Search direct Rutracker with relaxed quality
            result = await self._search_rutracker_direct(
                query,
                "1080p",
                category,
                diag,
                episode_number=episode_number if is_episode_tracking else None,
                season_number=season_number if is_episode_tracking else None,
            )
            if result and is_acceptable_quality(result.get("quality"), result.get("title", "")):
                found_quality = normalize_quality(result.get("quality")) or "1080p"
                return FoundRelease(
                    monitor_id=monitor.id,
                    user_id=monitor.user_id,
                    title=monitor.title,
                    torrent_title=result["title"],
                    quality=found_quality,
                    size=result["size"],
                    seeds=result["seeds"],
                    magnet=result["magnet"],
                    source="rutracker",
                    is_preliminary=True,
                )

        return None

    def _filter_results_for_episode(
        self,
        results: list[Any],
        quality: str,
        episode_number: int | None,
        season_number: int | None,
        diag: CheckDiagnostics,
        get_title: Any = None,
        get_seeds: Any = None,
    ) -> Any | None:
        """Filter search results, considering season packs for episode tracking.

        Args:
            results: Raw search results
            quality: Target quality
            episode_number: Target episode (None for non-episode tracking)
            season_number: Target season (None for non-episode tracking)
            diag: Diagnostics collector
            get_title: Function to extract title from result
            get_seeds: Function to extract seeds from result

        Returns:
            Best matching result or None
        """
        if not results:
            return None

        if get_title is None:
            get_title = lambda r: getattr(r, "name", getattr(r, "title", ""))  # noqa: E731
        if get_seeds is None:
            get_seeds = lambda r: getattr(r, "seeds", 0)  # noqa: E731

        valid_results = []

        for r in results:
            title = get_title(r)
            seeds = get_seeds(r)
            r_quality = getattr(r, "quality", None)

            diag.total_results += 1

            # Check quality
            if not quality_matches(r_quality, quality, title):
                diag.quality_rejected += 1
                continue

            # Check seeds
            if seeds < self._min_seeds:
                diag.seeds_rejected += 1
                continue

            # For episode tracking, verify the result actually contains the episode
            if episode_number is not None and season_number is not None:
                # Check if it's a season pack containing our episode and correct season
                if title_contains_episode(title, episode_number) and _title_matches_season(
                    title, season_number
                ):
                    diag.season_pack_checked = True
                    ep_range = extract_episode_range(title)
                    if ep_range:
                        diag.season_pack_episode_range = f"{ep_range[0]}-{ep_range[1]}"
                    valid_results.append(r)
                    continue

                # Check for direct episode match (S01E05 in title)
                ep_pattern = rf"S0?{season_number}E0?{episode_number}\b"
                if re.search(ep_pattern, title, re.IGNORECASE):
                    valid_results.append(r)
                    continue

                # If title has no episode info at all, it might be a full season
                # Accept it only if it seems like a season pack for the right season
                if extract_episode_range(title) is None and _title_matches_season(
                    title, season_number
                ):
                    # Season pack without explicit episode range — accept it
                    # (likely contains all episodes)
                    valid_results.append(r)
                    continue

                # Title doesn't match our episode criteria — skip
                continue

            # Non-episode tracking: quality and seeds are enough
            valid_results.append(r)

        if not valid_results:
            return None

        # Return best result by seeds
        return max(valid_results, key=get_seeds)

    async def _search_torapi_multi(
        self,
        title: str,
        quality: str,
        category: str,
        diag: CheckDiagnostics,
        episode_number: int | None = None,
        season_number: int | None = None,
    ) -> dict[str, Any] | None:
        """Search TorAPI across multiple providers.

        Tries Rutracker, Kinozal, and RuTor for broader coverage.

        Args:
            title: Search query
            quality: Desired quality
            category: Content category
            diag: Diagnostics collector
            episode_number: Target episode for season pack filtering
            season_number: Target season for season pack filtering

        Returns:
            Best matching result or None
        """
        await self._rate_limit()

        providers = [
            TorAPIProvider.RUTRACKER,
            TorAPIProvider.KINOZAL,
            TorAPIProvider.RUTOR,
        ]

        all_results = []

        try:
            async with TorAPIClient() as torapi:
                for provider in providers:
                    diag.providers_tried.append(f"torapi:{provider.value}")
                    try:
                        # Search WITHOUT quality filter — we do our own matching
                        results = await torapi.search(title, provider)

                        if results:
                            for r in results:
                                # Tag results with provider source
                                r.provider = provider.value
                            all_results.extend(results)

                    except Exception as e:
                        logger.debug(
                            "torapi_provider_error",
                            provider=provider.value,
                            title=title,
                            error=str(e),
                        )
                        continue

        except Exception as e:
            logger.warning("torapi_search_error", title=title, error=str(e))
            return None

        if not all_results:
            return None

        # Use unified filtering (handles episode detection + quality + seeds)
        best = self._filter_results_for_episode(
            all_results,
            quality,
            episode_number,
            season_number,
            diag,
            get_title=lambda r: r.name,
            get_seeds=lambda r: r.seeds,
        )

        if best:
            logger.info(
                "torapi_monitor_match_found",
                title=title,
                torrent_title=best.name,
                seeds=best.seeds,
                provider=best.provider,
                episode_number=episode_number,
                season_pack=diag.season_pack_checked,
            )

            return {
                "title": best.name,
                "size": best.size,
                "seeds": best.seeds,
                "quality": best.quality or quality,
                "magnet": best.magnet,
                "source": best.provider,
            }

        return None

    async def _search_rutracker_direct(
        self,
        title: str,
        quality: str,
        category: str,
        diag: CheckDiagnostics,
        episode_number: int | None = None,
        season_number: int | None = None,
    ) -> dict[str, Any] | None:
        """Search Rutracker directly via scraping.

        Args:
            title: Search query
            quality: Desired quality
            category: Content category
            diag: Diagnostics collector
            episode_number: Target episode for season pack filtering
            season_number: Target season for season pack filtering

        Returns:
            Best matching result or None
        """
        await self._rate_limit()
        diag.providers_tried.append("rutracker_direct")

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

                best = self._filter_results_for_episode(
                    results,
                    quality,
                    episode_number,
                    season_number,
                    diag,
                    get_title=lambda r: r.title,
                    get_seeds=lambda r: r.seeds,
                )

                if best:
                    logger.info(
                        "rutracker_monitor_match_found",
                        title=title,
                        torrent_title=best.title,
                        seeds=best.seeds,
                        episode_number=episode_number,
                        season_pack=diag.season_pack_checked,
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
        diag: CheckDiagnostics,
    ) -> dict[str, Any] | None:
        """Search PirateBay for a release.

        Args:
            title: Search query
            quality: Desired quality
            diag: Diagnostics collector

        Returns:
            Best matching result or None
        """
        await self._rate_limit()
        diag.providers_tried.append("piratebay")

        query = f"{title} {quality}"

        try:
            async with PirateBayClient() as client:
                results = await client.search(query, min_seeds=self._min_seeds)

                if not results:
                    return None

                # PirateBay uses S01E05 format well, no season pack filtering needed
                valid_results = []
                for r in results:
                    diag.total_results += 1
                    if r.seeds < self._min_seeds:
                        diag.seeds_rejected += 1
                        continue
                    if not quality_matches(r.quality, quality, r.title):
                        diag.quality_rejected += 1
                        continue
                    valid_results.append(r)

                if not valid_results:
                    return None

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
                    title=getattr(monitor, "title", "unknown"),
                    error=str(e),
                )

        return found_releases
