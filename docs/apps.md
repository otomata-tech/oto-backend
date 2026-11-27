# Apps — des écrans bâtis sur la plateforme

Une **app** est un écran qu'un utilisateur (ou un agent, pour lui) bâtit sur ses
tableaux et ses agents : tableau de bord, file de revue, formulaire, dépôt de fichier
qui lance un agent. Capacité `me.app` : outil MCP `oto_apps`, REST `POST /api/me/apps`
(une route, un `op`, comme `/api/me/docs`).

## Ce qu'est une app

- **Un manifeste** (JSONB) : les tableaux et champs qu'elle lit, les valeurs qu'elle
  peut poser sur un champ (`set`), les colonnes qu'un formulaire peut remplir (`add`),
  les agents qu'elle lit — ou lance, avec `run: true` (et `input: true` pour leur
  passer un texte) —, `files: true` pour déposer des fichiers dans l'app.
- **Du HTML**, exécuté par le front dans un cadre isolé (origine opaque, sans réseau,
  derrière un document-garde qui interdit toute navigation sortante), qui ne parle à
  la plateforme que par le pont exposé sur `window.oto` (`oto.rows()`, `oto.set()`,
  `oto.upload()`, `oto.files()`, `oto.runAgent()`, `oto.open()`…). Le serveur ne
  l'exécute jamais.
- **Une vignette** (image publiée sur le stockage média, ou couleur + emoji, et les
  réglages du dessin qui l'a produite). ⚠️ L'image doit être sous la base publique
  du stockage média de l'instance (`media_store.public_base()`) : la liste l'affiche
  hors du cadre isolé, une adresse libre ferait appeler un tiers par chaque lecteur.

Ce **n'est pas une page de mémoire** : ni dans l'arbre des pages, ni dans la base de
connaissance ; `oto_search` la trouve par son TITRE seul (kind `app`, celles que
`op=list` rend : « cherchable ⇔ lisible »), jamais par son HTML. Tables `apps`, `app_revisions`,
`app_projects`, `app_consents`, `app_files` (`db/schema/apps.py`) ; lectures et écritures dans
`db/apps.py` (`db.apps.<fn>`).

## À qui est une app, qui l'ouvre

**Une app a son propriétaire et ses partages, comme un tableau** — kind `app`
d'`ownership` (`apps.owner_type/owner_id`, partages dans `resource_grants`,
`resource_type='app'`). Décision du 28/10/2026 :

- **Une app neuve appartient à qui la crée, dans l'org active** (`context_org_id`),
  **privée** — sauf si un admin de l'org a réglé le partage par défaut des apps
  (`oto_org_settings domain=partage_par_defaut`, type `app` : `aucun` | `lecture` |
  `ecriture`) ; ce partage d'office est MARQUÉ et se retire seul. `ecriture` est
  permis pour une app (refusé pour un agent) : une app n'a pas d'identité à elle,
  chaque geste agit au nom de son lecteur, borné par son auteur. `lecture` pose un
  partage `viewer` : l'org OUVRE l'app sans en cliquer les boutons (View, ci-dessous).
- **View / Use / Edit / Manage** — `viewer`, `contributor`, `editor`, `manager`, par
  `oto_resource_v2` (`resource_type=app` : `get`, `share`, `unshare`, `transfer` ;
  l'héritée `oto_resource` est sous cliquet et ne prend pas `contributor`) — le même
  écran « Partager » qu'un projet. View = `can_access` lecture (l'ouvrir, la lire) ;
  **Use** = l'ouvrir ET cliquer ses boutons (`apps_appel.peut_cliquer` : écriture sur
  l'app, ou un partage `contributor`, nominatif, d'équipe ou d'org), sans la modifier —
  projection contenu `read`, refusé hors d'une app (400 `role_for_apps_only`) ; Edit = la
  modifier ; Manage = la repartager (gouvernance : propriétaire, admin, gérant). Use ne
  donne aucun droit sur ce que l'app touche : chaque geste reste jugé auteur ∩ lecteur
  (ci-dessous). Un partage `viewer` d'avant ce rôle reste View : il ne clique plus —
  `oto-mcp maintenance apps-lecteurs-actifs` liste ceux qui cliquaient et, avec
  `--appliquer`, les passe en Use (`docs/commands.md`). Transférer l'app à l'org ou
  à une équipe la rend à tous leurs membres (propriétaires : ils cliquent).
- **L'org de l'app suspendue** ferme l'app à tous ses lecteurs, ceux d'une autre org
  compris (`docs/orgs-suspendues.md` §2) ; l'org de l'appel reste gardée comme toute
  capacité.
- **L'org d'une app** (`apps_manifeste.org_de_l_app`) est celle de son propriétaire :
  l'org, l'org de l'équipe, et pour une app perso l'org où elle a été créée (sinon
  l'org perso de la personne). C'est là que vivent ses tableaux et ses agents, et là
  qu'elle se liste.
- **Un projet RANGE une app, il ne l'ouvre pas** (`app_projects`, 0..n projets) :
  `op=file` / `op=unfile` (écrire l'app ET le projet ; des écritures pour la garde de
  lecture seule) l'affichent sur la page du projet ; une app rangée dans un projet
  qu'on lit reste invisible si elle n'est pas partagée. Une carte ne nomme
  (`project_ids`) que les projets que son lecteur LIT : ranger ne révèle pas un projet.
- **`op=list`** sans projet : les apps de l'org active — possédées par l'org ou ses
  équipes, mes apps perso créées ici, partagées à l'un des principals de la liste
  (mêmes seams que la liste des projets, « listé ⇔ lisible »). Avec `project_ids` :
  celles qui y sont rangées et que je lis. Chaque carte porte `project_ids` et
  `can_edit`.
- **Migration** (`20261030_1200_apps_owned`) : les apps d'avant prennent le
  propriétaire et l'org de leur projet, y restent rangées, et reçoivent une copie des
  partages du projet — personne ne perd l'accès qu'il avait. ⚠️ **Entre le tag et
  `migrer upgrade head`, les apps sont invisibles** (fail-closed : sans propriétaire,
  personne ne les lit) — le démarrage ne joue que le DDL : jouer la migration juste
  après le tag. Index `apps (owner_id)` et `app_projects (filed_by)` : la fusion de
  comptes repointe ces colonnes d'un bloc.

## Rôles d'une app (`manifest.roles`, `capabilities/apps_roles.py`)

Une app peut dire **qui, parmi ses lecteurs, voit et fait quoi** : le manifeste déclare
des rôles (`roles: {admin: {…}, user: {…}}`) et un `default_role`, OBLIGATOIRE dès
qu'un rôle existe. Chaque rôle **resserre** le manifeste, jamais plus :

- `tables` absent = tous les tableaux déclarés ; présent = ceux-là seuls, chacun `true`
  ou `{fields?, set?, add?}` — une clé absente hérite du manifeste, une clé présente
  en est un sous-ensemble (`set: {}`, `add: []` : aucune écriture). Jugé à l'écriture
  (`apps_manifeste.forme`) : un rôle qui nomme ce que le manifeste n'ouvre pas est
  refusé, 400 `invalid_manifest` ; ses champs sont jugés contre le schéma comme ceux
  du manifeste.
- `agents` (liste de noms, ou `{nom: true | {run?, input?}}`), `files` (`false` = ni
  dépôt ni liste), `sections` (noms libres, retirés du HTML marqué — ci-dessous), `label`,
  `rows` (périmètre de lignes, plus bas).

**Qui tient quel rôle.** Le rôle d'app d'un PARTAGE de l'app
(`resource_grants.app_role`, révision `20261127_1200_app_roles`) : posé par qui
GOUVERNE l'app (`oto_apps op=set_role`, `principal_type`, `principal_id`, `role` ;
sans `role` = retour au défaut ; 404 `not_shared` sans partage, 400 `unknown_role` pour
un rôle que ni l'app publiée ni son brouillon ne déclare), lu par `op=roles`. Il part
avec le partage (retrait, échéance) et suit la fusion de comptes comme lui ; un partage
échu puis repris est un partage NEUF et repart du `default_role` (`db.grant_resource`),
comme après un retrait — ni plus étroit ni plus large par principe : ce que choisit qui
repartage, pas un reste. Changer le rôle de PARTAGE (Voir → Utiliser) ou l'échéance
d'un partage vivant le garde. Un rôle ne se pose que sur un partage vivant. Nominatif
d'abord, puis équipe, puis org ; à type égal, le premier dans l'ordre du manifeste.
Sans rôle posé, ou un rôle que le manifeste ne déclare plus : `default_role` — jamais
le manifeste entier. **Qui peut modifier l'app n'est borné par aucun rôle** (il
réécrirait le manifeste) ; il l'essaie sous un rôle par `as_role` sur l'appel
(403 `as_role_editors_only` sinon). Une app possédée par une org est modifiable par
tous ses membres : pour des rôles entre collègues, la garder possédée par une personne
ou une équipe et la PARTAGER à l'org (Use).

**Où il s'applique : une fois, à l'entrée de l'appel** (`apps_appel._app_pour` →
`_au_role`). Toute action lit le manifeste après ce point — lectures, lot, agrégats,
gestes et leur consentement, dépôt et liste de fichiers, runs — donc toutes sont
bornées sans le savoir. `op=get` sert au lecteur le manifeste de SON rôle, sans la
définition des rôles, et `access` `{role, role_label, sections}` (ce que l'app lit par
`oto.access()`) ; à qui peut la modifier, le manifeste entier et `role: null`.

