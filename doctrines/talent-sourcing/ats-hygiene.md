---
slug: ats-hygiene
title: Hygiène de l'ATS (Greenhouse / Lever / Ashby / Teamtailor / Recruitee)
description: Garder l'ATS propre — dédup, notes de source, stages à jour, candidats rattachés au bon poste.
category: Recrutement
tags: recrutement, ats, greenhouse, lever, ashby, hygiene
---

# Hygiène de l'ATS

Skill de la doctrine `talent-sourcing`. Un ATS propre = des décisions fiables et
un pipeline lisible. Vaut pour les connecteurs **Greenhouse**, **Lever**, **Ashby**,
**Teamtailor** et **Recruitee** (surfaces analogues, vocabulaire ci-dessous).

## Vocabulaire par connecteur

| Concept | Greenhouse | Lever | Ashby | Teamtailor | Recruitee |
|---|---|---|---|---|---|
| Candidat | candidate | opportunity | candidate | candidate | candidate |
| Poste | job | posting | job | job | offer |
| Candidature | application | (opportunity) | application | job-application | (placement) |
| Note | activity note | note | note | note | note |

## Règles d'or

1. **Dédup avant création.** Cherche par email d'abord (`greenhouse_candidates` avec
   `email=`, `lever_opportunities` `email=`, `ashby_search_candidates`,
   `teamtailor_candidates` `email=`, `recruitee_candidates` `query=`). Un humain =
   une fiche. Si doublon → enrichis l'existant, ne crée pas.

2. **Toujours rattacher au poste.** Un candidat créé sans poste est orphelin :
   passe `posting_ids` (Lever), `applications`/`job_id` (Greenhouse), `offer_ids`
   (Recruitee). Sinon il sort des reportings.

3. **Note de source à la création.** Première note = d'où vient le candidat, la
   requête de sourcing gagnante, la raison de l'intérêt, le lien du profil.
   `*_add_note` / `*_create … note`. C'est la mémoire du « pourquoi lui ».

4. **`on_behalf_of` / `perform_as`.** Greenhouse et Lever exigent un id
   d'utilisateur sur les écritures (`greenhouse_users`, `lever_users`). Récupère-le
   une fois, réutilise-le. Une écriture sans cet id échoue.

5. **Stages à jour.** Fais avancer le candidat dans le pipeline au rythme réel des
   échanges (`*_stages` pour le référentiel). Un pipeline qui ment ne sert à rien.

## Garde-fous

- **Écritures réversibles d'abord.** Crée/note volontiers ; en revanche confirme
  avant tout changement de stage massif ou rejet — ce sont des actions vues par
  toute l'équipe recrutement.
- Si un champ obligatoire de l'ATS manque, **demande-le** plutôt que d'inventer une
  valeur de remplissage.
