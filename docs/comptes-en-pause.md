---
title: Mettre un compte en pause — neutraliser sans détruire
type: explanation
---

# Mettre un compte en pause — neutraliser sans détruire

> Le cran qui manquait entre « vivant » et « supprimé ». Posé le **2026-09-03**.
> Surface : capacité `admin.account`, tool MCP `oto_admin_account`, route REST
> `POST /api/admin/users/{sub}/suspension`.

## 1. Le trou qu'on ferme

Un compte n'avait que deux états, et le second n'existe même pas comme geste de
produit : **il n'y a aucune suppression de compte** dans le backend — le seul
`DELETE FROM users` du dépôt est l'étape 4 de `db.migrate_sub`, la fusion de deux
identités. Il n'existe ni op MCP, ni route REST, ni console qui supprime un compte.

Et cette suppression-là ne neutralise pas proprement, dans les deux sens à la fois :

- **elle orpheline** — la grande majorité des tables keyed-by-sub n'a **aucune FK**
  vers `users` (`org_members`, `projects`, `docs`, `nodes`, `tool_calls`,
  `connector_credentials`, `runs`…). Sans le repointage manuel de `migrate_sub`, un
  `DELETE` nu laisse des lignes qui désignent un identifiant disparu : la jointure
  est vide, l'affichage dit « inconnu » ;
- **elle détruit** — les dix colonnes qui ont bien une FK sont toutes en
  `ON DELETE CASCADE` (jetons d'API, comptes de messagerie, prêts de compte,
  désinscriptions). Elles partent, sans retour.

Le besoin qui a fait remonter le manque : la fin de migration d'un partenaire vers son
propre tenant. Il en sort des comptes dont personne ne veut plus mais qu'on ne peut
pas supprimer — huit personnes présentes deux fois, dix-huit appartenances, vingt
projets et neuf clés de connecteur qui pendent d'eux, et des comptes dont l'identité
n'existe plus dans aucun annuaire mais qui servaient encore du trafic l'avant-veille.

## 2. Les six arbitrages

### ① Un seul état, pas deux

La demande disait « pauser ou archiver ». Ce sont deux **intentions** — suspendre
quelqu'un qui reviendra, sortir quelqu'un qui ne reviendra pas — et **zéro différence
de comportement** : dans les deux cas le compte ne peut plus rien faire et rien n'est
détruit. Deux états auraient demandé deux fois les mêmes gardes, dont l'une
indistinguable de l'autre à l'exécution.

Ce qui les sépare s'écrit : c'est le **motif**, exigé à la pose. Et le vrai
« définitif » existe déjà plus loin, comme un tout autre geste avec un tout autre
délai : l'effacement de la personne (ADR 0062-D2 — suppression plus pseudonymisation
stable du journal).

### ② Ce qu'un compte en pause ne peut plus faire : **tout**, dès la requête suivante

⚠️ **Le point qui fait ou défait le mécanisme.** Un jeton émis **avant** la pause
reste signé et valide jusqu'à son expiration : une heure pour un JWT Logto, et **sans
limite pour un jeton `oto_`**, qui n'a pas de login du tout. Une pause vérifiée à la
connexion aurait donc été un bouton qui rassure sans agir.

⚠️ **Et ce n'est pas le drain d'alias qui s'en charge — c'est la pause, à sa porte.**
`db.resolve_sub` suit la chaîne d'alias et refuse un identifiant qui n'aboutit nulle
part, mais son prédicat de vivacité est l'**existence de la ligne `users`** : un compte
en pause garde la sienne, donc le drain rend son sub canonique, et c'est très bien —
ce n'est pas son travail. La garde de pause tourne **après** le drain, sur le sub
canonique, et c'est ce chemin-là qui portera le trafic des comptes gelés (épinglé par
`test_un_ancien_identifiant_ALIASE_vers_un_compte_gele_nest_pas_servi`, dont la pause
n'est posée que sur le sub canonique — sans quoi il ne prouverait pas l'ordre).

Le refus tombe **à l'entrée de chaque requête**, sur les quatre portes :

| face | porte | refus |
|---|---|---|
| REST, jeton `oto_` | `api.base._authenticate`, branche haute | `403 account_suspended` |
| REST, JWT | `api.base._authenticate`, branche basse | `403 account_suspended` |
| REST, ancien identifiant | la levée de `db.upsert_user` | `403 account_suspended` |
| MCP, **toute** requête | `AccountSuspendedMiddleware.on_request` | `McpError`, `account_suspended` |
| lien d'upload signé (sub scellé, sans jeton) | `api.uploads._do_signed_upload` | `403 account_suspended` |

