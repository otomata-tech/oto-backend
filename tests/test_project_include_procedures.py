"""`include=procedures` — lire la RÈGLE qui a produit une fiche (#313).

Besoin partenaire : un jeton PORTÉ, borné à un projet, doit pouvoir lire le corps des
procédures que ce projet lie. La route `GET /api/me/projects/{id}` nomme déjà sa cible
dans le chemin et est déjà ouverte aux jetons portés — l'inclusion s'y greffe, plutôt
que d'ouvrir la famille `/api/me/instructions/*` entière pour un besoin qui tient à un
projet.

Quatre choses gardées ici, dont une non négociable :

1. **aucune métadonnée d'exécution** — la contrainte produit : une procédure servie ne
   dit rien des modèles. Figée par une ALLOWLIST, pas par une liste de mots interdits ;
2. **additif à l'octet près** — sans le paramètre, la réponse ne bouge pas ;
3. **le droit se vérifie procédure par procédure** — un projet peut lier une procédure
   d'une autre org (partage cross-org) : lire le projet n'emporte pas son corps ;
4. **une procédure absente ne casse rien** — lien mort ou accès refusé : le projet
   reste lisible, la procédure manque simplement à l'appel.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import projects as cap

# `_view` lit les partages vivants du projet (#1367) : aucun ici, sans base.
pytestmark = pytest.mark.usefixtures("projets_sans_partages")


class _Ctx:
    def __init__(self, sub="sub-lecteur", org_id=1):
        self.sub, self.org_id, self.group_id = sub, org_id, None


PROCEDURE = {
    "id": 5, "org_id": 1, "owner_type": "org", "owner_id": "1",
    "slug": "qualification", "title": "Qualification d'un prospect",
    "description": "Comment qualifier", "body_md": "1. Vérifier le SIREN\n2. Noter",
    "slots": [{"name": "cible", "type": "tableau"}], "version": 3,
    "set_by": "sub-auteur", "created_at": "2026-08-01", "updated_at": "2026-08-10",
}

LIENS = [
    {"target_type": "procedure", "target_ref": "5", "title": "Qualification d'un prospect"},
    {"target_type": "tableau", "target_ref": "9", "namespace": "prospects"},
]


@pytest.fixture
def store(monkeypatch):
    """Le store et le seam d'accès, stubés — ce test porte sur la PROJECTION, pas
    sur le SQL (qui est exercé par le job `test` avec la vraie base)."""
    monkeypatch.setattr(cap.org_store, "get_instruction_by_id",
                        lambda i: PROCEDURE if int(i) == 5 else None)
    monkeypatch.setattr(cap.ownership, "can_access", lambda *a, **k: True)


# ── la contrainte non négociable ─────────────────────────────────────────────

def test_procedure_payload_is_an_allowlist(store):
    """⚠️ **La garde qui compte.** La contrainte produit est « aucune métadonnée
    d'exécution » ; une denylist de mots-clés la tiendrait le jour de son écriture et
    la perdrait au premier champ ajouté à `org_instructions`. On fige donc l'ensemble
    EXACT des clés servies : toute colonne future est absente par construction, et
    l'ajouter au rendu demandera de changer ce test — c'est-à-dire de le décider.

    `description` y est entrée le 07/09/2026 (oto#118), et ce test est l'endroit où
    cette décision a été prise : c'est la prose de l'auteur, pas une métadonnée
    d'exécution. Le stub porte exprès ce qui ne sort toujours PAS (`set_by`, `slots`,
    les horodatages) : ces champs existent en base et n'ont aucun chemin vers le
    client."""
    got = cap._linked_procedures("sub-lecteur", LIENS)

    assert len(got) == 1
    assert set(got[0]) == set(cap._PROCEDURE_FIELDS)
    assert set(got[0]) == {"ref", "slug", "title", "description", "version", "body_md"}
    assert got[0]["body_md"] == "1. Vérifier le SIREN\n2. Noter"
    assert got[0]["version"] == 3


def test_execution_metadata_never_leaks_even_if_the_row_grows(store, monkeypatch):
    """Le corollaire, joué sur le cas réel : demain quelqu'un ajoute une colonne
    d'exécution à `org_instructions`. Elle ne doit atteindre aucun client, sans que
    personne n'ait à y penser."""
    monkeypatch.setattr(cap.org_store, "get_instruction_by_id",
                        lambda i: {**PROCEDURE, "executed_by_model": "un-modèle",
                                   "candidate_models": ["a", "b"], "run_cost_eur": 0.42})

    got = cap._linked_procedures("sub-lecteur", LIENS)

    assert set(got[0]) == set(cap._PROCEDURE_FIELDS)
    rendu = repr(got)
    for interdit in ("model", "executed_by", "candidate", "cost"):
        assert interdit not in rendu, f"« {interdit} » a fuité : {rendu}"


