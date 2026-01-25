"""User profile storage module.

This module provides dual-backend storage (Postgres/SQLite) for user profiles,
credentials, preferences, watch history, watchlist, monitors, and crew stats.
"""

from src.user.profile import (
    ProfileManager,
    extract_blocklist_items,
    get_profile_summary,
)
from src.user.storage import (
    BaseStorage,
    BlocklistItem,
    Credential,
    CredentialType,
    CrewStat,
    Download,
    Monitor,
    PendingPush,
    PostgresStorage,
    Preference,
    SQLiteStorage,
    User,
    UserProfile,
    UserStorage,
    WatchedItem,
    WatchlistItem,
    get_storage,
    get_storage_backend,
    get_user_storage,
)

__all__ = [
    # Storage backends
    "BaseStorage",
    "PostgresStorage",
    "SQLiteStorage",
    "UserStorage",  # Alias for SQLiteStorage (backward compat)
    # Factory functions
    "get_storage",
    "get_storage_backend",
    "get_user_storage",
    # Profile management
    "ProfileManager",
    "get_profile_summary",
    "extract_blocklist_items",
    # Models
    "BlocklistItem",
    "Credential",
    "CredentialType",
    "CrewStat",
    "Download",
    "Monitor",
    "PendingPush",
    "Preference",
    "User",
    "UserProfile",
    "WatchedItem",
    "WatchlistItem",
]
