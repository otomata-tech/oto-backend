---
title: Déploiement de la plateforme
type: reference
description: >-
  `main` = tronc (CI seulement), tag vX.Y.Z = prod, seul déploiement : pourquoi le modèle tronc
  unique, les domaines RÉELLEMENT servis, ce que fait — ou ne fait pas — le workflow de
  release, la garde « tronc vert », la fenêtre finie du healthcheck et les gestes
  d'exploitation déclarés en trailer (`Geste-exploitation:`), recopiés dans les notes du tag.
  Vaut pour le backend et le dashboard ; les commandes sont dans `commands.md`.
---

# Déploiement — modèle tronc unique

> Les **commandes** (push, tag, logs, inspection de base) vivent dans `commands.md` ; ce doc
> porte le **modèle**, ses motifs et ses pièges. Le détail machine (box, scripts serveur,
> bleu/vert) est dans le dépôt d'infra privé ; la déclaration d'une autre cible de déploiement,
> dans `instance-cible.md`.

## Un tronc `main`, un checkout, pull/commit/push

Plus de dualité `canari`/`main` (backend et dashboard). Motif : les PR des sessions Claude
Code (web) ciblaient la branche de prod par défaut et se faisaient refuser par une garde —
« ça ne déploie plus ». Bascule vers le trunk-based :

