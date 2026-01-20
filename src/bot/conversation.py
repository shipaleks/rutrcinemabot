"""Natural language conversation handler for the Media Concierge Bot.

This module provides the integration between Telegram messages, Claude AI,
and the various tools (search, media metadata, user profile, seedbox).

It enables natural language queries like "найди Дюну в 4K" to be understood
by Claude, which then uses the appropriate tools to search and return results.
"""

import contextlib
import json
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.ai.claude_client import ClaudeClient, ConversationContext
from src.ai.tools import ToolExecutor, get_tool_definitions
from src.bot.streaming import send_streaming_message
from src.config import settings
from src.logger import get_logger
from src.media.kinopoisk import KinopoiskClient, KinopoiskError
from src.media.tmdb import TMDBClient, TMDBError
from src.search.piratebay import PirateBayClient, PirateBayError
from src.search.rutracker import RutrackerClient, RutrackerError
from src.seedbox import send_magnet_to_seedbox
from src.user.profile import ProfileManager
from src.user.storage import UserStorage, get_storage

logger = get_logger(__name__)

# Default database path
DEFAULT_DB_PATH = Path("data/users.db")

# Store conversation contexts per user
_conversation_contexts: dict[int, ConversationContext] = {}

# Store search results for download callbacks
_search_results_cache: dict[str, dict[str, Any]] = {}


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


