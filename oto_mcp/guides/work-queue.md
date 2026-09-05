---
title: "File de travail : drainer un vivier avec N agents"
description: claim atomique, bail, libération — le cycle qui garantit qu'une flotte de sous-agents traite un tableau sans doublon ni ligne perdue
---

# Drainer un tableau avec plusieurs agents

À lire **avant** tout fan-out sur un vivier partagé : enrichir N leads, qualifier N
sites, traiter N dossiers avec plusieurs sous-agents en parallèle. Le réflexe naturel —
lire la table, découper en lots, distribuer — est **non atomique** : deux agents qui
lisent au même instant voient la même ligne libre et la traitent tous les deux.

Le datastore a la primitive qui règle ça côté serveur : **`data_claim_next`**.

## Le principe : un bail, pas une liste d'exclusion

`data_claim_next(namespace, worker, filter?, lease_s?)` prend **la prochaine ligne
claimable et la réserve** dans la même transaction (`FOR UPDATE SKIP LOCKED`, patron
file de travail PostgreSQL). Deux workers concurrents n'obtiennent **jamais** la même
ligne — le second saute simplement à la suivante.

Le claim pose un **bail** : `_claimed_by` (ton libellé de worker) et `_claimed_until`
(maintenant + `lease_s`). Une ligne sous bail actif est invisible aux autres claims.

⚠️ **Le claim ne modifie PAS le contenu de la ligne** — pas de passage automatique en
« en cours ». C'est le bail, pas le statut, qui protège du double traitement. Ne
construis donc rien qui suppose que la ligne a changé après le claim.

Réponse : `{namespace, row}` avec `row = null` quand il n'y a plus rien à prendre
(file vide pour ce filtre, ou tout est déjà sous bail).

## Le cycle complet

```
0. run = run_start(label="<ma campagne>")      → run["run_id"]
1. row = data_claim_next(namespace="<table>", worker="<mon-libellé>",
                         filter={"status": "nouveau"}, _run_id=run["run_id"])
2. si row == null  → terminé, le worker s'arrête
3. traiter row (enrichissement, appels connecteurs, raisonnement…)
4. data_write(namespace="<table>", id=row["_id"],
              row={"status": "traité", ...livrables}, _run_id=run["run_id"])
5. data_release(namespace="<table>", id=row["_id"], worker="<mon-libellé>",
                _run_id=run["run_id"])
6. reboucler en 1                              → puis run_finish(run["run_id"], "done")
```

⚠️ **`_run_id` se repasse à CHAQUE appel, y compris celui qui écrit.** C'est le
manquement le plus fréquent, et il coûte la ligne : le jeton est presque toujours
passé au claim (la consigne est fraîche) puis oublié à l'écriture — et l'écriture est
alors refusée sur la ligne qu'on tient soi-même. Le serveur ne le retient pas d'un
appel au suivant : chaque appel arrive dans sa propre session. ⚠️ **Il n'y a pas d'autre voie : `data_write` ne
prend pas de `worker`.** Le jeton de run est le seul moyen de te faire reconnaître par
le verrou à l'écriture — porte-le sur CHAQUE appel qui écrit, pas seulement sur celui
qui a réservé.

**Rendre la ligne est un geste, pas une conséquence.** Écrire un verdict ne la libère
pas : le verrou ne connaît pas tes états métier, et il n'a pas à les connaître — « à
qualifier » ou « traité » sont TES mots, pas les siens. Ce qui rend une ligne :

- **`data_release`** — le geste normal, à la fin de chaque ligne traitée ;
- **la fin de ton traitement** — si tu as encadré ton travail par `run_start` /
  `run_finish`, toutes les lignes que tu tenais sont rendues à la fermeture, quelle
  qu'en soit l'issue. C'est le filet quand tu oublies le release ;
- **l'expiration du bail** — le dernier recours, celui qui joue si ton agent s'arrête
  sans rien fermer. Il peut être long : ne compte pas dessus (cf. la durée du bail
  plus bas).

Le `worker` est rejoué au release comme garde — on ne libère pas le claim d'un autre.

**Tant que tu tiens une ligne, personne d'autre ne peut l'écrire.** Le bail protège
désormais la donnée, pas seulement l'attribution : une écriture venue d'ailleurs est
refusée avec un message qui dit qui tient la ligne et jusqu'à quand.

**Et « quelqu'un d'autre », c'est toi aussi dès que tu cesses de te nommer.** Tu es
reconnu par ton **run**, et seulement par lui — à condition de passer `_run_id=` sur
l'appel qui écrit, pas seulement sur celui qui a réservé. Un `data_write` qui ne le
porte pas est un inconnu pour le verrou, même s'il vient du compte qui tient le bail :
il est refusé, et la ligne reste bloquée jusqu'à l'expiration du bail.

