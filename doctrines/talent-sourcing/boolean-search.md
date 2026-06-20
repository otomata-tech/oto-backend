---
slug: boolean-search
title: Recherche booléenne & X-ray pour le sourcing
description: Construire des requêtes booléennes et X-ray (Google/LinkedIn) pour trouver des candidats précis.
category: Recrutement
tags: recrutement, sourcing, boolean, x-ray, linkedin
---

# Recherche booléenne & X-ray

Skill de la doctrine `talent-sourcing`. Trouver les bons profils = bien formuler
la requête. Deux leviers : **booléen** (sur LinkedIn/ATS) et **X-ray** (Google qui
indexe LinkedIn et autres).

## Opérateurs booléens

- `AND` (implicite entre mots) — restreint. `OR` — élargit (synonymes). `NOT` /
  `-` — exclut. **Guillemets** `"…"` pour une expression exacte. **Parenthèses**
  pour grouper : `(développeur OR developer OR ingénieur) AND (python OR golang)`.
- Couvre **synonymes et variantes** (FR/EN, acronymes, intitulés voisins) :
  `("product manager" OR "chef de produit" OR "PM" OR "PO")`.
- Exclus le bruit : `NOT (stage OR alternance OR freelance)` si tu cherches un CDI.

## Recette : du brief à la requête

1. Liste les **must-have** → noyau `AND`. 2. Pour chacun, dérive les **synonymes**
   → `OR` interne. 3. Ajoute les **exclusions** (séniorité, statut, secteurs hors
   cible). 4. Teste, observe le volume, resserre/élargis.

Exemple : *Senior Python à Paris, pas d'agence* →
`(python OR django OR fastapi) AND (senior OR lead OR "tech lead") AND (paris OR "île-de-france" OR remote) NOT (freelance OR agency OR recrutement)`

## X-ray Google (via `serper_web_search`)

Cible un site qui indexe les profils :
- LinkedIn : `site:linkedin.com/in (python OR golang) (senior OR lead) paris`
- GitHub : `site:github.com python "machine learning" location:france`
- Portfolios/CV : `(intitle:cv OR intitle:resume) "data engineer" filetype:pdf`

`serper_web_search` rend les SERP structurées — itère sur les requêtes, déduplique
les profils, garde l'URL canonique comme clé.

## Sur LinkedIn (via `unipile_search`)

Passe la requête booléenne dans le mot-clé, puis affine par filtres (intitulé,
localisation, entreprise). `unipile_profile` pour dérouler un profil, `unipile_company`
pour qualifier l'employeur actuel (taille, secteur — signal de move probable).

## Garde-fous

- Une requête trop large noie le signal ; trop étroite rate des profils. Vise
  20–50 profils exploitables par itération.
- Note la **requête gagnante** dans la note ATS / le datastore — elle se réutilise
  sur des postes voisins.
