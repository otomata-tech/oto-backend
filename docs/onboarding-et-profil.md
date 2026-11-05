---
title: Onboarding & fiche profil
type: reference
description: >-
  Pas de mode d'accueil spécial : l'onboarding est un projet « Découverte » semé à la créati
  on de l'org perso. La fiche « situation avec oto » (capacité `me.profile`, deux faces dive
  rgentes à dessein) et `oto_whoami`.
---

# Onboarding = un projet « Découverte » + fiche profil (ADR 0032 §7)

> Extrait de `CLAUDE.md` le 2026-08-27 — le contenu n'a pas changé, seule sa place a bougé.
> La carte garde le résumé + le pointeur ; le détail (schémas, incidents datés et leurs
> leçons) vit ici.

## L'onboarding est un projet

**Plus de mode d'accueil spécial** (retiré le 2026-07-01) : pas de booléen `onboarded`,
pas de checklist dashboard, pas de tool d'onboarding scripté. L'onboarding est **un
projet** comme un autre — un projet « Découverte » porteur d'un brief d'accueil, **semé
à la création de l'org posée à l'inscription** (`discovery.seed_for_org`, appelé par
`org_store.ensure_home_org` sur la branche création, best-effort ; plus d'org perso depuis le 05/11/2026) — donc pas pour un
invité, qui n'en reçoit pas (`docs/ownership.md`) : il arrive dans l'org qui l'a invité. Il remonte à
l'agent via la ligne « Projets récents » du bloc C des instructions (`instructions.py`) ;
l'agent l'ouvre (`oto_use_project`) et déroule l'accueil depuis son brief.

## La fiche « situation avec oto »

**La fiche « situation avec oto » reste** (qui est l'user, son métier, ses objectifs, son
CRM, les connecteurs voulus, son ton) — découplée de l'accueil, c'est un data model libre
relu à chaque session :
- **Capacité `me.profile`** (`capabilities/profile.py`, ADR 0042 §Convergence des surfaces) :
  UNE implémentation, deux faces — `oto_profile(op="get"|"update", fields=…)` côté MCP
  (spine, hors gate, **toujours visible** via `PROTECTED_TOOLS`) + `GET`/`PUT /api/me/profile`
  côté dashboard. ⚠️ Divergence VOULUE entre les faces : `op=update` (agent) **filtre les
  valeurs vides** — un agent n'efface pas la fiche par mégarde ; le `PUT` (humain) écrit tel
  quel, donc vider un champ passe. Réponse unique `{profile, updated_at, fields, missing}`.
  *(Avant le 2026-07-28 : tool écrit à la main `tools/profile.py` **doublé** d'une capacité
  REST — deux contrats sur une donnée, et l'éditeur dashboard orphelin. Supprimé.)*
- DB : table `user_account_profile(sub PK, profile jsonb, created_at, updated_at)`
  (`db.get_account_profile` / `db.update_account_profile`). **Injectée au handshake**
  (bloc C, section « Ce que tu sais de l'utilisateur ») → enfin utilisée, plus seulement
  collectée. N'est plus exposée sur `/api/me` (le bloc `onboarding` a été retiré).

## `oto_whoami` — l'identité MCP courante

`tools/whoami.py` (spine, chargé explicitement dans `register_all`, hors gate
d'activation, **toujours visible** via `PROTECTED_TOOLS`) expose `oto_whoami()`
(lecture) — l'**identité MCP courante** sous laquelle Claude agit : compte (`sub` +
email + rôle plateforme) × **org active** (id/name/rôle) × **groupe actif**, plus un
résumé des connecteurs configurés et l'ancre de la KB d'org. C'est le pendant agent du badge
« identité MCP » du dashboard ; à appeler pour confirmer le contexte avant une action
sensible. Pour basculer : `oto_use_org`.

⚠️ **`connectors.platform_quotas` (oto-backend#710)** : pour un connecteur en mode
plateforme au quota jour PLAFONNÉ (ex. apollo), `{used, limit, remaining}` — dérivé
du même calcul que `status_for`/`/api/me`, aucune marche de cascade en plus. Sert
exactement le « avant une action sensible » ci-dessus quand l'action dépense un
quota partagé : un worker batch le regarde avant de lancer un lot d'appels plutôt
que de découvrir la limite au milieu d'un lead. Un connecteur `over_quota` reste
listé dans `platform_available` (il ne disparaît plus une fois épuisé).
