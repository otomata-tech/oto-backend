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
Jusqu'au 08/11/2026, les deux résolvent, avec le même contrôle de visibilité —
`datastore: 174` et `datastore: "edition-vivier"` désignent le même tableau.

**Emploie le numéro.** Un nom de tableau sera **REFUSÉ à partir du 08/11/2026** : il
n'est unique que par propriétaire, il change au renommage, et c'est le numéro que la
plateforme enregistre. D'ici là, chaque réponse obtenue par un nom le dit en tête, avec
le numéro à passer.

Où le trouver : `data_list_datastores` le donne sous **les deux noms** (`ns_id`, et `id`
— le même nombre, gardé pour les liens du tableau de bord), la création et le renommage
aussi, et surtout **les réponses le
rendent** — `ns_id` dans la réservation (`data_claim_next`), l'écriture (`data_write`),
la libération (`data_release`), la lecture d'une page (`data_rows`) et la lecture du
schéma (`data_get_schema`). Réserve, note le `ns_id`, adresse par lui ensuite.

⚠️ La clé `datastore` d'une réponse est le **nom canonique** du tableau, jamais l'écho de
ce que tu as envoyé : adresser `174` te répond `datastore: "edition-vivier"`, et non
`"174"`. C'est ainsi qu'on lit *quel* tableau a été touché. (`data_write` sur une ligne
seule fait exception et ne rend que `ns_id` : son corps **est** la ligne, une clé
`datastore` y entrerait en collision avec une colonne.)

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

## 2 bis. Un élément de liste s'écrit à son RANG

Pour toucher UN élément d'une colonne-liste, tu n'as pas à renvoyer la liste : écris à
l'adresse que tu lis — la même que dans `filters` et `group_by`.

| ce que tu veux | ce que tu écris |
|---|---|
| modifier un attribut | `{"contacts[1].email": "d@x.fr"}` — ou `{"valeur": …, "comment": …}` |
| annoter un attribut sans le réécrire | `{"contacts[1].email.comment": "site officiel"}` |
| effacer un attribut | `{"contacts[1].email": null}` |
| ajouter un élément en fin de liste | `{"contacts[+]": {"nom": "Cy", "email": "c@x.fr"}}` — une fiche complète |
| supprimer un élément | `{"contacts[0]": null}` |

L'attribut suit la règle d'une colonne (§3) : `comment`/`link` tombent avec une valeur
qui change, l'origine reste, les autres attributs et les autres éléments ne bougent pas.
Le rang part de 0 et **désigne la liste telle que tu l'as lue** : dans un même appel,
`{"contacts[0]": null, "contacts[2].email": …}` vise le troisième élément lu ; l'ajout
se fait en dernier. Supprimer le dernier élément efface la colonne. Seul l'élément que
tu écris est jugé par le schéma.

