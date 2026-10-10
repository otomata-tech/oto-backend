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
# La CI lance la MÊME commande : au tag, sans chemin (`pytest -q -n 4`, suite complète) ;
# au push sur `main`, suivie des fichiers que retient `scripts/selection_tests.py` (volets
# touchés + socle, `tests/volets.toml`, #1185) — le même script se lance à la main :
# `python3 scripts/selection_tests.py --base origin/main --tete HEAD`. Globber (`tests/*.py tests/*/`)
# reste permis (#508) : `tests/conftest.py` passé en argument est remplacé par un nœud vide
# (`pytest_collect_file`, tests/conftest.py) — sans lui, `import file mismatch` (trois
# `conftest.py` partagent le basename) interrompait toute la collecte.
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
#   cd <clone> && PYTHONPATH=<clone> \
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
# un PostgreSQL JETABLE via docker (sur un POSTE ; la CI installe PostgreSQL sur le runner,
# §Suite parallèle en CI) — étiqueté `oto-test=1`, `PGDATA` en tmpfs (aucun
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

# ── Suite parallèle en CI : des PARTS sur plusieurs runners (#1111) ─────────
# La suite ne tourne plus sur UN runner (`-n 4`, ~14 min le 08/10) mais répartie en N
# parts, une par runner : `.github/workflows/suite-tests.yml` (réutilisable, `workflow_call`,
# entrées `cible` — chemins de tests, vide = suite complète — et `ref`). Appelé au push par
# deploy-canari.yml (cible = la sélection #1185) et au tag par deploy.yml (cible vide, ref
# du tag validée par le job `tag`). Jobs :
#   `plan`      → N et la répartition, depuis les DURÉES mesurées par fichier
#                 (`scripts/durees_suite.json`, LPT, déterministe ; ~120 s de cas par
#                 worker, plafond 8 → 7 parts pour la suite complète sur 4 cœurs) ;
#   `part`      → la matrice : ses fichiers RECALCULÉS DEPUIS LE DISQUE (un fichier absent
#                 des durées tombe quand même dans une part, à la médiane), sa collecte
#                 mesurée, son `-n` dérivé, `pytest -n … --dist loadgroup --junitxml` ;
#                 sa base = PostgreSQL 17 + pgvector INSTALLÉS SUR LE RUNNER (paquets PGDG,
#                 ~30 s), servis par `OTO_TEST_PG_DSN` — AUCUNE image docker en CI depuis le
#                 09/10/2026 (Docker Hub « toomanyrequests » sur l'IP partagée des runners,
#                 sept parts rouges, aucun test en cause) ; les postes gardent docker ;
#   `reference` → la collecte de toujours (`pytest` sans chemin, ou la cible) + `uv lock --check` ;
#   `verdict`   → l'AGRÉGATEUR : union des collectes des parts = référence, sans doublon,
#                 sinon ROUGE (un test perdu ne laisse pas la CI verte et aveugle) ; fusionne
#                 les `junit-shard-<i>` en `junit-suite` (même sur un rouge : le constat
#                 #1185 de deploy.yml le lit) ; publie `suite-durees` sur une suite complète verte.
# Côté appelant, `test` n'est plus qu'un job simple (`needs: [suite]`, `if: always()`) qui
# rougit sauf si la suite a conclu `success` : un job appelé par `uses:` s'affiche
# « suite / … », or la garde de mise en prod et le script de bascule lisent un job nommé
# EXACTEMENT `test`. Le script : `scripts/repartir_suite.py` (`plan`, `part`, `reference`,
# `verifier`, `mesurer`, `fusionner`), bancs `tests/test_repartir_suite_1111.py`.
#
# Régénérer les durées (après un run COMPLET et vert — tag, ou push à sélection complète) :
#   gh run download <run_id> --repo otomata-tech/oto-backend -n suite-durees -D /tmp/durees
#   cp /tmp/durees/durees_suite.json scripts/durees_suite.json   # puis commit
# Le résumé du job `verdict` dit de combien les durées ont dérivé du fichier versionné.
# Le fichier du 08/10 est une AMORCE (mesure locale × 2,65, calée sur un run CI).
#
# ⚠️ Artefacts à nom fixe (`suite-part-<i>`, `junit-shard-<i>`, `suite-reference`,
# `junit-suite`, `suite-durees`) : un MÊME run qui appellerait deux fois `suite-tests.yml`
# les ferait entrer en collision (`overwrite: true` : le second écraserait le premier, et
# le verdict jugerait un mélange). Un seul appel par run, ou préfixer les noms par une entrée.
#
# Le nombre de workers N'EST PAS deviné, ni recopié : chaque part mesure le pic de SA
# collecte et lit `MemAvailable` de SA machine, puis applique la règle d'`empreinte-collecte`
# (marge 20 %, un process pour le contrôleur, borné par les cœurs — 4 sur un runner 4 vCPU,
# la mémoire n'étant pas le facteur limitant). C'est la leçon du 07/09 (quatre workers
# calibrés sur un poste à 28 cœurs → OOM killer, tronc rouge 6 h) appliquée : ne pas
# recopier un chiffre d'ailleurs, le lire sur CETTE machine.
#
# **`--dist loadgroup` est obligatoire avec `-n`** (#963) : un module à fixture module-scopée
# (`pg_module_dsn`, `live`) reste sur UN worker (`tests/_groupes_xdist.py`, marqueur
# `xdist_group` posé sur ~2 000 tests de ~230 fichiers, le plus gros groupe = 124 tests) ; les
# autres restent répartis test par test. Avec `load` (le défaut), deux tests d'un module
# atterrissaient sur deux workers, donc sur DEUX bases jetables : rouge intermittent sur un
# test sans rapport avec le changement. Un run série n'est pas concerné. Entre PARTS, l'unité
# est le fichier (les groupes portent le nom de leur fichier) ; un `xdist_group("…")` écrit à
# la main réunit ses fichiers dans la même part : un groupe ne se scinde jamais.
#
# ⚠️ **`-n 4` nu casse la suite — vérifié, pas supposé.** `pg_box` (fixture `pg_dsn`,
# session-scopée) passe par `_jeton_de_suite.py`, qui borne à `OTO_TEST_PG_PLACES=2`
# places PG simultanées — pensé pour le POSTE PARTAGÉ (plusieurs sessions d'agents qui se
# gênent, #07/09). Sous xdist, chaque WORKER est son propre process, donc sa propre
# session : dès que 2 des 4 workers touchent une base, ils tiennent leurs 2 places pour
# TOUTE leur durée de vie (scope session), et les 2 autres restent bloqués dans
# `jeton.prendre()` dès qu'ils ont eux aussi un test à base à jouer — jusqu'à ce qu'un des
# deux premiers workers épuise sa file entière. Reproduit le 14/09/2026 sur un clone
# jetable : 921 s (PLUS LENT que la suite série) et 414 erreurs en cascade (toute
# collecte de tests à base qui suit, dans le worker bloqué, échoue à la même fixture).
#
# Chaque part CI **relève `OTO_TEST_PG_PLACES` au nombre de ses workers** (écrit dans
# `$GITHUB_ENV` par `repartir_suite.py part`, pour cette part seule). C'est sûr ICI et nulle
# part ailleurs : un job CI est un conteneur ISOLÉ — ses workers sont les SEULS acteurs sur
# leurs bases jetables, il n'y a pas de voisine à protéger. Relever la même valeur sur le
# poste de dev partagé serait faux : là, la garde protège contre une VRAIE contention
# inter-sessions que la capacité de la machine ne change pas (cf. `_jeton_de_suite.py`).
# **Ne jamais relever `OTO_TEST_PG_PLACES` par défaut, seulement dans une part CI.**

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
#                  oto-backend-canari.sh <sha> : git reset --hard <sha du run> →
#                  preprod. ⚠️ Le sha, jamais `origin/main` — oto-backend#948,
#                  14/09/2026 : un `$1` absent y défaillait vers la pointe du
#                  tronc au moment de l'exécution, pas le sha que LE RUN venait de
#                  tester. Script sans repli depuis, comme oto-backend.sh)
#   tag  `v*`    → PROD    (« Deploy prod », deploy.yml : suite complète en parts
#                  parallèles (`suite-tests.yml`, agrégée par `test`), puis script
#                  serveur oto-backend.sh <tag> : git reset --hard <tag> → prod. La
#                  suite complète ne tourne QU'ICI depuis #1185 ; le push ne joue que
#                  volets touchés + socle, répartis de la même façon)
# Le deploy (les deux) = SSH box dédiée via runner self-hosted : reset au ref +
# **`uv sync --frozen`** (le jeu exact de `uv.lock` ; un ref sans verrou est refusé,
# docs/verrou-dependances.md) + restart + **smoke HTTP**
# (GET 200 /.well-known/oauth-authorization-server) + **rollback auto** si
# install/restart/smoke échoue. La couleur démarre par deploy/lanceur_secrets.py, celui
# du tag (secrets tirés par nom, #967 lot 5, depuis le 30/09/2026).
#
# Preprod = travailler sur `main`, commit, push : deploy preprod auto (gate
# `needs: test`). Claude Code (web) ouvre ses PR sur main → merge = deploy preprod.
git push origin main            # → PREPROD

