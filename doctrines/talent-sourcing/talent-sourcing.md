---
slug: talent-sourcing
title: Sourcing de talents — workflow de bout en bout
description: Du brief de poste au candidat loggé dans l'ATS — sourcing multi-canal, enrichissement, suivi.
category: Recrutement
tags: recrutement, sourcing, ats, talent
---

# Sourcing de talents — workflow de bout en bout

Doctrine de base du recrutement avec oto : transformer un **brief de poste** en
un **pipeline de candidats qualifiés**, loggés proprement dans l'ATS. Les skills
nommés (`boolean-search`, `candidate-screening`, `ats-hygiene`,
`recruiter-outreach`) détaillent chaque étape — charge-les à la demande.

## Principe

Un recrutement = un **entonnoir** : définir précisément la cible (ICP candidat),
sourcer large mais qualifié, enrichir les coordonnées, qualifier, puis approcher.
**Tout candidat retenu finit dans l'ATS** (Greenhouse / Lever / Ashby / Teamtailor
/ Recruitee) — jamais un sourcing perdu dans un fichier volant.

## Étapes

1. **Cadrer le poste (brief).** Intitulé, séniorité, must-have vs nice-to-have,
   localisation/remote, fourchette de rémunération, signaux d'exclusion. Écris
   l'ICP candidat noir sur blanc — c'est le filtre de toutes les étapes suivantes.
   Vérifie le poste ouvert côté ATS (`*_jobs` / `*_postings` / `*_offers`).

2. **Sourcer (multi-canal).** Voir le skill `boolean-search`.
   - LinkedIn via Unipile (`unipile_search`, `unipile_profile`, `unipile_company`).
   - Recherche web / X-ray (`serper_web_search`, `serper_scholar_search` pour les
     profils techniques/académiques).
   - Viviers ATS existants (candidats déjà en base, talent pools).

3. **Enrichir les coordonnées.** Email pro via `hunter_email_finder` /
   `hunter_domain_search`, waterfall via `fullenrich_enrich_linkedin`, vérif de
   délivrabilité via `zerobounce`. Ne contacte jamais sur un email non vérifié.

4. **Qualifier.** Voir `candidate-screening` — confronte chaque profil à l'ICP,
   note (scorecard), élimine tôt. Mieux vaut 10 profils en or que 100 tièdes.

5. **Logger dans l'ATS.** Crée le candidat (`*_add_candidate` /
   `*_create_candidate`), rattache-le au poste, pose une **note de sourcing**
   (source, raison de l'intérêt, lien du profil). Voir `ats-hygiene`.

6. **Approcher.** Voir `recruiter-outreach` — message personnalisé, séquence,
   suivi. Mets à jour le stage ATS au fil des réponses.

## Garde-fous

- **Pas d'invention.** Un email/téléphone/employeur non confirmé par un outil =
  donnée absente, pas une supposition. Marque-le comme « à vérifier ».
- **RGPD.** Tu manipules des données personnelles de candidats : minimise (ne
  collecte que l'utile), source-tracé (note la provenance dans l'ATS), et respecte
  les demandes de suppression.
- **Une source unique par candidat.** Avant de créer, cherche un doublon
  (`*_search_candidates` / `*_candidates?email=`) — un candidat = une fiche ATS.
- Encadre tes déroulés : `doctrine_start("talent-sourcing")` →
  `doctrine_finish(run_id, outcome)`. Un manque (outil/donnée) → `report_gap`.
