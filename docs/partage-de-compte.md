# Partager un compte connecteur

Un seul geste, « partager », pour ouvrir un compte connecté (une boîte mail, un compte
Google, une clé d'API…) à une personne, à une équipe ou à toute l'org. Ce document dit le
modèle cible, la règle de service et ce que deviennent les mécanismes d'avant. Il se lit
avec `connector-vault.md` (le coffre, « Qui VOIT une instance ») et
`roles-and-resolution.md` (la cascade).

## Pourquoi

Six chemins permettaient d'ouvrir un compte à quelqu'un d'autre, nés un par un :

| Chemin | Couvre | Écran |
|---|---|---|
| Compte posé au palier org ou équipe à la connexion OAuth | Google, Salesforce, WordPress ; Microsoft depuis le lot 5 | oui (« Autoriser » côté org/équipe ; Microsoft : backend seul, voir lot 5) |
| Clé d'org ou d'équipe | connecteurs `org_shareable` | oui |
| Prêt d'instance (`share_side`, ADR 0044) | tous, Microsoft depuis le 09/10 | **non**, agent seulement |
| Délégation « opérer mon compte » (#55) | messageries Unipile | oui, personnes seulement |
| Héritage de clés par partage de projet (#480) | les paliers du partageur | non |
| Accès à une clé plateforme (`share_down`, grants) | clés d'oto | super_admin |

Les deux derniers ne partagent pas un compte d'une personne (un projet, une clé d'oto) :
**hors périmètre**. Les quatre premiers se recouvrent (ouvrir à une équipe : trois façons ;
à une personne : deux), n'ont pas tous d'écran, et ne suivent pas la même règle quand le
compte d'un autre sert un appel.

## Le modèle : deux concepts, pas plus

**1. Le compte de l'org ou de l'équipe.** Posé par l'admin (org) ou le chef d'équipe,
il appartient à l'org ou à l'équipe : il survit au départ de qui l'a branché, il sert
ses membres **sans désignation**, comme aujourd'hui (palier équipe puis org de la
cascade). Clés d'API et comptes OAuth connectés « au nom de » l'org ou de l'équipe.

**2. Le compte partagé par une personne** — le *prêt*. Il reste à son propriétaire :
lui seul le partage et le reprend, il s'éteint avec lui. Un seul stockage, `share_side`
sur la ligne de coffre du propriétaire (`user:<sub>`, `group:<id>`, `org:<id>`), et un
seul geste :

- MCP : `oto_instance op=lend` (`to` = personne, `to_group` = équipe, `to_org=true` = l'org
  courante, exactement un des trois ; `account` ; `revoke`) ;
- REST : `POST /api/me/connectors/{connector}/lend`, même contrat ; la réponse dit l'état
  complet après le geste (`lent_to`, `lent_to_groups`, `lent_to_org`) ;
- dashboard : « Partager » sur la carte du compte, avec la liste de qui y a accès
  (`visible_to` de `GET /api/me/connector-instances`) et le retrait.

Une carte service (`outlook`, `gmail`…) prête le compte de son porteur
(`credential_of`), mais **pas tous ses services** (décision du 16/10/2026, signal 1399) :
voir « Les services prêtés » ci-dessous.

## Les services prêtés (décision du 16/10/2026)

Un compte Google ou Microsoft autorise ses services un par un (Gmail, Drive, Google Ads,
Outlook…), sur la même ligne de coffre. Le prêter prêtait la ligne, donc **tous** ses
services, et chaque service que le prêteur autorisait ensuite devenait accessible à
l'emprunteur, sans nouvel avertissement. On ne pouvait pas prêter Google Ads seul.

**Le prêt d'un compte Google ou Microsoft retient la liste des services prêtés**, par
emprunteur (`connector_credentials.share_services`, `{cible: [services]}`,
`access/prets.PORTEURS_A_SERVICES`) :

- à la pose, `services` donné, sinon la carte nommée (`connector='google_ads'`), sinon les
  services que le compte a autorisés **ce jour-là** — chacun doit être un service du compte
  qu'il a autorisé (`connectors/services_accordes`) ; la réponse dit la liste (`services`) ;
- **un service autorisé plus tard n'est jamais prêté implicitement** ; le prêteur étend ou
  réduit en prêtant à nouveau au même emprunteur avec `services` (la liste remplace la
  précédente, et le geste se journalise comme une pose) ;
- à l'usage, au résolveur unique (`access/resolve.resolve_credential` →
  `prets.exiger_service_prete`), un service hors de la liste est refusé
  `service_not_lent`, en nommant le prêteur, les services couverts et le geste qui étend ;
  les services prêtés à une personne sont l'union des listes de toutes les cibles qui
  l'atteignent (elle, ses équipes, son org) ; **une cible sans liste n'a aucun service
  prêté**, jamais « tous » ;