- **`main` n'est que le TRONC : il ne déploie rien** (décision du 19/11/2026 ; l'instance
  principale n'a plus de préproduction). Une poussée sur `main` — ou une PR vers `main` —
  lance la CI (`.github/workflows/ci.yml`, « CI ») et rien d'autre : aucun serveur ne suit
  le tronc. Le job `test` de la CI est le check requis par la protection de `main`. Depuis
  le 08/10/2026 (#1185), ce `test` ne joue que les **volets touchés**
  par le diff (base = `before` du push) **plus le socle** de gardes transverses (sortie
  déclarée des capacités, façade d'erreur MCP, contrat servi, concordance carte/client,
  portée, inventaire d'env, silences, SQL hors boucle, surfaces figées) : table
  `tests/volets.toml`, script `scripts/selection_tests.py`, le résumé du run dit ce qui a été
  retenu et pourquoi. Base inconnue, fichier transverse ou hors volet : suite complète. On ne
  commite sur `main` que du livrable : le prochain tag le prendra. Une PR fusionnée dans
  `main` ne sert rien tant qu'aucun tag ne la porte.
- **tag `vX.Y.Z` = PROD, et c'est le SEUL déploiement.** La mise en prod est un **acte
  explicite** : poser le tag sur un commit de `main` et le pousser déclenche « Deploy prod »
  (garde du tronc → suite complète → déploiement → healthcheck → rollback). Rollback = redéployer le tag précédent. Les tags `v*` sont **immuables**
  (ruleset GitHub : un tag de release ne se re-pointe pas). Côté backend, le tag est passé
  au script serveur (`oto-backend.sh <tag>` : `git reset --hard <tag>`). Côté dashboard :
  artefact seul, build au tag.
- **La suite complète tourne au tag, une seule fois** (#1185, 08/10/2026), **répartie en
  parts parallèles** (#1111) : « Deploy prod » valide le tag (job `tag`), appelle
  `.github/workflows/suite-tests.yml` sur l'arbre du tag (job `suite`, cible vide), puis le
  job simple `test` — l'agrégateur, qui rougit sauf si la suite a conclu `success` — et
  `deploy` attend `test`. La prod est donc servie après ~3 min 30 à 4 min de suite (7 parts,
  contre ~9-14 min sur un seul runner), plus ~1 min 30 de déploiement. Une relance « Re-run
  failed jobs » ne rejoue pas une part déjà verte. Sur un rouge, le résumé du run dit si
  chaque fichier rouge aurait été joué au push (constat demandé par #1185, lu dans
  l'artefact `junit-suite` ; sélection recalculée parent → sha, donc approchée).
- **Comment la suite se répartit** (`suite-tests.yml`, la même au push et au tag) : `plan`
  calcule N parts depuis les durées mesurées par fichier (`scripts/durees_suite.json`) ;
  chaque `part` recalcule ses fichiers depuis le disque (un fichier neuf, absent des durées,
  tombe quand même dans une part), dérive son `-n` de la mémoire de SA machine et joue
  `--dist loadgroup` ; `reference` collecte ce que jouait l'ancien job ; le **verdict**
  rougit si l'union des collectes des parts n'est pas exactement la référence (trou ou
  doublon). Au push, `test` de « CI » est le même agrégateur, sur la sélection.
  Régénérer les durées après un run complet et vert :
  `gh run download <run_id> -n suite-durees` puis copier `durees_suite.json` dans
  `scripts/` et commiter. ⚠️ Les artefacts ont des noms fixes (`junit-suite`, `suite-durees`,
  `suite-part-<i>`…) : un même run ne doit appeler `suite-tests.yml` qu'une fois, sinon ils
  entrent en collision. Détail : `docs/commands.md` §Suite parallèle en CI.
- **La prod exige aussi un tronc vert** (`scripts/garde_tronc_vert.py`) : le sha du tag est
  sur `main` (pointe ou ancêtre de la pointe), ET le run « CI » poussé sur `main` à ce sha
  exact a conclu `success`, son job `test` compris. Refusé sinon, en nommant le run lu et sa
  conclusion. Ce vert dit « sélection, socle et contrats des consommateurs verts », **pas**
  « suite complète ». Donc : taguer la **pointe d'une poussée sur `main`** dont le run « CI »
  est vert (un commit arrivé au milieu d'une poussée de plusieurs commits n'a pas de run à
  lui). Porte de secours (run purgé, incident GitHub) : « Deploy prod » en
  `workflow_dispatch` avec `sans_garde_tronc: true` — délibéré, tracé, hors d'atteinte d'une
  simple poussée de tag. Il ne contourne que la garde : la suite complète du tag tourne
  toujours.
- ⚠️ **La fenêtre du healthcheck serveur est finie** (120 s à ce jour, ~60 s constatés à
  l'origine) : un lot qui ajoute un travail one-shot au boot (re-projection, migration au
  démarrage) la vérifie — et l'élargit — **avant** le tag, sinon rollback automatique sur un
  déploiement sain. Ce qui n'a rien à faire au boot va en maintenance (`oto-mcp maintenance …`).
- **Aucun serveur ne sert le tronc avant la prod.** Ce qui se vérifiait en préproduction se
  vérifie désormais dans la CI (sélection, contrats) et au tag (suite complète). Une
  instance dont la déclaration a une préprod (`instance-cible.md`) garde la sienne ; elle y
  monte un tag, jamais le tronc. `OTO_ENV` et `config.est_la_production()` restent la garde
  des boucles de fond qui agissent sur un tiers (`live-migrations.md`).
- ⚠️ **Une révision ne suppose pas que le code neuf a démarré.** Chez nous, on démarre puis
  on migre (`migrer upgrade head` après le tag) ; une instance cible migre AVANT de démarrer
  (`instance-cible.md` § Les migrations, jouées par la montée). Ce qu'une révision attend
  d'une étape du code (la copie du coffre d'un connecteur renommé, qui exige la clé
  maîtresse) est une commande `oto-mcp maintenance` que le démarrage joue ET que la porte
  d'une cible joue avant la migration — aujourd'hui `copies-du-coffre` — et la révision lève,
  en nommant cette commande, si l'étape manque.
- **Release log = `RELEASES.md` du dépôt public `oto`** : notes datées **rédigées à la
  main** par le mainteneur, format libre, niveau produit/plateforme (cross-repo), décorrélé
  des tags (qui restent le mécanisme de déploiement). Pas de génération automatique en CI ni
  de GitHub Releases par tag (rejeté : « garder la main sur les notes »). Un agent peut
  proposer un brouillon (matière : les trailers `Changelog:` des commits), le mainteneur valide.
- **`main` est protégée légèrement** : la contrainte vise les tiers (proposition + check
  `test`), pas l'équipe — le mainteneur garde le push direct sur `main` (cf. `LICENSING.md` du dépôt public `oto` pour la forme de la
  protection).
- **Dépôts mono-branche** (sites, CLI, plugin…) : inchangés, un push = leur unique flux.

## Plusieurs sessions sur un même checkout : `--autostash` est interdit

Mesuré le 09/09/2026, après l'avoir causé. `--autostash` remise puis restaure le **contenu**
du travail en cours — mais **pas l'INDEX**. Deux sessions qui poussent à tour de rôle se
**dé-indexent mutuellement, en silence** : ce qui était `git add`é redevient non stagé.

La conséquence est plus grave que la gêne. L'index est précisément ce dont on se sert pour ne
prendre QUE ses fichiers dans un arbre partagé — indexer nommément est la protection contre
le commit trop large. Cette protection est effacée par le geste le plus banal du dépôt. Et le
mode d'échec est le pire : un `git commit` sur un index vidé **ne lève aucune erreur**, il
rend un commit vide ou incomplet, et on croit avoir livré.

Trois règles : ne jamais s'appuyer sur un index qui a passé du temps ; indexer et commiter
dans le même souffle ; vérifier juste avant de commiter, pas cinq minutes plus tôt. Si le
`pull --rebase` refuse à cause d'un travail en cours, **ne pas remiser le checkout
partagé** : commiter ses fichiers nommément d'abord.

## Deux processus sur une même base : une boucle de fond à la fois

Deux processus du serveur branchés sur la même base composent les mêmes boucles de fond :
les deux couleurs du bleu/vert pendant la vidange, ou plusieurs processus qui servent la
même instance. La composition (`boucles_de_fond.py`) écarte la préprod ; entre deux
processus de **production**, c'est la base qui tranche. **Aucune boucle qui agit vers
l'extérieur ne joue deux fois le même travail**, par l'un de deux mécanismes :

- **le verrou de boucle** (`oto_mcp/db/verrou_de_boucle.py`) : un verrou consultatif
  PostgreSQL de session, sur une connexion dédiée hors pool, en autocommit, tenu le temps
  d'UN tour et rendu dans un `finally` (puis par la fermeture de la connexion). Le second
  processus ne l'attend pas : il **saute son tour**, dit une fois en INFO
  (« boucle X : tour sauté, un autre processus la tient »), puis en DEBUG. Une base
  injoignable reste une erreur du tour. Boucles couvertes : `scheduler` (emails
  programmés : le claim laisse la ligne `pending`), `alerte_agents_bloques` et
  `alertes_slack_agents` (marque après l'envoi), `medecin_agents` (escalade lue puis
  écrite), `billing_runner` (réconciliation, rattrapages, factures ; les échéances
  gardent en plus leur réservation `db/billing_reservation.py`) ;
- **une réservation ligne à ligne** qui sort le travail de la file dans le même `UPDATE` :
  `transcription_worker` (`pending → running`, `FOR UPDATE SKIP LOCKED`),
  `jev_jobs_worker` (bail en base), `runner_tick` (compare-and-swap sur `next_due`).

Les boucles qui n'écrivent qu'en base (`file_extract_worker`, `rank_backfill`,
`formula_backfill`, `credits_echeances`) et le chien de garde local `hang_watch` n'ont ni
l'un ni l'autre : en double, elles refont un travail idempotent, sans effet chez personne.
`embed_worker` n'agit chez aucun tiers mais paie chaque appel à Mistral : il a les deux,
le verrou de boucle et un bail par source (`db/embed_file.py`), plus un plafond de tokens
par jour (`OTO_EMBED_PLAFOND_TOKENS_JOUR`). Une boucle tierce neuve choisit l'un des deux mécanismes ;
`tests/test_verrou_de_boucle.py` le vérifie.

⚠️ Ce que le verrou ne fait pas : un processus **gelé** en plein tour (boucle d'événements
bloquée, appel réseau sans fin) garde la boucle, et l'autre saute ses tours tant que la
connexion vit. Un processus **tué** la rend aussitôt (fin de session PostgreSQL).

## Les domaines servis

Mesurés le 03/09/2026 (jusque-là, la doc nommait les *hostnames de transition* du cutover
de la base chiffrée, pas les adresses réellement servies) :

| | backend | dashboard |
|---|---|---|
| **PROD** (tag `v*`) | `mcp.oto.cx` | `manage.oto.cx` |

Le backend n'a plus d'instance de préproduction depuis le 19/11/2026 : `mcp.oto.ninja` et
son alias `mcp-canari.oto.ninja` ne servent plus l'instance principale (retrait côté
serveur : gestes d'exploitation de la PR qui l'a décidé). La préproduction du dashboard
relève de son propre dépôt.

**Le piège est actif, pas théorique** : les anciens noms répondent encore.

- Un ancien hostname de dashboard répond **200** mais sert un **vieux build** (un chunk
  unique là où la prod en sert onze). **Ce n'est pas la prod** : une mesure faite là
  « prouve » que la production ne sert pas ce qu'elle sert. Un 404 aurait protégé ; un 200
  sur un build fossile ne protège de rien.
- `dashboard-canari.oto.ninja` répond **302** vers `manage.oto.ninja` : bon endroit, si l'outil
  de mesure suit les redirections.

Source de vérité côté dashboard : son propre `docs/deploiement.md`.

## Poser un tag ne fait pas ce qu'on croit

Lu dans les workflows (`release.yml` et `deploy*.yml` du backend, `tag-release.yml` et
`deploy*.yml` du dashboard).

- **Le workflow de release tague le SOMMET DU TRONC à l'instant du déclenchement, et
  n'accepte aucun commit nommé.** Le dashboard fait un checkout `ref: main` puis `git tag`
  sur ce HEAD ; le backend tague le HEAD du ref sur lequel il est dispatché (et refuse de
  publier si « CI » est rouge sur ce commit). Leur seul input de version est un
  `vX.Y.Z`, **jamais un SHA**. Un tag qui doit viser un commit précis, différent du sommet,
  se pose donc **localement** (`git tag vX.Y.Z <commit> && git push origin vX.Y.Z`) ;
  passer par le workflow taguerait autre chose, silencieusement.
- **Sur le dashboard, le tag posé par le workflow ne déclenche PAS le déploiement.** Un tag
  créé avec le `GITHUB_TOKEN` ne déclenche aucun autre workflow (garde anti-récursion
  GitHub) : `deploy.yml` (`on: push: tags: ["v*"]`) reste au repos. Il faut ensuite lancer
  « Deploy prod » en `workflow_dispatch` avec le même tag : **deux actes**. Le backend
  contourne la garde : `release.yml` **appelle** `deploy.yml` (`workflow_call`) juste après
  avoir posé la version : **un seul acte**. Les deux dépôts ne se pilotent donc pas pareil ;
  un tag posé depuis Actions sur le dashboard et laissé là, c'est une version gravée et rien
  de déployé.

## Le Python des couleurs (`BG_PYTHON`)

Décision du 24/11/2026 : oto.cx (la box `oto-platform`) passe en **Python 3.12**, le plancher
du code ne bouge pas (`requires-python = ">=3.10"`, gardé en CI par `syntaxe-plancher` et
`garde-plancher-python`). Le moyen : une variable facultative de la bibliothèque bleu/vert,
déclarée par notre wrapper.

- **`BG_PYTHON` = le chemin EXACT de l'interpréteur, au patch.** Déclaré dans
  `deploy/oto-backend.sh` : `/opt/oto-mcp-python/cpython-3.12.14-linux-x86_64-gnu/bin/python3.12`,
  interpréteur géré par uv posé par infra AVANT toute livraison. Le déploiement ne
  télécharge jamais de Python (`UV_PYTHON_DOWNLOADS=never`). Les instances cibles ne le
  posent pas : leur Python suit leur déclaration (`instance-cible.md` § Changer de Python).
- **Règle de recréation.** À chaque montée, avant l'installation, le venv de la couleur
  qu'on installe — l'INACTIVE, jamais celle qui sert — est comparé à BG_PYTHON en les
  exécutant (`sys.version_info[:3]`). Même version au patch : rien. Sinon il est recréé
  (`uv venv --seed --python "$BG_PYTHON"`), puis `uv sync --frozen --python "$BG_PYTHON"`
  le remplit. L'ancien venv est gardé (`.venv.avant-python`) jusqu'à la fin de
  l'installation ; à son échec, la couleur reprend son code ET son venv d'avant : elle reste
  le retour arrière. Une couleur en service ou dont l'unité tourne est refusée, rien n'est
  touché.
- **Garde avant démarrage.** Après l'installation, `.venv/bin/python` doit rendre exactement
  la version de BG_PYTHON, au patch près. Sinon : refus nommé (« garde Python »), la couleur
  est remise à son état d'avant, ne démarre pas, rien ne bascule.
- **Le retour arrière ne lit pas BG_PYTHON** : il redémarre la couleur précédente sur son
  venv. Pendant la transition, les deux couleurs n'ont pas le même Python : la première
  montée passe l'inactive en 3.12, la suivante l'autre ; un retour entre les deux repart en
  3.10, et c'est voulu.
- **Sans BG_PYTHON**, la bibliothèque fait les gestes d'avant à l'octet près (preuve :
  `tests/deploy/gestes_bleu_vert/`, scénarios d'avant rejoués sans la variable).
- ⚠️ **Piège du lien de version mineure.** uv range l'interpréteur au patch
  (`cpython-3.12.14-…`) mais pose aussi un lien `cpython-3.12-…`, et un venv créé depuis le
  chemin au patch inscrit CE LIEN dans le `home` de son `pyvenv.cfg`. Un
  `uv python install 3.12.15` dans `/opt/oto-mcp-python` déplacerait le lien, donc tous les
  venvs, en silence. Toute vérification lit donc la version en EXÉCUTANT l'interpréteur
  (`python -V`, `sys.version_info`), jamais le chemin ni `pyvenv.cfg` : c'est ce que fait la
  garde, qui refuserait la montée suivante. Changer de patch se prévoit : poser le nouvel
  interpréteur, changer `BG_PYTHON` dans le wrapper dans le même geste, puis monter un tag
  (deux montées pour les deux couleurs).

⚠️ **La bibliothèque et le wrapper de la box sont des copies posées À LA MAIN**
(`/opt/deploy/oto-mcp-bluegreen.sh`, `/opt/deploy/oto-backend.sh`), pas celles du tag. Une
fusion dans `main` ne change rien sur la box : ce comportement n'existe qu'une fois la
bibliothèque recopiée puis le wrapper qui déclare BG_PYTHON recopié (dans cet ordre : une
bibliothèque neuve sans BG_PYTHON fait les gestes d'avant ; un wrapper neuf sur l'ancienne
bibliothèque est ignoré), et éprouvé sur une montée réelle avant de dire « livré ».

Bancs : `tests/deploy/test_python_des_couleurs.py` (doublures, scénarios `prod-python-*`) et
son banc au vrai uv, CI seulement (job `plancher-python-uv-reel`). Le verrou entier
s'installe en roues cp312 sans rien compiler : job `verrou-roues-cp312` de `ci.yml`.

## Gestes d'exploitation : un trailer par geste, recopié dans les notes du tag

Une PR qui exige un **geste d'exploitation** (une commande à jouer à la main autour de la
mise en prod : maintenance, purge, réécriture de données…) le déclare dans un de ses
commits, par un trailer par geste, **dans l'ordre où les gestes se jouent** :

```
Geste-exploitation: <commande> ; <avant|après> ; <effaçant|non>
```

- `<commande>` : la commande exacte, telle qu'on la tape (elle peut contenir un `;` : on
  coupe aux deux derniers).
- `avant|après` : par rapport à la bascule de prod du tag. Le tronc ne déploie plus rien :
  aucun serveur n'a encore servi le code du tag quand un geste `avant` se joue. Un geste
  `avant` se joue donc **depuis un arbre du SHA du tag** (export `git archive <SHA>`, jamais
  le checkout de la box ni la couleur en service, qui portent l'ancienne version), **juste
  avant la bascule** — pour une instance, juste avant sa montée. `après` = une fois le tag
  servi, depuis la couleur active.
- `effaçant|non` : `effaçant` si le geste est irréversible (données retirées ou réécrites
  sans retour), `non` sinon.

Le trailer vit dans le **dernier paragraphe** du message, avec `Changelog:` et les autres ;
une ligne `Geste-exploitation:` ailleurs dans le message n'est pas lue par git comme un
trailer, et le script la refuse. Plusieurs gestes = plusieurs lignes, dans l'ordre.

Exemple :

```
Les jetons de lien sont rangés chiffrés

Changelog: changed
Geste-exploitation: oto-mcp maintenance jetons-de-lien --appliquer ; après ; non
Geste-exploitation: <une commande irréversible> ; après ; effaçant
```

**Lecture : `deploy/gestes_exploitation.py <tag précédent>..<SHA>`** (Python standard, sans
le paquet). Il lit les trailers des commits de la plage, du plus ancien au plus récent
(chaque commit une fois, les gestes d'un commit dans leur ordre), et imprime une ligne par
geste — le sha court (12) du commit, puis la ligne de trailer normalisée :

```
3f2a9c0d1e4b Geste-exploitation: oto-mcp maintenance jetons-de-lien --appliquer ; après ; non
```

Codes de sortie : `0` aucun geste ; `4` au moins un geste (lignes sur stdout) ; `2` trailer
mal formé ou hors du bloc de trailers (stderr nomme le commit, stdout vide) ; `3` appel
invalide ou plage illisible.

**La chaîne de tag recopie ces lignes dans les notes du tag.** `release.yml` joue, avant de
poser la version, `python3 deploy/gestes_exploitation.py "<dernier tag>..HEAD"` et ajoute
les lignes au message du tag annoté (et au résumé du run) ; un `2` refuse de poser la
version. Un tag posé à la main fait de même :
`git tag -a vX.Y.Z <SHA> -m "vX.Y.Z" -m "$(python3 deploy/gestes_exploitation.py vA.B.C..<SHA>)"`
(code `0` ou `4` ; `2` = corriger le commit avant de taguer). Les notes se relisent par
`git tag -l --format='%(contents)' vX.Y.Z`.

**Un automate de montée s'arrête au lieu de prendre le tag** dès qu'une ligne existe entre
le tag servi et le tag visé : il joue
`python3 deploy/gestes_exploitation.py <tag servi>..<tag visé>` et ne monte que sur `0`.
Sur `4`, il s'arrête et rapporte les lignes (un humain joue les gestes `avant`, monte, puis
joue les gestes `après`) ; sur `2` ou `3`, il s'arrête aussi : un geste illisible n'est pas
une absence de geste.
