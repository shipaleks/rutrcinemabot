"""TorAPI client for searching torrents via unofficial API.

TorAPI (https://github.com/Lifailon/TorAPI) provides a unified API
for searching across multiple Russian torrent trackers including
RuTracker, Kinozal, RuTor, and NoNameClub.

Advantages over direct scraping:
- No authentication required (API handles it)
- Clean JSON responses (no HTML parsing)
- Works without VPN (API acts as gateway)
- Multiple trackers in one request

Public instance: https://torapi.vercel.app
"""

import re
from dataclasses import dataclass
from enum import Enum

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Public TorAPI instance
TORAPI_BASE_URL = "https://torapi.vercel.app"

# Timeout for API requests
DEFAULT_TIMEOUT = 30.0


class TorAPIProvider(str, Enum):
    """Available torrent providers.

    Note: API endpoints use lowercase names.
    """

    RUTRACKER = "rutracker"
    KINOZAL = "kinozal"
    RUTOR = "rutor"
    NONAMECLUB = "nonameclub"
    ALL = "all"


@dataclass
class TorAPIResult:
    """Search result from TorAPI."""

    name: str
    torrent_id: str
    url: str
    torrent_url: str
    size: str
    size_bytes: int
    seeds: int
    peers: int
    category: str
    date: str
    provider: str
    quality: str | None = None
    magnet: str | None = None

    def to_display_string(self) -> str:
        """Format result for display."""
        quality_str = f"[{self.quality}] " if self.quality else ""
        seeds_str = f"S:{self.seeds}" if self.seeds > 0 else "S:?"
        return f"{quality_str}{self.name} | {self.size} | {seeds_str}"


# Quality detection patterns
# Note: 4K and 2160p are unified - both refer to the same quality
QUALITY_PATTERNS = {
    "4K": [r"4k", r"uhd", r"2160p", r"2160i", r"ultra\s*hd"],
    "1080p": [r"1080p", r"fullhd", r"full\s*hd", r"fhd"],
    "720p": [r"720p", r"hd(?!r)"],
    "HDR": [r"hdr10?\+?", r"dolby\s*vision", r"dv"],
}


def detect_quality(title: str) -> str | None:
    """Detect video quality from title."""
    title_lower = title.lower()
    for quality, patterns in QUALITY_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, title_lower):
                return quality
    return None


def parse_size_to_bytes(size_str: str) -> int:
    """Parse size string to bytes."""
    size_str = size_str.replace("\xa0", " ").strip()
    match = re.match(r"([\d.,]+)\s*(TB|GB|MB|KB|B)", size_str, re.IGNORECASE)
    if not match:
        return 0

    value = float(match.group(1).replace(",", "."))
    unit = match.group(2).upper()

    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(value * multipliers.get(unit, 1))


class TorAPIClient:
    """Client for TorAPI service."""

    def __init__(
        self,
        base_url: str = TORAPI_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        """Initialize TorAPI client.

        Args:
            base_url: TorAPI base URL (default: public Vercel instance).
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "TorAPIClient":
        """Enter async context."""
        self._client = httpx.AsyncClient(
            timeout=self.timeout,
            headers={
                "User-Agent": "MediaConciergeBot/1.0",
                "Accept": "application/json",
            },
        )
        return self

    async def __aexit__(self, *args) -> None:
        """Exit async context."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get HTTP client."""
        if self._client is None:
            raise RuntimeError("Client not initialized. Use async context manager.")
        return self._client

    async def search(
        self,
        query: str,
        provider: TorAPIProvider = TorAPIProvider.RUTRACKER,
        quality: str | None = None,
    ) -> list[TorAPIResult]:
        """Search for torrents.

        Args:
            query: Search query.
            provider: Torrent provider to search (default: RuTracker).
            quality: Optional quality filter (720p, 1080p, 4K, etc.).

        Returns:
            List of search results sorted by seeds.
        """
        logger.info(
            "torapi_search",
            query=query,
            provider=provider.value,
            quality=quality,
        )

        # Build search URL (provider values are already lowercase)
        url = f"{self.base_url}/api/search/title/{provider.value}"

        try:
            response = await self.client.get(url, params={"query": query})
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            logger.error("torapi_request_failed", error=str(e), url=url)
            return []
        except Exception as e:
            logger.error("torapi_parse_failed", error=str(e))
            return []

        # Parse results
        results = []

        # Handle response format (single provider or multiple)
        if provider == TorAPIProvider.ALL:
            # Response is dict with provider keys (e.g., {"RuTracker": [...], "Kinozal": [...]})
            for prov_name, prov_results in data.items():
                if isinstance(prov_results, list):
                    for item in prov_results:
                        result = self._parse_result(item, prov_name)
                        if result:
                            results.append(result)
        else:
            # Single provider - response is a direct list
            items = data if isinstance(data, list) else []
            for item in items:
                result = self._parse_result(item, provider.value)
                if result:
                    results.append(result)

        logger.info("torapi_results_found", count=len(results))

        # Apply quality filter
        if quality and results:
            quality_upper = quality.upper()
            filtered = [
                r
                for r in results
                if (r.quality and r.quality.upper() == quality_upper)
                or quality_upper in r.name.upper()
            ]
            if filtered:
                results = filtered
                logger.info("torapi_quality_filtered", count=len(results))

        # Sort by seeds (descending)
        results.sort(key=lambda r: r.seeds, reverse=True)

        return results[:50]  # Limit results

    def _parse_result(self, item: dict, provider: str) -> TorAPIResult | None:
        """Parse a single result item."""
        try:
            name = item.get("Name", "")
            if not name:
                return None

            size_str = item.get("Size", "0 MB")
            seeds = int(item.get("Seeds", 0) or 0)
            peers = int(item.get("Peers", 0) or 0)

            return TorAPIResult(
                name=name,
                torrent_id=str(item.get("Id", "")),
                url=item.get("Url", ""),
                torrent_url=item.get("Torrent", ""),
                size=size_str,
                size_bytes=parse_size_to_bytes(size_str),
                seeds=seeds,
                peers=peers,
                category=item.get("Category", ""),
                date=item.get("Date", ""),
                provider=provider,
                quality=detect_quality(name),
                magnet=item.get("Magnet"),
            )
        except Exception as e:
            logger.warning("torapi_parse_item_failed", error=str(e))
            return None

    async def get_details(self, torrent_id: str, provider: TorAPIProvider) -> dict | None:
        """Get detailed info including magnet link.

        Args:
            torrent_id: Torrent ID.
            provider: Provider name.

        Returns:
            Detailed torrent info or None.
        """
        url = f"{self.base_url}/api/search/id/{provider.value}"

        try:
            response = await self.client.get(url, params={"id": torrent_id})
            response.raise_for_status()
            return response.json()
        except Exception as e:
            logger.error("torapi_details_failed", error=str(e), torrent_id=torrent_id)
            return None


async def search_torapi(
    query: str,
    provider: TorAPIProvider = TorAPIProvider.RUTRACKER,
    quality: str | None = None,
) -> list[TorAPIResult]:
    """Convenience function to search TorAPI.

    Args:
        query: Search query.
        provider: Torrent provider.
        quality: Optional quality filter.

    Returns:
        List of search results.
    """
    async with TorAPIClient() as client:
        return await client.search(query, provider, quality)
