"""Industry news RSS parser service.

This module provides RSS feed parsing for film industry news
from sources like Deadline, Variety, and IndieWire.

Usage:
    async with NewsService() as service:
        news = await service.get_relevant_news(
            keywords=["Denis Villeneuve", "A24"],
            hours=24
        )
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

# Default RSS feeds for film industry news
DEFAULT_RSS_FEEDS = [
    {
        "name": "Deadline",
        "url": "https://deadline.com/feed/",
        "category": "film",
    },
    {
        "name": "Variety Film",
        "url": "https://variety.com/v/film/feed/",
        "category": "film",
    },
    {
        "name": "IndieWire",
        "url": "https://www.indiewire.com/feed/",
        "category": "indie",
    },
    {
        "name": "The Hollywood Reporter",
        "url": "https://www.hollywoodreporter.com/feed/",
        "category": "film",
    },
]

# Request timeout
REQUEST_TIMEOUT = 30.0


@dataclass
class NewsItem:
    """A news article from RSS feed."""

    title: str
    link: str
    description: str
    source: str
    published_at: datetime | None
    keywords_matched: list[str]


def parse_rss_date(date_str: str) -> datetime | None:
    """Parse various RSS date formats.

    Args:
        date_str: Date string from RSS feed

    Returns:
        Parsed datetime or None if parsing fails
    """
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",  # RFC 822
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",  # ISO 8601
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            continue

    return None


def extract_text_from_html(html: str) -> str:
    """Extract plain text from HTML content.

    Args:
        html: HTML string

    Returns:
        Plain text with HTML tags removed
    """
    # Remove HTML tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Remove extra whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class NewsService:
    """Service for fetching and filtering film industry news."""

    def __init__(
        self,
        feeds: list[dict[str, str]] | None = None,
    ):
        """Initialize news service.

        Args:
            feeds: List of RSS feed configs. Uses defaults if None.
        """
        self._feeds = feeds or DEFAULT_RSS_FEEDS
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "NewsService":
        """Enter async context manager."""
        self._client = httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; MediaConciergeBot/1.0)",
                "Accept": "application/rss+xml, application/xml, text/xml",
            },
            follow_redirects=True,
        )
        return self

    async def __aexit__(
        self,
        _exc_type: Any,
        _exc_val: Any,
        _exc_tb: Any,
    ) -> None:
        """Exit async context manager."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get HTTP client."""
        if self._client is None:
            raise RuntimeError("NewsService must be used as async context manager")
        return self._client

    async def fetch_feed(self, feed_url: str) -> list[dict[str, Any]]:
        """Fetch and parse an RSS feed.

        Args:
            feed_url: URL of the RSS feed

        Returns:
            List of parsed items with title, link, description, pubDate
        """
        try:
            response = await self.client.get(feed_url)
            response.raise_for_status()

            # Simple XML parsing (avoid heavy dependencies)
            content = response.text
            items = []

            # Extract items using regex (works for most RSS feeds)
            item_pattern = re.compile(
                r"<item[^>]*>(.*?)</item>",
                re.DOTALL | re.IGNORECASE,
            )

            for match in item_pattern.finditer(content):
                item_xml = match.group(1)

                # Extract fields
                title_match = re.search(r"<title[^>]*>(.*?)</title>", item_xml, re.DOTALL)
                link_match = re.search(r"<link[^>]*>(.*?)</link>", item_xml, re.DOTALL)
                desc_match = re.search(
                    r"<description[^>]*>(.*?)</description>", item_xml, re.DOTALL
                )
                date_match = re.search(r"<pubDate[^>]*>(.*?)</pubDate>", item_xml, re.DOTALL)

                if title_match:
                    # Clean CDATA wrappers
                    title = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", title_match.group(1))
                    title = extract_text_from_html(title)

                    link = ""
                    if link_match:
                        link = link_match.group(1).strip()
                        link = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", link)

                    description = ""
                    if desc_match:
                        description = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", desc_match.group(1))
                        description = extract_text_from_html(description)[:500]

                    pub_date = None
                    if date_match:
                        pub_date = parse_rss_date(date_match.group(1))

                    items.append(
                        {
                            "title": title,
                            "link": link,
                            "description": description,
                            "pubDate": pub_date,
                        }
                    )

            logger.debug("feed_fetched", url=feed_url, items_count=len(items))
            return items

        except Exception as e:
            logger.warning("feed_fetch_failed", url=feed_url, error=str(e))
            return []

    async def get_relevant_news(
        self,
        keywords: list[str],
        hours: int = 24,
        max_results: int = 10,
    ) -> list[NewsItem]:
        """Get news items matching keywords from the last N hours.

        Args:
            keywords: List of keywords to match (case-insensitive)
            hours: Look back this many hours
            max_results: Maximum number of results to return

        Returns:
            List of matching news items, sorted by relevance
        """
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        results: list[NewsItem] = []

        for feed_config in self._feeds:
            try:
                items = await self.fetch_feed(feed_config["url"])

                for item in items:
                    # Check date
                    pub_date = item.get("pubDate")
                    if pub_date and pub_date < cutoff:
                        continue

                    # Check keywords
                    text = f"{item['title']} {item['description']}".lower()
                    matched_keywords = [kw for kw in keywords if kw.lower() in text]

                    if matched_keywords:
                        results.append(
                            NewsItem(
                                title=item["title"],
                                link=item["link"],
                                description=item["description"],
                                source=feed_config["name"],
                                published_at=pub_date,
                                keywords_matched=matched_keywords,
                            )
                        )

            except Exception as e:
                logger.warning(
                    "feed_processing_failed",
                    feed=feed_config["name"],
                    error=str(e),
                )

        # Sort by number of matched keywords (descending), then by date
        results.sort(
            key=lambda x: (
                -len(x.keywords_matched),
                -(x.published_at.timestamp() if x.published_at else 0),
            )
        )

        logger.info(
            "relevant_news_found",
            keywords_count=len(keywords),
            results_count=len(results),
        )

        return results[:max_results]

    async def get_all_recent_news(
        self,
        hours: int = 24,
        max_per_feed: int = 5,
    ) -> list[NewsItem]:
        """Get all recent news items without filtering.

        Args:
            hours: Look back this many hours
            max_per_feed: Maximum items per feed

        Returns:
            List of news items
        """
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        results: list[NewsItem] = []

        for feed_config in self._feeds:
            try:
                items = await self.fetch_feed(feed_config["url"])
                count = 0

                for item in items:
                    if count >= max_per_feed:
                        break

                    pub_date = item.get("pubDate")
                    if pub_date and pub_date < cutoff:
                        continue

                    results.append(
                        NewsItem(
                            title=item["title"],
                            link=item["link"],
                            description=item["description"],
                            source=feed_config["name"],
                            published_at=pub_date,
                            keywords_matched=[],
                        )
                    )
                    count += 1

            except Exception as e:
                logger.warning(
                    "feed_processing_failed",
                    feed=feed_config["name"],
                    error=str(e),
                )

        return results
