"""Release monitoring module.

This module provides:
- Background worker for checking release availability
- APScheduler integration for periodic checks
- Telegram notifications when releases are found
- Optional auto-download to seedbox

Usage:
    from src.monitoring import MonitoringScheduler

    scheduler = MonitoringScheduler(bot)
    scheduler.start()
"""

from src.monitoring.checker import ReleaseChecker
from src.monitoring.scheduler import MonitoringScheduler

__all__ = [
    "MonitoringScheduler",
    "ReleaseChecker",
]
