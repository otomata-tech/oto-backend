---
title: Propriété de ressource — primitive `ownership` (ADR 0030)
type: reference
---

# Propriété de ressource — primitive `ownership` (ADR 0030)

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.


Le datastore n'est **plus scopé par `sub`** : il est le **pilote** de la primitive
d'ownership générique. `ownership.py` est le **seam unique** : une ressource
`(resource_type, resource_id)` est possédée par `(owner_type∈{user,group,org,platform},
owner_id)` (colonnes sur la ressource — pour le datastore : `user_datastores.owner_*`,
`resource_id = id::text`, **stable au renommage**) ; le partage cross-type vit dans
**`resource_grants`** (deny-by-default, remplace `datastore_shares`). Deux plans, jamais
confondus : **`can_access`** (CONTENU = owner-match ∪ grant ; *privacy by default* — pas
d'escalade admin sur du perso) et **`can_govern`** (GOUVERNANCE : re-partager/lister/
révoquer/supprimer/publier **sans lire**). La lecture opérateur du contenu
perso reste le **view-as audité** (ADR 0023). `DatastorePg._resolve` passe par
`can_access` ; le share/transfert/delete par `can_govern` (un super_admin/org_admin
gouverne donc un datastore tiers). ⚠️ **Scoping des LISTES de contenu** : une liste de
ressources possédées (datastore `list_datastores`, projets `op=list`) scope sur
**`ownership.active_owner(current_org)`** (= l'org active, le pendant `ownership` de
`current_org`/ADR 0023), **JAMAIS** sur `accessor_scope().owner_pairs()` (= union de
TOUTES les orgs de l'acteur, réservé au plan **gouvernance** `oto_resource list` +
découverte/modèles). Les confondre = fuite cross-org *fail-open* (le superset montre
plus que le contexte chargé) — vécu 2026-06-30 (projets/datastore d'une autre org
visibles dans le dashboard). Garde-fou : `tests/test_owner_scope_tripwire.py` fige les
call-sites `owner_pairs()`. **org-owned activé** : `data_create_datastore` /
`POST /api/datastore/datastores` acceptent un `owner` (classeur d'équipe). Capacité
générique **`oto_resource`** (`capabilities/resources.py`, op `list/get/transfer/share/
unshare`, autz combinateur `RESOURCE_GOVERN`) = chemin de gouvernance MCP+REST + alimente
l'object-browser admin.

> ⚠️ **DEUX surfaces depuis le 2026-09-01, et un défaut connu conservé sur l'héritée.**
> `oto_resource` / `POST /api/resources` donne à `resource_type` le défaut
> `datastore_namespace` (relique du pilote ADR 0030). **Omettre le champ ne veut donc pas
> dire « n'importe quel type »** : l'appel vise silencieusement un tableau, et sur
> `op=transfer`/`op=share` il **agit sur une autre ressource que celle visée** (même id
> numérique, autre famille). Le corriger en place a cassé de vrais appelants (#756,
> reverté par #774) : le défaut est donc **conservé, et écrit dans la description
> servie** — c'est le seul texte qu'un appelant relit à chaque appel.
> La correction vit sur une surface **doublée** : `oto_resource_v2` / `POST
> /api/resources/v2` (`capabilities/resources_v2.py`), où `resource_type` est obligatoire
> et `resource_id` numérique. **Même handler, même autz, même forme de sortie** — seul le
> contrat d'entrée diffère. Elle est en **bêta** (option `beta`, cf.
> `docs/tool-visibility.md`) ; la migration se fait appelant par appelant, sans
> date-couperet. Cliquet : `tests/test_resources_deux_surfaces.py` +
> `tests/resources_input_legacy.json` figent le schéma d'entrée servi de l'héritée. Catalogue du registre : **`GET /api/admin/capabilities`**
(`capabilities_catalog.py`, `PLATFORM_ADMIN`, JSON Schema dérivé des Input pydantic) →
UI admin **dérivée**. ⚠️ **Migration en cours** : `user_datastores.sub` + colonnes Sheets
sont des reliques nullable, **DROP différé** (Phase H) après cutover prod vérifié.

> **Partage unifié « audience × rôle » (ADR 0048, amende 0030/0032).** Le grant porte
> désormais un **RÔLE** (`resource_grants.role ∈ {viewer, editor, manager}`) et non plus
> seulement `permission ∈ {read, write}`. `permission` reste **la projection CONTENU
> appariée** (viewer→read, editor/manager→write), dérivée du rôle à l'écriture
> (`db.grant_resource`) → **tout le SQL du plan contenu est inchangé** (`max(g.permission)`,
> `g.permission='write'`). Le rôle **`manager` (gérant)** rend la **gouvernance GRANTABLE** :
> `can_govern = owner ∪ grant gérant ∪ escalade roles.py` (`_has_manager_grant`). Le
> **transfert de propriété** reste plus strict — `can_transfer = owner ∪ escalade` (jamais
> un gérant, ADR 0048 §3) ; le handler `oto_resource op=transfer` le re-garde après le gate
> `RESOURCE_GOVERN`. Surface unifiée : **`oto_resource op=share`** accepte deux axes —
> **audience** (`person`/`team`/`org` → grant ; `public`/`secret` → publication projet ;
> `private` → dépublier) × **rôle** ; l'ancien `permission` read/write est accepté en entrée
> (mappé). ⚠️ **Son défaut diverge entre les deux surfaces, à dessein (ADR 0068)** :
> `oto_resource` garde `"write"` — son schéma servi est un **cliquet** (empreinte JSON
> figée sur des appelants mesurés au journal le 01/09), et un défaut qui change y casse
> en production, chez quelqu'un d'autre, sans trace ; `oto_resource_v2` prend `"read"`,
> comme le veut « privé par défaut ». C'est le sens même de la duplication (ADR
> 0019/0050 : un contrat servi ne se durcit pas en place, il se double). Le partage de
> tableau (`data_share`, route REST), lui, n'a pas de cliquet : son défaut passe bien à
> `read`. Rétro-compat : backfill `role` depuis `permission` au boot (jamais `manager`, qui
> est un acte explicite). Tests purs + **tripwire gouvernance** (`test_ownership.py` :
> un lecteur/éditeur/inconnu ne gouverne JAMAIS). Front : sélecteur de rôle
> lecteur/éditeur/gérant (`lib/resourceRole.ts`).

