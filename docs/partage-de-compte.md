# Partager un compte connecteur

Un compte connecté (une boîte mail, un compte Google, une clé d'API, un compte de
messagerie hébergée) s'ouvre à une personne, à une équipe ou à toute l'org par une
**règle** posée sur le compte. Ce document dit le modèle en vigueur depuis la bascule
(oto-backend#1499, étape 3, lot L3), ses écarts avec oto-core et les décisions de #1499.
Il se lit avec `connector-vault.md` (le coffre) et `roles-and-resolution.md` (la cascade).

## Le modèle

C'est celui d'oto-core (`access_rules` sur un compte, `droits-d-acces.md` et
`connecteurs-et-comptes.md` d'oto-core), transposé.

- **Le compte** est une instance (`connector_instances`) : une personne (`member`
  `<org>:<sub>`), une équipe (`group`) ou l'org (`org`) le possède. Un compte de messagerie
  hébergée (`unipile_accounts`, sans ligne de coffre) a une **sous-instance** : `unipile`,
  propriétaire `member` `<org>:<sub>`, compte `canal:<provider>` (`db/sous_instances.py`) ;
  c'est elle qui porte les règles du compte de canal.
- **La règle** (`connector_account_rules`) : une par (compte, sujet) ; le sujet est une
  personne, une équipe ou l'org, exactement un ; un niveau `none` < `read` < `write` <
  `sensitive` < `manage`. Une règle qui nomme le propriétaire d'un compte personnel est
  refusée (`owner_subject`, par la surface puis par la base). `services` (NULL = tout) :
  pour un compte Google ou Microsoft seulement, les services que la règle ouvre. `origine` :
  `reprise` (posée par la bascule) ou `manuelle` (un geste).
- **Le journal** (`connector_account_share_events`) : chaque règle posée, changée ou
  retirée, écrit par un déclencheur dans la transaction du geste ; l'auteur est
  `oto.acteur`. L'ancien journal des prêts (`connector_loan_events`) reste en lecture.

## Le niveau qu'un appelant détient (`access/niveaux.niveau`)

Dans l'ordre :

1. un compte d'une autre org que celle de l'appel (**D1**) : sa propre clé posée là (#172)
   → `manage` ; sinon la seule règle qui NOMME l'appelant ; ni règle d'équipe ou d'org,
   ni défaut, ni rôle ne traversent les orgs ;
2. son propre compte personnel : `manage` ;
3. le compte personnel d'autrui : `none` si son propriétaire a quitté l'org ou est en
   pause ; sinon la règle qui nomme l'appelant ;
4. sinon la règle de son équipe ACTIVE (ou, pour un compte d'équipe, de l'équipe
   propriétaire dont il est membre) ;
5. sinon le défaut — compte d'équipe : `sensitive` pour ses membres, `none` pour les
   autres (**D2**, tranchée) ; compte d'org : `sensitive` pour ses membres (écart **(a)**,
   oto-core dit `read`) — REMPLACÉ par la règle d'org s'il y en a une, qui peut le
   restreindre (`none` compris) ;
6. enfin, s'il manque encore : l'admin de l'org gère tout compte d'org ou d'équipe
   (`manage`), le responsable d'une équipe gère son compte, sauf règle nominative.

Une règle nominative ou d'équipe l'emporte sur la règle d'org, dans les deux sens ; une
règle `none` ferme le compte. Une règle à `services` ne vaut que pour ces services
(`gmail`, `drive`, `google_ads`, `outlook`…) ; l'appel au compte lui-même (`google`,
`microsoft`) n'est pas un service.

## Quel compte sert un appel (`access/comptes.ChoixDuParcours`)

L'ordre d'oto-core : le compte que l'appel **nomme** (`_account=` / `_instance=`), puis le
sien, puis celui de son équipe ACTIVE, puis celui de l'org ; ne restent à chaque étape que
les comptes actifs dont le niveau couvre ce que l'appel exige. Un compte sert ; deux au même
étage sont refusés `ambiguous_account` (**D3**), sauf si un défaut tranche — le refus
nomme les candidats par leur libellé et dit comment en nommer un. Puis l'**étape 5** (plus
bas), puis les clés plateforme et le palier du tenant, inchangés.

