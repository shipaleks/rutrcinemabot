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
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from src.config import settings
from src.monitoring.checker import FoundRelease, ReleaseChecker
from src.monitoring.torrent_monitor import TorrentMonitor
from src.seedbox import send_magnet_to_seedbox
from src.user.storage import Monitor, get_storage

if TYPE_CHECKING:
    from telegram import Bot

logger = structlog.get_logger(__name__)

# Default check interval (6 hours)
DEFAULT_CHECK_INTERVAL_HOURS = 6

# Scheduler runs every 2 hours to check which monitors need updating
SCHEDULER_RUN_INTERVAL_HOURS = 2

# Memory maintenance intervals
MEMORY_ARCHIVAL_INTERVAL_HOURS = 24  # Run once daily
LEARNING_DETECTION_INTERVAL_HOURS = 12  # Run twice daily

# Follow-up intervals
FOLLOWUP_CHECK_INTERVAL_HOURS = 6  # Check for pending follow-ups 4 times daily
FOLLOWUP_DAYS_THRESHOLD = 3  # Days after download before sending follow-up

# Proactive push intervals
DIRECTOR_RELEASES_CHECK_HOURS = 168  # Check once per week (7 * 24)
HIDDEN_GEM_CHECK_HOURS = 168  # Send hidden gem once per week

# Push delivery settings
PUSH_DELIVERY_CHECK_HOURS = 4  # Check for pending pushes every 4 hours
PUSH_MIN_HOUR = 18  # Earliest hour to send pushes (evening)
PUSH_MAX_HOUR = 21  # Latest hour to send pushes

# Industry news settings
NEWS_CHECK_INTERVAL_HOURS = 24  # Check news once per day

# Torrent monitor settings
TORRENT_CHECK_INTERVAL_SECONDS = 300  # Check Deluge every 5 minutes
DELUGE_CLEANUP_INTERVAL_HOURS = 24  # Clean completed torrents from Deluge daily

