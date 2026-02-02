"""Main entry point for the Telegram bot.

This module initializes the bot and sets up handlers for commands and messages.
Supports both polling (development) and webhook (production) modes.
Includes an HTTP health check endpoint for Koyeb/Docker health monitoring.
"""

import asyncio
import contextlib
import json
import sys
from asyncio import StreamReader, StreamWriter
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

from src.bot.conversation import (
    handle_download_callback,
    handle_followup_callback,
    handle_magnet_callback,
    handle_message,
    handle_monitor_callback,
    handle_seedbox_callback,
    handle_torrent_callback,
)
from src.bot.handlers import error_handler, help_handler, profile_handler, reset_profile_handler
from src.bot.library import library_callback, library_command, library_search_handler
from src.bot.onboarding import (
    get_onboarding_conversation_handler,
    onboarding_start_handler,
    settings_callback_handler,
    settings_handler,
)
from src.bot.rutracker_auth import get_rutracker_conversation_handler
from src.bot.seedbox_auth import get_seedbox_conversation_handler
from src.bot.sync_api import (
    handle_library_index_request,
    handle_sync_complete_request,
    handle_sync_pending_request,
    send_sync_notification,
)
from src.config import settings
from src.logger import get_logger
from src.monitoring import MonitoringScheduler

logger = get_logger(__name__)

# Global flag to track bot health
_bot_healthy = False

# Global bot instance for sync notifications
_bot_instance = None

# Global monitoring scheduler instance
_monitoring_scheduler: MonitoringScheduler | None = None


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
    application.add_handler(CommandHandler("profile", profile_handler))
    application.add_handler(CommandHandler("settings", settings_handler))
    application.add_handler(CommandHandler("reset_profile", reset_profile_handler))
    application.add_handler(CommandHandler("health", health_check))

    # Register Rutracker credentials conversation handler
    application.add_handler(get_rutracker_conversation_handler())

    # Register Seedbox credentials conversation handler
    application.add_handler(get_seedbox_conversation_handler())

    # Register model settings handlers
    from src.bot.model_settings import get_model_handlers

    for handler in get_model_handlers():
        application.add_handler(handler)

    # Register onboarding conversation handler (handles file uploads, text inputs)
    application.add_handler(get_onboarding_conversation_handler())

    # Register callback query handlers for inline keyboards
    application.add_handler(CallbackQueryHandler(settings_callback_handler, pattern="^settings_"))
    application.add_handler(CallbackQueryHandler(handle_download_callback, pattern="^download_"))
    application.add_handler(CallbackQueryHandler(handle_monitor_callback, pattern="^monitor_"))

    # Register new download action handlers (magnet, torrent file, seedbox)
    application.add_handler(CallbackQueryHandler(handle_magnet_callback, pattern="^dl_magnet_"))
    application.add_handler(CallbackQueryHandler(handle_torrent_callback, pattern="^dl_torrent_"))
    application.add_handler(CallbackQueryHandler(handle_seedbox_callback, pattern="^dl_seedbox_"))

    # Register follow-up handlers for download feedback
    application.add_handler(CallbackQueryHandler(handle_followup_callback, pattern="^followup_"))

    # Register library browser handlers
    application.add_handler(CommandHandler("library", library_command))
    application.add_handler(CallbackQueryHandler(library_callback, pattern="^lib_"))

    # Library search text handler in higher-priority group.
    # When lib_awaiting_search is set, processes the query and raises ApplicationHandlerStop
    # to prevent handle_message from running. Otherwise returns without stopping.
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, library_search_handler), group=-1
    )

    # Register message handler for natural language conversation
    # This should be last to avoid intercepting commands
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Register error handler
    application.add_error_handler(error_handler)

    logger.info("application_created", handlers_registered=8)

    return application


