---
title: Suspendre une org — arrêter un espace sans rien détruire
type: explanation
---

# Suspendre une org — arrêter un espace sans rien détruire

> Le pendant, au palier de l'org, de la [pause de compte](comptes-en-pause.md).
> Posé le **2026-10-02**. Surfaces : `service.org.suspension` pour le commerce
> (`POST /api/service/orgs/{id}/suspension`, identité de service `commerce`) et
> `admin.org_suspension` pour un super admin (`POST /api/admin/orgs/{id}/suspension`,
> aussi en tool MCP) — même handler, `op=suspend|resume`, `reason` exigé pour
> suspendre.

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
| outils de plateforme écrits à la main (`data_*`, `run_*`, `oto_doc_app`…), directs et via `oto_call` | `activation_gate.require_active` (`org_suspension.outil_garde`) | `org_suspended` (`ErrorData.data.code`) |
| travaux d'agent | `claim_next_job` / `claim_fallback_job` : `NOT EXISTS` sur `orgs.suspended_at` | non réservés, restent en file |
| webhooks entrants | `runner_hook.declencher` | 409 `org_suspended`, journalisé `refused_suspended` |
| cron | `runner_tick._tick` : échéance consommée, rien d'enfilé | journalisé, et livraison `refused_suspended` sur la page de l'agent |
| projet publié sans login (`anonymous`, `secret` : MCP et UI navigable) | `subdomain_project.HostDispatch`, avant tout travail — l'appel n'a pas de `sub`, aucune garde d'appel ne le verrait | 403 `org_suspended` ; retiré de l'annuaire public |

L'org est celle sous laquelle l'appel **résout** (`access.current_org` : `_org`,
projet, run, `X-Oto-Org`, puis l'org par défaut) — jamais l'org « maison » à la place
de l'org visée.

## 3. Ce qui reste ouvert

`org_suspension.OUVERTES` : se voir, lister ses orgs, lire l'org, en changer, la
quitter ; rejoindre une AUTRE org par l'invitation qui y attend (`org.invite.claim`,
qui juge elle-même l'org rejointe `{id}` et refuse `org_suspended` si c'est elle qui
est suspendue), ou par le jeton du mail (`org.invite.accept`, qui juge l'org de
l'INVITATION de même), et décliner une invitation (`org.invite.reject`) ; une maison
suspendue cède alors la place à l'org active rejointe (`members.add_org_member` : la
connexion suivante atterrit sur l'org qui paie, pas sur le mur) ; les gestes de COMPTE (jetons, CGU, avatar) ; ce qui lit ou RETIRE un accès
(statut d'une connexion, déconnexion, effacer une clé) — jamais ce qui en ouvre un ni
ce qui consomme. Raison : les `me.*` résolvent l'org par défaut du compte, et un
membre dont l'espace perso est suspendu ne doit pas perdre ses réglages de compte
alors qu'il travaille dans une org qui paie. La facturation de l'org (`billing.*`
hors `admin_*`) reste ouverte : c'est par elle qu'un admin choisit un plan. Les
opérations `admin.*` et `platform.*` restent ouvertes, et le service du commerce,
sans org, n'est jamais gardé — c'est par eux qu'on suspend et qu'on lève. Côté
outils hors capacité : `oto_whoami`, `oto_tool_schema` et `oto_call` lui-même (sa
cible repasse par la garde, dans l'org que ses axes posent).

**Les capacités d'une app se jugent par l'org de l'APP** (`org_suspension.JUGEES_PAR_L_APP` :
`me.app`, `me.app.call`, `me.app.consent`, 13/11/2026). Un invité de l'extérieur ouvre
une app partagée depuis SON espace (l'org de l'appel) : l'essai de cet espace fini, l'app
d'une org qui paie reste ouverte pour lui ; l'org de l'app suspendue, elle se ferme à
tous. L'adaptateur les laisse passer, leur handler appelle `org_suspension.exiger` sur
l'org de l'app (geste sur une app existante, dépôt compris — `apps_appel._app_pour`) ou
sur l'org de l'appel (lister, créer).

Les routes REST écrites à la main (export CSV d'un tableau, logo, fichiers de projet
en upload) ne passent pas par ces gardes : exporter ses propres données reste
possible, c'est voulu. `tools/list` et le handshake non plus — la liste est servie,
les appels sont refusés.

## 4. Suspendue par son tenant

Désactiver un tenant (`docs/tenants.md` §Désactiver un tenant) suspend toutes ses orgs par
ce même mécanisme, en marquant l'origine : `orgs.suspended_tenant_id` = le tenant (NULL
pour une suspension posée sur l'org elle-même). Sa réactivation ne lève que celles-là.
Tant que le tenant est désactivé, `resume` refuse (`409 tenant_disabled`) ; `suspend`
sur une org qu'il a suspendue reprend la suspension à son compte (auteur, motif, origine
NULL), pour que la réactivation du tenant ne la lève pas.

## 5. Invariants

- **Rien n'est supprimé ni détaché.** Trois colonnes (`orgs.suspended_at`,
  `suspended_by`, `suspended_reason`), posées au boot, NULL = active ; une quatrième,
  `suspended_tenant_id`, dit l'origine (révision `0042_orgs_suspension_par_tenant`).
- **Re-suspendre ne réécrit rien** (auteur, date, motif d'origine), comme la pause de
  compte — sauf une suspension posée par le tenant, que le geste d'org reprend (§4) ;
  `resume` rend `changed=false` sur une org active.
- **Aucune lecture de base par appel.** La liste des orgs suspendues est gardée en
  mémoire par processus et relue toutes les 30 s (`TTL_S`, un balayage de `orgs`,
  petite table ; l'index partiel `idx_orgs_suspended_by` sert la fusion de comptes). Une org active se tranche en mémoire ; seule une org suspendue lit
  son détail (le motif du refus). Une suspension ou une levée atteint les AUTRES
  processus en 30 s au plus ; le processus qui a traité le geste d'admin la voit tout
  de suite (`invalider`). La réservation des travaux, elle, garde dans sa propre
  requête (`NOT EXISTS`) : aucun aller-retour de plus.
- **Fail-closed** : si la PREMIÈRE lecture de la liste échoue, l'appel échoue. Une
  relecture qui échoue ensuite garde la dernière liste connue (une suspension posée
  n'est jamais oubliée sur un hoquet) et retente sous 5 s. Seul le cron journalise et
  continue — la réservation garde de toute façon.
- **Le commerce suspend, un super admin garde la main** (décision du 04/10/2026) :
  suspendre coupe tous les membres d'un espace, c'est un geste de FACTURATION — il
  appartient au service qui facture (ADR 0070 §7 : le commerce écrit des droits), par
  son identité de service, et non à un compte qu'il faudrait garder super admin. Ni
  un admin de l'org ni un admin de tenant ne suspendent ni ne lèvent.

## 6. Le premier appel d'une org

`platform.usage.first_calls` (`GET /api/admin/usage/first-calls?org_ids=…`, admin
plateforme) rend le premier appel journalisé (`tool_calls`, MCP et REST) de chaque
org — l'horloge d'essai d'un tenant qui suspend à la fin de l'essai. Une sonde
d'index par org (`idx_tool_calls_org`). ⚠️ Le journal expire (~30 jours) : la date
glisse vers l'avant passé ce délai, l'appelant la fige de son côté.