# Prod = acte explicite : taguer un commit de main + pousser le tag.
git tag v1.2.3 && git push origin v1.2.3   # → PROD (tags v* immuables, ruleset)
# ⚠️ Depuis le 08/09/2026 la prod NE REJOUE PLUS la suite : elle EXIGE qu'un run de
# préproduction vert existe sur le sha exact du tag, et refuse sinon
# (`scripts/garde_preprod_verte.py`). C'est la vérification qu'on faisait à la main,
# devenue mécanique — et ~6 min 30 de moins entre le push du tag et la prod servie.
# Donc : taguer un commit DÉJÀ poussé sur main ET dont le run « Deploy preprod » est
# vert (jobs compris). Un refus nomme ce qu'il a lu — l'identifiant du run et sa
# conclusion exacte —, il n'y a rien à deviner. Il n'y a RIEN de neuf à faire côté
# préproduction : son run existe déjà à chaque poussée du tronc.
# Porte de secours (run de préproduction purgé, incident GitHub) : lancer « Deploy
# prod » en workflow_dispatch avec `sans_garde_preprod: true` — délibéré, tracé, et
# hors d'atteinte d'une simple poussée de tag.
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
# ⚠️ Depuis le passage de la box au lanceur (#967 lot 5, 30/09/2026), le `.env` ne porte plus AUCUN secret
# et `. .env` ne donne plus `DATABASE_URL` : passer par le lanceur (§Un script d'entretien
# par le lanceur, plus bas) — son `--script` prend un fichier, ici `python -c` n'en est pas un.

