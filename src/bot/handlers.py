"""Message handlers for the Telegram bot."""

import structlog
from telegram import Update
from telegram.ext import ContextTypes

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
        f"👋 Привет, {user.first_name}!\n\n"
        "Я **Media Concierge Bot** — твой личный помощник для поиска фильмов и сериалов.\n\n"
        "🎬 Что я умею:\n"
        "• Искать фильмы и сериалы по названию\n"
        "• Находить торренты в нужном качестве (1080p, 4K, etc.)\n"
        "• Показывать информацию о фильмах (рейтинги, актёры, описание)\n"
        "• Давать рекомендации на основе твоих предпочтений\n\n"
        "💬 Просто напиши мне, что хочешь посмотреть, и я помогу найти!\n\n"
        "Для списка команд используй /help"
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
        "📖 **Справка по командам:**\n\n"
        "**Основные команды:**\n"
        "/start — Приветственное сообщение\n"
        "/help — Показать эту справку\n"
        "/rutracker — Настроить логин/пароль Rutracker\n"
        "/settings — Настройки предпочтений\n\n"
        "**Как использовать:**\n\n"
        "🔍 **Поиск фильма:**\n"
        "Просто напиши название фильма или сериала:\n"
        '• _"Найди Дюну в 4K"_\n'
        '• _"Хочу посмотреть Интерстеллар"_\n'
        '• _"Скачай Игру Престолов 1080p"_\n\n'
        "🎯 **Рекомендации:**\n"
        '• _"Что-то похожее на Начало"_\n'
        '• _"Посоветуй хороший фантастический фильм"_\n\n'
        "ℹ️ **Информация:**\n"
        '• _"Расскажи про фильм Blade Runner 2049"_\n'
        '• _"Кто снимал Криминальное чтиво?"_\n\n'
        "💡 Я понимаю естественный язык, так что общайся со мной как с человеком!"
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
