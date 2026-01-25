"""PirateBay torrent search client.

Provides async search functionality for PirateBay torrent site.
Handles parsing of search results, category filtering, and error handling
with automatic fallback to mirrors when the main site is unavailable.

Note: PirateBay has many mirrors that change frequently.
"""

import asyncio
import contextlib
import re
from urllib.parse import quote_plus

import httpx
import structlog
from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

# PirateBay API endpoint (recommended - site requires JavaScript for HTML)
PIRATEBAY_API_URL = "https://apibay.org"

# API retry settings (API is flaky, returns 502 sometimes)
API_MAX_RETRIES = 3
API_RETRY_DELAY = 0.5  # seconds

# PirateBay mirrors (frequently change, ordered by reliability)
# Note: Most mirrors now require JavaScript to render results
PIRATEBAY_MIRRORS = [
    "https://thepiratebay.org",
    "https://thepiratebay10.org",
    "https://piratebay.live",
    "https://thepiratebay.zone",
    "https://tpb.party",
    "https://pirateproxy.live",
    "https://thehiddenbay.com",
    "https://pirate-bay.info",
    "https://thepiratebay.rocks",
    "https://tpb.tf",
]

# CSS selector patterns for different site layouts
# Each pattern is a tuple of (row_selector, pattern_name)
SELECTOR_PATTERNS = [
    # Pattern 1: Classic table layout
    ("table#searchResult tr", "classic_table"),
    # Pattern 2: Alternative table with tbody
    ("table#searchResult tbody tr", "classic_table_tbody"),
    # Pattern 3: Modern list layout
    ("ol#torrents li", "modern_list"),
    # Pattern 4: Generic table with list class
    ("table.list tr", "generic_table"),
    # Pattern 5: List items container
    ("li.list-entry", "list_entries"),
    # Pattern 6: Search result divs
    ("div.detName", "detname_divs"),
    # Pattern 7: Generic torrent rows
    ("tr[class*='torrent']", "torrent_rows"),
    # Pattern 8: Results container divs
    ("div.results-container div.result", "results_container"),
]

# Default base URL
PIRATEBAY_BASE_URL = PIRATEBAY_MIRRORS[0]

# User agent for requests
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Request timeout in seconds
REQUEST_TIMEOUT = 30.0

# Maximum results to return
MAX_RESULTS = 20

# PirateBay category IDs
CATEGORY_VIDEO = 200  # All Video
CATEGORY_VIDEO_MOVIES = 201
CATEGORY_VIDEO_TV = 205
CATEGORY_VIDEO_HD_MOVIES = 207
CATEGORY_VIDEO_HD_TV = 208


# =============================================================================
# Quality Definitions
# =============================================================================

# Quality patterns for matching in titles
# Using word boundaries to avoid false positives (e.g., DVDRip matching DV)
# Note: 4K and 2160p are unified - both refer to the same quality
QUALITY_PATTERNS = {
    "720p": [r"\b720p\b", r"\b720i\b", r"\bHD[\s._-]*720\b"],
    "1080p": [r"\b1080p\b", r"\b1080i\b", r"\bFull[\s._-]*HD\b", r"\bFHD\b"],
    "4K": [r"\b4K\b", r"\bUHD\b", r"\bUltra[\s._-]*HD\b", r"\b2160p\b", r"\b2160i\b"],
    "HDR": [r"\bHDR10\b", r"\bHDR\b", r"\bDolby[\s._-]*Vision\b"],
}


# =============================================================================
# Exceptions
# =============================================================================


class PirateBayError(Exception):
    """Base exception for PirateBay errors."""

    pass


class PirateBayUnavailableError(PirateBayError):
    """Raised when PirateBay is unavailable or blocked."""

    pass


class PirateBayParseError(PirateBayError):
    """Raised when response parsing fails."""

    pass


# =============================================================================
# Data Models
# =============================================================================


