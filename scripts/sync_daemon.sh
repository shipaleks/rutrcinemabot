#!/bin/bash
# Seedbox sync daemon — event-driven replacement for cron
#
# Polls the bot's /api/sync/pending endpoint every 30 seconds.
# When sync_needed=true, runs sync_seedbox.sh to rsync and sort files.
#
# Install as systemd service:
#   sudo cp sync_daemon.service /etc/systemd/system/
#   sudo systemctl enable --now sync_daemon

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="$SCRIPT_DIR/config.env"

if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Error: $CONFIG_FILE not found"
    exit 1
fi

# shellcheck source=/dev/null
source "$CONFIG_FILE"

: "${BOT_API_URL:?BOT_API_URL not set}"
: "${SYNC_API_KEY:?SYNC_API_KEY not set}"
: "${LOG_FILE:=$SCRIPT_DIR/logs/sync.log}"

POLL_INTERVAL="${POLL_INTERVAL:-30}"
SYNC_SCRIPT="$SCRIPT_DIR/sync_seedbox.sh"

mkdir -p "$(dirname "$LOG_FILE")"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [daemon] $1" >> "$LOG_FILE"
}

log "Sync daemon started (poll every ${POLL_INTERVAL}s)"

while true; do
    # Poll the bot API
    response=$(curl -sf \
        -H "X-API-Key: $SYNC_API_KEY" \
        "$BOT_API_URL/api/sync/pending" 2>/dev/null) || {
        # Fallback: try without /api prefix (Koyeb path stripping)
        response=$(curl -sf \
            -H "X-API-Key: $SYNC_API_KEY" \
            "$BOT_API_URL/sync/pending" 2>/dev/null) || {
            sleep "$POLL_INTERVAL"
            continue
        }
    }

    # Check if sync is needed
    sync_needed=$(echo "$response" | grep -o '"sync_needed": *true' || true)

    if [[ -n "$sync_needed" ]]; then
        log "Sync needed — starting rsync"
        bash "$SYNC_SCRIPT" 2>> "$LOG_FILE" || {
            log "Sync script failed"
        }
    fi

    sleep "$POLL_INTERVAL"
done
