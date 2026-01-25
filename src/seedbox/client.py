"""Seedbox client for sending torrents to remote torrent clients.

Supports multiple torrent client APIs:
- Transmission RPC API
- qBittorrent Web API
- Deluge JSON-RPC API

When seedbox is not configured, gracefully returns magnet links directly.
"""

import base64
import json
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

import httpx
import structlog

from src.config import settings

logger = structlog.get_logger()


# ============================================================================
# Exceptions
# ============================================================================


class SeedboxError(Exception):
    """Base exception for seedbox operations."""

    pass


class SeedboxAuthError(SeedboxError):
    """Authentication failed with seedbox."""

    pass


class SeedboxConnectionError(SeedboxError):
    """Failed to connect to seedbox."""

    pass


class SeedboxTorrentError(SeedboxError):
    """Error adding or managing torrent."""

    pass


# ============================================================================
# Enums and Models
# ============================================================================


class SeedboxType(str, Enum):
    """Supported seedbox torrent client types."""

    TRANSMISSION = "transmission"
    QBITTORRENT = "qbittorrent"
    DELUGE = "deluge"


class TorrentStatus(str, Enum):
    """Status of a torrent on the seedbox."""

    DOWNLOADING = "downloading"
    SEEDING = "seeding"
    PAUSED = "paused"
    CHECKING = "checking"
    QUEUED = "queued"
    ERROR = "error"
    UNKNOWN = "unknown"


class TorrentInfo:
    """Information about a torrent on the seedbox.

    Attributes:
        hash: Torrent info hash.
        name: Torrent name.
        status: Current status.
        progress: Download progress (0.0 - 1.0).
        size: Total size in bytes.
        downloaded: Downloaded bytes.
        upload_speed: Upload speed in bytes/sec.
        download_speed: Download speed in bytes/sec.
        seeds: Number of seeds.
        peers: Number of peers.
    """

    def __init__(
        self,
        hash: str,
        name: str,
        status: TorrentStatus,
        progress: float = 0.0,
        size: int = 0,
        downloaded: int = 0,
        upload_speed: int = 0,
        download_speed: int = 0,
        seeds: int = 0,
        peers: int = 0,
    ):
        self.hash = hash
        self.name = name
        self.status = status
        self.progress = progress
        self.size = size
        self.downloaded = downloaded
        self.upload_speed = upload_speed
        self.download_speed = download_speed
        self.seeds = seeds
        self.peers = peers

    def __repr__(self) -> str:
        return (
            f"TorrentInfo(hash={self.hash!r}, name={self.name!r}, "
            f"status={self.status}, progress={self.progress:.1%})"
        )

    @property
    def progress_percent(self) -> float:
        """Progress as percentage (0-100)."""
        return self.progress * 100

    @property
    def is_complete(self) -> bool:
        """Check if torrent is complete."""
        return self.progress >= 1.0 or self.status == TorrentStatus.SEEDING


# ============================================================================
# Base Client
# ============================================================================


