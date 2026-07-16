---
title: DSL de filtres Cognism (search_contacts / search_accounts)
description: référence complète des ~150 champs de filtre Cognism — à lire avant une recherche contact/société non triviale
---

# DSL de filtres Cognism

À lire avant de construire un `filters` un peu élaboré pour `cognism_search_contacts`
ou `cognism_search_accounts` — la DSL est large (~150 champs, plusieurs niveaux
d'imbrication) et les docstrings des tools restent volontairement courtes. `filters`
est un dict qui reproduit **exactement** le JSON attendu par l'API Cognism — pas de
transformation côté oto, à part la validation des champs à valeurs fermées listés
plus bas.

## Piège n°1 : recherche vs reveal

`cognism_search_contacts`/`cognism_search_accounts` renvoient des **flags booléens**
(`hasEmail`, `hasDirectPhoneNumbers`, …), **jamais** l'email ou le téléphone réel.
Pour obtenir la valeur réelle, il faut ensuite appeler `cognism_redeem_contacts`/
`cognism_redeem_accounts` avec l'`id` (ou le `redeemId` fourni dans le résultat de
recherche) — **cet appel consomme des crédits**, contrairement à la recherche.
`cognism_enrich_contact`/`cognism_enrich_account` sont le chemin inverse : retrouver
UN contact/société depuis des critères d'identité (email, LinkedIn, nom+société…)
sans passer par une recherche.

## Piège n°2 : pagination par curseur, pas par offset

`lastReturnedKey` est un **curseur séquentiel** renvoyé par la page précédente —
Cognism ne permet **pas** de sauter à une page arbitraire (pas de `page=5`). Pour
avancer, il faut reprendre le `lastReturnedKey` du résultat précédent, page après
page, dans l'ordre.

## Piège n°3 : profondeur différente selon l'endpoint pour les champs "société"