class PirateBayResult(BaseModel):
    """Represents a single PirateBay search result.

    Attributes:
        title: Full title of the torrent.
        size: Human-readable file size (e.g., "4.5 GiB").
        size_bytes: Size in bytes for sorting.
        seeds: Number of seeders.
        leeches: Number of leechers.
        magnet: Magnet link for downloading.
        quality: Detected video quality (if any).
        category: PirateBay category name.
        uploader: Name of the uploader.
        uploaded: Upload date/time string.
    """

    title: str = Field(..., description="Full title of the torrent")
    size: str = Field(..., description="Human-readable file size")
    size_bytes: int = Field(default=0, description="Size in bytes")
    seeds: int = Field(default=0, ge=0, description="Number of seeders")
    leeches: int = Field(default=0, ge=0, description="Number of leechers")
    magnet: str = Field(default="", description="Magnet link")
    quality: str | None = Field(default=None, description="Detected video quality")
    category: str | None = Field(default=None, description="PirateBay category")
    uploader: str | None = Field(default=None, description="Uploader name")
    uploaded: str | None = Field(default=None, description="Upload date")

    def to_display_string(self) -> str:
        """Format result for display to user.

        Returns:
            Formatted string with key information.
        """
        quality_str = f" [{self.quality}]" if self.quality else ""
        seeds_str = f"S:{self.seeds}" if self.seeds > 0 else "S:?"
        return f"{self.title}{quality_str} | {self.size} | {seeds_str}"


# =============================================================================
# Helper Functions
# =============================================================================


def detect_quality(title: str) -> str | None:
    """Detect video quality from title.

    Args:
        title: Torrent title to analyze.

    Returns:
        Quality string if detected, None otherwise.
    """
    title_upper = title.upper()

    for quality, patterns in QUALITY_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, title_upper, re.IGNORECASE):
                return quality

    return None


def parse_size(size_str: str) -> tuple[str, int]:
    """Parse size string to human-readable format and bytes.

    Args:
        size_str: Size string from PirateBay (e.g., "4.37 GiB").

    Returns:
        Tuple of (human-readable size, size in bytes).
    """
    size_str = size_str.strip()

    # Try to parse numeric size with unit
    match = re.match(r"([\d.,]+)\s*(GB|MB|KB|TB|GiB|MiB|KiB|TiB|B)?", size_str, re.IGNORECASE)
    if not match:
        return size_str, 0

    try:
        # Handle both comma and dot as decimal separator
        number = float(match.group(1).replace(",", "."))
        unit = (match.group(2) or "MB").upper()

        # Convert to bytes
        multipliers = {
            "B": 1,
            "KB": 1024,
            "KIB": 1024,
            "MB": 1024**2,
            "MIB": 1024**2,
            "GB": 1024**3,
            "GIB": 1024**3,
            "TB": 1024**4,
            "TIB": 1024**4,
        }

        size_bytes = int(number * multipliers.get(unit, 1024**2))
        return size_str, size_bytes
    except (ValueError, TypeError):
        return size_str, 0


def extract_magnet_link(element: Tag) -> str:
    """Extract magnet link from a result element.

    Args:
        element: BeautifulSoup Tag element containing magnet link.

    Returns:
        Magnet link string or empty string if not found.
    """
    # Try to find direct magnet link
    magnet_elem = element.select_one('a[href^="magnet:"]')
    if magnet_elem:
        href = magnet_elem.get("href")
        if isinstance(href, str):
            return href
        if isinstance(href, list) and href:
            return href[0]

    return ""


