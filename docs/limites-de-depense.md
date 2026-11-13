---
title: Limites de dépense du jour — le commerce décide, le cœur applique
type: explanation
---

# Limites de dépense du jour — le commerce décide, le cœur applique

> Posé le **2026-11-13**. Surfaces : `service.org.spend_blocks.put|get`
> (`PUT|GET /api/service/orgs/{id}/spend-blocks`, identité de service `commerce`),
> `service.org.usage.calls` (`GET /api/service/orgs/{id}/usage/calls`),
> `service.org.groups` (`GET /api/service/orgs/{id}/groups`), et la colonne `sub` du flux
> `service.credits.movements`.

## 1. Le besoin

Un admin d'org veut borner ce que son org dépense **par jour**, en crédits : pour l'org
entière, pour une équipe, pour une personne. Les crédits, leurs prix et leur solde sont
tenus par le **commerce**, pas par le cœur : c'est donc lui qui garde les limites, compte la
dépense du jour et décide qui est bloqué. Le cœur n'y ajoute **aucun** second budget —
il n'a ni limite, ni compteur de dépense : il applique une liste.

Deux choses manquaient au commerce pour le faire :

- **qui a dépensé.** Le payeur d'un mouvement est l'org par défaut (`credit_payer_modes`),
  et le relevé des appels lu par les membres (`org.usage.calls`) est volontairement sans
  identité ;
- **un levier par personne.** Tous ses leviers (retrait de grant, arête de tenant,
  suspension) arrêtent une org entière.

## 2. Qui a dépensé

| ce que le commerce lit | la personne | d'où elle vient |
|---|---|---|
| appels d'outils (`GET /api/service/orgs/{id}/usage/calls`) | `sub` de chaque ligne | `tool_calls.sub` — l'auteur d'un agent hébergé pour ses appels |
| tours de modèle (`GET /api/service/credits/movements`) | `sub` de chaque mouvement | `credit_holds.acting_sub`, posée à la réservation |

⚠️ La lentille **membre** (`org.usage.calls`) reste sans identité : la même lecture que
`list_billable_calls_for_org`, sans `avec_personne`. Un membre voit ce que son org
consomme, pas qui a fait quoi.

`acting_sub` est la personne qui a agi, **pas le payeur** : un tour d'agent hébergé porte
son auteur (`runner_jobs.sub`), un tour de l'agent intégré la personne de la
conversation. NULL = versement, réservation d'avant la colonne, ou travail sans auteur
— le commerce le compte à l'org seulement.

## 3. Les blocages

Le commerce pousse, par org, la liste **entière** de ce qui est bloqué
(`PUT …/spend-blocks`, `[]` lève tout) :

- `scope=org`, sans `sub` : tous les membres ;
- `scope=group` + `group_id` + `sub` : une personne, parce que la limite d'une de ses
  équipes est atteinte. Une équipe est un POT COMMUN : la dépense de tous ses membres
  actuels, quelle que soit leur équipe active (`service.org.groups` les rend tous) ; le
  commerce somme, puis déplie l'équipe en ses membres ;
- `scope=member` + `sub` : une personne, pour sa propre limite.

Un blocage finit à `until` (la remise à zéro, au plus 48 h devant) : **passé cet instant,
il ne bloque plus**, même si le commerce ne l'a pas retiré. Une journée finie se rouvre
seule.

## 4. Les portes — clés refacturées seulement

| porte | garde | refus |
|---|---|---|
| outil (direct, `oto_call`, agent hébergé) | `access/resolve.py`, quand la clé de plateforme ou celle du tenant primaire gagne, avant la réservation | `daily_spend_limit` (`ErrorData.data.code`, `scope`, `until`) |
| travail d'agent hébergé, à sa réservation | `runner_jobs` (clé partagée refacturée), avant le plancher | travail arrêté, raison `daily_spend_limit : …` |
| tour de modèle d'un agent hébergé | `runner_jobs._reserver_un_tour` | `daily_spend_limit` 429 |
| tour de l'agent intégré | `copilot/cle.reserver` | `daily_spend_limit` 429 |

La clé de l'org ou de la personne n'est **jamais** refusée : elle ne consomme pas de
crédits. Le refus le dit, et dit quand ça reprend.

⚠️ **Portée assumée, à revoir si besoin** : la porte de l'outil refuse TOUTE clé
refacturée qui gagne, quel que soit le connecteur — y compris un connecteur que le
commerce ne facture pas à l'appel (un compte de messagerie hébergé, facturé au siège).
Le cœur ne connaît pas les prix du commerce ; resserrer la porte demanderait que le
commerce pousse aussi la liste des connecteurs concernés.

## 5. Choix assumés

- **Fail-open**, à l'inverse de la suspension : une limite est un garde-fou que le client
  se pose. Une liste illisible ne bloque personne ; la dernière liste lue reste servie.
- **Aucune lecture de base par appel** : la liste des blocages en cours est en mémoire,
  relue toutes les 30 s par processus (`limite_du_jour.TTL_S`), comme celle des orgs
  suspendues. Un blocage atteint les autres processus en 30 s au plus.
- **Le dépassement est borné par le commerce, pas ici** : il lit l'usage par relevés
  successifs, une limite peut donc être dépassée de quelques minutes de dépense. Le cœur
  n'essaie pas de le rattraper : il n'a pas les prix.
- **Pas de limite sans admin** : le cœur n'a ni défaut ni plafond de plateforme. Sans
  blocage poussé, rien ne change.
