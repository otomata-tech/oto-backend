"""Garde-fou SYMÉTRIQUE : une route REST de plateforme naît capacité, elle aussi.

`test_platform_tools_are_capabilities.py` (ADR 0042 §Convergence, Décision 4) ferme
un côté — un verbe de plateforme ne doit pas naître `@mcp.tool()` écrit à la main,
sinon la face REST devra être écrite une SECONDE fois, avec sa propre autz à tenir
en phase.

Il ne scanne que `oto_mcp/tools/` : une route **REST-only** passait donc à travers,
alors qu'elle crée la même dette en miroir — le jour où l'agent en a besoin, on
écrit un tool MCP à côté. Angle mort constaté le 2026-07-28 (`api/zoho.py`
ajouté à la main le jour même de la convergence, sans que rien ne le signale).

⚠️ **Ce garde-fou avait lui-même un angle mort, fermé le 2026-08-11 (#286).** Son
glob disait `api_routes_*.py` — qui ne matche PAS `api/routes.py`, le fichier qui
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

from oto_mcp import deprecations
from oto_mcp.api import routes as api_routes

NATURE, DEBT = "nature", "debt"

_KNOWN: dict[str, str] = {
    # --- Retours de consentement OAuth : le fournisseur redirige le NAVIGATEUR
    # (302, sans en-tête d'auth). Hors contrat capacité (JSON + autz).
    "/api/zoho/oauth/callback": NATURE,
    "/api/google/oauth/callback": NATURE,
    # (`/api/atlassian/oauth/callback` et `/api/folkmcp/oauth/callback` ont quitté
    # cette liste le 2026-09-09 : partis avec la fédération MCP, ADR 0069.)
    "/api/salesforce/oauth/callback": NATURE,
    "/api/instagram_meta/oauth/callback": NATURE,
    "/api/meta_ads/oauth/callback": NATURE,
    # --- Webhooks : un tiers appelle, non authentifié côté Logto.
    # (`/api/unipile/webhook` a quitté cette liste le 2026-08-29, #581 : dormant depuis
    #  la v2 du fournisseur — plus aucun appelant légitime, donc plus de route.)
    "/api/billing/webhook": NATURE,
    # Le webhook des AGENTS (12/09/2026) : un tiers — un CRM, un formulaire, un
    # n8n — POSTe pour démarrer un agent hébergé. Même nature que celui de Mollie,
    # et impossible en capacité pour une raison de fond : il accepte un corps JSON
    # LIBRE, que l'adaptateur refuserait champ par champ (`unknown_fields`). Son
    # authentification est un secret par déclencheur (`Authorization: Bearer
    # otoh_…`), pas un jeton Logto.
    "/api/hooks/{trigger_id}": NATURE,
    # Le RECEVEUR des téléphones Apollo (28/09/2026) : Apollo POSTe le résultat d'un
    # reveal qu'oto a commandé. Corps libre (la forme d'Apollo, pas la nôtre) et
    # aucun JWT : l'autorisation est le jeton du chemin, propre à une commande.
    "/api/receivers/apollo/phones/{token}": NATURE,
    # --- Lien de DÉSINSCRIPTION d'une relance : un NAVIGATEUR suit un lien depuis un
    # webmail, sans en-tête d'auth, et reçoit une page HTML. Même nature qu'un
    # callback — et exiger une session la demanderait à celui-là même qui ne veut plus
    # rien avoir à faire avec nous. Le jeton signé EST l'autorisation.
    "/o/u/{token}": NATURE,
    # --- Lien de DÉSINSCRIPTION du DIGEST de signaux (oto#150) : même nature que la
    # ligne au-dessus (navigateur, sans auth, jeton signé = l'autorisation), route et
    # `typ` de jeton distincts, table distincte (`signal_digest_optouts`).
    "/o/d/{token}": NATURE,
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
    # ⚠️ La MESSAGERIE HÉBERGÉE côté membre a quitté cette liste le 2026-08-27 :
    # `/api/me/unipile{,/connect,/reconcile}` sont des capacités
    # (`capabilities/unipile_me.py`). `api/connectors.py` n'a plus porté que le
    # webhook de liaison, puis a DISPARU avec lui le 2026-08-29 (#581).
    # (`/api/admin/unipile/seats` a quitté cette liste le 15/08 : inventaire ET
    #  libération sont des capacités — `capabilities/unipile_seats.py`.)
    # ⚠️ Le palier PLATEFORME des connecteurs a quitté cette liste le 2026-08-27 :
    # le cran d'activation et l'accès plateforme sont des capacités
    # (`capabilities/platform_connectors.py`). C'était l'étage qui manquait — les
    # paliers ORG et ÉQUIPE de la même famille étaient déjà des capacités
    # (`capabilities/connectors/activation.py`), ce qui rendait la dette d'autant plus
    # visible : un même métier décrit de deux façons selon l'étage.
    # ⚠️ Le 2026-08-12 (#302), le datastore a quitté cette liste EN ENTIER — onze
    # chemins, zéro reste : le tableau (`datastores`, `datastores/{ns}`, `…/url`),
    # les lignes (`…/rows`, `…/rows/{row_id}`, `…/rows/{row_id}/release`, `…/queue`,
    # `…/aggregate`), le schéma (`…/schema`) et le partage (`…/share`) sont des
    # capacités (`capabilities/datastore/*.py`). Mêmes chemins, mêmes réponses,
    # entrée ET sortie déclarées. Une dette qu'on rembourse, pas une nature qu'on
    # découvre. `…/rows/{row_id}/activity` et `…/claim*` étaient déjà des capacités.
    # ⚠️ Les VERBES OAuth ont quitté cette liste le 2026-08-27 — Google ici, les deux
    # fédérations MCP plus bas : `…/oauth/{start,status}`, `DELETE …/oauth` et
    # `POST /api/google/oauth/default` sont des capacités (`capabilities/federated_oauth.py`).
    # Seuls les CALLBACKS restent écrits à la main, et par NATURE : le fournisseur y
    # redirige le NAVIGATEUR (302, sans en-tête d'auth) alors que l'adaptateur authentifie
    # toujours et répond en JSON.
    # ⚠️ Les JETONS API ont quitté cette liste le 2026-08-27, palier membre ET palier
    # admin (`capabilities/api_tokens.py`), avec les clés plateforme. Ce qui les y
    # retenait était nommé quelques lignes plus bas depuis le début : le cran
    # `allow_api_token=False`, « un jeton ne fabrique pas de jeton », que
    # `_rest_adapter` ne savait pas exprimer. Il est désormais un champ du BINDING
    # (`RestBinding.allow_api_token`) — déclaré au même endroit que le chemin, et
    # vérifié en JOUANT les six routes (`tests/test_api_tokens_capability.py`), pas en
    # relisant le descripteur : c'est l'application du cran qui est la garde.
    # (La fédération MCP per-user — atlassian, folkmcp — a migré le 2026-08-27 avec les
    #  verbes Google ci-dessus : mêmes trois verbes, `capabilities/federated_oauth.py`.
    #  Leurs chemins NOMMENT leur connecteur, ce qui est une dette de FRONT désormais
    #  comptée dans `tests/test_connector_flow.py::_NOMMES_TOLERES`.)

    # ======================================================================
    # `api/routes.py` — LE FICHIER PRINCIPAL, hors radar jusqu'au 2026-08-11
    # ======================================================================
    # Le glob ne matchait que `api_routes_<x>.py` : ces 36 chemins n'ont jamais été
    # vus (#286). Ils sont classés ici pour la PREMIÈRE fois — c'est de l'ancien
    # qu'on cesse d'ignorer, pas du neuf qu'on accueille (cf. le plafond plus bas).
    #
    # --- NATURE — servies SANS AUTH, donc hors contrat capacité par CONSTRUCTION :
    # `_rest_adapter` authentifie TOUJOURS, un anonyme ne peut pas y passer.
    # Trois d'entre elles sont même consommées par un PROGRAMME, sans en-tête
    # d'auth : le build du site vitrine (`oto-websites/web/scripts/refresh-catalog.mjs`
    # → catalog/connectors) et celui de docs.oto.cx
    # (`sites/docs.oto.cx/scripts/refresh-openapi.mjs` → openapi.json). Les vitrines
    # anonymes des guides (`/api/guide-library`, `/api/guides/library`) sont
    # retirées (otomata-tech/oto#84).
    "/api/mcp/catalog": NATURE,
    # Descriptif de la surface REST : décrit des FORMES, aucune valeur. Servi aux
    # deux chemins usuels parce qu'un intégrateur sonde l'un ou l'autre.
    "/openapi.json": NATURE,
    "/api/openapi.json": NATURE,
    # La version SERVIE (oto#33) : un ref git, un SHA, deux horodatages — aucune
    # valeur, comme le descriptif juste au-dessus. NATURE et non DEBT : ses deux
    # appelants n'ont pas de jeton par construction — un contrôle externe (Uptime
    # Kuma, un script de déploiement) et un consommateur qui cherche à DATER une
    # dérive de comportement, donc avant d'avoir résolu quoi que ce soit
    # d'identité. L'adaptateur REST des capacités authentifiant TOUJOURS, une
    # surface anonyme ne peut pas y passer.
    "/api/version": NATURE,
    # ⚠️ Seule route MIXTE du lot : anonyme (vitrine) ET authentifiée (le dashboard
    # y scope son catalogue sur l'org active). Classée NATURE parce que sa moitié
    # anonyme est un contrat du build vitrine — la migrer supposerait de SCINDER la
    # route. Si elle bouge un jour, ce sera par une capacité AJOUTÉE à côté, jamais
    # par déplacement de ce chemin.
    "/api/connectors": NATURE,
    # Aperçu d'invitation AVANT création de compte : par construction, il n'y a pas
    # encore de `sub` à autoriser. Le jeton EST le secret.
    "/api/invitations/{token}": NATURE,
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
    # ⚠️ Le COMPTE a quitté cette liste le 2026-08-27 : `GET /api/me`,
    # `/api/me/calls` et `/api/me/activity-summary` sont des capacités
    # (`capabilities/me_account.py` — `me.{get,calls,activity_summary}`), et
    # `api_routes_account.py` a été SUPPRIMÉ, vidé de ses trois handlers. Mêmes
    # chemins, mêmes codes, même corps sur le fil ; entrée ET sortie déclarées, donc
    # `GET /api/me` — la première requête de tout front qui se branche — est enfin
    # décrite dans `/api/openapi.json`.
    "/api/me/avatar": NATURE,                            # POST multipart
    # ⚠️ Les cinq chemins de forme JSON de cette famille sont partis en capacités le
    # 2026-08-27 (`capabilities/media_and_files.py`) : `DELETE /api/me/avatar`,
    # `DELETE /api/orgs/{id}/logo`, la LISTE des fichiers d'un projet, la SUPPRESSION
    # d'un fichier et la bascule de son partage. Ce qui reste ci-dessous n'est plus de
    # la dette : c'est de la NATURE, et pour une raison qui ne bougera pas.
    # --- NATURE — HORS DU MOULE PAR CONSTRUCTION, reclassées le 2026-08-27.
    #
    # `_rest_adapter` lit un corps JSON et répond en JSON. Ces quatre-là ne peuvent donc
    # pas être des capacités sans déformer l'adaptateur pour elles seules :
    #   - trois `POST` **multipart** (avatar, logo d'org, dépôt d'un fichier de projet) :
    #     le corps est BINAIRE, il n'y a pas de modèle pydantic à en dériver ;
    #   - un `GET` qui rend un **ZIP** (`application/zip` + `Content-Disposition`), pas
    #     du JSON.
    # Même raison que `/api/upload/{token}`, classé NATURE depuis toujours. Leurs
    # jumelles de forme JSON, elles, SONT des capacités — c'est bien la FORME qui
    # tranche, pas le domaine.
    #
    # ⚠️ Trois de ces chemins sont donc MIXTES : leur `POST` (ou leur `GET`) est écrit à
    # la main, leur autre verbe est généré. C'est sans effet sur le routage — Starlette
    # rend un `Match.PARTIAL` sur méthode non trouvée et poursuit son balayage.
    "/api/me/projects/{project_id:int}/files": NATURE,   # POST multipart
    "/api/me/projects/{project_id:int}/transcriptions/upload": NATURE,   # POST multipart (audio à transcrire)
    "/api/me/projects/{id}/export": NATURE,              # réponse application/zip
    # Le PDF d'une facture (#488). Même raison que l'export ci-dessus, et elle est
    # STRUCTURELLE, pas de la dette : un handler de capacité rend un `dict` que
    # l'adaptateur emballe en `JSONResponse` — il ne peut pas servir des octets.
    # La LISTE des factures, elle, est bien une capacité
    # (`me.billing.invoices.list`). Montée en toutes circonstances, gate ou pas :
    # une route qui apparaît selon l'environnement ferait mentir ce cliquet sur une
    # machine et pas sur l'autre. Le dark launch vit dans le handler (404).
    "/api/me/billing/invoices/{id}/pdf": NATURE,         # réponse application/pdf
    # L'export CSV d'un tableau entier (`api/datastore_export.py`) — même raison
    # STRUCTURELLE que les deux au-dessus : `text/csv`, pas du JSON. STREAMÉ
    # plutôt que matérialisé (un tableau peut porter des dizaines de milliers de
    # lignes, à la différence d'une facture ou d'un export projet), via
    # `base._file_stream` — même garde CORS que `_file`, cf.
    # `tests/api/test_reponses_binaires_cors.py`.
    "/api/datastores/{datastore}/rows/export.csv": NATURE,  # réponse text/csv, streamée
    "/api/orgs/{id}/logo": NATURE,                       # POST multipart
    # ⚠️ La TOOLBOX DU MEMBRE a quitté cette liste le 2026-08-27 : les six routes
    # `/api/me/tools*` sont des capacités (`capabilities/tools_me.py` —
    # `me.tools.{list,registry,disable,enable,detail,call}`), et `api_routes_tools.py`
    # a été SUPPRIMÉ. Migration EN BLOC, par contrainte de ROUTAGE : `{name}` capture un
    # segment, donc `…/tools/registry` doit précéder `…/tools/{name}` — or les routes de
    # capacité sont montées à la FIN de `make_routes`, migrer l'une sans l'autre aurait
    # fait servir `registry` comme un nom d'outil.
    #     Le miroir MCP écrit à la main est remboursé depuis #429 : `oto_disable_tool`
    # et `oto_enable_tool` sont la face MCP des mêmes capacités, `oto_list_my_tools`
    # une capacité MCP-only (`me.tools.search`) sur le noyau `tools/catalogue.py`.
    # ⚠️ La CONNEXION PAR SESSION NAVIGATEUR a quitté cette liste le 2026-08-27 :
    # `…/session/{start,finalize}` sont des capacités (`capabilities/browser_sessions.py`),
    # et `api_routes_credentials.py` a été SUPPRIMÉ. La pose d'un secret reste
    # dashboard-only par DESIGN (jamais un argument MCP, il transiterait dans le contexte
    # LLM) — mais une capacité peut être REST-only (`mcp=None`), c'était donc bien de la
    # dette et pas une nature. Le pendant AGENT du même geste existe et c'est
    # `me.connector_connect` (`POST /api/me/connectors/{name}/connect`).
    # --- NATURE — ALIAS DÉPRÉCIÉS, retrait le 29/10/2026 (#519, retrait suivi en
    # #526). Ces chemins ne portent AUCUN métier : ils répondent 308 vers le chemin
    # d'aujourd'hui, et s'en vont à une date écrite (`oto_mcp/deprecations.REST`).
    # Ce n'est donc pas de la dette — il n'y a rien à migrer, il y a une date à
    # tenir. Ils quittent cette liste au lot D, avec le module qui les déclare.
    # Montés EN DERNIER dans `make_routes` : un alias ne capture que ce que rien
    # d'autre ne sert.
    "/api/me/doctrines/library": NATURE,
    "/api/me/doctrines/library/{slug}": NATURE,
    "/api/me/doctrines/library/{id}": NATURE,
    "/api/me/doctrines/publish": NATURE,
    "/api/me/doctrines/fork": NATURE,
    "/api/me/doctrines/{doctrine_id}": NATURE,
    #
    # (Le palier admin — clés plateforme et jetons émis pour un tiers — a migré le
    #  2026-08-27 avec le palier membre ci-dessus. `api_routes_admin.py` a été SUPPRIMÉ.
    #  Le commentaire qui vivait ici disait « un cran que `_rest_adapter` ne sait pas
    #  ENCORE exprimer : c'est un travail de migration, pas une nature » — c'est
    #  exactement ce qui a été fait.)
}

# ⚠️ Les alias du renommage `namespace` → `datastore` (08/09/2026) sont AJOUTÉS ICI PAR
# DÉRIVATION, pas recopiés. Ils sont eux-mêmes dérivés des routes montées
# (`deprecations._alias_datastore`) : les lister à la main dans cette garde rétablirait
# exactement la copie que la dérivation supprime — et une garde qui porte une copie
# périmée refuse une route légitime ou en laisse passer une qui ne l'est pas.
_KNOWN.update({a.ancien: NATURE for a in deprecations._alias_datastore()})


class _FauxVerifieur:
    """`make_routes` n'a besoin que d'un objet à passer : rien n'est vérifié ici."""

    def verify_token(self, token):  # pragma: no cover — jamais appelé
        return None


