#!/bin/sh
set -e

# Ensure all application and data directories exist
mkdir -p /app/input /app/input/invoices /app/processed /app/errors /app/output /app/backups /app/reports /app/logs /app/config /app/data

# Sync Master.xlsx on container startup:
# 1. If Master.xlsx is missing in the container root, restore from mounted /app/data if present
if [ ! -f /app/Master.xlsx ] && [ -f /app/data/Master.xlsx ]; then
    echo "[entrypoint] Restoring Master.xlsx from /app/data/Master.xlsx..."
    cp /app/data/Master.xlsx /app/Master.xlsx
fi

# 2. If still missing, check if any previous backup exists in /app/backups
if [ ! -f /app/Master.xlsx ]; then
    LATEST_BACKUP=$(ls -t /app/backups/Master_*.xlsx 2>/dev/null | head -n 1)
    if [ -n "$LATEST_BACKUP" ] && [ -f "$LATEST_BACKUP" ]; then
        echo "[entrypoint] Found latest backup '$LATEST_BACKUP'. Restoring to /app/Master.xlsx..."
        cp "$LATEST_BACKUP" /app/Master.xlsx
    fi
fi

# 3. If Master.xlsx exists in root but not in /app/data, initialize /app/data
if [ -f /app/Master.xlsx ] && [ ! -f /app/data/Master.xlsx ]; then
    cp /app/Master.xlsx /app/data/Master.xlsx
fi

exec "$@"

