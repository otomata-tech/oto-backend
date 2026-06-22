#!/usr/bin/env bash
#
# Refresh mensuel du parquet SIRENE → Object Storage (S3), pour la lecture
# distante via httpfs (ADR 0002 : oto-mcp sur la box dédiée, parquet déporté).
#
# Remplace le download-vers-disque-local (refresh_sirene_stock.sh) quand le
# parquet est servi depuis S3 (SIRENE_STOCK_PARQUET_PATH=s3://…).
#
# Résout l'URL parquet du mois via l'API data.gouv (le chemin est daté → il
# change à chaque publication INSEE début de mois), télécharge en atomique,
# puis pousse vers S3 sous une CLÉ STABLE. Pas de restart oto-mcp nécessaire
# (DuckDB rouvre une connexion par requête).
#
# À installer en cron mensuel sur une machine avec disque + aws cli (ex.
# otomata-0), le 2 du mois (après publication INSEE) :
#
#   15 4 2 * * /opt/oto-mcp/deploy/refresh_sirene_stock_s3.sh >> /var/log/sirene-refresh-s3.log 2>&1
#
# Variables d'env requises :
#   S3_BUCKET            ex. oto-media
#   S3_KEY               ex. sirene/StockEtablissement.parquet   (clé stable)
#   S3_ENDPOINT          ex. https://s3.fr-par.scw.cloud
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_DEFAULT_REGION (creds Scaleway)

set -euo pipefail

# Dataset data.gouv « Base Sirene des entreprises et de leurs établissements ».
DATASET_ID="${SIRENE_DATAGOUV_DATASET:-5b7ffc618b4c4169d30727e0}"
S3_BUCKET="${S3_BUCKET:?S3_BUCKET requis}"
S3_KEY="${S3_KEY:-sirene/StockEtablissement.parquet}"
S3_ENDPOINT="${S3_ENDPOINT:?S3_ENDPOINT requis}"

echo "[$(date -Iseconds)] résolution de l'URL parquet StockEtablissement via data.gouv"
URL=$(curl -sf "https://www.data.gouv.fr/api/1/datasets/${DATASET_ID}/" \
  | python3 -c "import sys,json
d=json.load(sys.stdin)
cands=[r for r in d.get('resources',[])
       if (r.get('format')=='parquet')
       and 'stocketablissement-parquet' in (r.get('url') or '').lower()
       and 'historique' not in (r.get('url') or '').lower()
       and 'lienssuccession' not in (r.get('url') or '').lower()]
print(cands[0]['url'] if cands else '')")

if [ -z "${URL}" ]; then
  echo "[$(date -Iseconds)] ERREUR: URL parquet introuvable dans l'API data.gouv"; exit 1
fi
echo "[$(date -Iseconds)] URL: ${URL}"

TMP=$(mktemp --suffix=.parquet)
trap 'rm -f "${TMP}"' EXIT

echo "[$(date -Iseconds)] téléchargement → ${TMP}"
curl -L -f -# -o "${TMP}" "${URL}"

SIZE=$(stat -c '%s' "${TMP}")
if [ "${SIZE}" -lt 1000000000 ]; then
  echo "[$(date -Iseconds)] ERREUR: fichier trop petit (${SIZE} octets), abandon"; exit 1
fi

echo "[$(date -Iseconds)] upload → s3://${S3_BUCKET}/${S3_KEY} (${SIZE} octets)"
aws s3 cp "${TMP}" "s3://${S3_BUCKET}/${S3_KEY}" --endpoint-url "${S3_ENDPOINT}" --only-show-errors

echo "[$(date -Iseconds)] terminé."
