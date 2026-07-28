#!/usr/bin/env bash
# Nightly database backup. The Pi stays the source of truth; this is insurance.
#
# Install with:
#   chmod +x scripts/backup_mongo.sh
#   crontab -e
#   0 2 * * * /home/pi/AgriSync/scripts/backup_mongo.sh >> /home/pi/agrisync-backup.log 2>&1

set -euo pipefail

BACKUP_DIR="${HOME}/agrisync-backups"
KEEP_DAYS=30
STAMP=$(date +%Y%m%d-%H%M)
OUT="${BACKUP_DIR}/agrisync-${STAMP}.archive.gz"

mkdir -p "${BACKUP_DIR}"

# Dump straight out of the Docker container
docker exec mongo mongodump \
  --db=agrisync \
  --archive \
  --gzip > "${OUT}"

echo "$(date -Is) wrote ${OUT} ($(du -h "${OUT}" | cut -f1))"

# Prune local copies older than KEEP_DAYS
find "${BACKUP_DIR}" -name 'agrisync-*.archive.gz' -mtime +${KEEP_DAYS} -delete

# TODO: push offsite. Pick one and uncomment.
#
# Azure Blob (az login once first):
# az storage blob upload --account-name YOURACCOUNT --container-name agrisync \
#   --name "$(basename "${OUT}")" --file "${OUT}" --auth-mode login
#
# Or restore into Atlas:
# mongorestore --uri="$ATLAS_URI" --archive="${OUT}" --gzip --drop
