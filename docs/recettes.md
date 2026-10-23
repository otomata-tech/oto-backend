---
title: Les recettes — un outil de connecteur vers un tableau, sans modèle
type: explanation
description: >-
  `oto_recipe` : une description stockée et versionnée de la façon dont les résultats
  d'un outil de connecteur arrivent dans un tableau — outil, arguments, pagination,
  correspondance champ → colonne, clé —, exécutée par le serveur sans qu'aucun modèle
  ne relise ni ne recopie les lignes. Ce que ce premier lot couvre (le mode `pull`),
  ce qu'il refuse à tout client, et ce qui vient ensuite. À lire avant de
  toucher `oto_mcp/recipes/`, `db/recipes.py` ou `capabilities/recipes.py`.
---

# Les recettes

## Le problème

Faire passer les résultats d'un outil de connecteur dans un tableau, c'est aujourd'hui
un travail de modèle : il appelle l'outil, lit la page entière dans son contexte, puis
la retape en `data_write`, 25 lignes à la fois. Ça coûte deux fois (les jetons d'entrée
pour lire, ceux de sortie, plus chers, pour retaper), c'est l'étape où les runs meurent
avant d'avoir écrit (réponse tronquée à la limite de sortie), et les données de
personnes traversent un contexte de modèle. Il n'y a aucun jugement dedans.

## Le principe : le modèle écrit la recette une fois, le serveur l'exécute ensuite

Une **recette** dit quel outil appeler, avec quels arguments, comment parcourir ses
pages, quel champ de chaque élément va dans quelle colonne, et quelle colonne
identifie une ligne. Un agent l'écrit à partir de la forme d'une page (`sample`),
l'éprouve sur une vraie page sans rien écrire (`test`), la publie ; ensuite `run`
l'exécute autant qu'on veut, et ne rend que des comptes.

Une recette est une DONNÉE de l'org (tables `recipes` / `recipe_versions`), pas du code
du dépôt : ajouter un connecteur, c'est écrire une recette — ni PR ni version.

## Exemple (forme générique)

```json
{
  "tool": "linkedin_aiark_search",
  "params": {"company_uuid": {"required": true}, "company": {"required": true},
             "country": {"default": "France"}},
  "arguments": {"op": "people",
                "account": {"id": {"any": {"include": ["{{params.company_uuid}}"]}}},
                "contact": {"location": {"any": {"include": ["{{params.country}}"]}}}},
  "source": {"items": "content",
             "pagination": {"type": "page", "param": "page", "start": 0,
                            "size": 50, "size_param": "size", "last": "last"}},
  "where": [{"path": "location.country", "op": "eq", "value": "{{params.country}}"}],
  "map": {"linkedin_url": "link.linkedin", "full_name": "profile.full_name",
          "title": "profile.title", "headline": {"path": "profile.headline", "max": 300},
          "city": "location.city", "seniority": "department.seniority", "aiark_id": "id"},
  "values": {"company": "{{params.company}}", "status": "sourced"},
  "key": {"column": "contact_key",
          "template": "{{params.company|slug}}::{{item.link.linkedin}}"},
  "on_existing": "skip",
  "limits": {"max_units": 500, "max_pages": 20}
}
```

## Le langage, volontairement petit

- **Chemins** pointés avec index : `profile.title`, `emails[0].email`. Un chemin qui ne
  mène nulle part rend une case vide, jamais une erreur.
- **Gabarits** `{{params.x}}`, `{{item.a.b}}`, `{{row.col}}` (sous `for_each`), filtres
  `slug`, `lower`, `upper`, `strip`, `unaccent`, et les **normaliseurs** `domain`
  (`https://www.Acme.com/x` et `jane@acme.com` → `acme.com`), `email` (sans `mailto:`,
  vide si la valeur n'en a pas la forme : jamais une phrase d'erreur dans une colonne
  `email`), `email_domain`, `linkedin_slug` (`…/in/Jane-Doe/?trk=…` → `jane-doe`, sur un
  hôte LinkedIn seulement ; un domaine nu n'est pas un identifiant), `url` (écrite avec
  son schéma ; comparée sans schéma ni `www.` : `http://acme.com` = `https://www.acme.com`),
  `digits`. Un normaliseur qui reçoit une liste ou un objet REFUSE (`non_scalar_value`) :
  le chemin pointe un élément (`emails[0]`), jamais la liste entière.
  Un gabarit seul garde le type de sa valeur ; mêlé à du texte, il devient du texte.
