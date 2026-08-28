---
title: Recherche LinkedIn (Recruiter / Sales Navigator / Classic) — facettes & pagination
description: ce qui commande la pagination, c'est le PRODUIT (classic ne pagine pas, premium oui), pas le mode d'entrée ; résoudre chaque filtre en id via linkedin_facets (candidats sous `candidates`) ; keywords+company peut rendre 0 à tort ; l'URL reste une boîte noire plafonnée ; respecter le rate-limit (429 = backoff variable, de quelques secondes à ~1h selon la cadence récente — ne pas marteler)
---

# Rechercher sur LinkedIn via Unipile

À lire avant toute recherche de personnes/entreprises un peu sérieuse — surtout
dès que tu veux **plus d'une page de résultats** ou **filtrer par autre chose qu'un
mot-clé** (compétence, secteur, localisation, employeur, intitulé…).

## Ce qui commande le volume : le PRODUIT, pas le mode d'entrée

C'est le point à comprendre avant tout le reste, parce que se tromper ici coûte des
jours : **la pagination dépend du produit LinkedIn (`api=`), pas de la façon dont tu
formules la requête.**

- **`classic`** (défaut, compte sans abonnement) : **ne pagine pas**. Ni en URL, ni en
  structuré. Tu obtiens une première page et rien de plus, sans cursor exploitable —
  mesuré : 10 résultats rendus sur 92 annoncés. Passer de l'URL au structuré ne débloque
  RIEN sur ce palier ; ça n'améliore que la façon de filtrer.
- **`sales_navigator` / `recruiter`** : **paginent au cursor**, et c'est là seulement que
  le structuré donne accès au gisement complet.

⚠️ Une version antérieure de ce guide opposait « mode URL » et « mode structuré » en
présentant le second comme « la seule voie complète, paginée au cursor ». C'était faux et
trompeur : sur un compte classic, changer de mode ne change pas le plafond. La conclusion
naturelle — « je pagine mal, donc je m'y prends mal » — envoyait chercher un problème de
formulation là où il fallait un siège premium.

**Donc, dans l'ordre :** si tu as besoin de volume, vérifie d'abord le produit. S'il est
`classic`, aucune formulation ne te donnera la suite — il faut un siège Sales Navigator ou
Recruiter, activé **À LA CONNEXION** (`unipile_connect_start(premium=…)`), sinon
`403 "out of your scope"`. Recruiter et Sales Navigator sont exclusifs l'un de l'autre.

## Structuré plutôt qu'URL — pour les filtres, pas pour le volume

`linkedin_search` a deux entrées, `url=` et les paramètres structurés. **Privilégie le
structuré**, mais pour la bonne raison : il te rend les filtres maîtrisables, pas le
gisement complet (voir ci-dessus).

- **Mode URL** (`url="…"`) = **boîte noire**. Première page et rien de plus : le `start`
  de l'URL est **ignoré**, même la vraie URL « page 2 » redonne les premiers résultats.
  Unipile n'a **aucun endpoint pour décoder une URL en paramètres** (le `searchContextId`
  est opaque). ⇒ Aperçu jetable, rien d'autre.
- **Mode structuré** (`keywords=` + facettes) = filtres explicites, reproductibles, et
  **paginables si et seulement si le produit le permet**.

**Si on te donne une URL** : regarde si elle porte un `searchKeyword=` lisible. Si oui,
reprends ce mot-clé en structuré (`keywords=…`) et rajoute les filtres voulus. Si l'URL
n'a que du `searchContextId` (100 % facettes, aucun paramètre lisible), ses filtres sont
**irrécupérables** — redemande les critères et reconstruis-les en structuré.

## Résoudre les filtres : LinkedIn veut des IDs, pas du texte

Un filtre « compétence = Microsoft Excel » ou « secteur = Construction » ne s'écrit pas
en clair : LinkedIn l'identifie par un **id de facette**. Résous-le d'abord :

1. `linkedin_facets(facet_type, keywords)` → `{facet_type, candidates: [{id, name}]}`.
   Les candidats sont sous **`candidates`**, pas à la racine.