class SeedboxClient(ABC):
    """Abstract base class for seedbox clients.

    Concrete implementations must implement:
    - authenticate()
    - add_magnet()
    - get_torrent_status()
    - list_torrents()
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        timeout: float = 30.0,
    ):
        """Initialize seedbox client.

        Args:
            host: Seedbox host URL (e.g., https://seedbox.example.com:9091)
            username: Authentication username
            password: Authentication password
            timeout: HTTP request timeout in seconds
        """
        self.host = host.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "SeedboxClient":
        """Enter async context and authenticate."""
        self._client = httpx.AsyncClient(timeout=self.timeout)
        await self.authenticate()
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: Any,
    ) -> None:
        """Exit async context and close client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Get HTTP client, raising if not in context."""
        if self._client is None:
            raise RuntimeError("SeedboxClient must be used as async context manager")
        return self._client

    @abstractmethod
    async def authenticate(self) -> None:
        """Authenticate with the seedbox.

        Raises:
            SeedboxAuthError: If authentication fails.
            SeedboxConnectionError: If connection fails.
        """
        pass

    @abstractmethod
    async def add_magnet(self, magnet_link: str) -> str:
        """Add a magnet link to the seedbox.

        Args:
            magnet_link: Magnet URI to add.

        Returns:
            Torrent info hash.

        Raises:
            SeedboxTorrentError: If adding torrent fails.
        """
        pass

    @abstractmethod
    async def get_torrent_status(self, torrent_hash: str) -> TorrentInfo | None:
        """Get status of a specific torrent.

        Args:
            torrent_hash: Torrent info hash.

        Returns:
            TorrentInfo if found, None otherwise.
        """
        pass

    @abstractmethod
    async def list_torrents(self) -> list[TorrentInfo]:
        """List all torrents on the seedbox.

        Returns:
            List of TorrentInfo objects.
        """
        pass


# ============================================================================
# Transmission Client
# ============================================================================


class TransmissionClient(SeedboxClient):
    """Client for Transmission RPC API.

    Transmission uses a JSON-RPC API with CSRF token protection.
    Default RPC path is /transmission/rpc
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        timeout: float = 30.0,
        rpc_path: str = "/transmission/rpc",
    ):
        super().__init__(host, username, password, timeout)
        self.rpc_path = rpc_path
        self._session_id: str | None = None

    async def authenticate(self) -> None:
        """Authenticate with Transmission and get session ID."""
        try:
            # Transmission requires CSRF token from initial request
            response = await self.client.post(
                f"{self.host}{self.rpc_path}",
                auth=(self.username, self.password),
            )

            # 409 means we need the session ID from header
            if response.status_code == 409:
                self._session_id = response.headers.get("X-Transmission-Session-Id")
                if not self._session_id:
                    raise SeedboxAuthError("Failed to get Transmission session ID")
                logger.debug("transmission_authenticated", host=self.host)
            elif response.status_code == 401:
                raise SeedboxAuthError("Invalid Transmission credentials")
            else:
                # Some versions may return 200 directly
                self._session_id = response.headers.get("X-Transmission-Session-Id", "")
                logger.debug("transmission_authenticated", host=self.host)

        except httpx.ConnectError as e:
            raise SeedboxConnectionError(f"Failed to connect to Transmission: {e}") from e
        except httpx.TimeoutException as e:
            raise SeedboxConnectionError("Transmission connection timed out") from e

    async def _rpc_call(self, method: str, arguments: dict | None = None) -> dict:
        """Make an RPC call to Transmission.

        Args:
            method: RPC method name.
            arguments: Method arguments.

        Returns:
            Response result dict.

        Raises:
            SeedboxTorrentError: If RPC call fails.
        """
        headers = {}
        if self._session_id:
            headers["X-Transmission-Session-Id"] = self._session_id

        payload: dict[str, Any] = {"method": method}
        if arguments:
            payload["arguments"] = arguments

        try:
            response = await self.client.post(
                f"{self.host}{self.rpc_path}",
                json=payload,
                headers=headers,
                auth=(self.username, self.password),
            )

            # Handle CSRF token expiration
            if response.status_code == 409:
                self._session_id = response.headers.get("X-Transmission-Session-Id")
                headers["X-Transmission-Session-Id"] = self._session_id or ""
                response = await self.client.post(
                    f"{self.host}{self.rpc_path}",
                    json=payload,
                    headers=headers,
                    auth=(self.username, self.password),
                )

            if response.status_code != 200:
                raise SeedboxTorrentError(f"Transmission RPC failed: {response.status_code}")

            data = response.json()
            if data.get("result") != "success":
                raise SeedboxTorrentError(f"Transmission RPC error: {data.get('result')}")

            return data.get("arguments", {})

        except httpx.TimeoutException as e:
            raise SeedboxTorrentError("Transmission RPC timed out") from e

    async def add_magnet(self, magnet_link: str) -> str:
        """Add magnet link to Transmission."""
        result = await self._rpc_call(
            "torrent-add",
            {"filename": magnet_link},
        )

        # Check for torrent-added or torrent-duplicate
        torrent = result.get("torrent-added") or result.get("torrent-duplicate")
        if not torrent:
            raise SeedboxTorrentError("Failed to add torrent to Transmission")

        torrent_hash = torrent.get("hashString", "")
        logger.info(
            "transmission_torrent_added",
            hash=torrent_hash,
            name=torrent.get("name"),
        )
        return torrent_hash

    def _map_status(self, status_code: int) -> TorrentStatus:
        """Map Transmission status code to TorrentStatus."""
        status_map = {
            0: TorrentStatus.PAUSED,  # stopped
            1: TorrentStatus.QUEUED,  # check pending
            2: TorrentStatus.CHECKING,  # checking
            3: TorrentStatus.QUEUED,  # download pending
            4: TorrentStatus.DOWNLOADING,  # downloading
            5: TorrentStatus.QUEUED,  # seed pending
            6: TorrentStatus.SEEDING,  # seeding
        }
        return status_map.get(status_code, TorrentStatus.UNKNOWN)

    async def get_torrent_status(self, torrent_hash: str) -> TorrentInfo | None:
        """Get status of a specific torrent from Transmission."""
        result = await self._rpc_call(
            "torrent-get",
            {
                "ids": [torrent_hash],
                "fields": [
                    "hashString",
                    "name",
                    "status",
                    "percentDone",
                    "totalSize",
                    "downloadedEver",
                    "rateUpload",
                    "rateDownload",
                    "seeders",
                    "peersConnected",
                    "error",
                    "errorString",
                ],
            },
        )

        torrents = result.get("torrents", [])
        if not torrents:
            return None

        t = torrents[0]
        status = self._map_status(t.get("status", -1))

        # Check for errors
        if t.get("error", 0) != 0:
            status = TorrentStatus.ERROR

        return TorrentInfo(
            hash=t.get("hashString", ""),
            name=t.get("name", ""),
            status=status,
            progress=t.get("percentDone", 0.0),
            size=t.get("totalSize", 0),
            downloaded=t.get("downloadedEver", 0),
            upload_speed=t.get("rateUpload", 0),
            download_speed=t.get("rateDownload", 0),
            seeds=t.get("seeders", 0),
            peers=t.get("peersConnected", 0),
        )

    async def list_torrents(self) -> list[TorrentInfo]:
        """List all torrents from Transmission."""
        result = await self._rpc_call(
            "torrent-get",
            {
                "fields": [
                    "hashString",
                    "name",
                    "status",
                    "percentDone",
                    "totalSize",
                    "downloadedEver",
                    "rateUpload",
                    "rateDownload",
                    "seeders",
                    "peersConnected",
                    "error",
                ],
            },
        )

        torrents = []
        for t in result.get("torrents", []):
            status = self._map_status(t.get("status", -1))
            if t.get("error", 0) != 0:
                status = TorrentStatus.ERROR

            torrents.append(
                TorrentInfo(
                    hash=t.get("hashString", ""),
                    name=t.get("name", ""),
                    status=status,
                    progress=t.get("percentDone", 0.0),
                    size=t.get("totalSize", 0),
                    downloaded=t.get("downloadedEver", 0),
                    upload_speed=t.get("rateUpload", 0),
                    download_speed=t.get("rateDownload", 0),
                    seeds=t.get("seeders", 0),
                    peers=t.get("peersConnected", 0),
                )
            )

        return torrents


