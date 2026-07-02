# Propriété de ressource — primitive `ownership` (ADR 0030)

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.


Le datastore n'est **plus scopé par `sub`** : il est le **pilote** de la primitive
d'ownership générique. `ownership.py` est le **seam unique** : une ressource
`(resource_type, resource_id)` est possédée par `(owner_type∈{user,group,org},
owner_id)` (colonnes sur la ressource — pour le datastore : `user_datastores.owner_*`,
`resource_id = id::text`, **stable au renommage**) ; le partage cross-type vit dans
**`resource_grants`** (deny-by-default, remplace `datastore_shares`). Deux plans, jamais
confondus : **`can_access`** (CONTENU = owner-match ∪ grant ; *privacy by default* — pas
d'escalade admin sur du perso) et **`can_govern`** (GOUVERNANCE = owner ∪ escalade
`roles.py` : transférer/lister/partager **sans lire**). La lecture opérateur du contenu
perso reste le **view-as audité** (ADR 0023). `DatastorePg._resolve` passe par
`can_access` ; le share/transfert/delete par `can_govern` (un super_admin/org_admin
gouverne donc un datastore tiers). ⚠️ **Scoping des LISTES de contenu** : une liste de
ressources possédées (datastore `list_namespaces`, projets `op=list`) scope sur
**`ownership.active_owner(current_org)`** (= l'org active, le pendant `ownership` de
`current_org`/ADR 0023), **JAMAIS** sur `accessor_scope().owner_pairs()` (= union de
TOUTES les orgs de l'acteur, réservé au plan **gouvernance** `oto_resource list` +
découverte/modèles). Les confondre = fuite cross-org *fail-open* (le superset montre
plus que le contexte chargé) — vécu 2026-06-30 (projets/datastore d'une autre org
visibles dans le dashboard). Garde-fou : `tests/test_owner_scope_tripwire.py` fige les
call-sites `owner_pairs()`. **org-owned activé** : `data_create_namespace` /
`POST /api/datastore/namespaces` acceptent un `owner` (classeur d'équipe). Capacité
générique **`oto_resource`** (`capabilities/resources.py`, op `list/get/transfer/share/
unshare`, autz combinateur `RESOURCE_GOVERN`) = chemin de gouvernance MCP+REST + alimente
l'object-browser admin. Catalogue du registre : **`GET /api/admin/capabilities`**
(`capabilities_catalog.py`, `PLATFORM_ADMIN`, JSON Schema dérivé des Input pydantic) →
UI admin **dérivée**. ⚠️ **Migration en cours** : `user_datastores.sub` + colonnes Sheets
sont des reliques nullable, **DROP différé** (Phase H) après cutover prod vérifié.

> **Suppression du « perso » (2026-06-30, amende ADR 0015/0023/0030).** Plus d'état
> **org-less** (`org_id=0` / `current_org`=None) : **tout user est TOUJOURS dans une org**.
> Chaque user a une **org perso dédiée** (`orgs.personal_of=sub`, privée mono-membre) —
> `org_store.ensure_personal_org` (créée au 1er insert d'`upsert_user` + au boot par
> `backfill_personal_orgs`, **reclaim sûr** : ne marque une org existante comme perso que
> si c'est la SEULE org du user, créée par lui ; sinon org fraîche → multi-org intact, zéro
> fuite). Les ressources `owner_type='user'` ont **migré** vers l'org perso ; les **défauts
> de création** (datastore/projet) vont dans l'**org active** (`current_org`, toujours posé).
> Plus de retour-perso (`clear_active_org` retiré ; `oto_clear_org` REST → org perso, MCP →
> maison). Filets gardés : `ownership` accepte encore `owner_type='user'` **en lecture**
> (reliquat) ; `session_visibility` `prof_org = active_org or 0` (défensif). `org_id=0`
> purgé des profils de visibilité.
