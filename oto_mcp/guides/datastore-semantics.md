---
title: Sémantique du datastore — couches, faces, clé métier
description: valeur/comment/link/origine, ce qu'une écriture détruit, ce que readonly et la clé métier protègent, ce qui diverge entre data_* et REST /api/datastore, et ce qu'une réponse ne contient pas
---

# Sémantique du datastore

À lire **avant** d'écrire dans un tableau que tu n'as pas rempli toi-même, ou d'appeler
la face REST (`/api/datastore/…`) à la place des outils `data_*`. Les deux faces parlent
au même stockage : ce guide dit ce que ce stockage fait d'une écriture, où les deux
faces divergent, et ce qu'une réponse ne dit pas.

## 0. Adresser un tableau : par son NUMÉRO

Un tableau porte un **numéro** (`ns_id`, ex. `174`) et un **nom** (`edition-vivier`).
Les deux résolvent, partout, avec le même contrôle de visibilité — `namespace: 174` et
`namespace: "edition-vivier"` désignent le même tableau, sur les deux faces.

**Emploie le numéro.** Le nom est en cours de retrait : il marche encore aujourd'hui, et
rien n'est cassé, mais il n'est unique que par propriétaire, il change au renommage, et
c'est le numéro que la plateforme enregistre.

Où le trouver : `data_list_namespaces` le donne (`id`), et surtout **les réponses le
rendent** — `ns_id` dans la réservation (`data_claim_next`), l'écriture (`data_write`),
la libération (`data_release`), la lecture d'une page (`data_rows`) et la lecture du
schéma (`data_get_schema`). Réserve, note le `ns_id`, adresse par lui ensuite.

⚠️ La clé `namespace` d'une réponse est le **nom canonique** du tableau, jamais l'écho de
ce que tu as envoyé : adresser `174` te répond `namespace: "edition-vivier"`, et non
`"174"`. C'est ainsi qu'on lit *quel* tableau a été touché. (`data_write` sur une ligne
seule fait exception et ne rend que `ns_id` : son corps **est** la ligne, une clé
`namespace` y entrerait en collision avec une colonne.)

`slot:<nom>` reste compris des deux côtés : c'est une référence de projet, pas un nom de
tableau, et elle se résout vers l'un comme vers l'autre.

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

Les deux formes sont **asymétriques** (imbriqué à l'écriture, plat à la lecture par
défaut). Le paramètre `layers` de `data_rows`, `GET …/rows` et `GET …/rows/{row_id}`
lève l'asymétrie : `flat` (défaut) = ce qui précède ; `nested` = `adresse` revient
`{"valeur": …, "comment": …}` — `valeur` toujours, les couches renseignées seulement,
la forme dans laquelle on écrit ; une cellule sans couche reste le même scalaire, une
colonne-liste applique la règle dans ses items. Toute autre valeur est refusée en
nommant le paramètre. En `nested`, `fields` nomme des colonnes (`adresse.comment`
n'existe qu'en `flat`). **Le défaut basculera vers `nested`, avec préavis daté** :
nomme `layers` dès maintenant si tu dépends d'une forme.

Dans les deux formes l'aller-retour tient : une clé plate `adresse.comment` réémise à l'écriture est
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

**Deux écritures sur des colonnes différentes ne s'écrasent jamais**, même parties au
même instant. **Seulement quand ce que tu écris a été CALCULÉ d'après une ligne lue**
(une liste ou un texte que tu renvoies en entier, un statut choisi d'après le statut en
place) : passe la `_revision` de cette lecture — `data_write(id=…, expected_revision=…)`,
ou `PATCH …/rows/{row_id}?expected_revision=…` (en paramètre de requête, jamais dans le
corps). Si la ligne a changé depuis — n'importe quelle colonne, ou sa réservation —,
rien n'est écrit et l'appel est refusé avec `revision_conflict` et la révision actuelle
(REST : `409`, `details.current_revision`) : relis, recalcule, réécris. Sinon, ne la
passe pas.

## 4. ⚠️ `origine: "system"` est SUPPRIMÉ

Ce cran armait une capture automatique : à la première écriture qui changeait une
valeur, la plateforme figeait la précédente comme origine. **Il n'existe plus** (08/09/2026).

Ce qui le remplace : **`donnees_d_origine`** (section suivante), qui fige la version
d'origine **au moment où la valeur entre**. Un geste déclaré, au lieu d'un filet qui
dépendait de l'ordre dans lequel on déclarait le cran et importait les données — c'est
cet ordre inversé qui avait produit 837 cellules « origine inconnue » sur un tableau de
production.

⚠️ **Ce que le retrait change pour toi, et il faut le savoir** : plus rien ne capture
automatiquement. Une valeur qu'un agent écrase n'est plus retenue par personne. Si tu
veux garder ce que la cliente a remis, **déclare-le à l'import**.

⚠️ **Les couches `origine` déjà en base ne bougent pas** — 28 799 cases en portent une
au moment du retrait. Elles restent lues, servies et jamais réécrites. C'est le
mécanisme qui part, pas la donnée.

