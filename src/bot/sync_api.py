"""Sync notification API for seedbox-to-NAS sync script.

This module provides a simple webhook endpoint that the VM sync script
calls when files are synced from the seedbox to the local NAS.

Usage:
    The sync script should call:
    POST /api/sync/complete
    Headers:
        X-API-Key: <SYNC_API_KEY>
    Body:
        {"torrent_hash": "...", "filename": "...", "local_path": "..."}
"""

import json
from datetime import UTC, datetime

import structlog

from src.config import settings
from src.user.storage import get_storage

logger = structlog.get_logger(__name__)


async def handle_sync_complete_request(
    request_body: bytes,
    api_key: str | None,
) -> tuple[dict, int]:
    """Handle sync complete webhook from VM script.

    Args:
        request_body: Raw request body (JSON)
        api_key: X-API-Key header value

    Returns:
        Tuple of (response_dict, status_code)
    """
    # Validate API key
    expected_key = settings.sync_api_key
    if not expected_key:
        logger.warning("sync_api_key_not_configured")
        return {"error": "API not configured"}, 503

    if api_key != expected_key.get_secret_value():
        logger.warning("sync_api_invalid_key")
        return {"error": "Unauthorized"}, 401

    # Parse request body
    try:
        data = json.loads(request_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logger.warning("sync_api_invalid_json", error=str(e))
        return {"error": "Invalid JSON"}, 400

    torrent_hash = data.get("torrent_hash")
    filename = data.get("filename")
    local_path = data.get("local_path")

    if not (torrent_hash or filename):
        return {"error": "torrent_hash or filename required"}, 400

    logger.info(
        "sync_complete_received",
        torrent_hash=torrent_hash,
        filename=filename,
        local_path=local_path,
    )

    # Update torrent status in database
    try:
        async with get_storage() as storage:
            # Try to find by hash first, then by filename
            if torrent_hash:
                success = await storage.update_torrent_status(
                    torrent_hash=torrent_hash,
                    status="synced",
                    synced_at=datetime.now(UTC),
                    local_path=local_path,
                )
                if success:
                    # Get user for notification
                    user = await storage.get_user_by_torrent_hash(torrent_hash)
                    if user:
                        logger.info(
                            "sync_complete_user_found",
                            user_id=user.id,
                            telegram_id=user.telegram_id,
                        )
                        # Return user info for notification (caller can send message)
                        return {
                            "ok": True,
                            "telegram_id": user.telegram_id,
                            "message": "Sync recorded, notification pending",
                        }, 200
                    return {"ok": True, "message": "Sync recorded"}, 200
                logger.warning("sync_torrent_not_found", torrent_hash=torrent_hash)
                return {"ok": False, "message": "Torrent not found"}, 404
            # No hash provided, just log
            return {"ok": True, "message": "Sync noted (no hash)"}, 200

    except Exception as e:
        logger.exception("sync_api_error", error=str(e))
        return {"error": str(e)}, 500


async def send_sync_notification(
    bot,
    telegram_id: int,
    filename: str | None = None,
    local_path: str | None = None,
) -> bool:
    """Send sync notification to user via Telegram.

    Args:
        bot: Telegram bot instance
        telegram_id: User's Telegram ID
        filename: Name of synced file
        local_path: Path where file was synced

    Returns:
        True if notification sent successfully
    """
    try:
        message_parts = ["📦 Файл синхронизирован на NAS и готов к просмотру!"]
        if filename:
            message_parts.append(f"\n📁 {filename}")
        if local_path:
            # Show just the folder, not full path
            folder = local_path.rsplit("/", 1)[-1] if "/" in local_path else local_path
            message_parts.append(f"\n📂 {folder}")

        await bot.send_message(
            chat_id=telegram_id,
            text="".join(message_parts),
        )
        logger.info("sync_notification_sent", telegram_id=telegram_id)
        return True
    except Exception as e:
        logger.error("sync_notification_failed", telegram_id=telegram_id, error=str(e))
        return False
