---
title: Datastore (spine natif PG, ADR 0016)
type: reference
description: >-
  Référence du spine de stockage structuré per-user de oto-backend : tables PG
  user_datastores + datastore_rows (JSONB natif, uuid7, _created/_updated_at auto),
  chargé hors gate d'activation (provider=None, ADR 0011), partage par la chaîne
  d'octrois `resource_grants` (ADR 0030 ; l'ex-table `datastore_shares` est DROPpée),
  deep-link dashboard via data_url. Couvre les surfaces MCP data_*
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
Sheets — ADR 0016). Un datastore = une ligne `user_datastores` ; les rows vivent
dans `datastore_rows` (un dict **JSONB** par row, types préservés nativement,
fin de la sentinelle `__j:`). Schéma libre. Trois champs auto-managés exposés à
plat : `_id` (uuid7-like), `_created_at`, `_updated_at`.

**Datastore = spine plateforme** (`provider=None`, ADR 0011), PAS un connecteur
Google : chargé explicitement dans `register_all` (à côté de meta/orgs),
donc **hors gate d'activation** et **sans dépendance externe** — marche sans
connecter Google (plus de `412 google_not_connected`). Le partage est **DB-only** : il passe par la chaîne d'octrois `resource_grants`
(ADR 0030) — l'ex-table `datastore_shares` a été remplacée puis **DROPpée**, et rien
ne la lit plus. Le destinataire lit via son propre `sub`, sans permission Drive. `data_url` renvoie un **deep-link dashboard** (`/console/data`),
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
- MCP tools `data_*` (`data_create_datastore`, `data_write`, `data_rows`,
  `data_delete_row`, `data_url`, `data_share`, etc.) — pour Claude.ai / Claude Code.
