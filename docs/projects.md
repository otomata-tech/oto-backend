---
title: Projet — couche d'organisation (ADR 0030/0032)
type: reference
---

# Projet — couche d'organisation (ADR 0030/0032)

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.


Conteneur de travail **possédé** (owned resource ADR 0030) : un but + ses entités. Tables
`projects` (owner_type/owner_id, `brief_md` = doc d'entrée, soft-delete `archived_at`),
`project_links` (pointeur typé `target_type∈{tableau,procedure,connecteur,base}` + `target_ref`
+ label, pas de FK cross-store), `docs` (pages markdown **en arbre** `parent_id`, héritent de
l'accès du projet — pas d'ownership propre), `project_activity` (journal best-effort).
Capacités co-déclarées : **`oto_project`** (`capabilities/projects.py`, op create/list/get/
update/archive/link/unlink/activity, `POST /api/me/projects`), **`oto_doc`** (`capabilities/
docs/`, op create/list/get/update/patch/delete/move/revisions/revert…, `POST /api/me/docs` —
un package depuis le 01/09, dispatcher et descripteur dans `docs/core.py`).
Partage/transfert via **`oto_resource`** (resource_type=`project` ajouté au dispatch `_OPS`).

> **Archiver se défait, et dit ce qu'il range (23/09/2026, oto#38).** `op=archive` pose
> `archived_at` : le projet sort de toutes les listes, rien n'est détruit. Jusque-là il
> n'avait **aucun inverse** — un choix assumé (oto-backend#929), rompu sur décision
> d'Alexis comme il l'avait été pour les procédures le 10/09 : le seul retour était un
> UPDATE SQL par un admin, et un projet archivé par erreur portait dans son brief
> les règles opérationnelles d'une mission. Désormais : `op=unarchive` (même garde,
> `can_govern` ; journalisé `project.unarchive` ; `was_archived_at` = ce qui a été annulé,
> `unarchived: false` n'est pas une erreur) ; `op=archive` rend `unreachable` (pages,
> procédures liées, liens, brief) et **refuse** (409 `confirm_required`, même compte dans
> `details.unreachable`) un projet qui porte un brief non vide ou une procédure liée, sauf
> `confirm=true` ; `op=list archived=true` rend les projets archivés de l'org consultée
> (mêmes propriétaires que la liste vivante, sans les partages reçus : qui reçoit ne
> gouverne pas).

> **Une liste rend son INDEX, jamais les corps (14/08).** ⚠️ **Ce doc a laissé croire le
> contraire jusqu'au 14/08** : `op=list` a longtemps rendu la fiche entière de chaque
> élément, et rien ici ne le signalait. Mesuré en prod : `oto_doc(op=list)` = **201 170
> caractères** pour 37 pages, `oto_project(op=list)` = **73 K** pour 26 projets — au-delà du
> plafond d'un tool result, donc refusé par le client, qui devait déverser en fichier puis
> reparser au `jq`. Un agent sans shell (client MCP nu, n8n) calait simplement.
> Désormais, les deux `op=list` (+ `list_templates`) rendent une **vue de tri** :
> `body_md`/`brief_md`/`mcp_instructions_md` sont remplacés par `<champ>_length`, et la
> réponse porte un bloc **`projection`** qui NOMME ce qui a été écarté. Le brut reste
> atteignable — `fields=["*"]` (le dashboard le passe : un navigateur rend les corps et n'a
> pas de fenêtre de contexte), `fields=[…]` choisit des colonnes, `fields=[]` est **refusé**
> plutôt qu'avalé. Une colonne-corps explicitement nommée est servie **entière**.
> Le seam est partagé : `output_projection.summarize()`.
> **Projeter ≠ tronquer** — on retire des colonnes (réversible, et le retour le dit) ; on ne
> coupe jamais un texte à N caractères. Un extrait arbitraire tombe pile avant ce qui
> départage deux éléments et l'agent croit avoir lu (mesuré le 11/08 sur un feed coupé à
> 600 c. : deux cas limites sur cinq tranchés à l'aveugle). D'où la TAILLE, pas l'extrait.
> Budget figé par `tests/test_list_view_budget.py` : le retour d'une liste croît avec le
> NOMBRE d'éléments, jamais avec la taille de leur contenu.