- **Une clé personnelle suspendue est sautée** : l'étape suivante sert (équipe, org,
  étape 5, plateforme), comme avant la bascule, et la réponse le dit (`served_by`,
  `suspended_key_skipped`). Nommée, elle est refusée `account_suspended` : on ne sert pas
  un autre compte que celui qu'on nomme.
- Le compte d'autrui sert **nommé**. Un nom se cherche d'abord dans l'org de
  l'appel (ses étapes, puis les comptes des autres personnes), puis parmi les comptes
  d'autres orgs que l'appelant peut nommer (les siens, #172 ; ceux qu'une règle lui
  ouvre, D1) ; deux au même endroit : `ambiguous_account`. Un nom qu'aucun compte ne
  porte lève `CompteIntrouvable`. `_instance=inst:<id>` nomme aussi un compte.
- **L'étape 5** : quand rien de local ne sert aux étapes 2 à 4 (nommé, personnel, équipe
  active, org) et rien n'est nommé, un compte prêté sert SANS NOM, au niveau que l'appel
  exige (sous ce niveau, il ne sert pas), avant la clé du tenant et celle de la
  plateforme, jamais devant un compte local. L'ordre : d'abord un compte de l'org de
  l'appel qu'une règle lui ouvre (celui d'une autre personne, ou d'une équipe dont la
  règle la nomme ; une seule sorte de règle, celles de la reprise comprises), puis sa
  propre clé posée dans une autre org (#172), puis un compte qu'une autre org lui prête
  (D1). Dans chaque groupe, le plus récent : pour un prêt, la date de la règle ; pour sa
  clé, `set_at`. Les comptes de messagerie hébergée (Unipile) suivent le même ordre quand
  la personne n'a pas de compte à elle sur le canal (`comptes_heberges.prete_sans_nom`) :
  les anciens pointeurs, repris en règles, retrouvent ainsi leur usage. La réponse dit
  qui prête (`lent_by` : l'e-mail de la personne, ou `team <nom>`), `lent_by_org` et
  `level`, ou `your_account_in_org` ; le statut affiché porte `mode: "elsewhere"` et le
  même champ ; `oto_context.accounts.to_name[].serves_unnamed` désigne le compte qui
  sert. Une politique « clé perso interdite » (#1239) écarte à l'étape 5 les comptes de
  personnes.
- Un compte d'une autre org qui ne sert pas est **listé** par le refus (aucun compte,
  `ambiguous_account`, `insufficient_level`) parmi les candidats à nommer (`to_name` : libellé,
  `inst:<id>`, « lent by <org> » et le niveau, ou « your own account in <org> : name it,
  or set the key in this org »). L'appelant ne voit rien d'autre de l'org prêteuse : ni
  ses autres comptes, ni ses membres (le refus de niveau dit « whoever manages it in
  <org> »). La réponse de l'appel (`lent_by_org`, `level`, ou `your_account_in_org`) et
  `oto_context` (`accounts.to_name`) le disent.
- Sous un jeton de délégation verrouillé sur l'org de son travail (`verrou_org`), un
  travail nomme encore un compte qu'une règle ouvre à son porteur : il agit dans son
  org, avec ce compte (le pin `_instance=` garde l'org de l'appel). La présence du
  propriétaire se lit sur son appartenance réelle (`org_store.get_org_role`).
- **La facturation** d'un appel servi par un compte d'une autre org revient à l'org de
  l'appel ; la clé est celle de l'org prêteuse, qui n'est débitée de rien.
- Des comptes vus mais tous sous le niveau exigé : refus `insufficient_level`, jamais la
  clé plateforme à leur place.
- Ce qui a disparu : le pointeur Unipile vers le compte d'autrui (la règle reprise le
  remplace, à l'étape 5, après le compte de la personne), la clé personnelle d'une autre
  org servie AVANT l'équipe et l'org (#172 : elle sert désormais à l'étape 5, après
  elles),
  l'équipe héritée par un projet partagé (#480), les prêts entre orgs hors des règles.

## La garde de niveau

Ce qu'un appel exige vient de `tools/effets.py` : lire → `read`, écrire → `write`, envoyer
ou détruire → `sensitive`. Les deux bords d'un appel (`middleware/call_context.py`,
`tools/meta.executer_cible` pour `oto_call` et les recettes) notent l'outil et ses
arguments ; le résolveur juge **le compte réellement résolu**. Une op sans classe déclarée
lève (`effect_undeclared`). Le refus est un 403 déclaré `insufficient_level` qui nomme le
compte, le niveau détenu, le niveau exigé et qui gère le compte (`managed_by`).

Le journal d'appels (`tool_calls`) porte l'identifiant de l'instance servie
(`instance_id`), à côté de `ref` et `key_mode`.

## Poser et retirer une règle

`oto_instance` (MCP) et la capacité REST correspondante (`docs/rest-api.md`) : poser une
règle (`level`, sujet personne/équipe/org, `services`), la retirer, lire les règles et le
journal d'un compte. Réservé à qui **gère** le compte (son propriétaire, le responsable de
l'équipe propriétaire, l'admin de l'org). Seul l'admin de l'org pose `manage` ; un
responsable d'équipe ne pose que `none`, `read` ou `write`. Le geste porte
`origine='manuelle'` et son auteur (`oto.acteur`).

Retirés, sans alias : `oto_instance op=lend` et `POST /api/me/connectors/{connector}/lend`,
`oto_account_access` et les routes `/api/me/connector-accounts…/grants`,
`oto_org op=list_loans` et `GET /api/orgs/{id}/loans`, les marques `shared`/`granted`
d'`oto_identity` (remplacées par `level`), la part prêt d'`oto_admin_unipile_seat`.

## Changer le propriétaire d'un compte

La règle d'oto-core : `oto_instance op=transfer` et sa face REST font passer un compte de
l'org à une personne membre ou à une équipe de l'org, et l'inverse. Il faut gérer le compte
avant ET après ; le rendre personnel est réservé à l'admin de l'org. Le compte ne change
jamais d'org.

La base (`db/transfert_de_compte.transferer`), en une transaction : la ligne de coffre change
d'entité en place (le secret n'est ni lu ni copié), l'instance garde son identifiant, ses
règles et son journal ; sur un compte devenu personnel tombent la règle d'org et celle qui
nommerait le nouveau propriétaire ; le défaut porté tombe si le nouveau palier a le sien ;
le journal des partages note `transferred` (le nouveau propriétaire en sujet, l'ancien en
`owner_before`). Refus nommés : compte inconnu, sous-instance de canal (la connexion d'une
personne ne se donne pas), même propriétaire, nom de compte déjà pris chez le nouveau.

