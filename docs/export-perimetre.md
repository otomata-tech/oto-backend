---
title: Export par périmètre de propriétaire
type: reference
description: >-
  Extraire d'une base partagée tout ce qui appartient à un propriétaire, et rien
  d'autre, pour le verser dans une instance dont la base est née par `init_db`,
  AVANT le premier démarrage de l'app (oto-backend#1088, #1161, ADR 0070 §7.6). Le
  classement déclaré de chaque table (possédée, indirecte, instance, exclue), le refus d'une table non classée, le
  périmètre dérivé d'orgs déclarées (et de leur tenant), les lignes d'anciens comptes
  (rattachées au jumeau, sinon omises, comptées), les refus de l'extraction, et
  l'import dans une base née par `init_db` et jamais démarrée (refus nommé de toute
  table écrite déjà semée) : tenant sur la ligne 1, comptes dénudés, secrets rechiffrés à l'export, vérification par relecture, commande
  `oto-mcp perimetre`. Le journal d'appels hors de la fenêtre de coupure : export
  principal sans journal, tranches de dates poussées la veille puis diff, import
  idempotent, faits de run complets.
---

# Export par périmètre de propriétaire

`oto_mcp/export_perimetre/`. **État : classement, extraction avec rechiffrement, import
par lots et commande `oto-mcp perimetre` existent, éprouvés de bout en bout sur des
bases de test (`tests/export_perimetre/test_import_bout_en_bout.py`). La répétition à
blanc sur une copie est en cours (#1088) : la première a mis au jour les lignes d'anciens
comptes (ci-dessous), et un temps dominé par le journal d'appels (« Le journal hors
fenêtre »).**

## La commande

```bash
# Chez NOUS, sur la base source (DATABASE_URL, OTO_MCP_MASTER_KEY, OTO_MCP_S3_* de notre
# instance) — écrit perimetre.jsonl et perimetre.jsonl.objets.tar :
OTO_EXPORT_CLE_CIBLE=<clé de l'instance cible> \
  oto-mcp perimetre export --org 12 [--org 13 …] --sortie perimetre.jsonl
# Sur l'instance cible (sa DATABASE_URL, SA clé maîtresse, SON stockage OTO_MCP_S3_*),
# depuis l'arbre du tag qu'elle servira, dans CET ordre — naître, importer, démarrer :
oto-mcp perimetre naitre                      # base VIDE → schéma, tête du registre, rien d'autre
oto-mcp perimetre import perimetre.jsonl      # les deux fichiers côte à côte
#   … puis seulement, le premier démarrage de l'app (la première montée) : il sème ses
#   guides plateforme à côté des lignes importées.

# Le jour J : le journal d'appels voyage à part (« Le journal hors fenêtre ») —
oto-mcp perimetre export --org 12 --sans-journal --sortie perimetre.jsonl
oto-mcp perimetre journal export --org 12 --depuis <date> --jusqu-a <date> \
  --sortie journal.jsonl [--faits-de-run-complets]
oto-mcp perimetre journal import journal.jsonl
```

Un refus s'imprime nommé et sort en code 2, sans rien écrire. Le résumé ne cite jamais
une clé, seulement l'empreinte de la clé cible.

⚠️ **Importer AVANT le premier démarrage de l'app** (#1161). Le démarrage sème ses
propres lignes dans des tables que l'import écrit (les guides plateforme : `nodes`,
`blocks`), sous des identifiants que l'import préserve : il tombait tard, en
`nodes_pkey`. Il refuse désormais une telle cible dès son contrôle préalable, en nommant
chaque table et son nombre de lignes. Il n'y a pas d'option pour passer outre : repartir
d'une base neuve, et y importer avant de démarrer l'app.

**`oto-mcp perimetre naitre`** (`naissance.naitre`) fait naître cette base sans démarrer
l'app : la moitié « schéma » du démarrage et elle seule — `init_db`, qui crée le schéma
et pose la tête du registre des migrations sur une base neuve (`db/_version_alembic.py`),
avec ce que sème `init_db` lui-même (les lignes de `naissance` du classement), et rien
de ce que fait ensuite la préparation du démarrage (backfills, blocs et guides
plateforme). La base doit être VIDE : une base qui porte déjà des tables, ou une
`alembic_version`, est refusée en code 2 (`NaissanceRefusee`). La tête posée est celle
de l'arbre qui joue la commande : la lancer depuis le tag que l'instance servira, celui
dont l'export a la version de schéma. Le démarrage qui suit l'import complète la base :
ses semis tombent sur des séquences que l'import a portées au-delà de ses lignes. Le banc
`test_naitre_importer_puis_demarrer` joue la séquence entière et exige que chaque étape
de la préparation du démarrage réussisse. Sur une instance cible, l'ordre de la première
montée : `docs/instance-cible.md`.

