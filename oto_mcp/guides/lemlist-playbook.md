---
title: Conduire lemlist (lemlist_campaign, lemlist_lead, lemlist_inbox, lemlist_watchlist…)
description: l'ordre de construction d'une campagne, quel outil rend quel id, ce qui déclenche un envoi, et les 8 endroits où l'API lemlist ne fait pas ce que sa doc annonce — à lire avant toute écriture sur lemlist
---

# Conduire lemlist

lemlist est le plus gros connecteur de la boîte à outils : 141 routes, ~30 tools. Les
docstrings disent ce que fait CHAQUE outil ; elles ne peuvent pas dire dans quel ORDRE
les enchaîner, ni d'où vient l'identifiant que le suivant réclame. C'est l'objet de ce
guide.

**Provenance.** Tout ce qui suit a été rejoué sur une vraie clé le 2026-08-31, objets
créés puis supprimés — à l'exception des envois réels (voir « Ce qui envoie »). Là où ce
guide contredit la documentation lemlist, c'est le guide qui a raison : la doc s'est
trompée huit fois, listées plus bas.

## Piège n°1 : une campagne créée est DÉJÀ en marche

`lemlist_campaign(op="create")` rend une campagne dont le `status` affiche **« draft »**
— et dont l'état d'exécution réel est **`running`**. Le `status` est un affichage dérivé
de l'absence d'étape et de lead, pas un interrupteur.

Conséquences, toutes vérifiées :

- `lemlist_campaign_start` répond `400 "You can't start campaigns that are already
  running"` sur une campagne fraîche. Il ne sert qu'à REPRENDRE une campagne en pause.
- `lemlist_campaign(op="pause")` est le vrai interrupteur.
- Rien ne part tant qu'aucun lead n'est **lancé** — le verrou est la revue par lead, pas
  l'état de la campagne. Mais c'est UN verrou, pas deux.

**Donc : crée, puis mets en pause, puis construis.** Une campagne dupliquée
(`op="duplicate"`), elle, naît réellement en pause.

## L'ordre de construction

```
1. lemlist_campaign(op="create", name=…)        → cam_… + sequenceId + scheduleIds
2. lemlist_campaign(op="pause", campaign_id=…)   ← interrupteur coupé pendant qu'on monte
3. lemlist_team(op="user_channels")              → usr_… (utilisateur) + usm_… (boîtes)
4. lemlist_campaign(op="update", sender_user_ids=["usr_…"])
5. lemlist_sequence(op="add_step", sequence_id="seq_…", step={"type":"email", …})
6. lemlist_schedule(op="create"/"associate")     ← si la fenêtre par défaut ne convient pas
7. lemlist_campaign(op="statutes")               ← LIT ce qui bloquerait : level 3 = bloquant
8. lemlist_create_lead(campaign_id=…, email=…)   → lea_… (en attente de revue)
9. lemlist_campaign_start(campaign_id=…)         ← masqué par défaut
10. lemlist_launch_lead(lead_id="lea_…")         ← masqué par défaut : C'EST l'envoi
```

L'étape 7 vaut le détour : `statutes` fait tourner la MÊME validation que l'interface
lemlist et nomme ce qui manque (`level` 3 bloque le lancement — pas d'expéditeur, DNS
cassé ; 2 avertit — limite journalière, planning absent ; 1 informe). La lire évite de
découvrir le problème après coup.

## Contact ≠ Lead

La confusion la plus coûteuse de cette API.

- Un **lead** (`lea_…`) est l'exemplaire d'une personne **dans une campagne** : son état
  d'envoi, ses variables de personnalisation. Outils : `lemlist_create_lead`,
  `lemlist_lead`, `lemlist_launch_lead`.
- Un **contact** (`ctc_…`) est la personne **dans le CRM lemlist**, indépendante de
  toute campagne. Outils : `lemlist_contact`, et c'est lui que l'inbox, les tâches et le
  drapeau do-not-contact désignent.

Créer un lead crée le contact au passage (le retour porte les deux ids).

## Quel outil rend quel identifiant

