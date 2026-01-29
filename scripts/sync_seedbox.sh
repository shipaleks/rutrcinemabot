#!/bin/bash
# Seedbox to NAS sync script
# Syncs completed downloads from Ultra.cc seedbox to local Freebox NAS
# Runs every 30 minutes via cron
#
# Logic:
#   1. Rsync new files from seedbox to NAS staging folder
#   2. Sort video files into folders: TV shows -> Сериалы, Movies -> Кино
#      - Extracts clean series name (e.g. "Patriot S01" from various torrent names)
#   3. Clean up staging (move, not copy)
#   4. Delete synced files from seedbox immediately after successful sync
#   5. Notify bot API about completed syncs
#
# Setup:
#   1. Copy config.env.template to config.env
#   2. Fill in your credentials
#   3. chmod 600 config.env
#   4. Add to cron: */30 * * * * /path/to/sync_seedbox.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.env"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: $CONFIG_FILE not found"
    exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

: "${SEEDBOX_HOST:?SEEDBOX_HOST not set}"
: "${SEEDBOX_USER:?SEEDBOX_USER not set}"
: "${SEEDBOX_PASS:?SEEDBOX_PASS not set}"
: "${SEEDBOX_PATH:?SEEDBOX_PATH not set}"
: "${NAS_MOVIES:?NAS_MOVIES not set}"
: "${NAS_TV:?NAS_TV not set}"
: "${NAS_STAGING:?NAS_STAGING not set}"
: "${LOG_FILE:=$SCRIPT_DIR/logs/sync.log}"

MANIFEST="$SCRIPT_DIR/synced_files.manifest"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

mkdir -p "$(dirname "$LOG_FILE")"
touch "$MANIFEST"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# Extract clean series/movie name from torrent folder or filename
# Examples:
#   "www.UIndex.org - Patriot S01E01 Milwaukee..." -> "Patriot S01"
#   "Station.Eleven.S01.COMPLETE.2160p..." -> "Station Eleven S01"
#   "The.Bear.2022.S03.2160p..." -> "The Bear S03"
extract_series_name() {
    local name="$1"

    # Remove common prefixes like "www.UIndex.org - " or "www.Torrenting.com - "
    name=$(echo "$name" | sed -E 's/^www\.[^ ]+ +- +//')

    # Extract up to and including season identifier (S01, Season 1, etc.)
    # Try S01E01 pattern first - keep up to S01
    local series
    series=$(echo "$name" | grep -oiE '^.*?S[0-9]{1,2}' | head -1)
    if [[ -z "$series" ]]; then
        # Try "Season X" pattern
        series=$(echo "$name" | grep -oiE '^.*?Season.?[0-9]+' | head -1)
    fi
    if [[ -z "$series" ]]; then
        # No season found, use full name up to quality marker
        series=$(echo "$name" | sed -E 's/[. ](2160p|1080p|720p|480p|WEB|BDRip|BluRay|HDTV|COMPLETE).*//i')
    fi

    # Clean up: replace dots/underscores with spaces, trim
    series=$(echo "$series" | tr '._' ' ' | sed -E 's/ +/ /g; s/^ +//; s/ +$//')

    echo "$series"
}

# Lock file to prevent concurrent runs
LOCK_FILE="/tmp/sync_seedbox.lock"
if [[ -f "$LOCK_FILE" ]]; then
    pid=$(cat "$LOCK_FILE")
    if kill -0 "$pid" 2>/dev/null; then
        log "Another sync is running (PID $pid), exiting"
        exit 0
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

log "Starting sync from $SEEDBOX_HOST"

# 1. Rsync to NAS staging folder, then delete from seedbox
log "Rsyncing from seedbox to staging..."
mkdir -p "$NAS_STAGING"
sshpass -p "$SEEDBOX_PASS" rsync -avz \
    --remove-source-files \
    -e "ssh $SSH_OPTS" \
    "$SEEDBOX_USER@$SEEDBOX_HOST:$SEEDBOX_PATH/" \
    "$NAS_STAGING/" 2>> "$LOG_FILE" || {
        log "Rsync failed"
        exit 1
    }