2. **Choisis le bon candidat** — une saisie renvoie souvent plusieurs facettes
   (« Microsoft Excel » → Excel, Microsoft Office, VBA…). Lis les `name`, garde l'`id`
   pertinent. **Ce choix est ton jugement**, il n'est pas automatisable.
3. Passe l'`id` à `linkedin_search`.

Types de facette confirmés : **`SKILL`, `LOCATION`, `INDUSTRY`, `COMPANY`**. D'autres
existent (essaie `TITLE`, `SCHOOL`, `FUNCTION`, `SENIORITY`, `LANGUAGE`…) — un type
invalide lève `Expected kind 'StringEnum'`. La résolution marche même hors Recruiter/SN.

`location` / `company` / `industry` de `linkedin_search` acceptent déjà **noms OU ids** —
en cas d'ambiguïté, résous d'abord via `linkedin_facets` et passe l'id exact.

⚠️ **`keywords=` combiné à `company=[…]` peut rendre `total_count: 0`** là où des profils
correspondants existent bel et bien (observé le 10/08/2026 avec un id de facette `COMPANY`
pourtant correct ; idem avec `advanced_keywords={company, title}`). **Un zéro sur cette
combinaison ne prouve rien** — ne conclus pas « personne ne correspond ». Relance avec la
seule facette (`company=[<id>]`, sans `keywords`) et trie les intitulés toi-même sur les
résultats : la facette seule est fiable.

## Paginer, quand c'est possible

Sur un produit premium : repasse le `cursor` renvoyé (`linkedin_search(cursor=…)`) — il
ré-encode toute la requête, ne reconstruis rien. Boucle jusqu'à cursor vide.

Sur `classic` : il n'y a pas de suite à aller chercher. Si le besoin porte sur un gisement
entier, dis-le plutôt que de boucler — c'est un problème de siège, pas de méthode.

## Cadence & rate-limit (ne pas cramer le compte)

LinkedIn rate-limite **par compte**, en couches (Unipile renvoie `429 We only allow
1 / 10 / 100 requests. Retry in N`). **Le `Retry in N` suit ta cadence récente — ce
n'est pas une constante** : quelques secondes après une rafale légère (mesuré 3-38s le
21/07), mais **~55 min** derrière un pilote qui a enchaîné les appels (mesuré le 07/08,
sur un seul appel isolé, puis ~53 min au suivant). Lis le délai renvoyé, ne suppose pas
qu'il est court : c'est lui qui fait foi.

Ce n'est **pas** un cap dur : le danger n'est pas « 100 et bloqué », c'est **le
martèlement** — enchaîner des dizaines d'appels en rafale fait passer le compte de
`429` → **timeouts** → **checkpoint / déconnexion** (vécu : un compte cassé ainsi en une
soirée). Un backoff qui grimpe à l'heure est le signal que la cadence précédente était
déjà trop haute.

Règles :

- **Espace tes appels** — pas des dizaines de `linkedin_profile(op="company")`
  / `linkedin_search` en rafale. Traite les leads en série tranquille, pas en tir groupé.
- **Sur un `429`** : le serveur arme un backoff (= le délai qu'Unipile a demandé, plafonné
  à 1h) et refuse les scrapes d'ici là en annonçant l'attente restante. **Respecte-la,
  RALENTIS** ; n'insiste pas en boucle (ça aggrave le throttle). Si l'attente se compte en
  dizaines de minutes, passe à autre chose et reviens — ne sonde pas.
- **`linkedin_profile(op="company")` est mis en cache 6h** par compte : relire
  une société déjà vue ne coûte rien — mais ne relis pas inutilement.
- **Search-first** : une page de résultats porte déjà nom/poste/entreprise/headline. Ne
  fais un `linkedin_profile` (op="company"/"person") que si tu as VRAIMENT
  besoin du détail — c'est la route la plus contrainte (~100/fenêtre).
- **Gros volume** = plusieurs comptes (chaque siège a sa propre limite) + délégation à un
  sous-agent, pas un seul compte poussé à fond.

## Gros volumes

Un réseau/export complet dépasse le plafond de tokens d'un résultat d'outil et pollue le
contexte. Pour balayer profond (des centaines/milliers de profils), **délègue à un
sous-agent** qui pagine chez lui et ne te remonte qu'un reçu léger — cf. guide `bulk-load`.
