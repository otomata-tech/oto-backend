# Architecture — l'arbre des modules, les 4 couches, les deux faces

> Le détail que le `CLAUDE.md` portait jusqu'au 2026-08-31. La carte, elle, y reste :
> la liste des modules et qui appelle qui. Ce document en est la version longue —
> l'arbre commenté module par module, la règle du domaine, les deux faces d'une
> capacité, et l'assemblage de la face REST.

⚠️ **Le dossier d'un fichier EST son domaine** (tranché le 27/08) : une famille de
≥ 4 fichiers au même marqueur devient un package et les fichiers y perdent ce marqueur
(`datastore/schema.py` → `datastore/schema.py`), `tests/` en est le miroir, et un
déplacement ne laisse **jamais** de ré-export à l'ancien chemin. La règle complète (le
critère, le module « nu » en `core.py`, les trois façades d'exception, où naît un
fichier neuf) : **`docs/conventions.md` §Où vit un fichier**.

```
oto_mcp/
├── server.py         # FastMCP + uvicorn, _SERVER_INSTRUCTIONS, routes /api, tools
├── capabilities/     # les CAPACITÉS (ADR 0009), sous-rangées par domaine : `orgs/`,
│                     #   `connectors/`, `datastore/`, `groups/`, `docs/`. Le socle commun
│                     #   (`registry`, `_authz`, `_types`, `_mcp_adapter`,
│                     #   `_rest_adapter`) reste à la racine du package.
├── api/              # la face REST `/api/*` : `routes` (la TABLE — son ordre est un
│                     #   contrat), `base` (auth, CORS, `_json`, préflight, `bind`),
│                     #   puis un module de handlers par domaine. ⚠️ Une route neuve
│                     #   naît CAPACITÉ, pas ici : la dette REST vaut ZÉRO.
├── auth/             # QUI parle au serveur, et comment un credential s'ACQUIERT.
│                     #   Entrant : hooks (le sub du jeton), facade (façade DCR devant
│                     #   Logto), token_scopes (portée d'un jeton `oto_…`), anon (shim
│                     #   OAuth des endpoints publics). Sortant : flow (la danse
│                     #   `authorization_code`, écrite UNE fois), pkce, puis un module
│                     #   par fournisseur — atlassian, folk, google, salesforce, zoho.
├── connectors/       # le connecteur côté PLATEFORME : activation, selection, identities,
│                     #   link, flow, verify, field_schema, schema_store + `docs/` (la
│                     #   fiche how-to en markdown) et `docs_reader`. ⚠️ trois voisins à
│                     #   ne pas confondre : `providers/` = le REGISTRE, `tools/` = les
│                     #   outils servis à l'agent, `connectors/` = la gouvernance.
├── fod/              # les clients du service FOD (ADR 0028) : `http` = le transport
│                     #   partagé, un module par domaine de données publiques FR.
├── datastore/        # le spine de records typés : core (le store qui COMPOSE), schema,
│                     #   schema_ops, columns, errors, journal. `__init__` sans code.
├── middleware/       # la chaîne MCP, 1 module par middleware (alias, empty_result,
│                     #   call_context, field_redaction, error_envelope, disabled_tools,
│                     #   dynamic_instructions). L'ORDRE d'enregistrement est un contrat
│                     #   et vit dans `server.py` — figé par
│                     #   `tests/middleware/test_middleware_order.py`.
├── tools/            # 1 module par connecteur, chacun expose register(mcp)
├── providers/        # le REGISTRE : 1 module de déclaration par connecteur
│                     #   (`CONNECTOR = _c(…)`), `__init__` AGRÈGE, `_model` = la forme.
│                     #   Séparé de tools/ : le registre doit rester PUR (tools/ importe
│                     #   access → le registre, et se charge en try/except — une dép
│                     #   optionnelle manquante retirerait le connecteur du catalogue).
├── access/           # rôles, contexte, cascade de credentials, quotas (package depuis le 27/08 — 2 000 lignes en 10 modules). Surface plate `access.<fn>` via __init__, cf. ci-dessous
│   ├── scope.py      #   qui agit : rôle plateforme, current_org/group/project, ce que le projet ÉPINGLE
│   ├── quotas.py     #   ce qui est métré (quota jour, usage) et ce qui est payé (option, comp, abonnement)
│   ├── cascade.py    #   le WALKER unique perso > cross-org > équipe > org > TENANT (L-clés, 29/08) > plateforme + ses 3 sondes
│   ├── rbac.py       #   qui a le droit : RBAC connecteur org/équipe, tools masqués, garde d'instance, redaction
│   ├── resolved_credential.py  # le TYPE rendu par toute résolution (extrait de resolve le 29/08, cliquet #584)
│   ├── tenant_budget.py  # le budget par org de l'arête tenant→org (L-clés PR 2), appliqué à la résolution
│   ├── resolve_anon.py  # l'endpoint MCP anonyme (ADR 0032), extrait de resolve le 29/08 — l'étage tenant n'y vient que d'une arête
│   ├── resolve.py    #   la résolution réelle d'un credential (chemin chaud)
│   ├── views.py      #   vues minces : resolve_api_key/_fields, mount, credential_mode_for, option_open
│   └── status.py     #   le snapshot par connecteur de /api/me
├── db/               # store PG (package) : _conn (pool/connexion), _schema (DDL), _init (migrations) + 1 module/domaine (users, keys, usage, datastore, projects, opendata…). Surface plate `db.<fn>` via __init__
├── org_store/        # palier ORG (package, découpé le 2026-08-27) : orgs (la fiche), members (appartenance + MAISON),
│                     #   vault (secrets), settings (redaction/email/MFA), personal (org perso + boot),
│                     #   invitations (plateforme/org/équipe), instructions (procédures), library (guides publiés).
│                     #   DAG à 2 étages : {personal, invitations} → {orgs, members} ; library → instructions.
│                     #   Surface plate `org_store.<fn>` via __init__, qui REDESCEND les écritures sur le module
│                     #   propriétaire (sinon un monkeypatch de test serait mort en silence). Cliquet :
│                     #   `tests/test_org_store_surface_frozen.py` (surface, signatures, DAG, 500 lignes).

└── config.py         # require_env

deploy/
├── oto-mcp.service       # systemd, User=root, /opt/oto-mcp, port 9103
├── oto-mcp-maintenance.{service,timer}   # les travaux sortis du boot (ADR 0065), prod seulement
├── oto-journal-archive.{service,timer}   # archivage du journal des appels
├── Caddyfile.snippet     # mcp.oto.ninja → 9103 (pas de bearer-gate, masquerait WWW-Authenticate)
└── *.sh, *.py            # oto-backend{,-canari}.sh (deploy), bluegreen/drain, start-encrypted, refresh SIRENE, ingestions

```

> ⚠️ **`deploy/DEPLOY.md` n'existe plus depuis le 2026-06-21** (sortie de l'infra sensible avant l'ouverture
> du repo) : la procédure DNS + Caddy + systemd vit dans le repo privé `otomata-tech/infra`. Cet arbre — et la
> carte `CLAUDE.md` — l'ont cité comme présent jusqu'au 2026-09-03.

> ⚠️ **L'extension Chrome (Oto Companion) n'a plus d'interlocuteur** — ce paragraphe a
> décrit son couplage comme vivant jusqu'au 2026-09-05. Elle vit dans
> `oto-websites/extension/` (et non `oto-app/extension/`, qui n'existe pas) et appelle
> encore `POST|DELETE /api/settings/linkedin`, `/api/whatsapp/pair/*` et
> `/api/whatsapp/status` : **aucune** de ces routes n'est servie (contrôlé sur
> `tests/api/api_routes_table.txt`). Son dernier commit date du 2026-06-25. **Décision
> d'Alexis du 05/09/2026 : elle est dépubliée du Chrome Web Store** — une extension
> installable qui ne peut plus rien faire coûte davantage que pas d'extension du tout.
> Cf. oto-backend#423.

**4 couches à frontière à sens unique** (ADR 0004) : **backend-core** (`db`,
`credentials_store`, `org_store`, `access`, `crypto`, `providers`, `auth.hooks`) —
**adaptateur MCP** — **adaptateur REST** — **runtime connecteurs**. Adaptateurs et runtime
dépendent du backend-core, **jamais l'inverse**, et l'appellent **par interface**
(`access.resolve_*`), pas par accès table croisé.

Une opération exposée sur **deux faces** s'écrit UNE fois : une **capacité**
(`oto_mcp/capabilities/`, ADR 0009) co-déclare handler core + `Input` pydantic (seule
validation) + règle `authz` **obligatoire** + bindings `mcp`/`rest`. Jamais un
`@mcp.tool()` main-écrit doublé d'une route REST (ADR 0042 §Convergence des surfaces,
garde-fou CI). La validation **REFUSE un champ inconnu** côté REST (400 `unknown_fields`) :
garde posée au SEAM, **sans allowlist**, tripwire `test_rest_rejects_unknown_fields.py`.
⚠️ L'autz reste **déclarée au niveau capacité**, jamais redescendue dans le handler.
⚠️ **Secret brut jamais en argument MCP** — la pose de secret est dashboard-only (binding
`mcp` retiré) ; le MCP ne porte que les droits/grants.
⚠️ **`capabilities/` est SOUS-RANGÉ par domaine depuis le 28/08** : `orgs/`,
`connectors/`, `datastore/`, `groups/`, et `docs/` depuis le 01/09 (oto-backend#734 — un
module de 870 lignes, découpé en `common`/`view`/`notify`/`reads`/`writes`/`patch`/
`history`/`changes`, le dispatcher et le descripteur restant dans `core`), le socle
commun (`registry`, `_authz`, `_types`, les deux adaptateurs) restant à la racine. Le
hub `capabilities/__init__.py` déclare chaque module en **import absolu** — il ne lie
aucun nom court, donc `orgs/core` et `groups/core` ne se disputent rien et l'ordre des
déclarations reste celui qu'on lit.
**Détail : `docs/couches-et-capacites.md`.**

La face REST **assemble** et n'implémente plus : depuis le 2026-08-27, `api/routes.py`
ne contient aucun handler (`make_routes` : 1 370 lignes et 52 handlers imbriqués → ~180
lignes de montage), les handlers vivent par domaine dans `api_routes_<domaine>.py`.
⚠️ La table est FIGÉE par `tests/api/test_api_routes_table_frozen.py` — retirer, ajouter ou
RÉORDONNER un chemin fait rouge (Starlette prend le premier match), et régénérer
`tests/api/api_routes_table.txt` EST la déclaration de l'ajout. **Détail : `docs/rest-api.md`.**