- **Une vue filtrée, par lien** (23/10/2026, signal 1355) : `data_url` accepte `filter`
  (forme de `data_rows`), `q` et `order_by`/`order_dir`, et rend l'adresse que la page
  tableau du tableau de bord sait déjà lire à l'ouverture — `?f=` (triplets `[champ, op,
  valeur]` en JSON), `q`, `sort`/`dir` (oto-app, `DatastoreTable.vue::readTableQuery`,
  `lib/datastoreFilters.ts::filtersFromParam`) —, avec `count`, le nombre de lignes
  qu'elle montre (`datastore/vue_par_url.py`). Le filtre est jugé par le VRAI chemin
  (`count_rows`), une colonne trouvée nulle part est refusée, et ce que la page
  écarterait sans rien dire (une valeur vide, un `in` sans liste) est refusé plutôt que
  lié. ⚠️ **Seulement vers notre page** (`links.table_lit_sa_vue`) : le produit d'un
  tenant, ou une instance qui déclare son propre chemin de tableau, reçoit un refus nommé
  — jamais un lien qui montrerait tout le tableau en se donnant pour la vue. Sans ces
  paramètres, la réponse ne change pas. Hors périmètre : regrouper par colonne, replier
  des sections — la page ne les sait pas. Banc : `tests/datastore/test_data_url_vue_filtree.py`.
- MCP **App** `data_app` (`@mcp.tool(app=True)`, SEP-1865, prefab_ui) — variante à
  interface rendue : sans `datastore` = table des datastores ; avec `datastore` =
  table triable/cherchable/paginée des rows, avec `filter` exact-match optionnel
  (même forme que `data_rows`) et `show_meta` pour les colonnes `_id/_created/_updated`.
  Rend le contenu INLINE dans le chat au lieu du seul deep-link `data_url`. Dégradation
  gracieuse si l'extra `fastmcp[apps]` est absent (non enregistré). Pattern : cf.
  `tools/foncier.py` (`foncier_*_app`).
- REST `/api/datastore/*` — pour le CLI `oto data` + UI dashboard. **Face DÉRIVÉE
  depuis le 2026-08-12** (#302) : plus une seule route écrite à la main, tout vient
  des capacités `capabilities/datastore/{datastores,rows,schema,sharing,claim,
  activity,columns}.py`. Conséquences pratiques : les 24 opérations (au 2026-09-04) portent
  leur schéma d'entrée ET de réponse dans `/api/openapi.json` (un intégrateur les génère),
  et un **champ inconnu est refusé** (400 `unknown_fields`) au lieu d'être ignoré —
  sauf le corps d'un ajout/patch de ligne, qui EST la donnée (`body_field`) — **UNE**
  ligne par `POST …/rows`, jamais un lot : un corps à clé unique portant une liste
  d'objets, ou dont la clé `rows` en porte une quelles que soient les autres, est
  refusé `400 batch_body` (oto#48, oto#151) ; le lot = `POST …/rows/batch` (corps
  `{"rows": […], "key"?, "donnees_d_origine"?}`, capacité `me.datastore.write_rows`,
  le moteur `write_rows` de `data_write(rows=…)`), `data_write(rows=…)` ou l'upload
  signé NDJSON/CSV.
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

> **Les colonnes `date`/`datetime` : lire large, stocker une forme (#859, 01/10/2026).**
> Mesuré le 30/09 sur 225 tableaux : les agents écrivent une date sous toutes les formes,
> et le tri comme les filtres comparaient du texte. Un seul juge, `datastore/dates.lire`,
> sert l'écriture, la validation, les filtres et la reprise de l'existant.
> - **Lu** : ISO sous toutes ses variantes (`Z`, offset `+02:00`/`+0200`/`+02`,
>   fractions, sans secondes, séparateur espace), `JJ/MM/AAAA` et `JJ/MM/AAAA HH:MM`
>   (jour d'abord, toujours), un horodatage Unix — nombre, ou chaîne de 9 chiffres et
>   plus : secondes sous 10¹¹, millisecondes au-delà ; sous 10⁸ (mars 1973), refusé,
>   `2026` ou `45900` n'étant pas des instants —, et une date imprécise `2026-09-04`,
>   `2026-09`, `2026`.
> - **Stocké** (`normaliser_ligne`, sur le PAYLOAD, avant la fusion — sinon une même
>   date sous une autre forme ferait tomber `comment`/`link`) : un instant en
>   `AAAA-MM-JJTHH:MM:SSZ` UTC, fractions retirées ; un instant SANS fuseau est supposé
>   UTC et la réponse le dit dans `notices` ; une date imprécise reste à sa précision,
>   sans minuit fabriqué. Dans une colonne `date`, `AAAA-MM-JJ` — et un instant avec
>   fuseau y devient la date QU'IL PORTE dans ce fuseau, pas en UTC
>   (`2026-09-04T00:30:00+02:00` reste le 4). Couvert à tout niveau qu'une déclaration
>   atteint (`object.fields`, `list.of`). La couche `origine` posée par
>   `donnees_d_origine` reçoit la forme stockée.
> - **Illisible** : jamais réécrit. Au premier niveau, le type déclaré s'arme seul
>   (`types_declares`) : refusé, sur tout tableau. Un sous-champ sur un tableau sans
>   validation est gardé et signalé dans `notices`.
> - **Tri et filtres** lisent une case à son DÉBUT de période (`db.query.date_debut_sql`,
>   `2026-09` = le 1ᵉʳ à minuit UTC, garde `pg_input_is_valid`). La BORNE d'un filtre
>   couvre sa période entière (`dates.bornes_du_filtre`) : `gte` = depuis son début,
>   `lte` = jusqu'à sa fin (`lte 2026-09-04` retrouve `2026-09-04T10:00:00Z`), `gt` =
>   après sa fin, `lt` = avant son début, `eq` = la case commence dans la borne (`eq
>   2026-09` retrouve `2026-09-15` et `2026-09` ; `eq 2026-09-04` ne retrouve pas
>   `2026-09`), `ne` = le contraire, cases sans date comprises. Un instant est un point.
>   Typé au premier niveau seulement (`dates.typer_les_clauses`, depuis `_clauses` et le
>   périmètre de réservation) ; `contains` reste textuel ; une borne illisible est
>   refusée.
> - **L'existant** : `scripts/normaliser_dates.py` (à blanc par défaut, `--appliquer`),
>   lancé une fois depuis le commit.

> **Une colonne composite ENTIÈRE ne se compare pas (oto#22, 30/09/2026).** Sur une
> colonne déclarée `list` ou `object`, `eq`/`ne`/`in`/`gt`/`gte`/`lt`/`lte`
> comparaient le TEXTE du JSON — `eq` ne matchait jamais, `ne` matchait tout. Ils sont
> **refusés** en nommant la destination (`contacts[].<attribut>`, « il existe un
> élément dont… ») ; `empty`, `not_empty` et `contains` (recherche de texte) restent
> permis. La garde lit le type DÉCLARÉ (`columns._refuse_composite_compare`), sur
> toutes les lectures, conditions de métrique et file de travail comprises.

**Journal de travail : les deux surfaces, une seule table (2026-07-28).** Un geste fait
au cockpit (dashboard, REST) était journalisé au seul grain ROUTE (`RestCallLogger`,
`tool='PATCH /api/datastore/…'`) : on voyait qu'une écriture avait eu lieu, jamais
LAQUELLE ni depuis quel état — cliquer une transition de cycle de vie ne laissait donc
rien d'exploitable (ni retrouver la ligne, ni annuler). Les mutations REST posent
désormais AUSSI une ligne **sémantique** dans la même table `tool_calls`
(`kind='rest'`), nommée dans le **vocabulaire des tools MCP** (`data_write`,
`data_delete_row`, `data_release`) et portant `datastore`/`ns_id`/`id`/`fields`/
`from_status`/`to_status`. Helper unique `calllog.log_rest_call` (best-effort, hors
chemin chaud) ; colle datastore dans `datastore/journal.py`. Lectures : capacités
`me.datastore.row_activity` (`GET …/rows/{row_id}/activity`) et `me.datastore.activity`
(`GET …/activity`, `?limit=` borné 200) — elles ne filtrent plus `kind='mcp'`, et
résolvent `sub → email` **à la lecture** (un lot par page : `tool_calls.email` n'est
peuplé par aucun sink).

⚠️ **`from_status` vient de la MUTATION, pas d'une relecture.** Les mutations du store
(`update_row`/`delete_row`/`append_row`/`force_release`) acceptent un **relevé** `trace`
(dict mutable) qu'elles remplissent avec `ns_id`/`datastore`/`status_key`/`title_key`/
`prev_status` — pris là où ils sont déjà calculés. Le relire avant l'appel courrait avec
un write concurrent (un agent qui bouge la ligne entre les deux) et ferait proposer au
cockpit une annulation vers un état que la ligne n'a jamais eu ; ça ajoutait en prime
4 requêtes PG synchrones par mutation, sur un serveur mono-loop.

⚠️ **Le journal cite l'ENTITÉ, pas la chaîne tapée.** Le calllog journalise les args
BRUTS de l'appel — or `data_write` prend `datastore: str`, que l'agent remplit tantôt du
nom, tantôt de l'id, tantôt d'un `slot:<name>`. Corréler là-dessus obligeait à matcher
par NOM, avec trois dettes : un nom n'est unique que **par propriétaire**
(`uq_user_datastores_owner_ns`) donc il fallait le borner au tenant sous peine de fuite
cross-org, il change au **renommage** (historique orphelin), et un `slot:` n'est pas
rétro-résolvable. Depuis le 2026-07-28 les deux surfaces corrèlent sur le **`ns_id`
résolu serveur** : la face REST le tient de sa route, la face MCP du **relevé d'appel**
(`session_org.note_call_trace`, rempli par `DatastorePg._resolve` APRÈS les gardes —
un tableau refusé ne laisse pas de trace ; versé dans les args par `_calllog_sink`,
clés **fermées** `server._TRACED_ARGS`). Index d'expression partiel `idx_tool_calls_ns`.
⚠️ **`…/activity` lit ses deux axes en `UNION ALL`, jamais en OU** (29/10/2026, 503 `route_busy`) : le OU `ns_id`/nom privait le plan de `idx_tool_calls_ns` ; l'axe nom, borné au propriétaire, ne prend plus que les lignes SANS `ns_id` (création, refus de garde, relevé REST vide).

> Le relevé est un **HOLDER MUTABLE** (dict posé vide par `CallContextMiddleware`), pas
> une valeur rebindée : les handlers de tools sont majoritairement des `def` sync
> dispatchés en threadpool, où `copy_context()` copie les BINDINGS — un `.set()` fait
> dans le thread ne remonte JAMAIS au contexte appelant, la mutation du dict posé en
> amont si (même objet). Garde-fou : `test_the_trace_survives_the_threadpool`.

⚠️ **La REMISE aussi cite l'entité, depuis le 07/09/2026 (retrait des noms de
tableau, palier 1).** Le même raisonnement que ci-dessus valait pour ce que le serveur
REND, et il n'y était pas appliqué : `namespace` répétait la chaîne reçue — lire par
numéro répondait `namespace: "600"`. Deux appelants sur le même tableau y lisaient donc
deux valeurs, et la clé ne disait pas QUEL tableau avait été touché. Deux changements,
au même endroit :

- **`ns_id` s'ajoute** aux remises de la réservation (`data_claim_next`, + les deux
  capacités REST `claim_next`/`claim_row`), de l'écriture (ligne seule et lot, MCP et
  `POST`/`PATCH …/rows`), de la libération, de la page de lignes et de la lecture du
  schéma. **Déclaré au contrat** (`ClaimResult`, `WrittenRow`, `RowPage`, `ReleasedRow`,
  `SchemaOut`) et non seulement toléré par `extra="allow"` : une intégration qui génère
  son client depuis l'`openapi.json` doit le voir. Nom de clé pris à l'existant, jamais
  inventé — c'est déjà celui de `_resolve`, du relevé `trace`, de `_TRACED_ARGS`, de la
  cible d'un upload signé, et il est **déjà servi** par `oto_search` (`ref: {ns_id,
  row_id}`). `id` était pris : dans une remise de ligne il désigne la LIGNE.
- **`namespace` devient le nom CANONIQUE** du tableau, quelle que soit la forme de
  l'adresse reçue. **C'est le seul changement de comportement du palier** : un appelant
  qui adressait par numéro (ou par `slot:`) et relisait cette clé pour se vérifier reçoit
  désormais le nom du tableau. C'est ce que la clé prétendait dire.

⚠️ **Le CATALOGUE avait été oublié, et il est rattrapé le 23/09/2026 (oto#176).** Le
palier ci-dessus avait laissé les trois remises du registre — `data_list_datastores` /
`GET /api/datastores`, la création, le renommage — rendre le numéro sous `id` seul,
pendant que la description de `data_rows` prescrivait `ns_id` comme « la forme à
employer ». L'agent cherchait donc la clé prescrite dans la seule remise qui ne l'avait
pas : **deux confusions d'identifiant en deux jours**, dont une demande de SUPPRESSION
visant un tableau étranger à la mission, rattrapée par un humain et non par une garde.
Le contrat REST, lui, PROMETTAIT `ns_id` sur `DatastoreEntry` depuis le 07/09 — un
`Output` décrit, il ne sérialise pas : la clé était publiée à l'`openapi.json` et absente
du fil. Correctif : `registre.py` pose les deux noms par `identite.identite` (un seul
nombre, donc ils ne peuvent pas diverger), `CreatedDatastore`/`RenamedDatastore` les
déclarent, et le renommage REST rend `ns_id` — c'est la remise après laquelle l'adresse
par nom que l'appelant détenait est périmée. **`id` RESTE** : le tableau de bord bâtit
`/data/<id>` dessus (`DataView.vue`, `DatastoreTable.vue`), et on ne casse pas un
consommateur vivant pour réparer un vocabulaire. Retirer `id` demanderait un préavis
daté, qui n'est pas pris.

Le couple vient de `DatastorePg.dernier_tableau` — un relevé posé par `_resolve` dans la
ligne `user_datastores` qu'il vient de lire, donc **sans une requête de plus** — et se met
en forme par `datastore/identite.py` (source unique des deux clés, des deux faces). Sans
résolution, `ns_id` vaut `null` et `datastore` retombe sur l'adresse reçue : **la présence
du numéro est la preuve que le tableau a été atteint**.

⚠️ **Des chiffres qui désignent DEUX tableaux sont REFUSÉS (#365, 24/09/2026).** Une
adresse accepte un nom ou un numéro, et un nom peut être fait de chiffres : quand
`"77"` est à la fois le numéro d'un tableau visible et le NOM d'un autre, c'était le nom
qui gagnait, en silence. Or les ponts qui désignent un tableau par sa clé (fiche de
nœud, lignes d'un nœud, `slot:`) passent justement ce numéro — un tableau nommé « 77 »,
posé par n'importe quel membre, captait donc tout ce qui visait le tableau 77.
`db.resolve_datastore_ns` lève `AdresseAmbigue`, que le store rend en
`DatastoreAmbigu` : sous-classe de `DatastoreNotFound`, pour que chaque surface qui
refuse « introuvable » refuse aussi ce cas sans une ligne de plus, avec un `indice` qui
nomme les deux tableaux. Ni l'un ni l'autre n'est servi : préférer le numéro trahirait
qui a nommé son tableau « 2024 ».

⚠️ **`data_rows(id=…)` et `GET …/rows/{row_id}` n'en portent RIEN, exprès.** Leur corps
EST la ligne, et c'est l'objet même que la plateforme invite à relire puis republier tel
quel (promotion de `_id`, #354/#390) : une clé de réponse posée dedans reviendrait en
écriture — colonne fantôme sur un tableau libre, ligne entière perdue sur un tableau qui
refuse l'inconnu. Pour la même raison, l'écriture d'une ligne seule gagne `ns_id` mais
**pas** `namespace` : « namespace » est un nom de colonne trop plausible. Garde-fou :
`tests/datastore/test_datastore_numero_de_tableau.py`.

⚠️ **Là où une réponse VOYAGE, le nom ne suffit pas : elle porte l'identifiant
(11/09/2026, oto#160).** Les deux règles ci-dessus valent pour ce qu'on rend à qui vient
d'appeler. Deux réponses, elles, sont lues **plus tard et par quelqu'un d'autre** — et
sur celles-là un nom seul est un piège, parce que le lecteur le résoudra avec SON propre
demandeur, donc vers son homonyme personnel :

- la **charge utile d'un travail de runner** (`runner_jobs.payload`) ne portait que
  `namespace`. Elle porte `datastore_id`, résolu à l'enfilage au nom de qui a déclaré la
  campagne (`capabilities/runner_jobs.py::_id_du_tableau_vise`). ⚠️ Elle est PERSISTÉE :
  les travaux enfilés avant ce jour n'en auront jamais, et l'écran doit vivre sans ;
- un **lien de projet vers un tableau** (`db.list_project_links`) ne portait que
  `datastore` (le nom) — or son `target_ref` est tantôt un id (posé par le dashboard),
  tantôt un nom (posé par un agent, #117). Il porte `datastore_id`, résolu dans la portée
  du **propriétaire du projet** (`_portee_du_projet` + `db.resolve_datastore_ids_by_name`,
  même prédicat de visibilité que `resolve_datastore_ns`) : un lien appartient au projet,
  pas à qui l'ouvre, donc il doit désigner le même tableau pour tous. Hors de cette
  portée ⇒ **pas de clé**, et l'écran ne devine pas à sa place.

Les deux additions sont purement additives (le nom reste servi, c'est le libellé) et le
`datastore_id` d'un lien est **mangé** par les six projections explicites qui recopient
un sous-ensemble de clés (`op=inventory`, `oto_use_project`, `share_ui`, cascade
`resources`, `orgs/instructions`, `project_audit`) — aucune n'en a besoin aujourd'hui.
Banc : `tests/test_designation_par_identifiant.py` (la reproduction y est le faux
résolveur qui rejoue l'`ORDER BY` : le même nom, deux demandeurs, deux tableaux).

⚠️ **Un nom de tableau sera REFUSÉ à partir du 08/11/2026** (lot 0 du retrait, 29/09/2026 ;
la date vit dans `deprecations.RETRAIT_NOM_DE_TABLEAU`, alignée sur le retrait de
`namespace`). D'ici là le nom résout encore, partout, avec le même contrôle de visibilité
(`db.resolve_datastore_ns`), et **rien ne l'accepte en silence** : une réponse MCP obtenue
par un nom porte en tête l'avis qui donne le numéro et la date (`session_org.noter_avis`,
posé par `middleware.rappel_contexte`) — `data_*`, `oto_project op=link`, `oto_fleet
op=create` —, une route REST adressée par un nom répond avec `Deprecation` / `Sunset`, et
la portée d'un jeton range ses tableaux par numéro (`auth/token_scopes`, oto#158). Les
données qui gardent un nom (automatisations, liens de projet, portées de jetons,
procédures `datastore="…"`) se migrent par `scripts/tableaux_par_numero.py`, à blanc par
défaut.
Ce qui a changé est ce qu'on MONTRE — descriptions de `data_claim_next`/`data_write`/
`data_release`/`data_rows`/`data_get_schema`, instruction de flotte
(`capabilities/_instruction.py`), guide `datastore-semantics` §0 — parce qu'un agent
recopie l'exemple qu'on lui montre.

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
  Renouveler prolonge le bail **sans changer son run** (`_claimed_run`, oto#230) : seule
  une prise (ligne libre, ou bail échu) pose le run de l'appel — changer de run, c'est
  libérer puis réserver.
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

**Rallumé, pour le datastore seulement (27/10/2026, #830).** Le repli était mort depuis
le 28/08/2026 (lu sur le `MiddlewareContext`). Le middleware lit désormais la pile sur
`fastmcp_context` et pose le sommet dans une ContextVar À PART, le run de SESSION
(`session_org.current_session_run`), quand l'appel ne nomme aucun `_run_id`. Le datastore
le lit par `session_org.current_call_or_session_run()` — l'explicite d'abord : un agent
qui encadre son travail par `run_start` réserve, écrit sur sa ligne et la voit libérée par
`run_finish` sans repasser le jeton, tant que sa session tient ; l'indice « tu as oublié
`_run_id` » (#547) ne parle plus que si aucun run, ni nommé ni de session, n'est connu.
`current_call_run()` garde son sens — le run NOMMÉ seul — pour la résolution d'org du run
(#639) et les gardes de flotte (#830), qui ne le lisent pas par choix
(`docs/org-context.md` §L'org du run). `_run_id` reste obligatoire dans la description
servie : un client qui renouvelle sa session à chaque appel n'a pas de pile. Banc :
`tests/test_run_de_session_830.py`.

**Un agent ne réserve que dans un run (23/09/2026, #727).** Sans run, `data_claim_next`
rendait une vraie ligne ET posait un bail ; mais le bail se tient par son RUN
(`_lease_guard`), donc l'écriture était refusée APRÈS l'enquête, et le claim suivant,
sous run, rendait une autre ligne (2 occurrences sur 2). La ligne était retirée à tout le
monde pendant le bail et refusée à qui l'avait demandée. Le tool REFUSE désormais hors run,
**avant tout bail**, en nommant `run_start` et `_run_id` (`tools/datastore.py`,
`_REFUS_SANS_RUN`). Jamais de run implicite : ouvrir un déroulé à la place de l'appelant
lui attribuerait un fait qu'il n'a pas posé. La face REST (`me.datastore.claim_next`) n'est
pas concernée : une personne sur la file du dashboard réserve sans run. Banc :
`tests/datastore/test_claim_sans_run_refuse_727.py`.

⚠️ **Écrire un état terminal ne libère plus la ligne** — le store émet une notice à la
place. Un tableau dont le statut n'a aucun état terminal est une file qui ne libère
rien : `set_schema` le signale à la pose.

**Le bail servi dit POUR QUEL RUN : `_claimed_run` (01/09/2026).** Les lignes servies
portaient deux tiers du bail — à QUI (`_claimed_by`) et JUSQU'À QUAND
(`_claimed_until`) — jamais POUR QUEL RUN, alors que `datastore_rows.claimed_run` le
porte depuis #317. Une vue de surveillance voyait donc qu'un agent tenait une ligne et
jamais **laquelle** : elle pouvait relier un travail à son TABLEAU, pas à sa LIGNE. Le
serveur savait pourtant déjà répondre : la colonne portait le fait depuis #317, elle
n'était simplement servie à personne — et ce qu'un run sait de lui-même ne dit rien à un
tiers qui regarde la file.

`_claimed_run` est désormais rendu partout où `_claimed_by` l'est (liste, fiche par id,
les deux curseurs, `queue`, et le claim lui-même). **Trois états, pas deux :**

| forme | sens |
|---|---|
| `_claimed_run: "<run>"` | ce run tient la ligne — l'adresse du travail en cours |
| `_claimed_run: null` | le bail a été pris **sans run** (une personne sur la file du dashboard, par la face REST ; un bail antérieur au 23/09 — un agent ne réserve plus hors run, cf. ci-dessous) — un fait, pas un trou |
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

La garde se **déclare** sur le cycle de vie du champ `role="status"` — et, depuis
oto#101, un **plafond de plateforme** s'applique à défaut (voir plus bas) :

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
un entier ≥ 1. `data_claim_next` accepte un `max_claims` qui ne peut qu'ASSOUPLIR le
plafond en vigueur pour une passe, jamais le serrer (#132 : l'abandon vaut pour la ligne,
définitivement, quand le paramètre ne vaut que pour l'appel) ; l'état d'abandon, lui,
reste une affaire de schéma.

**Sans déclaration, le plafond de plateforme (oto#101).** Ni `max_claims` ni
`abandon_state` déclarés, la garde était inactive — et une ligne relâchée sans écriture
revenait en tête de la file indéfiniment (livelock reproduit trois fois sur trois). Le
réglage d'instance `OTO_MCP_CLAIM_DEFAULT_MAX_CLAIMS` (3, la valeur que déclarent les
tableaux à file) s'applique alors : la ligne est **mise de côté** par la seule colonne de
plateforme (`abandon_reason`, motif suffixé « platform default »), **sans toucher
à ses données** — aucun état ne lui est désigné, son statut reste ce qu'il était. Même
sortie de file, même journalisation, même réparation par une écriture. Un réglage
illisible (pas un entier ≥ 1) fait LEVER la réservation.

**L'ordre de service (oto#101).** `claim_next` servait `ORDER BY row_id` — la plus
ancienne ligne éligible, sans mémoire de ce qu'il venait de servir : relâchée, une ligne
qui correspondait encore au filtre revenait en tête. Il sert désormais la ligne servie
le **moins récemment** : `claimed_at ASC NULLS FIRST, row_id` (`rowlock._ORDRE_DE_SERVICE`).
`claimed_at` date la dernière PRISE (par `claim_next`, ou `claim_row` quand la ligne
change de mains — pas un renouvellement) ; ni le relâchement, ni l'expiration, ni
l'écriture ne l'effacent. Une ligne jamais servie passe donc devant toute ligne déjà
servie, relâchée ou expirée ; entre égales, l'ordre de création. L'index partiel
`idx_datastore_rows_file (ns_id, claimed_at NULLS FIRST, row_id) WHERE abandon_reason IS
NULL` porte exactement cette clé (révision 0030, CONCURRENTLY ; le démarrage la pose sur
une base neuve). ⚠️ Aucun `order_by` n'est accepté par la réservation : le jour où il
l'est, il passe DEVANT cette clé, qui reste le départage.

Au-delà du plafond, le serveur verse la ligne dans `abandon_state`, pose le motif dans
une colonne de plateforme (`abandon_reason`, rendue `_abandon` : « abandoned after 3
claims without a write, ceiling 3 » — le motif **cite ses chiffres**, le plafond
ayant pu changer depuis), libère le bail, et **journalise** (tableau, ligne, compteur).
Deux moments d'évaluation, et deux seulement :

- **au relâchement sans écriture** (`data_release`, et `run_finish` qui libère tout ce
  que le run tenait) — le cas nominal du faux départ ;
- **au claim**, en filet, avant de servir : c'est ce qui rattrape le bail expiré que
  personne n'a relâché (l'agent mort), sans quoi ce chemin contournerait le plafond.

⚠️ Une ligne sous bail **actif** n'est jamais abandonnée : son titulaire travaille
encore, et lui retirer la ligne serait la course que le bail existe pour empêcher.

**Le motif dit la CAUSE (oto-backend#491).** « 3 réservations sans écriture » réunissait
quatre causes à quatre gestes différents : un redémarrage qui a coupé les sessions, un
modèle qui dégénère, un outil en panne, une ligne impossible. Le motif finit donc par
`— last attempt: <cause>`, et `abandon_run` (servi `_abandon_run`) garde le run de
cette tentative. La cause, par ordre de préférence (`rowabandon._cause_de_l_abandon`) :

| cause | d'où elle vient |
| --- | --- |
| `job error: <texte>` | l'erreur que le worker déclare en concluant le job en échec (`runner.jobs` op=complete), passée au relâchement qu'il déclenche |
| ``run closed `failed`: <note>`` (ou `blocked`) | `runs.outcome`/`note`, posés par `run_finish` avant qu'il ne relâche |
| `write refused: <motif>` (sauf un refus du schéma, déjà nommé « write refused by the schema: … », repris tel quel) | la DERNIÈRE `data_write` du run sur cette ligne (ou sans ligne lisible au journal) a échoué — `tool_calls.ok`/`error` |
| `calls in error: <outil>[, …]` | les outils en échec du run (cinq au plus, le plus récent d'abord ; `oto_call` nommé par sa cible) |
| `lease expired without a release` | le filet du claim : la ligne portait encore un bail échu, personne ne l'a rendue |
| `no write attempted` | un run connu, rien de ce qui précède |
| `unknown cause: claimed outside a run` | aucun run : rien où lire |

Le journal des appels est lu **par le run et depuis la dernière prise** (`claimed_at`,
comparé en base), par `idx_tool_calls_run (run_id, created_at)`, `LIMIT` à chaque
lecture, sous un point de sauvegarde et un `statement_timeout` de 2 s : une lecture qui
échoue n'empêche ni l'abandon ni la réservation qui le porte — le motif dit alors
`unknown cause: log unreadable`, et le journal applicatif l'écrit en erreur. Le
journal des révisions (`datastore_row_revisions`) n'y sert pas : il n'écrit que ce qui a
été écrit, jamais un refus.

Une ligne abandonnée **quitte la file quel que soit le filtre du client** : le pick de
`claim_next` exclut `abandon_reason IS NOT NULL`, filet de plateforme indépendant de ce
que l'appelant filtre. Elle reste lisible, et réparable : toute écriture réussie remet
le compteur à zéro ET efface le motif, donc la rouvre. ⚠️ Rouvrir son **statut** suppose
que le cycle de vie déclare la transition de retour (`"echec": ["a_traiter"]`) — la
plateforme verse la ligne dans l'état d'abandon, elle ne s'autorise pas à l'en sortir.

**La contrepartie : l'AVANCE d'une passe à la suivante (`lifecycle.advance`, oto#95,
05/10/2026).** Une campagne en plusieurs passes traite chaque ligne passe après passe ;
l'agent lisait le marqueur de passe à la réservation et ne l'écrivait jamais (mesuré :
0 écriture sur 2 passages) — personne ne pouvait reprendre, ni savoir qu'une passe était
finie. Les passes sont des ÉTATS de la colonne de file, et leur suite se déclare :

```
lifecycle: {
  states: ["a_traiter", "societe", "dirigeant", "email", "fait", "echec"],
  transitions: {"a_traiter": ["societe"], "societe": ["dirigeant", "echec"], …},
  terminal: ["fait", "echec"], max_claims: 3, abandon_state: "echec",
  advance: {"societe": "dirigeant", "dirigeant": "email", "email": "fait"}
}
```

L'agent réserve par l'état de SA passe (`filter`, ou `claimable`) ; quand la ligne est
**relâchée** après une écriture, la plateforme la fait passer à l'état suivant. Règles :

- **la preuve d'écriture est `claims = 0`** au relâchement — le compteur que l'abandon
  lit, à l'envers : il monte à chaque prise, retombe à zéro à chaque écriture, dans la
  transaction de l'écriture, et sous bail actif seul le run titulaire peut écrire. Un
  relâchement finit donc en avance ou compte vers le plafond, jamais les deux, jamais
  aucun. Le journal des révisions n'a pas été retenu comme preuve : il ne garde pas une
  écriture SANS EFFET (une passe qui confirme la valeur en place remettrait le compteur
  à zéro sans révision — ni abandonnée, ni avancée, la ligne tournerait sur place), il
  se coupe (`OTO_JOURNAL_REVISIONS=off`) et se purge ;
- **l'écriture de l'agent fait foi** : si une révision depuis la prise (`claimed_at`)
  change la colonne d'état, on n'avance pas une seconde fois. C'est la seule question
  posée au journal — le compteur ne sait pas QUOI a été écrit. Journal coupé : pas
  d'avance, une erreur au journal applicatif (avancer à l'aveugle pourrait sauter une
  passe, et l'avance ne serait ni lisible ni réversible) ;
- **chaque relâchement** : `data_release`, `run_finish` et la conclusion d'un job
  (`datastore_release_by_run`), la libération forcée d'un superviseur — un seul chemin,
  `rowlock._relacher`, qui libère et avance dans la MÊME transaction, la ligne
  verrouillée (sinon une réservation s'intercale et reprend la ligne dans son état
  d'avant). **Pas l'expiration d'un bail** : un agent qui n'a pas relâché n'a pas rendu
  son verdict, la passe se refait ;
- **une révision comme une autre** : `source = system`, acteur `service:file-de-travail`,
  le geste de l'appel qui relâche, et le run QUI TENAIT la ligne (pas celui de l'appel —
  un `run_finish`, un superviseur). Lisible dans `data_row_history`, réversible par une
  écriture qui emprunte une transition déclarée, comme toute autre ;
- **la déclaration est jugée à la pose** (`definition._erreurs_d_avance`) : la forme
  `{"état": "état suivant"}` (une passe a UNE suite), des états déclarés, **une
  transition déclarée** (refus qui donne le patch, destinations existantes comprises ;
  un patch qui retire ensuite la transition est refusé de même), jamais depuis un état
  terminal (une ligne conclue — ou abandonnée — n'a plus de passe). Elle se patche état
  par état comme `transitions` (`advance: {"societe": null}` retire une suite).
  `advance` est une clé de FILE (`FILE_KEYS`) : elle désigne la colonne de file, et deux
  files restent refusées. Ce n'est pas une garde : rien n'est refusé à l'écriture ni au
  relâchement ;
- `data_release` (et `POST …/release`) rend `advanced: {field, from, to}` quand la ligne
  a avancé. La valeur à couches garde ses couches (`valeur` seule réécrite).

**Un filtre de réservation DÉCLARÉ sur le tableau : `lifecycle.claimable` (#517,
29/08/2026).** Sans lui, `data_claim_next` sert toute ligne du tableau dont le bail est
libre ou expiré. Mesuré sur un fichier de 8 910 lignes : un
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

⚠️ **La forme de `transitions` est un objet de LISTES (oto#63, 04/10/2026).**
`{"a": "b"}` passait la pose — la boucle de contrôle enrobait la chaîne d'une liste pour
la juger — et se stockait tel quel. Chaque lecteur le relisait à sa façon : le
validateur d'écriture le parcourait **lettre par lettre** (`a → fait` refusé, « autorisées :
['a', 'f', 'i', 't'] », et le patch proposé par le refus aurait lui-même été refusé), le
dashboard appelait `.map` dessus et la vue entière cessait de se rendre. Désormais :
`cycle_de_vie.fautes_de_transitions` est l'unique jugement — appelé à la pose et au patch
sur **chaque** colonne qui porte un `lifecycle` (un bloc secondaire est servi et lu par un
écran), le refus donnant la forme exacte (`{"a": ["b"]}`) ; `table_des_transitions` est
l'unique lecture, et **lève** sur un bloc stocké hors forme (même parti que
`max_claims_of`) : le changement d'état est refusé en nommant la faute et le patch qui
répare, jamais deviné. L'existant se convertit par `scripts/transitions_en_liste.py`
(tableaux, slots de procédure, entrées de bibliothèque ; à blanc par défaut,
`--appliquer` pour écrire, journalisé `migration_systeme`), lancé **avant** le tag qui
porte le refus : le code d'avant lit déjà les listes.

⚠️ **`terminal` aussi est une LISTE (oto#63, 05/10/2026).** La pose bouclait dessus sans
juger sa forme : `"terminal": "fait"` y devenait `f`, `a`, `i`, `t` sur la colonne de
file, et passait sans un mot sur une colonne secondaire ; la lecture (`terminal_states`)
ignorait la chaîne et **dérivait** d'autres terminaux à sa place ; le dashboard l'appelle
en `.map`. `cycle_de_vie.fautes_de_terminal` la juge par le même chemin que
`transitions` — à la pose et au patch sur chaque colonne qui porte un bloc, refus qui
donne `"terminal": ["fait"]` —, et `terminal_states` **lève** sur un bloc stocké hors
forme. Le même script la convertit (`"fait"` → `["fait"]`).

**Les libellés d'étape : `lifecycle.labels` (oto#140, 25/09/2026).** Un état est un
code (`a_qualifier`) ; un écran le montrait dérivé du code, sans accents (« A
qualifier »). `labels: {"a_qualifier": "À qualifier", "perdu": "Perdu"}` donne le nom
affiché de chaque étape. **Présentation, jamais validation** : aucune écriture ne le
lit, une ligne porte toujours le CODE, et le dashboard l'affiche à la place du libellé
dérivé (`etatLisible`). Déclaré front seul dans `schema_keys.CLES_DU_CYCLE`. Sa FORME
est jugée à la pose, sur **chaque** colonne qui porte un cycle de vie (pas seulement la
file) : un objet ; des clés qui sont des états de `states` — **une clé inconnue est
refusée et nommée**, car un libellé posé sur une faute de frappe serait servi et jamais
affiché ; des chaînes non vides d'au plus 60 caractères (`LIBELLE_ETAT_MAX`). Un état
sans libellé est permis. Il se patche comme `transitions`, **état par état** :
`data_patch_schema(fields=[{key: statut, lifecycle: {labels: {"perdu": "Perdu"}}}])`
nomme cette étape sans toucher aux autres libellés ni au reste du bloc, `labels:
{"perdu": null}` retire ce libellé, `labels: null` tous.

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
vraie réponse, et coûterait un parcours verrouillé de tout le datastore. ⚠️ **La 3ᵉ option du signal
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

La moitié NÉGATIVE du signal — « cette clé, je ne la connais pas » — est depuis le
01/10/2026 un REFUS (§ « Le vocabulaire d'un schéma est fermé ») ; `enforced` en est la
moitié positive, la seule qu'un client puisse vérifier contre le serveur qui lui répond.
⚠️ `cles_jumelles_warning` (deux clés libres à un caractère l'une de l'autre) est
RETIRÉ avec la fermeture : une clé libre ne peut plus être posée hors de `meta`, que la
plateforme ne lit pas.

**Une ligne créée sans la clé métier le DIT (#390, 3ᵉ demande).** Les deux premières
sont servies depuis le 13-15/08 : le bail protège l'ÉCRITURE et pas seulement
l'attribution (`_lease_guard` sous le verrou de ligne, `_assert_writable` sur les gestes
qui n'en ouvrent pas, titulaire reconnu par son RUN — et par rien d'autre), et l'adresse
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

**Deux réglages de tête, un par axe (oto#127, 02/10/2026).** Trois réglages
gouvernaient un tableau — `strict`, `unknown_fields`, `key_required` — et aucun ne se
comprenait seul : 18 tableaux écrivaient `strict: false`, 10 `key_required: false`.
C'étaient deux axes, dont l'un avait gagné trois crans par accrétion. Ils sont
remplacés par deux réglages (`datastore/reglages.py`, seule lecture des deux axes) :

| réglage | question | crans |
|---|---|---|
| `unknown_columns` | le sort d'une colonne que le schéma ne déclare pas — **retiré le 05/10/2026** (oto#124, en fin de document) : depuis le 21/10/2026 une colonne non déclarée est refusée partout, et le réglage n'est plus lu | (étaient `"create"` · `"report"` · `"reject"`) |
| `new_rows` | le droit d'une ligne nouvelle de naître | `"create"` (défaut) · `"reject"` (une écriture qui ne désigne aucune ligne existante est refusée ; exige `key`) |

Les valeurs sont des verbes, les mêmes sur les deux axes : ce que la plateforme FAIT de
la chose inconnue. `columns` plutôt que `fields` : l'axe juge le premier niveau, et
`unknown_fields` est aussi le code du 400 qui refuse un paramètre d'appel inconnu.

**Ce que `strict` faisait d'autre suit l'axe des colonnes** (relevé sur le code du
02/10, après J4 et le durcissement du 01/10 — aucun effet n'était devenu sans objet) :
hors `"create"`, le format FAIT CONTRAT (`reglages.format_contraignant`) — la
validation entière s'arme (`validation_active` : les `options` de premier niveau, la
structure des types non armés, les couches inconnues d'une valeur ne sont jugées que
là), les sous-records déclarés se ferment (#544), la colonne du périmètre de
réservation doit être déclarée, les colonnes orphelines sont signalées à la pose. La
seule règle retirée est « `unknown_fields: "reject"` exige `strict` » : c'est un seul
réglage.

**Traduction exacte** (`reglages.traduire`, appliquée une fois au parc par
`scripts/renommer_reglages_tete.py` — tableaux, slots de procédure, entrées de
bibliothèque — par les voies d'écriture normales) : `strict` absent ou faux →
`create` (un `unknown_fields` y était inerte) ; `strict: true` → `report` ;
`strict: true` + `unknown_fields: "reject"` → `reject` (`report` sans colonne
déclarée, où le cran était inerte) ; `key_required: true` avec `key` → `new_rows:
"reject"`, sinon `create`. Les anciens noms sont **refusés** à la pose et au patch
(MCP et REST, schéma comme paramètres de `data_patch_schema`), et le refus donne
l'équivalent exact calculé sur la combinaison reçue. Stockés, ils sont tolérés tant
qu'on n'y touche pas, **ne sont plus lus**, et la lecture les nomme avec leur
équivalent. `data_get_schema` sert les deux réglages tels qu'ils s'appliquent
(`reglages`), défauts compris ; `enforced` annonce `new_rows`.
Les paragraphes qui suivent racontent ces crans sous leurs noms d'alors.

> ⚠️ **`unknown_columns` est RETIRÉ le 05/10/2026 (oto#124)** — plus aucun réglage, les
> colonnes et les valeurs sont toujours vérifiées. Ce qu'il réglait est la règle de
> tous les tableaux depuis le 21/10/2026 : la colonne non déclarée est refusée
> (`colonnes_non_declarees`) et le format déclaré fait contrat (`validation_complete`),
> cf. la dernière section. `scripts/renommer_reglages_tete.py`
> est retiré avec lui (il posait ce réglage) ; `new_rows` reste le seul réglage de tête.

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

⚠️ **La distinction se lit aussi sans la phrase (oto#151 §3).** Les deux formes sortaient
sous le même code, sans `details`, et la première s'ouvrait sur « n'est pas renseigné » :
un client REST qui venait d'envoyer `"key": "<clé>"` dans son corps l'a lu comme un refus
de ce qu'il avait envoyé. Le refus s'ouvre maintenant sur « cette écriture porte `siren` =
… » ou « cette écriture ne porte pas `siren` », et porte `details` : `key`, `cle_portee`,
`valeur` (si portée), `a_renvoyer` (gabarit de la colonne-clé déclarée, oto#135) et
`cle_du_lot` quand un lot dédoublonnait sur une AUTRE colonne que la ligne portait. Un
champ `key` dans la ligne est nommé : c'est une colonne, pas le nom de la clé. REST rend
`details` ; `data_write` finit son message par la charge (`charge_a_renvoyer.clause`),
ligne seule comme lot — le lot garde `details` en changeant de désignation. Bancs :
`tests/datastore/test_datastore_key_required.py` (`test_151_*`),
`tests/api/test_rest_lot_oto151_live.py`.

**Le défaut ne bouge pas** : sans `key_required`, la création reste possible et reste
signalée par le `notices` de #390. Le cran est une déclaration du propriétaire du
tableau, jamais une politique de plateforme — un tableau se remplit souvent avant
d'avoir sa clé. Corollaire assumé : **un tableau fermé ne se peuple plus par écriture**,
`oto_upload_url` compris (il passe par le même `_write_rows_to_ns`) ; pour l'ouvrir,
`data_patch_schema(new_rows="create")` — et pour le fermer, `data_patch_schema(
new_rows="reject")` (ex-`key_required`, oto#127), sans réécrire le schéma (29/08/2026 : jusque-là seul `set` posait
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

**`key=` sur une écriture de ligne SEULE** (signaux 986, 1125, 1135, 1154) : refusé depuis
le 09/09 parce qu'il n'était passé à rien, SAUF quand il nomme la clé métier **déclarée**,
que `row` en porte la valeur et qu'aucun `id=` ne vise de ligne — l'écriture unitaire
rapproche déjà sur cette clé, le paramètre est alors réglé et non ignoré
(`jetons.key_unitaire_redondant`). Toute autre colonne se dédouble par le lot, même
pour une seule ligne : `rows=[{…}]` avec `key=` ; le refus le dit et nomme la clé
déclarée.

**La clé métier RÉÉCRITE par un patch sur `id` (#527).** Le geste voisin de #516, un cran
plus grave : là-bas on crée une ligne orpheline qu'on peut retrouver, ici on rend
orpheline une ligne qu'on ne retrouvera plus. Mesuré le 28/08/2026 : `data_write(id=…)` avec
un `siren` INEXISTANT (faute de frappe) dans le corps était accepté sans un mot — la ligne
existe toujours, le compte ne bouge pas, mais elle porte un numéro qui n'existe pas et rien
ne la rapproche plus du fichier client. Deux crans, comme #516 : un tableau **ouvert**
laisse faire (corriger un SIREN mal saisi à l'import est légitime) mais le DIT — `notices`
porte « clé métier `siren` modifiée sur la ligne « … » : ancienne → nouvelle » ; un tableau
**fermé** (`key_required`) le **refuse**, en nommant l'ancienne et la nouvelle valeur, et la
sortie passe par le schéma (`data_patch_schema(new_rows="create")` le temps du geste),
jamais par un paramètre « forcer » sur l'écriture. Ne sont PAS des réécritures : poser la
clé d'une ligne qui n'en avait pas, redire la même valeur, ou l'enrichir d'une provenance.
Garde : `cle_metier.cle_reecrite`, branchée dans `update_row` (donc aussi `append_row` avec
`_id` et REST `PATCH …/rows/{id}`) ; le lot, qui vise par la clé, n'est pas concerné.

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

**Vider un champ `required_when` et changer l'aiguillage tiennent dans UN SEUL appel
(#649, 17/09/2026).** `required_when` se juge sur la ligne MERGÉE, aiguillage compris
(#347) — pas sur une suite d'états intermédiaires. Vider le champ gaté SEUL, pendant que
l'aiguillage porte encore la valeur qui arme la garde, est refusé : la ligne finale viole
toujours la contrainte. Le geste qui passe est un `data_write` unique portant les deux
écritures — c'est ce que le refus dit désormais lui-même (`_clause_un_seul_appel`) et ce
que la description de `required_when` sur `data_set_schema` prévient dès la déclaration.

⚠️ **Ce n'est pas une indifférence à l'ordre, et aucun texte ne doit la promettre.** En
DEUX appels, changer l'aiguillage d'abord passe (la garde s'éteint), et le vidage qui
suit passe aussi : rien ne refuse un champ gaté resté rempli quand la garde tombe. La
contrainte porte sur le VIDE tant que la garde tient, jamais sur le PLEIN quand elle
s'éteint. Corollaire côté refus : la clause se TAIT quand l'appel écrit déjà une clé de
la condition — l'appelant vient de choisir la valeur qui arme la garde, lui dire « les
deux dans le même appel » serait lui décrire un geste qu'il a déjà fait.

**Une valeur se compare à une déclaration d'UNE façon** (oto-backend#412) : les options
(tout type, `enum` compris) et les conditions de `required_when` passent par
`options_declarees.parmi`, qui met les deux côtés sous la forme où la base les compare
(`valeur_comparee`, ce que rend `data->>champ`, donc ce que lisent le filtre, le tri et
le relevé de la pose). `5 ≡ "5"`, `true ≡ "true"` ; `5.0 ≢ "5"`. Avant, le contrôle
d'`enum` exigeait une chaîne (`5` refusé sur les options `"1"`…`"5"`) quand
`required_when` comparait en `str()` (`5` satisfaisait `"5"`, mais `False` ne
satisfaisait pas `"false"`). La valeur stockée reste celle envoyée : son texte, seul lu
par le filtre et le tri, est le même.

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

**Un motif SEUL s'applique (oto#103, arbitré le 24/09/2026).** `max_length` n'est plus
obligatoire : le sujet d'un motif est borné par `max_length` s'il est déclaré, sinon par
`PATTERN_MAX_SUBJECT` (1 000) — une règle, `declaration.borne_du_motif`, pour la pose, la
lecture et l'écriture. Le budget se calcule contre cette borne ; à l'écriture, une valeur
plus longue est refusée **sans exécuter le motif**. `pattern` arme la validation comme
`max_length` (`_exige`). Avant, un motif sans borne était refusé à la pose et, hérité d'un
schéma plus ancien, restait inerte : accepté, servi, sans effet. ⚠️ Un tableau souple dont
la seule exigence est un motif sans borne bascule donc en validation (types, `options` de
premier niveau comprises) : la requête qui les compte se lance avant la mise en prod.

**Le type `phone` (oto#103, arbitré le 24/09/2026).** La famille des types métier reste
limitée à ceux dont le rendu et la validation valent partout : `url`, `email`, `phone`. Le
reste (SIREN, IBAN, code postal…) se contraint par un `pattern` posé par le consommateur.
`datastore/telephone.py` juge le numéro sur sa forme compacte (`normaliser`) : séparateurs
retirés (espaces, points, tirets, barres, parenthèses, `(0)` après l'indicatif), `00` en
tête lu comme `+` ; passe l'international E.164 (`+`, indicatif qui ne commence pas par 0,
7 à 15 chiffres) ou un national de 6 à 15 chiffres. Armé par sa déclaration comme `email`
(`types_declares.TYPES_ARMES`), avec la destination du refus (`.comment` pour le poste ou la
source ; `@empty` et la raison dans `.comment` si rien n'a été trouvé, et omettre la
colonne seulement pour ne pas toucher à la case, oto#140). ⚠️ La valeur n'est **pas réécrite** en
base : la réécrire changerait son identité face à la fusion, et un même numéro remis dans
une autre mise en forme emporterait ses couches.

**Les champs que l'appelant n'écrit pas (#586, #606 ; 29/08/2026).** **Deux**
crans de colonne sous UNE garde (`dsv2.reserved_refusals`, le geste dans
`datastore/reserves.py`), pour deux gestes mesurés sur la même campagne contre la
donnée remise par le client — l'écraser, et détruire sa copie de secours.

⚠️ **Un troisième cran a existé du 01/09 au 07/09/2026 et a été RETIRÉ** (#607) : la
plateforme y reposait la VALEUR d'une colonne à chaque écriture, depuis une source
qu'elle observe. Il a été livré, annoncé dans la description que les agents relisent à
chaque appel — et déclaré par **zéro colonne sur les 5 615 de production**. Retiré pour
cette raison, avec la règle qui va avec : *rien n'exige aujourd'hui qu'un premier usage
réel existe avant qu'une capacité ne soit annoncée à des agents.* Le besoin qui l'avait
motivé — une colonne que l'agent remplissait de mémoire et qui dérivait d'une fiche à
l'autre — reste servi par le **pointeur** : le run sur la ligne, et ce que le run sait se
lit au run. *Une valeur qu'on rejoint ne dérive pas, une valeur qu'on recopie dérive.* Même hiérarchie que #516 : le chemin n'existe pas >
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
  CHANGEMENT de valeur** : nommer la valeur (nue, `null`, ou `{"valeur": …}`) d'une case
  qui a une valeur POSÉE avec une valeur différente est refusé ;
  ⚠️ **« ne se modifie plus une fois posé », pas « ne s'écrit plus après la création »**
  (oto#140 J5, 04/10/2026) : une case sans valeur (clé absente, `null`, `""` ordinaire) se remplit — la
  lecture d'avant rendait toute colonne déclarée après son import définitivement vide.
  Le vide est celui d'`est_vide` (`[]`, `{}` aussi) ; `@empty` (stocké `{"valeur": "",
  "oto.vide_assume": true}`) EST une valeur posée ; une colonne calculée reste fermée
  même vide. Juge unique : `couches.valeur_posee`, le même que le filtre des `null` en
  écho ; **une valeur identique n'est pas une
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
- **⚠️ Écrire l'origine se DÉCLARE (oto#70 lot 2, refus en vigueur depuis le 01/10/2026).** Ce
  qui est refusé n'est pas l'écriture, c'est le **silence** : `origine_override=true`
  sur l'appel (les deux faces) et elle passe ; sans lui, elle est refusée par un message
  qui nomme les deux issues — écrire la valeur seule, ou déclarer. **Rien à demander à
  personne**, aucun droit à provisionner : le paramètre engage celui qui l'envoie, et
  c'est tout (décision d'Alexis, 05/09/2026 : « c'est notre modèle d'agent experience »).
  Le préavis daté (`origine_warning`, réglage de date) est retiré le 06/10/2026 : le
  refus est la règle, sans date. Pour un import, `donnees_d_origine=true` pose l'origine
  sans que l'appelant l'écrive, et le refus le nomme. Le relevé qui comptait les
  écrivains de la couche pendant le préavis (`origine_ecritures`) est retiré le
  08/10/2026 (révision `0048_origine_ecritures_retiree`, oto-backend#1109) : zéro refus
  en production sur les quatorze jours précédents.
  **La manœuvre « lever le format, écrire, remettre » ne rouvre rien** : la garde
  regarde ce que l'appelant écrit, pas ce que la colonne déclare (mesuré). En revanche
  `data_drop_column` et `data_delete_row` **emportent** l'origine — rien n'y écrit une
  couche, le paramètre n'a donc rien à y déclarer, et déclarer une destruction ne la
  rendrait pas réversible ; ces deux portes attendent le verrou humain.

- ⚠️ **`origine: "system"` est SUPPRIMÉ (décision produit, 08/09/2026)** — remplacé
  par `donnees_d_origine`, qui fige la version d'origine au moment où la valeur entre
  au lieu d'armer un filet qui dépendait de l'ordre des gestes. **Plus rien ne capture
  automatiquement** : une valeur qu'un agent écrase n'est plus retenue. Mesuré au
  moment du retrait : 500 colonnes déclaraient le cran sur 10 tableaux, et 28 799
  cases portaient une couche `origine` — **ces données ne bougent pas**, elles restent
  lues et servies ; c'est le mécanisme qui part. Une déclaration `origine` est REFUSÉE
  depuis le 01/10/2026, et celles qui subsistaient sont retirées par
  `scripts/durcir_schemas.py`. `system_origin_fields()` est GARDÉE
  et rendue vide plutôt que supprimée : ses six appelants s'éteignent d'eux-mêmes, au
  lieu de disparaître un par un au risque d'en oublier un.
  ⚠️ **Ce que le cran faisait encore la veille du retrait** : sur un tableau de
  campagne, il avait retenu 39 valeurs d'agent qu'un geste nocturne emportait. C'est la
  contrepartie assumée — l'origine cesse de dépendre de qui pense à déclarer un cran
  avant l'import, et elle cesse d'être rattrapée quand personne ne l'a déclarée.

  *Ce qui suit décrit le mécanisme retiré, gardé pour lire l'existant :*
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
  ⚠️ **La capture de la VALEUR est PARESSEUSE, pas à la pose du schéma** : un format ne
  vaut que pour l'avenir (doctrine de `_overlong_warning` et consorts) — et elle rend la
  MÊME valeur, puisque rien n'a bougé entre la pose et la première modification.
  ⚠️ **Mais la pose n'est PAS neutre sur les lignes DÉJÀ LÀ.** À la déclaration, un
  balayage écrit `<champ>.origine = "(origine inconnue)"` sur chaque ligne existante
  (`datastore_capturer_origine`, oto#46). C'est un **aveu, pas une capture** : au moment
  où le cran arrive, la valeur d'import a déjà pu être écrasée par un agent, et poser la
  valeur courante la présenterait comme celle de la cliente — ce que la définition
  interdit. Le marqueur rend l'ignorance explicite plutôt que muette : sans lui, « pas
  d'origine » se confondrait avec « origine vide ».
  ⚠️ **Balayage et marqueur RETIRÉS (oto#116, 30/09/2026).** Un texte logé là où vit
  une valeur est indistinguable d'une valeur : un écran de restitution l'a comparé à la
  valeur courante et en a conclu à une correction. Le code qui l'écrivait
  (`datastore_capturer_origine`, `_capturer_origine_des_colonnes_neuves`, la clé
  `origines_capturees` et `marqueurs_poses_warning`) est supprimé, et les cases déjà
  marquées se reprennent par `scripts/purger_origine_inconnue.py` (à blanc par défaut,
  `--apply`, lots bornés par la clé primaire, écriture estampillée
  `service:reprise-origine-inconnue` au journal des révisions) : la couche `origine`
  part, la valeur reste, et une cellule réduite à sa seule valeur redevient plate.
  **Une case sans couche `origine` n'a pas de valeur de départ connue** — l'absence de
  la couche le dit, et « origine vide » reste `""`, qui n'a jamais été servi.
  ⚠️ **`null` efface, et le reste (oto#140, décidé le 23/09/2026).** La « fin de `null` »
  (préavis puis refus au 1er décembre 2026) est ANNULÉE : `null` efface une case quel que
  soit son type, et `@empty` (sa raison dans `comment`) dit « cherché, rien » — les deux
  seuls gestes. **`@keep` et `@clear`, qui doublaient l'omission et `null`, sont REFUSÉS
  (J3)** : une écriture qui les porte, où que ce soit, est refusée ENTIÈRE, lot compris, et
  le refus nomme le geste à la place — `@clear` → `null`, `@keep` → omettre le sous-champ
  (ou renvoyer sa valeur quand elle doit survivre à une valeur qui change)
  (`mots_deprecies.controler`, sur les quatre chemins d'écriture : création, patch par
  `id`, lot, import par URL signée). Sans ce refus, `"@keep"` serait stocké comme une
  valeur littérale.
  ⚠️ **J2 : `""` et `[]` sont des VALEURS** — ils remplacent la valeur en place, et la
  valeur remplacée revient dans `valeurs_effacees`. L'écart (#608) et le refus « écriture
  sans effet » (#724) ne valent plus que pour `{}` (jamais une valeur, oto#165). Posé sur
  les deux arbitrages (`ecriture`, qui porte aussi les lots, et `ecriture_par_id`).
  Au même jalon, `null` efface une case au vide ASSUMÉ, marqueur compris (section oto#204).
  Les préavis datés de J2 (06/10/2026) et J3 (08/10/2026) et leurs réglages de date ont
  été retirés le 06/10/2026 : la règle s'applique sans date.
  ⚠️ **`force: [chemins]` (oto#140)** remplace la portée « tout l'appel » du booléen :
  `force=["raison_sociale.origine"]` ne force que ce qui est nommé, colonne ou couche.
  Le nommer vaut demande — pas besoin de `readonly_override` en plus. ⚠️ **Ça change la
  PORTÉE, pas la retenue**, et le contrat le dit lui-même : mesuré, 56 appels sur 57
  portant le forçage étaient à « oui ». Ce qui TIENT un forçage est son palier
  (propriétaire ∪ gouvernant), inchangé. Le refus sur une colonne verrouillée absente
  de la liste dit que c'est la LISTE qui ne la nomme pas — sinon celui qui a le palier
  part chercher un droit qu'il possède déjà. `None` (rien demandé) et `[]` ne sont pas
  la même chose : le second est refusé, parce que le lire comme une absence désarmerait
  silencieusement le geste.
  ⚠️ **Le bloc `lifecycle` DÉSIGNE la colonne d'état (08/09/2026)** — plus d'étiquette
  `role: "status"`, plus de clé de schéma. Trois mécanismes ont désigné cette colonne
  en une journée ; le troisième est le seul qui n'ait rien à synchroniser. **Le défaut
  disparaît par construction** : un `lifecycle` qui ne s'applique pas ne peut plus
  exister — cinq tableaux, dont quatre en production, en portaient un jamais lu. Deux
  blocs sont refusés à la pose (sinon l'ordre de déclaration trancherait en silence).
  ⚠️ Et le silence de l'ancien incident #360 est repris sur le fait qui est maintenant
  vrai : sans aucun bloc, le tableau n'a PAS de file, `claim_next` y rend `{}` sans
  raison — l'avertissement le nomme, sans quoi j'aurais remplacé un avertissement par
  rien. `role` reste servi et non interprété : ses 1 000+ autres colonnes (`note` 441,
  `metric` 211, `qualif` 202…) décrivent de l'affichage et appartiennent au
  consommateur.
  ⚠️ **~~`status_field` au niveau du schéma~~ — RETIRÉ le jour même** — la colonne d'état se déclare
  comme la clé métier, au lieu d'être désignée par l'étiquette `role: "status"`. Motif
  mesuré le 08/09/2026 : `status_field()` rendait **le PREMIER champ trouvé**, donc
  l'ordre de déclaration tranchait en silence ; et un `lifecycle` posé sur une colonne
  sans l'étiquette n'est jamais lu, alors que le schéma affiche le contraire — une
  campagne avait armé une garde qui ne gardait rien. Une clé de schéma ne désigne
  qu'une colonne, et une colonne absente se refuse à la pose, en disant ce qu'on perd
  (état terminal, plafond de reprises, périmètre de réservation). Même leçon que `key`,
  et que `display: "title"` qui a déjà remplacé `role: "title"`. **Palier 1 : la clé
  s'ajoute et gagne quand elle est là ; `role: "status"` continue de valoir.** ⚠️ Le
  reste de `role` est de l'affichage — mesuré sur quatre tableaux : un seul rôle sur
  les deux tableaux de PRODUCTION, dix-huit des dix-neuf autres décrivant comment un
  écran rend une colonne. Son retrait complet viendra avec son préavis.
  ⚠️ **`versions` (oto#140) — palier 1 de la lecture à deux versions.** `current` et
  `origine` se nomment (`versions=["current","origine"]`), et la réponse déclare ce
  qu'elle a servi sous `versions_servies` : sans cette déclaration, « pas demandée » et
  « absente de cette case » se lisent pareil. ⚠️ **Le nom NU garde toujours la version
  courante** — faire porter deux sens à `champ` selon un paramètre serait le piège
  qu'on retire ailleurs du produit. **Par défaut, la valeur actuelle seule (décision du
  02/10/2026)** : l'origine (la première version de la donnée) se demande avec
  `versions`. Les réponses d'ÉCRITURE de ligne seule se lisent comme une lecture :
  `PATCH …/rows/{id}`, `POST …/rows` et `data_write` (mode ligne) acceptent `versions`,
  `layers` et `empties` (mêmes défauts) et rendent `versions_servies` ; un lot ne rend
  aucune ligne. ⚠️ Cette bascule n'est pas une économie
  de données : elle supprime la surface d'un incident — un écran s'apprêtait à annoncer
  à une cliente « nous avons corrigé votre valeur » en lui montrant du texte de la
  plateforme, et ce genre d'accident demande que la donnée soit là **sans qu'on l'ait
  demandée**. Les deux versions arrivent dans le MÊME appel, jamais deux : deux appels
  ne sont pas atomiques.
  ⚠️ **`donnees_d_origine: true` (oto#140) supprime cette dépendance à l'ordre**, et
  c'est le remède de fond : au lieu d'un filet armé à l'avance, un GESTE explicite. Un
  appel qui le porte déclare qu'il apporte la donnée telle que la cliente l'a remise,
  et chaque case fige sa version d'origine **au moment où la valeur entre** — même
  geste, donc plus d'avant ni d'après. La provenance voyage dans `<champ>.comment`
  (pas de paramètre séparé : ce serait une deuxième façon d'écrire un commentaire).
  Trois règles : une origine posée n'est jamais réécrite (un ré-import est rejouable),
  une case vide ne reçoit rien (`0` et `false` sont des valeurs remises), et le défaut
  ne bouge pas. ⚠️ **Une origine VIDE (`""`, `[]`, absente) n'est pas posée** (oto#164) :
  le marqueur `""` de l'ancienne capture dit « rien n'avait été remis », et le compter
  comme posé le gelait au moment où la cliente remettait enfin une valeur — un
  ré-import la pose donc. La réponse le DIT, dans `notices`, cumulé sur le geste :
  origines posées par colonne (nombre de lignes), et sautées par raison — `déjà
  posée`, `valeur vide`, `écrite par l'appelant`. Les origines posées par le paramètre
  n'entrent pas dans le refus des origines écrites sans le dire (oto#70) : sur une
  ligne existante, un ré-import aurait été averti, puis refusé à la date, pour avoir
  fait ce qu'il déclarait. Branché sur les QUATRE chemins d'écriture — ⚠️ j'en avais câblé trois,
  et c'est le banc qui a trouvé le quatrième, celui qui ressemblait le plus aux autres.
  Déclaré au MINT sur l'upload signé, comme `origine_override` : le `PUT` ne porte
  aucun paramètre.
  ⚠️ **Sans lui, l'ORDRE DES GESTES décide de tout : déclarer le cran AVANT l'import,
  jamais après.** Déclaré avant, la ligne n'existe pas encore, rien n'est balisé, et la capture
  paresseuse fige la valeur de la cliente au premier enrichissement. Déclaré après, rien
  ne se reconstitue — **mesuré le 08/09/2026 : 837 cellules sur 846 marquées** sur un
  tableau de production, découvert trois semaines après la pose, à la veille d'une
  restitution cliente. Ce n'est pas un défaut, c'est une inversion, et elle est évitable
  à coût nul sur tout tableau neuf. La réponse à la pose rendait le compte sous
  `origines_capturees` — ⚠️ **nom trompeur : il comptait des PERTES** (clé et
  avertissement retirés avec le balayage, oto#116). Refusé à la pose sur un composite ou un `json` (la capture rangerait
  l'objet entier dans la couche — ⚠️ **motif reformulé le 2026-09-01, #728** : il
  invoquait l'exemption `json` de la grammaire des couches, qui ne vaut plus pour
  l'adresse ; c'est la pose AUTOMATIQUE qui ne s'y déclare pas, l'annotation elle-même
  s'y écrit à la main), sous un sous-record, sur une cible de couche ; se combine avec
  `readonly` (valeur verrouillée ET couche d'origine fermée — la pose n'a jamais lieu
  tant que la valeur ne bouge pas, et joue le jour où le propriétaire lève `readonly`).
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
- **Le refus nomme le geste À QUI PEUT S'EN SERVIR** : sans le paramètre, il dit comment
  forcer et à qui c'est ouvert — c'est ce qui détourne du schéma. ⚠️ **Amendement du
  13/09/2026 (oto#234) : à qui ne tient PAS le palier, il ne nomme plus rien du
  forçage.** Un agent hébergé avait lu `readonly_override` dans son refus et l'avait
  rejoué au coup suivant. Le nommer à qui ne peut pas s'en servir n'ouvre aucune
  écriture — ça lui apprend qu'un levier existe. Ce refus-là dit donc que son accès ne
  permet pas de passer outre (sans nommer ni le paramètre ni le palier), où va la
  divergence (`<colonne>.comment`), et à qui demander la correction : qui possède le
  tableau. Les **descriptions servies** (MCP, REST), elles, ne bougent pas : elles
  s'adressent à tout le monde, et une capacité qu'aucun texte n'annonce n'existe pas.
- **La trace est au journal des appels**, clé `readonly_forced` (ligne, colonne, valeur
  remplacée), à côté du `sub` que le journal stampe déjà — face MCP par
  `session_org.note_call_trace` + l'allowlist `server._TRACED_ARGS`, face REST par
  `calllog.log_rest_call(forced=…)` (hors `args`, qui stringifierait la liste). ⚠️ **Le
  journal ne remonte qu'à ~35 jours** : la trace disparaîtra alors que la valeur forcée
  restera. Arbitré en connaissance de cause le 02/09/2026 — pas de colonne de plus sur la
  ligne.
  **Un relevé borné le dit (oto#139).** Au plus `MAX_RELEVE` = 25 substitutions par
  appel, chaque valeur coupée à `MAX_VALEUR` = 120 caractères. Une valeur coupée finit
  par `…` et l'entrée porte `was_len` / `now_len` (longueur d'origine) ; un relevé écrêté
  ajoute la clé `readonly_forced_bilan` (mêmes deux faces que `readonly_forced`) —
  `{relevees: 25, total: N, plancher: true, geste_id?, historique}`. Sans elle, le relevé
  est complet ; avec elle, le compte des entrées est un **plancher**. L'histoire entière,
  valeurs entières, est au journal des révisions de ligne (oto#273) : `data_row_history` /
  `GET …/rows/{id}/history`, où les révisions de l'appel portent son `geste_id`.

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
> valeur, en clair). Poser la valeur seule au tour suivant emporterait la couche. Pour
> compléter une valeur déjà en place sans la réécrire, nomme les couches SANS \`valeur\`
> (\`"qualification": {"comment": "…"}\`) : un geste qui ne pose aucune valeur, ou une
> valeur vide, n'est jamais soumis à cette exigence.`

**Ce que la garde ne fait pas.** Elle n'empêche pas un commentaire **faux**. Elle oblige à
**nommer une source**, ce qui rend le mensonge vérifiable ; la vérité reste à la relecture
sur pièces.

**Ce qui la déclenche, et ce qui ne la déclenche pas :**

- une valeur nulle, vide, ou une **couche posée seule sans valeur** ne déclenche rien —
  c'est la forme légitime d'une remarque ;
- **la garde juge le GESTE, pas la case en place** (complément du 11/09/2026, corrigé le
  24/09/2026) : un geste qui ne nomme pas `valeur` (`{"origine": …}`, `{"comment": …}`)
  n'arme pas l'exigence sur la valeur déjà là, même si elle n'a pas la couche. Avant,
  nommer la colonne pour n'y poser que l'origine était refusé par un texte qui affirmait
  le contraire. Les chemins qui fusionnent (lot par clé métier, patch par `id`) passent
  ce que le geste a nommé (`pose`) jusqu'à la garde ; la création et le remplacement
  n'en ont pas besoin, le fusionné y est le posé. Nommer `valeur`, même identique à
  celle en place, reste une pose ;
- la portée descend dans les composites : sous-champs d'un `object`, et sous-champs des
  éléments d'une `list` (une liste réémise remplace l'ancienne **en bloc**, couches
  comprises — c'est là que la perte est la plus lourde). Un sous-champ fautif se nomme
  **une fois** pour toute la colonne, sur le premier élément qui le porte ;
- elle ne s'applique **ni** à `readonly`, **ni** à la colonne qui porte le cycle de
  vie : exiger une provenance de qui n'écrit pas la valeur refuserait des écritures que
  personne ne peut corriger ;
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
un objet, un nombre) : une couche que la plateforme ne connaît pas n'exigerait rien, et
son auteur croirait la provenance exigée — la faute exacte que l'attribut a commise
pendant trois schémas. Le **lecteur**, lui, reste muet sur une déclaration illisible déjà
en base : un vieux schéma ne doit pas faire exploser une écriture (même parti pris que
`max_length_of` / `pattern_of`).

**⚠️ La liste VIDE est acceptée, et ce n'est pas une tolérance.** `required_layers: []`
veut dire « aucune couche exigée » — c'est ce que l'**écriture** en fait depuis le premier
jour (`required_layers_of` rend `()`). La pose la refusait : la même valeur avait deux
sens dans les deux moitiés du même attribut. Le coût était mesuré et immédiat — **quatre
tableaux vivants portent `required_layers: []`**, dont deux chez une organisation
cliente, et le refus les rendait **inpatchables** : `patch_schema` recopie le schéma
courant, le fusionne et le repasse **entier** par `set_schema`, donc un patch portant sur
une **tout autre colonne** aurait été refusé lui aussi. Un geste qui marchait aurait cessé
de marcher sur un schéma que personne n'a modifié. Ce qui reste refusé à la pose est ce
qui protège vraiment : une valeur qui n'est **pas** une liste, ou une liste contenant une
couche **inconnue**.

**Les deux faces, prouvées par une épreuve chacune.** Le seam d'écriture est unique
(`_check_row`), mais « unique » est une lecture de code : l'avertissement voisin sur les
clés de schéma non reconnues ne sort que côté REST alors que la description de l'outil
promet le contraire. Le banc joue donc `data_write` (face outil, `McpError`) **et** la
route `POST /rows` / `PATCH /rows/{row_id}` (face REST, `400 row_invalid`), et vérifie que
**rien n'est écrit** dans les deux cas.

**Ce lot n'active la capacité nulle part** : aucun tableau ne déclare de couche exigée.
Il en existe pourtant qui portent l'attribut — **quatre tableaux vivants**, tous avec la
liste **vide**, donc sans rien exiger (relevé du 06/09/2026). Restent les barreaux 2 (un
motif sur le CONTENU d'une couche) et 3 (lier la présence d'une couche au contenu d'une
autre) — oto#75.

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

⚠️ **La valeur inconnue est refusée à la pose**, comme l'est depuis le 01/10/2026 une
CLÉ que son niveau n'admet pas. Un `agent_access: "non"` retomberait en
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
`data_app`), et `core._entry` (le catalogue de `data_list_datastores`). Le store, lui,
continue de lire le schéma ENTIER par `_schema_of` : masquer à la validation ferait
passer l'écriture de l'agent comme un champ hors schéma.

**Le réglage ne se rouvre pas sans TITRE** (`acces_agent.refus_de_schema`, câblé dans
`set_schema`, donc aussi dans `patch_schema` qui y repasse) : poser, changer ou retirer
`agent_access` est réservé à qui **possède** le tableau (lui, son org, son équipe) ou le
**gouverne** — le palier du forçage de `readonly` (`_peut_forcer`), par l'outil comme
par l'écran. Qui n'a reçu qu'un accès en écriture partagé est refusé sur les deux faces.
Jugé sur le DELTA ancien→nouveau, pour qu'un patch qui TRANSPORTE le réglage passe (et
le palier n'est interrogé que si le réglage bouge). Sans ce cran la capacité serait
décorative — la manœuvre « lever, écrire, refermer » de #658/#668 n'aurait même pas eu
besoin de refermer.

⚠️ **Jusqu'au 24/09/2026, ce refus regardait la FACE** (oto#93) : tout appel par un
outil était refusé, propriétaire compris, alors que le forçage voisin de `readonly` lui
était ouvert sous le même palier ; et l'écran laissait passer un simple écrivain
partagé. Les deux crans suivent désormais la même règle.

Le second refus, lui, reste une affaire de **face** : sur la face agent, on ne repose
pas un schéma entier sur un tableau réglé (`set_schema` REMPLACE, et la face agent
masque les colonnes `none` à tout appelant, propriétaire compris : le refus nomme
`data_patch_schema`).

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

⚠️ **La leçon de méthode qui reste de #607, son cran ayant été retiré le 07/09/2026.**
Ce paragraphe a longtemps écarté un lot au motif que « la pose lit le RUN à chaque
écriture — une I/O sur le chemin chaud ». Les deux moitiés étaient fausses : le run de
l'appel est une ContextVar, sans I/O, et la seule source qui touchait la base se cachait
derrière un cache par run. La conclusion reposait donc sur un coût **supposé, jamais
mesuré**. *Une réserve de perf qu'on n'a pas mesurée est une opinion qui prend
l'autorité d'un fait en étant écrite ici.*

⚠️ **Le troisième geste a manqué un an, et son absence rendait une demande simple
impossible.** Entre la fusion qui COMPLÈTE et le retrait qui enlève une COLONNE, rien ne
savait enlever un ATTRIBUT — un `role` mort, une déclaration qu'on décommissionne. Le
seul chemin était de reposer le schéma ENTIER, c'est-à-dire exactement le geste
destructeur décrit ci-dessous. *On avait troqué la destruction accidentelle contre
l'impossibilité de retirer un mot.* Découvert en cherchant à exécuter une demande du
propriétaire — sortir du schéma des rôles que plus rien ne lit : la demande était
simple, la plateforme ne savait pas la servir sans risquer tout le reste.

Le relevé d'effacement suit : une annonce de la forme `champ.attribut` tait cette
disparition-là, et **rien d'autre** — le filet reste tendu sur ce qui se perd en plus,
seul moyen de voir une fusion qui laisserait échapper quelque chose.

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
`remove_attrs` = `{colonne: [attribut, …]}`, le **retrait d'un ATTRIBUT sur une colonne
qui reste** (`dsv2.remove_field_attrs`, 07/09/2026) ;
`key`/`new_rows` = les clés de tête, inchangées si omises (le cran
de création y entre le 29/08/2026 sous le nom `key_required`, #516 ; renommés oto#127) ; les crans de CHAMP
`readonly` / `origine: "system"` (#586/#606) se posent et se lèvent par `fields`, `null`
levant sans réécrire ni toucher les lignes. Le résultat repasse par
`store.set_schema`, donc par ses gardes (doublons de clé métier, index UNIQUE,
`new_rows: "reject"` sans `key` — poser `key` et `new_rows` dans le même patch passe)
et ses avertissements — la logique n'est pas doublée. ⚠️ Un réglage remis à son défaut
ÉCRIT `"create"` au lieu de retirer la clé : le relevé d'effacement ci-dessous compte
les disparitions de tête sans exception — retirer la clé ferait crier un geste
explicite sur lui-même. ⚠️ `remove` sort le champ du
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

> ⚠️ **Retirés le 21/10/2026 (oto#124, oto-backend#1292)** : le relevé `hors_schema` et le
> cran `reject` décrits dans les deux paragraphes suivants n'existent plus — une colonne
> non déclarée est refusée sur tous les tableaux (`colonnes_non_declarees`, dernière
> section), au même seam et sur le même `posed`. Ils restent ici pour l'histoire.

**Écrire hors du format se DIT, sans être refusé (#294).** Sur un datastore `strict`,
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
- Il se pose par `data_patch_schema(unknown_columns="reject")` (ex-`unknown_fields`,
  oto#127) — un tableau se ferme
  quand il a FINI d'être exploré, donc quand son schéma est long, et le poser par `set`
  obligerait à réécrire quatre-vingts champs pour une clé de tête. `enforced` l'annonce.

**…mais DANS un composite déclaré, `strict` REFUSE (#544, 29/08/2026).** La liberté
qu'on protège au premier niveau n'existe pas un cran plus bas, et c'est toute la
différence : une clé inconnue en tête de ligne crée une vraie **colonne**, que
l'interface affiche et qu'on peut déclarer après coup — c'est ce qui permet d'explorer
un tableau avant de le typer. Dans un `object.fields` ou un `list.of.fields`, il n'y a
pas de « sous-colonne libre » : la déclaration EST le seul référentiel, l'attribut
serait stocké là où ni le schéma ni l'interface ne le lisent — et l'export CSV rend la colonne-liste en une seule cellule, chaque élément en JSON, joints par `; `, sans colonne
par attribut (il n'existe pas d'export à plat, cf. §5.3 de
`datastore-colonne-tableau.md`). Sur un tableau `strict`, un attribut non déclaré est
donc **refusé**, en nommant l'élément — `contacts[1].email_pattern`.

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
paierait la différence. ⚠️ Le refus est **borné** : un attribut inconnu est nommé une
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
tel quel, donc **créer** une ligne depuis un gabarit ne change pas de comportement (sauf
`{}`, qui n'est jamais stocké comme valeur : sur une case vide il est ÉCARTÉ et dit dans
`valeurs_ecartees`, oto#165 — `datastore_columns.sans_les_objets_vides`) — et
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
résolus (`id=@claimed` — le pronom d'alors, retiré depuis —, ou identifiant de table
absent des arguments). Enfin, la valeur
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
du LLM. Un datastore peut déclarer une **clé métier** au schéma (`schema.key`, ex.
`"email"`/`"siren"` ; cf. `data_set_schema`) : une écriture qui porte une valeur de clé
déjà présente ne duplique pas (param `key` explicite prioritaire) — nommée par l'appel
(`key=`), elle **désigne** la ligne et la modifie ; sinon l'écriture **ajoute**, et ne
fusionne qu'avec `upsert=true` (sans lui refusée depuis le 21/10/2026 : oto#141,
ci-dessous) ; les rows sans clé sont appendées. Renvoie `{inserted,
updated, count, key, ids, fusions?}`.
⚠️ **Ajouter n'est pas désigner (oto#141, arbitré le 30/09/2026, précisé le 01/10).**
Jusque-là, toute écriture SANS `id` dont la valeur de clé existait déjà mettait la ligne
en place à jour en silence — ligne seule, lot, REST, upload signé —, et deux lignes d'un
même lot à la même clé fusionnaient entre elles (`ids: [r1, r1, r2, r3]` pour trois
lignes écrites, rien ne le disant). La règle :
  - **Désigner** — `id=`, ou `key=` passé EXPLICITEMENT (lot ; ligne seule quand il nomme
    la clé déclarée, `data_write(row=, key=)` ou `POST …/rows?key=` ; clé nommée à la
    frappe d'un upload signé ou à `oto_import`, scellée dans le jeton —
    `target.cle_passee`), ou toute écriture sans `id` sur un tableau FERMÉ
    (`key_required`) : une valeur présente MODIFIE sa ligne, sans `upsert` ni
    avertissement ; une valeur absente crée la ligne (sauf tableau fermé).
  - **Ajouter** — ni `id` ni `key=` : une valeur déjà présente fusionne avec
    `upsert=true` ; sans lui, refusée.
  - **Deux lignes du même appel à la même clé** : on ne désigne pas deux fois la même
    ligne — sans `upsert`, lot refusé ENTIER, en désignation comme en ajout.
  - Paramètre `upsert: bool` (défaut `false`) sur toutes les faces qui prennent une clé :
    `data_write` (ligne et lot), `POST …/rows` (`?upsert=true`), `POST …/rows/batch`, la
    frappe d'un upload signé (scellé comme `donnees_d_origine`) et `oto_import`
    (`datastore/upsert_implicite.py`). Livré le 01/10/2026 en préavis daté (un
    avertissement dans `notices`), le refus est en vigueur depuis le 21/10/2026 ; la
    date, son réglage et les avertissements sont retirés (oto-backend#1292).
  - **Le refus** : `business_key_exists` (MCP : `INVALID_PARAMS` ;
    REST et réception d'upload : 409), qui nomme la ligne en place et les gestes
    (désigner par `id=`/`key=`, ou `upsert=true`) ; un lot est jugé ENTIER avant sa
    première ligne — doublons internes toujours, clés déjà en base s'il ajoute
    (`juger_le_lot` ; lookup `db.datastore_find_row_id_by_key` par valeur distincte,
    texte de clé rendu par la base `db.datastore_textes_de_cle` : aucune règle de
    comparaison à part) —, un import en tranches sur le fichier entier avant la
    première tranche. Une course perdue sous l'index suit la même règle.
  - **`fusions`** (lot, upload, import), rendu quand il y en a :
    `[{rang, dans_rang, id, cle}]`, `dans_rang` = la ligne du MÊME geste qui a posé la
    ligne visée (`null` : déjà en base), à travers les tranches d'un import ; `ids`
    reste aligné rang pour rang. L'accusé d'un upload signé, lu par un porteur de lien
    anonyme, le rend SANS `id`, et son refus ne nomme aucune ligne par son identifiant
    (oto#86).
  - `upsert=true` est refusé là où il ne fusionnerait rien : avec `id=` (MCP), et sans
    clé en vigueur (`upsert_without_key` à la frappe d'un upload, refus d'entrée
    ailleurs). Un `key=` de ligne seule qui ne nomme pas la clé déclarée reste refusé
    (`jetons.refus_de_key_sans_lot`), sur la face REST aussi.
  - Bancs : `tests/datastore/test_upsert_implicite_141_live.py` (chaque face).
⚠️ **La fusion par clé n'est PAS réservée au lot** (vérifié le 28/08 sur table jetable, et
la doc servie disait le contraire jusqu'au 29/08) : `data_write(row={siren: X})` **sans
`id`**, sur un tableau qui déclare `key: "siren"` et où X existe, met à jour la ligne
existante et rend son identifiant — `append_row` applique la même dédup que le batch
depuis #109 ch.3 (et la même règle `upsert` depuis oto#141). Croire l'inverse fait écrire en lots de un pour obtenir une fusion, ou
pire, fait chercher un `id` qu'on n'a pas. Cœur : `store.write_rows` →
`_write_rows_to_ns(ns_id, rows, key)` (keyé par ns_id → réutilisable **hors contexte d'org**)
+ `db.datastore_find_row_id_by_key` (lookup dédup JSONB paramétré). Pour du **volumineux**,
préférer `oto_upload_url(target='datastore')` (push NDJSON/CSV out-of-bande → même batch-upsert ;
ns_id scellé au mint, autz réappliquée via `ownership.can_access(datastore_namespace, write)`).
Cf. `docs/projects.md` §push out-of-bande (issue #105).

**Server-side import (`oto_import`, capability `me.import`, `POST /api/me/import`).**
When the file is already reachable — a link, a Drive file (a native Sheet exports its
first tab as CSV), a project file, a Gmail attachment — the server fetches it through
`file_source.resolve` (egress guard, 25 MB, redirects followed only here, each hop
re-checked, no https→http) and writes it; nothing passes through the model. Targets:
`datastore` and `project_file` only. CSV is read by `csv_tolerant` on BOTH paths (this
one and the signed PUT): separator detected by field-count consistency (`,` `;` tab `|`,
comma wins ties), UTF-8 / UTF-16 (BOM) / cp1252, headers matched to keys, then ignoring
case and accents, then to declared labels; two headers on one column are refused before
any write. `declare_columns` (default true here, off on the PUT) declares unmatched
headers as text columns (label = header). The write runs in 500-row slices under a 40 s
budget: an unfinished call returns `resume_from` + `source.sha256`, and a resume is
refused if the file changed — safe without a key. A refused row names its absolute
position in `details` (`row`, `written`, `resume_from` = the next row). The source
URL is masked in the call log and returned without its query string. Measured on local
Postgres: ~340 rows/s appended, ~425 rows/s upserted on a key (10k rows, 9 columns).

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
tout lecteur SQL adressé par champ passe donc par `db.field_value_sql` /
`field_read_sql` — filtres, tri, agrégats, contrôles de schéma — et aucun ne recopie
l'expression.

**Cette expression est le jumeau d'`unwrap`** (oto#163, `db/paths.py`). Pour une case :
un objet qui porte `valeur` rend cette valeur (un `null` reste NULL) ; un objet non vide
fait de couches seules (`origine`, `comment`, `link`, dérivées de `LAYER_KEYS`) vaut
NULL ; tout le reste — scalaire, liste, objet métier (une clé hors couches), `{}` — rend
son texte. Une case `{"comment": "à vérifier"}` est donc trouvée par `empty`, ignorée par
`not_empty`, `eq` et `contains`, regroupée et triée comme une case vide, jamais signalée
« hors options » à la pose d'un enum, et comptée par l'avertissement d'un champ
`required` ; son commentaire reste lisible par `champ.comment`. Avant oto#163, le SQL
retombait sur le texte de l'enveloppe, et ces cinq lectures comptaient la case remplie.

**La clé métier compare la valeur que sert la lecture** (oto#223) : l'index d'unicité
`ds_bkey_<ns>`, le lookup d'upsert, les groupes de doublons de la pose de schéma et leur
fusion par la maintenance prennent tous `bkey_index_expr`, qui EST la règle ci-dessus.
Une case sans valeur servie (couches seules, `{"valeur": null, …}`) n'entre pas dans
l'index partiel et ne fait doublon avec rien ; une clé nue et la même clé enveloppée
sont la même clé. Le lookup fait rendre le texte de la valeur cherchée par la base
(`#>> '{}'`), comme `data->>k` celui de la case : `true`, pas `True`. Jusqu'à oto#223,
trois règles coexistaient (index en `COALESCE` V1, doublons et fusion en `data->>k`
brut). ⚠️ L'index est d'EXPRESSION et le lookup ne le sert qu'à expression identique :
toucher la règle ou `LAYER_KEYS` change ce texte, et impose une migration qui
reconstruit les index (modèle : révision `0029_cle_metier_valeur_servie`) ; le banc
`test_cle_metier_index_223.py` fige la définition et le plan.

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
| `{"champ": Y}` sur un champ `readonly: true` | **refusé** si Y change une valeur posée (case absente, `null` ou `""` : se remplit ; `@empty` est posée) ; identique = no-op ; `{"champ": {"comment": …}}` passe (#606) ; `readonly_override=true` force, pour cet appel, si l'appelant possède ou gouverne le tableau (#658) |
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

⚠️ **Un nom de colonne VIDE ne s'écrit pas** (oto#231) : `""`, ou fait seulement
d'espaces, était accepté et créait une colonne qui ne se déclare pas au schéma, ne
s'adresse par aucun filtre et ne se lit nulle part. Refusé (`RowValidationError`, REST
`400 row_invalid`, import `400 bad_row`) en citant la clé, rien d'écrit, par la
validation des noms de colonne (`points._refuse_dotted_names`) — celle que la sonde de
`test_lot_refuse_cles_pointees.py` exige devant chaque porte d'écriture en base.

**Le blob lu en TEXTE pour l'embedding** est reconstruit avec les valeurs à la place des
enveloppes (`ROW_VALUES_TEXT_SQL`), sinon l'embedding porterait la provenance au même
titre que le contenu. Gardé par un `jsonb_path_exists` mesuré : ×6,4 si systématique,
×1,5 sur une table sans couches.

**La recherche `q` des lignes** (`data_rows`, `data_aggregate`, `GET …/rows`,
`GET …/aggregate`, lignes d'un nœud tableau) — #307. Chaque MOT de `q` doit se
retrouver, dans n'importe quel ordre, **par le sens** (tsquery `french`, accents et
casse repliés : « cahiers » trouve « Le Cahier », « corp acme » trouve « ACME Corp »)
**ou par fragment** (sous-chaîne : un bout de SIREN, d'URL, de référence collé tel
quel). ⚠️ Le fragment n'est pas un repli : une recherche tokenisée seule ne retrouve
pas « 52100 » dans « 552100554 », et l'utilisateur conclurait que la ligne n'existe
pas. Construction partagée avec `oto_search` (`db/search.py::_lexical_ou_fragment`),
à deux écarts près : un prédicat PAR MOT (le sens d'un mot, le fragment d'un autre),
et `plainto_tsquery` (un fragment « -123 » deviendrait `!123` en syntaxe web).
Sans `order_by`, une recherche **classe** : `ts_rank_cd` (|32, comme `oto_search`)
d'abord, les lignes trouvées seulement par fragment ensuite (rang 0), puis l'ordre de
création (`order_dir`) ; un `order_by` explicite, `_created_at` compris, garde la main.
Côté MCP, le curseur d'une recherche classée est donc un curseur d'OFFSET.
`q_scope` choisit le texte : `values` = les seules valeurs servies (fonction IMMUTABLE
`datastore_valeurs_texte_v1`, ni clé, ni couche, ni échappement JSON) ; `all` =
`data::text` entier, clés et couches comprises (retrouve une ligne par son
`comment`) — défaut `all`, une valeur inconnue est refusée. Performances, mesurées
sur 5 000 lignes d'un tableau parmi 30 000 : `values` passe par ses deux GIN
(`idx_datastore_rows_valeurs_fts`/`_trgm`, révision 0041) — 2 à 45 ms pour un mot
sélectif ; `all` filtre ligne à ligne sous `ns_id` (le planificateur ne prend pas les
GIN de `data::text` dans un tableau), la moitié lexicale lisant le vecteur
matérialisé `search_vec` — 0,55 s, le prix de l'ancienne sous-chaîne seule. La
fonction porte `SET jit = off` : sans lui, le JIT recompilait son corps à chaque
appel (200 ms par ligne, donc par écriture). Les deux GIN doublent à peu près le coût
d'une insertion (5 000 lignes : 1,9 s → 4,1 s).

⚠️ **Le coût de `values` suivait le nombre de lignes TROUVÉES, pas `limit`** (mesuré le
16/10/2026). Les deux GIN d'expression trouvent les candidats en moins d'une
milliseconde, mais l'index trigramme exige de REVÉRIFIER chaque candidat, ce qui
recalcule `datastore_valeurs_texte_v1(data)` ligne à ligne ; le classement
(`ts_rank_cd`) la recalcule une deuxième fois, le `COUNT` du total une troisième. Sur un
tableau large (~260 colonnes) : 19 à 22 s en prod pour un mot présent dans 1 243 lignes
sur 9 200, avec `limit=8`.

**Le texte est donc MATÉRIALISÉ par ligne** (`db/valeurs_servies.py`, révision 0061) :
`valeurs_texte` (le texte replié) et `valeurs_vec` (son vecteur), deux colonnes
nullables remplies par le déclencheur `datastore_rows_25_valeurs` à chaque écriture de
`data` — quel que soit le code qui écrit, l'ancien compris pendant la bascule —, et leurs
deux GIN (`idx_datastore_rows_valeurs_vec`, `idx_datastore_rows_valeurs_texte_trgm`). La
revérification et le classement relisent une colonne. Banc de 9 200 lignes larges (~110
colonnes), `limit=8` : un mot présent dans 1 243 lignes passe de 5,4 s à 29 ms (6 en
parallèle : 5,6 s → 53 ms).
- **Lues seulement quand leurs deux index sont valides** (`valeurs_servies.pretes`, relu
  au plus toutes les 60 s) ; avant, l'ancien chemin, à l'identique.
- **Une ligne pas encore remplie n'est jamais perdue** : chaque mot se cherche dans les
  colonnes, OU, pour une ligne dont `valeurs_texte` est NULL, par l'ancien prédicat sur
  le GIN d'expression. Elle coûte l'ancien prix, les autres le nouveau.
- **Le rattrapage** des lignes nées avant : `scripts/remplir_valeurs_servies.py` (à blanc
  par défaut), par `SET data = data` — le déclencheur calcule, la révision et le journal
  de la ligne n'avancent pas.
- **Le schéma du tableau n'y entre pas** : le texte ne dépend que de `data`. Un
  changement de schéma qui ne réécrit pas `data` ne change aucun texte servi ; celui qui
  le réécrit (renommage, retrait de colonne) passe par le déclencheur.
- Le prix : une écriture calcule le texte une fois de plus (le déclencheur, en plus des
  deux GIN d'expression). Une fois le rattrapage fini partout, retirer l'ancien
  prédicat ET ses deux GIN (révision dédiée) rendra une écriture moins chère qu'avant.

**`total_max`** (`/rows`) borne en plus le compte : au-delà, `total` vaut `total_max` et
`total_capped: true` le dit ; absent, le total reste exact (le dashboard pagine et
exporte sur lui).

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

**Sur une colonne-liste**, la même question se pose sans énumérer les rangs :
`group_by: "contacts[].fonction"` compte une occurrence par item de chaque ligne
retenue (`count` = les contacts, `count_rows` = les fiches), et `sum`/`avg`/`min`/`max`
sur `contacts[].<attribut>` portent sur tous les items. Le tri reste refusé. Détail :
`datastore-colonne-tableau.md` §5.1.

**Les compteurs de N colonnes en un appel** (`GET …/counts?fields=a,b,c`,
`data_aggregate(count_by=[…])`, 16/10/2026). Un écran qui affiche au-dessus de sa vue
filtrée les compteurs de 20 à 30 colonnes les demandait un `…/aggregate` par colonne,
et chaque appel relisait tout le jeu filtré. `count_by` lit `data` UNE fois par ligne,
amincie aux colonnes comptées et filtrées (le mécanisme de `thin_read_cte_sql`), puis
déroule chaque ligne en (colonne, valeur). Par colonne : `values` (les plus fréquentes
d'abord, `values_limit` = 50 par défaut, 500 au plus), `empty` (absente, `null`, `""`,
ou couche sans valeur : un vide est un vide), `distinct`, `total` (= lignes du jeu
filtré) et `truncated` — vrai quand des valeurs restent au-delà de la liste, jamais une
liste coupée qui aurait l'air complète. Mêmes `filter`/`filters`/`q` que `…/rows`.
Refusés en le disant : une colonne non déclarée (comptée, elle rendrait « tout est
vide »), une couche ou un chemin de liste (`group_by` les sert, une colonne par appel),
un doublon, et une colonne masquée aux agents sur la face agent. Mesuré sur un banc de
10 000 lignes à 260 colonnes : 25 colonnes en 1,1 s, contre 7,6 s en 25 `group_by`.

**Projeter coûte moins que tout lire** (`fields`, même date). Sous projection, les
couches d'une colonne ne sont aplaties que si l'une d'elles est NOMMÉE (`ville.comment`) :
`flat_layers` était payé pour chaque colonne puis jeté, et une projection sur 260 noms
nus coûtait plus cher que la ligne complète. Les bases d'une projection et les colonnes
déclarées se calculent une fois par page, plus une fois par ligne.

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
| `datastore/rangs.py` | l'écriture PAR RANG d'une colonne-liste (`contacts[0].email`, `contacts[+]`, `contacts[0]: null`) et l'ajout de lignes à une colonne texte (`journal[+]`) : grammaire, refus, gardes à l'élément, résolution contre la ligne en place (oto#22) |
| `datastore/reserves.py` | les champs que l'appelant n'écrit pas : refuser, et poser l'origine à sa place (#586/#606) |
| `datastore/claimable.py` | le périmètre de réservation déclaré (`lifecycle.claimable`, #517) : décision, clauses du pick, refus, phrase — **n'importe le moteur qu'à l'appel** |
| `datastore/schema.py` | **une FAÇADE, plus un corps** : elle ré-exporte les douze modules ci-dessous et rien d'autre. Une cinquantaine de sites importent `datastore.schema` — ce contrat les tient tous |
| `datastore/couches.py` | le vocabulaire des couches d'une cellule et leurs formes |
| `datastore/motifs.py` | le COÛT d'un `pattern` — la garde qui empêche un motif de figer le serveur |
| `datastore/telephone.py` | le type `phone` : la forme compacte d'un numéro (`normaliser`), que la validation juge (oto#103) |
| `datastore/declaration.py` | LIRE une déclaration : « que déclare ce schéma ? » |
| `datastore/cycle_de_vie.py` | états, transitions, terminaux, plafond de reprises, périmètre |
| `datastore/hors_schema.py` | une clé qu'un sous-record déclaré ne nomme pas, refusée ; la couche mal écrite |
| `datastore/champs_reserves.py` | `readonly` / `origine: system` / `agent_access` |
| `datastore/definition.py` | valider le SCHÉMA lui-même, à la pose |
| `datastore/cles_inconnues.py` | refuser une clé que son niveau n'admet pas (`schema_keys.ADMISES`) |
| `datastore/couches_exigees.py` | `required_layers` — ce que la valeur doit porter avec elle |
| `datastore/validation.py` | VALIDER une ligne à l'écriture, et ses textes de refus |
| `datastore/effacements.py` | fusionner / retirer un format, et le relevé de ce qui a disparu |
| `datastore/vocabulaire.py` | ce que CETTE version lit et fait respecter, dérivé du code |
| `datastore/non_applique.py` | ce qu'un tableau déclare et que la plateforme laisse inerte |
| `datastore/schema_ops.py` | poser/retoucher/nettoyer le FORMAT (mixin du store) |
| `datastore/outils.py` | les fonctions LIBRES du store : curseurs, horodatage, `_new_id`, clauses de filtre, adresse du tableau |
| `datastore/registre.py` | le REGISTRE des tableaux : lister, créer, renommer, supprimer, adresser (mixin) |
| `datastore/lecture.py` | LIRE des lignes : une, une page, un curseur, un compte, un agrégat (mixin) |
| `datastore/ecriture.py` | ÉCRIRE une ligne : ajouter, fusionner, remplacer, patcher, effacer (mixin) |
| `datastore/lots.py` | écrire un LOT — le chemin des imports, et ses refus qui NOMMENT la ligne (mixin) |
| `datastore/file_de_travail.py` | la FILE DE TRAVAIL : réserver, rendre, et les deux gardes du bail (mixin) |
| `datastore/controles.py` | ce qu'une écriture a le DROIT de poser, ce qu'on en écarte, et le relevé rendu (mixin) |
| `datastore/core.py` | le store qui COMPOSE — noyau d'identité (`__init__`, `_resolve`, `_row_to_dict`, `_trace`) + les sept greffons ci-dessus |

Déplacements PURS : `db/datastore.py`, `datastore/schema.py` et `datastore/core.py`
ré-exportent, la surface plate `db.<fn>` est figée par `tests/test_db_surface_frozen.py`
(cliquet : on peut ajouter, jamais retirer). ⚠️ Une scission fait dormir les noms hérités
des globals dans les branches rares — balayage figé par
`tests/datastore/test_datastore_ns_duplicate.py`.

⚠️ **Deux pièges que la coupe de `schema.py` a payés le 07/09/2026**, et que la coupe
de `core.py`, le même jour, a repayés tous les deux — huit clés (`destination`,
`permission`, `lease`, `claimed_run`…) seraient sorties du vocabulaire dérivé, deux
bancs visaient encore la porte d'entrée. Ils frapperont la prochaine :

- **`vocabulaire._read_keys` scanne une liste de FICHIERS**, pas de clés : c'est ainsi
  que le serveur établit ce qu'il interprète, en lisant son propre source. Un module
  qui se met à lire un attribut de colonne doit y être ajouté — sinon la garde de la
  déclaration (`tests/test_schema_keys_oto56.py`) ne le voit pas, et une clé lue
  pourrait n'être admise nulle part, donc refusée à la pose.
- **Un banc qui remplace une fonction SUR LA FAÇADE ne mord plus.** Une ré-exportation
  est une seconde référence vers le même objet : le module appelant, lui, a importé
  l'originale. Cinq bancs ont rougi bruyamment ; le vrai danger est celui qui reste
  VERT en ne testant plus rien. Viser le module qui EXÉCUTE, jamais la façade.

## Ce qu'oto SAIT d'un champ, et ce qu'il ne saura jamais (14/08)

⚠️ **Ce qu'oto SAIT d'un champ, et ce qu'il ne saura jamais** (tranché par Alexis le
14/08). Oto gère les **types standards** : un `number` se trie numériquement, une date
chronologiquement — l'ignorer donnait `10, 100, 2, 9` (livré v1.112.0). Il ne gère PAS
l'interprétation métier d'une VALEUR : que `20_49` soit une tranche INSEE qui suit
`1_2` est le savoir du consommateur, jamais celui d'oto. Entre les deux, l'ordre des
`options` déclarées au schéma **est honoré** — parce que c'est une DEMANDE adressée à
oto, pas une compréhension qu'il aurait du métier. La frontière tient dans une phrase :
**exécuter une déclaration n'est pas deviner une convention.**

## La face REST est 100 % DÉRIVÉE depuis le 2026-08-12 (#302)

> **La face REST est 100 % DÉRIVÉE depuis le 2026-08-12 (#302)** : les 17 routes
> écrites à la main d'`api/datastore.py` (10 chemins) sont des capacités
> (`capabilities/datastore/{datastores,rows,schema,sharing}.py`, aux côtés de
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
   conserve l'entrée, **commence par un EXTRAIT des valeurs supprimées** (datastore,
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


## `@claimed` — le pronom de la réservation, RETIRÉ le 07/09/2026 (#517, #599, #645)

**Ce qu'il faisait**, du 29/08 au 07/09/2026 : `data_write(datastore=…, id="@claimed",
row={…})`, et le même geste sur `data_release`, `data_rows`, `data_delete_row`,
`data_url`, `data_aggregate`. Le serveur relisait le bail du run courant et écrivait sur
la ligne qu'il tenait. `datastore="@claimed"` marchait aussi — la réservation porte le
tableau autant que la ligne.

**Pourquoi il est né.** Pour écrire sa fiche, un agent devait repasser les trente-deux
caractères que sa réservation venait de lui rendre. Mesuré sur trois passages d'une
campagne réelle, il en altère un (`…-7c06-…` pour `…-7c16-…`) ou en fabrique un dans une
convention étrangère (un uuid v4, 24 hexadécimaux à la mode ObjectId, une chaîne
`temp_blocage_<date>`). **Recopier une chaîne aléatoire n'est pas une question de
rigueur** : aucune consigne ne l'obtient. Et ce qui coûte n'est pas le refus, c'est ce
qui le suit — l'agent refusé réessaie **sans identifiant**, et une écriture sans
identifiant **crée** au lieu de corriger : le 29/08/2026, deux entreprises inexistantes
sont nées ainsi dans un tableau d'évaluation ; la veille, cinq fiches d'essai dans le
fichier d'une cliente et dans un livrable déjà remis. **Fréquence faible, et la seule
famille qui fabrique de la donnée fausse.**

**Pourquoi il est parti.** *Il se résolvait par le RUN courant, et un agent est sans
état.* Il n'y a pas de « moi » stable auquel accrocher un pronom, et plusieurs runs d'un
même compte coexistent : « la ligne que je tiens » était ambigu **par construction**, pas
par accident d'implémentation. Chaque palier l'a montré sous une forme nouvelle — pas de
run sur l'appel, le run tient ailleurs, le run tient plusieurs lignes (#517), le run est
clos (#645 : **99 refus sur 200 écritures** d'un harnais qui écrivait après
`run_finish`) — et chaque fois le correctif a été un refus mieux écrit, jamais une
adresse moins ambiguë. Bilan à la mesure du retrait : **1 300 refus en 90 jours**, tous
des agents qui l'emploient alors que leur run ne tient rien. Les tenants tiers ne
l'employaient pas — toute cette famille de refus leur totalise quatorze cas en trois mois.

> **Un pronom suppose un locuteur qui dure.** Sur une surface sans état, le raccourci qui
> économise une recopie achète l'ambiguïté — et il la paie en refus, pas en données
> fausses, ce qui le rend long à voir.

**Ce qui reste, et qui répond au besoin d'origine.** L'agent ne recopie toujours rien de
mémoire : `data_claim_next` lui rend la ligne ENTIÈRE, donc son `_id` **et** sa clé
métier, et c'est cette réponse qu'il relit. Là où l'identifiant se perdait quand même,
deux textes le lui rendent :

- **le refus du pronom** (`jetons.JETONS_RETIRES`) ne dit jamais « inconnu » ni
  « introuvable » : il dit que le raccourci a été retiré, pourquoi, et nomme les **deux**
  façons d'adresser la ligne — par `_id`, ou par la clé métier. *Un jeton retiré ne
  disparaît pas du trafic le jour où on le retire ; s'il repartait tel quel, le stockage
  enverrait chercher une faute de frappe dans une chaîne parfaitement orthographiée.*
- **la piste des refus d'adresse** (`DatastorePg.claimed_hint`) énonce l'identifiant que
  le run tient, au moment où « introuvable » tombe — c'est-à-dire au seul instant où
  l'agent peut encore corriger. Elle renvoyait vers le pronom ; elle rend l'identifiant.

### Les leçons du pronom, qui lui survivent

Elles ne sont pas de lui : elles ont seulement été payées sur lui, et elles vivent
maintenant dans du code qui reste.

- **Un exemple dans un refus est un gabarit à remplir.** Le 29/08 à 15:24, « un
  identifiant a la forme `01a04aef-…` (cinq groupes hexadécimaux) » a produit
  `6738f4c2-57c0-43b9-9d78-XXXXXXXXXXXX` — douze X pour le groupe inconnu. `_introuvable`
  **décrit** désormais la forme sans en montrer une, et dit quand ce qui est reçu n'a pas
  cette forme : c'est la preuve qu'il a été inventé, pas altéré.
- **Une invitation à fournir un identifiant est une invitation à l'inventer.** Le cas
  NORMAL derrière « ton travail ne tient rien » est la fin de file, pas une réservation
  oubliée : le claim avait rendu `row: null` parce que la dernière ligne était sous le
  bail d'un pair, qui l'a écrite 71 ms plus tard. Le rendu d'un claim à vide dit donc de
  **n'écrire rien**, de n'inventer aucun identifiant, et de terminer son travail.
- **Un refus juste qui n'est pas le bon refus coûte autant qu'un refus faux** (#645) : il
  envoie chercher une réservation oubliée là où c'est l'ordre des gestes qui est en
  cause. Deux heures perdues, sur un mécanisme découvert deux fois à douze heures d'écart.
- **Quand un agent met la bonne valeur dans le mauvais champ, c'est une information sur
  la façon dont il a compris ce qu'on lui a dit.** À sa première rencontre avec l'alias,
  la flotte l'a posé dans `namespace` et non dans `id` — deux écritures refusées sur
  cinq. On leur retirait un champ à recopier ; ils y mettaient le raccourci qu'on venait
  de leur apprendre, et ils n'avaient pas tort : on leur avait dit « la réservation est
  l'adresse », et une adresse commence par le tableau. *Refuser sur le champ voisin, c'est
  refuser une demande qu'on sait satisfaire.* La couture `jetons.py` est née de là, et
  elle reste — c'est elle qui porte aujourd'hui le refus du pronom.
- **Un agent n'apprend pas une notion par verbe.** L'alias était accepté sur `data_write`
  et `data_release`, refusé sur les quatre verbes voisins, sur le même objet et dans la
  même session. Le geste d'adresse est depuis écrit **une fois** (`_adresse`) et servi à
  tous : c'est ce qui fait que le refus du pronom, aujourd'hui, est le même partout.

⚠️ **Ce qui part avec le pronom, et ce qui ne part pas.** Partent : `CLAIMED_REF`,
`est_ref_reservation`, `resolve_claimed_ref`, `resolve_claimed_target`, les refus qui les
accompagnaient (`ClaimedRefUnresolved`, y compris celui de la clôture, #645), la lecture
du bail restreinte par libellé de `worker`, et les paramètres `store` / `worker` / `ligne`
de la couture d'adresse — aucun n'existait pour autre chose. **Ne partent pas** : le BAIL
lui-même (`claimed_by`, `claimed_until`, `claimed_run`, `datastore_claimed_rows`,
`data_claim_next`, `data_release`), qui n'a jamais été le pronom malgré le mot commun ;
`slot:<nom>`, fonctionnalité produit vivante ; `*` ; la garde `PARAMETRES_D_APPEL` ; et
`db.run_closed_at`, écrite pour le refus de #645 et qui n'a plus d'appelant — c'est
**dit** plutôt que corrigé en la supprimant, parce qu'une lecture juste qui se voit se
redemande, là où une lecture retirée se réécrit de travers.

## Le run sait où il travaille (#631, 29/08 21:11)

Dans un même travail, `data_claim_next(<nom>)` ok à 21:10:05, puis
`data_write(<nom>, id=<ligne>)` refusé **« namespace inconnu »** à 21:11:23, puis
`data_write("@claimed")` ok à 21:11:35 (ce pronom a été retiré le 07/09/2026 ; le
diagnostic ci-dessous ne dépend pas de lui) — 103 refus de cette famille sur la soirée. La
cause n'était pas dans le datastore : l'écriture refusée était le seul des trois appels
**sans axe `_org=`**, donc résolue dans l'org MAISON de l'appelant, où le tableau n'existe
pas. Le journal ne montre pas l'axe (le middleware le retire des arguments avant le sink) ;
la preuve est la colonne `org_id` stampée — celle du tableau sur les deux appels ok, la
maison sur le refus. Le pronom échouait pareil, et pour la même raison : il relisait le
NOM dans la réservation, puis le résolvait dans la mauvaise org.

Deux gestes, sur le seul chemin qui échouait (`datastore/hors_org.py`) :

- **résolution par la réservation** : quand le nom demandé est celui d'un tableau que la
  réservation active du run porte, `_resolve` le trouve par elle — sans axe. Le bail
  LOCALISE, il ne donne aucun droit : `ownership.can_access` reste exigé, org-agnostique
  (un tiers qui connaît le jeton d'un run n'y gagne rien — testé).
- **sinon le refus le dit** : « il existe dans une autre de tes organisations : org X
  « … ». Cet appel a été résolu dans l'org Y « … » — passe `_org=X` ». La face REST le
  disait déjà
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

## Les jetons réservés — où chacun s'écrit, et les trois issues (#517, 29/08)

Deux jetons voyagent dans les appels du datastore. **Un seul endroit dit où chacun a
un sens** — `oto_mcp/datastore/jetons.py` —, et les deux faces s'en servent.

| jeton | ce qu'il désigne | champs qui l'acceptent |
|---|---|---|
| `slot:<nom>` | le tableau bindé sous ce nom par le projet actif | `datastore` |
| `*` | toutes les colonnes | `fields` |

Le même module tient une **seconde** liste, `JETONS_RETIRES`, et il n'y en a qu'une
entrée : `@claimed`, retiré le 07/09/2026 (§ ci-dessus). Un jeton retiré n'est plus résolu
nulle part et n'est accepté par aucun champ ; il reste nommé pour une seule raison — que
celui qui l'écrit encore reçoive un refus qui NOMME le geste qui aboutit, au lieu du
« tableau inconnu » du stockage. *La liste est faite pour se vider quand le trafic se sera
tu.*

**Trois issues, jamais une quatrième :** (et les champs réservés PAR LE SCHÉMA —
`readonly`, `origine: "system"`, #586/#606 — ne sont pas des jetons : ils se jugent
dans le store, là où le schéma est connu, cf. § `key_required` et suivants.)

| ce qu'on lit | ce qui se passe |
|---|---|
| jeton **accepté** par ce champ | résolu |
| jeton **reconnu mais mal placé** | refus qui NOMME le champ où il s'écrit |
| jeton **inconnu** ici | rien — la valeur part telle quelle |

### `fields=["*"]` demande TOUTES les colonnes

`*` est le chemin vers le brut sur `oto_doc` et sur le feed depuis toujours. Sur
`data_rows` il tombait dans « colonne inconnue » et rendait `_id` seul : l'agent croyait
demander la ligne entière et recevait une ligne vide, **sans erreur**. Un jeton qu'une
autre surface de la même plateforme accepte n'est pas une faute de frappe.

⚠️ Le delta d'empreinte servie de ce lot est **nul** : rien n'a été ajouté aux
descriptions. C'est délibéré, et c'est la mesure du 27/08 qui le dicte (§ longueur des
descriptions ↔ appels malformés). Ce lot ne rend possible que ce que les agents
**tentaient déjà** ; il n'a donc rien à leur enseigner.

### Pourquoi une couture, et pas une garde de plus

L'inventaire du 29/08 est parti chercher des jetons mal **nommés** ; il a trouvé que
les cas coûteux sont les jetons mal **placés**, et parmi eux **ceux que rien ne
refusait** :

- `@claimed` écrit dans le **contenu** d'une ligne — accepté, gravé en clair dans un
  fichier client (le pronom étant retiré, ce cas est clos : ce n'est plus une adresse
  mal placée, c'est une chaîne) ;
- `_run_id` posé comme **colonne** — même famille, et **toujours vivante** : un contexte
  d'exécution (ADR 0038)
  inscrit dans une fiche livrable ;
- `slot:<nom>` sur une opération de **ligne** côté capacité — passé brut au stockage,
  qui répondait « namespace inconnu », **alors que les opérations de schéma de la même
  couche le résolvaient depuis toujours**.

> **Une divergence qui refuse est visible ; une divergence qui répond une cause fausse
> s'instruit pendant des jours.** Les deux premières ne refusaient rien du tout.

### ⚠️ Ce que la couture ne fait PAS, et c'est délibéré

**Elle ne devine pas.** La reconnaissance est exacte : `slots:x` n'est pas un jeton, il
part tel quel et échoue comme avant. *Un alias qui pardonne remplace une chaîne à recopier
par une grammaire à deviner — la même faute, un cran plus haut.* La règle vaut aussi pour
la liste des retirés : `@claim` et `@claimed-2` ne sont pas `@claimed`, et ne reçoivent
donc pas son refus.

**Elle ne regarde pas les mêmes jetons dans le contenu que dans l'adresse.** Seuls les
paramètres d'appel sont refusés dans une valeur de ligne, parce qu'ils n'ont **aucun** sens
comme donnée. `slot:`, `*` et — depuis son retrait — `@claimed` sont des chaînes qu'une
ligne peut porter — « slot: machine à café » est une note, pas une adresse.
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
du plafond de réservations), **quelle que soit l'issue** (`done`/`partial`/`failed`/`blocked`), et
le dit dans sa réponse (`rows_released`). Best-effort : si la base tousse, le run se
ferme quand même et la ligne reste au bail. Le niveau base était couvert
(`test_row_lock_native.py`) ; le lien entre le **verbe servi** et la libération est figé
par `tests/test_run_finish_releases_613.py`, sur le chemin réel (middleware + outils
montés par `register_all`, PostgreSQL) — y compris « un autre run qui se ferme ne rend
rien » et « la ligne rendue est reprise par le claim suivant ».

⚠️ **La libération appartient au PROPRIÉTAIRE du run**, comme la clôture (`db.finish_run`,
#108) et la clôture REST (`run_thread._clore`) : un `run_id` est une poignée que
n'importe qui peut taper, pas un droit. `run_finish` sur un run indexé sous un AUTRE
compte ne rend rien (`rows_released: 0`) ; un run absent de l'index (`run_start` le pose
en best-effort) garde le comportement d'avant. Figé par le même banc.

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

## Deux écritures sur une même ligne : le verrou, puis la révision (12/09/2026)

**Le défaut, mesuré sur l'arbre servi (v1.274.0), base jetable.** Le patch par `id` —
REST `PATCH …/rows/{row_id}`, `data_write(id=…)`, et `append_row` avec `_id` qui s'y
promeut — lisait la ligne dans une connexion du pool, fusionnait en Python, puis
réécrivait le JSON ENTIER dans une autre (`datastore_update_row`, retiré). Deux patchs
partis ensemble sur deux colonnes DIFFÉRENTES en perdaient un dans **100 %** des cas
(9 à 15 % avec un décalage de 0 à 50 ms) ; la voie par clé métier, verrouillée depuis
#197, n'en perdait **aucun**.

**(a) Le verrou, invisible pour les écrivains.** `update_row` vit dans
`datastore/ecriture_par_id.py` et passe par `db.datastore_merge_row_locked` : tout son
travail sur la ligne tourne sous `FOR UPDATE`, le bail est vérifié sous ce même verrou
(`_lease_guard`), la fusion et l'UPDATE suivent. Le motif qui l'avait tenu hors du
verrou — « remplacer n'est pas fusionner » — valait pour `upsert_row` (retiré par oto#156),
pas pour un patch.
Le jugement du forçage (`_forcage_readonly`, le palier) reste AVANT le verrou : il lirait
une seconde connexion en tenant la ligne. `rafraichir_rang` garde le recalcul du vecteur
que faisait l'ancien UPDATE ; la fusion par clé ne l'a jamais fait, divergence conservée.

**(b) La révision, pour ce que le verrou ne ferme pas.** Un écrivain qui LIT, recalcule
(une liste renvoyée entière, un statut choisi d'après le statut en place) puis écrit
écrase ce qu'un autre a posé entre sa lecture et la sienne. Colonne
`datastore_rows.rev BIGINT NOT NULL DEFAULT 0`, servie `_revision` (chaîne) sur toute
ligne lue — lecture par id, listes et curseurs, `rows_by_ids`, réponse du PATCH, de la
réservation et de la file ; `data_rows(fields=…)` ne la rend que NOMMÉE
(`fields=["statut", "_revision"]`, sans avertissement), la projection par défaut restant
`_id` + les colonnes demandées. `_revision` est
une colonne de plateforme (`_META_COLS`) : relue puis republiée, elle ne s'écrit pas.

- **Précondition** : REST `?expected_revision=` (en QUERY — le corps EST le patch, une
  clé de plus y serait écrite comme une colonne ; un serveur qui ne connaît pas le
  paramètre rend `400 unknown_fields`, rien n'est écrit) ; MCP
  `data_write(id=…, expected_revision=…)`, refusée sans `id` ou avec `rows`.
- **Ordre sous le verrou** : le bail (`409 row_locked`, inchangé), puis la révision
  (`409 revision_conflict`, `details.current_revision` ; `INVALID_PARAMS` du même texte
  côté MCP), puis la fusion et l'UPDATE. Un refus sort AVANT l'UPDATE : ni la donnée, ni
  `rev`, ni `updated_at`, ni `claims` ne bougent. Aucune relance côté serveur.

**Pourquoi un déclencheur, et pas `rev = rev + 1` dans le code.** Préproduction et
production partagent la base, et la bascule bleu/vert sert deux versions pendant 120 s :
une écriture de l'ANCIEN code, qui ignore `rev`, doit la faire avancer, sinon une
précondition de la nouvelle passe par-dessus. Seul PostgreSQL voit toutes les écritures.
C'est le **premier déclencheur du dépôt** (`db/revision.py`) : fonction
`datastore_revision_avance()` (`NEW.rev := OLD.rev + 1`, reposée à chaque boot — `CREATE
OR REPLACE FUNCTION` ne verrouille pas la table), déclencheur `datastore_rows_20_revision`
`BEFORE UPDATE … WHEN (data | claimed_by | claimed_run | claimed_until IS DISTINCT)`,
**créé seulement s'il manque**, par la connexion DDL au `lock_timeout` borné : `CREATE
TRIGGER` prend `SHARE ROW EXCLUSIVE`, le reprendre à chaque boot de préprod bloquerait les
écritures de la prod. Changer la condition = un nouveau nom et le retrait de l'ancien.

- ⚠️ **Ordre des déclencheurs** : PostgreSQL exécute les `BEFORE ROW` d'une table dans
  l'ordre ALPHABÉTIQUE de leur nom — d'où `_20_`, qui laisse passer avant lui un futur
  `datastore_rows_10_bail`.
- **Avance** : `data` (valeur, clé ajoutée ou retirée ; absent → `null` → `[]` sont trois
  états), réserver, renouveler (`claimed_until`), libérer. **N'avance pas** : écriture sans
  effet, JSON seulement réordonné, `updated_at`, `claims`/`abandon_reason`, `embed_dirty`,
  `search_vec`.

**Bancs** : `tests/datastore/test_ecritures_simultanees.py` (trois portes, départ
simultané, course réservation/patch, recalcul sous précondition, refus sans effet, bail
avant révision) et `tests/datastore/test_revision_de_ligne.py` (couverture, écriture de
l'ancien code, échec fermé, lecture servie, garde AST « toute projection de ligne
sélectionne `rev` »).

**Hors lot** : l'identité du titulaire d'un bail sur REST. La face REST ne pose aucun run,
le titulaire ne s'y reconnaît donc pas (`POST …/claim` puis `PATCH` par le même compte =
`409 row_locked`) ; `claimed_by` n'est pas une identité (cf. `_lease_guard`). *(Fermé
depuis par l'en-tête `X-Oto-Run`, oto#227 — cf. `docs/rest-api.md`.)*

### La même précondition sur la suppression et la libération (23/09/2026, oto#217)

Le patch avait sa précondition ; **la suppression et la libération n'en avaient aucune**,
ni côté serveur ni côté contrat. Rejoué sur base jetable : une ligne modifiée entre la
lecture et le clic disparaissait AVEC la modification, et une libération forcée décidée
sur un écran d'il y a dix minutes retirait le bail qu'un SECOND worker venait de prendre
— la ligne repartait alors à deux travaux à la fois. Aucune perte n'a été constatée en
production ; c'est le mécanisme qui est fermé, pas un incident qui est réparé.

**Une seule révision, opaque, bail compris.** Une réservation ou une libération survenue
depuis la lecture EST un changement concurrent : le `409` qu'elle produit n'est pas un
défaut à contourner. Pas de révision « métier » séparée, aucun nouveau compteur.

- **Suppression** : `expected_revision` optionnelle, REST en query (`DELETE
  …/rows/{row_id}?expected_revision=`), MCP `data_delete_row(expected_revision=…)`. Elle
  passe désormais par une suppression VERROUILLÉE, jumelle de l'écriture par `id` et dans
  le même ordre — verrou, **bail**, **révision**, puis `DELETE`. Deux conséquences : la
  garde de bail ne repose plus sur une lecture séparée (la fenêtre où une réservation
  s'intercalait est fermée), et `409 row_locked` est désormais TRADUITE côté REST — elle
  traversait l'adaptateur, donc la suppression d'une ligne réservée sortait en **500
  muet**. Refus : `409 revision_conflict` (+ `details.current_revision`), `409
  row_locked`, `404 row_not_found`. Droits inchangés.
- **Libération** : `expected_revision` optionnelle sur la libération REST, appliquée aux
  DEUX régimes (avec et sans `worker`) — la cantonner au régime gardé l'aurait rendue
  inerte là où elle protège le plus. Zéro ligne touchée : relecture sous verrou, puis
  `404 row_not_found`, `409 revision_conflict`, ou le verdict d'avant. La politique de
  force ne change pas (toujours refusée à un jeton porté), et « rien à rendre » reste le
  succès bénin `released: false` / `reason: no_lease` (#517).
- **Les refus sont DÉCLARÉS** (`Capability.errors`) sur l'écriture par id, la suppression,
  la libération et les deux réservations : `row_locked`, `row_invalid`,
  `invalid_row_input`, `row_not_found`, `datastore_not_found`, `datastore_read_only`,
  `row_claimed`, `row_outside_claimable`, `worker_required`, `invalid_claim`,
  `jeton_mal_place`. Ils étaient émis depuis toujours et publiés nulle part — un client
  généré les traitait en panne. Leur vocabulaire vit dans
  `capabilities/datastore/_refus.py`, tiers neutre importé par `rows.py` et `claim.py`
  (importer l'un depuis l'autre changerait l'ordre de la table de routes, qui est figée).
- **Ce qui ne bouge pas** : un appelant qui ne passe pas la précondition garde exactement
  le geste d'avant. `_assert_writable` (garde de bail sur lecture séparée) ne sert plus
  qu'au REMPLACEMENT, seul chemin de ligne qui n'ouvre pas de verrou.

**Ce que la révision ne voit toujours pas** : `claims` et `abandon_reason` ne la font pas
avancer, alors que toute écriture les remet à zéro — une transition portant une révision
VALIDE peut donc remettre en file une ligne abandonnée. Et ni le lot, ni la fusion par
clé, ni l'upsert ne portent de précondition : un appelant qui tient la révision y écrit
quand même à l'aveugle. Les deux sont connus, aucun n'est fermé ici.

**Banc** : `tests/datastore/test_preconditions_de_revision_217.py` — les scénarios de
l'énoncé rejoués sur base jetable (transition aveugle qui écrase, la même refusée sous
précondition, réservation/libération intercalées, suppression après modification
concurrente, suppression sous bail, libération forcée après reprise par un second worker,
ligne abandonnée, zéro écriture partielle et aucune entrée de journal sur un refus), plus
le rejeu de chaque refus déclaré sur la route servie.

## Journal des révisions de ligne — M1, écriture fantôme (oto#273, 23/09/2026)

Le couple `origine`/`actuel` ne garde que deux états, sans auteur ni date. oto#273 le
remplace par un **journal des révisions** ; le jalon M1 ne fait qu'**écrire** : aucune
lecture, aucune route, aucun outil, aucun texte servi. On mesure le volume deux semaines
avant d'y brancher quoi que ce soit. La lecture arrive en M3 (plus bas).

- **Table** `datastore_row_revisions` (`db/schema/datastore.py`, `CREATE TABLE IF NOT
  EXISTS` au boot et par la révision Alembic `0011_journal_revisions_ligne`) : `id`,
  `ns_id`, `row_id`, `rev`, `diff jsonb`, `acteur`, `run_id`, `source`, `geste_id`, `at`,
  et depuis la révision `0016_journal_suppression` `suppression` (plus bas).
  Index `(ns_id, row_id, rev)`, NON unique : une ligne supprimée puis recréée sous le
  même id repart de `rev` 0.
- **Écrite par PostgreSQL**, comme `rev` et pour la même raison (bleu/vert sur base
  partagée) : deux déclencheurs `AFTER` sur `datastore_rows` (`db/journal_revisions.py`),
  `datastore_rows_30_journal_insert` et `datastore_rows_30_journal_update … WHEN
  (OLD.data IS DISTINCT FROM NEW.data)`. `AFTER` parce qu'il lit la `rev` déjà avancée et
  couvre l'INSERT ; la condition sur `data` seule fait qu'un mouvement de bail
  (`claimed_*`), `claims`, `updated_at` ou `embed_dirty` n'appelle même pas la fonction.
- **Le diff** : `{champ: {"avant": v, "apres": v}}`, calculé clé par clé dans le
  déclencheur, valeurs ENTIÈRES (couches comprises). Un côté absent n'a pas sa clé :
  `{"apres": 1}` = ajouté, `{"avant": 1}` = retiré, `{"avant": null, "apres": 2}` = une
  valeur `null` remplacée. Une insertion porte tous ses champs en `apres`. Une écriture
  sans effet n'écrit rien. Les **valeurs sont stockées**, par décision d'oto#273.
- **Ce qui reste, ce qui part** : pas de FK vers la ligne, donc les révisions d'une ligne
  supprimée RESTENT, et la suppression en ajoute une (plus bas). FK `ON DELETE CASCADE`
  vers `user_datastores` : un tableau supprimé emporte son historique, quel que soit le
  chemin qui le supprime. ⚠️ `data_drop_column`
  écrit UNE révision par ligne qui portait la colonne, valeur retirée comprise.
- **Estampille** (acteur, run, source, geste) : posée par le serveur depuis M2, voir
  la section suivante. La fonction lit `oto.acteur`, `oto.run_id`, `oto.source`,
  `oto.geste_id` (`current_setting(…, true)`, `''` lu comme `NULL`).
- **Interrupteur** `OTO_JOURNAL_REVISIONS=off` (défaut `on`) : le pool ouvre ses
  connexions avec `-c oto.journal_revisions=off`, lu par la fonction. Il coupe les
  écritures de CE processus seulement (la fonction est commune à la prod et à la préprod
  sur la base partagée) ; il se change par l'environnement et un redémarrage, sans
  redéployer. Toute autre valeur LÈVE à l'ouverture du pool.

**Mesure du volume** (deux semaines, par jour et par tableau ; `octets` = taille des
lignes du journal, hors en-têtes de tuple et index) :

```sql
SELECT date_trunc('day', r.at)::date AS jour, r.ns_id,
       count(*)                      AS revisions,
       sum(pg_column_size(r.*))      AS octets,
       max(pg_column_size(r.diff))   AS diff_max
  FROM datastore_row_revisions r
 WHERE r.at >= now() - interval '14 days'
 GROUP BY 1, 2
 ORDER BY 1 DESC, revisions DESC;

-- L'emprise réelle, index et TOAST compris :
SELECT pg_size_pretty(pg_total_relation_size('datastore_row_revisions'));
```

Aucun index sur `at` : la mesure parcourt la table, ce qui est le bon prix pour deux
semaines de données et une requête à la main.

**Banc** : `tests/datastore/test_journal_revisions_273.py` (base réelle).

### L'estampille — M2 (oto#273, 24/09/2026)

Chaque révision dit **qui** a écrit, **par quelle face**, **dans quel geste** et **sous
quel run**. Aucune lecture en M2 : ni route, ni outil, ni texte servi.

**Le point de passage** : `db.estampille.ecriture_de_lignes()`. Toute écriture de
`datastore_rows.data` du serveur ouvre sa transaction là, et nulle part ailleurs : une
connexion, une transaction explicite (même sur une connexion en autocommit), puis
`set_config('oto.*', …, true)` — l'équivalent de `SET LOCAL`, la valeur meurt avec la
transaction et ne suit pas la connexion dans le pool. Écrivains aujourd'hui :
`datastore_insert_row`, `datastore_merge_row_locked`,
`datastore_capturer_origine`, `datastore_drop_column`, `datastore_merge_key_duplicates`,
`datastore_delete_row` (`db/datastore.py`) et `abandonner_les_lignes_a_bout`
(`db/rowabandon.py`). `tests/datastore/test_estampille_273.py` parcourt l'AST du paquet :
une requête qui écrit `data` ou supprime une ligne hors de ce point, ou un écrivain
nouveau non nommé, fait échouer le banc.
Coût : un aller-retour (`SELECT set_config(…)` ×4) par transaction d'écriture.

**Le contexte** : `oto_mcp/geste.py`, une `ContextVar` par requête (même modèle que les
axes d'appel de `session_org`), posée à l'entrée de chaque face et remise à zéro à la
sortie. Le run n'y est pas : c'est l'axe `session_org.current_call_or_session_run()`, lu
au moment d'écrire (`_run_id=`, sinon le run actif de la session en MCP — #830 —,
`X-Oto-Run` en REST).

| face | posé par | `source` | `acteur` | `geste_id` |
|---|---|---|---|---|
| outil MCP (`data_*`, apps comprises) | `calllog.ToolCallLogger` | `agent` | sub de l'appelant | `call_uid` de la ligne `tool_calls` |
| REST, session interactive (JWT) — la console | `capabilities._rest_adapter` | `console` | sub du porteur | neuf, versé à `tool_calls.call_uid` |
| REST, jeton `oto_` (utilisateur ou délégation) | idem | `api` | sub du porteur | idem |
| REST, secret de worker (`otow_`) | idem | `system` | `worker_sub` | idem |
| upload signé vers un tableau (NDJSON, CSV) | `api.uploads` | `upload` | sub scellé au jeton | `jti` du jeton |
| `donnees_d_origine=true` (toute face, upload compris : scellé au jeton au mint) | le store, `@geste.import_si_donnees_d_origine` | `import` | inchangé | inchangé |
| abandon d'une ligne à bout de reprises | `db/rowabandon.py` | `system` | `service:file-de-travail` | celui de l'appel qui l'a déclenché |
| boucle des formules | `formula_backfill_worker` | `system` | `service:formules` | neuf, par tour |
| maintenance `key-indexes` (fusion des doublons) | `maintenance.key_indexes` | `system` | `service:maintenance` | neuf, par tableau |
| aucun contexte connu | — | `system` | `NULL` | neuf, par transaction |
| **hors serveur** : SQL à la main, migration, ancien code pendant une bascule | — | `NULL` | `NULL` | `NULL` |

Règles, dans l'ordre où elles s'appliquent :

1. `import` si et seulement si l'appel DÉCLARE `donnees_d_origine=true`, quelle que
   soit la face : il apporte la donnée telle qu'elle a été remise. Pour un upload signé,
   la déclaration est faite au mint (`oto_upload_url … donnees_d_origine=true`) et
   scellée dans le jeton. C'est ce qui permettra à M4 de projeter l'origine sur la
   première révision `import` sans y compter un fichier quelconque. L'acteur et le geste
   restent ceux de l'appel.
2. Sinon, la **face** : MCP = `agent` (le serveur ne distingue pas un agent d'un clic
   humain dans une app MCP, `data_review_decide` compris) ; REST = `console` pour une
   session interactive, `api` pour un jeton, `system` pour un worker de plateforme ;
   upload signé non déclaré = `upload`.
3. Une écriture que le **serveur** décide seul (abandon, formules, maintenance) est
   `system`, avec un acteur `service:<nom>` — jamais le sub de l'appel qui l'a
   déclenchée.
4. Sans contexte : `system` et acteur `NULL` — l'écriture dit qu'on ne sait pas qui,
   elle ne s'invente pas d'auteur. `NULL` en `source` est réservé à ce qui n'est pas
   passé par le serveur.

L'acteur est toujours le **porteur réel** : sous un « voir en tant que » en écriture,
c'est l'opérateur, pas la cible (même règle que `tool_calls.sub`). Un geste = un appel
d'outil ou une requête REST : toutes les lignes d'un lot le partagent, deux appels en
ont deux. Le geste relie une révision à SA ligne `tool_calls` (`call_uid`), en MCP comme
en REST.

Pour lire l'estampille à la main :

```sql
SELECT r.at, r.row_id, r.rev, r.source, r.acteur, r.run_id, t.tool
  FROM datastore_row_revisions r
  LEFT JOIN tool_calls t ON t.call_uid = r.geste_id
 WHERE r.ns_id = $1
 ORDER BY r.id DESC LIMIT 50;
```

**Banc** : `tests/datastore/test_estampille_273.py` (base réelle, chaque face par son
vrai chemin).

### La lecture — M3 (oto#273, 24/09/2026)

**L'historique d'une ligne** : `GET /api/datastores/{datastore}/rows/{row_id}/history`, et
sur la face agent `data_row_history` — une seule capacité (`me.datastore.row_history`,
`capabilities/datastore/history.py`). La lecture du journal vit dans `db/historique.py`,
seul module à lire la table (`journal_revisions.py` est importé par `_conn.py`, il ne peut
pas ouvrir de connexion).

- **Réponse** : `{datastore, ns_id, row_id, row_deleted, champ, coverage, revisions,
  next_before_id}` ; chaque révision porte `id, rev, at, acteur, run_id, source,
  geste_id, diff`. Plus récente d'abord, par `id` décroissant (pas par `rev` : une ligne
  supprimée puis recréée repart de `rev` 0).
- **Paramètres** : `champ=<colonne>` ne garde que les révisions qui touchent cette colonne
  et ne rend que sa partie du diff (`champ` vide = 400) ; `limit` (50 par défaut, 200 au
  plus) ; `before_id` = le `next_before_id` de la page précédente.
- **Accès** : celui de la lecture de la ligne (le store résout le tableau : org active,
  ownership, périmètre d'un endpoint partagé ; hors périmètre = 404). **Une ligne
  supprimée** garde son historique, lisible par qui GOUVERNE le tableau
  (`ownership.can_govern`), avec `row_deleted: true` et `deletion` (plus bas). Un simple
  lecteur reçoit `404 row_not_found`, comme sur un identifiant inconnu : lui servir les
  valeurs d'une ligne que le propriétaire a retirée n'est pas son droit, et lui dire
  qu'elle a existé divulguerait un fait qu'il ne peut plus lire ailleurs. Sans aucune
  révision, même le gouverneur reçoit un 404.
- **Face agent** : les colonnes `agent_access: "none"` sortent des diffs ; une révision
  qui ne touchait qu'elles disparaît de la page.
- **Couverture** (`coverage`) : `journal_since` = `historique.MISE_EN_SERVICE`
  (`2026-09-24`, une CONSTANTE : commit M1 sur le tronc à 07:42Z, prod `v1.341.0` à
  08:26Z, la préprod partageant la base a pu poser les déclencheurs entre les deux ; pas
  `min(at)`, que la rétention déplacera), `insert_recorded` (une `rev` 0 est dans le
  journal ; faux = la ligne existait avant, son historique commence en cours de vie),
  `first_revision_at`, `total_revisions`, et `note`, la phrase servie : une ligne sans
  révision n'est PAS une ligne jamais modifiée, et `OTO_JOURNAL_REVISIONS=off` laisse
  des trous. La description de l'outil le dit aussi.

**Le parcours d'une ligne lit les valeurs** (`GET …/rows/{row_id}/activity`,
`capabilities/datastore/activity.py`). Les entrées gagnent quatre champs, aucun ne part :
`geste_id` (le `call_uid` de l'appel), `source`, `acteur` et `revisions` (la forme de
`/history`). Chaque appel qui a écrit la ligne porte ses révisions, rattachées par le
geste ; une écriture qu'aucun appel ne porte (upload signé, formules, maintenance, SQL à
la main) devient une entrée `kind='revision'`, `tool: null`. `tool_calls` reste la
source des lectures, réservations et refus. Pour que la face REST se rattache,
`calllog.log_rest_call` verse désormais le geste en `call_uid` sur la ligne de SENS
(`data_*`), comme la ligne de route le faisait déjà. ⚠️ Les révisions écrites avant
l'estampille (M1, `geste_id` NULL) ne se rattachent à rien : elles s'affichent en
`kind='revision'` à côté de l'appel qui les a faites. L'activité d'un TABLEAU
(`…/activity`) ne lit pas le journal : ses entrées portent les nouveaux champs vides.

**Banc** : `tests/datastore/test_historique_ligne_273.py` (base réelle, vraies routes).

### La suppression est une révision (oto#273, 24/09/2026)

Jusqu'ici, supprimer une ligne ne laissait rien au journal : ses révisions restaient,
mais rien ne disait qu'elle était partie, ni qui l'avait supprimée, ni avec quelles
valeurs.

- **La forme** : une révision `suppression = true`, `diff` = tous les champs de la ligne
  en `avant` (`{"a": {"avant": 1}, "note": {"avant": null}}`, `{}` pour une ligne vide),
  `rev` = celle de la ligne au moment où elle part, l'estampille de qui l'a supprimée.
  Une COLONNE plutôt qu'une convention dans `diff` : une mise à jour qui retire tous les
  champs a le même diff, et une clé réservée dans `diff` pourrait être le nom d'une
  colonne. `rev` 0 porté par une suppression ne compte pas pour une insertion
  (`coverage.insert_recorded`).
- **Écrite par PostgreSQL** : `datastore_rows_30_journal_delete`, `AFTER DELETE FOR EACH
  ROW`, sur sa PROPRE fonction `datastore_journal_suppression()`. Pas dans
  `datastore_journal_revision()` : celle-là est reposée à chaque démarrage par toute
  version servie sur la base partagée, et une version d'avant ne sait pas traiter
  `DELETE` — ses écritures lèveraient, et chaque suppression de ligne avec. Même
  interrupteur (`OTO_JOURNAL_REVISIONS=off`), même refus silencieux et signalé
  (`WARNING`) d'un `data` qui n'est pas un objet. Estampillée : `datastore_delete_row`
  passe désormais par `ecriture_de_lignes()`, comme la fusion des doublons.
- **Un tableau supprimé** : la cascade depuis `user_datastores` supprime ses lignes et
  son historique. Le déclencheur de suppression de chaque ligne s'exécute alors que le
  tableau n'existe déjà plus : il le constate (`NOT EXISTS … user_datastores`) et
  n'écrit rien. Sans cette garde, la révision violerait la clé étrangère et la
  suppression du tableau échouerait. L'historique d'un tableau supprimé part avec lui :
  c'est voulu.
- **La lecture** : chaque révision de `/history` porte `suppression`. La réponse gagne
  `deletion` (`id, at, acteur, run_id, source, geste_id`) : la révision de suppression
  quand la ligne n'existe plus et que sa révision la plus récente en est une. Elle n'est
  jamais masquée à la face agent, même vidée de ses colonnes cachées. `row_deleted` reste
  lu sur le TABLEAU : le journal peut avoir des trous (`off`), et une ligne recréée
  pendant un trou existe malgré la suppression qui la précède au journal. Une ligne
  supprimée avant le déploiement est `row_deleted: true`, `deletion: null`.
- **Colonne** : révision Alembic `0016_journal_suppression`, ET démarrage (sous garde de
  catalogue, avant la fonction qui l'écrit) ; ordre indifférent
  (`docs/migrations-versionnees.md`).
- **Ce que le parcours d'une ligne en montre** : rien de plus, puisqu'il exige une ligne
  vivante ; une ligne recréée sous le même `_id` y montre la suppression parmi ses
  révisions.

**Banc** : `tests/datastore/test_journal_revisions_273.py` (suppression, ligne vide,
recréation, cascade par le code et à la main, `data` non objet, révision `0016`) et
`tests/datastore/test_historique_ligne_273.py` (`deletion`, ligne recréée, ligne
supprimée sans révision).

### La rétention — 90 jours, sauf l'origine (oto#273, décision du 23/09/2026)

Les révisions de plus de `OTO_JOURNAL_REVISIONS_RETENTION_DAYS` jours (**90** par défaut)
sont purgées, **sauf celles de `source = 'import'` d'une ligne qui existe encore** :
c'est l'origine de la donnée (M4 la projettera). Une ligne supprimée rend son import à la
règle commune ; supprimée puis recréée sous le même `_id`, aussi (la révision de
suppression, plus récente, le dit). Les révisions de suppression suivent la règle
commune.

- **Qui** : `oto-mcp maintenance revisions` (`maintenance.revisions`), dans `all`, donc
  dans le timer quotidien `oto-mcp-maintenance.timer` (03:20, prod seulement : la base
  est partagée). Un travail à part de `retention` : chacun son réglage, et un réglage
  illisible ici ne prive pas les runs de leur purge (fail-open par travail).
- **Le réglage** est lu à CHAQUE tir (`journal_revisions.retention_jours`) : le changer
  ne demande ni déploiement ni redémarrage. Illisible (pas un entier, ou moins d'un
  jour), le travail LÈVE — une ligne `ÉCHEC` au journal du timer, jamais un repli
  silencieux sur 90, ni un 0 qui purgerait tout.
- **Comment** (`db/retention_revisions.py`) : par lots de 1 000 révisions, chacun sa
  transaction, en avançant sur la clé primaire (`id > curseur ORDER BY id LIMIT 1000`),
  une requête par lot (sélection, prédicat, `DELETE … USING`). La table n'a pas d'index
  sur `at` : filtrer sur `at` seul la parcourrait entière à chaque lot. `id` est
  monotone et suit `at` à la durée d'une transaction près, donc la purge s'arrête au
  premier lot sans AUCUNE révision assez vieille : elle ne lit que la zone ancienne, plus
  un lot. Plafond de 1 000 lots par passage (`complet: false` : le suivant reprend). Les
  imports gardés sont relus à chaque passage ; leur nombre est celui des lignes importées
  vivantes. Aucun verrou long : des verrous de ligne sur 1 000 révisions au plus, que
  personne d'autre ne modifie.
- **À blanc** : `oto-mcp maintenance revisions --dry-run` compte ce qui partirait (un
  parcours de la table, pour une lecture à la main).

Le prédicat, pour une révision `r` :

```sql
r.at < now() - make_interval(days => 90) AND (
    r.source IS DISTINCT FROM 'import'
    OR NOT EXISTS (SELECT 1 FROM datastore_rows l
                    WHERE l.ns_id = r.ns_id AND l.row_id = r.row_id)
    OR EXISTS (SELECT 1 FROM datastore_row_revisions s
                WHERE s.ns_id = r.ns_id AND s.row_id = r.row_id
                  AND s.suppression AND s.id > r.id))
```

**Effacement d'une personne** : l'ADR 0062-D2 le décide, mais le dépôt n'a pas encore de
chemin qui l'exécute (aucun geste de produit ne supprime un compte ; le seul `DELETE FROM
users` est la fusion de comptes de `db.migrate_sub`, qui ne touche aucun tableau). Le
jour où il existera et supprimera des tableaux ou des lignes, la cascade de
`user_datastores` emportera leur historique ; s'il efface des lignes une à une, chacune
laissera une révision de suppression portant ses valeurs, qu'il faudra purger avec elles.

**Banc** : `tests/datastore/test_retention_revisions_273.py` (base réelle).

## Un tableau personnel retient l'org où il a été créé (oto#160, 24/09/2026)

`user_datastores.context_org_id` : l'org active de l'appel qui crée un tableau
PERSONNEL, même sens que `projects.context_org_id`. NULL pour un tableau d'org ou
d'équipe (son contexte se dérive du propriétaire) et pour un personnel créé avant la
colonne. Remplie par `db.create_datastore(…, context_org_id=…)`, que passent les deux
voies de création du code : le store (`data_create_datastore` et `POST /api/datastores`,
org lue par `_org_de_l_appel`, la même source que `_active_scope`) et le vivier provisionné
par la copie d'un projet (l'org où la copie est rangée). L'écriture à clé qui créait son
tableau à la volée (`upsert_row`) est retirée depuis oto#156. Une garde d'AST (`tests/datastore/test_contexte_org_160.py`)
refuse une voie qui l'omettrait.

> ⚠️ **Resserré le 07/10/2026** (`docs/ownership.md` « Une liste s'en tient à l'org
> consultée ») : un tableau perso se liste, pour son propriétaire, dans son org de création
> et **nulle part ailleurs** — l'org perso ne rend plus ceux créés dans une autre org, seulement
> ceux sans `context_org_id` et ceux partagés à moi en personne.

> ⚠️ **Remplacé le 28/09/2026 (ADR 0030 §9, `docs/ownership.md` « Dans une org, on ne
> voit QUE l'org »)** : un tableau personnel ne se liste plus dans son org de création
> mais dans l'org PERSO de son propriétaire, quelle que soit cette org ; hors de l'org
> perso, `list_datastores` et la recherche ne rendent ni tableau personnel ni partage fait
> à une personne. `ownership.tableaux_du_contexte` est retiré ; `context_org_id` reste
> écrit, plus lu. Sans `owner`, un tableau reste à la PERSONNE depuis n'importe quelle
> org (29/09/2026) et se liste dans l'org perso. **Amendé le 29/09/2026** (une org perso
> est une org comme une autre) : mon tableau perso se liste aussi, pour moi seul, dans
> l'org où je l'ai créé (`ownership.mes_tableaux_ici`, `context_org_id` est de nouveau
> lu) ; « partagés avec moi » (`GET /api/me/datastores/shared`) est servi dans toute org.
> Bancs : `tests/test_listes_seule_l_org.py`, `tests/test_org_perso_est_une_org.py`.
> Le paragraphe suivant décrit la règle d'avant.

**La liste, depuis la phase 2 (25/09/2026)** : `list_datastores` — donc
`GET /api/datastores`, `data_list_datastores`, l'index de `data_app` et la vérification
des tableaux d'un jeton porté — et la recherche (`search._accessible_namespaces`, en
parité) passent le jeu possédé par `ownership.tableaux_du_contexte` : un tableau
personnel de l'appelant n'y figure que si `context_org_id` = l'org active **ou** NULL.
Les tableaux d'org, d'équipe et reçus ne changent pas ; « partagés avec moi »
(`GET /api/me/datastores/shared`) non plus.

C'est un filtre de **liste**, pas un droit :
- `ownership.active_org_principals` n'a pas bougé — il sert aussi `visible_in_org` et
  les listes de projets et de pages ; le personnel y reste un principal de l'acteur ;
- l'accès ne change pas : `resolve_datastore_ns` résout un personnel de l'appelant, par
  son **numéro** comme par son nom, depuis n'importe quelle org, et `can_access` le lui
  ouvre en propriétaire. Un tableau créé dans A s'ouvre donc depuis B ; il n'est
  seulement plus listé dans B ;
- `oto_resource op=list` (plan de gouvernance, toutes les orgs de l'acteur) ne filtre pas.

Banc (retiré le 28/09/2026 avec la règle) : `tests/datastore/test_liste_par_org_160.py`.

**Les personnels d'avant la colonne** : la révision `0018_contexte_org_rempli` remplit
ceux qu'une trace désigne sans ambiguïté — le journal des appels (la création, même
compte, même nom à ±2 min en MCP, ±10 s en REST), sinon les projets qui lient le
tableau. Jamais « l'org dont le propriétaire est l'unique membre ». Les autres gardent
NULL (mesuré le 24/09 : 15 sur 36). Détail : `docs/migrations-versionnees.md` §5.1.

## Toute colonne déclarée est servie, à `null` sans valeur (oto#182, 13/09/2026)

**Le constat.** Dans une ligne JSONB stockée, une colonne jamais écrite n'existe pas : elle était
donc ABSENTE du document servi. Pour un agent, ce sont deux lignes différentes — clé absente, il
fabrique une valeur (un domaine composé depuis la raison sociale) ; `null`, il la cherche.

**Le contrat.** `data_rows`, `data_claim_next` et leurs faces REST (`GET …/rows`, `…/rows/{id}`,
`POST …/claim_next`) servent chaque colonne DÉCLARÉE au premier niveau du schéma, à `null` quand
aucune valeur n'est en place. La complétion vit dans `DatastorePg._row_to_dict`, le point par
lequel passe toute ligne servie par le store — les réponses d'écriture et `claim_row`/`queue` la
reçoivent aussi.

- **Rien n'est écrit** : le stockage ne change pas, une valeur présente (`""`, `null`, une couche
  seule) est servie telle quelle.
- **Masquage agent** : une colonne `agent_access: "none"` reste absente.
- **Ordre** : celui du schéma (`declaration.cles_declarees`).
- **Pas en profondeur** : un sous-champ jamais écrit d'un élément de liste reste absent ; une
  colonne `list`/`object` jamais écrite vaut `null`, pas `[]` ni `{}`.
- **Projection** : `fields` garde une colonne déclarée demandée, donc à `null` ; un nom non
  déclaré et absent reste omis.
- **Hors lot** : `oto_node_rows` (sert `""`) et la page publique.

⚠️ **`null` veut dire « aucune valeur en place », pas « jamais écrit ».** Un `null` écrit avant
la fin du `null` et une case ne portant qu'une couche se lisent pareil.

**Un `null` qui n'efface rien ne s'écrit pas** (`columns.sans_les_nulls_sans_effet`, validé par
Alexis le 13/09). Sans ce filtre, relire une ligne complétée puis la réémettre — le geste d'un agent
qui lit, modifie, réécrit — ajoutait une clé, faisait tourner la révision et déclenchait le préavis
de `null` (le refus à partir du 01/12/2026) ; cinq bancs d'aller-retour rougissaient. Désormais, sur
les trois chemins d'écriture (création et fusion, mise à jour par id, lots) :

- un `null` sur une colonne DÉCLARÉE SANS valeur en place (absente, `null` stocké, `""`, couche
  seule — mais pas un vide ASSUMÉ, qu'il efface depuis le J2 d'oto#140) n'est pas écrit et ne compte pas pour le préavis ; une colonne HORS schéma écrite à `null`
  garde son comportement (elle naît, le relevé la nomme, ou le tableau la refuse) — un écho ne peut
  viser que le déclaré, et filtrer l'inconnu ferait taire une faute de frappe ; en couches, ce qui l'accompagne reste
  (`{"valeur": null, "comment": …}` → `{"comment": …}`) ;
- un `null` sur une valeur EN PLACE reste l'effacement nommé, préavis puis refus, inchangé ;
- la ligne en place n'est lue que si l'écriture porte un `null` (sous le verrou pour la mise à jour
  par id ; par la clé métier, avant la fusion, pour la création et les lots).

⚠️ **Limite actée : un CSV ne sait pas dire `null`.** L'export du tableau de bord écrit une cellule
vide pour `null` (`lib/csv.ts`), et l'import écrit `""` là où il n'y avait rien (#608). Après un
tour CSV, une colonne déclarée jamais écrite revient donc `""` — elle existe alors en base, et la
révision tourne. Banc : `test_un_CSV_d_export_se_reimporte_et_referme_l_aller_retour`.

## Le vide ASSUMÉ — étape 1 : lu, validé, servi, jamais émis (oto#204, 13/09/2026)

**Le besoin.** Un sous-champ requis doit pouvoir porter « aucune source ne donne ce titre »
sans que l'agent invente la valeur. Une chaîne vide ordinaire et une cellule CSV vide ne le
disent pas : elles restent refusées (arbitrage du plan, option 2 ; aucune migration des
anciennes chaînes vides).

**La représentation.** Un marqueur interne rangé dans l'enveloppe de la cellule :
`{"valeur": "", "oto.vide_assume": true}` (`couches.VIDE_ASSUME`). Le POINT rend la collision
impossible : un nom de colonne ou de sous-champ ne peut pas en porter. Pas de colonne, pas de
DDL.

**Livrée en deux étapes, parce que préprod et prod partagent la base.** L'étape 1 lit, valide
et sert une cellule marquée, et n'en crée AUCUNE. L'étape 2 — l'émission par la résolution de
`@empty`, y compris à la création — ne touche `main` qu'une fois l'étape 1 servie en production :
une version qui ignore le marqueur ne doit jamais en lire un.

Ce que fait l'étape 1 :

- **servi `""` par défaut, `"@empty"` sur `empties=sentinel` (étape 2), jamais le marqueur** :
  MCP (`data_rows`, `data_claim_next`), REST `flat` et
  `nested` — donc l'export CSV du tableau de bord, bâti sur elles —, et la page publique d'un
  tableau partagé, qui lit la base brute (`share_ui._lisible`) ;
- **`required` satisfait** par le marqueur, en colonne comme en sous-champ de `list.of` ;
  `datastore_rows_missing_required` ne compte pas une cellule marquée ; le marqueur n'est pas un
  « sous-champ inconnu » ;
- **fusion** : renvoyer le `""` servi garde le marqueur ; écrire une vraie valeur le fait tomber,
  comme `comment` et `link` ;
- **refusé à l'écriture** : un client qui écrit la clé, à n'importe quelle profondeur, est refusé
  en nommant `@empty` (`columns.refuser_cles_internes`, sur tous les points d'entrée).

⚠️ **Limite : une liste SANS `of.key` se remplace en bloc** (#120). La réémettre telle que servie
fait du `""` d'un sous-champ marqué un vide ordinaire : l'écriture est REFUSÉE (« champ requis
manquant »), sans perte. Sur une liste à clé, la fusion élément par élément garde le marqueur.
Pour réécrire un vide assumé dans une liste sans clé, le geste est `@empty` — et l'étape 2 le
relit sous cette forme (`empties=sentinel`, section suivante).

Un `null` écrit sur une cellule marquée l'EFFACE, marqueur compris, comme sur toute autre case
(oto#140, J2) : le vide assumé est une valeur en place, et `columns.sans_les_nulls_sans_effet`
ne l'écarte plus comme un `null` sans effet. C'est le geste qui remplace `@clear` (étape 2),
refusé (J3). Jusqu'au J2, ce `null` était écarté (oto#182) et `@clear`
seul démarquait. Bancs : `tests/datastore/test_vide_est_une_valeur_j2.py`.

Bancs : `tests/datastore/test_vide_assume_lecture_204.py` et `…_204_live.py`.

## Le vide ASSUMÉ — étape 2 : `@empty` l'émet, `empties=sentinel` le relit (oto#204, 13/09/2026)

**Le défaut fermé.** Une liste sans `of.key` se remplace en bloc (#120). Relue au défaut puis
renvoyée, une case au vide assumé revenait `""`, un vide ORDINAIRE. Sur un requis, l'écriture
était refusée — sans perte, mais sans issue honnête : les deux vides se servaient pareil, donc le
client inventait une valeur, ou posait `@empty` sur un vide que personne n'avait cherché. Sur un
non requis, le marqueur tombait sans un mot. Arbitrage du plan (option 2) : la lecture EXPOSE
l'intention sous la forme qui la réécrit. Ni identité inventée par le rang, ni `required`
assoupli, ni obligation de clé.

⚠️ **Ce lot ÉMET le marqueur** : il ne touche `main` qu'une fois l'étape 1 servie en production,
préprod et prod partageant la base.

### Le mot

| mot | sur la VALEUR d'une case | sur une COUCHE (`comment`, `link`) |
|---|---|---|
| `@empty` | vide ASSUMÉ : valeur `""`, marqueur posé — satisfait `required` | vide la couche seule |

`@keep` et `@clear` (étape 2 d'origine : « tient ce qui est en place », « efface sans
assumer ») sont **retirés** (oto#140 J3) : refusés à l'entrée de toute écriture par
`mots_deprecies.controler`, ils n'atteignent plus la fusion, et leur résolution a été retirée
le 06/10/2026. La preuve que tout chemin qui fusionne passe d'abord par `controler` est une
garde (`tests/datastore/test_controler_garde_toutes_les_ecritures.py`) : un nouveau point
d'entrée qui fusionnerait sans lui la fait tomber — sinon `"@keep"` serait stocké comme du
texte.

`""` garde son comportement : le `""` servi, renvoyé sur une case marquée, ne change rien ; sur
une valeur en place il la remplace (oto#140 J2).

**Un résolveur** : `couches.est_vide_delibere`, lu par la fusion (`_merge_column`,
`_merge_items`, `_sentinelles_dans_les_items`) et par la création
(`columns.mots_resolus_a_la_creation`, appelée par `append_row` sans clé métier et la ligne
neuve d'un lot, avant la capture d'origine et la validation).

Couches et origine suivent les règles existantes (bancs `test_4a…`, `test_4b…`, `test_4c…`) :

- `{"valeur": "@empty", "comment": …}` sur une case DÉJÀ vide : la valeur ne change pas, donc un
  `link` non envoyé reste ; sur une valeur en place, `comment` et `link` non envoyés tombent et
  `origine` survit ;
- un mot dans une couche ne vide que cette couche : ni la valeur ni le marqueur ne bougent ;
- une `origine` renvoyée inchangée ne change rien au geste.

**Deux trous fermés.** À la création, `"@empty"` partait en base comme texte et satisfaisait
`required` (#183). Dans une liste à clé, un élément NOUVEAU entrait tel quel (`_merge_items`),
mot compris : il se résout désormais comme une liste sans identité.

### Où un mot se pose

Sur une CASE : une colonne, ou l'attribut d'une fiche dans une liste, chacune avec ses couches.
Ailleurs, refus `row_invalid` qui nomme le chemin, rien n'est écrit
(`columns.refuser_les_mots_mal_places`, sur les cinq points d'entrée) :

- l'identité d'un élément (`of.key`) — pas de `@empty` ;
- un élément d'une liste de valeurs (`tags[1]`) ;
- un sous-champ d'objet, le contenu d'une colonne `json`, le contenu d'un attribut.

### La lecture `empties`

`plain` (défaut) sert le vide assumé `""` ; `sentinel` le sert `"@empty"`, et un `""` ordinaire
reste `""`. À plat : `"fonction": "@empty"`, couches à côté. Imbriqué :
`{"valeur": "@empty", …couches}`, même sans autre couche, au premier niveau comme dans un élément.
Surfaces : REST `GET …/rows`, `GET …/rows/{id}`, `POST …/claim_next`, `POST …/rows/{id}/claim` ;
MCP `data_rows`, `data_claim_next`. Une valeur inconnue rend `400 invalid_empties` (MCP :
`INVALID_PARAMS`), qui nomme `plain` et `sentinel`. Cliquet de parité : toute lecture qui accepte
`layers` accepte `empties` — dans le source, dans l'OpenAPI servi et dans le schéma MCP.

⚠️ Une face ne passe `empties` au store que s'il vaut `sentinel` (patron `claim._lease`) : le
défaut appartient au store, et deux doublures de banc figent la signature d'avant.

Inchangés : le défaut, la page publique d'un tableau partagé (base brute sans clé interne),
l'export CSV du tableau de bord (bâti sur le défaut), les réponses d'écriture.

Le geste qui réécrit une liste : lire en `sentinel`, renvoyer chaque élément tel qu'il est venu.
Réordonnée, éléments identiques, élément retiré : le mot voyage dans l'élément, aucune identité
n'est inventée.

### Refus et relevés

- **Requis** : `row_invalid`, dont le message nomme le chemin (`contacts[0].fonction`), `@empty`
  et la relecture `empties=sentinel`. Atomique : base et `_revision` intactes.
- **Relecture périmée** : `expected_revision` rend `409 revision_conflict`, rien n'est écrit.
- **Relique pointée** : `@empty` est un effacement comme `null` (`reliques._efface`) ; sur une
  relique qui porte une valeur, refusé.
- **`valeurs_effacees`** : `@empty` sur une valeur en place la nomme, comme `null`.
  Les valeurs rendues le sont sans clé interne.
- **Vide assumé non requis rendu ordinaire** : `couches_effacees`, couche `@empty`
  (`columns.vides_assumes_perdus`), jugé après la fusion, par attribut et par COMPTE — un
  marqueur de moins ET un vide ordinaire de plus. Pas relevés, délibérément : une vraie valeur,
  un élément retiré, une liste réordonnée renvoyée en
  `sentinel`. Le chemin est celui de la liste ÉCRITE.

OpenAPI : `invalid_layers` et `invalid_empties` déclarés sur les quatre lectures.
`row_invalid` et `row_locked` le sont depuis oto#217 sur `PATCH …/rows/{id}` (et `row_locked`
sur `DELETE`) : le cliquet d'atteignabilité ne voyait que les `raise AuthzDenied(…)`, quand
`_write_refusal` et `ns_not_found` RETOURNENT le refus que l'appelant lève
(`raise _write_refusal(e)`) — c'est l'idiome du dépôt, et il rendait ces refus indéclarables.
`tests/_refus_atteignables.py` relève désormais aussi le refus FABRIQUÉ, avec son cas au banc
factice ; les cinq bancs qui figent ce retour restent vrais, la fonction rend toujours.
⚠️ **`POST …/rows` (ajout) ne les déclare toujours pas** : le lot #217 s'est tenu à l'écriture
par id, la suppression, la libération et les deux réservations — l'ajout attend son rejeu.

Bancs : `tests/datastore/test_vide_assume_reemission_204.py` et `…_204_live.py`.


## Les exigences en profondeur — J4 du contrat d'écriture (oto#137, 24/09/2026)

**Le défaut.** `validation_active` cherchait `required`/`required_when` au seul premier
niveau (par crainte d'armer rétroactivement des schémas posés), `max_length` en profondeur,
et ni `options` ni `max_items` nulle part. Un schéma dont la seule exigence vivait dans un
élément de liste était accepté, validation éteinte : zéro erreur sur un contact sans son
`nom` déclaré requis.

**L'armement** (`declaration.validation_active`, `_exige`) : `unknown_columns` autre
que `"create"` (ex-`strict`, oto#127), ou `required`,
`required_when`, `max_length`, `pattern` (depuis oto#103), `max_items` à toute profondeur
(`_walk_fields`), ou
`options` dans un sous-record. ⚠️ `options` de premier niveau n'arme pas seule : elle relève
de la validation complète (`validation_complete`, oto#124), en vigueur sur tout tableau à
colonnes depuis le 21/10/2026. Mesuré en production avant la bascule : un
seul tableau change de régime (34 lignes, les `options` de deux listes) et aucune ligne
existante ne viole un `required` de sous-champ. Livré sans préavis.

**Les formes inertes, refusées à la pose** (`definition._validate_fields_def`) :
`max_items` dans `of` (il borne la liste, se pose à côté de `type: "list"`), et `fields`/`of`
sous une colonne sans `type` — la validation ne descend que dans un composite typé. Le refus
donne la forme correcte. Aucun schéma de production ne porte ces formes (mesuré le 24/09).

**Liste à `of.key` : seuls les éléments ÉCRITS sont jugés** (`validation.elements_reecrits`).
Les chemins qui fusionnent (`_merge_into_row`, `update_row`) passent la ligne en place
(`en_place`) jusqu'à `validate_row` ; la fusion par créneau a déjà eu lieu, et un élément qui en
sort identique à celui de même identité en place n'est pas écrit : ce qui y cloche part dans
`gelees` (`hors_type` de la réponse), comme une colonne non écrite. Un élément neuf, sans
identité ou modifié est jugé. Sans `of.key`, sans ligne en place, ou sur une liste en place à
identité en double (remplacée en bloc par la fusion), tout est jugé.
⚠️ Hors de portée : `required_layers` (`couches_exigees.py`, armé par sa propre
déclaration) juge encore tous les éléments d'une liste réécrite.

Bancs : `tests/datastore/test_exigences_en_profondeur_oto137.py`.

## La charge à renvoyer — un refus donne la forme qui marche (oto#135, 24/09/2026)

Un refus de validation porte `details["a_renvoyer"]` : un fragment de `row` calculé depuis
la DÉCLARATION (`charge_a_renvoyer.py`), jamais depuis la donnée. Seuls les champs à
corriger y figurent, chacun marqué d'un gabarit (`phrases_de_refus.gabarit` : `"<text>"`,
`"<number>"`, `"<a | b>"`, borne et motif compris ; un objet rend ses sous-champs requis ;
une couche, ce qu'elle porte — `gabarit_de_couche`). `| @empty` s'ajoute sur un requis
manquant là où le geste est permis : colonne, attribut d'élément de liste — pas l'identité
`of.key`, pas un sous-champ d'objet, pas une cible de couche.

Dans une liste, l'élément fautif SEUL : son identité `of.key` recopiée (la seule donnée
qui l'est), sinon son rang, et `details["a_renvoyer_elements"]` nomme les éléments visés
(`contacts[role=DAF]`, `contacts[1]`). Jamais la liste entière.

**Le chemin est STRUCTURÉ.** Le validateur note au fil de sa descente un tuple de segments
(`("k", clé)`, `("e", rang, identité)`) et une feuille ; `rendre` assemble. Aucune phrase
n'est reparsée. Quatre familles notent : requis manquant (`_row_errors`), type ou format
(`_type_error`, borne, motif, et `types_trahis` sur un tableau souple), sous-champ inconnu
(attribut d'un composite fermé → la clé déclarée la plus proche, `cle_la_plus_proche` ;
couche mal orthographiée → la couche la plus proche), couche exigée (`couches_exigees`).
Rien n'est noté pour une colonne gelée ni pour un élément que le geste n'écrit pas ; ni pour
`max_items` (quels éléments retirer est le choix de l'agent).

**Les faces.** REST rend `details` tel quel (`_write_refusal`). MCP n'a pas d'enveloppe :
`data_write` attrape `RowValidationError` avant `ValueError` et finit le message par
`charge_a_renvoyer.clause` — la charge en JSON, et pour une liste la règle du renvoi (la
liste entière, l'élément corrigé à sa place : un élément omis est retiré ; ou chaque
attribut à son rang, `contacts[1].nom`, sans renvoyer la liste). Un lot garde
`details` en changeant de désignation (`lots.py`).

**Le refus de signature** (`error_taxonomy._arg_error_message`) nomme en plus le paramètre
le plus proche d'une clé inconnue — « Rejoue `data_write(…)` avec `rows=` à la place de
`rows_data` » — et ne dit plus « requis absent » un paramètre simplement mal écrit.
`ErrorEnvelopeMiddleware` lit les paramètres de l'outil que NOMME l'erreur
(`outil_de_signature`, derrière `oto_call` compris) dans le catalogue brut.

Bancs : `tests/datastore/test_charge_a_renvoyer_oto135.py`,
`tests/test_parametre_le_plus_proche_135.py`.

## L'écriture par rang dans une colonne-liste (oto#22 point c, 01/10/2026)

On écrit à l'adresse qu'on lit. `contacts[0].email` était refusé à l'écriture
(`_refuse_dotted_names`) alors que le filtre, le tri et l'agrégat le comprenaient : pour
corriger un email, il fallait reposer la liste entière, couches de tous les éléments
réémises. La spec, les refus et ce qui est écarté : `docs/datastore-colonne-tableau.md`
§5.2. Ici, comment c'est tenu.

**La grammaire est celle de la lecture** (`db/paths.split_list_path`), un cran de plus :
`contacts[0].email`, `contacts[0].email.comment`, `contacts[+]` (une fiche ajoutée),
`contacts[0]: null` (l'élément supprimé). `rangs.sortir_les_rangs` la lit sur CHAQUE
porte d'écriture, AVANT toute autre garde — laissée dans le payload, l'adresse tomberait
dans `_refuse_dotted_names` comme un nom pointé — et refuse en nommant la forme qui
aboutit (rang vide, forme imbriquée `contacts[0]: {…}`, identité `contacts[role=X]`,
attribut d'un ajout, colonne entière ET rang dans un geste, colonne déclarée non-liste).
Ce qui ressemble sans en être (`Prix [EUR]`, `note[a]`) reste un nom de colonne.

**Les gardes du payload jugent chaque élément à son VRAI rang** (`EcrituresParRang.
preparer`) : rangement des couches pointées (`points._ranger_une_fiche`), clés internes,
mots réservés (`columns.mots_dans_l_element`, l'identité `of.key` comprise), couches mal
orthographiées, dates (la normalisation du store, l'élément déclaré à son adresse). Une
liste reconstituée pour les faire tourner aurait nommé `contacts[0]` pour un geste sur
`contacts[3]` — et l'agent aurait corrigé le mauvais contact.

**La résolution se fait sous le verrou** (`appliquer`, appelée dans le `_apply` de
`_merge_into_row` et d'`update_row`, contre `{}` à la création) : les rangs désignent la
liste EN PLACE, avant le geste ; chaque attribut se fusionne dans son élément par
`_merge_column` (la règle de `_merge_items` pour un élément apparié) ; les suppressions
se font du plus grand rang au plus petit ; l'ajout en dernier ; sous `of.key`, une
identité qui deviendrait double se refuse. Le résultat est une colonne-liste COMPLÈTE,
qui suit l'arbitrage des vides, les champs réservés, la validation et le journal comme
toute colonne — d'où « même fusion, même journal » sans copie du chemin. Deux points :

- la boucle de fusion passe par `rangs.fusionner` : une colonne écrite par rang arrive
  déjà fusionnée, elle est REPOSÉE dans ses couches de colonne (`reposer_la_liste`, le
  même geste que la branche `of.key` de `_merge_column`), jamais refusionnée — sans
  `of.key` la liste serait remplacée en bloc, avec `of.key` un attribut effacé par le
  geste reviendrait de l'élément en place ;
- la validation reçoit `ecrits_par_rang` (`{colonne: rangs}` dans la liste résultante) :
  `_row_errors` et `couches_exigees` ne jugent que ces éléments, `of.key` ou non (J4 —
  sans `of.key`, `elements_reecrits` ne savait rien distinguer).

Une liste vidée par le geste efface la colonne (`None`) plutôt que de poser `[]` : jusqu'à
la bascule d'oto#140 J2, `[]` est un vide ÉCARTÉ, et supprimer le dernier contact aurait
été refusé « sans effet ». Le banc :
`tests/datastore/test_ecriture_par_rang_oto22_live.py` (store, REST, MCP ; patch, clé,
lot, création).

## Ajouter et retirer dans une liste sans la relire, compter ses éléments (oto#102, 04/10/2026)

**Le défaut.** Une colonne-liste ne s'écrivait qu'en entier, ou un élément à la fois par
`contacts[+]` : pour ajouter plusieurs entrées ou retirer une valeur, l'agent relisait,
recalculait et réécrivait, et deux écritures concurrentes perdaient un ajout sauf
précondition de révision — la garde était chez le consommateur, et facultative. Et une
liste de VALEURS ne se comptait pas par élément (`tags[]` n'était pas un chemin).

**Le contrat retenu : la grammaire du rang, deux signes de plus** (`datastore/rangs.py`).
`tags[+]` porte un élément ou une LISTE d'éléments (ajoutés dans l'ordre, doublons
gardés) ; `tags[-]` retire une valeur ou une liste de valeurs (toutes les occurrences ;
liste de valeurs seulement ; une valeur absente se refuse). Pas d'objet d'opération
(`{"@append": …}`) dans la valeur : `[+]` était déjà le verbe d'ajout servi sur toutes
les faces, en ouvrir un second aurait fait deux voies pour un geste. La résolution se
fait dans `EcrituresParRang.appliquer`, sous le `FOR UPDATE` de la ligne, contre la
liste en place : rangs, puis retraits, puis ajouts. Rien de neuf dans les chemins
d'écriture — patch par `id`, fusion par clé, lot, création, REST, MCP, dépôt NDJSON
héritent du geste parce qu'ils appellent déjà `sortir_les_rangs`/`appliquer`. Les éléments ajoutés passent la
validation (`ecrits` les nomme à leur rang dans la liste résultante, retraits compris) ;
le journal porte l'avant et l'après de la colonne entière (déclencheur, inchangé).

⚠️ `tags[-]` était jusqu'ici un nom de colonne littéral (le motif de rang ne
reconnaissait pas `-`). Requête de vérification en production, lecture seule :
`SET statement_timeout = '15s'; SELECT count(*) FROM datastore_rows, jsonb_object_keys(data) k WHERE k LIKE '%[-]';`

**Compter** (`db/paths.py`) : `split_list_path` reconnaît l'élément NU, `tags[]` et
`tags[0]` (`_LIST_ELEMENT_RE`, nom de colonne sans espace ni point, comme l'écriture) ;
`leaf_read_sql(…, None)` lit l'élément lui-même par la règle d'une case. Filtre,
`group_by` et métriques en héritent sans autre changement (`db/query.py` inchangé).

Bancs : `tests/datastore/test_ajout_retrait_liste_oto102_live.py`.

## Les lignes en place face au schéma posé — `existing_violations` (#479, 30/09/2026)

Poser un schéma ne revalide pas l'existant. Les avertissements de la pose disaient en
PHRASES quatre crans (`required`, `max_length`, `pattern`, options) ; restaient muets
`required_when`, les sous-champs de liste (le cas fondateur : 854 lignes sur 8 910
portaient déjà une valeur que la garde condamnait), le type déclaré et la clé métier.

`set_schema` (donc `patch_schema`) rend désormais `existing_violations`, par CHEMIN
(`contacts[].email`) : `rows`, `blocking_rows` (lignes qui n'acceptent plus AUCUNE
écriture, sur aucune colonne — `required`, `required_when`), `sample_ids` (5 au plus) et
`consequence` en clair. `existing_violations_scope` dit ce qui a été examiné
(`rows_examined`, `rows_total`, `complete`). Trois états : clé absente = rien à juger
(rien d'armé, tableau vide) ; `{}` avec `complete: true` = examiné, rien.

- **Un seul moteur** (`datastore/violations_existantes.py`) : chaque ligne est jugée par
  `validate_row`, deux fois — réécrite en entier (tout ce que le format condamne), puis
  sous une écriture d'une AUTRE colonne (ce qui bloque). Le jugement ne liste aucun cran :
  ce que le moteur fera respecter demain sera relevé demain. Le chemin se lit en tête du
  refus (`<chemin>: …`, ou `` `<colonne>` est déclarée … `` pour le type) ; le banc fixe
  chaque famille.
- **Ne balaie que si le schéma arme quelque chose** (`arme`) : un libellé changé ne coûte
  pas dix mille lignes jugées. Son banc le confronte à toutes les sondes de `enforced`.
- **Borne dite** : 10 000 lignes examinées au plus, par pages de 500. Au-delà,
  `complete: false`, et le `warning` dit que les comptes sont des PLANCHERS.
- Les phrases des quatre crans d'origine restent : elles comptent en SQL sur la table
  entière, sans plafond.

Banc : `tests/datastore/test_violations_existantes_479.py`.


## Le vocabulaire d'un schéma est fermé (oto#34, oto#35, oto#127, 01/10/2026)

**Avant** : une clé inconnue était acceptée, puis SIGNALÉE (`unknown_keys_warning`,
#316/oto#56). Mesuré le 01/10/2026 sur 442 tableaux à schéma : 45 portaient des clés
que personne ne lisait — `labels` sur une colonne (31 tableaux), `explained_by` (29),
`editable` (26), `depends_on` (25), `enum` pour `options` (23), `note` (12)… et
`origine` (29), déclarée sans lecteur depuis le 08/09. L'avertissement ne suffisait
pas : il parle sur un appel qui réussit, et la liste qui le fondait — dérivée des
`.get()` du code — en comptait trop (`strict` ou `states` posés sur une colonne
passaient, lus à un AUTRE niveau).

**Désormais** : chaque niveau DÉCLARE ce qu'il admet (`schema_keys.ADMISES`) — la tête,
une colonne, un sous-champ (dérivé de la colonne, moins
`PREMIER_NIVEAU_SEULEMENT` : `readonly`, `agent_access`, `lifecycle`, `formula`,
`display`), l'élément d'une liste (`of` : `key`, `type`, `fields`, `of`, `options`,
`label`, `description`) et le bloc `lifecycle`. Une clé absente de son niveau est
REFUSÉE par `cles_inconnues.refus`, appelé en tête de `validate_schema_def` — donc à la
pose et au patch, MCP et REST. Le refus (400 `invalid_schema` / `invalid_patch_schema`)
nomme le chemin (`fields.contacts.of.fields.email`), la clé, et où elle va : la clé
proche (`FAUTES_CONNUES`, puis `phrases_de_refus.cle_la_plus_proche`), un autre niveau
(`states` → `lifecycle.states`), un paramètre d'appel (`semantic_search`), `description`
pour un texte d'aide, une clé retirée (`origine`), sinon la liste du niveau et `meta`.
Les listes sont servies par niveau sur `GET /api/datastore/schema/keys` (`levels` ;
`keys` reste le niveau colonne, que le dashboard confronte).

- **`meta`, la zone libre** : un objet, admis à chaque niveau (cycle et élément
  compris — le rangement en a besoin pour `lifecycle.initial`), transporté tel quel,
  jamais lu, borné à `META_MAX_OCTETS` = **4096 octets** de JSON compact (UTF-8). Un
  schéma est relu à chaque écriture de ligne : une annotation n'a pas à le faire
  grossir sans limite.
- **Les textes d'aide** : `description` est le seul. `note`, `help`, `hint` et
  `placeholder` (aucun lu par le dashboard, vérifié sur son `origin/main`) y sont
  repliés par la migration et refusés depuis.
- ⚠️ **Le refus porte sur ce que le GESTE pose ou modifie, jamais sur ce qui est
  stocké.** `validate_schema_def(schema, ancien)` reçoit le schéma en place
  (`set_schema` le lit AVANT de valider) ; une clé inconnue qui y figure au même chemin
  avec la même valeur passe. Sans quoi le patch d'une AUTRE colonne — qui repasse le
  schéma fusionné par `set_schema` — serait refusé sur tout tableau pas encore migré.
  Modifier une clé stockée (y compris la passer à `null`) est un geste : refusé ; la
  retirer passe par `remove_attrs`. Ce qui reste stocké est dit en `warning`
  (`residus_warning`), à la pose comme à la lecture (`data_get_schema`), avec la clé qui
  fait foi quand sa cousine est posée à côté (`enum` → `options`).
- `FILE_KEYS` perd `lease` et `claims` : aucune déclaration ne les admettait, aucun
  schéma ne les portait, et un marqueur que nul ne peut poser ne désigne rien.

**La migration** (`scripts/durcir_schemas.py`), lancée une fois par l'exploitation
depuis le commit qui l'apporte, AVANT la bascule — jamais au boot. À blanc par défaut,
`--appliquer` pour écrire. Elle replie les textes d'aide, supprime `enum` quand
`options` existe (renomme sinon — ce qui ARME la liste sur un tableau strict : la pose
rend le relevé des lignes qui la violent), supprime `origine` et `semantic_search` en
tête, et range TOUTE autre clé inconnue dans `meta` au même niveau — jamais convertie en
clé active. Chaque schéma s'écrit par `DatastorePg.set_schema` (store système : sans
principal, résolu par numéro), journalisé dans `tool_calls` (`data_set_schema`, `sub`
nul, `args.migration_systeme`). Un tableau qui demanderait un arbitrage (`meta` qui
n'est pas un objet, conflit de clé dans `meta`, borne dépassée) est listé, jamais
écrit ; un schéma qui a bougé depuis l'inventaire est sauté.

**Les schémas cibles des slots de procédure** (`org_instructions.slots[*].schema`, ADR
0035 × 0046) suivent les mêmes règles : `slots.validate_slots` les juge comme une pose,
contre les slots STOCKÉS de la procédure (le slot du même nom ; une restauration
`from_version` est jugée contre la version restaurée) — une procédure pas encore
migrée reste enregistrable. La migration les range aussi, par `org_store.set_instruction`
(nouvelle version, ancienne gardée dans `org_instruction_revisions`, `set_by` =
`migration:durcir_schemas`, `expected_version` contre l'écrasement) ; une procédure
retirée est listée, jamais écrite. Ce schéma est provisionné tel quel sur le tableau
lié (`provision_tableau_schema`) : non rangé, il réintroduirait l'ancien vocabulaire.

## Lire un schéma : compact, et tel qu'il est stocké (oto#35, oto#94, 01/10/2026)

`data_get_schema` / `GET /api/datastores/{ds}/schema` prennent deux paramètres, sur les
deux faces (une capacité, une autz, un handler) :

- **`forme=compacte`** (oto#35) : `schema` ne garde, à chaque niveau, que les clés que le
  VALIDATEUR lit (`schema_keys.CONTRAINTES`, dérivée des lecteurs déclarés), sans clé
  nulle — ni `description`, ni `label`, ni `meta`, ni `hidden`/`width`/`role`, ni les
  `labels` d'un cycle, ni une clé résiduelle. La réponse porte `forme: "compacte"`.
  ⚠️ **Une forme compacte ne se repose jamais** : `set_schema` REMPLACE, elle effacerait
  toutes les descriptions. Lecture d'inspection seulement. Les avertissements se
  calculent toujours sur le schéma entier.
- **`tel_que=stocke`** (oto#94) : le schéma STOCKÉ, entier, colonnes `agent_access:
  "none"` comprises, quelle que soit la face. Réservé au TITRE — possède ou gouverne
  (`_peut_forcer`, le palier qui pose `agent_access`) — sinon `403 forbidden`, avec la
  phrase qui renvoie au défaut. La réponse porte `gardes` : `verrouillees`
  (`readonly_fields`, formules comprises), `masquees_a_l_agent`, `lecture_seule_agent`
  (`acces_agent.masquees`/`fermees`) et `sans_effet` (`[{chemin, cle, raison}]` :
  `agent_access` sur la clé métier ou à valeur inconnue, `options` d'un tableau non
  strict, `lifecycle` hors colonne de file, clé qu'aucun niveau n'admet). Tout est lu
  des prédicats qui DÉCIDENT, jamais d'une copie de leur règle.

**`tel_que` est toujours dans la réponse** (`servi` par défaut) : deux réponses de même
forme ne se distinguaient pas. Sur la lecture servie, **`colonnes_masquees`** dit
combien de colonnes la face outil a retirées — le compte, jamais le nom ; absent quand
rien n'est retiré (toujours sur REST, qui ne masque pas).

Reste hors de ce lot : un marqueur de champ ABANDONNÉ (oto#35 §4) — aucune clé du
vocabulaire ne le porte, la forme compacte ne peut donc pas encore les écarter ; et
l'état actif/inerte de CHAQUE contrainte de validation (oto#34).

## Une colonne se déclare avant de s'écrire — une règle, plus de modes (oto#124, 05/10/2026)

Une écriture qui nommait une colonne inconnue la créait à la volée : le schéma
s'étendait au fil des écritures, sans geste dédié (« ça fait n'importe quoi dans les
flottes d'agents », 07/09). Décidé le 05/10 : **une colonne que le schéma ne déclare pas
est REFUSÉE, sur TOUS les tableaux**, tableau sans schéma compris. Pour écrire dans une
colonne nouvelle, on la déclare d'abord (`data_patch_schema(fields=[{"key": …}])`, REST
`PATCH …/schema`). Livré en préavis daté (la colonne créée, une phrase dans `notices`),
**en vigueur depuis le 21/10/2026** ; la date, son réglage
(`OTO_COLONNE_NON_DECLAREE_REFUSEE_LE`) et le préavis sont retirés le même jour
(oto-backend#1292).

- **Le refus** (`datastore/colonnes_non_declarees.py`) : `ColonneNonDeclaree` (sous-classe
  de `RowValidationError`) — REST et réception d'upload `400 unknown_column`
  (`details.colonnes`, `details.expected_column` quand la clé est une couche mal
  écrite, `nom_comment` → `nom.comment`), MCP `INVALID_PARAMS` avec le même texte. Un lot
  (MCP, REST, upload signé) et un import en tranches sont jugés ENTIERS avant leur
  première ligne (`juger_le_lot`) : rien n'est écrit. Texte servi : `data_write`,
  `data_set_schema`, `data_patch_schema`, routes REST d'écriture, guide
  `datastore-semantics` §4 sexies bis.
- **Un seul seam** : `_check_row`, que traversent la création unitaire, la fusion par
  clé, le patch par `id` et la ligne de lot. Le prédicat (`non_declarees`) juge ce que le
  geste POSE : une ligne qui porte une colonne ancienne non déclarée reste écrivable sur
  ses colonnes déclarées, et `null` sur l'ancienne l'efface (un effacement ne crée rien).
  Le relevé `hors_schema` (#294) et le cran `unknown_columns: "reject"` (#614/#678), qui
  vivaient au même seam, sont retirés : la règle commune les rend sans objet.
- **Découplé du format qui fait contrat** — arbitré à part le même jour : c'est la
  section suivante.
- **Le gel** (`scripts/figer_colonnes.py`, à blanc par défaut, `--appliquer`) : chaque
  tableau — à schéma ou sans — reçoit la déclaration des colonnes que ses lignes
  portent ; type déduit des valeurs (`datastore/types_inferes.py` : homogénéité stricte,
  sinon pas de `type`, qui ne contraint rien ; ni `options` ni `required`). Par
  `set_schema` via le store système de `durcir_schemas`, journalisé, schéma relu avant
  d'écrire (bougé ⇒ sauté), une requête bornée par tableau, idempotent. Joué sur la base
  partagée le 20/10/2026 ; il reste le geste de toute AUTRE base née avant la règle
  (une instance dédiée a sa propre base, `docs/instance-cible.md`) — sans lui, ses
  tableaux refusent l'écriture des colonnes qu'ils portent sans les déclarer.
- **Ceux qui déclarent pour celui qui écrit** : `oto_import` déclare les en-têtes CSV
  neufs et les clés NDJSON neuves (typées par la même règle) ; une recette déclare ses
  colonnes aussi sur un tableau libre. L'upload signé (`oto_upload_url`) ne déclare
  rien : une colonne neuve y est refusée.
- **Un tableau créé nu** naît sans colonne : sa première écriture est refusée tant qu'on
  ne lui a pas déclaré ses colonnes — le refus le dit (« ce tableau ne déclare encore
  aucune colonne ») et donne le geste.
- **Naître avec son schéma** (suite d'oto#124) : `data_create_datastore(schema=…)` et
  `POST /api/datastores` (`{"datastore", "schema"}`) prennent le MÊME objet que
  `data_set_schema` / `PUT …/schema` — c'est le geste normal pour créer un tableau qu'on
  va remplir (texte servi : `colonnes_non_declarees.description_creation`).
  - **Pourquoi `schema` entier et pas `fields`** : la clé métier se pose le plus souvent
    à la naissance (c'est elle que désigne `key`) ; `fields` seul imposerait un second
    appel pour elle, exactement ce qu'on retire. Et `schema` est le nom et la forme du
    paramètre de `data_set_schema` : aucun vocabulaire de plus.
  - **Un seul chemin de pose** : `set_schema` est coupé en résolution + `_poser_schema`
    (validation, vocabulaire fermé, index de clé métier, relevés), que la création
    appelle sur le tableau qu'elle vient d'insérer — sans le re-résoudre par son nom, qui
    pourrait désigner un homonyme sous l'org active.
  - **Atomique pour l'appelant** : un schéma refusé retire le tableau à peine inséré et
    relève l'erreur d'origine (MCP `INVALID_PARAMS` « n'a PAS été créé », REST
    `400 invalid_schema` avec le message) — sinon la reprise sous le même nom prendrait
    `datastore_exists`. Un nom pris reste `409` AVANT toute pose.
  - Sans `schema` (absent ou `null`), la réponse et le tableau sont ceux d'avant. Garde :
    `tests/datastore/test_creation_avec_schema_124_live.py`.

## Le format déclaré fait contrat partout — plus aucun réglage (oto#124, 05/10/2026)

La validation COMPLÈTE d'une écriture était un réglage : `unknown_columns` autre que
`create` faisait respecter les `options` de premier niveau, la forme des valeurs
`text`/`url`/`object`/`list`/`enum`, les couches inconnues, la fermeture des sous-records
déclarés et, à la pose, la garde de `lifecycle.claimable`. Mesuré le 05/10 : active sur
100 tableaux, éteinte sur 382 — et 596 lignes déjà en faute sur 25 de ces 382 (options
295 lignes / 16 tableaux, forme 411 / 12, sous-records fermés 3 / 1, couches 0). Décidé le
05/10 : **toujours refuser, plus aucun réglage.** Livré en préavis daté (la faute écrite,
une phrase dans `notices`), **en vigueur depuis le 21/10/2026** ; la date, son réglage
(`OTO_VALIDATION_COMPLETE_LE`), le préavis et la lecture transitoire du réglage stocké
sont retirés le même jour (oto-backend#1292).

- **La décision est UNE fonction** : `validation_complete.complete(schema)` — vraie pour
  tout tableau qui déclare une colonne. `declaration.validation_active`,
  `validate_row(contraignant=…)`, la garde de `claimable` à la pose et l'avertissement
  des colonnes orphelines la lisent.
- **Un seul seam** (`controles._check_row`) : MCP, REST, lots, upload signé, import. Le
  refus : `row_invalid`, ou la valeur hors options ÉCARTÉE quand elle est la seule faute
  (#667). À la pose, l'existant hors liste est relevé (`_offending_enum_warning`).
- **On juge ce que le geste ÉCRIT, jamais ce que la ligne porte déjà** — vérifié famille
  par famille dans `_row_errors` : options et forme (`errs_type and not pose` → `gelees`),
  sous-records fermés (`closed or (contraignant and pose)`), élément de liste non écrit
  (`elements_reecrits`) ; une couche inconnue EN PLACE part dans `gelees` (servie en
  `hors_type`), et une valeur hors options en place n'entre pas dans `hors` (ce que
  l'écriture peut écarter, #667). Les données des tenants ne sont pas corrigées.
- **`unknown_columns` retiré** : `schema_keys.CLES_RETIREES` (refusé à la pose, au patch,
  dans un slot, à la création, avec « plus aucun réglage : les colonnes et les valeurs
  sont toujours vérifiées »), paramètre de `data_patch_schema` retiré et refusé
  (`reglages.refus_parametres`, les deux faces), ni servi dans `reglages` ni annoncé
  dans `enforced`, et **plus lu nulle part**.
- **Le retrait du stocké** : joué sur la base partagée le 21/10/2026 par un script
  dédié (`retirer_unknown_columns`, 0 tableau sur 543 au passage à blanc suivant), retiré
  ensuite (oto-backend#1292). Une autre base qui le porterait encore le garde sans effet
  — toléré tant qu'on n'y touche pas, dit à la lecture comme toute clé qu'aucun niveau
  n'admet (`cles_inconnues.residus_warning`) — et le range par
  `scripts/durcir_schemas.py`, qui le supprime en tête.

## Ajouter une ligne à une cellule texte sans la relire (06/10/2026)

**Le défaut.** Une colonne `text` ne s'écrivait qu'entière : pour ajouter une ligne à
un journal tenu dans une cellule, l'agent relisait et réémettait jusqu'à 25 000
caractères, et deux écritures concurrentes perdaient un ajout. Une perte de données a
eu lieu (signaux d'usage 929, 1158 et leurs doublons).

**Le contrat retenu : le même verbe `[+]`** (`datastore/rangs.py`). Sur une colonne
déclarée `text` — ou, non déclarée, qui porte déjà du texte — `journal[+]` ajoute en fin
de cellule, chaque morceau sur sa ligne (`\n`) ; une liste ajoute plusieurs lignes ;
une cellule vide prend l'ajout tel quel. Pas de second verbe ni de séparateur au
choix : `[+]` est déjà l'ajout servi sur toutes les faces, et une ligne est l'unité
d'un journal. La résolution se fait dans `EcrituresParRang.appliquer`, sous le
`FOR UPDATE` de la ligne, contre la valeur en place : deux ajouts simultanés arrivent
tous les deux. Le texte résultant suit la fusion et la validation d'une colonne
entière (`max_length`, `pattern`, mots réservés refusés), et le journal porte l'avant
et l'après. Refusés et nommés : `[+]` sur une colonne ni liste ni texte, un morceau
qui n'est pas une chaîne, une chaîne vide, un rang ou un retrait sur un texte. Une
colonne non déclarée et vide garde l'ajout d'élément de liste, comme avant.
Le banc : `tests/datastore/test_ajout_texte_live.py` (store, REST, MCP ; patch, lot,
création, concurrence).

## Mettre à jour PAR FILTRE — `data_update_where` (07/10/2026)

Une étiquette DÉRIVÉE (palier Hot / Tier 1-3 calculé sur plusieurs colonnes) devait
passer par `data_write(rows=[…])` : chaque `_id` rapatrié dans le contexte du modèle
puis renvoyé par lots — du temps, des jetons, et des lignes perdues au-delà de ~57 Ko
de lot (29/09/2026). La règle restait sinon un filtre de LECTURE, réimplémenté par
chaque consommateur, invisible dans le tableau, sans date.

`data_update_where(datastore, set={col: val}, filter|filters, only_if_empty, dry_run,
cursor)` — capacité `me.datastore.update_where`, REST
`POST /api/datastores/{datastore}/rows/update_where` ; le travail vit dans le store
(`datastore/par_filtre.py`).

- **Sélection** : la grammaire de `data_rows`, par le même `_clauses`. Aucun filtre =
  toutes les lignes (la règle « le reste »).
- **`only_if_empty`** ajoute une clause `empty` par colonne visée : des règles
  ordonnées jouent « la première qui gagne ». C'est le prédicat de `data_rows` : une
  case `@empty` y compte comme VIDE, et la règle la remplit. Refusé quand `set` ne
  pose que des couches (rien à attendre vide).
- **Chaque ligne passe par `update_row`** — verrou, bail, `readonly`, champs réservés,
  cycle de vie, formules — et donc par le journal des révisions (déclencheur). Pas
  d'UPDATE en masse : il contournerait toutes ces gardes.
- **Précondition de révision** : la `rev` lue à la sélection est passée à
  `update_row` ; une ligne changée entre-temps est SAUTÉE (`conflicts`).
- **Valeur déjà en place** : jugée au TYPE près (`couches.same_value`, valeur déballée
  et couches), comptée `unchanged`, rien n'est écrit — une écriture sans effet
  remettrait quand même à zéro l'état de file de la ligne. `updated` = la révision a
  avancé, lue sur ce que rend `update_row`.
- **Colonne manquante** : déclarée d'abord (`patch_schema`, type déduit : `bool`,
  `number`, `json` pour une liste ou un objet, `text` sinon ; retirée si la règle est
  ensuite refusée), seulement
  si le tableau déclare déjà des colonnes — sur un tableau sans schéma, un premier
  champ rendrait toutes les autres colonnes « non déclarées » d'un coup.
- **Fautes de la RÈGLE, refusées avant toute ligne** : une valeur qui EFFACE (`null`,
  `""`, `[]`, `{}`, `@empty`, une couche vide — effacer en masse sans `confirm`, c'est
  `data_drop_column`), la clé métier, un rang de liste, la couche `origine`, une valeur
  hors `options` déclarées, une colonne `readonly` hors `only_if_empty` (le verrou
  laisse remplir une case vide). Ensuite, si les 3 premières lignes tentées sont
  TOUTES refusées et rien n'est écrit, la règle est refusée entière ; un refus propre
  à une ligne est compté dans `refused` et n'arrête pas les autres.
- **Budget de temps** (`BUDGET_S` = 30 s, sous les 45 s de la face REST) : au-delà,
  `next_cursor` = la dernière ligne traitée ; repasser l'appel avec `cursor=` continue.
  Mesuré : ~9,5 ms par ligne sur base locale ⇒ ~2 000 lignes par appel.
- **Réponse en comptes**, jamais en lignes : `matched` (après le curseur quand il est
  passé), `updated/unchanged/conflicts/refused`, au plus 20 exemples de refus, de
  conflits et de `valeurs_ecartees` (total dans `valeurs_ecartees_total`), et `ns_id`.

## Commentaires d'une ligne (27/10/2026)

Les personnes qui lisent une ligne en parlent À CÔTÉ de ses valeurs : le panneau de ligne
du front y montre un fil. `GET|POST /api/datastores/{datastore}/rows/{row_id}/comments`,
`DELETE …/comments/{comment_id}`, et sur la face agent `data_row_comments` /
`data_row_comment` (`capabilities/datastore/comments.py`, table `datastore_row_comments`).

- **Pas une colonne.** Commenter n'écrit pas la ligne : ni `rev`, ni journal des
  révisions, ni réveil d'un agent qui surveille les écritures. Une colonne `comments`
  aurait fait de chaque mot une révision, et refusé le geste à qui ne peut qu'y lire.
- **Lire et écrire = l'accès de LECTURE** de la ligne : un client à qui l'on partage un
  tableau en lecture en commente les lignes. C'est l'usage visé. ⚠️ **Aucune limite de
  débit** : tout lecteur poste autant qu'il veut (5 000 caractères par commentaire).
  Question produit ouverte — faut-il un plafond, ou réserver l'écriture aux éditeurs ?
- **Retirer** : l'auteur, ou qui gouverne le tableau (`ownership.can_govern`) ; sinon
  `403 comment_not_yours`. Retrait doux (`deleted_at`), plus jamais servi.
- **Ligne supprimée** : la règle de l'historique — ses commentaires restent, lisibles par
  qui gouverne le tableau seulement ; personne n'y écrit plus (`404 row_not_found`).
- **Ordre** : une page rend les plus récents, rangés du plus ancien au plus récent (un
  fil) ; `before_id` = le `next_before_id` précédent lit la page plus ancienne.
- `author` est le sub, `author_email` se résout à la lecture (comme le parcours d'une
  ligne) ; `mine` et `can_delete` sont calculés pour l'appelant.
- `GET /api/datastores/{datastore}/comments/counts?row_ids=a,b,c` (REST seule, `row_ids`
  OBLIGATOIRE, 100 au plus — virgules et/ou paramètre répété, la forme de `fields`) :
  `{row_id: n}` des commentaires vivants de ces lignes, qui existent encore, lignes à
  zéro omises, `total`, et `latest` : `{row_id: {id, body, body_truncated, author,
  author_email, at}}`, le plus récent de chaque ligne comptée, corps ramené à 280
  caractères (le texte entier se lit dans le fil). Bornée aux lignes AFFICHÉES : une
  sonde d'index par ligne (`LATERAL … ORDER BY id DESC LIMIT 1`), jamais une
  agrégation du tableau entier — une seule agrégation, lue par le front une fois par chargement de tableau.
- Surface REST **provisoire** (`x-oto-provisoire`) tant que le front n'a pas tranché.
