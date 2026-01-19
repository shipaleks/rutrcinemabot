"""Monitoring scheduler using APScheduler.

This module provides periodic checking of release monitors
using APScheduler's AsyncIOScheduler.

Usage:
    from telegram import Bot
    from src.monitoring import MonitoringScheduler

    scheduler = MonitoringScheduler(bot)
    scheduler.start()

    # On shutdown:
    scheduler.stop()
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.config import settings
from src.monitoring.checker import FoundRelease, ReleaseChecker
from src.seedbox import send_magnet_to_seedbox
from src.user.storage import get_storage

if TYPE_CHECKING:
    from telegram import Bot

logger = structlog.get_logger(__name__)

# Default check interval (6 hours)
DEFAULT_CHECK_INTERVAL_HOURS = 6


class MonitoringScheduler:
    """Manages periodic monitoring checks using APScheduler.

    This class:
    - Runs periodic checks for all active monitors
    - Sends Telegram notifications when releases are found
    - Optionally triggers auto-download to seedbox
    - Handles graceful shutdown
    """

    def __init__(
        self,
        bot: "Bot",
        check_interval_hours: int = DEFAULT_CHECK_INTERVAL_HOURS,
    ):
        """Initialize the monitoring scheduler.

        Args:
            bot: Telegram Bot instance for sending notifications
            check_interval_hours: Interval between checks (default 6 hours)
        """
        self._bot = bot
        self._check_interval_hours = check_interval_hours
        self._scheduler: AsyncIOScheduler | None = None
        self._checker: ReleaseChecker | None = None
        self._is_running = False

    def _create_checker(self) -> ReleaseChecker:
        """Create a release checker with current settings."""
        rutracker_username = settings.rutracker_username
        rutracker_password = (
            settings.rutracker_password.get_secret_value() if settings.rutracker_password else None
        )

        return ReleaseChecker(
            rutracker_username=rutracker_username,
            rutracker_password=rutracker_password,
        )

    def start(self) -> None:
        """Start the monitoring scheduler.

        This schedules periodic checks for all active monitors.
        """
        if self._is_running:
            logger.warning("scheduler_already_running")
            return

        self._scheduler = AsyncIOScheduler()
        self._checker = self._create_checker()

        # Add the monitoring job
        self._scheduler.add_job(
            self._check_all_monitors,
            trigger=IntervalTrigger(hours=self._check_interval_hours),
            id="release_monitoring",
            name="Release Monitoring Check",
            replace_existing=True,
            max_instances=1,  # Prevent overlapping runs
        )

        self._scheduler.start()
        self._is_running = True

        logger.info(
            "monitoring_scheduler_started",
            interval_hours=self._check_interval_hours,
        )

    def stop(self) -> None:
        """Stop the monitoring scheduler gracefully."""
        if self._scheduler and self._is_running:
            self._scheduler.shutdown(wait=True)
            self._is_running = False
            logger.info("monitoring_scheduler_stopped")

    async def run_now(self) -> list[FoundRelease]:
        """Run a monitoring check immediately (manual trigger).

        Returns:
            List of found releases
        """
        return await self._check_all_monitors()

    async def _check_all_monitors(self) -> list[FoundRelease]:
        """Check all active monitors for all users.

        This is the main job function that runs periodically.

        Returns:
            List of found releases
        """
        logger.info("monitoring_check_started")

        if self._checker is None:
            self._checker = self._create_checker()

        found_releases: list[FoundRelease] = []

        try:
            async with get_storage() as storage:
                # Get all active monitors across all users
                # We need to iterate through users since monitors are per-user
                monitors = await storage.get_all_active_monitors()

                if not monitors:
                    logger.debug("no_active_monitors")
                    return []

                logger.info("checking_monitors", count=len(monitors))

                # Check all monitors
                found = await self._checker.check_all_monitors(monitors)

                for release in found:
                    found_releases.append(release)

                    # Update monitor status
                    await storage.update_monitor_status(
                        release.monitor_id,
                        status="found",
                        found_at=datetime.now(UTC),
                    )

                    # Get user's telegram_id for notification
                    user = await storage.get_user(release.user_id)
                    if user:
                        # Get monitor for auto_download setting
                        monitor = await storage.get_monitor(release.monitor_id)

                        # Send notification
                        await self._notify_user(user.telegram_id, release)

                        # Auto-download if enabled
                        if monitor and monitor.auto_download:
                            await self._auto_download(release)

        except Exception as e:
            logger.exception("monitoring_check_failed", error=str(e))

        logger.info(
            "monitoring_check_completed",
            found_count=len(found_releases),
        )

        return found_releases

    async def _notify_user(
        self,
        telegram_id: int,
        release: FoundRelease,
    ) -> None:
        """Send Telegram notification about found release.

        Args:
            telegram_id: User's Telegram ID
            release: Found release information
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        # Format notification message
        message = (
            f"🎉 **{release.title}** теперь доступен!\n\n"
            f"📺 {release.quality} | 📦 {release.size} | 🌱 {release.seeds} сидов\n\n"
            f"Источник: {release.source.title()}"
        )

        # Create inline keyboard for actions
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "⬇️ Скачать",
                        callback_data=f"monitor_download_{release.monitor_id}",
                    ),
                    InlineKeyboardButton(
                        "📋 Детали",
                        callback_data=f"monitor_details_{release.monitor_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🔕 Отменить мониторинг",
                        callback_data=f"monitor_cancel_{release.monitor_id}",
                    ),
                ],
            ]
        )

        try:
            await self._bot.send_message(
                chat_id=telegram_id,
                text=message,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            logger.info(
                "monitoring_notification_sent",
                telegram_id=telegram_id,
                title=release.title,
            )
        except Exception as e:
            logger.error(
                "monitoring_notification_failed",
                telegram_id=telegram_id,
                error=str(e),
            )

    async def _auto_download(self, release: FoundRelease) -> None:
        """Automatically send release to seedbox.

        Args:
            release: Found release with magnet link
        """
        try:
            result = await send_magnet_to_seedbox(release.magnet)

            if result.get("status") == "sent":
                logger.info(
                    "auto_download_started",
                    title=release.title,
                    torrent_hash=result.get("hash"),
                )
            else:
                logger.warning(
                    "auto_download_failed",
                    title=release.title,
                    error=result.get("error"),
                )

        except Exception as e:
            logger.error(
                "auto_download_error",
                title=release.title,
                error=str(e),
            )


# Storage extension for monitoring


async def _get_all_active_monitors_impl(storage: Any) -> list[Any]:
    """Get all active monitors from all users.

    This is a helper that can be added to storage classes.
    """
    # This method needs to be added to storage implementations
    # For now, return empty list if not implemented
    if hasattr(storage, "get_all_active_monitors"):
        return await storage.get_all_active_monitors()

    # Fallback: iterate through all users and collect monitors
    monitors = []
    try:
        # Get all users (limited for safety)
        users = await storage.get_all_users(limit=1000)
        for user in users:
            user_monitors = await storage.get_monitors(user_id=user.id, status="active")
            monitors.extend(user_monitors)
    except Exception:
        pass

    return monitors
