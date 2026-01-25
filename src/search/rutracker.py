"""Rutracker torrent search client.

Provides async search functionality for the Rutracker torrent tracker.
Handles parsing of search results, quality filtering, and error handling
for captcha and access blocks.

Note: Rutracker may require a proxy or VPN from certain regions.
"""

import contextlib
import re
from enum import Enum
from urllib.parse import quote_plus

import httpx
import structlog
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)


# =============================================================================
# Constants
# =============================================================================

# Rutracker base URL (may need to use mirrors in some regions)
RUTRACKER_BASE_URL = "https://rutracker.org"
RUTRACKER_SEARCH_URL = f"{RUTRACKER_BASE_URL}/forum/tracker.php"
RUTRACKER_TOPIC_URL = f"{RUTRACKER_BASE_URL}/forum/viewtopic.php"

# Alternative mirrors for blocked regions
RUTRACKER_MIRRORS = [
    "https://rutracker.org",
    "https://rutracker.net",
    "https://rutracker.nl",
]

# User agent for requests
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Request timeout in seconds
REQUEST_TIMEOUT = 30.0

# Maximum results to return
MAX_RESULTS = 20


# =============================================================================
# Quality Definitions
# =============================================================================


class VideoQuality(str, Enum):
    """Video quality levels for filtering."""

    Q_720P = "720p"
    Q_1080P = "1080p"
    Q_4K = "4K"  # Also matches 2160p, UHD
    Q_HDR = "HDR"


# Quality patterns for matching in titles
# Note: 4K and 2160p are unified - both refer to the same quality
QUALITY_PATTERNS = {
    VideoQuality.Q_720P: [r"720p", r"720i", r"HD\s*720"],
    VideoQuality.Q_1080P: [r"1080p", r"1080i", r"Full\s*HD", r"FHD"],
    VideoQuality.Q_4K: [r"4K", r"UHD", r"Ultra\s*HD", r"2160p", r"2160i"],
    VideoQuality.Q_HDR: [r"HDR", r"HDR10", r"Dolby\s*Vision", r"DV"],
}


# =============================================================================
# Category Definitions
# =============================================================================


class ContentCategory(str, Enum):
    """Content categories for filtering."""

    MOVIE = "movie"
    TV_SHOW = "tv_show"
    ANIME = "anime"
    DOCUMENTARY = "documentary"


# Rutracker forum IDs for different categories
# These are approximate - Rutracker has many sub-forums
CATEGORY_FORUM_IDS = {
    ContentCategory.MOVIE: [
        7,  # Фильмы
        22,  # Наше кино
        187,  # HD Video
        2198,  # HD Video (UHD)
    ],
    ContentCategory.TV_SHOW: [
        9,  # Сериалы
        189,  # HD Сериалы
        2366,  # Зарубежные сериалы
    ],
    ContentCategory.ANIME: [
        33,  # Аниме
    ],
    ContentCategory.DOCUMENTARY: [
        46,  # Документальные
    ],
}


# =============================================================================
# Exceptions
# =============================================================================


class RutrackerError(Exception):
    """Base exception for Rutracker errors."""

    pass


class RutrackerBlockedError(RutrackerError):
    """Raised when Rutracker is blocked or unavailable."""

    pass


class RutrackerCaptchaError(RutrackerError):
    """Raised when captcha is required."""

    pass


class RutrackerAuthError(RutrackerError):
    """Raised when authentication fails or credentials are missing."""

    pass


class RutrackerParseError(RutrackerError):
    """Raised when response parsing fails."""

    pass


# =============================================================================
# Data Models
# =============================================================================


class SearchResult(BaseModel):
    """Represents a single torrent search result.

    Attributes:
        title: Full title of the torrent distribution.
        size: Human-readable file size (e.g., "4.5 GB").
        size_bytes: Size in bytes for sorting.
        seeds: Number of seeders.
        leeches: Number of leechers.
        magnet: Magnet link for downloading.
        topic_id: Rutracker topic ID.
        quality: Detected video quality (if any).
        category: Content category (if detected).
        forum_name: Name of the forum/category on Rutracker.
    """

    title: str = Field(..., description="Full title of the torrent")
    size: str = Field(..., description="Human-readable file size")
    size_bytes: int = Field(default=0, description="Size in bytes")
    seeds: int = Field(default=0, ge=0, description="Number of seeders")
    leeches: int = Field(default=0, ge=0, description="Number of leechers")
    magnet: str = Field(default="", description="Magnet link")
    topic_id: int = Field(..., description="Rutracker topic ID")
    quality: str | None = Field(default=None, description="Detected video quality")
    category: str | None = Field(default=None, description="Content category")
    forum_name: str | None = Field(default=None, description="Forum/category name")

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
                return quality.value

    return None


