---
title: Charger un gros volume (réseau, export, enrichissement de masse)
description: déléguer à un sous-agent, garder les gros payloads hors contexte, ne remonter qu'un reçu léger
---

# Charger un gros volume sans saturer le contexte

À lire **avant** toute tâche qui va tirer beaucoup de données via oto : copier un
réseau LinkedIn complet (`unipile_relations`), exporter des milliers de lignes,
enrichir une longue liste, boucler sur de la pagination profonde. Ces opérations
renvoient des payloads qui **dépassent le plafond de tokens** d'un résultat d'outil
(ex. `unipile_relations` ≈ 70 Ko/page) et, traitées dans le contexte principal,
le polluent et coûtent cher.

## Le principe : délègue à un sous-agent, ne remonte qu'un reçu

Ne boucle **pas** un gros volume dans ta conversation. Lance un **sous-agent dédié**
qui :

1. **boucle** la récupération (pagination) et garde chaque gros payload **chez lui**,
   hors de ton contexte ;
2. **dédoublonne** par une clé stable (ex. `member_id`, `siren`, une URL) ;
3. **écrit** le résultat dans un tableau (datastore), pas dans sa réponse ;
4. te renvoie seulement un **reçu léger** — p.ex.
   `{distinct, doublons, pages, couverture, 5 exemples}`.

Bénéfice : ton contexte reste propre, le coût est borné, le run long ne te bloque pas.

## Ponter les outils : `oto_call`

⚠️ Un sous-agent que tu lances **hérite du registre d'outils figé** de ta session : si
un connecteur a été activé en cours de session, ses outils ne sont montés ni chez toi
ni chez lui. Le sous-agent doit donc les appeler via **`oto_call(name="…", arguments={…})}`**
(le pont universel), exactement comme toi. Pour un appel direct des nouveaux outils sans
`oto_call`, il faut une **session neuve**.

## Paginer proprement : le curseur

Pour relire un tableau volumineux, utilise le **curseur** de `data_rows` plutôt que de
tout tirer d'un coup : passe `limit`, lis `next_cursor` dans la réponse, et rappelle
`data_rows(cursor=<next_cursor>)` jusqu'à ce que `next_cursor` soit nul. Le curseur est
**stable** (les lignes écrites entre-temps ne décalent pas la pagination).

Côté source (ex. `unipile_relations`), pagine de même page par page ; n'accumule jamais
toutes les pages dans un seul message.

## Écrire en masse

- Beaucoup de lignes d'un coup : `data_write(namespace, rows=[…], key="<clé métier>")`
  en **lots** — la `key` dédoublonne (ré-écrire la même clé met à jour, ne duplique pas).
- Très gros volume / contenu lourd : demande une **URL d'upload** (`oto_upload_url`) et
  laisse le sous-agent y pousser le fichier côté serveur, sans faire transiter les octets
  par le contexte.

## Savoir quand tu as fini (convergence)

Ne conclus pas « fini » après une seule passe. Sur une source paginée dont l'ordre n'est
pas garanti (ex. relations LinkedIn), **itère les décalages** (grilles d'offset) jusqu'à
**deux passes consécutives sans aucune nouvelle ligne** — une seule passe en rate souvent
5–15 %. Récupère si possible la **cible autoritaire** (p.ex. `connections_count` du profil)
pour **mesurer ta couverture** et détecter un plateau réel vs un arrêt prématuré.

## Reçu type à remonter

> Réseau chargé : **2942** distinct sur ~3010 annoncés (couverture 98 %), 4 pages ×
> 4 grilles d'offset, 63 doublons écartés. Écrit dans `linkedin-reseau-n1`. Exemples :
> …