def _handwritten_routes() -> dict[str, str]:
    """`{chemin: module}` des routes que le serveur monte À LA MAIN.

    ⚠️ **Lu dans la TABLE SERVIE, pas dans un motif de nom de fichier (2026-08-28).**
    Ce relevé se faisait par glob (`api_routes*.py`) + scan AST des `Route("…")`. Ce
    glob a déjà eu un angle mort — `api_routes_*.py` excluait `api/routes.py`, le
    fichier qui portait le plus de chemins, et le garde-fou a promis de couvrir 81
    chemins en n'en voyant que 45 (#286). Un motif de NOM ne survit ni à un
    renommage ni à un rangement : c'est le rangement par domaine (`api_routes*.py` →
    `api/`) qui l'a re-cassé.

    Le critère est désormais une propriété du RÉSULTAT, et elle dit exactement ce
    qu'on veut dire : **une route est écrite à la main quand son endpoint est défini
    dans `oto_mcp/api/`** — les routes dérivées d'une capacité, elles, sortent toutes
    de `capabilities/_rest_adapter`. Le préflight partagé (`options_handler`, monté
    par `bind` pour chaque chemin) n'est pas une route déclarée : il est écarté.

    Équivalence VÉRIFIÉE au moment de la bascule : les deux mécanismes rendaient le
    même ensemble de 36 chemins, à l'élément près.
    """
    out: dict[str, str] = {}
    for route in api_routes.make_routes(_FauxVerifieur(), mcp_instance=None):
        module = getattr(route.endpoint, "__module__", "")
        if not module.startswith("oto_mcp.api"):
            continue                       # dérivée d'une capacité : pas écrite ici
        if getattr(route.endpoint, "__name__", "") == "options_handler":
            continue                       # préflight partagé, monté par `bind`
        out[route.path] = module
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