# ============================================================================
# qBittorrent Client
# ============================================================================


class QBittorrentClient(SeedboxClient):
    """Client for qBittorrent Web API.

    qBittorrent uses cookie-based session authentication.
    Default Web UI path is /api/v2
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        timeout: float = 30.0,
        api_path: str = "/api/v2",
    ):
        super().__init__(host, username, password, timeout)
        self.api_path = api_path
        self._cookies: dict[str, str] = {}

    async def authenticate(self) -> None:
        """Authenticate with qBittorrent and get session cookie."""
        try:
            response = await self.client.post(
                f"{self.host}{self.api_path}/auth/login",
                data={
                    "username": self.username,
                    "password": self.password,
                },
            )

            if response.status_code == 200:
                if response.text == "Ok.":
                    self._cookies = dict(response.cookies)
                    logger.debug("qbittorrent_authenticated", host=self.host)
                else:
                    raise SeedboxAuthError("Invalid qBittorrent credentials")
            elif response.status_code == 403:
                raise SeedboxAuthError(
                    "qBittorrent: Too many failed login attempts. Please try again later."
                )
            else:
                raise SeedboxAuthError(f"qBittorrent authentication failed: {response.status_code}")

        except httpx.ConnectError as e:
            raise SeedboxConnectionError(f"Failed to connect to qBittorrent: {e}") from e
        except httpx.TimeoutException as e:
            raise SeedboxConnectionError("qBittorrent connection timed out") from e

    async def _api_call(
        self,
        endpoint: str,
        method: str = "GET",
        data: dict | None = None,
    ) -> httpx.Response:
        """Make an API call to qBittorrent.

        Args:
            endpoint: API endpoint (e.g., /torrents/add)
            method: HTTP method
            data: Form data for POST requests

        Returns:
            HTTP response

        Raises:
            SeedboxTorrentError: If API call fails.
        """
        try:
            url = f"{self.host}{self.api_path}{endpoint}"

            if method == "GET":
                response = await self.client.get(url, cookies=self._cookies)
            else:
                response = await self.client.post(url, data=data, cookies=self._cookies)

            if response.status_code == 403:
                # Session expired, re-authenticate
                await self.authenticate()
                if method == "GET":
                    response = await self.client.get(url, cookies=self._cookies)
                else:
                    response = await self.client.post(url, data=data, cookies=self._cookies)

            return response

        except httpx.TimeoutException as e:
            raise SeedboxTorrentError("qBittorrent API timed out") from e

    async def add_magnet(self, magnet_link: str) -> str:
        """Add magnet link to qBittorrent."""
        response = await self._api_call(
            "/torrents/add",
            method="POST",
            data={"urls": magnet_link},
        )

        if response.status_code != 200:
            raise SeedboxTorrentError(f"Failed to add torrent to qBittorrent: {response.text}")

        # Extract hash from magnet link
        torrent_hash = self._extract_hash_from_magnet(magnet_link)
        logger.info("qbittorrent_torrent_added", hash=torrent_hash)
        return torrent_hash

    def _extract_hash_from_magnet(self, magnet_link: str) -> str:
        """Extract info hash from magnet link."""
        # Magnet format: magnet:?xt=urn:btih:HASH&...
        if "btih:" in magnet_link:
            hash_part = magnet_link.split("btih:")[1].split("&")[0]
            # Handle base32 encoded hashes (40 chars hex, 32 chars base32)
            if len(hash_part) == 32:
                # Base32 to hex
                decoded = base64.b32decode(hash_part.upper())
                return decoded.hex()
            return hash_part.lower()
        return ""

    def _map_status(self, state: str) -> TorrentStatus:
        """Map qBittorrent state to TorrentStatus."""
        state_map = {
            "error": TorrentStatus.ERROR,
            "missingFiles": TorrentStatus.ERROR,
            "uploading": TorrentStatus.SEEDING,
            "pausedUP": TorrentStatus.PAUSED,
            "queuedUP": TorrentStatus.QUEUED,
            "stalledUP": TorrentStatus.SEEDING,
            "checkingUP": TorrentStatus.CHECKING,
            "forcedUP": TorrentStatus.SEEDING,
            "allocating": TorrentStatus.CHECKING,
            "downloading": TorrentStatus.DOWNLOADING,
            "metaDL": TorrentStatus.DOWNLOADING,
            "pausedDL": TorrentStatus.PAUSED,
            "queuedDL": TorrentStatus.QUEUED,
            "stalledDL": TorrentStatus.DOWNLOADING,
            "checkingDL": TorrentStatus.CHECKING,
            "forcedDL": TorrentStatus.DOWNLOADING,
            "checkingResumeData": TorrentStatus.CHECKING,
            "moving": TorrentStatus.CHECKING,
        }
        return state_map.get(state, TorrentStatus.UNKNOWN)

    async def get_torrent_status(self, torrent_hash: str) -> TorrentInfo | None:
        """Get status of a specific torrent from qBittorrent."""
        response = await self._api_call(
            f"/torrents/info?hashes={torrent_hash}",
        )

        if response.status_code != 200:
            return None

        try:
            torrents = response.json()
        except json.JSONDecodeError:
            return None

        if not torrents:
            return None

        t = torrents[0]
        return TorrentInfo(
            hash=t.get("hash", ""),
            name=t.get("name", ""),
            status=self._map_status(t.get("state", "")),
            progress=t.get("progress", 0.0),
            size=t.get("total_size", 0),
            downloaded=t.get("downloaded", 0),
            upload_speed=t.get("upspeed", 0),
            download_speed=t.get("dlspeed", 0),
            seeds=t.get("num_seeds", 0),
            peers=t.get("num_leechs", 0),
        )

    async def list_torrents(self) -> list[TorrentInfo]:
        """List all torrents from qBittorrent."""
        response = await self._api_call("/torrents/info")

        if response.status_code != 200:
            return []

        try:
            torrents_data = response.json()
        except json.JSONDecodeError:
            return []

        torrents = []
        for t in torrents_data:
            torrents.append(
                TorrentInfo(
                    hash=t.get("hash", ""),
                    name=t.get("name", ""),
                    status=self._map_status(t.get("state", "")),
                    progress=t.get("progress", 0.0),
                    size=t.get("total_size", 0),
                    downloaded=t.get("downloaded", 0),
                    upload_speed=t.get("upspeed", 0),
                    download_speed=t.get("dlspeed", 0),
                    seeds=t.get("num_seeds", 0),
                    peers=t.get("num_leechs", 0),
                )
            )

        return torrents


# ============================================================================
# Deluge Client
# ============================================================================


class DelugeClient(SeedboxClient):
    """Client for Deluge JSON-RPC API.

    Deluge uses a JSON-RPC API with session cookie authentication.
    Default Web UI path is /json
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        timeout: float = 30.0,
        api_path: str = "/json",
    ):
        super().__init__(host, username, password, timeout)
        self.api_path = api_path
        self._cookies: dict[str, str] = {}
        self._request_id = 0

    async def authenticate(self) -> None:
        """Authenticate with Deluge and get session cookie."""
        try:
            # Deluge web UI authentication
            response = await self._rpc_call_raw("auth.login", [self.password])

            if response.get("result") is True:
                logger.debug("deluge_authenticated", host=self.host)
            else:
                raise SeedboxAuthError("Invalid Deluge password")

            # Check if connected to daemon
            response = await self._rpc_call_raw("web.connected", [])
            if not response.get("result"):
                # Try to connect to first available daemon
                hosts = await self._rpc_call_raw("web.get_hosts", [])
                if hosts.get("result"):
                    host_id = hosts["result"][0][0]
                    await self._rpc_call_raw("web.connect", [host_id])

        except httpx.ConnectError as e:
            raise SeedboxConnectionError(f"Failed to connect to Deluge: {e}") from e
        except httpx.TimeoutException as e:
            raise SeedboxConnectionError("Deluge connection timed out") from e

    async def _rpc_call_raw(self, method: str, params: list) -> dict:
        """Make a raw JSON-RPC call to Deluge."""
        self._request_id += 1

        payload = {
            "method": method,
            "params": params,
            "id": self._request_id,
        }

        response = await self.client.post(
            f"{self.host}{self.api_path}",
            json=payload,
            cookies=self._cookies,
        )

        # Store session cookies
        self._cookies.update(dict(response.cookies))

        if response.status_code != 200:
            raise SeedboxTorrentError(f"Deluge RPC failed: {response.status_code}")

        return response.json()

    async def _rpc_call(self, method: str, params: list | None = None) -> Any:
        """Make an RPC call to Deluge with error handling.

        Args:
            method: RPC method name.
            params: Method parameters.

        Returns:
            Response result.

        Raises:
            SeedboxTorrentError: If RPC call fails.
        """
        if params is None:
            params = []

        response = await self._rpc_call_raw(method, params)

        if response.get("error"):
            error = response["error"]
            raise SeedboxTorrentError(f"Deluge RPC error: {error.get('message', 'Unknown error')}")

        return response.get("result")

    async def add_magnet(self, magnet_link: str) -> str:
        """Add magnet link to Deluge."""
        # Add magnet with default options
        result = await self._rpc_call(
            "core.add_torrent_magnet",
            [magnet_link, {}],
        )

        if not result:
            raise SeedboxTorrentError("Failed to add torrent to Deluge")

        torrent_hash = result
        logger.info("deluge_torrent_added", hash=torrent_hash)
        return torrent_hash

    def _map_status(self, state: str) -> TorrentStatus:
        """Map Deluge state to TorrentStatus."""
        state_map = {
            "Downloading": TorrentStatus.DOWNLOADING,
            "Seeding": TorrentStatus.SEEDING,
            "Paused": TorrentStatus.PAUSED,
            "Checking": TorrentStatus.CHECKING,
            "Queued": TorrentStatus.QUEUED,
            "Error": TorrentStatus.ERROR,
            "Active": TorrentStatus.DOWNLOADING,
        }
        return state_map.get(state, TorrentStatus.UNKNOWN)

    async def get_torrent_status(self, torrent_hash: str) -> TorrentInfo | None:
        """Get status of a specific torrent from Deluge."""
        try:
            result = await self._rpc_call(
                "core.get_torrent_status",
                [
                    torrent_hash,
                    [
                        "hash",
                        "name",
                        "state",
                        "progress",
                        "total_size",
                        "total_done",
                        "upload_payload_rate",
                        "download_payload_rate",
                        "num_seeds",
                        "num_peers",
                    ],
                ],
            )
        except SeedboxTorrentError:
            return None

        if not result:
            return None

        return TorrentInfo(
            hash=result.get("hash", torrent_hash),
            name=result.get("name", ""),
            status=self._map_status(result.get("state", "")),
            progress=result.get("progress", 0.0) / 100.0,  # Deluge uses 0-100
            size=result.get("total_size", 0),
            downloaded=result.get("total_done", 0),
            upload_speed=result.get("upload_payload_rate", 0),
            download_speed=result.get("download_payload_rate", 0),
            seeds=result.get("num_seeds", 0),
            peers=result.get("num_peers", 0),
        )

    async def list_torrents(self) -> list[TorrentInfo]:
        """List all torrents from Deluge."""
        result = await self._rpc_call(
            "core.get_torrents_status",
            [
                {},
                [
                    "hash",
                    "name",
                    "state",
                    "progress",
                    "total_size",
                    "total_done",
                    "upload_payload_rate",
                    "download_payload_rate",
                    "num_seeds",
                    "num_peers",
                ],
            ],
        )

        if not result:
            return []

        torrents = []
        for torrent_hash, t in result.items():
            torrents.append(
                TorrentInfo(
                    hash=t.get("hash", torrent_hash),
                    name=t.get("name", ""),
                    status=self._map_status(t.get("state", "")),
                    progress=t.get("progress", 0.0) / 100.0,
                    size=t.get("total_size", 0),
                    downloaded=t.get("total_done", 0),
                    upload_speed=t.get("upload_payload_rate", 0),
                    download_speed=t.get("download_payload_rate", 0),
                    seeds=t.get("num_seeds", 0),
                    peers=t.get("num_peers", 0),
                )
            )

        return torrents

    async def remove_torrent(self, torrent_hash: str, remove_data: bool = True) -> bool:
        """Remove a torrent from Deluge.

        Args:
            torrent_hash: Torrent info hash.
            remove_data: Whether to also remove downloaded data.

        Returns:
            True if removal was successful, False otherwise.
        """
        try:
            result = await self._rpc_call(
                "core.remove_torrent",
                [torrent_hash, remove_data],
            )
            logger.info(
                "deluge_torrent_removed",
                hash=torrent_hash,
                remove_data=remove_data,
                result=result,
            )
            return result is True
        except SeedboxTorrentError as e:
            logger.error("deluge_remove_failed", hash=torrent_hash, error=str(e))
            return False