- ⚠️ **`slug` reproduit la forme des clés déjà écrites** par les procédures de sourcing :
  minuscules, accents retirés, chaque suite non alphanumérique réduite à `_`, aucun `_`
  aux bords. La changer dédoublerait chaque ligne au premier passage.
- **`where`** : `eq`, `ne`, `in`, `not_in`, `contains_any`, `empty`, `not_empty`, sans
  casse ni accents — ou par le normaliseur de la clause (`normalize: "domain"`).
  `in_table` / `not_in_table` (`table`, `column`) comparent à la colonne d'un AUTRE
  tableau (liste d'exclusion, clients existants) : ses valeurs sont lues une fois par
  exécution (chaque valeur d'une case à plusieurs valeurs comptée), normalisées comme
  l'élément, au plus 50 000 (`match_table_too_large`). Un
  élément sans valeur n'est pas « dans » la liste : `not_in_table` le laisse passer.
- **`for_each`** (`datastore`, `status_column`, `filter`, `max_parents`,
  `max_items_per_row`) : chaque ligne d'un tableau PARENT dont la colonne d'état est vide
  déclenche l'appel (les personnes d'une société, les offres d'un domaine), citée
  `{{row.col}}` dans les arguments, la correspondance, les valeurs et la clé — et nulle
  part ailleurs : `row` hors `for_each` est refusé à l'écriture. Ses pages repartent de
  zéro à chaque parent ; `max_items_per_row` borne ce qu'un parent apporte, et réduit la
  taille demandée au fournisseur (au curseur, et sur la première page au numéro). Une
  ligne dont une colonne citée dans les `arguments` est vide n'est **pas sélectionnée** —
  l'appel serait payé pour rien : elle n'est ni appelée ni marquée, et reste en attente
  jusqu'à ce qu'on la remplisse. Pour qu'une exécution `done` ne la taise pas, le reçu
  compte ces lignes (`skipped_empty_input`) sur l'exécution qui s'achève (`done`) — un
  reçu partiel (reprise, plafond) ne le porte pas, un reçu sans `for_each` non plus.
- Tout ce qui demande davantage (expression régulière, condition, calcul) ira dans une
  fonction (`oto_function`) — on ne fait pas grandir ce langage.

## Les recettes en code (#1202)

Le langage déclaratif ci-dessus est GELÉ (#1202) : ce qu'il ne dit pas s'écrit désormais
en Python. Une recette-code se range comme les autres (mêmes tables, même cycle, même
`oto_recipe`) et se reconnaît à `kind: "code"` (`recipes/code.py`) :

```json
{"kind": "code", "sources": {"recette.py": "async def run(oto, params): ..."},
 "entrypoint": "recette:run", "params": {"company": {"required": true}},
 "limits": {"max_units": 200, "max_calls": 100, "max_seconds": 120}}
```

- **Le code** tourne dans le bac à sable d'`oto_function` par le pont
  (`docs/fonctions.md` §Le pont) : `await oto.call(outil, arguments)`. Il peut appeler
  les outils de connecteur DÉCLARÉS EN LECTURE (mêmes refus que ci-dessous), `data_rows`
  (une page d'un tableau que l'appelant lit, `MAX_LIGNES_LUES`) et `data_write(rows, key)`
  — dans le SEUL tableau qu'`op=run` désigne (`datastore`), jamais en épreuve, `key`
  exigée (`write_outside_target`, `write_in_test`). Aucun autre outil de connecteur qui
  écrit, hors la liste fermée ci-dessous (« Écrire chez un tiers »).
  Ces deux-là sont des appels ORDINAIRES des outils `data_*` (`meta.executer_cible`,
  comme tout appel du pont) : leurs gardes (droits sur le tableau, org suspendue, org du
  run), leur ligne dans `tool_calls`. Le pont rétrécit ce que le code leur passe, il
  n'ouvre jamais le store lui-même.
- **Le budget** est `limits` : unités facturées (`max_units`, obligatoire), appels,
  secondes. Atteint, les appels suivants sont refusés et le reçu le dit (`stopped`).
- **Pas de reprise** (`resume_not_supported`) : une relance repart du début, et ses
  écritures par clé ne doublent rien.
- **Le reçu** est ce que rend le code, borné à `code.MAX_RECU` caractères : des
  comptes, pas des données (`receipt_too_large`). L'épreuve rend en plus le `journal`.
- **Écrire chez un tiers** (lot 3) : un CRM, par une liste FERMÉE d'outils
  (`recipes/ecritures.py`) — créer, mettre à jour, ajouter à un groupe ; jamais
  supprimer ni fusionner. Une op permise peut s'appeler EN LOT (`items` / `ids` de
  l'outil, 50 fiches au plus) : c'est ce qui rend une poussée de quelques milliers de
  fiches faisable (≈50 appels au lieu de ≈2 000). Chaque écriture se DÉCLARE dans le corps,
  `writes: ["folk_record:create", "folk_record:update"]` : c'est ce que la plateforme relit
  avant de publier, et le pont refuse toute autre (`write_not_declared`). Les ops qui
  lisent de ces outils (`search`, `get`) passent sans déclaration ; un outil multiplexé
  exige son `op` littérale (`write_op_not_allowed` sinon). Une écriture n'est jamais
  jouée en épreuve (`write_in_test`), se fait dans l'org du run (`_org`, `_group`,
  `_project` refusés : `write_outside_run_org` ; `_account` choisit le compte), avec le
  compte de connecteur de qui exécute, et compte dans `limits.max_writes` (50 par défaut
  dès qu'une écriture est déclarée, sans plafond de plateforme : c'est la borne de qui
  écrit la recette), compté à la TENTATIVE et PAR FICHE
  (un lot de 50 = 50 écritures) ; un lot qui ne tient pas dans ce qui reste n'est pas
  joué du tout (`budget_exhausted`, le message dit combien il reste) — atteint,
  `stopped: "max_writes"`. Le reçu compte les écritures par nom (`writes`). Un outil qui
  peut déclencher un envoi (`lemlist_create_lead`) exige `allow_sending: true`. Pas de
  reprise ici non plus : une recette qui écrit cherche avant de créer, pour qu'une relance
  ne crée pas deux fois. Chaque appel reste un appel ordinaire de l'outil
  (`meta.executer_cible`) : sa garde d'activation, son journal, sa facturation.
- **Qui fait quoi** : le membre écrit (`create`, `propose`) et exécute (`run`) ; la
  PLATEFORME seule éprouve (`test`) et publie (`publish`, `platform_only` sinon). Le code
  tourne avec les droits de qui l'EXÉCUTE, pas de qui l'a écrit : tant que cette règle
  n'est pas tranchée (#1202), c'est celle d'`oto_function`. La publication exige une
  épreuve sans erreur ; une borne atteinte n'en est pas une.

## Les garde-fous

- **Seul un outil DÉCLARÉ EN LECTURE est appelable** (`@mcp.tool(annotations=LECTURE)`,
  `oto_mcp/tools/lecture.py` — l'annotation standard `readOnlyHint` du protocole). Un
  outil non déclaré est refusé (`recipe_tool_not_read_only`) avant tout appel : le défaut
  est le refus, jamais « présumé lecteur ». Un outil multiplexé par `op` ne se déclare que
  si toutes ses ops lisent. Les connecteurs à modèle (`jev`, `lighton`) sont refusés
  même s'ils lisent : une recette est « sans modèle ». Pour marquer un outil : le
  décorateur, et la garde `tests/test_outils_en_lecture.py` le vérifie.
- **Chaque page est un appel ordinaire de l'outil** (`tools/meta.executer_cible`, le
  corps d'`oto_call`) : activation du connecteur, axes, org du run, **journal sous le
  nom de l'outil avec sa `quantity`** — donc facturé comme un appel direct — et
  rédaction. Les lignes sont fabriquées depuis le résultat RÉDIGÉ ; un résultat retenu
  par la politique de l'org arrête l'exécution.
- **`limits.max_units` est obligatoire** (au moins 1, sans plafond de plateforme : la
  borne de dépense est celle de qui écrit la recette) et se compte sur ce que l'outil
  **FACTURE** : la `quantity` de sa ligne `tool_calls` (profils AI Ark, crédits
  Serper…). Seul un outil qui n'en rend pas est compté dans les unités de la recette
  (`units`) ; le reçu le dit (`units_basis` : `billed`, `declared` ou `mixed`). En
  pagination par numéro, une page n'est jamais coupée : si elle peut dépasser le reste
  du plafond, l'exécution s'arrête avant (`spend_cap`) — une taille réduite décalerait la
  fenêtre sur des éléments déjà lus. Au curseur, la dernière page est réduite au reste.
- **Une ligne existante n'est pas touchée** par défaut (`on_existing="skip"`) ;
  `update` ne réécrit que les colonnes de la correspondance, jamais les valeurs fixes.
- **La clé de la recette devient la clé déclarée du tableau.** Un tableau qui en
  déclare une autre est refusé (`key_mismatch`) avant tout appel ; un tableau qui n'en
  déclare aucune reçoit celle de la recette au premier `run`, avant d'écrire — l'unicité
  est alors tenue par la base, et deux exécutions concurrentes ne dédoublent pas. Des
  lignes qui répètent déjà une valeur la rendent impossible (`key_not_declarable`). Un
  élément sans valeur de clé est écarté — un gabarit dont un morceau manque n'en
  produit pas.
- **Les colonnes manquantes sont créées** (texte) ; les existantes jamais retouchées.
- **Une ligne parente faite reçoit son état** — `done` (des éléments), `empty` (aucun),
  `failed:<code>` (l'outil a refusé SON entrée : `invalid_input`, `not_found`,
  `call_refused`, `upstream_4xx` ; ou sa page est en dérive : `mapping_drift`) — dans
  `status_column`, déclarée si elle manque : c'est lui qui fait
  qu'une exécution suivante ne repaie pas une ligne faite. Un échec du compte ou du
  fournisseur (clé, crédits, délai, limite de débit) arrête l'exécution et laisse la ligne
  en attente. Trois lignes d'affilée qui échouent pareil arrêtent tout
  (`repeated_failure`) : c'est systémique, pas trois mauvaises lignes. Elles sont marquées
  `failed:<code>` et nommées au reçu (`failed_rows`) — pour qu'une exécution suivante ne
  bute pas sur les mêmes sans qu'on sache lesquelles ; vider leur `status_column` les remet
  en attente. `not_found` (une société inconnue du fournisseur) est marqué aussitôt, hors
  disjoncteur. Un appel refusé par le fournisseur compte dans la dépense (sa `quantity`,
  sinon une unité) et dans l'horloge.
  Une entrée qui se normalise en rien (`n/a|digits`) n'appelle pas : `failed:invalid_input`.
  Sous `for_each`, le reçu ne porte jamais le message du fournisseur (il peut citer une
  valeur de la ligne), seulement son code.
- **Ne sont sélectionnées que les lignes du `filter`** (la grammaire de `data_rows`,
  éprouvée avant tout appel : `invalid_filter`) **dont chaque colonne citée dans les
  `arguments` est remplie** : une ligne sans `siren` ne paie pas un appel pour rien, et
  attend qu'on la remplisse. Le tableau parent doit être ÉCRIVABLE et
  distinct de la cible (`for_each_same_table`), vérifié avant tout appel. Une ligne
  coupée par un plafond (dépense, pages, horloge) ou un refus de l'outil reste en
  attente : le jeton `resume` la reprend à sa page, une exécution neuve du début (les
  lignes déjà écrites sont reconnues par leur clé ; les pages, elles, se repaient).
  `max_parents` (facultatif, sans plafond) borne les parents d'un appel (`max_parents`
  au reçu, avec `resume`) ; absent, les parents se lisent par lots de 25 jusqu'à épuisement
  ou jusqu'à une autre borne (dépense, horloge). Le reçu compte `parents: {done, empty, failed}`. Une exécution
  NEUVE (sans `resume`) repart d'une dépense à 0 : au fil des exécutions, seuls les quotas
  de l'org bornent le total.
- **La dérive arrête l'exécution.** Les colonnes que l'épreuve de publication a remplies
  sur au moins 80 % des lignes (`test_report.fill`) sont surveillées — si l'épreuve a
  produit au moins 10 lignes : une colonne pleine sur une ligne ne prouve rien. Une page
  d'au moins 10 lignes où l'une revient vide PARTOUT n'est pas écrite, et l'exécution
  s'arrête (`mapping_drift`, `drifted_columns`) ; sous `for_each`, ce parent est marqué
  `failed:mapping_drift` et l'exécution passe au suivant (le disjoncteur arrête une
  série) — un fournisseur qui change la forme de sa réponse
  remplirait sinon le tableau de lignes creuses. Une colonne clairsemée à la publication
  (un `headline`, une ville) n'est pas surveillée.
- **L'épreuve d'une recette `for_each`** essaie jusqu'à trois parents en attente, une page
  chacun, et s'arrête au premier qui produit des lignes : le premier peut légitimement
  ne rien rendre. Elle n'écrit ni la cible, ni l'état des parents.
- **Budget d'horloge de 30 s**, qui court dès l'entrée (les lectures du tableau parent et
  des listes de correspondance comptent, chacune bornée à 10 s : `table_read_timeout`) et
  se vérifie après chaque appel, réussi ou non, et **`max_pages` par appel** quand la recette
  en pose un (facultatif, sans plafond de plateforme) : au-delà, reçu partiel et
  `resume`, que l'appel suivant passe pour continuer sans repayer les pages faites. Le
  jeton porte l'empreinte de la recette (une autre recette, ou une autre version, le
  refuse : `invalid_resume`) et jamais une dépense négative. Le
  plafond de dépense, lui, vaut pour toute la chaîne (le jeton porte la dépense faite).