> **Échelle 4 crans (ADR 0049, 2026-07-10).** Le projet rejoint l'échelle
> platform/org/group/user — **la visibilité DÉCOULE de l'ownership**, aucun mécanisme
> de restriction (leçon 0044 §G : restreindre = poser la ressource au bon scope).
> **group-owned** : création `oto_project(op=create, owner_type='group')` (garde
> `can_read_group`), listé dans l'org PARENTE (membres du pôle ; org_admin = tous les
> pôles de son org), `visible_in_org` mappe le groupe sur son org ; transfert cible
> `new_owner_group` (`oto_resource op=transfer`) — une équipe de l'org OÙ VIT la
> ressource seulement (`ownership.transfer`, 403 `group_outside_resource_org`, cascade
> comprise : une équipe ne fait pas changer d'org, `new_owner_org` le fait). Le
> propriétaire ne se change QUE par ce transfert : `oto_project op=update` refuse
> `owner_type`/`owner_id` (400 `owner_change_unsupported`). **platform-owned** (`owner_id=
> 'platform'`, sentinelle comme les guides) : le cran BIBLIOTHÈQUE — `can_access`
> read = tout utilisateur authentifié (un modèle est fait pour être copié), write/
> govern/transfer = admin plateforme ; `op=list_templates` inclut toujours l'owner
> platform. Non-fait : la publication MCP d'un projet group/platform-owned reste
> org-centrique (l'endpoint sert sous l'autorité d'une org).

> **Suppression du « perso » (2026-06-30, amende ADR 0015/0023/0030).** Plus d'état
> **org-less** (`org_id=0` / `current_org`=None) : **tout user est TOUJOURS dans une org**.
> Chaque user a une **org perso dédiée** (`orgs.personal_of=sub`, privée mono-membre) —
> `org_store.ensure_personal_org` (créée au 1er insert d'`upsert_user` + au boot par
> `backfill_personal_orgs`, **reclaim sûr** : ne marque une org existante comme perso que
> si c'est la SEULE org du user, créée par lui ; sinon org fraîche → multi-org intact, zéro
> fuite). Les ressources `owner_type='user'` ont **migré** vers l'org perso ; les **défauts
> de création** (datastore/projet) vont dans l'**org active** (`current_org`, toujours posé).
> Plus de retour-perso (`clear_active_org` retiré ; `oto_clear_org` REST → org perso, MCP →
> maison). Filets gardés : `ownership` accepte encore `owner_type='user'` **en lecture**
> (reliquat) ; `session_visibility` `prof_org = active_org or 0` (défensif). `org_id=0`
> purgé des profils de visibilité.

## Le seam `ownership` — ce que la carte en disait

`ownership.py` = seam unique : ressource possédée par `(owner_type∈{user,group,org},
owner_id)` + partages `resource_grants` (deny-by-default). **Deux plans jamais
confondus** : `can_access` (contenu, privacy by default) vs `can_govern` (gouvernance,
escalade roles.py). ⚠️ **Une LISTE de contenu scope sur `active_owner(current_org)`,
JAMAIS `owner_pairs()`** (union de toutes les orgs = fuite fail-open ; tripwire
`test_owner_scope_tripwire.py`). Plus de « perso » : tout user a une org perso dédiée
(`orgs.personal_of`), défauts de création = org active.
**Détail (datastore pilote, oto_resource, migration, abolition du perso) : `docs/ownership.md`**.

## Une liste s'en tient à l'org consultée (décision d'Alexis du 07/10/2026)

« L'affichage s'en tient à l'org consultée, on ne mélange pas. » Un objet personnel
(projet ou tableau, `owner_type='user'`) se LISTE, pour son seul propriétaire, dans l'org
où il a été créé (`context_org_id`) — **et nulle part ailleurs, org perso comprise**.
Jusque-là, l'org perso listait **tous** les projets et tableaux perso de l'appelant, d'où
qu'ils viennent : on croyait qu'ils y vivaient.

- `ownership.mes_objets_ici` rend `(sub, org, sans_org)` : `sans_org` (vrai dans l'org
  perso, hors vue bornée) n'ajoute plus que les objets **sans** org de création
  (`context_org_id` nul : legacy), faute de quoi ils ne sortiraient dans aucune liste.
  Les deux clauses SQL (`db.list_projects_for_owners`, `project_nodes.lignes_pour_proprietaires`)
  et `mes_tableaux_ici` lisent cette règle ; la liste des projets, `list_templates`,
  `archived=true`, la recherche, le rail et le bloc projets du handshake suivent.
- Le jeu POSSÉDÉ d'une liste de tableaux (`list_datastores`, `search._accessible_namespaces`)
  ne contient plus `("user", sub)` : `ownership.proprietaires_collectifs_de_liste` (l'org,
  mes équipes). Pris comme propriétaire, le principal personnel de l'org perso rendait
  tous mes tableaux perso. Il ne sert plus qu'aux **partages reçus**.
- **Inchangé** : les partages faits à moi en personne se listent toujours dans l'org perso
  (`perso_de_la_liste`) et par les lentilles « moi » dans toute org — leur place définitive
  (l'org d'origine, en accès « invité ») est un lot à part ; les procédures, guides et
  nœuds perso (sans org de création) restent dans l'org perso ; l'accès par identifiant ne
  change pas.

Banc : `tests/test_liste_s_en_tient_a_l_org.py` (base réelle : projets par la face servie,
tableaux par la liste et par la recherche).

## Plus d'org perso (décision du 05/11/2026)

> Remplace les deux sections suivantes, gardées pour l'histoire.

Plus d'« espace personnel », sur toutes les instances. Plus aucune org ne naît avec l'étiquette
`orgs.personal_of`, et les règles qui l'accompagnaient tombent. L'invariant « tout
compte a une org active » reste entier (`org_store.ensure_home_org`) :

- **Inscription sans invitation** → une org **ordinaire**, nommée d'après le domaine
  d'un email pro (sinon d'après la personne), avec le projet « Découverte ». Elle porte
  `orgs.auto_created`, une marque PERMANENTE sans règle à part. Tant que son créateur
  y est **seul** (`db/org_d_inscription.seule_d_inscription`, le critère écrit une
  fois — l'équivalent de l'ex-org perso), elle cède la place d'org active à l'org qu'il
  rejoint (`add_org_member`), elle est relâchée vide à l'acceptation d'une invitation
  (`liberer_perso_vierge`), et l'export d'un périmètre la range avec son compte
  (`export_perimetre.perimetre`). **Peuplée** (un collègue l'a rejointe), c'est une org
  d'équipe : elle garde sa place d'org active, et l'on y est promu quand on y est
  invité depuis une org d'essai seule ou suspendue. Elle ne compte jamais dans le quota
  de créations (`count_orgs_created_by`). La colonne naît de la révision
  `20261105_1700_orgs_auto_created`, qui marque aussi les orgs perso d'avant.
- **La maison** — où se listent un partage fait à la personne et un objet personnel
  sans org de création (`perso_de_la_liste`, le `sans_org` de `mes_objets_ici`, la
  maison créée par `maison_de`) — est l'**org active** (`org_store.home_org`). Elle
  suit la bascule d'org active : c'est voulu.
- **Ce qui est partagé à une personne se liste là où c'est rangé** (06/11/2026,
  `ownership.partages_a_moi_ici`) — projets, tableaux, apps, et pages reçues
  `scope="org"` (rangées par leur projet) : l'org propriétaire, l'org de l'équipe
  propriétaire ou l'org de création d'un objet perso (`org_de_rangement`), quand la
  personne en est membre — là seulement. La maison ne recueille que le projet
  sans org de rangement, ou celui d'une org dont elle n'est pas membre (collaborateur
  extérieur). Raison : la maison étant devenue une org ordinaire, le projet d'une org A
  partagé nominativement à un membre de A et de B se listait dans B, son org active —
  un projet d'un client affiché dans l'espace d'un autre. Cela revient sur « suit la
  bascule d'org active » pour les partages nominatifs ; les objets perso sans org de
  création, eux, restent dans la maison. Listes, recherche (projets et tableaux),
  bloc projets du handshake lisent la même règle.
- **Plus d'org perso posée à la demande** : un partage nominatif, une procédure
  personnelle, un nœud hors projet, une app ou un projet donnés à une personne vont
  dans son org active. La copie d'une procédure partagée va dans l'org active si l'on
  y est admin, sinon dans la plus ancienne de ses orgs où on l'est, sinon 403
  `no_writable_org`.
- **Quitter** : on ne quitte pas sa **dernière** org (409 `last_org`, qui remplace
  `personal_org`). Retiré de sa dernière org par un admin, ou laissé sans org par un
  archivage, un compte reçoit aussitôt une org ordinaire neuve.
- `personal` reste servi (fiche, `oto_list_orgs`, `active_org_is_personal`), toujours
  à `False`. La colonne `personal_of` et son index restent en base sans écrivain ;
  `scripts/retirer_orgs_perso.py` (à la main, après le tag et `migrer upgrade head` ;
  à blanc par défaut, il n'imprime que des nombres) range les objets sans org de
  création dans l'org perso qui les listait, marque ces orgs `auto_created`, archive
  les vides dont le propriétaire a une autre org, puis vide l'étiquette.

## Une org perso est une org comme une autre (décision d'Alexis du 29/09/2026)

> ⚠️ Resserré le 07/10/2026 (section précédente) : l'org perso ne liste plus les objets
> perso créés dans une autre org.

« Perso » n'est qu'une **étiquette** (`orgs.personal_of`), du même genre que la maison.
Fonctionnellement, une org perso est une org : on y invite, on y crée des équipes, on
l'archive par la même règle (`ORG_ADMIN_OF`). **Deux exceptions seulement** : elle est
**créée à l'inscription** (`ensure_personal_org`), et **son propriétaire ne peut pas la
quitter** (`me.leave_org`). L'étiquette reste posée quand d'autres y entrent
(`add_org_member`, ffbe1c64) ; sans étiquette, `ensure_personal_org` réclame la plus
ancienne org que le compte a créée et dont il est membre, quel que soit son nombre de
membres.

**Un invité n'en reçoit pas.** L'inscription ne la crée que pour un compte membre d'AUCUNE
org : celui que la réconciliation d'invitation (`reconcile_signup_with_invitation`) vient
de faire entrer dans une org n'a pas besoin d'un second espace. L'invité **par lien** la
reçoit encore au premier appel authentifié (le jeton n'arrive qu'après, par
`org.invite.accept`) : l'acceptation la relâche si elle est **vierge**
(`org_store.liberer_perso_vierge` — née il y a moins d'une heure, seul membre, rien dedans
que le projet « Découverte », ni clé déposée au coffre (org ou membre),
ni compte connecté, même déconnecté, ni partage nominatif reçu), par `archive_org` ; un
échec lève, l'acceptation ne le tait pas. Le boot (`backfill_personal_orgs`) ne
rattrape que les comptes **sans org active vivante** : membre d'une org, la plus ancienne
redevient maison ; d'aucune, l'org perso est créée. Un compte sans org perso en obtient une
**à la demande** là où un espace perso est nécessaire (`ensure_personal_org` : `org.clear`
REST, copie d'une procédure partagée, projet qui devient perso hors de ses orgs, **partage
nominatif reçu** — `ownership.maison_du_partage`, appelé par `grant` et par le droit que
`transfer` laisse à l'ancien propriétaire ; un agent, listé dans l'org où il tourne, n'en
crée pas). Sorti de sa dernière org, il en retrouve une tout de suite
(`members._rendre_un_espace`) ; un échec lève.

**La maison est fixe et explicite** (revue du 19/10/2026) : c'est l'org qui porte
l'étiquette `personal_of`, et elle seule (`ownership.org_perso_de`). Un compte sans org
perso n'a de maison nulle part — jamais son org active, qui ferait apparaître ses partages
nominatifs et ses objets sans org de création dans l'org d'entreprise, et déplacerait sa
maison à chaque bascule. D'où la création à la demande ci-dessus : ce qui a besoin d'une
maison la crée avant d'être posé.

**Un objet appartient à qui le crée et vit dans l'org où il l'a créé** : son
propriétaire l'y voit TOUJOURS (projets et tableaux, `ownership.mes_objets_ici` /
`mes_tableaux_ici`, qui lisent `context_org_id`) ; les autres membres ne le voient que
s'il le leur partage. L'org perso reste la **maison** de ce qui n'a pas d'org de
création (procédures, guides, nœuds perso, objets legacy sans `context_org_id`) et de ce
qui est partagé à la personne (`perso_de_la_liste`). **Les lentilles « moi »** —
`oto_project op=list scope="me"`, `GET /api/me/datastores/shared`, `oto_doc
op=shared_with_me` sans `scope` ou `scope="me"` — sont **servies dans toute org**, avec le
même contenu : un partage à une personne n'appartient à aucune org. Le 409
`personal_view_outside_personal_org` est retiré. La cascade des clés ne préfère plus l'org
perso (`personal_instance_org` : la clé la plus récente). Bancs :
`tests/test_org_perso_est_une_org.py`, `tests/test_projets_perso_visibles_ou_crees.py`.

## Dans une org, on ne voit QUE l'org (décision du 28/09/2026, ADR 0030 §9)

> ⚠️ Amendé le 29/09/2026 (section précédente) : mes objets perso se listent aussi dans
> l'org où je les ai créés, et les lentilles « moi » sont servies dans toute org.

La règle des LISTES — l'accès par identifiant ne change pas (`visible_in_org`,
`can_access`, `resolve_datastore_ns` gardent le principal personnel) :

