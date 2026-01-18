"""User onboarding flow with inline buttons for preference setup.

This module provides:
- Welcome message with inline keyboard buttons
- User profile creation on /start
- Preference settings (video quality, audio language)
- Inline buttons for quick setup

Usage:
    # In handlers registration:
    application.add_handler(CommandHandler("start", onboarding_start_handler))
    application.add_handler(CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"))
    application.add_handler(CommandHandler("settings", settings_handler))
"""

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.config import settings
from src.user.storage import UserStorage

logger = structlog.get_logger(__name__)

# Database path
DB_PATH = "data/users.db"

# =============================================================================
# Keyboard Layouts
# =============================================================================

# Video quality options
VIDEO_QUALITY_OPTIONS = [
    ("720p", "onboard_quality_720p"),
    ("1080p", "onboard_quality_1080p"),
    ("4K", "onboard_quality_4K"),
]

# Audio language options
AUDIO_LANGUAGE_OPTIONS = [
    ("Русский", "onboard_audio_ru"),
    ("English", "onboard_audio_en"),
    ("Оригинал", "onboard_audio_original"),
]

# Genre options for preferences
GENRE_OPTIONS = [
    ("Фантастика", "onboard_genre_scifi"),
    ("Боевик", "onboard_genre_action"),
    ("Драма", "onboard_genre_drama"),
    ("Комедия", "onboard_genre_comedy"),
    ("Триллер", "onboard_genre_thriller"),
    ("Ужасы", "onboard_genre_horror"),
]


def get_welcome_keyboard() -> InlineKeyboardMarkup:
    """Create welcome message inline keyboard.

    Returns:
        InlineKeyboardMarkup with setup and skip buttons
    """
    keyboard = [
        [InlineKeyboardButton("🎬 Настроить предпочтения", callback_data="onboard_setup_start")],
        [InlineKeyboardButton("⏭ Пропустить настройку", callback_data="onboard_skip")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_quality_keyboard() -> InlineKeyboardMarkup:
    """Create video quality selection keyboard.

    Returns:
        InlineKeyboardMarkup with quality options
    """
    keyboard = [
        [
            InlineKeyboardButton(label, callback_data=callback)
            for label, callback in VIDEO_QUALITY_OPTIONS
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="onboard_back_welcome")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_audio_language_keyboard() -> InlineKeyboardMarkup:
    """Create audio language selection keyboard.

    Returns:
        InlineKeyboardMarkup with language options
    """
    keyboard = [
        [
            InlineKeyboardButton(label, callback_data=callback)
            for label, callback in AUDIO_LANGUAGE_OPTIONS
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="onboard_back_quality")],
    ]
    return InlineKeyboardMarkup(keyboard)


def get_genre_keyboard(selected_genres: list[str] | None = None) -> InlineKeyboardMarkup:
    """Create genre selection keyboard with multi-select.

    Args:
        selected_genres: List of currently selected genre keys

    Returns:
        InlineKeyboardMarkup with genre options and checkmarks
    """
    selected = selected_genres or []

    # Create genre buttons in rows of 2
    genre_buttons = []
    row = []
    for label, callback in GENRE_OPTIONS:
        genre_key = callback.replace("onboard_genre_", "")
        check = "✅ " if genre_key in selected else ""
        row.append(InlineKeyboardButton(f"{check}{label}", callback_data=callback))
        if len(row) == 2:
            genre_buttons.append(row)
            row = []
    if row:
        genre_buttons.append(row)

    # Add navigation buttons
    genre_buttons.append(
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="onboard_back_audio"),
            InlineKeyboardButton("✅ Готово", callback_data="onboard_complete"),
        ]
    )

    return InlineKeyboardMarkup(genre_buttons)


def get_settings_keyboard(
    current_quality: str | None = None,
    current_audio: str | None = None,
) -> InlineKeyboardMarkup:
    """Create settings menu keyboard.

    Args:
        current_quality: Current video quality preference
        current_audio: Current audio language preference

    Returns:
        InlineKeyboardMarkup with settings options
    """
    quality_display = current_quality or "1080p"
    audio_display = {
        "ru": "Русский",
        "en": "English",
        "original": "Оригинал",
    }.get(current_audio or "ru", current_audio or "Русский")

    keyboard = [
        [InlineKeyboardButton(f"📺 Качество: {quality_display}", callback_data="settings_quality")],
        [InlineKeyboardButton(f"🔊 Аудио: {audio_display}", callback_data="settings_audio")],
        [InlineKeyboardButton("🎭 Любимые жанры", callback_data="settings_genres")],
        [InlineKeyboardButton("❌ Закрыть", callback_data="settings_close")],
    ]
    return InlineKeyboardMarkup(keyboard)


