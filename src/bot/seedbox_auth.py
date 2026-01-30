"""Seedbox credentials management for the Telegram bot.

This module provides:
- /seedbox command to set credentials
- ConversationHandler for host/username/password input flow
- Connection test before saving
- Secure storage with encryption

Usage:
    # In handlers registration:
    application.add_handler(get_seedbox_conversation_handler())
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

from src.seedbox.client import DelugeClient, SeedboxAuthError, SeedboxConnectionError
from src.user.storage import CredentialType, get_storage

logger = structlog.get_logger(__name__)

# Conversation states
WAITING_HOST = 1
WAITING_USERNAME = 2
WAITING_PASSWORD = 3


def get_seedbox_keyboard() -> InlineKeyboardMarkup:
    """Create seedbox settings keyboard.

    Returns:
        InlineKeyboardMarkup with options
    """
    keyboard = [
        [InlineKeyboardButton("🔧 Настроить seedbox", callback_data="seedbox_enter")],
        [InlineKeyboardButton("🗑 Удалить credentials", callback_data="seedbox_delete")],
        [InlineKeyboardButton("❌ Отмена", callback_data="seedbox_cancel")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def seedbox_command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Handle the /seedbox command.

    Shows options for managing seedbox credentials.

    Args:
        update: Telegram update object
        context: Callback context

    Returns:
        ConversationHandler.END or None
    """
    user = update.effective_user
    if not user:
        return ConversationHandler.END

    logger.info("seedbox_command", user_id=user.id)

    # Check if user already has credentials stored
    has_credentials = False
    seedbox_host = None
    try:
        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(user.id)
            if db_user:
                seedbox_host = await storage.get_credential(db_user.id, CredentialType.SEEDBOX_HOST)
                has_credentials = seedbox_host is not None
    except Exception as e:
        logger.warning("failed_to_check_credentials", error=str(e))

    if has_credentials:
        # Mask the host URL for display
        host_display = seedbox_host[:30] + "..." if len(seedbox_host) > 30 else seedbox_host
        message = f"🖥 **Seedbox Settings**\n\n✅ Настроен: `{host_display}`\n\nВыберите действие:"
    else:
        message = (
            "🖥 **Seedbox Settings**\n\n"
            "Seedbox не настроен.\n\n"
            "Seedbox позволяет отправлять торренты на удалённый сервер "
            "для скачивания.\n\n"
            "Поддерживается: **Deluge** (Ultra.cc и аналоги)\n\n"
            "Выберите действие:"
        )

    await update.message.reply_text(
        message,
        reply_markup=get_seedbox_keyboard(),
        parse_mode="Markdown",
    )
    return None


async def seedbox_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle seedbox inline button callbacks.

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
    logger.info("seedbox_callback", user_id=user.id, callback=callback_data)

    if callback_data == "seedbox_cancel":
        await query.edit_message_text("❌ Отменено.")
        return ConversationHandler.END

    if callback_data == "seedbox_delete":
        try:
            async with get_storage() as storage:
                db_user = await storage.get_user_by_telegram_id(user.id)
                if db_user:
                    await storage.delete_credential(db_user.id, CredentialType.SEEDBOX_HOST)
                    await storage.delete_credential(db_user.id, CredentialType.SEEDBOX_USERNAME)
                    await storage.delete_credential(db_user.id, CredentialType.SEEDBOX_PASSWORD)
                    logger.info("seedbox_credentials_deleted", user_id=user.id)
                    await query.edit_message_text(
                        "🗑 Seedbox credentials удалены.\n\n"
                        "Кнопка Seedbox будет использовать глобальные настройки (если есть)."
                    )
                else:
                    await query.edit_message_text("❌ Пользователь не найден.")
        except Exception as e:
            logger.error("failed_to_delete_credentials", error=str(e))
            await query.edit_message_text(f"❌ Ошибка: {e}")
        return ConversationHandler.END

    if callback_data == "seedbox_enter":
        await query.edit_message_text(
            "🔧 **Настройка Seedbox**\n\n"
            "**Шаг 1/3: URL сервера**\n\n"
            "Введите полный URL Deluge Web UI:\n"
            "`https://username.servername.usbx.me/deluge`\n\n"
            "_Для отмены отправьте /cancel_",
            parse_mode="Markdown",
        )
        return WAITING_HOST

    return ConversationHandler.END


