---
title: Monitoring & investigation des appels
type: reference
description: >-
  Référence du journal d'appels d'oto-backend : ToolCallLogger (oto_mcp/calllog.py)
  via hook on_call_tool, table tool_calls (kind mcp|rest|connector, corrélation
  session_id/run_id/org_id/client_id/sentry_event_id), rétention portée par le timer
  d'archivage (OTO_JOURNAL_RETENTION_DAYS, défaut 90 j) et non plus par un prune au
  boot — ADR 0065 lot 0. Décrit les surfaces d'investigation —
  console MCP oto_admin_monitoring + /platform/monitoring (plateforme),
  oto_org_monitoring + /org/monitoring (org_admin, mêmes lentilles bornées à SON org),
  /api/me/* (membre) — servies par les mêmes capacités. À consulter pour comprendre ce
  qui est tracé, enquêter sur une erreur, ou étendre la rétention.
---

# Monitoring & investigation des appels

## Ce qui est écrit

`ToolCallLogger` (`oto_mcp/calllog.py`, middleware inliné — ex-lib `otomata-calllog`
décommissionnée, contrat canonique dans le socle `otomata-mcp`) journalise **chaque**
appel de tool via le hook `on_call_tool` (point d'interception unique) dans la table
`tool_calls`. Best-effort : une erreur d'écriture du journal ne fait jamais échouer
l'appel ni n'avale l'exception métier, et l'INSERT part hors event loop
(`asyncio.to_thread`) — le chemin chaud de chaque appel ne doit pas attendre PG.

Colonnes canoniques : `server`, `kind`, `sub`, `email`, `tool`, `args` (**tronqués à
l'écriture**), `ok`, `error`, `error_kind`, `duration_ms`, `created_at`.

`kind` discrimine l'événement (ADR 0017, « un seul flux ») : `mcp` = invocation d'outil
(défaut), `rest` = appel `/api/*`, `connector` = échec de résolution de credential.

### `error_kind` — le résultat de la taxonomie, en colonne (oto#25 lot b1)

`error` est un texte brut tronqué (`str(e)[:500]`) : lisible par un humain, pas
filtrable. `error_kind` porte le `.code` que rend `error_taxonomy.classify(exc)` sur
l'exception CAPTURÉE (ex. `not_authorized` sur un 401/403 amont, `upstream_timeout`,
`internal`…) — écrit par `calllog._record` (via `calllog._error_kind`) sur un échec,
`NULL` sur un succès et sur tout l'historique antérieur à ce lot (non
reconstructible depuis le texte tronqué). Colonne additive, **sans index** — même
règle que `request_id`/`call_uid`/`effective_sub` (`docs/live-migrations.md`) : c'est
une lecture d'enquête, pas un chemin chaud. Exposée dans la fiche d'un appel
(`oto_admin_monitoring op=call` / `oto_org_monitoring op=call`, `db.get_tool_call`).

⚠️ **`error` n'est PAS ce que l'appelant a reçu — et s'y fier fait conclure l'inverse
de la vérité.** Le journal garde `str(e)`, l'exception telle qu'elle a été levée ; ce qui
part vers le modèle est le rendu de `error_taxonomy.classify(exc)`, qui peut être tout
autre chose. Une exception large et nue tombe en branche « interne » et devient **« Erreur
interne du serveur. » sans écho du message** (anti-fuite) : le journal montre alors une
phrase parfaitement actionnable que personne n'a jamais lue.

Vécu le 05/09/2026 sur `oto-backend#473` : le journal affichait « Facebook exige une
session ; cherche une autre source », ce qui a fait conclure que le défaut était réparé.
Il ne l'était pas — `classify` rendait « Erreur interne du serveur. », et les agents
abandonnaient. **La seule mesure qui vaut est `classify(exc)`, pas la colonne `error`** ;
`error_kind` est d'ailleurs là pour ça, et un `internal` à côté d'un texte utile est
exactement le signal de cet écart.

⚠️ **Ce que ce lot n'est PAS** : `error_kind` est un FAIT journalisé, rien de plus.
Aucun lecteur n'en dérive encore une action (marquer un credential rejeté, par
exemple) — ça viendra, séparément, avec son propre feu vert (lot b2 de l'issue).

### Ce que le journal ne porte JAMAIS : un jeton en clair

⚠️ **Corrigé le 2026-08-29 (#558) — le journal en portait.** La réduction de route
(`api/routes._normalize_route`) était une **allowlist de FORMES** : numérique ou UUID →
`:id`, tout le reste passe. Or les routes servies portent leur secret DANS le chemin
(`/api/upload/{token}`, `/api/public/docs/{token}`, `/api/invitations/{token}`, et
jusqu'au 15/09/2026 `/api/invitations/code/{code}` — retirée depuis avec le code court
d'invitation lui-même, oto-backend#560) et aucun de ces secrets n'a la forme d'un
identifiant. Ils partaient donc en clair dans `tool_calls.tool`, sur toute la fenêtre de
rétention,
relus par les trois étages de lentilles — **y compris le jeton d'invitation, que le
modèle de données refuse explicitement de persister ainsi** (`org_store/invitations.py`
n'enregistre que son empreinte). Un middleware transverse défaisait cette précaution.

La règle qui remplace la forme, source unique `oto_mcp/journal_secrets.py` :

- **par PROPRIÉTÉ, jamais par une liste de chemins** — un segment lié à un paramètre de
  route dont le NOM est déclaré secret (`token`, `code`) est réduit, quelle que soit son
  allure. La liste des routes concernées est **dérivée de la table servie**
  (`make_routes` appelle `declare_routes`) : une route future qui déclare `{token}` est
  couverte le jour où elle est montée, sans qu'on y pense ;
- **la route réduite ne porte pas le masque** — `tool` sert le `GROUP BY` du monitoring,
  une empreinte par jeton ferait exploser sa cardinalité. L'empreinte va dans `args`, où
  elle répond à « le même jeton a-t-il été rejoué ? » sans dire lequel ;
- **le masque est un HMAC clé (`#` + 12 hex), pas « les N derniers » ni un sha256 nu** —
  garder les N derniers caractères d'un secret court en exposerait une part lisible, et
  un sha256 nu se casse par force brute en quelques secondes pour qui lit le journal
  (même un token long de 256 bits ne protège rien si sa réduction, elle, est courte et
  devinable). La clé est celle des jetons signés (`OTO_MCP_OAUTH_STATE_SECRET`), donc le
  masque reste stable d'un boot à l'autre ;
- **la même propriété sur l'autre face** — le jeton d'invitation arrive aussi par
  `oto_org op=accept_invite`. Un argument de **capacité** portant un de ces noms est
  masqué (`truncated_args(..., tool=)`), y compris via `oto_call` (⚠️ **seulement depuis
  le 2026-09-01** — cf. §suivant) ; un argument de **connecteur** qui s'appelle pareil ne
  l'est pas (`droit_article(code='CT')` n'est pas un secret, et le cacher coûterait une
  lecture pour rien).

**Les lignes déjà écrites** se réparent à la main :
`oto-mcp maintenance journal-tokens` (§Rétention).

**Le journal d'accès d'uvicorn** (journald) écrivait lui aussi le chemin en clair jusqu'à
la v1.391.0 : `journal_secrets.MasqueCheminAcces` y pose le même masque depuis. Réparer
un journal système ne suffit pas — un jeton lu pendant la fenêtre doit cesser d'ouvrir
quoi que ce soit : `scripts/rotation_jetons_journal.py` (à blanc par défaut) refait le
lien public des pages et révoque les invitations en attente émises avant la bascule.
Les jetons courts (upload, 15 min) ont expiré d'eux-mêmes ; ceux de désinscription
n'ouvrent qu'une désinscription et ne se révoquent qu'en changeant le secret d'instance.

**La query, sur toute route** : la valeur d'une clé secrète par son NOM devient `***`
au journal d'accès — `code`, `state`, `session_state`, `id_token`, `access_token`,
`refresh_token`, `client_secret`, `token`, `key`, `apikey`, `api_key`, `access_key`, et
toute clé contenant `secret`, `token` ou `password`
(`journal_secrets.CLES_DE_REQUETE_SECRETES`). Jusqu'au 09/10/2026, le retour de chaque
fournisseur OAuth (`/api/<fournisseur>/oauth/callback?code=…&state=…`) écrivait le code
d'autorisation en clair dans journald ; seul `/oauth/callback` (relais) était masqué.

**Les requêtes SORTANTES** suivent la même règle : la ligne INFO d'httpx (`HTTP Request:
POST https://…?key=…`) écrivait la clé d'API d'un connecteur en clair.
`MasqueRequeteSortante` masque la query sur `httpx`, les loggers d'`httpcore` et
`urllib3.connectionpool` (ces deux derniers n'écrivent qu'en DEBUG) — masquer plutôt que
remonter en WARNING : la trace des appels sortants reste.
`journal_secrets.installer_masques_du_journal` (appelé par `server.main` avant
`uvicorn.run`) pose les deux filtres ; `tests/test_journal_acces_masque.py` garde leur
branchement.

Une route qui **reçoit un secret dans sa query** (protocole d'un tiers : le retour
d'autorisation WordPress porte `password=`) se déclare dans
`journal_secrets.routes_a_requete_secrete` : le même filtre remplace sa query par
`[redacted]` au journal d'accès, et `sentry_setup` la retire des événements — une seule
liste pour les deux canaux.

Cliquets : `tests/test_journal_secrets.py`, `tests/test_rest_call_logger.py`,
`tests/test_journal_no_plaintext_secret.py`, `tests/test_journal_token_purge_558.py`.

### Le dispatch universel écrivait hors de la fabrique (corrigé le 2026-09-01)

⚠️ **`tool_calls` n'a pas un seul écrivain, il en a cinq** — et la règle ci-dessus n'est
une propriété du journal que si tous passent par la même fabrique. Ce n'était pas le cas.
Un appel dispatché par `oto_call` (ADR 0036 §5) produit **deux** lignes, et aucune des
deux n'était sous la règle annoncée :

- **la ligne du nom CIBLE** (`tools/meta._trace_target_call`, celle qui rend le catalogue
  latent visible à l'inventaire d'usage) posait le dictionnaire d'arguments **tel quel** :
  ni tronqué, ni masqué. Mesuré en base le 2026-09-01 : **40 159 des 268 016 lignes
  `kind='mcp'`** viennent de là (6 664 avec un `run_id`, donc servies dans la timeline
  d'un déroulé), et les **111** lignes dont une valeur dépasse la borne annoncée — jusqu'à
  4 383 caractères — en viennent **toutes** ;
- **la ligne d'enveloppe** (`tool='oto_call'`) reprenait bien la déclaration de l'outil
  visé, mais ne masquait qu'**un** niveau de sous-dictionnaire. Le dispatch en ajoute un
  (`{"name": …, "arguments": {…}}`), donc le seul secret déclaré à deux niveaux
  (`lemlist_mailbox`, mots de passe SMTP/IMAP sous `smtp_imap`) repartait en clair — et
  cet outil n'étant pas au registre servi, `oto_call` est le **seul** chemin par lequel il
  s'appelle : la déclaration ne s'appliquait donc jamais.

Aucun secret n'avait fuité en pratique (0 appel aux deux outils qui en déclarent, 0
`token`/`code` non masqué sur `oto_org`) : défaut latent, pas incident. Les deux chemins
passent désormais par `truncated_args`, dont le masquage traverse les sous-dictionnaires
et les listes jusqu'à `MAX_MASK_DEPTH`.

**Et la facturation (corrigé le 2026-09-10).** La ligne du nom cible n'avait ni
`key_mode` ni `quantity` ni `instance`, et son `org_id` était relu **après** le reset des
axes — l'org maison de l'appelant, pas celle où la cible avait résolu ses credentials. Ce
que la cible consigne (`session_org.note_call_trace`) tombait dans le relevé de la
requête **enveloppe**, donc sur la ligne `tool='oto_call'`, que la lentille
`org.usage.calls` (filtrée par nom d'outil) ne lit jamais : une consommation passée par
le dispatch n'était facturée sous aucune org. Désormais :

- `oto_call` pose un relevé **propre à la cible** autour de `tool.run`, et lit l'org et le
  run de la cible **avant** de défaire ses axes ;
- **une seule ligne facture — celle de la cible** : `quantity` et `key_mode` y restent ;
  le reste du relevé (`resolved_account`, `resolved_connector`) est recopié dans le
  relevé enveloppe, pour que l'écho du compte rendu à l'agent survive au dispatch ;
- les deux écrivains appliquent **la même règle** (`calllog.apply_call_trace`, avec la
  liste fermée `server._TRACED_ARGS`).

Cliquet : `tests/test_oto_call_trace_facturation.py`.

**Ce que le banc n'avait pas vu, et pourquoi.** Le cliquet de chaînage posé le matin même
scannait `calllog` — le module qui se déclare « domicile unique du journal ». Il y avait
raison, et c'est tout le problème : un garde-fou qui part du module DÉCLARÉ ne peut pas
voir un écrivain qui ne s'y trouve pas. Il part maintenant des modules qui écrivent
RÉELLEMENT, **découverts** (`tests/test_timeline_args_declare.py`) — avec trois exceptions
nommées et comptées : le handshake, l'enrichissement du sink (il étend l'`args` déjà
fabriqué), et la ligne de surface REST (elle ne porte que l'empreinte des jetons du
chemin). ⚠️ Sa découverte cherche le nom NU : trois des cinq écrivains ne l'appellent pas,
ils le passent (`asyncio.to_thread(db.insert_tool_call, row)`).

Cliquets : `tests/test_journal_dispatch_universel.py`,
`tests/test_timeline_args_declare.py`.

**Extensions OTO-LOCALES** (hors contrat canonique, enrichies par le sink de
`server.py`) — ce sont les axes d'**investigation** :

| colonne | ce qu'elle répond | posé par |
|---|---|---|
| `session_id` | quelle conversation MCP | `ctx.session_id` |
| `run_id` | quel déroulé (`run_start`…`run_finish`) | jeton `_run_id=` puis pile `guide_run` |
| `org_id` | sous quelle org l'appel a été émis | seam `access.current_org` — depuis le 30/08/2026 (#639), l'org du RUN quand l'appel porte `_run_id` sans `_org` |
| `client_id` | depuis quelle surface (claude.ai, Claude Code…) | claim `azp` du JWT |
| `sentry_event_id` | où est le traceback | `SentryToolErrorMiddleware` |

⚠️ **Ces colonnes dépendent de l'ordre des middlewares.** `CallContextMiddleware` doit
rester le plus EXTERNE et `SentryToolErrorMiddleware` le plus INTERNE : sinon `_CALL_ORG`
est reset avant que le sink ne lise `current_org` (org d'audit fausse), ou l'event Sentry
n'est pas encore capturé quand la ligne s'écrit. fastmcp exécute les middlewares dans
l'**ordre d'ajout** (premier ajouté = plus externe). Contrat gardé par
`tests/middleware/test_middleware_order.py`.

### Lire les arguments d'un appel : `args` sur la fiche, `arg_keys` sur la liste (#634, 2026-08-30)

Le journal **porte** les arguments (colonne `args`, tronqués et masqués comme ci-dessus).
Ce qui les rend, et sous quel nom, ne se devine pas — et s'est mal deviné le 29/08/2026 :
443 lectures de `GET /api/orgs/{id}/monitoring/calls/{call_id}` en douze minutes, conclues
« `arguments: {}` » sur des lignes dont `args` faisait 135 à 397 caractères. Rejoué sur la
route servie (adaptateur + PostgreSQL, `tests/test_journal_args_634.py`) : la fiche rendait
`call.args` plein. **Aucune vue n'a jamais émis de clé `arguments`** ; un lecteur qui la
cherche avec un défaut `{}` fabrique lui-même l'objet vide, et « je ne sais pas le lire »
s'est écrit « on ne peut pas savoir ». Depuis ce jour, le contrat le dit :

- **la fiche** (`op=call` sur les deux consoles, `GET …/calls/{call_id}` aux deux étages) :
  `call.args`, **tels que journalisés** — bornés à l'écriture (`calllog.MAX_ARG_CHARS`,
  4 000 caractères par valeur depuis le 23/09/2026, 300 avant — #413 ; valeurs composées
  stringifiées), toute coupe **déclarée** dans `call.args._truncated` = `{"at": <borne>,
  "sizes": {<argument>: <taille réelle>}}` (absente quand rien n'a été coupé), jetons
  masqués (#582), `null` quand l'appel n'en portait aucun. Un seul chemin de lecture (`get_tool_call`) pour les trois faces ;
  le schéma de la 200 (`CallDetail`) le déclare ;
- **la liste** (`op=calls`, `GET …/calls`) ne porte pas le contenu — une page de 200
  lignes n'a pas à charrier 200 payloads — mais `arg_keys` : les **clés** des
  arguments, triées, `[]` sans argument. « Cet appel portait-il un numéro d'entreprise ? »
  se répond là, sans ouvrir une fiche, et sans qu'une valeur sorte (un secret masqué à
  l'écriture n'a jamais eu son NOM pour secret) ;
- **jamais un objet vide à la place d'une absence** : la liste n'a pas de champ `args`
  (la vue ne le porte pas), la fiche rend `null` (l'appel n'en avait pas) — les deux se
  lisent différemment, et c'est le but.

### La forme de la réponse : `result_shape` sur la fiche ET la liste (#644, 2026-09-23)

`result_size` (#340) dit **combien** l'outil a servi, jamais **quoi** : un 0 ne sépare pas
une liste vide d'un refus rendu poliment, un non-zéro ne sépare pas un résultat d'un
`{"error": …}` rendu sous `ok=true`. La colonne `tool_calls.result_shape`, écrite au même
point que la taille (`calllog.forme_servie`, middleware MCP), porte un vocabulaire
**fermé**, jamais le contenu :

- `empty` — `null`, `[]`, `{}`, `""` (une sortie non-objet que fastmcp range sous
  `{"result": …}` est déballée) ; sans donnée structurée, aucun bloc ou des textes vides ;
- `non_empty` — tout le reste ;
- `refused(<code>)` — un objet qui porte `ok: false` ou une clé `error` non vide à la
  racine. Le code est pris de `code`, `error_code` ou `error` (ou `error.code`) s'il a la
  forme d'un identifiant (minuscules et `_`, 40 au plus) ; sinon `refused(unnamed)` — un
  message libre ou une valeur à chiffres ne sort jamais dans le journal.

Le vocabulaire est fermé **par la base** : la contrainte `tool_calls_result_shape_ferme`
refuse toute autre valeur (un TEXT libre sous un nom de résultat pourrait garder une
réponse — garde `tests/test_runner_cle_de_modele.py`).

`NULL` = non mesurée : appel en échec (`ok=false` par exception), geste REST, forme
illisible, lignes antérieures au 23/09/2026. Un refus rendu en **texte seul** (sans
donnée structurée) n'est pas reconnu : ce serait parser le texte à chaque appel. Rendue
par la fiche (`call.result_shape`) et par chaque ligne de la liste, à côté d'`arg_keys`.
Colonne posée sur la base existante par la révision `0010_tool_calls_result_shape`, jouée
**avant la fusion** (`docs/migrations-versionnees.md` §5.1).

### L'émetteur d'un appel : le client déclaré et le jeton (otomata-tech/oto#187, 2026-09-24)

Le nom du logiciel client (`clientInfo` du handshake MCP) était reçu à **chaque**
`initialize` et gardé sur la ligne `kind='protocol'` — jamais sur l'appel, et aucune
lecture ne le rendait. `client_id` (le `azp` du JWT) était NULL sur trois appels sur
quatre (runner, jetons d'API), et `token_kind` NULL sur 100 % des appels MCP (le claim
était perdu à la vérification du jeton).

Chaque ligne `kind='mcp'` porte désormais son **émetteur déclaré**, posé par une règle
unique, `calllog.poser_emetteur`, pour les deux écrivains (middleware et cible d'`oto_call`) :

- le **logiciel client** sous la clé réservée **`args._client`** = `{"name", "version"}`,
  lu sur la session servie (`session.client_params.clientInfo`, aucune base). Une clé du
  JSON existant plutôt qu'une colonne : le journal compte des millions de lignes, et
  l'émetteur se **lit** (fiche, liste, export, couverture), il ne se filtre sur aucun chemin
  chaud. Exclue des `arg_keys`, comme `_truncated` ;
- le **jeton nommé** dans les colonnes existantes `token_id` / `token_kind`
  (`user` | `delegation`), relus du jeton de la requête (`auth.hooks.current_token_axes`).
  Absents = session OAuth (aucun jeton nommé ; `client_id` dit alors l'application).

Servi par **toutes les lectures du journal** (`journal_calls.EMITTER_SQL`) : la liste
(plateforme, org, membre), la fiche (plus `token_id`) et l'**export d'audit** d'une org
(`client_name`, `client_version`, `token_kind`). La couverture se lit dans l'agrégat
(`op=summary`) : `emitter_named_calls` sur `total_calls`, et `by_emitter`.

⚠️ **Déclaré par le client : lisible, jamais opposable.** `clientInfo` est ce que le
client écrit ; il distingue des **surfaces** (runner, CLI d'agent, client web, script),
pas la présence d'un humain au clavier — et rien ne doit s'en servir pour refuser un appel.
⚠️ Les lignes antérieures au lot n'ont pas d'émetteur : on ne réécrit pas un journal
(la jointure par `session_id` avec la ligne de handshake reste possible à la main).

## Ce qui n'est PAS tracé

⚠️ **Les refus du TRANSPORT l'étaient — ils ne le sont plus (11/09/2026).** Une requête
refusée par le transport du SDK `mcp` l'est **avant tout dispatch de session** : elle ne
traverse aucun middleware FastMCP, donc ni `tool_calls`, ni Sentry, ni aucune télémétrie
bâtie sur les hooks. Mesuré : **~2,2 % des `POST /mcp` de production**, en régime
permanent, dont on ne savait rien — la seule trace était la ligne de statut du journal
d'accès uvicorn, qui ne porte que le code HTTP et vit **deux jours**. Et il faut la
chercher sous les unités **colorées** (`journalctl -u oto-mcp@blue`), pas sous
`oto-mcp`, sinon on conclut « aucun journal », ce qui est un zéro crédible et faux.

`oto_mcp/transport_refusals.py` les compte désormais **avec leur cause**, depuis un
middleware ASGI posé dans `build_root_app`. La cause ne se lit que dans le corps de la
réponse : **cinq** refus différents rendent 400 (analyse, validation, `Mcp-Session-Id`
manquant, version de protocole, `Content-Type`) et le code JSON-RPC ne les sépare pas
non plus (`-32600` en couvre deux). Ces lignes portent `kind='transport'`, leur cause
dans `tool` (`refus:<cause>`), et l'environnement dans `args` — indispensable, puisque
préproduction et production écrivent dans la MÊME base et que `tool_calls.server` est un
littéral constant. Aucun `sub` : à cette couche il n'y a pas encore d'identité.

**Les 401/403 en sont exclus, délibérément** : ils viennent de la couche d'auth, que le
transport ne voit jamais, et sur `/mcp` un 401 est l'étape NORMALE de la découverte
OAuth — les compter noierait le signal sous le fonctionnement nominal. Constaté en
déploiement, pas en banc : la première version en a compté six en cinq minutes sur la
préproduction, tous étiquetés `autre`.

Lecture : `oto_admin_monitoring op=transport` / `GET /api/admin/monitoring/transport`.
⚠️ **Un volume non nul est le régime NORMAL, pas une panne.** Toutes les adresses
sources mesurées le 11/09/2026 étaient dans `160.79.106.0/24`, la plage de sortie de
claude.ai — aucune de nos machines. Ce sont des clients tiers qui parlent mal.

Pas la connexion d'un connecteur, pas le `tools/list`. (Ce paragraphe disait « uniquement
les invocations d'outils » jusqu'au 2026-08-29 : les appels `/api/*` y sont écrits depuis
`RestCallLogger`, et le handshake depuis `on_initialize` — c'est cet angle mort de lecture
qui a laissé passer #558.) Donc **compte actif ≠ usage** — un user avec un compte
(table `users`) mais 0 ligne `tool_calls` n'a jamais déclenché d'outil (connecté-mais-idle
OU handshake OAuth jamais réussi → diagnostiquer via `journalctl` 401). Vécu 2026-06-22.

`sentry_event_id` n'est posé que sur une erreur de **code** : une erreur GÉRÉE (4xx amont,
refus d'entrée) n'est pas capturée par Sentry (`before_send` la droppe), donc pas stampée.
Une ligne en erreur sans event id est donc normale — et informative : c'est un refus, pas
un bug.

Volumétrie bornée par le timer `oto-journal-archive` : il EXPORTE au froid S3 les mois
entiers au-delà de `OTO_JOURNAL_RETENTION_DAYS` (défaut **90 j**), puis les supprime.

⚠️ **Corrigé le 2026-08-28 (ADR 0065 lot 0, oto-backend#426), et il faut le savoir pour
lire un chiffre ancien** : jusque-là le boot purgeait `tool_calls` à **30 jours sans
archiver**, donc plus court que la politique écrite — et il vidait d'avance ce que
l'archive posée le 27/08 devait prendre (mesuré : 0 ligne au-delà de 30 j sur 969 314).
La rétention effective était d'un mois, personne ne l'avait décidé, et rien n'était parti
au froid. Depuis, elle a **un seul propriétaire**, l'archive. Le premier mois réellement
archivé sera août 2026, au tir du 2026-12-03.

**Un déroulé s'efface entier** (#289) : à la même borne, `oto-mcp maintenance retention`
(timer quotidien) retire les lignes `runs` qui viennent de perdre tous leurs faits. Un run *est* ses faits (ADR
0058-D2) et sa page est assemblée à la lecture — garder l'étiquette au-delà rendait, au
31ᵉ jour, une page VIDE sous une ligne qui annonçait « done ». Deux gardes : l'étiquette
d'un run **encore vivant** (ouvert il y a 40 jours, appelé hier) n'est jamais touchée, et
celle d'un run **récent** non plus, même si sa journalisation a échoué (best-effort).
Conséquence sur les lectures dérivées de `runs` (`project_runs`, `project_run_stats`,
pastille de procédure) : elles ne remontent pas au-delà de la fenêtre de rétention.
Elles ne portent en outre que sur les `PROJET_RUNS_RECENTS` (500) derniers runs du
projet (#1145) : le plus gros projet en porte environ 86 000, et chaque ouverture les
reconstruisait tous depuis le journal (plus d'un million de lignes lues). Une procédure
déroulée seulement avant ces 500-là se lit inerte dans l'audit, et sa pastille est vide.
⚠️ Ce filet ne joue PAS pour l'archive : elle exempte `run_start`/`run_finish`, donc un
run archivé garde ses faits et son étiquette — c'est la section suivante.

**Un run archivé garde ses bornes, et sa page le dit** (#665, arbitrage d'Alexis du
23/09/2026, option B). Passé la rétention, le run reste listé avec ses dates et son
issue ; son corps est au froid. Pour que sa page ne retombe pas sur la page vide de #289,
l'archive inscrit chaque mois dans le registre `journal_archives` (révision
`0005_journal_archives` ; mois, objet S3,
lignes relues, date) **après** la relecture qui autorise la suppression et **avant** la
suppression — si l'inscription échoue, rien n'est supprimé. La page d'un run
(`GET /api/orgs/{id}/monitoring/runs/{run_id}`, `GET /api/admin/usage/runs/{run_id}`,
`op=run` des deux consoles) porte alors `content_archived` — `archived_at`, `months`, et
`message` (« Contenu archivé le … ») à afficher **à la place** du contenu. Le registre
est lu, jamais déduit d'un corps absent : un run sans appel entre ses bornes n'est pas un
run archivé. Rendu côté front : à faire dans `oto-dashboard` (le champ est servi).
⚠️ Le timer exécute une **copie installée** du script (`/usr/local/sbin/oto-journal-archive.py`,
cf. l'unité) : elle doit être rafraîchie depuis `deploy/archive_tool_calls.py` avant le
premier tir qui archive, sinon le mois part sans être inscrit.

## Les deux surfaces (mêmes capacités, ADR 0009/0042)

Les lentilles vivent dans `capabilities/monitoring.py` — **un handler, deux faces**, autz
`PLATFORM_ADMIN` déclarée une fois. Ne pas rajouter de route écrite à la main ici.

**Face REST** (dashboard `/platform/monitoring`) :
`GET /api/admin/monitoring/{summary,rest,connectors,funnel,calls,calls/{id}}`.

**Face MCP** (agent) : console consolidée `oto_admin_monitoring(op=…)` — pattern ADR 0047,
un outil, verbe en `op` :

| op | pour | paramètres utiles |
|---|---|---|
| `summary` | agrégats (totaux, par outil avec avg+p95, par user, par jour) | `days`, `org_id`, `sub` |
| `calls` | le journal brut filtré — chaque ligne porte `arg_keys` et `result_shape`, jamais `args` | `tool`, `sub`, `errors`, `days`, `org_id`, `run_id`, `session_id`, `min_duration_ms`, `error_contains` |
| `call` | la fiche d'UN appel (`call.args` tels que journalisés + corrélation) | `call_id` |
| `run` / `runs` | timeline d'un déroulé / déroulés récents | `run_id`, `limit` |
| `rest` | lentille REST par route (`/api/*`) — **les gestes du tableau de bord sont ICI, pas dans `calls`**. `by_status` ventile les erreurs par code HTTP (oto#179) : un 4xx attendu ne se lit plus comme une panne ; `status: null` = aucune réponse journalisée, c'est-à-dire une exception non rattrapée (500 servi plus haut) ou un client parti | `days`, `org_id`, `sub`, `route` |
| `connectors` / `funnel` | santé connecteurs / activation | `days` (+ `org_id` pour `connectors`) |
| `gaps` / `tool_quality` | signaux d'usage agrégés | `days` |

`sub` accepte un **email OU un sub** (on enquête sur « les appels de jane@acme.test », pas sur un
identifiant opaque). Les signaux bruts et leur résolution restent sur `oto_admin_signal`.

⚠️ **Un paramètre que l'op ne lit pas est REFUSÉ** (`param_not_read_by_op`), jamais
ignoré. Jusqu'au 2026-09-03, `op=rest` acceptait `sub`/`org_id` et les jetait : on
croyait lire l'activité REST d'un compte, on lisait celle de toute la plateforme, et
aucune forme de la réponse ne distinguait les deux (#451). Les deux axes sont
désormais honorés — mais ⚠️ **`org_id` d'une ligne REST vient de l'en-tête de
consultation** posé par le client (`RestCallLogger`, best-effort), pas d'une
résolution : une requête sans en-tête ne porte aucune org et sort du filtre, donc un
0 sous `org_id` ne prouve pas une org inactive. La réponse le dit (`org_id_caveat`).

⚠️ **`summary` et `calls` ne portent que le flux AGENT (`kind='mcp'`)** : ce qu'une
personne fait depuis le tableau de bord ou l'API n'y figure JAMAIS — c'est `op=rest`.
**Zéro appel n'est donc pas un compte inactif**, et c'était la deuxième moitié du
#451 : une enquête honnête concluait « ce compte ne s'est jamais servi d'oto » sur
une lentille qui, par construction, ne pouvait pas voir ses gestes.

### Recette : enquêter sur une erreur

1. `op=summary` → quel outil concentre les échecs.
2. `op=calls` avec `tool=` + `errors=true` → les lignes fautives.
3. `op=call` sur une ligne → args, org, surface cliente, déroulé, **event Sentry**.
4. `op=calls` avec le `run_id` (ou `session_id`) de la fiche → ce qui s'est passé autour.

Pour un gel d'event loop : `op=calls` avec `min_duration_ms=5000` (cf.
`docs/event-loop-perf.md`). Pour la fenêtre longue, `days=` jusqu'à 365.

Côté dashboard, ces mêmes gestes sont l'onglet « journal » : filtres serveur, ligne
dépliable en fiche, axes de corrélation cliquables (ils refiltrent le journal), lien
Sentry quand l'event id est présent (gaté sur `VITE_SENTRY_ORG_URL` — sans lui, l'id est
rendu copiable plutôt qu'un lien cassé).

## Trois étages, un seul journal

La même table sert trois sièges, qui ne diffèrent que par le SCOPE — jamais par le
mécanisme, jamais par une projection dupliquée :

| étage | qui | ce qu'il voit | surface |
|---|---|---|---|
| membre | tout user | SON activité dans l'org active | `GET /api/me/{activity-summary,calls}` (agrégats : une org active, sinon `400 no_active_org` — #1145) |
| **org** | **org_admin** | **tout ce qui a été émis SOUS son org** | **`oto_org_monitoring(op=…)` + `GET /api/orgs/{id}/monitoring/*`** |
| plateforme | platform_admin | tout | `oto_admin_monitoring(op=…)` + `/api/admin/monitoring/*` |

**Scope org = `tool_calls.org_id` / `usage_signals.org_id`, jamais l'appartenance du
membre.** Un membre de N orgs n'apporte à chaque étage que ce qu'il a fait sous celle-là,
donc les chiffres d'un écran org et ceux de l'export d'audit (#67) coïncident par
construction. ⚠ Les appels antérieurs à la colonne `org_id` (NULL) sont invisibles à
l'étage org — non reconstructibles.

L'étage org (`capabilities/org_monitoring.py`, autz `ORG_ADMIN_OF`) rejoue les lentilles
plateforme avec `org_id` posé, **plus une** qui n'existe qu'à cet étage, et **moins deux** :

| op | note |
|---|---|
| `summary` · `calls` · `call` · `runs` · `run` · `connectors` · `gaps` · `tool_quality` | mêmes projections, `org_id` passé |
| `adoption` | **propre à l'org** — membre par membre : qui s'en sert, qui n'a jamais essayé, qui est bloqué par un connecteur. Part d'`org_members` (sinon un membre à 0 appel serait invisible — c'est justement lui qu'on cherche) |
| `export` | rebranche `org.audit_log.export` (#67), même autz, même scope. **Dit sa complétude** (`total`/`truncated`/`next_cursor`, #770) — voir plus bas |
| ~~`rest`~~ · ~~`funnel`~~ | ne descendent pas : télémétrie de surface `/api/*` et comptes de toute la base = santé d'infra, pas usage d'org. `adoption` répond à la question du funnel à l'échelle d'une équipe |

**Gardes cross-org à ne pas perdre** : `call_id` est un BIGSERIAL donc devinable →
`op=call` compare `row.org_id` et rend le **même 404** qu'un id inexistant ; `op=run`
filtre en SQL puis 404 sur timeline vide. Testé par `tests/test_org_monitoring.py` — un
handler ajouté sans sa garde y casse.

**Le scope se DIT, et il compte ce qu'il laisse dehors (#630, 29/08).** Un `data_write`
refusé à 21:11:23 était dans `op=run` (les 17 appels du run) et absent de `op=calls
org_id=<l'org du run>` interrogé trois fois avec des motifs que son texte contenait — parce qu'il
avait été RÉSOLU sous l'org maison de l'appelant (axe `_org` absent, #631), donc stampé
avec l'org maison. La vue était exacte dans son périmètre ; le lecteur ne le connaissait pas, et
un « zéro » lu là était un plancher muet. `op=calls` scopé à une org (org ou plateforme
avec `org_id`) rend désormais, à côté des lignes : `scope` (la règle), `hors_scope` (les
appels des runs de l'org stampés sous une autre org, sous LES MÊMES filtres — même à 0)
et `hors_scope_hint` (où les voir : `op=run`). Fenêtre du plancher = `days`, sinon la
page quand elle est pleine, sinon 30 j — dite dans l'indice ; jamais sans borne
(28 ms/jour mesurés en prod). La construction des filtres est partagée
(`db/journal_calls.py`) : la page et son plancher ne peuvent pas diverger.
**Depuis le 30/08 (#639)**, la cause du cas mesuré n'existe plus : un appel sans `_org`
dans un run est résolu — donc stampé — dans l'org du run. `hors_scope` reste, pour ce
qu'un axe explicite continue légitimement de mettre dehors (agent multi-org).

## L'export d'audit dit s'il est complet — ou dit qu'il ne peut pas (#770, 01/09)

`GET /api/orgs/{id}/audit-log/export` n'est pas une lentille de confort : c'est la
pièce qu'un client produit **pour se justifier** devant un auditeur ou un délégué à la
protection des données. Il ne rendait que `count = len(calls)` APRÈS troncature — un
fichier de 1000 lignes ne disait donc pas si 1000 ou 50 000 appels avaient eu lieu.
**Une pièce qui ne dit pas si elle est complète n'atteste de rien**, et une absence
dans une vue plafonnée se lit comme un zéro.

| champ | ce qu'il dit |
|---|---|
| `total` | la population de la **fenêtre**, indépendante de `limit` et du curseur |
| `count` | les lignes de **cette réponse** (`len(calls)`) |
| `truncated` | cette réponse ne porte pas toute la fenêtre |
| `next_cursor` | opaque, à renvoyer tel quel ; `null` = plus rien après cette page |
| `until_effectif` | la borne haute **réellement appliquée** ; `until` reste le réécho de ce qui a été reçu |

⚠️ **Un total calculé sur un autre jeu que la page qu'il coiffe est PIRE que pas de
total** : il a l'air d'attester. C'est la faute corrigée le même jour sur `node_rows`
(#621), où le pied du tableau comptait des noms de colonnes non résolus pendant que la
page les résolvait — sans que rien n'échoue. Trois mécanismes l'interdisent ici, et
aucun n'est une intention : **une seule construction de clauses**
(`usage._audit_window_clauses`, comme `journal_calls.call_filter_clauses` pour la page
et son plancher) ; **une seule transaction en REPEATABLE READ**, donc un snapshot
partagé ; **une borne haute toujours posée**, gelée au premier appel et reportée par le
curseur.

**Le curseur porte la FENÊTRE, pas seulement la position** — sans quoi elle se
rouvrirait à chaque page (journal alimenté en continu, trié récent d'abord) et le
`total` de la page 2 dépasserait celui de la page 1 : deux vérités successives, et une
concaténation qui ne vaut plus son total. Corollaire servi : repasser `since`/`until`
avec un `cursor` est **refusé** (`400 window_with_cursor`), jamais ignoré ; un curseur
abîmé rend `400 invalid_cursor` (même code et même geste que `node_rows`), pas un 500.
Le gel de la borne haute répond du même coup à la vraie demande de conformité : un
export sur une **période fermée**.

**Le curseur nomme SON org, et on le vérifie** — même garde d'identité que `node_rows`
(le namespace résolu doit être celui que le nœud désigne). Rejoué sur une autre org dont
l'appelant est aussi administrateur, il rendrait une page prise à la position d'un AUTRE
export : des lignes sautées, aucune erreur, et un `total` qui décrit pourtant bien la
nouvelle fenêtre — une pièce qui a l'air entière sans l'être.

⚠️ **Le keyset se bâtit sur un horodatage à la microseconde**, jamais sur le
`created_at` servi : le row factory tronque ce dernier à la seconde
(`_conn._normalize_value`). Un curseur bâti sur la valeur servie sauterait, en silence,
les lignes qui partagent la seconde de la dernière ligne de la page.

**Ce que `total` ne peut pas dire, et qui est écrit dans le schéma servi** (pas
seulement ici, parce que le lecteur d'un export ne lit pas cette page) : il compte ce
qui **existe**, pas ce qui a eu lieu. En deçà de la rétention (ci-dessous) et pour les
appels antérieurs à la colonne `org_id`, un `total: 0` ne veut pas dire « rien n'a eu
lieu ». C'est la borne basse historique de l'instrument.

## Le relevé de consommation : appel par appel, ou par outil en une lecture (#1145, 04/10)

Deux lentilles MEMBRE, même étroitesse (ni `sub`, ni email, ni erreur), mêmes appels
comptés (`kind='mcp'`, sous l'org, réussis, dans la fenêtre) :

| route | ce qu'elle rend |
|---|---|
| `GET /api/orgs/{id}/usage/calls?tool=…` ou `?run_id=…` | les appels, page par page, avec `total` de la fenêtre et curseur — le détail (job, trouvé, run) |
| `GET /api/orgs/{id}/usage/tools[?tool=a&tool=b]` | par outil × mode de clé : `calls`, `quantity` (NULL compté 1), `jobs` distincts — **une** lecture pour tous les outils |

La somme des `calls` d'un outil égale le `total` de `usage/calls` sur la même fenêtre.
Le consommateur qui relisait chaque outil à chaque rafraîchissement (une requête par
outil, toutes dans la même seconde) lit `usage/tools`, puis `usage/calls` pour le
seul outil dont il veut le détail.

**Bornes, servies et nommées** : une fenêtre d'au plus `RELEVE_FENETRE_MAX_JOURS`
(92 j, la rétention du journal plus une marge) — sans `since`, la fenêtre maximale
s'applique et `since_effectif` la rend ; au-delà, `400 window_too_large`, jamais une
fenêtre rognée. Une page d'au plus `RELEVE_LIMITE_MAX` (5 000) lignes — au-delà,
`400 limit_too_large`, là où la valeur était écrêtée en silence. Les deux lectures
passent par `idx_tool_calls_org_tool_ok (org_id, tool, created_at DESC) WHERE ok`
(révision 0032).

`usage/calls` garde ses deux lectures (le `total` puis la page) : avec l'index, le
compte est un parcours d'index sur l'org et l'outil, et le replier dans la page
(`count(*) OVER ()`) forcerait à extraire les arguments JSON de TOUTES les lignes de la
fenêtre au lieu des seules lignes de la page.

## Les lectures d'agrégat sont bornées à 10 s (#1145, 04/10)

Le pool applicatif ne pose aucun `statement_timeout` (les migrations de démarrage en
ont besoin). Les lectures d'agrégat sur `tool_calls` en reçoivent un, à LEUR niveau :
`db.lecture_bornee.lecture_d_agregat(objet)` remplace `_connect()`, ouvre une
transaction et y pose `SET LOCAL statement_timeout` = `DUREE_MAX_MS` (10 s) — `LOCAL`,
donc rendu à la fin de la transaction, jamais laissé sur une connexion du pool. Un
dépassement lève `LectureTropLongue`, que l'enveloppe `capabilities._lecture_bornee.bornee`
rend en **`503 aggregate_timeout`**, message compris (« resserrer la fenêtre ou le
périmètre »), sur les deux faces — jamais un résultat partiel, jamais un 500 anonyme.

Lectures bornées (celles qui passent par les totaux par jour depuis #1147 comprises) :
`list_billable_calls_for_org`, `billable_usage_by_tool_for_org`,
`instruction_usage`, `tool_call_stats`, `rest_call_stats`, `connector_failure_stats`,
`activation_funnel`, `list_tenants_overview`, `get_tenant_overview` ; depuis le 08/10
(infra#9, « plus de route lourde ») aussi `list_runs`, `list_tool_calls`,
`count_calls_of_org_runs_elsewhere`, `list_rest_calls`, `list_view_as_writes`,
`transport_refusal_stats`, `org_adoption`, `export_tool_calls_for_org`. Capacités
enveloppées : `org.usage.{calls,tools}`, `org.instruction.usage`, `me.{activity_summary,calls}`,
`org.monitoring.{summary,console,connectors,calls,runs,adoption,view_as_writes}`,
`monitoring.{summary,rest,connectors,funnel,calls,rest_calls,transport}`, `usage.runs`,
`org.audit_log.export`, `admin.monitoring`, `admin.{tenants,tenant,tenant_console}`
(`tests/test_lecture_bornee.py` tient la liste). La borne est un FILET : ce qui rend une
lecture légère, c'est sa forme (fenêtre, page, index) — ci-dessous.

**Une liste de runs choisit sa page AVANT de reconstruire** (infra#9) : `list_runs`
(`/api/admin/usage/runs`, `/api/orgs/{id}/monitoring/runs`, `op=runs` des deux consoles)
et `recent_runs` (bloc C du handshake) prennent leurs N dernières ouvertures `run_start`
dans le journal (`_derniers_runs`), puis ne reconstruisent qu'elles. Avant, la liste
groupait TOUT le journal par run pour compter les appels, et reconstruisait tous les runs
de sa portée — les ouvertures n'étant jamais archivées, le coût suivait l'historique
entier, pas la page. Le compte d'appels d'un run est un LATERAL servi par
`idx_tool_calls_run`.
Le choix de page lui-même est servi par trois index partiels des ouvertures
(`index_releve.OUVERTURES`, révision 0049 : plateforme, org, compte × org), rangés dans
l'ordre de la page : sans eux, il parcourait le journal à rebours (5 s pour la page
plateforme, plus de 15 s pour la plus grosse org, mesurés le 09/10). D'où des égalités
dans la portée (`org_id = %s` ou `IS NULL`), jamais `IS NOT DISTINCT FROM`, que l'index
ne sait pas servir dans l'ordre.

**L'entonnoir lit une seule fenêtre** : « REST seul » (`rest_only`) compte les comptes
venus en REST SANS appel d'outil sur la fenêtre `days`, comme `active` et `blocked` —
il lisait jusqu'au 08/10 le journal entier, deux fois, et sortait coupé à 10 s.

**Le résumé plateforme SANS périmètre est borné à 7 jours** (`monitoring.summary`, et
`oto_admin_monitoring op=summary` sans `org_id` ni `sub`) : il lit le journal de toute
la plateforme — 452 s pour un jour sous contention le 04/10, un parcours séquentiel
d'environ 1,35 M lignes pour 60 jours. Au-delà, `400 days_too_large`, qui dit de passer
`org_id` ou `sub` (fenêtre jusqu'à 90 jours). Les totaux par jour (#1147, ci-dessous) servent
désormais cette lecture ; la borne reste, la vue plateforme partant vers Grafana.

**La fiche d'un tenant part de SES comptes**, primaire compris
(`tenants._overview_par_comptes`) : ses subs d'abord, puis le journal en UNE passe
groupée par sub — là où la passe générique classait chaque utilisateur par
sous-requête corrélée et lisait la fenêtre deux fois. Pour le primaire, dont les comptes
sont presque tous ceux de la plateforme, cette passe lit toute la fenêtre — sur les
totaux par jour depuis #1147, plus le journal direct pour la veille et le jour courant.

Ce que la borne ne fait pas : limiter le nombre de lectures simultanées par route ni
le débit par jeton — c'est le budget des routes lourdes, posé à part.

## Les totaux du journal par jour UTC (#1147, 09/10)

Les écrans de consommation et de monitoring n'ont pas à relire `tool_calls` (~12 M
lignes, `args` compris) pour chaque fenêtre de 30 ou 90 jours. Trois tables
(`db/schema/usage.py::JOURNAL_JOUR`, révision 0050) portent les jours CLOS, et
`db/journal_jour.py` les tient :

| table | ce qu'elle porte |
|---|---|
| `journal_jours_consolides` | le registre : un jour y figure = ses totaux sont COMPLETS (un jour sans appel y figure aussi) |
| `journal_totaux_jour` | par jour × `kind` × `org_id` × `sub` × `tool` × `ok` × `key_mode` × émetteur : `appels`, `quantite` (NULL compté 1), durées et tailles (nombre, somme, et les VALEURS en `int[]`), `dernier_at` |
| `journal_jobs_jour` | les jobs fournisseur distincts relevés par les appels facturables, par jour × org × outil × mode de clé |

**Exact, pas approché.** Les dimensions sont celles que lisent les écrans, et rien de
plus. Une somme s'additionne d'un jour à l'autre ; un DISTINCT non — d'où `sub` et
`org_id` en dimensions (comptes actifs, membres), la table des jobs (jobs distincts du
relevé), et les valeurs de durée et de taille gardées telles quelles : un p95
(`percentile_cont`) recalculé sur leur union est celui du journal, là où un histogramme
à seaux l'aurait approché (mesuré : ~20 octets par appel agrégé, en-têtes compris — de
l'ordre de 250 Mo pour 12 M lignes, contre 5 Go de journal). Natures
agrégées : `mcp` et `connector` ; le REST (le flux le plus gros), le protocole et le
transport restent au journal — leurs lecteurs sont des vues de la plateforme.

**Alimentation : la maintenance** (`oto-mcp maintenance journal-jour`, en tête de
`all`, timer quotidien de 03:20, prod seulement) consolide la veille et les jours clos
manqués depuis le dernier consolidé, au plus 7. Un jour se consolide en UNE transaction
(`consolider_jour` : retrait du registre — totaux et jobs partent en cascade —,
réinscription, `INSERT … SELECT`), sous `statement_timeout`, agrégation par tri
(`enable_hashagg = off` : pas un tableau par groupe en mémoire sur une nano de 4 Go),
verrou consultatif contre une consolidation concurrente du même jour. Idempotente ; le
jour courant est refusé (`JourNonClos`).

**Rattrapage : à la main**, une fois (`scripts/rattraper_journal_jour.py`, à blanc par
défaut, `--appliquer` pour écrire) : un jour par transaction, une pause entre deux,
dans l'ordre qui garde la couverture contiguë (après le dernier consolidé en avançant,
puis avant le premier en reculant), reprise idempotente, arrêt au premier jour qui
dépasse sa borne. Sur une base neuve (instance cible, `perimetre`), il se rejoue une
fois le journal versé.

**Les lecteurs** (`journal_jour.source`) découpent leur fenêtre en trois morceaux
disjoints : le journal direct jusqu'au premier jour entier consolidé, les TOTAUX des
jours entiers que le registre porte, le journal direct après le dernier (la veille tant
que la maintenance n'est pas passée, le jour courant, le bout d'un jour qu'une borne
coupe). Les morceaux directs passent par la MÊME projection que la consolidation, en
plages simples de `created_at` (jamais un `OR`). Le lecteur agrège par-dessus (somme,
max, `count(DISTINCT sub)`, `percentile_cont` sur les valeurs) et rend le MÊME contrat
qu'avant — `tests/db/test_journal_jour_lecteurs.py` compare, lecteur par lecteur, sa
réponse à l'ancienne lecture du journal, recopiée dans le banc.

| lecteur | surfaces | sur les totaux |
|---|---|---|
| `billable_usage_by_tool_for_org` | `GET /api/orgs/{id}/usage/tools` (`org.usage.tools`) | oui — jobs distincts par la table des clés |
| `org_usage_by_person` | `service.org.usage` (commerce) | oui — bornes `[since, until)` |
| `tool_call_stats` | `monitoring.summary`, `org.monitoring.summary`, `me.activity_summary`, consoles `op=summary` | oui — p95 sur les valeurs |
| `connector_failure_stats` | `monitoring.connectors`, `org.monitoring.connectors`, `op=connectors` | oui |
| `org_adoption` | `org.monitoring.adoption`, `op=adoption` | oui — le dernier appel sur tout l'historique consolidé |
| `list_tenants_overview`, `get_tenant_overview` | `admin.tenants`, `admin.tenant`, console de tenant | oui |
| `list_billable_calls_for_org`, `list_tool_calls`, `list_runs`, `export_tool_calls_for_org`, activité d'un tableau | `usage/calls`, `calls`, `runs`, `export`… | non — des LISTES, ligne à ligne |
| `rest_call_stats`, `list_rest_calls`, `transport_refusal_stats`, `activation_funnel` | `monitoring.{rest,rest_calls,transport,funnel}` | non — le REST n'est pas agrégé (vues plateforme, vers Grafana) |
| `instruction_usage`, `instructions_usage_by_slug` | `org.instruction.usage`, `me/instructions-usage` | non — la procédure vient d'`args`, servie par l'index partiel `(org_id, tool) WHERE ok` sur deux verbes |
| `org_members_by_seniority` | `service.org.members` | non — un dernier appel par membre, pas une période |

**Refus nommé, jamais le journal en silence** : un jour clos de la fenêtre que le
registre n'a pas lève `AgregatIncomplet` — trou dans le registre, retard (un jour clos
depuis plus d'un jour après le dernier consolidé : la maintenance n'est pas passée), ou
historique non rattrapé (le journal a des lignes dans un jour entier de la fenêtre avant
le premier consolidé). Les capacités enveloppées par `bornee` le rendent en **`503
aggregate_incomplete`**, motif et geste compris, et le journalisent en erreur ;
`service.org.usage` le laisse en 500. La veille non encore consolidée se lit en direct.

**Fenêtres inchangées** : 92 j pour `usage/tools` (elle est partagée avec `usage/calls`,
qui lit le journal ; l'élargir romprait l'égalité « somme des `calls` = `total` de
`usage/calls` » au-delà de la rétention), 7 j pour le résumé plateforme sans périmètre
(la vue part vers Grafana), 90 j pour un résumé d'org ou de compte.

Ce qui n'est pas suivi : la purge d'archive (`deploy/archive_tool_calls.py`) retire des
mois du journal, pas leurs totaux — une fenêtre plus longue que la rétention lit donc
au-delà de ce que le journal garde. `migrate_sub` repointe `journal_totaux_jour.sub`
avec le journal.

## Rétention : 90 jours en ligne, le reste en froid (posé le 2026-08-27)

Le journal n'avait **aucune** rétention : 47 % de la base, et une croissance passée de
9 600 à ~90 000 lignes/jour en deux semaines sous la charge d'une campagne de runner.
Décidé par Alexis le 27/08 : **90 jours consultables**, au-delà chaque mois clos part en
CSV compressé sur l'Object Storage (`journal/tool_calls/YYYY-MM.csv.gz`, objet **privé**)
avant d'être effacé de la base. Travail **quotidien** `oto-journal-archive.timer` (04:45
UTC ; mensuel, le 3, jusqu'au correctif de #1197), script versionné
`deploy/archive_tool_calls.py`. Un passage sans mois éligible ne fait rien ; un mois
devient éligible le jour où il sort entièrement de la fenêtre (fin du mois + 90 jours).

**Ce n'est pas une purge de logs, et c'est le point à comprendre avant d'y toucher.**
Cette table est à double emploi : journal d'observabilité, ET **source de vérité des
exécutions** — un run n'est pas stocké, il est reconstruit depuis ses faits, qui sont
deux lignes d'ici (`run_start` / `run_finish`). Les effacer effacerait l'historique des
runs. Ils sont donc **exemptés de toute suppression** ; ils pèsent ~3 % du volume, les
garder indéfiniment ne coûte rien. ⚠️ Conséquence assumée : un run dont les appels
ordinaires ont été archivés garde son ouverture, sa clôture et son issue, mais son
« dernier signe de vie » retombe sur sa date d'ouverture (`last_seen_at` se dérive du
dernier appel rattaché) — sans effet sur un run clos, et un run resté ouvert depuis plus
de 90 jours est de toute façon lu comme silencieux. Sa page, elle, dit « contenu archivé
le … » (registre `journal_archives`, #665 — cf. plus haut).

**Trois précautions dans le script, chacune payée par une mesure du jour même :**
- **La suppression n'est autorisée que par une RELECTURE de l'archive** (téléchargée,
  décompressée, parsée, recomptée contre la base). Comparer la taille déposée à la taille
  locale prouve que l'upload n'a rien perdu — pas que l'export contenait tout, ni qu'il se
  relit. Sur une opération irréversible, la seule preuve est de refaire le chemin.
- **Le mois doit être ENTIÈREMENT sorti de la fenêtre.** Archiver un mois à cheval
  déposerait un fichier incomplet que la passe suivante ne compléterait pas (l'objet
  existe déjà) : des lignes effacées sans copie nulle part.
- **Ne jamais compter les enregistrements en comptant les sauts de ligne** du flux CSV :
  `args` et `error` en contiennent. Mesuré ici — 12 830 « lignes » annoncées pour 12 459
  enregistrements réels. Le seul compte juste est celui de la base.

**Un passage interrompu se reprend, et une archive ne se réécrit jamais** (#1197). Avant
le correctif, un passage tué pendant la suppression (délai de 3 h du service, crash)
laissait un reste ; le passage suivant RÉÉCRIVAIT l'objet du mois avec ce seul reste, et
l'inscription avec son compte — les lignes déjà supprimées n'étaient plus nulle part (le
versionnage du bucket est suspendu, il ne rattrape rien). Désormais l'état du mois se
LIT avant d'agir :

| registre `journal_archives` | objet S3 | ce que fait le passage |
| --- | --- | --- |
| absent | absent | nominal : export, relecture, inscription, suppression |
| présent | présent | **reprise** : relit l'objet, exige son compte = l'inscription et CHAQUE ligne restante présente par son `id`, puis finit la suppression — sans réexporter |
| absent | présent | passage coupé entre dépôt et inscription, ou `--export-only` : l'objet est **adopté** (inscrit, pas réécrit) s'il porte exactement les lignes en base — même compte, aucun doublon, chaque `id` couvert |
| présent | absent | erreur |

Tout écart lève `ArchiveIncoherente`, dont le message dit quoi vérifier, et rien n'est
supprimé : objet relu à un autre compte que l'inscription (remplacé ou tronqué), objet
sans inscription qui n'est pas l'export de ce qui reste, ligne entrée dans le mois après
l'export, inscription qui désigne un autre objet, objet inscrit introuvable — ce dernier
cas est le plus grave : ne PAS retirer l'inscription pour réexporter, retrouver l'objet.
L'export lui-même refuse d'écrire sur un objet existant, et un refus d'accès au `HEAD`
n'est jamais pris pour une absence. Une inscription ne se réécrit plus (elle se posait
en `ON CONFLICT DO UPDATE`). Bancs : `tests/deploy/test_archive_journal_reprise_1197.py`.

**La suppression avance par l'index** (#1197). Son prédicat
`to_char(date_trunc('month', created_at), 'YYYY-MM') = mois` ne servait aucun index :
chaque lot de 20 000 relisait la table depuis son début (ou toute la table, selon le plan)
— 53 s pour un mois d'un million de lignes à l'étude, plus de 3 h estimées pour
septembre 2026 (8 à 9 M lignes sur ~12 M). Elle filtre maintenant sur la PLAGE
`created_at >= début AND created_at < fin` (bornes calculées par la base dans le fuseau
de la session, le même que celui du `date_trunc` qui compte les mois), sert
`idx_tool_calls_created_at`, et chaque lot repart de la date de la dernière ligne
supprimée : il ne relit ni la table ni ce qui est déjà purgé. L'export lit la même plage,
sans `ORDER BY` (les lignes sortent dans l'ordre de lecture, chacune porte son `id`).
Mesuré sur une base jetable de 6 M lignes, DDL et 13 index réels, mois de 1,94 M lignes :

| | avant | après |
| --- | --- | --- |
| plan d'un lot | parcours de la clé primaire, filtre : 1,2 M lignes rejetées dès le 1er lot | `Index Scan Backward using idx_tool_calls_created_at`, `Index Cond` sur la plage |
| durée d'un lot | 0,62 s médian, 2 s au dernier (il parcourt tout le reste de la table) | 0,06 s médian, 0,09 s max, constant |
| suppression du mois (sans pause) | 63 s | 5,9 s |
| WAL | 818 Mo (442 o/ligne) | 636 Mo (344 o/ligne), 6,6 Mo par lot |

Le WAL d'une suppression est surtout fait de pages entières : la prod checkpointe toutes
les 30 s (`max_wal_size` 1 Go), et chaque page de tas touchée pour la première fois après
un checkpoint s'y écrit en entier — ~une page par ~44 lignes en prod (183 o/ligne de
tas), d'où **~245 o/ligne estimés en prod**, soit **~2,1 Go pour 9 M lignes**, plus ~1 Go
pour l'autovacuum qui suit (mesuré au banc à ~130 o/ligne). D'où la **pause entre deux
lots, réglable** (`--pause S`, 1 s par défaut) : ~4,7 Mo de WAL par lot en prod, soit
~130 Mo par fenêtre de checkpoint — loin du `max_wal_size` qui forcerait des
checkpoints. Sans pause, ~60 Mo/s atteindraient ce plafond en une vingtaine de secondes.
Septembre avec la pause : ~450 lots, une dizaine de minutes de suppression, loin du
`TimeoutStartSec=3h`.

**Où il tourne** : sur la box, en travail planifié, jamais dans le processus MCP —
mono-boucle, et c'est ce même journal qui l'a gelé le 27/08. Un verrou consultatif PG
protège de deux exécutions simultanées (prod et preprod partagent la base). Options :
`--dry-run` (dit ce qui partirait), `--export-only` (dépose et vérifie sans supprimer —
c'est ce qui permet d'éprouver le chemin réel sans engager la moitié irréversible ; le
passage suivant adopte l'objet déposé, cf. plus haut), `--retention-days N` (ou
`OTO_JOURNAL_RETENTION_DAYS`), `--pause S` (entre deux lots de suppression).
⚠️ Le timer n'est pas posé par le déploiement : passer au quotidien demande d'installer
`deploy/oto-journal-archive.timer` sur la box et de recharger systemd.

⚠️ **La rétention à 90 jours n'effacera rien avant fin octobre 2026** : à sa mise en
place, le journal ne remontait qu'au 28/07. Un premier passage qui ne supprime rien est
le comportement attendu, pas une panne.

### Réparer les jetons déjà écrits (#558)

```bash
oto-mcp maintenance journal-tokens            # À BLANC : compte, n'écrit rien
oto-mcp maintenance journal-tokens --apply    # réécrit
```

**Une réparation, pas une suppression** : la ligne reste (qui, quand, quel code, quelle
durée), sa route est ramenée à la forme réduite et l'argument secret à son empreinte.
Ce qu'elle cherche est **dérivé de la même déclaration** que le masquage à l'écriture —
pas d'une seconde liste qui divergerait.

⚠️ **À blanc par défaut, hors timer et hors `all`** (comme `key-index-rebuild`, #421) :
elle réécrit des lignes servies aux lentilles de supervision, sur une base **partagée
prod/preprod**. La lancer est une décision, pas un effet de bord de sortie de maintenance.
Le piège qu'elle évite, et qui justifie son test contre un vrai PostgreSQL : si deux
routes à secret déclarées partagent un préfixe, la passe d'une route GÉNÉRIQUE
écraserait la route réduite par la passe d'une route plus SPÉCIFIQUE sous le même
préfixe, si les préfixes plus spécifiques n'étaient pas exclus — l'exemple concret qui
illustrait ce piège, `/api/invitations/` face à `/api/invitations/code/`, a disparu
avec le code court d'invitation lui-même (15/09/2026, oto-backend#560) ; le mécanisme
générique décrit ici reste valide pour toute paire future de routes qui se
chevaucheraient (ex. `/api/upload/{token}` sous un préfixe plus spécifique).

## Error tracking (Sentry)

Exceptions backend → **Sentry SaaS** (gaté `OTO_SENTRY_DSN`, no-op si absent →
le serveur boote sans). Deux captures : **500 des routes REST `/api/*`** via
l'intégration Starlette (auto) ; **exceptions des tools MCP** via
`SentryToolErrorMiddleware` (`sentry_setup.py`) — une erreur de tool est une erreur
JSON-RPC en **HTTP 200**, invisible à l'intégration Starlette, donc capturée là où
l'exception est vivante (vrai traceback, tag `mcp.tool` + `user.id=sub`). RGPD :
`send_default_pii=False` **et** `include_local_variables=False`. `before_send`
**droppe les 4xx amont** (`HTTP 4xx` d'une API tierce = input rejeté, pas un bug
backend). Env box : `OTO_SENTRY_{DSN,ENV,RELEASE,TRACES_SAMPLE_RATE}` ; région **EU**
`de.sentry.io` (org slug `otomata-vz`). Surveillance/triage = guide oto
`surveillance-erreurs` (token API en SOPS `sentry_api_token`).

⚠️ **Le middleware est le SEUL capteur — deux copies coupées à l'init (oto-backend#869,
2026-09-04)**, mesurées à un triplet exact par erreur (528/528/528, 293/293/293). (1)
`sentry_sdk.integrations.mcp.MCPIntegration` s'AUTO-ACTIVE dès `mcp>=1.15.0` (installé :
1.27.2) et capturait la MÊME `McpError` **sans** le tag `mcp.tool` ni l'utilisateur,
sans passer par `before_send` — d'où une issue Sentry au titre trompeur « Erreur interne
du serveur » ; coupée via `disabled_integrations=[MCPIntegration()]`. (2) La
`LoggingIntegration` relayait `logger.exception("Error calling tool …")` de fastmcp
(logger `fastmcp.server.server`) — même événement que le middleware, sans rien ajouter ;
coupée via `ignore_logger("fastmcp.server.server")`. Les deux coupes ne perdent aucune
information : le middleware capture déjà tout ce qui n'est pas une erreur gérée.

**Troisième source, hors exception : les refus du registre de tenants** (`tenancy._refus`,
07/09). Une ligne `tenants` refusée au boot — slug invalide ou réservé, émetteur ou host
déjà tenu — laisse une déclaration **non chargée** : les jetons du tenant
routent vers le verifier primaire, qui les rejette, donc rien n'échoue de notre côté et
rien ne remonte. Le WARNING existait et est resté **sans destinataire** en préprod (un
tenant non chargé toute une journée) : il part désormais AUSSI en `capture_message` de
niveau `error`, tag `oto.registre_tenants=refus`, **texte identique à la ligne de
journal** (slug + émetteur ⟹ une issue par conflit, pas un fourre-tout). Même précédent
que `loop_watch` : une anomalie d'exploitation qui n'est pas une exception. ⚠️ Ce n'est
pas un rapprochement « déclaré vs chargé » — celui-là existe, en LECTURE, sur
`/platform/tenants` (`pending_restart`) : **un tenant déclaré APRÈS le boot, ou une ligne
sans émetteur (écartée par le SQL), n'alerte toujours pas.** Une base illisible non plus
(`load_tenants`) : elle ne reste pas silencieuse — tout ce qui la touche ensuite lève.

⚠️ **`include_local_variables=False` n'est pas un doublon de `send_default_pii=False`, et
sans lui cette section était FAUSSE** (#564, corrigé le 2026-08-29). Elle affirmait
« jamais les args d'appel dans l'event » : `send_default_pii` ne couvre que ce que le SDK
collecte AUTOMATIQUEMENT (IP, cookies, en-têtes), pas le contenu des frames — et le défaut
du SDK pour les locales est `True`. Chaque exception repartait donc avec les variables
locales de toute la pile, dont celles du chemin de résolution de credential, qui tiennent
le secret **déchiffré**. Un réglage, pas un `before_send` qui scrube : une liste de noms à
scruber redevient fausse au premier renommage. Défense en profondeur dans le même geste :
le `repr` de `ResolvedCredential` et de `CascadeRung` est **expurgé** — c'est l'objet qui
voyage (frame, `logger.debug('%r')`, sérialisation d'un collecteur), pas la variable.
⚠️ **À vérifier hors dépôt** : les events déjà remontés chez le tiers sur la fenêtre de
rétention — le correctif ne les efface pas.
Un appel sur un tool HORS toolbox de session (la visibilité filtre `tools/list`,
pas `tools/call`) = erreur **GÉRÉE actionnable** `tool_not_mounted`
(`error_taxonomy` : oto_call immédiat / `oto_connector op=select`), droppée de
Sentry — plus jamais un « Erreur interne du serveur » opaque (vécu 16/07, #224/#225).

⚠️ **Un statut amont rangé dans la CHAÎNE au lieu d'un attribut est invisible aux deux
mécanismes à la fois.** `_upstream_status` cherche `.status_code` / `.status` /
`.response.status_code` **sur l'objet exception** : un client oto-core qui lève
`Exception(f"… API {status} on …: {corps}")` n'en porte aucun. Le refus tiers échappe
donc *et* au drop de `before_send` (Sentry ouvre une issue de bug backend pour un 4xx
normal) *et* à l'enveloppe, qui tombe en étape (5) « interne » — la seule branche qui
n'écho RIEN du message. **Les deux symptômes ont la même cause, et le journal, lui,
garde le corps en clair** : un écart entre ce que dit `tool_calls.error` et ce que
l'agent a lu est la signature de ce défaut.
Attio en était le cas type (signal #610, 18 refus en 45 jours, 16 remontés à tort) :
Attio nommait le champ fautif et l'enregistrement en conflit, l'agent lisait « Erreur
interne du serveur. » et a conclu à un bug amont inexistant. Corrigé **au tool**
(`tools/attio.py`, `_rendre_le_refus_lisible` posé sur l'unique sortie HTTP du client)
en re-typant vers `UpstreamHTTPError` — le porteur canonique d'oto-core, déjà honoré
ici. ⚠️ **Pas en `McpError`** : elle conserverait le message mais écraserait le verdict
machine (`code`/`retryable`) que la taxonomie dérive du statut. ⚠️ Ce qui est relayé du
corps amont est une **liste blanche bornée** (`code`, `path`, `message`) : un corps tiers
n'est jamais rendu en bloc.

## `client_id` — ce qu'il ne dit pas

> Reprise mot pour mot du `CLAUDE.md` (2026-08-31).

⚠️ **`client_id` n'identifie PAS le front d'où vient l'utilisateur** : énumérer avant d'en
tirer une population.
