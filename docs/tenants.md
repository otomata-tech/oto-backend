---
title: Tenants (ADR 0052)
type: reference
description: >-
  Convention du 08/10/2026 : on n'étend plus le tenant (rien de neuf par tenant, gel
  mécanique). L'étage d'identité entre plateforme et org : émetteur dédié, hosts, préfixe d'outils, écra
  n de suivi, et les pièges d'une bascule de compte (clés personnelles abandonnées, marque d
  'espace personnel, colonnes à sub triagées).
---

# Tenant — l'étage d'identité au-dessus des orgs (ADR 0052)

> Extrait de `CLAUDE.md` le 2026-08-27 — le contenu n'a pas changé, seule sa place a bougé.
> La carte garde le résumé + le pointeur ; le détail (schémas, incidents datés et leurs
> leçons) vit ici.

## Convention (08/10/2026) : on n'étend plus le tenant

Décision d'Alexis, le 08/10/2026. L'épic oto-backend#1167 (« un tenant réduit au strict
nécessaire ») est fermée sans être réalisée : rien n'est retiré d'office, mais rien de
nouveau ne s'ajoute.

- **Le tenant existant est conservé tel quel.** Ce qui est servi aujourd'hui continue de
  l'être.
- **On n'ajoute AUCUNE** nouvelle capacité, route, colonne, table, rang de cascade, marque
  ou réglage **par tenant**.
- **Un besoin qui semble appeler « par tenant » se traite par instance** (une instance
  dédiée, un réglage d'instance) **ou par org**.
- **Retirer du tenant reste permis à tout moment.** L'inventaire de ce qui pourrait partir,
  et le noyau légitime (annuaire tiers + ses orgs), est dans oto-backend#1167.

Garde mécanique : `tests/test_tenant_ne_s_etend_plus.py` relève les surfaces « tenant »
(capacités du registre et leurs opérations, valeurs d'énumération, routes REST, champs
de `tenancy.TenantIssuer`, tables, colonnes et domaines du schéma, variables
d'environnement) et échoue sur toute entrée absente de `tests/tenant_inventaire_gele.txt`.
Le gel ne fait que rétrécir : un retrait passe, et retire sa ligne dans le même commit.

## Le tenant primaire est une DÉCLARATION de l'instance (28/09/2026, oto-backend#969)

La ligne 1 de `tenants` est le tenant de l'instance elle-même : ses subs restent nus,
`orgs.tenant_id DEFAULT 1` y rattache toute org. Son identité était la constante
`tenancy.PRIMARY_SLUG = "oto"`, semée au démarrage sous notre nom : toute base neuve
naissait avec notre tenant (ADR 0070 §7.2). Elle se **déclare** désormais, sans défaut :

| variable | rôle |
|---|---|
| `OTO_TENANT_PRIMAIRE_SLUG` | le slug, lu par `tenancy.primary_slug()` à chaque appel — tout le code qui disait `PRIMARY_SLUG` |
| `OTO_BRAND_NAME` | le nom de la marque (#968), qui est aussi celui du tenant, posé **à la naissance** de la base seulement |

- absente ou invalide : le démarrage refuse (`identite_instance.verifier`, dans
  `server.main`, avant d'avoir rien écrit) ;
- base neuve : la ligne 1 naît avec le slug et le nom déclarés (`db/_tenant_primaire.py`) ;
- base existante dont la ligne 1 porte **un autre slug** : refus du démarrage
  (`TenantPrimaireDiscordant`) — une base ne change pas de tenant primaire par un réglage ;
- l'instance historique déclare le slug `oto`, ce que porte sa base (qui garde son nom).

## Désactiver un tenant — un geste, jamais l'édition de la ligne (oto-backend#1165)

> **Procédure.** `oto_admin_tenant op=disable slug=<slug> reason="<pourquoi, et à quelle
> condition réactiver>"` (MCP) ou `POST /api/admin/tenants/{slug}/disablement
> {"op": "disable", "reason": …}` (REST), super admin. Réactiver : `op=enable`. **Ne
> jamais vider `issuer`/`jwks_uri` à la main pour couper un tenant**, et ne pas compter
> sur `op=reload` pour le faire : c'est précisément ce qui a laissé une session ouverte.

**Ce qui s'est passé.** Pour désactiver un tenant on a vidé `issuer` et `jwks_uri` de sa
ligne puis rechargé le registre : un compte qualifié par ce tenant a continué de lire
l'admin REST depuis une session de tableau de bord déjà ouverte, jusqu'à ce qu'on
rétrograde son rôle à la main. Retirer l'émetteur n'éteint que les JWT **signés par
l'annuaire du tenant**, et dans le **seul processus rechargé** (le registre est par
processus). Trois portes n'y passent jamais :

- le **jeton `oto_`** (API ou délégation d'un travail d'agent) porte son sub en base ;
- l'**ancien identifiant** d'une personne basculée vers le tenant : son JWT est signé par
  NOTRE annuaire sous son sub nu, que le drain d'alias (`sub_aliases`, armé en
  permanence, `tenant_migration.py`) canonicalise en `<slug>:…`. Une session de tableau
  de bord ouverte sur notre annuaire sert donc le compte du tenant, émetteur du tenant ou
  pas — c'est la lecture la plus probable de l'incident, les comptes d'un premier
  partenaire ayant été basculés ainsi ;
- le **processus non rechargé** (préprod ↔ prod, bleu/vert).

**L'état est une colonne** (`tenants.disabled_at`, `disabled_by`, `disabled_reason`,
révision `0040_tenants_desactivation`), lue à **chaque vérification d'identité** par la
garde centrale `garde_identite.refus`, sur le sub **canonique** (après le drain), sans
cache, dans la base — donc sur toutes les instances, dès le geste, sans rechargement :

| porte | où | refus |
|---|---|---|
| REST, jeton `oto_` (API, délégation) | `api.base._authenticate`, branche haute | `403 tenant_disabled` |
| REST, JWT (tout émetteur, ancien identifiant compris) | `api.base._authenticate`, branche basse | `403 tenant_disabled` |
| MCP, toute requête (JWT OAuth ou `oto_`) | `AccountSuspendedMiddleware.on_request` | `McpError`, `data.code=tenant_disabled` |
| lien d'upload signé (sub scellé, sans jeton) | `api.uploads._do_signed_upload` | `403 tenant_disabled` |
| naissance d'un compte du tenant | `db.upsert_user` (gate `inserted`) | `TenantDesactive` → `tenant_disabled` |

L'inscription, l'autorisation et le rafraîchissement OAuth se font chez l'annuaire du
tenant (CIMD) : la plateforme ne les voit pas, et refuse à chaque requête le jeton qui en
sort. L'entrée d'un tenant désactivé **reste** dans le registre : son émetteur sert à
reconnaître ses jetons pour les refuser nommément, et ses comptes restent classés sous lui.

La même garde porte la **pause de compte** (`docs/comptes-en-pause.md`) : le tenant est
jugé d'abord, la personne ensuite. Les deux prédicats ne s'appellent nulle part
ailleurs ; `tests/test_garde_identite_chemins.py` énumère les chemins et fige les
appelants. Coût : **zéro lecture pour un sub nu** (tenant primaire), une lecture d'une
table de quelques lignes pour un compte qualifié.

**Ce que rend `op=disable`.** L'état, `changed` (re-désactiver ne réécrit ni l'auteur ni
la date ni le motif), et ce que le geste a coupé :

