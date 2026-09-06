---
title: Commandes
type: how-to
description: >-
  Recettes d'exécution du backend oto : lancer les tests (le venv local n'a pas
  pytest — recette exacte), tester un clone sans réinstaller les deps et ses deux
  pièges d'import, RECONNAÎTRE le faux rouge du venv partagé en retard sur le pin
  oto-core (une grappe de rouges « No module named 'oto.tools.<connecteur>' » qui
  n'est PAS ton lot — §Pin oto-core), déployer (push main = preprod, tag v* = prod),
  lire les logs, inspecter la base managée, et les gotchas de registre d'outils hors
  serveur. À charger avant toute exécution de test, de déploiement ou d'inspection
  de prod.
---

```bash
# Transport stdio RETIRÉ (2026-06-13) : oto-mcp ne se sert qu'en streamable_http
# (toujours authentifié Logto). Usage local = CLI `oto`. Pour un serveur local,
# lancer en http avec les LOGTO_* et taper avec un bearer.

# Tests — le venv .venv N'A PAS pytest (extra `dev` non installé) et `uv run pytest`
# crée un env éphémère SANS les deps projet (piège, ModuleNotFoundError). Recette :
uv pip install --python .venv/bin/python "pytest>=8.0" "pytest-asyncio>=0.24"
.venv/bin/python -m pytest -q
# ⚠️ **`/data/oto/backend/.venv` est PARTAGÉ entre N sessions parallèles** — ce qu'on y
# installe, on l'installe chez les voisines, au milieu de leurs runs. Poser pytest est le
# SEUL geste tolérable : additif, et il ne touche pas oto-core.
# **Y réinstaller oto-core (`pip install --force-reinstall …`) est INTERDIT ici** : la
# commande est juste sur un venv qu'on possède SEUL (venv jeté d'un scratchpad, runner CI,
# poste mono-session) et fausse sur celui-ci, où elle règle son propre faux rouge en le
# déplaçant chez les voisines. Ce qu'il faut faire à la place : §Pin oto-core → « Faux
# rouge ». ⚠️ **Toute recette qui écrit dans un environnement doit dire LEQUEL elle
# suppose** — sans cette phrase, elle est fausse pour la moitié de ceux qui la lisent, et
# ils la suivront justement PARCE QU'elle est documentée (vécu le 01/09).

# Tester un CLONE (clone scratchpad, ou `git archive <commit>` pour isoler un commit du WIP
# voisin du tree partagé) SANS réinstaller les deps : réutiliser le venv local (deps+pytest
# présents) en résolvant `oto_mcp` depuis le clone.
#   cd <clone> && PYTHONPATH=<clone> OTO_CONFIG_DISABLE_SOPS=1 \
#     /data/oto/backend/.venv/bin/python -m pytest -q tests/...
#
# ⚠️ **Le `cd <clone>` n'est PAS cosmétique : c'est LUI qui fait marcher la recette.**
# `PYTHONPATH` seul NE PRIME PAS sur l'editable install — son finder vit dans `sys.meta_path`,
# consulté AVANT `sys.path`. Lancé depuis `/data/oto/backend`, le même PYTHONPATH importe
# donc `/data/oto/backend/oto_mcp` : on croit tester le clone, on teste le tree partagé.
# Le mode d'échec est un **faux négatif silencieux** — vécu 11/08, un agent a conclu « le
# code d'avant passe déjà mes 16 tests » en testant en réalité son propre correctif, ce qui
# invalide la seule chose que le clone servait à prouver.
# **Valider l'instrument avant d'en tirer une conclusion**, une ligne suffit :
#   cd <clone> && PYTHONPATH=<clone> /data/oto/backend/.venv/bin/python -c \
#     "import oto_mcp.db.search as m; print(m.__file__)"   # doit pointer DANS le clone
#
# ⚠️ **2e piège (12/08) : la validation ci-dessus ne couvre PAS un fichier NEUF.** Si ton
# lot CRÉE un module (ex. `grants_chain.py`), le finder editable le sert depuis
# /data/oto/backend même avec le `cd` — le clone de HEAD ne l'a pas, l'import retombe sur
# le tree partagé, et la ligne de validation ne l'attrape pas (elle teste un module qui
# existe des deux côtés). Le test « rouge sur le code d'avant » devient alors un mensonge.
# Parade : un `sitecustomize.py` dans le clone qui retire les finders editable :
#   import sys; sys.meta_path = [f for f in sys.meta_path
#                                if "__editable__" not in type(f).__module__]
# Puis re-valider en important LE MODULE NEUF : il doit lever ImportError dans le clone.
# ⚠️ **3e piège (26/08, revécu en masse le 01/09) : le venv porte une COPIE FIGÉE
# d'oto-core.** `.venv` a une version INSTALLÉE de la lib, pas un lien vers le checkout —
# elle ne bouge donc pas quand le tronc bump son pin. Le mode d'échec va dans les DEUX sens :
#   • trop VIEUX → `ModuleNotFoundError: No module named 'oto.tools.<neuf>'` en masse
#     (48 échecs + 33 erreurs le 26/08 sur airtable/tavily/waalaxy ; 28 rouges le 01/09 sur
#     tally/lemlist) : un faux ROUGE qu'on impute au tronc ou à son propre lot, alors que
#     rien n'est cassé ;
#   • trop RÉCENT (ou PYTHONPATH sur un checkout en avance) → un vert local sur des méthodes
#     que le pin du tronc n'a PAS : faux VERT, et la garde version-skew le rattrape en CI.
# Parade : **NE PAS réaligner `/data/oto/backend/.venv` ni `git pull` `/data/oto/oto-core`**
# — les deux sont PARTAGÉS entre N sessions //, les muter casse le WIP des voisines. On
# clone oto-core à part AU TAG ÉPINGLÉ. Recette complète, la trace exacte à reconnaître et
# la ligne qui tranche : **§Pin oto-core → « Faux rouge : le venv partagé est en retard
# sur le pin »**, en bas de ce fichier.

# Tests À BASE (fixture `pg_dsn`) : elle prend `OTO_TEST_PG_DSN` s'il existe, sinon monte
# un PostgreSQL JETABLE via docker — étiqueté `oto-test=1`, `PGDATA` en tmpfs (aucun
# volume), retiré au finalizer ET sur atexit/SIGTERM/SIGINT ; chaque session balaie
# d'abord les conteneurs étiquetés de plus de 2 h (#640, `tests/_pg_hygiene.py`). Un
# `oto-test-pg-*` de plus d'une heure est un orphelin : `docker rm -f -v` (sans `-v`
# le volume anonyme reste — c'était la fuite du chemin normal). Sans
# l'un ni l'autre, ces tests sont **SKIPPÉS** — et un vert local sans base ne vaut RIEN
# contre la CI qui en a une (tronc cassé une heure ainsi le 23/08). Vérifier le compte de
# skips : `pytest -q -rs` ne doit montrer AUCUN skip motivé par l'absence de PostgreSQL.

# Convention : tester la LOGIQUE PURE (helpers hors DB, ex. `effective_for_group`,
# `_connector_blocked`/seams) + les gardes de capacité par stub ; le chemin SQL est vérifié
# au déploiement (le job `test` du CI tourne le vrai suite avec toutes les deps).

# ── AVANT DE POUSSER : l'arbre du COMMIT s'importe-t-il ? ────────────────────
# Une référence poussée SANS SON OBJET (`from . import x` commité, `x.py` jamais
# `git add`é) est invisible pour son auteur et visible pour tous les autres : son
# répertoire de travail complète le commit, donc chez lui tout s'importe. Vécu le
# 02/09/2026 — toute la suite échouait à la COLLECTE, préproduction sautée, plus rien
# ne pouvait partir en prod. La classe NAÎT du staging sélectif, qu'on impose pourtant
# pour protéger le WIP des sessions voisines : rien, au moment du commit, ne dit qu'on
# vient de pousser un import sans son fichier.
.venv/bin/python scripts/arbre-importable.py          # juge HEAD, pas le répertoire
.venv/bin/python scripts/arbre-importable.py <ref>    # juge n'importe quelle ref
# Il extrait l'arbre par `git archive`, l'importe DE LÀ, et vérifie module par module
# que ce qui entre dans `sys.modules` sort bien de cet arbre — il embarque donc la
# parade au finder editable décrite plus haut, sans `sitecustomize.py` à poser.
# Sorties : 0 = s'importe · 1 = référence sans objet (le message nomme le `git add`)
#           2 = RIEN N'A PU ÊTRE JUGÉ (jamais un vert muet).
# ⚠️ « 9 607 tests collectés chez moi » ne prouve RIEN sur le commit : c'est le disque
# qu'on mesure. Le même contrôle tourne en CI (job `arbre-importable`, sur les PR ET
# sur le tronc) et gate `deploy-preprod`.

# Deploy — modèle tronc unique (refonte 2026-07-20, ADR 0020) :
#   push `main`  → PREPROD (« Deploy preprod », deploy-canari.yml, script serveur
#                  oto-backend-canari.sh : git reset --hard origin/main → preprod)
#   tag  `v*`    → PROD    (« Deploy prod », deploy.yml, script serveur
#                  oto-backend.sh <tag> : git reset --hard <tag> → prod)
# Le deploy (les deux) = SSH box dédiée via runner self-hosted : reset au ref +
# pip install -e . + **force-reinstall oto-core depuis le tag pinné** (lu du
# pyproject ; pip saute sinon une dép VCS déjà présente) + restart + **smoke HTTP**
# (GET 200 /.well-known/oauth-authorization-server) + **rollback auto** si
# install/restart/smoke échoue. Le restart relance start-encrypted (refetch master
# key). ⚠️ start-encrypted.sh untracked → survit au git reset.
#
# Preprod = travailler sur `main`, commit, push : deploy preprod auto (gate
# `needs: test`). Claude Code (web) ouvre ses PR sur main → merge = deploy preprod.
git push origin main            # → PREPROD

# Prod = acte explicite : taguer un commit de main + pousser le tag.
git tag v1.2.3 && git push origin v1.2.3   # → PROD (tags v* immuables, ruleset)
# ⚠️ `canari` est DÉPRÉCIÉE (ne déploie plus) : un checkout encore dessus doit
# passer sur main (`git checkout main`). guard-main + sync-main-to-canari retirés.

# Logs
ssh -i ~/.ssh/<clé> root@<box> "journalctl -u oto-mcp -f"

# DB inspect (PG managed) — depuis la box (env du process inclut DATABASE_URL via .env)
# ⚠️ `psql` n'est PAS installé sur la box dédiée → passer par le venv + psycopg :
ssh -i ~/.ssh/<clé> root@<box> 'cd /opt/oto-mcp && set -a; . .env; set +a; ./.venv/bin/python -c "
import os, psycopg
with psycopg.connect(os.environ[\"DATABASE_URL\"]) as c:
    for r in c.execute(\"SELECT sub, email, role FROM users\"): print(r)
"'

# ⚠️ Même besoin pour tout script d'ENTRETIEN lancé à la main (`python -m scripts.X`) :
# il n'hérite pas de l'environnement du service systemd, donc il sort en
# « RuntimeError: DATABASE_URL not set » avant d'avoir rien fait. Sourcer d'abord :
#   cd /opt/oto-mcp && set -a && . ./.env && set +a && ./.venv/bin/python -m scripts.X
# Vécu 19/08 sur scripts.archive_empty_kb_projects (dry-run par défaut, --apply pour agir).

# ⚠️ Un script HORS SERVEUR ne voit AUCUN outil : `tool_registry.boot_tool_names()`
# rend [] tant que le registre n'est pas réchauffé (le serveur le fait au lifespan).
# Toute validation de nom d'outil renvoie alors un `unknown_tool` TROMPEUR — vécu
# 05/08, j'ai failli annoncer un blocage inexistant. Diagnostic fidèle :
#   register_all(mcp := FastMCP("x")); tool_registry.bind(mcp)
#   asyncio.run(tool_registry.warm_registry(mcp))   # → 665 outils, la validation passe

# ⚠️ Déchiffrer un credential ad-hoc : OTO_MCP_MASTER_KEY n'est PAS dans .env
# (fetchée au boot depuis Secret Manager) → recette complète + pièges (RuntimeError
# ≠ InvalidTag ; status_for = credential_status, jamais get_credential_with_meta) :
# docs/connector-vault.md §Déchiffrer un credential ad-hoc.
```

## Maintenance — les travaux qui ont quitté le boot (ADR 0065 lot 0)

```bash
# Sur la box, avec l'environnement du service (jamais une copie du .env) :
sudo systemctl start oto-mcp-maintenance.service          # la passe complète, à la main
sudo journalctl -u oto-mcp-maintenance -n 50 --no-pager   # ce qu'elle a fait, avec ses durées
systemctl list-timers oto-mcp-maintenance.timer           # le prochain tir

# Un travail seul, et d'abord À BLANC — sur une base PARTAGÉE prod/preprod, la
# première question devant une purge est « combien de lignes ? ».
sudo -E env $(cat /opt/oto-mcp/.env | xargs) \
  /opt/oto-mcp/.venv/bin/oto-mcp maintenance retention --dry-run
#   retention | blocks | key-indexes | all      les travaux du timer
#   check-boot                                  rejoue l'ORDRE du boot en transaction
#                                               ANNULÉE — un diagnostic sans effet,
#                                               jouable contre la base servie
#   key-index-rebuild                           ⚠️ #421, n'a JAMAIS tourné en prod :
#                                               l'appeler est une décision, pas une
#                                               routine (hors `all`, hors timer)

# Purge rétroactive des jetons écrits en clair dans le journal (#558). Le SEUL travail
# qui est à blanc PAR DÉFAUT : ici c'est `--apply` qui écrit, pas `--dry-run` qui
# retient. Hors `all` et hors timer — il réécrit des lignes servies aux lentilles de
# supervision, sur une base partagée prod/preprod.
sudo -E env $(cat /opt/oto-mcp/.env | xargs) \
  /opt/oto-mcp/.venv/bin/oto-mcp maintenance journal-tokens            # compte
sudo -E env $(cat /opt/oto-mcp/.env | xargs) \
  /opt/oto-mcp/.venv/bin/oto-mcp maintenance journal-tokens --apply    # écrit
```

⚠️ **Le timer n'est posé qu'en PROD**, par `deploy/oto-backend.sh` au tag (jamais à la
main, jamais en crontab) : prod et preprod partagent la base, deux exécutants se
disputeraient les mêmes lignes.

## Pin oto-core — une version déployée = une coordonnée reproductible

- **`oto-core` PINNÉ sur un tag git** (`oto-core[anonymize] @ git+…@vX.Y.Z` dans `pyproject.toml` — l'extra est `anonymize`, `browser` a été retiré côté backend et n'est resté que sur le CLI local ; plus `@main` flottant ni dép `oto-cli`) : une version déployée = coordonnée reproductible. ⚠️ **`pip` ne réinstalle PAS une dép VCS déjà présente** (`oto-core` "satisfait" quelle que soit sa version) → `pip install -e .` seul ne monte JAMAIS oto-core au tag bumpé. Le deploy **force-réinstalle** oto-core depuis le tag lu du `pyproject` (`pip install --force-reinstall …@$tag`). Bump connecteurs = tag oto-core + édit du pin + deploy (PAS de `git pull` box). Cf. ADR 0020. ⚠️ **Symptôme trompeur en LOCAL** : des tests rouges peuvent être un venv en retard sur le pin, pas du code cassé (05/08 : 17 rouges sur un connecteur neuf ; 26/08 : 48 ; 01/09 : 28) → **sous-section ci-dessous**, qui porte la trace exacte à reconnaître et la recette. (⚠️ box `otomata-0` a un VIEUX oto-mcp décommissionné/stoppé avec un editable legacy `oto-cli` pré-split — ne pas s'y fier, le runtime live est la box dédiée.) ⚠️ **Le pin est un champ que TOUTES les sessions // éditent → régressions silencieuses récurrentes** : vécu 2026-07-07, un commit concurrent a réécrit le pin `v1.18.0→v1.17.0` et **cassé un tool déployé SANS erreur** (le tool était enregistré, sa méthode absente de l'ancien oto-core → `AttributeError` seulement à l'appel). Toujours bumper en **superset** (tag haut ⊇ tags bas) ; à la moindre divergence de pin en merge/rebase, **garder la version haute**.
- **Lire ce qui est RÉELLEMENT installé, sans SSH** : `curl -s https://mcp.oto.cx/api/version` (preprod : `mcp.oto.ninja`) rend le tag du backend servi **et** `oto_core.tag` — l'installé, pas le pin. ⚠️ **Ne PAS demander à `pip show oto-core`** : son numéro est **gelé à 1.100.0** depuis que les tags ont cessé de bumper le champ `version` du manifeste (mesuré le 01/09/2026 : `1.100.0` annoncé pour un `v1.101.0` installé). La coordonnée fiable est ce que pip **écrit** à l'installation, `direct_url.json` — c'est elle que sert `/api/version`. Localement : `.venv/bin/python -c "from oto_mcp import version; print(version.oto_core())"`. Cf. `docs/version-servie.md`.

### Faux rouge : le venv partagé est en retard sur le pin

> **Depuis le 01/09/2026, la suite te le DIT — tu n'as plus à reconnaître le motif.**
> `tests/_oto_core_pin.py` compare le tag oto-core **installé** (lu dans `direct_url.json`) au
> tag **épinglé** par le manifeste, et affiche une bannière `=== PIN oto-core ===` **aux deux
> bouts du run** — au démarrage, et surtout en fin de run, contre les `FAILED`, là où on se
> demande à qui sont ces rouges. Elle **nomme les deux versions** (« installé dans ce venv :
> v1.101.0 / épinglé par pyproject : v1.103.0 ») : c'est le couple qui se comprend d'un coup,
> pas le mot « divergence ». **En local**, les tests qui portent le marqueur
> `exige_pin_oto_core` sont alors **passés comme non concluants** plutôt que rouges — un rouge
> qui ne prouve rien vaut moins qu'un test explicitement non concluant. **En CI, jamais** : la
> garde version-skew doit y rester mordante, c'est là qu'elle protège la prod.
>
> ⚠️ **La bannière ne survit pas à `| grep passed`** (#790, mesuré le 01/09/2026 : elle
> s'imprime sur des lignes à côté de celle qui contient ce mot, donc un filtre — le geste le
> plus courant sur neuf mille tests — l'avale). Depuis, `pytest_report_teststatus`
> (`conftest.py`) range ces skips à part **dans le résumé final de pytest lui-même** — la SEULE
> ligne garantie de contenir « passed ». `8924 passed, 103 skipped, …` devient
> `8924 passed, 5 skipped, 98 non concluant(s) — venv ≠ pin oto-core vX.Y.Z, …` : le nombre ne
> change pas, mais il **distingue** désormais les skips ordinaires des skips du pin, et **nomme**
> la version attendue — même lu au travers d'un `grep`.
>
> Ce qui suit reste vrai, et sert à comprendre ce que la bannière annonce.

**Reconnaître AVANT d'enquêter.** Lancée depuis `/data/oto/backend` (ou avec son `.venv`), la
suite sort une grappe de rouges concentrée sur les connecteurs **les plus récents** — jamais sur
le domaine que ton lot touche. Relevé du **01/09/2026**, redécouvert le même jour par **six
sessions** qui ont chacune payé l'enquête (une **septième** l'a repayée le soir même, en le
remontant cette fois comme « le tronc est rouge, plus aucune PR ne peut entrer » — la CI était
verte de bout en bout ; c'est cette septième qui a fait poser la bannière) :

```
FAILED tests/test_tally.py::…                                                        (22)
FAILED tests/test_lemlist_surface_coverage.py::…                                     (3)
FAILED tests/test_lemlist_tools.py::test_client_exposes_methods_called_by_campaign_tools
FAILED tests/test_tools_client_methods_exist.py::…_on_pinned_core[lemlist]
FAILED tests/test_tools_client_methods_exist.py::…_on_pinned_core[lemlist_crm]
28 failed, 8647 passed          (suite complète)
28 failed, 146 passed, 1 skipped   (les cinq fichiers seuls)
```

Au fond de la trace, l'un de ces trois messages :

- `ModuleNotFoundError: No module named 'oto.tools.tally'` — le module n'existe pas dans
  l'oto-core **installé** ;
- `AssertionError: lemlist_crm.py appelle des méthodes absentes de LemlistClient (oto-core
  épinglé) : [...] — bump le pin oto-core dans CETTE PR (version-skew, cf. leçon folk_user)` ;
- `AssertionError: exception(s) sur une méthode qui n'atteint plus l'API : ['unsubscribe_lead']`
  / `exception(s) sur un paramètre inexistant : ['get_lead.version', …]`.

⚠️ **Les deux derniers accusent la mauvaise pièce** : ils te disent « ton pin » ou « ta liste
d'exceptions », alors que ce qui manque est **le client** dans l'oto-core installé. Ils sont
écrits pour la CI, où oto-core est installé AU tag — là-bas leur accusation est juste.

⚠️ **Et le discriminant « ça dit *No module named* » ne couvre que la MOITIÉ des cas.** C'est ce
trou-là qui a fait mettre trois de ces rouges de côté comme « les vrais, distincts des faux
rouges », pendant sept enquêtes. Il n'attrape qu'un connecteur **AJOUTÉ** après la version
installée — `tally` n'existe nulle part dans un v1.101.0, donc l'import casse net et le message
est sans ambiguïté. Un connecteur **déjà présent mais RABOUGRI** échoue tout autrement : le
client `lemlist` fait **724 lignes en v1.101.0 et 2547 en v1.102.0** (le lot « exposer l'API
entière »), donc en venv périmé les tools appellent un client d'avant son élargissement et le
message devient « méthodes appelées mais absentes du client » / « exception(s) sur un paramètre
inexistant » — trait pour trait **une vraie régression de version-skew**. Ne pas trier au
message, donc, mais **compter** : 22 `tally` + 3 `lemlist_surface_coverage` + 3 autres = **28**,
le compte exact du relevé ci-dessus. Tout ce qui est dans les 28 est environnemental.

⚠️ **Corollaire, et c'est le piège coûteux** : `NON_EXPOSEES` / `PARAMETRES_NON_TRANSMIS` de
`test_lemlist_surface_coverage.py` **ne se vident pas pour faire taire ces tests**. Le test
accuse la liste, mais c'est le client qui manque : la vider rend vert tout de suite, détruit la
couverture, et redevient rouge au prochain bump. Le test est juste — il est simplement exercé
contre le mauvais oto-core.

**La phrase qui tranche : ces rouges sont PRÉEXISTANTS et ENVIRONNEMENTAUX, ton lot ne les a pas
causés.** Ne pas enquêter : **vérifier**, en rejouant sur `origin/main` **pristine** dans un clone
jeté (recette ci-dessous). Mêmes rouges sur pristine ⇒ ils ne sont pas à toi, et la CI — qui
installe au tag — passe intégralement.

**Le contrôle en deux lignes** — lire le **tag git installé**, pas le numéro de version :

```bash
grep -o '"requested_revision":"[^"]*"' \
  /data/oto/backend/.venv/lib/python3.*/site-packages/oto_core-*.dist-info/direct_url.json
grep -o 'oto-core\.git@v[0-9.]*' pyproject.toml     # les deux doivent coïncider
```

01/09 : installé `v1.101.0`, épinglé `v1.103.0` → `tally` n'existe nulle part dans le venv, et
`lemlist` y est une version d'avant sa surface complète. ⚠️ **`pip show oto-core` et le nom du
`dist-info` MENTENT ici** : le champ `version` d'oto-core est resté à `1.100.0` de v1.101.0 à
v1.103.0, donc l'instrument affiche le même numéro pour l'installé périmé et pour le bon —
un instrument qui ne peut pas voir l'écart qu'on lui demande de mesurer. Seul
`requested_revision` est la coordonnée.

**La recette qui marche — deux clones jetés, zéro écriture sur du partagé.**
⚠️ **Ne PAS force-réinstaller oto-core dans `/data/oto/backend/.venv`, ne PAS `git pull`
`/data/oto/oto-core`** : les deux sont utilisés **en même temps par N sessions parallèles**, les
muter casse le WIP des voisines. Ces deux commandes ne sont pas fausses en soi — elles
supposent un environnement qu'on possède **seul** (venv jeté d'un scratchpad, runner CI, poste
mono-session), et c'est cette hypothèse jamais écrite qui les rend dangereuses ici : elles
règlent le faux rouge de celui qui les lance en le déplaçant chez ses voisines. Le 01/09, une
session les a reprises de bonne foi **parce qu'elles étaient documentées** — d'où leur retrait de
ce fichier, et cette phrase à leur place. Et un checkout en AVANCE sur le pin fabrique le faux VERT
symétrique (vert local sur des méthodes que le tronc n'épingle pas, rattrapé en CI par la garde
version-skew).

```bash
SP=<ton scratchpad>                        # jamais /data/oto/*
git clone git@github.com:otomata-tech/oto-backend.git "$SP/bk"     # origin/main pristine
git clone git@github.com:otomata-tech/oto-core.git    "$SP/core"
TAG=$(grep -o 'oto-core\.git@v[0-9.]*' "$SP/bk/pyproject.toml" | cut -d@ -f2)
git -C "$SP/core" checkout "$TAG"          # oto-core AU tag que CE tronc épingle

cd "$SP/bk"                                # le `cd` n'est pas cosmétique (cf. §Tests)
export PYTHONPATH="$SP/core:$SP/bk"        # `oto` est un namespace package : PYTHONPATH
                                           # prime sur le site-packages du venv
export OTO_CONFIG_DISABLE_SOPS=1
/data/oto/backend/.venv/bin/python -m pytest -q > "$SP/pristine.txt" 2>&1
```

Le venv partagé ne sert que de **fournisseur de dépendances tierces et de pytest, en lecture
seule** — on ne lui installe rien. **Valider l'instrument AVANT de conclure**, les deux chemins
doivent pointer dans les clones :

```bash
/data/oto/backend/.venv/bin/python -c \
  "import oto_mcp, oto.tools as t; print(oto_mcp.__file__); print(t.__path__)"
```

Éprouvé le 01/09/2026 : les 28 rouges disparaissent et `origin/main` sort **8676 passed,
1 skipped, 3 xfailed** en 3 min 20. Ton lot se rejoue ensuite dans le même clone
(`git fetch && git checkout <ta branche>`), même commande — la comparaison est alors propre.

**Comparer les LISTES d'échecs, jamais les nombres.** Deux comptes égaux peuvent recouvrir deux
ensembles différents ; le compte de `passed` est le témoin le plus lisible, mais il ne remplace
pas le diff :

```bash
grep '^FAILED' "$SP/pristine.txt" | sort > "$SP/a"; grep '^FAILED' "$SP/lot.txt" | sort > "$SP/b"
diff "$SP/a" "$SP/b"        # vide = aucune régression, quel que soit le compte
```

⚠️ **Et ne JAMAIS mesurer à travers un `| tail -N`** : la liste des `FAILED` est tronquée pendant
que le résumé, lui, annonce le vrai total. Vécu le 01/09 — 24 lignes capturées pour un résumé à
28, ce qui se lit « moins d'échecs qu'avant », soit l'inverse d'une régression : le mensonge
confortable, celui qu'on ne va pas questionner. Rediriger la sortie **entière** dans un fichier,
puis filtrer.

⚠️ **Ces chiffres sont datés et bougeront au prochain bump du pin.** Ce qui ne bouge pas, c'est la
forme : une grappe de rouges sur les connecteurs les plus récents, hors du domaine de ton lot,
avec `No module named 'oto.tools.<connecteur>'` au fond ⇒ contrôle `requested_revision` vs pin,
puis pristine.