> **Revenir en arrière sur une page, et savoir ce qu'une suppression emporte (#657, 31/08).**
> Le snapshot était pris depuis toujours (`update_doc` archive l'état antérieur dans
> `doc_revisions`) et **rien ne le reposait** : le retour arrière se faisait à la main —
> lire `op=revisions`, republier le corps par `op=update` — ce qu'un front tiers ne peut pas
> offrir comme un geste. **`op=revert`** (`doc_id` + `revision_id` = le `id` d'une ligne de
> `op=revisions` ; les pages ne portent **pas** de numéro de version, et un rang calculé sur
> une liste plafonnée désignerait une autre version au prochain appel) restaure titre + corps
> **EN AVANT** — même régime que `org.instruction.revert` : l'état courant est snapshotté à
> son tour, donc rien n'est perdu et un revert se re-revert ; la réponse porte `reverted_from`.
> Il passe par `update_doc` (donc backlinks re-résolus, renommage propagé) et honore
> `expected_rev` → 409, ce que les instructions n'ont pas : restaurer sans garde écrase
> l'édition d'un pair.
> ⚠️ **`revert` ≠ annuler une suppression.** `op=delete` cascade sur tout le sous-arbre via la
> FK auto-référente et emporte `doc_revisions`/`doc_change_requests`/`doc_links` avec lui :
> après coup il n'y a plus de ligne à restaurer (prouvé, pas affirmé —
> `tests/test_doc_revert_et_suppression_657.py`). Il n'y a **ni corbeille ni `deleted_at`**.
> La cascade était en plus **muette** : l'accusé dit désormais combien de pages sont parties
> (`descendants`, + un `warning` quand il y en a), et **`dry_run: true`** rend le même compte
> **sans rien supprimer** — de quoi annoncer « ceci supprimera N pages » avant de le faire.
> Le compte n'est pas une estimation prise à part : c'est le `RETURNING` de la suppression
> récursive elle-même. Un `archive` non destructif (le vocabulaire déjà en place pour les
> procédures et les projets) reste **à faire** : c'est un changement de modèle, hors de ce lot.
> ⚠️ **La FK auto-référente n'interdit AUCUN cycle** (2026-09-01) : elle exige que la page
> visée existe, pas que l'arbre soit acyclique — `UPDATE docs SET parent_id = <un
> descendant>` passait, et les trois descentes récursives (compter, supprimer, déplacer)
> tournaient alors sans fin en remplissant `pgsql_tmp`. `move_doc` et `move_doc_to_project`
> **refusent** désormais de ranger une page sous sa propre descendance (`DocParentCycle`), et
> les trois descentes partagent une définition unique **bornée** par la clause SQL `CYCLE`.
> Le raisonnement complet, la mesure et le relevé de production (0 cycle) sont dans
> `docs/noeuds.md` — même défaut, même correction, une table plus loin.

> **Une rafale d'enregistrements ne fait qu'une version (oto#274, 24/09).** Le dashboard
> enregistre une page après 4 s d'inactivité ; chaque enregistrement empilait un instantané.
> `update_doc` ne le prend plus quand l'écriture (`update`/`patch`, face REST) suit un
> instantané du **même compte**, par la **face REST**, vieux de **moins de 5 minutes** —
> fenêtre fixe, comptée depuis l'instantané qui a ouvert la rafale : une session continue
> produit une version toutes les 5 minutes, jamais une seule pour l'heure. L'état d'avant la
> rafale reste la version restaurable. Toujours une version : l'agent (MCP), un autre compte,
> un écrivain inconnu, une restauration (`revert`), une fenêtre dépassée. Aucune révision
> n'est effacée ni réécrite (on saute un `INSERT`) : les `revision_id` servis restent
> valides, et `expected_rev`, haché du contenu, n'en dépend pas. La porte de chaque version
> est gardée (`doc_revisions.face`) ; la page sert `updated_by`, l'auteur de la dernière
> modification (`null` après un déplacement), sur `op=get` et `op=list`.

> **Un `unlink` qui n'a rien retiré le DIT (#699, 04/09).** `op=unlink` répondait `ok: true`
> sur un no-op — le lien visé figurait encore dans les `links` de la réponse à ce même appel.
> Deux causes, une seule correction. (1) Le **rowcount** de `remove_project_link` existait et
> personne ne le lisait : l'unlink rend désormais `removed` (nombre de bindings retirés) et
> **refuse** (`link_not_found`, 404) quand il n'a rien matché — un succès qui n'a rien fait est
> pire qu'un refus, et la face MCP ne rend que le *message*, donc celui-ci nomme les refs que
> le projet porte vraiment. (2) **Deux écritures désignent la même entité** : le stockage est
> canonique (id) depuis que `link` normalise nom/slug→id, mais les lignes d'avant portent le
> NOM du namespace ou le SLUG du guide — bien vivantes, résolues à la lecture (#117). L'unlink
> canonisait la réf demandée puis supprimait cet id : zéro ligne quand la ligne porte l'autre
> écriture, et le lien devenait **indélogeable** par le MCP (vécu en prod : un lien
> stocké sous le NOM du tableau, l'unlink visant son id). `_unlink_refs` confronte les deux côtés **canonisés** et
> vise les refs BRUTES stockées, dans les deux sens. `op=link` reste inchangé : il canonise, et
> refuse un nom introuvable (`unknown_tableau`) — on ne rouvre pas la porte aux liens morts.

> **Un lien par NOM se résout dans la portée du PROPRIÉTAIRE, une fois, et nulle part
> ailleurs (#365, 24/09/2026).** `db.list_project_links` est le seul lieu où un lien
> `tableau` posé par nom devient un tableau : dans la portée du propriétaire du projet
> (`_portee_du_projet` — le porteur et son org de contexte, l'org, ou l'équipe et son org
> PARENTE), rang du plus spécifique au plus large (perso, équipe, org, partages). Trois
> dérives fermées : (1) le libellé `datastore` se posait dès que le nom existait
> N'IMPORTE OÙ sur la plateforme — un lien « vivait » grâce au tableau d'une autre org ;
> il ne se pose plus qu'avec `datastore_id`, et l'audit voit ce lien mort ; (2) deux
> tableaux au MÊME rang (deux partages homonymes) donnaient le plus petit id en silence :
> le lien porte `datastore_ambigu: true`, n'a ni libellé ni identifiant, et l'audit le
> range dans `dead_links` en le disant ; (3) **`slot:` rendait le NOM du tableau bindé**,
> que le store résolvait ensuite chez l'appelant — son « vivier » perso passait devant
> celui du projet. Il rend désormais `datastore_id`, et refuse un binding ambigu en le
> nommant. L'endpoint MCP partagé et la page partagée lisent le même `datastore_id`
> au lieu de refaire chacun leur résolution (le premier cherchait dans l'org et
> exposait, sur un projet d'équipe, le tableau de l'org au lieu de celui de l'équipe).

> **Un lien par IDENTIFIANT se juge dans la même portée qu'un lien par nom (12/10/2026).**
> Nom et slug se résolvaient dans la portée du projet ; un identifiant numérique était
> stocké tel quel, sous la seule garde d'écriture du PROJET, puis servi tel quel par tous
> ses lecteurs : libellé et titre dans les liens, lignes et corps sur la page publiée,
> lignes recopiées par `op=copy` (mode `seeded`), corps recopié dans l'org de la copie,
> schéma posé par un slot. Le numéro d'un tableau ou d'une procédure de n'importe quelle
> org suffisait. Une seule règle, `db.refs_dans_la_portee` (le prédicat de
> `list_project_links`) : la cible appartient au propriétaire du projet, se range dans
> son org (l'org, une de ses équipes, un tableau perso créé en elle, la procédure perso
> d'un de ses membres — une procédure perso ne se range dans aucune org), lui est partagée
> (partage vivant à l'un de ses principaux), ou vient de la bibliothèque plateforme.
> (1) **À la pose**, `op=link` refuse en 404 (`unknown_tableau` / `unknown_procedure`,
> même message que pour un numéro inexistant) une cible hors de cette portée, ET une
> cible que le lieur ne LIT pas — même quand elle appartient au propriétaire du projet :
> écrire un projet n'est pas en être membre (un bénéficiaire extérieur d'un partage en
> écriture liait les tableaux de l'org, puis les recopiait chez lui). Exception au même
> message : une cible que le lieur LIT mais hors de la portée (le tableau d'une autre org
> dont il est aussi membre) est refusée en 403 `target_out_of_scope`, qui dit quoi faire —
> la copier dans l'org du projet, ou la partager à cette org ; l'existence ne lui apprend rien.
> Un lien posé avant
> la garde vers une cible qui existe ailleurs porte `hors_portee: true`, et l'audit le
> dit « hors de la portée du projet » plutôt que « n'existe plus ». **La copie** (`op=copy`)
> écarte, en le disant, tout tableau ou procédure que le COPIEUR ne lit pas
> (`duplicate_project(lisible=…)`) : lire le projet n'est pas lire ses tableaux. (2) **À la lecture**, `list_project_links` ne pose `datastore_id` (tableau)
> et `procedure_id` (procédure, clé neuve, même sens) que sur une cible dans la
> portée ; la page publiée, la copie, l'inventaire, l'audit et le schéma cible d'un slot
> lisent CES clés, jamais le `target_ref` brut — un lien posé avant la garde reste en
> base, mort pour tous. `slots.provision_tableau_schema` re-vérifie lui-même la portée
> (`project_id` obligatoire). L'unlink n'est pas gardé : un lien hors portée doit
> pouvoir se retirer.

> **Push out-of-bande de gros contenu par un agent (issue #105).** Écrire un GROS contenu
> via `oto_doc(body_md=…)`/multipart le fait transiter INLINE par le contexte du LLM (coût
> tokens + troncature/paraphrase sur du verbatim). **`oto_upload_url(target)`** (capacité
> `me.upload_url`, MCP-only) rend une **URL signée** à usage unique + TTL court (15 min) sur
> laquelle l'agent PUT le contenu **hors-bande** (`curl --data-binary @fichier`) ; le backend
> matérialise dans la cible (`PUT /api/upload/<token>`) et renvoie un accusé léger (id +
> longueur), **jamais le body**. Le jeton (`upload_tokens.py`) est **stateless** — HMAC
> signant `{typ:upload, jti, sub, org, target, exp}` (secret `OTO_MCP_OAUTH_STATE_SECRET`) —
> et **scelle la cible** (jamais acceptée d'un param client à la réception : verrou IDOR) ;
> l'autz d'écriture est **réappliquée** à la réception (`ownership.can_access(project, write)`)
> et l'usage unique est garanti par la table `upload_tokens_used` (`db.consume_upload_token`,
> consommée AVANT matérialisation → anti-rejeu). Cibles : `target='doc'` (op create/update
> d'une page Documents), `target='project_file'` (fichier brut « Autre document » — comble
> le gap upload multipart dashboard-only : un agent peut déposer un PDF/CSV, plafond
> `OTO_MCP_UPLOAD_MAX_BYTES`, déf. 25 Mo) et `target='datastore'` (lot de lignes NDJSON/CSV
> → batch-upsert sur clé `schema.key`/param `key`, ns_id résolu+scellé au mint sous l'org
> active, autz réappliquée org-agnostiquement via `ownership.can_access(datastore_namespace,
> write)`) et, depuis le 2026-08-29, `target='image'` (**une image publiée à une URL
> publique permanente** — `media_store.upload_image`, préfixe `images/<sub>/`, png/jpeg/
> gif/webp par magic bytes, 2 Mo, clé = hash du contenu ; aucune ressource cible, le
> porteur est la garde ; l'accusé rend `url` — la tête d'un `email_send`, cf.
> `docs/email.md`). **Double voie pour le MÊME lien** : un agent avec shell PUT le corps brut ; sans
> shell (claude.ai), il transmet l'URL à l'humain qui l'ouvre → **page d'upload HTML
> autoportée** (`GET /api/upload/<token>`, POST multipart `file`, jeton consommé au POST pas
> au GET). L'upload multipart humain dashboard (`POST /api/me/projects/{id}/files`) reste.
> ⚠️ **Un seul plafond pour un fichier de projet** : `upload_tokens.max_bytes()`
> (`OTO_MCP_UPLOAD_MAX_BYTES`) borne le dépôt par lien signé, le dépôt multipart du tableau
> de bord ET la lecture du worker d'extraction (`file_extract_worker`). Sans `max_bytes`,
> `media_store.upload_object`/`fetch_object` retombent sur le plafond d'une IMAGE (2 Mo) —
> jusqu'au 21/09/2026, un fichier de plus de 2 Mo était refusé au dépôt du tableau de bord
> et finissait `object_too_large` à l'extraction (ADR 0074, étape 0).

> **Désigner un fichier du projet au lieu de le transporter (ADR 0074, étape 1).**
> `file_source.resolve` (le résolveur « fichier côté oto » des outils qui prennent une
> `source` : `lighton_*`, `pennylane_upload_file`, `gmail_compose` `attachments`,
> `lemlist_lead op=upload_audio`) connaît le kind `{"kind":"project_file","project_id":…,
> "file_id":…}` (ids rendus par `oto_project_files op=list`) : les octets sont lus côté
> serveur dans le stockage objet (`media_store.fetch_object`), jamais par le contexte de
> l'agent. Garde = celle d'une lecture par-id d'un projet — `ownership.visible_in_org`
> (org active, ADR 0023) ET `can_access(read)` — et ⚠️ **un refus répond mot pour mot
> comme un fichier inconnu** (ou rangé sur un autre projet) : distinguer les deux dirait
> l'existence du fichier. Plafond = celui de `file_source` (`max_bytes`, 25 Mo par défaut),
> vérifié AVANT de matérialiser (`head_object`).

> **Déposer par référence, et un fichier PRODUIT par l'agent (oto-backend#1149).** Le
> dépôt côté serveur est `oto_import(source=…, target="project_file", project_id=…)` :
> n'importe quelle `source` de `file_source` est résolue côté serveur et posée telle quelle
> dans les fichiers du projet (écriture, `me.import`), avec le contrat d'erreur du seam
> (`source_unreadable` : taille, source injoignable, type inconnu). Un fichier produit par
> un run (un `.xlsx`) passe EN LIGNE par `{"kind":"content","filename":…,"base64":…}` (ou
> `text` pour de l'UTF-8), borné à `file_source.CONTENT_MAX_BYTES` (256 Ko, vérifié sur la
> longueur encodée avant décodage) : au-delà, le PUT signé d'`oto_upload_url`.
> **Le retour** : une pièce jointe reçue sur une messagerie hébergée (`linkedin_unipile_chat`,
> `whatsapp_chat`, `telegram_chat`, `instagram_chat`, `op="read"`, une LECTURE) porte une
> `source` `{"kind":"unipile","channel":…,"url":…}` — l'URL que sert Unipile, qui exige la
> clé. `file_source` la résout avec le client du compte OPÉRÉ du canal et refuse une URL
> hors de `https://<dsn>/v2/<ce compte>/…` (une clé partagée lirait sinon la pièce d'un
> autre compte) ; redirection refusée, flux borné par `max_bytes`, budget de 90 s.

