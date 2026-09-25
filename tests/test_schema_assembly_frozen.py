"""Le DDL assemblé est GELÉ : découper `_schema.py` ne devait rien changer à la chaîne.

Le DDL vivait dans un littéral unique de 1 578 lignes ; il vit maintenant dans
`db/schema/<domaine>.py`, que `_schema.ASSEMBLAGE` concatène dans un ordre figé.
Le déplacement était PUR — pas un espace de différence — et c'est exactement ce
qu'un déplacement de fichiers ne prouve pas tout seul : la seule preuve possible
est l'empreinte de la chaîne servie.

Ce que le gel protège, une fois le déplacement fait :

1. **Une base PARTAGÉE prod/preprod** (`docs/live-migrations.md`) : le DDL exécuté
   au boot preprod s'applique instantanément à la production, qui tourne encore
   l'ancien code. Une altération accidentelle du DDL n'a pas de fenêtre de rattrapage.
2. **L'ORDRE est une contrainte d'exécution**, pas une mise en page : PostgreSQL
   crée les tables dans l'ordre du DDL, et une FK vers une table pas encore créée
   échoue sur une base VIERGE (#151 sur `orgs`, `tenants` avant `orgs` en L1,
   `grants` avant `grant_counters` en L4). Un simple réordonnancement de la liste
   d'assemblage — le genre de geste qu'un tri alphabétique « propre » produit —
   casserait tout premier boot, et rien d'autre ne le verrait.
3. **Un fragment orphelin est silencieux** : une constante déclarée dans un module
   de domaine mais absente de `ASSEMBLAGE` ne lève aucune erreur ; ses tables
   n'existent simplement jamais.

⚠️ **Ce hash se met à jour À LA MAIN**, dans le commit qui change le DDL, jamais
séparément. Un changement de DDL légitime le fait échouer : c'est voulu — il rend
l'écriture du DDL délibérée et visible en revue, au même titre qu'un `ALTER` de
`_init.py`. Recalculer :

    python -c "import hashlib;from oto_mcp.db import _schema;\
print(hashlib.sha256(_schema._SCHEMA.encode()).hexdigest())"
"""
from __future__ import annotations

import hashlib
import re

from oto_mcp.db import _schema, schema

