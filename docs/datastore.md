---
title: Datastore (spine natif PG, ADR 0016)
type: reference
description: >-
  Référence du spine de stockage structuré per-user de oto-backend : tables PG
  user_datastores + datastore_rows (JSONB natif, uuid7, _created/_updated_at auto),
  chargé hors gate d'activation (provider=None, ADR 0011), partage DB-only via
  datastore_shares, deep-link dashboard via data_url. Couvre les surfaces MCP data_*
  et REST /api/datastore/*, la file de travail (bail, plafond de reprises), l'auth double (JWT Logto ou API token oto_*), l'OAuth
  Google per-user multi-compte (flux /api/google/oauth/*, refresh token chiffré,
  scopes Sheets/Drive/Gmail/Tasks et gotcha CASA gmail.modify restricted), et la
  procédure de setup GCP one-shot. À consulter pour ajouter ou déboguer le datastore,
  configurer OAuth Google ou comprendre la séparation identité Logto vs délégation.
adr:
  - "0016"
  - "0011"
---

# Datastore (spine natif PG, ADR 0016)

> ⚠️ **Un tableau créé sans préciser son propriétaire naît PERSONNEL** (ADR 0068,
> 04/09/2026). Il naissait possédé par l'**org active** — « suppression du perso », un
> choix assumé du temps où l'appelant était un humain devant un écran, qui voit ce
> qu'il crée et où. L'appelant est aujourd'hui un agent qui ne lit que le nom du
> verbe : le geste le plus banal du produit posait du contenu lisible de toute l'org,
> sous une description servie qui annonçait « unique **per user** ». La face REST
> faisait déjà l'inverse (`owner.get("type") or "user"`) **en annonçant « classeur
> d'org par défaut »** — deux propriétaires pour un seul verbe, chaque texte affirmant
> le contraire de sa propre face. Les deux convergent maintenant sur `user` ; partager
> se dit (`owner: {type: "org"|"group", id: N}`), et **les tableaux existants ne
> bougent pas**. `data_share` suit la même règle : son défaut passe de `write` à
> `read`, parce que « partager » veut dire « qu'il puisse le lire » — sur un tableau,
> l'écriture n'ajoute pas un droit, elle en retire un à son propriétaire.
> Garde : `tests/test_description_dit_le_proprietaire.py` lit le défaut **dans le
> code** puis exige que le texte servi le nomme — jamais l'inverse, sinon il se
> périmerait en restant vert.

Stockage structuré léger par user, **substrat PostgreSQL natif** (plus Google
Sheets — ADR 0016). Un namespace = une ligne `user_datastores` ; les rows vivent
dans `datastore_rows` (un dict **JSONB** par row, types préservés nativement,
fin de la sentinelle `__j:`). Schéma libre. Trois champs auto-managés exposés à
plat : `_id` (uuid7-like), `_created_at`, `_updated_at`.

**Datastore = spine plateforme** (`provider=None`, ADR 0011), PAS un connecteur
Google : chargé explicitement dans `register_all` (à côté de meta/orgs),
donc **hors gate d'activation** et **sans dépendance externe** — marche sans
connecter Google (plus de `412 google_not_connected`). Le partage est **DB-only**
(`datastore_shares` ; le destinataire lit via son propre `sub`, plus de
permission Drive). `data_url` renvoie un **deep-link dashboard** (`/console/data`),
pas une URL de Sheet. Code : `datastore/core.py` (`DatastorePg`) + `tools/datastore.py`
(face MCP) + `capabilities/datastore/*.py` (face REST, depuis #302 — plus
`api/datastore.py`, qui n'en porte plus rien) + fonctions `db.datastore_*`.

> **Export/sync vers un provider tiers** (Sheets/Docs/Notion — édition humaine,
> garantie de sortie) = projection optionnelle, **déférée à otomata#29**. C'est
> la raison d'être de l'unbundle, construite après.

> **Backfill** (Sheets → PG) : `scripts/migrate_datastore_to_pg.py` (idempotent,
> auto-suffisant pour la lecture Sheets). À lancer sur la box **après** le restart
> du code PG (brève fenêtre datastore-vide).

Surfaces :
- MCP tools `data_*` (`data_create_namespace`, `data_write`, `data_rows`,
  `data_delete_row`, `data_url`, `data_share`, etc.) — pour Claude.ai / Claude Code.
- MCP **App** `data_app` (`@mcp.tool(app=True)`, SEP-1865, prefab_ui) — variante à
  interface rendue : sans `namespace` = table des namespaces ; avec `namespace` =
  table triable/cherchable/paginée des rows, avec `filter` exact-match optionnel
  (même forme que `data_rows`) et `show_meta` pour les colonnes `_id/_created/_updated`.
  Rend le contenu INLINE dans le chat au lieu du seul deep-link `data_url`. Dégradation
  gracieuse si l'extra `fastmcp[apps]` est absent (non enregistré). Pattern : cf.
  `tools/foncier.py` (`foncier_*_app`).
- REST `/api/datastore/*` — pour le CLI `oto data` + UI dashboard. **Face DÉRIVÉE
  depuis le 2026-08-12** (#302) : plus une seule route écrite à la main, tout vient
  des capacités `capabilities/datastore/{namespaces,rows,schema,sharing,claim,
  activity,columns}.py`. Conséquences pratiques : les 24 opérations (au 2026-09-04) portent
  leur schéma d'entrée ET de réponse dans `/api/openapi.json` (un intégrateur les génère),
  et un **champ inconnu est refusé** (400 `unknown_fields`) au lieu d'être ignoré —
  sauf le corps d'un ajout/patch de ligne, qui EST la donnée (`body_field`) — **UNE**
  ligne par `POST …/rows`, jamais un lot : un corps à clé unique portant une liste
  d'objets est refusé `400 batch_body` (oto#48) ; le lot = `data_write(rows=…)` ou
  l'upload signé NDJSON/CSV.
  ⚠️ Éditer un de ces chemins = éditer sa capacité ; en rajouter un à la main casse
  le garde-fou `tests/test_rest_modules_are_capabilities.py`.
- **Guide servi aux agents** `datastore-semantics` (seed `oto_mcp/guides/
  datastore-semantics.md`, lu par `oto_guide op=read`) — couches, `readonly`, clé
  métier, ce qui diverge entre les deux faces et ce qu’une réponse ne contient pas ;
  chaque phrase y est ancrée dans le code (vérifié le 2026-09-05). `data_write`/
  `data_rows` et leurs capacités REST (`list_rows`, `append_row`) y
  renvoient en une ligne (otomata-tech/oto#51) : le contrat minimal reste dans la
  description, le détail vit ici et dans le guide (cf. §`data_write` — deux sémantiques).

> **Trier ET filtrer sur les dates système (05/08).** `order_by` acceptait déjà
> `_created_at`/`_updated_at`/`_id` ; le WHERE, lui, ne connaissait que
> `data ->> <champ>` — donc un filtre « modifiée depuis le 1er » cherchait la clé
> `_updated_at` DANS le JSON, ne la trouvait jamais et rendait **zéro ligne sans
> erreur**. `_ds_filter_clauses` route désormais ces trois noms vers leur vraie
> colonne (`_DS_META_TS_COLS`/`_DS_META_TEXT_COLS`), sur les deux faces (dashboard
> `filters=[…]`, agent `data_rows(filter={"_updated_at": {"gte": "2026-08-01"}})`).
> Deux règles à connaître : une valeur **date seule** désigne la **journée entière**
> (`lte "2026-08-05"` inclut le 5 — un `<=` nu comparerait à minuit et effacerait la
> journée saisie), et les ops sans objet sur une colonne NOT NULL (`empty`,
> `not_empty`, `contains`) sont **refusées** (400 nommant les ops valides) plutôt que
> servies vides. Une valeur de date malformée lève aussi côté Python : le cast SQL
> aurait rendu un 500 opaque au lieu d'un `invalid_filters`.

**Journal de travail : les deux surfaces, une seule table (2026-07-28).** Un geste fait
au cockpit (dashboard, REST) était journalisé au seul grain ROUTE (`RestCallLogger`,
`tool='PATCH /api/datastore/…'`) : on voyait qu'une écriture avait eu lieu, jamais
LAQUELLE ni depuis quel état — cliquer une transition de cycle de vie ne laissait donc
rien d'exploitable (ni retrouver la ligne, ni annuler). Les mutations REST posent
désormais AUSSI une ligne **sémantique** dans la même table `tool_calls`
(`kind='rest'`), nommée dans le **vocabulaire des tools MCP** (`data_write`,
`data_delete_row`, `data_release`) et portant `namespace`/`ns_id`/`id`/`fields`/
`from_status`/`to_status`. Helper unique `calllog.log_rest_call` (best-effort, hors
chemin chaud) ; colle datastore dans `datastore/journal.py`. Lectures : capacités
`me.datastore.row_activity` (`GET …/rows/{row_id}/activity`) et `me.datastore.activity`
(`GET …/activity`, `?limit=` borné 200) — elles ne filtrent plus `kind='mcp'`, et
résolvent `sub → email` **à la lecture** (un lot par page : `tool_calls.email` n'est
peuplé par aucun sink).

⚠️ **`from_status` vient de la MUTATION, pas d'une relecture.** Les mutations du store
(`update_row`/`delete_row`/`append_row`/`force_release`) acceptent un **relevé** `trace`
(dict mutable) qu'elles remplissent avec `ns_id`/`namespace`/`status_key`/`title_key`/
`prev_status` — pris là où ils sont déjà calculés. Le relire avant l'appel courrait avec
un write concurrent (un agent qui bouge la ligne entre les deux) et ferait proposer au
cockpit une annulation vers un état que la ligne n'a jamais eu ; ça ajoutait en prime
4 requêtes PG synchrones par mutation, sur un serveur mono-loop.

⚠️ **Le journal cite l'ENTITÉ, pas la chaîne tapée.** Le calllog journalise les args
BRUTS de l'appel — or `data_write` prend `namespace: str`, que l'agent remplit tantôt du
nom, tantôt de l'id, tantôt d'un `slot:<name>`. Corréler là-dessus obligeait à matcher
par NOM, avec trois dettes : un nom n'est unique que **par propriétaire**
(`uq_user_datastores_owner_ns`) donc il fallait le borner au tenant sous peine de fuite
cross-org, il change au **renommage** (historique orphelin), et un `slot:` n'est pas
rétro-résolvable. Depuis le 2026-07-28 les deux surfaces corrèlent sur le **`ns_id`
résolu serveur** : la face REST le tient de sa route, la face MCP du **relevé d'appel**
(`session_org.note_call_trace`, rempli par `DatastorePg._resolve` APRÈS les gardes —
un tableau refusé ne laisse pas de trace ; versé dans les args par `_calllog_sink`,
clés **fermées** `server._TRACED_ARGS`). Index d'expression partiel `idx_tool_calls_ns`.

> Le relevé est un **HOLDER MUTABLE** (dict posé vide par `CallContextMiddleware`), pas
> une valeur rebindée : les handlers de tools sont majoritairement des `def` sync
> dispatchés en threadpool, où `copy_context()` copie les BINDINGS — un `.set()` fait
> dans le thread ne remonte JAMAIS au contexte appelant, la mutation du dict posé en
> amont si (même objet). Garde-fou : `test_the_trace_survives_the_threadpool`.

L'axe NOM subsiste en **repli, borné au propriétaire** (`db._owner_clause` → `l.org_id`
ou `l.sub`), pour l'historique écrit avant cette bascule — il s'éteint de lui-même avec
la rétention 30 j. Même borne sur l'autre axe flou : la valeur de clé métier du parcours
d'une ligne (cherchée en sous-chaîne dans les args). Owner inconnu ou tableau d'équipe ⇒
l'axe flou est abandonné (sous-couvrir, jamais sur-matcher) ; `ns_id` et `row_id` (uuid4,
accès déjà prouvé) se matchent nus.

⚠️ **La lentille REST admin ne compte que les ROUTES.** `kind='rest'` porte maintenant
deux natures (route `MÉTHODE /chemin` de `RestCallLogger`, geste métier `data_write` du
journal) → `db.rest_call_stats` filtre sur la forme `position(' /' in tool) > 0`, sinon
chaque mutation du cockpit double-compte et `by_route` liste des pseudo-routes à latence
nulle. Les autres lentilles de monitoring filtrent `kind='mcp'` : elles sont intactes.

**File de travail : les deux surfaces RÉSERVENT (2026-08-08, signal #362).** Le bail
(ADR 0046 D, colonnes `claimed_by`/`claimed_until`) n'était posable que depuis le MCP
(`data_claim_next`) : une application web pouvait lire la file (`GET …/queue`) et
libérer, jamais réserver. Les fronts compensaient en écrivant un verrou **dans les
données** de la ligne — coopératif, donc non atomique (deux personnes qui cliquent à la
même seconde obtiennent la même ligne), et deux colonnes à prévoir par tableau pour une
mécanique déjà en base. Deux **capacités** REST-only comblent le trou
(`capabilities/datastore/claim.py`, `mcp=None` assumé — `data_claim_next` tient la face
agent) :

- `POST …/claim_next` `{worker, filter?, lease_s?}` → la prochaine ligne libre, réservée
  (`FOR UPDATE SKIP LOCKED`), ou `{row: null, hint}` quand il n'y a plus rien — le `hint`
  nomme le périmètre déclaré du tableau s'il y en a un (`lifecycle.claimable`, #517) ;
- `POST …/rows/{row_id}/claim` `{worker, lease_s?}` → **cette** ligne. **409
  `row_claimed`** si un autre la tient (avec qui et jusqu'à quand) — un conflit se dit,
  il ne se devine pas. Renouvelable sans erreur par le **même** `worker` : rafraîchir son
  écran ne doit pas coûter sa ligne (`db.datastore_claim_row`, UPDATE conditionnel).
  **409 `row_outside_claimable`** si le tableau déclare un périmètre que la ligne ne
  satisfait pas (#517), jugé AVANT le bail — `details.claimable` porte le périmètre.

`worker` (libellé stable de celui qui réserve) est **exigé aux deux claims** : c'est la
garde rejouée au release. D'où le second cran, sur `POST …/rows/{row_id}/release` :
corps `{worker}` ⇒ libération **gardée** (`release_claim`) ; corps vide ⇒ libération
**forcée** (supervision dashboard), mais **refusée à un jeton porté** (`auth.token_scopes.
current()` non None → 400 `worker_required`). Un jeton porté est le vecteur des
intégrations multi-utilisateurs : y laisser le forcé, c'est laisser chacun retirer la
ligne de son collègue. Côté portée, réserver **est une écriture** (`_ALLOWED` : les deux
claims en `WRITE`) — un jeton `read` lit la file sans pouvoir en retirer une ligne.

**Le bail sait qui le tient, et il ne se lève plus tout seul (13/08, #317).** Trois
défauts constatés en PRODUCTION au premier essai réel, sur une campagne de 8 910
lignes. ① Le lien entre une ligne et le traitement en cours n'était jamais enregistré
(la source lue ne rend un run que s'il est passé explicitement, or un agent qui encadre
son travail empile dans l'état de session) : rien ne se libérait à la fin, et le
TITULAIRE lui-même se voyait refuser l'écriture. Les deux sources sont désormais lues
une seule fois, au middleware de contexte. ② Ce refus, non traduit, ressortait en
« erreur interne » : il est maintenant un refus NOMMÉ, portant qui tient la ligne,
jusqu'à quand, et comment la libérer. ③ La protection du chemin par LOT n'avait jamais
rien protégé — un fail-open sur les horodatages « rendus en texte », alors que le row
factory du dépôt normalise tout horodatage en texte : le cas cru marginal était le cas
normal. Les deux chemins avaient donc des comportements opposés, l'un refusant tout le
monde y compris le titulaire, l'autre ne refusant jamais personne. Une date illisible
REFUSE désormais au lieu d'ouvrir : un bail dont on ne sait pas s'il court protège
peut-être encore quelqu'un.

⚠️ **Correction datée (29/08/2026, #547) : la seconde source ne rend rien.** Le
paragraphe ci-dessus dit « les deux sources sont désormais lues une seule fois, au
middleware de contexte » — le jeton `_run_id=` explicite, puis la pile de session. Le
repli sur la pile est en fait **inerte** : `CallContextMiddleware.on_call_tool` appelle
`guide_run.active_run_id(context)` avec le `MiddlewareContext` de FastMCP, qui n'a pas
de `get_state` ; la lecture lève, est avalée, et rend une pile vide. Le test qui couvre
ce chemin passe un contexte de laboratoire qui, LUI, a `get_state` — le même piège que
celui dont son propre en-tête met en garde. Conséquence : **le datastore ne connaît un
run que si `_run_id=` est passé explicitement**, y compris dans une session serveur qui
tient un run actif. C'est ce qui rend le jeton obligatoire dans les faits, et ce que la
description de l'axe dit désormais (`call_axes.RUN`). Mesuré, non corrigé ici : le
correctif de la lecture est un lot à part, avec sa propre mesure.

⚠️ **Écrire un état terminal ne libère plus la ligne** — le store émet une notice à la
place. Un tableau dont le statut n'a aucun état terminal est une file qui ne libère
rien : `set_schema` le signale à la pose.

**Le bail servi dit POUR QUEL RUN : `_claimed_run` (01/09/2026).** Les lignes servies
portaient deux tiers du bail — à QUI (`_claimed_by`) et JUSQU'À QUAND
(`_claimed_until`) — jamais POUR QUEL RUN, alors que `datastore_rows.claimed_run` le
porte depuis #317. Une vue de surveillance voyait donc qu'un agent tenait une ligne et
jamais **laquelle** : elle pouvait relier un travail à son TABLEAU, pas à sa LIGNE. Le
serveur savait pourtant déjà répondre — l'alias `@claimed` résout run → ligne par cette
même colonne — mais seulement au run **lui-même**, qui doit porter son jeton ; jamais à
un tiers qui regarde la file.

`_claimed_run` est désormais rendu partout où `_claimed_by` l'est (liste, fiche par id,
les deux curseurs, `queue`, et le claim lui-même). **Trois états, pas deux :**

| forme | sens |
|---|---|
| `_claimed_run: "<run>"` | ce run tient la ligne — l'adresse du travail en cours |
| `_claimed_run: null` | le bail a été pris **sans run** (une personne sur la file du dashboard, un agent qui n'a pas passé `_run_id`) — un fait, pas un trou |
| clé **absente** (comme `_claimed_by`) | pas de bail du tout ; sur des millions de lignes jamais réservées, trois `null` par ligne seraient du bruit dans toutes les lectures |

⚠️ **Ce que `_claimed_run` ne dit PAS.** Il répond « sur quelle ligne ce run est-il
MAINTENANT », jamais « quelle ligne ce run a-t-il travaillée » : rendre les lignes d'un
run (`run_finish`, ou le runner concluant son job, #633) efface la colonne. Le lien
avec un travail **conclu** ne peut donc pas venir d'ici — il doit venir du harnais, qui
connaît sa ligne (et qui, sur le chemin « conversations », ne la connaît pas toujours :
il la retrouve par alias, et ce recours échoue dès que la ligne est relâchée).

⚠️ `datastore_release` n'efface pas `claimed_run` (#664), donc la colonne peut rester
garnie sur une ligne libre. **Rien n'en sort** : la projection ne parle du run que sous
`claimed_by IS NOT NULL`. Et `_row_to_dict` lit la colonne **par clé**, pas par `.get` :
un chemin de lecture qui l'oublierait LÈVE au lieu de servir un faux « ce bail n'a pas
de run ». La garde mécanique est
`tests/datastore/test_claimed_run_projection.py::test_toute_projection_de_ligne_porte_claimed_run`
— elle relit par AST les requêtes de `db/` et refuse celle qui projette une ligne avec
`claimed_by` sans `claimed_run`.

**Un bail EXPIRÉ n'est pas une réservation, et c'est POSTGRESQL qui tranche
(01/09/2026, #726).** Mesuré sur un fichier de production : **495 lignes sur 8 910
portaient `_claimed_by`, et les 495 étaient expirées** — la plus ancienne depuis
dix-huit jours, au nom de travailleurs d'une campagne close. La garde le savait déjà
(`datastore_active_lease` filtre `claimed_until > NOW()`, « expiré compte pour
libre ») ; la lecture, non. *Deux lectures voisines de la même donnée, une seule
connaissait la règle — et le champ servi affirmait ce que le système tenait pour faux.*

La fraîcheur voyage désormais **en colonne calculée** dans les cinq requêtes qui
projettent une ligne avec son bail :
`(claimed_until IS NOT NULL AND claimed_until > NOW()) AS claim_active`. Le sérialiseur
la **lit**, il ne la recalcule pas.

> ⚠️ **Pourquoi pas une comparaison en Python, qui aurait coûté trois lignes.** Elle
> aurait été une SECONDE implémentation de la règle, et elle était fausse en germe :
> comparer les horodatages en TEXTE n'est juste que tant que `_normalize_value` émet un
> séparateur espace sans fuseau. Le jour où un chemin de lecture rendrait un `T`
> (`0x54 > 0x20`), **tout bail se serait lu ACTIF** — sans exception, sans rien rougir,
> et déclenché par un changement anodin ailleurs. La lecture et la garde ne se
> *ressemblent* pas : elles partagent le prédicat, sur la même horloge.

**Deux contrats, et le défaut est le sûr.** `_row_to_dict(..., bail_echu=...)` vaut
`"taire"` partout — un chemin de lecture neuf tait le bail mort sans avoir à y penser —
et `"servir"` dans la seule `DatastorePg.queue`, dont le contrat (écrit dans
`db.rowlock.datastore_claimed_rows`, `DatastorePg.queue` et la capacité
`me.datastore.queue`, et **antérieur** à ce lot) est de rendre le bail *actif OU expiré,
le consommateur tranche sur `_claimed_until`*.

⚠️ **Neutraliser le bail dans le sérialiseur partagé pour tout le monde produirait
l'INVERSE du but visé** : la requête de la file continue de rendre les lignes échues,
mais dépouillées — l'écran les compte alors « sous bail » pendant que son compteur
d'échus tombe à zéro, et le bouton « Libérer le bail » (gaté sur `_claimed_by`)
disparaît précisément sur les lignes qu'il faut libérer. Le témoin qui fige les deux
contrats sur **la même ligne au même instant** est
`tests/test_bail_expire_nest_pas_une_reservation.py::test_la_SUPERVISION_voit_le_bail_echu_que_la_LECTURE_tait`.

Gardes mécaniques : `test_toute_projection_de_ligne_dit_la_FRAICHEUR_du_bail` (les cinq
copies du prédicat restent identiques — un cliquet qui les tient égales vaut une
définition unique) et `test_aucune_comparaison_de_bail_ne_se_refait_en_Python`.

⚠️ **Aucune date d'échéance en dur dans ces bancs**, même lointaine : les baux s'y
posent en **intervalle relatif** (`NOW() + '-18 days'`). Un test qui fige un instant
futur passe jusqu'à la veille du jour où il devient faux, et un relevé statique de ces
dates sur-déclare trop pour servir (77 candidats, 0 vraie) — seule une horloge décalée
tranche.

**Le plafond de reprises : distinguer « ça tourne » de « ça tourne à vide » (#433).**
Depuis que la ligne réservée est liée au run, la conclusion d'un traitement la libère —
c'est le design. Effet de bord mesuré au rodage d'une campagne : un agent qui réserve,
enquête, puis conclut SANS écrire rend sa ligne dans la minute, et le job suivant la
reprend pour refaire le même faux départ. **Deux lignes servies deux fois en dix
minutes, aucune écriture** — et rien qui le dise, puisque les jobs se terminent en
`done`. Un ordonnanceur de flotte ne peut pas borner ça par ligne : il ignore laquelle
l'agent a réservée. **Seul le serveur le sait.**

D'où un compteur porté par la LIGNE (colonne `datastore_rows.claims`, rendue `_claims`) :
il monte à chaque **prise** — `claim_next` comme `claim_row` — et **retombe à zéro à la
première écriture réussie**. Prendre, c'est acquérir une ligne libre ou dont le bail a
lâché ; le titulaire qui **renouvelle** son propre bail ne la prend pas (elle ne lui a
jamais échappé) et ne consomme donc rien : sur une file pilotée à la main, rafraîchir son
écran est le geste le plus banal, et le compter viderait le tableau de ses lignes. C'est cette remise à zéro qui
sépare « reprise après un vrai travail » de « faux départ répété » ; rien d'autre ne
les distingue de l'extérieur.

La garde est **OPT-IN et déclarée**, sur le cycle de vie du champ `role="status"` :

```
lifecycle: {
  states: ["a_traiter", "traite", "echec"],
  transitions: {"a_traiter": ["traite", "echec"], "echec": ["a_traiter"]},
  terminal: ["traite", "echec"],
  max_claims: 3,              # réservations SANS écriture tolérées
  abandon_state: "echec"      # DOIT être un état terminal déclaré
}
```

Les deux clés vont ensemble et se refusent à la pose : `max_claims` sans
`abandon_state` (garde qui ne pourrait pas s'appliquer), `abandon_state` non terminal
(la ligne reviendrait dans la file qu'elle vient de quitter), `max_claims` qui n'est pas
un entier ≥ 1. Ni l'une ni l'autre déclarée = **aucun plafond**, comportement historique.
`data_claim_next` accepte un `max_claims` qui SERRE la déclaration pour une passe (un
ordonnanceur peut être plus strict que le tableau) ; l'état d'abandon, lui, reste une
affaire de schéma.

Au-delà du plafond, le serveur verse la ligne dans `abandon_state`, pose le motif dans
une colonne de plateforme (`abandon_reason`, rendue `_abandon` : « abandonnée après 3
réservations sans écriture, plafond 3 » — le motif **cite ses chiffres**, le plafond
ayant pu changer depuis), libère le bail, et **journalise** (tableau, ligne, compteur).
Deux moments d'évaluation, et deux seulement :

- **au relâchement sans écriture** (`data_release`, et `run_finish` qui libère tout ce
  que le run tenait) — le cas nominal du faux départ ;
- **au claim**, en filet, avant de servir : c'est ce qui rattrape le bail expiré que
  personne n'a relâché (l'agent mort), sans quoi ce chemin contournerait le plafond.

⚠️ Une ligne sous bail **actif** n'est jamais abandonnée : son titulaire travaille
encore, et lui retirer la ligne serait la course que le bail existe pour empêcher.

Une ligne abandonnée **quitte la file quel que soit le filtre du client** : le pick de
`claim_next` exclut `abandon_reason IS NOT NULL`, filet de plateforme indépendant de ce
que l'appelant filtre. Elle reste lisible, et réparable : toute écriture réussie remet
le compteur à zéro ET efface le motif, donc la rouvre. ⚠️ Rouvrir son **statut** suppose
que le cycle de vie déclare la transition de retour (`"echec": ["a_traiter"]`) — la
plateforme verse la ligne dans l'état d'abandon, elle ne s'autorise pas à l'en sortir.

**Un filtre de réservation DÉCLARÉ sur le tableau : `lifecycle.claimable` (#517,
29/08/2026).** Sans lui, `data_claim_next` sert « la plus ancienne ligne dont le bail est
libre ou expiré » — toute ligne du tableau. Mesuré sur un fichier de 8 910 lignes : un
jalon en cible 100 (`lot_test = jalon-100`), le harnais dicte le filtre dans la prose de
l'ordre et l'agent le recopie — à 5 % d'oubli, cinq fiches hors lot par jalon, servies,
écrites, payées. **Une contrainte demandée par la prose n'est pas une contrainte.** D'où
une déclaration à côté du plafond, dans la grammaire de `filter` :

```
lifecycle: {
  …,
  claimable: {"lot_test": "jalon-100", "statut": "a_enrichir"}   # {col: val} ou {col: {op: val}}
}
```

Trois effets, sur les DEUX faces (`data_claim_next`, REST `claim_next` et `claim_row`) :

- **le serveur ne sert jamais une ligne hors de ce filtre, quel que soit le `filter`
  passé** : le périmètre passe DEVANT le filtre de l'appelant, en ET — il resserre, jamais
  n'élargit. Un filtre qui le contredit (`{lot_test: jalon-200}`) ne sert rien ;
- **la réservation ciblée refuse une ligne hors périmètre** (`RowOutsideClaimable` →
  REST **409 `row_outside_claimable`**, `details.claimable` = le périmètre), jugée AVANT
  le bail : sinon `claim_row` serait la porte de côté du périmètre. Le titulaire qui
  renouvelle une ligne sortie du périmètre est refusé lui aussi — la ligne n'est plus
  servie ; elle reste lisible ;
- **une réservation qui ne trouve rien nomme le périmètre** — « aucune ligne libre dans
  le périmètre déclaré `{lot_test: jalon-100, statut: a_enrichir}` — ton filtre `{…}` s'y
  ajoute en ET » — une phrase (`claimable.phrase_vide`) pour les deux faces. Un
  `row: null` nu se lisait « file vide » là où c'était le filtre de l'ordre qui
  contredisait la déclaration.

Validé **à la pose par le moteur qui le servira** (`db.query`, jamais une grammaire
parallèle — `ds_filter_specs`, ex-`core._filter_specs`, y a été déplacé pour ça) :
opérateurs whitelistés, colonnes déclarées sous `strict` (les méta-colonnes `_id`/
`_updated_at`/`_created_at` restent admises), clause inerte (`in: []`) refusée — elle ne
restreindrait rien —, et une valeur du statut hors des `states` refusée : la file serait
vide pour toujours, sans un mot. Annoncé par `enforced` (sonde sur la fonction qui produit
les clauses du pick). La décision, les clauses, le refus et la phrase vivent dans
`datastore/claimable.py` ; `schema.claimable_of` y donne accès depuis un schéma.

`lifecycle` se patche désormais **par fusion** (`merge_lifecycle`) :
`data_patch_schema(fields=[{key: statut, lifecycle: {claimable: {…}}}])` pose le périmètre
sans toucher `max_claims`/`abandon_state`, et `lifecycle: {claimable: null}` le lève.
⚠️ Jusqu'au 29/08/2026, un patch qui nommait `lifecycle` le REMPLAÇAIT en bloc — en
oublier une clé la faisait disparaître sans un mot, la promesse inverse du patch. Ce qui
ne change pas : sans déclaration, `filter` reste le seul périmètre (comportement
historique) ; `abandon_reason IS NULL` reste le filet de plateforme, indépendant des deux.

⚠️ **Et depuis le 05/09/2026 (oto#64), la fusion descend d'un cran de plus dans
`transitions`** : `lifecycle: {transitions: {"perdu": ["en_cours"]}}` ajoute cette
sortie **sans toucher aux autres états**. C'était le même défaut, une couche plus bas —
et il frappait le geste de RÉPARATION : un état terminal n'enferme pas (la sortie se
déclare), mais celui qui la déclarait en ne nommant qu'elle effaçait tout le reste du
cycle de vie, sans un mot. Le retrait devient donc explicite : `transitions: {"perdu":
null}` retire les sorties d'un état, `lifecycle: {transitions: null}` retire la table
entière. La LISTE d'un état, elle, se remplace — c'est l'ensemble de ses sorties, et une
fusion de listes rendrait le retrait d'une destination impossible sans une grammaire de
plus. `claimable` ne descend pas non plus : c'est un filtre entier, dont le remplacement
en bloc est le geste voulu.

**Le refus de transition NOMME la porte** (`refus_de_transition`) : il ne disait que ce
qui est fermé, et l'appelant en concluait qu'il n'y avait pas de sortie — un agent a
préféré ne rien écrire du tout et rendre la main à un humain, sur un tableau qu'une
ligne de schéma aurait rouvert (signal 726). ⚠️ Le patch qu'il conseille porte les
destinations **déjà autorisées plus la nouvelle** : conseiller la nouvelle seule
reformerait, dans le message écrit pour l'éviter, le défaut ci-dessus.

Refus de schéma : `ds_append`/`ds_update_row` traduisent `RowValidationError` en
**400 `row_invalid`** (détail = les champs/transitions fautifs), pas en 500 — c'est le
chemin d'échec d'une annulation (transition de retour devenue illégale).

**Purger une colonne morte (#296 / signal #385).** Un schéma s'ajoute et se remplace,
il ne réduisait pas : retirer un champ le sortait de la vue, mais la clé restait dans
chaque ligne — rendue à la lecture, et acceptée en écriture. Après un renommage
(`actualite_sociale` → `analyse1`), l'ancien nom **décrit le contenu mieux que le
nouveau**, donc un agent qui relit une ligne écrit dedans en croyant viser juste (trois
fois de suite sur une mission, deux analyses sourcées perdues). Le geste manquant :
capacité **`me.datastore.drop_column`** (MCP `data_drop_column`, `rest=None` tant que le
cockpit ne l'affiche pas) → `db.datastore_drop_column` = `data = data - key`, l'opérateur
qui EFFACE là où écrire `null` conserve (une clé nulle reste une clé). Gardes dans le
STORE, donc valables pour toute face future : `confirm=True` obligatoire, refus d'une clé
**encore déclarée** au schéma (un `confirm` ne protège pas d'une faute de nom ;
l'échappatoire est le geste naturel du renommage — retirer le champ du schéma d'abord),
refus des colonnes de plateforme. En amont, `set_schema` **avertit** des colonnes
orphelines (`_orphan_columns_warning`, échantillon de 1000 lignes, strict seulement) : le
piège s'arme à la pose du schéma, c'est là qu'il faut le dire. ⚠️ **La purge n'est pas
sérialisée avec les écritures applicatives** : elle borne son UPDATE aux lignes portant la
clé (`WHERE data ? key` — les autres ne sont pas réécrites), mais un write concurrent fait
un read-merge-write du blob ENTIER (`_merge_into_row`, `SELECT FOR UPDATE` + UPDATE) — si
son SELECT précède la purge et son UPDATE la suit, la clé purgée **revient** sur cette
ligne. Fenêtre étroite et effet bénin (re-purgeable), mais réel : purger quand rien ne
draine le tableau, ou repasser après. Prendre le verrou de ligne dans la purge serait la
vraie réponse, et coûterait un parcours verrouillé de tout le namespace. ⚠️ **La 3ᵉ option du signal
— que `data_rows` cesse d'exposer les clés non déclarées en strict — est écartée** : elle
cacherait des données réelles, alors que le contrat 0016 promet qu'un champ libre
*s'affiche* et que #294 vient de trancher « signaler, jamais refuser ni masquer ». On
supprime la colonne ou on la déclare ; on ne la rend pas invisible.

⚠️ **Une purge qui ne touche rien est un REFUS, plus un `rows: 0` (#680, 01/09/2026).**
Le même zéro valait pour deux vérités opposées — « la colonne existait, aucune ligne ne
la portait » et « ce nom n'est pas une colonne, rien n'a été fait ». Mesuré alors qu'une
purge d'environ 190 noms hors schéma s'apprêtait à partir sur un fichier de production :
elle était inoffensive, mais **personne ne pouvait le savoir avant de l'essayer**, et
l'opérateur aurait coché comme retirés des noms jamais touchés — le contournement étant
de recompter l'inventaire APRÈS coup au lieu de lire les réponses. Le succès porte donc
toujours `rows >= 1` ; l'ambiguïté disparaît au lieu de se signaler. Le refus dit
laquelle des deux choses il constate, et **le diagnostic se fait APRÈS la purge, jamais
avant** : une clé pointée LITTÉRALE au premier niveau du blob (posée par un chemin qui a
contourné la garde d'écriture, cf. #647) *est* une colonne, elle se retire, et refuser
sur la seule forme du nom la rendrait inatteignable. Deux branches, donc :
`site_web.comment` quand `site_web` existe pour de bon (en base — `datastore_has_column`,
prédicat EXACT là où `datastore_row_keys` échantillonne — ou au schéma) est nommé pour ce
qu'il est, une **annotation** servie à plat mais stockée sous sa colonne, avec le geste
qui la retire (`data_write {"site_web": {"comment": null}}`, éprouvé au banc) ; sinon
c'est « aucune colonne de ce nom », sans jamais nommer une colonne porteuse qui
n'existe pas — **une destination inventée est pire qu'une destination absente**.
⚠️ Corollaire au-delà de la purge : une colonne à couches a **trois formes** (écrite,
stockée imbriquée, servie aplatie par `flat_layers`), donc tout contrôle qui compare les
clés SERVIES aux `fields` déclarés compte chaque couche comme une colonne inventée — 304
couches prises pour 304 colonnes fabriquées le 31/08.

**Dire ce que cette version fait respecter (#389).** Quatrième signal du même jour sur
`data_set_schema`, et celui qui rendait les trois autres dangereux : il ne demandait pas
une contrainte de plus, mais de savoir lesquelles MORDENT. Le vrai sujet n'est pas le
vocabulaire, c'est le **décalage de déploiement** — `max_length: 60` posé sur quatre
colonnes d'un tableau de production, code de validation écrit le jour même, version
servie qui ne l'exécutait pas encore. Vérifié à l'époque : un PATCH idempotent rendait
200 ; avec le code à jour, **75 lignes sur 600** devenaient inécritables. Profil de
panne : effet DIFFÉRÉ au prochain déploiement, MASSIF et SIMULTANÉ, cause vieille de
plusieurs semaines — personne ne relie « les agents n'écrivent plus sur ces lignes » à
« quelqu'un a posé une borne un mardi », d'autant que l'erreur porte sur un champ que le
patch refusé ne touchait pas.

D'où **`enforced`** (`dsv2.enforced_keys()`), servi par les DEUX faces du schéma — à la
pose (`set_schema`, donc aussi `patch_schema`) et à la LECTURE (`data_get_schema`), sans
quoi il faudrait écrire un schéma pour poser une question. C'est une propriété du
SERVEUR, pas du schéma : rendue même quand rien n'est déclaré, parce que c'est au moment
où l'on s'apprête à déclarer qu'on veut la connaître.

⚠️ **Le relevé s'établit en FAISANT TOURNER le validateur**, jamais en recopiant une
liste : une sonde par clé = un schéma minimal + une ligne qui le viole, et la clé n'est
annoncée que si `validate_row` refuse ici et maintenant. Une liste parallèle divergerait
le jour où quelqu'un exécute une clé de plus (ou cesse d'en exécuter une) et se mettrait
à mentir dans les deux sens — exactement ce que le signal reproche au silence. Même parti
qu'`interpreted_keys` (dérivé du code), poussé d'un cran : dérivé du COMPORTEMENT, donc
insensible à la façon dont le code est écrit. Les clés dont l'effet est d'ARMER autre
chose portent en plus un **témoin** qui doit PASSER : `strict` n'interdit rien par
lui-même, et sans témoin on l'annoncerait dès que la conformité de type est vérifiée,
c'est-à-dire vrai par accident.

La moitié NÉGATIVE du signal — « `pattern` reçu : stocké mais non appliqué » — était
déjà servie depuis le 13/08 par `unknown_keys_warning` (#316, avec near-miss) et
`options_not_enforced_warning` (#319). `enforced` en est la moitié positive, la seule
qu'un client puisse vérifier contre le serveur qui lui répond.

**Une ligne créée sans la clé métier le DIT (#390, 3ᵉ demande).** Les deux premières
sont servies depuis le 13-15/08 : le bail protège l'ÉCRITURE et pas seulement
l'attribution (`_lease_guard` sous le verrou de ligne, `_assert_writable` sur les gestes
qui n'en ouvrent pas, titulaire reconnu par son RUN ou par `writing_as`), et l'adresse
égarée est traitée (`_id` dans `row` PROMU en adresse de fusion, un `id` nu non déclaré
REFUSÉ en nommant la ligne fantôme). Restait le cas sans adresse du tout : une insertion
franche sur un tableau dont le schéma déclare une clé métier, mais dont la ligne ne la
porte pas. Elle est légitime — un tableau se remplit souvent avant d'avoir sa clé — mais
aucune écriture ultérieure ne la retrouvera, et le lot qui dédouble passera à côté :
c'est la forme résiduelle de l'incident (une 501ᵉ ligne sans SIREN née avec tout
l'enrichissement, sans une erreur). D'où une `notice` sur `append_row`, pas un refus,
sans I/O supplémentaire (la clé est déjà résolue pour la dédup).
⚠️ **Mesuré avant de la poser** : 197 tableaux à clé métier déclarée, 50 024 lignes,
**3** sans clé. L'avertissement ne parlera quasiment jamais — c'est ce qui le rendra
lisible le jour où il parlera.
⚠️ **La 2ᵉ demande du signal — refuser une insertion sans `id` quand des baux sont actifs
sur le tableau — est écartée** : elle imposerait une lecture des baux à CHAQUE insertion,
sur le chemin chaud, pour couvrir un cas que les deux gardes d'adresse ferment déjà.

**`key_required` : un tableau où l'on ne crée pas, on VISE (#516, 29/08/2026).** Le
`notices` ci-dessus signale ; il ne refuse pas. **Un signal dans une réponse qu'un agent
ne consomme pas n'existe pas** — un refus nommé, lui, est lu par construction. D'où un
cran de schéma OPT-IN, `key_required: true`, à côté de la `key` qu'il durcit : sur un
tableau qui le porte, une écriture qui ne désigne **aucune ligne existante** — ni par son
identifiant (`data_write(id=…)`, ou l'`_id` promu depuis `row`), ni par une valeur de clé
que le tableau porte déjà — est **REFUSÉE** (`BusinessKeyRequired` → MCP INVALID_PARAMS,
REST `400 business_key_required`) au lieu de créer une ligne.

⚠️ **« Sans clé » couvre DEUX gestes, et le refus les distingue** — dire « clé requise »
à qui vient d'en fournir une le ferait chercher longtemps :
- **la clé n'est pas renseignée** — le cas du 28/08 : 8 911 lignes pour 8 910 sur un
  tableau de production, une ligne née sans `siren`, contenu bon, doublon
  parfait que rien ne rapprochera ;
- **la clé ne désigne aucune ligne** — le cas du 29/08, plus grave : deux agents refusés
  sur un identifiant INVENTÉ (deux conventions étrangères, aucune n'a la forme d'un `_id`
  d'ici) réécrivent sans identifiant avec un SIREN ; les deux SIREN sont inconnus **du
  registre**, deux lignes naissent, et les fiches affirment « registre — lu via fr_get »
  sur des entreprises qui n'existent pas. **Une clé n'empêche rien tant qu'elle peut être
  inconnue** : c'est cette porte-là que le cran ferme, et rien d'autre ne le pouvait
  (une garde de comptage côté runner ne voit qu'APRÈS).

**Le défaut ne bouge pas** : sans `key_required`, la création reste possible et reste
signalée par le `notices` de #390. Le cran est une déclaration du propriétaire du
tableau, jamais une politique de plateforme — un tableau se remplit souvent avant
d'avoir sa clé. Corollaire assumé : **un tableau fermé ne se peuple plus par écriture**,
`oto_upload_url` compris (il passe par le même `_write_rows_to_ns`) ; pour l'ouvrir,
`data_patch_schema(key_required=false)` — et pour le fermer, `data_patch_schema(
key_required=true)`, sans réécrire le schéma (29/08/2026 : jusque-là seul `set` posait
ou retirait le cran, ce qui obligeait à réécrire un schéma de 80 champs pour une clé de
tête). Il n'y a pas de paramètre d'échappement sur
`data_write` : un bouton « forcer » devient un réflexe et le cran redevient une
étiquette (même parti que l'absence de « forcer » sur le bail, #317).

Deux endroits, un seul par chemin d'écriture : `append_row` (ligne seule, face MCP ET
face REST) et `_write_rows_to_ns` (lot + upload signé) — le refus du lot NOMME la ligne
fautive et ce qui est déjà écrit, comme un refus de schéma (#412). ⚠️ Dans le lot, la
garde se juge sur la clé **déclarée** (celle qui porte l'index UNIQUE), même quand le lot
dédouble sur une autre via `key=` explicite : sinon un tableau fermé refuserait une ligne
qu'il porte déjà. `key_required` sans `key` se refuse **à la pose** (`validate_schema_def`),
et reste inerte s'il traîne dans un schéma déjà en base — un vieux schéma ne doit pas
rendre un tableau inécrivable. Il est annoncé par `enforced` (#389) via une sonde qui
interroge la fonction qui décide : il ne se prouve pas sur une ROW, puisqu'il se juge
contre le CONTENU du tableau.

**Le refus disait une sortie impraticable — corrigé le 02/09/2026 (#668).** Le cran
faisait exactement ce que #516 a voulu ; ce qui manquait était le CHEMIN DE RETOUR. Le
refus ne nommait qu'une sortie — « vise-la par son identifiant » — vraie, et sans objet
dans le cas même qui la déclenche : la ligne n'existe pas, et sur un tableau fermé
encore **vide** il n'y a aucun `_id` à viser. Un tableau fermé ne pouvait alors plus
recevoir sa première ligne par aucune écriture, et rien ne le disait à qui écrivait.
⚠️ **Et la description servie de `data_write` disait le contraire du code** : « WITHOUT
`id` = append a NEW row … UNLESS … a value that **already exists** » — soit, mot pour
mot, la promesse qu'une clé inédite crée. Le commit qui a posé le cran (`b07c0747`)
avait documenté `key_required` dans `data_set_schema`, l'outil qui le POSE, et pas dans
`data_write`, l'outil qui le SUBIT. *Une description d'outil est relue à chaque appel ;
`docs/` ne l'est jamais par un agent.*

Le coût, daté des deux côtés sur la **même** procédure de journalisation de mails :
le 01/09, l'agent refusé relit le schéma, trouve seul la manœuvre —
`key_required=false`, le lot de 47 lignes, `key_required=true` — et c'est de là que
viennent les lignes du tableau ; le 02/09, un autre passage ne la
retrouve pas, essaie les trois formes d'écriture, et s'arrête sur 19 lignes non
journalisées. Même refus, même tableau, deux issues : la différence tenait à ce que le
refus ne disait pas.

Correctif — **la doctrine ne bouge pas, la carte du retour s'affiche** : le refus
(`_refus_de_creation`) nomme désormais les DEUX gestes, viser une ligne existante et,
si la ligne doit vraiment naître, lever le cran par le schéma ; la description de
`data_write` annonce le cran, dit que `data_get_schema` répond, et donne la manœuvre.
Toujours **pas** de paramètre « forcer » sur l'écriture : la sortie passe par le SCHÉMA,
délibérément (un bouton force devient un réflexe, et le cran redevient une étiquette).
Empreinte servie : `data_write` +681 caractères, schémas JSON **inchangés**
(`scripts/empreinte_servie.py --diff`).

**Un refus `required_when` dit OÙ écrire (#545, 29/08/2026).** Mesuré sur le troisième
passage d'une campagne — 105 écritures refusées, lues dans les arguments du journal des
appels : **35 sur 105**, un tiers, ne viennent pas d'une erreur de fond mais de la FORME
d'un champ. Le motif manque, ou il est écrit DANS la colonne énumérée qui le déclenche au
lieu de la colonne de texte libre. L'agent se corrige (27 sur 27 rattrapés au coup
d'après) : **rien ne casse, mais un tiers des écritures sont doublées** — sur 1 778
lignes, quelques centaines d'appels et leurs jetons.

Le refus disait qu'une contrainte n'était pas satisfaite ; il ne disait pas OÙ écrire.
Trois ajouts, tous DÉRIVÉS du schéma :
- **la FORME de la colonne attendue** (`_forme_attendue`) : ce qu'elle accepte — options,
  type, borne, motif — parce qu'un agent qui exécute une procédure écrite par un autre
  n'a jamais lu le schéma ;
- **la condition, en français** : « requis quand `retraitement` vaut a | b | c » au lieu
  du `repr` Python du dict, lisible seulement pour qui connaît déjà le tableau ;
- **le POINTEUR quand la valeur a atterri dans un autre champ de la même ligne** : quand
  la colonne énumérée qui déclenche la contrainte reçoit une chaîne hors de ses options,
  le refus dit « cette valeur va dans `retraitement_motif` (du texte libre, ≤ 300
  caractères), pas dans `retraitement` ». Plus la prévention symétrique sur le refus du
  champ manquant (« ne l'écris pas dans `retraitement` ») — sans elle, la correction
  naturelle est le second refus.

⚠️ **Le pointeur est DÉRIVÉ, jamais deviné** (`_gated_by`) : une colonne est désignée
comme destination parce qu'elle déclare `required_when` **sur** la colonne qui vient de
refuser, pas parce qu'un nom ressemble à un autre. Sans relation déclarée, aucun pointeur
— envoyer écrire dans une colonne qui n'attend rien serait pire que se taire.

Le **code de refus ne change pas** (`row_invalid` côté REST, INVALID_PARAMS côté MCP) :
c'est le texte. Le refus porte en plus `details.expected_column`, rendu par la face REST
dans son enveloppe d'erreur (`AuthzDenied.details`, ADR 0009) — un front pointe alors le
bon champ sans reparser une phrase française, ce qui serait un contrat déguisé. La face
MCP n'a pas d'enveloppe structurée : **le message reste suffisant seul**, et c'est lui
que lit l'agent. Les détails suivent la ligne fautive à travers le lot (`#412`), là où la
reprise coûte le plus cher.

⚠️ **Contre-lecture gardée en tête** (issue) : « le champ est conçu contre le geste
naturel de l'agent, qui écrit le motif à l'intérieur du champ ; c'est le champ qu'il faut
changer, pas l'agent ». Un `retraitement` objet `{valeur, motif}` serait la voie longue.
Le message est la voie courte, prise parce qu'elle tient **sans consigne, sur toutes les
missions** — même famille que le refus d'identifiant qui nomme la forme attendue (#517).

**Contraindre la FORME d'une valeur (#387).** `field.pattern` — jumeau de
`field.max_length`, et il dit ce que la borne ne sait pas dire. Cas mesuré : un champ qui
doit porter une ÉNUMÉRATION de catégories séparées par des points-virgules, pas une
phrase de positionnement ; les longueurs des deux formes se recouvrent (20 à 207
caractères), donc borner à 150 tue les deux et borner à 250 n'attrape rien — **ce qui les
sépare est la structure**. Avant ce lot, `pattern` était accepté sans erreur et jamais
appliqué : le pire des deux mondes, puisque celui qui le pose croit avoir posé un contrat.
Le motif s'applique en `re.search` (donc il s'ancre lui-même : `^…$`), sur les seules clés
que le geste ÉCRIT — même restriction que la borne, et même raison : la validation portant
sur le mergé, une ligne déjà non conforme serait sinon inécritable pour n'importe quel
patch, y compris sur un champ sans rapport. Le refus cite la valeur CONSTATÉE et le motif
attendu. À la pose, `set_schema` avertit des lignes déjà hors motif
(`_offpattern_warning`), et ce verdict se calcule **en Python** sur les valeurs distinctes
rendues par `db.datastore_field_values` : le poser en SQL (`~` de PostgreSQL) ferait
compter par un moteur d'expressions et refuser par un autre, dont les dialectes divergent.

⚠️ **Une expression fournie par un appelant est une arme, et le serveur est mono-loop** :
un motif à explosion combinatoire n'y coûte pas une requête, il coûte le serveur entier —
même famille que la bombe de décompression (`docs/conventions.md`). Un garde purement
SYNTAXIQUE (« pas de groupe quantifié ») ne suffit pas, et c'est une mesure, pas une
intuition : sans un seul groupe ni une seule alternance, `.*.*.*.*.*z` sur 80 caractères
prend 0,75 s et `.*.*.*.*.*.*.*z` sur 60 caractères prend **14,8 s**. Ce qui explose est
le nombre de FAÇONS de découper le sujet. D'où un **budget** calculé sur l'arbre du motif
(`dsv2.pattern_refusal`) : le produit, quantificateur par quantificateur, du nombre de
longueurs qu'il peut prendre — une majoration de l'espace de recherche du moteur, plafonnée
à `PATTERN_BUDGET` (100 000). Il se calcule **contre la longueur du sujet**, ce qui rend
`max_length` OBLIGATOIRE sur un champ porteur de motif (≤ 1 000) : sans sujet borné il n'y
a pas de budget, donc pas de garantie. Sont refusés à la POSE, chacun en nommant sa raison :
la regex invalide, le motif > 200 caractères, le groupe ambigu répété (`(a+)+`, `(a|aa)*`),
la référence arrière, les assertions avant/arrière, et **toute construction que l'analyse
ne reconnaît pas** (fail-closed — un motif accepté par ignorance est exactement le défaut
à éviter). Conséquence assumée : le même motif est accepté sur un champ borné à 250 et
refusé sur un champ borné à 1 000, et une grammaire structurée (`^[^;]+(;[^;]+)*$`) est
refusée — l'interprétation métier d'une valeur n'est pas le métier d'oto.
⚠️ L'analyse emprunte le parseur de la stdlib, `re._parser` (3.11+) ou `sre_parse` (3.10,
**la version de la box**) : les deux chemins sont exercés au banc, et l'absence des deux
refuse tout motif plutôt que de laisser passer.
⚠️ **Aucun `pattern` n'existait en base au moment de la pose du garde** (inventaire du
28/08 sur les 210 schémas de production : 185 `max_length`, 0 `pattern`) — ce lot ne peut
donc geler aucune ligne existante. Un motif hérité qui ne passerait pas le garde reste
INERTE à l'écriture (`pattern_of` est muette, comme `max_length_of` sur une borne mal
formée) mais fait REFUSER la prochaine pose du schéma : c'est là qu'on peut encore corriger.

**Les champs que l'appelant n'écrit pas (#586, #606, #607 ; 29/08 → 01/09/2026).** Trois
crans de colonne sous UNE garde (`dsv2.reserved_refusals`, le geste dans
`datastore/reserves.py`), pour trois gestes mesurés sur la même campagne contre la
donnée remise par le client — l'écraser, détruire sa copie de secours, et graver une
déclaration à la place d'une trace. Même hiérarchie que #516 : le chemin n'existe pas >
la machine refuse > un contrôle détecte > la consigne interdit ; jusqu'ici un contrôle
de fin de passage détectait après coup.

- **`readonly: true` — la colonne du fichier source.** Mesuré au 5ᵉ passage : **quatorze
  valeurs sur douze fiches par cent** (`adresse` ×9, `naf` ×3, `date_creation` ×2)
  écrasées à l'exact — l'agent « complète » la colonne avec ce que dit le registre —,
  **onze sans aucune couche de récupération**. Le cran verrouille la VALEUR (déballée,
  `unwrap`) : une écriture qui la CHANGE est refusée en nommant la colonne, la raison et
  **où va la chose** — pas une colonne de report séparée, mais **la couche `comment` de la
  colonne elle-même** : `adresse` garde la valeur de la cliente, `adresse.comment` reçoit
  « registre — 20 B AVENUE … ». C'est la seule forme qui reste attachée au champ, se
  compte et se livre ; quatre fiches sur quatorze la pratiquaient déjà, en écrasant la
  valeur en plus. **Les couches restent donc ouvertes** (`comment`, `link`, et `origine`
  sauf si le système la pose) — la garde sait les séparer de la valeur parce que la
  fusion les a toujours distinguées (`_merge_column`). **Le refus ne porte que sur un
  CHANGEMENT de valeur** : nommer la valeur (nue, `null`, ou `{"valeur": …}`) d'une ligne
  en place avec une valeur différente est refusé ; **une valeur identique n'est pas une
  écriture** — no-op silencieux, couches préservées, et `{"valeur": <identique>,
  "comment": …}` écrit le comment (c'est le geste utile). Ce que le cran ne ferme PAS,
  et c'est dit : la **création** d'une ligne (rien n'est écrasé ; un tableau qui ne doit
  pas grossir se ferme par `key_required`), et un **vide non-`null`** sur une valeur en
  place (#608 l'écarte avant la garde).
  ⚠️ **La colonne-clé ne se pose pas en `readonly` (29/08/2026).** `siren` figure dans
  CHAQUE écriture pour désigner la ligne : elle se protège par `key_required` (« une
  autre valeur est une autre ligne »), et la pose est **refusée** à `set_schema`/
  `patch_schema`. Un schéma déjà en base qui la porterait n'est pas fermé, puisque
  l'identique passe.
  ⚠️ **Deux erreurs datées du 29/08/2026, dans la même journée.** (1) **v1.165.0, trou
  éprouvé sur copie jetable** : l'identique passait (« rien n'a changé ») et la règle « une
  valeur nue réécrite emporte ses couches » **détruisait `adresse.comment`** — un agent qui
  réémettait sa fiche avec l'adresse inchangée effaçait la divergence qu'il venait
  d'écrire. (2) **L'erreur d'une heure (#623)** : la réparation a d'abord REFUSÉ l'identique
  (« l'agent n'a aucun cas où réécrire est utile »). Huit charges d'écriture échantillonnées
  sur le terrain, toutes : le geste dominant **réémet la fiche entière**, valeurs
  verrouillées comprises (`{"valeur": <identique>, "origine": <la même>}` plus vingt
  colonnes d'enrichissement) — chaque fiche aurait été refusée, **une flotte à l'arrêt, pas
  un garde-fou**. La vraie réparation est au **substrat** : partout, une valeur IDENTIQUE
  à celle en place est un no-op qui préserve `comment`/`link`/`origine` (§ Sous-champs) ;
  le refus, lui, ne porte que sur un changement. Et sur une colonne `origine: "system"`,
  une `.origine` égale à ce que le système poserait (l'origine stockée, sinon la valeur
  de base en place, à la création la valeur écrite) est acceptée comme no-op — seule une
  valeur différente est refusée. Prouvé par les quatre appels du terrain contre
  PostgreSQL (`test_champs_reserves_live.py::test_terrain_*`).
  `details.expected_column = "<colonne>.comment"` pour la face REST (#545) ; le code ne
  bouge pas (`row_invalid` / INVALID_PARAMS), c'est le texte qui enseigne.
- **⚠️ Écrire l'origine se DÉCLARE, à partir du 1er octobre 2026 (oto#70 lot 2).** Ce
  qui est refusé n'est pas l'écriture, c'est le **silence** : `origine_override=true`
  sur l'appel (les deux faces) et elle passe ; sans lui, elle est refusée par un message
  qui nomme les deux issues — écrire la valeur seule, ou déclarer. **Rien à demander à
  personne**, aucun droit à provisionner : le paramètre engage celui qui l'envoie, et
  c'est tout (décision d'Alexis, 05/09/2026 : « c'est notre modèle d'agent experience »).
  La date vit dans le code (`ORIGINE_REFUS_LE`) pour que ce qui est annoncé soit ce qui
  refuse, et le réglage `OTO_ORIGINE_REFUS_LE` la déplace sans déploiement. **Avant cette
  date**, l'écriture passe et la réponse porte `origine_warning` — la seule annonce faite
  aux écrivains, aucun envoi ne partira. **Un relevé** (`origine_ecritures`, une ligne
  par écrivain × tableau × colonne) compte les deux populations séparément : le journal
  d'appels ne peut pas dire qui écrit une COUCHE (il ne garde que les clés de premier
  niveau et tronque les arguments), et les compteurs déclaré/non-déclaré sont ce qui
  distinguera, après la date, l'écrivain qui s'est adapté de celui qui a disparu.
  **La manœuvre « lever le format, écrire, remettre » ne rouvre rien** : la garde
  regarde ce que l'appelant écrit, pas ce que la colonne déclare (mesuré). En revanche
  `data_drop_column` et `data_delete_row` **emportent** l'origine — rien n'y écrit une
  couche, le paramètre n'a donc rien à y déclarer, et déclarer une destruction ne la
  rendrait pas réversible ; ces deux portes attendent le verrou humain.

- **`origine: "system"` — la copie de secours posée par la plateforme.** Sur 41 fiches
  portant une couche `<champ>.origine` censée conserver la valeur remise, **une** l'a
  réécrite avec la valeur nouvelle (un homonyme adopté comme raison sociale, recopié
  dans l'origine) : la couche était écrite par l'agent, donc destructible par lui, et
  c'était l'unique copie. Désormais, à la **première écriture qui change la valeur**, la
  plateforme écrit `<champ>.origine` = la valeur d'avant, **une seule fois, jamais
  réécrite** ; et **toute écriture de `<champ>.origine` par un appelant est refusée** —
  `{"origine": …}`, `{"valeur": …, "origine": …}`, `{"origine": null}` —, **création
  comprise** (une origine posée à la création marquerait « déjà posée » avec la valeur de
  l'agent : la porte de côté du défaut). Une valeur inchangée ne pose rien (la colonne
  reste plate) ; un champ VIDE au départ reçoit `""`, le marqueur « rien n'avait été
  remis » — sans lui la deuxième écriture capturerait la première valeur de l'agent
  comme si elle venait du client (`flat_layers` ne sert pas une couche vide : à la
  lecture, « vide à l'origine » et « jamais modifié » se confondent, et c'est juste —
  dans les deux cas il n'y a rien à rétablir). **Compatible avec l'existant** : une
  couche déjà écrite par un agent avant la pose reste lue telle quelle, jamais réécrite.
  ⚠️ **À l'EFFACEMENT, le marqueur vide ne survit pas — corrigé le 03/09/2026 (signal
  #695).** Un `null` nommé laissait `{"origine": ""}` : une enveloppe sans valeur, qui
  n'est plus une valeur d'énumération valide et rend la ligne **invisible au filtrage et
  aux facettes**. Mesuré sur trois lignes remises à zéro — quatre champs sur quatre,
  exactement ceux qui portaient une couche `origine` ; les champs texte nullés au même
  appel n'avaient pas ce résidu. On le découvrait en relançant un patch de schéma et en
  lisant son avertissement, jamais autrement. Le marqueur `""` **qualifie une valeur** :
  quand la valeur s'en va, il ne reste rien à qualifier. Une origine **pleine**, elle,
  survit à l'effacement — c'est le point de départ, parfois l'unique copie de la valeur
  remise.
  ⚠️ **Et le vide ne se lit qu'à l'effacement, jamais au cas général.** Une première
  correction traitait `""` comme vide partout : elle faisait tomber le marqueur dès la
  RÉÉCRITURE, et la deuxième écriture aurait alors capturé la première valeur de l'agent
  comme si elle venait du client — exactement le défaut que ce marqueur existe pour
  empêcher. C'est un banc existant qui l'a attrapée, pas une relecture.
  ⚠️ **La capture est PARESSEUSE, pas à la pose du schéma** : un format ne vaut que pour
  l'avenir et ne réécrit aucune ligne (doctrine de `_overlong_warning` et consorts) — et
  elle rend la MÊME valeur, puisque rien n'a bougé entre la pose et la première
  modification. Refusé à la pose sur un composite ou un `json` (la capture rangerait
  l'objet entier dans la couche — ⚠️ **motif reformulé le 2026-09-01, #728** : il
  invoquait l'exemption `json` de la grammaire des couches, qui ne vaut plus pour
  l'adresse ; c'est la pose AUTOMATIQUE qui ne s'y déclare pas, l'annotation elle-même
  s'y écrit à la main), sous un sous-record, sur une cible de couche ; se combine avec
  `readonly` (valeur verrouillée ET couche d'origine fermée — la pose n'a jamais lieu
  tant que la valeur ne bouge pas, et joue le jour où le propriétaire lève `readonly`).
- **`system: "<source>"` — la VALEUR posée par la plateforme (#607, 01/09/2026).** Une
  colonne `modele` que l'agent remplissait de mémoire dérivait : `…2407` sur une fiche,
  `…2511` sur une autre le lendemain, quand les 102 travaux enregistrés du run disaient
  tous `…2512`. *Une valeur recopiée de mémoire est une déclaration, pas une trace.* Le
  cran est le frère d'`origine: "system"` d'un cran plus haut : là la plateforme pose une
  COUCHE une seule fois, ici elle pose la valeur de base **à chaque écriture**, sans que
  l'appelant nomme la colonne — et toute écriture de l'appelant dessus est refusée en
  nommant la source. Sources FERMÉES : `run.id`, `run.started_at`, `write.at`.
  **Hors run, rien n'est posé et le refus reste** : une estampille devinée serait la
  déclaration de mémoire qu'on remplace, avec le sceau de la plateforme en plus.
  ⚠️ **`run.model` est REFUSÉ à la déclaration, et le refus dit pourquoi.** La source que
  la demande visait n'existe nulle part côté serveur — `runs` n'a pas de colonne `model`,
  `run_start` n'en reçoit pas, `runner_jobs.result` n'en porte pas, et le handshake ne
  connaît qu'un nom de CLIENT (`claude.ai`, `Claude Code`), qui n'est pas un modèle. La
  seule valeur disponible serait celle que l'appelant en dit : le cran aurait blanchi la
  déclaration de mémoire au lieu de la remplacer. Ce qui est servi à la place est le
  **pointeur** — `run.id` sur la ligne, et ce que le run sait se lit au run : *une valeur
  qu'on rejoint ne dérive pas, une valeur qu'on recopie dérive.* Rouvrir `run.model`
  demande d'abord une colonne `runs.model` et un point qui l'écrit ; c'est un lot, et il
  traverse le contrat du runner.
  **Une valeur identique reste un no-op**, comme pour ses deux sœurs — et il en faut
  DEUX ici : celle qu'on s'apprête à poser (l'agent réémet l'estampille courante) et
  celle DÉJÀ en base (une fiche lue sous le run A, réémise sous le run B — c'est notre
  propre lecture qui revient). Refusée : celle qui ne vient d'aucune des deux.
  Refusé à la pose sur un composite/`json`, sous un sous-record, sur la **clé métier**
  (la plateforme déciderait de l'identité des lignes, et chaque écriture viserait une
  ligne neuve) et **avec `readonly` sur la même colonne** : l'un dit « ne change
  jamais », l'autre « reposée à chaque écriture » — ensemble l'un des deux ment, et le
  schéma ne dit pas lequel. `run.started_at` lit `runs` une fois par run (cache borné,
  même parti que `run_org`, la colonne étant immuable) ; `run.id` et `write.at` ne
  coûtent aucune I/O.
  ⚠️ **La pose n'entre pas dans les clés « écrites » que voit la validation** : la borne
  de longueur et le motif se jugent sur ce que l'APPELANT pose, et un refus portant sur
  une valeur qu'il ne contrôle pas serait inactionnable.

⚠️ **Le cran borne TOUT LE MONDE PAR DÉFAUT, faces humaine et REST comprises — et c'est
dit.** Le store ne sait pas distinguer un agent d'un humain : il connaît un sub et une
org, et le run n'est pas obligatoire sur toute écriture ; une exemption par défaut serait
un trou (un agent hors run passerait).

⚠️ **Amendement du 02/09/2026 (#658) — `readonly` seul, et il faut dire pourquoi le
parti d'avant est tombé.** La sortie du propriétaire était le schéma :
`data_patch_schema(fields=[{"key": "adresse", "readonly": false}])`, écrire, refermer —
deux gestes délibérés, même parti que le bail (#317) et `key_required` (#516) ; il n'y
avait pas de « forcer » sur `data_write`, et le refus n'enseignait pas comment lever le
cran, au motif qu'un bouton nommé dans un refus devient un réflexe. **Ce parti a été
mesuré sur `key_required`, et il ne tient pas.** Sur la même procédure, deux jours de
suite (#668) : le 01/09 l'agent refusé retrouve seul la manœuvre et la rejoue deux fois
sur deux tableaux — il referme ; le 02/09 un autre passage ne la retrouve pas et
s'arrête. *Il suffit qu'une exécution s'interrompe entre « lever » et « remettre » pour
que le verrou reste ouvert sans que personne le sache* — et une colonne déverrouillée ne
produit **aucun** signal. Un cran qu'on doit ouvrir pour écrire est donc plus dangereux
que le forçage qu'il voulait éviter.

Ce qui le remplace, en trois pièces (`datastore/forcage.py`) :

- **`readonly_override=true` SUR L'APPEL** (`data_write` en MCP ; paramètre de query sur
  `POST`/`PATCH …/rows` en REST — le corps y EST la ligne). Il vaut pour cet appel et
  rien d'autre : rien à rouvrir dans le schéma, donc rien à refermer, rien à oublier.
- **Le palier : propriétaire du tableau ∪ qui le gouverne** (`ownership.owns` ∪
  `ownership.can_govern`). Les deux ensembles se croisent sans s'inclure — un membre de
  l'org propriétaire possède sans gouverner, un gérant (ADR 0048) gouverne sans posséder
  — d'où l'union. ⚠️ Ce qui reste dehors est le tiers à qui le tableau a été PARTAGÉ en
  écriture : il écrit, il ne force pas. Sinon quiconque peut écrire pourrait lever le
  verrou, ce qui est la définition d'une colonne ouverte. Le palier est lu **une fois par
  appel et hors de toute transaction** — l'évaluer sous le `FOR UPDATE` du verrou de
  ligne prendrait une seconde connexion du pool en tenant un verrou. Zéro SQL de plus sur
  le chemin nominal : sans demande, ou sans colonne `readonly` déclarée, rien n'est lu.
- **Le refus nomme le geste**, dans les deux sens : sans le paramètre, il dit comment
  forcer et à qui c'est ouvert ; avec le paramètre mais sans le palier, il dit qui peut,
  et ne renvoie pas l'appelant au paramètre qu'il a déjà passé.
- **La trace est au journal des appels**, clé `readonly_forced` (ligne, colonne, valeur
  remplacée), à côté du `sub` que le journal stampe déjà — face MCP par
  `session_org.note_call_trace` + l'allowlist `server._TRACED_ARGS`, face REST par
  `calllog.log_rest_call(forced=…)` (hors `args`, qui stringifierait la liste). ⚠️ **Le
  journal ne remonte qu'à ~35 jours** : la trace disparaîtra alors que la valeur forcée
  restera. Arbitré en connaissance de cause le 02/09/2026 — pas de colonne de plus sur la
  ligne.

## `required_layers` — la valeur n'arrive pas sans sa provenance (oto#75, 06/09/2026)

**Le cinquième cran de la famille, et celui qui n'existait qu'en apparence.** L'attribut
était posé dans **trois schémas de production** et n'avait **aucun lecteur** : accepté en
silence, servi dans le contrat, sans le moindre effet. Quelqu'un avait écrit ce qu'il
voulait obtenir, la plateforme l'avait pris, et personne ne s'en était aperçu. Ce lot lui
donne son lecteur.

```json
{"key": "qualification", "type": "text", "required_layers": ["comment"]}
```

Une écriture qui laisse une **valeur non vide** dans cette colonne sans y poser la couche
est **refusée**, en nommant la colonne et **en disant où écrire** :

> `qualification: valeur posée sans \`comment\` — cette colonne exige que la valeur
> arrive AVEC sa provenance. Écris-la en couches, dans le MÊME appel :
> `"qualification": {"valeur": <ta valeur>, "comment": "…"}` (\`comment\` = d'où vient la
> valeur, en clair). Poser la valeur seule au tour suivant emporterait la couche. Une
> valeur vide, ou une couche posée sans valeur, ne déclenche rien.`

**Ce que la garde ne fait pas.** Elle n'empêche pas un commentaire **faux**. Elle oblige à
**nommer une source**, ce qui rend le mensonge vérifiable ; la vérité reste à la relecture
sur pièces.

**Ce qui la déclenche, et ce qui ne la déclenche pas :**

- une valeur nulle, vide, ou une **couche posée seule sans valeur** ne déclenche rien —
  c'est la forme légitime d'une remarque ;
- la portée descend dans les composites : sous-champs d'un `object`, et sous-champs des
  éléments d'une `list` (une liste réémise remplace l'ancienne **en bloc**, couches
  comprises — c'est là que la perte est la plus lourde). Un sous-champ fautif se nomme
  **une fois** pour toute la colonne, sur le premier élément qui le porte ;
- elle ne s'applique **ni** à `readonly`, **ni** à `system`, **ni** à la colonne qui porte
  le cycle de vie : exiger une provenance de qui n'écrit pas la valeur refuserait des
  écritures que personne ne peut corriger ;
- sur une colonne `origine: "system"`, la couche `origine` est **retirée** de l'exigence —
  la plateforme la pose elle-même et REFUSE que l'appelant la nomme.

**⚠️ La restriction qui évite le gel, et le piège qu'elle a failli reproduire.** La garde
ne juge que les colonnes que **le geste nomme** (`written`), comme `max_length` et
`pattern`. Sans elle, un patch sur une autre colonne serait refusé pour une couche absente
ailleurs — le gel silencieux d'oto-backend#284, sous un nom neuf.

Mais `written` ne contient **que des clés de premier niveau**, et c'est mesuré : une borne
déclarée sur une clé **pointée** (`qualification.comment`, #377) n'y figure jamais, donc
elle ne refuse rien sur un patch — **pas même sur le patch qui écrit la colonne**. C'est
l'état actuel de `pattern`/`max_length` posés sur une couche : ils ne mordent qu'à
l'insertion. La garde de ce lot se restreint donc par la colonne de **base**, et le banc
exerce les deux côte à côte (`tests/datastore/test_datastore_required_layers_oto75.py`).

**Armée par sa PROPRE déclaration**, jamais par `validation_active` — comme le cycle de
vie. `validation_active` arme la validation *entière* (types, requis, composites fermés) :
l'élargir ferait basculer dans ce régime, du jour au lendemain, les tableaux qui portent
déjà l'attribut, sur des règles qu'ils n'ont jamais demandées.

**Une déclaration illisible est refusée à la POSE** (`["commentaire"]`, `"comment"` nu,
liste vide) : une couche que la plateforme ne connaît pas n'exigerait rien, et son auteur
croirait la provenance exigée — la faute exacte que l'attribut a commise pendant trois
schémas. Le **lecteur**, lui, reste muet sur une déclaration illisible déjà en base : un
vieux schéma ne doit pas faire exploser une écriture (même parti pris que `max_length_of`
/ `pattern_of`).

**Les deux faces, prouvées par une épreuve chacune.** Le seam d'écriture est unique
(`_check_row`), mais « unique » est une lecture de code : l'avertissement voisin sur les
clés de schéma non reconnues ne sort que côté REST alors que la description de l'outil
promet le contraire. Le banc joue donc `data_write` (face outil, `McpError`) **et** la
route `POST /rows` / `PATCH /rows/{row_id}` (face REST, `400 row_invalid`), et vérifie que
**rien n'est écrit** dans les deux cas.

**Ce lot livre la capacité et ne l'active nulle part** : `required_layers` n'est posé sur
aucun tableau. Restent les barreaux 2 (un motif sur le CONTENU d'une couche) et 3 (lier la
présence d'une couche au contenu d'une autre) — oto#75.

## `agent_access` — à qui une colonne est servie (oto#83, 06/09/2026)

**Le quatrième cran de la même famille, et le premier qui soit CIBLÉ.** Les trois
précédents bornent tout le monde ; celui-ci ferme une colonne **à l'agent seul** —
l'écran de son propriétaire continue de la voir et de l'écrire, à l'identique.

**Le fait qui l'a motivé.** Une colonne de suivi commercial d'un tableau client,
déclarée modifiable, dont la description dit « Où en est VOTRE démarche auprès de cette
entreprise. À vous de le renseigner ». Servie telle quelle à un agent, cette phrase
s'adresse à LUI : un agent a posé un statut de clôture sur un prospect avant tout
contact, sans aucune source. Le schéma savait dire qu'une colonne est verrouillée
(`readonly`), jamais **pour qui** — entre « le client la modifie » et « personne ne la
modifie », il n'y avait rien.

Un attribut, au premier niveau, trois valeurs, la valeur inconnue REFUSÉE à la pose :

| `agent_access` | schéma servi à l'agent | ligne servie/réservée | écriture d'agent |
|---|---|---|---|
| absent / `"write"` | la colonne | la colonne | acceptée — le défaut d'avant |
| `"read"` | la colonne | la colonne | **refusée** sur la VALEUR si elle change ; `.comment`/`.link` ouverts ; identique = no-op |
| `"none"` | rien | rien (ni couches, ni alias plats) | **refusée**, quelle que soit la forme |

⚠️ **Trois valeurs et pas un booléen** parce que le diagnostic porte sur deux crans
distincts — « pas éditable par l'agent » et « pas même montrée ». Un `hidden_from_agent`
les aurait soudés : impossible ensuite de servir en lecture une colonne qu'un agent doit
CONSULTER pour décider sans avoir le droit de la réécrire.

⚠️ **La valeur inconnue est refusée à la pose**, contrairement au vocabulaire des CLÉS
qui reste ouvert (on signale, on n'empêche pas). Un `agent_access: "non"` retomberait en
silence sur le défaut et le propriétaire croirait sa colonne fermée : c'est mot pour mot
la plaie de `read_only` écrit pour `readonly`, à ceci près qu'ici on peut la fermer.
⚠️ **La clé métier ne se ferme pas** — elle figure dans chaque écriture pour désigner la
ligne. Refusé à la pose, ET écarté à la lecture (`acces_agent._cles_par_acces`) pour les
schémas antérieurs au cran, où l'attribut n'était qu'une clé transportée.

**« Un agent » = la FACE, et rien d'autre.** Un appel entré par un tool MCP est piloté
par un modèle ; les routes REST `/api/*` servent le dashboard, les fronts tiers et les
scripts du client. C'est la seule chose que le serveur sache de l'appelant sans la tenir
de lui. La face est posée par `CallContextMiddleware` (donc sur CHAQUE instance MCP,
l'anonyme comprise), lue par `acces_agent.appel_d_agent()`, et **passée au store en
paramètre** : le store ne devine toujours rien, on le lui DIT.

⚠️ **Pourquoi PAS `_run_id`**, qui était le candidat naturel : il est déclaré par celui
qu'on juge (le runner le pose en `setdefault`, un modèle qui envoie le sien gagne ; le
chemin des Conversations ne peut rien injecter du tout), et **rien ne le pose sur la
face REST** — un `or run` dans le prédicat serait une branche inerte. Un agent sans run
ouvert est donc reconnu, et c'est justement le cas dangereux.

**Où le masquage porte** — trois goulots, jamais une liste de surfaces :
`core._row_to_dict` (toute ligne servie : `data_rows`, `data_claim_next`, l'écho de
`data_write`, la file, `data_app`, `oto_node_rows`), `schema_ops.get_schema` +
la sortie de `set_schema` (`data_get_schema`, `data_patch_schema`, l'index de
`data_app`), et `core._entry` (le catalogue de `data_list_namespaces`). Le store, lui,
continue de lire le schéma ENTIER par `_schema_of` : masquer à la validation ferait
passer l'écriture de l'agent comme un champ hors schéma.

**Le réglage ne se rouvre pas depuis la face agent** (`acces_agent.refus_de_schema`,
câblé dans `set_schema`, donc aussi dans `patch_schema` qui y repasse) : un agent ne
pose, ne change ni ne retire `agent_access` — jugé sur le DELTA ancien→nouveau, pour
qu'un patch qui TRANSPORTE le réglage passe —, et il ne repose pas un schéma entier sur
un tableau réglé (`set_schema` REMPLACE, et il n'en voit qu'une partie : le refus nomme
`data_patch_schema`). Sans ce cran la capacité serait décorative — la manœuvre « lever,
écrire, refermer » de #658/#668 n'aurait même pas eu besoin de refermer.

⚠️ **Ce que le masquage N'EST PAS, et ce qu'il ne couvre pas.** Une colonne masquée
n'est pas supprimée : la valeur reste, l'écran la lit et l'écrit, les exports du
propriétaire la voient. Il porte sur ce qui est **servi**, pas sur ce qui est
**interrogé** — un agent qui connaîtrait le nom peut encore filtrer, trier ou agréger
dessus, et la recherche plein texte (`oto_search`, qui sert un extrait du JSONB brut
sans passer par `_row_to_dict`) balaie toutes les valeurs. Non couvert non plus : la
page web publique (`share_ui.render_data`, qui lit `db.datastore_list_rows` en direct —
c'est la publication du client, pas une face agent), et **un agent qui atteint la face
REST avec la clé de son propriétaire** : sur REST, la clé du client et l'écran du client
sont le même porteur. Fermer ce dernier axe demande une identité d'agent — la brique
existe à moitié (`token_kind ∈ {user, delegation}`, frappé à la réservation d'un job
runner) mais elle est jetée à la vérification du jeton MCP.

`null` lève un cran comme
une clé de champ ordinaire, et **la levée ne touche aucune ligne** : une origine posée
reste. `enforced` annonce `readonly`, `origine` et `agent_access` par une sonde qui interroge la fonction
qui décide (comme `key_required`, elles ne se prouvent pas sur une ROW seule). **Cinq
chemins d'écriture, une garde** : création (ligne seule, lot, upload signé — le même
`_write_rows_to_ns`), fusion sous verrou, patch par `id`, remplacement (où une colonne
readonly absente du corps compte comme changée). ⚠️ **Pas dans le registre des jetons
(#602)** : celui-ci juge AVANT la résolution, sans schéma ; un champ réservé est une
propriété du TABLEAU et se juge là où le schéma est connu. Les deux se complètent —
jeton mal placé : « il s'écrit dans tel champ » ; champ réservé : « il ne s'écrit pas,
voici où va la chose ».

⚠️ **Ce paragraphe a dit le contraire jusqu'au 2026-09-01.** Il annonçait « #607 reste à
son issue : la pose y lit le RUN à chaque écriture — une I/O sur le chemin chaud —, ce
que cette garde n'accueille pas sans grossir ». **Les deux moitiés étaient fausses** :
le run de l'appel est une ContextVar (`_current_run`, aucune I/O), et la seule source
qui touche la base (`run.started_at`) se cache derrière un cache par run, la colonne
étant immuable. La conclusion « c'est un autre lot » reposait donc sur un coût supposé,
jamais mesuré. *Une réserve de perf qu'on n'a pas mesurée est une opinion qui prend
l'autorité d'un fait en étant écrite ici.*

**Retoucher un schéma sans le détruire (#388).** `data_set_schema` REMPLACE — bon geste
pour POSER un format, piège pour l'ÉDITER : deux appels indiscernables (même méthode,
même succès, même réponse) n'ont pas le même effet selon que l'appelant a patché en
mémoire ou reconstruit la liste des champs. Mesuré en une journée sur un même tableau :
un patch a préservé 78 notes de champ, une reconstruction a détruit un `pattern` et un
`max_length`, 52 notes ont disparu entre deux sessions. Un avertissement n'aurait rien
changé — personne ne lit un avertissement sur un appel qui réussit —, d'où un geste qui
ne PEUT pas détruire : capacité **`me.datastore.patch_schema`** (MCP `data_patch_schema`).
`fields` = **fusion par clé** (`dsv2.merge_fields`, récursive dans les composites
déclarés — patcher un sous-record ne détruit pas ses sous-champs ; l'ordre existant n'est
jamais rebrassé, il pilote le rendu) ; `remove` = le **retrait explicite**
(`dsv2.remove_fields`), pendant OBLIGÉ de la fusion — sans lui on troquerait la
destruction accidentelle contre l'impossibilité de nettoyer, et une clé inconnue y est
REFUSÉE (un `remove` avalé sur une faute de frappe ferait croire au nettoyage) ;
`strict`/`key`/`key_required` = les clés de tête, inchangées si omises (`key_required`
y entre le 29/08/2026, #516 : il ne se posait que par `set`) ; les crans de CHAMP
`readonly` / `origine: "system"` (#586/#606) se posent et se lèvent par `fields`, `null`
levant sans réécrire ni toucher les lignes. Le résultat repasse par
`store.set_schema`, donc par ses gardes (doublons de clé métier, index UNIQUE,
`key_required` sans `key` — poser `key` et `key_required` dans le même patch passe) et
ses avertissements — la logique n'est pas doublée. ⚠️ `key_required=false` ÉCRIT `false`
au lieu de retirer la clé : `key_required_of` lit la valeur, et le relevé d'effacement
ci-dessous compte les disparitions de tête sans exception — retirer la clé ferait crier
un geste explicite sur lui-même. ⚠️ `remove` sort le champ du
**SCHÉMA** ; effacer la **COLONNE** des données reste `data_drop_column`.

⚠️ **Et la POSE dit désormais ce qu'elle efface (28/08, remède A du même signal).** Le
geste qui ne peut pas détruire ne suffisait pas : `set_schema` reste la bonne façon de
POSER un format, et sa réponse ne disait rien de ce qu'elle emportait. Le point exact du
signal est que **le mode d'écriture était indétectable côté appelant** — la même session,
le même jour, sur le même tableau : sa migration a PRÉSERVÉ 78 notes de champ (elle
patchait le schéma relu en mémoire), son remappage en a DÉTRUIT deux (il rebâtissait la
liste), même méthode, même succès, réponse identique. Il fallait connaître son propre
code pour savoir ce qu'on venait de perdre, ce qui est hors de portée d'un agent qui
exécute une procédure écrite par un autre. C'est la forme exacte du défaut corrigé le
27/08 sur les LIGNES (`valeurs_effacees`), et il reçoit le même remède : toute réponse de
pose porte `declarations_effacees` + `declarations_effacees_hint`
(`dsv2.declarations_effacees`/`_report`). Trois natures dans un seul relevé — un champ
RETIRÉ (`retire: true`), les déclarations perdues sur un champ survivant (une note, une
borne, des options), et les clés de TÊTE (`key`, `strict` — ce qu'on perd en premier est
la clé métier et son index UNIQUE partiel). Le relevé descend dans les sous-records
(`contacts[].email`, même convention de chemin que `unknown_declaration_keys`) et ne
répète pas les enfants d'un champ déjà relevé comme retiré. **Les VALEURS y sont**, pas
seulement les noms : après la pose, la réponse en est la seule copie — bornes de rendu
20 entrées et 300 caractères par valeur, au-delà la TAILLE (projeter n'est pas tronquer).
Seules les DISPARITIONS comptent : réécrire une note est un geste qui se nomme lui-même.
⚠️ `patch_schema` passe son `remove` en `retraits_annonces` : le geste explicite ne crie
pas sur lui-même, mais **le filet reste tendu pour tout le reste** — c'est le seul moyen
de voir une fusion qui laisserait échapper quelque chose. ⚠️ Conséquence : `set_schema`
RELIT le schéma en place avant de le remplacer (un `SELECT` de plus sur un geste rare) —
un banc qui stubbe l'écriture doit désormais stubber aussi `_ns_of`.

**Écrire hors du format se DIT, sans être refusé (#294).** Sur un namespace `strict`,
un nom de champ que le schéma ne déclare pas est accepté (contrat 0016 : un champ libre
s'affiche, il ne débloque rien) et la valeur persiste — mais dans une colonne hors
format, que l'interface et tout ce qui s'appuie sur le schéma ignorent. Un humain relit
sa colonne et voit le vide ; un agent reçoit un accusé de réception et passe à la ligne.
Toute réponse d'écriture porte donc `hors_schema` + `hors_schema_hint`
(`dsv2.off_schema_keys`/`off_schema_warning`, relevé par le SEAM `DatastorePg._check_row`
— par lequel passent append/batch/merge/upsert/patch — et servi par
`store.off_schema_report()` aux **trois** surfaces : `data_write`, REST append/patch,
et la matérialisation d'upload signé). Le relevé porte sur les clés que le geste POSE
(pas sur le mergé, même raison que la borne `max_length` : une colonne hors format déjà
en base ne doit pas ré-alerter à chaque patch), il agrège un lot en une entrée par
chemin (`contacts[].tel`), et il est **vide hors strict** — là, le champ libre est un
droit explicite du contrat, pas une anomalie. Refuser franchement aurait été plus net,
mais aurait cassé cette liberté : ce qui manquait était un signal, pas une barrière.

**…et depuis le 01/09/2026, un tableau peut demander la barrière (#614/#678).** Le
signal a tenu un an et il a une limite mesurée : `strict` **promettait** un refus et
livrait un signalement, si bien qu'on a cessé de surveiller ce qu'on croyait gardé —
douze clés inventées en vingt-deux occurrences au dernier relevé, dont trois dans des
fiches clientes, et 162 colonnes hors schéma accumulées dans un fichier de production.
*Une option qui promet plus qu'elle ne fait est pire qu'une option absente.* La réponse
n'est pas de fermer le premier niveau — ce serait retirer un droit du contrat 0016 et
casser l'exploration d'un tableau non encore typé — mais de le **paramétrer** :
`unknown_fields` en clé de tête, `"report"` (le défaut, tout ce qui précède) ou
`"reject"`. En `reject`, la colonne non déclarée est **refusée**, rien n'est stocké, et
le refus nomme la colonne, le référentiel (borné à 15 noms) et le fait qu'**aucune
colonne déclarée ne porte ce nom** — jamais une destination approchante : *une
destination inventée est pire qu'une destination absente*, et une colonne inconnue n'en
a aucune par construction.

- **Clé distincte plutôt que `strict: "refuse"`** : `strict` est lu comme un BOOLÉEN à
  cinq endroits (`validation_active`, `off_schema_keys`, `validate_row`,
  `claimable.erreurs`, `_orphan_columns_warning`) — en changer le type ferait mentir
  chaque lecture existante, en silence, et sur le chemin chaud.
- **Le refus vit au même seam que le relevé** (`_check_row`) et partage son prédicat
  (`_unknown_subkeys`) : le rapporteur et le refuseur ne peuvent pas diverger sur ce
  qu'est « hors du référentiel ». Il couvre donc les six portes d'un coup, upload signé
  compris.
- **Il juge ce que le geste POSE, jamais le mergé** — c'est ce qui le rend tenable : un
  tableau qui porte déjà 162 colonnes hors schéma reste écrivable, et un patch sur une
  colonne sans rapport ne se fait pas refuser pour un défaut hérité (la faute de #284,
  sur une autre règle).
- **Un cran qui ne pourrait pas s'appliquer est refusé à la POSE** : `reject` sans
  `strict` (rien n'est relevé hors strict — il ne parlerait jamais) ou sans aucun champ
  déclaré (tout serait hors schéma — le tableau serait inécrivable d'un coup). Les deux
  extrêmes du même trou, et refuser d'être inerte est la moitié du lot : reproduire en
  le corrigeant le défaut qu'on corrige serait le comble.
- Il se pose par `data_patch_schema(unknown_fields="reject")` — un tableau se ferme
  quand il a FINI d'être exploré, donc quand son schéma est long, et le poser par `set`
  obligerait à réécrire quatre-vingts champs pour une clé de tête. `enforced` l'annonce.

**…mais DANS un composite déclaré, `strict` REFUSE (#544, 29/08/2026).** La liberté
qu'on protège au premier niveau n'existe pas un cran plus bas, et c'est toute la
différence : une clé inconnue en tête de ligne crée une vraie **colonne**, que
l'interface affiche et qu'on peut déclarer après coup — c'est ce qui permet d'explorer
un tableau avant de le typer. Dans un `object.fields` ou un `list.of.fields`, il n'y a
pas de « sous-colonne libre » : la déclaration EST le seul référentiel, l'attribut
serait stocké là où ni le schéma, ni l'interface, ni l'export à plat (§5.3 de
`datastore-colonne-tableau.md`, dont les colonnes se dérivent de `of.fields`) ne le
lisent. Sur un tableau `strict`, un attribut non déclaré est donc **refusé**, en
nommant l'élément — `contacts[1].email_pattern`.

*Le fait qui l'a montré* : un tableau `strict: true` a accepté deux fois, sur un rejeu
de nuit, une clé `email_pattern` **à l'intérieur** d'un contact — sans refus, sans
`hors_schema`, sans un mot ; la veille, le même geste au premier niveau avait été
interdit **par consigne**. « Une interdiction protège la forme qu'elle décrit ; le même
geste reparaît là où le texte ne regardait pas. » Le `strict` est précisément ce qui
doit rendre la prose inutile.

Quatre bornes, toutes voulues :

- **`strict` seul ferme** — un tableau non strict ne change pas de comportement, même
  quand la validation est armée par ailleurs (un `required` suffit à l'armer) ;
- **ce que le geste RÉÉCRIT seulement** — la fermeture ne descend que dans les
  composites nommés par l'écriture. Sans cette restriction, une ligne portant déjà un
  attribut hors format deviendrait inécritable pour n'importe quel patch, y compris sur
  un champ sans rapport : le gel de 23 lignes d'oto-backend#284, à ne pas rejouer ;
- **une liste dont le `of` ne déclare aucun champ reste LIBRE** — sans référentiel,
  rien n'est hors référentiel, et c'est la même règle qu'au premier niveau (un schéma
  strict sans aucun field ne relève rien) ;
- **une COUCHE n'est pas un attribut** — la forme servie d'un item aplatit ses couches
  (`email.origine`), donc un aller-retour lecture → écriture les repose telles quelles.

Le refus et le signal partagent **un seul prédicat** (`dsv2._unknown_subkeys`) : deux
définitions du « hors référentiel » finiraient par diverger, et c'est l'appelant qui
paierait la différence. Conséquence à connaître : sur un tableau `strict`, les chemins
**imbriqués** ne sortent plus dans `hors_schema` — le refus arrive avant le relevé.
`hors_schema` garde le premier niveau, qui est le seul endroit où la colonne libre est
un droit. ⚠️ Le refus est **borné comme le relevé** : un attribut inconnu est nommé une
fois par colonne-liste, sur le premier élément qui le porte — 300 contacts fautifs ne
rendent pas 300 lignes de refus.

**Ce que l'écriture VIDE se dit aussi (13/08, #407/#408/#409).** Ne pas nommer un champ
le laisse intact ; le nommer avec `null` l'EFFACE. Deux gestes différents, et le second
est indiscernable d'un `None` de sérialisation dans un payload — variable non peuplée,
gabarit à demi rempli, aller-retour de lecture. Toute réponse d'écriture porte donc
`valeurs_effacees` (`{ligne, champ, valeur}` — la valeur PERDUE, sans quoi il n'y a
rien à rétablir) + `valeurs_effacees_hint`, relevé dans les deux chemins qui fusionnent
(`update_row`, `_merge_into_row`) et servi par le même `off_schema_report()`. Bornes :
20 effacements nommés, une valeur rendue jusqu'à 300 caractères puis remplacée par sa
TAILLE. Le geste reste PERMIS — vider une valeur fausse n'a pas d'autre porte.
⚠️ **Erreur d'attribution à connaître** : trois signaux du 13/08 accusaient l'écriture
partielle d'avoir mis à `null` un champ *qu'elle ne nommait pas*. Le journal des appels
dit l'inverse (deux appels consécutifs du journal `tool_calls`, même ligne) — huit
minutes plus tôt, la même session avait écrit `row={'moteur': None, …}` ligne par ligne. La règle du merge
tenait ; c'est le silence qui a fait chercher le défaut au mauvais endroit.

⚠️ **Une chaîne vide n'est PAS une valeur (28/08, #608) — et l'annoncer ne suffisait
pas.** Un client a perdu un signal de recrutement daté parce que son lot de sourcing
portait `best_signal: ""` dans son **gabarit** de ligne : un gabarit s'écrit une fois et
se réutilise sur toutes les lignes, donc un champ vide dans un gabarit était un vecteur
de perte à CHAQUE merge. La valeur n'a été rétablie que grâce à `valeurs_effacees`.
La cause tenait à une contradiction interne : `_is_empty` (le validateur) traite `""`,
`[]` et `{}` en **absence** — pas de contrôle de type, et « champ requis manquant » sur
un champ requis — pendant que `_merge_column` les traitait en **valeur** et les laissait
écraser. Tranché pour l'absence : **un vide non-`null` ne DÉPLACE jamais une valeur, il
ne peut s'écrire que là où il n'y a rien.** C'est ce que rend une source muette, pas une
demande d'effacement ; `null`, lui, ne se fabrique pas tout seul dans un gabarit.
La règle est volontairement étroite — là où la colonne était déjà vide, le geste passe
tel quel, donc **créer** une ligne depuis un gabarit ne change pas de comportement — et
elle se DIT : `valeurs_ignorees` + `valeurs_ignorees_hint`, clé distincte de
`valeurs_effacees` parce que les valeurs qu'elle nomme sont **encore en base**. Un
seul parcours (`datastore_columns.arbitrer_les_vides`) rend le payload corrigé et les
deux relevés : les faire diverger serait rejouer le défaut. Une écriture en couches qui
pose `{"valeur": "", "origine": …}` garde son origine — écarter la valeur vide n'emporte
pas ce qui l'accompagne (#326). Un refus dur a été écarté : 8 897 cellules à chaîne vide
sur 59 tableaux en production le 28/08, plus 5 643 listes vides sur 11 — les refuser
rétroactivement casserait des tableaux qui n'ont rien demandé.

**Une valeur refusée s'ÉCARTE, la fiche s'écrit (03/09, #667).** Le refus de schéma
partait en bloc : une seule sous-valeur hors des options déclarées, et tout l'appel
repartait. Mesuré le 02/09 sur une vague de 40 écritures d'agents — **8 rejets, dont 5
pour ce seul motif**, chacun emportant une fiche entière (effectif relevé au registre,
convention collective vérifiée, interlocuteurs trouvés, qualification rédigée et
sourcée), soit ~60 000 jetons déjà payés à repayer. Le refus reste légitime — la colonne
du cas déclare `__non_conserve__`, une exigence de confidentialité du client — mais pas
sa PORTÉE : l'agent n'a pas commis une faute de structure, il a rangé une donnée publique
dans un champ que le schéma ferme. **Le verrou doit protéger la donnée, pas détruire le
reste.** D'où la **sixième** clé du relevé, `valeurs_ecartees` (`{champ, motif,
valeur_rejetee}`) + son `hint` : ni en base (à la différence de `hors_options`), ni
détruite (à la différence de `valeurs_effacees`) — jamais entrée. Le geste vit dans
`datastore/ecartes.py`, décidé au seam `_check_row`.

⚠️ **Quatre gardes bornent l'écartement, et chacune ferme un trou.** *(1)* Les refus sont
TOUS des valeurs hors options : un requis manquant ou une réservation ambiguë portent sur
la COHÉRENCE de la ligne, et leur rejet total reste juste — les écarter écrirait une fiche
fausse. *(2)* Le champ fautif est posé par CE geste : amputer un patch d'une valeur qu'il
n'a pas écrite serait un effacement silencieux de la base. *(3)* La ligne amputée REPASSE
la validation entière — retirer une valeur peut en défaire une autre (une colonne-
aiguillage écartée cesse de rendre requis ce qu'elle gardait), et sans second tour on
écrirait une ligne incomplète sur la foi d'un contrôle qui n'a pas vu sa forme finale.
*(4)* Il RESTE quelque chose à écrire : quand la valeur fautive est tout ce que le geste
pose, l'amputer ne sauve aucune fiche — elle en crée une **vide**, sous un `ok`. Le motif
du lot est de préserver un travail déjà fait ; là où il n'y en a pas, refuser reste juste.
Cette borne-là a été rappelée par le banc du régime strict (#319), pas par un raisonnement
— **deux bancs existants sur quatre gardes** : une règle neuve se mesure contre ce qui est
déjà gardé avant de se croire complète.

⚠️ **Et une valeur MAL RANGÉE n'est pas une valeur à jeter.** Quand le schéma déclare une
destination pour elle (#545 — la colonne qui se dit requise par celle qui refuse), le
refus dit OÙ l'écrire et 27 agents sur 27 se corrigent : l'écarter écrirait une fiche qui
prétend ne pas avoir été qualifiée, sous un `ok: true`. *Perdre du travail coûte cher ; en
corrompre en silence coûte plus cher.* La première rédaction de ce lot écartait
uniformément — c'est le banc de #545 qui l'a attrapée, pas un raisonnement.
⚠️ **Pas de réglage par tableau**, demande explicite du signal : un cran de plus à tenir
est un défaut qui ne se voit qu'en production, quand il est mal posé.

⚠️ **Un vide qui ne pose RIEN d'autre est REFUSÉ, et le refus écrit la porte (#724,
01/09/2026).** Entre 04:16 et 04:20 ce jour-là, dix `data_write(id=…,
row={'contacts': []})` sur des fiches clientes : dix `200`, zéro retrait, découverts en
relisant les fiches.

**La porte existait, et la réponse la nommait déjà** : `contacts: null` efface, et
`valeurs_ignorees_hint` disait mot pour mot « Pour vider un champ pour de bon, nomme-le
avec `null` ». Elle n'a pas été empruntée — il n'y a eu **qu'une seule** écriture `null`
explicite ce jour-là, sur une table d'**essai** jetable, jamais sur les fiches ratées,
dont l'une porte encore le contact qu'on voulait retirer. C'est la réfutation de « il
suffit de le dire » : **un témoin logé dans le corps d'une réponse réussie n'oblige
personne à le lire.** Le refus, lui, ne se rate pas, et il arrive au moment où
l'appelant peut encore corriger.

**Deux options ont été écartées, et savoir pourquoi évite de les rejouer.**

*Faire effacer la liste vide, comme `null`* : détruit la charge dominante, où `[]` veut
dire « rien trouvé ». *Faire effacer la liste vide SEULE* : ferait dépendre un geste
**destructeur** de ses voisines — « selon le contexte ta donnée disparaît » est une
perte silencieuse, quand « selon le contexte ton appel échoue » est un désagrément qui
enseigne. La contextualité d'un refus ne coûte pas ce que coûte celle d'un effacement.

**La mesure qui fonde tout ça** — journal de production, 30 j glissants au 2026-09-01
(`tool_calls` croisé avec `datastore_rows`) :

| mesure | valeur |
|---|---|
| écritures unitaires `data_write` | 43 444 |
| … portant une **liste vide** | **574** (chaînes vides : 296, population distincte) |
| … qui **réémettent la fiche entière** | **562 — 98 %** |
| … qui portent le **vide seul** | **12 — 2 %** |
| couples (appel, colonne) résolubles en base | 324 |
| … visant une colonne **encore peuplée** | **105 — 32 %**, dont **104 réémissions** |

⚠️ **Trois réserves, et la troisième est une déduction, pas une mesure.** Les arguments
journalisés sont tronqués à 300 caractères par valeur : les listes vides des longues
fiches réémises sont coupées, donc **sous-comptées**. 250 couples n'ont pas pu être
résolus (`id=@claimed`, ou identifiant de table absent des arguments). Enfin, la valeur
« en place » est relue **aujourd'hui** : une colonne vidée depuis se présente comme
« rien à perdre », ce qui a d'abord fait conclure que le vide seul ne visait qu'**une**
colonne peuplée. C'est faux. Neuf des dix lignes du 01/09 portent aujourd'hui une valeur
nulle alors qu'aucune liste vide n'y a jamais été écrite — si elles avaient été vides à
l'instant de l'appel, elles porteraient `[]`. **Par quel geste elles sont passées à nul,
le journal tronqué ne le dit pas ; ce qui est établi par élimination, c'est qu'elles
étaient peuplées quand les appels ont échoué.** Le cas réel n'est donc pas « un appel
par mois » mais neuf en quatre minutes.

**La règle, portée par `datastore_columns.refuser_geste_sans_effet`** (appelée par
`update_row` et `_merge_into_row`, donc les deux chemins, #322) : la ligne de partage
est l'**effet du geste**, pas le type de la valeur.

- l'écriture **pose autre chose** (fiche réémise, gabarit à demi peuplé) : inchangée —
  la valeur en place survit et c'est dit (#608). **Cette branche protège les 104** ;
- l'écriture **ne pose plus rien** : refus, et le message **écrit** `{"champ": null}`
  en toutes lettres plutôt que de s'en remettre à un relevé.

Conséquence structurelle : une row de **lot** porte toujours sa clé métier (c'est par
elle que la fusion l'a trouvée), donc elle pose — un import de 500 lignes est par
construction une réémission et ne peut pas casser sur ce refus. C'était l'objection qui
avait fait écarter un refus dur en #608 ; elle ne s'applique pas ici.

**Batch write + clé métier (2026-07-03).** `data_write` accepte un LOT `rows` (list[dict])
écrit en un appel — importer un dataset sans faire transiter chaque ligne par le contexte
du LLM. Un namespace peut déclarer une **clé métier** au schéma (`schema.key`, ex.
`"email"`/`"siren"` ; cf. `data_set_schema`) : toute écriture qui porte cette clé fait alors
un **UPSERT (merge)** sur elle au lieu de dupliquer (param `key` explicite prioritaire) — les
rows sans clé sont appendées. Renvoie `{inserted, updated, count, key, ids}`.
⚠️ **La fusion par clé n'est PAS réservée au lot** (vérifié le 28/08 sur table jetable, et
la doc servie disait le contraire jusqu'au 29/08) : `data_write(row={siren: X})` **sans
`id`**, sur un tableau qui déclare `key: "siren"` et où X existe, met à jour la ligne
existante et rend son identifiant — `append_row` applique la même dédup que le batch
depuis #109 ch.3. Croire l'inverse fait écrire en lots de un pour obtenir une fusion, ou
pire, fait chercher un `id` qu'on n'a pas. Cœur : `store.write_rows` →
`_write_rows_to_ns(ns_id, rows, key)` (keyé par ns_id → réutilisable **hors contexte d'org**)
+ `db.datastore_find_row_id_by_key` (lookup dédup JSONB paramétré). Pour du **volumineux**,
préférer `oto_upload_url(target='datastore')` (push NDJSON/CSV out-of-bande → même batch-upsert ;
ns_id scellé au mint, autz réappliquée via `ownership.can_access(datastore_namespace, write)`).
Cf. `docs/projects.md` §push out-of-bande (issue #105).

⚠️ **Un lot N'EST PAS atomique, et son refus le dit (#412).** Il s'arrête à la première
ligne que le schéma refuse ; celles d'avant sont écrites et le RESTENT. Le refus nomme
donc la ligne autant que le champ — index dans le lot, valeur de la clé métier — et
combien de lignes ont atterri, parce que c'est ce qui décide de la reprise (rejouer le
lot entier re-fusionne les premières, ou les duplique sans clé métier). Vécu sur un
import de 8 910 lignes par lots de 200 : une adresse sans arobase dans le fichier
client, et le coût n'était pas les 199 lignes perdues avec elle mais le temps de la
retrouver. `DatastorePg._designation_de_lot` + `RowValidationError(row=…)` — le refus
garde sa CLASSE (les surfaces s'en servent pour choisir leur code), seule sa
désignation change.

Auth :
- MCP tools : Logto JWT comme les autres tools.
- REST `/api/datastore/*` : Logto JWT **ou** API token long-lived (préfixe
  `oto_`, vérifié contre `user_api_tokens`).

OAuth Google per-user (Gmail + Tasks ; scopes Sheets/Drive latents pour l'export
#29 — **plus requis par le datastore**, ADR 0016 ; **multi-compte**) :
- `GET /api/google/oauth/start` (Logto auth) → renvoie `{auth_url}` à
  ouvrir dans le browser. `prompt=consent select_account` → l'user choisit
  quel compte Google connecter (rejouer le flow ajoute un 2e compte).
- `GET /api/google/oauth/callback?code=…&state=…` — Google redirige ici, on
  échange, dérive l'email du compte via le profil Gmail, persiste, puis
  redirige vers `app.oto.ninja/?datastore=connected`.
- `GET /api/google/oauth/status` → `{connected, accounts:[{email,is_default,scopes,granted_at}], …}`.
- `POST /api/google/oauth/default` body `{account}` → choisit le compte par défaut.
- `DELETE /api/google/oauth[?account=<email>]` → révoque un compte (ou tous).
- Scopes : `spreadsheets` + `drive.file` + `gmail.modify` + `tasks`.
- Multi-compte : dans le coffre `connector_credentials` (connector='google',
  `account=email`, `is_default` dans meta). Les tools `gmail_*`/`tasks_*`
  sans param `account` utilisent le compte par défaut (cf. `db.set_google_oauth`,
  `docs/connector-vault.md`).
- Refresh token **chiffré** (`secret_enc`) dans le coffre. access_token reste en
  clair dans `meta` (bearer ~1h, dérivé).

**Pourquoi un client OAuth séparé du connecteur Logto Google** : Logto
gère l'**identité** (scopes `openid email profile`), pas la délégation
d'accès aux ressources Google. Donc deux clients OAuth distincts dans le
même projet GCP — séparation propre identité ≠ délégation.

⚠️ **Conséquence de l'ajout de Gmail** : `gmail.modify` est un scope
**restricted** Google (contrairement à `drive.file`, non-sensible). Tant que
l'écran de consentement est en mode *Testing* (test users only), pas de
contrainte. S'il passe en *published/external*, Google impose un audit
sécurité annuel (CASA). Le flow étant unifié, **tout** user qui connecte
Google pour le datastore se voit aussi demander l'accès Gmail. Choix assumé
(substrat unique vs deux flows séparés).

## Sous-champs d'une colonne (#318, #322, #326)

**Toute colonne a des sous-champs** — ce n'est pas une forme que certaines valeurs
adoptent, c'est le contrat. Une colonne « plate » est simplement une colonne dont les
sous-champs sont vides. Vocabulaire FERMÉ, source unique dans `datastore/schema.py` :
`valeur` (la colonne elle-même) + trois couches, `origine` · `comment` · `link`.

**Le nom nu rend toujours la VALEUR.** `row["email"]` rend un e-mail, provenance ou
pas — sans quoi tout consommateur casserait, silencieusement, le jour où quelqu'un
pose une source. Les couches renseignées s'ajoutent à plat sous `champ.couche`, et
s'atteignent comme des colonnes : `{"field": "email.origine", "op": "empty"}` répond à
« quelles valeurs n'ont pas de provenance ? », qui est ce qui sépare une provenance
vérifiable d'une provenance décorative. Pas de `COALESCE` sur une couche : sur une
colonne scalaire elle est NULL, et c'est la bonne réponse.

**Deux formes de lecture — `layers` (oto#53).** L'écriture est imbriquée, la lecture par
défaut est plate : c'est l'asymétrie de oto#47 (un client qui relit `row["champ"]` en
attendant la forme qu'il a écrite conclut que sa couche a disparu). `layers=nested` sur
`data_rows` (MCP) et sur `GET …/rows` / `GET …/rows/{id}` (REST) rend une cellule à
couches comme elle s'écrit, `{"valeur": …, "origine": …, "comment": …, "link": …}` —
`valeur` toujours (`None` quand seule une provenance est posée), les couches seulement
renseignées ; une cellule sans couche reste un scalaire ; dans une colonne-liste, la même
règle un cran plus bas (`item["email"]["link"]`). Toute autre valeur est refusée en
nommant le paramètre (`invalid_layers`). Mise en forme dans `datastore/layers.py` ; le
défaut (`layers.DEFAUT`) y vit seul et les deux faces le recopient — palier 3 : bascule
vers `nested` avec préavis daté et double-service ; d'ici là `flat` reste le défaut, et
l'épreuve « le défaut reste flat » tombe seule si quelqu'un le bouge. Les réponses
d'écriture (`data_write`, `POST/PATCH …/rows`), `queue`, `claim_next`, `data_app` et la
vue nœud restent plates : l'option ne couvre que les deux lectures nommées.

**La table reste MIXTE pour toujours** (personne ne réécrira les lignes existantes) :
tout lecteur adressé par champ passe donc par `db.field_value_sql` /
`field_read_sql` — filtres, tri, agrégats, clé métier, contrôles de schéma — et aucun
ne recopie l'expression. L'index d'unicité de clé métier est un index d'EXPRESSION :
il doit matcher la chaîne du lookup au caractère près, d'où le littéral échappé plutôt
qu'un paramètre sur ce seul chemin.

**Écrire : l'écriture ne touche QUE ce qu'elle nomme** (`_merge_column`). Une règle,
dont découlent les deux défauts payés :

| écriture | effet |
|---|---|
| `{"champ": Y}` (ou `null`) | valeur posée/effacée, **origine intacte** |
| `{"champ": {"valeur": Y}}` | idem |
| `{"champ": {"origine": X}}` | **valeur intacte**, origine posée |
| `{"champ": {"origine": null}}` | origine effacée ; ne reste que la valeur ⇒ colonne à nouveau plate |
| `{"champ": {"origine": X}}` sur un champ `origine: "system"` | **refusé** — la plateforme la pose (#586) |
| `{"champ": X}` avec X **identique** à la valeur en place | **no-op : toutes les couches restent** (29/08/2026 — le round-trip relire → repousser porte la valeur nue, il ne doit rien détruire) |
| `{"champ": Y}` sur un champ `readonly: true` | **refusé** si Y change la valeur ; identique = no-op ; `{"champ": {"comment": …}}` passe (#606) ; `readonly_override=true` force, pour cet appel, si l'appelant possède ou gouverne le tableau (#658) |
| `{"champ": Y}` sur un champ `agent_access: "read"` | **refusé depuis la face MCP** si Y change la valeur (création comprise) ; identique = no-op ; `{"champ": {"comment": …}}` passe. Depuis la face REST : accepté, le cran est inerte (oto#83) |
| `{"champ": Y}` sur un champ `agent_access: "none"` | **refusé depuis la face MCP**, quelle que soit la forme et même à l'identique — la colonne ne lui est pas servie, il ne peut pas la tenir d'une lecture. Depuis la face REST : accepté (oto#83) |
| `{"champ": {"origine": X}}` sur un champ `origine: "system"`, X = ce que le système poserait | accepté, no-op (29/08/2026 — le geste dominant du terrain) |

`comment` et `link` décrivent la valeur : quand elle CHANGE sans qu'ils soient
renommés, ils tombent avec elle — les garder ferait affirmer une provenance fausse ;
quand elle ne change pas (`{"valeur": <identique>, "comment": …}`), ils s'écrivent et
le reste demeure.
`origine` décrit le point de départ, elle survit. C'est une protection contre
l'ACCIDENT, pas contre l'intention : un geste explicite remplace ce qu'il vise.
⚠️ **Une valeur identique n'est pas un changement** (29/08/2026, trou de v1.165.0) :
la lecture sert la valeur nue et met les couches à côté (`flat_layers`, `champ.couche`),
donc un round-trip fidèle repousse forcément la valeur nue — et « réécrire emporte
`comment`/`link` » la détruisait au passage. Le jugement est au TYPE près (`0` n'est pas
`False`). Jusque-là, un client qui annotait une colonne source perdait l'annotation à
la fiche suivante, sans un mot.

**Asymétrie lecteur/écrivain** : une couche inconnue est IGNORÉE à la lecture (un
déploiement progressif ne doit pas perdre ce qu'un nœud plus récent a écrit) et
REFUSÉE par son nom à l'écriture (une couche mal orthographiée s'apprend tout de
suite, pas six semaines plus tard). ⚠️ Un dict qui mêle une couche connue et une clé
inconnue reste une donnée `json` métier — arbitré en #329.

⚠️ **Une colonne déclarée `json` S'ANNOTE** (2026-09-01, #728). Elle ne s'annotait pas,
et le refus **mentait sur ce qu'il avait regardé** : « `X` n'est aucune colonne de ce
tableau : ni dans cette écriture, ni sur la ligne visée, ni au schéma » — alors que le
geste écrivait `X` deux clés plus haut et que le schéma la déclare. L'exemption `json`
(l'objet métier ne se réinterprète pas) portait aussi sur l'ADRESSE : l'annotation
n'était jamais rangée, restait pointée, et tombait dans le refus réservé aux colonnes
introuvables. Asymétrie qui a mis la puce à l'oreille : `effectif.comment`, colonne
scalaire, passait dans la MÊME écriture. **L'exemption protège le CONTENU de l'objet,
pas le droit d'annoter la colonne** — le lecteur ne l'a jamais exemptée (`flat_layers`
sert `X.comment` pour toute colonne), donc l'écriture doit reprendre ce qu'il sert.
Restent exempts : le contenu de l'objet, et la garde des couches mixtes ci-dessus.
Une seule ambiguïté en découle, et elle se tranche sur la VALEUR : un objet métier qui
porte un champ nommé `comment` est servi comme s'il portait l'annotation, donc une
réémission renvoie les deux formes — même valeur ⟹ c'est la lecture qui revient, on ne
touche à rien ; valeur différente ⟹ refus qui nomme les deux.

**Le blob lu en TEXTE** (recherche plein-texte, extrait, embedding) est reconstruit
avec les valeurs à la place des enveloppes (`ROW_VALUES_TEXT_SQL`), sinon `q=hunter`
matcherait toute ligne dont l'e-mail VIENT de Hunter. Gardé par un `jsonb_path_exists`
mesuré : ×6,4 si systématique, ×1,5 sur une table sans couches.

## Interroger PLUSIEURS colonnes à la fois (oto#22 barreau 1)

Une notion vit souvent sur des colonnes numérotées (`contact1_fonction`…). Un filtre
peut viser plusieurs colonnes **déclarées par l'appelant** — le serveur n'interprète
jamais un motif de nom :

```jsonc
filters: [{"fields": ["contact1_fonction","contact2_fonction","contact3_fonction"],
           "op": "in", "value": ["DRH","DAF"], "match": "any"}]
```

`match` : `any` (défaut, une colonne suffit) ou `all` — et `all` n'est pas la négation
d'`any` : « aucun rang n'a de contact » (`empty` + `all`) ne s'obtient pas en niant
« au moins un rang en a ». Une métrique d'agrégat porte sa propre condition (`where`,
même grammaire) : le total et la sous-population dans la MÊME requête, donc un taux
sans recouper deux appels. `group_by` accepte une liste — les valeurs sont mises en
commun, `count` compte les occurrences et `count_rows` les fiches.

Surfaces : `data_rows(filters=…)`, `data_aggregate(filters=…, metrics=[{…, "where":…}],
group_by=[…])`, et le même `filters` côté REST.

## Setup GCP (one-shot, par projet)

1. **Console GCP** → choisir/créer un projet (peut être le même que celui
   qui héberge le connecteur Logto Google).
2. **APIs & Services → Library** : enable
   - `Google Sheets API`
   - `Google Drive API`
   - `Gmail API`
3. **APIs & Services → OAuth consent screen** :
   - User type : `External` (sauf Workspace)
   - App name : `Oto Datastore` (visible aux users sur le consent)
   - Support email : <l'adresse de support du projet>
   - Authorized domains : `oto.ninja`
   - **Scopes** : `.../auth/spreadsheets`, `.../auth/drive.file`,
     `.../auth/gmail.modify`, `.../auth/tasks`
   - **API à activer** : ajouter aussi `Google Tasks API` dans APIs & Services → Library
   - **Test users** (si en mode "Testing") : ajouter les emails autorisés
     tant que l'app n'est pas publiée. ⚠️ `gmail.modify` est un scope
     **restricted** → en mode Testing c'est OK, mais publier l'app en
     External imposerait un audit sécurité CASA annuel (cf. section OAuth
     ci-dessus). `drive.file` reste non-sensible ; c'est Gmail qui ajoute
     la contrainte.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** :
   - Application type : **Web application** (pas "Desktop")
   - Name : `oto-mcp datastore`
   - Authorized redirect URIs — le backend émet
     `{OTO_MCP_PUBLIC_URL}/api/google/oauth/callback` ; cette URL **exacte** doit
     figurer ici, sinon Google renvoie « requête invalide » (redirect_uri_mismatch).
     Depuis le cutover ADR 0040 (2026-07-06) le client est **partagé prod + preprod**,
     déclarer les deux :
     - `https://mcp.oto.cx/api/google/oauth/callback` (**PROD** — `mcp.oto.cx` depuis le cutover)
     - `https://mcp.oto.ninja/api/google/oauth/callback` (**PREPROD** — ex-prod avant le cutover)
     - `http://localhost:9103/api/google/oauth/callback` (dev, optionnel)
5. Copier `client_id` + `client_secret` → SOPS.
6. Générer le state secret :
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

## Env vars requises

À poser dans le `.env` systemd (ou SOPS exporté au boot) :

- `GOOGLE_DATASTORE_CLIENT_ID` / `GOOGLE_DATASTORE_CLIENT_SECRET` — issus
  de l'étape 5.
- `OTO_MCP_OAUTH_STATE_SECRET` — étape 6, HMAC anti-CSRF du state.
- `OTO_MCP_PUBLIC_URL` — déjà utilisée pour Logto (base du redirect URI).
- `OTO_APP_URL` (optionnel, défaut `https://app.oto.ninja`) — base où on
  redirige l'user après le callback OAuth. À override en dev local
  (`http://localhost:5174`).

Bootstrap d'un token CLI (pour Alexis) :
```bash
ssh -i ~/.ssh/<clé> root@<box> \
  "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.issue_token <SUB> cli"
# → imprime un `oto_…` à stocker dans SOPS comme OTO_API_KEY
```

## Découpé par COUTURES depuis le 13/08 (#325)

**Découpé par COUTURES depuis le 13/08 (#325)** — le fichier est l'unité d'occupation
d'une session sur un tree partagé, et quatre chantiers ont dû entrer dans les trois
mêmes fichiers en une semaine (gels en série, un incident de tree). Où poser un lot :

| module | ce qu'il porte |
|---|---|
| `db/paths.py` | désigner une valeur : `email` · `email.origine` · `contacts[0].email` · `contacts[].email` |
| `db/query.py` | construire filtres/tris/agrégats — **PUR**, ne touche jamais une connexion |
| `db/rowlock.py` | le bail d'une ligne (file de travail) |
| `db/rowabandon.py` | le plafond de reprises : quand la file cesse de tourner à vide |
| `db/datastore_ns.py` | le TABLEAU : existence, nom, propriété, partages |
| `db/datastore.py` | les LIGNES : CRUD + clé métier/index |
| `datastore/errors.py` | les refus — **aucune dépendance**, importable de partout |
| `datastore/columns.py` | la colonne côté Python : fusion des couches, résolution des anciens noms |
| `datastore/reserves.py` | les champs que l'appelant n'écrit pas : refuser, et poser l'origine à sa place (#586/#606) |
| `datastore/claimable.py` | le périmètre de réservation déclaré (`lifecycle.claimable`, #517) : décision, clauses du pick, refus, phrase — **n'importe le moteur qu'à l'appel** |
| `datastore/schema.py` | le FORMAT : le vocabulaire déclaré et sa validation |
| `datastore/schema_ops.py` | poser/retoucher/nettoyer le FORMAT (mixin du store) |
| `datastore/core.py` | le store qui COMPOSE — gros par nature |

Déplacements PURS : `db/datastore.py` et `datastore/core.py` ré-exportent, la surface plate
`db.<fn>` est figée par `tests/test_db_surface_frozen.py` (cliquet : on peut ajouter,
jamais retirer). ⚠️ Une scission fait dormir les noms hérités des globals dans les
branches rares — balayage figé par `tests/datastore/test_datastore_ns_duplicate.py`.

## Ce qu'oto SAIT d'un champ, et ce qu'il ne saura jamais (14/08)

⚠️ **Ce qu'oto SAIT d'un champ, et ce qu'il ne saura jamais** (tranché par Alexis le
14/08). Oto gère les **types standards** : un `number` se trie numériquement, une date
chronologiquement — l'ignorer donnait `10, 100, 2, 9` (livré v1.112.0). Il ne gère PAS
l'interprétation métier d'une VALEUR : que `20_49` soit une tranche INSEE qui suit
`1_2` est le savoir du consommateur, jamais celui d'oto. Entre les deux, l'ordre des
`options` déclarées au schéma **est honoré** — parce que c'est une DEMANDE adressée à
oto, pas une compréhension qu'il aurait du métier. Même frontière que `flat_alias` :
exécuter une déclaration n'est pas deviner une convention.

## La face REST est 100 % DÉRIVÉE depuis le 2026-08-12 (#302)

> **La face REST est 100 % DÉRIVÉE depuis le 2026-08-12 (#302)** : les 17 routes
> écrites à la main d'`api/datastore.py` (10 chemins) sont des capacités
> (`capabilities/datastore/{namespaces,rows,schema,sharing}.py`, aux côtés de
> `claim`/`activity`/`columns` déjà migrés) — mêmes chemins, mêmes réponses, **mêmes
> codes** (201 sur les créations), mais entrée et sortie déclarées : les 22 opérations
> datastore de `/api/openapi.json` portent désormais un schéma de réponse, contre 5
> avant. `mcp=None` partout : les tools `data_*` sont inchangés, ce lot n'a migré que
> le REST. Trois crans ont été ajoutés au moule pour que ce soit possible sans casser
> le fil (`RestBinding.status`/`body_field`/`reads_body`, cf. §Couche capacité).
> ⚠️ Le refus de champ inconnu s'applique donc maintenant à ces chemins : `oto data
> list --filter k:v` (oto-cli) envoie un `filter` que la route ignorait en silence
> depuis le passage à `page_rows` — il rend désormais 400. Le paramètre est mort côté
> serveur, pas côté client.

## `data_write` — deux sémantiques à connaître (sorties de la description de l'outil le 27/08)

Ces deux paragraphes vivaient dans la description de `data_write` servie au modèle (ajoutés entre v1.148 et v1.151). Ils en sont retirés le 27/08 pour un essai A/B : la fréquence des appels d'outil malformés d'une campagne est passée de 21 % à 62 % sur la vague lancée juste après v1.151.0, et **la longueur des descriptions d'outils est le seul changement sur le chemin de cette campagne** (sensibilité à la longueur d'instruction mesurée le 15/08). Le comportement, lui, est inchangé et reste servi par les réponses de l'outil (`valeurs_effacees`, refus nommant la ligne).

- **Ne pas nommer un champ le laisse intact ; le nommer avec `null` (ou `""`) l'EFFACE.** Deux gestes différents — un `null` glissé dans un payload (variable non remplie, gabarit à moitié rempli) détruit la valeur en place. Les effacements reviennent dans `valeurs_effacees` (champ, ligne, valeur PERDUE) pour réécrire ce qu'on n'a pas voulu vider.
- **Un LOT n'est pas atomique** : il s'arrête à la première ligne que le schéma refuse, et les lignes d'AVANT restent écrites. Le refus nomme cette ligne (son index dans le lot, sa clé métier quand il y en a une) et dit combien de lignes ont atterri — reprendre de là plutôt que rejouer le lot entier.

Si l'essai montre que la longueur ne pèse pas, les deux paragraphes reviennent dans la description (ils y sont utiles) ; s'il montre qu'elle pèse, la règle devient : **les descriptions d'outils portent le contrat minimal, le détail vit dans les réponses et les guides**.


## Provenance : une origine se CORRIGE, elle ne se supprime pas (28/08)

Deux règles, sorties d'un incident de mission (14/08 → 28/08) où une purge de couches
`origine` « hors vocabulaire » a été prise, quinze jours plus tard, pour la destruction
d'une pièce contractuelle — et où l'écran qui la restituait était resté vide sans que
personne le voie.

1. **Une couche `origine` se corrige, elle ne se purge pas — et jamais sur un critère
   générique.** Ce que `origine` doit porter est un contrat **par champ**, déclaré par le
   schéma ou la procédure : sur la plupart des champs, un NOM DE SOURCE (`client`,
   `registre`, `apollo`…) — une origine qui y recopie la valeur est hors vocabulaire et se
   **réécrit** vers le bon nom ; mais sur d'autres champs, `origine` **conserve la valeur
   d'entrée du client** (avant enrichissement ou réattribution), et « origine identique à
   la valeur » y est précisément le cas « retrouvée identique » que la restitution attend.
   ⚠️ **La liste des champs à entrée conservée est PROPRE À CHAQUE TABLEAU et se RELÈVE
   avant toute purge — jamais de mémoire, jamais d'un autre tableau** : sur le vivier du
   28/08 c'étaient `raison_sociale`, `nom_commercial` (743 + 383 « identiques ») et
   `charge_affaires` (79 origines = l'attribution du fichier client avant réattribution
   métier, écrites par la consigne en cours) — illustration du jour, pas définition. ⚠️ **« Purger là où l'origine
   égale la valeur » détruit donc exactement la mesure attendue** sur ces champs : une
   purge NOMME les champs où l'origine doit être une source, ne touche jamais ceux où elle
   conserve l'entrée, **commence par un EXTRAIT des valeurs supprimées** (namespace,
   row_id, chemin, valeur) déposé hors du tableau, et passe par l'outil (`data_write`,
   journalisé) — jamais par un SQL direct que le journal des appels ne voit pas. Le cas
   « retrouvée identique » se restitue aussi par comparaison avec la colonne `initial_of`
   déclarée au schéma.
2. **Une bascule de calcul se vérifie contre la donnée RÉELLE avant de servir.** Un
   consommateur qui passe « d'une colonne dédiée à la couche native » parce que c'est plus
   élégant vérifie d'abord que la couche est REMPLIE sur les lignes qu'il sert
   (`data_aggregate` sur le chemin, compte par ligne) — sinon l'écran est vide et le
   reste : personne ne regarde un écran vide. Même famille que « un filtre ignoré en
   silence » : tester par différentiel, pas à la lecture.

Corollaire de conversion : quand une colonne devient une colonne-liste
(`datastore-colonne-tableau.md`), la provenance des feuilles DOIT suivre (« la provenance
vit au grain feuille »). Sur le cas du 28/08 elle a suivi — mais parce que la procédure
portait déjà la source dans chaque élément (511 contacts sur 515), pas parce que la
conversion la garantit : vérifier après conversion que le compte des origines par feuille
égale celui d'avant, et ne pas lire l'absence d'une colonne SUPPRIMÉE par la conversion
comme une perte de provenance.


## Écrire « sur ma réservation » — l'alias `@claimed` (#517, 29/08)

**Le geste** : `data_write(namespace=…, id="@claimed", row={…})`, et le même sur
`data_release`. Le serveur relit le bail du run courant et écrit sur la ligne qu'il
tient. Rien à recopier, rien à deviner.

**Pourquoi la plateforme change plutôt que la consigne.** Pour écrire sa fiche, un
agent devait repasser les trente-deux caractères que sa réservation venait de lui
rendre. Mesuré sur trois passages d'une campagne réelle, il en altère un
(`…-7c06-…` pour `…-7c16-…`) ou en fabrique un dans une convention étrangère
(un uuid v4, 24 hexadécimaux à la mode ObjectId, une chaîne fabriquée `temp_blocage_<date>`).
**Recopier une chaîne aléatoire n'est pas une question de rigueur** : aucune consigne ne
l'obtient, et l'agent a par ailleurs tout ce qu'il faut dans son bail.

⚠️ **Ce qui coûte n'est pas le refus, c'est ce qui le suit.** L'agent refusé réessaie
**sans identifiant** — c'est la conduite qu'on lui écrit —, et une écriture sans
identifiant **crée** au lieu de corriger. Le 29/08, deux entreprises inexistantes sont
nées ainsi dans un tableau d'évaluation (deux raisons sociales absentes
du lot, du fichier client **et** du registre national, avec une provenance attribuée à
ce registre) ; la veille, cinq fiches d'essai étaient nées dans le fichier d'une
cliente et dans un livrable déjà remis. **Fréquence faible — 4 refus sur 105 mesurés —
mais c'est la seule famille qui fabrique de la donnée fausse.**

### Trois refus, et chacun dit quoi faire

| situation | ce que dit le refus |
|---|---|
| aucun run sur l'appel | passe `_run_id` — il n'est **pas** hérité (cf. #547) |
| le run ne tient rien ici, mais tient ailleurs | **nomme le tableau réservé** — c'est le cas qui a écrit chez la cliente |
| le run tient plusieurs lignes ici | les nomme, et refuse de choisir |

Le deuxième est le plus utile : l'agent visait le mauvais tableau, **et sa réservation
savait lequel était le bon**. L'information existait, elle ne sortait pas.

Le refus `row … introuvable` du chemin d'écriture porte désormais la même charge : la
**forme** attendue d'un identifiant, le rappel que `data_claim_next` la rend telle
quelle, et ce que le run tient déjà.

### Le claim à vide, puis l'alias (29/08, 15:24) — ce que les refus ne disent PLUS

Trois appels d'un même travail, même `_run_id` à l'octet : `data_claim_next` ok, puis
`data_write(namespace="@claimed")` refusé « ton travail ne tient aucune ligne », puis
`data_write(id="<un id de ligne>")` refusé « introuvable ». Lu
d'abord comme « le claim pose une identité que l'alias ne retrouve pas » — **faux**, et
c'est figé par un test contre PostgreSQL sur le chemin réel (middleware + outil) : la
réservation s'écrit sur le run (`claimed_run` = le `_run_id` de l'appel, posé par le
middleware AVANT le dispatch) et l'alias se lit sur le run ; `worker` n'est qu'un libellé,
et `data_write` n'en passe aucun. Le fait, relu dans le journal et dans les lignes : **le
claim avait rendu `row: null`** — la dernière ligne « à enrichir » était sous le bail
actif d'un pair, qui l'a écrite 71 ms plus tard. Le refus était juste. Sa **fin** ne
l'était pas : « … ou écris avec un identifiant explicite », puis « un identifiant a la
forme `01a04aef-…` (cinq groupes hexadécimaux) ». L'agent a fait exactement ce qu'on lui
disait — un identifiant fabriqué sur le gabarit, douze X pour le groupe qu'il ne
connaissait pas. Rien n'est passé (le second refus a tenu), mais c'est la plateforme qui
avait soufflé le geste.

Quatre textes changent, **aucune description d'outil** (empreinte servie nulle) :

- le rendu d'un claim à vide dit qu'on ne tient rien et qu'on **n'écrit rien** ;
- « ne tient aucune ligne » nomme la fin de file comme cas normal et la conduite (rien
  à écrire, `run_finish`) — plus aucune invitation à fournir un identifiant ;
- « introuvable » **décrit** la forme (UUID de 36 caractères, rendu par `data_write`/
  `data_claim_next`, on ne l'invente pas) sans en montrer une — et dit quand ce qui est
  reçu n'a pas cette forme ;
- un `worker` rejoué différent de celui du claim n'est plus « aucune réservation
  active » : le refus nomme le libellé tenu.

⚠️ Pour savoir si un claim a rendu une ligne, lire les **lignes** (`claims`,
`claimed_run`, `updated_at`), pas le relevé du runner : sur son chemin « conversations »
il ne voit pas la sortie du claim et déclare `claims: 1` dès qu'un appel de travail a
suivi. Le journal `tool_calls` ne porte pas non plus `_run_id` dans `args` (le middleware
l'a retiré avant) : sa colonne `run_id` est la seule trace, et elle est la même source
que le datastore.

### Ce que l'alias n'est PAS

**Il ne crée aucune propriété par identifiant.** La preuve d'appartenance reste le
jeton de run (ADR 0038) — c'est le sens du refus de #546 : consoler l'appartenance par
l'identifiant viderait la notion de run. Sans jeton, `@claimed` refuse ; il rend
seulement lisible ce que le serveur sait déjà.

**Et il ne pardonne rien.** Égalité exacte : `@claim`, `@claimed-2`, `@ma_ligne` partent
tels quels et échouent comme avant. Un alias tolérant remplacerait une chaîne à recopier
par une grammaire à deviner — la faute qu'on ferme, un cran plus haut.

### Le bail lu comme une adresse

`db.datastore_active_leases_of(run_id=…, worker=…)` ne rend que les baux **actifs**,
contrairement à `datastore_claimed_rows` (qui sert la vue de supervision et inclut les
baux échus). La différence est le sujet : ici la réponse **désigne une ligne où écrire**,
et un bail échu ne désigne plus rien — la ligne est peut-être repartie à quelqu'un
d'autre. Sans `run_id`, la fonction rend une liste vide : `worker` est une étiquette
choisie par l'appelant, elle restreint, elle ne prouve pas.

### Le run sait où il travaille (#631, 29/08 21:11)

Dans un même travail, `data_claim_next(<nom>)` ok à 21:10:05, puis
`data_write(<nom>, id=<ligne>)` refusé **« namespace inconnu »** à 21:11:23, puis
`data_write("@claimed")` ok à 21:11:35 — 103 refus de cette famille sur la soirée. La
cause n'était pas dans le datastore : l'écriture refusée était le seul des trois appels
**sans axe `_org=`**, donc résolue dans l'org MAISON de l'appelant, où le tableau n'existe
pas. Le journal ne montre pas l'axe (le middleware le retire des arguments avant le sink) ;
la preuve est la colonne `org_id` stampée — celle du tableau sur les deux appels ok, la
maison sur le refus. Et `namespace="@claimed"` échouait pareil : l'alias relisait le NOM
dans la réservation, puis le résolvait dans la mauvaise org.

Deux gestes, sur le seul chemin qui échouait (`datastore/hors_org.py`) :

- **résolution par la réservation** : quand le nom demandé est celui d'un tableau que la
  réservation active du run porte, `_resolve` le trouve par elle — sans axe. Le bail
  LOCALISE, il ne donne aucun droit : `ownership.can_access` reste exigé, org-agnostique
  (un tiers qui connaît le jeton d'un run n'y gagne rien — testé).
- **sinon le refus le dit** : « il existe dans une autre de tes organisations : org X
  « … ». Cet appel a été résolu dans l'org Y « … » — passe `_org=X` ; et si ton travail
  tient une ligne, `_run_id` + `id="@claimed"` suffisent ». La face REST le disait déjà
  (`X-Oto-Org`, signal #316) : la face MCP répondait « inconnu » nu — **une divergence
  entre deux faces, pas un manque d'information**. La recherche est désormais commune
  (`hors_org.ou_existe`), chaque face phrase son remède.

Ce que ce lot NE fait PAS, et pourquoi : « l'org du run devient l'org de l'appel par
défaut » aurait aussi corrigé le stamp du journal (#630) — mais mesuré sur 7 jours,
2 010 appels sur 30 828 dans un run ont une org d'appel ≠ org du run, et presque tous
sont LÉGITIMES (un agent multi-org ouvre son run dans une org et travaille dans deux
autres avec `_org=` explicite). Seuls 82 étaient le défaut, tous des `data_write` refusés. Changer le
seam d'org pour ces 82 engage les credentials de tous les connecteurs : c'est un lot à
part, pas un correctif de datastore.

⚠️ **Décidé et fait le 30/08/2026 (#639)** : le lot à part a eu lieu. Sans `_org=`, un
appel qui porte `_run_id` se résout désormais dans l'org du run (`runs.org_id`),
appartenance gardée ; `_org=`/`_project=` explicites priment, un run inconnu ne change
rien. Le geste du 29/08 21:11:23 (`data_write` sans axe, sans réservation) s'écrit donc
dans le bon tableau, et le journal le stampe dans l'org du travail. La résolution par
la réservation ci-dessus reste — elle couvre un run mal posé. Détail et mesure :
`docs/org-context.md` §« L'org du run ».

### `@claimed` s'écrit aussi en TABLEAU (29/08, premier contact avec des agents réels)

**Deux écritures refusées sur cinq, en « namespace `@claimed` inconnu ».** À sa première
rencontre avec l'alias, la flotte l'a posé dans `namespace`, pas dans `id`.

> **On leur retire un champ à recopier ; ils y mettent l'alias qu'on vient de leur
> apprendre.** Et ils n'ont pas tort : on leur a enseigné « la réservation est
> l'adresse », et une adresse commence par le tableau.

**La réservation porte les deux.** `namespace="@claimed"` résout donc le tableau **et** la
ligne ; `namespace=<table>` + `id="@claimed"` reste la forme canonique ; les deux à
`@claimed` désignent la même ligne. Sans réservation, refus nommé — jamais « inconnu ».

⚠️ **Et `@claimed` posé dans le CONTENU d'une ligne est refusé en nommant la faute** :
c'est une adresse, pas une donnée, et écrit dans `row` il finirait en clair dans un
fichier client. *Un refus qui dit « inconnu » sur un jeton que l'outil reconnaît envoie
chercher une faute de frappe là où il n'y en a pas — c'est ce qui a coûté les deux
écritures.*

**La leçon dépasse l'alias** : quand un agent met la bonne valeur dans le mauvais champ,
c'est d'abord une information sur la façon dont il a compris ce qu'on lui a dit. Refuser
sur le champ voisin, c'est refuser une demande qu'on sait satisfaire.

### L'alias vaut sur TOUS les verbes qui adressent, pas seulement l'écriture (29/08)

L'inventaire des jetons réservés a montré le trou : `@claimed` était accepté sur
`data_write` et `data_release`, refusé sur `data_rows`, `data_delete_row`, `data_url`
et `data_aggregate` — les verbes voisins, sur le même objet, dans la même session.

**Un agent n'apprend pas un alias par verbe, il l'apprend par notion.** À qui a compris
« ma réservation est mon adresse », il est naturel de relire la ligne qu'il tient avant
de l'écrire. Le refus tombait sur ce geste-là, et il disait « namespace inconnu » —
donc envoyait chercher une faute d'orthographe dans une chaîne correcte.

Les quatre verbes passent désormais par le **même** geste que l'écriture
(`_adresse_reservee`) : `@claimed` en tableau, en ligne, ou les deux ; les mêmes trois
refus, chacun portant sa conduite à tenir. `data_url` et `data_aggregate` n'adressent
qu'un tableau — on n'y résout pas de ligne.

### `fields=["*"]` demande TOUTES les colonnes

`*` est le chemin vers le brut sur `oto_doc` et sur le feed depuis toujours. Sur
`data_rows` il tombait dans « colonne inconnue » et rendait `_id` seul : l'agent croyait
demander la ligne entière et recevait une ligne vide, **sans erreur**. Un jeton qu'une
autre surface de la même plateforme accepte n'est pas une faute de frappe.

⚠️ Le delta d'empreinte servie de ce lot est **nul** : rien n'a été ajouté aux
descriptions. C'est délibéré, et c'est la mesure du 27/08 qui le dicte (§ longueur des
descriptions ↔ appels malformés). Ce lot ne rend possible que ce que les agents
**tentaient déjà** ; il n'a donc rien à leur enseigner.

## Les jetons réservés — où chacun s'écrit, et les trois issues (#517, 29/08)

Trois jetons voyagent dans les appels du datastore. **Un seul endroit dit où chacun a
un sens** — `oto_mcp/datastore/jetons.py` —, et les deux faces s'en servent.

| jeton | ce qu'il désigne | champs qui l'acceptent |
|---|---|---|
| `@claimed` | la ligne que le run réserve, et son tableau — **tant que le run est ouvert** (#645) | `namespace`, `id` |
| `slot:<nom>` | le tableau bindé sous ce nom par le projet actif | `namespace` |
| `*` | toutes les colonnes | `fields` |

**Trois issues, jamais une quatrième :** (et les champs réservés PAR LE SCHÉMA —
`readonly`, `origine: "system"`, #586/#606 — ne sont pas des jetons : ils se jugent
dans le store, là où le schéma est connu, cf. § `key_required` et suivants.)

| ce qu'on lit | ce qui se passe |
|---|---|
| jeton **accepté** par ce champ | résolu |
| jeton **reconnu mais mal placé** | refus qui NOMME le champ où il s'écrit |
| jeton **inconnu** ici | rien — la valeur part telle quelle |

### Pourquoi une couture, et pas une garde de plus

L'inventaire du 29/08 est parti chercher des jetons mal **nommés** ; il a trouvé que
les cas coûteux sont les jetons mal **placés**, et parmi eux **ceux que rien ne
refusait** :

- `@claimed` écrit dans le **contenu** d'une ligne — accepté, gravé en clair dans un
  fichier client ;
- `_run_id` posé comme **colonne** — même famille : un contexte d'exécution (ADR 0038)
  inscrit dans une fiche livrable ;
- `slot:<nom>` sur une opération de **ligne** côté capacité — passé brut au stockage,
  qui répondait « namespace inconnu », **alors que les opérations de schéma de la même
  couche le résolvaient depuis toujours**.

> **Une divergence qui refuse est visible ; une divergence qui répond une cause fausse
> s'instruit pendant des jours.** Les deux premières ne refusaient rien du tout.

### ⚠️ Ce que la couture ne fait PAS, et c'est délibéré

**Elle ne devine pas.** La reconnaissance est exacte : `@claim`, `@claimed-2`,
`slots:x` ne sont pas des jetons, partent tels quels et échouent comme avant. *Un alias
qui pardonne remplace une chaîne à recopier par une grammaire à deviner — la même
faute, un cran plus haut.*

**Elle ne regarde pas les mêmes jetons dans le contenu que dans l'adresse.** Seuls
`@claimed` et les paramètres d'appel sont refusés dans une valeur de ligne, parce
qu'ils n'ont **aucun** sens comme donnée. `slot:` et `*` sont des chaînes qu'une ligne
peut légitimement porter — « slot: machine à café » est une note, pas une adresse.
Les refuser là serait se protéger d'une faute qu'on ne sait pas distinguer d'un texte
ordinaire ; un test existe pour l'empêcher de jamais devenir vrai.

## Une description ne prescrit pas un geste dont l'outil n'est pas servi (#613, 29/08)

**Mesuré sur cent fiches d'une campagne** où `data_release` était filtré à l'inclusion :
la description de `data_claim_next` disait « then RELEASE the row … Release with
`data_release` », relue à chaque appel par des agents qui ne pouvaient pas l'exécuter.
Un agent qui a une intention et pas de destination s'en fabrique une : ils ont écrit
leur intention **dans la fiche de l'entreprise** — colonnes `_liberation: "run_finish"`,
`_action: "release"` (et `_run_id`, refusé depuis #602) — des paramètres d'appel dans
des données clientes, exportées. Même mécanique que les 37,5 % de `_run_id` (#547) : le
texte le plus près du geste gagne.

**Ce qui est vrai, et prouvé AVANT d'être écrit dans une description servie** : la
fermeture du run libère ce que le run tenait. `run_finish` appelle
`datastore_release_by_run(run_id)` (`claimed_run = run` ⇒ bail effacé, puis évaluation
du plafond de réservations), **quelle que soit l'issue** (`done`/`failed`/`blocked`), et
le dit dans sa réponse (`rows_released`). Best-effort : si la base tousse, le run se
ferme quand même et la ligne reste au bail. Le niveau base était couvert
(`test_row_lock_native.py`) ; le lien entre le **verbe servi** et la libération est figé
par `tests/test_run_finish_releases_613.py`, sur le chemin réel (middleware + outils
montés par `register_all`, PostgreSQL) — y compris « un autre run qui se ferme ne rend
rien » et « la ligne rendue est reprise par le claim suivant ».

**La description dit désormais ce qui se passe sans l'outil**, en une phrase : « Release
the row with `data_release` if you have it; otherwise finishing your run (`run_finish`)
releases it. Never write your intent into the row (no `_action`/`_liberation`
columns). » Delta servi **−5 caractères** (1737 → 1732), la phrase étant payée en
resserrant ce qui existait : la clause SQL (`FOR UPDATE SKIP LOCKED`), l'exemple de
libellé de `worker`, « historic behaviour », « last resort … without closing anything ».
Aucun paramètre de `data_claim_next` n'est en cause : les colonnes fabriquées partent
par `data_write`, pas par un champ de cet outil — rien à répéter dans un schéma.

⚠️ Ce que la phrase ne couvre PAS : l'agent qui meurt (il n'appelle pas `run_finish`) —
son filet reste l'expiration du bail (`lease_s`). Et le point 2 de #613 (une description
rendue selon les outils réellement servis à la session) est une décision de conception
à part : une description conditionnelle n'atteint pas un client qui fige `tools/list`
au handshake (cf. #547).

**Addendum #633 (29/08/2026) — l'agent mort SOUS UN JOB DU RUNNER est couvert, et le
zéro est écrit.** Le worker survit à l'agent et conclut le job : `POST /api/me/runner/jobs`
`op=complete` libère désormais les baux du run que le job connaît (`run_id` de l'appel,
sinon `bind_run`), best-effort, et rend `rows_released` avec **`0` explicite** — `null` +
`release: no_run|failed` quand rien n'a été libéré. `run_finish` écrit lui aussi
`rows_released` toujours (`0`, ou `null` si la libération a échoué) : un poste de flotte
distingue « zéro ligne rendue » de « champ absent ». Détail et table des formes :
`docs/runner-et-automatisations.md` § `complete`. Reste non couvert : l'agent
conversationnel (hors runner) qui meurt — le bail seul.

## `@claimed` après la clôture : un refus qui dit un MOMENT (#645, 30/08)

**Suite directe du précédent, et son coût.** La clôture libère (#613) ; ce que personne
n'avait dit, c'est que l'ADRESSE cesse de résoudre au même instant. Huitième passage du
palier, 30/08/2026 : **99 refus sur 200 écritures**, tous « `@claimed` en tableau : ton
travail ne tient aucune ligne en ce moment (aucune réservation active) » — émis sur des
appels d'un harnais qui écrivait **après** `run_finish`. Le refus était exact et décrivait
un **état** ; le problème était un **moment**.

> **Un refus juste qui n'est pas le bon refus coûte autant qu'un refus faux** : il envoie
> chercher une réservation oubliée là où c'est l'ordre des gestes qui est en cause. Deux
> heures perdues, sur un mécanisme découvert deux fois à douze heures d'écart.

**Ce qui change.** Quand le run de l'appel est clos, le refus le dit — « ton travail est
CLOS depuis `<horodatage>` (`run_finish`), et sa clôture a libéré toutes ses lignes —
l'alias ne désigne une ligne que TANT QUE le travail est ouvert, jamais après » — au lieu
du texte de fin de file, qui reste servi quand le run est **ouvert** (c'est le cas normal,
#517). L'heure y est parce que c'est elle qui fait le lien avec le geste précédent :
« depuis 21:08:53 » se reconnaît dans un journal, « clos » ne se reconnaît pas.

⚠️ **La clôture se lit du FAIT, jamais de l'index** — `db.run_closed_at` réutilise
`_run_closure`, comme les lentilles. `runs.finished_at` est une écriture de confort que
`finish_run` rate en silence quand l'index n'a pas été posé : un refus qui annoncerait
une clôture d'après une colonne manquée mentirait exactement dans le cas qu'il est censé
expliquer. Cas figés dans `tests/test_run_single_source.py`.

⚠️ **Chemin d'ÉCHEC seulement**, et **le refus prime sur sa propre précision** : la
requête n'est payée que lorsque le run ne tient rien (le nominal résout un bail sans y
passer, test dédié), et si le journal est illisible on journalise puis on retombe sur le
texte de fin de file — une erreur interne effacerait la conduite au moment précis où elle
sert (`_adresse_reservee`).

**Et la borne est dans la description servie**, là où le geste se construit : `data_write`
la porte sur ses deux champs qui acceptent l'alias (`id` : « it resolves only while that
run is OPEN, `run_finish` releases what it held » ; `namespace` : « open run only ») —
l'asymétrie entre les deux est précisément ce qui avait coûté deux écritures le 29/08
(#599) — et `data_release` sur sa phrase d'alias. Empreinte servie mesurée par
`scripts/empreinte_servie.py --diff` : `data_release` description **+30**, `data_write`
schéma **+72** ; aucune autre. Le refus ne prescrit **aucun outil de plus** : l'identifiant
de la ligne est une valeur déjà reçue, pas un geste à exécuter (règle #613/#632,
`docs/conventions.md`).

Preuve de bout en bout dans `tests/datastore/test_claimed_run_clos_645.py` : réserver sous
`_run_id=R`, clore R, écrire sous `@claimed` — contre PostgreSQL, par les outils montés
par `register_all`, chaque appel dans sa propre session (le chemin de la flotte), avec un
lecteur indépendant (`db.my_runs(open_only=True)`) qui atteste au passage que les faits
écrits ont bien la forme d'une clôture.