## La bascule (révision `20261105_1000_bascule_comptes`)

Dans une transaction : la reprise est **rejouée** (figée dans la révision), puis les
anciennes tables (`connector_account_grants`, `connector_account_group_grants`,
`unipile_operated_accounts`), les colonnes `share_side` et `share_services` de
`connector_credentials` et `connector_instances.visibility` sont retirées. `share_mode` et
`share_down` restent : ils portent l'accès aux clés plateforme. Le retour arrière recrée
les structures vides et laisse les règles.

La reprise : prêts Unipile (personne, équipe) et `share_side` → règles `sensitive`, avec
la liste de `share_services` pour un compte Google ou Microsoft ; une cible `org:<id>` →
règle d'org ; tout compte d'org → règle d'org `sensitive` (écart a). Un prêt à une
PERSONNE hors de l'org du compte (Unipile ou coffre) devient une règle hors org (D1) ; un
prêt à une équipe ou une org d'une autre org n'est pas repris. Une règle `manuelle` n'est
jamais touchée.

## Écarts assumés avec oto-core

- **(a)** le défaut d'un membre sur un compte d'org est `sensitive` (oto-core : `read`),
  tranché le 05/11/2026 : un compte d'org sert ses membres comme avant les règles, celui
  posé après la bascule aussi ; une règle d'org ou de personne le restreint, `none`
  compris (`DEFAUT_MEMBRE_ORG`, `access/niveaux.py`). La reprise a aussi posé une règle
  d'org `sensitive` sur chaque compte d'org d'avant : redondante, gardée visible ;