## Le classement : chaque table, une classe

`classement.CLASSEMENT` donne une entrée à **chaque** table du schéma réel :

| classe | ce qui part | la règle |
|---|---|---|
| **possédée** | les lignes du périmètre | directe : `ParOrg`, `ParSub`, `ParGroupe`, `ParEntite` (couple polymorphe `owner_type`/`owner_id`) |
| **indirecte** | les lignes dont le parent part | `Via` seulement, par une FK réelle (`fk=True`) ou logique (`fk=False`, id polymorphe en texte) |
| **instance** | rien : la cible naît avec les siennes | aucune ; une raison écrite |
| **exclue** | rien, mais le manifeste **compte** ce qui n'est pas parti | une règle et une raison (notre commerce, nos documents légaux, nos relances) |

⚠️ **Ajouter une table au schéma, c'est la classer dans le même commit** — et, si
elle est `exclue` ou `instance`, trancher son EFFACEMENT (`effacement/disposition.py`,
`docs/effacement-d-org.md`) : l'effacement d'une org s'appuie sur ce même classement.
`decouverte.verifier_classement` refuse, en listant **toutes** les anomalies d'un coup :
une table non classée, une entrée sans table, une colonne nommée qui n'existe pas, un
`Via fk=True` sans la clé étrangère correspondante, un héritage d'une table qui ne part
pas. Le garde-fou est `tests/export_perimetre/test_classement_couvre_schema.py`, sur le
schéma que monte `init_db` et pas sur une reconstitution. On prouve qu'il mord en lui
présentant chaque anomalie.

⚠️ Une ligne dont `org_id` est NULL (droit « personne, partout » de #1089, appel hors
de toute org) appartient à son **compte** : les tables à `org_id` nullable combinent
`ParOrg` et `ParSubSansOrg`, sans quoi ces lignes tomberaient hors du périmètre en
silence.

`ParEntite` ne retient jamais `platform` ni `tenant` : ces lignes sont celles de
l'instance. Un membre s'y lit en `'<org_id>:<sub>'`.

`comptes` nomme les **colonnes-compte** d'une table possédée par org : la ligne est celle
d'UN compte dans l'org (sa préférence, son journal, son abonnement) et la colonne dit
lequel (`connector_selection_seeded.sub`, `runner_jobs.sub`…). Les règles ajoutent les
leurs (`classement.comptes_de`) : la colonne de `ParSubSansOrg`, le couple de `ParEntite`
quand il désigne un `user` ou un `member`. Une colonne qui trace seulement l'auteur d'un
geste sur un objet de l'org (`created_by`, `actor_sub`) n'en est pas.
`test_classement_couvre_schema.py` exige que toute colonne `sub` d'une table exportée
soit la règle, une colonne-compte, ou une exception écrite (`SUB_SANS_COMPTE`).

## Le périmètre : des orgs déclarées, et leur tenant

`perimetre.resoudre(conn, orgs)`. Les équipes, les comptes (membres d'org et d'équipe),
les **orgs personnelles** de ces comptes et le **tenant** qui les héberge s'en
dérivent. Le tenant d'une org est son tenant EFFECTIF, l'union des trois axes de
`db.tenants.org_tenant_slug`, lue par la même expression. Refus nommés :

- `ComptesPartages` : un compte aussi membre d'une org hors périmètre (décision du
  28/09/2026 : le refus reste) ;
- `TenantsMultiples` : des orgs de plusieurs tenants — la cible n'a qu'un tenant primaire ;
- `TenantPartage` : le tenant héberge aussi des orgs hors périmètre ;
- `ComptesHorsTenant` : un compte sans le préfixe `<slug>:` du tenant. Sur la cible, le
  tenant devient PRIMAIRE et ses subs y sont nus (`tenancy.qualify`) : un sub sans le
  préfixe est celui d'un autre annuaire.

Ces refus portent sur les MEMBRES. Les comptes qui ne le sont plus mais dont des lignes
du périmètre portent encore la trace relèvent d'une autre règle (« Les lignes d'anciens
comptes », plus bas). Pour elle, le périmètre lit aussi les préfixes des tenants tiers
de la source (`prefixes_tiers`) : un sub qui n'en porte aucun est NU, celui de l'annuaire
du tenant primaire (`tenancy.tenant_of`).

