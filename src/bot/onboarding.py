"""User onboarding flow - clean, professional setup.

This module provides:
- Welcome with feature explanation
- Letterboxd export import (optional, upload ZIP file)
- Favorite movies question (if no Letterboxd data)
- Rutracker credentials setup
- Quality/audio preferences

Usage:
    application.add_handler(CommandHandler("start", onboarding_start_handler))
    application.add_handler(CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"))
"""

import contextlib

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters

from src.bot.conversation import get_conversation_context
from src.user.storage import get_storage

logger = structlog.get_logger(__name__)

# Conversation states
WAITING_LETTERBOXD_FILE = 1
WAITING_MOVIES = 2
WAITING_DEVICE = 3
WAITING_AUDIO_SETUP = 4
WAITING_VIEWING_PARTNERS = 5
WAITING_RUTRACKER_USER = 6
WAITING_RUTRACKER_PASS = 7


# =============================================================================
# Message Templates (no emojis, professional tone)
# =============================================================================

WELCOME_MESSAGE = """Привет, {name}.

Я твой персональный помощник для поиска фильмов и сериалов.

**Что я умею:**
- Искать и скачивать фильмы с торрент-трекеров
- Показывать информацию, рейтинги, рекомендации
- Отслеживать твой watchlist и историю просмотров
- Импортировать данные из Letterboxd
- Уведомлять, когда появляется нужный релиз

**Как пользоваться:**
Просто пиши, что хочешь посмотреть. Например:
- "Найди Дюну в 4K"
- "Что-нибудь похожее на Интерстеллар"
- "Добавь Оппенгеймер в watchlist"

Давай настроим бота под тебя."""

LETTERBOXD_QUESTION = """**Letterboxd**

Если у тебя есть аккаунт Letterboxd, я могу импортировать историю просмотров и оценки.

**Как экспортировать:**
1. Зайди на letterboxd.com/settings/data/
2. Нажми "Export Your Data"
3. Скачай ZIP-файл и отправь его сюда

Это позволит мне сразу понять твои вкусы: любимые фильмы, что не понравилось, стиль твоих рецензий.

Нажми "Пропустить", если нет аккаунта."""

LETTERBOXD_PROCESSING = """Обрабатываю экспорт Letterboxd..."""

MOVIES_QUESTION = """**Любимые фильмы**

Назови 2-3 фильма, которые тебе нравятся. Это поможет мне понять твои предпочтения.

Просто напиши названия через запятую."""

DEVICE_QUESTION = """**Устройство**

На чём обычно смотришь фильмы?"""

AUDIO_SETUP_QUESTION = """**Звук**

Какое аудио оборудование используешь?"""

VIEWING_PARTNERS_QUESTION = """**Компания**

С кем обычно смотришь?"""

RUTRACKER_QUESTION = """**Rutracker**

Для поиска на Rutracker нужна авторизация. Если у тебя есть аккаунт, введи логин.

Твои данные хранятся в зашифрованном виде и используются только для поиска.

Нажми "Пропустить", если не хочешь подключать Rutracker сейчас."""

RUTRACKER_PASSWORD = """Теперь введи пароль от Rutracker.

Пароль будет зашифрован и использован только для авторизации на трекере."""

QUALITY_QUESTION = """**Качество видео**

Какое качество предпочитаешь по умолчанию?"""

AUDIO_QUESTION = """**Язык аудио**

Какой язык аудио предпочитаешь?"""

SETUP_COMPLETE = """**Готово**

Настройки сохранены{letterboxd_note}{movies_note}{rutracker_note}

Качество: {quality}
Аудио: {audio}{device_note}{viewing_note}

Теперь просто напиши, что хочешь посмотреть. Для изменения настроек: /settings"""

SKIP_MESSAGE = """Хорошо, используем стандартные настройки.

Качество: 1080p
Аудио: Русский

Можешь изменить в любой момент через /settings

Напиши, что хочешь посмотреть."""


# =============================================================================
# Keyboards
# =============================================================================


def get_welcome_keyboard() -> InlineKeyboardMarkup:
    """Welcome screen keyboard."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Настроить", callback_data="onboard_setup")],
            [InlineKeyboardButton("Пропустить", callback_data="onboard_skip")],
        ]
    )


def get_skip_keyboard() -> InlineKeyboardMarkup:
    """Skip button keyboard."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Пропустить", callback_data="onboard_skip_step")],
        ]
    )


def get_quality_keyboard() -> InlineKeyboardMarkup:
    """Quality selection keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("720p", callback_data="onboard_quality_720p"),
                InlineKeyboardButton("1080p", callback_data="onboard_quality_1080p"),
                InlineKeyboardButton("4K", callback_data="onboard_quality_4K"),
            ],
        ]
    )


def get_audio_keyboard() -> InlineKeyboardMarkup:
    """Audio language keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Русский", callback_data="onboard_audio_ru"),
                InlineKeyboardButton("English", callback_data="onboard_audio_en"),
                InlineKeyboardButton("Оригинал", callback_data="onboard_audio_original"),
            ],
        ]
    )


# Device options for viewing
DEVICE_OPTIONS = [
    ("tv_large", 'ТВ большой (55"+)'),
    ("tv", "ТВ"),
    ("projector", "Проектор"),
    ("laptop", "Ноутбук"),
    ("mobile", "Телефон/Планшет"),
]

# Audio setup options
AUDIO_SETUP_OPTIONS = [
    ("builtin", "Встроенные динамики"),
    ("soundbar", "Soundbar"),
    ("headphones", "Наушники"),
    ("hometheater", "Домашний кинотеатр 5.1+"),
]