- **org qui n'est pas l'org perso de l'appelant** : projets (`oto_project op=list`,
  `list_templates`, `archived=true`), tableaux (`list_datastores` : `GET /api/datastores`,
  `data_list_datastores`, index de `data_app`), recherche (`accessible_project_ids`,
  `search._accessible_namespaces`, guides), rail (`me.shell`), bloc projets du handshake,
  « Dernières modifications » et pages reçues `scope="org"` rendent ce que possèdent
  l'org et ses équipes, plus ce qui est partagé à l'org ou à une de ses équipes. **Aucun
  objet `owner_type='user'`**, quel que soit son `context_org_id`, **aucun partage fait à
  une personne** ;
- **org perso** (`orgs.personal_of = sub`, `me.active_org_is_personal`) : s'y ajoutent
  **tous** les projets et tableaux perso de l'appelant, quelle que soit l'org où ils ont
  été créés, et tout ce qui lui est partagé en personne (projets, tableaux, pages).

Une seule source : `ownership.perso_de_la_liste(sub, org)` (`[("user", sub)]` dans l'org
perso, `[]` ailleurs), que composent `principaux_de_liste` (principals des listes) et
`project_list_owners` (propriétaires des projets listés).

**Les lentilles « moi »** — `oto_project op=list scope="me"`,
`GET /api/me/datastores/shared`, `oto_doc op=shared_with_me` sans `scope` ou `scope="me"`
— étaient servies dans l'org perso seulement (409 ailleurs) ; depuis le 29/09/2026 elles
le sont dans toute org (section précédente). Un tableau absent des listes s'ouvre par
`GET /api/datastores/{datastore}`, qui n'en dépend pas. `GET /api/me/datastores/shared`
ne dédoublonne plus avec la liste de l'org perso, qui rend aussi ces tableaux.

