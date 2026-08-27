"""Garde-fou SYMÉTRIQUE : une route REST de plateforme naît capacité, elle aussi.

`test_platform_tools_are_capabilities.py` (ADR 0042 §Convergence, Décision 4) ferme
un côté — un verbe de plateforme ne doit pas naître `@mcp.tool()` écrit à la main,
sinon la face REST devra être écrite une SECONDE fois, avec sa propre autz à tenir
en phase.

Il ne scanne que `oto_mcp/tools/` : une route **REST-only** passait donc à travers,
alors qu'elle crée la même dette en miroir — le jour où l'agent en a besoin, on
écrit un tool MCP à côté. Angle mort constaté le 2026-07-28 (`api_routes_zoho.py`
ajouté à la main le jour même de la convergence, sans que rien ne le signale).

⚠️ **Ce garde-fou avait lui-même un angle mort, fermé le 2026-08-11 (#286).** Son
glob disait `api_routes_*.py` — qui ne matche PAS `api_routes.py`, le fichier qui
porte le plus de routes. Trente-six chemins y vivaient invisibles pendant que le
docstring promettait « la CI casse ». Un garde-fou qui couvre 45 chemins sur 81 en
annonçant qu'il les couvre tous est pire qu'absent : on cesse de regarder. Le glob
dit désormais `api_routes*.py`, et les 36 chemins découverts sont classés ci-dessous.

**Grain = la ROUTE, pas le module.** Première version classée par module : un seul
webhook « par nature » y blanchissait les 17 autres routes du même fichier. La
plupart des modules sont mixtes (un callback OAuth + dix verbes de dashboard), donc
seule la route est classifiable.

Trois natures :
- `NATURE` — un tiers appelle, hors contrat capacité : **callback** de redirection
  (302, sans auth), **webhook**, ou **API consommée par un programme externe**
  (oto-core/oto-cli), dont le chemin est un contrat gelé.
- `DEBT` — verbe de dashboard/agent écrit à la main : à migrer en capacité.
- absente de la liste — nouvelle route : la CI casse (réflexe = déclarer une capacité).

La liste DEBT doit décroître, jamais s'étendre.
"""
from __future__ import annotations

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent / "oto_mcp"

NATURE, DEBT = "nature", "debt"