- un appel au compte lui-même (`google`, `microsoft` : l'identité seule) n'est pas un
  service et n'est pas filtré ; une clé sans services propres (Unipile, une clé d'API) se
  prête entière, et la réponse le dit.

Les prêts posés avant cette règle ont été **figés** par la révision
`0060_services_pretes` : chaque cible a reçu les services que le compte avait autorisés au
moment de la révision. Rien n'a cassé, rien ne s'élargit ensuite.

## La règle de service (décision du 15/10/2026)

**Un compte prêté par une autre personne ne sert que désigné explicitement**
(`_account=`, ou `_instance=`), quel que soit le connecteur et quelle que soit la cible du
prêt (personne, équipe, org). Sans désignation, l'appel est refusé `account_required`, et
le refus **nomme** les comptes prêtés et leur propriétaire : l'agent choisit, puis rappelle.
La réponse d'un appel servi par un prêt dit le compte et `lent_by`.

*Pourquoi* : le 14/10, une personne sans compte propre a écrit dans la boîte de la
collègue qui la lui avait prêtée, sans le savoir. Désigner coûte un aller-retour la première
fois ; écrire au nom de quelqu'un par surprise ne se rattrape pas.

Les comptes du concept 1 (ceux de l'org ou de l'équipe elles-mêmes) restent servis sans
désignation. La règle s'écrit **une fois**, sous `oto_mcp/access/` (`prets.py`), et chaque
chemin de résolution s'y branche : Microsoft (`auth/microsoft._lent_for_call`, nourri par
`prets.prets_de` — personne, équipe, org), puis la cascade — paliers équipe et org, nourris
par `group_store.lent_instances` et `live_loans`, décidés dans leur sonde
(`PretsDuParcours.au_palier_equipe`, `au_palier_org`). Un seul juge par appel : les paliers
équipe et org de la cascade, qui servent aussi Microsoft depuis le lot 5, n'y lisent que les
comptes de l'équipe et de l'org elles-mêmes et laissent ses prêts à son résolveur
(`prets.RESOLVEUR_PROPRE`). Dans la cascade, sans désignation,
le compte de l'équipe elle-même sert, sinon celui de l'org ; si seuls des prêts auraient
servi, le refus les nomme (avec « via the team … » ou « to the whole org … »), jamais la
clé plateforme à leur place. Un prêt à l'org ne sert que les membres de cette org, la ligne
du prêteur exceptée ; l'écho dit `lent_by` et `lent_via_org`.

*Ce qui change pour qui s'en servait* : un membre d'équipe qui utilisait un prêt à son
équipe sans le nommer reçoit `account_required` à son prochain appel, et le rejoue avec
`_account=` ; une équipe qui a son propre compte ET un prêt sert désormais le sien, au lieu
d'un refus d'ambiguïté.

## Invariants

- **On ne prête que sa propre ligne de coffre**, dans l'org courante (`SUB_ONLY`). Un
  prêt à une équipe ou à l'org reste dans l'org du prêteur ; un prêt à une personne
  peut la traverser.
- **Une cible qu'un connecteur ne sait pas servir est refusée à la pose**, nommément —
  jamais un `ok` inerte.
- **La garde des niveaux d'accès juge le propriétaire de la clé servie**
  (`connectors/access_level_gate.py`, `_judged_contexts`) : tout chemin qui sert la clé
  d'un autre propriétaire y est ajouté, sinon une équipe réglée en lecture se fait écrire
  dessus. Un prêt ne servant que désigné, la désignation (`_instance=`, `_account=`) suffit
  à juger, avant la résolution, l'org du prêteur et l'équipe emprunteuse (`_loan_contexts`).