> **Livraison d'un projet COMPLET vers l'org d'un client (otomata-private#52).**
> `oto_resource` : share/unshare acceptent un principal **org** (`org_id`, sans exigence
> d'appartenance — on donne un accès) ; **`cascade=true`** sur share/transfer d'un projet
> répercute le geste sur les `project_links` avec rapport par entité — **tableau** = même
> geste (grant/transfert du namespace), **procédure** = grant `read` au partage (modèle
> licence : oto garde le master) / **copie chez la cible + re-pointage du lien** au
> transfert (`org_store.copy_instruction_to_org`, l'originale intacte), **connecteur** =
> `recipient_credential` (le client branche SA clé ; la surcharge identité/instructions du
> lien voyage avec le projet) ; docs/fichiers suivent d'office (héritage d'accès). Famille
> **`procedure`** de la primitive ownership (owner lu sur `org_instructions.owner_type/owner_id`,
> resource_id = id surrogate ; valeur stockée : `ownership.TYPE_RESSOURCE_PROCEDURE`, cf.
> `docs/ownership.md`) → lecture cross-org **par id** `oto_procedure(op='get', guide_id=…)` /
> `GET /api/me/guides/{guide_id}`, gatée `ownership.can_access`. Un projet
> livré remonte chez le client dans `oto_project(op=list)` (flag `shared`+`permission`) ET
> dans le bloc C du handshake (#50) — ouvrable en un message. Reste à cadrer : push des màj
> post-livraison (re-share = re-grant idempotent, mais pas de notification). UI : `oto-dashboard`
`/projects` + page dédiée `/projects/:id` (`ProjectDetailView`, ADR 0030). Reliquats du modèle
(MCP-App rendu, édition temps réel/lock, pré-set vendable=copie) **non faits**.

> **Une liste s'en tient à l'org consultée (07/10/2026, remplace le paragraphe suivant
> pour les projets perso).** Un projet perso se liste, pour moi seul, dans l'org où je l'ai
> créé (`context_org_id`) et **nulle part ailleurs, org perso comprise** ; l'org perso ne
> garde que mes projets perso sans org de création (legacy) et ce qui m'est partagé en
> personne. Détail : `docs/ownership.md` « Une liste s'en tient à l'org consultée ».

> **Dans une org, on ne voit QUE l'org (28/09/2026, ADR 0030 §9 — remplace le paragraphe
> suivant pour les projets perso et les partages faits à moi).** `op=list` (portée `org`)
> rend les projets de l'org consultée, de ses pôles, et ce qui est partagé à elle ou à mes
> équipes en elle — **aucun projet perso**, quel que soit son `context_org_id`, et aucun
> partage fait à moi. Dans mon **org perso**, s'y ajoutent **tous** mes projets perso et ce
> qui m'est partagé en personne (`shared: true`). `list_templates`, `archived=true`, la
> recherche et le rail suivent (`ownership.project_list_owners` / `principaux_de_liste`).
> `op=create`/`op=copy` sans `owner_type` : le projet reste « moi, org » (§8) depuis
> n'importe quelle org, et se LISTE, pour moi seul, dans l'org où je l'ai créé (et dans
> mon org perso). `scope="me"` est servi dans toute org (29/09/2026 : une org perso est une
> org comme une autre). Détail : `docs/ownership.md`.

