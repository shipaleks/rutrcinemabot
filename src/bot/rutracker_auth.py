"""Rutracker credentials management for the Telegram bot.

This module provides:
- /rutracker command to set credentials
- ConversationHandler for username/password input flow
- Secure storage with encryption

Usage:
    # In handlers registration:
    application.add_handler(get_rutracker_conversation_handler())
"""

import contextlib

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from src.user.storage import CredentialType, get_storage

logger = structlog.get_logger(__name__)

# Conversation states
WAITING_USERNAME = 1
WAITING_PASSWORD = 2


def get_rutracker_keyboard() -> InlineKeyboardMarkup:
    """Create Rutracker settings keyboard.

    Returns:
        InlineKeyboardMarkup with options
    """
    keyboard = [
        [InlineKeyboardButton("🔑 Ввести логин/пароль", callback_data="rutracker_enter")],
        [InlineKeyboardButton("🗑 Удалить credentials", callback_data="rutracker_delete")],
        [InlineKeyboardButton("❌ Отмена", callback_data="rutracker_cancel")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def rutracker_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int | None:
    """Handle the /rutracker command.

    Shows options for managing Rutracker credentials.

    Args:
        update: Telegram update object
        context: Callback context

    Returns:
        ConversationHandler.END or None
    """
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    logger.info("rutracker_command", user_id=user.id)

    # Check if user already has credentials stored
    has_credentials = False
    try:
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                username = await storage.get_credential(
                    db_user.id, CredentialType.RUTRACKER_USERNAME
                )
                has_credentials = username is not None
    except Exception as e:
        logger.warning("failed_to_check_credentials", error=str(e))

    if has_credentials:
        message = (
            "🔐 **Rutracker Credentials**\n\n✅ Credentials уже настроены.\n\nВыберите действие:"
        )
    else:
        message = (
            "🔐 **Rutracker Credentials**\n\n"
            "Для поиска на Rutracker необходимо авторизоваться.\n\n"
            "⚠️ Ваши данные будут зашифрованы и хранятся безопасно.\n\n"
            "Выберите действие:"
        )

    await update.message.reply_text(
        message,
        reply_markup=get_rutracker_keyboard(),
        parse_mode="Markdown",
    )
    return None


async def rutracker_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle Rutracker inline button callbacks.

    Args:
        update: Telegram update object
        context: Callback context

    Returns:
        Conversation state
    """
    query = update.callback_query
    if not query:
        return ConversationHandler.END

    await query.answer()
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    callback_data = query.data
    logger.info("rutracker_callback", user_id=user.id, callback=callback_data)

    if callback_data == "rutracker_cancel":
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    if callback_data == "rutracker_delete":
        try:
            async with get_storage() as storage:
                db_user = await storage.get_user_by_telegram_id(user.id)
                if db_user:
                    await storage.delete_credential(db_user.id, CredentialType.RUTRACKER_USERNAME)
                    await storage.delete_credential(db_user.id, CredentialType.RUTRACKER_PASSWORD)
                    logger.info("rutracker_credentials_deleted", user_id=user.id)
                    await query.edit_message_text(
                        "🗑 Rutracker credentials удалены.\n\n"
                        "Поиск будет использовать глобальные настройки (если есть)."
                    )
                else:
                    await query.edit_message_text("❌ Пользователь не найден.")
        except Exception as e:
            logger.error("failed_to_delete_credentials", error=str(e))
            await query.edit_message_text(f"❌ Ошибка: {e}")
        return ConversationHandler.END

    if callback_data == "rutracker_enter":
        await query.edit_message_text(
            "📝 **Введите логин Rutracker:**\n\n"
            "Отправьте ваш логин (username) с rutracker.org\n\n"
            "_Для отмены отправьте /cancel_",
            parse_mode="Markdown",
        )
        return WAITING_USERNAME

    return ConversationHandler.END


async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and store Rutracker username.

    Args:
        update: Telegram update object
        context: Callback context

    Returns:
        Next conversation state
    """
    user = update.effective_user
    if not user or not update.message:
        return ConversationHandler.END

    username = update.message.text.strip()
    if not username:
        await update.message.reply_text("❌ Логин не может быть пустым. Попробуйте снова:")
        return WAITING_USERNAME

    # Store username temporarily in context
    context.user_data["rutracker_username"] = username

    logger.info("rutracker_username_received", user_id=user.id)

    # Delete the message with username for security
    with contextlib.suppress(Exception):
        await update.message.delete()

    await update.message.reply_text(
        "✅ Логин получен.\n\n"
        "🔑 **Теперь введите пароль:**\n\n"
        "_Сообщение будет удалено после получения для безопасности._\n"
        "_Для отмены отправьте /cancel_",
        parse_mode="Markdown",
    )
    return WAITING_PASSWORD


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and store Rutracker password.

    Args:
        update: Telegram update object
        context: Callback context

    Returns:
        ConversationHandler.END
    """
    user = update.effective_user
    if not user or not update.message:
        return ConversationHandler.END

    password = update.message.text.strip()
    if not password:
        await update.message.reply_text("❌ Пароль не может быть пустым. Попробуйте снова:")
        return WAITING_PASSWORD

    # Delete the message with password for security
    with contextlib.suppress(Exception):
        await update.message.delete()

    username = context.user_data.get("rutracker_username")
    if not username:
        await update.message.reply_text("❌ Ошибка: логин не найден. Начните заново с /rutracker")
        return ConversationHandler.END

    # Store credentials encrypted
    try:
        async with get_storage() as storage:
            # Get or create user
            db_user, created = await storage.get_or_create_user(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
            )

            # Store encrypted credentials
            await storage.store_credential(db_user.id, CredentialType.RUTRACKER_USERNAME, username)
            await storage.store_credential(db_user.id, CredentialType.RUTRACKER_PASSWORD, password)

            logger.info(
                "rutracker_credentials_stored",
                user_id=user.id,
                db_user_id=db_user.id,
            )

            await update.message.reply_text(
                "Rutracker credentials сохранены!\n\n"
                "Теперь поиск на Rutracker будет работать с вашим аккаунтом.\n\n"
                "Попробуйте: Найди Дюну в 4K",
            )

    except Exception as e:
        logger.error("failed_to_store_credentials", error=str(e))
        await update.message.reply_text(f"❌ Ошибка при сохранении: {e}")

    # Clear temporary data
    context.user_data.pop("rutracker_username", None)

    return ConversationHandler.END


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the conversation.

    Args:
        update: Telegram update object
        context: Callback context

    Returns:
        ConversationHandler.END
    """
    user = update.effective_user
    if user:
        logger.info("rutracker_auth_cancelled", user_id=user.id)

    # Clear temporary data
    context.user_data.pop("rutracker_username", None)

    if update.message:
        await update.message.reply_text("❌ Отменено.")

    return ConversationHandler.END


def get_rutracker_conversation_handler() -> ConversationHandler:
    """Create ConversationHandler for Rutracker credentials flow.

    Returns:
        ConversationHandler instance
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("rutracker", rutracker_command_handler),
            CallbackQueryHandler(rutracker_callback_handler, pattern="^rutracker_"),
        ],
        states={
            WAITING_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username),
            ],
            WAITING_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_handler),
            CallbackQueryHandler(rutracker_callback_handler, pattern="^rutracker_cancel$"),
        ],
        name="rutracker_auth",
        persistent=False,
    )


async def get_user_rutracker_credentials(telegram_id: int) -> tuple[str | None, str | None]:
    """Get user's Rutracker credentials from storage.

    Args:
        telegram_id: Telegram user ID

    Returns:
        Tuple of (username, password) or (None, None) if not found
    """
    try:
        logger.debug("getting_rutracker_credentials", telegram_id=telegram_id)

        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(telegram_id)
            logger.debug(
                "db_user_lookup",
                telegram_id=telegram_id,
                found=db_user is not None,
                db_user_id=db_user.id if db_user else None,
            )

            if not db_user:
                logger.info("rutracker_user_not_found", telegram_id=telegram_id)
                return None, None

            username = await storage.get_credential(db_user.id, CredentialType.RUTRACKER_USERNAME)
            password = await storage.get_credential(db_user.id, CredentialType.RUTRACKER_PASSWORD)

            logger.info(
                "rutracker_credentials_lookup",
                telegram_id=telegram_id,
                db_user_id=db_user.id,
                has_username=username is not None,
                has_password=password is not None,
            )

            return username, password

    except Exception as e:
        logger.warning("failed_to_get_credentials", telegram_id=telegram_id, error=str(e))
        return None, None
