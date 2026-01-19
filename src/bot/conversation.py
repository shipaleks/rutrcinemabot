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
from src.user.storage import UserStorage

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

    # Try to get per-user credentials first
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
            "rutracker_search": rutracker_handler,
            "piratebay_search": handle_piratebay_search,
            "tmdb_search": handle_tmdb_search,
            "tmdb_credits": handle_tmdb_credits,
            "kinopoisk_search": handle_kinopoisk_search,
            "get_user_profile": handle_get_user_profile,
            "seedbox_download": handle_seedbox_download,
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
            initial_text="🤔 Думаю...",
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