async def receive_host(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive and validate seedbox host URL.

    Args:
        update: Telegram update object
        context: Callback context

    Returns:
        Next conversation state
    """
    user = update.effective_user
    if not user or not update.message:
        return ConversationHandler.END

    host = update.message.text.strip()
    if not host:
        await update.message.reply_text("❌ URL не может быть пустым. Попробуйте снова:")
        return WAITING_HOST

    # Validate URL format
    if not host.startswith(("http://", "https://")):
        await update.message.reply_text(
            "❌ URL должен начинаться с http:// или https://\n"
            "Пример: `https://username.server.usbx.me/deluge`\n\n"
            "Попробуйте снова:",
            parse_mode="Markdown",
        )
        return WAITING_HOST

    # Store host temporarily in context
    context.user_data["seedbox_host"] = host

    logger.info("seedbox_host_received", user_id=user.id)

    await update.message.reply_text(
        "✅ URL получен.\n\n"
        "**Шаг 2/3: Имя пользователя**\n\n"
        "Введите имя пользователя Deluge:\n"
        "(обычно это ваше имя пользователя Ultra.cc)\n\n"
        "_Для отмены отправьте /cancel_",
        parse_mode="Markdown",
    )
    return WAITING_USERNAME


async def receive_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive seedbox username.

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
        await update.message.reply_text(
            "❌ Имя пользователя не может быть пустым. Попробуйте снова:"
        )
        return WAITING_USERNAME

    # Store username temporarily in context
    context.user_data["seedbox_username"] = username

    logger.info("seedbox_username_received", user_id=user.id)

    await update.message.reply_text(
        "✅ Имя пользователя получено.\n\n"
        "**Шаг 3/3: Пароль**\n\n"
        "🔑 Введите пароль Deluge Web UI:\n"
        "(пароль, который вы установили в настройках Deluge)\n\n"
        "_Сообщение будет удалено после получения для безопасности._\n"
        "_Для отмены отправьте /cancel_",
        parse_mode="Markdown",
    )
    return WAITING_PASSWORD


async def receive_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive seedbox password and test connection.

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

    host = context.user_data.get("seedbox_host")
    username = context.user_data.get("seedbox_username")

    if not host or not username:
        await update.message.reply_text(
            "❌ Ошибка: данные сессии потеряны. Начните заново с /seedbox"
        )
        return ConversationHandler.END

    # Test connection before saving
    status_msg = await update.message.reply_text("🔄 Проверяю подключение к Deluge...")

    try:
        client = DelugeClient(host=host, username=username, password=password)
        async with client:
            # If we get here, authentication was successful
            pass

        await status_msg.edit_text("✅ Подключение успешно! Сохраняю credentials...")

    except SeedboxAuthError as e:
        logger.warning("seedbox_auth_test_failed", user_id=user.id, error=str(e))
        await status_msg.edit_text(
            f"❌ Ошибка авторизации: {e}\n\nПроверьте пароль Deluge и попробуйте снова с /seedbox"
        )
        _clear_seedbox_context(context)
        return ConversationHandler.END

    except SeedboxConnectionError as e:
        logger.warning("seedbox_connection_test_failed", user_id=user.id, error=str(e))
        await status_msg.edit_text(
            f"❌ Ошибка подключения: {e}\n\nПроверьте URL сервера и попробуйте снова с /seedbox"
        )
        _clear_seedbox_context(context)
        return ConversationHandler.END

    except Exception as e:
        logger.error("seedbox_test_unexpected_error", user_id=user.id, error=str(e))
        await status_msg.edit_text(f"❌ Неожиданная ошибка: {e}\n\nПопробуйте снова с /seedbox")
        _clear_seedbox_context(context)
        return ConversationHandler.END

    # Store credentials encrypted
    try:
        async with get_storage() as storage:
            # Get or create user
            db_user, _created = await storage.get_or_create_user(
                telegram_id=user.id,
                username=user.username,
                first_name=user.first_name,
                last_name=user.last_name,
            )

            # Store encrypted credentials
            await storage.store_credential(db_user.id, CredentialType.SEEDBOX_HOST, host)
            await storage.store_credential(db_user.id, CredentialType.SEEDBOX_USERNAME, username)
            await storage.store_credential(db_user.id, CredentialType.SEEDBOX_PASSWORD, password)

            logger.info(
                "seedbox_credentials_stored",
                user_id=user.id,
                db_user_id=db_user.id,
            )

            await status_msg.edit_text(
                "🎉 Seedbox настроен!\n\n"
                "Теперь кнопка «Seedbox» в результатах поиска "
                "будет отправлять торренты на ваш сервер.\n\n"
                "Попробуйте: найди Дюну в 4K",
            )

    except Exception as e:
        logger.error("failed_to_store_credentials", error=str(e))
        await status_msg.edit_text(f"❌ Ошибка при сохранении: {e}")

    # Clear temporary data
    _clear_seedbox_context(context)

    return ConversationHandler.END


def _clear_seedbox_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear seedbox-related temporary data from context."""
    context.user_data.pop("seedbox_host", None)
    context.user_data.pop("seedbox_username", None)


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
        logger.info("seedbox_auth_cancelled", user_id=user.id)

    # Clear temporary data
    _clear_seedbox_context(context)

    if update.message:
        await update.message.reply_text("❌ Отменено.")

    return ConversationHandler.END


def get_seedbox_conversation_handler() -> ConversationHandler:
    """Create ConversationHandler for seedbox credentials flow.

    Returns:
        ConversationHandler instance
    """
    return ConversationHandler(
        entry_points=[
            CommandHandler("seedbox", seedbox_command_handler),
            CallbackQueryHandler(seedbox_callback_handler, pattern="^seedbox_"),
        ],
        states={
            WAITING_HOST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_host),
            ],
            WAITING_USERNAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_username),
            ],
            WAITING_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_password),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_handler),
            CallbackQueryHandler(seedbox_callback_handler, pattern="^seedbox_cancel$"),
        ],
        name="seedbox_auth",
        persistent=False,
    )


