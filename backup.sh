#!/usr/bin/env sh
# Copy the recordings somewhere they will survive this host.
#
# The database is the one thing here that cannot be rebuilt: the exchange keeps about two
# and a half hours of history and the price feed a few more, so a disk lost on Thursday
# cannot be re-collected on Friday. Everything else in this repository can be restored from
# git in a minute.
#
# Uses SQLite's own backup, not `cp`, because the Collector is writing the whole time and a
# copied file can be a torn one.
set -eu
cd "$(dirname "$0")"

DEST="${STRATEGY_LAB_BACKUP_DIR:-$HOME/lab-backups}"
KEEP="${STRATEGY_LAB_BACKUP_KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M)"

mkdir -p "$DEST"

docker compose exec -T collector python -c "
import sqlite3
source = sqlite3.connect('/data/lab.db')
target = sqlite3.connect('/data/.backup-in-progress.db')
source.backup(target)
target.close()
source.close()
"

mv data/.backup-in-progress.db "$DEST/lab-$STAMP.db"
echo "saved $DEST/lab-$STAMP.db ($(du -h "$DEST/lab-$STAMP.db" | cut -f1))"

# Keep the most recent few and let the rest go.
ls -1t "$DEST"/lab-*.db 2>/dev/null | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -f "$old"
    echo "removed $old"
done