**Création** (29/09/2026) : sans propriétaire nommé, un projet ou un tableau est **à la
personne**, depuis n'importe quelle org (ADR 0068 et §8 de l'ADR 0030, inchangés) — et
se LISTE alors dans son org perso, pas dans l'org où il a été créé. L'org ou l'équipe se
demandent explicitement (`owner_type`/`owner`). Aucun refus à la création. Pas de
migration : les objets existants restent où ils sont.

`context_org_id` n'est plus lu par aucune liste. Il reste lu, pour un projet perso, par
la résolution des clés de son axe `project=` (`access/heritage`), l'org d'origine
(`org_origin`, ADR 0071), la vue bornée (`_perso_range_hors_de`, oto#270) et la portée de
résolution des `[[…]]` (`db/backlinks`). Pour un tableau, il est écrit et plus lu. Bancs :
`tests/test_listes_seule_l_org.py` (base réelle), `tests/test_search_scope_tripwire.py`.

## Partage unifié audience × rôle (ADR 0048)

> **Partage unifié audience × rôle (ADR 0048).** Le grant porte un **rôle**
> `resource_grants.role ∈ {viewer, editor, manager}` (`permission` read/write reste la
> projection CONTENU dérivée → SQL du plan contenu inchangé). `manager` (gérant) rend la
> **gouvernance grantable** : `can_govern = owner ∪ grant gérant ∪ escalade roles.py` ; le
> **transfert** reste `can_transfer = owner ∪ escalade` (jamais un gérant). Surface unique
> `oto_resource op=share` : axe **audience** (person/team/org→grant ; public/secret→publication
> projet ; private→dépublier) × **rôle**. Rétro-compat `permission` en entrée.
> Une LISTE dont le propriétaire est déjà lu juge sa gouvernance par **`can_govern_many`** :
> même règle que `can_govern` (`_gouverne`, écrite une fois), escalade jugée une fois par
> propriétaire distinct, partages `gérant` lus en une requête (`db.manager_resource_grants`)
> — c'est ce que sert `GET /api/datastores` (`docs/event-loop-perf.md`).

### Publier exige d'appartenir au propriétaire (#1176)

Le gérant gouverne, il ne **publie** pas s'il est extérieur. Le rôle `manager` se donne à
quiconque, membre ou non de l'org propriétaire, alors que l'endpoint MCP publié résout
clés et quota **sous l'org propriétaire** : avec `can_govern` seul, un gérant extérieur
ouvrait au public l'usage des clés d'une org dont il ne fait pas partie.

**La règle** (`ownership.can_publish`), qui s'AJOUTE au droit du geste sans le remplacer :

| propriétaire du projet | qui peut publier |
| --- | --- |
| une org | un membre de cette org |
| une équipe | un membre de l'org **parente** (l'équipe ne restreint pas la publication) |
| une personne | le propriétaire, et lui seul — pas un membre de l'org de rangement |
| la plateforme | l'escalade plateforme seule |

