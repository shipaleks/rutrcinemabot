#!/usr/bin/env python3
"""NAS library indexer — scans movie/TV directories and pushes index to bot.

Designed to run as a cron job on the VM that has NAS mounted:
    0 * * * * /usr/bin/python3 /home/mediabot/sync/library_indexer.py

Also runs once after each sync via sync_seedbox.sh (optional).

Configuration via config.env (same file as sync scripts):
    NAS_MOVIES, NAS_TV, BOT_API_URL, SYNC_API_KEY
"""

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.env"

# Load config from config.env
if CONFIG_FILE.exists():
    with open(CONFIG_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # Expand $VAR references
                while "$" in value:
                    match = re.search(r"\$(\w+)", value)
                    if not match:
                        break
                    var_name = match.group(1)
                    var_value = os.environ.get(var_name, "")
                    value = value.replace(f"${var_name}", var_value)
                os.environ.setdefault(key, value)

NAS_MOVIES = os.environ.get("NAS_MOVIES", "")
NAS_TV = os.environ.get("NAS_TV", "")
BOT_API_URL = os.environ.get("BOT_API_URL", "")
API_KEY = os.environ.get("SYNC_API_KEY", "")

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".wmv", ".mov"}


def scan_directory(base_path: str) -> list[dict]:
    """Scan top-level dirs and their video files."""
    if not base_path or not os.path.isdir(base_path):
        return []

    result = []
    try:
        for entry in sorted(os.scandir(base_path), key=lambda e: e.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir(follow_symlinks=False):
                # Scan video files inside this directory
                items = []
                try:
                    for sub in sorted(os.scandir(entry.path), key=lambda e: e.name.lower()):
                        if sub.is_file(follow_symlinks=False):
                            ext = os.path.splitext(sub.name)[1].lower()
                            if ext in VIDEO_EXTENSIONS:
                                stat = sub.stat(follow_symlinks=False)
                                items.append(
                                    {
                                        "name": sub.name,
                                        "type": "file",
                                        "size_mb": stat.st_size // (1024 * 1024),
                                    }
                                )
                except PermissionError:
                    pass
                result.append(
                    {
                        "name": entry.name,
                        "type": "dir",
                        "items": items,
                    }
                )
            elif entry.is_file(follow_symlinks=False):
                ext = os.path.splitext(entry.name)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    stat = entry.stat(follow_symlinks=False)
                    result.append(
                        {
                            "name": entry.name,
                            "type": "file",
                            "size_mb": stat.st_size // (1024 * 1024),
                            "items": [],
                        }
                    )
    except PermissionError:
        pass

    return result


def push_index(index: dict) -> bool:
    """POST index to bot API."""
    if not BOT_API_URL or not API_KEY:
        print("Error: BOT_API_URL and SYNC_API_KEY required", file=sys.stderr)
        return False

    body = json.dumps(index, ensure_ascii=False).encode("utf-8")

    # Try with /api prefix first, then without (Koyeb strips it)
    for url_path in ("/api/sync/library-index", "/sync/library-index"):
        url = f"{BOT_API_URL.rstrip('/')}{url_path}"
        req = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "X-API-Key": API_KEY,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                print(f"Index pushed: {result}")
                return True
        except urllib.error.HTTPError as e:
            if e.code == 404:
                continue  # Try next URL
            print(f"HTTP error: {e.code} {e.read().decode()}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"Request failed: {e}", file=sys.stderr)
            return False

    print("Error: all API URLs returned 404", file=sys.stderr)
    return False


def main():
    print(f"Scanning movies: {NAS_MOVIES or '(not set)'}")
    print(f"Scanning TV: {NAS_TV or '(not set)'}")

    index = {
        "movies": scan_directory(NAS_MOVIES),
        "tv": scan_directory(NAS_TV),
    }

    movies_count = len(index["movies"])
    tv_count = len(index["tv"])
    print(f"Found: {movies_count} movies, {tv_count} TV shows")

    if push_index(index):
        print("Done")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
