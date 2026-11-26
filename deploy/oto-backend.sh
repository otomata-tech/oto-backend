#!/bin/bash
# Source versionnée : oto-backend/deploy/oto-backend.sh (repo otomata-tech/oto-backend)
#   — modifier là puis recopier ici (/opt/deploy/oto-backend.sh sur oto-platform).
# Déploiement build-serveur d'oto-backend PROD (oto-mcp), exécuté par le runner
# self-hosted via `sudo /opt/deploy/oto-backend.sh <tag>` (sudoers NOPASSWD scope).
# Modèle de release : seul un TAG vX.Y.Z déploie, en PROD (CE script) ; `main` n'est
# que le tronc, jugé par la CI sans rien servir (plus de préproduction, 19/11/2026). Le tag est passé en argument par le workflow
# (github.ref_name) ; pas de défaut (prod = acte explicite, aucun fallback branche).
# Cf. ADR 0020. Endpoints : mcp.oto.cx, les hôtes des tenants tiers, *.mcp.oto.cx, *.share.oto.cx.
#
# 28/08/2026 — BLEU/VERT. Avant : `systemctl restart` sec, 36-39 s port fermé, donc
# TOUTE session MCP ouverte coupée à chaque release. Maintenant : la nouvelle
# version démarre à côté sur le port de la couleur inactive, on la valide en direct,
# on bascule l'amont Caddy (reload gracieux — les connexions établies CONTINUENT sur
# l'ancienne couleur), on DRAINE l'ancienne (jusqu'à 4 min, le temps que les requêtes
# en cours finissent), puis on l'arrête. Détail : deploy/oto-mcp-bluegreen.sh.
# ⚠️ L'amont de l'on-demand TLS (`ask` -> /api/mcp/tls-check) bascule AVEC le reste :
# il est dans le même fichier d'amont généré, sinon les certificats à la demande
# casseraient dès l'arrêt de l'ancienne couleur.
#
# Usage : oto-backend.sh v1.2.3      -> déploie ce tag
#         oto-backend.sh --rollback  -> rebascule sur la couleur inactive, qui
#                                       porte encore la version précédente.
set -uo pipefail

REF="${1:-}"
[ -n "$REF" ] || { echo "usage: oto-backend.sh <tag>  (ex: v1.2.3) | --rollback" >&2; exit 2; }

BG_ENV=prod
BG_UNIT=oto-mcp
BG_TREE=/opt/oto-mcp
BG_PORT_blue=9103
BG_PORT_green=9107
BG_UPSTREAM=/etc/caddy/upstream-oto-prod.conf
BG_SNIPPET=oto_prod
BG_DOCSHARE_HOST=mcp.oto.cx
BG_ASK=1                     # la prod porte le `ask` de l'on-demand TLS
BG_ACTIVE=/etc/oto-mcp/active-prod
BG_PUBLIC="https://mcp.oto.cx/.well-known/oauth-protected-resource"
# Plafond de la vidange. Mesuré le 28/08 sous charge réelle : la vidange ne protège
# PAS les sessions MCP (elles tombent en <4 s, le client rouvre), seulement les
# requêtes déjà en cours. Elle s'arrête dès que la couleur n'a plus de connexion
# (quelques secondes en pratique) : le plafond ne mord que sur un appel long.
# 240 s (19/11/2026) : relevé du journal d'appels de la production sur 30 jours, le plus
# long appel ABOUTI dure 190 s (p99,9 des appels d'outils : 33 s) ; 120 s en coupait
# trois. Les seuls appels plus longs (2, à 300 s) échouent sur un délai du fournisseur —
# et la vidange en vol leur donne 240 + 240 s (plafond, puis arrêt gracieux d'uvicorn).
# ⚠️ Égal à l'arrêt gracieux d'uvicorn (`oto_mcp/bornes_processus.ARRET_GRACIEUX_S`) et
# sous le TimeoutStopSec des unités de la couleur (5 min), avec une marge pour fermer.
# Garde : tests/deploy/test_vidange_sans_coupure.py.
BG_DRAIN_MAX=240
# Ce qui était codé en dur dans la bibliothèque avant qu'elle ne serve aussi une
# instance tierce (#967) — mêmes valeurs, désormais DÉCLARÉES ici.
BG_LOCK=/var/lock/oto-mcp-bluegreen-prod.lock
BG_CADDYFILE=/etc/caddy/Caddyfile
BG_DRAIN=/opt/deploy/oto-mcp-drain.sh
BG_DRAIN_UNIT=oto-mcp-drain-prod
# Notre lanceur est celui DU TAG (deploy/lanceur_secrets.py, secrets par nom sous /prod) :
# rien n'est propagé d'une couleur à l'autre. Box passée le 30/09/2026 (#967, lot 5).
BG_LANCEUR=versionne
# Le Python des couleurs (24/11/2026 : oto.cx en 3.12, `requires-python` inchangé à >=3.10).
# Chemin EXACT, au patch : l'interpréteur géré par uv posé par infra, jamais téléchargé au
# déploiement. Une montée recrée le venv de la couleur qu'elle installe s'il ne rend pas
# cette version, et refuse de démarrer une couleur qui ne la rend pas ; le retour arrière
# n'y touche pas. ⚠️ Changer de patch = poser le nouvel interpréteur PUIS changer cette
# ligne, jamais l'inverse : docs/deploiement.md § Le Python des couleurs.
BG_PYTHON=/opt/oto-mcp-python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12

. /opt/deploy/oto-mcp-bluegreen.sh

# Timer de maintenance (ADR 0065 lot 0, oto-backend#533) : il vit au niveau de
# l'HÔTE, pas de la couleur — donc UNE fois par déploiement, APRÈS la bascule, et
# jamais dans le démarrage d'une couleur (sinon les deux couleurs se disputeraient
# les mêmes lignes). On lit les unités dans l'arbre de la couleur qui vient d'être
# activée : le chemin figé /opt/oto-mcp de #533 lirait la couleur PRÉCÉDENTE.
# Ne fait JAMAIS échouer le déploiement — une maintenance manquée se rattrape,
# un service arrêté non.
# L'archive du journal suit la même pose depuis #1807 : posée à la main avant, elle visait
# /opt/oto-mcp, un arbre fixe, et tournait sur un script en retard sur le tag servi.
install_timers() {
  local tree="$1" base u
  for base in oto-mcp-maintenance oto-journal-archive; do
    for u in "$base.service" "$base.timer"; do
      if [ ! -f "$tree/deploy/$u" ]; then
        echo "$base : $u absent de $tree (tag antérieur) — ignoré"
        continue 2
      fi
      install -m 0644 "$tree/deploy/$u" "/etc/systemd/system/$u" || continue 2
    done
    # L'unité nomme l'arbre par @ARBRE@ (elle passe par le lanceur, qui est dans le tag).
    sed -i "s|@ARBRE@|$tree|g" "/etc/systemd/system/$base.service" || continue
    systemctl daemon-reload || continue
    systemctl enable --now "$base.timer" || continue
    echo "$base : timer actif, prochain tir $(systemctl show -p NextElapseUSecRealtime --value "$base.timer")"
  done
  return 0
}

if bg_run "$REF"; then
  install_timers "$(bg_tree "$(bg_active)")"
else
  exit 1
fi