async def run_polling(application: Application) -> None:
    """Run the bot in polling mode (for development).

    Also starts a health server for local testing of deployment readiness.

    Args:
        application: The bot application instance
    """
    global _bot_healthy, _monitoring_scheduler

    health_port = settings.health_port
    logger.info("starting_polling_mode", health_port=health_port)

    # Start health check server (useful for local testing)
    health_server = await start_health_server(health_port)

    # Initialize the application
    await application.initialize()
    await application.start()
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )

    # Start monitoring scheduler
    _monitoring_scheduler = MonitoringScheduler(application.bot)
    _monitoring_scheduler.start()

    # Mark bot as healthy
    _bot_healthy = True

    logger.info(
        "bot_started_polling",
        mode="polling",
        health_endpoint=f"http://localhost:{health_port}/health",
    )

    # Keep the bot running
    try:
        # Run until stopped
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("bot_stopping", reason="user_interrupt")
    finally:
        # Cleanup
        _bot_healthy = False
        if _monitoring_scheduler:
            _monitoring_scheduler.stop()
            _monitoring_scheduler = None
        health_server.close()
        await health_server.wait_closed()
        await application.updater.stop()
        await application.stop()
        await application.shutdown()
        logger.info("bot_stopped")


async def handle_health_request(reader: StreamReader, writer: StreamWriter) -> None:
    """Handle incoming HTTP requests for health checks and sync API.

    Supports:
    - GET /health - Health check endpoint
    - POST /api/sync/complete - Sync notification from VM script

    Args:
        reader: Async stream reader for the connection
        writer: Async stream writer for the connection
    """
    try:
        # Read the request line
        request_line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        if not request_line:
            return

        request_str = request_line.decode("utf-8").strip()

        # Read headers
        headers: dict[str, str] = {}
        content_length = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            if line == b"\r\n" or line == b"\n" or not line:
                break
            header_line = line.decode("utf-8").strip()
            if ":" in header_line:
                key, value = header_line.split(":", 1)
                headers[key.strip().lower()] = value.strip()
                if key.strip().lower() == "content-length":
                    content_length = int(value.strip())

        # Parse request path
        parts = request_str.split(" ")
        if len(parts) >= 2:
            method, path = parts[0], parts[1]
        else:
            method, path = "GET", "/"

        # Handle /health endpoint
        if path == "/health" and method == "GET":
            status = "healthy" if _bot_healthy else "starting"
            status_code = 200 if _bot_healthy else 503
            body = json.dumps(
                {"status": status, "service": "media-concierge-bot", "ready": _bot_healthy}
            )
            response = (
                f"HTTP/1.1 {status_code} {'OK' if status_code == 200 else 'Service Unavailable'}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{body}"
            )

        # Handle /api/sync/pending endpoint (VM daemon polls this)
        elif path in ("/api/sync/pending", "/sync/pending") and method == "GET":
            api_key = headers.get("x-api-key")
            result, status_code = await handle_sync_pending_request(api_key)
            body = json.dumps(result)
            response = (
                f"HTTP/1.1 {status_code} {'OK' if status_code == 200 else 'Error'}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{body}"
            )

        # Handle /api/sync/complete endpoint (also /sync/complete when Koyeb strips /api prefix)
        elif path in ("/api/sync/complete", "/sync/complete") and method == "POST":
            # Read request body
            request_body = b""
            if content_length > 0:
                request_body = await asyncio.wait_for(reader.read(content_length), timeout=10.0)

            api_key = headers.get("x-api-key")
            result, status_code = await handle_sync_complete_request(request_body, api_key)

            # Send notification if we have a target
            if not _bot_instance:
                logger.warning("sync_notification_skipped_no_bot_instance")
            if _bot_healthy and _bot_instance:
                telegram_id = result.get("telegram_id")
                should_notify = result.get("notify")
                if telegram_id or should_notify:
                    try:
                        # Notify only the specific user who initiated the download
                        notify_ids = []
                        if telegram_id:
                            notify_ids = [telegram_id]
                        if not notify_ids:
                            logger.warning(
                                "sync_notification_no_user",
                                result=result,
                            )
                        for tid in notify_ids:
                            await send_sync_notification(
                                _bot_instance,
                                tid,
                                filename=result.get("filename"),
                                local_path=result.get("local_path"),
                            )
                    except Exception as e:
                        logger.error("sync_notification_error", error=str(e))

            body = json.dumps(result, ensure_ascii=False)
            status_text = "OK" if status_code == 200 else "Error"
            response = (
                f"HTTP/1.1 {status_code} {status_text}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body.encode('utf-8'))}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{body}"
            )

        # Handle /api/sync/library-index endpoint (VM indexer pushes NAS file list)
        elif path in ("/api/sync/library-index", "/sync/library-index") and method == "POST":
            request_body = b""
            if content_length > 0:
                request_body = await asyncio.wait_for(reader.read(content_length), timeout=30.0)

            api_key = headers.get("x-api-key")
            result, status_code = await handle_library_index_request(request_body, api_key)
            body = json.dumps(result, ensure_ascii=False)
            response = (
                f"HTTP/1.1 {status_code} {'OK' if status_code == 200 else 'Error'}\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body.encode('utf-8'))}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{body}"
            )

        else:
            # Return 404 for other paths
            body = json.dumps({"error": "Not Found"})
            response = (
                f"HTTP/1.1 404 Not Found\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Connection: close\r\n"
                f"\r\n"
                f"{body}"
            )

        writer.write(response.encode("utf-8"))
        await writer.drain()

    except TimeoutError:
        logger.debug("health_request_timeout")
    except Exception as e:
        logger.debug("health_request_error", error=str(e))
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def start_health_server(port: int) -> asyncio.Server:
    """Start the HTTP health check server.

    Args:
        port: Port to listen on for health checks

    Returns:
        The running asyncio Server instance
    """
    server = await asyncio.start_server(handle_health_request, "0.0.0.0", port)
    logger.info("health_server_started", port=port, endpoint="/health")
    return server


