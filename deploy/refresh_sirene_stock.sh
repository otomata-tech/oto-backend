#!/usr/bin/env bash
#
# Refresh mensuel du parquet SIRENE depuis data.gouv.fr.
#
# À installer en cron mensuel sur tuls.me (le 1er du mois, après la publication
# INSEE qui sort en début de mois) :
#
#   5 4 1 * * /opt/oto-mcp/deploy/refresh_sirene_stock.sh >> /var/log/oto-mcp-sirene-refresh.log 2>&1
#
# Le download est atomique (tmp + rename). Restart oto-mcp à la fin pour que
# DuckDB rouvre une connexion fraîche sur le nouveau fichier (pas vraiment
# nécessaire car on n'a pas de connection persistante, mais propre).

set -euo pipefail

URL="https://object.files.data.gouv.fr/data-pipeline-open/siren/stock/StockEtablissement_utf8.parquet"
DEST_DIR="${SIRENE_STOCK_DIR:-/opt/oto-mcp/data/sirene}"
DEST_FILE="${DEST_DIR}/StockEtablissement.parquet"
TMP_FILE="${DEST_FILE}.tmp"

mkdir -p "${DEST_DIR}"

echo "[$(date -Iseconds)] downloading SIRENE stock from data.gouv → ${TMP_FILE}"
curl -L -f -# -C - -o "${TMP_FILE}" "${URL}"

SIZE=$(stat -c '%s' "${TMP_FILE}")
if [ "${SIZE}" -lt 1000000000 ]; then
  echo "[$(date -Iseconds)] ERROR: downloaded file too small (${SIZE} bytes), aborting"
  rm -f "${TMP_FILE}"
  exit 1
fi

echo "[$(date -Iseconds)] atomic swap → ${DEST_FILE} (${SIZE} bytes)"
mv -f "${TMP_FILE}" "${DEST_FILE}"

# Restart oto-mcp pour reset les éventuelles file descriptors ouverts (DuckDB
# n'en garde pas en pratique vu qu'on ouvre/ferme par requête, mais propre).
if systemctl is-active --quiet oto-mcp; then
  echo "[$(date -Iseconds)] restarting oto-mcp"
  systemctl restart oto-mcp
fi

echo "[$(date -Iseconds)] done."
