---
title: Suspendre une org — arrêter un espace sans rien détruire
type: explanation
---

# Suspendre une org — arrêter un espace sans rien détruire

> Le pendant, au palier de l'org, de la [pause de compte](comptes-en-pause.md).
> Posé le **2026-10-02**. Surface : capacité `admin.org_suspension`, route REST
> `POST /api/admin/orgs/{id}/suspension` (`op=suspend|resume`, `reason` exigé pour
> suspendre). Pas de tool MCP.

## 1. Le besoin

Un tenant qui facture ses orgs coupe les clés qu'il paie quand une org ne paie pas
(retrait de grant, arête de tenant archivée). Ça ne suffit pas quand la règle est
« pas d'abonnement = l'espace s'arrête » : tout ce qui ne passe pas par une clé
payée — procédures, agents hébergés, tableaux, connecteurs sur la clé du client —
continuait. Une pause de COMPTE ne convient pas non plus : elle arrête une personne
dans toutes ses orgs, là où il faut arrêter un espace pour tous ses membres, et
seulement lui.

## 2. Ce que la suspension arrête

| porte | garde | refus |
|---|---|---|
| capacités (MCP et REST) | `org_suspension.garde_capacite`, dans les deux adaptateurs juste après la règle d'autz | `org_suspended` 403 |
| outils de connecteur, directs et via `oto_call` | `activation_gate._refus`, avant l'activation | `org_suspended` (`ErrorData.data.code`) |
| travaux d'agent | `claim_next_job` / `claim_fallback_job` : `NOT EXISTS` sur `orgs.suspended_at` | non réservés, restent en file |
| webhooks entrants | `runner_hook.declencher` | 409 `org_suspended`, journalisé `refused_suspended` |
| cron | `runner_tick._tick` : échéance consommée, rien d'enfilé | — |

L'org est celle sous laquelle l'appel **résout** (`access.current_org` : `_org`,
projet, run, `X-Oto-Org`, puis l'org par défaut) — jamais l'org « maison » à la place
de l'org visée.

## 3. Ce qui reste ouvert

`org_suspension.OUVERTES` : se voir, lister ses orgs, lire l'org, en changer, la
quitter ; les gestes de COMPTE (jetons, CGU, avatar) ; ce qui lit ou RETIRE un accès
(statut d'une connexion, déconnexion, effacer une clé) — jamais ce qui en ouvre un ni
ce qui consomme. Raison : les `me.*` résolvent l'org par défaut du compte, et un
membre dont l'espace perso est suspendu ne doit pas perdre ses réglages de compte
alors qu'il travaille dans une org qui paie. Les opérations `admin.*` restent ouvertes
— c'est par elles qu'on lève la suspension.

Les routes REST écrites à la main (export CSV d'un tableau, logo, fichiers de projet
en upload) ne passent pas par ces gardes : exporter ses propres données reste
possible, c'est voulu. `tools/list` et le handshake non plus — la liste est servie,
les appels sont refusés.

## 4. Invariants

- **Rien n'est supprimé ni détaché.** Trois colonnes (`orgs.suspended_at`,
  `suspended_by`, `suspended_reason`), posées au boot, NULL = active.
- **Re-suspendre ne réécrit rien** (auteur, date, motif d'origine), comme la pause de
  compte ; `resume` rend `changed=false` sur une org active.
- **Aucune lecture de base par appel.** La liste des orgs suspendues est gardée en
  mémoire par processus et relue toutes les 30 s (`TTL_S`, une requête servie par
  l'index partiel). Une org active se tranche en mémoire ; seule une org suspendue lit
  son détail (le motif du refus). Une suspension ou une levée atteint les AUTRES
  processus en 30 s au plus ; le processus qui a traité le geste d'admin la voit tout
  de suite (`invalider`). La réservation des travaux, elle, garde dans sa propre
  requête (`NOT EXISTS`) : aucun aller-retour de plus.
- **Fail-closed** : si la PREMIÈRE lecture de la liste échoue, l'appel échoue. Une
  relecture qui échoue ensuite garde la dernière liste connue (une suspension posée
  n'est jamais oubliée sur un hoquet) et retente sous 5 s. Seul le cron journalise et
  continue — la réservation garde de toute façon.
- **Super admin seulement** : suspendre coupe tous les membres d'un espace, c'est un
  geste de facturation, pas d'administration de l'org.

## 5. Le premier appel d'une org

`platform.usage.first_calls` (`GET /api/admin/usage/first-calls?org_ids=…`, admin
plateforme) rend le premier appel journalisé (`tool_calls`, MCP et REST) de chaque
org — l'horloge d'essai d'un tenant qui suspend à la fin de l'essai. Une sonde
d'index par org (`idx_tool_calls_org`). ⚠️ Le journal expire (~90 jours) : la date
glisse vers l'avant passé ce délai, l'appelant la fige de son côté.