async def run_webhook(application: Application) -> None:
    """Run the bot in webhook mode (for production on Koyeb).

    Starts both the webhook server for Telegram updates and
    an HTTP health check server for deployment monitoring.

    Args:
        application: The bot application instance
    """
    global _bot_healthy, _monitoring_scheduler

    webhook_url = settings.webhook_url
    webhook_path = settings.webhook_path
    port = settings.port
    health_port = settings.health_port

    if not webhook_url:
        logger.error("webhook_url_not_configured")
        raise ValueError("WEBHOOK_URL must be set for webhook mode")

    logger.info(
        "starting_webhook_mode",
        webhook_url=webhook_url,
        webhook_path=webhook_path,
        webhook_port=port,
        health_port=health_port,
    )

    # Start health check server first (so Koyeb can see we're starting)
    health_server = await start_health_server(health_port)

    # Initialize the application
    await application.initialize()
    await application.start()

    # Start the webhook server for Telegram updates
    # Note: start_webhook calls set_webhook internally, don't call it twice
    await application.updater.start_webhook(
        listen="0.0.0.0",
        port=port,
        url_path=webhook_path,
        webhook_url=f"{webhook_url}{webhook_path}",
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )

    # Start monitoring scheduler
    _monitoring_scheduler = MonitoringScheduler(application.bot)
    _monitoring_scheduler.start()

    # Mark bot as healthy and store bot instance for sync notifications
    _bot_healthy = True
    global _bot_instance
    _bot_instance = application.bot

    logger.info(
        "bot_started_webhook",
        mode="webhook",
        url=f"{webhook_url}{webhook_path}",
        health_endpoint=f"http://0.0.0.0:{health_port}/health",
    )

    # Keep the bot running
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("bot_stopping", reason="user_interrupt")
    finally:
        # Cleanup
        _bot_healthy = False
        if _monitoring_scheduler:
            _monitoring_scheduler.stop()
            _monitoring_scheduler = None
        health_server.close()
        await health_server.wait_closed()
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