# ── oto#118 : le résumé de l'auteur, à côté du corps ─────────────────────────

def test_le_resume_de_lauteur_est_servi(store):
    """Le défaut, tel que vécu : l'allowlist s'arrêtait à `body_md`, donc une
    application tierce affichait à sa lectrice un corps de quinze mille caractères
    faute de pouvoir servir un résumé de deux lignes. `description` existe en base,
    est saisie par l'auteur et se corrige sans toucher au corps — elle sort."""
    [got] = cap._linked_procedures("sub-lecteur", LIENS)

    assert got["description"] == "Comment qualifier"
    assert got["body_md"] == "1. Vérifier le SIREN\n2. Noter"


def test_sans_resume_le_champ_est_vide_jamais_null(store, monkeypatch):
    """Une procédure sans résumé n'est pas une anomalie : la colonne est NULL en base
    et le champ servi vaut `""`. Un client qui concatène ou mesure la longueur n'a
    donc pas à distinguer « pas de résumé » de « champ absent »."""
    monkeypatch.setattr(cap.org_store, "get_instruction_by_id",
                        lambda i: {**PROCEDURE, "description": None})

    [got] = cap._linked_procedures("sub-lecteur", LIENS)

    assert got["description"] == ""


# ── le droit, procédure par procédure ────────────────────────────────────────

def test_an_unreadable_procedure_is_absent_not_an_error(store, monkeypatch):
    """Un projet peut lier une procédure d'une AUTRE org (partage cross-org, #52) :
    lire le projet n'emporte pas le droit de lire tout ce qu'il désigne. La procédure
    interdite est ABSENTE — elle ne fait pas échouer la lecture du projet, et reste
    visible comme lien (titre, ref), seul son corps ne suit pas."""
    monkeypatch.setattr(cap.ownership, "can_access", lambda *a, **k: False)

    assert cap._linked_procedures("sub-lecteur", LIENS) == []


def test_ownership_is_consulted_for_the_guide_not_the_project(store, monkeypatch):
    """Le seam est interrogé sur la PROCÉDURE (`doctrine`, son id), pas sur le projet —
    sinon le partage cross-org d'une procédure serait décidé par l'accès au projet qui
    la lie, ce qui n'est pas la même question."""
    vus = []
    monkeypatch.setattr(cap.ownership, "can_access",
                        lambda sub, rtype, rid, perm: vus.append((rtype, rid, perm)) or True)

    cap._linked_procedures("sub-lecteur", LIENS)

    assert vus == [("doctrine", "5", "read")]


def test_a_dead_link_does_not_break_the_read(store, monkeypatch):
    """Procédure supprimée : `audit.dead_links` est l'endroit qui le SIGNALE. Ce rendu
    ne le redit pas et surtout ne fait pas tomber la lecture du projet."""
    monkeypatch.setattr(cap.org_store, "get_instruction_by_id", lambda i: None)

    assert cap._linked_procedures("sub-lecteur", LIENS) == []


def test_a_slug_reference_is_skipped_not_resolved(store):
    """L'ADR 0032 a fixé l'id comme référence stable. Un `target_ref` non numérique est
    un résidu : on le saute plutôt que d'ouvrir un second chemin de résolution."""
    assert cap._linked_procedures(
        "sub-lecteur", [{"target_type": "procedure", "target_ref": "qualification"}]) == []


def test_order_follows_the_links(store, monkeypatch):
    """L'ordre des procédures est celui des liens du projet — c'est l'ordre que
    l'utilisateur a curé, et le lecteur le retrouve tel quel."""
    monkeypatch.setattr(cap.org_store, "get_instruction_by_id",
                        lambda i: {**PROCEDURE, "id": int(i), "slug": f"p{i}"})
    liens = [{"target_type": "procedure", "target_ref": r} for r in ("7", "5", "9")]

    assert [p["ref"] for p in cap._linked_procedures("sub", liens)] == ["7", "5", "9"]


