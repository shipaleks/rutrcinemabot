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

from src.user.storage import get_storage

logger = structlog.get_logger(__name__)

# Conversation states
WAITING_LETTERBOXD_FILE = 1
WAITING_MOVIES = 2
WAITING_RUTRACKER_USER = 3
WAITING_RUTRACKER_PASS = 4


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
Аудио: {audio}

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
# Handlers
# =============================================================================


async def onboarding_start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /start command."""
    user = update.effective_user
    message = update.message
    if not user or not message:
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
                logger.info("preferences_saved", user_id=user.id, quality=quality, audio=audio)
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

    message = SETUP_COMPLETE.format(
        quality=quality,
        audio=audio_display,
        letterboxd_note=letterboxd_note,
        movies_note=movies_note,
        rutracker_note=rutracker_note,
    )

    await query.edit_message_text(message, parse_mode="Markdown")
    return ConversationHandler.END


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
            format_analysis_for_profile,
        )

        parser = LetterboxdExportParser()
        analysis = parser.parse_zip(bytes(file_bytes))

        # Save to profile
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                from src.user.profile import ProfileManager

                profile_manager = ProfileManager(storage)

                # Format analysis for profile
                profile_section = format_analysis_for_profile(analysis)

                # Add review style if available
                review_style = extract_review_style(analysis.review_samples)
                if review_style:
                    profile_section += f"\n**Review Style:** {review_style}"

                await profile_manager.update_section(
                    db_user.id,
                    "Letterboxd Import",
                    profile_section,
                )

                # Also save favorites to separate section
                if analysis.favorites:
                    favorites_text = ", ".join(
                        f"{f.name} ({f.year})" if f.year else f.name
                        for f in analysis.favorites[:15]
                    )
                    await profile_manager.update_section(
                        db_user.id,
                        "Favorite Films",
                        favorites_text,
                    )

                # Save disliked to blocklist section
                if analysis.hated or analysis.disliked:
                    disliked_all = analysis.hated + analysis.disliked[:10]
                    disliked_text = ", ".join(
                        f"{f.name} ({f.year})" if f.year else f.name for f in disliked_all[:15]
                    )
                    await profile_manager.update_section(
                        db_user.id,
                        "Disliked Films",
                        disliked_text,
                    )

        # Store stats for completion message
        if context.user_data is not None:
            context.user_data["letterboxd_stats"] = (
                f"{analysis.total_watched} watched, {analysis.total_rated} rated"
            )
            context.user_data["setup_step"] = "rutracker"
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

        # Move to Rutracker (skip movies question since we have Letterboxd data)
        await message.reply_text(
            RUTRACKER_QUESTION,
            parse_mode="Markdown",
            reply_markup=get_skip_keyboard(),
        )
        return WAITING_RUTRACKER_USER

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
        context.user_data["setup_step"] = "rutracker"

    logger.info("movies_input", user_id=user.id if user else None, movies=movies)

    # Save to profile
    try:
        from src.user.profile import ProfileManager

        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                profile_manager = ProfileManager(storage)
                await profile_manager.update_section(
                    db_user.id,
                    "Favorite Films",
                    movies,
                )
    except Exception as e:
        logger.warning("profile_update_error", error=str(e))

    await message.reply_text(
        RUTRACKER_QUESTION,
        parse_mode="Markdown",
        reply_markup=get_skip_keyboard(),
    )
    return WAITING_RUTRACKER_USER


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
                    prefs = await storage.get_preferences(db_user.id)
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
                    prefs = await storage.get_preferences(db_user.id)
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
