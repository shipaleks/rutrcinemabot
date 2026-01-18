"""User profile storage module.

This module provides SQLite-based storage for user profiles,
credentials, preferences, and watch history.
"""

from src.user.storage import (
    Credential,
    CredentialType,
    MigrationManager,
    Preference,
    User,
    UserStorage,
    WatchedItem,
    get_user_storage,
)

__all__ = [
    "Credential",
    "CredentialType",
    "MigrationManager",
    "Preference",
    "User",
    "UserStorage",
    "WatchedItem",
    "get_user_storage",
]
