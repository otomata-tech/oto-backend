---
title: Le verrou des dépendances — une coordonnée, un jeu installé
type: explanation
---

# Le verrou des dépendances — une coordonnée, un jeu installé

> **Le fait à retenir** : ce qui s'installe, c'est `uv.lock`, versionné. Le manifeste
> (`pyproject.toml`) dit ce que le code **accepte** ; le verrou dit ce qui est
> **installé**, identique en CI et dans chaque arbre de la box. `GET /api/version` dit si c'est le cas (`deps_conformes`).

## Le problème (#932)

Mesuré le 11/09 puis le 30/09/2026 : quatre arbres de déploiement, quatre jeux de
dépendances différents (mcp 1.27.2 sur la production active, 1.29.1 sur la couleur en
attente). Trois causes, une seule racine — **aucun jeu n'était décidé** :

- le déploiement faisait `pip install -e .` dans un venv existant, et pip ne met jamais à
  jour une dépendance déjà satisfaite : chaque couleur gardait ce qu'elle avait résolu le
  jour de sa création, et une bascule ordinaire changeait le SDK MCP servi sans commit ;
- `uv.lock` existait mais était gitignoré : personne ne le lisait ;
- la CI résolvait à neuf à chaque run (`pip install -e ".[dev]"`) : elle testait plus
  récent que ce qui sert. Une CI verte ne disait rien de ce qui tourne.

## Ce qui est en place

| où | comment |
|---|---|
| `uv.lock` | Versionné. **Premier verrou = le jeu que servait la production active** (v1.404.0, relevé par `pip freeze --all`) : passer au verrou ne change aucune version. |
| CI (`ci.yml`, `couverture.yml`, et `deploy-cible.yml` pour dériver le contrat du tag) | `uv sync --frozen [--extra dev]`, par l'action `.github/actions/installer-par-le-verrou` — la version d'uv (celle de la box) y vit à un seul endroit. Le job `test` refuse un verrou en retard sur le manifeste (`uv lock --check`). `garde-plancher-python` installe le verrou sous 3.10, l'interpréteur de la box. |
| hebdomadaire (`verrou-hebdo.yml`) | `uv lock --upgrade` puis la suite entière : l'alerte précoce que l'ancienne CI donnait par accident. Il ne modifie rien ; rouge = une montée casserait, le résumé nomme les versions qui bougeraient. |
| `GET /api/version` | `deps_sha`, `lock_sha`, `deps_conformes`, relevés au démarrage (`docs/version-servie.md`). |
| déploiement (`bg_install`) | `UV_PYTHON_DOWNLOADS=never uv sync --frozen` dans l'arbre de la couleur, à la place de `pip install -e .` et du force-reinstall d'oto-core ; `uv` est celui du `PATH` de la machine, `.venv/bin/python` reste l'interpréteur ; sur notre box, `--python "$BG_PYTHON"` tient le venv à l'interpréteur des couleurs (`deploiement.md` § Le Python des couleurs). Un tag sans `uv.lock` (antérieur à #932) est **refusé**, rien n'est installé. Le journal dit « installé par le verrou : uv.lock <sha12> ». Vaut pour nos deux environnements et chaque cible (même bibliothèque). ⚠️ Le script de la box (`/opt/deploy/oto-mcp-bluegreen.sh`) est posé par infra depuis ce dépôt, pas par le déploiement. |

## Les gestes

- **Changer le manifeste** (dépendance, borne, **pin oto-core**) : éditer `pyproject.toml`,
  lancer `uv lock`, versionner les deux **dans le même commit**. `uv lock` ne monte que ce
  que le changement exige ; les autres versions restent.
- **Monter une dépendance** : `uv lock --upgrade-package <nom>` — un commit daté, qui dit
  pourquoi. Ainsi `mcp` 1.28.1 → 1.30.0 (25/11/2026, CVE-2026-59951 et quatre autres
  avis) : `mcp` est désormais déclarée (`>=1.30.0,<2.0`), et ses défauts de transport de
  1.30 (corps 4 MiB, inactivité 30 min, 10 000 sessions) sont posés explicitement au
  démarrage par `oto_mcp/borne_session.py` (`plafonner`), cf. `docs/monitoring.md`.
- **Installer un poste** : `uv sync --frozen --extra dev`.
- **Une PR venue d'un fork** : le mainteneur qui bumpe le pin sur le tronc
  (procédure des PR de fork, `docs/contributions-forks.md` du méta-dépôt) relance `uv lock` dans le même commit.

## Ce que l'installation exacte retire

`uv sync --frozen` est **exact** : un paquet que le verrou ne prescrit pas est retiré.
Relevé sur le venv de la production (v1.404.0), hors verrou et importé par aucun module
du dépôt (`oto_mcp`, `deploy`, `scripts`, `tests`) :

- `oto-cli` (CLI locale, installée à la main en editable) et ce qu'elle seule tirait :
  `typer`, `shellingham`, `annotated-doc`, et l'extra `stock` d'oto-core (`duckdb`,
  `pandas`, `pyarrow`, `numpy`, `pytz`, `tzdata`) ;
- `otomata-calllog` — la bibliothèque est inlinée dans `oto_mcp/calllog.py` depuis le
  23/07/2026 ;
- `o-browser` (reste de l'extra `browser`, retiré le 01/09), `stripe` ;
- `py-spy` — un outil d'exploitation (`docs/event-loop-perf.md`), pas une dépendance :
  il s'installe hors du venv du service.

`pip`, `setuptools` et `wheel`, posés par `python -m venv`, ne sont **pas** retirés par
`uv sync` (mesuré, uv 0.12.19). Tant que le verrou ne les prescrit pas, ils ne comptent
ni dans `deps_conformes` ni dans `deps_sha` : deux couleurs au même verrou servent le
même `deps_sha`, quelle que soit la date d'amorçage de leur venv.