# ============================================================================
# Factory and Convenience Functions
# ============================================================================


def detect_seedbox_type(host: str) -> SeedboxType:
    """Detect seedbox type from host URL.

    Heuristics:
    - URLs containing 'transmission' -> Transmission
    - URLs containing 'qbittorrent' or port 8080 -> qBittorrent
    - URLs containing 'deluge' or port 8112 -> Deluge
    - Default: Transmission

    Args:
        host: Seedbox host URL.

    Returns:
        Detected SeedboxType.
    """
    host_lower = host.lower()

    if "transmission" in host_lower or ":9091" in host_lower:
        return SeedboxType.TRANSMISSION
    if "qbittorrent" in host_lower or ":8080" in host_lower:
        return SeedboxType.QBITTORRENT
    if "deluge" in host_lower or ":8112" in host_lower:
        return SeedboxType.DELUGE

    # Default to Transmission (most common)
    return SeedboxType.TRANSMISSION


def create_seedbox_client(
    host: str,
    username: str,
    password: str,
    seedbox_type: SeedboxType | None = None,
    timeout: float = 30.0,
) -> SeedboxClient:
    """Create a seedbox client of the appropriate type.

    Args:
        host: Seedbox host URL.
        username: Authentication username.
        password: Authentication password.
        seedbox_type: Client type (auto-detected if None).
        timeout: HTTP request timeout.

    Returns:
        Appropriate SeedboxClient subclass.
    """
    if seedbox_type is None:
        seedbox_type = detect_seedbox_type(host)

    client_classes = {
        SeedboxType.TRANSMISSION: TransmissionClient,
        SeedboxType.QBITTORRENT: QBittorrentClient,
        SeedboxType.DELUGE: DelugeClient,
    }

    client_class = client_classes[seedbox_type]
    return client_class(host, username, password, timeout)


