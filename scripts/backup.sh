#!/bin/sh
set -eu
umask 077
: "${HADAN_DATA_DIR:?Set HADAN_DATA_DIR}"
: "${HADAN_BACKUP_KEY_FILE:?Set HADAN_BACKUP_KEY_FILE}"
backup_root="${HADAN_BACKUP_DIR:-$HADAN_DATA_DIR/backups}"
mkdir -p "$backup_root"
backup_name="$backup_root/$(date -u +%Y%m%dT%H%M%SZ).hadan.enc"
/opt/quant-subscribe/current/.venv/bin/python -m hadan.cli backup "$backup_name" --key-file "$HADAN_BACKUP_KEY_FILE"
# A separate read-only uploader group may read only the completed encrypted file.
if [ "${HADAN_BACKUP_GROUP_READ:-0}" = 1 ]; then
    chmod 0640 "$backup_name"
fi
/opt/quant-subscribe/current/.venv/bin/python -m hadan.cli cleanup --apply
# Only remove this application's old backup files after a new backup succeeded.
find "$backup_root" -maxdepth 1 -type f -name '*.hadan.enc' -mtime +14 -delete