# ── additif : sans le paramètre, rien ne bouge ───────────────────────────────

def test_without_include_the_response_is_unchanged(monkeypatch):
    """Contrat publié depuis #302 : `procedures` est un champ OPTIONNEL. Sans
    `include`, la clé n'apparaît pas — pas même à `null`, qui serait déjà un
    changement de forme pour un client qui énumère les clés."""
    monkeypatch.setattr(cap.ownership, "can_access", lambda *a, **k: True)
    appels = []
    monkeypatch.setattr(cap, "_linked_procedures",
                        lambda *a, **k: appels.append(1) or [])

    modele = cap.ProjectRead.model_fields
    assert "procedures" in modele and not modele["procedures"].is_required()
    assert cap.ProjectRead.model_fields["spine"].is_required() is False
    assert appels == []


# ── la portée du jeton n'est pas contournée par le paramètre ─────────────────

def test_the_include_does_not_widen_a_scoped_token():
    """⚠️ Le point de sécurité du lot, exigé par le brief : un jeton borné au projet
    12 lit ses procédures, un jeton borné au projet 13 ne lit pas celles du 12.

    Ça pourrait sembler acquis — c'est la route qui est bornée, pas le paramètre —
    mais l'acquis tient à un détail : `token_scopes.authorize` matche le CHEMIN, et
    son motif est ancré (`$`). Si la query string atteignait un jour ce match, le
    motif ne matcherait plus, la boucle tomberait au `return False` final… ou pire,
    un motif relâché laisserait passer. On garde donc le comportement plutôt que la
    lecture du code."""
    from oto_mcp.auth import token_scopes

    porte_12 = {"projects": {"12": "read"}}

    # Le chemin servi par le routeur — le paramètre vit dans la query, pas ici.
    assert token_scopes.authorize(porte_12, "GET", "/api/me/projects/12") is True
    # Un jeton borné AILLEURS ne lit pas ce projet, paramètre ou non.
    assert token_scopes.authorize({"projects": {"13": "read"}}, "GET",
                                  "/api/me/projects/12") is False
    # Et si un appelant passait l'URL ENTIÈRE au gate, il refuse (motif ancré) —
    # fail-closed, jamais l'inverse.
    assert token_scopes.authorize(
        porte_12, "GET", "/api/me/projects/12?include=procedures") is False


def test_include_is_declared_on_both_surfaces():
    """L'option doit être atteignable par la route REST (le besoin partenaire) ET par
    `oto_project op=get` — c'est le même handler (ADR 0009), donc le paramètre doit
    exister des deux côtés sous peine d'une divergence de surface silencieuse."""
    assert "include" in cap.ProjectReadInput.model_fields
    assert "include" in cap.ProjectInput.model_fields


# ── L'option DÉCLARÉE était INATTEIGNABLE par la route qui la porte (#367) ───────

async def _get_par_la_route(query: bytes) -> tuple[int, dict]:
    """Exerce le VRAI handler REST de `me.project_read`, avec une vraie query string.

    ⚠️ **Pourquoi ce test existe, et pourquoi les deux d'au-dessus ne suffisaient
    pas.** Ils vérifiaient que le champ est DÉCLARÉ (`"include" in model_fields`) et
    que la portée du jeton laisse passer le chemin. Les deux étaient verts, et
    pourtant `GET /api/me/projects/12?include=procedures` — la requête EXACTE du
    besoin partenaire, celle que la description de la capacité annonce — répondait
    `400 invalid_input` depuis le jour de sa livraison (c46d81e, 13/08) : l'adaptateur
    verse la query string telle quelle (`{"include": "procedures"}`, une CHAÎNE, parce
    qu'une URL ne connaît pas les listes), et pydantic refuse une chaîne là où le
    champ déclare `list[str]`.

    C'est le défaut que `docs/conventions.md` nomme : un test qui affirme une INTENTION
    grave le bug. On exerce donc le montage réel — adaptateur compris — plutôt que le
    modèle tout seul.
    """
    import json as _json

    from starlette.requests import Request
    from starlette.responses import JSONResponse

    from oto_mcp.capabilities import _rest_adapter
    from oto_mcp.capabilities.registry import CAPABILITIES

    capa = next(c for c in CAPABILITIES if c.key == "me.project_read")
    [binding] = capa.rest_bindings()

    def _json_error(_req, status, code, message=None):
        return JSONResponse({"error": code, "detail": message}, status_code=status)

    def _json_response(_req, payload, status=200):
        return JSONResponse(payload, status_code=status)

    async def _auth(_req, _verifier, **_kw):
        return "sub-lecteur", None

    handler = _rest_adapter._make_handler(capa, binding, None, _auth,
                                          _json_response, _json_error)

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request({"type": "http", "method": "GET", "path": "/api/me/projects/12",
                   "headers": [], "query_string": query,
                   "path_params": {"project_id": 12}}, _receive)
    rep = await handler(req)
    return rep.status_code, _json.loads(bytes(rep.body))