Refusé, avec la forme qui marche : un rang qui n'existe pas (« `contacts` a 2 éléments ;
rang 5 inexistant ; pour ajouter : `contacts[+]` »), `{"contacts[0]": {…}}` (écris ses
attributs), `contacts[].email` (adresse de lecture), `contacts[+].email` (un élément
s'ajoute entier), `contacts[role=DAF].email` (vise le rang), et la colonne entière avec
l'un de ses rangs dans le même appel.

## 3. Ce qu'une écriture fait — et détruit

Une écriture ne touche **que ce qu'elle nomme**. Sur une colonne ouverte il n'y a pas
d'annulation : la valeur précédente quitte la ligne quand la tienne arrive. Elle ne
survit que dans le journal des révisions — `data_row_history` rend l'avant et l'après
de chaque écriture, 90 jours par défaut — et rien ne la remet en place pour toi.

| tu écris | effet |
|---|---|
| `{"champ": Y}` ou `{"champ": {"valeur": Y}}` | valeur remplacée ; `origine` intacte ; `comment` et `link` **tombent** (ils décrivaient l'ancienne valeur) |
| `{"champ": Y}` avec Y identique à la valeur en place | **no-op** : toutes les couches restent |
| `{"champ": null}` | valeur effacée ; une `origine` pleine survit ; l'effacement revient dans `valeurs_effacees` (champ, ligne, valeur perdue) |
| `{"champ": ""}` ou `{"champ": []}` sur une valeur en place | **à partir du 6 octobre 2026** : valeur **remplacée**, comme par n'importe quelle valeur ; la valeur remplacée revient dans `valeurs_effacees`. Avant cette date : ignoré (ci-dessous) |
| `{"champ": {}}` sur une valeur en place (et, avant le 6 octobre 2026, `""` ou `[]`) | **ignoré** : la valeur reste, le relevé `valeurs_ignorees` le dit ; si c'était tout ce que l'écriture posait, l'appel est **refusé** en nommant `null` |
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

**Supprimer et libérer prennent la même précondition** — `data_delete_row(id=…,
expected_revision=…)`, `DELETE …/rows/{row_id}?expected_revision=…`, et
`POST …/rows/{row_id}/release` — quand c'est une ligne LUE qui t'a fait décider. Une
suppression sur un état périmé emporte la modification qu'un autre venait d'y poser ; une
libération sur un état périmé retire le bail que quelqu'un d'autre a repris depuis. Même
refus, même conduite : relis, décide de nouveau, rejoue. Supprimer une ligne réservée par
un autre travail est refusé (`row_locked`), comme l'écrire.

**Plusieurs lignes à supprimer = UN appel** : `data_delete_row(ids=["r1", {"id": "r2",
"expected_revision": "3"}, …])` (500 au plus). Chaque ligne garde sa précondition ; une
ligne refusée n'arrête pas les autres et revient dans `refused` avec sa raison (et
`current_revision` sur un conflit) ; une ligne déjà absente revient dans `not_found`.
Un élément mal formé refuse tout l'appel, avant la moindre suppression.

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
mécanisme qui part, pas la donnée. Une seule exception : le texte « origine inconnue »
que le balayage posait À LA PLACE d'une valeur a été retiré des cases qui le portaient.
**Une case sans couche `origine` n'a pas de valeur de départ connue** : c'est
l'absence de la couche qui le dit, jamais un texte à sa place.

Une déclaration `origine` est **refusée** depuis le 01/10/2026, comme toute clé que son
niveau n'admet pas (§ 4 octies) ; celles qui subsistaient sont retirées par la migration
de la fermeture.

## 4 bis. `donnees_d_origine: true` — quand TU apportes la donnée de la cliente

Le format ci-dessus est un filet : il rattrape la valeur d'avant quand quelqu'un
écrase. Ce paramètre-ci est l'inverse — **un geste, pas un filet**. Tu déclares que cet
appel apporte la donnée **telle que la cliente l'a remise**, et chaque case fige sa
version d'origine au moment où la valeur entre.

```
data_write(datastore="…", key="siren", donnees_d_origine=True,
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

- une **origine déjà posée n'est jamais réécrite**. Un ré-import du même fichier (avec
  `key=`, qui désigne les lignes, cf. §6) met à jour la version courante et laisse
  l'origine du premier — tu peux rejouer un import sans rien détruire ;
- une **case vide ne reçoit rien**. « La cliente n'a rien remis » et « la cliente a
  remis du vide » sont deux faits différents ; `0` et `false`, eux, sont des valeurs
  remises et gardent leur origine ;
- le **défaut ne change pas**. Sans ce paramètre, ton écriture est ordinaire et vise la
  version courante, comme avant.

**Pourquoi ce paramètre existe** : avant lui, avoir une origine dépendait d'un ORDRE DE
GESTES — il fallait que `origine: "system"` ait été déclaré **avant** que la ligne
n'existe, sans quoi la capture n'avait plus rien à figer. ⚠️ **Ce silence a coûté 837
cellules sur 846** sur un tableau de campagne : cran déclaré après coup, valeurs de la
cliente déjà écrasées par des agents, et un balayage qui n'a pu poser qu'un aveu
d'ignorance, depuis retiré. Un geste explicite ne se trompe pas d'ordre.

**Sur un import en volume** (`oto_upload_url` → `PUT /api/upload/{token}`), déclare-le
**au mint**, avec le reste : le `PUT` signé ne porte aucun paramètre, donc celui qui
livre les octets ne peut pas décider que son fichier est la donnée de la cliente.

## 4 ter. `versions` — quelles versions tu veux LIRE

Une case existe en deux versions : `current`, ce qu'on a établi, et `origine`, ce que
la cliente a remis. Une écriture vise toujours la courante et n'a pas à le dire ; une
lecture, elle, nomme ce qu'elle veut.

```
data_rows(datastore="…", versions=["current", "origine"])   # sans `versions` : current seul
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

**Par défaut, la valeur actuelle seule** : l'origine (la première version de la donnée)
se demande avec `versions`.

## 4 quater. `force` — forcer ce qu'on NOMME, pas tout l'appel

```
data_write(datastore="…", id="…", force=["raison_sociale", "raison_sociale.origine"],
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

## 4 quinquies. Deux gestes sur une case : `null` efface, `@empty` dit « cherché, rien »

| ce que tu veux | ce que tu écris |
|---|---|
| effacer la case | `{"champ": null}` — la valeur perdue revient une fois dans `valeurs_effacees` |
| dire « cherché, rien » | `{"champ": {"valeur": "@empty", "comment": "registre et mentions légales : rien"}}` — il satisfait un champ requis |
| ne pas y toucher | **omets le champ** |

`@empty` vaut aussi sur le champ d'un élément de liste (`{"contacts": [{"nom": …,
"fonction": "@empty"}]}`). Posé sur une couche (`comment`, `link`), il ne vide que cette
couche. Un vide assumé se relit `""` par défaut, et `"@empty"` avec `empties=sentinel` :
c'est la lecture à faire avant de renvoyer une liste sans `of.key`, qui se remplace
entière. Il est refusé sur l'identité d'un élément (`of.key`), dans une liste de valeurs
et dans un objet.

⚠️ **`@empty` doit être la valeur ENTIÈRE du sous-champ, seul.** Mélangé à une phrase, ce
n'est plus que du texte, stocké tel quel — `"@empty ; rien sur les mentions légales"`
atterrit dans la case, et une cliente le lit dans son livrable. La raison va dans
`comment`. Le mot ne mord qu'au mot entier, et c'est délibéré : une sentinelle reconnue
au milieu d'un texte viderait une valeur sur la foi d'une sous-chaîne.

⚠️ **Sur une valeur en place, `null` comme `@empty` la retirent**, et elle n'est gardée
nulle part (la réponse la rend une fois, dans `valeurs_effacees`) ; seule la version
remise par la cliente, si elle a été posée à l'import, reste lisible dans
`champ.origine` (`versions`, 4 ter). **Omettre le champ veut dire « pas à moi »** (la
valeur est gardée), et une couche `comment` seule ne dit pas « cherché, rien ».

⚠️ **`@keep` et `@clear` sont dépréciés, REFUSÉS à partir du 8 octobre 2026** : une
écriture qui les porte, où que ce soit (valeur, couche, élément de liste, ligne d'un
lot), est alors refusée ENTIÈRE, et le refus nomme les colonnes et le geste à faire.
Avant cette date, elle réussit et la réponse porte un avertissement daté dans
`notices`. À la place de `@clear`, écris `null`. À la place de `@keep`, omets le
sous-champ — ou, pour un `comment` ou un `link` qui doit survivre à une valeur qui
change, renvoie-le tel quel : écrire une valeur fait tomber le `comment` et le `link`
qui l'accompagnaient.

⚠️ **`""` et `[]` sont des valeurs : ils REMPLACENT la valeur en place à partir du
6 octobre 2026**, et la valeur remplacée revient dans `valeurs_effacees`. Avant cette
date, un `""` ou un `[]` sur une case qui porte une valeur est ignoré (et refusé comme
écriture sans effet quand il est tout le geste), et la réponse porte un avertissement
daté dans `notices`. Un `""` posé ne satisfait pas `required` et compte comme vide :
pour dire « cherché, rien », c'est `@empty`. Pour garder une valeur, omets la colonne ;
pour l'effacer, écris `null`. `{}` n'est pas une valeur et ne sera jamais stocké.

`null` efface aussi une case au vide assumé (`@empty`), marqueur compris : c'est le
geste qui remplace `@clear` pour la démarquer.

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

**Le nom affiché d'une étape** se déclare dans le même bloc :
`"labels": {"a_qualifier": "À qualifier", "perdu": "Perdu"}`. C'est de la
présentation : une ligne s'écrit toujours avec le CODE (`"statut": "a_qualifier"`),
jamais avec le libellé. Chaque clé doit être un état de `states` (une clé inconnue est
refusée, et nommée), chaque valeur une chaîne non vide d'au plus 60 caractères ; un
état sans libellé est permis. Pour nommer une étape sur un tableau existant :
`data_patch_schema(fields=[{"key": "statut", "lifecycle": {"labels": {"perdu":
"Perdu"}}}])` — les autres libellés et le reste du cycle de vie ne bougent pas.

Deux colonnes qui porteraient un bloc sont **refusées à la pose** : sinon le premier
trouvé gagnerait, et l'ordre de déclaration trancherait en silence.

⚠️ **Et si aucune colonne n'en porte, le tableau n'a PAS de cycle de vie** — mais il a
toujours une file : `data_claim_next` réserve sans rien déclarer. Ce qui manque, ce
sont les gardes (transitions, plafond de reprises, état d'abandon, périmètre de
réservation). Une colonne avec ses `options`, ou l'ancienne étiquette, ressemble à un
état sans en être un — la réponse te le dit plutôt que de te laisser conclure de son
silence.

## 4 septies. Une exigence déclarée s'applique, à toute profondeur

La validation d'un tableau s'arme dès que son schéma déclare une exigence : `strict`,
ou `required`, `required_when`, `max_length`, `max_items` sur une colonne, un
sous-champ d'objet ou l'attribut d'un élément de liste — et `options` dans un
sous-champ. « Chaque contact porte un nom » se déclare sur `of.fields`, et il est
tenu. Seules les `options` d'une colonne de premier niveau restent indicatives hors
`strict` ; la réponse le dit.

Deux formes qui ne s'appliqueraient pas sont **refusées à la pose**, avec la bonne :
`max_items` se pose sur la liste (`{"type": "list", "max_items": 3, "of": {…}}`),
jamais dans `of` ; des sous-champs (`fields`, `of`) exigent le `type` de leur colonne
(`object` ou `list`).

Dans une liste à `of.key`, seuls les éléments que ton écriture **change** sont jugés —
et, dans toute liste, seuls ceux que tu écris par leur rang (§2 bis).
Un élément renvoyé tel quel, qui ne respectait pas une exigence posée après lui, ne
bloque pas ton écriture : il est signalé dans `hors_type`, à corriger quand tu y
reviens.

## 4 octies. Le vocabulaire d'un schéma est FERMÉ

Depuis le 01/10/2026, chaque niveau d'un schéma déclare les clés qu'il admet — la
**tête**, une **colonne**, un **sous-champ** (`fields` d'un objet ou d'un élément), l'**élément
d'une liste** (`of`) et le bloc **`lifecycle`** — et une clé absente de son niveau est
**refusée**, à la pose (`data_set_schema`, `PUT …/schema`) comme au patch
(`data_patch_schema`, `PATCH …/schema`). Les listes, et qui lit chaque clé, sont servies
sur `GET /api/datastore/schema/keys` (`levels`).

Le refus nomme le chemin, la clé, et où elle va :

```
fields.statut : `enum` n'est pas admise sur une colonne — voulais-tu `options` ?
fields.etat : `states` n'est pas admise sur une colonne — elle se pose DANS le bloc `lifecycle` de la colonne (`lifecycle.states`) …
tête : `semantic_search` n'est pas admise en tête du schéma — c'est un PARAMÈTRE de l'appel …
```

- **un texte d'aide** va dans `description` — le seul : `note`, `help`, `hint` et
  `placeholder` y ont été repliés et sont refusés ;
- **une annotation à toi** (une dépendance, un libellé de valeur, une provenance du
  format) va dans `meta` : un objet, admis à chaque niveau, transporté tel quel,
  **jamais lu**, borné en taille (`meta_max_bytes`). Rien de ce qui est dans `meta` ne
  devient actif — un `readonly` rangé là ne verrouille rien ;
- **une clé déjà stockée** et inchangée ne bloque rien : le refus porte sur ce que le
  geste POSE ou MODIFIE. Le patch d'une autre colonne passe ; la réponse et
  `data_get_schema` la nomment dans `warning`. Pour la retirer :
  `data_patch_schema(remove_attrs={"colonne": ["clé"]})`.

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

Quand le schéma déclare une `key` (`data_set_schema`), une écriture **sans `id`** qui
porte une valeur de clé **déjà présente** ne crée pas de doublon : un index unique le
garantit. Ce qu'elle fait de cette ligne dépend de ce que **ton appel dit** (oto#141) :

- **Tu DÉSIGNES** — par `id=`, ou en nommant la clé avec **`key=`** (un lot, ou une ligne
  seule avec `key=` = la clé déclarée ; REST `?key=` ; la frappe d'un upload) : une
  valeur déjà présente **modifie** sa ligne (retour de son `_id`), une valeur neuve la
  crée. Pas besoin d'`upsert`.
- **Tu AJOUTES** — ni `id` ni `key=` : une valeur déjà présente est un doublon que tu
  n'as pas vu.

| | jusqu'au 20 octobre 2026 | à partir du **21 octobre 2026** |
|---|---|---|
| ajout, clé déjà présente, sans `upsert` | fusionne, et `notices` avertit, daté | **refusé** `business_key_exists` : le refus nomme la ligne en place et les gestes |
| ajout, clé déjà présente, `upsert=true` | **fusionne** (retour de son `_id`) | **fusionne** |
| deux lignes d'un même appel à la même clé, sans `upsert` (désignation comme ajout) | fusionnent, averties | le lot est **refusé ENTIER** avant sa première ligne, rangs nommés (« lignes 1 et 2 : même siren '111' ») |

On ne désigne pas deux fois la même ligne dans un appel : retire le doublon, ou passe
`upsert=true` s'il doit fusionner. `upsert` vaut sur toutes les faces — ligne seule,
lot, REST, `oto_upload_url` (déclaré à la frappe, scellé dans le jeton, comme `key`) et
`oto_import` —, et il est refusé là où il ne fusionnerait rien : avec `id=`, ou sur un
tableau sans clé (et, pour un lot, sans `key=`).

Un lot rend chaque fusion dans **`fusions`** : `{rang, dans_rang, id, cle}` — `rang` le
rang de la ligne dans le lot (à partir de 1), `dans_rang` le rang de la ligne du **même**
lot qui a posé la ligne visée, `null` pour une ligne déjà en base, `cle`
`{colonne: valeur}`. **`ids` reste aligné rang pour rang** : deux entrées peuvent
désigner la même ligne, et `fusions` dit lesquelles. L'accusé d'un upload signé, lu par
un porteur de lien, rend `fusions` sans `id`.

Sur un tableau **fermé** (`key_required`, ci-dessous), toute écriture sans `id` est une
désignation par la clé : il ne crée jamais, la valeur de clé y est la façon de viser
sa ligne.

Une ligne créée sans valeur de clé est créée quand même et la réponse le signale
(`notices`) : aucune écriture ultérieure ne la retrouvera par sa clé.

`key_required: true` ferme le tableau : une écriture qui ne désigne aucune ligne
existante — ni `id`, ni valeur de clé déjà portée ; une clé simplement **nouvelle**
compte comme inconnue — est **refusée** (`business_key_required`) au lieu de créer.
Le refus dit si l'écriture PORTAIT la clé (« porte `siren` = … » : la valeur est
inconnue) ou non (« ne porte pas `siren` ») ; REST : `details.cle_portee`, `details.valeur`,
`details.a_renvoyer`. Un lot dédoublonné par `key=` sur une autre colonne est jugé sur
la clé DÉCLARÉE.
Ouvrir, écrire, refermer : `data_patch_schema(key_required=false)` puis `…=true`.

Un **lot** (`data_write(rows=[…])`, `oto_upload_url`) n'est pas atomique : il s'arrête à
la première ligne refusée, les précédentes restent écrites, le refus nomme la ligne et
dit combien ont atterri. Deux refus font exception, jugés sur le lot ENTIER avant sa
première ligne : un mot déprécié (`@keep`, `@clear`) et, sans `upsert`, une clé en
doublon ou déjà portée par un ajout (ci-dessus). `key=` sur le lot désigne par une autre
colonne que la clé déclarée s'il le faut ; sur une ligne seule, seule la clé déclarée
joue.

## 7. Deux faces, un seul stockage

| geste | MCP (`data_*`) | REST (`/api/datastores` = `DS`) |
|---|---|---|
| tableaux | `data_list_datastores`, `data_create_datastore`, `data_rename_datastore`, `data_delete_datastore`, `data_url` | `GET`/`POST DS` ; `PATCH`/`DELETE DS/{tableau}` ; `GET DS/{tableau}/url` |
| lignes | `data_rows` (page, ou `id`), `data_write`, `data_delete_row` | `GET`/`POST DS/{tableau}/rows` ; `GET`/`PATCH`/`DELETE …/rows/{row_id}` |
| schéma | `data_get_schema`, `data_set_schema`, `data_patch_schema`, `data_drop_column` | `GET`/`PUT`/`PATCH …/schema` ; `POST …/drop_column` |
| file de travail | `data_claim_next`, `data_release` | `POST …/claim_next` ; `POST …/rows/{row_id}/claim` ; `POST …/rows/{row_id}/release` ; `GET …/queue` |
| agrégat | `data_aggregate` | `GET …/aggregate` |
| partage | `data_share` | `GET`/`POST`/`DELETE …/share` |
| historique d'une ligne | `data_row_history` | `GET …/rows/{row_id}/history` |
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
  clé unique dont la valeur est une liste d'objets (`{"data": [...]}`), ou dont la
  clé `rows` en porte une quelles que soient les autres (`{"rows": [...], "key":
  "siren"}`), est refusé `400 batch_body`, rien n'est écrit — sauf si une colonne de
  ce nom est déclarée au schéma. Un corps qui est une liste JSON est refusé `400
  invalid_body`. Le lot REST est `POST …/rows/batch`, corps `{"rows": [...], "key":
  "siren", "donnees_d_origine": true}` (`key`, `upsert` et
  `donnees_d_origine` facultatifs) : le même geste que `data_write(rows=[…])` — même
  moteur, mêmes refus nommant la ligne, mêmes notices, même réponse (`inserted`,
  `updated`, `count`, `ids`, `fusions`). `POST …/rows` prend `?key=` et `?upsert=true` en query
  (son corps est la ligne). Pour un volume, `oto_upload_url`.
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
- **`key=`** : sur un lot, MCP et REST (`…/rows/batch`) prennent n'importe quelle
  colonne ; sur une ligne seule (`data_write(row=)`, `POST …/rows?key=`), seule la clé
  déclarée.
- **Refus de schéma : la charge à renvoyer.** Un refus de validation (requis manquant,
  type ou format, sous-champ inconnu, couche exigée) porte le fragment de `row` à
  renvoyer : seuls les champs à corriger, un gabarit `<…>` à la place de chaque valeur
  (`"<texte>"`, `"<nombre>"`, `"<a | b>"`), `| @empty` là où ce geste est permis, et dans
  une liste l'élément fautif SEUL, désigné par son `of.key` (sinon son rang, dans
  `a_renvoyer_elements`). MCP : en fin de message. REST : `details.a_renvoyer`. Remplace
  les gabarits et réécris ; une liste se renvoie entière, cet élément corrigé à sa place
  — ou n'écris que ses attributs, à son rang (§2 bis).
- **Refus.** MCP : erreur `INVALID_PARAMS` qui porte le message. REST : 400 nommé
  (`row_invalid`, `business_key_required`, `invalid_row_input`, `jeton_mal_place`,
  `invalid_filters`…), 403 `datastore_read_only` (tableau partagé en lecture seule),
  404 `datastore_not_found` (avec l'org où il vit, s'il existe dans une autre des
  tiennes) ou `row_not_found` ; 409 `row_locked` (ligne réservée par un autre),
  `revision_conflict` (la ligne a changé depuis la `_revision` passée) ou
  `business_key_exists` (ajout sur une clé déjà portée, ou doublon dans l'appel, sans
  `upsert`, §6 — `details` : `id` de la
  ligne en place, ou `doublons` et `existantes` d'un lot).

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

## 9. Une colonne `type: "formula"` est calculée, jamais écrite

Une formule OpenFormula posée au schéma (`formula: "IFS(…)"`) calcule la colonne à
partir des autres colonnes **de la même ligne** — jamais une autre ligne, jamais une
autre colonne calculée. Sous-ensemble fermé : `IFS`, `IF`, `SWITCH`, `AND`, `OR`, `NOT`,
`LEFT`, `MID`, `LEN`, `COUNTA`, `TRUE`, `FALSE`, comparaisons ; le reste, une colonne
inconnue ou un chaînage est refusé **à la pose**, en le nommant. Grammaire complète :
docstring de `oto_mcp/datastore/formule.py`.

- **Non écrivable.** Une écriture est refusée (« colonne CALCULÉE ») et `readonly_override`
  n'y change rien : la valeur serait recalculée. Écris les colonnes d'**entrée**.
  `champ.comment` porte la provenance (la branche `IFS` gagnante) ; `origine` n'est
  jamais touchée.
- **Une liste se lit en plage** : `contacts[].telephone` (avec `[]` — un point nu désigne
  une couche) donne le sous-champ de chaque élément, et ne se consomme que dans
  `COUNTA(…)`.
- ⚠️ **`contacts<>""` n'est pas un test de liste vide.** Lue comme un scalaire, une
  liste vaut son texte (`[]` → `"[]"`) : `contacts<>""` est vrai même pour `[]`. « Au
  moins une valeur » s'écrit `COUNTA(contacts[].telephone)>0`.
- **Le vide assumé `@empty` est un vide**, pour les comparaisons comme pour `COUNTA`.
- **Recalcul** : à chaque écriture de la ligne ; en masse, en tâche de fond, quand la
  formule est posée ou que son **texte** change (`data_set_schema`, `data_patch_schema`
  rendent `formules_recalcul_en_cours: true` ; `get_schema` dit `formules_a_recalculer`
  tant qu'il en reste). Re-poser le même texte ne recalcule rien ; le modifier, même d'un
  espace, relance le recalcul de toutes les lignes.
- **Tri, filtre, agrégation** lisent la valeur **stockée**, comme pour toute colonne. La
  colonne n'a pas de type de résultat : le tri est **textuel** — juste pour un rang
  « 1 »…« 5 » ou un nom, faux pour des nombres à plusieurs chiffres (`"10"` avant
  `"9"`, oto-backend#1034).