_KNOWN: dict[str, str] = {
    # --- Retours de consentement OAuth : le fournisseur redirige le NAVIGATEUR
    # (302, sans en-tête d'auth). Hors contrat capacité (JSON + autz).
    "/api/zoho/oauth/callback": NATURE,
    "/api/google/oauth/callback": NATURE,
    "/api/folkmcp/oauth/callback": NATURE,
    "/api/atlassian/oauth/callback": NATURE,
    "/api/salesforce/oauth/callback": NATURE,
    # --- Webhooks : un tiers appelle, non authentifié côté Logto.
    "/api/unipile/webhook": NATURE,
    "/api/billing/webhook": NATURE,
    # --- Formulaire public du site vitrine (POST anonyme).
    "/api/contact": NATURE,
    # --- APIs consommées par un PROGRAMME externe (oto-core / oto-cli), chemins
    # gelés par contrat : `SireneStock` HTTP client, repli CLI des accords quand le
    # transport MCP est indisponible. Un tool MCP existe en parallèle, mais c'est un
    # CONNECTEUR (`fr_*`), pas un verbe de plateforme — pas la dette visée ici.
    "/api/sirene/headquarters": NATURE,
    "/api/sirene/siege": NATURE,
    "/api/sirene/etablissements": NATURE,
    "/api/sirene/siret": NATURE,
    "/api/sirene/search": NATURE,
    "/api/sirene/info": NATURE,
    "/api/fr/accords/search": NATURE,
    "/api/fr/accords/themes": NATURE,
    "/api/fr/accords/{id_or_numero}": NATURE,

    # --- DETTE : verbes de dashboard écrits à la main, à migrer en capacités.
    # Connexion hébergée + gouvernance connecteur.
    "/api/me/unipile": DEBT,
    "/api/me/unipile/connect": DEBT,
    "/api/me/unipile/reconcile": DEBT,
    # (`/api/admin/unipile/seats` a quitté cette liste le 15/08 : inventaire ET
    #  libération sont des capacités — `capabilities/unipile_seats.py`.)
    "/api/admin/connectors/activation": DEBT,
    "/api/admin/connectors/{provider}/platform-access": DEBT,
    # ⚠️ Le 2026-08-12 (#302), le datastore a quitté cette liste EN ENTIER — onze
    # chemins, zéro reste : le tableau (`namespaces`, `namespaces/{ns}`, `…/url`),
    # les lignes (`…/rows`, `…/rows/{row_id}`, `…/rows/{row_id}/release`, `…/queue`,
    # `…/aggregate`), le schéma (`…/schema`) et le partage (`…/share`) sont des
    # capacités (`capabilities/datastore_*.py`). Mêmes chemins, mêmes réponses,
    # entrée ET sortie déclarées. Une dette qu'on rembourse, pas une nature qu'on
    # découvre. `…/rows/{row_id}/activity` et `…/claim*` étaient déjà des capacités.
    # OAuth Google : les VERBES (le callback ci-dessus est, lui, par nature).
    "/api/google/oauth": DEBT,
    "/api/google/oauth/start": DEBT,
    "/api/google/oauth/status": DEBT,
    "/api/google/oauth/default": DEBT,
    # Jetons CLI/API de l'utilisateur.
    "/api/me/tokens": DEBT,
    "/api/me/tokens/{token_id}": DEBT,
    # Fédération MCP per-user (mêmes verbes répétés par connecteur fédéré).
    "/api/atlassian/oauth/start": DEBT,
    "/api/atlassian/oauth/status": DEBT,
    "/api/atlassian/oauth": DEBT,
    "/api/folkmcp/oauth/start": DEBT,
    "/api/folkmcp/oauth/status": DEBT,
    "/api/folkmcp/oauth": DEBT,

    # ======================================================================
    # `api_routes.py` — LE FICHIER PRINCIPAL, hors radar jusqu'au 2026-08-11
    # ======================================================================
    # Le glob ne matchait que `api_routes_<x>.py` : ces 36 chemins n'ont jamais été
    # vus (#286). Ils sont classés ici pour la PREMIÈRE fois — c'est de l'ancien
    # qu'on cesse d'ignorer, pas du neuf qu'on accueille (cf. le plafond plus bas).
    #
    # --- NATURE — servies SANS AUTH, donc hors contrat capacité par CONSTRUCTION :
    # `_rest_adapter` authentifie TOUJOURS, un anonyme ne peut pas y passer.
    # L'argument est déjà écrit dans le code (`doctrines_library_public` : « route
    # écrite à la main car l'adaptateur REST des capacités authentifie toujours »).
    # Quatre d'entre elles sont même consommées par un PROGRAMME, sans en-tête
    # d'auth : le build du site vitrine (`oto-websites/web/scripts/refresh-catalog.mjs`
    # → catalog/connectors/doctrines/guides) et celui de docs.oto.cx
    # (`sites/docs.oto.cx/scripts/refresh-openapi.mjs` → openapi.json).
    "/api/mcp/catalog": NATURE,
    "/api/doctrines/library": NATURE,
    "/api/doctrines/library/{slug}": NATURE,
    "/api/guides/library": NATURE,
    "/api/guides/library/{slug}": NATURE,
    # Descriptif de la surface REST : décrit des FORMES, aucune valeur. Servi aux
    # deux chemins usuels parce qu'un intégrateur sonde l'un ou l'autre.
    "/openapi.json": NATURE,
    "/api/openapi.json": NATURE,
    # ⚠️ Seule route MIXTE du lot : anonyme (vitrine) ET authentifiée (le dashboard
    # y scope son catalogue sur l'org active). Classée NATURE parce que sa moitié
    # anonyme est un contrat du build vitrine — la migrer supposerait de SCINDER la
    # route. Si elle bouge un jour, ce sera par une capacité AJOUTÉE à côté, jamais
    # par déplacement de ce chemin.
    "/api/connectors": NATURE,
    # Aperçu d'invitation AVANT création de compte : par construction, il n'y a pas
    # encore de `sub` à autoriser. Le jeton (ou le code) EST le secret.
    "/api/invitations/{token}": NATURE,
    "/api/invitations/code/{code}": NATURE,
    # Partage public d'un doc par token — lecture seule, le token EST le secret.
    # `/p/d/…` rend du HTML server-rendered (lisible par un agent sans JS), pas du
    # JSON : ce n'est même pas la forme d'une capacité.
    "/api/public/docs/{token}": NATURE,
    "/p/d/{token}": NATURE,
    # Réception d'un upload signé (#105) : PAS de JWT, le jeton scellé de l'URL fait
    # foi (sub/org/cible, TTL, usage unique). Appelée par un `curl` d'agent (PUT) ou
    # le formulaire humain (POST/GET) — un tiers, hors session dashboard.
    "/api/upload/{token}": NATURE,
    # Icône de marque servie au NAVIGATEUR (l'endpoint MCP n'a pas de page racine) :
    # du SVG, pas du JSON, pas d'autz à tenir. Ce n'est pas une opération d'API.
    "/favicon.svg": NATURE,
    "/favicon.ico": NATURE,
    #
    # --- DETTE — verbes de dashboard authentifiés, à migrer en capacités.
    # ⚠️ Le COMPTE a quitté cette liste le 2026-08-27 : `GET /api/me`,
    # `/api/me/calls` et `/api/me/activity-summary` sont des capacités
    # (`capabilities/me_account.py` — `me.{get,calls,activity_summary}`), et
    # `api_routes_account.py` a été SUPPRIMÉ, vidé de ses trois handlers. Mêmes
    # chemins, mêmes codes, même corps sur le fil ; entrée ET sortie déclarées, donc
    # `GET /api/me` — la première requête de tout front qui se branche — est enfin
    # décrite dans `/api/openapi.json`.
    "/api/me/avatar": DEBT,
    # Fichiers bruts d'un projet + export — le reste du domaine projet est déjà en
    # capacités (`/api/me/projects` sert tout le métier en `op=`), ces quatre-là non.
    "/api/me/projects/{project_id:int}/files": DEBT,
    "/api/me/projects/{project_id:int}/files/{file_id:int}": DEBT,
    "/api/me/projects/{project_id:int}/files/{file_id:int}/public": DEBT,
    "/api/me/projects/{id}/export": DEBT,
    "/api/orgs/{id}/logo": DEBT,
    # ⚠️ La TOOLBOX DU MEMBRE a quitté cette liste le 2026-08-27 : les six routes
    # `/api/me/tools*` sont des capacités (`capabilities/tools_me.py` —
    # `me.tools.{list,registry,disable,enable,detail,call}`), et `api_routes_tools.py`
    # a été SUPPRIMÉ. Migration EN BLOC, par contrainte de ROUTAGE : `{name}` capture un
    # segment, donc `…/tools/registry` doit précéder `…/tools/{name}` — or les routes de
    # capacité sont montées à la FIN de `make_routes`, migrer l'une sans l'autre aurait
    # fait servir `registry` comme un nom d'outil.
    #     Le miroir MCP (`oto_list_my_tools`/`oto_enable_tool`/`oto_disable_tool`, nommé
    # DETTE dans `test_platform_tools_are_capabilities.py`) n'est PAS remboursé ici : les
    # deux faces n'ont pas la même forme, les unifier casserait l'une des deux. Décision
    # de contrat, suivie en oto-backend#429.
    # ⚠️ La CONNEXION PAR SESSION NAVIGATEUR a quitté cette liste le 2026-08-27 :
    # `…/session/{start,finalize}` sont des capacités (`capabilities/browser_sessions.py`),
    # et `api_routes_credentials.py` a été SUPPRIMÉ. La pose d'un secret reste
    # dashboard-only par DESIGN (jamais un argument MCP, il transiterait dans le contexte
    # LLM) — mais une capacité peut être REST-only (`mcp=None`), c'était donc bien de la
    # dette et pas une nature. Le pendant AGENT du même geste existe et c'est
    # `me.connector_connect` (`POST /api/me/connectors/{name}/connect`).
    # Palier admin. ⚠️ Les deux routes `tokens` portent `allow_api_token=False` (un
    # jeton ne fabrique pas de jeton) — un cran que `_rest_adapter` ne sait pas
    # encore exprimer : c'est un travail de migration, pas une nature. Leurs miroirs
    # membres (`/api/me/tokens*`) sont déjà classés en dette pour la même raison.
    "/api/admin/platform-keys": DEBT,
    "/api/admin/platform-keys/{provider}/{label}": DEBT,
    "/api/admin/users/{sub}/tokens": DEBT,
    "/api/admin/users/{sub}/tokens/{token_id}": DEBT,
}