@pytest.fixture
def projet_lisible(store, monkeypatch):
    # L'autz d'une capacité est résolue par l'adaptateur AVANT le handler : sans ces
    # deux seams, la résolution d'org part chercher une base qui n'existe pas ici.
    from oto_mcp.capabilities import _authz
    monkeypatch.setattr(_authz.access, "current_org", lambda sub: 1)
    monkeypatch.setattr(_authz.access, "get_user_role", lambda sub: "member")
    monkeypatch.setattr(cap.db, "get_project_by_id", lambda pid: {
        "id": 12, "name": "Mission cliente", "icon": None, "brief_md": "",
        "owner_type": "org", "owner_id": "1"})
    monkeypatch.setattr(cap.db, "list_project_links", lambda pid: list(LIENS))
    monkeypatch.setattr(cap.db, "project_spine",
                        lambda pid, **kw: {"pages": 0, "root_doc": None, "tree": []})
    monkeypatch.setattr(cap, "_require_active_org_visible", lambda ctx, row: None)
    from oto_mcp import project_audit
    monkeypatch.setattr(project_audit, "audit_project",
                        lambda pid, links, **_k: {})


@pytest.mark.asyncio
async def test_la_requete_exacte_du_partenaire_aboutit(projet_lisible):
    """`GET /api/me/projects/12?include=procedures` — le geste littéral du signal
    #367 : un front partenaire affiche le chantier d'une org cliente et veut montrer
    le TEXTE de la procédure, pas seulement son titre."""
    code, corps = await _get_par_la_route(b"include=procedures")
    assert code == 200, corps
    assert [p["slug"] for p in corps["procedures"]] == ["qualification"]
    assert corps["procedures"][0]["body_md"] == "1. Vérifier le SIREN\n2. Noter"
    # oto#118 — et le RÉSUMÉ arrive par la même route, sur le même appel : c'est ce
    # que le front tiers affiche à la place du corps entier.
    assert corps["procedures"][0]["description"] == "Comment qualifier"


@pytest.mark.asyncio
async def test_plusieurs_inclusions_se_nomment_par_une_virgule(projet_lisible):
    """`?include=spine,procedures` — la virgule dans une valeur unique."""
    code, corps = await _get_par_la_route(b"include=spine,procedures")
    assert code == 200, corps
    assert "procedures" in corps and "spine" in corps


@pytest.mark.asyncio
async def test_la_forme_repetee_dit_la_MEME_chose_que_la_virgule(projet_lisible):
    """`?include=spine&include=procedures` — la sérialisation par défaut d'un `array`
    OpenAPI. Jusqu'au 29/08 (#418) l'adaptateur ne gardait que la DERNIÈRE valeur et
    ce fichier disait la forme « inutilisable » : `spine` disparaissait sans un mot."""
    code, corps = await _get_par_la_route(b"include=spine&include=procedures")
    assert code == 200, corps
    assert "procedures" in corps and "spine" in corps


@pytest.mark.asyncio
async def test_sans_le_parametre_la_reponse_ne_bouge_pas(projet_lisible):
    """L'additivité, tenue de bout en bout et pas seulement au niveau du handler."""
    code, corps = await _get_par_la_route(b"")
    assert code == 200, corps
    assert "procedures" not in corps
