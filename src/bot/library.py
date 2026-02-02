"""NAS media library browser for the Telegram bot.

Provides /library command with inline keyboard navigation to browse
movies and TV shows stored on the NAS.

The VM periodically pushes a file index via POST /api/sync/library-index.
This module reads the index from the database.

Requires:
- User must have seedbox credentials (personal or global fallback)
- library_indexer.py running on VM (pushes index to bot)
"""

import json

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from src.bot.seedbox_auth import get_user_seedbox_credentials
from src.user.storage import get_storage

logger = structlog.get_logger(__name__)

PAGE_SIZE = 10


async def _get_index(category: str) -> list[dict]:
    """Load library index for a category from DB."""
    async with get_storage() as storage:
        raw = await storage.get_library_index(category)
    if not raw:
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


async def _get_counts() -> tuple[int, int]:
    """Get item counts for movies and tv."""
    movies = await _get_index("movies")
    tv = await _get_index("tv")
    return len(movies), len(tv)


async def _search_index(query: str) -> list[dict]:
    """Search across both categories by name substring."""
    query_lower = query.lower()
    results = []
    for category in ("movies", "tv"):
        items = await _get_index(category)
        for item in items:
            if query_lower in item.get("name", "").lower():
                results.append({**item, "category": category})
    results.sort(key=lambda x: x.get("name", "").lower())
    return results


async def _has_library_access(telegram_id: int) -> bool:
    """Check if user has access to the library (has seedbox credentials)."""
    host, _, _ = await get_user_seedbox_credentials(telegram_id)
    return host is not None


def _build_items_keyboard(
    items: list[dict],
    category: str,
    offset: int,
) -> InlineKeyboardMarkup:
    """Build paginated inline keyboard for directory listing."""
    page = items[offset : offset + PAGE_SIZE]
    keyboard = []

    for item in page:
        name = item["name"]
        if item["type"] == "dir":
            label = f"📁 {name}"
            callback = f"lib_dir_{category}:{name}"
            # Telegram callback_data max 64 bytes — truncate if needed
            if len(callback.encode("utf-8")) > 64:
                callback = f"lib_dir_{category}:{name[:20]}"
            keyboard.append([InlineKeyboardButton(label, callback_data=callback)])
        else:
            size = item.get("size_mb", 0)
            size_str = f"{size / 1024:.1f} GB" if size >= 1024 else f"{size} MB"
            label = f"🎬 {name[:40]} ({size_str})"
            keyboard.append([InlineKeyboardButton(label, callback_data="lib_noop")])

    # Navigation row
    nav = []
    if offset > 0:
        nav.append(
            InlineKeyboardButton(
                "◀️ Назад",
                callback_data=f"lib_page_{category}:{offset - PAGE_SIZE}",
            )
        )
    if offset + PAGE_SIZE < len(items):
        nav.append(
            InlineKeyboardButton(
                "Ещё ▶️",
                callback_data=f"lib_page_{category}:{offset + PAGE_SIZE}",
            )
        )
    if nav:
        keyboard.append(nav)

    keyboard.append([InlineKeyboardButton("🏠 В начало", callback_data="lib_home")])
    return InlineKeyboardMarkup(keyboard)


def _build_files_keyboard(
    items: list[dict],
    category: str,
    dir_name: str,
) -> InlineKeyboardMarkup:
    """Build keyboard showing files inside a directory."""
    keyboard = []
    for item in items[:20]:  # Cap at 20 files
        name = item.get("name", "")
        size = item.get("size_mb", 0)
        size_str = f"{size / 1024:.1f} GB" if size >= 1024 else f"{size} MB"
        label = f"🎬 {name[:35]} ({size_str})"
        keyboard.append([InlineKeyboardButton(label, callback_data="lib_noop")])

    keyboard.append([InlineKeyboardButton("⬆️ К списку", callback_data=f"lib_cat_{category}")])
    keyboard.append([InlineKeyboardButton("🏠 В начало", callback_data="lib_home")])
    return InlineKeyboardMarkup(keyboard)


def _build_search_results_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    """Build keyboard for search results."""
    keyboard = []
    for item in items[:PAGE_SIZE]:
        cat_icon = "🎬" if item.get("category") == "movies" else "📺"
        name = item["name"]
        category = item.get("category", "movies")
        label = f"{cat_icon} {name[:45]}"
        callback = f"lib_dir_{category}:{name}"
        if len(callback.encode("utf-8")) > 64:
            callback = f"lib_dir_{category}:{name[:20]}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

    keyboard.append([InlineKeyboardButton("🏠 В начало", callback_data="lib_home")])
    return InlineKeyboardMarkup(keyboard)