**Sections : le balisage marqué est retiré au serveur** (`capabilities/apps_sections.py`).
L'auteur marque une partie du HTML par `data-oto-section="nom"` (plusieurs noms séparés
par des blancs : visible si le rôle en ouvre UN). Servi à un lecteur borné par un rôle
(`op=get` — le seul chemin qui rend du HTML à qui ne peut pas modifier l'app : le
brouillon, `revision` et `restore` sont réservés aux éditeurs, `revisions` ne porte
pas de HTML), le HTML perd **l'élément entier** — balise, contenu, `<script>` et
`<style>` intérieurs — de chaque section que son rôle n'ouvre pas : il n'atteint pas le
navigateur, « afficher la source » comprise. Un `<script data-oto-section="admin">` part
aussi. Rôle sans `sections` : rien n'est retiré ; `sections: []` : tout élément marqué
part ; qui peut modifier l'app, ou une app sans rôle : le HTML entier. `oto.access()`
dit toujours à la page ses sections. Le reste du HTML est recopié à l'octet, et `rev`
reste celui de l'app.

- **Fermé du côté sûr.** Un lecteur linéaire suit le découpage du tokenizer HTML
  (commentaires, valeurs entre guillemets contenant `>`, texte brut de `script` et de
  ses états échappés, `style`, `textarea`, `title`, CDATA et `/>` en SVG/MathML) ; un
  élément marqué ne finit qu'à SA balise fermante, et seulement si le navigateur la
  suivrait (une fermeture ignorée à travers un tableau, une liste, un `select`, un
  `template`… est ignorée ici aussi), jamais par la fermeture d'un ancêtre. **Jamais
  fermé — ou un élément marqué intérieur fermé par celle d'un autre — : tout jusqu'à la
  fin du document part** (un avertissement le dit à l'écriture). Retirer trop casse l'affichage d'un
  rôle ; retirer trop peu fuirait. Fermer explicitement chaque élément marqué (`</li>`,
  `</p>`, `</td>` compris).
- **Ce qui n'est PAS protégé** : tout ce qui vit hors d'un élément marqué — une
  constante ou un appel dans un `<script>` non marqué, un gabarit HTML construit en JS
  (`'<div data-oto-section="admin">'` dans une chaîne n'est PAS lu), le CSS, les
  attributs d'éléments non marqués. Mettre ce qui n'appartient qu'à une section DANS un
  élément marqué (un `<script data-oto-section="admin">` pour son code, un
  `<template data-oto-section="admin">` pour son gabarit). Les données des tableaux sont
  protégées par le rôle de toute façon : ce qu'il ne lit pas n'est jamais servi.
- **Le mode STRICT** (`apps_sections.strict`, `non_classes`) — dès qu'un rôle déclare
  `sections`, sauf `strict_sections: false` au manifeste : **chaque `<script>`, chaque
  `<style>` et chaque balise à gestionnaire `on…=` doit être classé** — marqué d'une
  section, ou déclaré COMMUN par `data-oto-section="*"` (servi à tous les rôles, même à
  `sections: []` ; `*` est réservé, aucun rôle ne le déclare). Jugé sur ce qui reste une
  fois retirés les éléments marqués d'une section (le texte d'un script n'est pas lu
  comme du balisage). ⚠️ **C'est la seule exception à « le HTML avertit, ne refuse
  jamais »** : à CHAQUE écriture (`create`, `update` — y compris un manifeste seul sur le
  HTML gardé —, `patch`, `restore`, `publish`), 400 `sections_not_classified` qui nomme
  chaque ligne, rien d'écrit ; `op=validate` le rend dans `problems`. Au service, en
  dernier recours (une app écrite avant la règle, ou base modifiée à la main), un
  lecteur borné reçoit 403 `app_unavailable`, sans nom de rôle ni de section ; qui
  modifie l'app l'ouvre et voit le problème en écrivant. Ce que le mode strict ne
  couvre PAS : le balisage et le texte non marqués (communs par définition), les
  attributs `style=`, les URLs — et ce qu'un script COMMUN fait : un avertissement
  signale une section nommée en clair dans le commun (`noms_dans_le_commun`).
- **À l'écriture**, en `warnings` (jamais un refus) : une section qu'un rôle ouvre sans
  qu'aucun élément ne la porte (ce que la page cache pour elle par script part chez
  tous), une marque qu'aucun rôle ne déclare (retirée pour tout rôle à `sections`), un
  élément marqué jamais fermé. `op=validate` ne les rend qu'à qui peut modifier l'app.

⚠️ Le rôle ne fait que retirer : Use reste nécessaire pour cliquer, et
chaque geste reste jugé auteur ∩ lecteur. Un lecteur qui tient le tableau par un
partage ORDINAIRE le lit en entier hors de l'app : pour que le rôle soit la seule
porte, ouvrir le tableau **par l'app seulement** (ci-dessous) au lieu de le partager.

### Accès par l'app (`app_table_access`, `datastore/par_l_app.py`)

