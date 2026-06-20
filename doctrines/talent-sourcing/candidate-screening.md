---
slug: candidate-screening
title: Pré-qualification candidat (scorecard)
description: Qualifier un profil contre l'ICP du poste avec une scorecard structurée et traçable.
category: Recrutement
tags: recrutement, screening, qualification, scorecard
---

# Pré-qualification candidat (scorecard)

Skill de la doctrine `talent-sourcing`. Objectif : décider **go / no-go / à creuser**
sur un profil, vite et sans biais, avec une trace réutilisable.

## La scorecard

Pour chaque poste, fixe **4–6 critères** issus du brief, chacun must-have ou
nice-to-have. Note chaque candidat par critère : `++ / + / ? / -` (preuve à
l'appui, jamais d'impression nue).

| Critère | Type | Évaluation | Preuve |
|---|---|---|---|
| Stack technique | must | ++ / + / ? / - | repo, intitulé, projet |
| Séniorité | must | … | années, scope d'équipe |
| Domaine métier | nice | … | secteur des employeurs |
| Localisation / remote | must | … | profil, dernière localisation |
| Signaux de move | nice | … | ancienneté poste actuel |

**Règle d'élimination** : un `-` sur un must-have = no-go, on n'enrichit/contacte
pas. Économise les crédits d'enrichissement et le temps d'approche.

## Sources de preuve (outils)

- Profil & parcours : `unipile_profile`, `unipile_member_posts` (ce qu'il publie).
- Employeur actuel : `unipile_company`, `fr_get` (si entreprise FR — taille, santé).
- Travaux : `serper_web_search` / `serper_scholar_search`, GitHub via X-ray.

## Traçabilité

- Persiste la scorecard : note ATS (`*_add_note`) **ou** un namespace datastore
  (`data_write` sur `screening_<poste>`), pour comparer les candidats côté à côté.
- Réutilise la base de connaissance : `memento_mem_search` pour retrouver une
  qualification passée du même profil (ne refais pas un screening déjà fait).

## Garde-fous

- **Pas d'inférence de données sensibles** (âge, origine, situation familiale,
  santé…) : hors critère, hors note. RGPD + équité.
- Distingue **fait** (vérifié par un outil) et **hypothèse** (à confirmer en
  entretien) — étiquette-les. Une scorecard n'est pas un verdict d'embauche, c'est
  un tri d'entrée d'entonnoir.
