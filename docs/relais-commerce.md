---
title: Relais de lecture vers le commerce — `oto_credits`
type: explanation
---

# Relais de lecture vers le commerce — `oto_credits`

## Le besoin

Le cœur ne tient pas les crédits : une unité y est un entier, que seul le commerce convertit
(`credits-au-moment-de-l-appel.md`), et ce sont les pages du commerce qui disent le solde
de la période, l'allocation, ce qui est coupé et pourquoi, le barème et les limites de
dépense (`limites-de-depense.md`). Un membre les lit là. Un agent, lui, ne lisait rien :
il lançait une campagne sans savoir ce qu'il restait, et découvrait l'épuisement au refus
d'un appel, au milieu du travail.

## Ce que fait le relais

`oto_credits` (capacité `org.commerce.credits`, `oto_mcp/capabilities/commerce_relay.py`)
demande au commerce **au moment de l'appel** et rend sa réponse sous `result`. Rien n'est
recopié ni mis en cache côté cœur : le commerce reste la seule source.

| op | chemin chez le commerce | autz ici |
|---|---|---|
| `usage` | `GET /orgs/{org_id}/credits` | membre de l'org |
| `daily` | `GET /orgs/{org_id}/credits/daily?days=7\|30\|90` | membre (le commerce réserve à l'admin) |
| `agents` | `GET /orgs/{org_id}/credits/agents?days=…` | idem |
| `spend_limits` | `GET /orgs/{org_id}/spend-limits` | membre (le commerce borne ce qu'il voit) |
| `pricing` | `GET /pricing` | tout compte |

**Lecture seule, exprès.** Une limite, un plan, une recharge se changent sur les pages du
commerce, où un humain les voit. Un geste d'écriture relayé ferait valoir un rôle que le
cœur affirme dans un en-tête : un agent tournant sous un admin relèverait son propre
plafond.

## Le contrat avec le commerce

- **Config par instance, sans défaut** — même modèle que le crochet de création d'org
  (`org_created_hook.py`) : `OTO_COMMERCE_RELAY_URL` (base, sans `/` final) et
  `OTO_COMMERCE_RELAY_TOKEN` (`Authorization: Bearer`). L'une vide = **outil non monté**
  (`gate`) ; on n'envoie jamais un appel de service sans secret. Le jeton est distinct de
  celui du crochet : ce qu'il ouvre est différent.
- **Qui appelle** : `X-Oto-Sub` (la personne) et `X-Oto-Org-Role` — son rôle **dans l'org**
  (`org_admin` | `org_member`, par `roles.effective_org_role`, escalade plateforme
  incluse). ⚠️ Jamais `ResolvedCtx.role`, qui porte le rôle de **plateforme**.
- **Le cœur vérifie l'appartenance d'abord** (règle d'autz) : le commerce ne reçoit jamais
  une org que l'appelant ne voit pas. Ce qu'un membre non admin y lit, le commerce le
  décide seul ; un `403 admin_required` du commerce est rendu tel quel.
- **Tenant primaire seulement**, comme le crochet : l'org d'un tenant hébergé n'est pas
  facturée par le commerce de l'instance (`no_commerce`). Tenant primaire non déclaré =
  refus, on ne devine pas.
- **Le commerce ne rappelle pas le cœur** pendant une lecture relayée : ce serait faire
  attendre l'appel sur la lecture la plus lente du cœur, deux fois.

## Refus

`commerce_not_configured` (503), `no_commerce` (404), `admin_required` (403),
`commerce_unreachable` (502, délai de 20 s), `commerce_error` (502 — statut ≥ 400 ou corps
qui n'est pas un objet JSON). Le secret n'apparaît dans aucun refus ni journal.