- **Seule une version PUBLIÉE écrit.** `publish` éprouve une page réelle sans écrire :
  elle doit bâtir au moins une ligne, et s'arrêter sur la fin des résultats ou sur une
  BORNE (`max_pages`, `spend_cap`, `time_budget` — `BORNES_D_EPREUVE`), jamais sur un
  refus. Une recette à petit `max_units` s'arrête sur `spend_cap` au milieu de sa
  première page, lignes bâties : elle se publie. Une recette passée en ligne (`recipe`)
  ne sert qu'à `test` — `run` la refuse (`inline_recipe_cannot_write`).
- **Le reçu ne porte que des comptes et des codes** : pages, éléments vus, unités,
  lignes écrites / mises à jour / laissées intactes / écartées, codes d'échec.
- **`run` avec un `datastore` écrit dans ce tableau**, avec les droits de l'appelant sur
  lui : donner `oto_recipe` à un agent, c'est lui donner ce qu'il peut déjà écrire.
- **Un agent hébergé est un client comme un autre.** Sous le jeton de son travail, une
  recette se juge comme pour une personne : l'identité du porteur, l'org du travail
  (tenue par le verrou du jeton, `verrou_org.py` : une recette pour une autre org du
  porteur est refusée, `org_out_of_job`) et ses droits sur le tableau. Les refus du
  contrat valent pour tous. Aucune règle propre aux agents, comme pour `oto_call`.