# Viewing partners options
VIEWING_PARTNERS_OPTIONS = [
    ("solo", "Один"),
    ("partner", "С партнёром"),
    ("family", "С семьёй (есть дети)"),
    ("friends", "С друзьями"),
]


def get_device_keyboard() -> InlineKeyboardMarkup:
    """Device selection keyboard."""
    rows = []
    for device_id, device_name in DEVICE_OPTIONS:
        rows.append(
            [InlineKeyboardButton(device_name, callback_data=f"onboard_device_{device_id}")]
        )
    rows.append([InlineKeyboardButton("Пропустить", callback_data="onboard_skip_step")])
    return InlineKeyboardMarkup(rows)


def get_audio_setup_keyboard() -> InlineKeyboardMarkup:
    """Audio setup selection keyboard."""
    rows = []
    for audio_id, audio_name in AUDIO_SETUP_OPTIONS:
        rows.append(
            [InlineKeyboardButton(audio_name, callback_data=f"onboard_audiosetup_{audio_id}")]
        )
    rows.append([InlineKeyboardButton("Пропустить", callback_data="onboard_skip_step")])
    return InlineKeyboardMarkup(rows)


def get_viewing_partners_keyboard() -> InlineKeyboardMarkup:
    """Viewing partners selection keyboard."""
    rows = []
    for partner_id, partner_name in VIEWING_PARTNERS_OPTIONS:
        rows.append(
            [InlineKeyboardButton(partner_name, callback_data=f"onboard_partner_{partner_id}")]
        )
    rows.append([InlineKeyboardButton("Пропустить", callback_data="onboard_skip_step")])
    return InlineKeyboardMarkup(rows)


def get_settings_keyboard(
    current_quality: str = "1080p",
    current_audio: str = "ru",
) -> InlineKeyboardMarkup:
    """Settings menu keyboard."""
    audio_display = {"ru": "Русский", "en": "English", "original": "Оригинал"}.get(
        current_audio, current_audio
    )
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"Качество: {current_quality}", callback_data="settings_quality"
                )
            ],
            [InlineKeyboardButton(f"Аудио: {audio_display}", callback_data="settings_audio")],
            [InlineKeyboardButton("Letterboxd", callback_data="settings_letterboxd")],
            [InlineKeyboardButton("Rutracker", callback_data="settings_rutracker")],
            [InlineKeyboardButton("Закрыть", callback_data="settings_close")],
        ]
    )


# =============================================================================
# Entity Deep Link Handler
# =============================================================================


async def _handle_entity_deep_link(update: Update, param: str) -> bool:
    """Handle entity deep link parameter from /start command.

    Args:
        update: Telegram update object
        param: Deep link parameter (e.g., "p_137427", "m_693134", "t_1396")

    Returns:
        True if handled as entity link, False otherwise
    """
    from src.bot.entity_cards import format_movie_card, format_person_card, format_tv_card

    message = update.message
    if not message or not message.from_user:
        return False

    user_id = message.from_user.id

    # Parse entity type and ID
    if param.startswith("p_"):
        # Person card
        try:
            person_id = int(param[2:])
            logger.info("entity_deep_link", type="person", id=person_id)
            caption, photo_url = await format_person_card(person_id)
            await _send_entity_card(message, caption, photo_url)
            # Add to conversation context so Claude knows what user is viewing
            _add_entity_view_to_context(user_id, "person", person_id, caption)
            return True
        except ValueError:
            logger.warning("entity_deep_link_invalid_id", param=param)
            await message.reply_text("Некорректная ссылка на персону.")
            return True
        except Exception as e:
            logger.warning("person_card_failed", error=str(e), param=param)
            await message.reply_text("Не удалось загрузить информацию о персоне.")
            return True

    elif param.startswith("m_"):
        # Movie card
        try:
            movie_id = int(param[2:])
            logger.info("entity_deep_link", type="movie", id=movie_id)
            caption, photo_url = await format_movie_card(movie_id)
            await _send_entity_card(message, caption, photo_url)
            # Add to conversation context so Claude knows what user is viewing
            _add_entity_view_to_context(user_id, "movie", movie_id, caption)
            return True
        except ValueError:
            logger.warning("entity_deep_link_invalid_id", param=param)
            await message.reply_text("Некорректная ссылка на фильм.")
            return True
        except Exception as e:
            logger.warning("movie_card_failed", error=str(e), param=param)
            await message.reply_text("Не удалось загрузить информацию о фильме.")
            return True

    elif param.startswith("t_"):
        # TV show card
        try:
            tv_id = int(param[2:])
            logger.info("entity_deep_link", type="tv", id=tv_id)
            caption, photo_url = await format_tv_card(tv_id)
            await _send_entity_card(message, caption, photo_url)
            # Add to conversation context so Claude knows what user is viewing
            _add_entity_view_to_context(user_id, "tv", tv_id, caption)
            return True
        except ValueError:
            logger.warning("entity_deep_link_invalid_id", param=param)
            await message.reply_text("Некорректная ссылка на сериал.")
            return True
        except Exception as e:
            logger.warning("tv_card_failed", error=str(e), param=param)
            await message.reply_text("Не удалось загрузить информацию о сериале.")
            return True

    return False