def is_seedbox_configured() -> bool:
    """Check if seedbox is configured in settings.

    Returns:
        True if seedbox credentials are available.
    """
    return settings.has_seedbox


def extract_magnet_hash(magnet_link: str) -> str:
    """Extract info hash from magnet link.

    Args:
        magnet_link: Magnet URI.

    Returns:
        Info hash (lowercase hex) or empty string.
    """
    if "btih:" in magnet_link:
        hash_part = magnet_link.split("btih:")[1].split("&")[0]
        # Handle base32 encoded hashes
        if len(hash_part) == 32 and not all(c in "0123456789abcdefABCDEF" for c in hash_part):
            try:
                decoded = base64.b32decode(hash_part.upper())
                return decoded.hex()
            except Exception:
                pass
        return hash_part.lower()
    return ""


async def send_magnet_to_seedbox(magnet_link: str) -> dict[str, Any]:
    """Send a magnet link to the configured seedbox.

    If seedbox is not configured, returns the magnet link directly
    for the user to use manually.

    Args:
        magnet_link: Magnet URI to send.

    Returns:
        Dict with either:
        - {"status": "sent", "hash": "...", "seedbox": "..."} on success
        - {"status": "magnet", "magnet": "..."} if seedbox not configured
        - {"status": "error", "error": "..."} on failure
    """
    if not is_seedbox_configured():
        logger.info("seedbox_not_configured", action="return_magnet")
        return {
            "status": "magnet",
            "magnet": magnet_link,
            "hash": extract_magnet_hash(magnet_link),
            "message": "Seedbox не настроен. Используйте magnet-ссылку:",
        }

    host = settings.seedbox_host
    username = settings.seedbox_user
    password = settings.seedbox_password

    if not host or not username or not password:
        return {
            "status": "magnet",
            "magnet": magnet_link,
            "hash": extract_magnet_hash(magnet_link),
            "message": "Seedbox не настроен. Используйте magnet-ссылку:",
        }

    password_value = (
        password.get_secret_value() if hasattr(password, "get_secret_value") else str(password)
    )

    try:
        client = create_seedbox_client(host, username, password_value)
        async with client:
            torrent_hash = await client.add_magnet(magnet_link)

            logger.info(
                "seedbox_magnet_sent",
                hash=torrent_hash,
                seedbox_type=type(client).__name__,
            )

            return {
                "status": "sent",
                "hash": torrent_hash,
                "seedbox": host,
                "message": "Торрент добавлен на seedbox",
            }

    except SeedboxAuthError as e:
        logger.error("seedbox_auth_failed", error=str(e))
        return {
            "status": "error",
            "error": f"Ошибка авторизации: {e}",
            "magnet": magnet_link,
        }
    except SeedboxConnectionError as e:
        logger.error("seedbox_connection_failed", error=str(e))
        return {
            "status": "error",
            "error": f"Ошибка подключения: {e}",
            "magnet": magnet_link,
        }
    except SeedboxTorrentError as e:
        logger.error("seedbox_torrent_failed", error=str(e))
        return {
            "status": "error",
            "error": f"Ошибка добавления торрента: {e}",
            "magnet": magnet_link,
        }
    except Exception as e:
        logger.exception("seedbox_unexpected_error")
        return {
            "status": "error",
            "error": f"Неожиданная ошибка: {e}",
            "magnet": magnet_link,
        }


