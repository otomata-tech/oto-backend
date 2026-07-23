---
title: Recherche LinkedIn (Recruiter / Sales Navigator / Classic) — facettes & pagination
description: préférer le structuré (paginé au cursor), résoudre chaque filtre en id via unipile_search_facets ; l'URL est une boîte noire plafonnée à 25
---

# Rechercher sur LinkedIn via Unipile

À lire avant toute recherche de personnes/entreprises un peu sérieuse — surtout
dès que tu veux **plus de 25 résultats** ou **filtrer par autre chose qu'un mot-clé**
(compétence, secteur, localisation, employeur, intitulé…).

## La règle : structuré, pas URL

`unipile_search` a deux entrées, `url=` et les paramètres structurés. **Privilégie
TOUJOURS le structuré.**

- **Mode URL** (`url="…"`) = **boîte noire**. Il rend **la première page (25 résultats) et
  rien de plus** : pas de cursor, le `start` de l'URL est **ignoré**, même la vraie URL
  « page 2 » redonne les 25 premiers. Il n'existe **aucun** moyen de paginer une recherche
  URL, et Unipile n'a **aucun endpoint pour décoder une URL en paramètres** (le
  `searchContextId` est opaque). ⇒ N'utilise l'URL que pour un aperçu jetable des 25 premiers.
- **Mode structuré** (`keywords=` + facettes) = **paginé par cursor** → tu récupères tout.
  C'est la seule voie complète.

**Si on te donne une URL** : regarde si elle porte un `searchKeyword=` lisible. Si oui,
reprends ce mot-clé en structuré (`keywords=…`) et rajoute les filtres voulus. Si l'URL
n'a que du `searchContextId` (100 % facettes, aucun paramètre lisible), ses filtres sont
**irrécupérables** — redemande les critères et reconstruis-les en structuré.

## Résoudre les filtres : LinkedIn veut des IDs, pas du texte

Un filtre « compétence = Microsoft Excel » ou « secteur = Construction » ne s'écrit pas
en clair : LinkedIn l'identifie par un **id de facette**. Résous-le d'abord :

1. `unipile_search_facets(facet_type, keywords)` → `[{id, name}]`.
2. **Choisis le bon candidat** — une saisie renvoie souvent plusieurs facettes
   (« Microsoft Excel » → Excel, Microsoft Office, VBA…). Lis les `name`, garde l'`id`
   pertinent. **Ce choix est ton jugement**, il n'est pas automatisable.
3. Passe l'`id` à `unipile_search`.

Types de facette confirmés : **`SKILL`, `LOCATION`, `INDUSTRY`, `COMPANY`**. D'autres
existent (essaie `TITLE`, `SCHOOL`, `FUNCTION`, `SENIORITY`, `LANGUAGE`…) — un type
invalide lève `Expected kind 'StringEnum'`. La résolution marche même hors Recruiter/SN.

`location` / `company` / `industry` de `unipile_search` acceptent déjà **noms OU ids** —
en cas d'ambiguïté, résous d'abord via `unipile_search_facets` et passe l'id exact.

## Produit & pagination

- **Produit** (`api=`) : `classic` (défaut, sans abonnement), `sales_navigator`, `recruiter`.
  Les deux premium exigent le **siège activé À LA CONNEXION** (`unipile_connect_start(premium=…)`),
  sinon `403 "out of your scope"`. Recruiter et Sales Navigator sont exclusifs.
- **Pagination** : repasse le `cursor` renvoyé (`unipile_search(cursor=…)`) — il ré-encode
  toute la requête, ne reconstruis rien. Boucle jusqu'à cursor vide.

## Cadence & rate-limit (ne pas cramer le compte)

LinkedIn rate-limite **par compte**, en couches (Unipile renvoie `429 We only allow
1 / 10 / 100 requests. Retry in N`). Le `Retry in N` est **le plus souvent quelques
secondes** (throttle de rafale), rarement des heures. Ce n'est **pas** un cap dur : le
danger n'est pas « 100 et bloqué », c'est **le martèlement** — enchaîner des dizaines
d'appels en rafale fait passer le compte de `429` → **timeouts** → **checkpoint /
déconnexion** (vécu : un compte cassé ainsi en une soirée).

Règles :

- **Espace tes appels** — pas des dizaines de `unipile_company` / `unipile_search` en
  rafale. Traite les leads en série tranquille, pas en tir groupé.
- **Sur un `429`** : le serveur arme un court backoff (= le délai qu'Unipile a demandé)
  et refuse les scrapes d'ici là avec « réessaie dans ~Xs ». **Respecte-le, RALENTIS** ;
  n'insiste pas en boucle (ça aggrave le throttle).
- **`unipile_company` est mis en cache 6h** par compte : relire une société déjà vue ne
  coûte rien — mais ne relis pas inutilement.
- **Search-first** : une page de résultats porte déjà nom/poste/entreprise/headline. Ne
  fais un `unipile_company` / `unipile_profile` que si tu as VRAIMENT besoin du détail —
  c'est la route la plus contrainte (~100/fenêtre).
- **Gros volume** = plusieurs comptes (chaque siège a sa propre limite) + délégation à un
  sous-agent, pas un seul compte poussé à fond.

## Gros volumes

Un réseau/export complet dépasse le plafond de tokens d'un résultat d'outil et pollue le
contexte. Pour balayer profond (des centaines/milliers de profils), **délègue à un
sous-agent** qui pagine chez lui et ne te remonte qu'un reçu léger — cf. guide `bulk-load`.
