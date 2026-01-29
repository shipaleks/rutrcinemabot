"""Background torrent monitor for seedbox downloads.

Periodically checks Deluge for torrent completion status.
When a torrent finishes downloading, notifies the user and
signals the sync daemon to start copying to NAS.
"""

import asyncio
from typing import TYPE_CHECKING

import structlog

from src.bot.seedbox_auth import get_user_seedbox_credentials
from src.seedbox.client import DelugeClient
from src.user.storage import get_storage

if TYPE_CHECKING:
    from telegram import Bot

logger = structlog.get_logger(__name__)

# In-memory flag for sync daemon polling
_sync_needed = asyncio.Event()


def set_sync_needed() -> None:
    """Signal that new files are ready for sync."""
    _sync_needed.set()


def check_and_reset_sync_needed() -> bool:
    """Check if sync is needed and reset the flag."""
    if _sync_needed.is_set():
        _sync_needed.clear()
        return True
    return False


class TorrentMonitor:
    """Monitors active torrents on user seedboxes.

    Checks Deluge every interval for completed downloads,
    updates tracking status and notifies users.
    """

    def __init__(self, bot: "Bot") -> None:
        self._bot = bot

    async def check_active_torrents(self) -> None:
        """Check all torrents with 'downloading' status."""
        try:
            async with get_storage() as storage:
                # Get all downloading torrents grouped by user
                torrents = await storage.get_downloading_torrents()

            if not torrents:
                return

            # Group by user_id to reuse Deluge connections
            by_user: dict[int, list] = {}
            for t in torrents:
                by_user.setdefault(t.user_id, []).append(t)

            for user_id, user_torrents in by_user.items():
                await self._check_user_torrents(user_id, user_torrents)

        except Exception as e:
            logger.error("torrent_monitor_error", error=str(e))

    async def _check_user_torrents(self, user_id: int, torrents: list) -> None:
        """Check torrents for a specific user."""
        try:
            # Get user's telegram_id and seedbox credentials
            async with get_storage() as storage:
                user = await storage.get_user(user_id)
                if not user:
                    return

            host, username, password = await get_user_seedbox_credentials(user.telegram_id)
            if not host or not password:
                # Try global seedbox
                from src.config import settings

                if not settings.seedbox_host or not settings.seedbox_password:
                    return
                host = str(settings.seedbox_host)
                username = str(settings.seedbox_user or "")
                password = settings.seedbox_password.get_secret_value()

            async with DelugeClient(
                host=host, username=username or "", password=password
            ) as client:
                for torrent in torrents:
                    await self._check_single_torrent(client, torrent, user)

        except Exception as e:
            logger.warning(
                "torrent_check_user_failed",
                user_id=user_id,
                error=str(e),
            )

    async def _check_single_torrent(self, client: DelugeClient, torrent, user) -> None:
        """Check a single torrent's status."""
        try:
            info = await client.get_torrent_status(torrent.torrent_hash)
            if not info:
                logger.debug(
                    "torrent_not_found_on_seedbox",
                    hash=torrent.torrent_hash[:8],
                )
                return

            if info.is_complete:
                # Update status to seeding
                async with get_storage() as storage:
                    await storage.update_torrent_status(
                        torrent_hash=torrent.torrent_hash,
                        status="seeding",
                    )

                logger.info(
                    "torrent_completed",
                    hash=torrent.torrent_hash[:8],
                    name=torrent.torrent_name[:50],
                    user_id=user.id,
                )

                # Notify user
                clean_name = torrent.torrent_name.replace(".", " ")
                await self._bot.send_message(
                    chat_id=user.telegram_id,
                    text=(f"⬇️ Скачано на сервер!\n\n🎬 {clean_name[:80]}\n\nКопирую домой..."),
                )

                # Signal sync daemon
                set_sync_needed()

        except Exception as e:
            logger.debug(
                "torrent_check_failed",
                hash=torrent.torrent_hash[:8],
                error=str(e),
            )
