#!/bin/bash
# Läuft automatisch vor jedem Server-Start
DB="/Users/ersinozdemir/Downloads/citybot 2/content_os/instance/content_os.db"
BACKUP_DIR="/Users/ersinozdemir/Downloads/citybot 2/content_os/db_backups"
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y-%m-%d_%H-%M)
if [ -f "$DB" ]; then
  cp "$DB" "$BACKUP_DIR/content_os_$STAMP.db"
  # Nur die letzten 30 Backups behalten
  ls -t "$BACKUP_DIR"/*.db 2>/dev/null | tail -n +31 | xargs rm -f
  echo "Backup: $BACKUP_DIR/content_os_$STAMP.db"
fi