- **…mais elle n'appelle que les outils de la liste de l'agent** (`outils_du_travail`,
  #1256), et elle le sait AVANT de commencer : `recipes/outils_cites.py` lit ce que son
  corps cite (`source.tool`, chaque `oto.call("<nom>"` littéral), et une recette-code dont
  un outil cité manque est refusée avant le premier appel, en un reçu qui les nomme TOUS
  (`code: tool_not_in_job`, `missing_tools`, `fix` = l'`oto_trigger op=update` qu'une
  personne joue : un agent hébergé n'élargit aucune liste ni ne crée un agent plus
  outillé que lui, `tools_human_only`) —
  plutôt qu'un outil découvert par exécution, après des écritures. Le même calcul sert
  l'agent, avant le run : `missing_tools` et les avertissements `not_granted` de
  `oto_trigger` (`docs/runner-et-automatisations.md`). Un nom construit à l'exécution
  n'est pas lu : il reste jugé à l'appel.

## L'affichage d'une recette — `display`, sur la fiche

Ce qu'une page de processus montre à l'endroit où il lance la recette : ses étapes, dans
l'ordre où elle les fait, et ce que chacune facture. **Sur la FICHE** (`recipes.display`,
JSONB nullable, révision `20261022_2100_recipes_display` + démarrage), comme le titre et
la description — **pas dans le corps versionné** : le changer ne propose ni ne republie
rien (republier une recette-code coûte une épreuve facturée, pour une étiquette).