# Empreinte de `_SCHEMA`, mise à jour dans le commit qui change le DDL —
# jamais recopiée d'un côté d'un conflit : deux lots qui touchent le DDL
# la recalculent sur le résultat FUSIONNÉ, sinon la garde valide un DDL que
# personne n'a servi. Cf. l'avertissement du docstring avant de la toucher.
# 2026-08-28 (#493) : `billing_payments.customer_id` — le journal porte le customer
# Mollie de la tentative, seule mémoire du customer d'une org avant le 1ᵉʳ `confirm`.
# 2026-08-28 (#486) : la TVA. Deux changements dans le même lot — la table NEUVE
# `billing_identities` (qui paie, depuis quel pays, sous quel n° de TVA : le pays
# décide du montant débité) et cinq colonnes NULLABLES sur `billing_payments`
# (amount_ht, vat_rate_bps, vat_amount, country_code, vat_scheme), qui figent la
# décomposition fiscale de chaque tentative. Les colonnes existent aussi en ALTER
# dans `_init.py` : la base est PARTAGÉE prod/preprod, le CREATE TABLE ne sert
# qu'aux installs vierges.
# 2026-08-28 (#487) : le JOURNAL des acceptations, table NEUVE
# (`legal_acceptance_events` + son index). **Lot A additif** : `legal_acceptances` et
# sa PK `(sub, doc_slug)` ne bougent PAS — le code servi en prod y fait encore son
# `ON CONFLICT`, et la base est partagée. La projection devient transitoire (écriture
# double datée) et part avec l'issue #507 ; c'est ce drop-là qui sera le DDL
# destructif, une fois la prod sur le code qui lit le journal.
# 2026-08-28 (#488) : la FACTURE. Table NEUVE `billing_invoices` (+ ses deux index),
# une ligne par document émis chez Pennylane pour un encaissement — ou par tentative
# restée en attente. Lot ADDITIF : rien n'est retiré ni contraint sur les tables
# existantes, la base étant partagée prod/preprod. Sa clé d'unicité est
# `(payment_row_id, kind)` — un webhook rejoué ne crée ni seconde facture ni second
# avoir. Elle référence `billing_payments`, d'où sa place APRÈS le fragment
# SUBSCRIPTIONS dans `_schema.ASSEMBLAGE` : une FK vers une table pas encore créée
# échoue sur une base vierge.
# 2026-08-28 (#519, lot A) : AUCUN changement de SQL. Un seul COMMENTAIRE bouge —
# il pointait un module que ce lot renomme en `guide_run.py` ; un
# pointeur par nom de fichier qui survit à un renommage est un pointeur mort.
# Les objets servis (table `doctrine_library`, colonne `runs.doctrine`, index
# `idx_doctrine_library_*`) ne bougent PAS : la base est partagée prod/preprod,
# leur renommage est additif et part au lot B.
# 2026-08-28 (L6 pièce 2, maintenance) : `connector_instances.revoked_reason`, une
# colonne NULLABLE. Additive et sans index : le `CREATE TABLE` ne sert qu'aux installs
# vierges, la base PARTAGÉE la reçoit par l'`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
# de `_init.py`. Rien n'est retiré ni contraint — la prod qui tourne l'ancien code ne
# la lit pas. Elle porte POURQUOI une instance a été archivée : sans elle, « la clé a
# été retirée » et « on a réparé une orpheline d'avant le lot » sont indistinguables.
# 2026-08-28 (ADR 0065 lot 0, #426) : **AUCUN ordre SQL n'a changé** — la seule
# différence est un COMMENTAIRE SQL au-dessus de `tool_calls`, qui affirmait une
# volumétrie « bornée par un prune au boot » devenue fausse (la rétention est passée au
# timer d'archivage). L'empreinte porte la chaîne entière, commentaires compris, donc
# elle bouge ; la preuve que l'EXÉCUTÉ est intact est ailleurs, et elle est plus forte :
# `tests/test_boot_order_replay.py` compare l'empreinte de SCHÉMA (colonnes, index,
# contraintes) avant et après un rejeu, et l'empreinte du `_SCHEMA` COMMENTAIRES
# RETIRÉS est identique à celle de `main` (e1c50269…, 31 021 caractères).
# ⚠️ Valeurs RECALCULÉES sur le résultat du rebase, jamais recopiées d'un côté :
# quatre lots ont touché ce DDL le même jour.
# 2026-08-29 (L6 pièce 2 c2) : la table NEUVE `connector_settings` — les propriétés
# de connecteur surchargeables (`cardinality` aujourd'hui). Le patron du bloc
# d'instructions : constante du registre = défaut, ligne DB = surcharge, pour qu'un
# élargissement ne demande pas un déploiement. ADDITIVE : rien n'est retiré ni
# contraint sur les tables existantes, et la prod qui tourne l'ancien code ne la lit
# pas. Sans ligne posée, comportement et empreinte identiques.
# 2026-08-29 (L7, PR 1 — fenêtre de double lecture, blueprint ADR 0053) : la table
# NEUVE `access_shadow_l7`, compteur de la comparaison entre la chaîne de grants et
# la cascade. ADDITIVE et sans lecteur sur le chemin servi : rien n'est retiré, rien
# n'est contraint sur les tables existantes, et la prod qui tourne l'ancien code ne
# la voit pas. Elle se retire par un `DROP TABLE` — c'est ce qui rend cette PR
# réversible, l'irréversible étant le RETRAIT de `walk_cascade`, deux PR plus loin.
# 2026-09-01 (arrêt de la recopie, ADR 0054/0063) : `nodes` gagne QUATRE colonnes —
# `data`, qui sort la donnée métier de `props` (mêlées, une cellule nommée `title`
# écrase le titre du nœud), et `claimed_run`/`claims`/`abandon_reason`, qui complètent
# le bail pour que la file de travail reste UNE mécanique servant deux tables. ADDITIVE
# et sans réécriture : `ADD COLUMN` avec un `DEFAULT` constant est instantané (PG >= 11),
# et la prod qui tourne l'ancien code ne lit pas ces colonnes. Coût mesuré au banc avant
# de les poser : +4,7 % de volume, -14 % de temps d'écriture.
# 2026-09-01 (oto-private#85, scrub) : AUCUN changement de SQL exécuté. Trois
# COMMENTAIRES bougent — `legal.py`, `connectors.py` (×2), `tenants.py` — qui
# nommaient un tenant tiers ou une personne réelle en clair (règle du meta-repo :
# jamais de nom de client ni de personne dans un dépôt public). Remplacés par des
# exemples génériques (« un tenant tiers », « le compte de Jane »).
# 2026-09-01 (refus d'invitation, #654) : `org_invitations` gagne DEUX colonnes —
# `declined_at`/`declined_sub`, le « non » de l'invité, jusqu'ici impossible à dire
# (seul l'émetteur pouvait retirer une invitation). État PROPRE et non `accepted_at`
# réutilisé : les deux gestes doivent rester discernables partout, et l'acceptation
# idempotente (`_idempotent_accept`) aurait resservi un succès « tu as rejoint l'org »
# à qui vient de refuser. ADDITIVE, sans index et sans réécriture (`ADD COLUMN` NULL
# est instantané) ; la prod qui tourne l'ancien code ne les lit pas — elle continue
# simplement de ne jamais en voir de renseignées.
# ⚠️ Empreinte RECALCULÉE depuis le module assemblé sur le résultat du REBASE des
# TROIS lots ci-dessus (les quatre colonnes de `nodes`, puis le scrub, puis ces deux
# colonnes), jamais recopiée d'un côté du conflit : trois lots ont touché la chaîne
# le même jour, et un hash pris d'un seul côté valide un DDL que personne ne sert.
# 2026-09-01 (#781, cliquet du boot sur base existante) : UN ordre RETIRÉ du DDL
# assemblé — `CREATE INDEX IF NOT EXISTS idx_unipile_accounts_org` dans
# `schema/unipile.py`. Il était posé aux DEUX endroits : ici, et dans `_init.py`
# juste après l'`ALTER … ADD COLUMN org_id`. Or `org_id` peut MANQUER à une base qui
# existe déjà (le `CREATE TABLE IF NOT EXISTS` y est sauté) et le DDL assemblé
# s'exécute AVANT les ALTER : ce doublon aurait tué le premier boot d'une base
# antérieure à la colonne. L'index continue d'être créé — à sa vraie place. Le reste
# du delta est du COMMENTAIRE (l'explication laissée sur place, pour qu'il ne
# revienne pas).
# 2026-09-01 (fleet comme produit, R4) : `runner_fleets` — la CONFIGURATION
# DÉCLARÉE d'un passage d'agents (procédure, cible, périmètre, bornes, état), plus
# `runner_jobs.fleet_id` qui rattache un travail à son passage. Une flotte vivait
# jusqu'ici dans un fichier YAML sur une machine : rien n'en était visible du
# dashboard ni atteignable par un agent, et le suivi d'un passage n'existait que
# parce qu'une session poussait des messages à une autre.
# ⚠️ ORDRE : `runner_fleets` est créée AVANT `runner_jobs`, qui la référence — les
# deux vivent dans le même fragment, et l'inverse casserait tout premier boot sur
# une base vierge (c'est la panne que ce fichier garde depuis #151).
# ⚠️ Empreinte recalculée depuis le module assemblé, APRÈS avoir vérifié que le
# tronc sans ce fragment rendait bien l'empreinte précédente : un écart venu
# d'ailleurs se serait sinon fait passer pour le mien.
# ⚠️ Empreinte RECALCULÉE sur le résultat du rebase des DEUX lots ci-dessus
# (le retrait de l'index unipile, puis le fragment des flottes), jamais
# reprise d'un côté du conflit : un hash pris d'un seul côté valide un DDL
# que personne ne sert.
# 2026-09-01 (#800) : `blocks.node_id` devient une clé étrangère
# (`blocks_node_fk … ON DELETE CASCADE` vers `nodes`). Deux naissances, et il faut
# les deux : INLINE ici pour une base VIERGE (le `CREATE TABLE` s'y applique), par
# `_init.py::_pose_cascade_blocs` pour une base qui existe déjà (le `CREATE TABLE`
# y est sauté). Retirer l'une des deux ne rougirait que dans
# `tests/test_blocs_cascade.py`, et prod et install fraîche divergeraient en
# silence. ⚠️ Sur la base PARTAGÉE prod/preprod, la contrainte se pose `NOT VALID` :
# la cascade joue quand même — c'est la vérification des lignes DÉJÀ là qui est
# différée, pas l'effet — et le boot ne peut donc pas échouer sur un orphelin
# hérité. Empreinte recalculée sur le module assemblé.
# 2026-09-01 (R4b — l'intention se sépare du fait) : `runner_fleets.status` passe
# à SEPT valeurs, plus `armed_at`/`stopping_at`. `armed` (on a DEMANDÉ que ça
# tourne) ≠ `running` (un ordonnanceur l'a PRISE) ; `stopping` (arrêt demandé) ≠
# `stopped` (arrêt accusé). Sans cette séparation, une flotte armée que personne
# n'a réclamée se lirait « en cours », et un arrêt demandé se lirait « arrêté » —
# or croire qu'on a coupé une dépense qui continue est le plus coûteux des deux.
# ⚠️ La contrainte CHECK est REMPLACÉE dans `_init` : un `CREATE TABLE IF NOT
# EXISTS` ne la met pas à jour sur une base existante, et le boot passerait vert
# pendant que la base refuse les deux nouveaux états.
# ⚠️ Empreinte recalculée APRÈS avoir vérifié que le tronc sans ce fragment rend
# bien c05cd85f… / 125769.
# 2026-09-02 (otomata-private#55, extension groupe) : fragment NEUF
# `connector_account_group_grants` (+ son index), table SÉPARÉE de
# `UNIPILE.connector_account_grants` — elle référence `org_groups`, créée par
# `schema.orgs.GROUPS`, assemblé APRÈS `schema.unipile.UNIPILE` : l'embarquer dans
# `UNIPILE` casserait le tout premier boot sur une base vierge. D'où
# `schema.unipile.UNIPILE_GROUP_GRANTS`, un fragment à part posé juste après
# `schema.orgs.GROUPS` dans `_schema.ASSEMBLAGE`. ADDITIF : rien n'est retiré ni
# contraint sur les tables existantes.
# 2026-09-02 (identité portée par un travail, chantier « agents autonomes ») :
# `runner_jobs.sub` — QUI a demandé ce travail, donc au nom de qui l'agent
# agira. ⚠️ C'est le préalable du worker MUTUALISÉ : tant que l'identité vient
# du jeton présenté par le worker, il faut un worker par organisation — ce n'est
# pas un choix d'architecture, c'est l'empêchement qui a laissé 41 travaux sans
# personne pour les prendre (#814). NULLABLE et ça le reste : les travaux d'avant
# n'ont pas de créateur connu, et leur en inventer un donnerait un nom qui se
# lirait comme un fait.
# ⚠️ Empreinte recalculée après avoir vérifié que le tronc SANS ce fragment rend
# bien 30e2c6f2… / 135678, mesuré au moment du rebase et jamais avant.
# 2026-09-03 — trois colonnes sur `users` : `suspended_at`, `suspended_by`,
# `suspended_reason`. Le cran manquant entre « vivant » et « supprimé » : un
# compte en pause ne peut plus rien faire, et rien de ce qui pend de lui n'est
# touché. Colonnes NULL partout à la pose — la migration n'a aucun effet tant
# qu'un administrateur n'a pas posé le geste.
# ⚠️ Empreinte recalculée APRÈS avoir vérifié l'arithmétique : le fragment
# `USERS` grandit de 740 caractères, et l'assemblé exactement de 740 aussi
# (136 654 → 137 394). Un écart aurait voulu dire qu'un autre fragment avait
# bougé sous la mesure.
# 2026-09-04 — fragment `PORTEE` : la table `portee_elargissements` (ADR 0068 §4),
# qui enregistre les moments où un AGENT fait sortir un contenu du périmètre de son
# propriétaire. Elle naît en période d'OBSERVATION (décision d'Alexis) : chaque ligne
# porte les destinataires qu'elle aurait prévenus et l'urgence qu'elle aurait eue,
# et `notifie_at` reste NULL — aucun message ne part. On veut voir le volume avant
# d'écrire à qui que ce soit.
# ⚠️ Empreinte recalculée APRÈS l'arithmétique : le fragment fait 2 094 caractères et
# l'assemblé grandit exactement de 2 094 (137 394 → 139 488). Un écart aurait voulu
# dire qu'un autre fragment avait bougé sous la mesure — sur une base PARTAGÉE
# prod/preprod, c'est la vérification qui compte, pas le hash recopié.
# 2026-09-04 — `org_id` devient NULLABLE sur `org_instructions` et sa table
# d'historique : le palier PERSONNEL des procédures (ADR 0068, phase 2 de #681,
# décision d'Alexis « procédure doit pouvoir être privée »). Cette colonne portait
# l'org PARENTE du propriétaire ET la cascade de suppression ; une personne n'a pas
# d'org parente, et y ranger son org de CONTEXTE ferait disparaître une procédure
# personnelle avec l'org. Le store refusait d'écrire cette ligne-là, à raison.
# ⚠️ Geste RELÂCHANT : aucune ligne existante ne devient invalide, et il se rejoue
# sans effet. Les deux tables bougent ENSEMBLE — laisser l'historique NOT NULL
# ferait échouer la première ÉCRITURE d'une procédure perso, pas sa création, donc
# bien après qu'on aurait cru le lot fini.
# ⚠️ Empreinte recalculée APRÈS l'arithmétique : deux commentaires de 211 caractères
# ajoutés, deux « NOT NULL » (9 caractères) retirés → +422 attendus, +422 mesurés
# (139 488 → 139 910). Un écart aurait voulu dire qu'un autre fragment avait bougé.
# 2026-09-04 (jetons de délégation) : `user_api_tokens.kind` — qui a demandé le
# jeton, l'UTILISATEUR ou l'EXÉCUTION. ⚠️ Une colonne et non un filtre sur le
# libellé : `label` est du texte libre, un utilisateur peut nommer son jeton
# « runner job 42 ». Filtrer sur du texte libre n'est pas une garantie.
# ⚠️ Empreinte recalculée après avoir vérifié que le tronc SANS ce fragment rend
# bien 8ebb0a70… / 139910, mesuré au moment du lot.
# 2026-09-04 (oto#25 lot b1) : `tool_calls.error_kind` — le RÉSULTAT de
# `error_taxonomy.classify()` sur un échec (`.code`, ex. `not_authorized`), écrit
# par `calllog._record`. NULLABLE, sans index (même raison que `request_id`/
# `call_uid`/`effective_sub`) ; existe aussi en `ALTER … ADD COLUMN IF NOT
# EXISTS` dans `_init.py` pour la base PARTAGÉE prod/preprod, où le `CREATE
# TABLE` est sauté.
# ⚠️ Empreinte recalculée après avoir vérifié que le tronc SANS ce fragment rend
# bien fdbf80b8… / 140769 (isolé dans un clone jetable, sans le WIP d'autres
# sessions) — la colonne ajoute exactement 339 caractères (140769 → 141108).
# ⚠️ Recalculée pour `credential_disparitions` (oto#59 — les clés retirées sous des
# agents programmés actifs). L'arithmétique a été VÉRIFIÉE, pas supposée : le fragment
# `schema/alertes.ALERTES` fait exactement 1 467 caractères, et 141 108 + 1 467 = 142 575,
# la longueur mesurée. Un delta qui ne tombe pas juste dirait qu'autre chose a bougé dans
# le DDL assemblé — c'est le seul contrôle qui distingue « j'ai ajouté ma table » de
# « j'ai recopié le nombre que le test m'a donné ».
# 2026-09-04 — fragment `runs.py` : la colonne `runner_fleets.rows_at_launch`, le
# dénominateur d'un passage (oto-backend#836). Écrite à l'armement, `NULL` = inconnu :
# un taux calculé sur un total qui a bougé depuis est faux sans que rien ne le signale.
# ⚠️ Empreinte recalculée APRÈS l'arithmétique, et la vérification a servi : le fragment
# `runs.py` grandit de 653 caractères (17 067 → 17 720) et l'assemblé exactement de 653
# aussi (140 769 → 141 422). Un premier comptage fait sur le DIFF donnait 680 — il
# comptait des lignes de contexte : c'est la différence des ASSEMBLÉS qui fait foi, pas
# un `grep` sur un patch.
# ⚠️ 2026-09-05 — RECALCULÉE à la fusion de `main` dans la branche du fork (#836).
# L'arithmétique a été vérifiée, pas supposée, et c'est elle qui autorise à écrire ce
# nombre : le tronc rendait 142 575, le fragment `runs.py` de cette PR ajoute
# exactement 653 caractères (17 067 → 17 720 mesuré par son auteur), et l'assemblé
# rend 143 228 = 142 575 + 653. Le compte tombe juste, donc rien d'autre n'a bougé
# dans le DDL — c'est la seule chose qui distingue « j'ai fusionné proprement » de
# « j'ai recopié le nombre que le test m'a donné ».
# ⚠️ 2026-09-05 (#882) — `tool_calls.token_id` / `token_kind` : PAR QUEL MOYEN un
# appel a été fait, jamais le jeton lui-même. Le journal n'attribuait que les
# sessions JWT, donc tout appel par jeton API était anonyme là où on cherche qui a
# fait quoi.
# ⚠️ Empreinte recalculée, arithmétique VÉRIFIÉE des deux côtés : le fragment
# `schema/usage.py` grandit de 749 caractères (10 594 → 11 343) et l'assemblé
# exactement de 749 aussi (143 228 → 143 977). Mesurer le fragment ET l'assemblé
# est ce qui distingue « j'ai ajouté mes colonnes » de « j'ai recopié le nombre
# que le test m'a donné » — un delta qui ne tombe pas juste dirait qu'autre chose
# a bougé dans le DDL.
# ⚠️ 2026-09-05 (oto#70 lot 2) — `origine_ecritures` : qui pose la couche `origine`,
# l'instrument du préavis. Empilé sur le lot ci-dessus, arrivé entre-temps : le fragment
# `schema/origine.ORIGINE` fait 2 014 caractères, et l'assemblé passe donc de 143 977 à
# 145 991. ⚠️ Cette valeur a été RECALCULÉE après la fusion, jamais choisie entre les
# deux versions en conflit — une empreinte dépend du contenu final, pas de qui écrit en
# dernier.
# ⚠️ 2026-09-05 (#340) — `tool_calls.result_size` : la taille du texte SERVI, en
# caractères. La durée disait ce qu'un appel coûtait au serveur, jamais ce qu'il
# coûtait à la fenêtre de l'agent — et ce qu'on ne mesure pas ne produit aucune
# plainte. Arithmétique vérifiée des deux côtés : le fragment `schema/usage.py`
# grandit de 661 caractères, et l'assemblé exactement de 661 aussi (146 604 →
# 147 265). Un delta qui ne tombe pas juste dirait qu'autre chose a bougé.
# ⚠️ 2026-09-06 (4b23cb27, scrub du dépôt public) : AUCUN ordre SQL n'a changé.
# Un seul COMMENTAIRE bouge, dans le fragment `MEMBERSHIP` de `schema/orgs.py` — il
# nommait un client en clair parmi les exemples de credentials d'org (règle du
# meta-repo : jamais de nom de client ni de personne dans un dépôt public), remplacé
# par un exemple générique. L'empreinte porte la chaîne entière, commentaires compris,
# donc elle bouge ; la preuve que l'EXÉCUTÉ est intact est MESURÉE, pas supposée : les
# deux versions du fragment COMMENTAIRES RETIRÉS sont identiques au caractère près
# (1 034 des deux côtés).
# ⚠️ Arithmétique vérifiée des deux côtés : `MEMBERSHIP` grandit de 12 caractères
# (3 521 → 3 533) et l'assemblé exactement de 12 aussi (147 265 → 147 277). Un delta
# qui ne tombe pas juste dirait qu'autre chose a bougé dans le DDL — c'est le seul
# contrôle qui distingue « je sais ce qui a bougé » de « j'ai recopié le nombre que le
# test m'a donné ».
# ⚠️ Mis à jour SÉPARÉMENT du commit qui a changé le DDL — ce que le docstring
# ci-dessus interdit, et pour cause : 4b23cb27 n'a pas rejoué cette garde, et le tronc
# est resté rouge de 08h37 jusqu'ici, bloquant toute mise en production.
# ⚠️ 2026-09-09 (planity, oto-backend#913) : COMMENTAIRES SEULS. Le fragment
# `schema/connectors.py` gagne une note sur ce que protège la règle « pas de lecture
# sur le chemin chaud » — le lecteur froid et hors boucle qu'est `planity_session`
# y satisfait sans instantané. Aucune instruction SQL n'a bougé : l'assemblé grandit
# de 634 caractères (148788 → 149422), exactement la taille des commentaires ajoutés, et
# `test_les_tables_creees_sont_celles_qu_on_attend` le vérifie côté exécuté.
# 2026-09-09 (métrage par unité) : `tool_calls.quantity` — le nombre d'items TRAITÉS
# par un appel, pour qu'un appel bulk ne compte plus comme un appel simple côté
# facturation. ADDITIVE et NULLABLE : la prod qui tourne l'ancien code ne l'écrit ni
# ne la lit, et l'ALTER d'`init_db` la pose sur la base PARTAGÉE sans réécrire une
# ligne (colonne sans défaut). Pas d'index — cf. le commentaire de l'ALTER.
# ⚠️ Valeurs RECALCULÉES sur le tronc du jour, jamais recopiées d'une version
# antérieure de cette branche : elle a été rebasée deux fois, et chaque rebase
# gèle un DDL différent. Recopier l'ancienne empreinte gèlerait un schéma mort.
# ⚠️ Arithmétique vérifiée APRÈS REBASE sur le tronc du 09/09 : le fragment `USAGE`
# grandit de 843 caractères (11 292 → 12 135 — le commentaire de `quantity`, la
# colonne, et la virgule ajoutée après `token_kind`), et l'assemblé d'exactement 843
# aussi. Le delta tombe juste des deux côtés : rien d'autre n'a bougé dans le DDL.
# Les empreintes rencontrées en conflit pendant ce rebase — dc84b4e2 (planity seul),
# 36834a62 puis ca2f4729 (branche seule) — sont MORTES : aucune ne décrit le DDL
# fusionné. C'est exactement le cas que l'avertissement ci-dessus vise.
# 2026-09-09 (facturation par clé) : `tool_calls.key_mode` — sous quelle clé
# l'appel est passé (`user|group|org|tenant|platform`), pour que le consommateur
# de facturation ne compte QUE ce qui passe par la clé du partenaire : un client sur sa
# propre clé paie déjà le fournisseur. ADDITIVE et NULLABLE, posée par l'ALTER
# d'`init_db` sur la base PARTAGÉE sans réécrire une ligne. Pas d'index.
# ⚠️ Arithmétique vérifiée à nouveau : le fragment `USAGE` grandit de 838 caractères
# (12 135 → 12 973) et l'assemblé d'exactement 838 aussi. Rien d'autre n'a bougé.
# ⚠️ Le TOTAL, mesuré sur l'arbre fusionné et non déduit : le tronc c1b1714c assemble
# 150 323 caractères, cette branche y ajoute 843 (`quantity`) + 838 (`key_mode`) =
# 152 004. Les deux seuls fragments qui diffèrent du tronc sont `usage.USAGE`
# (11 292 → 12 973) et l'assemblé, du même delta : contrôle fait fragment par
# fragment, pas sur le seul total.
# ⚠️ c1b1714c (runner/worker) a gelé 150 306 alors que son propre DDL en assemblait
# 150 323 : 17 caractères d'écart, et le tronc était ROUGE en arrivant ici — la garde
# a fait exactement son travail. Le nombre ci-dessous est recalculé sur l'arbre
# fusionné, il ne reconduit donc pas cet écart.
# 2026-09-09 (#917) : `billing_identities.pennylane_customer_id`, UNE colonne
# NULLABLE — le client Pennylane de l'org, POSÉ À LA MAIN par un admin plateforme,
# jamais rapproché ni créé par le code (le rapprochement par référence frappée par
# oto faisait naître un second client chez le comptable dès que le premier avait été
# créé à la main). ADDITIVE : le `CREATE TABLE` ne sert qu'aux installs vierges, la
# base PARTAGÉE la reçoit par l'`ALTER … ADD COLUMN IF NOT EXISTS` de `_init.py`.
# Réversible : le code du tag précédent fait `SELECT *` et un UPSERT par colonnes
# nommées, il ne la lit ni ne l'écrit — un retour en arrière la laisse en base,
# vide, sans effet.
# ⚠️ Arithmétique vérifiée sur un `git archive` du sommet 1fb172cb (isolé, jamais le
# tree partagé), APRÈS le dernier amendement de `schema/billing.py` : le fragment
# IDENTITIES grandit de 287 caractères (1 314 → 1 601 — le commentaire SQL et la
# colonne), l'assemblé d'exactement 287 aussi (152 004 → 152 291). Rien d'autre n'a
# bougé dans le DDL.
# 12/09/2026 — le modèle déclaré par l'agent (fragment RUNS) : la colonne
# `runner_triggers.model` et son commentaire dans le CREATE TABLE (la base partagée
# la reçoit par l'ALTER de `_init.py`), et la table `runner_platform_depots` — la
# présence d'un worker de plateforme par famille de modèles. Additif : le code du
# tag précédent ne lit ni n'écrit l'une ou l'autre. 152 291 → 153 174.
# 12/09/2026 — la RÉVISION de ligne : la colonne `datastore_rows.rev` et son commentaire
# (pourquoi un déclencheur, ordre alphabétique des déclencheurs BEFORE ROW) dans le
# CREATE TABLE. La base partagée la reçoit par `db/revision.py` (ALTER, fonction, et le
# déclencheur posé seulement s'il manque). Additif : le code du tag précédent ne lit pas
# `rev`, et ses écritures la font avancer. 153 174 → 153 796.
# 12/09/2026 — le déclencheur par WEBHOOK (fragment RUNS) : six colonnes sur
# `runner_triggers` (`kind`, `hook_secret_hash`, `payload_mode`, `payload_fields`,
# `max_per_hour`, `fraicheur_s`), le relâchement de `cron`/`next_due` en NULLABLE,
# et la table `runner_hook_deliveries` avec son index de fenêtre. La base partagée
# reçoit tout par les ALTER de `_init.py`. Additif ET réversible : le code du tag
# précédent écrit toujours `cron`/`next_due`, et son tick ne voit pas une ligne
# dont `next_due` est NULL.
# ⚠️ Empreinte RECALCULÉE sur le résultat FUSIONNÉ (rebase du 13/09 sur `revision`) :
# recopier l'une des deux aurait validé un DDL que personne n'a servi.
# 2026-09-13 : `runner_hook_deliveries.due_at` + l'index partiel des CRÉNEAUX. Le
# lissage se juge sur les créneaux réservés et non plus sur les réceptions : dès que
# rien ne périme par défaut, un retard dure des jours et la fenêtre des réceptions ne
# dit plus rien de la file. Table NEUVE, jamais déployée — CREATE seul, aucun ALTER.
# 2026-09-13 : `held` entre au domaine de `runner_jobs.status` — la file d'un agent
# DÉCLENCHÉ est gelée par la pause au lieu d'être périmée (un événement n'a pas de
# successeur). Ajout PERMISSIF : l'ancien code de prod n'écrit jamais cette valeur,
# et sa réservation filtre `pending`, donc il ne peut ni la produire ni la servir.
# 153 796 → 157 700 (fragment RUNS, branche webhook #942).
# 14/09/2026 — les lignes que la FILE rend à un run (fragment RUNS, oto#243) : la colonne
# `runs.lignes_reservees` et son commentaire dans le CREATE TABLE. La base partagée la
# reçoit par l'ALTER de `_init.py`, sans défaut (les runs d'avant restent NULL, non
# mesurés). Additif : le code du tag précédent ne la lit ni ne l'écrit — ses runs restent
# NULL. 153 796 → 153 900.
# 14/09/2026 — ce que l'agent lit des outils (fragment RUNS, oto#241) : la colonne
# `runner_fleets.descriptions_outils` et son commentaire dans le CREATE TABLE. La base
# partagée la reçoit par l'ALTER de `_init.py`, sans défaut (NULL = rien ne part avec le
# travail). Additif : le code du tag précédent ne la lit ni ne l'écrit. 153 900 → 154 101.
# 14/09/2026 — le consommateur de facturation nommé par sa CLASSE (fragment USAGE) : le
# commentaire de `tool_calls.quantity` dans le CREATE TABLE ne nomme plus le dépôt
# externe. Commentaire SQL seulement, aucun effet sur la base : un `--` n'est pas
# stocké. 154 101 → 154 108.
# ⚠️ 15/09/2026 — FUSION de la PR #942 (déclencheur webhook) sur ce tronc : les deux
# branches divergent du MÊME socle commun (153 796, la révision de ligne), sur des
# fragments distincts (`runner_triggers`/`runner_hook_deliveries` côté #942, `runs`/
# `runner_fleets`/le commentaire `tool_calls.quantity` côté tronc) — le SQL fusionne
# sans conflit, seules les deux constantes qui le gèlent se contredisaient.
# Arithmétique : LONGUEUR(fusion) == LONGUEUR(socle commun) + Δ(#942) + Δ(tronc)
#              = 153 796 + (157 700 − 153 796) + (154 108 − 153 796)
#              = 153 796 + 3 904 + 312 = 158 012 — valeur obtenue, recalculée sur la
# chaîne assemblée réellement fusionnée (pas recopiée d'un des deux côtés).
# 15/09/2026 (oto#150, désinscription du digest de signaux) — la table NEUVE
# `signal_digest_optouts` dans le fragment USAGE (`schema/usage.py`), même forme que
# `outreach_optouts` (PK `sub`, `source`, `opted_out_at`) mais table DISTINCTE :
# décision d'Alexis, le digest de signaux et les relances de plateforme sont deux
# canaux, deux refus — l'un ne désinscrit jamais de l'autre. ADDITIVE, sans autre
# lecteur que `pending_signal_notices` (exclusion ajoutée dans la MÊME PR, côté
# `db/usage.py`, hors DDL) ; la prod qui tourne l'ancien code ne la voit pas.
# 158 012 → 159 376 (+1 364, commentaire du fragment compris).
# 15/09/2026 (suite, correctif du trouble ci-dessus) — `signal_digest_optouts`
# DÉPLACÉE du fragment `usage` (`schema/usage.py`) vers le fragment `outreach`
# (`schema/outreach.py`), auprès de sa sœur `outreach_optouts` : elle référence
# `users(sub)`, et `tests/test_runner_trigger_sans_worker.py` mounte le fragment
# `usage` SEUL (sans `users`) — ce que `outreach` n'a jamais subi, puisque rien ne
# le mounte isolément. Trunk rouge (`UndefinedTable: relation "users" does not
# exist`) signalé par oto cd ; correctif + nouvelle garde
# (`test_usage_fragment_seul_sur_base_vierge.py`) dans ce commit. Même DDL, nouveau
# domicile : la longueur bouge quand même (commentaire réécrit, la référence
# croisée à `outreach.py` devenant interne au fichier qui la porte désormais).
# 159 376 → 159 935 (+559).
# 15/09/2026 (oto-backend#560, retrait du code court d'invitation) — même DDL
# (`code TEXT` reste posé dans `CREATE TABLE org_invitations`, la colonne n'est
# pas droppée), commentaire du fragment `orgs.py` réécrit : le code court est
# retiré du produit au profit du seul token long, `code`/son index sont désormais
# documentés comme LEGACY plutôt que comme le mécanisme de partage courant.
# 159 935 → 160 173 (+238).
# 18/09/2026 (ADR 0073, la fonction) — fragment NEUF `schema/functions.py`, posé EN FIN
# d'assemblage : deux tables neuves, `functions` (identité, version publiée) et
# `function_versions` (chaque version, immuable, et son statut), sans FK vers une autre
# table que la première. ADDITIF : la prod qui tourne l'ancien code ne les voit pas.
# 161 215 → 163 324 (+2 109, commentaires du fragment compris).
# 21/09/2026 (qui tient une campagne) — colonne NULLABLE `runner_fleets.taken_by` dans le
# fragment RUNS, pour une base NEUVE seulement : une base existante la reçoit de la
# révision Alembic `0003_runner_fleets_preneur`, jouée à la main, jamais du boot (ADR
# 0065). ADDITIVE : l'ancien code ne la lit ni ne l'écrit.
# 163 324 → 163 978 (+654, commentaire compris).
# 22/09/2026 (ADR 0074, #674) — `transcription_create` devient ASYNCHRONE : table
# NEUVE `transcription_jobs` (fragment `schema/transcription.py`, en QUEUE de
# `ASSEMBLAGE`, après `functions` — après `projects` dont elle référence `projects(id)`).
# Additive, aucun autre fragment touché.
# 163 978 → 165 623 (+1 645, commentaire du fragment compris).
# 22/09/2026 (le motif de chaque tentative) — `ALTER TABLE runner_jobs ADD COLUMN IF
# NOT EXISTS attempt_errors JSONB NOT NULL DEFAULT '[]'::jsonb` dans le fragment RUNS.
# ADDITIVE et idempotente : l'ancien code ne la lit ni ne l'écrit, et un défaut non
# volatile se pose en métadonnée (PG 11+) — pas de réécriture de table sur la base
# PARTAGÉE. Elle existe parce que `last_error` n'en garde qu'UNE : un travail mort en
# trois essais ne montrait qu'un tiers de son histoire.
# 165 623 → 166 657 (+1 034, commentaire compris).
# 23/09/2026 (otomata-tech/oto#239) — la table `guides` sort du DDL assemblé : son
# `CREATE TABLE IF NOT EXISTS` est RETIRÉ du fragment `schema/guides.py`, qui ne porte
# plus que `platform_instructions`. Ses lignes vivent dans `nodes` depuis le lot M1 et
# plus aucun code ne la touche. ⚠️ Ce retrait NE DROPPE RIEN : un `CREATE TABLE IF NOT
# EXISTS` laissé en place aurait fait RENAÎTRE la table au démarrage suivant le `DROP`
# d'exploitation, qui reste une décision à part (docs/live-migrations.md).
# 166 657 → 165 217 (−1 440, commentaire de remplacement compris).
# 23/09/2026 (ADR 0070 §7, #806) — fragment NEUF `schema/entitlements.py`, en QUEUE de
# `ASSEMBLAGE` (après `orgs`, qu'il référence) : la table `org_entitlements`, les droits
# DÉCLARÉS par org. Hors du fragment `billing` : le cœur ne dépend pas du commerce.
# ADDITIVE : rien ne la lit encore, et l'ancien code l'ignore. La révision Alembic
# `0004_org_entitlements` exécute le même fragment.
# 165 217 → 165 940 (+723, commentaires du fragment compris).
# 23/09/2026 (oto-backend#665, option B) — fragment NEUF `schema/usage.py::JOURNAL_ARCHIVES`,
# en QUEUE de `ASSEMBLAGE` : la table `journal_archives`, registre des mois du journal
# archivés au froid, écrit par `deploy/archive_tool_calls.py` avant de supprimer, lu par
# la page d'un run pour dire « contenu archivé le … ». ADDITIVE, sans FK ; la révision
# Alembic `0005_journal_archives` exécute le même fragment.
# 165 940 → 166 957 (+1 017, commentaires du fragment compris).
# 23/09/2026 (#523) — `user_api_tokens.revoked_at/revoked_by/revoked_reason` : révoquer
# un jeton ne supprime plus sa ligne, la trace (qui, quand, pourquoi) reste. NULLABLES,
# sans index. Base existante : révision Alembic `0007_jetons_revocation_tracee`, pas le
# démarrage. Le fragment `schema/tokens.TOKENS` passe de 2 216 à 2 604 caractères
# (+388), et 166 957 + 388 = 167 345, la longueur mesurée sur le tronc b0d9ac3c.
# 23/09/2026 (#806) — `schema.billing.CONTRACTS`, en QUEUE d'`ASSEMBLAGE` (après `orgs`,
# qu'il référence) : la table NEUVE `billing_contracts`, les paramètres d'un abonnement
# réglé hors plateforme. ADDITIVE : l'ancien code l'ignore. La révision Alembic
# `0008_billing_contracts` exécute le même fragment.
# 167 345 → 168 301 (+956, commentaires du fragment compris).
# 24/09/2026 (#521) — `connector_credentials.secret_enc` passe NOT NULL dans le fragment
# `schema/connectors.CREDENTIALS`, commentaire du coffre réécrit. Base existante :
# révision Alembic `0009_coffre_secret_obligatoire`, pas le démarrage. Empreinte
# recalculée sur le tronc qui porte #806 (qui rendait bien 168 301 / 1e71a312… sans ce
# lot) : 168 301 → 168 453 (+152).
# 24/09/2026 (oto-backend#644) — `tool_calls.result_shape` : la FORME du résultat servi
# (`empty` | `non_empty` | `refused(<code>)`), jamais le contenu — vocabulaire fermé PAR
# LA BASE (contrainte `tool_calls_result_shape_ferme`). NULLABLE, sans index. Base
# existante : révision Alembic `0010_tool_calls_result_shape`, pas le démarrage (table
# de plusieurs millions de lignes). Recalculée sur le tronc qui porte #806 et #521 :
# 168 453 → 169 421 (+968, commentaire compris).
# 23/09/2026 (otomata-tech/oto#273, M1) — fragment NEUF `schema.datastore.REVISIONS`,
# assemblé juste après `DATASTORE` (sa FK vise `user_datastores`) : la table
# `datastore_row_revisions`, en ajout seul, écrite par déclencheur. `CREATE TABLE IF NOT
# EXISTS` d'une table neuve : rien ne se réécrit sur la base PARTAGÉE ; son index et ses
# déclencheurs sont posés par `_init.py`, sous garde de catalogue.
# 169 421 → 170 587 (+1 166, commentaire compris), recalculée sur le tronc 88ec6bc8.
# 24/09/2026 (otomata-tech/oto#39) — `resource_grants.expires_at` : l'échéance d'un
# partage, NULLABLE (NULL = sans échéance), sans index. Base existante : révision Alembic
# `0012_partages_echeance`, pas le démarrage (table lue par chaque contrôle d'accès).
# Recalculée sur le tronc 00a32612 : 170 587 → 170 929 (+342, commentaire compris).
# 24/09/2026 (otomata-tech/oto#274) — `docs.updated_by` (l'auteur de la dernière
# modification) et `doc_revisions.face` (la porte de l'écriture), NULLABLES, sans index.
# Base existante : révision Alembic `0013_pages_versions_regroupees`, pas le démarrage.
# Recalculée sur 94dfcfcf : 170 929 → 171 478 (+549, commentaires compris).
# 24/09/2026 (oto-backend#1043, OTO-130) — table NEUVE `user_model_subscriptions`, dans
# le fragment `runs` (la réservation la lit), SANS clé étrangère vers `users` : le
# fragment n'en porte aucune vers l'extérieur. `CREATE TABLE IF NOT EXISTS` d'une table
# neuve : rien ne se réécrit sur la base PARTAGÉE. Recalculée sur 74f8ff8e + #1043 :
# 171 478 → 174 235 (+2 757, commentaire compris).
# 24/09/2026 (oto-backend#1066) — `org_entitlements` prend sa forme cible dans le fragment
# `schema/entitlements` : `sub` (NULL = ligne d'org, sinon une personne dans l'org),
# `value` NOT NULL, et l'unicité `org_entitlements_une_ligne` UNIQUE NULLS NOT DISTINCT
# (org_id, sub, right_key, source) DANS le `CREATE TABLE` à la place de la PK. Ne sert
# qu'à une base NEUVE ; la base PARTAGÉE reçoit la forme par les révisions Alembic
# `0014_droits_portee_personne` (avant la fusion) et `0015_droits_valeur_obligatoire`
# (après le tag de production), pas par le démarrage. Recalculée sur bd66b92c :
# 174 235 → 174 403 (+168, commentaires compris).
# 24/09/2026 (oto#273) — `datastore_row_revisions` prend `suppression BOOLEAN NOT NULL
# DEFAULT false` : la suppression d'une ligne devient une révision. Base neuve par le
# fragment `schema/datastore::REVISIONS` ; base existante par la révision Alembic
# `0016_journal_suppression` ou le démarrage (`journal_revisions.DDL_COLONNE_SUPPRESSION`,
# même forme). 174 403 → 174 695 (+292, commentaire compris).
# 24/09/2026 (oto#160) — `user_datastores` prend `context_org_id BIGINT REFERENCES
# orgs(id) ON DELETE SET NULL` : l'org où un tableau personnel a été créé. Base neuve
# par le fragment `schema/datastore::DATASTORE` ; base existante par la révision
# Alembic `0017_tableaux_contexte_org` ou le démarrage
# (`datastore_ns.DDL_COLONNE_CONTEXTE_ORG`, même forme). 174 695 → 175 224 (+529,
# commentaire compris).
# 25/09/2026 — le PLAFOND de consommation des abonnements. (1) Constante NEUVE
# `schema/runs.py::MODEL_SUBSCRIPTION_LIMITS`, en QUEUE d'`ASSEMBLAGE` (après `orgs`,
# qu'elle référence ; à part de `RUNS`, qui se joue seul et ne porte aucune FK vers
# l'extérieur) : la table `org_model_subscription_limits`, le plafond réglé par org.
# `CREATE TABLE IF NOT EXISTS` d'une table neuve : rien ne se réécrit sur la base
# PARTAGÉE, l'ancien code l'ignore. (2) `user_model_subscriptions.limite_pct SMALLINT
# CHECK (1..100)`, NULLABLE, le plafond perso, dans le `CREATE TABLE` pour une base
# neuve ; base existante par la révision Alembic `0019_plafond_abonnements` ou le
# démarrage (`user_subscriptions.DDL_COLONNE_LIMITE`, même forme, sous garde de
# catalogue). 175 224 → 176 890 (+1 666, commentaires compris).
# 25/09/2026 — le POOL d'org des abonnements. Constante NEUVE
# `schema/runs.py::MODEL_SUBSCRIPTION_POOL`, en QUEUE d'`ASSEMBLAGE` après
# `MODEL_SUBSCRIPTION_LIMITS` (FK vers `orgs`) : les tables `org_model_subscription_modes`
# (le mode personnel | pool d'une org) et `user_model_subscription_loans` (le prêt d'un
# abonnement au pool d'une org), et l'index de la seconde, posé avec elle. `CREATE TABLE
# IF NOT EXISTS` de tables neuves : rien ne se réécrit sur la base PARTAGÉE, l'ancien
# code les ignore ; base existante aussi par la révision Alembic `0020_pool_abonnements`.
# 176 890 → 179 367 (+2 477, commentaires compris).
# 25/09/2026 — les LIMITES d'un run déclarées sur l'agent : `runner_triggers.max_tokens`,
# `runner_triggers.max_run_seconds` et `runner_fleets.max_run_seconds`, INT NULLABLES
# sans défaut (NULL = celles de l'exécuteur). Base neuve par les fragments
# `schema/runs.py` ; base existante par la révision Alembic `0021_limites_du_run` ou le
# démarrage (`ADD COLUMN IF NOT EXISTS`). Rien ne se réécrit sur la base PARTAGÉE,
# l'ancien code les ignore. 179 367 → 179 669 (+302, commentaires compris).
# 25/09/2026 — le webhook authentifié PAR SIGNATURE (Standard Webhooks). Trois colonnes
# dans les `CREATE TABLE` pour une base neuve : `runner_triggers.hook_auth TEXT NOT NULL
# DEFAULT 'bearer'`, `runner_triggers.hook_signing_secret_enc TEXT`,
# `runner_hook_deliveries.external_id TEXT`, et les deux protections
# `runner_triggers.max_per_day INT` (plafond journalier, optionnel) et
# `runner_triggers.hook_slug TEXT` (adresse privée), NULL = le comportement d'avant.
# Base existante par la révision Alembic `0022_signature_webhook` ou le démarrage
# (`_init.py`, ADD COLUMN IF NOT EXISTS). ⚠️ Les index uniques de déduplication et
# d'adresse ne sont PAS dans ce DDL (leurs colonnes naissent d'un ALTER que le DDL
# précède, #450) : un commentaire le dit à leur place.
# 179 669 → 181 284 (+1 615, commentaires compris).
EMPREINTE = "6e0a67e03144393eb03cf2d5fd61e3b59b4508bc24fa436ead6cefb1ffff6078"
LONGUEUR = 181284