# =============================================================================
# Message Templates
# =============================================================================

WELCOME_MESSAGE = """👋 Привет, {name}!

Я **Media Concierge Bot** — твой персональный помощник для поиска фильмов и сериалов.

🎬 **Что я умею:**
• Искать фильмы и сериалы по названию
• Находить торренты в нужном качестве
• Показывать рейтинги и информацию о фильмах
• Давать рекомендации по вкусу

💡 **Как использовать:**
Просто напиши мне, что хочешь посмотреть:
• _"Найди Дюну в 4K"_
• _"Что-то похожее на Интерстеллар"_
• _"Скачай Игру Престолов"_

Давай настроим бота под твои предпочтения!"""

QUALITY_SELECTION_MESSAGE = """📺 **Выбери предпочитаемое качество видео:**

• **720p** — для медленного интернета
• **1080p** — оптимальный баланс качества и размера
• **4K** — максимальное качество (большие файлы)"""

AUDIO_LANGUAGE_MESSAGE = """🔊 **Выбери предпочитаемый язык аудио:**

• **Русский** — дубляж на русском
• **English** — оригинальная озвучка
• **Оригинал** — язык оригинала фильма"""

GENRE_SELECTION_MESSAGE = """🎭 **Выбери любимые жанры:**

Нажми на жанры, которые тебе нравятся. Это поможет мне давать более точные рекомендации.

Можешь выбрать несколько жанров."""

SETUP_COMPLETE_MESSAGE = """✅ **Настройка завершена!**

Твои предпочтения сохранены:
• Качество: **{quality}**
• Аудио: **{audio}**
{genres_line}

Теперь просто напиши, что хочешь посмотреть, и я найду для тебя лучшие варианты!

Для изменения настроек используй /settings"""

SKIP_SETUP_MESSAGE = """👍 **Отлично!**

Я буду использовать стандартные настройки:
• Качество: **1080p**
• Аудио: **Русский**

Ты всегда можешь изменить настройки командой /settings

Теперь просто напиши, что хочешь посмотреть!"""

SETTINGS_MESSAGE = """⚙️ **Настройки**

Выбери, что хочешь изменить:"""


# =============================================================================
# Handler Functions
# =============================================================================


async def onboarding_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command with onboarding flow.

    Creates user profile if not exists and shows welcome message with buttons.

    Args:
        update: Telegram update object
        context: Callback context
    """
    user = update.effective_user
    message = update.message
    if not user or not message:
        return

    logger.info(
        "onboarding_start",
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )

    # Get or create user in database
    try:
        encryption_key = settings.encryption_key.get_secret_value()
        async with UserStorage(DB_PATH, encryption_key) as storage:
            db_user, created = await storage.get_or_create_user(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                language_code=user.language_code or "ru",
            )

            logger.info(
                "user_profile_handled",
                user_id=user.id,
                db_user_id=db_user.id,
                created=created,
            )
    except Exception as e:
        logger.exception("user_storage_error", user_id=user.id, error=str(e))
        # Continue with welcome message even if storage fails
        created = False

    # Store user info in context for later use
    if context.user_data is not None:
        context.user_data["telegram_id"] = user.id
        context.user_data["selected_genres"] = []

    # Build welcome message
    name = user.first_name or user.username or "пользователь"
    welcome_text = WELCOME_MESSAGE.format(name=name)

    try:
        await message.reply_text(
            welcome_text,
            parse_mode="Markdown",
            reply_markup=get_welcome_keyboard(),
        )
        logger.info("welcome_message_sent", user_id=user.id, new_user=created)
    except Exception as e:
        logger.exception("welcome_message_failed", user_id=user.id, error=str(e))
        # Fallback without markdown
        await message.reply_text(
            f"Привет, {name}! Я Media Concierge Bot. Используй /help для справки."
        )


async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /settings command.

    Shows current settings with buttons to change them.

    Args:
        update: Telegram update object
        context: Callback context
    """
    user = update.effective_user
    message = update.message
    if not user or not message:
        return

    logger.info("settings_command", user_id=user.id)

    # Get current preferences from database
    current_quality = "1080p"
    current_audio = "ru"

    try:
        encryption_key = settings.encryption_key.get_secret_value()
        async with UserStorage(DB_PATH, encryption_key) as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                prefs = await storage.get_preferences(db_user.id)
                if prefs:
                    current_quality = prefs.video_quality or "1080p"
                    current_audio = prefs.audio_language or "ru"
    except Exception as e:
        logger.exception("settings_fetch_error", user_id=user.id, error=str(e))

    try:
        await message.reply_text(
            SETTINGS_MESSAGE,
            parse_mode="Markdown",
            reply_markup=get_settings_keyboard(current_quality, current_audio),
        )
    except Exception as e:
        logger.exception("settings_message_failed", user_id=user.id, error=str(e))
        await message.reply_text("Настройки: /settings\nИспользуй кнопки для изменения.")