- **Écrire** : `oto_recipe op=describe` (`slug`, `title?`, `description?`, `display`),
  ou `display` dès `op=create`. `display={}` l'efface.
- **Forme**, validée à l'écriture (`contrat.valider_affichage`, refus `invalid_display`
  avec tous les défauts) : `{steps: [{label, detail?, credits?, per?}], saves?}` — 1 à 10
  étapes ; `label` ≤ 60 caractères ; `detail` une phrase ≤ 200 ; `credits` (0 à 10 000)
  et `per` (`contact` | `company` | `row`) vont ensemble, absents sur une étape gratuite ;
  `saves` une phrase MESURÉE (≤ 160) de ce que la recette épargne, jamais une estimation.
- **Une recette DÉCLARATIVE n'a rien à écrire** : sans `display`, la route LIT ses étapes
  dans le corps de la version en service, sinon de la dernière proposée tant qu'aucune
  n'est publiée (`recipes/affichage.py` : pour chaque ligne, l'outil et sa pagination, le
  filtre, les colonnes et la clé) — elles suivent le corps et ne dérivent jamais. Les
  corps de toute la liste se lisent en une requête (`db.recipes.get_bodies`). Pas de crédits : le corps n'en dit rien (`max_units` est
  un plafond, pas un prix). Une recette-CODE n'a rien de lisible : ses étapes s'écrivent.
