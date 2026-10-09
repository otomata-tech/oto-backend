---
title: Namespace fr_ & data.oto.zone
type: reference
description: >-
  Les sources FR unifiées sous `fr_`, les deux stocks SIRENE (établissements vs unités légales,
  d'où vient la catégorie PME/ETI/GE), le service FOD dédié, le `dirigeants` vide à trois
  lectures, et data.oto.zone devenue une plateforme publique, avec son déploiement sans
  preprod ni tag.
---

# Namespace `fr_` — données entreprise France

> Complète `docs/sirene-stock.md` (le parquet, ses perfs, `fr_groupe`) : ce doc porte la
> carte des sources, le service FOD et `data.oto.zone`.

## Les sources et les verbes

Toutes les données entreprise FR sont unifiées sous le namespace `fr_` (CLI `oto fr`, MCP
`fr_*`). Sources : API Recherche Entreprises, INSEE SIRENE, INPI/BCE, BODACC, BOAMP. Toutes
open data sauf SIRENE (clé API).

`fr_get` = fiche agrégée (identité + 7 ratios top du dernier bilan INPI + événements BODACC
récents, 3 sources en parallèle). Pour le bilan INPI complet (~13 ratios, jargon DAF
inclus) : `fr_bilan(siren, date_cloture)`. Les anciens noms (`recherche_entreprises_*`,
`sirene_*`) n'existent plus.

⚠️ Le BODACC de `fr_` part d'un **SIREN connu** (`fr_events`, `fr_events_batch`). Trouver les
entreprises **à partir** des annonces (les procédures collectives d'un département cette
semaine, les cessions d'une ville) est un autre connecteur, `bodacc` (`bodacc_notice`,
op=search|count|get, client `oto.tools.bodacc.notices`) : open data sans clé, quand `sirene`
est keyé — un connecteur porte un seul modèle de credential.

## Deux stocks SIRENE, pas un

Le parquet historique porte les **établissements** (implantation : adresse, NAF,
`trancheEffectifsEtablissement`) ; la **catégorie d'entreprise** (PME/ETI/GE) vit dans un
**second parquet, les unités légales** (`SIRENE_UL_PARQUET_PATH`, ~30 M lignes). L'INSEE la
calcule sur le périmètre **groupe** : une filiale petite par l'effectif de son établissement
sort en GE si elle appartient à un grand groupe — d'où
`fr_accords_search(exclude_categories=["GE"])` pour écarter les filiales d'un ciblage PME
(26 % du gisement d'un ciblage ACCO santé/prévoyance), et `fr_stock_*` côté batch. Ne pas
chercher la catégorie dans le stock établissement : elle n'y est pas, et la symétrie des deux
filtres le laisse croire.

**Stock établissements** (parquet INSEE ~2 Go, 43 M lignes) : la logique de requête (DuckDB)
vit dans **`france_opendata.sirene_stock`** (lib partagée, extra `[stock]`), consommée par le
backend (tools **`fr_stock_*`**, fusionnés dans le connecteur `sirene` ; REST
`/api/sirene/*`) et in-process par les apps co-localisées. Batch sièges :
`headquarters_addresses(sirens)` = 1 scan. Ne pas dupliquer en PG. **Service dédié FOD** : les
requêtes lisent un **parquet partitionné par département sur volume block local** de la box
du service (hive partitioning : pruning par département et code postal, pas de scan des 2 Go).
L'Object Storage n'intervient **qu'au refresh mensuel** (swap atomique), hors chemin
critique. Le backend appelle le service en réseau privé (`fod_client.py`).

## Un `dirigeants` vide a TROIS lectures, pas une

⚠️ Mesuré le 2026-09-01 : `fr_directors` rendait `[]`, la même réponse à l'octet, dans quatre
situations qui n'ont rien à voir : un SIREN **inexistant**, une **association** (catégorie
juridique 9220), une **commune** (7210), et une entreprise réellement sans dirigeant nommé.
Un agent lisait « pas de dirigeant » dans les quatre cas. *Trois causes, un seul silence* : le
registre des bénéficiaires ne couvre pas les personnes morales de droit public ni les
associations, et l'absence d'identité amont se confond avec l'absence de dirigeant.

Depuis, chaque entrée porte un `registre` à **trois** valeurs (`attendu` / `hors_registre` /
`indetermine`) et, quand la liste est vide, la note qui dit **pourquoi**. Trois valeurs et non
deux : les familles juridiques mixtes (droit étranger, droit public à activité commerciale)
restent `indetermine` plutôt que d'être tranchées dans le sens rassurant. La classification
s'arrête au **niveau I** de la catégorie juridique INSEE.

Corollaire de forme : l'appel unitaire rend un **dict**, plus une liste nue (une liste ne peut
pas dire pourquoi elle est vide). Et la qualité d'un entrepreneur individuel est posée **et
dite déduite** (`qualite_deduite`), jamais servie comme une déclaration du registre.

⚠️ **Où vit ce code, contre l'intuition** : les tools `fr_*` ne passent **pas** par oto-core.
La chaîne est `oto_mcp/tools/fr.py` → `oto_mcp/fod/fr.py` (proxies HTTP vers le service FOD)
→ la lib `france-opendata`, dépôt à part. oto-core porte bien des clients FR (`sirene`,
`inpi`, `naf`, `accords`), mais aucun de ceux que les tools `fr_*` appellent : un correctif
`fr_directors` ne demande donc **ni tag oto-core ni bump de pin**.

## `data.oto.zone` est une PLATEFORME PUBLIQUE

Plus une page de statut : accueil, catalogue des sources, **mentions légales** (provenance +
licence par source, avec la *preuve* de vérification : seules les sources vérifiées sont
affichées comme telles, les autres comme non vérifiées plutôt qu'inventées) et **banc d'essai**
dérivé de l'OpenAPI. S'y ajoutent des **permaliens de documents** publics et vérifiables :
`data.oto.zone/acco/{ACCOTEXT…}` (accords) et `/ccn/{KALIARTI…|KALICONT…}` (conventions
collectives), avec un 404 franc sur un identifiant inconnu. Le statut technique est en
`/statut`.

⚠️ **Le déploiement FOD n'a ni preprod ni tag : un push sur `master` EST la mise en
production** (≠ backend) ; et un lot qui bumpe le pin de la lib `france-opendata` échoue au
premier essai (propagation d'index PyPI) : le workflow réessaie 3× sur ce seul motif. Cartes
détaillées : les `CLAUDE.md` de `france-opendata-service` et `france-opendata`.