def test_rest_debt_stays_at_zero():
    """✅ **La dette REST vaut ZÉRO depuis le 2026-08-27, et ce test la maintient là.**

    Il a longtemps mesuré un plafond qui ne devait que baisser (« no silent caps » : un
    plafond tu est un plafond oublié). Il n'y a plus rien à mesurer : les 38 routes
    écrites à la main qui restaient ont été portées en capacités en huit lots, et les
    chemins encore montés à la main sont TOUS de nature, chacun avec sa raison écrite
    dans `_KNOWN` — callback de redirection, webhook, surface anonyme, API consommée par
    un programme externe, corps multipart, réponse non-JSON.

    **Le garde-fou survit au chantier, il change juste de rôle** : de compteur, il
    devient CLIQUET. Une route neuve écrite à la main a désormais deux issues, et deux
    seulement — naître capacité, ou être classée `NATURE` ici **avec sa raison**. La
    reclasser `DEBT` rouvrirait une dette qu'on vient de fermer : c'est un acte, pas un
    réglage, et il doit se voir en revue.

    (Le troisième garde-fou de la famille, `test_no_new_handwritten_rest_route`, refuse
    déjà toute route absente de `_KNOWN` : ensemble, les deux rendent impossible
    d'ajouter une route à la main sans le déclarer.)
    """
    debt = sorted(p for p, kind in _KNOWN.items() if kind == DEBT)
    assert not debt, (
        f"la dette REST est rouverte ({len(debt)} route(s)) : {debt}. Elle valait ZÉRO ; "
        "une route écrite à la main naît CAPACITÉ (`oto_mcp/capabilities/`), ou se "
        "classe `NATURE` avec sa raison. Il n'y a plus de troisième voie.")

def test_zoho_start_and_modes_are_capabilities_not_routes():
    """Régression de la migration du jour : ces deux verbes ont quitté le REST
    écrit à la main pour `capabilities/zoho_connect.py` (le callback, lui, reste)."""
    routes = _handwritten_routes()
    assert "/api/zoho/oauth/start" not in routes
    assert "/api/zoho/oauth/modes" not in routes
    assert "/api/zoho/oauth/callback" in routes
