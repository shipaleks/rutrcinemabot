#!/bin/bash
# Seedbox to NAS sync script
# Syncs completed downloads from Ultra.cc seedbox to local Freebox NAS
# Runs every 30 minutes via cron
#
# Logic:
#   1. Rsync new files from seedbox to NAS staging folder
#   2. Sort video files into folders: TV shows -> Сериалы, Movies -> Кино
#   3. Clean up staging (move, not copy)
#   4. Delete synced files from seedbox (older than DELETE_AFTER_DAYS)
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
DELETE_AFTER_DAYS="${DELETE_AFTER_DAYS:-1}"
SSH_OPTS="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR"

mkdir -p "$(dirname "$LOG_FILE")"
touch "$MANIFEST"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
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

# 1. Rsync to NAS staging folder (no --remove-source-files)
log "Rsyncing from seedbox to staging..."
mkdir -p "$NAS_STAGING"
sshpass -p "$SEEDBOX_PASS" rsync -avz \
    -e "ssh $SSH_OPTS" \
    "$SEEDBOX_USER@$SEEDBOX_HOST:$SEEDBOX_PATH/" \
    "$NAS_STAGING/" 2>> "$LOG_FILE" || {
        log "Rsync failed"
        exit 1
    }

# 2. Sort new video files into Кино / Сериалы
#    - Detect folder name from torrent (parent directory)
#    - TV shows go into NAS_TV/<folder>/, movies into NAS_MOVIES/<folder>/
#    - Move (not copy) from staging to avoid duplicating space
new_count=0
sorted_files=""

find "$NAS_STAGING" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.avi" -o -name "*.mov" \) | while IFS= read -r file; do
    # Skip if already processed
    if grep -qFx "$file" "$MANIFEST" 2>/dev/null; then
        continue
    fi

    filename=$(basename "$file")

    # Get the torrent folder name (parent dir relative to staging)
    rel_path="${file#$NAS_STAGING/}"
    torrent_folder=$(echo "$rel_path" | cut -d'/' -f1)

    # If file is directly in staging (no subfolder), use filename without extension as folder
    if [[ "$torrent_folder" == "$filename" ]]; then
        torrent_folder="${filename%.*}"
    fi

    # TV show detection: S01E01, s01e01, 1x01, Season.1, .E01.
    if echo "$torrent_folder" | grep -qiE 'S[0-9]{1,2}E[0-9]{1,2}|S[0-9]{1,2}\.COMPLETE|[0-9]+x[0-9]+|Season.[0-9]+|\.E[0-9]{2}\.'; then
        dest="$NAS_TV/$torrent_folder"
    elif echo "$filename" | grep -qiE 'S[0-9]{1,2}E[0-9]{1,2}|[0-9]+x[0-9]+|Season.[0-9]+|\.E[0-9]{2}\.'; then
        dest="$NAS_TV/$torrent_folder"
    else
        dest="$NAS_MOVIES/$torrent_folder"
    fi

    mkdir -p "$dest"
    mv "$file" "$dest/" 2>> "$LOG_FILE" && {
        echo "$file" >> "$MANIFEST"
        log "Sorted: $filename -> $dest"
        new_count=$((new_count + 1))
        sorted_files="${sorted_files}${filename}\n"

        # Notify bot API per file
        if [[ -n "${BOT_API_URL:-}" ]] && [[ -n "${SYNC_API_KEY:-}" ]]; then
            curl -s -X POST "$BOT_API_URL/api/sync/complete" \
                -H "X-API-Key: $SYNC_API_KEY" \
                -H "Content-Type: application/json" \
                -d "{\"filename\":\"$filename\",\"local_path\":\"$dest\"}" >> "$LOG_FILE" 2>&1 || true
        fi
    } || {
        log "Failed to sort: $filename"
    }
done

# Clean up empty directories in staging
find "$NAS_STAGING" -type d -empty -delete 2>/dev/null || true

log "Sorting done"

# 3. Delete files from seedbox older than DELETE_AFTER_DAYS
log "Cleaning seedbox (files older than ${DELETE_AFTER_DAYS}d)..."
sshpass -p "$SEEDBOX_PASS" ssh $SSH_OPTS "$SEEDBOX_USER@$SEEDBOX_HOST" \
    "find $SEEDBOX_PATH -type f -mtime +${DELETE_AFTER_DAYS} -delete 2>/dev/null; \
     find $SEEDBOX_PATH -type d -empty -delete 2>/dev/null" 2>> "$LOG_FILE" || true

log "Sync completed successfully"