_CREATE_TABLE = re.compile(r"^CREATE TABLE IF NOT EXISTS (\w+)", re.M)


def test_la_chaine_assemblee_est_celle_qui_est_gelee():
    """La preuve du déplacement pur, et la garde du DDL ensuite."""
    empreinte = hashlib.sha256(_schema._SCHEMA.encode("utf-8")).hexdigest()
    assert (empreinte, len(_schema._SCHEMA)) == (EMPREINTE, LONGUEUR), (
        "le DDL assemblé a changé. Si c'est délibéré (vraie évolution du schéma), "
        "mets à jour EMPREINTE/LONGUEUR dans CE commit. Sinon, c'est qu'un "
        "déplacement de fragment a modifié le SQL — ce qui touche la base PARTAGÉE "
        "prod/preprod au premier boot.")


def test_chaque_table_a_exactement_un_domicile():
    """Un `CREATE TABLE` par domaine, et un seul — sinon le DDL en crée deux, ou
    l'un des deux dérive sans que personne ne le voie."""
    domiciles: dict[str, list[str]] = {}
    for nom_module in schema.__all__:
        module = getattr(schema, nom_module)
        for const in dir(module):
            if const.startswith("_") or not isinstance(getattr(module, const), str):
                continue
            for table in _CREATE_TABLE.findall(getattr(module, const)):
                domiciles.setdefault(table, []).append(f"{nom_module}.{const}")

    doublons = {t: d for t, d in domiciles.items() if len(d) > 1}
    assert not doublons, f"tables déclarées à plusieurs endroits : {doublons}"

    assemblees = _CREATE_TABLE.findall(_schema._SCHEMA)
    assert len(assemblees) == len(set(assemblees)), "table créée deux fois dans l'assemblage"
    assert set(assemblees) == set(domiciles), (
        "écart entre les tables des fragments et celles de l'assemblage : "
        f"orphelines={sorted(set(domiciles) - set(assemblees))}, "
        f"inconnues={sorted(set(assemblees) - set(domiciles))}")


