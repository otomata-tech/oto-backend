#!/usr/bin/env bash
# Ramène le contrat ÉPINGLÉ par UN consommateur déclaré de notre API (#966) — un seul
# fichier, depuis son dépôt. Appelé par les deux jobs de contrat de
# `.github/workflows/deploy-canari.yml`, une fois par entrée de
# `.github/contrat-consommateurs.json`.
#
# Usage : lire-contrat-consommateur.sh <nom> <dépôt owner/repo> <chemin> <secret|"">
# Env   : CLE_LECTURE  la clé de déploiement en lecture seule (vide pour un dépôt public)
# Une PR venue d'un fork n'appelle pas ce script pour un consommateur à secret : le job
# `consommateurs` le retire de la matrice et l'annonce (GitHub n'y transmet aucun secret).
# Sorties (GITHUB_OUTPUT) : travail=<dossier temporaire>, lu=oui|non, contrat=<fichier>
#
# TROIS états, pas deux — et la distinction EST le sujet du contrat. Ce mécanisme existe
# parce qu'un geste de l'un rougissait la branche de l'autre. Rougir ICI parce que le
# fichier a bougé CHEZ LUI, ce serait le même défaut retourné. Donc :
# - un secret DÉCLARÉ mais absent = un défaut CHEZ NOUS → rouge, jamais un vert muet (#823) ;
# - tout ce qui nous empêche d'OBTENIR son contrat (dépôt illisible, fichier déplacé)
#   → avertissement bruyant, lu=non ;
# - seul le CONTENU de son contrat peut nous faire rougir (étape suivante du job).
set -euo pipefail
NOM="$1" DEPOT="$2" CHEMIN="$3" SECRET="$4"

if [ -n "$SECRET" ] && [ -z "${CLE_LECTURE:-}" ]; then
  echo "::error title=Contrat de $NOM NON JUGÉ::Le secret $SECRET est absent — le contrat épinglé par $NOM N'A PAS été confronté. Reposer le secret du dépôt, puis relancer."
  exit 1
fi

TRAVAIL="$(mktemp -d)"
echo "travail=$TRAVAIL" >> "$GITHUB_OUTPUT"
CONTRAT="$TRAVAIL/depot/$CHEMIN"
echo "contrat=$CONTRAT" >> "$GITHUB_OUTPUT"

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
  echo "::warning title=Contrat de $NOM inaccessible::Le dépôt $DEPOT n'a pas pu être lu (clé révoquée, dépôt renommé, déplacé ou devenu privé) — le contrat N'A PAS été confronté, AUCUN jugement n'a été rendu. Détail : $(tr '\n' ' ' < "$TRAVAIL/err" | head -c 300)"
  echo "lu=non" >> "$GITHUB_OUTPUT"
  exit 0
fi
git -C "$TRAVAIL/depot" sparse-checkout set --no-cone "/$CHEMIN" || true
if [ ! -s "$CONTRAT" ]; then
  echo "::warning title=Contrat de $NOM introuvable::$CHEMIN est absent ou vide dans $DEPOT — le fichier a probablement été renommé ou déplacé CHEZ LUI. Ce n'est pas un défaut de notre contrat : rien n'a été jugé. Corriger son chemin dans .github/contrat-consommateurs.json."
  echo "lu=non" >> "$GITHUB_OUTPUT"
  exit 0
fi
echo "lu=oui" >> "$GITHUB_OUTPUT"
