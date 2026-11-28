# Ferme de recettes — l'agent

Le processus du bac à sable des recettes-code (Deno + Pyodide) tourne sur une box dédiée,
pas sur la box de l'API. Le serveur garde le pont, le budget et toutes les gardes des
appels d'outils (`oto_mcp/functions/pont.py`, `oto_mcp/recipes/pont.py`) ; cet agent ne
fait que lancer le processus et transporter ses lignes. **La box n'a aucun secret de
production** : ni base, ni coffre, ni clé de connecteur — seulement le secret partagé
avec le serveur qui l'appelle.

Protocole : `oto_mcp/functions/distant.py`. Côté serveur : `OTO_FUNCTIONS_FERME`
(`hôte:port`) et `OTO_FUNCTIONS_FERME_SECRET` ; non posée, le bac à sable reste local.

## Ce que la box porte

- une copie de `ferme_recettes/` et de `oto_mcp/functions/` (bibliothèque standard
  seulement), DU MÊME TAG que le serveur : l'empreinte (`executor.empreinte()`) doit être
  la même des deux côtés, sinon l'agent refuse (le serveur rend 429 `sandbox_busy`) ;
- le répertoire du bac à sable (`deno`, `deno_dir/`, `wheels/`) posé par
  `scripts/installer_bac_a_sable.py`, en lecture seule pour l'utilisateur de l'agent ;
- l'isolement du processus (`OTO_FUNCTIONS_ISOLATION`, `systemd` par défaut :
  utilisateur dédié, sans réseau, mémoire et durée bornées).

## Réglages (environnement de l'unité)

| Variable | Rôle | Défaut |
| --- | --- | --- |
| `FERME_RECETTES_HOTE` | adresse PRIVÉE d'écoute (jamais toutes les interfaces) | — (exigée) |
| `FERME_RECETTES_PORT` | port | 8790 |
| `FERME_RECETTES_SECRET` | secret partagé (≥ 32 caractères), comparé en temps constant | — (exigé) |
| `FERME_RECETTES_PLACES` | processus simultanés | 2 |
| `FERME_RECETTES_ATTENTE_S` | attente d'une place avant le refus `busy` | 20 |
| `FERME_RECETTES_DUREE_MAX_S` | durée maximale d'un processus (sa location de place) | 900 |
| `OTO_FUNCTIONS_SANDBOX_DIR` | répertoire du bac à sable | — |
| `OTO_FUNCTIONS_ISOLATION` | `systemd` \| `bwrap` \| `direct` (dev seulement) | `systemd` |

Dimensionner `FERME_RECETTES_PLACES` sur la mémoire : un processus en cours tient
environ 200 Mo mesurés (recette de calcul pur), plafonné à 1 Go par l'isolement.

Unité : `oto-ferme-recettes.service` (gabarit à côté).