# TMDB enrichment settings
TMDB_ENRICHMENT_INTERVAL_HOURS = 1  # Enrich watched items every hour
TMDB_ENRICHMENT_BATCH_SIZE = 100  # Process 100 items per run


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

    if days_until <= 0:  # Already released — check frequently
        return 2
    if days_until <= 7:  # Within a week
        return 4
    if days_until <= 30:  # Within a month
        return 12
    if days_until <= 90:  # Within 3 months
        return 24
    # Far future
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

        # Suppress noisy APScheduler execution logs for frequent jobs
        import logging

        logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
        logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)

        self._scheduler = AsyncIOScheduler()
        self._checker = self._create_checker()
        self._torrent_monitor = TorrentMonitor(self._bot)

        # Add torrent monitor job - checks Deluge every 60 seconds
        self._scheduler.add_job(
            self._torrent_monitor.check_active_torrents,
            trigger=IntervalTrigger(seconds=TORRENT_CHECK_INTERVAL_SECONDS),
            id="torrent_monitor",
            name="Torrent Download Monitor",
            replace_existing=True,
            max_instances=1,
        )

        # Add daily Deluge cleanup job — 4:00 AM Paris time
        self._scheduler.add_job(
            self._torrent_monitor.cleanup_completed_torrents,
            trigger=CronTrigger(hour=4, minute=0, timezone="Europe/Paris"),
            id="deluge_cleanup",
            name="Deluge Completed Torrents Cleanup",
            replace_existing=True,
            max_instances=1,
        )

        # Add the monitoring job - runs frequently, smart frequency filters inside
        self._scheduler.add_job(
            self._check_all_monitors,
            trigger=IntervalTrigger(hours=SCHEDULER_RUN_INTERVAL_HOURS),
            id="release_monitoring",
            name="Release Monitoring Check",
            replace_existing=True,
            max_instances=1,  # Prevent overlapping runs
        )

        # Add memory archival job - runs once daily
        self._scheduler.add_job(
            self._run_memory_archival,
            trigger=IntervalTrigger(hours=MEMORY_ARCHIVAL_INTERVAL_HOURS),
            id="memory_archival",
            name="Memory Archival",
            replace_existing=True,
            max_instances=1,
        )

        # Add learning detection job - runs twice daily
        self._scheduler.add_job(
            self._run_learning_detection,
            trigger=IntervalTrigger(hours=LEARNING_DETECTION_INTERVAL_HOURS),
            id="learning_detection",
            name="Learning Detection",
            replace_existing=True,
            max_instances=1,
        )

        # Add download follow-up job - check every 6 hours
        self._scheduler.add_job(
            self._check_pending_followups,
            trigger=IntervalTrigger(hours=FOLLOWUP_CHECK_INTERVAL_HOURS),
            id="download_followups",
            name="Download Follow-ups",
            replace_existing=True,
            max_instances=1,
        )

        # Add director releases check - once per week
        self._scheduler.add_job(
            self._check_director_releases,
            trigger=IntervalTrigger(hours=DIRECTOR_RELEASES_CHECK_HOURS),
            id="director_releases",
            name="Director Releases",
            replace_existing=True,
            max_instances=1,
        )

        # Add hidden gem recommendations - once per week
        self._scheduler.add_job(
            self._generate_hidden_gems,
            trigger=IntervalTrigger(hours=HIDDEN_GEM_CHECK_HOURS),
            id="hidden_gems",
            name="Hidden Gem Recommendations",
            replace_existing=True,
            max_instances=1,
        )

        # Add push delivery job - check every 4 hours, sends in evening window
        self._scheduler.add_job(
            self._deliver_pending_pushes,
            trigger=IntervalTrigger(hours=PUSH_DELIVERY_CHECK_HOURS),
            id="push_delivery",
            name="Push Notification Delivery",
            replace_existing=True,
            max_instances=1,
        )

        # Add industry news check - once per day
        self._scheduler.add_job(
            self._check_industry_news,
            trigger=IntervalTrigger(hours=NEWS_CHECK_INTERVAL_HOURS),
            id="industry_news",
            name="Industry News Check",
            replace_existing=True,
            max_instances=1,
        )

        # Add TMDB enrichment for watched items - every hour
        self._scheduler.add_job(
            self._enrich_watched_tmdb,
            trigger=IntervalTrigger(hours=TMDB_ENRICHMENT_INTERVAL_HOURS),
            id="tmdb_enrichment",
            name="TMDB Watched Items Enrichment",
            replace_existing=True,
            max_instances=1,
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

                    # Get monitor for settings before updating status
                    monitor = await storage.get_monitor(release.monitor_id)
                    if not monitor:
                        continue

                    # Store found release data for later download via button
                    found_data = {
                        "magnet": release.magnet,
                        "quality": release.quality,
                        "size": release.size,
                        "seeds": release.seeds,
                        "source": release.source,
                        "torrent_title": release.torrent_title,
                    }

                    # Auto-create next episode monitor for episode tracking mode
                    next_monitor_id = None
                    if (
                        monitor.media_type == "tv"
                        and monitor.tracking_mode == "episode"
                        and monitor.episode_number is not None
                    ):
                        next_monitor_id = await self._create_next_episode_monitor(storage, monitor)

                    # Get user's telegram_id for notification
                    user = await storage.get_user(release.user_id)
                    if user:
                        # Send notification BEFORE updating status to "found"
                        # so that if notification fails, the monitor stays active
                        # and will be retried on next check
                        notified = await self._notify_user(
                            user.telegram_id, release, monitor, next_monitor_id
                        )

                        if not notified:
                            logger.warning(
                                "skipping_status_update_notification_failed",
                                monitor_id=release.monitor_id,
                                title=release.title,
                            )
                            continue

                        # Only mark as "found" after successful notification
                        await storage.update_monitor_status(
                            release.monitor_id,
                            status="found",
                            found_at=datetime.now(UTC),
                            found_data=found_data,
                        )

                        # Auto-download if enabled
                        if monitor.auto_download:
                            await self._auto_download(release)

                        # Sync monitors to memory (update active_context)
                        try:
                            await self._sync_monitors_to_memory(storage, release.user_id)
                        except Exception as e:
                            logger.warning(
                                "sync_monitors_to_memory_failed",
                                user_id=release.user_id,
                                error=str(e),
                            )

        except Exception as e:
            logger.exception("monitoring_check_failed", error=str(e))

        logger.info(
            "monitoring_check_completed",
            found_count=len(found_releases),
        )

        return found_releases

    async def _run_memory_archival(self) -> None:
        """Run periodic memory archival for all users.

        Archives old, low-access memory notes to prevent unbounded growth.
        """
        logger.info("memory_archival_started")

        try:
            from src.user.memory import MemoryArchiver

            async with get_storage() as storage:
                archiver = MemoryArchiver(storage)
                results = await archiver.run_archival_for_all_users()

                total_archived = sum(results.values())
                if total_archived > 0:
                    logger.info(
                        "memory_archival_completed",
                        users_affected=len(results),
                        total_archived=total_archived,
                    )
                else:
                    logger.debug("memory_archival_completed_nothing_to_archive")

        except Exception as e:
            logger.exception("memory_archival_failed", error=str(e))

    async def _sync_monitors_to_memory(self, storage: Any, user_id: int) -> None:
        """Sync active monitors to user's active_context memory block.

        Args:
            storage: Storage instance
            user_id: Internal user ID
        """
        from src.user.memory import CoreMemoryManager

        try:
            # Get active monitors
            monitors = await storage.get_monitors(user_id=user_id, status="active")

            if not monitors:
                # Clear active_context if no monitors
                memory_manager = CoreMemoryManager(storage)
                await memory_manager.update_block(
                    user_id=user_id,
                    block_name="active_context",
                    content="",
                    operation="replace",
                )
                return

            # Build summary text
            waiting_for = []
            for m in monitors[:5]:  # Limit to 5 most recent
                media_emoji = "📺" if m.media_type == "tv" else "🎬"
                waiting_for.append(f"{media_emoji} {m.title} ({m.quality})")

            content = "Waiting for releases:\n" + "\n".join(waiting_for)

            # Update active_context block
            memory_manager = CoreMemoryManager(storage)
            await memory_manager.update_block(
                user_id=user_id,
                block_name="active_context",
                content=content,
                operation="replace",
            )
            logger.debug("monitors_synced_to_memory", user_id=user_id, count=len(waiting_for))

        except Exception as e:
            logger.warning("sync_monitors_to_memory_error", user_id=user_id, error=str(e))

    async def _run_learning_detection(self) -> None:
        """Run periodic learning detection for all users.

        Analyzes user ratings and behavior to extract patterns.
        """
        logger.info("learning_detection_started")

        try:
            from src.user.memory import LearningDetector

            async with get_storage() as storage:
                users = await storage.get_all_users(limit=1000)
                total_learnings = 0

                for user in users:
                    try:
                        detector = LearningDetector(storage)
                        notes = await detector.analyze_ratings(user.id)
                        total_learnings += len(notes)
                    except Exception as e:
                        logger.warning(
                            "learning_detection_user_failed",
                            user_id=user.id,
                            error=str(e),
                        )

                if total_learnings > 0:
                    logger.info(
                        "learning_detection_completed",
                        users_processed=len(users),
                        learnings_created=total_learnings,
                    )
                else:
                    logger.debug("learning_detection_completed_no_new_learnings")

        except Exception as e:
            logger.exception("learning_detection_failed", error=str(e))

    async def _create_next_episode_monitor(
        self,
        storage: Any,
        current_monitor: Monitor,
    ) -> int | None:
        """Create monitor for the next episode automatically.

        Args:
            storage: Storage instance
            current_monitor: The monitor that was just found

        Returns:
            New monitor ID if created, None otherwise
        """
        if current_monitor.episode_number is None or current_monitor.season_number is None:
            return None

        next_episode = current_monitor.episode_number + 1

        # Fetch release_date for next episode from TMDB
        release_date = None
        if current_monitor.tmdb_id:
            try:
                from src.media.tmdb import TMDBClient

                async with TMDBClient() as tmdb:
                    air_date_str = await tmdb.get_episode_air_date(
                        current_monitor.tmdb_id,
                        current_monitor.season_number,
                        next_episode,
                    )
                    if air_date_str:
                        release_date = datetime.fromisoformat(air_date_str)
                        if release_date.tzinfo is None:
                            release_date = release_date.replace(tzinfo=UTC)
                        logger.info(
                            "next_episode_air_date_fetched",
                            tmdb_id=current_monitor.tmdb_id,
                            season=current_monitor.season_number,
                            episode=next_episode,
                            air_date=air_date_str,
                        )
            except Exception as e:
                logger.warning(
                    "fetch_next_episode_air_date_failed",
                    tmdb_id=current_monitor.tmdb_id,
                    error=str(e),
                )

        try:
            new_monitor = await storage.create_monitor(
                user_id=current_monitor.user_id,
                title=current_monitor.title,
                tmdb_id=current_monitor.tmdb_id,
                media_type="tv",
                quality=current_monitor.quality,
                auto_download=current_monitor.auto_download,
                tracking_mode="episode",
                season_number=current_monitor.season_number,
                episode_number=next_episode,
                release_date=release_date,
            )

            logger.info(
                "next_episode_monitor_created",
                title=current_monitor.title,
                season=current_monitor.season_number,
                episode=next_episode,
                new_monitor_id=new_monitor.id,
                release_date=release_date.isoformat() if release_date else None,
            )

            return new_monitor.id

        except Exception as e:
            logger.error(
                "next_episode_monitor_creation_failed",
                title=current_monitor.title,
                error=str(e),
            )
            return None

    @staticmethod
    def _escape_markdown(text: str) -> str:
        """Escape Telegram Markdown special characters.

        Args:
            text: Text to escape

        Returns:
            Escaped text safe for Markdown parse mode
        """
        # Telegram Markdown v1 special chars: _ * ` [
        for char in ("_", "*", "`", "["):
            text = text.replace(char, f"\\{char}")
        return text

    async def _notify_user(
        self,
        telegram_id: int,
        release: FoundRelease,
        monitor: Monitor | None = None,
        next_monitor_id: int | None = None,
    ) -> bool:
        """Send Telegram notification about found release.

        Args:
            telegram_id: User's Telegram ID
            release: Found release information
            monitor: Original monitor (for episode info)
            next_monitor_id: ID of auto-created next episode monitor

        Returns:
            True if notification was sent successfully
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        # Format episode info if available
        episode_info = ""
        if (
            monitor
            and monitor.tracking_mode == "episode"
            and monitor.season_number
            and monitor.episode_number
        ):
            episode_info = f" S{monitor.season_number:02d}E{monitor.episode_number:02d}"

        # Escape special Markdown characters in dynamic content
        safe_title = self._escape_markdown(release.title)
        safe_quality = self._escape_markdown(release.quality)
        safe_source = self._escape_markdown(release.source.title())

        # Format notification message
        message = (
            f"🎬 *{safe_title}{episode_info}* — доступен!\n\n"
            f"📊 {safe_quality} | {release.size} | {release.seeds} сидов\n"
            f"📡 Источник: {safe_source}"
        )

        # Add next episode info
        if next_monitor_id and monitor and monitor.episode_number:
            next_ep = monitor.episode_number + 1
            message += f"\n\n⏭️ Мониторинг следующего эпизода (E{next_ep:02d}) создан автоматически"

        # Create inline keyboard for actions
        buttons = [
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
        ]

        # Add cancel next episode button if one was created
        if next_monitor_id:
            buttons.append(
                [
                    InlineKeyboardButton(
                        "⏹️ Отменить следующий эпизод",
                        callback_data=f"monitor_cancel_{next_monitor_id}",
                    ),
                ]
            )
        else:
            buttons.append(
                [
                    InlineKeyboardButton(
                        "🔕 Отменить мониторинг",
                        callback_data=f"monitor_cancel_{release.monitor_id}",
                    ),
                ]
            )

        keyboard = InlineKeyboardMarkup(buttons)

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
                next_episode_created=next_monitor_id is not None,
            )
            return True
        except Exception as e:
            logger.warning(
                "monitoring_notification_markdown_failed",
                telegram_id=telegram_id,
                error=str(e),
            )

        # Fallback: send without Markdown formatting
        try:
            plain_message = (
                f"🎬 {release.title}{episode_info} — доступен!\n\n"
                f"📊 {release.quality} | {release.size} | {release.seeds} сидов\n"
                f"📡 Источник: {release.source.title()}"
            )
            if next_monitor_id and monitor and monitor.episode_number:
                next_ep = monitor.episode_number + 1
                plain_message += (
                    f"\n\n⏭️ Мониторинг следующего эпизода (E{next_ep:02d}) создан автоматически"
                )

            await self._bot.send_message(
                chat_id=telegram_id,
                text=plain_message,
                reply_markup=keyboard,
            )
            logger.info(
                "monitoring_notification_sent_plain",
                telegram_id=telegram_id,
                title=release.title,
            )
            return True
        except Exception as e:
            logger.error(
                "monitoring_notification_failed",
                telegram_id=telegram_id,
                error=str(e),
            )
            return False

    async def _check_pending_followups(self) -> None:
        """Check for downloads that need follow-up and send notifications.

        Sends "Did you like it?" messages for downloads older than FOLLOWUP_DAYS_THRESHOLD
        that haven't been followed up yet.
        """
        logger.info("followup_check_started")

        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            async with get_storage() as storage:
                # Get downloads that need follow-up
                pending = await storage.get_pending_followups(days=FOLLOWUP_DAYS_THRESHOLD)

                if not pending:
                    logger.debug("followup_check_no_pending")
                    return

                logger.info("followup_pending_found", count=len(pending))

                for download in pending:
                    try:
                        # Get user's telegram_id
                        user = await storage.get_user(download.user_id)
                        if not user:
                            continue

                        # Format the follow-up message
                        title = download.title
                        if download.season and download.episode:
                            title += f" S{download.season:02d}E{download.episode:02d}"
                        elif download.season:
                            title += f" S{download.season:02d}"

                        safe_title = self._escape_markdown(title)
                        message = (
                            f"🎬 Ты скачал *{safe_title}* несколько дней назад.\n\nПонравилось?"
                        )

                        # Create inline keyboard
                        buttons = [
                            [
                                InlineKeyboardButton(
                                    "👍 Да",
                                    callback_data=f"followup_yes_{download.id}",
                                ),
                                InlineKeyboardButton(
                                    "👎 Нет",
                                    callback_data=f"followup_no_{download.id}",
                                ),
                            ],
                            [
                                InlineKeyboardButton(
                                    "📝 Оценить 1-10",
                                    callback_data=f"followup_rate_{download.id}",
                                ),
                            ],
                        ]
                        keyboard = InlineKeyboardMarkup(buttons)

                        # Send the message with Markdown fallback
                        try:
                            await self._bot.send_message(
                                chat_id=user.telegram_id,
                                text=message,
                                parse_mode="Markdown",
                                reply_markup=keyboard,
                            )
                        except Exception:
                            await self._bot.send_message(
                                chat_id=user.telegram_id,
                                text=f"🎬 Ты скачал {title} несколько дней назад.\n\nПонравилось?",
                                reply_markup=keyboard,
                            )

                        # Mark as sent
                        await storage.mark_followup_sent(download.id)

                        logger.info(
                            "followup_sent",
                            user_id=user.id,
                            download_id=download.id,
                            title=download.title,
                        )

                    except Exception as e:
                        logger.warning(
                            "followup_send_failed",
                            download_id=download.id,
                            error=str(e),
                        )

                logger.info("followup_check_completed", sent=len(pending))

        except Exception as e:
            logger.exception("followup_check_failed", error=str(e))

    async def _check_director_releases(self) -> None:
        """Check for new movies from favorite directors.

        Analyzes memory_notes for favorite directors and checks TMDB
        for upcoming releases.
        """
        logger.info("director_releases_check_started")

        try:
            from src.media.tmdb import TMDBClient

            async with get_storage() as storage:
                users = await storage.get_all_users(limit=1000)

                for user in users:
                    try:
                        # Get memory notes about directors
                        notes = await storage.search_memory_notes(
                            user_id=user.id,
                            query="director",
                            limit=20,
                        )

                        # Also check for "режиссёр" in Russian
                        notes_ru = await storage.search_memory_notes(
                            user_id=user.id,
                            query="режиссёр",
                            limit=20,
                        )
                        notes.extend(notes_ru)

                        if not notes:
                            continue

                        # Extract director names from notes
                        director_names = set()
                        for note in notes:
                            # Simple extraction - look for patterns like "director: X" or "режиссёр X"
                            content = note.content.lower()
                            if "director" in content or "режиссёр" in content:
                                # Extract potential names (capitalized words)
                                import re

                                names = re.findall(
                                    r"[A-ZА-Я][a-zа-яё]+ [A-ZА-Я][a-zа-яё]+", note.content
                                )
                                director_names.update(names[:3])  # Limit per note

                        if not director_names:
                            continue

                        # Check each director for upcoming releases
                        async with TMDBClient() as tmdb:
                            for director_name in list(director_names)[:5]:  # Limit to 5 directors
                                try:
                                    # Search for the director
                                    persons = await tmdb.search_person(director_name)
                                    if not persons:
                                        continue

                                    # Get the most likely director
                                    director = None
                                    for p in persons:
                                        if p.get("known_for_department") == "Directing":
                                            director = p
                                            break
                                    if not director:
                                        director = persons[0]

                                    # Get upcoming movies
                                    upcoming = await tmdb.get_person_upcoming_movies(
                                        director["id"],
                                        role="Director",
                                    )

                                    if upcoming:
                                        # Create pending push for the first upcoming movie
                                        movie = upcoming[0]
                                        await storage.create_pending_push(
                                            user_id=user.id,
                                            push_type="director",
                                            priority=2,  # Medium priority
                                            content={
                                                "director_name": director["name"],
                                                "director_id": director["id"],
                                                "movie_title": movie["title"],
                                                "movie_id": movie["id"],
                                                "release_date": movie["release_date"],
                                            },
                                        )

                                        logger.info(
                                            "director_release_found",
                                            user_id=user.id,
                                            director=director["name"],
                                            movie=movie["title"],
                                        )

                                except Exception as e:
                                    logger.warning(
                                        "director_check_failed",
                                        director=director_name,
                                        error=str(e),
                                    )

                    except Exception as e:
                        logger.warning(
                            "director_releases_user_failed",
                            user_id=user.id,
                            error=str(e),
                        )

            logger.info("director_releases_check_completed")

        except Exception as e:
            logger.exception("director_releases_check_failed", error=str(e))

    async def _generate_hidden_gems(self) -> None:
        """Generate personalized hidden gem recommendations using Claude.

        Uses the user's profile and watch history to suggest non-obvious films.
        """
        logger.info("hidden_gems_generation_started")

        try:
            from src.user.memory import CoreMemoryManager

            async with get_storage() as storage:
                users = await storage.get_all_users(limit=1000)

                for user in users:
                    try:
                        # Check if user wants notifications
                        prefs = await storage.get_preferences(user.id)
                        if prefs and not prefs.notification_enabled:
                            continue

                        # Get user's core memory for profile context
                        memory_manager = CoreMemoryManager(storage)
                        profile_blocks = await memory_manager.get_all_blocks(user.id)

                        # Build profile context
                        profile_context = ""
                        for block in profile_blocks:
                            if block.content:
                                profile_context += f"\n{block.block_name}: {block.content}"

                        if not profile_context.strip():
                            continue  # No profile data to work with

                        # Get recent watch history
                        watched = await storage.get_watched(user.id, limit=20)
                        watched_titles = [w.title for w in watched]

                        # Generate recommendation using Claude
                        prompt = f"""Based on this user's profile, suggest ONE hidden gem film.

Profile:
{profile_context}

Recently watched: {", ".join(watched_titles[:10]) if watched_titles else "No data"}

Requirements:
- NOT a blockbuster (no Marvel, Star Wars, etc.)
- NOT in IMDb Top 250
- Matches user's taste based on profile
- Released before 2020 (so it's discoverable now)
- Must be available for streaming/download

Return ONLY a JSON object with:
{{"title": "Film Title", "year": 1999, "reason": "Why this matches their taste (1 sentence)", "tmdb_id": 12345}}

If you can't find a good match, return {{"error": "No suitable film found"}}"""

                        # Use Anthropic client directly for simple generation
                        import anthropic

                        client = anthropic.AsyncAnthropic(
                            api_key=settings.anthropic_api_key.get_secret_value()
                        )
                        message = await client.messages.create(
                            model="claude-sonnet-4-20250514",
                            max_tokens=500,
                            messages=[{"role": "user", "content": prompt}],
                        )

                        response = ""
                        for block in message.content:
                            if hasattr(block, "text"):
                                response += block.text

                        # Parse the response
                        import json
                        import re

                        # Extract JSON from response
                        json_match = re.search(r"\{[^{}]+\}", response)
                        if not json_match:
                            continue

                        try:
                            recommendation = json.loads(json_match.group())
                        except json.JSONDecodeError:
                            continue

                        if "error" in recommendation:
                            continue

                        # Create pending push
                        await storage.create_pending_push(
                            user_id=user.id,
                            push_type="gem",
                            priority=3,  # Low priority (after followups and director releases)
                            content={
                                "title": recommendation.get("title"),
                                "year": recommendation.get("year"),
                                "reason": recommendation.get("reason"),
                                "tmdb_id": recommendation.get("tmdb_id"),
                            },
                        )

                        logger.info(
                            "hidden_gem_generated",
                            user_id=user.id,
                            title=recommendation.get("title"),
                        )

                    except Exception as e:
                        logger.warning(
                            "hidden_gem_user_failed",
                            user_id=user.id,
                            error=str(e),
                        )

            logger.info("hidden_gems_generation_completed")

        except Exception as e:
            logger.exception("hidden_gems_generation_failed", error=str(e))

    async def _check_industry_news(self) -> None:
        """Check for industry news relevant to user's favorite directors/actors.

        Scans RSS feeds from Deadline, Variety, IndieWire, and Hollywood Reporter
        and creates pending pushes for relevant news items.
        """
        logger.info("industry_news_check_started")

        try:
            from src.services.news import NewsService

            async with get_storage() as storage:
                users = await storage.get_all_users(limit=1000)

                for user in users:
                    try:
                        # Check if user wants notifications
                        prefs = await storage.get_preferences(user.id)
                        if prefs and not prefs.notification_enabled:
                            continue

                        # Collect keywords from user's memory notes
                        keywords = set()

                        # Search for director mentions
                        notes = await storage.search_memory_notes(
                            user_id=user.id,
                            query="director",
                            limit=20,
                        )
                        notes_ru = await storage.search_memory_notes(
                            user_id=user.id,
                            query="режиссёр",
                            limit=20,
                        )
                        notes.extend(notes_ru)

                        # Search for actor mentions
                        actor_notes = await storage.search_memory_notes(
                            user_id=user.id,
                            query="actor",
                            limit=20,
                        )
                        actor_notes_ru = await storage.search_memory_notes(
                            user_id=user.id,
                            query="актёр",
                            limit=20,
                        )
                        notes.extend(actor_notes)
                        notes.extend(actor_notes_ru)

                        # Extract names from notes
                        import re

                        for note in notes:
                            # Extract names (capitalized words, likely names)
                            names = re.findall(
                                r"[A-ZА-Я][a-zа-яё]+ [A-ZА-Я][a-zа-яё]+",
                                note.content,
                            )
                            keywords.update(names[:3])  # Limit per note

                        if not keywords:
                            continue

                        # Fetch relevant news
                        async with NewsService() as news_service:
                            news_items = await news_service.get_relevant_news(
                                keywords=list(keywords)[:20],  # Limit keywords
                                hours=24,
                                max_results=5,
                            )

                            for news_item in news_items:
                                # Create pending push for relevant news
                                await storage.create_pending_push(
                                    user_id=user.id,
                                    push_type="news",
                                    priority=4,  # Lowest priority
                                    content={
                                        "headline": news_item.title[:200],
                                        "description": news_item.description[:300],
                                        "link": news_item.link,
                                        "source": news_item.source,
                                        "keywords": news_item.keywords_matched,
                                    },
                                )

                                logger.info(
                                    "news_push_created",
                                    user_id=user.id,
                                    headline=news_item.title[:50],
                                    source=news_item.source,
                                )

                    except Exception as e:
                        logger.warning(
                            "industry_news_user_failed",
                            user_id=user.id,
                            error=str(e),
                        )

            logger.info("industry_news_check_completed")

        except Exception as e:
            logger.exception("industry_news_check_failed", error=str(e))

    async def _deliver_pending_pushes(self) -> None:
        """Deliver pending push notifications with throttling.

        Only delivers pushes:
        - Between PUSH_MIN_HOUR and PUSH_MAX_HOUR (evening window)
        - Maximum 1 push per user per day
        - Highest priority push first
        """
        logger.info("push_delivery_started")

        # Check if we're in the delivery time window
        current_hour = datetime.now(UTC).hour
        if not (PUSH_MIN_HOUR <= current_hour <= PUSH_MAX_HOUR):
            logger.debug("push_delivery_outside_window", hour=current_hour)
            return

        try:
            async with get_storage() as storage:
                users = await storage.get_all_users(limit=1000)
                delivered_count = 0

                for user in users:
                    try:
                        # Check if user already received a push today
                        last_push = await storage.get_last_push_time(user.id)
                        if last_push:
                            # Ensure last_push has timezone info
                            if last_push.tzinfo is None:
                                last_push = last_push.replace(tzinfo=UTC)

                            now = datetime.now(UTC)
                            if (now - last_push).total_seconds() < 86400:  # 24 hours
                                continue

                        # Get highest priority pending push
                        push = await storage.get_highest_priority_push(user.id)
                        if not push:
                            continue

                        # Format and send the push
                        message, keyboard = self._format_push_message(push)

                        try:
                            await self._bot.send_message(
                                chat_id=user.telegram_id,
                                text=message,
                                parse_mode="Markdown",
                                reply_markup=keyboard,
                            )
                        except Exception:
                            # Fallback: send without Markdown
                            plain_message = message.replace("*", "").replace("_", "")
                            await self._bot.send_message(
                                chat_id=user.telegram_id,
                                text=plain_message,
                                reply_markup=keyboard,
                            )

                        # Mark as sent
                        await storage.mark_push_sent(push.id)
                        delivered_count += 1

                        logger.info(
                            "push_delivered",
                            user_id=user.id,
                            push_type=push.push_type,
                        )

                    except Exception as e:
                        logger.warning(
                            "push_delivery_user_failed",
                            user_id=user.id,
                            error=str(e),
                        )

                # Cleanup old pushes
                deleted = await storage.delete_old_pushes(days=7)
                if deleted > 0:
                    logger.info("old_pushes_deleted", count=deleted)

                logger.info("push_delivery_completed", delivered=delivered_count)

        except Exception as e:
            logger.exception("push_delivery_failed", error=str(e))

    def _format_push_message(
        self,
        push: Any,
    ) -> tuple[str, Any]:
        """Format a push notification message based on type.

        Args:
            push: PendingPush object

        Returns:
            Tuple of (message_text, keyboard)
        """
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        content = push.content
        buttons = []

        esc = self._escape_markdown

        if push.push_type == "followup":
            title = content.get("title", "")
            message = f"🎬 Ты скачал *{esc(title)}* несколько дней назад.\n\nПонравилось?"
            buttons = [
                [
                    InlineKeyboardButton("👍 Да", callback_data=f"followup_yes_{push.id}"),
                    InlineKeyboardButton("👎 Нет", callback_data=f"followup_no_{push.id}"),
                ],
            ]

        elif push.push_type == "director":
            director = content.get("director_name", "")
            movie = content.get("movie_title", "")
            release_date = content.get("release_date", "")
            message = (
                f"🎬 Твой любимый режиссёр *{esc(director)}* снимает новый фильм!\n\n"
                f"📽️ *{esc(movie)}*\n"
                f"📅 Дата выхода: {release_date}"
            )
            movie_id = content.get("movie_id")
            if movie_id:
                buttons = [
                    [
                        InlineKeyboardButton(
                            "📋 Создать монитор",
                            callback_data=f"push_monitor_{movie_id}",
                        ),
                    ],
                    [
                        InlineKeyboardButton(
                            "🚫 Не интересно", callback_data=f"push_dismiss_{push.id}"
                        ),
                    ],
                ]

        elif push.push_type == "gem":
            title = content.get("title", "")
            year = content.get("year", "")
            reason = content.get("reason", "")
            message = f"💎 *Hidden Gem для тебя!*\n\n📽️ *{esc(title)}* ({year})\n\n_{esc(reason)}_"
            buttons = [
                [
                    InlineKeyboardButton("🔍 Найти", callback_data=f"push_search_{title[:30]}"),
                    InlineKeyboardButton(
                        "📋 В watchlist", callback_data=f"push_watchlist_{push.id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🚫 Не интересно", callback_data=f"push_dismiss_{push.id}"
                    ),
                ],
            ]

        elif push.push_type == "news":
            headline = content.get("headline", "")
            source = content.get("source", "")
            message = f"📰 *Новость из мира кино*\n\n{esc(headline)}\n\n_Источник: {esc(source)}_"
            buttons = [
                [
                    InlineKeyboardButton(
                        "🚫 Не интересно", callback_data=f"push_dismiss_{push.id}"
                    ),
                ],
            ]

        else:
            message = f"📢 {content.get('message', 'Notification')}"

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        return message, keyboard

    async def _enrich_watched_tmdb(self) -> None:
        """Enrich watched items with TMDB data (director, tmdb_id).

        Runs periodically to gradually populate TMDB data for films
        imported from Letterboxd that don't have TMDB IDs.
        """
        logger.info("tmdb_enrichment_started")

        try:
            from src.media.tmdb import MediaType, TMDBClient

            async with get_storage() as storage:
                # Get watched items without TMDB data
                items = await storage.get_watched_without_tmdb_data(
                    limit=TMDB_ENRICHMENT_BATCH_SIZE
                )

                if not items:
                    logger.debug("tmdb_enrichment_no_items")
                    return

                enriched = 0
                failed = 0

                async with TMDBClient() as tmdb:
                    for item in items:
                        try:
                            # Search TMDB by title and year
                            if item.media_type == "movie":
                                results = await tmdb.search_movie(
                                    query=item.title,
                                    year=item.year,
                                )
                            else:
                                results = await tmdb.search_tv(
                                    query=item.title,
                                    year=item.year,
                                )

                            if not results:
                                failed += 1
                                await storage.mark_tmdb_enrichment_failed(item.id)
                                continue

                            # Get the best match
                            best_match = results[0]
                            tmdb_id = best_match.id

                            if not tmdb_id:
                                failed += 1
                                await storage.mark_tmdb_enrichment_failed(item.id)
                                continue

                            # Get director info for movies
                            director = None
                            if item.media_type == "movie":
                                credits = await tmdb.get_credits(tmdb_id, MediaType.MOVIE)
                                if credits:
                                    directors = credits.get_directors()
                                    if directors:
                                        director = directors[0].name

                            # Update the watched item
                            await storage.update_watched_tmdb_data(
                                watched_id=item.id,
                                tmdb_id=tmdb_id,
                                director=director,
                            )

                            enriched += 1

                            logger.debug(
                                "tmdb_enriched_item",
                                title=item.title,
                                tmdb_id=tmdb_id,
                                director=director,
                            )

                        except Exception as e:
                            logger.warning(
                                "tmdb_enrichment_item_failed",
                                title=item.title,
                                error=str(e),
                            )
                            failed += 1

                logger.info(
                    "tmdb_enrichment_completed",
                    enriched=enriched,
                    failed=failed,
                    total=len(items),
                )

        except Exception as e:
            logger.exception("tmdb_enrichment_failed", error=str(e))

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