Une déclaration `origine: "system"` qui subsiste dans un schéma est désormais une clé
qu'oto n'interprète pas : elle est stockée, servie, et sans aucun effet. L'avertissement
des clés non interprétées la signale.

## 4 bis. `donnees_d_origine: true` — quand TU apportes la donnée de la cliente

Le format ci-dessus est un filet : il rattrape la valeur d'avant quand quelqu'un
écrase. Ce paramètre-ci est l'inverse — **un geste, pas un filet**. Tu déclares que cet
appel apporte la donnée **telle que la cliente l'a remise**, et chaque case fige sa
version d'origine au moment où la valeur entre.

```
data_write(namespace="…", key="siren", donnees_d_origine=True,
           rows=[{"siren": "123456789",
                  "raison_sociale": {"valeur": "DUPONT",
                                     "comment": "fichier de la cliente du 05/08/2026"}}])
```

**La provenance va dans `comment`** — il n'y a pas de paramètre séparé pour ça. Les
couches que tu écris atterrissent dans les deux versions, parce qu'elles décrivent le
même fait le jour de l'import.

⚠️ **Réserve-le à l'IMPORT, jamais à l'enrichissement.** Ce qu'un agent établit est la
version **courante**. Marquer d'origine ta propre trouvaille présenterait ton travail
comme la donnée de la cliente — exactement ce que la définition de l'origine interdit,
et le genre d'erreur qu'on ne découvre qu'à la restitution, devant elle.

Trois règles qui te dispensent de précautions :

- une **origine déjà posée n'est jamais réécrite**. Un ré-import du même fichier met à
  jour la version courante et laisse l'origine du premier — tu peux rejouer un import
  sans rien détruire ;
- une **case vide ne reçoit rien**. « La cliente n'a rien remis » et « la cliente a
  remis du vide » sont deux faits différents ; `0` et `false`, eux, sont des valeurs
  remises et gardent leur origine ;
- le **défaut ne change pas**. Sans ce paramètre, ton écriture est ordinaire et vise la
  version courante, comme avant.

**Pourquoi ce paramètre existe** : avant lui, avoir une origine dépendait d'un ORDRE DE
GESTES — il fallait que `origine: "system"` ait été déclaré **avant** que la ligne
n'existe, sans quoi la capture n'avait plus rien à figer. ⚠️ **Ce silence a coûté 837
cellules sur 846** sur un tableau de campagne : cran déclaré après coup, valeurs de la
cliente déjà écrasées par des agents, et un balayage qui n'a pu poser que
`(origine inconnue)`. Un geste explicite ne se trompe pas d'ordre.

**Sur un import en volume** (`oto_upload_url` → `PUT /api/upload/{token}`), déclare-le
**au mint**, avec le reste : le `PUT` signé ne porte aucun paramètre, donc celui qui
livre les octets ne peut pas décider que son fichier est la donnée de la cliente.

## 4 ter. `versions` — quelles versions tu veux LIRE

Une case existe en deux versions : `current`, ce qu'on a établi, et `origine`, ce que
la cliente a remis. Une écriture vise toujours la courante et n'a pas à le dire ; une
lecture, elle, nomme ce qu'elle veut.

```
data_rows(namespace="…", versions=["current", "origine"])
```

⚠️ **Demande les DEUX dans le MÊME appel quand tu les compares.** Deux appels ne sont
pas atomiques : une écriture entre les deux te ferait comparer l'avant d'un état à
l'après d'un autre, et tu annoncerais « corrigé » sur une ligne que personne n'a
touchée.

⚠️ **Le nom NU porte toujours la version courante**, quelle que soit ta demande.
`versions` décide seulement de ce qui s'AJOUTE à côté (`champ.origine` et ses
sous-champs). Faire porter deux sens à `champ` selon un paramètre serait un piège, pas
une commodité.

**La réponse déclare ce qu'elle a servi**, dans `versions_servies`. C'est ce qui rend
discernables « je ne l'ai pas demandée » et « cette case n'en a pas » — sans quoi tu
réinventerais un marqueur, en pire, puisque cette fois tu l'aurais deviné.

Le défaut sert encore les deux. **Il basculera vers `current` seul, avec préavis daté**
— si un écran chez toi lit la valeur de départ, nomme-la dès maintenant.

## 4 quater. `force` — forcer ce qu'on NOMME, pas tout l'appel

```
data_write(namespace="…", id="…", force=["raison_sociale", "raison_sociale.origine"],
           row={"raison_sociale": {"valeur": "Dupont SAS"}})
```

Le nommer suffit : pas besoin de `readonly_override` en plus. Un chemin est une colonne
ou l'une de ses couches.

⚠️ **Ça change la PORTÉE, pas le droit.** Forcer reste réservé au propriétaire du
tableau ou à qui le gouverne — un accès en écriture partagé ne suffit pas, sinon le
verrou ne protégerait de personne. Ce que nommer tes cibles change, c'est qu'un lot de
cinq cents lignes cesse de forcer tout ce qu'il transporte.

