"""Natural language conversation handler for the Media Concierge Bot.

This module provides the integration between Telegram messages, Claude AI,
and the various tools (search, media metadata, user profile, seedbox).

It enables natural language queries like "найди Дюну в 4K" to be understood
by Claude, which then uses the appropriate tools to search and return results.
"""

import asyncio
import base64
import contextlib
import json
from pathlib import Path
from typing import Any

import httpx
from lxml import etree
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.ai.claude_client import ClaudeClient, ConversationContext
from src.ai.tools import ToolExecutor, get_tool_definitions
from src.bot.streaming import send_streaming_message
from src.config import settings
from src.logger import get_logger
from src.media.kinopoisk import KinopoiskClient, KinopoiskError
from src.media.omdb import OMDBClient, OMDBError
from src.media.tmdb import TMDBClient, TMDBError
from src.search.piratebay import PirateBayClient, PirateBayError
from src.search.rutracker import RutrackerClient, RutrackerError
from src.seedbox import send_magnet_to_user_seedbox
from src.user.profile import ProfileManager
from src.user.storage import get_storage

logger = get_logger(__name__)

# Default database path
DEFAULT_DB_PATH = Path("data/users.db")

# Store conversation contexts per user
_conversation_contexts: dict[int, ConversationContext] = {}

# Store search results for download callbacks
_search_results_cache: dict[str, dict[str, Any]] = {}
# Track result IDs that were cached/touched in the current request (preserves order)
_current_request_result_ids: list[str] = []


def get_conversation_context(user_id: int) -> ConversationContext:
    """Get or create conversation context for a user.

    Args:
        user_id: Telegram user ID.

    Returns:
        ConversationContext for the user.
    """
    if user_id not in _conversation_contexts:
        _conversation_contexts[user_id] = ConversationContext()
    return _conversation_contexts[user_id]


def clear_conversation_context(user_id: int) -> None:
    """Clear conversation context for a user.

    Args:
        user_id: Telegram user ID.
    """
    if user_id in _conversation_contexts:
        _conversation_contexts[user_id].clear()


def is_valid_magnet(magnet: str | None) -> bool:
    """Check if a magnet link is valid.

    Args:
        magnet: Magnet link to validate.

    Returns:
        True if valid magnet link, False otherwise.
    """
    if not magnet:
        return False
    # Valid magnet must start with "magnet:?" and contain btih (BitTorrent info hash)
    if not magnet.startswith("magnet:?"):
        return False
    if "btih:" not in magnet.lower() and "xt=urn:btih:" not in magnet.lower():
        return False
    # Reject known placeholders
    placeholders = ["PIRATEBAY_MAGNET", "PLACEHOLDER", "UNKNOWN", "N/A"]
    return all(ph not in magnet.upper() for ph in placeholders)


def cache_search_result(result_id: str, result_data: dict[str, Any]) -> None:
    """Cache a search result for later download.

    Args:
        result_id: Unique ID for the result.
        result_data: Result data including magnet link.
    """
    # Validate magnet before caching - clear invalid magnets
    magnet = result_data.get("magnet", "")
    if not is_valid_magnet(magnet):
        result_data["magnet"] = ""

    _search_results_cache[result_id] = result_data
    # Track this ID as touched in the current request (for card sending)
    # Use list to preserve order (first search results first)
    if result_id not in _current_request_result_ids:
        _current_request_result_ids.append(result_id)
    # Keep cache size reasonable
    if len(_search_results_cache) > 1000:
        # Remove oldest entries
        keys = list(_search_results_cache.keys())[:500]
        for key in keys:
            _search_results_cache.pop(key, None)


def get_cached_result(result_id: str) -> dict[str, Any] | None:
    """Get a cached search result.

    Args:
        result_id: Result ID to look up.

    Returns:
        Cached result data or None if not found.
    """
    return _search_results_cache.get(result_id)


def _extract_magnet_hash(magnet: str) -> str | None:
    """Extract info_hash from a magnet link.

    Args:
        magnet: Magnet link string.

    Returns:
        Info hash (40 character hex string) or None if not found.
    """
    import re

    # Magnet format: magnet:?xt=urn:btih:<hash>&...
    match = re.search(r"btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})", magnet)
    if match:
        return match.group(1).upper()
    return None


async def _record_download(
    telegram_id: int,
    result: dict[str, Any],
    magnet: str,
) -> None:
    """Record a download to the database.

    Args:
        telegram_id: Telegram user ID.
        result: Cached search result with title, source, quality, etc.
        magnet: Magnet link used for download.
    """
    try:
        async with get_storage() as storage:
            user = await storage.get_user_by_telegram_id(telegram_id)
            if not user:
                logger.warning("record_download_no_user", telegram_id=telegram_id)
                return

            await storage.add_download(
                user_id=user.id,
                title=result.get("title", "Unknown"),
                tmdb_id=result.get("tmdb_id"),
                media_type=result.get("media_type"),
                season=result.get("season"),
                episode=result.get("episode"),
                quality=result.get("quality"),
                source=result.get("source"),
                magnet_hash=_extract_magnet_hash(magnet),
            )
            logger.info(
                "download_recorded",
                telegram_id=telegram_id,
                title=result.get("title"),
                source=result.get("source"),
            )
    except Exception as e:
        # Don't fail the download operation if recording fails
        logger.warning("record_download_failed", error=str(e), telegram_id=telegram_id)


# =============================================================================
# Tool User ID Resolution Helper
# =============================================================================


async def _resolve_user_id(user_id_input: int | None) -> int | None:
    """Resolve user_id from tool input to internal database user ID.

    Claude provides telegram_user_id as user_id in tool calls.
    This function looks up the internal database user ID.

    Args:
        user_id_input: The user_id from tool input (may be telegram_id)

    Returns:
        Internal database user ID or None if not found
    """
    if user_id_input is None:
        return None

    try:
        async with get_storage() as storage:
            # First try as internal user ID
            user = await storage.get_user(int(user_id_input))
            if user:
                return user.id

            # If not found, try as telegram ID
            user = await storage.get_user_by_telegram_id(int(user_id_input))
            if user:
                return user.id

            return None
    except Exception as e:
        logger.warning("resolve_user_id_failed", user_id_input=user_id_input, error=str(e))
        return None


# =============================================================================
# Tool Handler Implementations
# =============================================================================


async def handle_rutracker_search(
    tool_input: dict[str, Any], telegram_id: int | None = None
) -> str:
    """Handle rutracker_search tool call.

    Uses TorAPI (unofficial API gateway) as primary method for reliability,
    falls back to direct scraping if TorAPI fails.

    Args:
        tool_input: Tool parameters (query, quality, category).
        telegram_id: Telegram user ID to get per-user credentials.

    Returns:
        JSON string with search results.
    """
    query = tool_input.get("query", "")
    quality = tool_input.get("quality")
    category = tool_input.get("category")

    # Auto-apply user's quality preference if not specified by Claude
    if not quality and telegram_id:
        try:
            async with get_storage() as storage:
                db_user = await storage.get_user_by_telegram_id(telegram_id)
                if db_user:
                    prefs = await storage.get_preferences(db_user.id)
                    if prefs and prefs.video_quality:
                        quality = prefs.video_quality
                        logger.info(
                            "rutracker_auto_quality",
                            quality=quality,
                            telegram_id=telegram_id,
                        )
        except Exception as e:
            logger.warning("rutracker_quality_lookup_failed", error=str(e))

    logger.info("rutracker_search", query=query, quality=quality, telegram_id=telegram_id)

    # Try TorAPI first (more reliable, no auth needed)
    try:
        from src.search.torapi import TorAPIClient, TorAPIProvider

        async with TorAPIClient() as torapi:
            results = await torapi.search(query, TorAPIProvider.RUTRACKER, quality)

            if results:
                logger.info("torapi_search_success", count=len(results))
                formatted_results = []
                for result in results[:10]:
                    result_id = f"rt_{hash(result.url) % 100000}"
                    cache_search_result(
                        result_id,
                        {
                            "title": result.name,
                            "magnet": result.magnet or "",
                            "torrent_url": result.torrent_url,
                            "torrent_id": result.torrent_id,
                            "source": "rutracker",
                            "seeds": result.seeds,
                            "size": result.size,
                            "quality": result.quality,
                        },
                    )
                    formatted_results.append(
                        {
                            "title": result.name,
                            "size": result.size,
                            "seeds": result.seeds,
                            "quality": result.quality if result.quality else "unknown",
                        }
                    )

                return json.dumps(
                    {
                        "status": "success",
                        "source": "rutracker",
                        "query": query,
                        "results_count": len(formatted_results),
                        "results": formatted_results,
                    },
                    ensure_ascii=False,
                )
            logger.info("torapi_no_results", query=query)

    except Exception as e:
        logger.warning("torapi_failed_trying_scraping", error=str(e))

    # Fall back to direct scraping
    logger.info("falling_back_to_scraping", query=query)

    # Try to get per-user credentials
    username = None
    password = None

    if telegram_id:
        from src.bot.rutracker_auth import get_user_rutracker_credentials

        username, password = await get_user_rutracker_credentials(telegram_id)
        if username:
            logger.info("using_per_user_credentials", telegram_id=telegram_id)

    # Fall back to global settings if no per-user credentials
    if not username:
        username = settings.rutracker_username
        password = (
            settings.rutracker_password.get_secret_value() if settings.rutracker_password else None
        )

    if not username:
        logger.warning("rutracker_credentials_not_configured", telegram_id=telegram_id)
        return json.dumps(
            {
                "status": "no_results",
                "source": "rutracker",
                "query": query,
                "reason": "no_credentials",
                "suggestion": "TorAPI не вернул результатов, а для прямого поиска нужны credentials. Используй /rutracker для настройки или piratebay_search",
            },
            ensure_ascii=False,
        )

    try:
        async with RutrackerClient(username=username, password=password) as client:
            results = await client.search(query, quality=quality, category=category)
            results = results[:10]  # Limit results

            # Format results for Claude
            formatted_results = []
            for result in results:
                result_id = f"rt_{hash(result.magnet) % 100000}"
                cache_search_result(
                    result_id,
                    {
                        "title": result.title,
                        "magnet": result.magnet,
                        "source": "rutracker",
                        "seeds": result.seeds,
                        "size": result.size,
                        "quality": result.quality,
                    },
                )
                formatted_results.append(
                    {
                        "title": result.title,
                        "size": result.size,
                        "seeds": result.seeds,
                        "quality": result.quality if result.quality else "unknown",
                    }
                )

            # Return different status for empty results
            if not formatted_results:
                logger.info("rutracker_search_no_results", query=query, quality=quality)
                return json.dumps(
                    {
                        "status": "no_results",
                        "source": "rutracker",
                        "query": query,
                        "reason": "not_found",
                        "suggestion": "Попробуй упростить запрос (убрать качество) или использовать piratebay_search с английским названием",
                    },
                    ensure_ascii=False,
                )

            return json.dumps(
                {
                    "status": "success",
                    "source": "rutracker",
                    "query": query,
                    "results_count": len(formatted_results),
                    "results": formatted_results,
                },
                ensure_ascii=False,
            )

    except RutrackerError as e:
        logger.warning("rutracker_search_failed", error=str(e))
        return json.dumps(
            {
                "status": "error",
                "source": "rutracker",
                "error": str(e),
            },
            ensure_ascii=False,
        )


async def handle_piratebay_search(
    tool_input: dict[str, Any], telegram_id: int | None = None
) -> str:
    """Handle piratebay_search tool call.

    Args:
        tool_input: Tool parameters (query, quality, min_seeds).
        telegram_id: Telegram user ID for quality preferences.

    Returns:
        JSON string with search results.
    """
    query = tool_input.get("query", "")
    quality = tool_input.get("quality")
    min_seeds = tool_input.get("min_seeds", 5)

    # Auto-apply user's quality preference if not specified by Claude
    if not quality and telegram_id:
        try:
            async with get_storage() as storage:
                db_user = await storage.get_user_by_telegram_id(telegram_id)
                if db_user:
                    prefs = await storage.get_preferences(db_user.id)
                    if prefs and prefs.video_quality:
                        quality = prefs.video_quality
                        logger.info(
                            "piratebay_auto_quality",
                            quality=quality,
                            telegram_id=telegram_id,
                        )
        except Exception as e:
            logger.warning("piratebay_quality_lookup_failed", error=str(e))

    logger.info("piratebay_search", query=query, quality=quality, min_seeds=min_seeds)

    try:
        async with PirateBayClient() as client:
            results = await client.search(query, min_seeds=min_seeds)
            results = results[:10]  # Limit results

            # Format results for Claude
            formatted_results = []
            for result in results:
                result_id = f"pb_{hash(result.magnet) % 100000}"
                cache_search_result(
                    result_id,
                    {
                        "title": result.title,
                        "magnet": result.magnet,
                        "source": "piratebay",
                        "seeds": result.seeds,
                        "size": result.size,
                        "quality": result.quality,
                    },
                )
                formatted_results.append(
                    {
                        "title": result.title,
                        "size": result.size,
                        "seeds": result.seeds,
                        "quality": result.quality if result.quality else "unknown",
                    }
                )

            # Return different status for empty results
            if not formatted_results:
                logger.info("piratebay_search_no_results", query=query, min_seeds=min_seeds)
                return json.dumps(
                    {
                        "status": "no_results",
                        "source": "piratebay",
                        "query": query,
                        "reason": "not_found",
                        "suggestion": "Контент не найден. Попробуй другое название или уменьшить min_seeds",
                    },
                    ensure_ascii=False,
                )

            return json.dumps(
                {
                    "status": "success",
                    "source": "piratebay",
                    "query": query,
                    "results_count": len(formatted_results),
                    "results": formatted_results,
                },
                ensure_ascii=False,
            )

    except PirateBayError as e:
        logger.warning("piratebay_search_failed", error=str(e))
        return json.dumps(
            {
                "status": "error",
                "source": "piratebay",
                "error": str(e),
            },
            ensure_ascii=False,
        )