def cache_search_result(result_id: str, result_data: dict[str, Any]) -> None:
    """Cache a search result for later download.

    Args:
        result_id: Unique ID for the result.
        result_data: Result data including magnet link.
    """
    _search_results_cache[result_id] = result_data
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
                            "source": "rutracker",
                        },
                    )
                    formatted_results.append(
                        {
                            "id": result_id,
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
                    },
                )
                formatted_results.append(
                    {
                        "id": result_id,
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


async def handle_piratebay_search(tool_input: dict[str, Any]) -> str:
    """Handle piratebay_search tool call.

    Args:
        tool_input: Tool parameters (query, quality, min_seeds).

    Returns:
        JSON string with search results.
    """
    query = tool_input.get("query", "")
    min_seeds = tool_input.get("min_seeds", 5)

    logger.info("piratebay_search", query=query, min_seeds=min_seeds)

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
                    },
                )
                formatted_results.append(
                    {
                        "id": result_id,
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
            elif media_type == "tv":
                results = await client.search_tv(query, year=year)
            else:
                results = await client.search_multi(query)

            results = results[:5]  # Limit results

            # Format results for Claude
            formatted_results = []
            for result in results:
                overview = result.overview or ""
                formatted_results.append(
                    {
                        "id": result.id,
                        "title": result.title,
                        "media_type": result.media_type,
                        "year": result.get_year(),
                        "overview": (overview[:200] + "...") if len(overview) > 200 else overview,
                        "vote_average": result.vote_average,
                        "poster_url": result.get_poster_url(),
                    }
                )

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
                    "directors": [{"name": d.name} for d in directors],
                    "writers": [{"name": w.name} for w in writers],
                    "cast": [{"name": p.name, "character": p.character} for p in top_cast],
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

        async with UserStorage(DEFAULT_DB_PATH, encryption_key) as storage:
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

    logger.info("seedbox_download", name=name, has_magnet=bool(magnet))

    result = await send_magnet_to_seedbox(magnet)

    if result.get("status") == "sent":
        return json.dumps(
            {
                "status": "success",
                "message": f"Торрент '{name}' добавлен на seedbox",
                "torrent_hash": result.get("hash"),
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
    user_id = tool_input.get("user_id")

    if user_id is None:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

    logger.info("read_user_profile", user_id=user_id)

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
    user_id = tool_input.get("user_id")
    section = tool_input.get("section")
    content = tool_input.get("content")

    if not all([user_id, section, content]):
        return json.dumps(
            {"status": "error", "error": "user_id, section, and content are required"},
            ensure_ascii=False,
        )

    logger.info("update_user_profile", user_id=user_id, section=section)

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


async def handle_add_to_watchlist(tool_input: dict[str, Any]) -> str:
    """Handle add_to_watchlist tool call."""
    user_id = tool_input.get("user_id")
    tmdb_id = tool_input.get("tmdb_id")
    media_type = tool_input.get("media_type", "movie")
    title = tool_input.get("title", "Unknown")
    year = tool_input.get("year")
    priority = tool_input.get("priority", 0)
    notes = tool_input.get("notes")

    if not user_id or not tmdb_id:
        return json.dumps(
            {"status": "error", "error": "user_id and tmdb_id are required"},
            ensure_ascii=False,
        )

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
    user_id = tool_input.get("user_id")
    tmdb_id = tool_input.get("tmdb_id")

    if not user_id or not tmdb_id:
        return json.dumps(
            {"status": "error", "error": "user_id and tmdb_id are required"},
            ensure_ascii=False,
        )

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
    user_id = tool_input.get("user_id")
    media_type = tool_input.get("media_type")
    limit = tool_input.get("limit", 20)

    if not user_id:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

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
    user_id = tool_input.get("user_id")
    tmdb_id = tool_input.get("tmdb_id")
    media_type = tool_input.get("media_type", "movie")
    title = tool_input.get("title", "Unknown")
    year = tool_input.get("year")
    rating = tool_input.get("rating")
    review = tool_input.get("review")

    if not user_id or not tmdb_id:
        return json.dumps(
            {"status": "error", "error": "user_id and tmdb_id are required"},
            ensure_ascii=False,
        )

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
    user_id = tool_input.get("user_id")
    tmdb_id = tool_input.get("tmdb_id")
    rating = tool_input.get("rating")
    review = tool_input.get("review")

    if not user_id or not tmdb_id or rating is None:
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
    user_id = tool_input.get("user_id")
    media_type = tool_input.get("media_type")
    limit = tool_input.get("limit", 20)

    if not user_id:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

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
    user_id = tool_input.get("user_id")
    block_type = tool_input.get("block_type")
    block_value = tool_input.get("block_value")
    block_level = tool_input.get("block_level", "dont_recommend")
    notes = tool_input.get("notes")

    if not all([user_id, block_type, block_value]):
        return json.dumps(
            {"status": "error", "error": "user_id, block_type, and block_value are required"},
            ensure_ascii=False,
        )

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
    user_id = tool_input.get("user_id")
    block_type = tool_input.get("block_type")

    if not user_id:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

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


async def handle_create_monitor(tool_input: dict[str, Any]) -> str:
    """Handle create_monitor tool call."""
    user_id = tool_input.get("user_id")
    title = tool_input.get("title")
    tmdb_id = tool_input.get("tmdb_id")
    media_type = tool_input.get("media_type", "movie")
    quality = tool_input.get("quality", "1080p")
    auto_download = tool_input.get("auto_download", False)

    if not user_id or not title:
        return json.dumps(
            {"status": "error", "error": "user_id and title are required"},
            ensure_ascii=False,
        )

    logger.info("create_monitor", user_id=user_id, title=title, quality=quality)

    try:
        async with get_storage() as storage:
            monitor = await storage.create_monitor(
                user_id=user_id,
                title=title,
                tmdb_id=tmdb_id,
                media_type=media_type,
                quality=quality,
                auto_download=auto_download,
            )

            return json.dumps(
                {
                    "status": "success",
                    "message": f"Мониторинг '{title}' создан",
                    "monitor_id": monitor.id,
                    "quality": quality,
                    "auto_download": auto_download,
                },
                ensure_ascii=False,
            )

    except Exception as e:
        logger.warning("create_monitor_failed", error=str(e))
        return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


async def handle_get_monitors(tool_input: dict[str, Any]) -> str:
    """Handle get_monitors tool call."""
    user_id = tool_input.get("user_id")
    status = tool_input.get("status")

    if not user_id:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

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
            deleted = await storage.delete_monitor(monitor_id)

            if deleted:
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
    user_id = tool_input.get("user_id")
    role = tool_input.get("role")
    min_films = tool_input.get("min_films", 2)

    if not user_id:
        return json.dumps({"status": "error", "error": "user_id is required"}, ensure_ascii=False)

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

    user_id = tool_input.get("user_id")
    letterboxd_username = tool_input.get("letterboxd_username")

    if not user_id or not letterboxd_username:
        return json.dumps(
            {"status": "error", "error": "user_id and letterboxd_username are required"},
            ensure_ascii=False,
        )

    sync_watchlist = tool_input.get("sync_watchlist", True)
    sync_diary = tool_input.get("sync_diary", True)
    diary_limit = tool_input.get("diary_limit", 50)

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


def create_tool_executor(telegram_id: int | None = None) -> ToolExecutor:
    """Create and configure a tool executor with all handlers.

    Args:
        telegram_id: Telegram user ID for per-user credentials.

    Returns:
        Configured ToolExecutor instance.
    """
    executor = ToolExecutor()

    # Create wrapper for rutracker handler to pass telegram_id
    async def rutracker_handler(tool_input: dict[str, Any]) -> str:
        return await handle_rutracker_search(tool_input, telegram_id=telegram_id)

    executor.register_handlers(
        {
            # Core search tools
            "rutracker_search": rutracker_handler,
            "piratebay_search": handle_piratebay_search,
            "tmdb_search": handle_tmdb_search,
            "tmdb_credits": handle_tmdb_credits,
            "kinopoisk_search": handle_kinopoisk_search,
            # User profile tools
            "get_user_profile": handle_get_user_profile,
            "read_user_profile": handle_read_user_profile,
            "update_user_profile": handle_update_user_profile,
            # Download tools
            "seedbox_download": handle_seedbox_download,
            # Watchlist tools
            "add_to_watchlist": handle_add_to_watchlist,
            "remove_from_watchlist": handle_remove_from_watchlist,
            "get_watchlist": handle_get_watchlist,
            # Watch history & ratings
            "mark_watched": handle_mark_watched,
            "rate_content": handle_rate_content,
            "get_watch_history": handle_get_watch_history,
            # Blocklist tools
            "add_to_blocklist": handle_add_to_blocklist,
            "get_blocklist": handle_get_blocklist,
            # Monitoring tools
            "create_monitor": handle_create_monitor,
            "get_monitors": handle_get_monitors,
            "cancel_monitor": handle_cancel_monitor,
            # Analytics tools
            "get_crew_stats": handle_get_crew_stats,
            # External service sync
            "letterboxd_sync": handle_letterboxd_sync,
        }
    )

    return executor


# =============================================================================
# Result Formatting
# =============================================================================


def format_search_results_keyboard(results: list[dict[str, Any]]) -> InlineKeyboardMarkup:
    """Create inline keyboard with download buttons for search results.

    Args:
        results: List of search result dicts with id and title.

    Returns:
        InlineKeyboardMarkup with download buttons.
    """
    buttons = []
    for result in results[:5]:  # Max 5 buttons
        result_id = result.get("id", "")
        title = result.get("title", "Unknown")
        # Truncate title for button
        short_title = title[:30] + "..." if len(title) > 30 else title
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"📥 {short_title}",
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

    logger.info(
        "message_received",
        user_id=user.id,
        username=user.username,
        message_length=len(user_message),
    )

    # Get conversation context
    conv_context = get_conversation_context(user.id)

    # Set telegram user ID for tool calls
    conv_context.telegram_user_id = user.id

    # Load user preferences into context
    try:
        encryption_key = None
        if settings.encryption_key:
            encryption_key = settings.encryption_key.get_secret_value()

        async with UserStorage(DEFAULT_DB_PATH, encryption_key) as storage:
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
    except Exception as e:
        logger.warning("failed_to_load_preferences", error=str(e))

    # Create Claude client with tools (pass telegram_id for per-user credentials)
    executor = create_tool_executor(telegram_id=user.id)
    client = ClaudeClient(
        tools=get_tool_definitions(),
        tool_executor=executor,
    )

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

        # Check if response contains search results that need buttons
        # Claude's response will mention result IDs if it wants to offer downloads
        if any(rid in response_text for rid in _search_results_cache):
            # Find mentioned results
            mentioned_results = []
            for result_id, result_data in _search_results_cache.items():
                if result_id in response_text:
                    mentioned_results.append(
                        {
                            "id": result_id,
                            "title": result_data.get("title", "Unknown"),
                        }
                    )

            if mentioned_results:
                keyboard = format_search_results_keyboard(mentioned_results)
                await update.message.reply_text(
                    "📥 Выберите раздачу для скачивания:",
                    reply_markup=keyboard,
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

    logger.info(
        "download_requested",
        user_id=query.from_user.id if query.from_user else None,
        result_id=result_id,
        title=title,
    )

    # Try to send to seedbox
    download_result = await send_magnet_to_seedbox(magnet)

    if download_result.get("status") == "sent":
        await query.edit_message_text(
            f"✅ Торрент добавлен на скачивание!\n\n"
            f"📥 **{title}**\n\n"
            f"Скачивание началось на вашем seedbox.",
            parse_mode="Markdown",
        )
    else:
        # Seedbox not configured - show magnet link
        # Split magnet link if too long for message
        message = query.message
        if len(magnet) > 4000:
            await query.edit_message_text(
                f"📥 **{title}**\n\nСкопируйте magnet-ссылку ниже:",
                parse_mode="Markdown",
            )
            if message:
                await message.reply_text(f"`{magnet}`", parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"📥 **{title}**\n\nСкопируйте magnet-ссылку:\n`{magnet}`",
                parse_mode="Markdown",
            )