def parse_size(size_str: str) -> tuple[str, int]:
    """Parse size string to human-readable format and bytes.

    Args:
        size_str: Size string from Rutracker (e.g., "4.37 GB").

    Returns:
        Tuple of (human-readable size, size in bytes).
    """
    size_str = size_str.strip()

    # Try to parse numeric size with unit
    match = re.match(r"([\d.,]+)\s*(GB|MB|KB|TB|GiB|MiB|KiB|TiB)?", size_str, re.IGNORECASE)
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


def extract_magnet_hash(magnet_or_hash: str) -> str:
    """Extract info hash from magnet link or return hash as-is.

    Args:
        magnet_or_hash: Either a magnet link or raw info hash.

    Returns:
        The info hash string.
    """
    if magnet_or_hash.startswith("magnet:"):
        match = re.search(r"btih:([a-fA-F0-9]{40})", magnet_or_hash)
        if match:
            return match.group(1).upper()
    return magnet_or_hash.upper()


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
    ]

    magnet = f"magnet:?xt=urn:btih:{info_hash}"

    if name:
        magnet += f"&dn={quote_plus(name)}"

    for tracker in trackers:
        magnet += f"&tr={quote_plus(tracker)}"

    return magnet


# =============================================================================
# Rutracker Client
# =============================================================================


