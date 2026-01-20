"""Message handlers for the Telegram bot."""

import structlog
from telegram import Update
from telegram.ext import ContextTypes

from src.user.storage import get_storage

logger = structlog.get_logger(__name__)


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /start command.

    Sends a welcome message to the user introducing the bot.

    Args:
        update: Telegram update object
        context: Callback context
    """
    user = update.effective_user

    logger.info(
        "start_command",
        user_id=user.id,
        username=user.username,
        first_name=user.first_name,
    )

    welcome_message = (
        f"Привет, {user.first_name}.\n\n"
        "**Media Concierge Bot** — поиск фильмов и сериалов.\n\n"
        "Возможности:\n"
        "- Поиск по названию с указанием качества\n"
        "- Информация о фильмах (рейтинги, актёры)\n"
        "- Рекомендации на основе предпочтений\n\n"
        "Напиши, что хочешь посмотреть. Команды: /help"
    )

    try:
        await update.message.reply_text(
            welcome_message,
            parse_mode="Markdown",
        )
        logger.info("start_response_sent", user_id=user.id)
    except Exception as e:
        logger.exception("start_handler_failed", user_id=user.id, error=str(e))
        # Fallback without markdown if parsing fails
        await update.message.reply_text(
            "Привет! Я Media Concierge Bot. Используй /help для списка команд."
        )


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /help command.

    Sends a list of available commands and usage examples.

    Args:
        update: Telegram update object
        context: Callback context
    """
    user = update.effective_user

    logger.info("help_command", user_id=user.id)

    help_message = (
        "**Команды:**\n"
        "/start — Начало работы\n"
        "/profile — Посмотреть свой профиль\n"
        "/rutracker — Настроить логин Rutracker\n"
        "/settings — Настройки качества и предпочтений\n"
        "/help — Эта справка\n\n"
        "**Типовые сценарии:**\n\n"
        "_Поиск фильма:_\n"
        '• "Найди Дюну" — поиск по названию\n'
        '• "Дюна 4K" — с указанием качества\n'
        '• "Dune 2021" — по оригинальному названию с годом\n\n'
        "_Рекомендации:_\n"
        '• "Что-то похожее на Interstellar"\n'
        '• "Фильм на вечер, не слишком длинный"\n'
        '• "Хороший детектив последних лет"\n\n'
        "_Информация:_\n"
        '• "Кто снял Blade Runner 2049?"\n'
        '• "Фильмография Вильнёва"\n\n'
        "_Мониторинг:_\n"
        '• "Уведоми когда выйдет Avatar 3"\n'
        '• "Отслеживай сериал The Last of Us"'
    )

    try:
        await update.message.reply_text(
            help_message,
            parse_mode="Markdown",
        )
        logger.info("help_response_sent", user_id=user.id)
    except Exception as e:
        logger.exception("help_handler_failed", user_id=user.id, error=str(e))
        # Fallback without markdown if parsing fails
        await update.message.reply_text(
            "/start - Приветствие\n/help - Справка\n\nПросто напиши название фильма для поиска!"
        )


async def profile_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle the /profile command.

    Shows user's markdown profile.

    Args:
        update: Telegram update object
        context: Callback context
    """
    user = update.effective_user

    logger.info("profile_command", user_id=user.id)

    try:
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                profile = await storage.get_profile(db_user.id)
                if profile and profile.profile_md:
                    # Truncate if too long for Telegram
                    profile_text = profile.profile_md
                    if len(profile_text) > 4000:
                        profile_text = profile_text[:4000] + "\n\n_...профиль сокращён_"

                    await update.message.reply_text(
                        f"**Ваш профиль:**\n\n```\n{profile_text}\n```",
                        parse_mode="Markdown",
                    )
                    return

        await update.message.reply_text(
            "Профиль не найден. Используйте /start для настройки."
        )
    except Exception as e:
        logger.exception("profile_handler_failed", user_id=user.id, error=str(e))
        await update.message.reply_text(
            "Не удалось загрузить профиль. Попробуйте позже."
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors that occur during update processing.

    Args:
        update: Telegram update object (or None)
        context: Callback context containing error information
    """
    logger.exception(
        "telegram_error",
        error=str(context.error),
        update=update,
    )

    # Try to notify the user if possible
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Произошла ошибка при обработке вашего запроса. "
                "Попробуйте позже или обратитесь к администратору."
            )
        except Exception as e:
            logger.error("error_notification_failed", error=str(e))