> **Ce qu'une liste d'org montre des partages (21/09/2026).** `oto_project op=list` (portée
> `org`, le défaut) rend les projets de l'org consultée, ceux de ses pôles, mes projets
> perso qui y sont rangés (ADR 0030 amendé) et ce qui est partagé **à l'org ou à mes
> équipes en elle** — jamais un partage fait **à moi** : il n'appartient à aucune org, et
> l'afficher dans l'une le faisait lire comme un projet de celle-ci. La règle #5.1 le
> montrait encore dans l'org « maison » ; elle est retirée. Ces projets se listent par
> **`scope="me"`**, quelle que soit l'org consultée. Rien n'est devenu inaccessible : la
> garde par-id (`visible_in_org`) et la recherche (`accessible_project_ids`) gardent le
> principal personnel — lisible ⇒ cherchable tient, seule la liste d'org ne le nomme plus.
> `op=list_templates` suit la même borne : les modèles de l'org consultée et de ses pôles,
> mes modèles perso rangés dans cette org, et la bibliothèque plateforme — plus l'union de
> toutes mes orgs.

> **Partage NAVIGABLE d'un projet — `<slug>.share.oto.cx` (ADR 0032).** Un projet publié en
> `secret` est servi sur son sous-domaine `<slug>.share.oto.cx` comme un petit site rendu
> **server-side** (lisible humain ET agent WebFetch), en **lecture seule** : la racine = index
> (brief + procédures/tableaux/docs + carte « brancher »), `/procedures/<id>` (prose), `/data/<ns>`
> (table), `/docs/<id>` (page). Le MCP reste au path `/mcp`. Rendu par
> `share_ui.py` (routeur `build_page`, lectures DB en threadpool, contenu échappé, markdown sûr),
> **gating fail-closed** par appartenance au projet (seules les entités liées sont navigables).
>
> ⚠️ **Ce que la face web montre est décidé par le MÊME seam que la face MCP** — `project_exposure`
> (`docs_exposed` / `datastore_exposed` / `datastore_writable`), fonctions pures sur la ligne
> `projects`. Les **procédures** sont navigables du seul fait d'être **liées** (un acte explicite
> du propriétaire) ; les **tableaux** demandent en plus `mcp_expose_datastore`, et les **pages**
> l'opt-in explicite `mcp_expose_docs`. Sans l'opt-in, le refus est **identique des deux côtés** :
> la face MCP retire le tool de sa liste et refuse l'appel, la face web omet l'entrée de l'index
> et rend **404** sur l'URL directe — sans même lire la ligne.
>
> ⚠️ **Erreur corrigée le 2026-08-29 (oto-backend#557, sévérité haute).** Jusqu'à cette date, la
> face web ne consultait **aucun** des deux opt-ins : elle listait les pages du projet et rendait
> leur `body_md` entier dès que le projet était publié, y compris en `anonymous` — c'est-à-dire
> **annoncé** par l'annuaire public `GET /api/public/mcp-projects`. Le propriétaire, lui, avait lu
> la garde côté MCP et croyait avoir publié des outils, pas ses notes. Le paragraphe ci-dessus
> **affirmait déjà** que `/data/<ns>` était « gaté par `mcp_expose_datastore` » alors que le code
> ne gatait que sur `mcp_access == 'secret'` : une doc qui décrit une garde absente fait passer la
> relecture suivante sans s'arrêter. Mesuré en production au moment du correctif : **3 projets
> publiés, 9 pages** servies sans opt-in (0 pour les tableaux — l'écart y était théorique). Le
> correctif fait converger les deux faces sur `project_exposure` ; un cliquet AST
> (`tests/test_project_exposure_seam.py`) interdit désormais de relire ces colonnes ailleurs.
> Même dispatch que `.mcp.oto.cx` (`subdomain_project.HostDispatch`, suffixes `.mcp`/`.share`,
> URL de branchement path-aware). `oto_project(op=get)` expose `mcp_url` (per-mode : `secret` →
> `share.oto.cx/mcp`, sinon `mcp.oto.cx/mcp`) + `share_url` (base navigable, `secret` uniquement).
> Infra : wildcard `*.share.oto.cx` (grey) + Caddy on-demand TLS gaté par `/api/mcp/tls-check`.
> **Remplace** l'ancien partage public **chiffré** zero-knowledge (`/p/p`, `project_public_shares`,
> `lib/crypto.ts`, `PublicProjectView.vue`) — **retiré** (le navigable live le supplante).
>
> **Canal de démonstration / acquisition (otomata-private).** L'index `share_ui` est soigné
> pour « claquer » : un **hero** « brancher dans Claude/Mistral » (URL MCP + copie), les tools
> exposés **groupés par CONNECTEUR** (pastille logo + description au survol + lien vers la fiche
> marketplace `dashboard.oto.ninja/connectors?tab=marketplace&connector=<name>`, dérivé de
> `providers.connector_for_namespace`), et une vue **tableau** pleine largeur avec recherche
> globale + tri 3 états + filtres par colonne (JS inline, opère sur le DOM rendu — pagination
> serveur inchangée). Le CTA **« Ajouter à mon Oto »** du hero (deep-link
> `/import?slug=<slug>` du dashboard) a été **retiré le 13/09/2026** : l'écran `/import`
> l'avait été avant lui (oto#192) et le bouton retombait sur l'accueil.
>
> ⚠️ **`op=copy` rend une copie possédée par QUI la fait (ADR 0068, 04/09/2026).** Elle
> était possédée par l'**org active** (ADR 0032 §7 B5a), y compris en dupliquant un
> projet PERSONNEL : son propre travail se publiait à ses collègues, sans qu'aucun
> paramètre ne l'ait demandé. Le cas que 0032 visait — copier un MODÈLE pour l'équipe —
> n'a pas disparu, il se DIT : `owner_type='org'|'group'` + `owner_id`, gardé par
> l'appartenance comme `op=create`. Une copie perso garde son `context_org_id` : elle
> reste rangée dans l'org où on travaille sans y être partagée — sans quoi on aurait
> remplacé un excès de partage par une disparition.
>
> **« Ajouter à mon Oto » — import d'un projet publié par slug : RETIRÉ le 13/09/2026
> (oto#192).** La capacité REST-only `me.import_project` (`POST /api/me/projects/import`)
> n'avait plus d'appelant — l'écran `/import` du dashboard puis le CTA de la page de
> partage qui y menait avaient été retirés — et le journal `tool_calls` n'en montrait
> aucun `POST` depuis son ouverture (29/07/2026). Sont partis avec elle
> `find_copied_project` et les paramètres `copied_from` de `create_project` et
> `track_source` de `duplicate_project`. ⚠️ La colonne `projects.copied_from` et son index
> `idx_projects_copied_from` restent en base (vides en prod le 13/09/2026, aucune DDL dans
> ce retrait) : plus rien ne les écrit ni ne les lit. Copier un projet passe par `op=copy`.

> **Endpoint MCP par projet — `<slug>.mcp.oto.cx` (ADR 0032, amende #44).** Un projet
> se **publie** comme serveur MCP dédié sur son propre sous-domaine (le « preset » de
> l'ADR 0032 §7). Colonnes `projects.mcp_slug`/`mcp_access`(`off|anonymous|secret|org`)/`mcp_tools[]` ;
> capacité `oto_project` op **`publish_mcp`/`unpublish_mcp`** (autz `can_govern` ; publier
> exige EN PLUS d'être membre de l'org propriétaire, 403 `publish_requires_membership` —
> `docs/ownership.md` §Publier exige d'appartenir au propriétaire, #1176).
> ⚠️ **Publier au-delà de l'org ne sort pas d'une conversation (04/09/2026).** Décision
> d'Alexis après l'inventaire des chemins d'élargissement : « org = explicite, public =
> interdit à l'agent ». `mcp_access` **n'a plus de défaut** (il valait `anonymous` — le
> web entier, *et* listé dans l'annuaire public : publier sans rien préciser était le
> geste le plus ouvert) ; et `anonymous`/`secret` sont refusés
> (`publication_reservee_a_l_humain`, 403) quand l'appel vient de la face **MCP**, que
> `ResolvedCtx.channel` nomme depuis les deux seuils (`_mcp_adapter`, `_rest_adapter`).
> L'asymétrie est délibérée : un contenu d'org reste dans une population nommée et se
> reprend, un contenu servi sans login est indexable et ne se reprend pas.
> ⚠️ **Ce n'est pas un contrôle d'accès mais un cran d'intention** : un porteur de jeton
> peut toujours appeler la face REST. Il vise le geste non voulu, pas l'adversaire — le
> nommer « sécurité » ferait croire le vrai contrôle posé. Même régime pour `oto_doc
> op=set_public` (l'OUVERTURE seule : `public=false` reste permis, sinon un agent
> constaterait une fuite sans pouvoir la refermer) et `oto_procedure op=publish`, dont
> le `visibility` perd aussi son défaut `public`. Le canal se lit sur le MONTAGE
> (`tests/test_canal_d_appel.py`) : non posé, toutes ces gardes seraient vertes et
> inertes. **Sonde de
> publication NON bloquante** (`_mcp_unresolvable_tools`) : pour un preset sans login, les tools
> **non credential-less** (`secret_kind≠none`) ou dont le connecteur n'a pas de clé résoluble pour
> l'org propriétaire sont **publiés quand même** mais **échouent proprement à l'appel** (McpError,
> pas de fallback) — la liste remonte en warning `mcp_unresolvable_tools` (choix produit : permettre,
> pas refuser). **Trois modes** :
> - **`anonymous`** (sans login + **listé** dans l'annuaire, contourne 100 % du blocage Logto #44) :
>   allowlist **figée = `mcp_tools`** (fail-closed, aucun autre tool visible), credential résolu via
>   l'**org propriétaire** du projet (`access.current_org(None)`→org du projet, `_resolve_credential_anon` :
>   org_secret > grant > clé plateforme, **sans quota**), rate-limité (token-bucket in-memory par IP+projet).
> - **`secret`** : **identique à `anonymous` côté serving** (même face anonyme du registre MCP — le même serveur monté sans auth, oto-backend#534 —, même
>   résolution de credential, même allowlist), mais **non listé** dans l'annuaire (`list_published_mcp_projects`
>   ne rend que `= 'anonymous'`) et **slug non devinable généré serveur** (`_gen_secret_slug` : préfixe
>   optionnel issu du slug saisi + suffixe `secrets.token_hex(6)`) → une **URL secrète**. Re-publier réutilise
>   le slug existant (ne casse pas l'URL déjà distribuée). Le dispatch traite `anonymous`/`secret` sur le même
>   chemin (`access_mode in ("anonymous","secret")`).
> - **`org`** : JWT Logto, **épingle l'org** ; le sous-domaine est enregistré comme **resource Logto**
>   (`auth.facade.ensure_api_resource`) + verifier **multi-audience** + PRM **host-aware**.
>
> **Host-routing** (`subdomain_project.HostDispatch`, monté `root_app` dans `server.main`) : une **2ᵉ app
> FastMCP sans auth** (`anon_mcp = mcp`, **réutilise l'instance no-auth module-level** — ne PAS en
> re-build une 3ᵉ, doublait `register_all`/`init_db` — et, jusqu'au 2026-09-09, le montage des
> connecteurs fédérés → boot timeout) sert les sous-domaines anonymes ;
> tout le reste → app authentifiée **inchangée**. **Même URL, 2 publics** : navigateur (`GET`+`Accept:
> text/html`) → **landing HTML** rendue **live depuis la ligne projet** (`anon_landing.render`, name/brief_md/
> mcp_tools) ; Claude/Mistral (`POST`) → MCP (rewrite path `/`→`/mcp`, `_root_to_mcp` — Claude tape la racine).
> Fichiers : `subdomain_project.py` (routing + rate-limit + `/api/mcp/tls-check` + `/api/public/mcp-projects`),
> `anon_visibility.py` (allowlist fail-closed), `auth/anon.py` (shim OAuth **auto-approve**, `.well-known/*`
> + `/register` + `/authorize`→302 sans login + `/token`→`anon-…`), `anon_landing.py` (HTML charté).
> **Infra** : **wildcard** `*.mcp.oto.cx` (CF-proxied) + Caddy **on-demand TLS** gaté par `/api/mcp/tls-check`
> (200 uniquement pour un slug **publié** → borne l'émission de certs). `publish_mcp` est la **seule** action
> par projet — **zéro DNS** à chaque publication. **Surface web** : annuaire public **oto.ninja/apps**
> (`web/AppsView.vue`) via `GET /api/public/mcp-projects` (CORS `*`, liste les projets `anonymous` publiés).

> **Corriger l'instruction servie sans republier (oto-backend#597, 24/09/2026).**
> `oto_project(op="update", project_id=…, mcp_instructions_md="…")` réécrit la prose que
> l'endpoint sert à son destinataire, et **elle seule** : slug, accès, outils et opt-ins
> ne bougent pas (`db.set_project_mcp_instructions`, une colonne). Autz **`can_govern`**
> — comme `publish_mcp` : c'est ce que l'agent d'un tiers lit au branchement —, journal
> `project.update_mcp_instructions`, `""` efface. Avant, la seule voie était de rejouer
> `publish_mcp` en redéclarant accès et outils ; une session prudente s'en abstenait pour
> une phrase, et une instruction périmée restait servie (29/08 : une clause retirée du
> brief, encore servie au destinataire). Les AUTRES champs de publication (`mcp_slug`,
> `mcp_access`, `mcp_tools`, `mcp_expose_*`) sont **refusés** sur `op=update`
> (`publication_field_on_update`, rien d'écrit) plutôt qu'ignorés : ils se redéclarent
> par `publish_mcp`.

## Dossier d'affichage d'un projet — `folder` (2026-10-16)

**Le besoin.** Une org de conseil tient un projet par client ; à quinze projets, la
sidebar se lit nom par nom et chaque consultant y cherche les siens. Ce qui manquait
n'est pas une hiérarchie, c'est un **regroupement visuel** : « les clients de X », « les
clients de Y ».

**La forme.** Colonne `projects.folder` (TEXT, NULL = hors dossier), posée par
`oto_project op=update folder="…"` — rognée, `""` la retire, plus de 80 caractères
refusé (`folder_too_long`, rien d'écrit : un libellé, pas une phrase). Servie par
`op=get`/`list` (`ProjectOut.folder`). Les projets au même libellé forment un dossier :
**il n'existe pas d'objet dossier** — ni table, ni id, ni dossier vide ; renommer un
dossier, c'est reposer le libellé sur chacun de ses projets.

**Ce que ce n'est pas.** Aucune règle d'accès (un dossier ne partage ni ne masque rien —
la visibilité reste celle du propriétaire et des grants), aucune imbrication, aucun
effet sur le contexte MCP. Même garde que le renommage : `write` sur le projet. Partagé
par tous ceux qui voient le projet — c'est un rangement d'équipe, pas une préférence
personnelle.

## Périmètre d'URL d'un projet — `excluded_url_prefixes` (#605, 2026-08-29)

**Le besoin.** Un contrat client peut exclure la CONSULTATION de certaines pages — le cas
fondateur : les profils personnels d'un réseau social professionnel. La consigne le disait
en toutes lettres ; sur cent fiches d'une campagne, **deux** ont consulté un profil quand
même (et trois autres ont refusé explicitement un profil qu'elles avaient sous les yeux).
Hiérarchie : *le chemin n'existe pas > la machine refuse > un contrôle détecte > la consigne
interdit*. On était au quatrième cran sur un engagement contractuel ; ceci est le premier.

**L'option.** `oto_project(op="update", project_id=…, excluded_url_prefixes=[…])` — une liste
de motifs d'URL, posée **sans republication**, affichée par `op=get`/`op=list`, et **portée
par l'endpoint publié** du projet (le projet de l'endpoint est celui de l'appel). `[]` retire.
Nommée en anglais comme les autres options de projet (`is_template`, `mcp_expose_docs`) ; pas
`excluded_domains`, parce que le nom dirait le contraire de la règle ci-dessous.

**Grammaire d'un motif — hôte + préfixe de chemin, jamais une regex.**
- `linkedin.com/in/` (≡ `https://www.LinkedIn.com/in`) : l'hôte ou l'un de ses
  sous-domaines (`fr.linkedin.com`), `www.` ignoré, casse ignorée ; le chemin se compare
  **segment par segment** (`/in/` couvre `/in/jane`, pas `/inbox`). Stocké sous forme
  canonique `linkedin.com/in/`.
- **Un domaine entier n'est jamais implicite.** `linkedin.com/company/` reste rendu sous
  `linkedin.com/in/`. Pour exclure tout un site, la forme est **explicite** : `exemple.com/*`.
  Un hôte nu (`exemple.com`, `exemple.com/`) est **refusé à la pose** (`invalid_url_prefix`,
  message qui donne les deux formes à écrire) — l'oubli d'un chemin ne doit pas devenir
  l'exclusion d'un site.
- Refusés aussi : `*` ailleurs que seul après l'hôte, requête/fragment, schéma non http(s),
  hôte sans point ; 50 motifs max, 200 caractères chacun ; un lot dont UN motif est faux
  n'est **pas** stocké (une pose partielle serait une exclusion partielle silencieuse).

**Un seul seam : `oto_mcp/url_perimeter.py`** (pur, sauf `perimeter_of_call()` = une lecture
`db.get_project_by_id`). Résolution du projet de l'appel : le jeton `_project=`
(`access.current_project`), sinon le projet de l'endpoint publié
(`subdomain_project.current_anon_project_id`). **Sans projet ou sans option, aucun changement**
— prouvé par différentiel deux clones sur 27 appels des outils couverts (diff vide), et par
identité d'objet dans `tests/test_url_perimeter.py`. Deux effets :
- **(a) sortie d'un outil de RECHERCHE** — `filter_results` : les résultats dont l'URL
  correspond ne sont pas rendus (à toute profondeur : liste de résultats, sitelinks, PAA,
  `data.web`, `metadata.sourceURL`, listes d'URLs nues), et la réponse porte
  `excluded_by_perimeter = {count, project_id, project, prefixes: {motif → n}}` — **jamais en
  silence, même à zéro** : l'agent sait qu'un périmètre est en force. Le filtre agit **avant**
  la projection `fields=` (sinon un profil sans son `link` passerait). Il ne filtre pas la
  prose (une `answer` synthétique qui cite une URL en texte).
- **(b) entrée d'un outil d'EXTRACTION** — `refuse_if_excluded` / `refuse_if_any_excluded` :
  l'URL est refusée **avant tout appel amont**, en nommant le motif et le projet (« relève du
  motif `linkedin.com/in/`, exclu par le périmètre du projet « X » — ne la contourne par
  aucun autre outil ») ; pour un lot, tout le lot est refusé en nommant chaque URL. Là où
  l'outil observe une URL finale après redirection (`web_read` cran ①/③, `browser_fetch`),
  elle repasse le seam : un `acme.fr/equipe/x` qui atterrit sur un profil est un profil.

**Couverture — relevée sur l'inventaire AST des tools à paramètre URL ou de recherche web.**
Critère : *couvert* = l'outil rend des pages du web ouvert ou lit une URL fournie par
l'appelant. *Non couvert* = l'URL y est un identifiant passé à un fournisseur, une
configuration, ou une page d'une plateforme propre — le cran pour ces connecteurs-là est
l'activation par org / l'allowlist `mcp_tools` de l'endpoint (ADR 0010/0011), pas un motif
d'URL. Le cliquet `test_every_covered_module_calls_the_seam` fige la liste des couverts.

| outil | point d'application | statut |
|---|---|---|
| `serper_search` (web, news, images, videos, places, shopping, scholar, patents) | sortie, avant projection | couvert (`autocomplete` : suggestions, pas d'URL) |
| `serper_lens` | entrée (URL d'image) + sortie | couvert |
| `serper_scrape` | entrée + URL finale de notre lecture directe | couvert |
| `serpapi_search` (tout moteur) | sortie | couvert |
| `searchapi_search` (tout moteur) | sortie | couvert |
| `tavily_search` | sortie | couvert (`answer` = prose, non filtrée) |
| `tavily_extract` | entrée (lot) | couvert |
| `tavily_map`, `tavily_crawl` | entrée (racine) + sortie (URLs/pages) | couvert |
| `firecrawl_search`, `firecrawl_crawl_status` | sortie | couvert |
| `firecrawl_scrape`, `firecrawl_map`, `firecrawl_crawl` | entrée (+ sortie pour `map`) | couvert |
| `firecrawl_extract` | entrée (lot) | couvert |
| `cloro_google` (serp, news), `cloro_ask` (sources) | sortie | couvert (la réponse IA = prose) |
| `web_read` | entrée + URL finale observée | couvert |
| `browser_fetch`, `browser_eval` | entrée (+ URL finale pour `fetch`) | couvert |
| `lighton_parse`, `pennylane_upload_file`, `gmail_compose` (`{kind:"url"}`) | entrée, via `file_source.resolve` | couvert |
| `serper_maps_sample/census`, `serper_reviews`, `serpapi_jobs/trends/finance/flights/hotels` | — | non couvert : lieux, offres, séries — pas des pages ; `website` d'un lieu n'est pas un résultat |
| `firecrawl_extract_status` | — | non couvert : données au schéma de l'appelant, pas des pages (l'entrée l'est) |
| `reddit_search`, `reddit_post`, `linkedin_unipile_*`, `linkedin_aiark_*` | — | non couvert : objets d'une plateforme propre ; exclure = ne pas activer le connecteur |
| `kaspr_enrich_linkedin`, `fullenrich_enrich_linkedin`, `apollo_match_person`, `cognism_enrich_*`, `lemlist_enrich`, `forager_organization` | — | non couvert : l'URL est un IDENTIFIANT passé à un fournisseur de données, pas une page lue |
| `apify_run*` | — | non couvert : `run_input` opaque d'un acteur tiers ; cran = activation |
| `http_get/post` | — | non couvert : chemin relatif à une `base_url` configurée par l'org (client d'API) |
| `browser_connect_start` | — | non couvert : page de login ouverte à l'humain, pas une lecture |
| `email_send(cta_url, image_url)`, `brevo_import_contacts(file_url)`, `fireflies_transcript(url)`, webhooks (`folk`/`linear`/`grain`/`granola`/`webflow`), `ahrefs_*`, `promptwatch_*`, `snitcher_*` | — | non couvert : URL écrite, importée par le fournisseur, ou de configuration — rien n'est lu par nous. (`apollo_reveal_phone`/`apollo_bulk_match` ne prennent plus d'URL depuis le 28/09/2026 : la réception est la nôtre, `docs/donnees-par-reference.md`.) |

**`serper_scrape` a DEUX amonts depuis le 2026-09-03 (#681).** Le scraper hébergé ne
rend aucun champ HTML, et les adresses obfusquées (base64 d'un `joomla-hidden-mail`,
`mailto:` en entités, `cloudflare-email-protection`) n'existent QUE là : l'outil relit
donc la page lui-même — sur `format="html"`, en repli après un refus du fournisseur, et
en sonde quand la page servie ne montre aucune adresse. Cette seconde lecture, elle,
observe une URL finale : le périmètre s'y applique aussi. Sur les deux chemins qui
SERVENT du contenu (`html`, repli), un atterrissage hors périmètre refuse ; sur la sonde,
qui ne sert que des adresses en complément d'un scrape déjà réussi, il l'ÉCARTE et le dit
dans `sonde_obfuscation` — un scrape réussi ne doit pas tomber à cause de son complément.

**Le refus du périmètre parle en premier (#632, 2026-08-29).** Sur une campagne,
`serper_scrape` d'un profil personnel a été refusé par une règle interne du client amont
(« se lit avec les outils `unipile_*` ») : un refus qui ouvre une porte — vers des outils que
l'appelant n'a pas forcément (famille de #613). Quand le périmètre exclut l'URL, c'est LUI qui
répond : il dit la vraie raison et n'indique aucun outil. Règle mécanique : dans chaque handler
couvert (`serper_scrape`, `serper_lens`, `web_read`, `browser_fetch`/`browser_eval`,
`firecrawl_scrape`/`map`/`crawl`/`extract`, `tavily_extract`/`map`/`crawl`,
`file_source._from_url`), **le seam est le premier geste** — rien avant `url_perimeter.refuse_*`
sinon la docstring et la résolution du périmètre. Cliquet AST
`tests/test_url_perimeter_order_632.py`, doublé d'un test par outil qui déclenche AUSSI une
règle interne (validation de `format`, d'hôte, taille du lot, `limit`, clé absente, substrat non
configuré, règle LinkedIn du client) et exige le message du périmètre. Cinq handlers passaient
une règle interne devant : `serper_scrape` (`format`), `browser_*` (l'hôte — une URL sans
schéma sortait « URL invalide »), `tavily_extract` (« 20 maximum », une porte : scinder le lot),
`tavily_map`/`crawl` (`limit`), `_from_url` (forme http(s) puis anti-SSRF — dont l'ordre
dépendait du RÉSEAU : hors ligne, la résolution DNS parlait à sa place). La règle LinkedIn
elle-même vit dans oto-core (`SerperClient._NEVER_SCRAPABLE`, épinglé par tag) : reformulée là
sans nommer d'outil, servie ici au prochain bump du pin.

**Ce qui n'est pas fait.** L'option n'est pas rappelée dans le préambule servi au destinataire
d'un endpoint publié (`compose_published_project`) : le refus et le compte sont le texte le
plus proche du geste, et c'est celui-là que l'agent lit. Le dashboard n'affiche pas encore
l'option (elle est dans le payload de `GET /api/me/projects/{id}`).

## Le projet — ce que la carte en disait (migré le 2026-08-27)

Conteneur de travail **possédé** : brief + liens typés (`project_links` : tableau/
procédure/connecteur/**doc** — `doc` = une page Documents attachée) + docs en arbre. Capacités `oto_project`/`oto_doc` ;
partage/transfert via `oto_resource`.
⚠️ **« C'est où ? » a une réponse SERVIE** (#599, 28/08) : un projet et une page portent
`url`, l'adresse pour les LIRE chez le lecteur — dérivée du seam `links.link_for`, comme
celle d'un tableau (`data_url`), donc elle suit le front du tenant et vaut `null` quand
son produit n'a pas cette vue. Elle survit à toutes les projections (l'accusé d'écriture
est justement le moment où l'on demande où aller). **Ne jamais la reconstruire côté
appelant** : un patron d'URL appris par cœur fabrique des liens plausibles et faux le
jour où la route bouge — c'est ce que faisait le contournement qui a produit le signal.
À ne pas confondre avec `mcp_url`/`share_url`, qui décrivent la PUBLICATION du projet
vers l'extérieur ; `url`, c'est où on le lit chez soi. S'y greffent : **livraison client cascade**
(#52), **endpoint MCP + partage navigable par projet** — un projet publié est servi sur
son sous-domaine dédié, modes **anonymous** (`<slug>.mcp.oto.cx`, sans login + listé) /
**secret** (`<slug>.share.oto.cx`, URL non devinable = **UI navigable** lecture seule des
procédures/tableaux/docs, rendu server-side `share_ui`, + MCP au path `/mcp`) / **org**
(authentifié) ; sonde credential-less **non bloquante** → `mcp_unresolvable_tools` en
warning ; annuaire oto.ninja/apps. (Le partage public **chiffré** `/p/p` a été retiré,
supplanté par ce partage navigable live.) La page navigable (`share_ui`) est un **canal
d'acquisition** : hero « brancher », connecteurs en pastilles (logo + tooltip + lien fiche),
tableau riche (recherche/tri/filtres). Son CTA « Ajouter à mon Oto » a été retiré le
13/09/2026 (oto#192), et avec lui la capacité `me.import_project`
(`POST /api/me/projects/import`) qu'il servait.
**Détail : `docs/projects.md`**.