- **Les crédits écrits sont une ESTIMATION** : déclarés par qui décrit la recette, pas
  mesurés (aucun reçu d'exécution n'est gardé). La page les marque « estimated ».
- **Lire** : `op=list` / `op=get` (la fiche), et `GET /api/orgs/{id}/recipes`, dont
  `display.source` dit `described` (écrit) ou `derived` (lu dans le corps).
- **Où la page la dessine** : sur l'étape du schéma du processus qui NOMME la recette —
  `oto_recipe <slug>` dans sa note de marge (guide plateforme `procedure-flowchart`).
  C'est l'agent qui dessine le processus qui la place ; la page ne devine pas.

## Lecture REST : les recettes d'une org (PROVISOIRE)

`GET /api/orgs/{id}/recipes` (capacité `org.recipes`, REST seule, membre de l'org,
`x-oto-provisoire`) rend les fiches de l'org SANS corps — le corps d'une recette-code est
du code : `slug`, `title`, `description`, `status` (`published` dès qu'une version est en
service, sinon `proposed`), `published_version`, `latest_version`, `updated_at` (ISO 8601), et
`display` — écrit, sinon lu dans le corps d'une recette déclarative, null pour une
recette-code sans affichage écrit.

## Ce qui vient ensuite

Le bloc `async` (soumettre puis collecter : Dropcontact, FullEnrich, Apify), le mode par
ligne (`call` : remplir des cases d'une ligne existante) et la poussée vers un CRM
(`push` — un effet chez le tiers : il lui faut une marche à blanc obligatoire et une
liste des outils à effet, puisqu'une recette n'appelle aujourd'hui que des outils
déclarés en lecture), puis les travaux de fond déclenchés par une planification ou un
webhook (l'exécutant reste à choisir).
