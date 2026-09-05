---
title: Sémantique du datastore — couches, faces, clé métier
description: valeur/comment/link/origine, ce qu'une écriture détruit, ce que readonly et la clé métier protègent, ce qui diverge entre data_* et REST /api/datastore, et ce qu'une réponse ne contient pas
---

# Sémantique du datastore

À lire **avant** d'écrire dans un tableau que tu n'as pas rempli toi-même, ou d'appeler
la face REST (`/api/datastore/…`) à la place des outils `data_*`. Les deux faces parlent
au même stockage : ce guide dit ce que ce stockage fait d'une écriture, où les deux
faces divergent, et ce qu'une réponse ne dit pas.

## 1. Toute colonne a quatre couches

Vocabulaire fermé : `valeur` (la colonne elle-même) et trois couches qui la décrivent —
`comment` (ce qu'une autre source dit de cette valeur), `link` (l'URL qui l'étaye),
`origine` (d'où elle vient ; ou, quand la plateforme la pose, la valeur d'avant).
Une colonne « plate » est une colonne dont les couches sont vides.

Une couche mal orthographiée est **refusée par son nom** à l'écriture, rien n'est écrit.
Un dict qui mêle une couche connue et une clé inconnue est refusé de même — sauf si la
colonne est déclarée `json` au schéma ; un dict qui ne porte **aucune** clé de couche
est une donnée `json` ordinaire, jamais interprétée.

## 2. Écrire imbriqué, relire à plat

Écriture — la couche se pose **dans** la colonne :

    {"adresse": {"valeur": "12 rue X", "comment": "registre — 20 B AVENUE Y"}}
    {"adresse": "12 rue X"}                 # la valeur seule, couches intactes
    {"adresse": {"comment": "…"}}           # le comment seul, valeur intacte

Lecture (`data_rows`, `GET …/rows`, `GET …/rows/{row_id}`) — le **nom nu rend toujours
la valeur**, et chaque couche renseignée arrive **à plat**, sous une clé `champ.couche` :

    {"_id": "…", "adresse": "12 rue X", "adresse.comment": "registre — 20 B AVENUE Y"}

Il n'y a jamais de `adresse.valeur` ; une couche vide (`null`, `""`) n'est pas servie.
Ces clés plates s'adressent comme des colonnes dans `fields` et `filters` :
`[{"field": "email.origine", "op": "empty"}]` = les valeurs sans provenance.

Les deux formes sont **asymétriques** (imbriqué à l'écriture, plat à la lecture) ; une
option de lecture imbriquée (`layers=nested`) arrive dans un autre lot. En attendant,
l'aller-retour tient : une clé plate `adresse.comment` réémise à l'écriture est
**rangée** sous `adresse` dès que la colonne existe quelque part — dans l'écriture, sur
la ligne visée ou au schéma. Si elle n'existe nulle part, l'écriture est refusée en
nommant la clé et les trois endroits regardés — jamais une colonne littérale
`adresse.comment` créée en silence.

## 3. Ce qu'une écriture fait — et détruit

Une écriture ne touche **que ce qu'elle nomme**. Sur une colonne ouverte il n'y a ni
historique ni annulation : la valeur précédente disparaît quand la tienne arrive.

| tu écris | effet |
|---|---|
| `{"champ": Y}` ou `{"champ": {"valeur": Y}}` | valeur remplacée ; `origine` intacte ; `comment` et `link` **tombent** (ils décrivaient l'ancienne valeur) |
| `{"champ": Y}` avec Y identique à la valeur en place | **no-op** : toutes les couches restent |
| `{"champ": null}` | valeur effacée ; une `origine` pleine survit ; l'effacement revient dans `valeurs_effacees` (champ, ligne, valeur perdue) |
| `{"champ": ""}` (ou `[]`, `{}`) sur une valeur en place | **ignoré** : la valeur reste, le relevé `valeurs_ignorees` le dit ; si c'était tout ce que l'écriture posait, l'appel est **refusé** en nommant `null` |
| `{"champ": {"comment": C}}` | comment posé ; valeur et autres couches intactes |
| `{"champ": {"valeur": Y_identique, "comment": C}}` | comment posé, rien ne tombe |
| `{"champ": {"origine": null}}` | origine effacée ; la colonne redevient plate |
| champ non nommé | intact |

Ne pas nommer un champ le laisse intact ; le nommer avec `null` l'efface — un `null`
glissé dans un gabarit à moitié rempli détruit une valeur en place.

## 4. `origine: "system"` — quand la plateforme pose l'origine

Déclaré au schéma (`{"key": "adresse", "origine": "system"}`), ce format fait garder la
valeur d'avant dans `adresse.origine`. Le filet est plus étroit qu'il n'y paraît :

- la capture a lieu **une seule fois**, à la **première écriture qui change la valeur
  après la déclaration** du format — pas à la pose du schéma, pas à la création de la
  ligne, jamais réécrite ensuite ; une valeur identique ne capture rien ;
- un format déclaré **tard** capture ce que le dernier écrivain a laissé — possiblement
  la valeur d'un autre agent, présentée sous le nom `origine` ;
- un champ vide au départ reçoit un marqueur vide, que la lecture ne sert pas ;
- une colonne **sans** ce format ne garde rien : écraser est définitif ;
- écrire soi-même une autre `origine` sur une telle colonne est **refusé** (création
  comprise) ; la valeur que le système poserait est acceptée (no-op).

La face d'appel n'y change rien : ligne créée par `data_write` ou par `POST …/rows`,
même comportement.

## 5. Ce que `readonly: true` protège — et ne protège pas

Une colonne `readonly` (schéma) verrouille la **valeur** d'une ligne en place : une
écriture qui la **change** (valeur nue, `null`, ou `{"valeur": …}`) est refusée en
nommant la colonne et où va la chose — `champ.comment`, qui reste ouvert. Une valeur
identique passe (no-op) ; `comment`, `link` et `origine` (sauf si le système la pose)
restent écrivables.

Ce que le cran ne ferme **pas** : la **création** d'une ligne — rien n'est écrasé ; un
tableau qui ne doit pas grossir se ferme par `key_required`. La colonne-clé ne peut pas
être `readonly` : c'est `key_required` qui la protège.

Pour remplacer quand même : `readonly_override=true` **sur l'appel** (argument de
`data_write` ; paramètre de query sur `POST`/`PATCH …/rows`). Réservé au propriétaire
du tableau ou à qui le gouverne — un tableau seulement partagé en écriture est refusé —
et chaque remplacement forcé est journalisé (ligne, colonne, valeur remplacée).

## 6. Ce que la clé métier fait à l'écriture

Quand le schéma déclare une `key` (`data_set_schema`), une écriture qui porte une valeur
de clé **déjà présente fusionne sur cette ligne** (upsert, retour de son `_id`) au lieu
d'en créer une — ligne seule comme lot ; un index unique le garantit. Une ligne créée
sans valeur de clé est créée quand même et la réponse le signale (`notices`) : aucune
écriture ultérieure ne la retrouvera par sa clé.

`key_required: true` ferme le tableau : une écriture qui ne désigne aucune ligne
existante — ni `id`, ni valeur de clé déjà portée ; une clé simplement **nouvelle**
compte comme inconnue — est **refusée** (`business_key_required`) au lieu de créer.
Ouvrir, écrire, refermer : `data_patch_schema(key_required=false)` puis `…=true`.

Un **lot** (`data_write(rows=[…])`, `oto_upload_url`) n'est pas atomique : il s'arrête à
la première ligne refusée, les précédentes restent écrites, le refus nomme la ligne et
dit combien ont atterri. `key=` sur le lot dédoublonne sur une autre colonne que la clé
déclarée ; sur une ligne seule, seule la clé déclarée joue.

## 7. Deux faces, un seul stockage

| geste | MCP (`data_*`) | REST (`/api/datastore/namespaces` = `NS`) |
|---|---|---|
| tableaux | `data_list_namespaces`, `data_create_namespace`, `data_rename_namespace`, `data_delete_namespace`, `data_url` | `GET`/`POST NS` ; `PATCH`/`DELETE NS/{tableau}` ; `GET NS/{tableau}/url` |
| lignes | `data_rows` (page, ou `id`), `data_write`, `data_delete_row` | `GET`/`POST NS/{tableau}/rows` ; `GET`/`PATCH`/`DELETE …/rows/{row_id}` |
| schéma | `data_get_schema`, `data_set_schema`, `data_patch_schema`, `data_drop_column` | `GET`/`PUT`/`PATCH …/schema` ; `POST …/drop_column` |
| file de travail | `data_claim_next`, `data_release` | `POST …/claim_next` ; `POST …/rows/{row_id}/claim` ; `POST …/rows/{row_id}/release` ; `GET …/queue` |
| agrégat | `data_aggregate` | `GET …/aggregate` |
| partage | `data_share` | `GET`/`POST`/`DELETE …/share` |
| activité | — | `GET …/activity` ; `GET …/rows/{row_id}/activity` |

`{tableau}` est le nom du tableau ; `slot:<nom>` et `@claimed` sont compris des deux
côtés. Le descriptif complet (entrées, réponses, codes) est `GET /api/openapi.json`,
sans auth ; la face REST s'appelle avec le même jeton que `/mcp`, ou un jeton API.

**Identique** sur les deux faces : le stockage, les couches à la lecture (clés plates),
la clé métier, `readonly`, les refus de schéma — une ligne créée d'un côté se lit de
l'autre, à l'identique.

**Diverge** :

- **Lot.** `POST …/rows` écrit **une** ligne : le corps **est** la ligne. Un corps à
  clé unique dont la valeur est une liste d'objets (`{"rows": [...]}`, `{"data":
  [...]}`) est refusé `400 batch_body`, rien n'est écrit — sauf si une colonne de ce
  nom est déclarée au schéma. Un corps qui est une liste JSON est refusé `400
  invalid_body`. Le lot passe par `data_write(rows=[…])` ou `oto_upload_url`.
- **Projection.** `fields` n'existe que sur `data_rows` ; REST rend la ligne entière.
- **Paramètres inconnus.** REST refuse tout paramètre de query ou de chemin qu'il ne
  connaît pas — 400 `unknown_fields`, qui nomme le champ et les attendus. Le corps de
  `POST`/`PATCH …/rows` est libre : ce sont les colonnes.
- **Pagination.** `data_rows` : `limit` (100) + `cursor` → `{rows, count, next_cursor}`.
  REST : `offset` + `limit` (50, max 500) → `{rows, total, offset, limit}`.
- **Filtres REST** : `filter`, `filters`, `metrics` sont du JSON **dans une chaîne** de
  query (`?filters=[{"field":…}]`), envoyée une seule fois.
- **`group_by`.** Une chaîne `"a,b"` est refusée sur les deux faces : le croisement
  n'existe pas. La forme **liste** `["a", "b"]` n'existe que sur `data_aggregate`, et
  elle **fusionne** les valeurs des champs sous une même clé, elle ne croise pas ;
  REST prend une colonne.
- **`key=` du lot** : MCP seulement ; REST joue toujours la clé déclarée.
- **Refus.** MCP : erreur `INVALID_PARAMS` qui porte le message. REST : 400 nommé
  (`row_invalid`, `business_key_required`, `invalid_row_input`, `jeton_mal_place`,
  `invalid_filters`…), 403 `namespace_read_only` (tableau partagé en lecture seule),
  404 `namespace_not_found` (avec l'org où il vit, s'il existe dans une autre des
  tiennes) ou `row_not_found`.

## 8. Ce qu'une réponse ne contient pas

Un succès n'est pas un accusé de ce que tu crois avoir fait ; lis ce qui manque.

- **La liste REST ne sait pas dire « il en reste ».** `{rows, total, offset, limit}`
  sans curseur : la fin se calcule (`offset + len(rows) >= total`). Un `offset` au-delà
  du total rend `rows: []` en 200 — la même réponse qu'un tableau vide ; une ligne
  supprimée entre deux pages décale les suivantes sans un mot. Sur `data_rows`,
  `next_cursor: null` est le seul signal de fin, et un curseur périmé est refusé.
- **`data_rows(fields=[…])` ne dit pas qu'une colonne est vide.** Une colonne
  déclarée au schéma mais renseignée nulle part rend des lignes `{_id}` **sans
  avertissement** — c'est voulu, pour ne pas accuser une faute d'orthographe qui n'en
  est pas ; le `warning` ne vient que pour un nom inconnu partout.
- **Un `200`/`201` d'écriture ne porte que ce qui a dévié.** `hors_schema`,
  `hors_options`, `valeurs_effacees`, `valeurs_ignorees`, `notices` sont absents quand
  tout est dans le format : leur absence est la réponse normale, leur présence est ce
  qu'il faut lire.
- **Un agrégat sur un champ absent rend un groupe de clé `null`**, pas une erreur : un
  seul groupe contenant tout est le signe d'un nom de colonne faux, pas d'une donnée
  vide.