# ⚠️ Même besoin pour tout script d'ENTRETIEN lancé à la main (`python -m scripts.X`) :
# il n'hérite pas de l'environnement du service systemd, donc il sort en
# « RuntimeError: DATABASE_URL not set » avant d'avoir rien fait. Sourcer d'abord :
#   cd /opt/oto-mcp && set -a && . ./.env && set +a && ./.venv/bin/python -m scripts.X
# Vécu 19/08 sur scripts.archive_empty_kb_projects (dry-run par défaut, --apply pour agir).
# ⚠️ Cela ne valait que tant que le `.env` portait les secrets : depuis le passage au lanceur
# (#967 lot 5, 30/09/2026), `. ./.env` ne suffit plus → `lanceur --script scripts/X.py` (§suivant).

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

## Un script d'entretien par le lanceur (#967 lot 5)

Depuis que notre box est passée au lanceur générique (30/09/2026), le `.env` ne porte plus que le non-secret :
`DATABASE_URL`, `OTO_MCP_MASTER_KEY`, les clés S3… n'existent que dans l'environnement que
`deploy/lanceur_secrets.py` construit à chaque démarrage. Un script lancé à la main les
reçoit de lui, comme le service, et non d'un `.env` qu'on sourcerait. Le lanceur lit la clé
d'API du Secret Manager dans `$CREDENTIALS_DIRECTORY/scw` : hors d'une unité, c'est
`systemd-run` qui la présente (`LoadCredential`), à la façon des unités de maintenance.

```bash
# Sur la box (prod ; pour la préprod : /opt/oto-mcp-canari et lanceur-canari.env).
lanceur() {
  # l'arbre de la couleur ACTIVE, pas /opt/oto-mcp (qui est l'arbre bleu)
  local A=/opt/oto-mcp-$(cat /etc/oto-mcp/active-prod)
  sudo systemd-run --pipe --wait --quiet --collect \
    -p WorkingDirectory="$A" \
    -p EnvironmentFile=/opt/oto-mcp/.env -p EnvironmentFile=/etc/oto-mcp/lanceur-prod.env \
    -p LoadCredential=scw:/etc/oto-mcp/scw.key \
    "$A/.venv/bin/python" "$A/deploy/lanceur_secrets.py" "$@"
}
lanceur maintenance retention --dry-run                    # le serveur (`oto-mcp …`)
lanceur migrer current                                     # Alembic (docs/migrations-versionnees.md §5)
lanceur --script scripts/archive_empty_kb_projects.py      # un script de l'arbre, sous son Python
lanceur --script deploy/archive_tool_calls.py --dry-run    # l'archive du journal
lanceur --noms                                             # les NOMS de l'environnement, jamais une valeur
```

- `--script CHEMIN [args]` : un fichier `.py` **de l'arbre** (chemin relatif), exécuté par le
  Python du venv de l'arbre — pas `python -m scripts.X`, qui n'est pas un fichier. Ce qui
  n'est pas un fichier (`python -c "…"`) se joue en le posant dans un script de l'arbre, ou
  se lit par `oto-mcp` lui-même.