async def _show_home(edit_func, movies_count: int, tv_count: int) -> None:
    """Show home screen with category buttons."""
    keyboard = [
        [InlineKeyboardButton(f"🎬 Кино ({movies_count})", callback_data="lib_cat_movies")],
        [InlineKeyboardButton(f"📺 Сериалы ({tv_count})", callback_data="lib_cat_tv")],
        [InlineKeyboardButton("🔍 Поиск", callback_data="lib_search")],
    ]
    await edit_func(
        "📚 Медиатека",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def library_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /library command — show NAS media library."""
    telegram_id = update.effective_user.id

    if not await _has_library_access(telegram_id):
        await update.message.reply_text(
            "Для доступа к библиотеке нужен настроенный seedbox.\n"
            "Используйте /seedbox для настройки."
        )
        return

    movies_count, tv_count = await _get_counts()
    if movies_count == 0 and tv_count == 0:
        await update.message.reply_text("Библиотека пуста. Индекс ещё не получен от сервера.")
        return

    await _show_home(update.message.reply_text, movies_count, tv_count)


async def library_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle library inline keyboard callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data

    telegram_id = update.effective_user.id
    if not await _has_library_access(telegram_id):
        await query.edit_message_text("Доступ к библиотеке недоступен.")
        return

    if data == "lib_noop":
        return

    if data == "lib_home":
        movies_count, tv_count = await _get_counts()
        await _show_home(query.edit_message_text, movies_count, tv_count)

    elif data.startswith("lib_cat_"):
        category = data.removeprefix("lib_cat_")
        items = await _get_index(category)
        if not items:
            await query.edit_message_text("Папка пуста.")
            return
        cat_name = "Кино" if category == "movies" else "Сериалы"
        await query.edit_message_text(
            f"📂 {cat_name} ({len(items)})",
            reply_markup=_build_items_keyboard(items, category, 0),
        )

    elif data.startswith("lib_dir_"):
        rest = data.removeprefix("lib_dir_")
        category, _, dir_name = rest.partition(":")
        # Find the directory in index and show its files
        items = await _get_index(category)
        target = None
        for item in items:
            if item.get("name", "").lower().startswith(dir_name.lower()):
                target = item
                break
        if not target or target.get("type") != "dir":
            await query.edit_message_text(
                f"📂 {dir_name}\n\nПапка не найдена.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬆️ Назад", callback_data=f"lib_cat_{category}")]]
                ),
            )
            return
        files = target.get("items", [])
        if not files:
            await query.edit_message_text(
                f"📂 {target['name']}\n\nНет видеофайлов.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("⬆️ Назад", callback_data=f"lib_cat_{category}")]]
                ),
            )
            return
        await query.edit_message_text(
            f"📂 {target['name']} ({len(files)} файлов)",
            reply_markup=_build_files_keyboard(files, category, target["name"]),
        )

    elif data.startswith("lib_page_"):
        rest = data.removeprefix("lib_page_")
        category, _, offset_str = rest.rpartition(":")
        offset = int(offset_str)
        items = await _get_index(category)
        cat_name = "Кино" if category == "movies" else "Сериалы"
        await query.edit_message_text(
            f"📂 {cat_name} ({len(items)})",
            reply_markup=_build_items_keyboard(items, category, offset),
        )

    elif data == "lib_search":
        context.user_data["lib_awaiting_search"] = True
        await query.edit_message_text(
            "🔍 Введите название фильма или сериала для поиска:",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Отмена", callback_data="lib_home")]],
            ),
        )


async def library_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input when library search is active.

    Registered in a higher-priority group (-1). When search is active,
    processes the query and raises ApplicationHandlerStop to prevent
    the general message handler from running.
    """
    if not context.user_data.get("lib_awaiting_search"):
        return

    context.user_data.pop("lib_awaiting_search", None)
    query_text = update.message.text.strip()

    if not query_text:
        raise ApplicationHandlerStop

    items = await _search_index(query_text)
    if not items:
        await update.message.reply_text(
            f"По запросу «{query_text}» ничего не найдено.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("🔍 Искать ещё", callback_data="lib_search")],
                    [InlineKeyboardButton("🏠 В начало", callback_data="lib_home")],
                ]
            ),
        )
        raise ApplicationHandlerStop

    await update.message.reply_text(
        f"🔍 Результаты поиска «{query_text}» ({len(items)}):",
        reply_markup=_build_search_results_keyboard(items),
    )
    raise ApplicationHandlerStop