Un tableau peut être ouvert à quelqu'un **à travers une app seulement** : `oto_apps
op=grant_through_app` (`app_id`, `principal_type`, `principal_id`, `tables` optionnel —
par défaut tous ceux que l'app déclare), `op=revoke_through_app` pour le retirer,
`op=roles` le liste par partage (`through_app`). Règles :

- **Poser = partager le tableau** : qui gouverne l'app ET peut partager chaque tableau
  (`can_govern`) ; un tableau qu'il ne peut partager est sauté et nommé (`skipped`).
  Seulement à un principal qui tient un partage VIVANT de l'app (404 `not_shared`).
- **Droit** : `write` si l'app écrit dans ce tableau (`set` ou `add`), sinon `read` ; à
  l'appel, `write` retombe à `read` si le manifeste resserré au rôle n'y écrit plus.
- **Ne vaut que dans l'appel de CETTE app** : `apps_appel._par_l_app` ouvre une portée
  (`ContextVar`) le temps de l'appel, pour les seuls tableaux du manifeste resserré ;
  `DatastorePg._trouver` la consulte quand le lecteur ne tient pas le tableau lui-même
  assez. **Aucune autre lecture ne voit `app_table_access`** : outils `data_*`, face
  REST des tableaux, écran Tableaux, recherche, une autre app — rien. Le reste de
  l'appel est jugé comme avant (manifeste, rôle, auteur ∩ lecteur, consentement) ;
  l'historique des lignes garde le lecteur.
- **Part avec le partage de l'app, et ne revient pas avec un autre.** L'accès est lié
  à l'IDENTITÉ du partage sous lequel il a été ouvert (`share_id` : un numéro jamais
  repris, tiré par `db.grant_resource`, posé aussi sur `app_table_access`) et ne compte
  que tant que le MÊME principal tient CE partage, vivant (`db.apps._sous_son_partage`).
  Un partage vivant garde son identité quand on change son rôle (Voir → Utiliser,
  Manage) ou son échéance : l'accès reste. Un partage reposé en a une neuve — après un
  retrait, une échéance passée (il n'y a pas d'événement : la ligne échue reste, et la
  reprise par `ON CONFLICT` change l'identité), un transfert qui fait du principal le
  propriétaire puis l'en retire, un lien d'invitation, un partage d'org par défaut
  (`partage_par_defaut` : changer le réglage ne retire aucun partage ; retirer celui
  d'une org est un retrait ordinaire) — : **rien de ce qui avait été ouvert ne revit,
  il se rouvre** (`grant_through_app`). Deux gardes, la seconde de propreté : la
  liaison (aucun chemin qui termine un partage ne peut l'oublier, puisqu'elle ne
  dépend d'aucun) et le retrait des lignes avec le partage au point unique
  `db.revoke_resource_grant` (`oto_resource op=unshare` et sa cascade, `ownership.revoke`,
  le nouveau propriétaire d'un `transfer`) et avec une équipe supprimée
  (`group_store.delete_group`). Les listes (`op=roles`, `through_app` du tableau) ne
  montrent que les accès qui valent. Une app supprimée emporte les siens (FK) ; une
  équipe ou une org n'a jamais son numéro repris. La fusion de comptes déplace partage
  et accès ensemble (même identité) ; si les deux comptes tenaient la même ligne, celle
  du survivant reste, et ne vaut que sous SON partage. Un transfert de l'app laisse
  aux AUTRES principaux leur partage, donc leur accès — toujours borné par l'auteur
  du manifeste, qui doit pouvoir partager le tableau. Un partage d'avant `share_id`
  (NULL) est numéroté au premier accès ouvert sous lui ; une ligne sans partage
  numéroté ne vaut rien.
- **Un agent lancé depuis l'app en hérite, en LECTURE** (`apps_appel._droit_du_run`) :
  le run d'un travail sur lequel le lecteur a brûlé un consentement `start_agent` de
  cette app (`app_consents.job_id`, travail de la requête `verrou_org`) lit ce que ce
  lecteur lit par l'app, rejugé à chaque résolution (partage vivant, rôle, auteur qui
  peut partager le tableau). Deux bornes de plus, parce que l'agent lit par les outils
  de tableau, qui ne projettent pas : **lecture seule**, et seulement d'un tableau que
  le rôle ouvre **entier** (sans liste de champs). Un tableau aux champs bornés ne lui
  est pas ouvert.
- **Le gérant du tableau le voit et le retire** : `oto_resource op=get` d'un tableau
  rend `through_app` (app, principal, droit) ; `oto_apps op=revoke_through_app` avec
  `tables` (numéros) lui suffit, sans aucun droit sur l'app.

### Périmètre de lignes (`roles.<r>.rows`, `resource_grants.app_scope`)

Un lecteur peut ne voir d'un tableau que **SES lignes** — un partenaire sa fiche, jamais
celle du voisin. Deux façons de le dire, cumulables :

- **Une valeur posée sur le partage** : `oto_apps op=set_scope` (`app_id`,
  `principal_type`, `principal_id`, `scope` `{tableau: {champ: valeur}}` ; sans `scope`
  = retiré). Rangé par NUMÉRO de tableau (`resource_grants.app_scope`, même révision
  `20261127_1200_app_roles`) : renommer le tableau dans le manifeste ne l'éteint pas ;
  `op=roles` le rend par nom (`grants[].scope`). Gardes : gouverner l'app ET pouvoir
  partager chaque tableau du périmètre posé et de celui qu'il remplace (403
  `forbidden`) ; un partage existant (404 `not_shared`) ; chaque champ une colonne
  déclarée d'un type qu'une égalité compare (texte, nombre, email, url, téléphone, enum,
  lien, booléen — ni date, ni JSON, ni formule, ni composite) et chaque valeur un
  scalaire non vide (400 `invalid_scope`, rien d'écrit). Il part avec le partage et suit
  la fusion de comptes comme lui.
- **Une colonne déclarée par le rôle** : `roles.partner.rows = {fiches: {contact_email:
  "$email"}}` — l'adresse VÉRIFIÉE du lecteur (celle de l'annuaire, seulement quand le
  jeton de la requête porte `email_verified: true`) — ou `"$share"` — la valeur posée
  sur son partage pour ce champ. Jugé à l'écriture : tableau déclaré ET ouvert par le
  rôle, nom de colonne nu, valeur `$email` ou `$share`, champ que le rôle ne peut PAS
  poser (`set` : sinon il déplacerait une ligne vers le voisin), puis contre le schéma
  (`apps_manifeste.auteur`, même sur un tableau sans liste de champs) : déclaré, d'un
  type comparable.

**Ce qui s'applique, une fois, à l'entrée de l'appel** (`apps_appel._au_role` →
`apps_roles.lignes_du_lecteur`, rangé dans `app["row_scope"]`) : par tableau du manifeste
resserré, chaque contrainte RESTREINT (ET) — la valeur du rôle et chaque valeur posée
sur un partage retenu ; deux valeurs différentes pour un champ = aucune ligne.
**Fail-closed** : `$share` sans valeur posée, `$email` sans adresse vérifiée (jeton API,
« voir en tant que », claim absent) = le tableau ne sert AUCUNE ligne et refuse tout
geste. L'égalité est exacte ; pour `$email`, l'adresse telle que l'annuaire la tient
ou en minuscules.

**Quel partage** : celui dont le rôle a été retenu (nominatif > équipe > org, puis ordre
du manifeste) ; au rôle par défaut, ou sans rôles, les partages du type le plus précis
qu'il tient. Plusieurs partages retenus (deux équipes, même rôle) : tous leurs
périmètres s'appliquent (ET). Un partage nominatif sans périmètre l'emporte donc sur un
partage d'org qui en porte un : c'est le partage le plus précis qui dit ce qu'il voit.

**Toutes les portes** :
- `read_rows`, `read_aggregate`, chaque lecture d'un `batch` : le périmètre part au
  store en `filters` (la forme riche), en ET avec le `filter` de l'app et la recherche
  `q` — l'app ne peut ni le remplacer (nommer le même champ ne fait que restreindre) ni
  en sortir par la recherche.
- `write_cell` : la ligne visée est relue au serveur par la même requête (périmètre +
  `_id`) avant tout — hors du périmètre, 404 `row_not_found`, le refus d'une ligne qui
  n'existe pas (jamais le 409 de révision qui citerait celle du voisin) ; un champ du
  périmètre ne se change pas (403 `row_scope_field`). La révision attendue couvre la
  course : une ligne sortie du périmètre entre le contrôle et l'écriture a changé de
  révision.
- `add_row` : les champs du périmètre sont IMPOSÉS — la valeur du lecteur remplace la
  sienne, qu'ils soient dans `add` ou non ; le résumé du consentement montre la valeur
  imposée ; périmètre non résolu → 403 `row_scope_unresolved`.
- le consentement juge exactement comme l'appel (`_geste`), et l'appel rejuge.
- **un run d'agent n'hérite JAMAIS d'un tableau à périmètre** (`_droit_du_run`) : l'agent
  lit par les outils de tableau, qui ne posent pas ce filtre.

**Qui n'est pas borné** : qui peut modifier l'app (il réécrirait le manifeste). Par
`as_role`, il essaie un rôle sans partage retenu : `$email` se résout sur SA propre
adresse vérifiée, `$share` ne trouve rien (aucune ligne). ⚠️ Comme le rôle, le
périmètre ne borne que l'appel de l'app : un lecteur qui tient le tableau par un partage
ordinaire le lit entier ailleurs — ouvrir le tableau **par l'app seulement**.
⚠️ Résidu connu : `add_row` sur un tableau à clé métier rend 409 `business_key_exists`
si la clé est déjà portée, y compris par une ligne hors du périmètre — choisir une clé
qui contient le champ du périmètre, ou aucune.

## Ce que le serveur juge

- **À l'écriture** (`capabilities/apps_manifeste.py`) — `create`, `update` ET
  `restore` : la forme du manifeste (mêmes règles que le front, au plus 32 000
  caractères de JSON), puis que chaque tableau déclaré se résout **pour l'auteur**,
  que chaque champ déclaré existe dans son schéma, et que chaque tableau et chaque
  agent **vivent dans l'org de l'app** (`org_de_l_app`) — jamais dans l'org active de
  l'auteur ni du lecteur. Un manifeste qui nomme ce que l'auteur ne voit pas,
  ou ce qui vit ailleurs, est refusé, 400 `invalid_manifest`, chaque problème listé ;
  `op=validate` (`app_id`, sinon l'org active ; `html` optionnel) rend la liste sans
  rien écrire.
- **Le HTML se LIT à l'écriture, sans refuser** (`capabilities/apps_html.py`) —
  `create`, `update`, `patch`, `restore` et `validate` (qui prend aussi `html`) rendent
  `warnings` : chaque appel `oto.rows` / `oto.aggregate` écrit en clair (le pont
  sous un autre nom compris, `const T = window.oto`) qui trie, filtre, regroupe,
  mesure ou cherche hors de ce que le manifeste déclare, avec sa ligne. Ce sont
  exactement les appels que l'app ouverte se verrait refuser (« An app can only sort
  on a field it declares. ») devant son premier lecteur : l'agent qui écrit les
  corrige tout de suite. Seul le contenu des `<script>` est lu, commentaires blanchis ;
  un appel par variable n'est pas jugé, un `orderBy` / `groupBy` hors appel l'est
  contre tous les tableaux. Avertir, jamais refuser : c'est une lecture du texte, pas
  une exécution. Les avertissements se calculent AVANT l'écriture (un défaut du lecteur
  refuse sans rien écrire, jamais après une création commitée), et la lecture est
  bornée (`MAX_APPELS` appels examinés, `MAX_ARGS` caractères d'arguments par appel).
- **`op=patch` édite le HTML sans le réémettre** (`capabilities/remplacements.py`,
  moteur partagé) : `edits = [{find, replace, all?}]`, appliqués dans l'ordre, tout ou
  rien ; chaque `find` exact et unique (deux occurrences qui se chevauchent sont
  ambiguës aussi ; sinon `all: true`), absent ou ambigu → 400 `invalid_edits` qui
  nomme l'edit, rien d'écrit ; la longueur que le HTML AURAIT est jugée avant chaque
  remplacement (au plus 1 000 000 de caractères, `MAX_HTML`). Mêmes gardes que `update` : bêta,
  écriture sur l'app, auteur qui lit ce que l'app déclare (→ 403 `would_break_app`),
  révision ; `expected_rev` → 409, et sans `expected_rev`, la révision LUE (une
  écriture concurrente n'est jamais écrasée sans un mot). Il rend un **reçu** (`rev`,
  `replaced`, `warnings`), jamais le HTML : l'op existe pour qu'un agent change une
  ligne d'une app de 40 000 caractères en deux cents jetons, au lieu de la réécrire
  entière — ce qui coupait l'agent intégré à `max_tokens`.
- **Chaque tableau déclaré est rangé par son NUMÉRO** (résolu à l'écriture pour
  l'auteur) : un nom résoudrait dans l'org active de qui le lit.
- **`set`, `add`, `agents` bornent les gestes** de l'appel d'une app (plus bas).

## Qui écrit une app

- **Toute org** depuis le 2026-11-04 : `create`, `update`, `patch`, `archive`,
  `unarchive`, `restore`, `file`, `unfile` ne demandent plus l'option `beta` (le refus
  403 `apps_beta_only` est retiré, `oto_apps` est sorti de `BETA_TOOLS`). Ce qui borne
  une app reste le manifeste, jugé contre la portée de l'AUTEUR et l'org de l'app, et
  les droits de chacun sur l'app (écriture pour la changer, lecture pour l'ouvrir).
- **REST : session interactive seule** (`allow_api_token=False` sur `/api/me/apps`
  et sur l'appel) : un jeton `oto_`, délégation d'agent hébergé comprise, est refusé
  en 403 `api_token_forbidden`. Un agent écrit une app par `oto_apps`, en MCP.

## Une app ouverte — auteur ∩ lecteur (`me.app.call`)

Une app ouverte ne parle qu'à `POST /api/me/apps/{app_id}/call` (face REST seule, pas
d'outil MCP : un agent n'est jamais le lecteur d'une app). **Actions servies :**
`read_rows`, `read_aggregate` (lire), `write_cell`, `add_row`, `start_agent`,
`upload_file` (gestes, avec consentement), `list_files`, `agent_runs`, `run_result` —
et `batch`, un lot de lectures (plus bas).

- **Droits d'une app = droits de son auteur ∩ droits de son lecteur.** L'auteur est
  `updated_by` ; il est jugé par l'ownership SEUL (numéro du tableau), jamais par sa
  session. Le lecteur est jugé en exécutant POUR LUI les capacités existantes (lignes,
  agrégat). Auteur inconnu ou qui a perdu l'accès : 403 `app_author_unknown` /
  `app_author_lost_access` ; tableau sorti de l'org du projet depuis l'écriture :
  403 `table_outside_project_org`.
- **Borné au manifeste, côté serveur** : projection aux champs déclarés (plus `_id`,
  `_revision`, les dates), filtre / tri / regroupement / mesure sur ces champs seuls,
  recherche plein texte seulement sans liste de champs. `limit` et `offset` sont des
  entiers (sinon 400 `invalid_options`), `limit` ≤ 200.
- **Route lourde** (`api/routes_lourdes.py`) : 240/min par jeton, 8 en parallèle — une
  app laissée ouverte relit ses tableaux en fond. L'appel relit le manifeste sans le
  HTML (`db.apps.get_for_call`).
- **Une lecture = UN emprunt au pool** : `read_rows` et `read_aggregate` tiennent leur
  chemin dans `db.reuse_connection()` (17 emprunts mesurés avant, ~3 allers-retours
  chacun, pour une app qui ouvre son écran par huit à quinze lectures), une question déjà
  posée dans l'appel n'y repart pas (`lecture_d_appel` sur le propriétaire et la ligne du
  tableau), et la projection aux champs déclarés DESCEND au store
  (`page_rows(fields=…)`) au lieu d'être faite après coup. Le chemin ne fait que lire —
  condition de la portée, gardée par `tests/test_apps_appel_une_connexion.py`. Les
  gestes restent hors de la portée : ils écrivent.
- **Un lot de lectures** (`action: "batch"`) : l'écran d'une app s'ouvre sur huit à
  quinze lectures, et chacune prenait une des 8 places de la route — pour tout le
  processus, tous lecteurs confondus (503 `route_busy` au-delà) — et rejouait
  l'authentification, l'appartenance, l'app, son partage et le jugement de son tableau.
  Corps : `{action: "batch", draft?, reads: [{id, action, table, options}]}`, au plus
  `MAX_LECTURES_PAR_LOT` (10) lectures, `action` ∈ `read_rows` | `read_aggregate`.
  Réponse : `{results: [...]}`, une par lecture, dans l'ordre, sous son `id` :
  `{id, ok: true, rows, total}` ou `{id, ok: true, groups}` — ce qu'un appel seul aurait
  rendu —, ou `{id, ok: false, status, code, detail}` — le refus qu'un appel seul aurait
  rendu (`details` quand il en porte). Ce qui se juge UNE fois pour tout le lot : la
  forme du lot (400 `invalid_batch` : `reads` vide ou absent, une lecture sans `id` de 1 à
  64 caractères, deux `id` égaux, un champ d'appel à côté de `reads` ; 400
  `batch_too_large`), l'app, son partage et son brouillon (`_app_pour` : un refus là
  refuse tout le lot, comme il refuserait chaque appel). Chaque lecture est ensuite
  jugée par le MÊME code qu'un appel seul (`_lire`) — son tableau, son org, l'auteur, le
  lecteur, sans rien partager avec les autres lectures : un champ inconnu
  (`unknown_fields`), une forme invalide (`invalid_input`), un champ non déclaré, un
  auteur qui a perdu le tableau sont SA réponse, les autres lectures sont servies. Une
  lecture que la borne `statement_timeout` du pool coupe rend SA réponse
  `{ok: false, status: 503, code: "read_timeout"}` : l'emprunt est en autocommit, les
  suivantes restent servies. Un geste, ou toute action qui n'est
  pas une lecture (`list_files`, `file_url`, `agent_runs`, `run_result`), est refusé
  dans le lot (400 `batch_reads_only`) : un geste a son jeton et reste seul. Tout le
  lot tient UN emprunt au pool (`db.reuse_connection`), sur un chemin qui ne fait que
  lire. **Débit** : le lot prend une place de concurrence, mais chaque lecture coûte
  au débit par jeton ce que coûte un appel — la garde compte la requête, la capacité
  débite les lectures suivantes sur la même fenêtre (`routes_lourdes.debiter_en_plus`) ;
  si ce qui reste ne les couvre pas, le lot est refusé entier (429 `rate_limited`,
  `Retry-After`). Une erreur imprévue (ni refus nommé ni réponse) d'une lecture rend le
  lot entier en 500, comme elle rendrait son appel seul. ⚠️ Un lot tient sa place et sa
  connexion le temps de toutes ses lectures, l'une après l'autre : dix lectures lentes
  la tiennent dix fois plus longtemps qu'une. Gardé par
  `tests/test_apps_appel_lot.py`.
- **Journal** : l'appel est tracé comme toute requête REST (`tool_calls`, `kind=rest`),
  mais la route y est réduite (`/api/me/apps/:id/call`) : le numéro de l'app et le
  tableau lu n'y sont pas. Qui a lu quel tableau par quelle app se lit dans le **journal
  des lectures** (§ suivant).


**La clé métier ne se choisit pas d'un périmètre** (`apps_appel._cle_hors_perimetre`) :
unique sur TOUT le tableau, le refus d'une clé déjà portée (« existe déjà ») dirait à un
partenaire qu'une ligne hors de son périmètre la porte. Un lecteur au périmètre de lignes
ne peut donc ni ajouter une ligne qui porte la colonne clé, ni la réécrire (403
`row_scope_key`) — sauf si son périmètre IMPOSE cette colonne. Jugé à chaque geste sur le
schéma VIVANT (une clé déclarée après le manifeste compte), sans regarder les données :
clé libre ou prise, le même refus. Ajouter SANS la clé reste permis (la ligne naît sans
clé, non rapprochable) ; un partenaire ne peut plus enregistrer une ligne par une clé
qu'il tape. À l'écriture du manifeste, un rôle à `rows` qui ajoute ou pose la clé est
refusé. Et pour tout lecteur d'app, le 409 `business_key_exists` d'un ajout — qui nomme
la ligne en place (`id`, `details`) — devient 409 `row_conflict`, sans rien d'elle.

### Journal des lectures (`app_reads`)

Le gérant d'un tableau ouvert à un partenaire — par un partage ou À TRAVERS l'app
seulement (§ Accès par l'app) — doit pouvoir répondre à « qui a lu quoi ». Les gestes ont
leur journal (`app_consents`) ; les lectures ont celui-ci.

- **Des compteurs, pas une ligne par lecture.** Une app ouvre son écran par huit à quinze
  lectures et se rafraîchit ; la route en admet 240 par minute et par jeton. Une ligne
  par **(app, jour UTC, tableau, lecteur, accès)** : `row_reads` (lectures de lignes),
  `aggregate_reads` (agrégats), `rows_served` (lignes rendues), `first_at` / `last_at`.
  `access` dit par où le lecteur atteignait le tableau : `own` (son propre partage),
  `app` (l'accès par l'app seul, `Portee.servis`), `run` (un agent lancé depuis l'app,
  par cet accès).
- **Écrit APRÈS l'appel, hors de son emprunt.** Le chemin de lecture reste lecture seule
  et à UN emprunt au pool (`tests/test_apps_appel_une_connexion.py`) : chaque lecture
  SERVIE — seule, ou chacune d'un lot — est notée en mémoire pendant l'appel, puis le
  handler de la route (`apps_appel._call_journalise`) écrit le tout en UNE requête
  (`db.apps.note_reads`, `INSERT … ON CONFLICT DO UPDATE`, sommée par tableau et accès),
  sur un second emprunt. Rien n'est tamponné entre deux appels : un processus qui
  tombe ne perd que l'appel en cours. Une écriture du journal qui échoue est
  journalisée (Sentry) et la lecture est servie quand même. Une lecture refusée, ou
  coupée par la borne de durée, n'a rien servi : elle n'est pas notée.
- **Les runs** : un agent lancé depuis l'app qui lit un tableau par l'accès de l'app
  (`_droit_du_run`) est noté au point où le store SERT le tableau (`par_l_app.servi`),
  sous l'app et le lanceur, `access = 'run'` : `row_reads` y compte les **ouvertures**
  du tableau (une résolution par le store, pas une page), `rows_served` reste 0 — le run
  lit par les outils de tableau, qui ne remontent pas leur compte. Une ouverture faite
  dans une portée de connexion partagée (lecture groupée, transaction d'écriture) n'est
  pas notée, et le dit (journal du processus).
- **Ce qui n'est PAS noté** : les lectures d'un run par son propre partage du tableau
  (c'est le journal d'appels, `tool_calls`, qui les porte), les fichiers (`list_files`,
  `file_url`) et les runs (`agent_runs`, `run_result`) lus par l'app, la lecture que fait
  le jugement d'un geste, et QUELLES lignes ont été servies : un ensemble d'identifiants
  borné serait faux dès qu'il déborde, et réécrirait la ligne chaude à chaque lecture.
  La fusion de deux comptes qui ont lu le même tableau le même jour par la même app
  garde les compteurs du survivant pour ce jour-là.
- **Qui le lit** (`oto_apps op=reads`, `days` ≤ 180, 30 par défaut ; session
  interactive, comme tout `oto_apps`) : qui **gouverne l'app** (propriétaire, admin,
  Manage) lit toutes ses lignes (`tables` : celles-là) ; qui gouverne un **tableau**
  seulement lit les lignes de ce tableau — par `app_id`, sans même pouvoir lire l'app,
  ou sans `app_id`, `tables` = des numéros de tableaux qu'il gouverne, toutes apps
  confondues. Ne gouverner ni l'app ni aucun de ses tableaux : 404 `unknown_app` (403 à
  qui lit l'app sans la gérer). Au plus 2 000 lignes, les plus récentes d'abord.
- **Rétention : 180 jours**, purgés par `oto-mcp maintenance app-reads` (timer
  quotidien, `all`), jamais au démarrage. L'effacement d'une app ou d'un tableau emporte
  ses lignes (`ON DELETE CASCADE`) ; une app ne se supprime pas hors l'effacement de son
  org (on l'archive).

### Écrire, ajouter, lancer, déposer — l'accord du lecteur, donné hors de l'app (`me.app.consent`, #1386)

**Use d'abord** : un geste exige Use, Edit ou Manage (`peut_cliquer`), jugé à
l'émission du consentement, à l'appel et au dépôt — après le refus du brouillon, pour
ne rien dire de son existence. View reçoit 403 `app_view_only`.

**Le clic dans l'app VAUT accord, jugé par l'hôte** (décision du 31/10/2026, qui remplace
celle du 29/10). Le serveur ne distingue pas un clic du lecteur d'un appel que l'app fait
seule : c'est l'écran hôte qui en juge, sur l'activation navigateur. Un geste qui écrit ou
lance (`write_cell`, `add_row`, `start_agent`, `upload_file`) exige toujours un **jeton de
consentement**, que seul l'écran hôte obtient :

1. l'app DEMANDE le geste à l'écran hôte (`postMessage`) ;
2. l'hôte appelle `POST /api/me/apps/{app_id}/consent` avec le MÊME corps que l'appel ;
   le serveur juge le geste exactement comme l'appel le jugera (manifeste, auteur ∩
   lecteur, valeur déclarée, révision présente, plafonds…), puis rend `consent`
   (`secrets.token_urlsafe(32)`, stocké en SHA-256 dans `app_consents`), `expires_in`,
   `gestures` (toujours 1) et **`summary`**, construit par le serveur à partir des
   arguments jugés : `action`, `app_id`, `app_title`, `target` (tableau et ligne,
   agent, ou l'app), `content` (ce qui est ÉCRIT ou remis : `{set: {champ: valeur}}`,
   `{row: {colonne: valeur…}}`, `{input, files: [{id, name, media_type, size}]}`,
   `{file: {name, media_type, size}}`) et `text`, les mêmes faits en une phrase ;
3. sur un clic récent dans l'app, l'hôte utilise le jeton aussitôt ; sinon il AFFICHE ce
   résumé dans son propre chrome et attend un clic de confirmation. Puis il exécute
   l'appel (`POST …/call` avec `consent`, ou le dépôt `POST …/files`) ;
4. le serveur rejuge, BRÛLE le jeton en une écriture (`UPDATE … WHERE used_at IS NULL
   AND expires_at > now() RETURNING id` : deux appels concurrents ne le brûlent pas deux
   fois ; `consent_invalid` s'il est brûlé, expiré, d'un autre lecteur ou d'autres
   arguments ; `consent_required` sans jeton), puis agit.

**Un jeton = un geste** (`GESTES_PAR_ACCORD = 1`) : app, lecteur, action et empreinte
des arguments, calculée au serveur aux deux bouts. Valable **deux minutes, une fois** (15 minutes pour un dépôt, plus bas).

**L'émission n'est servie qu'à l'hôte** — 403 `consent_from_host_only` sinon :

- face REST seule (pas d'outil MCP), session interactive (JWT) : ni jeton `oto_` —
  porté, émetteur ou délégation d'agent hébergé —, ni secret de worker ;
- `Origin` présente et déclarée par l'instance (`OTO_MCP_CORS_ORIGINS`) : une requête
  sans origine, d'une origine tierce, ou d'origine **`null`** — celle d'une app dans
  son cadre isolé (`sandbox` sans `allow-same-origin`) — est refusée. Une app dont la
  CSP s'ouvrirait (`connect-src`) n'obtiendrait donc toujours pas de jeton elle-même ;
- pas une requête de cadre (`Sec-Fetch-Dest: iframe`, `frame`, `embed`, `object`).

### Contrat de l'écran hôte

L'écran hôte est le front qui embarque l'app (hors de l'iframe). Pour chaque geste
qu'une app demande :

1. **Recevoir** la demande par `postMessage` depuis l'iframe de CETTE app (vérifier
   `event.source`), sans rien exécuter.
2. **Demander le jeton** : `POST /api/me/apps/{app_id}/consent`, même corps que l'appel,
   depuis le chrome de l'hôte (son origine déclarée, sa session). Un refus (403, 400,
   413…) se montre au lecteur et se rend à l'app ; aucun jeton n'existe.
3. **Juger le clic** : le geste vaut accord si le navigateur montre une activation
   (`navigator.userActivation.isActive`), si le chrome de l'hôte n'a pas été touché
   depuis 5 secondes, et sous 20 gestes en 5 secondes. Sans activation (minuteur,
   ouverture de l'app), le geste est refusé.
4. **Sinon, demander** : afficher `summary` dans le chrome de l'hôte, jamais dans
   l'iframe, et attendre un clic de confirmation dans ce chrome. **Annuler** : n'utiliser
   aucun jeton (il expire en deux minutes), rendre un refus à l'app.
5. **Exécuter** l'appel (ou le dépôt) avec `consent`. Le jeton ne quitte pas l'hôte.
6. L'hôte n'affiche rien après un geste : l'app dessine son propre retour (`oto:done`,
   `oto.undo`, `oto:undone`).

**Un lien vers l'extérieur** (`open-link`, sans serveur ni jeton : rien n'est écrit ; seulement
sur un hôte qui le gère, et que l'instance déclare, cf. « Liens vers l'extérieur ») :
recevoir le message depuis l'iframe de CETTE app ; ne garder qu'une adresse `http(s)`
sans partie utilisateur, normalisée (`new URL(…).href`, 4 000 caractères au plus) ;
l'ignorer sans activation (`navigator.userActivation.isActive`) ou si une question est
déjà ouverte ; afficher l'adresse **entière** dans le chrome de l'hôte ; sur confirmation
seulement, l'ouvrir dans un nouvel onglet (`noopener,noreferrer`). Voir « Liens vers
l'extérieur » plus bas.

**Ce que le serveur garantit** : l'app seule, dans son cadre isolé, n'obtient pas de
jeton (origine `null`, pas de réseau) ; un agent ou un script porteur de clé non plus ;
un jeton ne sert qu'une fois, deux minutes, au geste résumé — mêmes app, lecteur,
action, arguments ; le résumé décrit les arguments que le serveur a jugés et que l'appel
exécutera ; chaque geste est journalisé (`app_consents`).

**Ce qu'il ne peut pas garantir** : que le lecteur a voulu ce geste. Un clic dans une app
déclenche ce que son manifeste permet, en son nom, sans résumé affiché, y compris une
entrée remise à un agent. Le manifeste, auteur ∩ lecteur et le journal restent les bornes.

- `write_cell` : un champ déclaré (`set`), à une valeur déclarée, avec la `_revision`
  montrée (sinon 400 `revision_required`) ; l'auteur doit pouvoir ÉCRIRE le tableau.
- `add_row` : colonnes déclarées (`add`) seules.
- `start_agent` : un agent déclaré `run: true`, dans l'org du PROJET de l'app, au nom
  du lecteur par `run_now` (accès en écriture du lecteur, abonnement, modèle, clé) ;
  l'auteur doit pouvoir écrire l'agent. L'entrée (`input: true`, ≤ 300 caractères)
  arrive au run comme donnée venue de l'app, après sa consigne. **Dédupliqué même
  avec une entrée** (409 `run_already_queued`), ce que `run_now` ne fait pas pour un
  `handoff`.
- **Journal** : la ligne de `app_consents` reste une fois brûlée — qui, quelle app,
  quelle action, quand, le `geste_id` des révisions écrites
  (`datastore_row_revisions.geste_id`), le travail lancé (`job_id`). C'est là, pas
  dans `tool_calls` (route réduite), que se lit « cette écriture vient de l'app N ».

### Des fichiers pour les agents (`files: true`)

Une équipe dépose un PDF, un tableur, un document Word dans une app, et un agent le traite.

- **Déposer est un geste** : l'écran hôte demande un consentement
  (`POST …/consent`, `action=upload_file`, `file={name, size, media_type}`), affiche le
  résumé (nom, type, taille) et, sur confirmation, envoie le fichier en multipart à
  **`POST /api/me/apps/{app_id}/files`** (`file` + `consent`, route de nature
  `api/apps_files.py`, session interactive seule). Ce qui arrive doit répondre à ce qui
  a été annoncé (même nom, même taille, même type déclaré). Le corps est **borné avant
  d'être lu** : un `Content-Length` au-delà de 20 Mo (plus l'enveloppe multipart) est
  refusé 413 `content_too_large` sans rien lire, et la lecture en flux s'arrête au même
  plafond (`uploads.lire_multipart_borne`).
- **Le type se juge sur les octets** (`media_store.type_servi`) : ce qu'un agent sait
  LIRE — PDF, Word, Excel, texte, CSV, Markdown, JSON ; ni images ni PowerPoint (pas
  d'OCR, pas d'extracteur : décidé le 28/10/2026). 20 Mo au plus par fichier, **1 Go par
  lecteur et par app** (413 `app_storage_full`) et **5 Go par lecteur toutes apps
  confondues** (413 `viewer_storage_full`). Les plafonds sont tenus à l'INSERTION :
  compte et insertion dans une transaction, sous un verrou consultatif propre au
  lecteur (`db.apps.file_add`) — deux dépôts concurrents ne passent pas tous deux ; un
  fichier refusé là voit son objet retiré du stockage. Un fichier qui n'est pas ce qu'il dit,
  ou que le stockage neutraliserait (HTML, SVG, exécutable…), est refusé 415 `file_type`.
- Le jeton d'un dépôt vaut **15 minutes** (deux pour les autres gestes) : il se brûle
  quand les octets sont arrivés, et 20 Mo sur un lien lent dépassent deux minutes.
- **Rangé à part** : `app_files` (app, déposant, clé, nom, type, taille), octets sous
  `app-files/<app_id>/…` (clé propre à chaque dépôt). **Seul celui qui l'a déposé le
  lit**, et les runs qu'il lance — ils tournent en son nom. `list_files` (appel d'app)
  rend les siens.
- **Remis à un run** : `start_agent` prend `files` (5 au plus, de CETTE app, déposés par
  CE lecteur). Le run reçoit, après l'entrée de l'app, chaque fichier — nom, type,
  taille — et son TEXTE quand on sait l'extraire (PDF, Word, tableur, texte :
  `file_extract`), borné pour que tout tienne dans le `handoff` (20 000 caractères).
  Une URL signée n'y figure pas : un run peut attendre une place au-delà de son heure.
- **Rendu par l'agent** : `oto_apps op=put_file` (`app_id`, `name`, `content` texte ou
  `content_base64`, `media_type`) range un fichier RÉSULTAT dans l'app pour le lecteur au
  nom duquel le run tourne (`origin='agent'`, mêmes types, taille et plafond, pas de
  consentement : personne devant l'écran, et lui seul le lira ; une écriture pour la
  garde de lecture seule). Un CSV ou TSV rendu est **neutralisé** : une cellule qui
  commence par `=`, `+`, `-`, `@` (ou une tabulation, un retour) est préfixée d'une
  apostrophe, sauf un nombre signé (`-12,5`, `+33 6…`) — l'agent écrit souvent ce qu'il a
  lu ailleurs, et le lecteur ouvre le fichier dans un tableur. L'app le liste
  (`list_files`, `origin`) et le fait télécharger : action `file_url` → lien signé de
  5 minutes, `Content-Disposition: attachment`. Hors de la porte bêta des écritures :
  elle borne qui écrit une app, pas ce qu'un de ses lecteurs y range.
- **Lu en entier par l'agent** : `oto_apps op=read_file` (`app_id`, `file_id`, une
  lecture pour la garde de lecture seule) rend le
  fichier comme les pièces jointes Gmail ou Drive (`file_content.render_for_agent`) :
  texte d'un PDF ou tableur en CSV inline, plus un lien signé vers l'original.
  ⚠️ `oto_apps` est un outil bêta : l'agent doit l'avoir dans ses `tools`, sinon il
  n'a que le texte remis (borné) — `missing_tools` le dit à la pose.
- **Rétention : 30 jours** (décidé le 28/10/2026, comme les pièces jointes du copilote,
  oto-backend#1420) — `oto-mcp maintenance app-files`, dans le timer quotidien : l'objet
  du stockage d'abord, puis la ligne, 5 000 au plus par passage. Un run déjà lancé a
  reçu le texte ; un `read_file` après la purge rend `unknown_file`. **Supprimer l'app
  ne laisse pas d'orphelin** : `app_files.app_id` est `ON DELETE SET NULL` (révisions
  `20261030_1300_app_files`, `20261030_1400_app_files_origin`) — la ligne reste, sans
  app, invisible, et la purge suivante retire son objet puis la ligne sans attendre les
  30 jours. Même famille que `copilot/pieces.py` : à converger.

### Ce que le cadre isolé laisse sortir

Le cadre de l'app n'appelle rien (`connect-src 'none'`, formulaires et navigations
fermés par le document-garde). Fermé en plus, avant toute ligne de l'auteur, parce
qu'aucune directive CSP ne le fait dans les trois moteurs (mesuré le 28/10/2026) :
WebRTC (constructeurs retirés — `webrtc 'block'` est ignoré, une requête STUN sortait),
le prefetch DNS, et `<link rel=preconnect>` (WebKit ouvrait une connexion). Restent,
par construction : les chemins de requête vers les CDN permis et Google Fonts (une
requête peut y porter des données, mais vers le CDN seul), et ce qu'un LECTEUR tape
dans un faux formulaire d'une app — seule donnée que l'auteur n'a pas déjà. Et une
adresse que le lecteur a vue en entier et choisi d'ouvrir (ci-dessous).

#### Liens vers l'extérieur (`oto.open`, 9/11/2026)

**Un choix d'instance.** Tous les hôtes ne gèrent pas `open-link` : sur un hôte qui ne
le gère pas, `oto.open` n'existe pas dans l'app. L'instance déclare donc que son hôte
le gère, par `OTO_APPS_LIENS_EXTERIEURS` (`1`, `true`, `yes`, `on` ; absente, vide ou
un non = non ; toute autre valeur refuse le démarrage). La description servie
d'`oto_apps` ne nomme `open(url)` que sur une instance qui le déclare
(`capabilities/apps.py`, `liens_exterieurs`) : sinon, un agent qui la suivrait écrirait
un appel qui lève. Ce n'est pas un repli : c'est ce que sait faire l'hôte de cette
instance.

Sur un hôte qui le gère, le cadre ne quitte toujours pas l'app : `window.open` et
`location.href = …` y restent sans effet (un `target="_blank"` sur un lien `https://`
est ignoré, le lien passe par la question ci-dessous). Un site hors de l'hôte s'ouvre
par l'hôte, après l'accord du lecteur :