def _add_entity_view_to_context(
    user_id: int, entity_type: str, entity_id: int, caption: str
) -> None:
    """Add entity view to conversation context.

    This helps Claude understand what the user was just looking at
    when they say things like "find torrent for this".

    Args:
        user_id: Telegram user ID
        entity_type: "person", "movie", or "tv"
        entity_id: TMDB ID of the entity
        caption: HTML caption of the entity card (to extract name)
    """
    import re

    # Extract entity name from caption (first <b>...</b> block)
    name_match = re.search(r"<b>([^<]+)</b>", caption)
    entity_name = name_match.group(1) if name_match else f"ID {entity_id}"

    # Get or create conversation context
    context = get_conversation_context(user_id)

    # Create a system-style message about what user is viewing
    type_names = {"person": "персону", "movie": "фильм", "tv": "сериал"}
    type_name = type_names.get(entity_type, entity_type)

    # Add as user message so Claude sees it in conversation flow
    view_message = (
        f'[Пользователь просматривает карточку: {type_name} "{entity_name}" '
        f"(TMDB {entity_type}_id: {entity_id})]"
    )
    context.add_message("user", view_message)

    logger.info(
        "entity_view_added_to_context",
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
    )


async def _send_entity_card(message, caption: str, photo_url: str | None) -> None:
    """Send entity card as photo with caption or text only.

    Args:
        message: Telegram message object to reply to
        caption: HTML-formatted caption text
        photo_url: URL of the photo to send, or None for text-only
    """
    if photo_url:
        try:
            await message.reply_photo(
                photo=photo_url,
                caption=caption,
                parse_mode="HTML",
            )
        except Exception as e:
            # Fallback to text if photo fails
            logger.warning("entity_card_photo_failed", error=str(e))
            await message.reply_text(caption, parse_mode="HTML")
    else:
        await message.reply_text(caption, parse_mode="HTML")


# =============================================================================
# Handlers
# =============================================================================


async def onboarding_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start command with optional entity deep link."""
    user = update.effective_user
    message = update.message
    if not user or not message:
        return ConversationHandler.END

    # Check for entity deep link parameter (e.g., /start p_137427)
    if context.args and len(context.args) == 1:
        param = context.args[0]
        if await _handle_entity_deep_link(update, param):
            return ConversationHandler.END

    logger.info("onboarding_start", user_id=user.id, username=user.username)

    # Create user in database
    try:
        async with get_storage() as storage:
            db_user, created = await storage.get_or_create_user(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
                language_code=user.language_code or "ru",
            )
            logger.info("user_handled", user_id=user.id, db_id=db_user.id, created=created)
    except Exception as e:
        logger.exception("user_storage_error", user_id=user.id, error=str(e))

    # Initialize context
    if context.user_data is not None:
        context.user_data["telegram_id"] = user.id
        context.user_data["setup_step"] = "welcome"

    name = user.first_name or user.username or "друг"

    await message.reply_text(
        WELCOME_MESSAGE.format(name=name),
        parse_mode="Markdown",
        reply_markup=get_welcome_keyboard(),
    )
    return ConversationHandler.END


async def onboarding_callback_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    """Handle onboarding callbacks."""
    query = update.callback_query
    if not query or not query.data:
        return None

    await query.answer()

    user = update.effective_user
    callback = query.data

    logger.info("onboarding_callback", user_id=user.id if user else None, callback=callback)

    try:
        # Welcome screen
        if callback == "onboard_setup":
            return await _start_setup(query, context)
        if callback == "onboard_skip":
            return await _skip_all(query, context)

        # Skip current step
        if callback == "onboard_skip_step":
            return await _skip_current_step(query, context)

        # Device selection
        if callback.startswith("onboard_device_"):
            device = callback.replace("onboard_device_", "")
            return await _save_device(query, context, device)

        # Audio setup selection
        if callback.startswith("onboard_audiosetup_"):
            audio_setup = callback.replace("onboard_audiosetup_", "")
            return await _save_audio_setup(query, context, audio_setup)

        # Viewing partners selection
        if callback.startswith("onboard_partner_"):
            partner = callback.replace("onboard_partner_", "")
            return await _save_viewing_partners(query, context, partner)

        # Quality selection
        if callback.startswith("onboard_quality_"):
            quality = callback.replace("onboard_quality_", "")
            return await _save_quality(query, context, quality)

        # Audio selection
        if callback.startswith("onboard_audio_"):
            audio = callback.replace("onboard_audio_", "")
            return await _save_audio_and_complete(query, context, audio)

    except Exception as e:
        logger.exception("onboarding_error", callback=callback, error=str(e))

    return None


async def _start_setup(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start the setup flow with Letterboxd question."""
    if context.user_data is not None:
        context.user_data["setup_step"] = "letterboxd"

    await query.edit_message_text(
        LETTERBOXD_QUESTION,
        parse_mode="Markdown",
        reply_markup=get_skip_keyboard(),
    )
    return WAITING_LETTERBOXD_FILE


