#!/usr/bin/env bash
# Ramène le contrat ÉPINGLÉ par UN consommateur déclaré de notre API (#966) — un seul
# fichier, depuis son dépôt. Appelé par les deux jobs de contrat de
# `.github/workflows/deploy-canari.yml`, une fois par entrée de
# `.github/contrat-consommateurs.json`.
#
# Usage : lire-contrat-consommateur.sh <nom> <dépôt owner/repo> <chemin> <secret|"">
# Env   : CLE_LECTURE  la clé de déploiement en lecture seule (vide pour un dépôt public)
#         DEPOT_REF, CHEMIN_REF  (facultatifs) les NOMS des secrets qui portent le dépôt
#         et le chemin d'un consommateur à référence INDIRECTE : leurs valeurs arrivent
#         en <dépôt> et <chemin>, et ne s'écrivent jamais dans un message (voir plus bas).
# Une PR venue d'un fork n'appelle pas ce script pour un consommateur à secret : le job
# `consommateurs` le retire de la matrice et l'annonce (GitHub n'y transmet aucun secret).
# Sorties (GITHUB_OUTPUT) : travail=<dossier temporaire>, lu=oui|non, et si lu=oui
# contrat=<fichier> — une copie à un nom neutre, jamais le chemin chez le consommateur.
#
# TROIS états, pas deux — et la distinction EST le sujet du contrat. Ce mécanisme existe
# parce qu'un geste de l'un rougissait la branche de l'autre. Rougir ICI parce que le
# fichier a bougé CHEZ LUI, ce serait le même défaut retourné. Donc :
# - un secret DÉCLARÉ mais absent = un défaut CHEZ NOUS → rouge, jamais un vert muet (#823) ;
# - tout ce qui nous empêche d'OBTENIR son contrat (dépôt illisible, fichier déplacé)
#   → avertissement bruyant, lu=non ;
# - seul le CONTENU de son contrat peut nous faire rougir (étape suivante du job).
#
# RÉFÉRENCE INDIRECTE. Un consommateur dont le dépôt est privé ET dont le nom ne doit
# paraître nulle part (ce dépôt est public, ses journaux d'Actions aussi) se déclare par
# `depot_ref`/`chemin_ref` : des noms de SECRETS, pas de variables — GitHub imprime l'`env:`
# de chaque step en tête de son journal et ne masque d'office que les secrets
# (même règle que `deploy-cible.yml`). Ici, avant tout autre geste, le dépôt, son nom de
# dépôt seul et le chemin sont masqués à leur tour (`::add-mask::`), et chaque message dit
# « le dépôt de <nom> », « son contrat », jamais la valeur.
set -euo pipefail
NOM="$1" DEPOT="$2" CHEMIN="$3" SECRET="$4"
DEPOT_REF="${DEPOT_REF:-}" CHEMIN_REF="${CHEMIN_REF:-}"

if [ -n "$DEPOT_REF" ] || [ -n "$CHEMIN_REF" ]; then
  # Masquer AVANT tout echo, clone ou sparse-checkout : le masque ne couvre que la suite.
  [ -n "$DEPOT" ] && { echo "::add-mask::$DEPOT"; echo "::add-mask::${DEPOT#*/}"; }
  [ -n "$CHEMIN" ] && echo "::add-mask::$CHEMIN"
  INDIRECT=1 DIT_DEPOT="le dépôt de $NOM" DIT_CHEMIN="son contrat"
else
  INDIRECT=0 DIT_DEPOT="$DEPOT" DIT_CHEMIN="$CHEMIN"
fi

# Un secret déclaré mais absent : rouge NOMMÉ, par le nom du secret seulement (#823).
absents=()
if [ -n "$SECRET" ] && [ -z "${CLE_LECTURE:-}" ]; then absents+=("$SECRET"); fi
if [ "$INDIRECT" = 1 ]; then
  [ -n "$DEPOT_REF" ] && [ -n "$CHEMIN_REF" ] && [ -n "$SECRET" ] || {
    echo "::error title=Contrat de $NOM NON JUGÉ — déclaration incomplète::une référence indirecte exige depot_ref, chemin_ref et secret."
    exit 1
  }
  [ -z "$DEPOT" ] && absents+=("$DEPOT_REF")
  [ -z "$CHEMIN" ] && absents+=("$CHEMIN_REF")
