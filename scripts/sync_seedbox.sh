#!/bin/bash
# Seedbox to NAS sync script
# Syncs completed downloads from Ultra.cc seedbox to local Freebox NAS
# Runs every 30 minutes via cron
#
# Setup:
#   1. Copy config.env.template to config.env
#   2. Fill in your credentials
#   3. chmod 600 config.env
#   4. Add to cron: */30 * * * * /path/to/sync_seedbox.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.env"

# Check config exists
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: $CONFIG_FILE not found"
    echo "Copy config.env.template to config.env and fill in credentials"
    exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

# Ensure required vars are set
: "${SEEDBOX_HOST:?SEEDBOX_HOST not set}"
: "${SEEDBOX_USER:?SEEDBOX_USER not set}"
: "${SEEDBOX_PASS:?SEEDBOX_PASS not set}"
: "${SEEDBOX_PATH:?SEEDBOX_PATH not set}"
: "${NAS_MOVIES:?NAS_MOVIES not set}"
: "${NAS_TV:?NAS_TV not set}"
: "${LOG_FILE:=$SCRIPT_DIR/logs/sync.log}"

# Create log directory
mkdir -p "$(dirname "$LOG_FILE")"

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

# Create temp directory for this sync
TEMP_DIR="/tmp/seedbox_sync_$$"
mkdir -p "$TEMP_DIR"

# 1. Rsync completed files from seedbox
log "Rsyncing from seedbox..."
sshpass -p "$SEEDBOX_PASS" rsync -avz --progress \
    --remove-source-files \
    "$SEEDBOX_USER@$SEEDBOX_HOST:$SEEDBOX_PATH/" \
    "$TEMP_DIR/" 2>> "$LOG_FILE" || {
        log "Rsync failed, check credentials and connectivity"
        rm -rf "$TEMP_DIR"
        exit 1
    }

# Count files
file_count=$(find "$TEMP_DIR" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.avi" -o -name "*.mov" \) 2>/dev/null | wc -l)
log "Found $file_count video files"

if [[ "$file_count" -eq 0 ]]; then
    log "No files to sync"
    rm -rf "$TEMP_DIR"
    exit 0
fi

# 2. Sort files: TV shows vs Movies
# TV show patterns: S01E01, s01e01, 1x01, Season 1
find "$TEMP_DIR" -type f \( -name "*.mkv" -o -name "*.mp4" -o -name "*.avi" -o -name "*.mov" \) | while read -r file; do
    filename=$(basename "$file")

    # Check if it's a TV show
    if echo "$filename" | grep -qiE 'S[0-9]{1,2}E[0-9]{1,2}|[0-9]+x[0-9]+|Season.[0-9]+|\.E[0-9]{2}\.'; then
        dest="$NAS_TV"
    else
        dest="$NAS_MOVIES"
    fi

    # Move file to destination
    mv "$file" "$dest/" 2>> "$LOG_FILE" && {
        log "Moved: $filename -> $dest"

        # Notify bot API if configured
        if [[ -n "${BOT_API_URL:-}" ]] && [[ -n "${SYNC_API_KEY:-}" ]]; then
            curl -s -X POST "$BOT_API_URL/api/sync/complete" \
                -H "X-API-Key: $SYNC_API_KEY" \
                -H "Content-Type: application/json" \
                -d "{\"filename\":\"$filename\",\"local_path\":\"$dest\"}" >> "$LOG_FILE" 2>&1 || true
        fi
    } || {
        log "Failed to move: $filename"
    }
done

# 3. Clean up empty directories in temp
find "$TEMP_DIR" -type d -empty -delete 2>/dev/null || true
rm -rf "$TEMP_DIR"

# 4. Clean up empty directories on seedbox
log "Cleaning empty directories on seedbox..."
sshpass -p "$SEEDBOX_PASS" ssh "$SEEDBOX_USER@$SEEDBOX_HOST" \
    "find $SEEDBOX_PATH -type d -empty -delete 2>/dev/null" 2>> "$LOG_FILE" || true

log "Sync completed successfully"
