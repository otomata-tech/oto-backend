---
title: Migrations versionnées — note de conception
type: explanation
description: >-
  L'inventaire mesuré de ce que `db/_init.py` exécute à chaque démarrage (143 ALTER
  écrits, 297 ordres SQL réellement émis, 40 écritures dont le coût suit la taille de
  la base), la datation de chaque migration, les 79 devenues inertes, deux défauts
  constatés au passage — et, depuis le lot 0 de l'ADR 0065 (2026-08-28), ce que le
  boot fait RÉELLEMENT : un `init_db` au lieu de trois, la maintenance passée en
  timer, la rétention du journal rendue à l'archivage qu'elle annulait, et la
  répartition chiffrée des 36-39 s de démarrage (§1.2 — la base n'en pèse que 6,4).
---

# Migrations versionnées — note de conception

> **Ce document ne décidait rien** : il mesurait l'existant, l'inventoriait, et posait
> trois options avec leurs risques. **Le choix est fait depuis** — ADR 0065 du
> 2026-08-27, option C — et son **lot 0 est livré** (oto-backend#426, 2026-08-28) :
> la §1 ci-dessous décrit donc un état RÉVOLU, conservé parce qu'il explique
> pourquoi ; la §1.3 dit ce que le boot fait maintenant. Le reste (§2 et §3) reste
> l'inventaire du régime en place, que les lots 1 et 2 attaqueront ; **les options et
> les six questions ouvertes sont parties au chantier** (§4).
>
> Rédigé le 2026-08-27, en marge du lot qui a découpé `db/_schema.py` par domaine
> (déplacement pur, DDL inchangé au caractère près). Ce lot n'a **pas** touché
> `_init.py` : la §0 dit pourquoi, et c'est la première donnée d'entrée du choix.

## 0. Pourquoi `_init.py` n'a pas été découpé par domaine

La question posée était : peut-on regrouper les ALTER par domaine, comme on vient de
le faire pour le DDL, sans rien changer à l'ordre d'exécution ? La réponse est non,
et elle se mesure plutôt qu'elle ne s'argumente.

`_init_db_once` est **une** fonction de 914 lignes dont le corps utile est un seul
`with _connect() as conn:` portant **226 instructions de premier niveau** (209
appels, 5 `for`, 7 `if`, 4 imports locaux, 1 affectation). En attribuant chaque
instruction au domaine de la table qu'elle touche — avec la carte des domaines que
la découpe du DDL vient de fixer — on obtient **97 blocs contigus de même domaine**
pour 226 instructions.

Autrement dit : `projects` apparaît en **13 morceaux séparés**, `orgs` en 8,
`procedures` en 6, `usage` et `users` en 4. Les migrations sont écrites dans l'ordre
**chronologique** — chaque lot ajoute à la fin, quel que soit son domaine — et cet
ordre n'est pas commutatif :

- le tenant 1 doit être semé **avant** l'`ALTER` qui pose `orgs.tenant_id NOT NULL
  DEFAULT 1 REFERENCES tenants(id)` (garde-fou `test_tenant_l1_migration`) ;
- l'index unique cible doit exister **avant** le `DROP CONSTRAINT` de la PK legacy,
  sinon l'arbitre `ON CONFLICT` de la prod tombe (`live-migrations.md`) ;
- la copie legacy→cible doit précéder le `DROP` de la table legacy, **au même boot**.

Regrouper par domaine **en gardant l'ordre** produirait donc 97 fragments répartis
sur 20 modules, rappelés dans un ordre chronologique entrelacé : la même séquence,
avec 97 indirections de plus et 97 occasions nouvelles de se tromper d'ordre. Ce
n'est pas de la localité, c'est du camouflage. Et regrouper **sans** garder l'ordre
est précisément ce que les trois incidents ci-dessus interdisent.

**La conclusion utile n'est pas « on ne peut pas ranger `_init.py` »**, c'est : le
rangement de `_init.py` n'est pas un problème de fichiers. C'est le symptôme d'un
mécanisme qui rejoue tout l'historique à chaque démarrage parce qu'il n'a aucune
notion de « déjà fait ». C'est ce que le chantier adresse (§4).

## 1. Le boot : ce qu’il faisait, ce que ça coûtait, ce qu’il fait depuis le lot 0

Mesures du 2026-08-27, `init_db()` rejoué contre un PostgreSQL 17 jetable
(`pgvector/pgvector:pg17`, conteneur local, RTT négligeable, base **sans données**),
deux passes sur la même base :

| passe | situation | ordres SQL émis | durée |
| --- | --- | ---: | ---: |
| 1 | base **vierge** (installation neuve) | 390 | ≈ 170–190 ms |
| 2 | base **déjà migrée** — le cas de la PRODUCTION | 297 | ≈ 55–70 ms |

Répartition des 297 ordres de la passe 2 — celle qui tourne à **chaque**
redémarrage en production, y compris un `systemctl restart` sans nouveau code, et
où *aucun* n'a d'effet :

| ordre | n |
| --- | ---: |
| `ALTER TABLE` | 154 |
| `CREATE INDEX` (dont 10 `UNIQUE`) | 44 |
| `SELECT` (sondes, lectures de backfill) | 30 |
| `UPDATE` | 20 |
| `DROP TABLE` | 15 |
| `DELETE` | 13 |
| `INSERT` | 7 |
| `DROP INDEX` | 3 |
| autres (`CREATE EXTENSION` ×2, `CREATE TABLE` ×2, `DO`, `CREATE SEQUENCE`, `DROP SCHEMA`, le bloc `_SCHEMA` lui-même…) | 11 |

**Ce que ces chiffres disent, et ce qu'ils ne disent pas.** 55–70 ms sur une base
locale **vide** ne se transposent pas à la production : la base y est une RDB
managée (chaque ordre est un aller-retour réseau) et surtout **40 de ces ordres
écrivent des données** — leur coût suit la taille des tables, pas le nombre
d'ordres. Sur une base de test vide ils sont gratuits ; c'est précisément ce qui
rend la mesure locale rassurante à tort. Les 154 `ALTER` no-op, eux, sont bornés et
bon marché : ils **ne sont pas** le problème de la fenêtre de healthcheck. Ils sont
le problème de la lisibilité et du risque d'écriture.

### 1.1 Le boot était devenu un ordonnanceur de maintenance — il ne l'est plus

*(état jusqu'au 2026-08-28 ; corrigé par le lot 0 de l'ADR 0065, cf. §1.3.)*

C'était le constat le plus important de cette note, et il débordait le sujet des ALTER.
Après avoir commité sa transaction de schéma, `init_db` enchaînait **quatre travaux
qui n'ont rien de DDL**, tous sur le chemin du démarrage, tous `fail-open`, tous à coût
croissant avec la base :

| travail | ce qu'il fait | ce dont le coût dépend | mesuré le 28/08 |
| --- | --- | --- | --- |
| `backfill_node_blocks()` | parse le corps markdown des nœuds en blocs, en Python | nombre de nœuds modifiés | 130 ms en régime stable — **mais ~19 s à chaque rotation de marqueur** (1 526 nœuds × 4 allers-retours) |
| `prune_tool_calls(30 j)` | purge du journal **et** des runs orphelins — deux `DELETE` non bornés | volume du journal | < 10 ms — parce qu'il n'y avait plus rien à purger, et c'est le problème (§1.4) |
| `prune_run_messages(30 j)` | purge du fil des runs hébergés | volume du fil | 20 ms |
| `_ensure_datastore_key_indexes()` | par datastore : résorption des doublons puis `CREATE UNIQUE INDEX` | nombre de datastores × leurs lignes | **644 ms** pour 204 datastores, zéro index manquant |

Et à l'intérieur de la transaction, cinq conversions de contenu appelées depuis
`db/nodes.py` (`convert_projects`, `convert_docs`, `convert_guides`,
`convert_tables`, `convert_rows`) plus `db/guides.py` et `db/aux_embed.py`. Celles-là
**restent au boot** : elles sont additives, idempotentes, dans la transaction, et leur
coût mesuré est négligeable.

**Un cinquième travail ne tournait pas du tout.** `migrate_business_key_indexes()` était
appelée à la dernière ligne d'`init_db`, *après* la boucle de retry — dont le corps
`return` en cas de succès et `raise` à la dernière tentative. Aucun chemin ne
l'atteignait, et ce depuis #318. C'est oto-backend#421 ; le lot 0 a retiré l'appel mort
et en a fait une commande explicite, **sans timer** — la faire tourner pour la première
fois est une décision, pas un effet de bord.

### 1.2 Ce qui consommait la fenêtre de healthcheck, mesuré

La fenêtre est de **120 s** depuis le 2026-08-27 (`deploy/oto-backend.sh`, sonde directe
sur `127.0.0.1:9103` — elle interrogeait le canari depuis le 06/07). Le boot préprod
relevé au journal le 28/08 : 39 s, 40 s, 39 s, 36 s (« Started » → « Application startup
complete »).

Répartition mesurée le 2026-08-28 contre la base servie (RDB managée, **RTT 3,14 ms par
ordre** — c'est lui qui convertit un nombre d'allers-retours en secondes) :

| poste | coût unitaire | fois par boot | total |
| --- | ---: | ---: | ---: |
| `init_db` (297 ordres en régime stable) | 0,93 s | **3** | 2,8 s |
| `_ensure_datastore_key_indexes` (204 datastores, 1 aller-retour chacun) | 0,64 s | **3** | 1,9 s |
| `backfill_personal_orgs` (82 users × 2 allers-retours) | 0,58 s | **2** | 1,2 s |
| `backfill_node_blocks` (la sonde, no-op) | 0,13 s | **3** | 0,4 s |
| les quatre autres backfills + les deux purges | < 30 ms | 2-3 | ~0,1 s |
| **total base** | | | **≈ 6,4 s** |

**Deux enseignements, et le second est le plus utile.** D'abord : `init_db` était appelé
**trois** fois par boot et les six backfills **deux** fois (`_build_mcp` est appelé pour
l'instance anonyme puis pour l'authentifiée, et `main` rappelait `init_db` par-dessus) —
c'est ce que le lot 0 corrige, pour ~4,5 s. Ensuite : **la base ne pesait que 6,4 s des
36-39 s**. Le reste — une trentaine de secondes — est l'import Python et **deux
`_build_mcp` complets** (`register_all` + montage des capacités). C'est là qu'est le
gras du démarrage, et ce n'est pas un problème de migrations.

### 1.3 Ce que le boot fait depuis le lot 0 (2026-08-28)

1. **Une seule préparation de base par process** (`server._prepare_database`, gardée par
   un drapeau de module) : un `init_db`, un tour de backfills. La garde est au point
   d'appel et **pas** dans `init_db`, qui doit rester rejouable — c'est ainsi que les
   tests prouvent son idempotence.
2. **Les quatre travaux de maintenance sont sortis**, chacun devenu une commande nommée
   dans `oto_mcp/maintenance.py` : `oto-mcp maintenance retention | blocks |
   key-indexes | all`, tirée par `deploy/oto-mcp-maintenance.timer` (quotidien, posé et
   activé par `deploy/oto-backend.sh`, **prod seulement** — la base est partagée, deux
   exécutants se disputeraient les mêmes lignes).
3. **Chaque étape du boot se chronomètre dans le journal** (`boot: <étape> <n> ms`) —
   « on décide sur mesure, pas sur intuition ». ⚠️ **Ces lignes n'ont RIEN produit à leur
   premier déploiement** (2026-08-28, corrigé le même soir) : `logging.basicConfig`
   vivait dans `server.main`, or le premier tiers du démarrage se passe **à l'import**
   d'`oto_mcp.server` — un `logger.info` émis avant tout handler est jeté, le
   `lastResort` de la stdlib n'émettant qu'à partir de WARNING. Zéro ligne `boot:` dans
   le journal de la box alors que le code en émettait une par étape. Le journal est
   désormais configuré par `oto_mcp/cli.py` avant l'import, et
   `tests/test_boot_logs_visibles.py` garde l'ORDRE. **Une instrumentation qui ne
   journalise pas est pire que pas d'instrumentation** : on la croit posée.
4. **L'ordre du boot est rejouable hors du démarrage** : `apply_boot_schema(conn)` +
   `replay_boot_schema_dry(conn)` (transaction annulée), gardés par
   `tests/test_boot_order_replay.py`, et jouables contre une base servie par
   `oto-mcp maintenance check-boot`. C'est le garde-fou qui manquait le 27/08 (#450) :
   un index posé dans le DDL sur une colonne née d'un `ALTER` — ni le DDL seul ni la
   migration seule ne pouvaient l'attraper, **seul leur ordre échouait**.
   ⚠️ **Ce garde-fou a été AVEUGLE à ce piège jusqu'au 2026-09-01 (#781)** : il ne
   bootait qu'une base **vierge**, où le `CREATE TABLE IF NOT EXISTS` pose la colonne
   inline et où l'index la trouve donc toujours. `check-boot`, lui, faisait la bonne
   chose — il rejoue contre la base **servie** — mais il n'est dans aucun contrôle
   automatique : il faut y penser, c'est de la discipline, pas un cliquet. Depuis
   #781 la CI joue le cas : elle retire du schéma neuf chacune des colonnes que le
   boot pose par `ALTER` (relevées sur le SQL exécuté, pas lues dans le source) et
   rejoue la séquence sur la base amputée, en exigeant qu'elle passe **et** qu'elle
   converge vers le schéma neuf. `check-boot` reste utile pour ce que la CI ne peut
   pas avoir : la vraie base, avec ses vraies données et son vrai historique.

### 1.4 Le défaut que le lot 0 a mis au jour : la purge annulait l'archive

Le boot supprimait le journal à **30 jours, sans l'archiver**. Le timer
`oto-journal-archive` posé le 2026-08-27 exporte au froid S3 les **mois entiers** au-delà
de **90 jours** — il n'aurait donc jamais trouvé un seul mois à prendre. Mesuré le
28/08 : `tool_calls` = 969 314 lignes, dont **0 au-delà de 30 jours**. Ce n'était pas une
politique de rétention en double, c'était une politique qui en annulait une autre en
silence, et la plus courte gagnait.

Depuis le lot 0, la rétention du journal a **un seul propriétaire** : l'archive, qui
exporte puis supprime, à `OTO_JOURNAL_RETENTION_DAYS` (90 par défaut, la même variable
des deux côtés). Le boot ne purge plus rien ; la moitié « runs devenus orphelins » de
`prune_tool_calls` est passée dans la commande de maintenance, à la même borne.

**Ce que ça change en volumétrie, et pourquoi ce n'est pas un problème de latence** : le
journal en ligne passe d'un mois à trois-quatre (360 Mo de table + 168 Mo d'index →
environ le triple ; base entière 923 Mo aujourd'hui), et le premier vrai archivage
tombera le 2026-12-03 (le premier mois entièrement passé sous la nouvelle borne est août
2026). Les lectures servies, elles, **ne ralentissent pas** : mesuré le 28/08, le bloc
`tool_call_stats` sur sa fenêtre par défaut coûte 309 ms et passe par un *Index Only
Scan* sur `idx_tool_calls_kind (kind, created_at)` — **borné par la fenêtre demandée, pas
par la taille de la table** ; idem pour le listing admin (`idx_tool_calls_created_at`,
0,4 ms) et la vue par org (`idx_tool_calls_org`, 3,2 ms). Ce qui grandit est le disque,
pas le temps de réponse.

## 2. L'inventaire

### 2.1 Le compte exact

`_init.py` contient **143 occurrences** de la chaîne `ALTER TABLE`. Elles ne sont
pas homogènes, et c'est structurant pour toute solution :

| | n | remarque |
| --- | ---: | --- |
| en prose (docstring) | 1 | pas un ordre |
| de forme canonique (`ALTER TABLE <t> <action> <cible>`) | 136 | inventoriés ci-dessous |
| **hors forme** | 6 | 2 construites en `f"…{_t}…"` dans une boucle sur des tables, 3 `ADD PRIMARY KEY` (sans `IF NOT EXISTS` : l'idempotence tient à un `if` **Python**, pas au SQL), 1 `RENAME TO` |
| **total des ordres** | **142** | |

À l'exécution, le boot en régime de production émet **154** `ALTER TABLE` : les
boucles déplient les formes dynamiques. Toute solution « un fichier = une
migration » doit décider quoi faire des ordres générés, et des trois `ADD PRIMARY
KEY` dont l'idempotence n'est pas dans le SQL.

### 2.2 Par domaine

Domaines au sens de la découpe du DDL (`db/schema/<domaine>.py`).

| domaine | ALTER | dont `ADD COLUMN` | dont **inertes** | autres |
| --- | ---: | ---: | ---: | ---: |
| `projects` | 28 | 25 | 21 | 3 |
| `orgs` | 25 | 21 | 7 | 4 |
| `usage` | 15 | 13 | 12 | 2 |
| `procedures` | 14 | 8 | 7 | 6 |
| `users` | 12 | 2 | 2 | 10 |
| `datastore` | 11 | 8 | 6 | 3 |
| `connectors` | 8 | 6 | 6 | 2 |
| `unipile` | 8 | 8 | 7 | 0 |
| `tenants` | 7 | 7 | 4 | 0 |
| `guides` | 2 | 2 | 1 | 0 |
| `runs` | 2 | 2 | 2 | 0 |
| `tokens` | 2 | 2 | 2 | 0 |
| `billing` | 1 | 1 | 1 | 0 |
| `grants` | 1 | 1 | 1 | 0 |
| **total** | **136** | **106** | **79** | **30** |

### 2.3 Par date d'apparition

Datation par `git log -S` sur le fragment SQL propre à chaque ordre
(`oto_mcp/db/_init.py` et son ancêtre `oto_mcp/db.py`), premier commit qui
l'introduit :

| mois | ordres introduits |
| --- | ---: |
| 2026-06 | 53 |
| 2026-07 | 67 |
| 2026-08 | 14 |

Deux ordres non datables (formes dynamiques : leur texte n'existe pas littéralement
dans l'historique). **Rien n'a jamais été retiré** : la file est strictement
croissante depuis l'origine du fichier. La seule raison pour laquelle elle ne
grandit pas plus vite est que l'essentiel du DDL neuf part directement dans
`_schema.py`, où il est déclaratif — et donc gratuit au boot suivant.

### 2.4 Les ordres qui touchent des DONNÉES

Ce sont eux, et eux seuls, dont le coût varie avec la taille de la base. Ils sont
idempotents **par prédicat**, jamais par marqueur : ils reposent la question à
toute la table à chaque démarrage. Dans le source de `_init.py` :

- **8 `INSERT … SELECT`** : seed du tenant 1, conversion des instructions plateforme
  en `guides`, fusion des procédures d'équipe dans `org_instructions` (+ révisions),
  dépliage de `user_disabled_tools`/`user_enabled_tools` par org, conversion
  `org_connector_access`/`group_connector_access` → `connector_acl`.
- **14 `UPDATE`**, dont plusieurs balayages complets : `docs`, `projects` et `guides`
  re-marqués `embed_dirty` par un `NOT IN (SELECT …)` sur la table d'embeddings ;
  renumérotation de `docs.position` par fonction de fenêtre `ROW_NUMBER() OVER
  (PARTITION BY project_id, parent_id)` ; réécriture du `schema` JSONB de tous les
  `user_datastores` ; adossement `orgs.kb_project_id`.
- **3 `DELETE FROM`** : `project_links` de type `doc`, l'instruction plateforme
  `onboarding`, et un `DELETE FROM {_t} WHERE org_id = 0` généré en boucle.

À l'exécution, ces 25 ordres écrits en deviennent **40** (20 `UPDATE`, 13 `DELETE`,
7 `INSERT`), les boucles et les conversions appelées dans `db/nodes.py` fournissant
le reste — encore un point où le compte du fichier ne dit pas le compte du boot.

> Le `UPDATE docs SET embed_dirty = TRUE WHERE embed_dirty = FALSE AND id NOT IN
> (SELECT doc_id FROM doc_embeddings)` est l'exemple canonique : il est *correct*,
> il est *idempotent*, il ne coûte rien sur une base de test — et il relit la
> totalité de `docs` et de `doc_embeddings` à chaque redémarrage, pour un travail
> fait une fois en juillet.

### 2.5 Les 79 `ADD COLUMN` devenus inertes

Un `ALTER TABLE t ADD COLUMN IF NOT EXISTS c` est **inerte** dès lors que `c`
figure aussi dans le `CREATE TABLE` de `_SCHEMA` : une installation neuve reçoit la
colonne par le DDL de base, et une base existante l'a reçue par l'ALTER, une fois,
il y a des semaines. C'est le cas de **79 des 106** `ADD COLUMN` — 21 sur
`projects`, 12 sur `usage`, 7 sur `procedures`, `orgs` et `unipile`…

**C'est le seul gain que cette note identifie comme indépendant des trois
options** : ces 79 ordres sont supprimables sans changer de régime, à une condition
qui se vérifie en une requête sur les bases réelles — que la colonne y soit
effectivement présente :

```sql
-- Pour chaque (table, colonne) de la liste des 79 : doit rendre 79 lignes.
SELECT table_name, column_name FROM information_schema.columns
WHERE table_schema = 'public' AND (table_name, column_name) IN ( … );
```

⚠️ **Ce n'est pas gratuit pour autant.** Retirer l'ALTER rend le DDL de base **seul**
porteur de la colonne. Une base restaurée depuis une sauvegarde antérieure à
l'ALTER, ou un environnement oublié qui n'a pas booté depuis, ne rattrapera plus
jamais la colonne — et le symptôme sera une `UndefinedColumn` à l'exécution, pas au
boot. La vérification doit donc couvrir **toutes** les bases servies, pas seulement
celle de production. C'est aussi, exactement, l'auto-réparation dont l'option B du chantier dit
qu'on la perd : la retirer ici en est le premier acompte.

### 2.6 Les 30 ordres non additifs

Ceux qu'aucune migration ne peut rejouer à l'aveugle, et qui sont la raison d'être
de la « danse en N lots ». Datés :

| date | ordre |
| --- | --- |
| 2026-06-09 | `DROP CONSTRAINT connector_credentials_pkey` |
| 2026-06-11 | `DROP COLUMN connector_credentials.secret` |
| 2026-06-13 | `RENAME COLUMN tool_calls.tool_name`, `tool_calls.called_at` |
| 2026-06-15 | `ALTER COLUMN users.role` |
| 2026-06-16 / 06-22 | `ALTER COLUMN org_invitations.org_id`, `.email` |
| 2026-07-01 | `ALTER COLUMN org_instructions.id` (×2), `DROP CONSTRAINT project_links_…_key`, `DROP COLUMN user_account_profile.{onboarded, onboarded_at, discovery_project_id}` |
| 2026-07-03 | `DROP COLUMN orgs.default_tools`, `org_groups.default_tools` |
| 2026-07-08 | `DROP COLUMN users.{access_status, invite_quota, invited_by, access_granted_at, referral_code}` |
| 2026-07-10 | `DROP COLUMN user_datastores.{sub, spreadsheet_id, owner_email}`, `ALTER COLUMN org_instruction{,_revision}s.owner_id`, `DROP CONSTRAINT org_instruction{,_revision}s_pkey` |
| 2026-07-20 | `ALTER COLUMN doc_change_requests.doc_id`, `ADD CONSTRAINT dcr_target` |
| (dynamique) | `DROP COLUMN users.{col}` généré en boucle |

Tous ont plus d'un mois. Aucun ne peut être supprimé sans la vérification de §2.5,
et pour les `DROP` la vérification est inverse (la colonne doit être **absente**
partout).

### 2.7 Deux défauts constatés au passage

Relevés pendant l'inventaire de découpe ; le premier a été traité par le lot 0 de
l'ADR 0065 (2026-08-28), le second reste ouvert.

1. **`migrate_business_key_indexes()` ne tournait jamais** — *traité, oto-backend#421*.
   Le lot 0 a retiré l'appel mort et en a fait `oto-mcp maintenance
   key-index-rebuild`, **hors du timer** : la fonction est maintenant appelable, mais
   la faire tourner pour la première fois reste une décision (elle reconstruit tous
   les index de clé métier de la production). Le constat d'origine : Elle est appelée à la
   dernière ligne d'`init_db`, *après* la boucle de retry — dont le corps `return`
   en cas de succès et `raise` à la dernière tentative. Aucun chemin n'atteint donc
   l'appel, et `attempts` ne peut pas valoir 0 (`max(1, …)`). **Vérifié
   empiriquement** : `init_db()` instrumenté sur un PostgreSQL jetable ne l'appelle
   pas. Le commentaire au-dessus explique soigneusement pourquoi elle doit vivre
   hors transaction (`CREATE INDEX CONCURRENTLY`, #318) — l'intention est claire,
   c'est le placement qui est faux. Impact : la matérialisation des index de clé
   métier ne s'est jamais faite au boot ; `_ensure_datastore_key_indexes()`, elle,
   tourne bien et couvre un besoin voisin, ce qui explique que ça ne se soit pas vu.
2. **`idx_doc_change_requests_doc` est posé loin de sa table.** L'index vit à la fin
   du bloc des embeddings, entre `datastore_row_embeddings` et `project_activity`,
   alors que `doc_change_requests` est déclarée ~90 lignes plus haut. Sans
   conséquence à l'exécution (l'ordre reste valide), mais c'est une trace de
   sédimentation : la découpe par domaine l'a laissé au début du fragment
   `projects.PROJECT_FILES` plutôt que de le déplacer, le lot étant un déplacement
   pur.

## 3. La contrainte que toute solution doit respecter

**Prod et preprod partagent LA MÊME base** (`docs/live-migrations.md`). Ce fait ne
change avec aucune des trois options, et il en découle trois invariants :

1. **La « danse en N lots » survit intacte.** Un DDL destructif reste découpé en
   lots promus séparément, chacun ne détruisant que ce que le code prod *courant*
   ne référence plus. Versionner les migrations ne rend pas les `DROP` sûrs — ça ne
   fait que les jouer une seule fois.
2. **Migrer « avant le restart » migre aussi la prod.** Le script de deploy de la
   preprod tourne sur la base de la prod. La seule fenêtre de test reste le
   décalage entre les deux redémarrages, comme aujourd'hui.
3. **Un rollback ne rembobine pas le DDL.** `oto-backend.sh` rollback en
   redéployant le tag précédent ; la base, elle, reste migrée. C'est déjà vrai, et
   c'est ce qui rend la porte de version de l'option A coûteuse (au chantier, §4).

## 4. Le régime de migration — au chantier

> Les **trois options** du 27/08 avec leurs risques (ex-§4) et les **six questions**
> que le choix devait trancher (ex-§5) vivent dans `oto-private`,
> `docs/chantiers/chantier-migrations-versionnees.md`, avec l'ordre des lots qui
> restent et l'état de chaque question. Le choix est fait — **ADR 0065, option C** —
> et son lot 0 est livré (§1.3).
>
> *(Il n'y a plus de §5 : les numéros des sections suivantes sont conservés tels
> quels pour ne pas casser les renvois existants.)*

## 5. L'outil est posé — Alembic, sans ORM (14/09/2026)

Le registre n'est plus à écrire : c'est **Alembic**, l'outil standard, en mode SQL. Ce
qu'on lui prend, c'est ce qu'on n'a pas envie d'écrire soi-même — le registre de ce qui
a déjà tourné, l'ordre garanti quand deux branches ajoutent une migration la même
semaine, l'essai à blanc, et la pose d'un point de départ sur une base vivante.

**Ce qu'on ne lui prend pas** : l'ORM. Il n'y a pas de métadonnées cibles, donc pas de
détection automatique — chaque migration s'écrit en SQL, à la main. La détection
n'aurait de toute façon su qu'ajouter des colonnes, et c'est déjà le travail du boot.

| fichier | ce qu'il porte |
|---|---|
| `alembic.ini` | l'emplacement des révisions. **Aucun DSN** |
| `oto_mcp/db/migrations/env.py` | la connexion, lue dans `DATABASE_URL` comme le pool applicatif, et le **verrou consultatif** |
| `oto_mcp/db/migrations/versions/` | une révision par changement |
| `tests/test_migrations_registre.py` | la file reste unique : un seul point de départ — la référence (§5.4) —, une seule fin, chaque révision décrite, aucune révision retirée qui revienne |
| `oto_mcp/db/_version_alembic.py` | la version d'une base **neuve**, posée par le démarrage (§5.2) ; la référence du registre et le refus nommé d'une base plus ancienne (§5.4) |
| `tests/test_boot_pose_la_version.py` | les cas du §5.2 et le refus du §5.4, sur vraie base |

**Le verrou n'est pas fourni par l'outil** : Alembic n'en pose aucun. `env.py` prend un
verrou consultatif PostgreSQL avant d'écrire et le rend ensuite. Ce n'est pas une
précaution théorique — la base est partagée entre la préproduction et la production, et
le déploiement est bleu/vert.

Les commandes passent par `oto-mcp migrer` (`oto_mcp/migrer.py`, oto-backend#1105) : les
arguments de la commande `alembic`, tels quels, sur la configuration du dépôt — `alembic.ini`
de l'arbre et le registre là où le démarrage le lit, quel que soit le répertoire courant ; la
base est celle de `DATABASE_URL`, lue par `env.py`.

```bash
oto-mcp migrer upgrade head --sql   # l'essai à blanc : imprime, n'écrit rien
oto-mcp migrer upgrade head         # applique
oto-mcp migrer revision -m "ce que ça fait"
oto-mcp migrer current              # où en est CETTE base
```

**Sur la box**, `DATABASE_URL` n'est dans aucun `.env` : elle n'existe que dans
l'environnement que construit le lanceur de secrets (`deploy/lanceur_secrets.py`), qui
exécute `oto-mcp <args>` de son arbre. La commande exacte, en root, avec les fichiers
d'environnement et la clé d'API de l'unité de prod (`oto-mcp@.service`), depuis l'arbre de la
couleur qui sert — celui qui porte le registre déployé :

```bash
A=/opt/oto-mcp-$(cat /etc/oto-mcp/active-prod) && systemd-run --pipe --wait --quiet --collect -p WorkingDirectory=$A -p EnvironmentFile=/opt/oto-mcp/.env -p EnvironmentFile=/etc/oto-mcp/lanceur-prod.env -p LoadCredential=scw:/etc/oto-mcp/scw.key $A/.venv/bin/python $A/deploy/lanceur_secrets.py migrer upgrade head
```

`migrer current`, `migrer upgrade head --sql` : même ligne, autres arguments. Le fichier de
port de l'unité n'y est pas : la migration ne sert rien. `migrer` joue le registre de
**l'arbre** qui l'exécute — une révision absente de cet arbre n'est pas jouée. La base est
partagée : une seule application vaut pour la prod et la préprod. Le banc
`tests/deploy/test_maintenance_lanceur_967.py` garde cette ligne alignée sur les fichiers
d'environnement des unités.

**Le banc : `scripts/essai_migrations.sh`.** Il monte un PostgreSQL 17 jetable dans un
conteneur, applique pour de vrai, pose une migration, la défait, et vérifie qu'une
migration concurrente **attend** le verrou. Il ne touche à aucune base réelle.

> **Pourquoi ce banc existe, et ce qu'il a déjà attrapé.** L'essai à blanc n'ouvre
> aucune connexion : il a rendu un SQL parfait alors que l'application réelle
> **n'écrivait rien**, sortait avec un code zéro et laissait la base intacte. La cause :
> prendre le verrou ouvrait une transaction implicite, Alembic voyait une transaction
> déjà en cours, sa propre transaction ne faisait plus rien, et tout était annulé à la
> fermeture de la connexion. Le succès déguisé parfait — invisible sans une vraie base.

⚠️ **Sur une base qui existait avant le registre** (la nôtre), un geste se fait une seule
fois : `alembic stamp head`. Il écrit que le point de départ est atteint, sans rien
rejouer. C'est une écriture sur la base de production — elle passe par la procédure de
production, pas par un déploiement. **Une base NEUVE, elle, reçoit sa version du
démarrage** (§5.2).

### 5.1 Chaque révision suivante s'applique À LA MAIN, pas au déploiement (17/09/2026)

**Rien n'appelle `alembic upgrade` automatiquement sur NOTRE box** : ni le pipeline de
déploiement (`.github/`), ni le démarrage du serveur (`server.main`/`db._init`). C'est voulu
— le chantier qui câblerait cet appel dans notre déploiement **est en pause, sur décision
d'Alexis** ; le poser par la bande à l'occasion d'une migration de perf serait rouvrir
ce chantier sans l'avoir décidé.

**Une instance cible, elle, migre à la montée** (oto-backend#1163, puis #1195 sur décision
d'Alexis du 09/10/2026, pour qu'une cible suive le tronc sans geste humain) : son déploiement
(`deploy/cible/deployer.sh`) amène la base du rôle à la tête du registre DU TAG avant de
démarrer la couleur neuve — `migrer upgrade head` depuis l'arbre du tag, sur une chaîne
linéaire seulement — et refuse tout autre écart en le nommant (docs/instance-cible.md,
§ Les migrations, jouées par la montée).

Donc, sur notre box, une révision au-delà de la référence (ex.
`0042_orgs_suspension_par_tenant`) ne prend effet qu'après un geste D'EXPLOITATION, manuel,
joué par qui déploie — la même
procédure que `stamp head` ci-dessus : `oto-mcp migrer upgrade head` sur la box, par le
lanceur (la ligne `systemd-run` du §5).

**L'ordre entre ce geste et le tag applicatif se lit dans l'en-tête de chaque révision.**
Deux régimes :

- **avant la fusion** — le code du lot LIT ou ÉCRIT ce que la révision pose (une colonne
  lue à chaque requête, une contrainte dont le code ne se garde plus) : jouée après, la
  préproduction répondrait `UndefinedColumn`. L'ancien code ignore l'ajout : la jouer
  avant le déploiement est sûr. Prod et préprod partagent la base ;
- **ordre indifférent** — la révision pose un objet neuf que le démarrage d'une base
  neuve pose aussi (`CREATE TABLE IF NOT EXISTS`, même fragment `db/schema/…`), ou un
  index que l'ancien code profite déjà d'avoir : les deux sens sont sûrs, seulement plus
  ou moins rapides.

⚠️ L'identifiant d'une révision tient en **32 caractères** : `alembic_version.version_num`
est un `VARCHAR(32)`, que le démarrage d'une base neuve estampille à la tête du registre
(§5.2) — un identifiant plus long y fait échouer chaque démarrage à neuf.

**Un index sur une grosse table servie** se pose CONCURRENTLY, sous le verdict commun
d'`oto_mcp/db/index_concurrent.py` (révision : `poser_par_revision` ; démarrage d'une base
neuve : `poser_au_demarrage`, non concurrent). Au-delà du seuil de taille (estimé par
`pg_class.reltuples`), ni la révision ni le démarrage ne le construisent — mesuré en
production, 172 s pour environ 12 M lignes de `tool_calls`, au-delà des 120 s de la
fenêtre de démarrage : la révision lève `ConstructionManuelleRequise`, le démarrage le dit
en erreur et continue. Le geste manuel est une **commande versionnée**, jouée par le
lanceur comme `migrer` (la ligne `systemd-run` du §5), puis la révision rejouée constate
les index et ne construit rien :

```
oto-mcp maintenance index-concurrents 0049      # ou l'identifiant complet de la révision
oto-mcp migrer upgrade head
```

Elle prend les index de la révision dans le registre unique du code
(`index_concurrent.declares()`, gardé par un banc contre toute déclaration oubliée) et,
un par un, sur une connexion hors transaction (`statement_timeout = 0`,
`lock_timeout = '5min'`) : un index déjà valide est laissé tel quel ; un index absent est
construit (`IndexConcurrent.ddl_concurrent`, le `CREATE` exact tiré du code — une
expression qui diffère d'un caractère de celle de la requête ne sert aucune lecture), sa
durée journalisée, puis `indisvalid` exigé ; un index **INVALIDE** arrête tout, sans
construire les suivants, et la commande **nomme** le `DROP INDEX CONCURRENTLY IF EXISTS
<nom>` à jouer **sans le jouer** — un index cassé reste un geste humain (décision
d'Alexis, 09/10/2026), qu'on fait suivre de la même commande. Sortie : `0` posés,
`1` refus (révision sans index de ce régime, index invalide), `2` erreur (base
injoignable, construction interrompue : la relancer, elle dira si l'index est resté
invalide). Aucun plafond de taille : c'est tout l'objet du geste.

Un index sans révision (posé avant la référence du registre, §5.4 —
`idx_tool_calls_org_tool_ok`, les deux index de la recherche dans les valeurs) n'a pas de
commande : sur une base vivante il existe déjà, et sa procédure reste le SQL brut
(`SET statement_timeout = 0; SET lock_timeout = '5min'; DROP INDEX CONCURRENTLY IF EXISTS
<nom>; CREATE INDEX CONCURRENTLY …;` puis vérifier `indisvalid`).

⚠️ **`lock_timeout` de quelques minutes, pas de quelques secondes** : la phase
concurrente attend la fin de chaque transaction plus ancienne qu'elle par une attente
de verrou sur son `virtualxid`, et `lock_timeout` coupe aussi ces attentes. À 2 s, la
construction a échoué en production derrière une transaction `idle in transaction`
(`LockNotAvailable`), laissant l'index **invalide** au catalogue. Pendant cette attente,
seul un `ShareUpdateExclusiveLock` est demandé sur la table : ni les lectures ni les
écritures ne sont bloquées.

⚠️ **Un index invalide ne se répare pas tout seul** : `IF NOT EXISTS` le prend pour
fait, et il ne sert aucune lecture. D'où la vérification d'`indisvalid` après chaque
construction, et le `DROP INDEX CONCURRENTLY` avant de reconstruire. Ni la révision ni
le démarrage ni la commande ne le font à votre place : la révision et la commande lèvent
`IndexInvalide` en donnant ces gestes, le démarrage le dit en erreur et continue.

**Le registre au 06/10/2026** (squash, §5.4) — la liste par révision qui précédait, de
`0001_point_de_depart` à `0040_tenants_desactivation`, est archivée dans git (le tag
`v1.441.0` porte ce document entier, révisions et en-têtes compris) :

| révision | statut | ce qu'elle porte |
|---|---|---|
| `0041_recherche_valeurs_servies` | **référence** — vide, sans précédente | rien : elle documente « schéma = celui du démarrage au 06/10/2026 ». Y monter lève, en descendre lève |
| `0042_orgs_suspension_par_tenant` | vivante | `orgs.suspended_tenant_id BIGINT`, nullable sans défaut ni index (écriture de catalogue ; `AccessExclusiveLock` bref sur `orgs`, attente bornée par `lock_timeout` 5 s). NULL = suspension posée sur l'org ; sinon le tenant dont la désactivation l'a posée, et que sa réactivation lève. **Avant la fusion** (oto-backend#1165). Retour arrière : retire la colonne |
| `0048_origine_ecritures_retiree` | vivante | `DROP TABLE IF EXISTS origine_ecritures` (le relevé du préavis d'oto#70, sans lecteur ni écrivain), seulement si la table existe (`to_regclass`, sans verrou) ; là où elle existe, `AccessExclusiveLock` bref sur elle seule, attente bornée par `lock_timeout` 5 s. **Après le tag** : l'ancien code l'écrit et son démarrage la recrée (oto-backend#1109). Retour arrière : lève (irréversible) |
| `0049_tool_calls_ouvertures_runs` | vivante | trois index partiels des ouvertures de runs (`WHERE tool = 'run_start' AND run_id IS NOT NULL`) : `idx_tool_calls_run_start (created_at DESC, id DESC)`, `idx_tool_calls_run_start_org (org_id, created_at DESC, id DESC)`, `idx_tool_calls_run_start_sub (sub, org_id, created_at DESC, id DESC)` — le choix de page des listes de runs (`usage._derniers_runs`, infra#9). CONCURRENTLY sous le verdict commun : sur la base servie, **geste manuel** (ci-dessus : `oto-mcp maintenance index-concurrents 0049`), la révision rejouée le constate. `ShareUpdateExclusiveLock` sur `tool_calls`, rien de bloqué. **Ordre indifférent.** Retour arrière : `DROP INDEX CONCURRENTLY` des trois |

Les bases vivantes ce jour-là : la base partagée prod/préprod, en `0042`, et une instance
dédiée (prod et préprod), en `0041`. `0042` vit tant que cette instance ne l'a pas reçue.

### 5.2 Une base neuve naît à la tête du registre (24/09/2026, oto-backend#969)

Une base neuve reçoit tout son schéma du démarrage : chaque colonne qu'une révision pose
sur une base existante est aussi dans le `CREATE TABLE` de son fragment. Elle naît donc
**dans l'état d'après toutes les révisions** — et, jusqu'ici, sans rien dans
`alembic_version`. Un `alembic upgrade head` joué plus tard sur elle aurait rejoué tout
le registre sur un schéma qui le porte déjà. Le geste juste sur une base neuve est
`stamp head`, jamais `upgrade` depuis zéro ; c'est le démarrage qui le fait
(`oto_mcp/db/_version_alembic.py`, appelé par `apply_boot_schema`) :

| état constaté **avant le premier ordre du démarrage**, sous son verrou consultatif | ce que fait le démarrage |
|---|---|
| **neuve** — le schéma courant ne contient aucune table | crée `alembic_version` (forme d'Alembic) et y écrit la tête du registre, **en fin de la même transaction** que le schéma |
| **versionnée** — `alembic_version` existe | n'y touche jamais ; sa tenue est à Alembic (§5.1) |
| **antérieure à la référence** — `alembic_version` porte une révision retirée par un squash | **refuse de démarrer**, en nommant la révision, la référence et le tag qui la monte d'abord (§5.4) |
| **sans version** — des tables, pas d'`alembic_version` | n'estampille pas (on ignore quelles révisions elle a reçues) et **le dit en erreur** à chaque démarrage |

Ce n'est pas une migration (ADR 0065) : aucune révision n'est exécutée, seule la version
est écrite, et seulement là où le démarrage vient de créer le schéma entier. La tête est
lue dans le registre (`oto_mcp/db/migrations/versions/`) ; un registre à plusieurs têtes
fait échouer le démarrage d'une base neuve plutôt que d'en choisir une.

**Une base « sans version »** se traite à la main : établir la dernière révision que son
schéma porte déjà (lire l'en-tête de chaque révision et le catalogue), puis
`alembic stamp <révision>` et `alembic upgrade head` — depuis l'arbre du tag d'avant le
squash si cette révision est antérieure à la référence (§5.4).

**Ce que la naissance exige, vérifié avant le premier ordre** (`oto_mcp/db/_prerequis.py`,
28/09/2026) : refus nommé (`PrerequisBaseManquant`) au lieu d'une erreur PostgreSQL brute
au milieu de la transaction.

| prérequis | exigé de | ce qu'il faut fournir |
|---|---|---|
| un schéma courant où le rôle a `CREATE` | une base **neuve** seulement | `GRANT CREATE ON SCHEMA <s> TO <rôle>` ou un rôle propriétaire |
| l'extension `vector` (pgvector) | toute base où elle **manque** | un serveur qui la propose ; la créer exige un superutilisateur — sinon `CREATE EXTENSION vector` joué une fois par un administrateur |
| l'extension `pg_trgm` | toute base où elle **manque** | extension de confiance : le droit `CREATE` sur la base suffit, sinon même geste |

Une base existante où les deux extensions sont déjà posées (la nôtre) n'exige **aucun
droit de plus** qu'avant. Le tenant primaire, semé dans la même transaction, suit la
déclaration de l'instance (`docs/tenants.md`, §« Le tenant primaire est une
déclaration »). Banc : `tests/test_naissance_d_une_base.py`.

⚠️ **Ce que le stamp suppose** : que le démarrage produise bien l'état de la tête — c'est
la discipline de chaque révision (poser aussi sa colonne dans le fragment du schéma).
Rien ne compare encore le schéma d'une base neuve à celui d'une base remise à niveau par
`upgrade`.

### 5.3 Un retrait passe TOUJOURS par une révision (06/10/2026, oto-backend#1162)

Un retrait — `DROP COLUMN`, `DROP TABLE`, `DROP CONSTRAINT`, tout changement non additif —
**s'écrit dans une révision du registre**, jamais en geste manuel sur la base de
production, **même si cette base est partagée** et qu'un seul geste suffirait pour la
préprod et la prod. Le registre est la seule trace qu'une autre base rejoue : une
instance cible, une base née avant le geste, une base de test. Un geste joué à la main
n'existe que là où il a été joué.

**Un commentaire de code n'est jamais la trace d'un DDL.** « Son `DROP` n'est pas ici :
geste de l'opérationnel » ne se rejoue nulle part. Vécu sur
`unipile_accounts.feed_synced_at` : le commentaire de `_init.py` a été suivi en
production, la colonne est restée sur une instance née avant, et l'import de périmètre
l'a refusée ; la révision `0039_feed_synced_at_retiree` a rattrapé le geste.

Ce qui ne change pas : un retrait n'est **jamais au démarrage** (ADR 0065, base
partagée), il attend que plus aucun code servi ne lise l'objet (`docs/live-migrations.md`,
« la danse en N lots »), et la révision le rend **idempotent** — constat au catalogue
avant l'ordre, pour ne prendre aucun verrou là où il est déjà fait. Son retour arrière
lève plutôt que de recréer un objet vide.

### 5.4 Une révision ne vit que tant qu'une base vivante est en retard sur elle (06/10/2026, oto-backend#1162)

Une base neuve ne rejoue aucune révision : elle naît à la tête (§5.2). Le registre ne sert
donc qu'aux bases **en retard** — et une révision que toutes les bases vivantes ont déjà
reçue ne sert plus à rien, sinon à coûter : un banc par révision qui la rejoue sur un
schéma ramené en arrière, des aides gardées pour elle seule, une liste à relire.

**La règle.** Une révision vit tant qu'une base vivante est en retard sur elle. Quand
toutes l'ont dépassée, elle part, au **squash** suivant :

1. recenser les bases vivantes et la révision que porte chacune (`migrer current`, en
   lecture seule) — une base qui n'est plus servie n'en est pas une, et ne conditionne
   rien ;
2. la plus ancienne révision portée devient la **référence** : même identifiant (les bases
   le portent dans `alembic_version`), `down_revision = None`, aucun ordre — son
   `upgrade()` et son `downgrade()` lèvent ; son en-tête dit « schéma = celui du démarrage
   à cette date ». Ce qu'elle posait est déjà dans le démarrage d'une base neuve ;
3. toutes les révisions qui la précèdent sont supprimées du registre, et leurs bancs avec
   elles ; un banc qui tenait un comportement durable (ce que le démarrage pose, ce que la
   base refuse) est réécrit sans révision ;
4. dans `oto_mcp/db/_version_alembic.py`, une entrée `Squash` de plus dans `SQUASHS` :
   la référence, la date, les identifiants retirés, et le **dernier tag** qui les porte
   encore. Les entrées précédentes restent : une base très ancienne dit encore quel tag la
   monte ;
5. ici, la liste par révision du §5.1 est remplacée par la référence et les révisions
   vivantes ; l'ancienne reste dans git.

**Une base plus ancienne que la référence est refusée en la nommant**, partout où une base
se lit :

| qui | ce qu'il fait d'une base à une révision retirée |
|---|---|
| le démarrage (`_version_alembic.constater`, sous son verrou, avant son premier ordre) | refuse de démarrer (`BaseAnterieureALaReference`) |
| `oto-mcp migrer` et toute commande d'Alembic (`env.py`, avant `upgrade`, `downgrade`, `current`, `stamp`) | sort en la nommant — jamais sur le « Can't locate revision identified by … » d'Alembic, qui ne dit pas quoi faire |
| `deploy/cible/migrations_a_jour.py` | refuse la montée (code 1), sans proposer `upgrade head` du tag qu'on monte |

Le message : « révision `<x>` antérieure à la référence `<référence>` (squash du
`<date>`) : monter d'abord cette base avec un tag antérieur au squash (`<tag>`) ». Le geste :
installer ce tag dans un arbre, y jouer `migrer upgrade head` (la base passe la référence),
puis monter le code d'après le squash. Aucun repli : le registre d'après ne sait pas
rejouer ce qu'il n'a plus.

⚠️ **Une révision inconnue n'est pas une révision retirée.** Une base migrée par le tag
SUIVANT (une révision « avant la fusion », jouée sur la base partagée) porte une révision
que le code qui sert ne connaît pas encore : le démarrage la laisse passer — la refuser
casserait le bleu/vert et la préprod qui partage la base. Seuls les identifiants retirés,
nommés dans `SQUASHS`, sont refusés au démarrage ; `migrations_a_jour.py`, lui, refuse
toute révision inconnue du tag avant une montée (docs/instance-cible.md).

Le premier squash, le 06/10/2026 : quarante révisions retirées (`0001` à `0040`), référence
`0041_recherche_valeurs_servies`, dernier tag qui les porte `v1.441.0`. Bancs :
`tests/test_migrations_registre.py`, `tests/test_boot_pose_la_version.py`,
`tests/deploy/test_migrations_cible_1163.py`.

## 6. Références

- `docs/live-migrations.md` — la danse en N lots, les techniques et les pièges déjà
  payés sur la base partagée. **À lire avant toute migration destructive**, quelle
  que soit l'option retenue.
- `oto_mcp/db/_schema.py` — le DDL déclaratif, assemblé par domaine depuis le
  2026-08-27 (`db/schema/<domaine>.py`), gelé fragment par fragment dans
  `tests/schema_gele/` (un `.sql` normalisé par fragment, plus `ORDRE`) et comparé par
  `tests/test_schema_assembly_frozen.py`. Une révision qui pose aussi sa colonne dans
  un fragment régénère le figé en local (`python -m scripts.schema_gele --regen`) et
  commite son diff avec elle ; plus d'empreinte à recalculer (oto-backend#789).
- `oto_mcp/db/_init.py` — l'objet de cette note.
- ADR 0020 (stratégie de release) et CLAUDE.md racine §Déploiement — le modèle
  tronc unique et la fenêtre de healthcheck.

## Le chiffre de boot depuis le lot 0

> Reprise mot pour mot du `CLAUDE.md` (2026-08-31).

Boot prod/préprod mesuré 36-39 s, dont **≈ 1,5 s de base** depuis le lot 0 — le reste est
l'import Python et les deux `_build_mcp`. Un lot qui ajoute un travail one-shot au boot doit
le mesurer **avant** de poser son tag.

Le drapeau de module qui garde cet appel unique s'appelle `_PREPARED` (`server._prepare_database`).