- `--noms` ne démarre rien : il lit les secrets comme un démarrage, refuse comme lui s'il en
  manque un, et imprime les noms, triés. **Ne jamais** afficher les valeurs d'un secret.
- Le lanceur **refuse** un secret déjà présent dans l'environnement : ne pas sourcer un
  ancien `.env` (`.env.avant-lot5`) devant lui.

## Maintenance — les travaux qui ont quitté le boot (ADR 0065 lot 0)

```bash
# Sur la box, avec l'environnement du service (jamais une copie du .env) :
sudo systemctl start oto-mcp-maintenance.service          # la passe complète, à la main
sudo journalctl -u oto-mcp-maintenance -n 50 --no-pager   # ce qu'elle a fait, avec ses durées
systemctl list-timers oto-mcp-maintenance.timer           # le prochain tir

# Un travail seul, et d'abord À BLANC — sur une base PARTAGÉE prod/preprod, la
# première question devant une purge est « combien de lignes ? ».
# ⚠️ Ces `sudo -E env $(cat .env)` valaient tant que le `.env` portait `DATABASE_URL` ; depuis
# le passage au lanceur (#967 lot 5, 30/09/2026), même geste par `lanceur maintenance …` (§précédent).
sudo -E env $(cat /opt/oto-mcp/.env | xargs) \
  /opt/oto-mcp/.venv/bin/oto-mcp maintenance retention --dry-run
#   retention | blocks | key-indexes            les travaux du timer
#   revisions                                   (timer) purge du journal des révisions
#                                               de ligne au-delà de
#                                               OTO_JOURNAL_REVISIONS_RETENTION_DAYS
#                                               (90 j), sauf l'import d'une ligne
#                                               vivante — docs/datastore.md
#   alertes-credential | all                    (idem : `all` joue le timer)
#   instagram-tokens                            renouvelle les autorisations
#                                               Instagram avant leur terme. ⚠️ Ce
#                                               jeton ne se renouvelle que TANT
#                                               QU'IL VIT (pas de refresh_token
#                                               chez Meta) : sauter la passe assez
#                                               longtemps ne dégrade pas la
#                                               connexion, elle la PERD, et seule
#                                               l'utilisatrice peut la refaire.
#                                               Le timer ne tourne qu'en PROD :
#                                               une connexion posée en preprod
#                                               n'est renouvelée qu'à l'usage.
#   oauth-relay-callbacks [--apply]             ACTE, à blanc par défaut : constate, ou
#                                               pose avec --apply, le rappel du relais
#                                               d'autorisation sur les hosts DÉJÀ inscrits
#                                               dans OTO_MCP_OAUTH_RELAY_HOSTS — avant de
#                                               redémarrer (docs/auth-logto.md §relais)
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

- **`oto-core` PINNÉ sur un tag git** (`oto-core[anonymize] @ git+…@vX.Y.Z` dans `pyproject.toml` — l'extra est `anonymize`, `browser` a été retiré côté backend et n'est resté que sur le CLI local ; plus `@main` flottant ni dép `oto-cli`) : une version déployée = coordonnée reproductible. ⚠️ **`pip` ne réinstalle PAS une dép VCS déjà présente** (`oto-core` "satisfait" quelle que soit sa version) → `pip install -e .` seul ne monte JAMAIS oto-core au tag bumpé. Le deploy installe **par le verrou** (`uv sync --frozen`, #932) : oto-core au commit que `uv.lock` désigne, plus de force-reinstall. Bump connecteurs = tag oto-core + édit du pin **+ `uv lock` dans le même commit** (le verrou est versionné, la CI installe par lui et refuse un verrou en retard — `docs/verrou-dependances.md`) + deploy (PAS de `git pull` box). Cf. ADR 0020. ⚠️ **Symptôme trompeur en LOCAL** : des tests rouges peuvent être un venv en retard sur le pin, pas du code cassé (05/08 : 17 rouges sur un connecteur neuf ; 26/08 : 48 ; 01/09 : 28) → **sous-section ci-dessous**, qui porte la trace exacte à reconnaître et la recette. (⚠️ box `otomata-0` a un VIEUX oto-mcp décommissionné/stoppé avec un editable legacy `oto-cli` pré-split — ne pas s'y fier, le runtime live est la box dédiée.) ⚠️ **Le pin est un champ que TOUTES les sessions // éditent → régressions silencieuses récurrentes** : vécu 2026-07-07, un commit concurrent a réécrit le pin `v1.18.0→v1.17.0` et **cassé un tool déployé SANS erreur** (le tool était enregistré, sa méthode absente de l'ancien oto-core → `AttributeError` seulement à l'appel). Toujours bumper en **superset** (tag haut ⊇ tags bas) ; à la moindre divergence de pin en merge/rebase, **garder la version haute**.
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