- **Un lien `<a href="https://…">`** (de préférence) : le pont intercepte le clic, le
  remet à l'hôte, qui demande s'il faut ouvrir ce site extérieur en montrant l'adresse
  entière, puis l'ouvre dans un nouvel onglet (`noopener,noreferrer`) si le lecteur
  confirme. Un `target` sur le lien est ignoré. Un lien ni `http(s)` ni `#` (`mailto:`…)
  n'est pas pris : il fait quitter la page, le document-garde l'arrête et l'app se ferme
  — n'en pas mettre.
- **`oto.open(url)`**, depuis le gestionnaire d'un clic (un bouton) : la même question.
- **Règles** : `http(s)` seul ; une adresse avec une partie utilisateur
  (`https://site@autre.site/`) est refusée — le site y est `autre.site`, que le lecteur
  lirait mal ; seulement juste après un clic ou une touche du lecteur, jamais au
  chargement, sur un minuteur ni par un `a.click()` du script (`isTrusted`) ; une
  question à la fois. Une demande refusée ne rend rien à l'app.
- **Pourquoi l'accord** : une adresse peut porter des données. Le lecteur voit tout ce
  qui part ; une app garde ses adresses lisibles — pas de données tassées dans la
  requête (`?q=`) : un lecteur qui ne comprend pas une adresse ne l'ouvre pas.