Depuis le 06/10/2026 (oto-backend#1165), ces portes n'appellent plus le prédicat de pause
directement : elles traversent la **garde d'identité** unique, `garde_identite.refus`, qui
juge d'abord le **tenant désactivé** (`tenant_disabled`, `docs/tenants.md` §Désactiver un
tenant) puis la pause. Le lien d'upload signé — un porteur d'identité 15 minutes durant,
sans aucun jeton — n'était gardé par rien jusque-là ; il l'est par la même fonction.

Côté MCP, la garde est sur `on_request` et pas sur `on_call_tool` : le handshake
injecte les instructions de l'org (guides de plateforme, d'org, d'équipe) et
`tools/list` révèle la boîte composée pour ce compte. Garder l'appel seul laisserait
un compte sorti continuer à **lire** ce qu'on lui a retiré le droit de faire.

Coût : une lecture sur clé primaire par requête — à côté d'un `upsert_user` que la
face REST fait déjà à chaque appel (une lecture aussi tant que l'adresse, le nom et la
langue ne changent pas : il n'écrit que ce qui change, cf. `docs/event-loop-perf.md`). **Pas de cache** : une pause doit mordre à la
requête suivante, pas à la prochaine expiration d'un cache.

### ③ Ce qui survit : tout le reste

Le geste n'écrit que trois colonnes de `users`. Restent en place et continuent de
désigner ce compte : ses **appartenances** (`org_members`), son **espace personnel**,
ses **projets**, **documents**, **lignes de tableau** et **nœuds**, ses **credentials**
dans le coffre, ses lignes de **journal**, sa fiche de profil. Un document qu'il a
écrit dans une organisation continue de dire qui l'a écrit.

C'est la raison d'être du geste : le départ d'un membre laisse à l'org un patrimoine
qu'elle arbitre objet par objet, et **l'inaction ne doit pas détruire** (ADR 0062-D4).
La pause est l'état dans lequel cet arbitrage peut prendre le temps qu'il prend.

⚠️ **Sauf ce qu'il prêtait : ses prêts s'arrêtent avec lui** (oto-backend#898,
arbitrage du 23/09/2026, option A). Un compte de connecteur prêté
(`connector_account_grants`, nominatif ou de groupe) et une clé prêtée à un pair
(`share_side`, ADR 0044) cessent de servir leurs bénéficiaires pendant la pause. Rien
n'est détaché pour autant : le grant, le `share_side` et le pointeur « identité
opérée » du bénéficiaire restent écrits, c'est la **résolution** qui lit l'état du
prêteur à chaque appel. Au réveil, le prêt revient tel quel, **sans rien
reconfigurer**.

Le bénéficiaire, lui, n'est pas en pause — il reçoit un **refus nommé**, code
`lender_suspended`, qui dit que le prêteur est en pause, que le prêt n'est ni révoqué
ni supprimé et qu'il reprendra à son réveil. Pas le motif de la pause : il appartient
à l'exploitant. Où il tombe :

| face | porte | refus |
|---|---|---|
| compte prêté, pointeur ou `_account=` | `connectors.identities.resolve_operated_account_id` | `PreteurEnPause` → `McpError`, `data.code=lender_suspended` |
| compte prêté, épinglé par le projet | `tools.unipile.unipile_client` | idem |
| compte prêté, choisi comme identité | `_unipile_select` → capacité `connectors.set_default_identity` | `403 lender_suspended` |
| clé prêtée (`share_side`), `_instance=` ou binding | `access.rbac.guard_instance_access` | `McpError`, `data.code=lender_suspended` |

À la lecture, `oto_account_access` (`granted_to_me`) marque le prêt retenu
`active=false, owner_suspended=true` — distinct d'un canal déconnecté.

### ④ Ce que voient les autres — et les sièges

**Le membre reste dans la liste des membres de son organisation, marqué `suspended`.**
Le retirer serait détruire une appartenance qu'on a justement choisi de ne pas
détruire, et laisserait un administrateur devant des documents signés par quelqu'un
qui n'apparaît plus nulle part.

⚠️ **`active` et `suspended` ne parlent pas de la même chose**, et le premier est un
faux ami de longue date : `active` dit que **cette org est l'org maison** de la
personne — un membre parfaitement en état y est `false` dès qu'il travaille par défaut
ailleurs.

**Sièges facturés : la question est sans objet, et c'est mesuré.** Le backend ne
facture **jamais** au siège. `org_members` n'entre dans aucune décision de
facturation : les quatre paliers de `billing.PLANS` sont des forfaits plats par org,
sans quantité, et le montant débité est `meta["amount"]` — jamais multiplié par un
effectif. Le seul compteur de « sièges » du dépôt,
`db.count_unipile_accounts_for_org`, porte sur des **comptes de messagerie hébergés**,
pas sur des humains, et son plafond est levé pour tout abonné. Mettre un compte en
pause ne change donc **aucune facture, d'aucun centime**, aujourd'hui.

Si un jour la facturation compte des membres, c'est là qu'il faudra trancher — et la
réponse par défaut devrait être « un compte qui ne peut plus rien faire ne se facture
pas », sans quoi le geste devient impraticable pour qui en a le plus besoin.

### ⑤ Qui peut le faire

| qui | sur qui | par quoi |
|---|---|---|
| **super admin de plateforme** | n'importe quel compte | la règle plateforme de `TENANT_ADMIN_OF_TARGET` |
| **admin de tenant** | les comptes de **son** tenant | `tenant_admins`, sans aucun rôle de plateforme |
| ~~org_admin~~ | — | **non**, et c'est délibéré |

**Pas l'org_admin** : un compte n'appartient pas à une org — il a un espace personnel
et souvent plusieurs appartenances (dix-huit pour huit personnes, dans le chantier qui
a motivé ce geste). Le mettre en pause le couperait d'espaces sur lesquels cet
administrateur n'a aucun titre. Le geste org-scopé existe déjà et répond exactement à
son besoin : retirer le membre (`admin.org_member`), qui ne touche que l'appartenance
visée.

**L'admin de tenant, lui, opère sans privilège de plateforme** — c'est le point de la
demande, et l'ADR 0056-D3 l'écrit mot pour mot : un tenant administre ses orgs,
« créer, paramétrer, **suspendre** », sans grant à obtenir.

⚠️ **Le périmètre vient du serveur, pas de la requête** (ADR 0066-R3). C'est ce qui
distingue `TENANT_ADMIN_OF_TARGET` de sa sœur `TENANT_ADMIN_OF` : celle-ci prend un
slug que l'appelant écrit ; celle-là **dérive** le tenant du sub visé. L'`Input` ne
porte aucun champ de tenant — il n'y a rien à remplir pour prétendre à un périmètre.

⚠️ **La cible est un `sub`, pas une adresse électronique**, contrairement aux autres
consoles de compte qui acceptent les deux. Une adresse ne désigne pas un compte de
façon unique : les huit doublons du chantier partagent la leur entre leur identité
chez nous et celle chez le partenaire. Résoudre par adresse choisirait à la place de
l'opérateur, exactement là où il doit choisir lui-même.

⚠️ **`tenant_admins` est à zéro ligne en production à ce jour.** C'est un fait de
peuplement, pas de conception : nommer un admin de tenant est un geste qui existe
(`admin.tenant_admins.add`, super admin, `POST /api/admin/tenants/{slug}/admins`).
Tant que personne n'y est nommé, seule la plateforme peut exercer le verbe.

### ⑥ La réversibilité et sa trace

`suspended_at`, `suspended_by`, `suspended_reason` — l'état courant, et **le motif est
exigé** : une pause sans motif écrit devient, six mois plus tard, une pause que
personne n'ose lever et que personne ne sait expliquer.

Re-poser une pause **ne réécrit rien** : ni l'auteur, ni la date, ni le motif
d'origine. Une pause est un fait daté ; l'écraser ferait perdre la seule chose que ces
colonnes existent pour retenir.

⚠️ **Un motif trop long est REFUSÉ, jamais raboté** (corrigé le 03/09/2026, le jour
même du premier usage). Il était coupé en silence à `_MOTIF_MAX` : le premier gel posé
avec cet outil s'est arrêté au milieu d'une phrase et a emporté sa dernière ligne —
celle qui disait à quelle condition réveiller le compte. C'est le pire endroit possible
pour une coupe muette, parce que la consigne de sortie s'écrit **en dernier** et que ce
texte n'existe que pour être relu, des mois plus tard, par quelqu'un qui n'était pas
là. La colonne est en `TEXT` : la borne est un choix de surface, pas une contrainte de
stockage — donc elle se dit, et le refus donne la mesure pour que l'opérateur
raccourcisse en sachant ce qu'il coupe.

Ce cas appartient à l'inventaire « le code sait, et son savoir s'arrête à la frontière
de la réponse » (oto#42), et il y a produit une règle. Il prend la **règle 2** — « un
chemin qui coupe rend un total, un curseur ou un drapeau » — **à l'envers** : ici même
un drapeau n'aurait rien réparé, puisque la fin du texte ne survit nulle part et que
celui qui l'avait s'en va. D'où la **règle 4**, ajoutée le 03/09 : *une coupe sur une
LECTURE se signale, une coupe sur une ÉCRITURE se refuse*. En lecture, l'appelant
redemande ; en écriture, il n'y a rien à redemander.
⚠️ Et ce n'est pas la garde qui a sauvé ce texte-là, elle n'existait pas encore : c'est
d'avoir relu la réponse dans la minute. Le journal, lui, écrit le motif **après** la
coupe — il n'aurait rien gardé. Rien dans le système ne signalait le manque.

Le réveil est au **même palier** que la pose, et c'est voulu : obliger un partenaire à
venir nous voir pour réveiller un de ses comptes reproduirait exactement le problème
qu'on résout. Il rend `changed` — « réveillé » et « il ne dormait pas » ne sont pas la
même réponse, et une console qui affiche « fait » dans les deux cas ment une fois sur
deux.

**L'historique complet** (qui a posé, qui a réveillé, quand, combien de fois) vit dans
le **journal des appels** : le geste est une capacité, donc chaque exercice y est
enregistré avec son auteur et ses arguments. Les colonnes, elles, ne portent que
l'état courant — c'est-à-dire exactement ce qui est vrai au moment où la question se
pose. Si un historique durable et interrogeable devient nécessaire, ce sera une table
à part, sur le modèle d'`outreach_optouts`.

## 3. Le non-négociable : aucune résurrection automatique

⚠️ **C'est déjà arrivé.** Un compte supprimé par une fusion a été **recréé** par un
mécanisme automatique, puis a servi **884 appels sous une identité morte**. Le
mécanisme : `resolve_sub` ne fait **qu'un saut** (une chaîne d'alias à trois maillons
a été mesurée), et l'`upsert_user` de la face REST n'est **pas** sous la même commande
que le drain — un vieux sub non redirigé n'échoue pas, il **crée** la ligne, avec son
espace personnel neuf.

Deux gardes ferment les deux seuls chemins par lesquels une pause peut disparaître :

1. **`db.upsert_user` refuse de créer** une ligne pour un sub dont la **chaîne**
   d'alias mène à un compte en pause. La vérification ne coûte que sur un vrai
   `INSERT` — une fois dans la vie d'un compte — et tombe **avant** les effets de
   naissance : rien n'est créé quand elle refuse, et la levée annule la transaction.

   ⚠️ **La chaîne, pas le premier saut**, et c'est un trou qui a été mesuré rouge
   pendant l'écriture de ce lot. `migrate_sub` écrit `(old → new)` sans aplatir les
   alias qui pointaient déjà vers `old` : deux fusions successives laissent A→B→C.
   Une garde qui ne regarde qu'un maillon trouve B — un compte que la fusion a
   supprimé, donc ni vivant ni en pause — et **laisse passer**. C'est exactement le
   cas du chantier qui a motivé ce geste : des comptes déjà fusionnés une fois. La
   remontée porte une **clause `CYCLE` native**, la convention du dépôt pour toute
   récursion sur un graphe auto-référent, plus une borne de 16 maillons en seconde
   ceinture.
2. **`db.migrate_sub` refuse de fusionner** dès que l'un des deux comptes est en
   pause, **dans les deux sens** : une source en pause verrait sa marque partir avec
   le `DELETE` de l'étape 4 pendant que son patrimoine passe à un compte vivant ; une
   cible en pause recevrait le patrimoine d'un vivant et le rendrait inatteignable.

⚠️ **Ce refus vaut aussi pour l'acte d'opérateur** (`operator_source`). Le
rapprochement automatique a été désarmé le 2026-09-03, mais la porte manuelle reste
ouverte — c'est celle qui reste, donc celle qu'il faut fermer. Un opérateur qui veut
vraiment fusionner **réveille d'abord**, explicitement, avec sa trace.

Les deux **lèvent** (`db.CompteEnPause`) au lieu de rendre une valeur falsy : sur
`migrate_sub`, `False` veut déjà dire « déjà migré / inexistant », et les appelants
d'`upsert_user` ignorent tous sa valeur de retour. Un refus indistinguable d'un no-op
n'est pas un refus — c'est le mode d'échec que cette pause existe pour fermer.

## 4. Ce qui a été éprouvé, et comment

**28 gardes, chacune prouvée rouge-puis-vert par mutation** : la garde est
neutralisée une à une, et le test qui la nomme doit tomber. Une garde jamais vue rouge
n'en est pas une — le vert d'un test ne prouve pas qu'il garde, il peut être vert
parce que le système l'est.

⚠️ **Deux tests d'autorisation ont d'abord été trouvés inertes par ce banc** : ils
restaient verts avec leur branche neutralisée, sauvés par une branche voisine
(`is_tenant_admin`). Ils ont été rendus **discriminants** — chacun neutralise
explicitement les autres branches — et décrivent maintenant le seul scénario où leur
branche est la dernière ligne. Celui du tenant primaire ferme le trou le plus large du
mécanisme : une ligne `tenant_admins('oto', …)` posée à la main donnerait à son
porteur un pouvoir sur **tous** les comptes de la plateforme sans être super admin.

⚠️ **Une garde n'est pas prouvable par le comportement, et c'est un cliquet de CLASSE
qui la tient** : la protection de cycle de la remontée d'alias reste verte quand on la
retire, parce qu'un cycle ne fait apparaître aucun compte en pause de plus. J'en avais
d'abord conclu qu'elle bornait « un coût, pas un résultat » et je l'avais sortie du
banc. Le dépôt en juge autrement, et il a raison : `test_node_parent_cycle.py` exige
une clause `CYCLE` pour **toute** récursion sur un graphe auto-référent, au grain de la
fonction — *« mieux vaut un refus à expliquer qu'une boucle à découvrir en
production »*. Il a attrapé cette requête à la première exécution de la suite. La
mutation est donc revenue au banc, avec **ce cliquet-là pour cible** : c'est lui qui
rougit, pas un test de comportement.

Fichiers : `tests/test_account_suspension_gates.py` (les quatre portes),
`tests/test_account_suspension_authz.py` (qui peut, sur qui),
`tests/test_account_suspension_db_live.py` (la résurrection et la fusion, sur SQL
réel, avec le contrefactuel de chaque refus),
`tests/test_account_suspension_prets.py` (les prêts suspendus avec le prêteur et
rendus au réveil, sur SQL réel, #898).

## 5. Coût de surface, chiffré

`oto_admin_account` pèse **1 242 caractères** servis (17 de nom, 908 de description,
317 de schéma), soit **1,37 %** de ce qu'un compte ordinaire reçoit au handshake.

Ce coût vient du plancher de visibilité `None`, et il est **assumé** : masquer l'outil
ne protégerait rien (ADR 0031/0066-R4) mais fastmcp en refuserait aussi l'**appel**
(#471) — un admin de tenant à qui on le masquerait ne pourrait tout simplement pas
s'en servir depuis un agent. Or c'est par là qu'un partenaire travaille, et c'est
exactement le défaut qui a rendu inerte la surface bornée existante : servie côté web
seulement, elle ne rendait rien à qui ne travaille qu'en MCP.

## 6. Ce qui n'est pas fait

- **Aucune interface** : le dashboard ne montre ni le marqueur `suspended` de la liste
  des membres, ni les champs de la fiche admin, ni de bouton. Les données sont servies
  (`GET /api/orgs/{id}`, `GET /api/admin/users/{sub}`), le rendu reste à faire —
  c'est un lot `oto-dashboard`.
- **Pas d'historique interrogeable** des poses et des réveils au-delà du journal des
  appels (cf. ⑥).
- **Pas de pause d'organisation.** L'archivage d'org existe (`org.archive`) et n'a
  aucun rapport : il ne touche pas les comptes de ses membres.
- **Un endpoint de projet publié** (ADR 0032) qui agit sous le compte Unipile d'un
  membre de l'org continue d'agir si ce membre est en pause : ce n'est pas un prêt à
  une personne, et l'arbitrage du 23/09 ne l'a pas tranché (reste dans #898).
- **Pas d'expiration automatique.** Une pause dure jusqu'à ce qu'on la lève. Un réveil
  qui tomberait tout seul serait, très exactement, la résurrection automatique que
  ce lot existe pour interdire.