async def onboarding_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard callbacks for onboarding flow.

    Routes callbacks to appropriate handlers based on callback_data.

    Args:
        update: Telegram update object
        context: Callback context
    """
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()  # Acknowledge the callback

    user = update.effective_user
    callback_data = query.data

    logger.info(
        "onboarding_callback",
        user_id=user.id if user else None,
        callback_data=callback_data,
    )

    try:
        # Route to appropriate handler
        if callback_data == "onboard_setup_start":
            await _handle_setup_start(query, context)
        elif callback_data == "onboard_skip":
            await _handle_skip_setup(query, context)
        elif callback_data.startswith("onboard_quality_"):
            await _handle_quality_selection(query, context, callback_data)
        elif callback_data.startswith("onboard_audio_"):
            await _handle_audio_selection(query, context, callback_data)
        elif callback_data.startswith("onboard_genre_"):
            await _handle_genre_selection(query, context, callback_data)
        elif callback_data == "onboard_complete":
            await _handle_complete_setup(query, context)
        elif callback_data == "onboard_back_welcome":
            await _handle_back_to_welcome(query, context)
        elif callback_data == "onboard_back_quality":
            await _handle_back_to_quality(query, context)
        elif callback_data == "onboard_back_audio":
            await _handle_back_to_audio(query, context)
        # Settings callbacks
        elif callback_data == "settings_quality":
            await _handle_settings_quality(query, context)
        elif callback_data == "settings_audio":
            await _handle_settings_audio(query, context)
        elif callback_data == "settings_genres":
            await _handle_settings_genres(query, context)
        elif callback_data == "settings_close":
            await _handle_settings_close(query, context)
        elif callback_data.startswith("settings_set_quality_"):
            await _handle_set_quality(query, context, callback_data)
        elif callback_data.startswith("settings_set_audio_"):
            await _handle_set_audio(query, context, callback_data)
        else:
            logger.warning("unknown_callback", callback_data=callback_data)
    except Exception as e:
        logger.exception(
            "callback_handler_error",
            callback_data=callback_data,
            error=str(e),
        )


# =============================================================================
# Onboarding Flow Handlers
# =============================================================================


async def _handle_setup_start(query, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the preference setup flow."""
    await query.edit_message_text(
        QUALITY_SELECTION_MESSAGE,
        parse_mode="Markdown",
        reply_markup=get_quality_keyboard(),
    )


