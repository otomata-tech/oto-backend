---
title: Apps rendues (cartes interactives dans le chat)
description: quand appeler un outil `*_app` plutôt que son équivalent JSON, ce que voit l'utilisateur, replis quand le client ne rend pas
---

# Apps rendues — montrer les données dans la conversation

Certains outils oto ne renvoient pas du JSON mais une **carte interactive rendue
dans le chat** (table triable/cherchable, fiche dépliée, page mise en forme) : ce
sont les **MCP Apps** (extension standard SEP-1865). L'utilisateur voit une vraie
interface sans quitter la conversation ; toi tu reçois quand même le contenu en
données structurées, que tu peux lire normalement.

## Les apps disponibles

| Outil | Ce qu'il rend | Équivalent JSON |
| --- | --- | --- |
| `data_app` | le datastore : sans `namespace` = table de tes tableaux ; avec = table triable des lignes (`filter` exact-match, `show_meta`) ; `row=<id\|clé\|titre>` ou un filtre qui isole 1 ligne = **fiche détail** (sous-records dépliés, statut + cycle de vie) | `data_rows`, `data_list_namespaces` |
| `oto_doc_app` | les pages/docs, **lecture seule** : sans argument = arbre de la Base de connaissance de l'org active ; `project_id` = arbre des pages d'un projet (enfants indentés) ; `doc_id` = une page en Markdown rendu ; `query` = recherche plein-texte avec extraits | `oto_doc` |
| `foncier_site_app` | fiche d'un site (géocodage + parcelle + bâti) | `foncier_geocode`, `foncier_parcelle`, `foncier_bati` |
| `foncier_comparables_app` | ventes DVF comparables autour d'une adresse | `foncier_comparables_adresse` |
| `foncier_prix_m2_app` | stats €/m² d'une commune | `foncier_prix_m2` |

## Quand appeler l'app, quand appeler le JSON

- **L'utilisateur veut VOIR ou explorer** (« montre-moi le tableau », « ouvre la
  fiche », « fais-moi voir cette page ») → l'app. Une table de 50 lignes rendue
  triable vaut mieux qu'un pavé JSON dans ta réponse.
- **Toi tu veux TRAITER les données** (boucler, filtrer, croiser, compter) →
  l'équivalent JSON. L'app est faite pour les yeux de l'utilisateur, pas pour
  l'itération programmatique (pagination limitée, cellules résumées).
- **Écrire** (créer, modifier, supprimer, partager) → jamais par une app : elles
  sont en lecture seule. Utilise l'outil d'écriture (`data_write`, `oto_doc`…).
- Les deux se combinent bien : traite en JSON, puis termine par un appel d'app
  pour donner à l'utilisateur une vue propre du résultat.

## Si la carte ne s'affiche pas

Le rendu dépend du **client** : il exige un hôte qui supporte les MCP Apps
(claude.ai le fait). Dans un client sans support (certaines CLI, agents headless),
le résultat apparaît comme un payload JSON `{"$prefab": …}` — ce n'est **pas une
erreur**, c'est la dégradation prévue. Dans ce cas ne réappelle pas l'app en
boucle : lis le contenu du payload (il contient les données), ou repasse sur
l'outil JSON équivalent, et signale via `feedback(signal='tool_feedback')` si le
comportement semble anormal côté serveur (erreur, carte vide alors que les
données existent).