def _handwritten_routes() -> dict[str, str]:
    """`{chemin: module}` de toute `Route("…")` déclarée dans un `api_routes*.py`.

    ⚠️ Le glob n'a PAS d'underscore avant l'étoile, et c'est le tout du correctif
    #286 : `api_routes_*.py` excluait `api_routes.py`, le fichier principal.
    """
    out: dict[str, str] = {}
    for path in sorted(ROOT.glob("api_routes*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "Route" and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                out[node.args[0].value] = path.name
    return out


def test_no_new_handwritten_rest_route():
    found = _handwritten_routes()
    unexpected = sorted(p for p in found if p not in _KNOWN)
    assert not unexpected, (
        f"Routes REST écrites à la main hors liste connue : {unexpected}. "
        "Déclare le verbe comme une CAPACITÉ (`oto_mcp/capabilities/`) : les "
        "adaptateurs en dérivent les faces MCP et REST depuis un descripteur "
        "unique, avec UNE autz — cf. ADR 0042 §Convergence des surfaces. "
        "Exception admise (callback de redirection, webhook, API consommée par un "
        "programme externe) : à déclarer ici en `NATURE`, avec sa raison.")
    gone = sorted(p for p in _KNOWN if p not in found)
    assert not gone, (
        f"Ces routes n'existent plus : {gone}. Retire-les de `_KNOWN` — la liste "
        "doit refléter le réel, jamais mentir.")


def test_rest_debt_only_shrinks():
    """La dette est NOMMÉE et COMPTÉE (« no silent caps » : un plafond tu est un
    plafond oublié). Ce plafond ne doit que baisser, au fil des migrations.

    ⚠️ **Il est passé de 37 à 49 le 2026-08-11 (#286), et c'est le SEUL cas où
    l'élargir est légitime** : on ne l'a pas relevé pour accueillir du NEUF, mais
    pour cesser d'ignorer de l'ANCIEN. Les 21 routes ajoutées vivaient déjà dans
    `api_routes.py`, hors de portée du glob depuis toujours ; les compter ne crée
    pas une dette, elle la RÉVÈLE. Toute autre hausse est un relâchement.
    """
    debt = sorted(p for p, kind in _KNOWN.items() if kind == DEBT)
    assert len(debt) <= 27, (
        f"la dette REST a grossi ({len(debt)} routes) : {debt}. Elle doit "
        "DÉCROÎTRE — migre en capacité plutôt que d'élargir le plafond.")


def test_zoho_start_and_modes_are_capabilities_not_routes():
    """Régression de la migration du jour : ces deux verbes ont quitté le REST
    écrit à la main pour `capabilities/zoho_connect.py` (le callback, lui, reste)."""
    routes = _handwritten_routes()
    assert "/api/zoho/oauth/start" not in routes
    assert "/api/zoho/oauth/modes" not in routes
    assert "/api/zoho/oauth/callback" in routes