async def _skip_all(query, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Skip entire setup."""
    await query.edit_message_text(SKIP_MESSAGE, parse_mode="Markdown")
    return ConversationHandler.END


async def _skip_current_step(query, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Skip current step and move to next."""
    step = context.user_data.get("setup_step") if context.user_data else None

    if step == "letterboxd":
        # No Letterboxd data, ask for favorite movies
        if context.user_data is not None:
            context.user_data["setup_step"] = "movies"
        await query.edit_message_text(
            MOVIES_QUESTION,
            parse_mode="Markdown",
            reply_markup=get_skip_keyboard(),
        )
        return WAITING_MOVIES

    if step == "movies":
        # Move to device selection
        if context.user_data is not None:
            context.user_data["setup_step"] = "device"
        await query.edit_message_text(
            DEVICE_QUESTION,
            parse_mode="Markdown",
            reply_markup=get_device_keyboard(),
        )
        return WAITING_DEVICE

    if step == "device":
        # Move to audio setup selection
        if context.user_data is not None:
            context.user_data["setup_step"] = "audio_setup"
        await query.edit_message_text(
            AUDIO_SETUP_QUESTION,
            parse_mode="Markdown",
            reply_markup=get_audio_setup_keyboard(),
        )
        return WAITING_AUDIO_SETUP

    if step == "audio_setup":
        # Move to viewing partners
        if context.user_data is not None:
            context.user_data["setup_step"] = "viewing_partners"
        await query.edit_message_text(
            VIEWING_PARTNERS_QUESTION,
            parse_mode="Markdown",
            reply_markup=get_viewing_partners_keyboard(),
        )
        return WAITING_VIEWING_PARTNERS

    if step == "viewing_partners":
        # Move to Rutracker
        if context.user_data is not None:
            context.user_data["setup_step"] = "rutracker"
        await query.edit_message_text(
            RUTRACKER_QUESTION,
            parse_mode="Markdown",
            reply_markup=get_skip_keyboard(),
        )
        return WAITING_RUTRACKER_USER

    if step == "rutracker":
        # Move to quality
        if context.user_data is not None:
            context.user_data["setup_step"] = "quality"
        await query.edit_message_text(
            QUALITY_QUESTION,
            parse_mode="Markdown",
            reply_markup=get_quality_keyboard(),
        )
        return None

    return None


async def _save_device(query, context: ContextTypes.DEFAULT_TYPE, device: str) -> int:
    """Save device preference and move to audio setup."""
    if context.user_data is not None:
        context.user_data["primary_device"] = device
        context.user_data["setup_step"] = "audio_setup"

    await query.edit_message_text(
        AUDIO_SETUP_QUESTION,
        parse_mode="Markdown",
        reply_markup=get_audio_setup_keyboard(),
    )
    return WAITING_AUDIO_SETUP


async def _save_audio_setup(query, context: ContextTypes.DEFAULT_TYPE, audio_setup: str) -> int:
    """Save audio setup preference and move to viewing partners."""
    if context.user_data is not None:
        context.user_data["audio_setup"] = audio_setup
        context.user_data["setup_step"] = "viewing_partners"

    await query.edit_message_text(
        VIEWING_PARTNERS_QUESTION,
        parse_mode="Markdown",
        reply_markup=get_viewing_partners_keyboard(),
    )
    return WAITING_VIEWING_PARTNERS


async def _save_viewing_partners(query, context: ContextTypes.DEFAULT_TYPE, partner: str) -> int:
    """Save viewing partners preference and move to Rutracker."""
    if context.user_data is not None:
        context.user_data["viewing_partners"] = partner
        context.user_data["setup_step"] = "rutracker"

    await query.edit_message_text(
        RUTRACKER_QUESTION,
        parse_mode="Markdown",
        reply_markup=get_skip_keyboard(),
    )
    return WAITING_RUTRACKER_USER


async def _save_quality(query, context: ContextTypes.DEFAULT_TYPE, quality: str) -> None:
    """Save quality and show audio selection."""
    if context.user_data is not None:
        context.user_data["quality"] = quality
        context.user_data["setup_step"] = "audio"

    await query.edit_message_text(
        AUDIO_QUESTION,
        parse_mode="Markdown",
        reply_markup=get_audio_keyboard(),
    )


async def _save_audio_and_complete(query, context: ContextTypes.DEFAULT_TYPE, audio: str) -> int:
    """Save audio and complete setup."""
    user = query.from_user

    quality = context.user_data.get("quality", "1080p") if context.user_data else "1080p"
    letterboxd_stats = context.user_data.get("letterboxd_stats") if context.user_data else None
    movies = context.user_data.get("favorite_movies") if context.user_data else None
    has_rutracker = context.user_data.get("has_rutracker", False) if context.user_data else False

    # Get watch context data
    primary_device = context.user_data.get("primary_device") if context.user_data else None
    audio_setup = context.user_data.get("audio_setup") if context.user_data else None
    viewing_partners = context.user_data.get("viewing_partners") if context.user_data else None

    # Save to database
    try:
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                await storage.update_preferences(
                    user_id=db_user.id,
                    video_quality=quality,
                    audio_language=audio,
                )

                # Initialize Core Memory with onboarding data
                try:
                    from src.user.memory import CoreMemoryManager

                    memory_manager = CoreMemoryManager(storage)

                    # Build watch context content
                    device_names = dict(DEVICE_OPTIONS)
                    audio_setup_names = dict(AUDIO_SETUP_OPTIONS)
                    partner_names = dict(VIEWING_PARTNERS_OPTIONS)

                    device_display = device_names.get(primary_device, "") if primary_device else ""
                    audio_setup_display = (
                        audio_setup_names.get(audio_setup, "") if audio_setup else ""
                    )
                    partner_display = (
                        partner_names.get(viewing_partners, "") if viewing_partners else ""
                    )

                    preferences_content = f"Video quality: {quality}\nAudio: {audio}"
                    if device_display:
                        preferences_content += f"\nDevice: {device_display}"
                    if audio_setup_display:
                        preferences_content += f"\nAudio setup: {audio_setup_display}"
                    if partner_display:
                        preferences_content += f"\nUsually watches: {partner_display}"

                    await memory_manager.update_block(
                        db_user.id, "preferences", preferences_content, operation="replace"
                    )

                    logger.info("core_memory_initialized", user_id=user.id)
                except Exception as mem_error:
                    logger.warning("core_memory_init_failed", error=str(mem_error))

                logger.info(
                    "preferences_saved",
                    user_id=user.id,
                    quality=quality,
                    audio=audio,
                    device=primary_device,
                    audio_setup=audio_setup,
                    viewing_partners=viewing_partners,
                )
    except Exception as e:
        logger.exception("save_preferences_error", user_id=user.id, error=str(e))

    # Build completion message
    audio_display = {"ru": "Русский", "en": "English", "original": "Оригинал"}.get(audio, audio)

    letterboxd_note = ""
    if letterboxd_stats:
        letterboxd_note = f"\nLetterboxd: {letterboxd_stats}"

    movies_note = ""
    if movies:
        movies_note = f"\nЛюбимые фильмы: {movies}"

    rutracker_note = ""
    if has_rutracker:
        rutracker_note = "\nRutracker: подключён"

    # Build device/viewing note
    device_note = ""
    if primary_device:
        device_names = dict(DEVICE_OPTIONS)
        device_note = f"\nУстройство: {device_names.get(primary_device, primary_device)}"

    viewing_note = ""
    if viewing_partners:
        partner_names = dict(VIEWING_PARTNERS_OPTIONS)
        viewing_note = f"\nСмотрит: {partner_names.get(viewing_partners, viewing_partners)}"

    message = SETUP_COMPLETE.format(
        quality=quality,
        audio=audio_display,
        letterboxd_note=letterboxd_note,
        movies_note=movies_note,
        rutracker_note=rutracker_note,
        device_note=device_note,
        viewing_note=viewing_note,
    )

    await query.edit_message_text(message, parse_mode="Markdown")

    # Generate instant hidden gem recommendation if user has enough data
    if letterboxd_stats or movies:
        try:
            await _send_instant_recommendation(query, user.id)
        except Exception as e:
            logger.warning("instant_recommendation_failed", user_id=user.id, error=str(e))

    return ConversationHandler.END


# =============================================================================
# Instant Recommendation After Setup
# =============================================================================


async def _send_instant_recommendation(query, telegram_id: int) -> None:
    """Generate and send a personalized recommendation after setup.

    Args:
        query: Telegram callback query for sending messages.
        telegram_id: User's Telegram ID.
    """
    import json

    from src.bot.conversation import handle_get_hidden_gem

    try:
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(telegram_id)
            if not db_user:
                return

            # Call the hidden gem handler
            result_json = await handle_get_hidden_gem({"user_id": db_user.id})
            result = json.loads(result_json)

            if result.get("status") != "success":
                logger.info(
                    "instant_recommendation_skipped",
                    user_id=telegram_id,
                    reason=result.get("error", "unknown"),
                )
                return

            rec = result.get("recommendation", {})
            title = rec.get("title", "")
            year = rec.get("year", "")
            reason = rec.get("reason", "")
            director = rec.get("director", "")

            if not title:
                return

            # Build recommendation message
            message_parts = [
                "**Рекомендация на основе твоих вкусов:**",
                "",
                f"**{title}** ({year})",
            ]

            if director:
                message_parts.append(f"Режиссёр: {director}")

            if reason:
                message_parts.append("")
                message_parts.append(reason)

            message_parts.append("")
            message_parts.append("_Напиши название, чтобы найти и скачать._")

            await query.message.reply_text(
                "\n".join(message_parts),
                parse_mode="Markdown",
            )

            logger.info(
                "instant_recommendation_sent",
                user_id=telegram_id,
                title=title,
            )

    except Exception as e:
        logger.warning("instant_recommendation_error", user_id=telegram_id, error=str(e))


# =============================================================================
# Letterboxd Data Storage Helper
# =============================================================================


async def _save_letterboxd_to_storage(
    storage,
    user_id: int,
    analysis,
) -> tuple[int, int, int, int]:
    """Save Letterboxd films to watched and watchlist tables.

    Args:
        storage: Storage instance
        user_id: Internal user ID
        analysis: LetterboxdExportAnalysis object

    Returns:
        Tuple of (watched_saved, watched_skipped, watchlist_saved, watchlist_skipped)
    """
    watched_saved = 0
    watched_skipped = 0
    watchlist_saved = 0
    watchlist_skipped = 0

    # Collect all watched films (favorites, loved, liked, disliked, hated)
    all_watched = (
        analysis.favorites + analysis.loved + analysis.liked + analysis.disliked + analysis.hated
    )

    # Save watched films
    for film in all_watched:
        try:
            # Check if already in watched history
            watched_list = await storage.get_watched(user_id, limit=500)
            is_duplicate = any(
                w.title.lower() == film.name.lower() and w.year == film.year for w in watched_list
            )

            if is_duplicate:
                watched_skipped += 1
                continue

            # Convert Letterboxd rating (0.5-5.0) to 1-10 scale
            rating_10 = None
            if film.rating is not None:
                rating_10 = round(film.rating * 2, 1)

            await storage.add_watched(
                user_id=user_id,
                media_type="movie",
                title=film.name,
                year=film.year,
                rating=rating_10,
                review=film.review,
                watched_at=film.watched_date or film.logged_date,
            )
            watched_saved += 1
        except Exception as e:
            logger.warning("letterboxd_save_watched_error", film=film.name, error=str(e))
            watched_skipped += 1

    # Save watchlist films
    for film in analysis.watchlist:
        try:
            # Check if already in watchlist
            existing_watchlist = await storage.get_watchlist(user_id, limit=500)
            is_duplicate = any(
                w.title.lower() == film.name.lower() and w.year == film.year
                for w in existing_watchlist
            )

            if is_duplicate:
                watchlist_skipped += 1
                continue

            await storage.add_to_watchlist(
                user_id=user_id,
                media_type="movie",
                title=film.name,
                year=film.year,
            )
            watchlist_saved += 1
        except Exception as e:
            logger.warning("letterboxd_save_watchlist_error", film=film.name, error=str(e))
            watchlist_skipped += 1

    logger.info(
        "letterboxd_storage_saved",
        user_id=user_id,
        watched_saved=watched_saved,
        watched_skipped=watched_skipped,
        watchlist_saved=watchlist_saved,
        watchlist_skipped=watchlist_skipped,
    )

    return watched_saved, watched_skipped, watchlist_saved, watchlist_skipped


# =============================================================================
# File Upload Handler (Letterboxd Export)
# =============================================================================


async def handle_letterboxd_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Letterboxd export ZIP file upload."""
    user = update.effective_user
    message = update.message
    if not message or not message.document:
        return WAITING_LETTERBOXD_FILE

    document = message.document
    logger.info(
        "letterboxd_file_received",
        user_id=user.id if user else None,
        file_name=document.file_name,
        file_size=document.file_size,
    )

    # Validate file
    if not document.file_name or not document.file_name.endswith(".zip"):
        await message.reply_text(
            "Нужен ZIP-файл с экспортом Letterboxd.\nСкачай его на letterboxd.com/settings/data/",
            reply_markup=get_skip_keyboard(),
        )
        return WAITING_LETTERBOXD_FILE

    # Size limit (5MB should be plenty)
    if document.file_size and document.file_size > 5 * 1024 * 1024:
        await message.reply_text(
            "Файл слишком большой. Максимум 5MB.",
            reply_markup=get_skip_keyboard(),
        )
        return WAITING_LETTERBOXD_FILE

    # Download and process
    processing_msg = await message.reply_text(LETTERBOXD_PROCESSING)

    try:
        # Download file
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()

        # Parse export
        from src.services.letterboxd_export import (
            LetterboxdExportParser,
            extract_review_style,
        )

        parser = LetterboxdExportParser()
        analysis = parser.parse_zip(bytes(file_bytes))

        # Save to profile
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                # Save Letterboxd data to Core Memory
                from src.user.memory import CoreMemoryManager

                memory_manager = CoreMemoryManager(storage)

                # Build preferences content from Letterboxd analysis
                prefs_parts = []
                if analysis.favorites:
                    top_films = ", ".join(f.name for f in analysis.favorites[:5])
                    prefs_parts.append(f"Favorites: {top_films}")

                if analysis.average_rating:
                    if analysis.average_rating >= 4.0:
                        prefs_parts.append("Rating style: generous")
                    elif analysis.average_rating <= 2.5:
                        prefs_parts.append("Rating style: critical")

                # Add review style if available
                review_style = extract_review_style(analysis.review_samples)
                if review_style:
                    prefs_parts.append(f"Review style: {review_style}")

                if prefs_parts:
                    letterboxd_prefs = "\n".join(prefs_parts)
                    await memory_manager.update_block(
                        db_user.id,
                        "preferences",
                        letterboxd_prefs,
                        operation="append",
                    )

                # Save disliked films to blocklist
                if analysis.hated or analysis.disliked:
                    disliked_all = analysis.hated + analysis.disliked[:5]
                    disliked_text = ", ".join(f"{f.name}" for f in disliked_all[:10])
                    blocklist_content = f"Disliked films: {disliked_text}"
                    await memory_manager.update_block(
                        db_user.id,
                        "blocklist",
                        blocklist_content,
                        operation="append",
                    )

                logger.info(
                    "letterboxd_saved_to_core_memory",
                    user_id=user.id,
                    has_favorites=bool(analysis.favorites),
                    has_disliked=bool(analysis.hated or analysis.disliked),
                )

                # Save films to watched and watchlist tables
                (
                    watched_saved,
                    watched_skipped,
                    watchlist_saved,
                    watchlist_skipped,
                ) = await _save_letterboxd_to_storage(storage, db_user.id, analysis)
                logger.info(
                    "letterboxd_data_saved_to_tables",
                    user_id=user.id,
                    watched_saved=watched_saved,
                    watchlist_saved=watchlist_saved,
                )

                # Extract learnings from Letterboxd data
                try:
                    from src.user.memory import LearningDetector

                    detector = LearningDetector(storage)
                    learnings = await detector.analyze_letterboxd_data(db_user.id, analysis)
                    if learnings:
                        logger.info(
                            "letterboxd_learnings_extracted",
                            user_id=user.id,
                            learnings_count=len(learnings),
                        )
                except Exception as e:
                    logger.warning(
                        "letterboxd_learning_detection_error",
                        user_id=user.id,
                        error=str(e),
                    )

        # Store stats for completion message
        if context.user_data is not None:
            context.user_data["letterboxd_stats"] = (
                f"{analysis.total_watched} watched, {analysis.total_rated} rated"
            )
            context.user_data["setup_step"] = "device"
            context.user_data["has_letterboxd"] = True

        # Build result message
        result_lines = [
            "**Импортировано из Letterboxd:**",
            f"- Просмотрено: {analysis.total_watched}",
            f"- С оценкой: {analysis.total_rated}",
            f"- Рецензий: {analysis.total_reviews}",
        ]

        if analysis.favorites:
            top3 = ", ".join(f.name for f in analysis.favorites[:3])
            result_lines.append(f"\n**Любимые:** {top3}")

        if analysis.hated:
            bottom3 = ", ".join(f.name for f in analysis.hated[:3])
            result_lines.append(f"**Не понравились:** {bottom3}")

        if analysis.average_rating:
            result_lines.append(f"\n**Средняя оценка:** {analysis.average_rating:.1f}/5")

        result_lines.append("\nТвои предпочтения сохранены в профиле.")

        await processing_msg.edit_text(
            "\n".join(result_lines),
            parse_mode="Markdown",
        )

        # Move to device selection (skip movies question since we have Letterboxd data)
        await message.reply_text(
            DEVICE_QUESTION,
            parse_mode="Markdown",
            reply_markup=get_device_keyboard(),
        )
        return WAITING_DEVICE

    except ValueError as e:
        logger.warning("letterboxd_parse_error", error=str(e))
        await processing_msg.edit_text(
            f"Ошибка обработки файла: {e}\nУбедись, что это ZIP-файл с экспортом Letterboxd.",
            reply_markup=get_skip_keyboard(),
        )
        return WAITING_LETTERBOXD_FILE

    except Exception as e:
        logger.exception("letterboxd_import_error", error=str(e))
        await processing_msg.edit_text(
            f"Ошибка импорта: {e}\nПопробуй ещё раз или нажми 'Пропустить'.",
            reply_markup=get_skip_keyboard(),
        )
        return WAITING_LETTERBOXD_FILE


# =============================================================================
# Text Input Handlers
# =============================================================================


async def handle_movies_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle favorite movies input."""
    user = update.effective_user
    message = update.message
    if not message or not message.text:
        return WAITING_MOVIES

    movies = message.text.strip()

    if context.user_data is not None:
        context.user_data["favorite_movies"] = movies
        context.user_data["setup_step"] = "device"

    logger.info("movies_input", user_id=user.id if user else None, movies=movies)

    # Save to Core Memory
    try:
        from src.user.memory import CoreMemoryManager

        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                memory_manager = CoreMemoryManager(storage)
                await memory_manager.update_block(
                    db_user.id,
                    "preferences",
                    f"Favorite movies: {movies}",
                    operation="append",
                )
    except Exception as e:
        logger.warning("core_memory_update_error", error=str(e))

    await message.reply_text(
        DEVICE_QUESTION,
        parse_mode="Markdown",
        reply_markup=get_device_keyboard(),
    )
    return WAITING_DEVICE


async def handle_rutracker_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Rutracker username input."""
    user = update.effective_user
    message = update.message
    if not message or not message.text:
        return WAITING_RUTRACKER_USER

    rutracker_user = message.text.strip()

    if context.user_data is not None:
        context.user_data["rutracker_username"] = rutracker_user

    logger.info("rutracker_user_input", user_id=user.id if user else None)

    await message.reply_text(RUTRACKER_PASSWORD, parse_mode="Markdown")
    return WAITING_RUTRACKER_PASS


async def handle_rutracker_pass_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Rutracker password input."""
    user = update.effective_user
    message = update.message
    if not message or not message.text:
        return WAITING_RUTRACKER_PASS

    password = message.text.strip()
    username = context.user_data.get("rutracker_username") if context.user_data else None

    # Delete password message for security
    with contextlib.suppress(Exception):
        await message.delete()

    if not username:
        await message.reply_text("Ошибка: введи сначала логин.")
        return WAITING_RUTRACKER_USER

    logger.info("rutracker_pass_input", user_id=user.id if user else None)

    # Verify and save credentials
    try:
        from src.search.rutracker import RutrackerClient
        from src.user.storage import CredentialType

        # Test login
        async with RutrackerClient(username=username, password=password) as client:
            if await client._login():
                # Save credentials
                async with get_storage() as storage:
                    db_user = await storage.get_user_by_telegram_id(user.id)
                    if db_user:
                        await storage.store_credential(
                            db_user.id, CredentialType.RUTRACKER_USERNAME, username
                        )
                        await storage.store_credential(
                            db_user.id, CredentialType.RUTRACKER_PASSWORD, password
                        )

                        if context.user_data is not None:
                            context.user_data["has_rutracker"] = True

                        await update.effective_chat.send_message(
                            "Rutracker подключён.\n\nТеперь выбери качество видео:",
                            reply_markup=get_quality_keyboard(),
                        )

                        if context.user_data is not None:
                            context.user_data["setup_step"] = "quality"
                        return ConversationHandler.END
            else:
                await update.effective_chat.send_message(
                    "Не удалось войти. Проверь логин и пароль.\n"
                    "Введи логин ещё раз или нажми 'Пропустить'.",
                    reply_markup=get_skip_keyboard(),
                )
                return WAITING_RUTRACKER_USER

    except Exception as e:
        logger.exception("rutracker_login_error", error=str(e))
        await update.effective_chat.send_message(
            f"Ошибка авторизации: {e}\nНажми 'Пропустить' или попробуй ещё.",
            reply_markup=get_skip_keyboard(),
        )
        return WAITING_RUTRACKER_USER

    return WAITING_RUTRACKER_USER


# =============================================================================
# Settings Handlers
# =============================================================================


async def settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /settings command."""
    user = update.effective_user
    message = update.message
    if not user or not message:
        return

    logger.info("settings_command", user_id=user.id)

    current_quality = "1080p"
    current_audio = "ru"

    try:
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                prefs = await storage.get_preferences(db_user.id)
                if prefs:
                    current_quality = prefs.video_quality or "1080p"
                    current_audio = prefs.audio_language or "ru"
    except Exception as e:
        logger.exception("settings_fetch_error", error=str(e))

    await message.reply_text(
        "**Настройки**\n\nВыбери, что изменить:",
        parse_mode="Markdown",
        reply_markup=get_settings_keyboard(current_quality, current_audio),
    )


async def settings_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle settings callbacks."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()

    user = query.from_user
    callback = query.data

    logger.info("settings_callback", user_id=user.id, callback=callback)

    try:
        if callback == "settings_close":
            await query.delete_message()

        elif callback == "settings_quality":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("720p", callback_data="set_quality_720p"),
                        InlineKeyboardButton("1080p", callback_data="set_quality_1080p"),
                        InlineKeyboardButton("4K", callback_data="set_quality_4K"),
                    ],
                    [InlineKeyboardButton("Назад", callback_data="settings_back")],
                ]
            )
            await query.edit_message_text(
                "**Качество видео**\n\nВыбери предпочитаемое качество:",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        elif callback == "settings_audio":
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Русский", callback_data="set_audio_ru"),
                        InlineKeyboardButton("English", callback_data="set_audio_en"),
                        InlineKeyboardButton("Оригинал", callback_data="set_audio_original"),
                    ],
                    [InlineKeyboardButton("Назад", callback_data="settings_back")],
                ]
            )
            await query.edit_message_text(
                "**Язык аудио**\n\nВыбери предпочитаемый язык:",
                parse_mode="Markdown",
                reply_markup=keyboard,
            )

        elif callback.startswith("set_quality_"):
            quality = callback.replace("set_quality_", "")
            async with get_storage() as storage:
                db_user = await storage.get_user_by_telegram_id(user.id)
                if db_user:
                    await storage.update_preferences(user_id=db_user.id, video_quality=quality)
                    # Sync updated settings to Core Memory
                    from src.user.memory import CoreMemoryManager

                    memory_manager = CoreMemoryManager(storage)
                    prefs = await storage.get_preferences(db_user.id)
                    if prefs:
                        prefs_content = f"Quality: {prefs.video_quality or '1080p'}\nAudio: {prefs.audio_language or 'ru'}"
                        await memory_manager.update_block(
                            db_user.id, "preferences", prefs_content, operation="replace"
                        )
                    await query.edit_message_text(
                        f"Качество: **{quality}**",
                        parse_mode="Markdown",
                        reply_markup=get_settings_keyboard(
                            quality, prefs.audio_language if prefs else "ru"
                        ),
                    )

        elif callback.startswith("set_audio_"):
            audio = callback.replace("set_audio_", "")
            audio_display = {"ru": "Русский", "en": "English", "original": "Оригинал"}.get(
                audio, audio
            )
            async with get_storage() as storage:
                db_user = await storage.get_user_by_telegram_id(user.id)
                if db_user:
                    await storage.update_preferences(user_id=db_user.id, audio_language=audio)
                    # Sync updated settings to Core Memory
                    from src.user.memory import CoreMemoryManager

                    memory_manager = CoreMemoryManager(storage)
                    prefs = await storage.get_preferences(db_user.id)
                    if prefs:
                        prefs_content = f"Quality: {prefs.video_quality or '1080p'}\nAudio: {prefs.audio_language or 'ru'}"
                        await memory_manager.update_block(
                            db_user.id, "preferences", prefs_content, operation="replace"
                        )
                    await query.edit_message_text(
                        f"Аудио: **{audio_display}**",
                        parse_mode="Markdown",
                        reply_markup=get_settings_keyboard(
                            prefs.video_quality if prefs else "1080p", audio
                        ),
                    )

        elif callback == "settings_back":
            async with get_storage() as storage:
                db_user = await storage.get_user_by_telegram_id(user.id)
                if db_user:
                    prefs = await storage.get_preferences(db_user.id)
                    await query.edit_message_text(
                        "**Настройки**\n\nВыбери, что изменить:",
                        parse_mode="Markdown",
                        reply_markup=get_settings_keyboard(
                            prefs.video_quality if prefs else "1080p",
                            prefs.audio_language if prefs else "ru",
                        ),
                    )

        elif callback == "settings_letterboxd":
            await query.edit_message_text(
                "**Letterboxd**\n\n"
                "Отправь ZIP-файл с экспортом Letterboxd для импорта данных.\n"
                "Скачать: letterboxd.com/settings/data/\n\n"
                "Или нажми 'Назад'.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Назад", callback_data="settings_back")],
                    ]
                ),
            )

        elif callback == "settings_rutracker":
            await query.edit_message_text(
                "**Rutracker**\n\nДля настройки Rutracker используй команду /rutracker",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("Назад", callback_data="settings_back")],
                    ]
                ),
            )

    except Exception as e:
        logger.exception("settings_error", callback=callback, error=str(e))


def get_onboarding_conversation_handler() -> ConversationHandler:
    """Create conversation handler for onboarding flow.

    Returns:
        ConversationHandler for onboarding
    """
    from telegram.ext import CallbackQueryHandler

    return ConversationHandler(
        entry_points=[
            # Entry from "Настроить" button
            CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"),
        ],
        states={
            WAITING_LETTERBOXD_FILE: [
                MessageHandler(filters.Document.ZIP, handle_letterboxd_file),
                CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"),
            ],
            WAITING_MOVIES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_movies_input),
                CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"),
            ],
            WAITING_DEVICE: [
                CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"),
            ],
            WAITING_AUDIO_SETUP: [
                CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"),
            ],
            WAITING_VIEWING_PARTNERS: [
                CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"),
            ],
            WAITING_RUTRACKER_USER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_rutracker_user_input),
                CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"),
            ],
            WAITING_RUTRACKER_PASS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_rutracker_pass_input),
                CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"),
            ],
        },
        fallbacks=[
            CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"),
        ],
        per_message=False,
        per_chat=True,
        per_user=True,
    )