Les mêmes champs société (`types`, `fundingEvent.*`, `hiringEvent.*`,
`accountSearchOptions.*`, `technologies`, `industries`, `headcount`…) vivent :
- sous **`account.*`** dans `cognism_search_contacts` (le contact est la racine, la
  société qui l'emploie est imbriquée) ;
- **à la racine** dans `cognism_search_accounts` (la société EST la racine, là).

Exemple : `{"account": {"types": ["Public Company"]}}` pour chercher des contacts
dans des sociétés cotées, mais `{"types": ["Public Company"]}` (sans le préfixe
`account.`) pour chercher directement ces sociétés.

## Piège n°4 : un typo d'enum ne lève pas d'erreur côté Cognism — page vide silencieuse

Pour les champs à liste FERMÉE ci-dessous, oto valide côté client AVANT l'appel
réseau et lève une erreur explicite sur une valeur hors liste. Sans cette validation,
Cognism répondrait 200 avec une page vide — indiscernable d'une recherche qui ne
matche vraiment rien.

---

## Champs contact (racine de `cognism_search_contacts`)

| Champ | Type | Notes |
|---|---|---|
| `ids` / `excludeIds` | Array[String] | contact ids OU redeemIds — pas de mix des deux dans un même appel |
| `fullName` / `firstName` / `lastName` | String | |
| `jobTitles` / `excludeJobTitles` | Array[String] | tokenisé par défaut — voir `searchOptions.match_exact_job_title` |
| `seniority` ⚠️validé | Array[String] | **Manager, Director, Partner, CXO, Owner, VP** |
| `jobFunctions` ⚠️validé | Array[String] | **Oversight, Technology, Operations, Sales, Marketing, Client Success, HR, Accounting, Business, Production** |
| `managementLevel` ⚠️validé | Array[String] | **Entry-Level, Team-Lead, Experienced Staff, Executive-Level, Senior Leadership, Middle-Management, CxO** |
| `regions` / `countries` / `states` / `cities` / `zip` (+ `exclude*`) | Array[String] | valeurs dynamiques — `cognism_filter_values(kind="regions"\|"countries"\|"states")` |
| `locations` | Array[object] | `{country, city, state, zip}` — OR entre les objets du tableau |
| `skills` / `excludeSkills` | Array[String] | dynamique — `cognism_filter_values(kind="skills")` |
| `linkedinUrl` | String | |
| `education.schools` / `education.degrees` | Array[String] | |
| `mobilePhoneNumbers.{medium,high,highPlus}` | Boolean | qualité du numéro mobile disponible |
| `directPhoneNumbers.{medium,high,highPlus}` | Boolean | idem, ligne directe |
| `emailQuality.{medium,high,highPlus}` | Boolean | |
| `sha256` | Array[String] | recherche par email hashé |
| `lastConfirmed.{from,to}` | Long (ms epoch) | dernière confirmation du profil |

## Champs société (sous `account.*` — voir Piège n°3)

| Champ (sous `account.`) | Type | Notes |
|---|---|---|
| `ids` / `excludeIds` / `names` / `excludeNames` / `domains` / `excludeDomains` / `websites` | Array[String] | |
| `description` / `excludeDescription` / `shortDescription` | String | recherche par mot-clé |
| `keywords` | Array[String] | |
| `revenue.{from,to}` | Long | |
| `founded.{from,to}` | Int | année |
| `types` ⚠️validé | Array[String] | **Public Company, Educational, Educational Institution, Government Agency, Partnership, Privately Held, Self-Employed, non profit** |
| `regions` / `countries` / `excludeCountries` / `states` / `excludeStates` / `cities` / `excludeCities` / `zip` | Array[String] | dynamique, cf. `cognism_filter_values` |
| `locations` | Array[object] | `{country, city, state, zip}`, OR entre objets |
| `industries` / `excludeIndustries` | Array[String] | dynamique — `cognism_filter_values(kind="industries")` |
| `sic` / `isic` / `naics` | Array[String] | dynamique — `cognism_filter_values(kind="sic"\|"isic"\|"naics")` |
| `headcount.{from,to}` | Int | |
| `technologies` / `excludedTechnologies` | Array[String] | dynamique — `cognism_filter_values(kind="technologies", search=...)` (seule liste cherchable/paginée) |
| `lastConfirmed.{from,to}` | Long | |
| `hqPhoneNumbers.{medium,high,highPlus}` / `officePhoneNumbers.{medium,high,highPlus}` | Boolean | |

### Événements société (sous `account.*`)

| Champ | Type | Notes |
|---|---|---|
| `hiringEvent.eventDateFrom/To` | Long | |
| `hiringEvent.jobTitle` | String | |
| `hiringEvent.department` ⚠️validé | String | **legal, it, administration, marketing, sales, R&D, customer, operations, finance** |
| `hiringEvent.country/state/city` | Array[String] | |
| `fundingEvent.eventDateFrom/To` | Long | |
| `fundingEvent.fundingType` ⚠️validé | Array[String] | **venture, seed, grant, private_equity, angel, debt_financing, corporate_round, convertible note, equity_crowfunding** |
| `fundingEvent.series` ⚠️validé | Array[String] | **A, B, C, D, E, F, G, H, I, J, K** |
| `ipoEvent.eventDateFrom/To` | Long | |
| `acquisitionEvent.eventDateFrom/To` | Long | |
| `acquisitionEvent.acquirer` / `.acquiree` | Array[String] | noms de sociétés |

### Options de recherche société (`account.accountSearchOptions.*`)

| Champ | Type | Notes |
|---|---|---|
| `match_exact_account_name` / `match_exact_domain` | Boolean | |
| `filter_email` / `filter_domain` ⚠️validé | String | **exists, missing** |
| `show_max_events` | Int | |
| `location_type` ⚠️validé | String | **ALL, HQ** |
| `events_operator` ⚠️validé | String | **AND, OR** (défaut OR) |
| `operators.technologies` / `operators.excludedTechnologies` ⚠️validé | String | **AND, OR** |

## Sociétés précédentes du contact (`previousAccounts.*`, contact search seulement)

`names` / `excludeNames` / `titles` / `regions` / `countries` / `excludeCountries` /
`states` / `excludeStates` / `cities` / `excludeCities` / `zip` (Array[String]),
`emailQuality.{medium,high,highPlus}` (Boolean), `jobFunction` ⚠️validé (JOB_FUNCTIONS,
**singulier** — pas `jobFunctions`), `seniority` ⚠️validé, `managementLevel` ⚠️validé
(mêmes listes que ci-dessus).

## Événements de mobilité du contact

`locationMoveEvent.{eventDateFrom,eventDateTo,fromCountry,fromState,fromCity,
toCountry,toState,toCity}`, `jobJoinEvent.{eventDateFrom,eventDateTo,fromCompany,
fromJobTitle,fromIndustry,toCompany,toJobTitle,toIndustry}`, `jobLeaveEvent.{…mêmes
champs…}`.

## Options de recherche contact (`searchOptions.*`)

| Champ | Type | Notes |
|---|---|---|
| `match_exact_job_title` | Boolean | false (défaut) = tokenisé (contient tous les mots, ordre libre) ; true = titre exact |
| `ai_job_title` | Boolean | expansion IA du titre — combinable avec `match_exact_job_title:true` pour un match large mais ciblé |
| `sort_fields` ⚠️validé | Array[String] | **LastConfirmedContactDESC/ASC, EmailQualityDESC/ASC, ProfileScoreDESC/ASC** |

## Recherche société (`cognism_search_accounts`, champs à la racine)

Mêmes champs que la section "Champs société" ci-dessus, **sans le préfixe
`account.`** (cf. Piège n°3) — `names`, `domains`, `types`, `industries`,
`technologies`, `headcount`, `accountSearchOptions`, `hiringEvent`, `fundingEvent`… à
la racine directement.

## Listes dynamiques (pas figées ici — appeler `cognism_filter_values`)

`regions`, `countries`, `states`, `industries`, `sic`, `isic`, `naics`, `skills`,
`technologies` (seule paginée/cherchable via `search=`), `companySizes`,
`companyTypes`, `jobFunctions`/`managementLevels`/`seniority` (dupliqués côté Filter
API mais déjà figés ci-dessus, l'appel live n'est utile que pour vérifier une
éventuelle mise à jour côté Cognism).

## Champ hors DSL de filtre : redeem / enrich

`cognism_redeem_contacts`/`cognism_redeem_accounts` prennent `ids` OU `redeem_ids`
(exclusif) + `merge_phones_and_locations`. `cognism_enrich_contact`/
`cognism_enrich_account` prennent des critères d'identité directement (pas de dict
`filters`) :
- `enrich_contact` : `email`/`sha256`/`linkedin_url` sont les identifiants les plus
  fiables seuls ; sinon combiner `first_name`+`last_name`+`job_title` avec
  `account_name`/`account_website`. Défaut `min_match_score` Cognism = 30 (<27 =
  faible qualité).
- `enrich_account` : `website`/`domain`/`linkedin_url` seuls sont les plus fiables ;
  sinon `name` combiné à `country`/`city` (HQ ou bureau). Défaut `min_match_score`
  Cognism = **40** ici (<35 = faible qualité) — seuil différent de `enrich_contact`.