L'escalade plateforme (`roles.is_platform_admin`) passe toujours, comme partout dans ce
seam. Une page se publie sous la règle de son projet.

**Un seul point de décision, quatre chemins.** `capabilities/_publication.exiger_appartenance`
porte le refus nommé **403 `publish_requires_membership`**, déclaré par chaque capacité
qui l'atteint :
- l'endpoint MCP — `publish_project_mcp`, le cœur commun d'`oto_project op=publish_mcp`
  (tous modes, `org` compris) et d'`oto_resource`/`oto_resource_v2` audience
  `public`/`secret`, sur les deux faces ;
- la page publique — `oto_doc op=set_public public=true` (garde `write` d'abord) ;
- le fichier public — `POST /api/me/projects/{project_id}/files/{file_id}/public` avec
  `public=true` (garde `write` d'abord).

`op=update` ne publie pas (les champs de publication y sont refusés,
`publication_field_on_update`) ; corriger `mcp_instructions_md` reste sous `can_govern`.

**Ce qui reste ouvert au gérant extérieur** : dépublier (`audience=private`,
`unpublish_mcp`, refermer une page ou un fichier — réduire la portée n'est jamais
gardé), modifier, partager à une personne, une équipe ou une org. Banc :
`tests/test_publication_exige_appartenance_1176.py` (base réelle).

## Une procédure se gouverne sous son nom : `procedure` (oto#65)

> **Le nom servi.** `oto_resource` / `POST /api/resources[/v2]` publient la famille
> `resource_type="procedure"`. La valeur ÉCRITE en base (`resource_grants.resource_type`,
> kind d'`ownership`) garde son nom d'avant #519 jusqu'au lot D (#526) : elle est nommée
> une seule fois (`ownership.TYPE_RESSOURCE_PROCEDURE`) et traduite à la frontière par
> `resources_contract.KIND_OF`, que lisent la règle `RESOURCE_GOVERN` ET le handler.
> Aucun des deux n'adresse `ownership` sous le nom public. L'ancien nom reste ACCEPTÉ en
> entrée jusqu'à sa date, avec un `deprecation_warning` (`docs/alias-deprecies.md`).
> **Transférer une procédure la DÉPLACE** (`op=transfer`, vers une personne, une équipe
> ou une autre org dont on est membre) : l'`id` ne change pas, et ses révisions, ses liens
> de projet et ses partages la suivent (`org_store.move_instruction`, une transaction ;
> slug suffixé s'il est pris chez la cible, jamais d'écrasement). **La cascade d'un
> projet, elle, COPIE** ses procédures chez la cible et re-pointe le lien (#52) :
> l'originale reste chez la source. Les deux sont écrits dans la description servie.
> Banc : `tests/test_transfert_procedure_oto65.py` (org A → org B, contre un vrai PG).

## Les clés d'un projet partagé (#480, arbitrage du 23/09/2026)

> **La règle.** Qui reçoit un projet y travaille **avec ses propres clés**. Les clés du
> propriétaire — clé d'org, clé de l'équipe propriétaire, accès plateforme accordés à son
> org, plan de son org — ne lui sont prêtées que si le partageur le **déclare** au partage
> (`oto_resource op=share … credentials="inherit"`), **borné à ses propres droits**, et
> **révocable** (`credentials="own"`, ou `unshare`). **Iso** : le même paramètre et le
> même comportement pour une personne, une équipe ou une org — la règle ne regarde que
> l'appartenance de l'appelant à l'org du projet, jamais le type de grant par lequel il
> est entré.

> **Le trou fermé.** `_project=` co-pose l'org propriétaire comme contexte de l'appel
> (`call_axes._pin_project`) ; le barreau ORG de la cascade n'était gardé par aucune
> appartenance, et un bénéficiaire hors de l'org agissait sous ses clés d'org. Le barreau
> équipe l'était déjà (`can_read_group` à la pose).

> **Mécanique** (`access/heritage.py`). Le verdict `ClesDuProjet` se calcule UNE fois à
> la pose de `_project=` (threadpool) et voyage dans un contextvar ; le walker
> (`walk_cascade`), la traversée L7 (`chain_resolution`), la garde d'un binding de projet
> et le plan de l'org (`_win_quota`) le lisent sans requête. Pour un **bénéficiaire hors
> de l'org** : sa clé membre dans l'org du projet s'il l'y a posée, sinon **sa clé
> personnelle posée ailleurs** (org perso d'abord, sinon la plus récente — l'instance
> cross-org de #172, étendue à tout connecteur à clé personnelle, multi-compte compris),
> son tenant, ses propres accès plateforme ; jamais le barreau org de l'org du projet ni
> ses accès `org:<id>`. Le refus « aucune clé » lui dit alors les deux sorties (poser sa
> clé, ou demander l'héritage).

> **L'héritage est une arête de la chaîne de grants** (ADR 0053, point d'extension
> `resource_kind`) : `grants.resource_kind='project_credentials'`, `resource_id=
> 'project:<id>'`, émise par le partageur (`grantor = user:<sub>`), reçue par le principal
> du partage. **Aucune colonne neuve** : révoquer = archiver l'arête (`revoked_at`). La
> **borne se relit à chaque pose** — le prêt n'ouvre le barreau org que si le partageur
> est ENCORE membre de l'org du projet (et le barreau d'équipe que s'il la lit) ; sorti de
> l'org, son prêt s'éteint sans rien réécrire. À la déclaration, un partageur hors de l'org
> propriétaire est refusé (`403 inherit_beyond_sharer_rights`). Omis, `credentials` laisse
> l'état existant intact (un re-partage qui ne change qu'un rôle ne retire pas un prêt) ;
> `op=get` rend le `credentials` de chaque grant d'un projet.

> **Hors des clés, la portée.** L'org co-posée par `_project=` n'est pas pour autant une
> org de l'appelant : pour un bénéficiaire **hors de l'org** (verdict `membre=False`), la
> pièce `("org", X)` sort de tous les seams de portée — `active_org_principals` (donc
> `principaux_de_liste` et `visible_in_org`), `project_scope_owners`, `owner_in_scope`, le
> `_active_scope` du datastore et les partages reçus de la recherche — par un seam unique,
> `ownership.owner_de_portee` (← `heritage.org_du_perimetre`). Sans lui, `data_list_datastores`
> sous `_project=` rendait tout le catalogue de l'org (nom, propriétaire, schéma), et un
> tableau de l'org se lisait par son numéro ou par un `slot:` du projet (la lecture ne
> repasse pas par `can_access`). Le partage d'un projet ne partage pas les tableaux qu'il
> lie : le bénéficiaire lit ceux qui lui sont partagés. L'héritage des clés n'y change
> rien — il prête des clés, jamais l'appartenance
> (`tests/test_projet_partage_hors_org_listes.py`).

## L'échéance d'un partage (otomata-tech/oto#39, 24/09/2026)

> **La règle.** Un partage de ressource (audience × rôle, ADR 0048) peut porter une
> échéance, comme un jeton d'API depuis le 04/09 : `oto_resource[_v2] op=share … ttl_days=N`
> (`POST /api/resources[/v2]`), entier ≥ 1, audiences `person`/`team`/`org` seulement
> (ailleurs : `400 ttl_days_grant_only` ; une valeur illisible : `400 invalid_input`,
> jamais lue comme « sans échéance », contrairement aux jetons). Passée l'échéance, le
> partage **ne donne plus rien** : ni le contenu, ni les listes (projets, tableaux, pages
> reçus, pastille « partagé »), ni le prêt des clés d'un projet (`credentials="inherit"`).
> **Aucun rappel** avant l'échéance (décision du 24/09 ; il attend otomata-tech/oto#272).

> **Stockage.** `resource_grants.expires_at TIMESTAMPTZ`, NULL = sans échéance — tout
> partage d'avant garde son sens. Base existante : révision Alembic
> `0012_partages_echeance`, jouée **avant la fusion** (le code lit la colonne à chaque
> contrôle d'accès). **Le refus vit dans chaque LECTURE** de la table, par un seul
> prédicat (`db/_partage_vivant.py`) ; `tests/test_partages_echeance_39.py` refuse toute
> lecture de `resource_grants` qui ne le porte pas, sauf exemption motivée. La seule :
> `list_resource_grants`, la liste de gouvernance d'`op=get`, qui rend le partage échu
> **marqué** (`expires_at`, `expired: true`) au lieu de le taire — le propriétaire le
> constate, comme un jeton expiré. Elle n'ouvre aucun accès.

> **Re-partager.** Omis, `ttl_days` laisse à un partage VIVANT son échéance (un
> re-partage qui ne change qu'un rôle ne la retire pas en silence) ; un partage neuf ou
> ÉCHU n'en reçoit pas (re-partager ce qui a expiré le rouvre). Retirer une échéance :
> `unshare` puis `share`. `cascade=true` pose la même échéance sur les tableaux et
> procédures liés. L'héritage des clés (`access/heritage.evaluer`) ne lit une arête
> `project_credentials` que pour un principal dont le partage du projet est vivant.
> Hors périmètre : `data_share` (partage d'un tableau par sa propre surface) ne pose pas
> d'échéance.

## Partager avec une adresse sans compte : le partage EN ATTENTE (29/09/2026)

> **Le besoin.** Partager UN objet (projet, tableau, procédure, page) avec quelqu'un qui
> n'a pas encore de compte. Jusqu'ici `op=share` rendait 404 `unknown_user`, et la seule
> issue était d'inviter la personne dans l'ORG — qui lui ouvre tout ce que l'org possède.
> **La règle.** `oto_resource[_v2] op=share` vers un `email` sans compte ne refuse plus :
> il pose un partage EN ATTENTE (`oto_mcp/partage_en_attente.py`). C'est une ligne
> d'`org_invitations` qui porte l'objet (`org_id` NULL, colonnes `resource_*`, révision
> `0028_partage_en_attente`) : même jeton long (seul son hash est persisté), même lien
> `/invitation/<token>` envoyé par le gabarit de partage, même échéance, même refus par
> l'invité. **À l'inscription** (adresse vérifiée, `upsert_user` →
> `honorer_au_signup`) **ou en ouvrant le lien** (`org.invite.accept`), la personne reçoit
> l'accès à CET objet par `ownership.grant`, et jamais une adhésion. Au moment d'honorer,
> l'émetteur doit toujours gouverner l'objet : sinon le partage est caduc et ne donne rien.
> **La réponse** porte `pending: true` et `pending_note` ; un second partage vers la même
> adresse rend le premier (`already_pending`, un index unique partiel l'impose). `op=get`
> liste le partage en attente dans `grants` (`pending: true`, sans `principal_id`) ;
> `op=unshare` avec la même adresse le retire. **Refusé** (`pending_share_plain_only`) avec
> `cascade=true` ou `credentials="inherit"` : ce sont des gestes sur un compte, à refaire
> une fois la personne inscrite. `ttl_days` se garde : l'échéance court depuis
> l'acceptation. Un `sub` inconnu reste un 404 : il désigne un compte, pas une adresse.

## Partager UNE page sans son projet (kind `doc`, signal #1084)

> **Le besoin.** Faire lire une page d'un projet à des personnes d'une autre org sans leur
> ouvrir le projet (ses autres pages, internes comprises) ni rendre la page publique
> (`oto_doc op=set_public`, lisible par quiconque a le lien).
> **La surface** est celle des autres familles (ADR 0047/0048) : `oto_resource op=share
> resource_type="doc" resource_id=<id de la page>` + `email`/`sub` · `org_id` ·
> `group_id` ; `op=unshare` retire ; `op=get` rend la fiche et ses bénéficiaires ;
> `op=list` rend les pages partagées une à une. Le destinataire lit par `oto_doc op=get`
> et retrouve par `oto_doc op=shared_with_me` (toutes orgs confondues : vue « moi », servie
> dans toute org).
> **Sa portée se choisit (21/09/2026)** : `scope="me"` = les pages partagées à la PERSONNE
> seule, quelle que soit l'org ; `scope="org"` = celles partagées à l'org CONSULTÉE
> (`X-Oto-Org` en REST, l'org de session en MCP) et aux équipes de l'appelant dans cette
> org, jamais à lui — sauf dans son org PERSO, où les pages partagées à lui s'y ajoutent
> (`ownership.principaux_de_liste`, décision du 28/09/2026) — c'est ce qu'affiche
> l'écran des projets d'une org. Sans `scope`, l'union historique (lui, toutes
> ses orgs, toutes ses équipes) reste servie : un contrat servi se double, il ne se durcit
> pas en place. La réponse nomme la portée appliquée (`scope`, `null` = l'union).
> **Aucun DDL** : `resource_grants.resource_type` est un TEXT libre ; la valeur persistée
> est `doc` (`docs/common.DOC_RTYPE`).
> **Le kind `doc`** (`capabilities/docs/common.py`) : une page n'a pas de propriétaire
> propre — `owner_getter` rend celui de son PROJET, et `governed_by` fait gouverner la
> page par son projet (`ownership.can_govern` ne lit que ce parent). Qui partage le projet
> partage ses pages ; un lecteur de la page ou du projet, jamais. `reparent` refuse (une
> page se déplace par `op=move`, elle ne se transfère pas).
> **Ce que le partage ouvre : `op=get` sur CETTE page, et rien d'autre.** La seule porte
> qui lit un partage de page est `common.acces_a_la_page`, appelée par `reads.get` seul ;
> `list`, `search`, `revisions`, `backlinks` et toutes les écritures restent gardées par
> le projet. D'où : ni pages sœurs, ni sous-pages (le grant est lu sur `(doc, id)` de la
> ligne, jamais hérité), ni historique (une version antérieure peut porter ce que
> l'auteur a retiré avant de partager), ni backlinks (ils nomment d'autres pages), ni
> tableaux liés ou intégrés (leur droit est le leur). L'`url` servie vaut `None` pour
> ce lecteur : elle ouvre la page dans son projet, qu'il ne lit pas.
> **Rôle `viewer` seulement** (`doc_viewer_only` sinon) ; `public`/`secret` ne
> s'appliquent pas à une page par ce chemin (`publication_unsupported`).
> **Trace** : le partage passe par le même handler que les autres familles — e-mail au
> destinataire personne (`_notify_grant`, libellé « page ») et ligne
> `portee_elargissements` (ADR 0068 §4, muette) qui nomme l'AUTEUR de la page comme
> propriétaire du contenu élargi.
> ⚠️ **Ce qui n'est pas ouvert** : `oto_doc_app`, la fiche de nœud (`oto_node`, qui sert
> le fil d'Ariane et la fratrie) et la recherche restent gardées par le projet — le
> destinataire n'y voit rien, et la section « Partagé » du chrome compte ces partages
> dans `grants_sans_noeud`. Un partage SUIT la page si elle change de projet
> (`op=move to_project`) ; supprimer la page laisse une ligne de grant orpheline,
> inerte (ids jamais recyclés, listes jointes sur `docs`).
> Bancs : `tests/test_partage_une_page_1084.py` (base réelle, sentinelles).

## Privé par défaut, et l'observation qui l'accompagne (ADR 0068, 04/09/2026)

> **La règle.** Aucune opération ne donne à ce qu'elle crée une portée plus large que
> son auteur, sauf si l'appel le demande par un **paramètre nommé**. Trois régimes,
> gradués par la **réversibilité** de l'élargissement et non par sa gravité ressentie :
> ① **privé** partout par défaut ; ② **org / équipe** explicite, ouvert à un agent
> (population nommée, comptes, administrateur — l'élargissement se répare) ; ③ **sans
> login** interdit à un agent, disponible sur la face REST où vit le dashboard (ce qui
> est servi sans compte est indexable ; le retirer n'efface pas ce qui a été lu).
> Le mécanisme : `ResolvedCtx.channel`, posé aux deux SEUILS (`_mcp_adapter`,
> `_rest_adapter`) et **jamais** par une règle d'autz — les règles servent les deux
> faces et ne peuvent pas savoir d'où vient l'appel.
> ⚠️ **Ce n'est pas un contrôle d'accès** : un porteur de jeton peut appeler la face
> REST et faire ce que la face MCP lui refuse. Le régime ③ vise le geste NON VOULU,
> pas l'adversaire — l'appeler « sécurité » ferait croire posé un contrôle qui ne l'est
> pas, et personne ne poserait le vrai ensuite (`capabilities/_publication.py`).

> **Le second volet : savoir — en OBSERVATION, rien ne part.** Une garde ne couvrira
> jamais tous les chemins, et un agent peut légitimement passer le paramètre parce que
> la demande était ambiguë. `capabilities/_portee.observer()` enregistre donc chaque
> élargissement fait par un agent dans `portee_elargissements`, avec **les
> destinataires qu'on aurait prévenus** (le propriétaire ET l'auteur du geste) et
> **l'urgence qu'il aurait eue** (`immediat` = ouverture sans login ; le reste serait
> groupé, un agent qui partage trente lignes devant produire un message et non trente).
> ⚠️ **Aucun e-mail n'est envoyé** (décision d'Alexis) : on mesure d'abord le volume —
> `oto-mcp maintenance portee-observation`, lecture pure, sans `--apply`. Ouvrir un
> canal en devinant son débit, c'est le refermer une semaine plus tard après avoir
> appris à ses destinataires à l'ignorer. `notifie_at` reste NULL, et ce NULL EST la
> preuve que rien n'est parti.
> ⚠️ **`tool_calls` journalise déjà tout, et personne ne le regarde** : journaliser
> n'est pas avertir. Cette table ne retient que les gestes qui CHANGENT QUI VOIT, et
> n'enregistre jamais le contenu élargi — une trace qui recopie ce qu'elle surveille
> est un second exemplaire à protéger.
> ⚠️ Limite CONNUE de l'observation : le canal `mcp` est un proxy d'« agent », pas la
> vérité. Un jeton `oto_` porté sur la face REST est une machine et n'est pas compté ;
> le distinguer demande de séparer, sur REST, un JWT Logto d'un jeton porté.

> **Le palier PERSONNEL des procédures est ouvert (04/09/2026, phase 2 de #681).**
> Décision d'Alexis : « procédure doit pouvoir être privée ». Le préalable était réel —
> `org_instructions.org_id` était `NOT NULL`, elle porte l'org PARENTE du propriétaire
> **et la cascade de suppression**, et une personne n'a pas d'org parente. Y ranger son
> org de CONTEXTE aurait fait disparaître une procédure personnelle avec l'org : le
> store refusait d'écrire cette ligne-là, à raison. La colonne est relâchée **aux deux
> tables** (vivante et historique — la laisser `NOT NULL` sur l'historique ferait
> échouer la première ÉCRITURE, pas la création, donc bien après qu'on aurait cru le
> lot fini) ; une procédure perso y porte `NULL`.
> Ouvert : `OWNER_TYPES` gagne `user`, `_parent_org_id` rend `None`, `_owner_of`
> accepte `scope='user'` (identité prise dans le contexte d'autz, **jamais** un champ
> client — le palier personnel d'autrui n'est pas atteignable), `_get_guide` a sa
> branche de lecture, et `oto_resource op=transfer` déplace une procédure vers une
> personne.
> ⚠️ **Le DÉFAUT reste l'org** — c'est un choix, pas un oubli. Le basculer fait tomber
> une vingtaine de bancs et une garde (`…_reads_honor_explicit_org`) : l'absence de
> scope voyage jusqu'à des modèles d'entrée en aval qui l'exigent. Ouvrir la porte
> d'abord, déplacer le défaut ensuite — dans cet ordre chaque pas se vérifie seul.
> ⚠️ Le défaut des surfaces d'ADMIN (`org.instruction.*`, gardées `ORG_ADMIN_OPT`)
> restera l'org quoi qu'il arrive : leur objet EST d'écrire la procédure de l'org.

## Partager un agent hébergé dans son org (kind `runner_trigger`, 02/10/2026)

> Un agent (`runner_triggers`) appartient à la PERSONNE sous qui il tourne
> (`owner_getter` = `("user", trigger.sub)`, `reparent` = `db.reprendre_trigger`, vers
> une personne seulement). Ses partages sont des lignes ordinaires de
> `resource_grants` (`viewer`/`editor`), mais ils se posent par `oto_trigger
> op=share|unshare|shares`, jamais par `oto_resource` : un agent ne se partage qu'à
> l'intérieur de son org. La règle et ses raisons : `docs/runner-et-automatisations.md`
> § « Un agent est à son propriétaire ».