class RutrackerClient:
    """Async client for searching Rutracker.

    Handles HTTP requests, HTML parsing, and result extraction.
    Supports quality filtering, authentication, and handles various error conditions.

    Example:
        async with RutrackerClient(username="user", password="pass") as client:
            results = await client.search("Dune 2021", quality="1080p")
            for result in results:
                print(result.title, result.magnet)
    """

    def __init__(
        self,
        base_url: str = RUTRACKER_BASE_URL,
        timeout: float = REQUEST_TIMEOUT,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        """Initialize Rutracker client.

        Args:
            base_url: Base URL for Rutracker (use mirror if needed).
            timeout: Request timeout in seconds.
            username: Rutracker username for authentication (optional).
            password: Rutracker password for authentication (optional).
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._username = username
        self._password = password
        self._client: httpx.AsyncClient | None = None
        self._authenticated = False

    @property
    def has_credentials(self) -> bool:
        """Check if credentials are configured."""
        return bool(self._username and self._password)

    async def __aenter__(self) -> "RutrackerClient":
        """Enter async context manager."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
            follow_redirects=False,  # Handle redirects manually to detect login redirects
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit async context manager."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._authenticated = False

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

    async def _login(self) -> bool:
        """Perform login to Rutracker.

        Returns:
            True if login was successful, False otherwise.

        Raises:
            RutrackerAuthError: If credentials are missing or login fails.
            RutrackerCaptchaError: If captcha is required during login.
        """
        if not self.has_credentials:
            raise RutrackerAuthError(
                "Rutracker credentials not configured. "
                "Set RUTRACKER_USERNAME and RUTRACKER_PASSWORD environment variables."
            )

        logger.info("logging_into_rutracker")

        login_url = f"{self.base_url}/forum/login.php"
        login_data = {
            "login_username": self._username,
            "login_password": self._password,
            "login": "Вход",  # Submit button text
        }

        try:
            response = await self.client.post(
                login_url,
                data=login_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": f"{self.base_url}/forum/index.php",
                },
            )

            # Check response for login success indicators
            # Successful login typically redirects to index.php or profile
            if response.status_code in (301, 302, 303):
                location = response.headers.get("location", "")
                # If redirected to login.php again, login failed
                if "login.php" in location:
                    logger.warning("login_failed_redirect_to_login", location=location)
                    raise RutrackerAuthError("Login failed: invalid credentials")
                # Successful login redirects elsewhere
                self._authenticated = True
                logger.info("login_successful")
                return True

            # Check response body for error messages
            html = response.text

            if "captcha" in html.lower() or "капча" in html.lower():
                logger.warning("captcha_required_during_login")
                raise RutrackerCaptchaError(
                    "Captcha required during login. Try again later or use a different IP."
                )

            if "неверный пароль" in html.lower() or "wrong password" in html.lower():
                logger.warning("login_failed_wrong_password")
                raise RutrackerAuthError("Login failed: wrong password")

            if "пользователь не найден" in html.lower() or "user not found" in html.lower():
                logger.warning("login_failed_user_not_found")
                raise RutrackerAuthError("Login failed: user not found")

            # Check for bb_session cookie as success indicator
            cookies = response.cookies
            if "bb_session" in cookies or any("bb_" in name for name in cookies):
                self._authenticated = True
                logger.info("login_successful_cookie_found")
                return True

            # If we got 200 and no error messages, might be successful
            # Check if we're on the main page (logged in state)
            if response.status_code == 200 and (
                "выход" in html.lower() or "logout" in html.lower()
            ):
                self._authenticated = True
                logger.info("login_successful_logout_link_found")
                return True

            logger.warning(
                "login_failed_unknown",
                status_code=response.status_code,
                has_cookies=bool(cookies),
            )
            raise RutrackerAuthError("Login failed: unknown error")

        except httpx.HTTPError as e:
            logger.error("login_http_error", error=str(e))
            raise RutrackerAuthError(f"Login failed due to HTTP error: {e}") from e

    async def _ensure_authenticated(self) -> None:
        """Ensure the client is authenticated before making requests.

        Raises:
            RutrackerAuthError: If authentication fails or credentials are missing.
        """
        if self._authenticated:
            return

        if not self.has_credentials:
            logger.debug("no_credentials_skipping_auth")
            return

        await self._login()

    async def _fetch_page(
        self,
        url: str,
        params: dict | None = None,
        retry_auth: bool = True,
    ) -> str:
        """Fetch a page from Rutracker.

        Args:
            url: URL to fetch.
            params: Optional query parameters.
            retry_auth: Whether to retry with authentication if redirected to login.

        Returns:
            HTML content of the page.

        Raises:
            RutrackerBlockedError: If the site is blocked or unavailable.
            RutrackerCaptchaError: If captcha is required.
            RutrackerAuthError: If authentication is required but fails.
            RutrackerError: For other errors.
        """
        logger.debug("fetching_page", url=url, params=params)

        # Ensure we're authenticated before fetching
        await self._ensure_authenticated()

        try:
            response = await self.client.get(url, params=params)

            # Handle redirects manually to detect login page redirects
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                logger.debug("redirect_detected", location=location)

                # Check if redirected to login page
                if "login.php" in location:
                    logger.warning("redirect_to_login", url=url)

                    if retry_auth and self.has_credentials:
                        # Session expired, try to re-authenticate
                        self._authenticated = False
                        await self._login()
                        # Retry the original request
                        return await self._fetch_page(url, params, retry_auth=False)

                    raise RutrackerAuthError(
                        "Authentication required. Rutracker requires login to search. "
                        "Use /rutracker command to configure your credentials."
                    )

                # Follow other redirects
                redirect_url = location
                if not redirect_url.startswith("http"):
                    redirect_url = f"{self.base_url}{location}"
                response = await self.client.get(redirect_url)

            response.raise_for_status()
            html = response.text

            # Check for common error conditions
            if "captcha" in html.lower() or "капча" in html.lower():
                logger.warning("captcha_required", url=url)
                raise RutrackerCaptchaError(
                    "Captcha required. Try again later or use a different IP."
                )

            if "blocked" in html.lower() or "заблокирован" in html.lower():
                logger.warning("site_blocked", url=url)
                raise RutrackerBlockedError(
                    "Rutracker is blocked in your region. Try using a VPN or proxy."
                )

            # Check if we're on login page (session might have expired)
            is_login_page = (
                "login.php" in url
                or 'name="login_username"' in html
                or "форма входа" in html.lower()
            )
            if is_login_page:
                if retry_auth and self.has_credentials:
                    logger.warning("login_page_detected", url=url)
                    self._authenticated = False
                    await self._login()
                    return await self._fetch_page(url, params, retry_auth=False)
                logger.warning("login_page_no_credentials", url=url)
                raise RutrackerAuthError(
                    "Authentication required. Rutracker requires login to search. "
                    "Use /rutracker command to configure your credentials."
                )

            return html

        except httpx.ConnectError as e:
            logger.error("connection_error", url=url, error=str(e))
            raise RutrackerBlockedError(
                f"Cannot connect to Rutracker. Site may be blocked or down: {e}"
            ) from e
        except httpx.TimeoutException as e:
            logger.error("timeout_error", url=url, error=str(e))
            raise RutrackerError(f"Request timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            logger.error("http_error", url=url, status=e.response.status_code)
            raise RutrackerError(f"HTTP error {e.response.status_code}") from e

    def _parse_search_results(self, html: str) -> list[SearchResult]:
        """Parse search results from HTML.

        Args:
            html: HTML content of search results page.

        Returns:
            List of parsed SearchResult objects.
        """
        results: list[SearchResult] = []
        soup = BeautifulSoup(html, "lxml")

        # Check if search returned "no results" message
        no_results_indicators = [
            "Не найдено",
            "ничего не найдено",
            "Результатов нет",
            "No results",
        ]
        page_text = soup.get_text().lower()
        for indicator in no_results_indicators:
            if indicator.lower() in page_text:
                logger.info("search_page_no_results_message", indicator=indicator)
                return []

        # Find the results table - try multiple selectors
        # Rutracker may use different structures depending on the page/theme
        selectors_to_try = [
            "tr.tCenter.hl-tr",
            "tr.hl-tr",
            "#tor-tbl tbody tr",
            "#tor-tbl tr.hl-tr",
            "table.forumline tr.hl-tr",
            "#search-results tr",
        ]

        rows = []
        used_selector = None
        for selector in selectors_to_try:
            rows = soup.select(selector)
            if rows:
                used_selector = selector
                break

        # Log detailed info for debugging
        logger.info(
            "parse_search_results",
            rows_found=len(rows),
            selector_used=used_selector,
            html_length=len(html),
            has_tor_tbl=bool(soup.select("#tor-tbl")),
            has_tlink=bool(soup.select("a.tLink")),
        )

        for row in rows[:MAX_RESULTS]:
            try:
                result = self._parse_result_row(row)
                if result:
                    results.append(result)
            except Exception as e:
                logger.warning("failed_to_parse_row", error=str(e))
                continue

        return results

    def _parse_result_row(self, row: BeautifulSoup) -> SearchResult | None:
        """Parse a single result row.

        Args:
            row: BeautifulSoup element for the table row.

        Returns:
            SearchResult if parsing succeeds, None otherwise.
        """
        # Extract topic ID and title
        title_elem = row.select_one("a.tLink") or row.select_one("a[data-topic_id]")
        if not title_elem:
            return None

        title = title_elem.get_text(strip=True)
        topic_id_str = title_elem.get("data-topic_id") or ""

        # Try to extract topic ID from href if not in data attribute
        if not topic_id_str:
            href = title_elem.get("href", "")
            match = re.search(r"t=(\d+)", href)
            if match:
                topic_id_str = match.group(1)

        if not topic_id_str:
            return None

        try:
            topic_id = int(topic_id_str)
        except ValueError:
            return None

        # Extract size
        size_elem = row.select_one("td.tor-size") or row.select_one("a.dl-stub")
        size_text = size_elem.get_text(strip=True) if size_elem else "N/A"
        size, size_bytes = parse_size(size_text)

        # Extract seeds/leeches
        seeds = 0
        leeches = 0

        seeds_elem = row.select_one("td.seedmed") or row.select_one("b.seedmed")
        if seeds_elem:
            with contextlib.suppress(ValueError):
                seeds = int(seeds_elem.get_text(strip=True).replace(",", ""))

        leeches_elem = row.select_one("td.leechmed") or row.select_one("b.leechmed")
        if leeches_elem:
            with contextlib.suppress(ValueError):
                leeches = int(leeches_elem.get_text(strip=True).replace(",", ""))

        # Extract forum/category name
        forum_elem = row.select_one("td.f-name a") or row.select_one("a.f")
        forum_name = forum_elem.get_text(strip=True) if forum_elem else None

        # Try to extract magnet/hash
        magnet = ""

        # Look for download link with hash
        dl_elem = row.select_one("a.dl-stub") or row.select_one('a[href*="dl.php"]')
        if dl_elem:
            # The actual magnet needs to be fetched from topic page
            # For now, we'll generate it when the topic is accessed
            pass

        # Detect quality from title
        quality = detect_quality(title)

        return SearchResult(
            title=title,
            size=size,
            size_bytes=size_bytes,
            seeds=seeds,
            leeches=leeches,
            magnet=magnet,
            topic_id=topic_id,
            quality=quality,
            forum_name=forum_name,
        )

    async def get_magnet_link(self, topic_id: int) -> str:
        """Fetch magnet link for a specific topic.

        Args:
            topic_id: Rutracker topic ID.

        Returns:
            Magnet link string.

        Raises:
            RutrackerError: If magnet link cannot be extracted.
        """
        url = f"{self.base_url}/forum/viewtopic.php"
        html = await self._fetch_page(url, params={"t": str(topic_id)})

        soup = BeautifulSoup(html, "lxml")

        # Look for magnet link in page
        magnet_elem = soup.select_one('a[href^="magnet:"]')
        if magnet_elem:
            return magnet_elem.get("href", "")

        # Look for info hash in download button or script
        # Rutracker often includes hash in data attributes or scripts
        dl_btn = soup.select_one('a.dl-stub[href*="dl.php"]')
        if dl_btn:
            # Try to find the topic title for the magnet name
            title_elem = soup.select_one("h1.maintitle a") or soup.select_one("a.tLink")
            title = title_elem.get_text(strip=True) if title_elem else ""

            # Some pages have hash in script
            scripts = soup.select("script")
            for script in scripts:
                script_text = script.get_text()
                match = re.search(r'["\']([a-fA-F0-9]{40})["\']', script_text)
                if match:
                    return build_magnet_link(match.group(1), title)

        # If we still don't have a magnet, try to find hash in any element
        for elem in soup.find_all(attrs={"data-hash": True}):
            hash_val = elem.get("data-hash", "")
            if len(hash_val) == 40:
                title_elem = soup.select_one("h1.maintitle a")
                title = title_elem.get_text(strip=True) if title_elem else ""
                return build_magnet_link(hash_val, title)

        logger.warning("magnet_not_found", topic_id=topic_id)
        raise RutrackerError(f"Could not extract magnet link for topic {topic_id}")

    async def search(
        self,
        query: str,
        quality: str | None = None,
        category: str | None = None,
        fetch_magnets: bool = True,
    ) -> list[SearchResult]:
        """Search for torrents on Rutracker.

        Args:
            query: Search query (movie/TV show name).
            quality: Optional quality filter (720p, 1080p, 4K, etc.).
            category: Optional category filter (movie, tv_show, anime, documentary).
            fetch_magnets: Whether to fetch magnet links for each result.

        Returns:
            List of SearchResult objects sorted by seeds (descending).

        Raises:
            RutrackerBlockedError: If site is blocked.
            RutrackerCaptchaError: If captcha is required.
            RutrackerError: For other errors.
        """
        logger.info(
            "searching_rutracker",
            query=query,
            quality=quality,
            category=category,
        )

        # Build search URL with parameters
        params = {
            "nm": query,  # Search query
            "o": "10",  # Sort by seeds
            "s": "2",  # Sort descending
        }

        # Add category filter if specified
        if category:
            try:
                cat_enum = ContentCategory(category)
                forum_ids = CATEGORY_FORUM_IDS.get(cat_enum, [])
                if forum_ids:
                    # Rutracker uses 'f' parameter for forum filtering
                    params["f"] = ",".join(str(fid) for fid in forum_ids)
            except ValueError:
                logger.warning("unknown_category", category=category)

        # Fetch search results page
        search_url = f"{self.base_url}/forum/tracker.php"
        html = await self._fetch_page(search_url, params=params)

        # Parse results
        results = self._parse_search_results(html)
        logger.info("search_results_found", count=len(results))

        # If no results found and we used category filter, retry without it
        if not results and "f" in params:
            logger.info("retrying_without_category_filter", query=query)
            params_no_filter = {"nm": query, "o": "10", "s": "2"}
            html = await self._fetch_page(search_url, params=params_no_filter)
            results = self._parse_search_results(html)
            logger.info("search_results_after_retry", count=len(results))

        # Apply quality filter
        if quality:
            quality_upper = quality.upper()
            filtered_results = []
            for r in results:
                # Check detected quality
                if r.quality and r.quality.upper() == quality_upper:
                    filtered_results.append(r)
                    continue
                # Check if quality string is in title
                if quality_upper in r.title.upper():
                    filtered_results.append(r)
                    continue
                # Check quality patterns in title for flexible matching
                matched = False
                for q_enum, q_patterns in QUALITY_PATTERNS.items():
                    if q_enum.value.upper() == quality_upper:
                        for pattern in q_patterns:
                            if re.search(pattern, r.title, re.IGNORECASE):
                                filtered_results.append(r)
                                matched = True
                                break
                        break
                if matched:
                    continue
            # If filter removed all results, return unfiltered but log warning
            if filtered_results:
                results = filtered_results
            else:
                logger.warning(
                    "quality_filter_removed_all_results",
                    quality=quality,
                    original_count=len(results),
                )
            logger.info("results_after_quality_filter", count=len(results))

        # Fetch magnet links for results
        if fetch_magnets:
            for result in results:
                if not result.magnet:
                    try:
                        result.magnet = await self.get_magnet_link(result.topic_id)
                    except RutrackerError as e:
                        logger.warning(
                            "failed_to_fetch_magnet",
                            topic_id=result.topic_id,
                            error=str(e),
                        )
                        # Generate a placeholder magnet if we can't fetch
                        # This allows the user to still see the result

        # Sort by seeds (descending)
        results.sort(key=lambda r: r.seeds, reverse=True)

        return results


# =============================================================================
# Convenience Functions
# =============================================================================


async def search_rutracker(
    query: str,
    quality: str | None = None,
    category: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> list[SearchResult]:
    """Search Rutracker for movies/TV shows.

    Convenience function that creates a client and performs a search.

    Args:
        query: Search query (movie/TV show name).
        quality: Optional quality filter (720p, 1080p, 4K, etc.).
        category: Optional category filter (movie, tv_show, anime, documentary).
        username: Rutracker username for authentication (optional).
        password: Rutracker password for authentication (optional).

    Returns:
        List of SearchResult objects.

    Example:
        results = await search_rutracker("Dune 2021", quality="1080p")
        for r in results:
            print(f"{r.title} | {r.size} | Seeds: {r.seeds}")
            print(f"Magnet: {r.magnet}")
    """
    async with RutrackerClient(username=username, password=password) as client:
        return await client.search(query, quality=quality, category=category)


async def search_with_fallback(
    query: str,
    quality: str | None = None,
    category: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> list[SearchResult]:
    """Search Rutracker with automatic mirror fallback.

    Tries multiple mirrors if the primary site is blocked.

    Args:
        query: Search query.
        quality: Optional quality filter.
        category: Optional category filter.
        username: Rutracker username for authentication (optional).
        password: Rutracker password for authentication (optional).

    Returns:
        List of SearchResult objects.

    Raises:
        RutrackerBlockedError: If all mirrors are blocked.
    """
    last_error: Exception | None = None

    for mirror in RUTRACKER_MIRRORS:
        try:
            logger.info("trying_mirror", mirror=mirror)
            async with RutrackerClient(
                base_url=mirror, username=username, password=password
            ) as client:
                return await client.search(query, quality=quality, category=category)
        except RutrackerBlockedError as e:
            logger.warning("mirror_blocked", mirror=mirror, error=str(e))
            last_error = e
            continue
        except RutrackerCaptchaError:
            # Captcha is site-wide, don't retry other mirrors
            raise

    raise RutrackerBlockedError(
        f"All Rutracker mirrors are blocked or unavailable. Last error: {last_error}"
    )