- **Pages de l'app** : inchangé — `<a href="#/route">` ou `oto.go("/route")`.

### Suivre les runs

- `agent_runs` : les derniers runs d'un agent déclaré que l'auteur et le lecteur voient
  tous deux — statut, dates. L'erreur (`error`, le `last_error` du travail) et ce que
  l'agent a conclu (`outcome`, `note`) seulement pour les runs que le LECTEUR a lancés
  (`mine`) : l'erreur du run d'un autre peut citer ses données.
- `run_result` : un run que le lecteur a lancé, d'un agent déclaré ; sinon 404
  `unknown_run`.

## Révisions et conflits

`rev` est une ETag de contenu (titre, manifeste, HTML, vignette), comme celle des
pages. `update` et `patch` acceptent `expected_rev` et refusent en 409 `conflict` si l'app a changé.
Chaque mise à jour écrit d'abord l'état précédent dans `app_revisions` ; seules les
50 plus récentes révisions ORDINAIRES sont gardées (`db.apps.MAX_REVISIONS`, purge dans la
même transaction) — une version PUBLIÉE (`published_at`, voir plus bas) a sa propre borne,
`db.apps.MAX_PUBLISHED` (200), et la purge des ordinaires ne la touche jamais. **Puis un
budget en octets par app** (`db.apps.BUDGET_REVISIONS`, 100 Mo de HTML brut, versions
publiées COMPRISES) : au-delà, les plus anciennes tombent, de toute sorte, sauf les 10
ordinaires et les 20 publiées les plus récentes (`PLANCHER_ORDINAIRES`,
`PLANCHER_PUBLIEES`), gardées quel que soit leur poids. Compter ne bornait pas le poids
d'une app qui grandit (250 copies d'un million de caractères) ; une app à brouillon
n'écrit de révision qu'en publiant, d'où le plancher des publiées. La purge est
rétroactive : la première écriture d'une app au-delà du budget en retire l'excédent.
La taille se lit par `octet_length`, sans décompresser le HTML stocké. `restore`
remet une révision (en écrivant à son tour l'état courant), après avoir REJUGÉ son
manifeste : celui qui restaure en devient l'auteur, et l'état restauré est une version
publiée (un retour en arrière délibéré, avec sa `note`). `restore to_draft=true` la met
au brouillon à la place, pour l'essayer avant de la publier (app à brouillon seulement). `op=revision` rend une
version ENTIÈRE (manifeste, HTML), aux éditeurs seuls — un HTML d'hier peut porter ce
qu'on en a retiré — pour la lire ou la comparer avant de la restaurer. Une révision dit
quand son état a été REMPLACÉ (`created_at`), pas quand il a été écrit.

## Brouillon et publication

Décidé le 12/11/2026 : une app peut passer par un **brouillon**, comme un dépôt passe par
une branche avant d'arriver sur sa branche principale (`db/app_versions.py`,
`capabilities/apps_versions.py`).

