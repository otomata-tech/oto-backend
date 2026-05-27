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

ETAB_URL="https://object.files.data.gouv.fr/data-pipeline-open/siren/stock/StockEtablissement_utf8.parquet"
UL_URL="https://object.files.data.gouv.fr/data-pipeline-open/siren/stock/StockUniteLegale_utf8.parquet"
DEST_DIR="${SIRENE_STOCK_DIR:-/opt/oto-mcp/data/sirene}"

mkdir -p "${DEST_DIR}"

download_and_swap() {
  local url="$1"
  local dest="$2"
  local min_size="$3"
  local tmp="${dest}.tmp"

  echo "[$(date -Iseconds)] downloading ${url##*/} → ${tmp}"
  curl -L -f -# -C - -o "${tmp}" "${url}"

  local size
  size=$(stat -c '%s' "${tmp}")
  if [ "${size}" -lt "${min_size}" ]; then
    echo "[$(date -Iseconds)] ERROR: ${tmp} too small (${size} < ${min_size}), aborting"
    rm -f "${tmp}"
    exit 1
  fi
  echo "[$(date -Iseconds)] atomic swap → ${dest} (${size} bytes)"
  mv -f "${tmp}" "${dest}"
}

# Establishement parquet (~2 GB, ~43M rows).
download_and_swap "${ETAB_URL}" "${DEST_DIR}/StockEtablissement.parquet" 1000000000

# UniteLegale parquet (~1 GB, ~32M unités légales) — raison sociale, NAF société,
# dates de création, forme juridique. Joint avec StockEtablissement sur siren.
download_and_swap "${UL_URL}" "${DEST_DIR}/StockUniteLegale.parquet" 500000000

# Restart oto-mcp pour reset les éventuelles file descriptors ouverts (DuckDB
# n'en garde pas en pratique vu qu'on ouvre/ferme par requête, mais propre).
if systemctl is-active --quiet oto-mcp; then
  echo "[$(date -Iseconds)] restarting oto-mcp"
  systemctl restart oto-mcp
fi

echo "[$(date -Iseconds)] done."
