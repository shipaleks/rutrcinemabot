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

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.config import settings
from src.monitoring.checker import FoundRelease, ReleaseChecker
from src.seedbox import send_magnet_to_seedbox
from src.user.storage import Monitor, get_storage

if TYPE_CHECKING:
    from telegram import Bot

logger = structlog.get_logger(__name__)

# Default check interval (6 hours)
DEFAULT_CHECK_INTERVAL_HOURS = 6

# Scheduler runs every 2 hours to check which monitors need updating
SCHEDULER_RUN_INTERVAL_HOURS = 2


def get_check_interval_hours(release_date: datetime | None) -> int:
    """Calculate check interval based on expected release date.

    Args:
        release_date: Expected release date from TMDB

    Returns:
        Interval in hours between checks
    """
    if release_date is None:
        return 24  # Unknown date — check daily

    now = datetime.now(UTC)
    if release_date.tzinfo is None:
        release_date = release_date.replace(tzinfo=UTC)

    days_until = (release_date - now).days

    if days_until <= 0:      # Already released — check frequently
        return 2
    elif days_until <= 7:    # Within a week
        return 4
    elif days_until <= 30:   # Within a month
        return 12
    elif days_until <= 90:   # Within 3 months
        return 24
    else:                    # Far future
        return 72


def should_check_monitor(monitor: Monitor) -> bool:
    """Determine if a monitor should be checked based on smart frequency.

    Args:
        monitor: Monitor to check

    Returns:
        True if the monitor should be checked now
    """
    interval_hours = get_check_interval_hours(monitor.release_date)

    if monitor.last_checked is None:
        return True  # Never checked, should check now

    now = datetime.now(UTC)
    last_checked = monitor.last_checked
    if last_checked.tzinfo is None:
        last_checked = last_checked.replace(tzinfo=UTC)

    time_since_check = now - last_checked
    return time_since_check >= timedelta(hours=interval_hours)


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

        # Add the monitoring job - runs frequently, smart frequency filters inside
        self._scheduler.add_job(
            self._check_all_monitors,
            trigger=IntervalTrigger(hours=SCHEDULER_RUN_INTERVAL_HOURS),
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
        """Check active monitors using smart frequency logic.

        Monitors are checked at different intervals based on their release date:
        - Already released: every 2 hours
        - Within a week: every 4 hours
        - Within a month: every 12 hours
        - Within 3 months: every 24 hours
        - Far future: every 72 hours

        Returns:
            List of found releases
        """
        logger.info("monitoring_check_started")

        if self._checker is None:
            self._checker = self._create_checker()

        found_releases: list[FoundRelease] = []

        try:
            async with get_storage() as storage:
                # Get all active monitors
                all_monitors = await storage.get_all_active_monitors()

                if not all_monitors:
                    logger.debug("no_active_monitors")
                    return []

                # Filter to monitors that should be checked now
                monitors_to_check = [m for m in all_monitors if should_check_monitor(m)]

                # Sort by release date: closer releases first
                monitors_to_check.sort(
                    key=lambda m: m.release_date or datetime.max.replace(tzinfo=UTC)
                )

                logger.info(
                    "checking_monitors",
                    total=len(all_monitors),
                    to_check=len(monitors_to_check),
                )

                if not monitors_to_check:
                    logger.debug("no_monitors_need_checking")
                    return []

                # Check filtered monitors
                found = await self._checker.check_all_monitors(monitors_to_check)

                # Update last_checked for all checked monitors
                for monitor in monitors_to_check:
                    await storage.update_monitor_last_checked(monitor.id)

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
            f"**{release.title}** — доступен\n\n"
            f"{release.quality} | {release.size} | {release.seeds} сидов\n"
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