| Préfixe | Objet | D'où il vient |
|---|---|---|
| `cam_` | campagne | `lemlist_campaign(op="create")`, `lemlist_list_campaigns` |
| `seq_` | séquence | rendu par `create` (`sequenceId`), ou `lemlist_sequence(op="get")` |
| `stp_` | étape | `lemlist_sequence(op="get")` — jamais rendu par la campagne |
| `skd_` | planning | rendu par `create` (`scheduleIds`), ou `lemlist_schedule(op="list")` |
| `lea_` | lead | `lemlist_create_lead`, `lemlist_lead(op="list")` |
| `ctc_` | contact CRM | `lemlist_contact`, ou le champ `contactId` d'un lead |
| `cpn_` | société | `lemlist_company` |
| `usr_` | utilisateur | `lemlist_team(op="team"/"senders"/"user_channels")` |
| `usm_` | boîte mail | `lemlist_team(op="user_channels")` — requis par les envois d'inbox |
| `clt_` | liste de contacts | `lemlist_contact(op="lists")` |
| `lbl_` | libellé d'inbox | `lemlist_inbox(op="labels")` |
| `wat_` | watch list | `lemlist_watchlist(op="list")` |
| `dac_` | alerte délivrabilité | `lemlist_deliverability(op="list")` |
| `hoo_` | webhook | `lemlist_webhook(op="list")` |
| `pdp_` | persona | `lemlist_database(op="personas")` |
| `enr_` | enrichissement | `lemlist_enrich*` — à repasser à `lemlist_enrich_result` |

Règle générale : un id d'étape ne s'obtient qu'en relisant la séquence, et les ids
d'expéditeur qu'en passant par `lemlist_team`. Ne les invente pas.

## Ce qui envoie

Quatre outils envoient — ou **arment** l'envoi. Tous les quatre sont **masqués par
défaut** : ils restent appelables, mais il faut les activer
(`oto_enable_tool <nom>`), ou passer par `oto_call`.

- `lemlist_campaign_start` — déroule la séquence pour tous les leads lancés.
- `lemlist_launch_lead` — sort UN lead de la revue manuelle. C'est le geste d'envoi le
  plus courant.
- `lemlist_inbox_send` — email / LinkedIn / WhatsApp **directs** : ni campagne, ni
  séquence, ni revue devant eux. Le message part.
- `lemlist_campaign_auto_review` — n'envoie rien lui-même, mais fait partir tout lead
  **ajouté ensuite**. Avec lui, `lemlist_create_lead` devient un envoi.

Deux surfaces envoient **indirectement** et restent visibles : une watch list réglée sur
`push_to_campaign` alimente une campagne toute seule, et
`lemlist_mailbox(op="lemwarm_start")` envoie dans le réseau de chauffe (d'autres boîtes
lemlist), jamais vers un prospect.

⚠️ Les trois envois d'inbox n'ont **pas** été rejoués en vrai : ils ont été prouvés
jusqu'à la validation de lemlist avec des ids bidons. Le reste de ce guide l'a été.

## Si tu vois cette erreur — les 8 écarts doc↔API

Le connecteur absorbe déjà ces écarts ; c'est ici pour reconnaître un message et savoir
quoi faire, pas pour agir.

