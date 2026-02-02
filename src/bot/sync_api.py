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
from src.monitoring.torrent_monitor import check_and_reset_sync_needed
from src.user.storage import get_storage

logger = structlog.get_logger(__name__)


async def handle_sync_pending_request(
    api_key: str | None,
) -> tuple[dict, int]:
    """Handle sync pending check from VM daemon.

    Returns:
        Tuple of (response_dict, status_code)
    """
    expected_key = settings.sync_api_key
    if not expected_key:
        return {"error": "API not configured"}, 503

    if api_key != expected_key.get_secret_value():
        return {"error": "Unauthorized"}, 401

    sync_needed = check_and_reset_sync_needed()
    return {"sync_needed": sync_needed}, 200


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
                            "filename": filename,
                            "local_path": local_path,
                            "message": "Sync recorded, notification pending",
                        }, 200
                    return {"ok": True, "message": "Sync recorded"}, 200
                logger.warning("sync_torrent_not_found", torrent_hash=torrent_hash)
                return {"ok": False, "message": "Torrent not found"}, 404
            # No hash — try to find user by torrent name
            if filename:
                # Strip "(N файлов)" suffix added by sync daemon for multi-file syncs
                import re

                search_name = re.sub(r"\s*\(\d+\s+файлов?\)\s*$", "", filename)
                user = await storage.get_user_by_torrent_name(search_name)
                if user:
                    logger.info(
                        "sync_complete_user_found_by_name",
                        user_id=user.id,
                        telegram_id=user.telegram_id,
                        filename=filename,
                    )
                    return {
                        "ok": True,
                        "telegram_id": user.telegram_id,
                        "filename": filename,
                        "local_path": local_path,
                        "message": "Sync noted, notification pending",
                    }, 200
            return {
                "ok": True,
                "filename": filename,
                "local_path": local_path,
                "notify": True,
                "message": "Sync noted, user not found",
            }, 200

    except Exception as e:
        logger.exception("sync_api_error", error=str(e))
        return {"error": str(e)}, 500


async def handle_library_index_request(
    request_body: bytes,
    api_key: str | None,
) -> tuple[dict, int]:
    """Handle library index push from VM indexer script.

    Receives JSON with 'movies' and 'tv' arrays, stores in DB.
    """
    expected_key = settings.sync_api_key
    if not expected_key:
        return {"error": "API not configured"}, 503

    if api_key != expected_key.get_secret_value():
        return {"error": "Unauthorized"}, 401

    try:
        data = json.loads(request_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return {"error": f"Invalid JSON: {e}"}, 400

    movies = data.get("movies", [])
    tv = data.get("tv", [])

    try:
        async with get_storage() as storage:
            if "movies" in data:
                await storage.save_library_index("movies", json.dumps(movies, ensure_ascii=False))
            if "tv" in data:
                await storage.save_library_index("tv", json.dumps(tv, ensure_ascii=False))

        logger.info("library_index_saved", movies=len(movies), tv=len(tv))
        return {"ok": True, "movies": len(movies), "tv": len(tv)}, 200

    except Exception as e:
        logger.exception("library_index_error", error=str(e))
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
        message_parts = ["✅ Скачано и готово к просмотру!"]
        if filename:
            # Clean up filename for display
            clean = filename.rsplit(".", 1)[0] if "." in filename else filename
            clean = clean.replace(".", " ")
            message_parts.append(f"\n🎬 {clean}")
        if local_path:
            # Show just the destination folder
            folder = local_path.rsplit("/", 1)[-1] if "/" in local_path else local_path
            if "Сериалы" in (local_path or ""):
                message_parts.append(f"\n📂 Сериалы / {folder}")
            elif "Кино" in (local_path or ""):
                message_parts.append(f"\n📂 Кино / {folder}")
            else:
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