- `revoked` — les jetons que nous **stockons**, révoqués pour **tous** les comptes
  qualifiés sous le tenant, par type (`user` = API, `delegation` = travail d'agent ;
  `user_api_tokens.revoked_*`, la ligne reste, #523). Le geste rejoué ramasse ce qui a
  été émis depuis ;
- `accounts_cut`, `aliases_cut` — les comptes et les anciens identifiants désormais
  refusés ;
- `refused_not_stored` — ce que nous **ne stockons pas** et ne pouvons donc ni compter
  ni révoquer à la source : session de tableau de bord (JWT), jetons d'accès et de
  rafraîchissement OAuth du MCP (émis par l'annuaire du tenant), liens d'upload signés.
  Ils restent signés jusqu'à expiration et sont refusés à chaque présentation.

Le geste est journalisé (`warning` + le journal des appels, auteur et arguments).

**`op=enable`** rouvre les **nouvelles** connexions. La révocation ne se réactive pas :
les jetons révoqués restent morts, les personnes se reconnectent et réémettent leurs
jetons d'API.

**Garde-fous.** Le tenant primaire (ligne 1) ne se désactive pas (`409 primary_tenant`) ;
un slug inconnu rend `404 unknown_tenant` ; `reason` est exigé et refusé entier au-delà
de 500 caractères ; on ne désactive pas le tenant de son propre compte
(`409 self_tenant`). L'émetteur reste déclaré : il sert à reconnaître — et refuser
nommément — les jetons du tenant, au lieu d'un `401 invalid_token` anonyme. Un export de
périmètre ne transporte pas la désactivation (le tenant devient le primaire de sa cible).

**Le geste suspend aussi les ORGS du tenant** (décision du 06/10/2026). Couper les seuls
comptes laissait ouverts un compte d'un autre tenant (ou du primaire) membre d'une de ses
orgs, l'endpoint anonyme d'un projet publié d'une de ses orgs, et les automatisations
(cron, webhooks) qui continuaient d'enfiler. `op=disable` suspend donc, dans la même
transaction, toutes les orgs de `orgs.tenant_id` par la suspension d'org existante
(`orgs-suspendues.md`) — pas de second système : plus personne n'y agit, ses projets
publiés ne sont plus servis, rien ne s'enfile ni ne se réserve. Chaque suspension posée
ainsi porte son origine, `orgs.suspended_tenant_id` (révision
`0042_orgs_suspension_par_tenant`), avec pour auteur l'opérateur du geste et pour motif
`tenant <slug> désactivé : <motif>`. La réponse rend `orgs_suspended` (et leurs ids) et
`orgs_already_suspended` — celles qu'une autre raison suspendait déjà, non touchées.

- **Rejoué sur un tenant déjà désactivé**, le geste rattrape les orgs pas encore
  suspendues (une org née depuis, ou un tenant désactivé avant ce lot) : c'est le
  rattrapage d'un tenant coupé par la version précédente, qui ne suspendait pas ses orgs.
- **`op=enable`** lève les suspensions qu'il a posées, elles seules
  (`orgs_resumed`) : une org suspendue pour une autre raison (essai fini) le reste.
- **Le tenant passe avant l'org** : tant qu'il est désactivé, le geste d'org
  (`admin.org_suspension`, `service.org.suspension`) refuse de lever la suspension d'une
  de ses orgs (`409 tenant_disabled`) ; et suspendre par ce geste une org que le tenant
  avait suspendue REPREND la suspension à son compte — la réactivation du tenant ne la
  lèvera pas.
- Un export de périmètre ne transporte pas ces suspensions-là, comme la désactivation.

Révoquer les sessions **dans l'annuaire du tenant** (Management API, quand nous
l'administrons) est une autre décision non prise (`tenancy.ForeignTenantDirectory`).

## Un tenant tiers est SERVI depuis le 13/08 (oto-private#83)

> **Un tenant tiers est SERVI, depuis le 13/08 (oto-private#83).** Le premier partenaire a
> son émetteur, son host, son client OAuth et ses 10 comptes qualifiés. Trois règles en
> sont sorties, toutes contre-intuitives, toutes payées en prod :
> - ⚠️ **La découverte annonce l'ÉMETTEUR du tenant, en direct** (depuis le 20/10/2026,
>   `auth/decouverte.py`). Du 13/08 au 20/10 elle annonçait une FAÇADE sur le host du
>   tenant, parce que Logto self-hosted ne savait inscrire aucun client à la volée : la
>   façade rendait un client préparé (`tenants.oauth_client_id`), posait le rappel du
>   client dans SON annuaire (`tenants.logto_mgmt`, oto-backend#909) et, sur un host
>   relayé, réécrivait l'`iss` de la réponse (RFC 9207). Avec CIMD (Logto ≥ 1.44), le
>   client s'inscrit chez l'annuaire du tenant lui-même : l'annuaire d'un tenant servi
>   par cette instance DOIT avoir CIMD activé. `tenants.oauth_client_id` n'a plus de
>   lecteur (affiché seulement, suppression par une révision) ; `tenants.logto_mgmt`
>   (accès d'administration de son annuaire, jamais un secret : `credential` est le NOM
>   d'un couple de variables d'environnement) reste déclaré et lisible
>   (`logto_mgmt` vs `directory_admin` sur `/platform/tenants`), sans geste qui s'en serve
>   (`docs/auth-logto.md`).
> - ⚠️ **Pas de patron, pas de lien** (`links.py`, `tenants.link_paths`). Les chemins d'un
>   partenaire ne ressemblent pas aux nôtres et certaines de nos vues n'ont **aucun**
>   équivalent chez lui : coller nos chemins sous son domaine fabrique des liens morts, pire
>   qu'un lien à notre marque. Un lien AFFICHÉ peut valoir `None` ; une REDIRECTION (retour
>   OAuth) aboutit toujours (`redirect_for`) — on ne peut pas « ne pas rediriger ».
> - ⚠️ **Nos propres patrons par défaut se vérifient contre le ROUTEUR du front, pas de
>   mémoire.** Corrigé le 28/08 : `DEFAULT_PATHS["doc"]` a dit `/docs/{id}` du 13/08 au
>   28/08 — un chemin que le tableau de bord ne route pas (il ne connaît que la section
>   `/documents`, sans id) et que son attrape-tout renvoie sur `/overview`. Le lien
>   n'aurait affiché aucune erreur : il aurait ouvert la page d'accueil en se faisant
>   passer pour la page demandée. Il n'avait aucun appelant, ce qui l'a gardé invisible un
>   mois — c'est-à-dire le lien mort que ce module interdit, posé chez nous. Le vrai
>   chemin d'une page l'ouvre DANS son projet (`/projects/{project_id}?doc={id}`) : une
>   page n'a pas d'écran à elle. **Un patron sans appelant n'est pas inerte, il est
>   simplement non testé** — n'en déclarer un qu'avec ce qui l'utilise.
> - **Les chemins de NOTRE instance se déclarent par instance** (`OTO_DASHBOARD_LINK_PATHS`,
>   JSON `{type: chemin}` fusionné sur `DEFAULT_PATHS`). Le tenant primaire ne lit pas sa
>   ligne `tenants` (son émetteur vient de l'env) : ses `link_paths` n'ont jamais servi. Une
>   instance dont le tableau de bord range tout sous l'org (`/org/{org}/tables/{id}`)
>   servait donc `/data/{id}` à chaque tableau, une adresse que son routeur ne connaît pas.
>   Conformément à la convention ci-dessus, le correctif est un réglage d'INSTANCE, pas un
>   champ de plus sur le tenant. Les appelants de `table`, `doc`, `project` et `billing`
>   ne résolvent l'org (`access.current_org`) que si le chemin la réclame
>   (`patron_reclame`) ; sans org, aucun lien, jamais `/org//…`.
>   Une déclaration illisible ou un type de lien inconnu refuse le démarrage
>   (`links.verifier_reglages`), jamais nos chemins par défaut en silence. Deux types
>   n'existent que DÉCLARÉS (`links.DECLARED_ONLY_KINDS`, sans chemin par défaut) : `app`
>   (`/org/{org}/apps/{id}`) et `connect`, la page d'UN connecteur et de son formulaire
>   d'ajout (`/org/{org}/connect/{connector}`), où `oto_connector op=setup_link` envoie la
>   personne ; non déclaré, le lien retombe sur la page des connecteurs, ouverte sur la carte.
>   Un troisième, `webhook`, est l'écran où une personne tire le jeton d'un agent webhook
>   (`/org/{org}/webhook/{id}`). Le déclarer est un CHOIX D'INSTANCE, pas un repli : déclaré,
>   sur la face MCP, `oto_trigger op=create` (webhook) et `op=rotate_secret` ne rendent que
>   ce lien, jamais le jeton (la page doit donc exister sur l'instance) ; non déclaré,
>   l'agent reçoit le jeton, une fois, dans `hook_secret`, comme une personne.
> - **Le socle d'instructions suit le tenant** (`guides` scope `tenant`, owner = le slug) :
>   sinon l'assistant d'un partenaire se présente sous NOTRE marque à chaque session.
> - **La page publique d'un projet partagé suit la marque de son PROPRIÉTAIRE**
>   (15/09/2026). Son destinataire n'est pas authentifié : ni compte, ni session, ni org
>   — c'est ce qui la sépare des autres surfaces, qui tiennent toutes un `sub`. Le tenant
>   s'y résout donc par la DONNÉE : projet → org → `db.org_tenant_slug`. Pas par l'hôte,
>   qui ne dit que par où le visiteur est arrivé — et un lien frappé sur le mauvais
>   domaine rendrait un hôte qui ment. La marque vient d'`email_brand.marque`, même cas
>   exactement (un rendu HTML pour un non-authentifié, dont le tenant vient d'un objet),
>   avec le NOM d'affichage repris au registre : `marque()` est écrite pour
>   `orgs.front_brand`, où l'argument est déjà un mot de marque, pas un identifiant.
>   ⚠️ **Nos couleurs ne partent jamais** : un tenant sans palette déclarée prend les gris
>   du système à son nom, jamais notre saffran — une palette est une marque. Les jetons
>   que `Marque` ne couvre pas (papiers, accents) ne sont PAS dérivés : dériver produit un
>   dessin que personne n'a dessiné, et il ne se verrait qu'à l'arrivée.
> - **La page publique d'un DOC partagé suit la même règle** (15/09/2026), et c'est la
>   surface la plus exposée : n'importe qui avec le jeton, sans authentification, sans
>   même un sous-domaine qui dirait de qui il s'agit. Son handler ne recevait QUE le
>   jeton — `get_doc_by_public_token` joint désormais `projects` pour remonter le
>   propriétaire, en `LEFT JOIN` : un doc dont le projet a disparu reste servi sous la
>   marque de la plateforme, parce que perdre le document parce qu'on ne sait plus à qui
>   il est punirait le lecteur d'un défaut qui ne le concerne pas. La page **introuvable**
>   porte la marque elle aussi : c'est celle qu'un jeton périmé sert, donc la plus vue
>   depuis l'extérieur. La résolution commune aux deux pages vit dans `brand`
>   (`marque_du_proprietaire`, `jetons`, `nom_affiche`) — la laisser dans l'une ferait
>   dépendre la page de doc de l'UI de partage.
> - **Les guides à la demande aussi**, depuis le 15/09/2026 — même colonne, même clé.
>   `read_guide_scoped` cherche **tenant → plateforme → org → user**, et le catalogue
>   REMPLACE notre entrée de même slug au lieu de s'y ajouter (deux lignes pour un slug
>   feraient choisir l'agent entre deux guides dont un seul lui serait rendu). Le socle
>   PRESCRIT d'aller les lire : les tenant-iser à moitié laissait le texte le plus lu
>   après lui au niveau plateforme. ⚠️ **Rien ne change tant que personne n'a rédigé** —
>   un tenant sans guide propre reçoit le nôtre, à l'octet. **L'écriture a suivi la
>   lecture** : `oto_guide op=write|patch|delete scope=tenant owner_id=<slug>` (on-demand),
>   au même palier que le socle — admin plateforme, tenant connu du registre. Avant,
>   `guide_store.set_guide` refusait le scope : l'étage se lisait sans pouvoir s'écrire,
>   et un besoin propre à un partenaire (un guide que le cœur a retiré, par exemple)
>   n'avait d'autre place que le socle entier de ce tenant.
> - ⚠️ **Le socle ne suffisait pas : les OUTILS aussi portaient notre nom.** Dans la
>   conversation d'un client du partenaire, chaque appel s'affichait `Oto doc`,
>   `Oto project`… — le nom d'un outil n'est pas de la prose, il est réaffiché à chaque
>   tour. `tenants.tool_prefix` (NULL = inerte) fait traduire `oto_*` → `<prefix>_*` **au
>   bord du protocole seulement** (`tool_alias.py` + `ToolAliasMiddleware`, OUTERMOST) :
>   le nom CANONIQUE est rétabli avant que quoi que ce soit d'autre ne le lise, donc le
>   journal `tool_calls`, les toggles `user_disabled_tools`, les gates par namespace et
>   les refs `<tool:slug>` des procédures ne connaissent toujours qu'UN nom par outil —
>   rien à migrer. Les deux formes sont acceptées à l'appel (la prose déjà écrite cite
>   les canoniques). Sont traduits : `tools/list` (noms + descriptions), l'artefact de
>   session, **le corps d'un guide servi** (15/09/2026 — le socle prescrit `oto_guide
>   op=read`, et ce qu'on y lisait prescrivait à son tour des outils que le compte n'a
>   pas ; la portée est les NOMS D'OUTILS, jamais le nom du produit, qui relève de la
>   prose et donc de l'étage tenant), le contrat d'erreur, et les cinq tools qui
>   prennent un nom en argument
>   (`oto_call`, `oto_tool_schema`, `oto_list_my_tools`, `oto_{disable,enable}_tool`) —
>   sinon le catalogue et le dispatch parleraient deux langues. **Seul le namespace `oto`
>   bouge** : `data_*`, `run_*`/`feedback` et les connecteurs nomment une capacité ou un
>   fournisseur, pas notre marque. ⚠️ Un préfixe qui serait un namespace déclaré est
>   REFUSÉ (l'alias éclipserait un vrai outil) ; rien n'est réparé au passage (`Acme` est
>   refusé, pas abaissé — cf. le slug). Poser = `UPDATE tenants SET tool_prefix=…` **+
>   restart** (le registre est bâti au boot) ; l'écran `/platform/tenants` nomme l'écart
>   `tool_prefix` (déclaré) vs `tool_prefix_effectif` (appliqué). ⚠️ **Pas de canari
>   possible** : prod et preprod partagent la base (§Infra), donc la colonne posée vaut
>   pour les deux — la seule fenêtre de test est le décalage des redémarrages. La face
>   REST reste en canonique : ses écritures sont keyées par nom, et le dashboard d'un
>   tenant n'est pas le nôtre. **`serverInfo` du `initialize` est traduit aussi
>   (23/08)** : `name` suit le `tool_prefix` déclaré, `title` le nom du tenant
>   (`tool_alias.server_identity_for` + hook `on_initialize` du même middleware) —
>   rien de déclaré ⟹ l'annonce d'avant, à l'octet près. ⚠️ **Rien de cela n'atteignait
>   le client avant le 29/09/2026**, pas plus que le socle de tenant : le hook modifiait la
>   réponse après son envoi (`docs/guides.md`, `middleware/_handshake.py`). Depuis,
>   `name` part ; **`title` ne peut pas partir** — les options de session de la
>   bibliothèque MCP n'ont pas ce champ.
>
> **Le suivi est un ÉCRAN depuis le 15/08** (`capabilities/tenants_admin.py` + `db.list_tenants_overview`,
> REST `/api/admin/tenants[/{slug}]`, MCP `oto_admin_tenant`, dashboard `/platform/tenants`) : qui est
> servi, sous quel émetteur, avec quelles orgs/comptes/appels. Trois choses à savoir avant d'y toucher :
> **(1) lecture seule** — le provisionnement reste un runbook et le registre est bâti AU BOOT, d'où le
> verdict `pending_restart` (déclaré en base, absent du registre ⟹ ses jetons sont encore rejetés) plutôt
> qu'un formulaire. **Depuis le 23/08, la prise d'effet ne demande plus de restart** :
> `oto_admin_tenant op=reload` / `POST /api/admin/tenants/reload` (SUPER_ADMIN,
> `server.reload_tenant_registry`) relit la base et swappe atomiquement le registre installé ET les
> émetteurs acceptés du verifier vivant — échec de lecture ⟹ rien n'est écrit, l'ancien registre reste
> entier. **Un refus de ligne ALERTE depuis le 07/09** (`tenancy._refus` → Sentry, cf.
> `docs/monitoring.md`) : une déclaration refusée au boot laissait jusque-là un tenant non
> chargé en silence ; le rapprochement « déclaré vs chargé » reste, lui, en lecture seule
> (`pending_restart`). ⚠️ Par-process : recharger la preprod ne recharge pas la prod (même topologie que les `.env`) ; **(2) les deux rattachements sont comptés SÉPARÉMENT** (`orgs.tenant_id` vs la
> qualification du sub) et l'écart est nommé `orgs_desalignees` — en dériver un chiffre unique le ferait
> mentir ; **(3) c'est le premier LECTEUR de `orgs.tenant_id`** : le garde-fou L1 est passé d'une
> interdiction totale à une **allowlist** de deux fichiers (`test_tenant_l1_migration.py`) — un chemin de
> **résolution** qui dépendrait du rattachement d'org le casse toujours, et c'est voulu.
>
> ✅ **Une fusion de comptes RECHIFFRE les clés personnelles** (oto-backend#439). L'AAD
> dérive de l'entité : un `UPDATE entity_id` nu rendrait la ligne indéchiffrable, et c'est
> pourquoi `migrate_sub` les ABANDONNAIT sous l'ancien identifiant — invisibles pour leur
> propriétaire, qui ne savait pas devoir les reposer (13 clés membre sur 43 après la
> bascule du 13/08). `credentials_store.rekey_personal_credentials` (étape 3 ter de
> `repointer_patrimoine`) les déchiffre sous l'ancienne entité et les rechiffre EN PLACE
> sous la nouvelle ; l'instance suit avec son id. Une collision (le compte canonique a
> déjà la même clé : la sienne gagne) ou une ligne illisible reste en place, journalisée
> et comptée. ⚠️ Le scope `member` a `entity_id = "<org_id>:<sub>"` : une requête qui
> cherche le sub nu ne les voit PAS (elles sont pourtant la majorité), et un filtre par
> suffixe prendrait celles d'un sub qualifié qui se termine pareil.
> **Les résidus d'une fusion passée** (clés abandonnées avant #439, journal REST
> attribué au sub revendiqué avant que l'authentification ne publie le porteur résolu)
> se reprennent par `scripts/repointer_residus_alias.py` — le même `repointer_patrimoine`,
> à blanc par défaut ; un ancien identifiant RECRÉÉ depuis est écarté et nommé.
> ⚠️ **Deux passes** : une transaction pour ce qui se lit par un index (ou vit dans une
> table bornée), puis des LOTS par plage de `id`, chacun validé à sa fin, pour les colonnes
> sans index (`tool_calls.effective_sub`/`view_as_sub`, `nodes.owner_id`…) — d'un bloc,
> elles balaient la table et tombent sur `statement_timeout` (vécu le 30/09). Reprise :
> relancer, ou `--depuis <table>:<id>` ; la garde `test_repointer_residus_alias` refuse
> toute colonne traitée d'un bloc sans index ni raison d'être bornée.
>
> ⚠️ **Une fusion de comptes emporte la MARQUE d'espace personnel — depuis le 14/08
> seulement.** `orgs.personal_of` échappait aux deux garde-fous (pas une FK ⟹ invisible à
> `test_migrate_sub_cascade` ; pas dans l'inventaire ⟹ hors `test_migrate_sub_inventory`,
> qui vérifie que les entrées listées EXISTENT, jamais que les colonnes porteuses d'un
> identifiant soient listées). La marque restait donc sur un identifiant que l'étape 4
> supprime : plus d'espace personnel trouvable, et le boot suivant en fabriquait un neuf —
> **deux organisations au même nom**, dont l'ancienne, celle qui porte l'historique,
> n'était plus reconnue comme l'espace de son propriétaire. 14 comptes en prod, dont 9
> issus de la seule bascule du 13/08 ; archivés à la main le 14/08 (les 14 doublons
> n'avaient jamais servi : projets semés, aucune page, aucun tableau, aucune clé).
> Traitement à part (étape 2 quater, `test_migrate_sub_personal_org.py`) : l'espace de
> l'ANCIEN compte reste l'espace personnel sous le nouvel identifiant, celui du nouveau
> est démarqué — jamais archivé automatiquement, « cet espace n'a jamais servi » ne se
> décide pas au fond d'une transaction de merge.
>
> ✅ **Les colonnes à sub que le merge abandonnait sont TRIAGÉES depuis le 23/08.**
> L'historique (`runs.sub`, `project_activity.sub`, `runner_triggers.sub`,
> `tool_calls.effective_sub`) et les attributions (`resolved_by`/`created_by`/
> `granted_by`/`set_by`/`requested_by` de 10 tables) sont repointés par `_SUB_COLUMNS` ;
> `legal_acceptances` et `option_comps.entity_id` passent
> par le patron PK (`_PK_SUB_TABLES` — sub jamais numérique ⟹ l'UPDATE ne touche que les
> lignes user) ; ⚠️ **le JOURNAL des acceptations (`legal_acceptance_events`, #487) est
> ajouté à `_SUB_COLUMNS` et PAS au patron PK** — il n'a aucune unicité, et dédupliquer
> y supprimerait des consentements pour cause de doublon. C'est lui que le gate lit :
> `legal_acceptances` n'est plus qu'une projection transitoire (issue #507) ; les arêtes `grants.grantee_id` ont leur étape filtrée (3 bis). Le trou de
> méthode est fermé par le **tripwire inverse** `test_migrate_sub_sub_bearing_columns_are_
> triaged` : toute colonne du DDL de la famille « porte un sub » doit être repointée,
> pré-traitée ou allowlistée AVEC sa raison — une colonne neuve arrive rouge. Restent
> hors repointage NU : `connector_credentials.entity_id` (AAD — rechiffré, cf. plus haut)
> et la mécanique du merge lui-même (`sub_aliases`, `users.sub`, `orgs.personal_of`).
> ⚠️ Le tripwire reconnaissait une LISTE de noms, et laissait passer `updated_by`,
> `edited_by`, `disabled_by`, `view_as_sub`, `acteur_sub` : neuf colonnes abandonnées
> jusqu'au 29/09 (#439). Il lit désormais la FORME (`*sub`, `*_by`) ; les identités de
> machine (worker, ordonnanceur) et les baux éphémères sont allowlistés avec leur raison.
>
> ⚠️ **Une TROISIÈME famille depuis le 2026-09-02 : `_UNIQUE_INDEX_SUB_TABLES`** (étape
> 2 quinquies). Elle existe parce que les deux premières répondent à des questions
> différentes, et qu'une table peut n'entrer dans aucune : `outreach_sends` a pour clé
> primaire un `id` BIGSERIAL, mais son unicité réelle est un index **partiel**
> (`(campaign, sub) WHERE kind='send'`). Rangée d'abord dans `_PK_SUB_TABLES`, elle
> aurait fait prendre `kind` pour une colonne de clé et **supprimé des essais que rien
> ne menaçait** ; rangée dans `_SUB_COLUMNS` seule, l'UPDATE nu aurait levé
> `UniqueViolation` dès que les deux comptes d'une personne ont reçu la même campagne —
> et fait échouer TOUT le merge. La famille dédoublonne donc sur les colonnes de
> l'INDEX, prédicat partiel reporté sur les deux côtés, puis laisse l'UPDATE nu de
> l'étape 3 repointer (d'où l'obligation d'être AUSSI dans `_SUB_COLUMNS`).
> Garde-fous dérivés du DDL : `test_migrate_sub_unique_index.py` (l'index existe, avec
> ces colonnes et ce prédicat ; l'entrée a son repointage ; elle n'est pas dans deux
> bacs). **Le classement reste manuel, et c'est le vrai défaut** : deux erreurs
> symétriques en quatre heures le 02/09 (deux colonnes de clé mises en repointage
> simple le matin, une colonne hors clé mise en patron PK le soir).
>
> ⚠️ **Et le classement manuel a un second trou, celui-là JAMAIS gardé** : les trois
> familles répondent toutes à la même question — *un UPDATE nu peut-il lever une
> violation d'unicité ?* — mais rien ne vérifiait qu'une colonne à qui la réponse est
> OUI soit bien rangée quelque part. `test_pk_sub_tables_reste_matches_the_real_primary_
> key` juge les entrées DÉCLARÉES ; il ne va jamais chercher les MANQUANTES.
> `test_active_membership_tables_are_pre_treated` ne ferme qu'une forme écrite en dur
> (`ON <table>(sub) WHERE is_active`), et seulement dans `_schema.py`.
>
> `test_toute_colonne_sub_sous_index_unique_est_pre_traitee` (2026-09-02) ferme la
> CLASSE : toute colonne de `_SUB_COLUMNS` couverte par un index unique — partiel ou
> non, déclaré dans `_schema.py` **ou créé par `_init.py`** — doit être pré-traitée ou
> allowlistée avec sa raison. La moitié `_init.py` est le cœur du sujet : **dix des
> quatorze index uniques du schéma y sont créés, et aucune garde ne les avait jamais
> lus** — dont quatre des huit qui couvrent une colonne porteuse de sub.
> Une colonne qui n'est PAS repointée (`connector_instances.owner_id`,
> `orgs.personal_of`) sort d'elle-même du critère : pas d'UPDATE nu, pas de violation.
>
> 🔴 **Ce que la garde a trouvé, et qui reste OUVERT : `user_datastores.owner_id`.**
> `uq_user_datastores_owner_ns (owner_type, owner_id, namespace)` n'est pas partiel ;
> deux comptes d'une même personne ayant chacun un namespace du même nom font lever
> `UniqueViolation` à l'étape 3 et échouer TOUT le merge — le mode d'échec du 28/07,
> encore ouvert, reproduit sur base réelle le 2026-09-02. Il n'est pas corrigé avec la
> garde parce que le geste mécanique des autres familles (DELETE de la ligne en trop)
> serait PIRE que la panne : `datastore_rows` est en `ON DELETE CASCADE` sur
> `user_datastores(id)`, donc jeter le namespace de l'ancien compte détruit ses
> LIGNES. Un merge qui échoue est bruyant et rejouable ; des lignes effacées en
> silence, non. La résolution correcte est probablement de RENOMMER le namespace
> repris — le nom que verra l'utilisateur est une décision de produit, pas de
> tuyauterie. Entrée d'allowlist datée, à retirer une fois tranché.

## La clé de connecteur du tenant (L-clés PR 1 — 2026-08-29)

**Ce que c'est.** Un opérateur de N orgs était condamné à la clé plateforme mutualisée ou à
une clé par org. Depuis cette PR, **le tenant possède sa clé de connecteur** : une ligne du
coffre `connector_credentials` avec `entity_type='tenant'`, `entity_id` = le slug — même
sceau que les autres entités (l'AAD porte `tenant:<slug>`, un ciphertext de tenant ne se
transplante sur rien d'autre), même naissance d'instance à la pose (`connector_instances`,
`owner_type='tenant'`, prévu « inerte » par le lot L6 et activé ici), même archivage au
retrait. Aucun credential existant n'a bougé : l'AAD d'une ligne d'org est identique à
l'octet avant et après (`tests/test_tenant_key_vault_live.py`).

**Où elle sert.** Entre l'org et la plateforme dans le walker unique
(`access.cascade.walk_cascade`) : `membre > équipe > org > tenant > plateforme`. Elle sert à
toutes les orgs du tenant qui n'en ont pas de plus proche. Trois choix qui ne se relisent
pas dans le code seul :

- **Le tenant se lit sur le sub qualifié de l'appelant** (`tenant_vault.rung_tenant`),
  jamais sur le rattachement de l'org — c'est le garde-fou du lot L1 (aucun chemin de
  résolution ne dépend de ce rattachement), et il tient toujours. Un compte du tenant
  actif dans une org « désalignée » (`orgs_desalignees` du suivi) voit donc SA clé de
  tenant, pas celle du tenant de l'org. L'endpoint MCP anonyme (`<slug>.mcp.oto.cx`, pas
  de sub) n'a pas l'étage : lui le donner demande l'arête tenant→org de 0053 (PR 2).
- **Le tenant `oto` n'a pas de clé de tenant.** Ses clés partagées SONT les instances
  plateforme, avec leurs grants ; une clé « tenant oto » serait un second mécanisme pour
  la même fonction. Refusée à la pose (`PrimaryTenantKeyRefused`) et **jamais sondée** à
  la lecture — les deux d'un même geste. Conséquence : un sub nu ne coûte aucune lecture
  de plus par appel (serveur mono-loop : c'est ce qui a été mesuré, pas un confort).
- **La fenêtre L7 est symétrique** : la chaîne 0053 (`chain_shadow`) calcule le même
  étage à la même source. Sans ça, chaque appel servi par une clé tenant aurait compté
  une divergence `inconnu` — la seule classe que la porte de la PR 2 de L7 exige à zéro.

**Surfaces admin (plancher opérateur — le rôle « admin de tenant » est la PR 2).**

| geste | surface | plancher |
|---|---|---|
| lister (jamais le secret) | `GET /api/admin/tenants/{slug}/keys` · `oto_admin_tenant op=keys` | platform admin |
| poser / roter | `PUT /api/admin/tenants/{slug}/keys/{provider}` — **REST seule** (`api_key` \| `fields` \| `base_url`, `account`) | super admin |
| retirer | `DELETE /api/admin/tenants/{slug}/keys/{provider}` · `oto_admin_tenant op=key_clear` | super admin |

La pose est REST seule pour la même raison que les clés d'org et les clés plateforme depuis
le 2026-06-25 : **un secret brut ne traverse pas un appel d'outil.** Même validation qu'une
clé d'org (`providers.org_secret_meta`, écriture partielle #448, garde de compte #409).

**Retour arrière.** Retirer la clé (`key_clear`) ramène toutes les orgs du tenant à la
cascade d'avant, à l'appel suivant — rien à invalider. Aucune colonne n'a été ajoutée : la
valeur `tenant` d'`entity_type` reste inerte tant qu'aucune ligne ne la porte, et le
`CHECK` d'`owner_type` sur `connector_instances` l'acceptait déjà.

**Ce qui restait à la PR 2** (livré le 2026-08-29, section suivante) : le rôle « admin de
tenant », l'arête tenant→org de 0053, l'étage tenant de l'endpoint anonyme, la ligne
`tenant` dans `oto_instance op=list`. Reste côté dashboard : le mode `tenant` de `/api/me`
(`keyStack.ts`, oto-front).

## L'app Google du tenant — son écran de consentement (2026-09-23)

Un tenant qui veut que ses utilisateurs consentent chez Google sous SA marque (son projet
Google Cloud, ses scopes vérifiés sous son nom) pose son client OAuth comme **app
d'éditeur** du connecteur `google`, keyée `tenant:<slug>` — un espace de noms à part,
jamais confondable avec une région zoho — coffre chiffré (`docs/connector-vault.md`
§app d'éditeur), aucune colonne, aucun env : le mécanisme existait, Google ne le lisait
pas. Deux faces pour la poser, même ligne au coffre :

- **la sienne**, depuis SON tableau de bord (`tenant_apps`) :
  `PUT /api/admin/tenants/{slug}/apps/{connector} {"client_id", "client_secret"}` ;
  `GET …/apps` (liste, jamais le secret, avec le rappel de chacune) ; `DELETE
  …/apps/{connector}`. Plancher `TENANT_ADMIN_OF(slug)` comme ses clés : l'admin du
  tenant (rôle lu sur le sub qualifié) OU le super admin. **Scopé au slug de la
  route** : un admin de `pilote` ne voit ni ne pose rien sous `autre`, et l'app d'un
  tenant ne sert qu'aux comptes qualifiés sous son slug (`google_oauth.app_for`) —
  jamais à un autre tenant, jamais à la plateforme. **Liste fermée** : `google` seul —
  un connecteur n'y entre qu'avec un flux qui LIT l'app d'un tenant ; zoho lit une
  région, et un autre connecteur est refusé en 400 `tenant_app_unsupported` ;
- **celle de l'opérateur** (`platform.editor_app.set`, `POST /api/admin/editor-apps
  {"connector": "google", "data_center": "tenant:<slug>", …}`, super admin).

- **Le rappel est celui du tenant** : `https://<host>/api/google/oauth/callback`, sur le
  premier host déclaré que le tenant TIENT (`tenancy.callback_host` — un host réclamé
  mais tenu par un autre tenant est sauté ; rendu par la réponse de la pose). C'est
  CETTE URL qu'il déclare dans son client Google — pas la nôtre, que son client
  n'accepterait pas. ⚠️ **Ce host doit router vers ce backend** : le code
  d'autorisation et le state signé y arrivent. Un host qui mène ailleurs (le site du
  partenaire, par exemple) enverrait l'un et l'autre à un tiers — ce n'est pas vérifiable
  d'ici, c'est une condition de la déclaration du host. Sans host tenu, le rappel reste le
  nôtre.
- **Sans app posée, rien ne change** : le tenant consent sous notre client (env) et notre
  rappel. Poser l'app bascule TOUT compte du tenant d'un coup — consentement, échange,
  refresh. **Un jeton ne se rafraîchit qu'avec le client qui l'a émis** (noté sur le
  jeton) : un compte connecté avant la pose est refusé à son prochain appel, avant tout
  réseau — « reconnecte ce compte », compte marqué, jamais purgé.
- **Scopes** : sous le client du tenant, le consentement n'est pas incrémental
  (`include_granted_scopes` n'est posé que sous le nôtre) — le jeton ne ramène pas les
  scopes accordés aux autres produits du partenaire.
- ⚠️ **Le host du tenant arrive sur UNE instance** (la prod) : un consentement démarré en
  preprod avec l'app du tenant rappelle en prod, où le state est vérifié avec le secret de
  la prod. L'app d'un tenant se teste là où son host arrive.
- Retirer l'app (`DELETE /api/admin/tenants/<slug>/apps/google`, ou
  `DELETE /api/admin/editor-apps/google/tenant:<slug>`) ramène le tenant sur la nôtre — et
  rend ses comptes connectés sous la sienne à reconnecter (même refus nommé).

## Le plafond d'activation du tenant (2026-09-26)

Un tenant n'offre pas tout le catalogue — un service Google que son projet Google Cloud ne
déclare pas, un connecteur qu'il ne supporte pas. Le cran **`tenant`** de
`connector_availability` (`scope_id` = le slug) est un **plafond** entre la plateforme et
l'org : `enabled=false` coupe pour TOUTES ses orgs, et rien en dessous — override d'org,
équipe — ne rouvre (`cran_qui_coupe` rend `tenant`, le refus d'appel nomme l'hébergeur) ;
`enabled=true` ne fait que retirer la coupure, un tenant n'expose jamais ce que le master
plateforme ne donne pas. Résolution : `(override d'org > master > OFF)` **sous** le plafond
tenant, puis la coupure d'équipe (`connectors/activation._resolve`).

- Face du tenant (`tenant_connectors`) : `GET /api/admin/tenants/{slug}/connectors/activation`
  (master, ligne tenant, résultante) ; `PUT …/{name} {"enabled"}` ; `DELETE …/{name}`.
  Plancher `TENANT_ADMIN_OF(slug)` comme ses clés et ses apps. Primaire refusé.
- Le cockpit d'org (`connectors.activation.org_list`) porte `tenant_enabled` et refuse la
  pose ON sous un plafond fermé (`409 tenant_disabled`).
- Le tenant de l'org se lit par `db.org_tenant_slug` (union des trois axes) : une org du
  tenant primaire n'a pas de cran tenant.
- Un split de connecteur (`fanout_availability`) recopie les lignes de la source à TOUS les
  scopes, tenant compris : une coupure posée sur `google` suit ses services.

## Le rôle « admin de tenant » et l'arête tenant→org (L-clés PR 2 — 2026-08-29)

**Le rôle.** La sortie nommée du régime transitoire (0052 §Amendement 27/08 : l'opérateur du
premier tenant tiers est `super_admin` faute de ce rôle). Table `tenant_admins (slug, sub)`,
additive et réversible ; règle `_authz.TENANT_ADMIN_OF(slug, platform=…)` = la règle
plateforme essayée d'abord (`PLATFORM_ADMIN` pour lire, `SUPER_ADMIN` pour écrire), et sur
son 403 seulement, l'admin du tenant — **lu sur le sub qualifié** (`tenancy.tenant_of`), jamais
sur le rattachement d'une org (L1). Un admin déclaré sur `pilote` est un compte `pilote:…` ;
le tenant `oto` n'en a pas (ses admins sont ceux de la plateforme). Ce qu'il fait : poser et
retirer la clé de son tenant, en accorder l'usage à ses orgs, voir la fiche de son tenant, et
**mettre en pause / réveiller les comptes de son tenant** (2026-09-03,
`docs/comptes-en-pause.md`).
Déclarer le rôle reste un acte de la plateforme (`POST/DELETE /api/admin/tenants/{slug}/
admins[/{sub}]`, `oto_admin_tenant op=admin_add|admin_remove`, super admin).

⚠️ **L'admin de tenant agit par la face REST (son tableau de bord), pas par `oto_admin_tenant`.**
Le plancher d'un outil consolidé est le plus BAS de ses ops (`_authz._lowest_floor`) : une seule
op au rôle de tenant ferait descendre `oto_admin_tenant` à `None`, c'est-à-dire l'entrée de
l'outil dans le handshake de chaque compte de la plateforme. La console garde donc son plancher
`operator` (gardé par `test_tenant_admin_role.py`), et ce sont les capacités REST par geste qui
portent `TENANT_ADMIN_OF`.

⚠️ **Une exception assumée depuis le 2026-09-03 : `oto_admin_account`** (mettre un compte en
pause) est servi **en MCP**, plancher `None`, donc visible de tout le monde — 1 242 caractères,
1,37 % de ce qu'un compte ordinaire reçoit au handshake. Le raisonnement ci-dessus reste juste
(le plancher descend), mais sa conclusion ne vaut que pour une console qui a d'AUTRES ops : ici
la capacité est dédiée, et la restreindre à la face web reproduirait le défaut qui rend le rôle
inerte — **un partenaire travaille par MCP**, et fastmcp refuse aussi le `tools/call` d'un outil
masqué (#471), pas seulement le `tools/list`. Une capacité bornée inatteignable depuis la face
qu'on utilise ne rend littéralement rien.

⚠️ **`tenant_admins` est à zéro ligne en production à ce jour** : tant que personne n'y est
nommé, tous ces verbes ne sont exerçables que par la plateforme. C'est un fait de peuplement,
pas de conception — et c'est ce qui explique que l'opérateur du premier tenant tiers travaille
encore avec un rôle plateforme.

**L'arête tenant→org** (0053-D3 : « le tenant s'insère dans la même chaîne »). Ressource = la
clé du tenant (`tenant:{slug}:{connecteur}`, le ref du coffre), grantor `tenant:{slug}`,
grantee `org:{id}`, contrainte `quota` = appels/jour pour **toute l'org** (R10 : budget partagé,
la lettre de D7). Trois états, ceux de la clé plateforme au lot L5 :

| état | condition | ce que sert la cascade | la chaîne 0053 |
|---|---|---|---|
| MUETTE | aucune arête n'a jamais visé cette org | la clé, comme en PR 1 | `appartenance` |
| ACCORDE | ≥1 arête vivante | la clé, budget débité (`access.tenant_budget`) | `grant` |
| REFUSE | des arêtes, toutes révoquées | le barreau est **sauté** — l'org retombe sur la plateforme | idem |

`chain_shadow` lit l'arête par la **même fonction** que le walker (`grants_chain.tenant_rung`)
et passe au palier suivant sur REFUSE comme lui : aucun `inconnu` créé (gardé par
`test_tenant_edge_chain.py`). L'arête n'est lue qu'APRÈS que la sonde a trouvé une clé —
sans clé de tenant, zéro lecture.

⚠️ **Le budget est vérifié à la RÉSOLUTION et débité au SUCCÈS de l'appel** (L8) : la
résolution refuse une journée épuisée et note l'arête au relevé d'appel
(`tenant_grant_ids`) ; le bord de l'appel la débite une fois l'outil revenu sans erreur
(`access.reglement`), au même point que le compteur plateforme. Un appel qui échoue chez le
fournisseur ne compte plus. Hors relevé d'appel (face REST, commande de maintenance), il n'y a
pas de bord pour régler : le débit se fait à la résolution, comme avant L8.

**L'anonyme** (`<slug>.mcp.oto.cx`, ADR 0032) n'a pas d'identité : il n'obtient l'étage tenant
que par une **arête vivante** tenant→org (`grants_chain.tenant_for_org`), jamais par le
rattachement de l'org. Sans arête, sa cascade reste `org > plateforme`. Le budget vaut pour lui
aussi : l'org entière y puise, anonyme compris. ⚠️ L'arête ne re-tenante personne : un compte nu
dans une org accordée garde sa cascade d'avant.

**Surfaces** (`GET/PUT/DELETE /api/admin/tenants/{slug}/keys/{provider}/grants[/{org_id}]` ;
`oto_admin_tenant op=org_grants|org_grant|org_revoke`, plancher plateforme). Une arête sur une
clé absente est refusée (`no_tenant_key`) : elle ne servirait rien.

**`oto_instance op=list`** rend la clé du tenant de l'appelant au niveau `tenant` (entre `org`
et `platform`, `via=tenant_key`, `owner.type=tenant`), épinglable par `_instance=`. Un compte nu
ne la voit pas : il ne pourrait pas la résoudre (R9).

**Retour arrière.** Retirer le rôle (`admin_remove`) et révoquer les arêtes ramène à #603 —
avec une nuance à connaître : une org RÉVOQUÉE n'est pas une org « sans arête » (les archivées
restent, D7) ; pour la ramener à l'état MUET il faut supprimer ses lignes de `grants` (et
`grant_counters` d'abord, la FK refuse sinon). Aucune colonne retirée, une table neuve.