# Clean empty dirs on seedbox after rsync
sshpass -p "$SEEDBOX_PASS" ssh $SSH_OPTS "$SEEDBOX_USER@$SEEDBOX_HOST" \
    "find $SEEDBOX_PATH -type d -empty -delete 2>/dev/null" 2>> "$LOG_FILE" || true

# 2. Sort new video files into Кино / Сериалы
#    - Extract clean series name to group episodes together
#    - Move (not copy) from staging
# Collect sorted files for a single summary notification
SORTED_COUNT=0
LAST_DEST=""
LAST_CLEAN_NAME=""

while IFS= read -r file; do
    # Skip if already processed
    if grep -qFx "$file" "$MANIFEST" 2>/dev/null; then
        continue
    fi

    filename=$(basename "$file")

    # Get the torrent folder name (parent dir relative to staging)
    rel_path="${file#$NAS_STAGING/}"
    torrent_folder=$(echo "$rel_path" | cut -d'/' -f1)

    # If file is directly in staging (no subfolder), use filename
    if [[ "$torrent_folder" == "$filename" ]]; then
        torrent_folder="$filename"
    fi

    # TV show detection
    is_tv=false
    if echo "$torrent_folder" | grep -qiE 'S[0-9]{1,2}E[0-9]{1,2}|S[0-9]{1,2}\.COMPLETE|S[0-9]{1,2}\b|[0-9]+x[0-9]+|Season.[0-9]+|\.E[0-9]{2}\.'; then
        is_tv=true
    elif echo "$filename" | grep -qiE 'S[0-9]{1,2}E[0-9]{1,2}|[0-9]+x[0-9]+|Season.[0-9]+|\.E[0-9]{2}\.'; then
        is_tv=true
    fi

    if [[ "$is_tv" == "true" ]]; then
        clean_name=$(extract_series_name "$torrent_folder")
        if [[ -z "$clean_name" ]]; then
            clean_name="$torrent_folder"
        fi
        dest="$NAS_TV/$clean_name"
    else
        clean_name=$(extract_series_name "$torrent_folder")
        if [[ -z "$clean_name" ]]; then
            clean_name="$torrent_folder"
        fi
        dest="$NAS_MOVIES/$clean_name"
    fi

    mkdir -p "$dest"
    mv "$file" "$dest/" 2>> "$LOG_FILE" && {
        echo "$file" >> "$MANIFEST"
        log "Sorted: $filename -> $dest"
        SORTED_COUNT=$((SORTED_COUNT + 1))
        LAST_DEST="$dest"
        LAST_CLEAN_NAME="$clean_name"
    } || {
        log "Failed to sort: $filename"
    }
done < <(find "$NAS_STAGING" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.avi" -o -name "*.mov" \))

# Clean up empty directories in staging
find "$NAS_STAGING" -type d -empty -delete 2>/dev/null || true

# Send ONE summary notification (not per-file)
if [[ "$SORTED_COUNT" -gt 0 ]] && [[ -n "${BOT_API_URL:-}" ]] && [[ -n "${SYNC_API_KEY:-}" ]]; then
    if [[ "$SORTED_COUNT" -eq 1 ]]; then
        notify_name="$LAST_CLEAN_NAME"
    else
        notify_name="$LAST_CLEAN_NAME ($SORTED_COUNT файлов)"
    fi
    curl -s -X POST "$BOT_API_URL/api/sync/complete" \
        -H "X-API-Key: $SYNC_API_KEY" \
        -H "Content-Type: application/json" \
        -d "{\"filename\":\"$notify_name\",\"local_path\":\"$LAST_DEST\"}" >> "$LOG_FILE" 2>&1 || true
    log "Notification sent: $notify_name"
fi

log "Sync completed successfully (sorted $SORTED_COUNT files)"