async def handle_tmdb_search(tool_input: dict[str, Any]) -> str:
    """Handle tmdb_search tool call.

    Args:
        tool_input: Tool parameters (query, year, media_type, language).

    Returns:
        JSON string with search results.
    """
    query = tool_input.get("query", "")
    year = tool_input.get("year")
    media_type = tool_input.get("media_type")
    language = tool_input.get("language", "ru-RU")

    logger.info("tmdb_search", query=query, year=year, media_type=media_type)

    try:
        async with TMDBClient(language=language) as client:
            if media_type == "movie":
                results = await client.search_movie(query, year=year)
                # Fallback: if year was provided and few results, also search without year
                if year and len(results) <= 2:
                    logger.info(
                        "tmdb_search_augment_without_year",
                        query=query,
                        original_year=year,
                        results_with_year=len(results),
                    )
                    more_results = await client.search_movie(query, year=None)
                    # Merge results, keeping unique IDs (year-filtered first)
                    seen_ids = {r.id for r in results}
                    for r in more_results:
                        if r.id not in seen_ids:
                            results.append(r)
                            seen_ids.add(r.id)
            elif media_type == "tv":
                results = await client.search_tv(query, year=year)
                # Fallback: if year was provided and few results, also search without year
                if year and len(results) <= 2:
                    logger.info(
                        "tmdb_search_augment_without_year",
                        query=query,
                        original_year=year,
                        results_with_year=len(results),
                    )
                    more_results = await client.search_tv(query, year=None)
                    # Merge results, keeping unique IDs (year-filtered first)
                    seen_ids = {r.id for r in results}
                    for r in more_results:
                        if r.id not in seen_ids:
                            results.append(r)
                            seen_ids.add(r.id)
            else:
                results = await client.search_multi(query)

            results = results[:5]  # Limit results

            # Format results for Claude with OMDB ratings
            formatted_results = []
            async with OMDBClient(api_key=settings.omdb_api_key) as omdb_client:
                for result in results:
                    overview = result.overview or ""
                    result_data = {
                        "id": result.id,
                        "title": result.title,
                        "media_type": result.media_type,
                        "year": result.get_year(),
                        "overview": (overview[:200] + "...") if len(overview) > 200 else overview,
                        "vote_average": result.vote_average,
                        "poster_url": result.get_poster_url(),
                    }

                    # Try to fetch OMDB ratings (IMDB, RT, Metascore)
                    try:
                        omdb_result = await omdb_client.search_by_title(
                            result.title, year=result.get_year()
                        )
                        result_data["imdb_rating"] = omdb_result.imdb_rating
                        result_data["rt_rating"] = omdb_client.get_rotten_tomatoes_rating(
                            omdb_result
                        )
                        result_data["metascore"] = omdb_result.metascore
                    except OMDBError:
                        # OMDB not available or movie not found - continue without ratings
                        result_data["imdb_rating"] = None
                        result_data["rt_rating"] = None
                        result_data["metascore"] = None

                    formatted_results.append(result_data)

            return json.dumps(
                {
                    "status": "success",
                    "source": "tmdb",
                    "query": query,
                    "results_count": len(formatted_results),
                    "results": formatted_results,
                },
                ensure_ascii=False,
            )

    except TMDBError as e:
        logger.warning("tmdb_search_failed", error=str(e))
        return json.dumps(
            {
                "status": "error",
                "source": "tmdb",
                "error": str(e),
            },
            ensure_ascii=False,
        )


async def handle_tmdb_person_search(tool_input: dict[str, Any]) -> str:
    """Handle tmdb_person_search tool call.

    Args:
        tool_input: Tool parameters (query).

    Returns:
        JSON string with person search results including TMDB IDs.
    """
    query = tool_input.get("query", "")

    if not query:
        return json.dumps(
            {"status": "error", "error": "query is required"},
            ensure_ascii=False,
        )

    logger.info("tmdb_person_search", query=query)

    try:
        async with TMDBClient() as client:
            results = await client.search_person(query)
            results = results[:5]  # Limit to top 5

            return json.dumps(
                {
                    "status": "success",
                    "source": "tmdb",
                    "results": [
                        {
                            "id": r["id"],
                            "name": r["name"],
                            "known_for": r.get("known_for_department", ""),
                        }
                        for r in results
                    ],
                },
                ensure_ascii=False,
            )

    except TMDBError as e:
        logger.warning("tmdb_person_search_failed", error=str(e))
        return json.dumps(
            {"status": "error", "source": "tmdb", "error": str(e)},
            ensure_ascii=False,
        )


async def handle_tmdb_batch_entity_search(tool_input: dict[str, Any]) -> str:
    """Handle tmdb_batch_entity_search tool call.

    Searches for multiple people, movies, and TV shows in one call.
    Returns TMDB IDs for all found entities.

    Args:
        tool_input: Tool parameters (people, movies, tv_shows arrays).

    Returns:
        JSON string with found TMDB IDs for each entity.
    """
    people = tool_input.get("people", [])
    movies = tool_input.get("movies", [])
    tv_shows = tool_input.get("tv_shows", [])

    logger.info(
        "tmdb_batch_entity_search",
        people_count=len(people),
        movies_count=len(movies),
        tv_shows_count=len(tv_shows),
    )

    results: dict[str, dict[str, Any]] = {"people": {}, "movies": {}, "tv_shows": {}}

    try:
        async with TMDBClient() as client:
            # Search people (limit to 15)
            for name in people[:15]:
                try:
                    search_results = await client.search_person(name)
                    if search_results:
                        results["people"][name] = {
                            "id": search_results[0]["id"],
                            "name": search_results[0].get("name", name),
                        }
                except Exception as e:
                    logger.debug("batch_person_search_skip", name=name, error=str(e))

            # Search movies (limit to 15)
            for title in movies[:15]:
                try:
                    search_results = await client.search_movie(title)
                    if search_results:
                        results["movies"][title] = {
                            "id": search_results[0].id,
                            "title": search_results[0].title,
                        }
                except Exception as e:
                    logger.debug("batch_movie_search_skip", title=title, error=str(e))

            # Search TV shows (limit to 15)
            for title in tv_shows[:15]:
                try:
                    search_results = await client.search_tv(title)
                    if search_results:
                        results["tv_shows"][title] = {
                            "id": search_results[0].id,
                            "name": search_results[0].title,  # SearchResult uses title for both
                        }
                except Exception as e:
                    logger.debug("batch_tv_search_skip", title=title, error=str(e))

        found_count = len(results["people"]) + len(results["movies"]) + len(results["tv_shows"])
        logger.info("tmdb_batch_entity_search_complete", found_count=found_count)

        return json.dumps(
            {"status": "success", "results": results},
            ensure_ascii=False,
        )

    except TMDBError as e:
        logger.warning("tmdb_batch_entity_search_failed", error=str(e))
        return json.dumps(
            {"status": "error", "error": str(e)},
            ensure_ascii=False,
        )


async def _fetch_page_content(
    client: httpx.AsyncClient, url: str, max_chars: int = 800
) -> tuple[str, str]:
    """Fetch and parse page content from URL.

    Args:
        client: httpx async client.
        url: URL to fetch.
        max_chars: Maximum characters to return.

    Returns:
        Tuple of (content, fetch_status).
    """
    from bs4 import BeautifulSoup

    try:
        response = await client.get(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            },
            follow_redirects=True,
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, "lxml")

        # Remove scripts, styles, navigation elements
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()

        # Extract text
        text = soup.get_text(separator=" ", strip=True)

        # Clean up whitespace
        text = " ".join(text.split())

        return text[:max_chars], "success"

    except httpx.TimeoutException:
        return "", "timeout"
    except httpx.HTTPStatusError as e:
        logger.debug("web_fetch_http_error", url=url, status=e.response.status_code)
        return "", f"http_{e.response.status_code}"
    except Exception as e:
        logger.debug("web_fetch_failed", url=url, error=str(e))
        return "", "failed"


def _extract_text_from_xml_element(elem) -> str:
    """Extract text from XML element, stripping <hlword> tags."""
    if elem is None:
        return ""
    return "".join(elem.itertext()).strip()


def _parse_yandex_xml_results(root, max_results: int) -> list[dict[str, str]]:
    """Parse Yandex XML response to extract search results."""
    results = []
    groups = root.xpath("//group")

    for group in groups[:max_results]:
        doc = group.find("doc")
        if doc is None:
            continue

        url = doc.findtext("url", "")
        if not url:
            continue

        title = _extract_text_from_xml_element(doc.find("title"))

        snippet = ""
        passages = doc.find("passages")
        if passages is not None:
            snippet = _extract_text_from_xml_element(passages.find("passage"))

        results.append({"title": title, "url": url, "snippet": snippet})

    return results


async def handle_web_search(tool_input: dict[str, Any]) -> str:
    """Handle web_search tool call using Yandex Search API with content fetching.

    Searches Yandex and fetches full content from top 3 results
    to provide Claude with actual page content, not just snippets.

    Args:
        tool_input: Tool parameters (query, max_results).

    Returns:
        JSON string with search results including fetched content.
    """
    query = tool_input.get("query", "")
    max_results = tool_input.get("max_results", 5)

    if not query:
        return json.dumps(
            {"status": "error", "error": "query is required"},
            ensure_ascii=False,
        )

    # Check if Yandex Search API is configured
    if not settings.has_yandex_search:
        # Debug: log which values are missing
        has_key = settings.yandex_search_api_key is not None
        has_folder = settings.yandex_search_folder_id is not None
        logger.warning(
            "yandex_search_not_configured",
            has_api_key=has_key,
            has_folder_id=has_folder,
            folder_id_value=settings.yandex_search_folder_id[:10]
            if settings.yandex_search_folder_id
            else None,
        )
        return json.dumps(
            {"status": "error", "error": "Yandex Search API is not configured"},
            ensure_ascii=False,
        )

    logger.info("web_search_yandex", query=query, max_results=max_results)

    try:
        # Call Yandex Search API v2
        # These are guaranteed to be non-None by has_yandex_search check above
        assert settings.yandex_search_api_key is not None
        assert settings.yandex_search_folder_id is not None
        api_key = settings.yandex_search_api_key.get_secret_value()
        folder_id = settings.yandex_search_folder_id

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://searchapi.api.cloud.yandex.net/v2/web/search",
                headers={
                    "Authorization": f"Api-Key {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "query": {"searchType": "SEARCH_TYPE_RU", "queryText": query},
                    "folderId": folder_id,
                    "responseFormat": "FORMAT_XML",
                },
            )
            response.raise_for_status()

            # Parse response - XML is returned as base64
            response_data = response.json()
            xml_base64 = response_data.get("rawData", "")

            if not xml_base64:
                logger.warning("yandex_search_empty_response", query=query)
                return json.dumps(
                    {"status": "no_results", "query": query, "results": []},
                    ensure_ascii=False,
                )

            # Decode base64 and parse XML
            xml_bytes = base64.b64decode(xml_base64)
            root = etree.fromstring(xml_bytes)

            # Parse search results
            results = _parse_yandex_xml_results(root, max_results)

        formatted_results = []

        # Fetch full content for top 3 results
        top_results = results[:3]
        remaining_results = results[3:]

        if top_results:
            async with httpx.AsyncClient(timeout=10.0) as client:
                # Build fetch tasks only for results with URLs
                url_to_index: dict[str, int] = {}
                fetch_tasks = []
                for i, r in enumerate(top_results):
                    url = r.get("url", "")
                    if url:
                        url_to_index[url] = i
                        fetch_tasks.append(_fetch_page_content(client, url))

                # Fetch all URLs concurrently
                fetch_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

                # Map results back to URLs
                fetch_map: dict[str, tuple[str, str]] = {}
                for url, result in zip(url_to_index.keys(), fetch_results, strict=True):
                    if isinstance(result, BaseException):
                        fetch_map[url] = ("", "exception")
                    else:
                        fetch_map[url] = result

                # Build formatted results
                for r in top_results:
                    url = r.get("url", "")
                    snippet = r.get("snippet", "")

                    if url and url in fetch_map:
                        content, fetch_status = fetch_map[url]
                        if not content:
                            content = snippet  # Fallback to snippet
                    else:
                        content = snippet
                        fetch_status = "no_url" if not url else "skipped"

                    formatted_results.append(
                        {
                            "title": r.get("title", ""),
                            "url": url,
                            "snippet": snippet,
                            "content": content,
                            "fetch_status": fetch_status,
                        }
                    )

                    logger.debug(
                        "web_search_fetch",
                        url=url[:50] if url else "",
                        fetch_status=fetch_status,
                        content_length=len(content),
                    )

        # Add remaining results without fetching
        for r in remaining_results:
            formatted_results.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("snippet", ""),
                }
            )

        logger.info(
            "web_search_yandex_complete",
            query=query,
            results_count=len(formatted_results),
            fetched_count=len(top_results),
        )

        return json.dumps(
            {
                "status": "success",
                "query": query,
                "results_count": len(formatted_results),
                "results": formatted_results,
            },
            ensure_ascii=False,
        )

    except httpx.HTTPStatusError as e:
        logger.warning(
            "yandex_search_http_error",
            query=query,
            status=e.response.status_code,
            body=e.response.text[:200],
        )
        return json.dumps(
            {"status": "error", "error": f"Yandex Search API error: {e.response.status_code}"},
            ensure_ascii=False,
        )
    except Exception as e:
        logger.warning("web_search_failed", query=query, error=str(e))
        return json.dumps(
            {"status": "error", "error": f"Web search failed: {str(e)}"},
            ensure_ascii=False,
        )


async def handle_tmdb_credits(tool_input: dict[str, Any]) -> str:
    """Handle tmdb_credits tool call.

    Args:
        tool_input: Tool parameters (tmdb_id, media_type).

    Returns:
        JSON string with credits information.
    """
    tmdb_id = tool_input.get("tmdb_id")
    media_type = tool_input.get("media_type", "movie")

    if tmdb_id is None:
        return json.dumps(
            {
                "status": "error",
                "error": "tmdb_id is required",
            },
            ensure_ascii=False,
        )

    logger.info("tmdb_credits", tmdb_id=tmdb_id, media_type=media_type)

    try:
        async with TMDBClient() as client:
            credits = await client.get_credits(int(tmdb_id), media_type)

            # Format credits for Claude
            directors = credits.get_directors()
            writers = credits.get_writers()
            top_cast = credits.get_top_cast(5)

            return json.dumps(
                {
                    "status": "success",
                    "source": "tmdb",
                    "tmdb_id": tmdb_id,
                    "directors": [{"id": d.id, "name": d.name} for d in directors],
                    "writers": [{"id": w.id, "name": w.name} for w in writers],
                    "cast": [
                        {"id": p.id, "name": p.name, "character": p.character} for p in top_cast
                    ],
                },
                ensure_ascii=False,
            )

    except TMDBError as e:
        logger.warning("tmdb_credits_failed", error=str(e))
        return json.dumps(
            {
                "status": "error",
                "source": "tmdb",
                "error": str(e),
            },
            ensure_ascii=False,
        )