⚠️ **Ton `worker` ne te fait PAS reconnaître à l'écriture, contrairement à ce que ce
guide a dit jusqu'au 05/09/2026.** Le serveur sait le faire — mais aucune surface ne
lui passe ton libellé, et `data_write` n'a pas de paramètre où l'écrire. Le conseil
« porte `worker=` si tu écris hors run » désignait donc un geste impossible, au moment
précis où tu cherches quoi faire d'un refus. Le `worker` sert au claim et au release,
nulle part ailleurs.

## Les trois paramètres qui comptent

**`worker`** — un libellé que tu choisis (ex. `"enrich-13"`, `"qualif-nord-2"`), stable
pour un sous-agent donné et **rejoué verbatim** sur `data_release`. Il sert de garde et
rend la file lisible en supervision (« qui tient quoi »). Donne un libellé distinct par
sous-agent : c'est ce qui permet de voir lequel est mort.

**`filter`** — égalité exacte `{colonne: valeur}`, ce qui définit ce qui **compte comme
claimable**. Typiquement `{"status": "nouveau"}`. Sans filtre, toute ligne dont le bail
est libre est candidate — y compris celles déjà traitées. **Mets toujours un filtre**
dès que la table porte un statut. Si le tableau déclare un périmètre de réservation
(`lifecycle.claimable` dans son schéma), ton `filter` s'y ajoute en ET : il le resserre,
il ne l'élargit jamais — et une réponse `row: null` te nomme ce périmètre.

**`lease_s`** — durée du bail, 900 s (15 min) par défaut. C'est le mécanisme de
récupération : un worker qui meurt en cours de route ne bloque pas sa ligne
éternellement, elle redevient claimable à l'expiration. Cale-le sur la durée réelle
d'un traitement, avec de la marge : trop court, une ligne lente se fait voler et
traiter deux fois ; trop long, une ligne abandonnée dort.

## ⚠️ Le piège : sans états terminaux déclarés, rien n'est libéré

Le verrou est **le même pour tous les tableaux** : il n'y a rien à déclarer au schéma
pour qu'il fonctionne, et rien à régler par tableau. Un agent prend une ligne, la rend.

Ton champ de statut, lui, reste **entièrement le tien** : `"à qualifier"`, `"traité"`,
`"écarté"` sont des valeurs de ton métier, dans une colonne ordinaire. Le verrou ne les
lit pas — c'est ce qui garantit qu'il fonctionne pareil chez tout le monde.

> **Ce qui a changé (août 2026).** Écrire un état déclaré « final » libérait
> automatiquement la ligne. Ce comportement est retiré : la plateforme devait pour cela
> connaître tes états et deviner lesquels sont des fins, ce qui échouait en silence dès
> que la déclaration ne correspondait pas exactement à l'usage. Si tu dépendais de cette
> libération, ajoute un `data_release` après ton écriture — ou encadre ton travail par
> `run_start` / `run_finish`.

**Symptôme à reconnaître** : la file « se vide » alors que les lignes ne sont pas
traitées, puis se remplit à nouveau plus tard. C'est le bail qui expire, pas un bug — et
c'est le signe qu'il manque un `data_release` quelque part.

## Fan-out : ce que ça remplace

Avec le claim, un sous-agent n'a **pas besoin de savoir ce que font les autres**. Tu
n'as donc plus à :

- injecter une **liste d'exclusion** des lignes déjà prises à chaque agent (elle est
  périmée à la seconde où tu l'écris) ;
- découper en **lots de 3-4** figés à l'avance (un agent lent bloque son lot, un agent
  mort le perd) ;
- **dédupliquer en relisant la table** avant chaque traitement (coûteux et non atomique
  — deux relectures simultanées donnent le même verdict).

Le patron : lance N sous-agents identiques, chacun avec son `worker`, chacun bouclant
`claim → traiter → écrire → rendre la ligne` jusqu'à `row == null`. Le parallélisme se
règle par le nombre d'agents, pas par un découpage. Pour les gros volumes, combine avec
le guide `bulk-load` (garder les payloads hors du contexte principal).

## Divers

- `namespace` accepte `slot:<nom>` — le tableau bindé par le projet actif.
- Le bail n'apparaît sur une ligne (`_claimed_by` / `_claimed_until`) **que s'il est
  posé** : une lecture ordinaire d'une ligne libre n'a pas ces champs.
- L'ordre de service est l'ordre de création (les plus anciennes d'abord).
- Le dashboard montre les lignes sous bail (« en cours · worker ») et permet une
  libération forcée par un humain, sans garde de worker.
