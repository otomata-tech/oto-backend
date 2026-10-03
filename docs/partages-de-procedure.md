---
title: Partager une procédure par lien
type: reference
description: >-
  Une procédure d'org se publie par un lien `/p/<token>` : sans compte on lit sa VITRINE
  (titre, description, auteur, connecteurs, forme du graphe — jamais le corps), connecté
  on lit le corps et on est noté lecteur, puis on peut la copier dans son espace. Le
  propriétaire voit qui a lu et reçoit un résumé quotidien. Tables, faces, règles de ce
  qui reste caché, et le travail de maintenance qui envoie le résumé.
---

# Partager une procédure par lien

Côté produit (Tulina) : « See who's reading ». Un org_admin publie UNE procédure de son
org par un lien. Le lien sert trois publics :

| Qui | Face | Ce qu'il obtient |
|---|---|---|
| Visiteur sans compte | `GET /api/public/process-shares/{token}` (écrite main, `api/public.py`) | la **vitrine** |
| Lecteur connecté | `GET /api/me/process-shares/{token}` (`me.process_share.read`, `SUB_ONLY`) | vitrine + corps ; il est noté lecteur |
| Lecteur connecté | `POST /api/me/process-shares/{token}/copy` (`me.process_share.copy`) | une copie v1 dans son espace |
| Propriétaire | `GET/POST /api/me/instructions/{slug}/share`, `GET …/share/readers` ; `oto_procedure op=share_*` | le lien, ses réglages, ses lecteurs |

Le code : `capabilities/partages_procedure.py` (règles et formes servies),
`db/partages_procedure.py` (store), `digest_lecteurs.py` (résumé quotidien).

## Ce qui reste caché

- **La vitrine ne sert jamais le corps ni un texte d'étape.** Les vitrines anonymes de la
  bibliothèque ont été retirées pour cette raison (oto#84). Un seul constructeur,
  `vitrine()`, sert la face anonyme ET la lecture connectée : elles ne peuvent pas
  diverger sur ce qui reste caché.
- **La forme du graphe est un schéma FERMÉ** (`valider_forme`) : `{v: 1, trigger: null |
  {kind}, steps: [{kind, connectors}]}`, énumérations (`schedule|calendar|chat|event`,
  `ai|human|check`) et slugs de connecteur seulement, 30 étapes et 4 connecteurs par
  étape au plus. Toute autre clé, tout autre type est refusé (422
  `invalid_preview_shape`). Elle est CALCULÉE par le front du propriétaire (qui seul sait
  lire le dessin) et stockée avec la version de procédure dont elle vient
  (`shape_version`) ; la vitrine ne la sert que si cette version est la version courante.
- **Le nombre de lecteurs** n'apparaît en vitrine qu'à partir de cinq, jamais leurs noms.

## Qui est noté lecteur

Une lecture connectée crée ou met à jour une ligne `process_share_readers`, sauf pour un
**membre de l'org propriétaire** (il a déjà accès à la procédure). `recorded` suit le
réglage « voir qui lit » AU MOMENT de la lecture, et ne devient jamais vrai après coup :
une lecture faite sous « ne pas voir » reste invisible même si le réglage est rallumé.
La liste du propriétaire agrège tous les liens de la procédure (un lien retiré puis
republié porte un autre jeton, pas une autre procédure), et exclut aussi les membres
devenus membres depuis.

La première lecture d'un compte pose `acquired_via = {kind: "process_share", token, at}`
sur sa fiche (`user_account_profile.profile`) si elle est vide — c'est ce qui mesure les
comptes nés d'un partage.

## La copie

`org_store.copy_instruction_to_owner` vers l'org active de l'appelant s'il l'administre,
sinon vers son org perso (`ensure_personal_org`). Idempotente : la copie déjà faite par ce
lecteur depuis n'importe quel lien de la procédure est rendue (`created=false`) tant
qu'elle existe. `is_new_account` = l'org cible est perso ET la fiche ne porte ni
`onboarded_at` ni `onboarding_skipped_at` — le front envoie alors vers l'accueil.

## Publier ne sort pas d'une conversation

Le corps devient lisible par quiconque se crée un compte : c'est une ouverture au web.
`op=share_publish` est donc refusé à un agent (`_publication.refuser_si_agent`, 403
`publication_reservee_a_l_humain`) ; retirer, régler et lire les lecteurs lui restent
ouverts. La bibliothèque publique n'est pas touchée : elle reste éditée par la plateforme
(`LIBRARY_PUBLISHER`), et un partage n'est listé nulle part.

## Le résumé quotidien

`oto-mcp maintenance digest-lecteurs`, dans `all` (timer quotidien, PROD seulement) :
un mail par propriétaire (celui qui a publié) listant ses nouveaux lecteurs visibles, par
procédure, avec un bouton vers l'onglet Readers (`<front>/org/<id>/processes/<slug>/readers`).
`digested_at` est posé APRÈS l'envoi ; un refus du mailer laisse les lignes dues.

⚠️ **Fermé par défaut** : sans `OTO_DIGEST_LECTEURS=1`, le travail compte ce qu'il
enverrait et n'envoie ni ne marque rien.

Désinscription : lien signé `/o/r/<token>` (`outreach_optout.lien_lecteurs`, `typ`
`readers_digest_optout`), table `process_readers_digest_optouts` — troisième canal, qui ne
coupe ni les relances ni le digest de signaux.

## Tables

Fragment `db/schema/procedures.py::PROCESS_SHARES`, révision `0033_partages_de_procedure`.
`process_shares.instruction_id` est un lien **logique** vers `org_instructions.id` (cette
colonne naît dans `_init`, après l'assemblage : une FK échouerait sur une base vierge) ;
une procédure supprimée ou archivée rend son lien introuvable (404).

## Abus

La vitrine est bornée par IP avec le seau des projets publiés sans login
(`subdomain_project._check_bucket`, réglages `OTO_ANON_RATE_*`, clé `(ip, 0)`), répond
`no-store` / `no-referrer`, et le jeton (20 caractères aléatoires) est masqué au journal
par son nom de paramètre (`journal_secrets`).