async def handle_tmdb_tv_details(tool_input: dict[str, Any]) -> str:
    """Handle tmdb_tv_details tool call.

    Returns detailed TV show info including seasons and next episode.

    Args:
        tool_input: Tool parameters (tmdb_id).

    Returns:
        JSON string with TV show details.
    """
    tmdb_id = tool_input.get("tmdb_id")

    if tmdb_id is None:
        return json.dumps(
            {"status": "error", "error": "tmdb_id is required"},
            ensure_ascii=False,
        )

    logger.info("tmdb_tv_details", tmdb_id=tmdb_id)

    try:
        async with TMDBClient() as client:
            tv_show = await client.get_tv_show(int(tmdb_id))

            result = {
                "status": "success",
                "source": "tmdb",
                "tmdb_id": tmdb_id,
                "name": tv_show.name,
                "show_status": tv_show.status,
                "in_production": tv_show.in_production,
                "number_of_seasons": tv_show.number_of_seasons,
                "number_of_episodes": tv_show.number_of_episodes,
                "first_air_date": tv_show.first_air_date,
                "last_air_date": tv_show.last_air_date,
            }

            # Add next episode info if available
            if tv_show.next_episode_to_air:
                result["next_episode"] = {
                    "season": tv_show.next_episode_to_air.season_number,
                    "episode": tv_show.next_episode_to_air.episode_number,
                    "air_date": tv_show.next_episode_to_air.air_date,
                    "name": tv_show.next_episode_to_air.name,
                }

            # Add last episode info if available
            if tv_show.last_episode_to_air:
                result["last_episode"] = {
                    "season": tv_show.last_episode_to_air.season_number,
                    "episode": tv_show.last_episode_to_air.episode_number,
                    "air_date": tv_show.last_episode_to_air.air_date,
                    "name": tv_show.last_episode_to_air.name,
                }

            return json.dumps(result, ensure_ascii=False)

    except TMDBError as e:
        logger.warning("tmdb_tv_details_failed", error=str(e))
        return json.dumps(
            {"status": "error", "source": "tmdb", "error": str(e)},
            ensure_ascii=False,
        )


async def handle_kinopoisk_search(tool_input: dict[str, Any]) -> str:
    """Handle kinopoisk_search tool call.

    Args:
        tool_input: Tool parameters (query, year).

    Returns:
        JSON string with search results.
    """
    query = tool_input.get("query", "")
    year = tool_input.get("year")

    logger.info("kinopoisk_search", query=query, year=year)

    try:
        async with KinopoiskClient() as client:
            results = await client.search(query)
            results = results[:5]  # Limit results

            # Filter by year if specified
            if year:
                results = [r for r in results if r.year == year] or results

            # Format results for Claude
            formatted_results = []
            for result in results:
                formatted_results.append(
                    {
                        "kinopoisk_id": result.kinopoisk_id,
                        "title": result.name_ru or result.name_en,
                        "title_en": result.name_en,
                        "year": result.year,
                        "rating_kinopoisk": result.rating_kinopoisk,
                    }
                )

            return json.dumps(
                {
                    "status": "success",
                    "source": "kinopoisk",
                    "query": query,
                    "results_count": len(formatted_results),
                    "results": formatted_results,
                },
                ensure_ascii=False,
            )

    except KinopoiskError as e:
        logger.warning("kinopoisk_search_failed", error=str(e))
        return json.dumps(
            {
                "status": "error",
                "source": "kinopoisk",
                "error": str(e),
            },
            ensure_ascii=False,
        )