Une colonne verrouillée absente de ta liste est refusée normalement, **et le refus te
dit que c'est ta liste qui ne la nomme pas** — pas que tu manques d'un droit. La
distinction compte : sans elle, tu partirais chercher une permission que tu as déjà.

## 4 quinquies. ⚠️ `null` va cesser d'effacer — au 1er décembre 2026

Aujourd'hui `{"champ": null}` EFFACE la valeur. Partout ailleurs — un schéma, une
réponse, ton propre JSON — `null` veut dire « pas de valeur ». Le même jeton dit donc
une chose et son contraire selon l'endroit, et c'est ce qu'on retire.

| ce que tu veux | ce que tu écris |
|---|---|
| vider délibérément | `{"champ": {"valeur": "@empty"}}` |
| ne pas y toucher | **omets le champ** (ou `@keep`) |

⚠️ **`@keep` et `@empty` doivent être la valeur ENTIÈRE du sous-champ, seuls.** Mélangés
à une phrase, ce ne sont plus que du texte, stocké tel quel — `"@keep ; trouvé sur les
mentions légales"` atterrit dans la case, et une cliente le lit dans son livrable. Pour
garder ce qui est là ET ajouter quelque chose, il faut choisir : garder, ou remplacer.
Les deux ne s'écrivent pas dans la même chaîne.

⚠️ **Le mot ne mord qu'au mot entier, et c'est délibéré** : une sentinelle qui
reconnaîtrait `@keep` au milieu d'un texte effacerait ou figerait une valeur sur la foi
d'une sous-chaîne — `contact@keepcool.fr` en ferait les frais. Mieux vaut servir une
chaîne visible qu'exécuter une intention devinée.

⚠️ **Si `null` voulait dire « cherché, rien trouvé » chez toi — c'est l'usage le plus
courant — alors le geste juste est l'OMISSION, pas `@empty`.** Ne rien trouver n'est
pas effacer. Traduire mécaniquement tes `null` en `@empty` détruirait des valeurs que
tu voulais seulement laisser en place.

Jusqu'à la date, `null` efface encore et la réponse porte un avertissement. Après, il
est **refusé** — jamais interprété en silence, parce qu'un `null` traduit « pour rendre
service » ferait exactement le dégât qu'on cherche à empêcher.

## 4 sexies. Le `lifecycle` DÉSIGNE la colonne d'état

```json
{"key": "statut", "type": "enum", "lifecycle": {"states": [...], "terminal": [...]}}
```

**Rien d'autre à déclarer.** La colonne d'état est celle qui porte le bloc — pas
d'étiquette `role: "status"`, pas de clé de schéma à faire correspondre.

⚠️ **Ce que ça supprime** : il est désormais IMPOSSIBLE de poser un cycle de vie qui ne
s'applique pas. Avant, un `lifecycle` sur une colonne non étiquetée était stocké,
servi… et jamais lu — cinq tableaux étaient dans ce cas, dont quatre en production, et
leurs auteurs croyaient avoir armé une file de travail.

Deux colonnes qui porteraient un bloc sont **refusées à la pose** : sinon le premier
trouvé gagnerait, et l'ordre de déclaration trancherait en silence.

⚠️ **Et si aucune colonne n'en porte, le tableau n'a PAS de file** : `data_claim_next`
n'y réservera jamais rien. Une colonne avec ses `options`, ou l'ancienne étiquette,
ressemble à un état sans en être un — la réponse te le dit plutôt que de te laisser
conclure de son silence.

## 5. Ce que `readonly: true` protège## 5. Ce que `readonly: true` protège — et ne protège pas

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

`{tableau}` est le **numéro** du tableau (son nom marche encore, en cours de retrait —
cf. §0) ; `slot:<nom>` est compris des deux côtés. Le
descriptif complet (entrées, réponses, codes) est `GET /api/openapi.json`, sans auth ;
la face REST s'appelle avec le même jeton que `/mcp`, ou un jeton API.

**Identique** sur les deux faces : le stockage, les couches à la lecture (`layers`),
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
  tiennes) ou `row_not_found` ; 409 `row_locked` (ligne réservée par un autre) ou
  `revision_conflict` (la ligne a changé depuis la `_revision` passée).

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
- **Une réponse ne dit pas le tableau qu'elle n'a pas résolu.** `ns_id` est présent
  dès que le tableau a été atteint ; un `ns_id: null` signale un chemin qui a rendu
  sans résoudre, pas un tableau sans numéro.
- **Un `200`/`201` d'écriture ne porte que ce qui a dévié.** `hors_schema`,
  `hors_options`, `valeurs_effacees`, `valeurs_ignorees`, `notices` sont absents quand
  tout est dans le format : leur absence est la réponse normale, leur présence est ce
  qu'il faut lire.
- **Un agrégat sur un champ absent rend un groupe de clé `null`**, pas une erreur : un
  seul groupe contenant tout est le signe d'un nom de colonne faux, pas d'une donnée
  vide.