- **Une politique d'org qui interdit la clé perso** pour un connecteur interdit aussi
  d'en prêter une dans cette org : refus `personal_key_forbidden` à la pose ; un prêt posé
  avant la politique cesse de servir (prêt à une équipe : `lent_instances` l'ignore ; prêt
  épinglé : refus de l'instance perso désignée).
- **Le prêt suit son propriétaire** : en pause, il est suspendu (refus nommé
  `lender_suspended`) ; quand le propriétaire quitte l'org, ses prêts nés dans cette org
  cessent, à une personne comme à une équipe (refus nommé `lender_left_org` à l'épinglage,
  prêt à une équipe ignoré) — lu à chaque appel, rien n'est détaché du coffre.
- **La pose et le retrait se journalisent** : qui, quoi, vers qui, quand
  (`connector_loan_events`, révision `0058_journal_des_prets`), dans la transaction qui
  écrit le `share_side` ; seul un geste qui change l'état y entre, jamais un secret.
  L'admin d'une org lit les prêts actifs de son org (`oto_org op=list_loans`,
  `GET /api/orgs/{id}/loans`) : prêteur, compte, emprunteur, services, date de la dernière
  pose journalisée (`since: null` pour un prêt antérieur au journal). La lecture par le
  propriétaire reste à exposer (lot 4, avec l'écran).

## Les règles de comptes (oto-backend#1499, étape 3)

Le modèle cible est celui d'oto-core (`access_rules` sur un compte, `droits-d-acces.md` et
`connecteurs-et-comptes.md` d'oto-core), transposé. **Lot L1 : rien ne lit encore les
règles.** Les prêts ci-dessus servent toujours ; la bascule est le lot L3.

- **Le compte** est une instance (`connector_instances`). Un compte de messagerie hébergée
  (`unipile_accounts`, sans ligne de coffre) reçoit une **sous-instance** : `unipile`,
  propriétaire `member` `<org>:<sub>`, compte `canal:<provider>`, enfant de l'instance de
  la clé Unipile qui le porte quand elle est unique à son palier (siège plateforme : la clé
  plateforme ; sinon la clé de la personne dans l'org, puis celle de l'org), sans parent
  sinon (`db/sous_instances.py`). Elle naît à la connexion du compte
  (`db.set_unipile_account`) et à la reprise ; elle suit la personne à la fusion de deux
  comptes ; la maintenance des orphelines l'écarte.
- **La règle** (`connector_account_rules`) : une par (compte, sujet) ; le sujet est une
  personne, une équipe ou l'org, exactement un ; un niveau `none` < `read` < `write` <
  `sensitive` < `manage`. Une règle qui nomme le propriétaire d'un compte personnel est
  refusée par la base (`connector_account_rules_owner_subject`). `services` (NULL = tout) :
  la liste des services prêtés d'un compte Google ou Microsoft. `origine` : `reprise`
  (recalculée) ou `manuelle` (un geste ; la reprise n'y touche jamais).
- **Le journal** (`connector_account_share_events`) : chaque règle posée, dont le niveau
  ou les services changent, ou retirée, écrit par un déclencheur dans la transaction du
  geste ; l'auteur est `oto.acteur`, sinon NULL (geste hors serveur, la reprise).
- **La reprise** (`db.regles_de_comptes.reprendre_les_prets`, jouée par la révision
  `20261105_0900_regles_de_comptes`, rejouée par celle de L3 avant le retrait des
  anciennes tables) RECALCULE les règles reprises : prêts Unipile (personne, équipe) et
  `share_side` → règles `sensitive`, avec la liste de `share_services` pour un compte
  Google ou Microsoft ; une cible `org:<id>` → règle d'org. Idempotente ; un prêt retiré
  depuis retire sa règle reprise. La fusion de deux comptes repointe sujet et auteur ;
  la suppression d'une org, d'une équipe ou d'une personne emporte ses règles (clés
  étrangères en cascade).

Écarts assumés avec oto-core :

- **(a)** un compte d'org sert aujourd'hui à tout membre : la reprise lui pose une règle
  d'org `sensitive` explicite (le défaut d'oto-core serait la lecture) ;
- **(b)** `services` : un prêt Google ou Microsoft ne prête qu'une liste de services,
  propre au backend ;
- **(d)** un prêt à une personne hors de l'org du compte (prêt entre orgs), ou à une équipe
  ou une org d'une autre org, n'est **pas repris** : la reprise le rend dans son bilan
  (`non_reprises`) et le journalise ; à trancher avant L3 ;
- **(e)** une règle d'équipe vaudra pour l'équipe ACTIVE seule, comme la cascade
  d'aujourd'hui (oto-core lit toutes les équipes de la personne).

**Mesurer la bascule avant L3 (lot L2)** : `lanceur --script
scripts/mesure_bascule_comptes_1499.py [--jours 30]` (`docs/commands.md` § Un script
d'entretien par le lanceur), en transaction en lecture seule, avant comme après L1 (les
règles voulues sont calculées en mémoire par `regles_de_comptes.calculer_les_regles`).
Il compte, par connecteur et sans aucun identifiant : prêts entre orgs (coffre, Unipile),
clés personnelles d'une autre org (#172), nouveaux cas `ambiguous_account`, clés
personnelles suspendues qui ont un repli, pointeurs Unipile vers autrui, équipes héritables
par un projet partagé (#480), et, au journal d'appels, les paliers servis et les comptes
appelés par quelqu'un qui n'y aurait plus accès ou plus au niveau exercé ; ce qui dépend de
l'appel (équipe active, projet, politique de clé perso) est borné et nommé.

## Ce que deviennent les chemins d'avant

| Chemin | Devient |
|---|---|
| Compte posé au palier org/équipe à la connexion | Concept 1, inchangé ; **ouvert à Microsoft** (lot 5, livré) |
| Clé d'org ou d'équipe | Concept 1, inchangé |
| Prêt d'instance | Le geste unique (concept 2), étendu à l'org, avec écran |
| Délégation des messageries | Même geste à l'écran et dans l'agent ; stockage propre conservé, car le compte opéré n'est pas une ligne de coffre (le bénéficiaire l'atteint avec sa propre clé Unipile) |

## Lots

1. Ce document.
2. Règle de service partagée et prêts Microsoft à une équipe — lot de la session Microsoft.
3. Backend : cible `to_org`, règle de service étendue à la cascade, fin du prêt au départ
   de l'org, politique « clé perso interdite » lue par les prêts, journal. *3a* : la
   cascade (palier équipe), le départ, la politique, la garde des niveaux d'accès. *3b* :
   `to_org` (palier org, Microsoft, listing) et le journal, écrit ; sa lecture reste à
   exposer.
4. Dashboard : « Partager » sur la carte du compte.
5. Microsoft connectable au nom de l'org ou d'une équipe — *livré côté backend* (décision du
   15/10) :
   - connexion : `POST /api/me/connectors/{outlook|sharepoint|…|microsoft}/connect` avec
     `params.scope` = `org` (admin d'org) ou `group` (chef de l'équipe ACTIVE ; sans équipe
     active, refus), comme Google ; refus `403 scope_forbidden`, `400 invalid_scope`. Le
     state porte le palier, le droit est revérifié au retour. La ligne est posée sous
     `microsoft` à l'entité `org`/`group`, un compte par adresse Microsoft, avec les scopes
     du service demandé ; elle ne garde que ce que SES consentements ont demandé (plus le
     palier administrateur de ces services), au retour comme à chaque renouvellement — le
     jeton d'Entra porte aussi les consentements personnels du titulaire. Aucun défaut posé
     d'office : l'admin le choisit (`oto_identity op=set scope=org|group`) ;
   - service : compte propre → équipe active (puis équipe d'un projet partagé) → org, sans
     désignation, par les paliers de la cascade (`byo_org`) ; deux comptes au même palier
     sans `_account` ni défaut = refus d'ambiguïté qui les nomme ; puis les prêts, règle
     inchangée. Pas de `lent_by` : ce n'est pas un prêt ;
   - listing : `microsoft_accounts` (`shared` = `group`/`org`, `shared_owner`) et
     `GET /api/connectors/{connector}/identities` (`shared`, `shared_scope`, `shared_owner`,
     jamais `is_default`), carte du porteur comprise ;
   - retrait : `DELETE /api/settings/api-keys/microsoft?scope=org|group&account=…`, admin
     d'org ou chef de l'équipe active — la même règle que le compte partagé Google
     (`DELETE /api/google/oauth?scope=…`), sur la surface de retrait de Microsoft ;
   - niveaux d'accès : jugés sous l'org et l'équipe active de l'appel, qui sont les
     propriétaires des comptes que ces paliers servent ;
   - le consentement administrateur (`microsoft_admin_consent`) n'écrit rien et ne change
     pas : il ouvre le tenant Microsoft du client, chaque compte (d'une personne, de l'org
     ou d'une équipe) se connecte ensuite par sa carte.
   Reste au dashboard : proposer « Autoriser » côté org et équipe pour Microsoft sans
   application d'org posée (l'application est celle de la plateforme).
6. Délégation des messageries ramenée au même geste (écran, outil), cible équipe à
   l'écran.
7. Services prêtés (signal 1399). *7a*, backend : la liste par emprunteur, figée à la pose,
   exigée à l'usage, révision `0060_services_pretes` qui fige l'existant. *7b*, console :
   choisir les services au partage, les afficher sur la carte du compte et dans la liste des
   prêts de l'org, étendre ou réduire d'un geste.