async def handle_get_user_profile(tool_input: dict[str, Any]) -> str:
    """Handle get_user_profile tool call.

    Args:
        tool_input: Tool parameters (user_id).

    Returns:
        JSON string with user profile and preferences.
    """
    user_id = tool_input.get("user_id")

    if user_id is None:
        return json.dumps(
            {
                "status": "error",
                "error": "user_id is required",
            },
            ensure_ascii=False,
        )

    logger.info("get_user_profile", user_id=user_id)

    try:
        encryption_key = None
        if settings.encryption_key:
            encryption_key = settings.encryption_key.get_secret_value()

        async with get_storage(encryption_key) as storage:
            user = await storage.get_user_by_telegram_id(int(user_id))
            if not user:
                return json.dumps(
                    {
                        "status": "not_found",
                        "message": "User profile not found",
                    },
                    ensure_ascii=False,
                )

            preferences = await storage.get_preferences(user.id)

            return json.dumps(
                {
                    "status": "success",
                    "user": {
                        "telegram_id": user.telegram_id,
                        "username": user.username,
                        "first_name": user.first_name,
                    },
                    "preferences": {
                        "quality": preferences.video_quality if preferences else "1080p",
                        "audio_language": preferences.audio_language if preferences else "ru",
                        "genres": preferences.preferred_genres if preferences else [],
                    },
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("get_user_profile_failed", error=str(e))
        return json.dumps(
            {
                "status": "error",
                "error": str(e),
            },
            ensure_ascii=False,
        )


async def handle_seedbox_download(tool_input: dict[str, Any]) -> str:
    """Handle seedbox_download tool call.

    Args:
        tool_input: Tool parameters (magnet, name, user_id).

    Returns:
        JSON string with download status or magnet link.
    """
    magnet = tool_input.get("magnet", "")
    name = tool_input.get("name", "Unknown")
    user_id = tool_input.get("user_id")

    logger.info("seedbox_download", name=name, has_magnet=bool(magnet), user_id=user_id)

    result = await send_magnet_to_user_seedbox(magnet, user_id)

    if result.get("status") == "sent":
        # Track torrent for sync monitoring
        torrent_hash = result.get("hash")
        if torrent_hash and user_id:
            try:
                async with get_storage() as storage:
                    user = await storage.get_user_by_telegram_id(user_id)
                    if user:
                        await storage.track_torrent(
                            user_id=user.id,
                            torrent_hash=torrent_hash,
                            torrent_name=name,
                        )
            except Exception as e:
                logger.warning("track_torrent_failed", error=str(e))

        return json.dumps(
            {
                "status": "success",
                "message": f"Торрент '{name}' добавлен на seedbox",
                "torrent_hash": torrent_hash,
            },
            ensure_ascii=False,
        )

    if result.get("status") == "magnet":
        return json.dumps(
            {
                "status": "not_configured",
                "message": "Seedbox не настроен. Вот magnet-ссылка для ручного скачивания:",
                "magnet": magnet,
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "status": "error",
            "error": result.get("error", "Unknown error"),
            "magnet": magnet,
        },
        ensure_ascii=False,
    )


# =============================================================================
# Extended Tool Handlers (Phase 1-6)
# =============================================================================


async def handle_read_user_profile(tool_input: dict[str, Any]) -> str:
    """Handle read_user_profile tool call.

    Args:
        tool_input: Tool parameters (user_id).

    Returns:
        JSON string with user's markdown profile.
    """
    user_id_input = tool_input.get("user_id")

    if user_id_input is None:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

    # Resolve to internal user ID (Claude passes telegram_id as user_id)
    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("read_user_profile", user_id=user_id, input_id=user_id_input)

    try:
        async with get_storage() as storage:
            profile_manager = ProfileManager(storage)
            profile_md = await profile_manager.get_or_create_profile(user_id)

            return json.dumps(
                {
                    "status": "success",
                    "profile_md": profile_md,
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("read_user_profile_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_update_user_profile(tool_input: dict[str, Any]) -> str:
    """Handle update_user_profile tool call.

    Args:
        tool_input: Tool parameters (user_id, section, content).

    Returns:
        JSON string with update status.
    """
    user_id_input = tool_input.get("user_id")
    section = tool_input.get("section")
    content = tool_input.get("content")

    if not all([user_id_input, section, content]):
        return json.dumps(
            {"status": "error", "error": "user_id, section, and content are required"},
            ensure_ascii=False,
        )

    # Resolve to internal user ID (Claude passes telegram_id as user_id)
    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("update_user_profile", user_id=user_id, section=section, input_id=user_id_input)

    try:
        async with get_storage() as storage:
            profile_manager = ProfileManager(storage)

            if section == "notable_interactions":
                await profile_manager.add_notable_interaction(user_id, content)
            elif section == "conversation_highlights":
                await profile_manager.add_conversation_highlight(user_id, content)
            else:
                await profile_manager.update_section(user_id, section, content)

            return json.dumps({"status": "success", "section": section}, ensure_ascii=False)

    except Exception as e:
        logger.warning("update_user_profile_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


# =============================================================================
# Memory System Tool Handlers (MemGPT-style)
# =============================================================================


async def handle_read_core_memory(tool_input: dict[str, Any]) -> str:
    """Handle read_core_memory tool call.

    Args:
        tool_input: Tool parameters (user_id, block_name optional).

    Returns:
        JSON string with core memory blocks.
    """
    from src.user.memory import CoreMemoryManager

    user_id_input = tool_input.get("user_id")
    block_name = tool_input.get("block_name")

    if user_id_input is None:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("read_core_memory", user_id=user_id, block_name=block_name)

    try:
        async with get_storage() as storage:
            manager = CoreMemoryManager(storage)

            if block_name:
                block = await manager.get_block(user_id, block_name)
                if block is None:
                    return json.dumps(
                        {"status": "not_found", "error": f"Block '{block_name}' not found"},
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "status": "success",
                        "block": {
                            "name": block.block_name,
                            "content": block.content,
                            "max_chars": block.max_chars,
                            "usage_percent": block.usage_percent,
                        },
                    },
                    ensure_ascii=False,
                )
            blocks = await manager.get_all_blocks(user_id)
            return json.dumps(
                {
                    "status": "success",
                    "blocks": [
                        {
                            "name": b.block_name,
                            "content": b.content,
                            "max_chars": b.max_chars,
                            "usage_percent": b.usage_percent,
                        }
                        for b in blocks
                    ],
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("read_core_memory_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


def _normalize_block_name(block_name: str) -> str:
    """Normalize block name to canonical form.

    Maps common variations to standard block names.

    Args:
        block_name: Input block name (may be non-standard).

    Returns:
        Normalized block name.
    """
    if not block_name:
        return block_name

    # Lowercase and strip
    name = block_name.lower().strip()

    # Map common variations
    mappings = {
        # Preferences variations
        "subtitle preferences": "preferences",
        "video preferences": "preferences",
        "audio preferences": "preferences",
        "quality preferences": "preferences",
        "content preferences": "preferences",
        "prefs": "preferences",
        # Watch context variations
        "watch context": "watch_context",
        "viewing context": "watch_context",
        "equipment": "watch_context",
        "device": "watch_context",
        # Style variations
        "communication style": "style",
        "communication": "style",
        # Instructions variations
        "user instructions": "instructions",
        "rules": "instructions",
        "explicit instructions": "instructions",
        # Blocklist variations
        "block list": "blocklist",
        "blacklist": "blocklist",
        "avoid": "blocklist",
    }

    # Check direct mapping
    if name in mappings:
        return mappings[name]

    # Check if already valid
    valid_names = [
        "preferences",
        "watch_context",
        "active_context",
        "style",
        "instructions",
        "blocklist",
    ]
    if name.replace(" ", "_") in valid_names:
        return name.replace(" ", "_")

    return block_name


async def handle_update_core_memory(tool_input: dict[str, Any]) -> str:
    """Handle update_core_memory tool call.

    Args:
        tool_input: Tool parameters (user_id, block_name, content, operation).

    Returns:
        JSON string with update status.
    """
    from src.user.memory import CoreMemoryManager
    from src.user.storage import CORE_MEMORY_BLOCKS

    user_id_input = tool_input.get("user_id")
    block_name_raw = tool_input.get("block_name", "")
    content = tool_input.get("content")
    operation = tool_input.get("operation", "replace")

    # Normalize block name
    block_name = _normalize_block_name(block_name_raw)
    if block_name != block_name_raw:
        logger.info("block_name_normalized", original=block_name_raw, normalized=block_name)

    if not all([user_id_input, block_name, content]):
        return json.dumps(
            {"status": "error", "error": "user_id, block_name, and content are required"},
            ensure_ascii=False,
        )

    # Check if block is agent-editable
    block_config = CORE_MEMORY_BLOCKS.get(block_name)
    if not block_config:
        return json.dumps(
            {"status": "error", "error": f"Unknown block: {block_name}"},
            ensure_ascii=False,
        )

    if not block_config.get("agent_editable", False):
        return json.dumps(
            {
                "status": "error",
                "error": f"Block '{block_name}' is system-managed and cannot be updated by agent",
            },
            ensure_ascii=False,
        )

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info(
        "update_core_memory",
        user_id=user_id,
        block_name=block_name,
        operation=operation,
        content_len=len(content),
    )

    try:
        async with get_storage() as storage:
            manager = CoreMemoryManager(storage)
            block = await manager.update_block(user_id, block_name, content, operation)

            # Invalidate cached context so next message reloads from DB
            # user_id_input is the telegram_id passed by Claude
            telegram_id = user_id_input
            if telegram_id and telegram_id in _conversation_contexts:
                _conversation_contexts[telegram_id].context_loaded = False

            return json.dumps(
                {
                    "status": "success",
                    "block": {
                        "name": block.block_name,
                        "content": block.content,
                        "max_chars": block.max_chars,
                        "usage_percent": block.usage_percent,
                    },
                    "message": f"Block '{block_name}' updated successfully",
                },
                ensure_ascii=False,
            )

    except ValueError as e:
        logger.warning("update_core_memory_invalid", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)
    except Exception as e:
        logger.warning("update_core_memory_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_search_memory_notes(tool_input: dict[str, Any]) -> str:
    """Handle search_memory_notes tool call.

    Args:
        tool_input: Tool parameters (user_id, query, limit).

    Returns:
        JSON string with search results.
    """
    user_id_input = tool_input.get("user_id")
    query = tool_input.get("query")
    limit = tool_input.get("limit", 10)

    if not user_id_input or not query:
        return json.dumps(
            {"status": "error", "error": "user_id and query are required"},
            ensure_ascii=False,
        )

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("search_memory_notes", user_id=user_id, query=query, limit=limit)

    try:
        async with get_storage() as storage:
            notes = await storage.search_memory_notes(user_id, query, limit)

            # Update access counts for found notes
            for note in notes:
                await storage.update_memory_note_access(note.id)

            return json.dumps(
                {
                    "status": "success",
                    "count": len(notes),
                    "notes": [
                        {
                            "id": n.id,
                            "content": n.content,
                            "source": n.source,
                            "keywords": n.keywords,
                            "confidence": n.confidence,
                            "created_at": n.created_at.isoformat(),
                        }
                        for n in notes
                    ],
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("search_memory_notes_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_create_memory_note(tool_input: dict[str, Any]) -> str:
    """Handle create_memory_note tool call.

    Args:
        tool_input: Tool parameters (user_id, content, keywords).

    Returns:
        JSON string with created note.
    """
    user_id_input = tool_input.get("user_id")
    content = tool_input.get("content")
    keywords = tool_input.get("keywords", [])

    if not user_id_input or not content:
        return json.dumps(
            {"status": "error", "error": "user_id and content are required"},
            ensure_ascii=False,
        )

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("create_memory_note", user_id=user_id, content_len=len(content))

    try:
        async with get_storage() as storage:
            note = await storage.create_memory_note(
                user_id=user_id,
                content=content,
                source="conversation",
                keywords=keywords,
                confidence=0.6,  # Default confidence for conversation-based notes
            )

            return json.dumps(
                {
                    "status": "success",
                    "note": {
                        "id": note.id,
                        "content": note.content,
                        "keywords": note.keywords,
                    },
                    "message": "Memory note created successfully",
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("create_memory_note_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_add_to_watchlist(tool_input: dict[str, Any]) -> str:
    """Handle add_to_watchlist tool call."""
    user_id_input = tool_input.get("user_id")
    tmdb_id = tool_input.get("tmdb_id")
    media_type = tool_input.get("media_type", "movie")
    title = tool_input.get("title", "Unknown")
    year = tool_input.get("year")
    priority = tool_input.get("priority", 0)
    notes = tool_input.get("notes")

    if not user_id_input or not tmdb_id:
        return json.dumps(
            {"status": "error", "error": "user_id and tmdb_id are required"},
            ensure_ascii=False,
        )

    # Resolve to internal user ID
    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("add_to_watchlist", user_id=user_id, tmdb_id=tmdb_id, title=title)

    try:
        async with get_storage() as storage:
            # Check if already in watchlist
            if await storage.is_in_watchlist(user_id, tmdb_id=tmdb_id):
                return json.dumps(
                    {"status": "already_exists", "message": f"'{title}' уже в списке"},
                    ensure_ascii=False,
                )

            item = await storage.add_to_watchlist(
                user_id=user_id,
                tmdb_id=tmdb_id,
                media_type=media_type,
                title=title,
                year=year,
                priority=priority,
                notes=notes,
            )

            return json.dumps(
                {
                    "status": "success",
                    "message": f"'{title}' добавлен в список 'хочу посмотреть'",
                    "item_id": item.id,
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("add_to_watchlist_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_remove_from_watchlist(tool_input: dict[str, Any]) -> str:
    """Handle remove_from_watchlist tool call."""
    user_id_input = tool_input.get("user_id")
    tmdb_id = tool_input.get("tmdb_id")

    if not user_id_input or not tmdb_id:
        return json.dumps(
            {"status": "error", "error": "user_id and tmdb_id are required"},
            ensure_ascii=False,
        )

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("remove_from_watchlist", user_id=user_id, tmdb_id=tmdb_id)

    try:
        async with get_storage() as storage:
            removed = await storage.remove_from_watchlist(user_id, tmdb_id=tmdb_id)

            if removed:
                return json.dumps(
                    {"status": "success", "message": "Удалено из списка"}, ensure_ascii=False
                )
            return json.dumps(
                {"status": "not_found", "message": "Не найдено в списке"}, ensure_ascii=False
            )

    except Exception as e:
        logger.warning("remove_from_watchlist_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_get_watchlist(tool_input: dict[str, Any]) -> str:
    """Handle get_watchlist tool call."""
    user_id_input = tool_input.get("user_id")
    media_type = tool_input.get("media_type")
    limit = tool_input.get("limit", 20)

    if not user_id_input:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("get_watchlist", user_id=user_id, media_type=media_type)

    try:
        async with get_storage() as storage:
            items = await storage.get_watchlist(user_id, media_type=media_type, limit=limit)

            formatted = [
                {
                    "tmdb_id": item.tmdb_id,
                    "title": item.title,
                    "media_type": item.media_type,
                    "year": item.year,
                    "priority": item.priority,
                    "notes": item.notes,
                    "added_at": item.added_at.isoformat(),
                }
                for item in items
            ]

            return json.dumps(
                {"status": "success", "count": len(formatted), "items": formatted},
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("get_watchlist_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_mark_watched(tool_input: dict[str, Any]) -> str:
    """Handle mark_watched tool call."""
    user_id_input = tool_input.get("user_id")
    tmdb_id = tool_input.get("tmdb_id")
    media_type = tool_input.get("media_type", "movie")
    title = tool_input.get("title", "Unknown")
    year = tool_input.get("year")
    rating = tool_input.get("rating")
    review = tool_input.get("review")

    if not user_id_input or not tmdb_id:
        return json.dumps(
            {"status": "error", "error": "user_id and tmdb_id are required"},
            ensure_ascii=False,
        )

    # Resolve to internal user ID
    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("mark_watched", user_id=user_id, tmdb_id=tmdb_id, title=title, rating=rating)

    try:
        async with get_storage() as storage:
            # Check if already watched
            if await storage.is_watched(user_id, tmdb_id=tmdb_id):
                return json.dumps(
                    {"status": "already_watched", "message": f"'{title}' уже в истории просмотров"},
                    ensure_ascii=False,
                )

            # Add to watched
            item = await storage.add_watched(
                user_id=user_id,
                media_type=media_type,
                title=title,
                tmdb_id=tmdb_id,
                year=year,
                rating=rating,
                review=review,
            )

            # Remove from watchlist if it was there
            await storage.remove_from_watchlist(user_id, tmdb_id=tmdb_id)

            return json.dumps(
                {
                    "status": "success",
                    "message": f"'{title}' отмечен как просмотренный",
                    "item_id": item.id,
                    "ask_for_rating": rating is None,
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("mark_watched_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_rate_content(tool_input: dict[str, Any]) -> str:
    """Handle rate_content tool call."""
    user_id_input = tool_input.get("user_id")
    tmdb_id = tool_input.get("tmdb_id")
    rating = tool_input.get("rating")
    review = tool_input.get("review")

    if not user_id_input or not tmdb_id or rating is None:
        return json.dumps(
            {"status": "error", "error": "user_id, tmdb_id, and rating are required"},
            ensure_ascii=False,
        )

    # Validate rating
    if not 1 <= rating <= 10:
        return json.dumps(
            {"status": "error", "error": "rating must be between 1 and 10"},
            ensure_ascii=False,
        )

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("rate_content", user_id=user_id, tmdb_id=tmdb_id, rating=rating)

    try:
        async with get_storage() as storage:
            item = await storage.update_watched_rating(
                user_id=user_id,
                tmdb_id=tmdb_id,
                rating=rating,
                review=review,
            )

            if item:
                # Auto-capture conversation highlights for notable ratings
                if rating >= 9 or rating <= 2:
                    try:
                        from src.user.memory import CoreMemoryManager

                        memory_manager = CoreMemoryManager(storage)
                        title = item.title or f"TMDB:{tmdb_id}"
                        if rating >= 9:
                            highlight = f"Loved '{title}' ({rating}/10)"
                        else:
                            highlight = f"Disliked '{title}' ({rating}/10)"
                        await memory_manager.update_block(
                            user_id,
                            "learnings",
                            highlight,
                            operation="append",
                        )
                        logger.info(
                            "conversation_highlight_captured",
                            user_id=user_id,
                            title=title,
                            rating=rating,
                        )
                    except Exception as hl_error:
                        logger.warning("highlight_capture_failed", error=str(hl_error))

                return json.dumps(
                    {
                        "status": "success",
                        "message": f"Оценка {rating}/10 сохранена",
                        "suggest_recommendations": rating >= 8,
                    },
                    ensure_ascii=False,
                )
            return json.dumps(
                {"status": "not_found", "message": "Контент не найден в истории просмотров"},
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("rate_content_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_get_watch_history(tool_input: dict[str, Any]) -> str:
    """Handle get_watch_history tool call."""
    user_id_input = tool_input.get("user_id")
    media_type = tool_input.get("media_type")
    limit = tool_input.get("limit", 20)

    if not user_id_input:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

    user_id = await _resolve_user_id(user_id_input)
    if not user_id:
        return json.dumps(
            {"status": "error", "error": f"User not found for id {user_id_input}"},
            ensure_ascii=False,
        )

    logger.info("get_watch_history", user_id=user_id, media_type=media_type)

    try:
        async with get_storage() as storage:
            items = await storage.get_watched(user_id, media_type=media_type, limit=limit)

            formatted = [
                {
                    "tmdb_id": item.tmdb_id,
                    "title": item.title,
                    "media_type": item.media_type,
                    "year": item.year,
                    "rating": item.rating,
                    "review": item.review,
                    "watched_at": item.watched_at.isoformat(),
                }
                for item in items
            ]

            return json.dumps(
                {"status": "success", "count": len(formatted), "items": formatted},
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("get_watch_history_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_add_to_blocklist(tool_input: dict[str, Any]) -> str:
    """Handle add_to_blocklist tool call."""
    user_id_input = tool_input.get("user_id")
    block_type = tool_input.get("block_type")
    block_value = tool_input.get("block_value")
    block_level = tool_input.get("block_level", "dont_recommend")
    notes = tool_input.get("notes")

    if not all([user_id_input, block_type, block_value]):
        return json.dumps(
            {"status": "error", "error": "user_id, block_type, and block_value are required"},
            ensure_ascii=False,
        )

    # Resolve to internal user ID
    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("add_to_blocklist", user_id=user_id, block_type=block_type, block_value=block_value)

    try:
        async with get_storage() as storage:
            await storage.add_to_blocklist(
                user_id=user_id,
                block_type=block_type,
                block_value=block_value,
                block_level=block_level,
                notes=notes,
            )

            # Update profile blocklist section
            profile_manager = ProfileManager(storage)
            await profile_manager.sync_blocklist(user_id)

            return json.dumps(
                {
                    "status": "success",
                    "message": f"'{block_value}' добавлен в блоклист ({block_level})",
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("add_to_blocklist_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_get_blocklist(tool_input: dict[str, Any]) -> str:
    """Handle get_blocklist tool call."""
    user_id_input = tool_input.get("user_id")
    block_type = tool_input.get("block_type")

    if not user_id_input:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("get_blocklist", user_id=user_id, block_type=block_type)

    try:
        async with get_storage() as storage:
            items = await storage.get_blocklist(user_id, block_type=block_type)

            formatted = [
                {
                    "block_type": item.block_type,
                    "block_value": item.block_value,
                    "block_level": item.block_level,
                    "notes": item.notes,
                }
                for item in items
            ]

            return json.dumps(
                {"status": "success", "count": len(formatted), "items": formatted},
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("get_blocklist_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def _sync_monitors_to_memory(storage: Any, user_id: int) -> None:
    """Sync active monitors to user's active_context memory block.

    Updates the active_context block with a summary of what the user is waiting for,
    helping Claude understand the user's current focus.

    Args:
        storage: Storage instance (BaseStorage or subclass)
        user_id: Internal user ID
    """
    from src.user.memory import CoreMemoryManager

    try:
        # Get active monitors
        monitors = await storage.get_monitors(user_id=user_id, status="active")

        if not monitors:
            return

        # Build summary text
        waiting_for = []
        for m in monitors[:5]:  # Limit to 5 most recent
            media_emoji = "📺" if m.media_type == "tv" else "🎬"
            waiting_for.append(f"{media_emoji} {m.title} ({m.quality})")

        if waiting_for:
            content = "Waiting for releases:\n" + "\n".join(waiting_for)

            # Update active_context block
            memory_manager = CoreMemoryManager(storage)
            await memory_manager.update_block(
                user_id=user_id,
                block_name="active_context",
                content=content,
                operation="replace",
            )
            logger.debug("monitors_synced_to_memory", user_id=user_id, count=len(waiting_for))

    except Exception as e:
        logger.warning("sync_monitors_to_memory_error", user_id=user_id, error=str(e))


async def handle_create_monitor(tool_input: dict[str, Any]) -> str:
    """Handle create_monitor tool call."""
    user_id_input = tool_input.get("user_id")
    title = tool_input.get("title")
    tmdb_id = tool_input.get("tmdb_id")
    media_type = tool_input.get("media_type", "movie")
    quality = tool_input.get("quality", "1080p")
    auto_download = tool_input.get("auto_download", False)
    # TV series episode tracking
    tracking_mode = tool_input.get("tracking_mode", "season")
    season_number = tool_input.get("season_number")
    episode_number = tool_input.get("episode_number")

    if not user_id_input or not title:
        return json.dumps(
            {"status": "error", "error": "user_id and title are required"},
            ensure_ascii=False,
        )

    # Validate episode tracking parameters
    if tracking_mode == "episode" and (season_number is None or episode_number is None):
        return json.dumps(
            {
                "status": "error",
                "error": "Для режима 'episode' требуется указать season_number и episode_number",
            },
            ensure_ascii=False,
        )

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info(
        "create_monitor",
        user_id=user_id,
        title=title,
        quality=quality,
        tracking_mode=tracking_mode,
        season=season_number,
        episode=episode_number,
    )

    try:
        # Fetch release_date from TMDB for TV episodes
        release_date = None
        if tmdb_id and media_type == "tv" and season_number and episode_number:
            try:
                async with TMDBClient() as tmdb:
                    air_date_str = await tmdb.get_episode_air_date(
                        tmdb_id, season_number, episode_number
                    )
                    if air_date_str:
                        from datetime import datetime

                        release_date = datetime.fromisoformat(air_date_str)
                        logger.info(
                            "monitor_release_date_fetched",
                            tmdb_id=tmdb_id,
                            season=season_number,
                            episode=episode_number,
                            release_date=air_date_str,
                        )
            except Exception as e:
                logger.warning("fetch_episode_air_date_failed", error=str(e))

        async with get_storage() as storage:
            monitor = await storage.create_monitor(
                user_id=user_id,
                title=title,
                tmdb_id=tmdb_id,
                media_type=media_type,
                quality=quality,
                auto_download=auto_download,
                tracking_mode=tracking_mode,
                season_number=season_number,
                episode_number=episode_number,
                release_date=release_date,
            )

            # Sync to active_context memory block
            try:
                await _sync_monitors_to_memory(storage, user_id)
            except Exception as e:
                logger.warning("sync_monitors_to_memory_failed", error=str(e))

            # Build response message based on tracking mode
            if media_type == "tv":
                if tracking_mode == "episode":
                    msg = f"Мониторинг '{title}' S{season_number:02d}E{episode_number:02d} создан"
                elif season_number:
                    msg = f"Мониторинг '{title}' сезон {season_number} создан"
                else:
                    msg = f"Мониторинг сериала '{title}' создан"
            else:
                msg = f"Мониторинг '{title}' создан"

            return json.dumps(
                {
                    "status": "success",
                    "message": msg,
                    "monitor_id": monitor.id,
                    "quality": quality,
                    "auto_download": auto_download,
                    "tracking_mode": tracking_mode,
                    "season_number": season_number,
                    "episode_number": episode_number,
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("create_monitor_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_get_monitors(tool_input: dict[str, Any]) -> str:
    """Handle get_monitors tool call."""
    user_id_input = tool_input.get("user_id")
    status = tool_input.get("status")

    if not user_id_input:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("get_monitors", user_id=user_id, status=status)

    try:
        async with get_storage() as storage:
            monitors = await storage.get_monitors(user_id=user_id, status=status)

            formatted = [
                {
                    "id": m.id,
                    "title": m.title,
                    "tmdb_id": m.tmdb_id,
                    "media_type": m.media_type,
                    "quality": m.quality,
                    "auto_download": m.auto_download,
                    "status": m.status,
                    "created_at": m.created_at.isoformat(),
                }
                for m in monitors
            ]

            return json.dumps(
                {"status": "success", "count": len(formatted), "monitors": formatted},
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("get_monitors_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_cancel_monitor(tool_input: dict[str, Any]) -> str:
    """Handle cancel_monitor tool call."""
    monitor_id = tool_input.get("monitor_id")

    if not monitor_id:
        return json.dumps(
            {"status": "error", "error": "monitor_id is required"}, ensure_ascii=False
        )

    logger.info("cancel_monitor", monitor_id=monitor_id)

    try:
        async with get_storage() as storage:
            # Get monitor before deletion to know user_id
            monitor = await storage.get_monitor(monitor_id)
            user_id = monitor.user_id if monitor else None

            deleted = await storage.delete_monitor(monitor_id)

            if deleted:
                # Sync to memory after deletion
                if user_id:
                    try:
                        await _sync_monitors_to_memory(storage, user_id)
                    except Exception as e:
                        logger.warning("sync_monitors_to_memory_failed", error=str(e))

                return json.dumps(
                    {"status": "success", "message": "Мониторинг отменён"}, ensure_ascii=False
                )
            return json.dumps(
                {"status": "not_found", "message": "Мониторинг не найден"}, ensure_ascii=False
            )

    except Exception as e:
        logger.warning("cancel_monitor_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_get_crew_stats(tool_input: dict[str, Any]) -> str:
    """Handle get_crew_stats tool call."""
    user_id_input = tool_input.get("user_id")
    role = tool_input.get("role")
    min_films = tool_input.get("min_films", 2)

    if not user_id_input:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    logger.info("get_crew_stats", user_id=user_id, role=role, min_films=min_films)

    try:
        async with get_storage() as storage:
            stats = await storage.get_crew_stats(user_id=user_id, role=role, min_films=min_films)

            formatted = [
                {
                    "person_name": s.person_name,
                    "person_id": s.person_id,
                    "role": s.role,
                    "films_count": s.films_count,
                    "avg_rating": round(s.avg_rating, 1),
                }
                for s in stats
            ]

            return json.dumps(
                {"status": "success", "count": len(formatted), "stats": formatted},
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("get_crew_stats_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_letterboxd_sync(tool_input: dict[str, Any]) -> str:
    """Handle letterboxd_sync tool call (RSS-based import)."""
    from src.services.letterboxd_rss import LetterboxdRSSError, sync_letterboxd_to_storage

    user_id_input = tool_input.get("user_id")
    letterboxd_username = tool_input.get("letterboxd_username")

    if not user_id_input or not letterboxd_username:
        return json.dumps(
            {"status": "error", "error": "user_id and letterboxd_username are required"},
            ensure_ascii=False,
        )

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    sync_watchlist = tool_input.get("sync_watchlist", True)
    sync_diary = tool_input.get("sync_diary", True)
    diary_limit = tool_input.get("diary_limit", 10000)

    logger.info(
        "letterboxd_rss_sync",
        user_id=user_id,
        username=letterboxd_username,
        sync_watchlist=sync_watchlist,
        sync_diary=sync_diary,
    )

    try:
        async with get_storage() as storage:
            results = await sync_letterboxd_to_storage(
                username=letterboxd_username,
                storage=storage,
                user_id=user_id,
                sync_watchlist=sync_watchlist,
                sync_diary=sync_diary,
                diary_limit=diary_limit,
            )

            # Build response message
            parts = []
            if sync_watchlist:
                parts.append(
                    f"Watchlist: импортировано {results['watchlist_imported']}, "
                    f"пропущено {results['watchlist_skipped']}"
                )
            if sync_diary:
                parts.append(
                    f"Дневник: импортировано {results['diary_imported']}, "
                    f"пропущено {results['diary_skipped']}"
                )

            return json.dumps(
                {
                    "status": "success",
                    "message": ". ".join(parts),
                    **results,
                },
                ensure_ascii=False,
            )

    except LetterboxdRSSError as e:
        logger.warning("letterboxd_rss_sync_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)
    except Exception as e:
        logger.warning("letterboxd_rss_sync_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_get_industry_news(tool_input: dict[str, Any]) -> str:
    """Handle get_industry_news tool - fetch news from RSS feeds.

    Args:
        tool_input: Dict with keywords, hours, max_results.

    Returns:
        JSON with news items.
    """
    from src.services.news import NewsService

    keywords = tool_input.get("keywords", [])
    hours = tool_input.get("hours", 72)
    max_results = tool_input.get("max_results", 5)

    if not keywords:
        return json.dumps(
            {"status": "error", "error": "Нужно указать ключевые слова для поиска"},
            ensure_ascii=False,
        )

    try:
        async with NewsService() as service:
            news_items = await service.get_relevant_news(
                keywords=keywords,
                hours=hours,
                max_results=max_results,
            )

            if not news_items:
                return json.dumps(
                    {
                        "status": "no_results",
                        "message": f"Новостей по запросу '{', '.join(keywords)}' не найдено",
                    },
                    ensure_ascii=False,
                )

            results = []
            for item in news_items:
                results.append(
                    {
                        "title": item.title,
                        "description": item.description[:300] if item.description else "",
                        "source": item.source,
                        "link": item.link,
                        "published_at": item.published_at.isoformat()
                        if item.published_at
                        else None,
                        "keywords_matched": item.keywords_matched,
                    }
                )

            logger.info(
                "industry_news_fetched",
                keywords=keywords,
                results_count=len(results),
            )

            return json.dumps(
                {"status": "success", "news": results},
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("industry_news_fetch_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_get_recent_news(tool_input: dict[str, Any]) -> str:
    """Handle get_recent_news tool - fetch all recent news without filtering.

    Args:
        tool_input: Dict with hours, max_results.

    Returns:
        JSON with news items.
    """
    from src.services.news import NewsService

    hours = tool_input.get("hours", 24)
    max_results = tool_input.get("max_results", 10)

    try:
        async with NewsService() as service:
            news_items = await service.get_all_recent_news(
                hours=hours,
                max_per_feed=max_results // 4 + 1,  # Distribute across feeds
            )

            if not news_items:
                return json.dumps(
                    {
                        "status": "no_results",
                        "message": "Новостей за указанный период не найдено",
                    },
                    ensure_ascii=False,
                )

            # Limit results
            news_items = news_items[:max_results]

            results = []
            for item in news_items:
                results.append(
                    {
                        "title": item.title,
                        "description": item.description[:300] if item.description else "",
                        "source": item.source,
                        "link": item.link,
                        "published_at": item.published_at.isoformat()
                        if item.published_at
                        else None,
                    }
                )

            logger.info(
                "recent_news_fetched",
                results_count=len(results),
            )

            return json.dumps(
                {"status": "success", "news": results},
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("recent_news_fetch_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_get_hidden_gem(tool_input: dict[str, Any]) -> str:
    """Handle get_hidden_gem tool - generate personalized recommendation.

    Args:
        tool_input: Dict with user_id.

    Returns:
        JSON with hidden gem recommendation.
    """
    import anthropic

    from src.config import settings
    from src.user.memory import CoreMemoryManager

    user_id_input = tool_input.get("user_id")
    if not user_id_input:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

    user_id = await _resolve_user_id(user_id_input)
    if user_id is None:
        return json.dumps({"status": "error", "error": "User not found"}, ensure_ascii=False)

    try:
        async with get_storage() as storage:
            # Get user's core memory for profile context
            memory_manager = CoreMemoryManager(storage)
            profile_blocks = await memory_manager.get_all_blocks(user_id)

            # Build profile context
            profile_context = ""
            for block in profile_blocks:
                if block.content:
                    profile_context += f"\n{block.block_name}: {block.content}"

            if not profile_context.strip():
                return json.dumps(
                    {
                        "status": "error",
                        "error": "Недостаточно данных о предпочтениях. Нужно больше информации о вкусах.",
                    },
                    ensure_ascii=False,
                )

            # Get watch history (large limit to avoid recommending watched films)
            watched = await storage.get_watched(user_id, limit=1000)
            watched_titles = [w.title for w in watched if w.title]

            # Generate recommendation using Claude
            prompt = f"""Based on this user's profile, suggest ONE hidden gem film.

Profile:
{profile_context}

Already watched (DO NOT recommend any of these): {", ".join(watched_titles) if watched_titles else "No data"}

Requirements:
- NOT a blockbuster (no Marvel, Star Wars, Fast & Furious, etc.)
- NOT in IMDb Top 250
- Matches user's taste based on profile
- Released before 2020 (so it's discoverable now)
- Must be a real film that exists

Return ONLY a JSON object with:
{{"title": "Film Title", "year": 1999, "reason": "Why this matches their taste (2-3 sentences in Russian)", "director": "Director Name"}}

If you can't find a good match, return {{"error": "Недостаточно данных"}}"""

            client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
            message = await client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )

            response = ""
            for block in message.content:
                if hasattr(block, "text"):
                    response += block.text

            # Parse the response
            import re

            json_match = re.search(r"\{[^{}]+\}", response)
            if not json_match:
                return json.dumps(
                    {"status": "error", "error": "Не удалось сгенерировать рекомендацию"},
                    ensure_ascii=False,
                )

            recommendation = json.loads(json_match.group())

            if "error" in recommendation:
                return json.dumps(
                    {"status": "error", "error": recommendation["error"]},
                    ensure_ascii=False,
                )

            # Hard-filter: reject if title is in watched (direct DB check, retry up to 3 times)
            excluded = []
            for _attempt in range(3):
                rec_title = recommendation.get("title", "")
                if not rec_title or not await storage.is_watched_by_title(user_id, rec_title):
                    break
                logger.warning(
                    "hidden_gem_in_watched",
                    user_id=user_id,
                    title=rec_title,
                    attempt=_attempt + 1,
                )
                excluded.append(rec_title)
                # Retry with exclusion
                retry_prompt = (
                    prompt
                    + f"\n\nDO NOT suggest these (already watched or excluded): {', '.join(excluded)}"
                )
                retry_msg = await client.messages.create(
                    model="claude-sonnet-4-5-20250929",
                    max_tokens=500,
                    messages=[{"role": "user", "content": retry_prompt}],
                )
                retry_text = ""
                for blk in retry_msg.content:
                    if hasattr(blk, "text"):
                        retry_text += blk.text
                retry_match = re.search(r"\{[^{}]+\}", retry_text)
                if retry_match:
                    recommendation = json.loads(retry_match.group())
                    if "error" in recommendation:
                        return json.dumps(
                            {"status": "error", "error": recommendation["error"]},
                            ensure_ascii=False,
                        )
                else:
                    break

            logger.info(
                "hidden_gem_generated",
                user_id=user_id,
                title=recommendation.get("title"),
            )

            return json.dumps(
                {"status": "success", "recommendation": recommendation},
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("hidden_gem_generation_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_get_director_upcoming(tool_input: dict[str, Any]) -> str:
    """Handle get_director_upcoming tool - get upcoming movies from director.

    Args:
        tool_input: Dict with director_name.

    Returns:
        JSON with upcoming movies.
    """
    from src.media.tmdb import TMDBClient

    director_name = tool_input.get("director_name")
    if not director_name:
        return json.dumps(
            {"status": "error", "error": "director_name is required"},
            ensure_ascii=False,
        )

    try:
        async with TMDBClient() as tmdb:
            # Search for the director
            persons = await tmdb.search_person(director_name)
            if not persons:
                return json.dumps(
                    {"status": "no_results", "message": f"Режиссёр '{director_name}' не найден"},
                    ensure_ascii=False,
                )

            # Find the director (prefer known directors)
            director = None
            for p in persons:
                if p.get("known_for_department") == "Directing":
                    director = p
                    break
            if not director:
                director = persons[0]

            # Get upcoming movies
            upcoming = await tmdb.get_person_upcoming_movies(
                director["id"],
                role="Director",
            )

            if not upcoming:
                return json.dumps(
                    {
                        "status": "no_results",
                        "message": f"У {director['name']} нет анонсированных проектов",
                    },
                    ensure_ascii=False,
                )

            results = []
            for movie in upcoming[:5]:
                results.append(
                    {
                        "title": movie.get("title"),
                        "tmdb_id": movie.get("id"),
                        "release_date": movie.get("release_date"),
                        "overview": movie.get("overview", "")[:200],
                        "status": movie.get("status", "Announced"),
                    }
                )

            logger.info(
                "director_upcoming_fetched",
                director=director["name"],
                movies_count=len(results),
            )

            return json.dumps(
                {
                    "status": "success",
                    "director": director["name"],
                    "upcoming": results,
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("director_upcoming_fetch_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


def create_tool_executor(telegram_id: int | None = None) -> ToolExecutor:
    """Create and configure a tool executor with all handlers.

    Args:
        telegram_id: Telegram user ID for per-user credentials.

    Returns:
        Configured ToolExecutor instance.
    """
    executor = ToolExecutor()

    # Auto-inject telegram_id as user_id for tools that need it
    def _with_user_id(handler):
        async def wrapper(tool_input: dict[str, Any]) -> str:
            if telegram_id is not None:
                tool_input["user_id"] = telegram_id
            return await handler(tool_input)

        return wrapper

    # Create wrappers for handlers that need telegram_id
    async def rutracker_handler(tool_input: dict[str, Any]) -> str:
        return await handle_rutracker_search(tool_input, telegram_id=telegram_id)

    async def piratebay_handler(tool_input: dict[str, Any]) -> str:
        return await handle_piratebay_search(tool_input, telegram_id=telegram_id)

    executor.register_handlers(
        {
            # Core search tools
            "rutracker_search": rutracker_handler,
            "piratebay_search": piratebay_handler,
            "tmdb_search": handle_tmdb_search,
            "tmdb_person_search": handle_tmdb_person_search,
            "tmdb_batch_entity_search": handle_tmdb_batch_entity_search,
            "tmdb_credits": handle_tmdb_credits,
            "tmdb_tv_details": handle_tmdb_tv_details,
            "kinopoisk_search": handle_kinopoisk_search,
            # User profile tools (legacy)
            "get_user_profile": _with_user_id(handle_get_user_profile),
            "read_user_profile": _with_user_id(handle_read_user_profile),
            "update_user_profile": _with_user_id(handle_update_user_profile),
            # Memory system tools (MemGPT-style)
            "read_core_memory": _with_user_id(handle_read_core_memory),
            "update_core_memory": _with_user_id(handle_update_core_memory),
            "search_memory_notes": _with_user_id(handle_search_memory_notes),
            "create_memory_note": _with_user_id(handle_create_memory_note),
            # Download tools
            "seedbox_download": _with_user_id(handle_seedbox_download),
            # Watchlist tools
            "add_to_watchlist": _with_user_id(handle_add_to_watchlist),
            "remove_from_watchlist": _with_user_id(handle_remove_from_watchlist),
            "get_watchlist": _with_user_id(handle_get_watchlist),
            # Watch history & ratings
            "mark_watched": _with_user_id(handle_mark_watched),
            "rate_content": _with_user_id(handle_rate_content),
            "get_watch_history": _with_user_id(handle_get_watch_history),
            # Blocklist tools
            "add_to_blocklist": _with_user_id(handle_add_to_blocklist),
            "get_blocklist": _with_user_id(handle_get_blocklist),
            # Monitoring tools
            "create_monitor": _with_user_id(handle_create_monitor),
            "get_monitors": _with_user_id(handle_get_monitors),
            "cancel_monitor": handle_cancel_monitor,
            # Analytics tools
            "get_crew_stats": _with_user_id(handle_get_crew_stats),
            # External service sync
            "letterboxd_sync": _with_user_id(handle_letterboxd_sync),
            # Proactive features
            "get_industry_news": handle_get_industry_news,
            "get_recent_news": handle_get_recent_news,
            "get_hidden_gem": _with_user_id(handle_get_hidden_gem),
            "get_director_upcoming": handle_get_director_upcoming,
            # Web search
            "web_search": handle_web_search,
        }
    )

    return executor


# =============================================================================
# Result Formatting
# =============================================================================


def format_search_results_keyboard(results: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Create inline keyboard with download buttons for search results.

    Args:
        results: List of search result dicts with id, title, quality, size, seeds.

    Returns:
        InlineKeyboardMarkup with download buttons.
    """
    buttons = []
    for result in results[:5]:  # Max 5 buttons
        result_id = result.get("id", "")
        title = result.get("title", "Unknown")
        quality = result.get("quality", "")
        size = result.get("size", "")
        seeds = result.get("seeds", 0)

        # Build info string: [quality] size S:seeds (put first so it's always visible)
        info_parts = []
        if quality:
            info_parts.append(f"[{quality}]")
        if size:
            # Shorten size (e.g., "14.5 GB" -> "14.5G")
            short_size = size.replace(" GB", "G").replace(" MB", "M").replace(" TB", "T")
            info_parts.append(short_size)
        info_parts.append(f"S:{seeds}")
        info_str = " ".join(info_parts)

        # Truncate title to fit (Telegram button limit ~64 chars)
        max_title_len = 38 - len(info_str)
        if max_title_len < 8:
            max_title_len = 8
        short_title = title[:max_title_len] + ".." if len(title) > max_title_len else title

        # Info first, then title (so quality/size/seeds always visible)
        button_text = f"{info_str} | {short_title}"

        buttons.append(
            [
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"download_{result_id}",
                )
            ]
        )

    return InlineKeyboardMarkup(buttons)


def format_torrent_result_message(result: dict[str, Any]) -> str:
    """Format a single torrent result for display.

    Args:
        result: Torrent search result dict.

    Returns:
        Formatted message string.
    """
    title = result.get("title", "Unknown")
    size = result.get("size", "N/A")
    seeds = result.get("seeds", 0)
    quality = result.get("quality", "unknown")

    # Quality emoji
    quality_emoji = {
        "4K": "🎬",
        "2160p": "🎬",
        "1080p": "🎥",
        "720p": "📺",
        "HDR": "✨",
    }.get(quality, "📹")

    # Seeds color indicator
    if seeds >= 100:
        seeds_indicator = "🟢"
    elif seeds >= 20:
        seeds_indicator = "🟡"
    else:
        seeds_indicator = "🔴"

    return (
        f"{quality_emoji} **{title}**\n"
        f"📦 Размер: {size}\n"
        f"{seeds_indicator} Сиды: {seeds}\n"
        f"🎞 Качество: {quality}"
    )


def format_torrent_card(result: dict[str, Any], result_id: str) -> tuple[str, InlineKeyboardMarkup]:
    """Format a torrent result as a card with action buttons.

    Args:
        result: Cached result data.
        result_id: Unique result identifier.

    Returns:
        Tuple of (message text, inline keyboard).
    """
    title = result.get("title", "Unknown")
    size = result.get("size", "N/A")
    seeds = result.get("seeds", 0)
    quality = result.get("quality", "")
    source = result.get("source", "unknown")

    # Quality emoji
    quality_emoji = {
        "4K": "🎥",
        "2160p": "🎥",
        "1080p": "🎬",
        "720p": "📺",
        "HDR": "✨",
    }.get(quality, "🎬")

    # Seeds color indicator
    if seeds >= 50:
        seeds_emoji = "🟢"
    elif seeds >= 10:
        seeds_emoji = "🟡"
    else:
        seeds_emoji = "🔴"

    # Build quality string
    quality_str = f"{quality} | " if quality else ""

    # Format card text
    text = (
        f"{quality_emoji} <b>{title[:60]}</b>\n"
        f"📀 {quality_str}{size} | {seeds_emoji} Seeds: {seeds}\n"
        f"📂 {source.title()}"
    )

    # Build action buttons
    has_torrent = bool(result.get("torrent_url"))

    row = [
        InlineKeyboardButton("📋 Магнет", callback_data=f"dl_magnet_{result_id}"),
    ]

    # "Download" button only if torrent_url available (Rutracker via TorAPI)
    if has_torrent:
        row.append(InlineKeyboardButton("📥 Скачать", callback_data=f"dl_torrent_{result_id}"))

    row.append(InlineKeyboardButton("🌐 Seedbox", callback_data=f"dl_seedbox_{result_id}"))

    keyboard = InlineKeyboardMarkup([row])

    return text, keyboard


async def send_search_results_cards(
    bot, chat_id: int, result_ids: list[str], max_results: int = 3
) -> list[int]:
    """Send search results as individual card messages.

    Args:
        bot: Telegram bot instance.
        chat_id: Chat to send messages to.
        result_ids: List of cached result IDs.
        max_results: Maximum number of cards to send.

    Returns:
        List of sent message IDs.
    """
    # Get and sort results by quality (best first), then by seeds
    results_with_ids = []
    for rid in result_ids:
        result_data = get_cached_result(rid)
        if result_data:
            results_with_ids.append((rid, result_data))

    # Quality priority: 4K/2160p > 1080p > 720p > others
    def quality_priority(quality: str | None) -> int:
        if not quality:
            return 0
        q = quality.upper()
        if "4K" in q or "2160" in q or "UHD" in q:
            return 100
        if "1080" in q:
            return 50
        if "720" in q:
            return 25
        return 10

    # Sort by quality first (descending), then by seeds (descending)
    results_with_ids.sort(
        key=lambda x: (quality_priority(x[1].get("quality")), x[1].get("seeds", 0)),
        reverse=True,
    )

    # Limit results
    results_with_ids = results_with_ids[:max_results]

    sent_messages = []
    for result_id, result_data in results_with_ids:
        text, keyboard = format_torrent_card(result_data, result_id)
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            sent_messages.append(msg.message_id)
        except Exception as e:
            logger.warning("failed_to_send_torrent_card", error=str(e), result_id=result_id)

    return sent_messages


# =============================================================================
# Main Conversation Handler
# =============================================================================


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle incoming text messages with natural language understanding.

    This is the main entry point for user messages. It:
    1. Gets or creates conversation context for the user
    2. Sends the message to Claude with tool support
    3. Streams the response back to the user
    4. Handles any inline buttons for downloads

    Args:
        update: Telegram update object.
        context: Callback context.
    """
    if not update.message or not update.message.text:
        return

    user = update.effective_user
    if not user:
        return

    user_message = update.message.text

    # Detect #запомни / #remember command
    remember_requested = False
    remember_prefixes = ("#запомни", "#remember", "#запомнить")
    for prefix in remember_prefixes:
        if user_message.lower().startswith(prefix):
            remember_requested = True
            # Remove prefix from message for cleaner processing
            user_message = user_message[len(prefix) :].strip()
            logger.info("remember_command_detected", user_id=user.id)
            break

    logger.info(
        "message_received",
        user_id=user.id,
        username=user.username,
        message_length=len(user_message),
        remember_requested=remember_requested,
    )

    # Get conversation context
    conv_context = get_conversation_context(user.id)

    # Set telegram user ID for tool calls
    conv_context.telegram_user_id = user.id

    # Set remember flag in context
    conv_context.remember_requested = remember_requested

    # Load user preferences and profile into context (skip if already cached)
    if not conv_context.context_loaded:
        try:
            encryption_key = None
            if settings.encryption_key:
                encryption_key = settings.encryption_key.get_secret_value()

            async with get_storage(encryption_key) as storage:
                db_user = await storage.get_user_by_telegram_id(user.id)
                if db_user:
                    preferences = await storage.get_preferences(db_user.id)
                    if preferences:
                        conv_context.user_preferences = {
                            "quality": preferences.video_quality,
                            "audio_language": preferences.audio_language,
                            "genres": preferences.preferred_genres,
                        }
                        logger.debug(
                            "user_preferences_loaded",
                            user_id=user.id,
                            quality=preferences.video_quality,
                            audio_language=preferences.audio_language,
                            genres=preferences.preferred_genres,
                        )

                    # Load core memory blocks (new MemGPT-style system)
                    from src.user.memory import CoreMemoryManager, migrate_profile_to_core_memory

                    memory_manager = CoreMemoryManager(storage)
                    blocks = await memory_manager.get_all_blocks(db_user.id)

                    # Migrate old profile to core memory if blocks are empty
                    if not blocks or all(not b.content for b in blocks):
                        # Only load legacy profile for migration purposes
                        profile_manager = ProfileManager(storage)
                        legacy_profile = await profile_manager.get_or_create_profile(
                            db_user.id, user=db_user, preferences=preferences
                        )
                        if legacy_profile:
                            logger.info(
                                "migrating_profile_to_core_memory",
                                user_id=user.id,
                            )
                            blocks = await migrate_profile_to_core_memory(
                                storage, db_user.id, legacy_profile
                            )

                    # Render core memory for Claude's context
                    if blocks:
                        conv_context.core_memory_content = memory_manager.render_blocks_for_context(
                            blocks
                        )
                        logger.debug(
                            "core_memory_loaded",
                            user_id=user.id,
                            blocks_count=len(blocks),
                            content_length=len(conv_context.core_memory_content or ""),
                        )

                    # Mark context as loaded to skip DB queries on subsequent messages
                    conv_context.context_loaded = True
        except Exception as e:
            logger.warning("failed_to_load_preferences", error=str(e))

    # Get user's AI model settings
    from src.bot.model_settings import get_user_model_settings

    user_model, thinking_budget = await get_user_model_settings(user.id)

    # Create Claude client with tools (pass telegram_id for per-user credentials)
    executor = create_tool_executor(telegram_id=user.id)
    client = ClaudeClient(
        tools=get_tool_definitions(),
        tool_executor=executor,
        model=user_model,
        thinking_budget=thinking_budget,
    )

    # Clear the set of result IDs touched in this request
    _current_request_result_ids.clear()

    try:
        # Stream response to user
        response_text = await send_streaming_message(
            update,
            context,
            client.stream_message(user_message, conv_context),
            initial_text="Думаю...",
        )

        logger.info(
            "response_sent",
            user_id=user.id,
            response_length=len(response_text),
        )

        # Check for search results touched during this request
        # Using _current_request_result_ids ensures we show buttons even for cached results
        touched_result_ids = list(_current_request_result_ids)

        # Only show buttons if search was performed in this request
        if touched_result_ids:
            # Update context with current result IDs for potential re-use
            conv_context.last_search_result_ids = touched_result_ids[:10]

            # Send top-3 results as individual cards with action buttons
            await send_search_results_cards(
                context.bot,
                update.message.chat_id,
                touched_result_ids,
                max_results=3,
            )

    except Exception as e:
        logger.exception("message_handling_failed", user_id=user.id, error=str(e))

        # Try to send error message
        with contextlib.suppress(Exception):
            await update.message.reply_text(
                "Произошла ошибка при обработке вашего запроса. "
                "Пожалуйста, попробуйте ещё раз или перефразируйте запрос."
            )


async def handle_download_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle download button callbacks.

    Args:
        update: Telegram update object.
        context: Callback context.
    """
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    # Extract result ID from callback data
    result_id = query.data.replace("download_", "")

    # Get cached result
    result = get_cached_result(result_id)
    if not result:
        await query.edit_message_text(
            "К сожалению, эта раздача больше недоступна. Попробуйте выполнить поиск заново."
        )
        return

    title = result.get("title", "Unknown")
    magnet = result.get("magnet", "")
    torrent_id = result.get("torrent_id", "")
    source = result.get("source", "")

    # Validate magnet - clear invalid/placeholder magnets
    if not is_valid_magnet(magnet):
        magnet = ""

    logger.info(
        "download_requested",
        user_id=query.from_user.id if query.from_user else None,
        result_id=result_id,
        title=title,
        has_magnet=bool(magnet),
    )

    # If no magnet cached, fetch it via authenticated Rutracker client
    if not magnet and torrent_id and source == "rutracker":
        telegram_id = query.from_user.id if query.from_user else None
        magnet = await _fetch_magnet_via_rutracker(torrent_id, telegram_id)
        if magnet:
            result["magnet"] = magnet
            cache_search_result(result_id, result)

    # If still no magnet, show error
    if not magnet:
        logger.warning("no_magnet_available", result_id=result_id, title=title)
        await query.edit_message_text(
            f"<b>{title}</b>\n\nНе удалось получить magnet-ссылку. Попробуйте найти раздачу заново.",
            parse_mode="HTML",
        )
        return

    # Ensure magnet is a string (not a list)
    if isinstance(magnet, list):
        magnet = magnet[0] if magnet else ""

    telegram_id = query.from_user.id if query.from_user else None
    logger.info(
        "sending_magnet_to_user",
        result_id=result_id,
        telegram_id=telegram_id,
        magnet_length=len(magnet),
        magnet_preview=magnet[:100] if magnet else "empty",
    )

    # Try to send to seedbox (user's first, then global fallback)
    download_result = await send_magnet_to_user_seedbox(magnet, telegram_id)

    if download_result.get("status") == "sent":
        seedbox_label = "ваш seedbox" if download_result.get("user_seedbox") else "seedbox"
        await query.edit_message_text(
            f"Торрент добавлен на скачивание!\n\n"
            f"<b>{title}</b>\n\n"
            f"Скачивание началось на {seedbox_label}.",
            parse_mode="HTML",
        )
    else:
        # Seedbox not configured - show magnet link
        logger.info("seedbox_not_configured", showing_magnet=True)
        message = query.message

        # Use HTML mode and <code> tag for magnet (more reliable than Markdown backticks)
        if len(magnet) > 3500:
            # Magnet too long for single message, send in parts
            await query.edit_message_text(
                f"<b>{title}</b>\n\nMagnet-ссылка отправлена отдельным сообщением:",
                parse_mode="HTML",
            )
            if message and hasattr(message, "reply_text"):
                # Send magnet as plain text without formatting for reliability
                await message.reply_text(magnet)
        else:
            try:
                await query.edit_message_text(
                    f"<b>{title}</b>\n\nСкопируйте magnet-ссылку:\n<code>{magnet}</code>",
                    parse_mode="HTML",
                )
            except Exception as e:
                # If HTML formatting fails, try plain text
                logger.warning("html_format_failed", error=str(e))
                await query.edit_message_text(f"{title}\n\nMagnet-ссылка:\n{magnet}")


def _extract_info_hash_from_torrent(torrent_data: bytes) -> str | None:
    """Extract info_hash from torrent file data using simple bencode parser.

    Args:
        torrent_data: Raw torrent file bytes.

    Returns:
        Info hash as hex string or None if extraction fails.
    """
    import hashlib

    def decode_bencode(data: bytes, idx: int = 0) -> tuple[Any, int]:
        """Simple bencode decoder."""
        if data[idx : idx + 1] == b"d":
            # Dictionary
            idx += 1
            result = {}
            while data[idx : idx + 1] != b"e":
                key, idx = decode_bencode(data, idx)
                value, idx = decode_bencode(data, idx)
                if isinstance(key, bytes):
                    key = key.decode("utf-8", errors="replace")
                result[key] = value
            return result, idx + 1
        if data[idx : idx + 1] == b"l":
            # List
            idx += 1
            result = []
            while data[idx : idx + 1] != b"e":
                item, idx = decode_bencode(data, idx)
                result.append(item)
            return result, idx + 1
        if data[idx : idx + 1] == b"i":
            # Integer
            idx += 1
            end = data.index(b"e", idx)
            return int(data[idx:end]), end + 1
        if data[idx : idx + 1].isdigit():
            # String
            colon = data.index(b":", idx)
            length = int(data[idx:colon])
            start = colon + 1
            return data[start : start + length], start + length
        raise ValueError(f"Invalid bencode at {idx}")

    try:
        decoded, _ = decode_bencode(torrent_data)
        if not isinstance(decoded, dict) or "info" not in decoded:
            return None

        # Re-encode info dict to get its bencoded form for hashing
        def encode_bencode(obj) -> bytes:
            if isinstance(obj, dict):
                items = sorted(obj.items())
                encoded = b"d"
                for k, v in items:
                    if isinstance(k, str):
                        k = k.encode("utf-8")
                    encoded += encode_bencode(k) + encode_bencode(v)
                return encoded + b"e"
            if isinstance(obj, list):
                return b"l" + b"".join(encode_bencode(i) for i in obj) + b"e"
            if isinstance(obj, int):
                return f"i{obj}e".encode()
            if isinstance(obj, bytes):
                return f"{len(obj)}:".encode() + obj
            if isinstance(obj, str):
                encoded = obj.encode("utf-8")
                return f"{len(encoded)}:".encode() + encoded
            raise ValueError(f"Cannot encode {type(obj)}")

        info_bencoded = encode_bencode(decoded["info"])
        return hashlib.sha1(info_bencoded).hexdigest()
    except Exception as e:
        logger.warning("bencode_parse_failed", error=str(e))
        return None


async def _fetch_magnet_from_torrent_url(torrent_url: str, title: str) -> str | None:
    """Download torrent file and extract magnet link.

    Args:
        torrent_url: URL to download .torrent file.
        title: Torrent title for magnet display name.

    Returns:
        Magnet link or None if extraction fails.
    """
    import urllib.parse

    import httpx

    if not torrent_url:
        return None

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(
                torrent_url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                },
            )
            if response.status_code != 200:
                logger.warning("torrent_download_failed", status=response.status_code)
                return None

            info_hash = _extract_info_hash_from_torrent(response.content)
            if info_hash:
                # Build magnet link
                encoded_title = urllib.parse.quote(title[:100])
                magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={encoded_title}"
                logger.info("magnet_extracted_from_torrent", info_hash=info_hash[:8])
                return magnet
    except Exception as e:
        logger.warning("magnet_extraction_failed", error=str(e))

    return None


async def _fetch_magnet_via_rutracker(topic_id: str, telegram_id: int | None = None) -> str | None:
    """Fetch magnet link from Rutracker using authenticated client.

    Args:
        topic_id: Rutracker topic ID (torrent_id from cache).
        telegram_id: User's telegram ID to get credentials.

    Returns:
        Magnet link or None if extraction fails.
    """
    from src.search.rutracker import RutrackerClient

    if not topic_id:
        return None

    # Get user credentials
    username = None
    password = None

    if telegram_id:
        from src.bot.rutracker_auth import get_user_rutracker_credentials

        username, password = await get_user_rutracker_credentials(telegram_id)

    # Fall back to global credentials
    if not username:
        username = settings.rutracker_username
        password = (
            settings.rutracker_password.get_secret_value() if settings.rutracker_password else None
        )

    if not username:
        logger.warning("no_rutracker_credentials_for_magnet")
        return None

    try:
        async with RutrackerClient(username=username, password=password) as client:
            magnet = await client.get_magnet_link(int(topic_id))
            logger.info("magnet_fetched_via_rutracker", topic_id=topic_id)
            return magnet
    except Exception as e:
        logger.warning("rutracker_magnet_fetch_failed", error=str(e), topic_id=topic_id)
        return None


async def handle_magnet_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle magnet button callback - send magnet link as text.

    If magnet is not in cache, fetches it from TorAPI details endpoint.

    Args:
        update: Telegram update object.
        _context: Callback context (unused).
    """
    query = update.callback_query
    if not query or not query.data:
        return

    result_id = query.data.replace("dl_magnet_", "")
    result = get_cached_result(result_id)

    if not result:
        await query.answer("Результат не найден", show_alert=True)
        return

    magnet = result.get("magnet", "")
    title = result.get("title", "Unknown")

    # Ensure magnet is a string
    if isinstance(magnet, list):
        magnet = magnet[0] if magnet else ""

    # If magnet is empty, try to fetch via authenticated Rutracker client
    if not magnet:
        torrent_id = result.get("torrent_id", "")
        source = result.get("source", "")
        telegram_id = update.effective_user.id if update.effective_user else None

        await query.answer("Загружаю магнет-ссылку...")

        # Try Rutracker authenticated client first (for rutracker source)
        if torrent_id and source == "rutracker":
            magnet = await _fetch_magnet_via_rutracker(torrent_id, telegram_id)
        else:
            # Fallback to torrent file extraction for other sources
            torrent_url = result.get("torrent_url", "")
            magnet = await _fetch_magnet_from_torrent_url(torrent_url, title)

        if magnet:
            # Update cache with fetched magnet
            result["magnet"] = magnet
            cache_search_result(result_id, result)
        else:
            await query.answer("Магнет-ссылка недоступна", show_alert=True)
            return
    else:
        await query.answer("Магнет-ссылка отправлена")

    # Send magnet as reply to the card message
    message = query.message
    if message:
        if len(magnet) > 3500:
            # Magnet too long, send as plain text
            await message.reply_text(
                f"🔗 Магнет-ссылка для <b>{title[:50]}</b>:", parse_mode="HTML"
            )
            await message.reply_text(magnet)
        else:
            await message.reply_text(
                f"🔗 Магнет-ссылка:\n\n<code>{magnet}</code>",
                parse_mode="HTML",
            )

    logger.info(
        "magnet_sent", result_id=result_id, user_id=query.from_user.id if query.from_user else None
    )

    # Record the download for follow-up
    if query.from_user:
        await _record_download(query.from_user.id, result, magnet)


async def handle_torrent_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle torrent download button callback - download and send .torrent file.

    Args:
        update: Telegram update object.
        _context: Callback context (unused).
    """
    import io

    import httpx

    query = update.callback_query
    if not query or not query.data:
        return

    result_id = query.data.replace("dl_torrent_", "")
    result = get_cached_result(result_id)

    if not result:
        await query.answer("Результат не найден", show_alert=True)
        return

    torrent_url = result.get("torrent_url", "")
    title = result.get("title", "Unknown")
    magnet = result.get("magnet", "")

    message = query.message

    if torrent_url:
        await query.answer("Загружаю торрент-файл...")
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
                response = await client.get(
                    torrent_url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    },
                )
                if response.status_code == 200:
                    # Create safe filename
                    safe_title = "".join(c for c in title[:50] if c.isalnum() or c in " ._-")
                    filename = f"{safe_title}.torrent"

                    if message:
                        await message.reply_document(
                            document=io.BytesIO(response.content),
                            filename=filename,
                            caption="📥 Торрент-файл",
                        )
                    logger.info(
                        "torrent_file_sent",
                        result_id=result_id,
                        user_id=query.from_user.id if query.from_user else None,
                    )
                    return
                logger.warning(
                    "torrent_download_failed",
                    status=response.status_code,
                    url=torrent_url,
                )
        except Exception as e:
            logger.warning("torrent_download_error", error=str(e), url=torrent_url)

    # Fallback: show magnet link (with lazy loading if needed)
    if isinstance(magnet, list):
        magnet = magnet[0] if magnet else ""

    if not magnet:
        # Try to fetch magnet via authenticated Rutracker client
        torrent_id = result.get("torrent_id", "")
        source = result.get("source", "")
        telegram_id = update.effective_user.id if update.effective_user else None

        # Try Rutracker authenticated client first (for rutracker source)
        if torrent_id and source == "rutracker":
            magnet = await _fetch_magnet_via_rutracker(torrent_id, telegram_id)
        else:
            # Fallback to torrent file extraction for other sources
            torrent_url_fallback = result.get("torrent_url", "")
            magnet = await _fetch_magnet_from_torrent_url(torrent_url_fallback, title)

        if magnet:
            result["magnet"] = magnet
            cache_search_result(result_id, result)

    await query.answer("Торрент-файл недоступен")
    if message and magnet:
        await message.reply_text(
            f"📥 Торрент-файл недоступен для этого источника.\n\n"
            f"🔗 Магнет-ссылка:\n<code>{magnet[:3500]}</code>",
            parse_mode="HTML",
        )
    elif message:
        await message.reply_text(
            "📥 Торрент-файл и магнет-ссылка недоступны для этого источника.",
        )


async def handle_seedbox_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle seedbox button callback - send torrent to configured seedbox.

    If magnet is not in cache, fetches it from TorAPI details endpoint.

    Args:
        update: Telegram update object.
        _context: Callback context (unused).
    """
    query = update.callback_query
    if not query or not query.data:
        return

    result_id = query.data.replace("dl_seedbox_", "")
    result = get_cached_result(result_id)

    if not result:
        await query.answer("Результат не найден", show_alert=True)
        return

    magnet = result.get("magnet", "")
    title = result.get("title", "Unknown")

    # Ensure magnet is a string
    if isinstance(magnet, list):
        magnet = magnet[0] if magnet else ""

    # If magnet is empty, try to fetch via authenticated Rutracker client
    if not magnet:
        torrent_id = result.get("torrent_id", "")
        source = result.get("source", "")
        telegram_id = update.effective_user.id if update.effective_user else None

        await query.answer("Загружаю магнет-ссылку...")

        # Try Rutracker authenticated client first (for rutracker source)
        if torrent_id and source == "rutracker":
            magnet = await _fetch_magnet_via_rutracker(torrent_id, telegram_id)
        else:
            # Fallback to torrent file extraction for other sources
            torrent_url = result.get("torrent_url", "")
            magnet = await _fetch_magnet_from_torrent_url(torrent_url, title)

        if magnet:
            # Update cache with fetched magnet
            result["magnet"] = magnet
            cache_search_result(result_id, result)
        else:
            await query.answer("Магнет-ссылка недоступна", show_alert=True)
            return

    await query.answer("Отправляю на seedbox...")
    message = query.message
    telegram_id = query.from_user.id if query.from_user else None

    # Try to send to seedbox (user's first, then global fallback)
    try:
        download_result = await send_magnet_to_user_seedbox(magnet, telegram_id)

        if download_result.get("status") == "sent":
            seedbox_label = "ваш seedbox" if download_result.get("user_seedbox") else "seedbox"
            if message:
                await message.reply_text(
                    f"✅ Торрент добавлен на {seedbox_label}\n\n"
                    f"<b>{title[:60]}</b>\n"
                    f"📡 {download_result.get('seedbox', 'Unknown')}",
                    parse_mode="HTML",
                )
            logger.info(
                "seedbox_sent",
                result_id=result_id,
                user_id=telegram_id,
                user_seedbox=download_result.get("user_seedbox"),
            )

            # Track torrent for sync monitoring
            torrent_hash = download_result.get("hash")
            if torrent_hash and telegram_id:
                try:
                    async with get_storage() as storage:
                        user = await storage.get_user_by_telegram_id(telegram_id)
                        if user:
                            await storage.track_torrent(
                                user_id=user.id,
                                torrent_hash=torrent_hash,
                                torrent_name=title,
                            )
                except Exception as e:
                    logger.warning("track_torrent_failed", error=str(e))

            # Record the download for follow-up
            if query.from_user:
                await _record_download(query.from_user.id, result, magnet)
            return

        # Handle specific error from user's seedbox
        if download_result.get("status") == "error" and download_result.get("user_seedbox"):
            if message:
                await message.reply_text(
                    f"⚠️ {download_result.get('error', 'Ошибка seedbox')}\n\n"
                    f"Проверьте настройки: /seedbox\n\n"
                    f"🔗 Магнет-ссылка:\n<code>{magnet[:3500]}</code>",
                    parse_mode="HTML",
                )
            return

    except Exception as e:
        logger.warning("seedbox_send_failed", error=str(e))

    # Seedbox not configured or error - show magnet link
    if message:
        await message.reply_text(
            f"⚠️ Seedbox не настроен или недоступен.\n\n"
            f"Настройте: /seedbox\n\n"
            f"🔗 Магнет-ссылка:\n<code>{magnet[:3500]}</code>",
            parse_mode="HTML",
        )


async def handle_followup_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle follow-up button callbacks for download feedback.

    Callback patterns:
    - followup_yes_{download_id}: User liked the download
    - followup_no_{download_id}: User didn't like the download
    - followup_rate_{download_id}: User wants to rate 1-10

    Args:
        update: Telegram update object.
        _context: Callback context (unused).
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    query = update.callback_query
    if not query or not query.data:
        return

    callback_data = query.data
    message = query.message

    # Parse callback data
    if callback_data.startswith("followup_yes_"):
        download_id = int(callback_data.replace("followup_yes_", ""))
        await query.answer("Спасибо за отзыв!")

        try:
            async with get_storage() as storage:
                download = await storage.get_download(download_id)
                if download:
                    # Mark as answered positively
                    await storage.mark_followup_answered(download_id, rating=8.0)

                    # Add positive fact to learnings
                    user = await storage.get_user(download.user_id)
                    if user:
                        await storage.create_memory_note(
                            user_id=user.id,
                            content=f"Понравилось: {download.title}",
                            source="followup",
                            keywords=["liked", download.title.lower()[:30]],
                            confidence=0.8,
                        )

            if message:
                await message.edit_text(
                    "✅ Отлично, что понравилось!\n\nЗапомню это для будущих рекомендаций.",
                    parse_mode="HTML",
                )

            logger.info("followup_positive", download_id=download_id)

        except Exception as e:
            logger.warning("followup_yes_failed", error=str(e), download_id=download_id)

    elif callback_data.startswith("followup_no_"):
        download_id = int(callback_data.replace("followup_no_", ""))
        await query.answer()

        try:
            async with get_storage() as storage:
                download = await storage.get_download(download_id)
                if download:
                    await storage.mark_followup_answered(download_id, rating=3.0)

                    # Ask if they want to block similar content
                    if message:
                        buttons = [
                            [
                                InlineKeyboardButton(
                                    "Больше не рекомендуй такое",
                                    callback_data=f"followup_block_{download_id}",
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "Просто не понравилось",
                                    callback_data=f"followup_dismiss_{download_id}",
                                ),
                            ],
                        ]
                        keyboard = InlineKeyboardMarkup(buttons)

                        await message.edit_text(
                            "😔 Жаль, что не понравилось.\n\n"
                            "Хочешь, чтобы я больше не рекомендовал подобное?",
                            reply_markup=keyboard,
                            parse_mode="HTML",
                        )

            logger.info("followup_negative", download_id=download_id)

        except Exception as e:
            logger.warning("followup_no_failed", error=str(e), download_id=download_id)

    elif callback_data.startswith("followup_rate_"):
        download_id = int(callback_data.replace("followup_rate_", ""))
        await query.answer()

        # Show rating buttons 1-10
        buttons = []
        row = []
        for i in range(1, 11):
            row.append(
                InlineKeyboardButton(
                    str(i),
                    callback_data=f"followup_rating_{download_id}_{i}",
                )
            )
            if len(row) == 5:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        keyboard = InlineKeyboardMarkup(buttons)

        if message:
            await message.edit_text(
                "Оцени по шкале от 1 до 10:",
                reply_markup=keyboard,
            )

    elif callback_data.startswith("followup_rating_"):
        parts = callback_data.replace("followup_rating_", "").split("_")
        download_id = int(parts[0])
        rating = float(parts[1])

        await query.answer(f"Оценка {int(rating)} сохранена!")

        try:
            async with get_storage() as storage:
                download = await storage.get_download(download_id)
                if download:
                    await storage.mark_followup_answered(download_id, rating=rating)

                    # Save rating to memory
                    user = await storage.get_user(download.user_id)
                    if user:
                        sentiment = "понравилось" if rating >= 7 else "не понравилось"
                        await storage.create_memory_note(
                            user_id=user.id,
                            content=f"{download.title}: оценка {int(rating)}/10 ({sentiment})",
                            source="followup",
                            keywords=["rating", download.title.lower()[:30]],
                            confidence=0.9,
                        )

            if message:
                emoji = (
                    "🌟" if rating >= 8 else "👍" if rating >= 6 else "🤔" if rating >= 4 else "👎"
                )
                await message.edit_text(
                    f"{emoji} Оценка {int(rating)}/10 сохранена!\n\n"
                    f"Учту это для будущих рекомендаций.",
                    parse_mode="HTML",
                )

            logger.info("followup_rated", download_id=download_id, rating=rating)

        except Exception as e:
            logger.warning("followup_rating_failed", error=str(e), download_id=download_id)

    elif callback_data.startswith("followup_block_"):
        download_id = int(callback_data.replace("followup_block_", ""))
        await query.answer("Запомнил!")

        try:
            async with get_storage() as storage:
                download = await storage.get_download(download_id)
                if download:
                    # Add to blocklist (title for now, could be extended)
                    user = await storage.get_user(download.user_id)
                    if user:
                        await storage.add_to_blocklist(
                            user_id=user.id,
                            block_type="title",
                            block_value=download.title,
                            block_level="dont_recommend",
                            notes="Didn't like it (from follow-up)",
                        )

            if message:
                await message.edit_text(
                    "🚫 Запомнил! Больше не буду рекомендовать подобное.",
                    parse_mode="HTML",
                )

            logger.info("followup_blocked", download_id=download_id)

        except Exception as e:
            logger.warning("followup_block_failed", error=str(e), download_id=download_id)

    elif callback_data.startswith("followup_dismiss_"):
        download_id = int(callback_data.replace("followup_dismiss_", ""))
        await query.answer("Понял!")

        if message:
            await message.edit_text(
                "👌 Понял, просто не понравилось. Буду иметь в виду!",
                parse_mode="HTML",
            )

        logger.info("followup_dismissed", download_id=download_id)


async def handle_monitor_callback(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle monitoring button callbacks (download, details, cancel).

    Callback patterns:
    - monitor_download_{monitor_id}: Download the found release
    - monitor_details_{monitor_id}: Show release details
    - monitor_cancel_{monitor_id}: Cancel monitoring
    """
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    callback_data = query.data
    user = update.effective_user
    if not user:
        return

    logger.info("monitor_callback", user_id=user.id, callback=callback_data)

    # Parse callback data
    if callback_data.startswith("monitor_download_"):
        monitor_id = int(callback_data.replace("monitor_download_", ""))
        await _handle_monitor_download(query, user.id, monitor_id)
    elif callback_data.startswith("monitor_details_"):
        monitor_id = int(callback_data.replace("monitor_details_", ""))
        await _handle_monitor_details(query, user.id, monitor_id)
    elif callback_data.startswith("monitor_cancel_"):
        monitor_id = int(callback_data.replace("monitor_cancel_", ""))
        await _handle_monitor_cancel(query, user.id, monitor_id)


async def _handle_monitor_download(query: Any, telegram_id: int, monitor_id: int) -> None:
    """Handle download button for monitoring notification."""
    async with get_storage() as storage:
        # Get monitor with found_data
        monitor = await storage.get_monitor(monitor_id)
        if not monitor:
            await query.edit_message_text("Монитор не найден.")
            return

        # Verify ownership
        db_user = await storage.get_user_by_telegram_id(telegram_id)
        if not db_user or monitor.user_id != db_user.id:
            await query.edit_message_text("У вас нет доступа к этому монитору.")
            return

        # Check if we have found_data with magnet
        if not monitor.found_data or not monitor.found_data.get("magnet"):
            await query.edit_message_text("Данные о релизе не найдены. Попробуйте поиск вручную.")
            return

        magnet = monitor.found_data["magnet"]

        # Validate magnet - reject placeholders
        if not is_valid_magnet(magnet):
            logger.warning("invalid_monitor_magnet", monitor_id=monitor_id, magnet=magnet[:50])
            await query.edit_message_text(
                "Magnet-ссылка недействительна. Попробуйте поиск вручную."
            )
            return
        title = monitor.title
        quality = monitor.found_data.get("quality", "")
        size = monitor.found_data.get("size", "")

        # Try to send to seedbox (user's first, then global fallback)
        try:
            result = await send_magnet_to_user_seedbox(magnet, telegram_id)
            if result.get("status") == "sent":
                torrent_hash = result.get("hash", "")[:8]
                seedbox_label = "ваш seedbox" if result.get("user_seedbox") else "seedbox"
                await query.edit_message_text(
                    f"**{title}** ({quality}, {size})\n\n"
                    f"✅ Отправлено на {seedbox_label}\n"
                    f"Hash: `{torrent_hash}...`",
                    parse_mode="Markdown",
                )
                return
        except Exception as e:
            logger.warning("seedbox_send_failed", error=str(e))

        # Seedbox not available - show magnet link
        try:
            await query.edit_message_text(
                f"<b>{title}</b> ({quality}, {size})\n\n"
                f"Скопируйте magnet-ссылку:\n<code>{magnet}</code>",
                parse_mode="HTML",
            )
        except Exception:
            await query.edit_message_text(f"{title}\n\nMagnet:\n{magnet}")


async def _handle_monitor_details(query: Any, telegram_id: int, monitor_id: int) -> None:
    """Handle details button for monitoring notification."""
    async with get_storage() as storage:
        monitor = await storage.get_monitor(monitor_id)
        if not monitor:
            await query.edit_message_text("Монитор не найден.")
            return

        # Verify ownership
        db_user = await storage.get_user_by_telegram_id(telegram_id)
        if not db_user or monitor.user_id != db_user.id:
            await query.edit_message_text("У вас нет доступа к этому монитору.")
            return

        # Build details message
        found_data = monitor.found_data or {}
        details = [
            f"**{monitor.title}**",
            f"Тип: {monitor.media_type}",
            f"Качество: {monitor.quality}",
            f"Статус: {monitor.status}",
        ]

        if found_data:
            details.append("")
            details.append("**Найденный релиз:**")
            if found_data.get("torrent_title"):
                details.append(f"Название: {found_data['torrent_title']}")
            if found_data.get("quality"):
                details.append(f"Качество: {found_data['quality']}")
            if found_data.get("size"):
                details.append(f"Размер: {found_data['size']}")
            if found_data.get("seeds"):
                details.append(f"Сиды: {found_data['seeds']}")
            if found_data.get("source"):
                details.append(f"Источник: {found_data['source'].title()}")

        if monitor.found_at:
            details.append(f"\nНайден: {monitor.found_at.strftime('%d.%m.%Y %H:%M')}")

        # Keep action buttons
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬇️ Скачать",
                        callback_data=f"monitor_download_{monitor_id}",
                    ),
                    InlineKeyboardButton(
                        "🔕 Отменить",
                        callback_data=f"monitor_cancel_{monitor_id}",
                    ),
                ],
            ]
        )

        await query.edit_message_text(
            "\n".join(details),
            parse_mode="Markdown",
            reply_markup=keyboard,
        )


async def _handle_monitor_cancel(query: Any, telegram_id: int, monitor_id: int) -> None:
    """Handle cancel button for monitoring notification."""
    async with get_storage() as storage:
        monitor = await storage.get_monitor(monitor_id)
        if not monitor:
            await query.edit_message_text("Монитор не найден.")
            return

        # Verify ownership
        db_user = await storage.get_user_by_telegram_id(telegram_id)
        if not db_user or monitor.user_id != db_user.id:
            await query.edit_message_text("У вас нет доступа к этому монитору.")
            return

        title = monitor.title

        # Delete the monitor
        deleted = await storage.delete_monitor(monitor_id)
        if deleted:
            await query.edit_message_text(
                f"🔕 Мониторинг **{title}** отменён.",
                parse_mode="Markdown",
            )
            logger.info("monitor_cancelled", user_id=telegram_id, monitor_id=monitor_id)
        else:
            await query.edit_message_text("Не удалось отменить мониторинг.")