- **`drafts_enabled`** (`op=set_drafts enabled=…`, écriture sur l'app) : faux par défaut —
  une app d'avant reste publiée à chaque écriture. Vrai : `update` et `patch` écrivent
  le manifeste et le HTML au BROUILLON (`apps.draft_*`, sur la ligne de l'app : publier
  ne verrouille qu'une ligne), et la réponse le dit (`target: "draft"`). Le titre et la
  vignette — la carte, que la liste montre — restent écrits en direct. Couper le
  brouillon avec un brouillon en cours est refusé (409 `draft_pending`).
  Écrire un brouillon dans une app qui n'en a pas est refusé (409 `drafts_off`), jugé
  sous le verrou de la ligne : un brouillon posé à côté d'écritures vivantes vieillirait
  sous elles, et sa publication les déferait sans un mot.
- **Le brouillon a son ETag** (`draft_rev`, préfixé `d`) : `op=get draft=true` le sert
  (manifeste, HTML, `rev`) ; `update`/`patch` s'écrivent contre lui. Une écriture qui
  nomme la version PUBLIÉE alors qu'un brouillon existe est refusée (409
  `draft_exists`) : l'agent a lu une version et écrirait sur une autre. Un `patch` sans
  `expected_rev` s'applique au brouillon (ou, sans lui, à la version publiée dont il
  naît) et s'écrit contre le `rev` de ce qu'il a lu.
