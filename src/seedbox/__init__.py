"""Seedbox integration module.

Provides clients for sending torrents to various seedbox torrent clients.
Supports Transmission, qBittorrent, and Deluge APIs.
"""

from src.seedbox.client import (
    SeedboxAuthError,
    SeedboxClient,
    SeedboxConnectionError,
    SeedboxError,
    SeedboxTorrentError,
    SeedboxType,
    TorrentInfo,
    TorrentStatus,
    create_seedbox_client,
    get_torrent_status,
    is_seedbox_configured,
    send_magnet_to_seedbox,
    send_magnet_to_user_seedbox,
)

__all__ = [
    "SeedboxClient",
    "SeedboxError",
    "SeedboxAuthError",
    "SeedboxConnectionError",
    "SeedboxTorrentError",
    "TorrentStatus",
    "TorrentInfo",
    "SeedboxType",
    "create_seedbox_client",
    "send_magnet_to_seedbox",
    "send_magnet_to_user_seedbox",
    "get_torrent_status",
    "is_seedbox_configured",
]
