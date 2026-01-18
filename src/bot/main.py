"""Main entry point for the Telegram bot.

This module initializes the bot and sets up handlers for commands and messages.
Supports both polling (development) and webhook (production) modes.
"""

import asyncio
import sys
from typing import NoReturn

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.bot.conversation import handle_download_callback, handle_message
from src.bot.handlers import error_handler, help_handler
from src.bot.onboarding import (
    onboarding_callback_handler,
    onboarding_start_handler,
    settings_callback_handler,
    settings_handler,
)
from src.config import settings
from src.logger import get_logger

logger = get_logger(__name__)


async def health_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Health check endpoint for deployment platforms.

    Args:
        update: Telegram update object
        context: Callback context
    """
    await update.message.reply_text("OK")


def create_application() -> Application:
    """Create and configure the Telegram bot application.

    Returns:
        Configured Application instance
    """
    logger.info("creating_application", environment=settings.environment)

    # Create application with bot token
    application = (
        Application.builder().token(settings.telegram_bot_token.get_secret_value()).build()
    )

    # Register command handlers
    application.add_handler(CommandHandler("start", onboarding_start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("settings", settings_handler))
    application.add_handler(CommandHandler("health", health_check))

    # Register callback query handlers for inline keyboards
    application.add_handler(CallbackQueryHandler(onboarding_callback_handler, pattern="^onboard_"))
    application.add_handler(CallbackQueryHandler(settings_callback_handler, pattern="^settings_"))
    application.add_handler(CallbackQueryHandler(handle_download_callback, pattern="^download_"))

    # Register message handler for natural language conversation
    # This should be last to avoid intercepting commands
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Register error handler
    application.add_error_handler(error_handler)

    logger.info("application_created", handlers_registered=8)

    return application


async def run_polling(application: Application) -> None:
    """Run the bot in polling mode (for development).

    Args:
        application: The bot application instance
    """
    logger.info("starting_polling_mode")

    # Initialize the application
    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    logger.info("bot_started_polling", mode="polling")

    # Keep the bot running
    try:
        # Run until stopped
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("bot_stopping", reason="user_interrupt")
    finally:
        # Cleanup
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("bot_stopped")


async def run_webhook(application: Application) -> None:
    """Run the bot in webhook mode (for production on Koyeb).

    Args:
        application: The bot application instance
    """
    webhook_url = settings.webhook_url
    webhook_path = settings.webhook_path
    port = settings.port

    if not webhook_url:
        logger.error("webhook_url_not_configured")
        raise ValueError("WEBHOOK_URL must be set for webhook mode")

    logger.info(
        "starting_webhook_mode",
        webhook_url=webhook_url,
        webhook_path=webhook_path,
        port=port,
    )

    # Initialize the application
    await application.initialize()
    await application.start()

    # Set up webhook
    await application.bot.set_webhook(
        url=f"{webhook_url}{webhook_path}",
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    # Start the webhook server
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=f"{webhook_url}{webhook_path}",
    )

    logger.info("bot_started_webhook", mode="webhook", url=f"{webhook_url}{webhook_path}")

    # Keep the bot running
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("bot_stopping", reason="user_interrupt")
    finally:
        # Cleanup
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("bot_stopped")


async def main_async() -> None:
    """Main async entry point for the bot."""
    logger.info(
        "bot_starting",
        environment=settings.environment,
        log_level=settings.log_level,
    )

    # Create application
    application = create_application()

    # Choose run mode based on environment
    if settings.is_production and settings.webhook_url:
        logger.info("using_webhook_mode")
        await run_webhook(application)
    else:
        logger.info("using_polling_mode")
        await run_polling(application)


def main() -> NoReturn:
    """Main entry point for the bot.

    This function is called when running the module directly.
    """
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("bot_interrupted")
        sys.exit(0)
    except Exception as e:
        logger.exception("bot_crashed", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