- **Qui le voit** : ses éditeurs seuls. `op=get` ne nomme le brouillon (`draft` :
  `rev`, auteur, date) qu'à qui peut modifier l'app ; `draft=true` refuse un lecteur
  du même 403 `draft_editors_only` qu'il y ait un brouillon ou non. Aucune carte de liste
  ne le porte.
- **L'auteur du brouillon** (`draft_by`) est le dernier à l'avoir écrit, avec la règle
  d'une écriture de l'app : il doit atteindre ce que le manifeste du brouillon déclare
  (403 `would_break_app`), un manifeste écrit se juge contre lui.
- **L'aperçu** : `POST /api/me/apps/{id}/call` et `…/consent` avec `draft: true`, et le
  dépôt de fichier avec le champ `draft`, ouvrent le brouillon — jugé comme une app à
  part entière : SON manifeste, SON auteur ∩ le lecteur (éditeur). Un jeton émis pour
  l'aperçu ne se brûle pas sur l'app publiée (`draft` entre dans l'empreinte, absent
  quand il est faux : les jetons d'avant restent valides). L'aperçu n'est pas une
  préproduction : il lit et écrit les VRAIS tableaux et lance les vrais agents.
- **`op=publish`** (`expected_rev` = le `rev` du brouillon vu, obligatoire ; `note`
  facultative, 500 caractères) : le manifeste du brouillon est REJUGÉ contre qui publie,
  qui devient l'auteur de l'app ; l'état remplacé part dans `app_revisions`, l'état
  publié porte `published_at` et `version_note`, le brouillon est vidé — une
  transaction, sous le verrou de la ligne. Un brouillon changé depuis l'aperçu est
  refusé (409 `draft_conflict`). `op=delete_draft` le jette.
- **L'agent intégré** demande l'accord de la personne avant `publish` et
  `delete_draft` (`copilot/classement._OP_DESTRUCTRICE`) ; `tools/effets.py` les classe
  `detruire`, `revision` `lire`.
- Une écriture DIRECTE du manifeste ou du HTML (app sans brouillon) fait perdre à l'état
  vivant sa marque de version publiée ; changer la carte la garde.

⚠️ **Base partagée** : tant que la prod tourne un code d'avant ce lot, elle écrit l'état
vivant en direct et purge les révisions sans voir les versions publiées — n'activer le
brouillon d'une app qu'une fois le tag en prod.

## Liste

`op=list` prend les `project_ids` dont lister les apps (au plus 200 — ceux que
l'écran tient déjà), juge l'accès à tous en une passe (`ownership.can_access_many`)
et rend des **cartes** : jamais le HTML, qui pèse des dizaines de Ko par app.