- **(b)** `services` sur une règle Google ou Microsoft, propre au backend ;
- **(e)** une règle d'équipe vaut pour l'équipe ACTIVE seule (oto-core lit toutes les
  équipes de la personne) ;
- **D1** le partage entre orgs : oto-core ne connaît qu'une org par compte ; ici une règle
  peut nommer une personne hors de l'org du compte, servie nommée, ou sans nom à
  l'étape 5 quand rien de local ne sert ;
- **D3** le défaut encadré : oto-core refuse toute ambiguïté ; ici un défaut posé par qui
  gère le compte départage les comptes d'une même étape.

## Les décisions de #1499

Toutes tranchées par le propriétaire le 04/11/2026, chacune isolée dans une fonction
nommée de `oto_mcp/access/niveaux.py` ; la changer = changer cette seule fonction et ses
tests (`tests/access/test_resolution_par_regles_1499.py`).

- **D1 — accès entre orgs** (`niveau_entre_orgs`) : **gardé, dans les règles**. Une règle
  peut nommer une PERSONNE hors de l'org du compte (jamais une équipe ni une org
  extérieures), posée par qui gère le compte, aux mêmes conditions qu'une règle locale ;
  la surface désigne la personne par l'e-mail d'un compte oto existant et refuse
  neutrement, sans dire si le compte existe. Le journal porte `hors_org`. Le compte sert
  au niveau de la règle : nommé, ou SANS NOM à l'**étape 5** — quand aucun candidat
  local n'existe aux étapes 2 à 4 (nommé, personnel, équipe, org), avant la clé du
  tenant et celle de la plateforme, jamais devant un compte local, et après un compte de
  l'org de l'appel qu'une règle lui prête ; de plusieurs candidats, le plus récent
  (l'ordre complet : § Quel compte sert un appel). La réponse le dit (`lent_by_org`, ou
  `your_account_in_org` pour sa propre clé), `oto_context` aussi ; l'org de l'appel paie.
  La clé personnelle posée dans une autre org (#172) suit la même règle, pour tout
  connecteur. Une politique « clé perso interdite » (#1239) de l'org de l'appel écarte à
  l'étape 5 les comptes de personnes.
- **D2 — reprise de l'usage** : la révision de bascule pose une règle `sensitive`
  (origine `reprise`) par couple personne × compte d'équipe que la personne, non
  membre, a appelé dans les 30 derniers jours (`tool_calls`), sauf si elle est sortie
  de l'org du compte.
- **D2 — compte d'équipe pour un non-membre** (`niveau_equipe_non_membre`) : **on
  coupe** (`none`) ; une règle nominative peut l'ouvrir.
- **D3 — plusieurs comptes au même étage** (`defaut_qui_tranche`) : **le défaut encadré**.
  `ambiguous_account`, sauf un défaut unique qui départage les comptes d'une même étape
  (org, ou équipe active), posé par qui gère le compte, et seulement si l'appelant a sur
  lui le niveau que l'op exige ; il ne passe jamais devant un compte nommé ni devant le
  compte personnel. Chaque pose du défaut (`meta.is_default` de la ligne de coffre)
  enregistre son auteur et sa date (`meta.default_set_by`, `meta.default_set_at`), par
  tous les chemins ; `oto_context` (`accounts.defaults`) les montre. Un défaut d'avant,
  sans auteur, compte comme posé par un gestionnaire : aux étapes org et équipe, seuls
  l'admin de l'org et le responsable de l'équipe ont jamais pu le poser
  (`capabilities/connectors/identities._require_scope`, `auth/google.scope_target`, la
  connexion Google à ces portées), et au palier personnel seule la personne.