async def get_user_seedbox_credentials(
    telegram_id: int,
) -> tuple[str | None, str | None, str | None]:
    """Get user's seedbox credentials from storage.

    Args:
        telegram_id: Telegram user ID

    Returns:
        Tuple of (host, username, password) or (None, None, None) if not found
    """
    try:
        logger.debug("getting_seedbox_credentials", telegram_id=telegram_id)

        async with get_storage() as storage:
            db_user = await storage.get_user_by_telegram_id(telegram_id)
            logger.debug(
                "db_user_lookup",
                telegram_id=telegram_id,
                found=db_user is not None,
                db_user_id=db_user.id if db_user else None,
            )

            if not db_user:
                logger.info("seedbox_user_not_found", telegram_id=telegram_id)
                return None, None, None

            host = await storage.get_credential(db_user.id, CredentialType.SEEDBOX_HOST)
            username = await storage.get_credential(db_user.id, CredentialType.SEEDBOX_USERNAME)
            password = await storage.get_credential(db_user.id, CredentialType.SEEDBOX_PASSWORD)

            logger.debug(
                "seedbox_credentials_lookup",
                telegram_id=telegram_id,
                db_user_id=db_user.id,
                has_host=host is not None,
                has_username=username is not None,
                has_password=password is not None,
            )

            return host, username, password

    except Exception as e:
        logger.warning("failed_to_get_seedbox_credentials", telegram_id=telegram_id, error=str(e))
        return None, None, None
