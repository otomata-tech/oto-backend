---
title: Apps rendues (cartes interactives dans le chat)
description: quand appeler un outil `*_app` plutôt que son équivalent JSON, ce que voit l'utilisateur, replis quand le client ne rend pas
---

# Apps rendues — montrer les données dans la conversation

Certains outils oto ne renvoient pas du JSON mais une **carte interactive rendue
dans le chat** (table triable/cherchable, fiche dépliée, page mise en forme) : ce
sont les **MCP Apps** (extension standard SEP-1865). L'utilisateur voit une vraie
interface sans quitter la conversation. Ce que TOI tu lis, c'est le texte du
résultat — la carte, tu ne la vois pas. `oto_doc_app` y met le contenu entier (la page,
l'arbre, les extraits) ; les autres apps n'y mettent qu'un marqueur
`[Rendered Prefab UI]` : pour lire leurs données, appelle l'outil JSON équivalent.
Ne décris jamais une carte dont tu n'as pas lu le contenu.

## Les apps disponibles

| Outil | Ce qu'il rend | Équivalent JSON |
| --- | --- | --- |
| `data_app` | le datastore : sans `datastore` = table de tes tableaux ; avec = table triable des lignes (`filter` exact-match, `show_meta`) ; `row=<id\|clé\|titre>` ou un filtre qui isole 1 ligne = **fiche détail** (sous-records dépliés, statut + cycle de vie) | `data_rows`, `data_list_datastores` |
| `oto_doc_app` | les pages/docs, **lecture seule** : sans argument = arbre du projet de documents HISTORIQUE de l'org active, quel que soit son nom (résolu par son ancre) — passe `project_id` pour le projet que tu vises vraiment ; `project_id` = arbre des pages d'un projet (enfants indentés) ; `doc_id` = une page en Markdown rendu ; `query` = recherche plein-texte avec extraits | `oto_doc` |
| `oto_connector_app` | la carte « Connecter » d'UN connecteur : son nom, ce que la personne fera (coller une clé, autoriser, se connecter), un bouton qui OUVRE sa page dans son produit — **à montrer au lieu de demander une clé dans la conversation** ; ne prend aucun secret, n'appelle aucun outil ; ensuite `oto_connector(op="list", name=…)` pour savoir si c'est fait | `oto_connector(op="setup_link")` |
| `data_review_app` | la **file de revue** d'un tableau : UNE ligne à la fois au statut `pending` (titre, 4 champs, la note), deux boutons — l'utilisateur clique `approve` ou `reject`, le statut s'écrit, la carte passe à la suivante. Pour l'étape HUMAINE d'une procédure (« une personne relit et lance ») | `data_rows` + `data_write` |
| `foncier_site_app` | fiche d'un site (géocodage + parcelle + bâti) | `foncier_geocode`, `foncier_site(op="parcelle")`, `foncier_site(op="bati")` |
| `foncier_comparables_app` | ventes DVF comparables autour d'une adresse | `foncier_dvf(op="comparables_adresse")` |
| `foncier_prix_m2_app` | stats €/m² d'une commune | `foncier_dvf(op="prix_m2")` |

## Quand appeler l'app, quand appeler le JSON

- **L'utilisateur veut VOIR ou explorer** (« montre-moi le tableau », « ouvre la
  fiche », « fais-moi voir cette page ») → l'app. Une table de 50 lignes rendue
  triable vaut mieux qu'un pavé JSON dans ta réponse.
- **Toi tu veux TRAITER les données** (boucler, filtrer, croiser, compter) →
  l'équivalent JSON. L'app est faite pour les yeux de l'utilisateur, pas pour
  l'itération programmatique (pagination limitée, cellules résumées).
- **Écrire** (créer, modifier, supprimer, partager) → jamais par une app, à UNE
  exception : `data_review_app`, où c'est **l'utilisateur** qui tranche en cliquant
  (un statut, sur une ligne encore en attente — une ligne changée entre-temps est
  sautée). Ne l'ouvre pas pour décider à sa place : si c'est à toi de trancher,
  `data_write`. La carte écrit un statut, elle n'envoie ni ne lance rien dans un autre
  outil — dis-le si l'étape suivante (un envoi, un lancement) reste à faire. En fin de
  file, l'utilisateur peut cliquer « Continue in chat » : son message « Done reviewing:
  2 launched, 1 skipped. » est le signal de reprendre la procédure.
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
