---
title: MCP Apps & guides
type: reference
description: >-
  Les tools qui renvoient une interface rendue (`prefab_ui`, extension MCP Apps SEP-1865) : 
  convention `*_app`, import guardé, gotcha de l'annotation de retour, et le régime tout-DB 
  des guides (seeds de boot vs table `guides`).
---

# MCP Apps — UI rendue (SEP-1865) & guides

> Extrait de `CLAUDE.md` le 2026-08-27 — le contenu n'a pas changé, seule sa place a bougé.
> La carte garde le résumé + le pointeur ; le détail (schémas, incidents datés et leurs
> leçons) vit ici.

## Le mécanisme

Certains tools renvoient une **interface rendue** (carte/table dans un iframe
sandbox côté host : claude.ai, VS Code…) au lieu de JSON brut, via l'extension
MCP Apps (SEP-1865, stable). Implémenté avec **`prefab_ui`** (extra
`fastmcp[apps]`, déclaré dans `pyproject.toml` → installé par le `pip install -e .`
du deploy) : un tool `@mcp.tool(app=True)` renvoie un composant `prefab_ui`
(`Card`/`Column`/`Heading`/`Text`/`DataTable`) que le host peint ; dégradation
gracieuse en texte pour les clients sans support.

## Convention `*_app`