async def get_torrent_status(torrent_hash: str) -> dict[str, Any]:
    """Get status of a torrent on the seedbox.

    Args:
        torrent_hash: Torrent info hash.

    Returns:
        Dict with torrent info or error.
    """
    if not is_seedbox_configured():
        return {
            "status": "error",
            "error": "Seedbox не настроен",
        }

    host = settings.seedbox_host
    username = settings.seedbox_user
    password = settings.seedbox_password

    if not host or not username or not password:
        return {
            "status": "error",
            "error": "Seedbox не настроен",
        }

    password_value = (
        password.get_secret_value() if hasattr(password, "get_secret_value") else str(password)
    )

    try:
        client = create_seedbox_client(host, username, password_value)
        async with client:
            info = await client.get_torrent_status(torrent_hash)

            if info is None:
                return {
                    "status": "not_found",
                    "hash": torrent_hash,
                    "message": "Торрент не найден",
                }

            return {
                "status": "found",
                "hash": info.hash,
                "name": info.name,
                "torrent_status": info.status.value,
                "progress": info.progress_percent,
                "size": info.size,
                "downloaded": info.downloaded,
                "download_speed": info.download_speed,
                "upload_speed": info.upload_speed,
                "seeds": info.seeds,
                "peers": info.peers,
                "is_complete": info.is_complete,
            }

    except SeedboxError as e:
        logger.error("seedbox_status_failed", error=str(e), hash=torrent_hash)
        return {
            "status": "error",
            "error": str(e),
            "hash": torrent_hash,
        }
    except Exception as e:
        logger.exception("seedbox_status_unexpected_error")
        return {
            "status": "error",
            "error": f"Неожиданная ошибка: {e}",
            "hash": torrent_hash,
        }