def build_magnet_link(info_hash: str, name: str = "") -> str:
    """Build a magnet link from info hash.

    Args:
        info_hash: BitTorrent info hash (40 hex characters).
        name: Optional display name for the torrent.

    Returns:
        Complete magnet URI.
    """
    # Standard trackers for DHT
    trackers = [
        "udp://tracker.opentrackr.org:1337/announce",
        "udp://open.stealth.si:80/announce",
        "udp://tracker.torrent.eu.org:451/announce",
        "udp://tracker.bittor.pw:1337/announce",
        "udp://public.popcorn-tracker.org:6969/announce",
        "udp://tracker.dler.org:6969/announce",
        "udp://exodus.desync.com:6969/announce",
    ]

    magnet = f"magnet:?xt=urn:btih:{info_hash}"

    if name:
        magnet += f"&dn={quote_plus(name)}"

    for tracker in trackers:
        magnet += f"&tr={quote_plus(tracker)}"

    return magnet


# =============================================================================
# PirateBay Client
# =============================================================================


class PirateBayClient:
    """Async client for searching PirateBay.

    Handles HTTP requests, HTML parsing, and result extraction.
    Supports category filtering and handles various error conditions.

    Example:
        async with PirateBayClient() as client:
            results = await client.search("Dune 2021", min_seeds=5)
            for result in results:
                print(result.title, result.magnet)
    """

    def __init__(
        self,
        base_url: str = PIRATEBAY_BASE_URL,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        """Initialize PirateBay client.

        Args:
            base_url: Base URL for PirateBay (use mirror if needed).
            timeout: Request timeout in seconds.
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "PirateBayClient":
        """Enter async context manager."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context manager."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get the HTTP client, ensuring it's initialized.

        Returns:
            The httpx async client.

        Raises:
            RuntimeError: If client is not initialized (not in context manager).
        """
        if self._client is None:
            raise RuntimeError("Client not initialized. Use 'async with' context manager.")
        return self._client

    async def _fetch_page(self, url: str, params: dict | None = None) -> str:
        """Fetch a page from PirateBay.

        Args:
            url: URL to fetch.
            params: Optional query parameters.

        Returns:
            HTML content of the page.

        Raises:
            PirateBayUnavailableError: If the site is unavailable.
            PirateBayError: For other errors.
        """
        logger.debug("fetching_page", url=url, params=params)

        try:
            response = await self.client.get(url, params=params)
            response.raise_for_status()
            html = response.text

            # Check for common error conditions
            if "Cloudflare" in html and "challenge" in html.lower():
                logger.warning("cloudflare_protection", url=url)
                raise PirateBayUnavailableError(
                    "PirateBay is behind Cloudflare protection. Try a different mirror."
                )

            if (
                "site is currently unreachable" in html.lower()
                or "503 service unavailable" in html.lower()
            ):
                logger.warning("site_unavailable", url=url)
                raise PirateBayUnavailableError(
                    "PirateBay is currently unavailable. Try again later."
                )

            return html

        except httpx.ConnectError as e:
            logger.error("connection_error", url=url, error=str(e))
            raise PirateBayUnavailableError(
                f"Cannot connect to PirateBay. Site may be blocked or down: {e}"
            ) from e
        except httpx.TimeoutException as e:
            logger.error("timeout_error", url=url, error=str(e))
            raise PirateBayUnavailableError(f"Request timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error("http_error", url=url, status=e.response.status_code)
            if e.response.status_code in (403, 503, 520, 521, 522, 523, 524):
                raise PirateBayUnavailableError(
                    f"PirateBay returned error {e.response.status_code}. Try a different mirror."
                ) from e
            raise PirateBayError(f"HTTP error {e.response.status_code}") from e

    async def _search_api(self, query: str, category: str | None = None) -> list[PirateBayResult]:
        """Search using the apibay.org API.

        The API is more reliable than scraping HTML since the main site
        requires JavaScript to render results.

        Args:
            query: Search query.
            category: Optional category filter (not used by API, filtered after).

        Returns:
            List of PirateBayResult objects.

        Raises:
            PirateBayUnavailableError: If API is unavailable.
            PirateBayError: For other errors.
        """
        api_url = f"{PIRATEBAY_API_URL}/q.php"
        params = {"q": query}

        logger.info("searching_piratebay_api", query=query, api_url=api_url)

        # Retry loop for flaky API (502 errors)
        last_error: Exception | None = None
        response = None

        for attempt in range(API_MAX_RETRIES):
            try:
                response = await self.client.get(api_url, params=params)
                response.raise_for_status()
                break  # Success, exit retry loop
            except httpx.HTTPStatusError as e:
                if e.response.status_code in (502, 503, 504) and attempt < API_MAX_RETRIES - 1:
                    logger.warning(
                        "api_retry",
                        attempt=attempt + 1,
                        max_retries=API_MAX_RETRIES,
                        status=e.response.status_code,
                    )
                    last_error = e
                    await asyncio.sleep(API_RETRY_DELAY * (attempt + 1))
                    continue
                # Non-retryable error or last attempt
                logger.error("api_http_error", status=e.response.status_code)
                raise PirateBayUnavailableError(
                    f"PirateBay API returned error {e.response.status_code}"
                ) from e
            except httpx.ConnectError as e:
                logger.error("api_connection_error", error=str(e))
                raise PirateBayUnavailableError(f"Cannot connect to PirateBay API: {e}") from e
            except httpx.TimeoutException as e:
                logger.error("api_timeout_error", error=str(e))
                raise PirateBayUnavailableError(f"API request timed out: {e}") from e
        else:
            # All retries exhausted
            if last_error:
                logger.error("api_all_retries_failed", attempts=API_MAX_RETRIES)
                raise PirateBayUnavailableError(
                    f"PirateBay API failed after {API_MAX_RETRIES} retries"
                ) from last_error

        # Parse response
        if response is None:
            raise PirateBayUnavailableError("No response received from API")

        try:
            data = response.json()
        except Exception as e:
            logger.error("api_json_error", error=str(e))
            raise PirateBayError(f"Failed to parse API response: {e}") from e

        # API returns a list of results
        # If no results, returns: [{"id":"0","name":"No results returned",...}]
        if not data or (len(data) == 1 and data[0].get("id") == "0"):
            logger.info("api_no_results", query=query)
            return []

        results: list[PirateBayResult] = []

        for item in data[:MAX_RESULTS]:
            try:
                # Extract fields from API response
                info_hash = item.get("info_hash", "")
                name = item.get("name", "")
                size_bytes = int(item.get("size", 0))
                seeds = int(item.get("seeders", 0))
                leeches = int(item.get("leechers", 0))
                username = item.get("username", "")
                added = item.get("added", "")
                category_id = item.get("category", "")

                if not name or not info_hash or len(info_hash) != 40:
                    continue

                # Build magnet link from info hash
                magnet = build_magnet_link(info_hash, name)

                # Format size
                size = self._format_size(size_bytes)

                # Detect quality from title
                quality = detect_quality(name)

                # Format upload date
                uploaded = None
                if added and added != "0":
                    with contextlib.suppress(Exception):
                        from datetime import datetime

                        dt = datetime.fromtimestamp(int(added))
                        uploaded = dt.strftime("%Y-%m-%d")

                result = PirateBayResult(
                    title=name,
                    size=size,
                    size_bytes=size_bytes,
                    seeds=seeds,
                    leeches=leeches,
                    magnet=magnet,
                    quality=quality,
                    category=category_id,
                    uploader=username if username else None,
                    uploaded=uploaded,
                )
                results.append(result)

            except (ValueError, KeyError, TypeError) as e:
                logger.warning("failed_to_parse_api_result", error=str(e), item=item)
                continue

        logger.info("api_results_found", count=len(results))
        return results

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format size in bytes to human-readable string.

        Args:
            size_bytes: Size in bytes.

        Returns:
            Human-readable size string.
        """
        if size_bytes <= 0:
            return "N/A"

        units = ["B", "KB", "MB", "GB", "TB"]
        unit_index = 0
        size = float(size_bytes)

        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1

        return f"{size:.2f} {units[unit_index]}"

    def _parse_search_results(self, html: str) -> list[PirateBayResult]:
        """Parse search results from HTML.

        Tries multiple CSS selector patterns to handle different site layouts.

        Args:
            html: HTML content of search results page.

        Returns:
            List of parsed PirateBayResult objects.
        """
        results: list[PirateBayResult] = []
        soup = BeautifulSoup(html, "lxml")
        rows = []
        matched_pattern = None

        # Try each selector pattern until we find results
        for selector, pattern_name in SELECTOR_PATTERNS:
            rows = soup.select(selector)

            # Special handling for detName divs - need to get parent containers
            if pattern_name == "detname_divs" and rows:
                rows = [r.parent.parent for r in rows if r.parent and r.parent.parent]

            if rows:
                matched_pattern = pattern_name
                logger.info(
                    "selector_pattern_matched",
                    pattern=pattern_name,
                    selector=selector,
                    rows_found=len(rows),
                )
                break

        if not rows:
            # Last resort: try to find any links that look like torrent links
            torrent_links = soup.select('a[href*="/torrent/"]')
            if torrent_links:
                # Get unique parent rows
                seen_parents = set()
                for link in torrent_links:
                    parent = link.parent
                    while parent and parent.name not in ("tr", "li", "div"):
                        parent = parent.parent
                    if parent and id(parent) not in seen_parents:
                        seen_parents.add(id(parent))
                        rows.append(parent)
                if rows:
                    matched_pattern = "fallback_torrent_links"
                    logger.info(
                        "selector_pattern_matched",
                        pattern="fallback_torrent_links",
                        rows_found=len(rows),
                    )

        if not rows:
            # Log HTML snippet for debugging
            html_preview = html[:2000] if len(html) > 2000 else html
            logger.warning(
                "no_results_found_with_any_pattern",
                html_length=len(html),
                html_preview=html_preview,
            )

        logger.debug("found_result_rows", count=len(rows), pattern=matched_pattern)

        for row in rows[:MAX_RESULTS]:
            try:
                result = self._parse_result_row(row)
                if result:
                    results.append(result)
            except Exception as e:
                logger.warning("failed_to_parse_row", error=str(e))
                continue

        return results

    def _parse_result_row(self, row: Tag) -> PirateBayResult | None:
        """Parse a single result row.

        Args:
            row: BeautifulSoup Tag element for the result row.

        Returns:
            PirateBayResult if parsing succeeds, None otherwise.
        """
        # Skip header rows
        if row.select_one("th"):
            return None

        # Extract title - try multiple patterns
        title_elem = (
            row.select_one("a.detLink")
            or row.select_one("a.detName")
            or row.select_one('a[href*="/torrent/"]')
            or row.select_one("a.title")
            or row.select_one("div.detName a")
        )

        if not title_elem:
            return None

        title = title_elem.get_text(strip=True)
        if not title:
            return None

        # Extract magnet link
        magnet = extract_magnet_link(row)

        # If no magnet found directly, try to find info hash
        if not magnet:
            # Some mirrors have hash in data attribute
            hash_elem = row.select_one("[data-hash]")
            if hash_elem:
                info_hash_attr = hash_elem.get("data-hash", "")
                # Handle both str and list[str] return types from .get()
                if isinstance(info_hash_attr, list):
                    info_hash = info_hash_attr[0] if info_hash_attr else ""
                else:
                    info_hash = info_hash_attr or ""
                if len(info_hash) == 40:
                    magnet = build_magnet_link(info_hash, title)

        # Extract size from description
        size = "N/A"
        size_bytes = 0

        # Pattern 1: Size in description text
        desc_elem = row.select_one("font.detDesc") or row.select_one("div.detDesc")
        if desc_elem:
            desc_text = desc_elem.get_text()
            # Pattern: "Size 4.37 GiB" or "Size: 4.37 GiB"
            size_match = re.search(
                r"Size[:\s]*([\d.,]+\s*(?:GB|MB|KB|TB|GiB|MiB|KiB|TiB|B))",
                desc_text,
                re.IGNORECASE,
            )
            if size_match:
                size, size_bytes = parse_size(size_match.group(1))

        # Pattern 2: Separate size cell
        if size == "N/A":
            size_elem = row.select_one("td:nth-child(5)") or row.select_one(".size")
            if size_elem:
                size, size_bytes = parse_size(size_elem.get_text(strip=True))

        # Extract seeds/leeches
        seeds = 0
        leeches = 0

        # Pattern 1: Separate cells for seeds/leeches (classic layout)
        cells = row.select("td")
        if len(cells) >= 3:
            # Seeds is usually second to last, leeches last
            with contextlib.suppress(ValueError, IndexError):
                seeds_text = cells[-2].get_text(strip=True)
                seeds = int(seeds_text.replace(",", ""))
            with contextlib.suppress(ValueError, IndexError):
                leeches_text = cells[-1].get_text(strip=True)
                leeches = int(leeches_text.replace(",", ""))

        # Pattern 2: Seeds/leeches in description
        if seeds == 0 and desc_elem:
            desc_text = desc_elem.get_text() if desc_elem else ""
            seeds_match = re.search(r"Seeders[:\s]*(\d+)", desc_text, re.IGNORECASE)
            if seeds_match:
                seeds = int(seeds_match.group(1))
            leeches_match = re.search(r"Leechers[:\s]*(\d+)", desc_text, re.IGNORECASE)
            if leeches_match:
                leeches = int(leeches_match.group(1))

        # Pattern 3: Separate spans for seeds/leeches
        if seeds == 0:
            seeds_elem = row.select_one("td[align='right']:nth-child(3)") or row.select_one(
                ".seeds"
            )
            if seeds_elem:
                with contextlib.suppress(ValueError):
                    seeds = int(seeds_elem.get_text(strip=True).replace(",", ""))

        if leeches == 0:
            leeches_elem = row.select_one("td[align='right']:nth-child(4)") or row.select_one(
                ".leeches"
            )
            if leeches_elem:
                with contextlib.suppress(ValueError):
                    leeches = int(leeches_elem.get_text(strip=True).replace(",", ""))

        # Extract category
        category = None
        cat_elem = row.select_one("td.vertTh a") or row.select_one("a.category")
        if cat_elem:
            category = cat_elem.get_text(strip=True)

        # Extract uploader
        uploader = None
        uploader_elem = row.select_one("a.detDesc") or row.select_one(
            'font.detDesc a[href*="/user/"]'
        )
        if uploader_elem:
            uploader = uploader_elem.get_text(strip=True)

        # Extract upload date
        uploaded = None
        if desc_elem:
            desc_text = desc_elem.get_text()
            date_match = re.search(r"Uploaded\s+([^,]+)", desc_text)
            if date_match:
                uploaded = date_match.group(1).strip()

        # Detect quality from title
        quality = detect_quality(title)

        return PirateBayResult(
            title=title,
            size=size,
            size_bytes=size_bytes,
            seeds=seeds,
            leeches=leeches,
            magnet=magnet,
            quality=quality,
            category=category,
            uploader=uploader,
            uploaded=uploaded,
        )

    async def search(
        self,
        query: str,
        category: str | None = None,
        min_seeds: int = 0,
    ) -> list[PirateBayResult]:
        """Search for torrents on PirateBay.

        Uses the apibay.org API first (more reliable), then falls back to
        HTML scraping if the API is unavailable.

        Args:
            query: Search query (movie/TV show name).
            category: Optional category filter ("video", "movies", "tv").
            min_seeds: Minimum number of seeds required (default 0).

        Returns:
            List of PirateBayResult objects sorted by seeds (descending).

        Raises:
            PirateBayUnavailableError: If site is unavailable.
            PirateBayError: For other errors.
        """
        logger.info(
            "searching_piratebay",
            query=query,
            category=category,
            min_seeds=min_seeds,
        )

        results: list[PirateBayResult] = []

        # Try API first (recommended - HTML scraping doesn't work due to JavaScript)
        try:
            results = await self._search_api(query, category)
            logger.info("search_via_api_success", count=len(results))
        except (PirateBayUnavailableError, PirateBayError) as e:
            logger.warning("api_search_failed_trying_html", error=str(e))

            # Fall back to HTML scraping (likely won't work but try anyway)
            # Determine category ID
            cat_id = 0  # All categories by default
            if category:
                cat_lower = category.lower()
                if cat_lower in ("video", "all_video"):
                    cat_id = CATEGORY_VIDEO
                elif cat_lower in ("movies", "movie", "film"):
                    cat_id = CATEGORY_VIDEO_MOVIES
                elif cat_lower in ("tv", "tv_show", "series"):
                    cat_id = CATEGORY_VIDEO_TV
                elif cat_lower in ("hd_movies", "hd_movie"):
                    cat_id = CATEGORY_VIDEO_HD_MOVIES
                elif cat_lower in ("hd_tv", "hd_series"):
                    cat_id = CATEGORY_VIDEO_HD_TV

            # Build search URL
            encoded_query = quote_plus(query)
            search_url = f"{self.base_url}/search/{encoded_query}/0/7/{cat_id}"

            # Fetch and parse HTML
            html = await self._fetch_page(search_url)
            results = self._parse_search_results(html)

        logger.info("search_results_found", count=len(results))

        # Apply minimum seeds filter
        if min_seeds > 0:
            results = [r for r in results if r.seeds >= min_seeds]
            logger.info("results_after_seeds_filter", count=len(results))

        # Sort by seeds (descending)
        results.sort(key=lambda r: r.seeds, reverse=True)

        return results


# =============================================================================
# Convenience Functions
# =============================================================================


async def search_piratebay(
    query: str,
    category: str | None = "video",
    min_seeds: int = 0,
) -> list[PirateBayResult]:
    """Search PirateBay for movies/TV shows.

    Convenience function that creates a client and performs a search.

    Args:
        query: Search query (movie/TV show name).
        category: Optional category filter (default "video").
        min_seeds: Minimum number of seeds required.

    Returns:
        List of PirateBayResult objects.

    Example:
        results = await search_piratebay("Dune 2021", min_seeds=5)
        for r in results:
            print(f"{r.title} | {r.size} | Seeds: {r.seeds}")
            print(f"Magnet: {r.magnet}")
    """
    async with PirateBayClient() as client:
        return await client.search(query, category=category, min_seeds=min_seeds)


async def search_with_fallback(
    query: str,
    category: str | None = "video",
    min_seeds: int = 0,
) -> list[PirateBayResult]:
    """Search PirateBay with automatic mirror fallback.

    Tries multiple mirrors if the primary site is unavailable.

    Args:
        query: Search query.
        category: Optional category filter.
        min_seeds: Minimum number of seeds required.

    Returns:
        List of PirateBayResult objects.

    Raises:
        PirateBayUnavailableError: If all mirrors are unavailable.
    """
    last_error: Exception | None = None

    for mirror in PIRATEBAY_MIRRORS:
        try:
            logger.info("trying_mirror", mirror=mirror)
            async with PirateBayClient(base_url=mirror) as client:
                return await client.search(query, category=category, min_seeds=min_seeds)
        except PirateBayUnavailableError as e:
            logger.warning("mirror_unavailable", mirror=mirror, error=str(e))
            last_error = e
            continue

    raise PirateBayUnavailableError(
        f"All PirateBay mirrors are unavailable. Last error: {last_error}"
    )