| Erreur / symptôme | Cause réelle |
|---|---|
| `recordId is required` sur une tâche | `record_id` (le `ctc_…` ou `lea_…` porteur) est obligatoire, malgré la doc. |
| `Malformed filters` sur les tâches | `GET /tasks` exige `filters` ; le connecteur envoie `[]` d'office. |
| `segmentType is invalid` / `activate requires both segmentType and signalProcessingType` | Sur une watch list, `filters`, `segmentType`, `signalProcessingType` et `activate` sont TOUS obligatoires. Le message accuse les mauvais champs : c'est `activate` qui manque. |
| `WRONG_METADATA_FORMAT` sur un bulk d'enrichissement | Les valeurs de `metadata` doivent être des **chaînes** : `{"row": "1"}`, pas `{"row": 1}`. |
| Un export de leads rend une liste **vide** sur une campagne qui en a | Le défaut de lemlist filtre tout ; le connecteur force `state="all"` sur les **trois** routes de lecture de leads (`lemlist_lead op=list`, `lemlist_get_leads`, `lemlist_campaign op=export_leads`). Si tu passes `state` toi-même, sache ce que tu filtres. ⚠️ Le forçage n'existait que sur la route d'export jusqu'au 05/09/2026 : les deux autres rendaient `[]` sur une campagne pleine, alors que cette ligne annonçait déjà le contraire pour le connecteur entier. |
| Savoir si un lead ajouté va PARTIR, avant d'écrire | `lemlist_campaign op="reports"` rend `totalCount`, `reviewedCount`, `inSequenceLeadCount` et `emailsSent`. Ces quatre nombres prouvent l'état du verrou de revue **sans envoyer quoi que ce soit** — mesuré le 04/09/2026 sur une campagne d'un lead : `1 / 0 / 0 / 0`. Ni `op="get"` ni la fiche du lead ne le disent : `isPaused` ne distingue pas « en attente de revue » de « prêt à partir ». |
| `already running` sur un start | Cf. piège n°1. |
| `You can't pause campaigns that are not running` | Symétrique : la pause refuse une campagne à l'arrêt. |
| `string indices must be integers` (historique) | `GET /campaigns?version=v2` rend un objet, pas un tableau. Corrigé ; si ça réapparaît, la forme a rebougé. |

Autres refus qui ne sont **pas** des bugs, mais des limites de compte : étapes LinkedIn
(`Upgrade your plan`), endpoints CRM (`crm_filters`, `crm_users`,
`lemlist_lead(op="import_crm")` → « Endpoint not available » sans intégration CRM),
historique de watch list (bêta non activée), A/B tests (plan Email Pro).

## Watch lists : lis les filtres avant d'écrire

`lemlist_watchlist(op="create")` échoue si on devine la charge. Le contrat :

1. `lemlist_watchlist(op="filters", watch_type=…)` → les filtres de CE type, avec ceux
   marqués requis. Ex. `companyIsHiring` exige `title`, `location` et
   `maxIdentificationsPerDay`.
2. `lemlist_watchlist(op="filter_values", filter_id=…, query=…)` → les valeurs
   **canoniques**. Une chaîne libre est rejetée (`INVALID_FILTER_VALUE`) : « Head of
   Sales » ne passe pas, « head of operations » oui.
3. Les nombres voyagent en **chaînes** :
   `{"filterId": "maxIdentificationsPerDay", "in": ["5"]}`.

Une liste naît en brouillon (`activate=False`) — c'est voulu : activée avec
`signal_processing_type="push_to_campaign"`, elle alimente une campagne sans autre appel.

## Désinscriptions : trois listes qui ne se parlent pas

`lemlist_unsubscribe` couvre trois registres distincts, et écrire dans l'un n'écrit pas
dans les autres :

- `op="add"/"get"/"list"/"delete"` — emails **et domaines** (v1) ;
- `op="var_*"` — n'importe quelle valeur identifiante (email, domaine, URL LinkedIn,
  téléphone), avec `var_bulk` jusqu'à 10 000 ;
- `op="contact_*"` — le drapeau do-not-contact posé sur un **contact** du CRM.

Pour « ne plus jamais contacter cette personne », le drapeau contact est le plus sûr ;
pour « bannir ce domaine », c'est la v1.

## Deux gestes dont le défaut est le doux

- `lemlist_lead(op="delete")` supprime, `op="unsubscribe"` désinscrit en laissant le lead
  sur la campagne. Une seule route lemlist sert les deux, et son défaut est la
  désinscription — ici les deux ops sont nommées à part.
- `lemlist_lead(op="pause")` **sans** `campaign_id` met le lead en pause sur TOUTES ses
  campagnes, pas sur une. La portée large est le défaut.

## Coûts

L'enrichissement (`lemlist_enrich`, `lemlist_enrich_lead`, `lemlist_enrich_bulk`) dépense
des crédits lemlist à chaque action — `lemlist_team(op="credits")` les compte. Les
recherches dans la base partagée (`lemlist_database`) n'en dépensent pas.