async def send_magnet_to_user_seedbox(
    magnet_link: str,
    telegram_id: int | None = None,
) -> dict[str, Any]:
    """Send a magnet link to user's seedbox, falling back to global.

    Tries user's personal seedbox credentials first. If not configured
    or fails, falls back to global seedbox settings.

    Args:
        magnet_link: Magnet URI to send.
        telegram_id: Telegram user ID for per-user credentials lookup.

    Returns:
        Dict with either:
        - {"status": "sent", "hash": "...", "seedbox": "...", "user_seedbox": True/False}
        - {"status": "magnet", "magnet": "..."} if no seedbox configured
        - {"status": "error", "error": "..."} on failure
    """
    # Try user's personal seedbox first
    if telegram_id:
        from src.bot.seedbox_auth import get_user_seedbox_credentials

        host, username, password = await get_user_seedbox_credentials(telegram_id)

        if host and username and password:
            logger.info(
                "trying_user_seedbox",
                telegram_id=telegram_id,
                host=host[:30] + "..." if len(host) > 30 else host,
            )

            try:
                client = create_seedbox_client(host, username, password)
                async with client:
                    torrent_hash = await client.add_magnet(magnet_link)

                    logger.info(
                        "user_seedbox_magnet_sent",
                        telegram_id=telegram_id,
                        hash=torrent_hash,
                        seedbox_type=type(client).__name__,
                    )

                    return {
                        "status": "sent",
                        "hash": torrent_hash,
                        "seedbox": host,
                        "user_seedbox": True,
                        "message": "Торрент добавлен на ваш seedbox",
                    }

            except SeedboxAuthError as e:
                logger.warning(
                    "user_seedbox_auth_failed",
                    telegram_id=telegram_id,
                    error=str(e),
                )
                # Don't fall back on auth error - user should fix credentials
                return {
                    "status": "error",
                    "error": f"Ошибка авторизации на вашем seedbox: {e}",
                    "magnet": magnet_link,
                    "user_seedbox": True,
                }

            except SeedboxConnectionError as e:
                logger.warning(
                    "user_seedbox_connection_failed",
                    telegram_id=telegram_id,
                    error=str(e),
                )
                # Fall back to global seedbox
                logger.info("falling_back_to_global_seedbox", telegram_id=telegram_id)

            except SeedboxTorrentError as e:
                logger.warning(
                    "user_seedbox_torrent_failed",
                    telegram_id=telegram_id,
                    error=str(e),
                )
                return {
                    "status": "error",
                    "error": f"Ошибка добавления на ваш seedbox: {e}",
                    "magnet": magnet_link,
                    "user_seedbox": True,
                }

    # Fall back to global seedbox
    result = await send_magnet_to_seedbox(magnet_link)
    result["user_seedbox"] = False
    return result