async def _handle_skip_setup(query, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Skip setup and use default preferences."""
    await query.edit_message_text(
        SKIP_SETUP_MESSAGE,
        parse_mode="Markdown",
    )


async def _handle_quality_selection(
    query, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle video quality selection."""
    quality = callback_data.replace("onboard_quality_", "")
    if context.user_data is not None:
        context.user_data["selected_quality"] = quality

    logger.info(
        "quality_selected",
        user_id=query.from_user.id,
        quality=quality,
    )

    # Move to audio language selection
    await query.edit_message_text(
        AUDIO_LANGUAGE_MESSAGE,
        parse_mode="Markdown",
        reply_markup=get_audio_language_keyboard(),
    )


async def _handle_audio_selection(
    query, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle audio language selection."""
    audio = callback_data.replace("onboard_audio_", "")
    if context.user_data is not None:
        context.user_data["selected_audio"] = audio

    logger.info(
        "audio_selected",
        user_id=query.from_user.id,
        audio=audio,
    )

    # Move to genre selection
    selected_genres = context.user_data.get("selected_genres", []) if context.user_data else []
    await query.edit_message_text(
        GENRE_SELECTION_MESSAGE,
        parse_mode="Markdown",
        reply_markup=get_genre_keyboard(selected_genres),
    )


async def _handle_genre_selection(
    query, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Handle genre selection (toggle)."""
    genre = callback_data.replace("onboard_genre_", "")
    selected_genres: list[str] = (
        context.user_data.get("selected_genres", []) if context.user_data else []
    )

    # Toggle genre selection
    if genre in selected_genres:
        selected_genres.remove(genre)
    else:
        selected_genres.append(genre)

    if context.user_data is not None:
        context.user_data["selected_genres"] = selected_genres

    logger.info(
        "genre_toggled",
        user_id=query.from_user.id,
        genre=genre,
        selected=selected_genres,
    )

    # Update keyboard with new selection state
    await query.edit_message_reply_markup(
        reply_markup=get_genre_keyboard(selected_genres),
    )


async def _handle_complete_setup(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Complete the setup and save preferences."""
    user = query.from_user

    # Get selected preferences
    quality = "1080p"
    audio = "ru"
    genres: list[str] = []
    if context.user_data is not None:
        quality = context.user_data.get("selected_quality", "1080p")
        audio = context.user_data.get("selected_audio", "ru")
        genres = context.user_data.get("selected_genres", [])

    # Map genre keys to display names
    genre_map = {
        "scifi": "Фантастика",
        "action": "Боевик",
        "drama": "Драма",
        "comedy": "Комедия",
        "thriller": "Триллер",
        "horror": "Ужасы",
    }
    genre_names = [genre_map.get(g, g) for g in genres]

    # Map audio to display
    audio_display = {
        "ru": "Русский",
        "en": "English",
        "original": "Оригинал",
    }.get(audio, audio)

    # Save preferences to database
    try:
        encryption_key = settings.encryption_key.get_secret_value()
        async with UserStorage(DB_PATH, encryption_key) as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                await storage.update_preferences(
                    user_id=db_user.id,
                    video_quality=quality,
                    audio_language=audio,
                    preferred_genres=genres,
                )
                logger.info(
                    "preferences_saved",
                    user_id=user.id,
                    quality=quality,
                    audio=audio,
                    genres=genres,
                )
    except Exception as e:
        logger.exception("preferences_save_error", user_id=user.id, error=str(e))

    # Build completion message
    genres_line = ""
    if genre_names:
        genres_line = f"• Жанры: **{', '.join(genre_names)}**"

    message = SETUP_COMPLETE_MESSAGE.format(
        quality=quality,
        audio=audio_display,
        genres_line=genres_line,
    )

    await query.edit_message_text(
        message,
        parse_mode="Markdown",
    )


async def _handle_back_to_welcome(query, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Go back to welcome message."""
    name = query.from_user.first_name or query.from_user.username or "пользователь"
    welcome_text = WELCOME_MESSAGE.format(name=name)

    await query.edit_message_text(
        welcome_text,
        parse_mode="Markdown",
        reply_markup=get_welcome_keyboard(),
    )


async def _handle_back_to_quality(query, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Go back to quality selection."""
    await query.edit_message_text(
        QUALITY_SELECTION_MESSAGE,
        parse_mode="Markdown",
        reply_markup=get_quality_keyboard(),
    )


async def _handle_back_to_audio(query, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Go back to audio language selection."""
    await query.edit_message_text(
        AUDIO_LANGUAGE_MESSAGE,
        parse_mode="Markdown",
        reply_markup=get_audio_language_keyboard(),
    )


# =============================================================================
# Settings Menu Handlers
# =============================================================================


async def _handle_settings_quality(query, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show quality selection in settings."""
    keyboard = [
        [
            InlineKeyboardButton(label, callback_data=f"settings_set_quality_{code}")
            for label, code in [("720p", "720p"), ("1080p", "1080p"), ("4K", "4K")]
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")],
    ]
    await query.edit_message_text(
        "📺 **Выбери качество видео:**",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_settings_audio(query, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show audio language selection in settings."""
    keyboard = [
        [
            InlineKeyboardButton("Русский", callback_data="settings_set_audio_ru"),
            InlineKeyboardButton("English", callback_data="settings_set_audio_en"),
            InlineKeyboardButton("Оригинал", callback_data="settings_set_audio_original"),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data="settings_back")],
    ]
    await query.edit_message_text(
        "🔊 **Выбери язык аудио:**",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def _handle_settings_genres(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show genre selection in settings."""
    # Get current genres from database
    user = query.from_user
    selected_genres = []

    try:
        encryption_key = settings.encryption_key.get_secret_value()
        async with UserStorage(DB_PATH, encryption_key) as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                prefs = await storage.get_preferences(db_user.id)
                if prefs and prefs.preferred_genres:
                    selected_genres = prefs.preferred_genres
    except Exception as e:
        logger.exception("genres_fetch_error", user_id=user.id, error=str(e))

    if context.user_data is not None:
        context.user_data["selected_genres"] = selected_genres

    # Use settings-specific keyboard
    genre_buttons = []
    row = []
    for label, callback in GENRE_OPTIONS:
        genre_key = callback.replace("onboard_genre_", "")
        check = "✅ " if genre_key in selected_genres else ""
        # Use settings prefix for genre callbacks
        row.append(
            InlineKeyboardButton(f"{check}{label}", callback_data=f"settings_genre_{genre_key}")
        )
        if len(row) == 2:
            genre_buttons.append(row)
            row = []
    if row:
        genre_buttons.append(row)

    genre_buttons.append(
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="settings_back"),
            InlineKeyboardButton("✅ Сохранить", callback_data="settings_save_genres"),
        ]
    )

    await query.edit_message_text(
        "🎭 **Выбери любимые жанры:**\n\nНажми на жанры для выбора.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(genre_buttons),
    )


async def _handle_settings_close(query, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Close settings menu."""
    await query.delete_message()


async def _handle_set_quality(
    query, _context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Save selected quality and return to settings."""
    user = query.from_user
    quality = callback_data.replace("settings_set_quality_", "")

    try:
        encryption_key = settings.encryption_key.get_secret_value()
        async with UserStorage(DB_PATH, encryption_key) as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                await storage.update_preferences(
                    user_id=db_user.id,
                    video_quality=quality,
                )
                logger.info("quality_updated", user_id=user.id, quality=quality)

                # Get updated preferences for display
                prefs = await storage.get_preferences(db_user.id)
                current_quality = prefs.video_quality if prefs else quality
                current_audio = prefs.audio_language if prefs else "ru"

                await query.edit_message_text(
                    f"✅ Качество изменено на **{quality}**\n\n" + SETTINGS_MESSAGE,
                    parse_mode="Markdown",
                    reply_markup=get_settings_keyboard(current_quality, current_audio),
                )
                return
    except Exception as e:
        logger.exception("quality_update_error", user_id=user.id, error=str(e))

    await query.edit_message_text(
        f"✅ Качество: **{quality}**",
        parse_mode="Markdown",
    )


async def _handle_set_audio(query, _context: ContextTypes.DEFAULT_TYPE, callback_data: str) -> None:
    """Save selected audio language and return to settings."""
    user = query.from_user
    audio = callback_data.replace("settings_set_audio_", "")

    audio_display = {
        "ru": "Русский",
        "en": "English",
        "original": "Оригинал",
    }.get(audio, audio)

    try:
        encryption_key = settings.encryption_key.get_secret_value()
        async with UserStorage(DB_PATH, encryption_key) as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                await storage.update_preferences(
                    user_id=db_user.id,
                    audio_language=audio,
                )
                logger.info("audio_updated", user_id=user.id, audio=audio)

                # Get updated preferences for display
                prefs = await storage.get_preferences(db_user.id)
                current_quality = prefs.video_quality if prefs else "1080p"
                current_audio = prefs.audio_language if prefs else audio

                await query.edit_message_text(
                    f"✅ Аудио изменено на **{audio_display}**\n\n" + SETTINGS_MESSAGE,
                    parse_mode="Markdown",
                    reply_markup=get_settings_keyboard(current_quality, current_audio),
                )
                return
    except Exception as e:
        logger.exception("audio_update_error", user_id=user.id, error=str(e))

    await query.edit_message_text(
        f"✅ Аудио: **{audio_display}**",
        parse_mode="Markdown",
    )


# Additional handler for settings genre toggle and save
async def settings_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle settings-specific callbacks.

    Args:
        update: Telegram update object
        context: Callback context
    """
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    user = query.from_user
    callback_data = query.data

    logger.info(
        "settings_callback",
        user_id=user.id if user else None,
        callback_data=callback_data,
    )

    try:
        if callback_data.startswith("settings_genre_"):
            await _handle_settings_genre_toggle(query, context, callback_data)
        elif callback_data == "settings_save_genres":
            await _handle_settings_save_genres(query, context)
        elif callback_data == "settings_back":
            await _handle_settings_back(query, context)
    except Exception as e:
        logger.exception(
            "settings_callback_error",
            callback_data=callback_data,
            error=str(e),
        )


async def _handle_settings_genre_toggle(
    query, context: ContextTypes.DEFAULT_TYPE, callback_data: str
) -> None:
    """Toggle genre in settings."""
    genre = callback_data.replace("settings_genre_", "")
    selected_genres: list[str] = (
        context.user_data.get("selected_genres", []) if context.user_data else []
    )

    if genre in selected_genres:
        selected_genres.remove(genre)
    else:
        selected_genres.append(genre)

    if context.user_data is not None:
        context.user_data["selected_genres"] = selected_genres

    # Rebuild keyboard
    genre_buttons = []
    row = []
    for label, callback in GENRE_OPTIONS:
        genre_key = callback.replace("onboard_genre_", "")
        check = "✅ " if genre_key in selected_genres else ""
        row.append(
            InlineKeyboardButton(f"{check}{label}", callback_data=f"settings_genre_{genre_key}")
        )
        if len(row) == 2:
            genre_buttons.append(row)
            row = []
    if row:
        genre_buttons.append(row)

    genre_buttons.append(
        [
            InlineKeyboardButton("⬅️ Назад", callback_data="settings_back"),
            InlineKeyboardButton("✅ Сохранить", callback_data="settings_save_genres"),
        ]
    )

    await query.edit_message_reply_markup(
        reply_markup=InlineKeyboardMarkup(genre_buttons),
    )


async def _handle_settings_save_genres(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save genre preferences."""
    user = query.from_user
    genres: list[str] = context.user_data.get("selected_genres", []) if context.user_data else []

    try:
        encryption_key = settings.encryption_key.get_secret_value()
        async with UserStorage(DB_PATH, encryption_key) as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                await storage.update_preferences(
                    user_id=db_user.id,
                    preferred_genres=genres,
                )
                logger.info("genres_saved", user_id=user.id, genres=genres)

                # Get updated preferences for display
                prefs = await storage.get_preferences(db_user.id)
                current_quality = prefs.video_quality if prefs else "1080p"
                current_audio = prefs.audio_language if prefs else "ru"

                await query.edit_message_text(
                    "✅ Жанры сохранены!\n\n" + SETTINGS_MESSAGE,
                    parse_mode="Markdown",
                    reply_markup=get_settings_keyboard(current_quality, current_audio),
                )
                return
    except Exception as e:
        logger.exception("genres_save_error", user_id=user.id, error=str(e))

    await query.edit_message_text(
        "✅ Жанры сохранены!",
        parse_mode="Markdown",
    )


async def _handle_settings_back(query, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Return to main settings menu."""
    user = query.from_user

    # Get current preferences
    current_quality = "1080p"
    current_audio = "ru"

    try:
        encryption_key = settings.encryption_key.get_secret_value()
        async with UserStorage(DB_PATH, encryption_key) as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                prefs = await storage.get_preferences(db_user.id)
                if prefs:
                    current_quality = prefs.video_quality or "1080p"
                    current_audio = prefs.audio_language or "ru"
    except Exception as e:
        logger.exception("settings_back_error", user_id=user.id, error=str(e))

    await query.edit_message_text(
        SETTINGS_MESSAGE,
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(current_quality, current_audio),
    )