Décisions d'Alexis du 28/09/2026 : le tenant part et devient la ligne 1 de la cible ;
tout le journal d'appels part (au jour J : les 30 derniers jours et tous les faits de
run, décision du 30/09/2026, « Le journal hors fenêtre ») ; les droits déclarés et nos
acceptations légales restent (« exclue ») ; les orgs personnelles suivent leurs comptes.

## L'extraction

`extraction.exporter(conn, orgs, sortie)` travaille dans **une** transaction
`REPEATABLE READ READ ONLY`, donc dans un instantané cohérent où la base elle-même refuse
toute écriture. La connexion reste en lecture seule après l'appel. Avant la première
ligne écrite, elle refuse dans ces cas :

- `ComptesHorsRegle` : une ligne du périmètre désigne un compte hors périmètre que la
  règle des anciens comptes ne rattache ni n'omet (table, colonne, nombre) ;
- `SecretsChiffres` : une ligne exportée porte une valeur chiffrée (`secrets` du
  classement : coffre, secret de signature d'un déclencheur, clé d'une transcription)
  et l'appelant n'a pas donné la clé de l'instance cible (`cle_cible`) ;
- `ReferencesHorsPerimetre` : une clé étrangère d'une ligne exportée pointe vers une
  ligne qui ne part pas (un lien de page vers la page d'autrui, une ligne exclue) ;
- une clé étrangère vers une table **instance** n'est pas un refus : le manifeste la
  relève (`references_instance`). La cible doit porter ces lignes. Les clés vers
  `tenants(id)` ne sont pas contrôlées : l'import les remappe toutes vers la ligne 1.

Les identifiants sont **préservés**. Le fichier contient une ligne JSON par ligne de
table (`{"t", "l"}`, le `row_to_json` de PostgreSQL), les parents avant leurs enfants,
puis le manifeste. Celui-ci porte le compte par table, les lignes omises des tables
exclues, les partages omis (`partages_omis`), les lignes d'anciens comptes rattachées
ou omises (`comptes_hors_perimetre`), le maximum de chaque séquence, l'inventaire hors
base (clés d'Object Storage à copier à part), l'instantané, la version de schéma, les colonnes de chaque table, le
tenant (id, slug, nom), la correspondance des comptes source → cible, le compte des
secrets, l'EMPREINTE de la clé cible (`rechiffrement.empreinte_cle`, jamais la clé) et
l'empreinte SHA-256 des lignes. Les horodatages sont écrits en UTC. Un export existant
ne s'écrase pas.

## Les secrets : rechiffrés CHEZ NOUS, À L'EXPORT

Décision d'Alexis du 28/09/2026 : **notre clé maîtresse ne sort jamais de notre
infrastructure**. L'export tourne chez nous, sous notre clé (`OTO_MCP_MASTER_KEY`), et
reçoit la clé de l'instance cible pour cette seule exécution. Il déchiffre chaque
secret sous notre clé et l'AAD de la ligne source, puis le rechiffre sous la clé cible
et l'AAD de la ligne CIBLE. Le fichier ne porte QUE des secrets chiffrés pour la cible.
Il n'y a pas d'autre chemin : aucun mode ne transporte un secret sous notre clé.

⚠️ L'AAD d'un credential de compte ou de membre contient le sub, et le sub change à
l'import. L'export calcule donc la ligne cible par la fonction même de l'import,
`transformation.Transformation`, et il n'en existe qu'une. Les AAD viennent des
fonctions qui écrivent ces secrets (`credentials_store._aad`,
`runner_hook._aad_du_secret`, `transcription_worker._aad`). Le clair ne vit qu'en mémoire.

## Les objets du stockage objet : une archive scellée pour la cible

Décision d'Alexis du 28/09/2026 : les objets voyagent par une **archive**, et par elle
seule. Il n'y a ni copie directe d'un seau à l'autre, ni URL signée.

- **Quels objets** (`objets`) : ceux qu'une ligne du périmètre désigne, par une CLÉ
  (`project_files.s3_key`, `transcription_jobs.audio_key`) ou par une URL de notre
  stockage public, où qu'elle soit. Cela couvre les colonnes (`users.avatar_url`,
  `orgs.logo_url`, `project_files.public_url`) comme les contenus : une image déposée
  par un agent (`images/<sub>/…`) n'a d'autre trace que son URL collée dans une page,
  un tableau ou un JSON. L'export cherche `<base publique>/<chemin>` dans chaque ligne
  écrite (`cles_dans`). Le chemin n'est pas la clé : les URL stockées la citent encodée
  d'un niveau, et la clé s'en tire en décodant le chemin une fois, et une seule
  (`cle_du_chemin`) — une clé `images/<slug>%3A<id>/…` est citée par
  `…/images/<slug>%253A<id>/…`. Un banc rougit si une colonne `hors_base` n'est classée
  ni clé ni URL.
- **L'archive** (`<sortie>.objets.tar`) : un membre par objet, nommé par sa clé et
  scellé (`crypto.seal`, AES-256-GCM) sous la clé de l'instance CIBLE, avec une AAD
  qui le lie à sa clé d'objet. Elle est écrite chez nous, depuis notre stockage
  (`stockage`, `media_store`). Le manifeste l'inscrit (`objets` : nom, empreinte
  SHA-256, notre base publique, et par objet la taille et l'empreinte du clair, son
  type `content_type` et s'il était public, `public`, relevés à la source). Un objet
  absent de notre stockage, ou sous un préfixe sans règle d'écriture, refuse, tous
  nommés, et un export refusé ne laisse derrière lui ni lignes ni archive.
- **À l'import**, l'archive est vérifiée avant toute écriture. Elle se verse dans le
  stockage de la cible, avec ses propres identifiants, après la relecture et avant la
  validation. Chaque objet est déchiffré sous la clé de l'instance, comparé au
  manifeste, écrit sous la MÊME clé avec les en-têtes de l'écriture native, puis relu,
  octets et en-têtes. Un objet déjà là avec la même empreinte et les mêmes en-têtes est
  sauté : un import interrompu se reprend ; avec d'autres en-têtes, il est réécrit.
- **Les en-têtes** (ACL, `Content-Type`, `Cache-Control`, `Content-Disposition`) suivent
  la règle de l'écriture native, une seule source : `media_store.entetes_d_objet`,
  d'après le préfixe de la clé.
  - `avatars/`, `org-logos/`, `images/` (`upload_image`) : lisibles de tous
    (`public-read`), `Cache-Control: public, max-age=31536000, immutable`, type lu sur
    les octets — même si l'objet était privé à la source.
  - `project-files/`, `transcription-jobs/` (`upload_object`) : privés, type servi
    (`type_servi`) d'après le type de l'objet source, téléchargement forcé s'il est
    neutralisé ; un document partagé (`make_public`) à la source reste public.
  - Tout autre préfixe n'a pas de règle : l'export comme l'import refusent. Un
    manifeste antérieur au relevé des types et des ACL refuse aussi : refaire l'export.
- **Les URL** sont réécrites par la `Transformation` : `<notre base>/` devient `<base
  cible>/` dans toute valeur texte, colonne ou contenu ; le chemin ne bouge pas. La
  base cible est celle que la cible déclare (`media_store.public_base`), sans défaut de
  notre côté. La relecture refuse s'il subsiste une URL de notre stockage dans le
  périmètre.

Les archives froides du journal mêlent tous les propriétaires : elles restent hors
périmètre.

## Les partages hors périmètre : omis, comptés

`resource_grants` et `grants` désignent leur destinataire par un couple polymorphe,
sans clé étrangère (`classement` : `destinataire`). Décision du 28/09/2026 : une ligne
dont le destinataire n'est pas du périmètre **ne part pas**. Sur la cible, ce
destinataire n'existe pas, et la ligne y emporterait l'identité d'un tiers. Le
manifeste compte ces lignes (`partages_omis`).

## Les lignes d'anciens comptes : rattachées, sinon omises

`comptes`. Le périmètre dérive ses comptes des membres ; une table possédée par org
porte pourtant aussi des lignes écrites par des comptes qui n'en sont plus membres.
La répétition à blanc sur une vraie base (#1088) en a trouvé quatorze, tous d'anciens
comptes de l'annuaire du tenant primaire (sub NU), d'avant que le tenant tiers ait son
propre annuaire : quelque 8 300 appels du journal, 30 runs, des préférences. Pour sept
d'entre eux, un compte `<slug>:<même id>` est du périmètre. La `Transformation` dénude ce
jumeau en `<id>`, qui heurtait les lignes de l'ancien compte : l'import tombait sur une
violation d'unicité brute (`connector_selection_seeded_pkey`) au bout de deux minutes, et
les lignes des sept autres désignaient sur la cible un compte qui n'existe pas.

Règle décidée : **rattacher, sinon omettre**, tranché et compté À L'EXPORT. Pour chaque
colonne-compte d'une ligne du périmètre :

| la valeur | la ligne |
|---|---|
| NULL, ou un compte du périmètre | part, inchangée |
| un sub nu `X` dont `<slug>:X` est du périmètre | **rattachée** : elle part telle quelle et porte, sur la cible, le compte nu du jumeau |
| … et elle y doublonnerait une ligne du jumeau sur une clé unique | **omise** : le jumeau gagne |
| un sub nu sans jumeau | **omise** |
| autre chose : un compte d'un AUTRE tenant, un `<slug>:` hors périmètre | **refus** `ComptesHorsRegle` (table, colonne, nombre) |

Le doublon se juge sur chaque index unique qui lit une colonne-compte (`Schema.uniques`,
expressions et index partiels compris), évalué sur la ligne où le compte est remplacé
par son jumeau. La règle entre dans le prédicat de chaque table
(`extraction.compilateur`) : l'écriture, les enfants (`Via`, qui suivent leur parent
omis), la fermeture (une clé vers `users(sub)` d'une ligne rattachée vise le jumeau), les
objets, les séquences et la relecture lisent les mêmes lignes. L'import n'a rien à
deviner : le fichier ne porte que des comptes du périmètre, et la relecture sur la cible,
où tous les comptes sont du périmètre, ne retire rien.

Le manifeste (`comptes_hors_perimetre`) compte, par table, les lignes `rattachees`,
`omises_doublon`, `omises_sans_jumeau` et `omises_avec_leur_parent`, et le nombre de
comptes concernés (`rattaches`, `sans_jumeau`) — jamais leurs identifiants. Le résumé
de `oto-mcp perimetre export` l'affiche.

## L'import

`importation.importer(conn, fichier)` verse le fichier dans une base **née par
`oto-mcp perimetre naitre`, et jamais démarrée**, pour l'instance du propriétaire, en UNE transaction.
L'import ne connaît que la clé de SON instance. Avant d'écrire, il refuse
(`ImportRefuse`) dans ces cas :

- un fichier dont l'empreinte ou les comptes ne sont pas ceux du manifeste ;
- une version de schéma différente, ou des colonnes qui ne sont pas les mêmes. Elles se
  comparent par NOM, dans n'importe quel ordre : une base servie porte en fin de table
  les colonnes ajoutées par `ALTER TABLE … ADD COLUMN`, une base née par `init_db` à
  leur place de création, et l'écriture comme la relecture associent par nom. Le refus
  nomme, par table, les colonnes présentes d'un seul côté (`source seule`, `cible
  seule`) ;
- une base qui n'est pas vierge (`controler_vierge`) : une table que l'import écrit — les
  tables exportées du classement sous lequel l'export a été lu, TOUTES — porte des
  lignes autres que celles que la naissance de l'instance y sème. Ces dernières sont
  déclarées au classement (`naissance` : le tenant primaire, les disponibilités
  `platform` de `connector_availability`, les sentinelles `org_id = 0` de
  `connector_selection_seeded`), et `test_classement_couvre_schema.py` les tient égales
  à ce que sème `init_db`. Le refus nomme chaque table et son nombre de lignes, et dit le
  geste : une base neuve, ou importer avant le premier démarrage. Un export sans journal
  ne compte pas le journal : des tranches ont pu y être poussées la veille ;
- un tenant primaire cible dont le slug (`OTO_TENANT_PRIMAIRE_SLUG`) ou le NOM (semé
  depuis `OTO_BRAND_NAME`) n'est pas celui du tenant exporté : le refus donne les deux
  noms, l'import n'écrase pas le nom que l'instance déclare (décision du 28/09/2026) ;
- des secrets chiffrés sous une autre clé que la sienne (empreinte du manifeste ≠
  empreinte de `OTO_MCP_MASTER_KEY`), ou dont un ne se déchiffre pas sous l'AAD de sa
  ligne cible.

Ce qui change en chemin est `transformation.Transformation`, rien d'autre :

- **le tenant** : la ligne 1 semée par `init_db` prend les valeurs du tenant exporté,
  et toute clé vers `tenants(id)` vaut 1 ;
- **les comptes** perdent le préfixe `<slug>:` (validé le 28/09/2026) : toute VALEUR
  exactement égale à un sub du périmètre, ou à sa forme membre `<org>:<sub>`, est
  remplacée, à toute profondeur d'un JSON ;
- **les colonnes dérivées partent VIDES** (`transformation.DERIVEES`) : le vecteur de
  classement `search_vec` de chaque source classée (`docs`, `projects`,
  `org_instructions`, `nodes`, `datastore_rows`, `project_file_texts`, #318) et le texte
  des valeurs servies (`datastore_rows.valeurs_texte`/`valeurs_vec`,
  `db/valeurs_servies.py`). Ils recopient le contenu — sub qualifié et URL de notre
  stockage compris — en un vecteur ou un texte d'un seul tenant, que les deux règles
  ci-dessus ne savent pas réécrire : transportés tels quels, ils gardaient sur la cible
  la trace de l'ancien compte et décrivaient un contenu qui n'y est plus. La cible les
  reconstruit sur le contenu RÉÉCRIT, sans geste pour `search_vec` :
  - **à la lecture**, tout de suite : la recherche et le classement lisent
    `COALESCE(search_vec, <vecteur calculé>)` (`search.rank_expr`) — une ligne vide se
    trouve et se classe, au prix du calcul ;
  - **par la boucle `rank_backfill`**, au démarrage de l'instance : déclarée sans action
    chez un tiers (`boucles_de_fond`), elle tourne sur TOUTE instance, armée par défaut
    (`OTO_RANK_BACKFILL_ENABLED`), et remplit par tranches les vecteurs vides
    (`search.rank_backfill_sql`) ;
  - le texte des valeurs servies, lui, se recalcule par
    `scripts/remplir_valeurs_servies.py --appliquer` après l'import ; d'ici là, ces
    lignes se cherchent par l'ancien prédicat — trouvées, à l'ancien prix.

  Prouvé par `test_import_bout_en_bout.py::test_aucun_vecteur_de_classement_ne_survit_au_contenu_reecrit` :
  la source porte des vecteurs remplis où se lisent le slug du compte qualifié et l'hôte
  de notre stockage ; sur la cible, aucun vecteur n'a voyagé, la ligne se trouve quand
  même, et la requête de la boucle reconstruit des vecteurs égaux au calcul sur le
  contenu réécrit, sans ces deux traces.

L'écriture se fait par lots (`TAILLE_LOT` lignes par aller-retour, `executemany`) : un
journal d'appels complet compte des centaines de milliers de lignes.

### Les clés du tenant devenu primaire

Un tenant tiers range ses clés partagées au scope `tenant` du coffre. Le primaire n'en
porte jamais : ses clés partagées sont les instances PLATEFORME, et la cascade ne sonde
pas son barreau tenant. Versées telles quelles, ces lignes n'auraient personne pour les
lire, et les orgs du tenant perdraient en silence les clés qu'il leur fournissait.

Après la relecture, dans la même transaction, `cles_du_primaire.convertir` les range
donc en instances plateforme ouvertes à tous (`share_mode='open'`, aucun partage) :

- le secret est reposé par le coffre (`set_credential`), rechiffré sous l'AAD
  plateforme, `meta` et `set_by` conservés, avec une trace `_conversion` ; le label
  est le compte de la ligne s'il en porte un (« Main »), sinon le slug du tenant ;
- la ligne posée est relue avant de continuer : ouverte, déchiffrable sous sa propre
  AAD avec la même empreinte que l'original, instance nommée ;
- l'arête « tout le monde » (`grants_chain.EVERYONE`) est posée sur l'instance ;
- la ligne tenant est retirée par le coffre (son instance s'archive), et les arêtes
  tenant→org SANS contrainte qui la désignaient s'archivent.

Refus nommés, avant toute écriture, qui annulent l'import (`ImportRefuse`) : un
connecteur sans mode `platform` (sa clé n'aurait pas de place : la retirer à la source,
ou donner le mode au connecteur), deux clés tenant pour un même connecteur, une ligne
tenant qui porte un partage, une instance plateforme déjà là pour ce connecteur, une
arête AVEC contrainte (un quota par org) ou un lien de projet qui désigne une ligne
tenant — l'ouvrir à tous effacerait cette borne. Rejouée, la conversion ne change rien.

⚠️ **Ce que la conversion change pour les orgs du tenant**, à connaître avant d'importer :

- le **mode de clé** de ces connecteurs devient `platform` (`key_mode`) : c'est ce que
  lit la facturation partenaire ;
- les **quotas journaliers des clés plateforme** (`access.quotas`) s'appliquent
  désormais à ces appels, sauf pour qui porte le droit `platform_unmetered` ;
- un outil qui exige une clé de l'appelant refuse : Apollo avec `want="byo"`, par
  exemple, ne voit plus la clé du tenant comme la sienne.

Les déclencheurs de la cible (journal des révisions, vecteur de recherche) sont
suspendus le temps de la transaction : l'import reproduit un état, il ne rejoue pas des
gestes. Les clés étrangères restent vérifiées. Les auto-références (une page sous une
page) se posent une fois la table remplie. Les séquences sont portées au-delà du
maximum du manifeste, et ne reculent jamais (`avancer_sequence`) : celle du journal a pu
être avancée par une tranche.

**Vérification** : dans la même transaction, le périmètre est RELU sur la cible par la
lecture même de l'export (`extraction.ouvrir`). Par table, il faut le même nombre de
lignes et la même empreinte que les lignes écrites, sinon tout est annulé
(`VerificationEchouee`). Cette empreinte est une somme de hachés, indépendante de
l'ordre, de la forme canonique de chaque ligne : c'est la seule comparaison qui
survive aux remappages. L'empreinte brute du fichier, elle, est contrôlée avant toute
écriture. Un export sans journal se relit sous le même classement que l'export
(`classement.sans_journal`) : les appels déjà poussés ne sont pas comptés.

## Le journal hors fenêtre

`journal`, `classement.JOURNAL`. **Le constat** (répétition à blanc sur une vraie copie de
production) : export en 2 079 s, dont les deux passes de recensement des anciens comptes,
surtout sur le journal ; import en 3 847 s ; 4,47 M lignes, dont **4,33 M d'appels du
journal** (`tool_calls`). La fenêtre de coupure du jour J tient en 30 à 60 min : presque
tout le temps est le journal.

**La décision** : le journal sort de la fenêtre. Pendant la coupure, tout part SAUF lui ;
lui se verse par tranches de dates, sans coupure. Décision du 30/09/2026 : seuls les
**30 derniers jours** du journal partent (le `--depuis` de la première tranche), mais les
**faits de run** (`run_start`, `run_finish` : `classement.FAITS_DE_RUN`, les `RUN_FACTS`
de `deploy/archive_tool_calls.py`, qu'un test tient égaux) sont la source de vérité des
runs et partent EN ENTIER, quelle que soit leur date (`--faits-de-run-complets`).
L'export sans option, lui, emporte toujours tout le journal.

### L'export principal sans journal

`oto-mcp perimetre export … --sans-journal` (`exporter(journal=False)`) lit le périmètre
sous `classement.sans_journal` : le journal y est `exclue`. Il ne part pas, ni ses
recensements (les deux passes coûteuses) ; le manifeste le COMPTE — `journal` :
`{"inclus": false, "tables": {"tool_calls": {"horodatage", "lignes", "premier",
"dernier"}}}`, lignes du périmètre à la règle brute, bornes en UTC — et porte le maximum
de sa séquence, que l'import pose sur la cible : ses propres appels n'y prendront jamais
l'id d'un appel encore à verser.

Avant la première ligne, `decouverte.verifier_journal` refuse (`JournalNonDetachable`,
toutes les anomalies d'un coup) ce qui empêcherait le journal de voyager à part : une
table exportée qui y renvoie (clé étrangère ou `Via` : sa ligne viserait un appel pas
encore versé), une clé du journal vers une table exportée (il ne pourrait plus être
poussé avant l'import principal), une règle du journal qui passe par un parent, une
colonne d'horodatage ou de faits de run absente, une clé primaire absente. Sur le schéma
réel : aucune — `tool_calls` n'a aucune clé étrangère, ni vers `users` ni vers `orgs`, et
aucune table ne renvoie à lui.

L'import principal d'un tel export produit une instance complète et cohérente sans
journal : fermeture contrôlée à l'export, relecture conforme à l'import.

### Une tranche du journal

`oto-mcp perimetre journal export --org … --depuis D --jusqu-a J --sortie …
[--faits-de-run-complets]` (`journal.exporter_tranche`) exporte les appels du même
périmètre dont `created_at` est dans `[D, J)`, demi-ouverte ; une borne sans fuseau est en
UTC, le suffixe `Z` vaut `+00:00` (accepté aussi sous Python 3.10). Même déroulé que l'export principal (`extraction.exporter_lecture`) : même instantané
`REPEATABLE READ READ ONLY`, même règle des anciens comptes (rattachées, sinon omises,
comptées au manifeste de la tranche), même `Transformation`, même rechiffrement, et
l'archive scellée des objets que ses appels citent par URL. Seule la lecture change
(`journal.tranche`) : le journal seul, chaque prédicat borné à la fenêtre ; avec
`--faits-de-run-complets`, les faits de run antérieurs à `D` en plus. Son manifeste (format
`oto-export-perimetre-journal/1`) dit la tranche, et `apres` : les lignes du périmètre
au-delà de `J` dans l'instantané — **0** pour une dernière tranche bornée par le gel.

`oto-mcp perimetre journal import <fichier>` (`journal.importer_tranche`) vise une
instance NÉE par le démarrage, que l'import principal ait eu lieu ou non :

- il vérifie le fichier (empreinte, comptes), le schéma (version, colonnes par nom), le
  tenant primaire (slug et nom) et la clé de l'instance s'il porte des objets ;
- il ne demande à la cible ni les orgs ni les comptes : le périmètre vient du manifeste
  (`perimetre_du`) — la cible n'en a pas encore la veille, elle peut en avoir de
  nouveaux après la bascule ;
- il refuse un fichier qui porte autre chose que le journal, et un journal cible qui
  porterait des déclencheurs (une tranche se verse dans une instance qui peut servir :
  suspendre ses déclencheurs verrouillerait la table) ;
- il écrit par lots, en UNE transaction, `ON CONFLICT (<clé primaire>) DO NOTHING` : une
  tranche rejouée, ou deux tranches qui se chevauchent, n'insèrent rien deux fois ; le
  rapport compte `inserees` et `deja_presentes` ;
- il relit la fenêtre sur la cible (lignes du périmètre, nombre et empreinte) et annule
  tout en cas d'écart (`VerificationEchouee`) — c'est ce qui voit une clé primaire déjà
  prise par une AUTRE ligne, que `DO NOTHING` aurait tue ;
- il ne touche à aucune autre table ; la séquence du journal ne fait que monter.

### L'ordre du jour J : pousser, puis le diff

1. **La veille.** La base cible naît par `oto-mcp perimetre naitre` (tenant primaire en
   ligne 1, ni orgs ni comptes), et l'app n'y démarre PAS avant l'import principal
   (#1161). On y POUSSE les 30 derniers jours, en une ou plusieurs tranches contiguës,
   la première avec `--faits-de-run-complets` :
   ```bash
   oto-mcp perimetre naitre                           # sur la cible, base vide
   oto-mcp perimetre journal export --org 12 --depuis 2026-09-01T00:00:00Z \
     --jusqu-a 2026-10-01T00:00:00Z --faits-de-run-complets --sortie push.jsonl
   oto-mcp perimetre journal import push.jsonl        # sur la cible
   ```
2. **Le jour J, pendant la coupure**, une fois le gel posé à l'instant `G` :
   ```bash
   oto-mcp perimetre export --org 12 --sans-journal --sortie perimetre.jsonl
   oto-mcp perimetre import perimetre.jsonl           # la même base : vierge en orgs et
                                                      # comptes, elle porte déjà le push
   oto-mcp perimetre journal export --org 12 --depuis 2026-09-30T23:00:00Z \
     --jusqu-a <G> --sortie diff.jsonl                # le SEUL diff, avec une heure de
   oto-mcp perimetre journal import diff.jsonl        # recouvrement de sécurité
   ```
3. **Puis seulement, le premier démarrage de l'app** sur la cible (sa première montée,
   `docs/instance-cible.md`) : il sème ses guides plateforme à côté des lignes importées.

**Couvrir sans trou.** Des tranches demi-ouvertes contiguës (`jusqu-a` de l'une = `depuis`
de la suivante) couvrent chaque instant une fois ; un recouvrement ne coûte rien (clé
primaire). La dernière tranche se borne par l'instant du gel `G` : son `apres` doit valoir
0, sinon des appels du périmètre ont été écrits après le gel et la bascule n'est pas
propre. Le diff part de la borne haute du push, un peu avant : une transaction longue
ouverte au moment du push a pu valider après lui des appels datés d'avant.

⚠️ Une tranche vaut pour le périmètre de l'instant où elle est exportée. Si ses orgs ou
ses comptes changent entre le push et le jour J (un membre qui arrive, un qui part),
refaire le push après l'import principal : rejouée, une tranche ne réinsère rien, et la
relecture dit si la cible s'en écarte.

⚠️ **Jamais contre la base servie** tant que l'outil n'a pas été répété à blanc sur une
copie : production et préproduction partagent la même base.