def test_aucun_fragment_ne_reste_hors_de_l_assemblage():
    """Un fragment déclaré mais jamais assemblé ne lève rien : ses tables n'existent
    simplement pas. Le seul endroit où ça se voit est ici."""
    declares = set()
    for nom_module in schema.__all__:
        module = getattr(schema, nom_module)
        for const in dir(module):
            valeur = getattr(module, const)
            if not const.startswith("_") and isinstance(valeur, str) and "CREATE " in valeur:
                declares.add((nom_module, const))

    assembles = set()
    for fragment in _schema.ASSEMBLAGE:
        for nom_module, const in declares:
            if getattr(getattr(schema, nom_module), const) is fragment:
                assembles.add((nom_module, const))

    orphelins = sorted(declares - assembles)
    assert not orphelins, (
        f"fragments de DDL jamais assemblés : {orphelins}. Ils ne créent aucune "
        "table et personne ne s'en apercevrait — ajoute-les à `_schema.ASSEMBLAGE` "
        "à la bonne place (les FK imposent l'ordre) ou supprime-les.")


def test_l_ordre_impose_par_les_fk_est_tenu():
    """Les trois ordres déjà payés en incident, vérifiés sur la chaîne ASSEMBLÉE —
    c'est-à-dire à travers la frontière des modules, là où un déplacement les casse.

    Les tests de lot (`test_tenant_l1_migration`, `test_grants_l4_migration`) les
    gardent aussi ; ils sont répétés ici parce qu'ils sont désormais une propriété
    de l'ORDRE D'ASSEMBLAGE, pas d'un fichier."""
    ddl = _schema._SCHEMA
    for avant, apres, pourquoi in (
        ("tenants", "orgs", "orgs.tenant_id → tenants(id)"),
        ("orgs", "org_members", "org_members.org_id → orgs(id)"),
        ("grants", "grant_counters", "grant_counters → grants(id)"),
        ("docs", "doc_embeddings", "doc_embeddings.doc_id → docs(id)"),
        ("runner_fleets", "runner_jobs", "runner_jobs.fleet_id → runner_fleets(id)"),
        ("datastore_rows", "datastore_row_embeddings", "FK composite sur la PK"),
        ("org_groups", "connector_account_group_grants",
         "connector_account_group_grants.grantee_group_id → org_groups(id)"),
    ):
        i = ddl.index(f"CREATE TABLE IF NOT EXISTS {avant}")
        j = ddl.index(f"CREATE TABLE IF NOT EXISTS {apres}")
        assert i < j, (
            f"`{avant}` doit être créée avant `{apres}` ({pourquoi}) : sur une base "
            "VIERGE, PostgreSQL crée les tables dans l'ordre du DDL et la FK échoue.")