**Convention** : variantes **flagship `*_app`** (≠ remplacer les tools JSON), où
un visuel aide vraiment l'utilisateur. Les tools JSON équivalents restent la voie
par défaut/agent (« si le rendu échoue, utiliser le tool JSON équivalent »).
L'import de `prefab_ui` est **optionnel et guardé** dans le module (si l'extra
manque, les `*_app` ne s'enregistrent pas, les tools JSON restent). Premier jeu :
`tools/foncier.py` → `foncier_site_app` (fiche site : géocodage + parcelle +
bâti), `foncier_comparables_app` (ventes comparables DVF autour d'une adresse),
`foncier_prix_m2_app` (stats €/m² d'une commune). Mêmes clients open-data que les
tools JSON ; rendu **défensif** (colonnes dérivées des clés réelles) pour ne pas
dépendre d'un nom de champ. Gatés par le connecteur (namespace `foncier`).

## Apps spine, gotchas, et guides tout-DB

Depuis, deux apps **spine** (hors gate) : `data_app` (datastore — table + fiche v2
schema-aware, `tools/datastore.py`) et `oto_doc_app` (pages/docs + KB, lecture
seule, `tools/docs_app.py`). ⚠️ Gotcha récurrent : **pas d'annotation de retour
`-> Card`** sur un tool `app=True` (hints résolus contre les globals du module au
build du schéma, or l'import prefab_ui est local à `register()` → NameError fatal
au boot, vécu #69). **Doc consommable par les agents = guide plateforme `mcp-apps`**
(servi par `oto_guide`, inventaire + quand app vs JSON + replis) — à tenir à jour
quand une app s'ajoute. ⚠️ **Guides = tout-DB (2026-07-16)** : la table `guides` est
la source de vérité des TROIS scopes on-demand (platform/org/user) ; les fichiers
`oto_mcp/guides/*.md` ne sont que des **seeds de boot** (`seed_platform_guides`,
idempotent, n'écrase jamais une ligne DB). Écriture platform = platform_admin
(MCP `oto_guide op=write scope=platform` / REST `PUT /api/me/guides/platform/{slug}`
/ dashboard `/platform/instructions`). Une édition durable doit AUSSI retoucher le
fichier seed (sinon un environnement neuf naît avec l'ancien texte). **Surface = UNE
capacité `me.guide`** (`capabilities/guides.py`, ADR 0042 §Convergence des surfaces,
2026-07-28) : `oto_guide` op-aware côté MCP + `me.guides.*` côté REST, **mêmes
handlers, une seule autz de scope** (`_owner_for_write`) — l'ex-`tools/guide.py`
(qui redéclarait la sienne) est supprimé. `scope` omis à l'écriture = `user`. Le cap
64 KB et le refus d'un corps vide s'appliquent désormais **aux deux faces**.

## La seule app qui écrit — `data_review_app` (`FastMCPApp`)

Une procédure s'arrête souvent sur une étape HUMAINE (« une personne relit les lignes
en attente et les lance »). `data_review_app` (`tools/datastore_review_app.py`) la ramène
dans la conversation : la prochaine ligne au statut `pending`, deux boutons, puis la
suivante. C'est l'**utilisateur** qui tranche en cliquant ; les autres apps restent en
lecture seule.

**Les bornes, revérifiées au clic côté serveur** : une colonne (le statut) ; deux
valeurs fixées par l'appel du modèle, figées dans la carte, et refusées hors des
`options` déclarées ; une ligne par `_id`, écrite seulement si elle est TOUJOURS à
`pending` (sinon sautée, jamais écrasée) ; le droit d'écrire est celui du store. Le
`lifecycle` n'est pas lu pour choisir les boutons — son interprétation est en retrait
(#317). La carte n'envoie ni ne lance rien dans un autre outil. En fin de file seulement,
un bouton « Continue in chat » poste — au clic de l'utilisateur — un message factuel
(« Done reviewing: 2 launched, 1 skipped. ») pour que l'agent reprenne la procédure. Le
bilan voyage dans les arguments du bouton : fourni par le client, il est borné, affiché,
incrémenté sur une écriture réelle seulement — jamais une garde. ⚠️ Le support de
`ui/message` par claude.ai n'est pas vérifié : à éprouver en preprod.

**Mécanique** : `FastMCPApp` plutôt que `@mcp.tool(app=True)`, parce que les boutons
appellent un outil. `@app.ui()` = le point d'entrée servi au modèle ; `@app.tool()` =
le gestionnaire des boutons, **app-only** — absent de `tools/list` (zéro coût de
contexte), appelé sous un nom haché `<hash>_data_review_decide`, introuvable sous son
nom nu. Trois conséquences à connaître :
- ⚠️ **ce chemin contourne la visibilité de session** : fastmcp retrouve un outil d'app
  même masqué par un transform. Le gestionnaire ne peut compter que sur ses propres
  gardes (ici, celles du store).
- ⚠️ **et il contourne les axes d'appel** : `namespace_of` d'un nom haché n'est pas
  `data`, donc `CallContextMiddleware` n'y lit ni `_project` ni `_org`. Le contexte de
  l'appel d'entrée est donc FIGÉ dans les arguments du bouton au rendu, et reposé dans
  le gestionnaire par les gardes des axes eux-mêmes (`call_axes.PROJECT` / `ORG`). Les
  middlewares tournent bien (journal, rédaction), sous le nom haché.
- ⚠️ **l'absence de `tools/list` est marquée FIXME côté fastmcp** (le spec veut l'outil
  listé, filtré par le host) : un bump au-delà du pin `<3.5` peut le faire réapparaître
  dans le contexte du modèle.

`test_platform_tools_are_capabilities.py` compte désormais les `@app.ui()` comme des
tools écrits à la main : un point d'entrée `FastMCPApp` y passait sans être vu.

## Deux canaux, deux lecteurs — le modèle ne voit pas la carte

L'hôte peint la carte avec `structuredContent` et donne au **modèle** le seul `content`
texte. Un tool qui rend un composant nu laisse FastMCP poser au texte le marqueur
`[Rendered Prefab UI]` (`fastmcp/tools/base.py`, `_PREFAB_TEXT_FALLBACK`) : le modèle ne
lit rien et invente ce qu'il résume (signal #1083, 18/09/2026, `oto_doc_app`). Une app
qui montre un contenu que le modèle doit connaître rend
`ToolResult(content=[<texte>], structured_content=<carte>)` — patron `docs_app._rendu`.

Et le canal structuré d'une app n'est **jamais** retiré : c'est l'unique entrée du
renderer Prefab, qui reste sinon sur « Waiting for content… ». `UnSeulCanalMiddleware`
l'a retiré à toutes les apps du 10/09 au 18/09/2026 ; il reconnaît désormais une app à
son `_meta.ui.resourceUri` (`est_une_app`). Les deux sont prouvés bout en bout dans
`tests/test_docs_app.py`, à travers la chaîne de middlewares servie.

## Construire une app qui agit — ce que la première a appris

- **Le JSON servi ne prouve pas l'écran.** Les tests lisent `structuredContent` : ils
  n'ont vu ni la carte qui ne passait jamais à la suivante, ni le double cadre, ni la
  pastille illisible en sombre. Avant la PR, cliquer dans un vrai host : un serveur
  FastMCP nu qui monte le module sur un store en mémoire (ni Logto ni base), puis
  l'inspecteur MCPJam, onglet **Chat** — l'onglet Tools ne peint pas les apps.
- **`$result` reçoit TOUT le `structuredContent`**, et un `Slot` ne peint qu'un composant
  (clé `type` au premier niveau) : quand le gestionnaire rend un `PrefabApp`, échanger
  par `SetState(clé, RESULT.view)`, jamais `RESULT`.
- **Toute couleur passe par une variable du host** (`--color-background-primary`,
  `--color-text-warning`…), accent compris : un host peut servir des fonds sombres sans
  poser `.dark`, et une couleur réglée par `.dark` seule reste claire sur fond sombre.
- **Le cadre est au host** : page transparente et sans marge, la carte ne redessine ni
  bordure ni arrondi — sinon un second fond apparaît derrière ses coins. `@app.ui()` ne
  transmet pas `prefersBorder` (fastmcp 3.4.x) : le rendu sans cadre de claude.ai web se
  vérifie en préprod.
- **L'iframe ne rétrécit pas** quand une carte plus courte remplace la précédente.
- **Bruit à ignorer** : les rapports CSP `eval` du panneau Sandbox de MCPJam viennent des
  sondes `new Function("")` du validateur embarqué dans le renderer — attrapées, sans effet.
- **Un serveur local redémarré** laisse MCPJam sur l'ancien `Mcp-Session-Id` : 404, « No
  tools found », erreur de chat. Couper puis rallumer le serveur dans Connect.
- **Un bilan porté par les arguments d'un bouton est une donnée du client** : borné,
  affiché, incrémenté sur une écriture réelle seulement — jamais une garde.
- **La PR annonce** l'empreinte (`scripts/empreinte_servie.py`) et ce qui reste à éprouver
  sur claude.ai (appel d'un outil absent de `tools/list`, `ui/message`, thème clair,
  cadre) — en préprod, sur une COPIE de tableau : préprod et prod partagent la base.

## Une app peinte par un front — `oto_procedure_app`

Le graphe d'une procédure (son dessin, `procedure_diagram.py`) se lit dans un front
partenaire, qui en tient SEUL la grammaire : le cœur refuse d'en écrire un jumeau Python.
`oto_procedure_app` (`tools/procedure_app.py`) ne dessine donc rien : sa page ui:// est une
coquille qui charge le bundle du front, et l'outil ne livre que le tracé (sans ses fences)
et les connecteurs qu'il peut nommer (pour leurs logos). Ce n'est pas une carte Prefab :
une ressource MCP Apps nue, dont la CSP (`resourceDomains`) admet l'origine du bundle et
`img.logo.dev`, et qui demande le cadre du host (`prefersBorder`).

- ⚠️ **Par instance, jamais par tenant** : `OTO_PROCEDURE_APP_BUNDLE` (l'URL du
  `….js`, sa feuille `.css` à côté) est l'adresse ET l'interrupteur. Absente, ni outil ni
  page — l'empreinte servie d'une instance sans front ne bouge pas.
- **Il accepte `_org=` et `_group=`** comme `oto_procedure op=get` : écrit à la main, il ne
  les recevait par aucun des deux mécanismes (ni capacité, ni tool de travail), et un
  compte multi-org ne pouvait montrer que les procédures de son org maison. Il est nommé
  dans `call_axes._LECTURES_D_ORG_ECRITES_A_LA_MAIN`.
- **La poignée de main est écrite à la main côté front** (`ui/initialize`,
  `size-changed`, `request-display-mode`, `resource-teardown`), méthodes et recette de
  hauteur du SDK `@modelcontextprotocol/ext-apps` : sans `size-changed`, l'iframe reste à
  la hauteur par défaut du host.
- **Un chemin relatif du front se résout contre le bac à sable du host**, pas contre son
  origine : le bundle rend absolus ses chemins d'images contre l'origine de son script.
- **Le dessin se demande à la NAISSANCE, pas seulement à l'ouverture** : là où le bundle
  est déclaré, `oto_procedure op=create` (ou un `set` qui écrit la v1, hors
  `from_version`, ou un `fork` de la bibliothèque — le dessin se porte alors sur
  l'écriture d'adaptation) d'un corps SANS tracé répond avec `draw_next`, la consigne de dessin
  (`procedure_console._a_dessiner_a_la_naissance` → `procedure_app.a_dessiner_a_la_naissance`).
  ⚠️ **Sans garde client**, contrairement à la consigne de l'app : la plupart des
  procédures naissent dans un client TEXTE (Claude Code), qui écrit des caractères de
  tracé sans rien peindre — et que la consigne de l'app, réservée aux clients qui
  peignent, ne touchait jamais. Le dernier pas diffère seul : montrer le graphe par
  l'app, ou donner le lien de la page. ⚠️ Jamais à un travail du runner
  (`sert_un_worker`), jamais à une édition d'une procédure qui existe : le dessin reste
  facultatif dans le cœur, la consigne est celle d'une instance qui a un front qui le peint.

## Un tableau peint par un front — `data_app` (`OTO_DATA_APP_BUNDLE`)

Même montage que le graphe : `tools/datastore_table_app.py` enregistre `data_app` avec
une page ui:// qui charge le bundle déclaré par `OTO_DATA_APP_BUNDLE` (adresse ET
interrupteur, par instance). Déclaré, la carte Prefab de `tools/datastore.py` se retire
— un seul `data_app`, jamais deux enregistrements du même nom ; absent, rien ne change.

- **Un outil, un gabarit** : le bundle peint TOUTES les réponses de `data_app` — les
  lignes d'un tableau (`view: "table"`), la liste des tableaux (`"tables"`), une note
  (`"note"` : tableau inconnu). La carte Prefab ne peut pas servir une branche et le
  bundle une autre.
- **L'agent choisit** les colonnes (`fields`, dans son ordre) et la page (`limit`, 25
  par défaut, 200 au plus) ; tri, filtre, recherche et page suivante sont des
  arguments, même grammaire que `data_rows` (un curseur ne passe jamais d'un régime de
  tri à l'autre). La vue ne trie rien elle-même : une page triée n'est pas le tableau
  trié.
- **Deux canaux** : les lignes vont à la vue (`structuredContent`), le modèle lit une
  ligne. Un client qui ne peint pas les apps reçoit les lignes en texte — il a demandé
  à les voir, rien d'autre ne les montrera.
- **Lecture seule**, et les colonnes gardées des agents (`agent_access`) absentes comme
  partout sur la face agent ; la vue en dit le nombre (`hidden_columns`), jamais le nom.

## Une carte qui ouvre un lien — `oto_connector_app`

La face visuelle d'`oto_connector(op="setup_link")` (`tools/connector_app.py`, même
handler, mêmes refus) : le nom du connecteur, le geste de la personne, et un bouton
`OpenLink` vers la page du connecteur dans SON produit. Le bouton n'appelle aucun outil
et n'écrit rien : la clé se saisit sur la page, jamais dans la carte ni dans la
conversation. Un refus (nom inconnu, ambigu, `api`) devient une carte nommée ; un lien
absent ne dessine pas de bouton et dit pourquoi. Deux canaux, comme les autres apps : le
modèle reçoit le lien et les champs en texte. **Mise en page de la carte connecteur
du produit** (front `connector-chat-card`) : logo, nom, pastille d'état (Not set up /
Included / Connected / One step left, lue dans le même instantané que `op=list`), deux
lignes de description, puis un pied avec l'action à gauche et la page complète à droite —
sans nom de produit, un partenaire sert la même carte. Le logo ne se charge que depuis
`img.logo.dev` (CSP de la ressource, `PrefabAppConfig`) ; ailleurs, l'initiale. ⚠️ `OpenLink` passe par l'hôte
(`ui/open-link`) : qu'un hôte l'ouvre sans confirmation n'est pas vérifié — à éprouver
en préprod sur claude.ai.

## La carte d'un webhook — `oto_webhook_app`

La face visuelle de `hook_secret_url` (`tools/webhook_app.py`) : sur une instance qui
déclare le type de lien `webhook`, `create` d'un webhook et `rotate_secret` ne rendent
jamais le porteur sur la face MCP, et l'agent montre cette carte plutôt qu'un lien nu
qu'il pourrait résumer ou perdre. Sur une instance qui ne le déclare pas (choix
d'instance, cf. `docs/runner-et-automatisations.md`), l'agent a reçu le porteur ; la carte
le dit et n'a pas de bouton. Nom de l'agent, pastille
(Waiting for its token / Receiving events / Set up on its page — lue dans le journal des
livraisons, SANS les corps), le type de jeton de l'expéditeur, et un bouton `OpenLink` vers
l'écran guidé (type de lien `webhook`). Mêmes lectures et mêmes droits qu'`oto_trigger
op=get`/`deliveries` (lecteur et au-delà) ; refus en mots fixes (l'id vient du modèle) ;
aucun nom de produit ; deux canaux.