fi
if [ "${#absents[@]}" -gt 0 ]; then
  echo "::error title=Contrat de $NOM NON JUGÉ — secret déclaré absent::Secret(s) absent(s) : ${absents[*]} — le contrat épinglé par $NOM N'A PAS été confronté. Reposer le secret du dépôt, puis relancer."
  exit 1
fi
# Une valeur tirée d'un secret n'est pas passée par le filtre de la liste : même forme
# exigée ici, refus sans citer la valeur.
if [ "$INDIRECT" = 1 ]; then
  [[ "$DEPOT" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || {
    echo "::error title=Contrat de $NOM NON JUGÉ — référence mal formée::le secret $DEPOT_REF n'a pas la forme owner/repo."; exit 1; }
  [[ "$CHEMIN" =~ ^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$ ]] || {
    echo "::error title=Contrat de $NOM NON JUGÉ — référence mal formée::le secret $CHEMIN_REF n'est pas un chemin relatif."; exit 1; }
fi

TRAVAIL="$(mktemp -d)"
echo "travail=$TRAVAIL" >> "$GITHUB_OUTPUT"
CONTRAT="$TRAVAIL/depot/$CHEMIN"

if [ -n "$SECRET" ]; then
  # Dépôt privé : clé de déploiement en LECTURE SEULE, sur ce seul dépôt.
  umask 077
  printf '%s\n' "$CLE_LECTURE" > "$TRAVAIL/cle"
  # Empreinte ed25519 de github.com, épinglée : relevée et vérifiée le 31/08/2026
  # (SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU). Épinglée et non découverte
  # au vol : un known_hosts rempli à l'exécution accepte le premier venu.
  echo 'github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl' > "$TRAVAIL/hotes"
  export GIT_SSH_COMMAND="ssh -i $TRAVAIL/cle -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$TRAVAIL/hotes"
  URL="git@github.com:$DEPOT.git"
else
  # Dépôt public : lu sans clé. S'il devient privé, la lecture échoue et le dit.
  URL="https://github.com/$DEPOT.git"
fi

# Un seul fichier ramené.
if ! git clone --depth 1 --filter=blob:none --sparse --quiet "$URL" "$TRAVAIL/depot" 2>"$TRAVAIL/err"; then
  detail="$(tr '\n' ' ' < "$TRAVAIL/err" | head -c 300)"
  if [ "$INDIRECT" = 1 ]; then
    # Le détail de git cite l'URL : le dépôt et son nom seul en sont retirés (en plus du masque).
    detail="${detail//"$DEPOT"/<dépôt>}"
    detail="${detail//"${DEPOT#*/}"/<dépôt>}"
  fi
  echo "::warning title=Contrat de $NOM inaccessible::$DIT_DEPOT n'a pas pu être lu (clé révoquée, dépôt renommé, déplacé ou devenu privé) — le contrat N'A PAS été confronté, AUCUN jugement n'a été rendu. Détail : $detail"
  echo "lu=non" >> "$GITHUB_OUTPUT"
  exit 0
fi
git -C "$TRAVAIL/depot" sparse-checkout set --no-cone "/$CHEMIN" >/dev/null 2>&1 || true
if [ ! -s "$CONTRAT" ]; then
  echo "::warning title=Contrat de $NOM introuvable::$DIT_CHEMIN est absent ou vide dans $DIT_DEPOT — le fichier a probablement été renommé ou déplacé CHEZ LUI. Ce n'est pas un défaut de notre contrat : rien n'a été jugé. Corriger son chemin (dans .github/contrat-consommateurs.json, ou dans le secret qui le porte)."
  echo "lu=non" >> "$GITHUB_OUTPUT"
  exit 0
fi
# Le fichier jugé sous un nom neutre : la sortie `contrat` est réimprimée dans l'`env:`
# des étapes suivantes, elle ne porte donc jamais le chemin chez le consommateur.
cp "$CONTRAT" "$TRAVAIL/contrat.json"
echo "contrat=$TRAVAIL/contrat.json" >> "$GITHUB_OUTPUT"
echo "lu=oui" >> "$GITHUB_OUTPUT"
