"""Le PÉRIMÈTRE DE LIGNES d'un lecteur d'app (`docs/apps.md` § Périmètre de lignes), contre
un PostgreSQL réel : deux partenaires sur un même tableau, chacun ne voit, n'écrit et
n'ajoute que SA fiche — par une valeur posée sur son partage (`op=set_scope`, `$share`)
ou par son adresse vérifiée (`$email`). Toutes les portes : lecture, agrégat, lot,
recherche, filtre de l'app, écriture, ajout, consentement, run d'agent ; un périmètre
qui ne se résout pas ne sert rien."""
from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

HOTE = "https://manage.oto.cx"

SCHEMA = {"fields": [{"key": "name", "type": "text"}, {"key": "partner_id", "type": "text"},
                     {"key": "contact_email", "type": "email"},
                     {"key": "status", "type": "text"}, {"key": "since", "type": "date"}]}


def _ctx(m, sub):
    from oto_mcp.capabilities._types import ResolvedCtx
    org = m["orgs"].get(sub, m["org"])
    return ResolvedCtx(sub=sub, org_id=org, channel="rest")


def _apps(m, sub, **kw):
    from oto_mcp.capabilities import apps as A
    return A._app(_ctx(m, sub), A.AppInput(app_id=m["app"], **kw))


def _appel(m, sub, **kw):
    from oto_mcp.capabilities import apps_appel as C
    return C._call(_ctx(m, sub), C.AppCallInput(app_id=m["app"], **kw))


def _lire(m, sub, **kw):
    return _appel(m, sub, action="read_rows", table="fiches", **kw)


def _noms(m, sub, **kw):
    return sorted(r["name"] for r in _lire(m, sub, **kw)["rows"])


def _refus(fn, *a, **kw):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        fn(*a, **kw)
    return e.value


@contextmanager
def _verifie(sub):
    """Le jeton de la requête dit l'adresse de `sub` vérifiée (`email_verified`)."""
    from oto_mcp.api import base
    j = base._EMAIL_VERIFIE.set((sub, True))
    try:
        yield
    finally:
        base._EMAIL_VERIFIE.reset(j)


@contextmanager
def _comme(sub):
    from oto_mcp import client_trace, geste
    jeton = client_trace.set_current(ip=None, user_agent=None, origin=HOTE, fetch_dest="empty")
    try:
        with geste.portee(geste.CONSOLE, sub, f"g-{uuid.uuid4().hex[:6]}"):
            yield
    finally:
        client_trace.reset(jeton)


def _consentir(m, sub, **corps):
    from oto_mcp.capabilities import apps_appel as C
    with _comme(sub):
        return C._consent(_ctx(m, sub), C.AppCallInput(app_id=m["app"], **corps))


def _geste(m, sub, **corps):
    from oto_mcp.capabilities import apps_appel as C
    with _comme(sub):
        accord = C._consent(_ctx(m, sub), C.AppCallInput(app_id=m["app"], **corps))
        return C._call(_ctx(m, sub), C.AppCallInput(app_id=m["app"],
                                                     consent=accord["consent"], **corps))


MANIFESTE = {
    # Le tableau ENTIER (sans liste de champs) : la recherche `q` est permise, et un
    # agent en hériterait sans le périmètre.
    "tables": {"fiches": {"table": None, "set": {"status": ["won", "lost"]},
                          "add": ["name", "status"]}},
    "roles": {"partner": {"rows": {"fiches": {"partner_id": "$share"}}},
              "contact": {"rows": {"fiches": {"contact_email": "$email"}}},
              "free": {}},
    "default_role": "partner"}


@pytest.fixture
def monde(live, monkeypatch):
    from oto_mcp import db, org_store
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.apps_manifeste import forme
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    from oto_mcp.datastore.core import make_store
    monkeypatch.setattr(R.email, "send_resource_shared_email", lambda to, **kw: True)
    u = uuid.uuid4().hex[:8]
    auteur, p1, p2, p3 = (f"{x}_{u}" for x in ("auteur", "p1", "p2", "p3"))
    for s in (auteur, p1, p2, p3):
        db.upsert_user(s, email=f"{s}@example.test")
    org = org_store.create_org(f"o_{u}", created_by=auteur)
    org_store.add_org_member(org, auteur, "org_admin")
    orgs = {}
    for p in (p1, p2, p3):
        # Chaque partenaire vit dans SA propre org : un invité de l'extérieur.
        orgs[p] = org_store.create_org(f"org_{p}", created_by=p)
        org_store.add_org_member(orgs[p], p, "org_admin")
    ns_id = db.create_datastore("user", auteur, f"fiches_{u}", context_org_id=org)
    st = make_store(auteur)
    st.set_schema(str(ns_id), SCHEMA)
    lignes = {}
    for nom, pid, mail in (("Alpha", "P-1", f"{p1}@example.test"),
                           ("Beta", "P-2", f"{p2}@example.test"),
                           ("Gamma", "", "")):
        r = st.append_row(str(ns_id), {"name": nom, "partner_id": pid,
                                       "contact_email": mail, "status": "open"})
        lignes[nom] = r
    clean, problems = forme({**MANIFESTE, "tables": {"fiches": {
        **MANIFESTE["tables"]["fiches"], "table": str(ns_id)}}})
    assert problems == []
    clean["tables"]["fiches"]["table"] = str(ns_id)
    app = db.apps.create(("user", auteur), org, title=f"Desk {u}", manifest=clean,
                         html="", thumbnail=None, by=auteur)
    m = {"auteur": auteur, "p1": p1, "p2": p2, "p3": p3, "org": org, "orgs": orgs,
         "ns_id": ns_id, "app": int(app["id"]), "lignes": lignes}
    for p in (p1, p2, p3):
        R._resources(_ctx(m, auteur), ResourceInputV2(
            op="share", resource_type="app", resource_id=str(m["app"]), audience="person",
            email=f"{p}@example.test", role="contributor"))
        # Le tableau par l'app SEULE : aucun partage ordinaire.
        _apps(m, auteur, op="grant_through_app", principal_type="user", principal_id=p)
    for p, pid in ((p1, "P-1"), (p2, "P-2")):
        _apps(m, auteur, op="set_scope", principal_type="user", principal_id=p,
              scope={"fiches": {"partner_id": pid}})
    return m


# ── Lire : chacun sa fiche ───────────────────────────────────────────────────────

def test_par_le_partage_chacun_ne_lit_que_sa_fiche(monde):
    m = monde
    assert _noms(m, m["p1"]) == ["Alpha"]
    assert _noms(m, m["p2"]) == ["Beta"]
    assert _lire(m, m["p1"])["total"] == 1


def test_par_l_adresse_verifiee_chacun_ne_lit_que_sa_fiche(monde):
    m = monde
    for p in ("p1", "p2"):
        _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m[p],
              role="contact")
        _apps(m, m["auteur"], op="set_scope", principal_type="user", principal_id=m[p])
    with _verifie(m["p1"]):
        assert _noms(m, m["p1"]) == ["Alpha"]
    with _verifie(m["p2"]):
        assert _noms(m, m["p2"]) == ["Beta"]


def test_sans_adresse_verifiee_rien(monde):
    m = monde
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["p1"],
          role="contact")
    _apps(m, m["auteur"], op="set_scope", principal_type="user", principal_id=m["p1"])
    assert _noms(m, m["p1"]) == []
    # Vérifiée pour un AUTRE compte : rien non plus.
    with _verifie(m["p2"]):
        assert _noms(m, m["p1"]) == []


def test_l_agregat_ne_compte_que_ses_lignes(monde):
    m = monde
    groupes = _appel(m, m["p1"], action="read_aggregate", table="fiches")["groups"]
    assert sum(g.get("count", 0) for g in groupes) == 1
    par = _appel(m, m["p1"], action="read_aggregate", table="fiches",
                 options={"group_by": "partner_id"})["groups"]
    assert [g.get("partner_id") or g.get("key") or g.get("group") for g in par] \
        and all("P-2" not in str(g) for g in par)


def test_le_lot_lit_chaque_lecture_dans_le_perimetre(monde):
    m = monde
    out = _appel(m, m["p1"], action="batch", reads=[
        {"id": "a", "action": "read_rows", "table": "fiches"},
        {"id": "b", "action": "read_aggregate", "table": "fiches"},
        {"id": "c", "action": "read_rows", "table": "fiches", "options": {"q": "Beta"}}])
    r = {x["id"]: x for x in out["results"]}
    assert [x["name"] for x in r["a"]["rows"]] == ["Alpha"]
    assert sum(g.get("count", 0) for g in r["b"]["groups"]) == 1
    assert r["c"]["rows"] == []


def test_la_recherche_reste_dans_le_perimetre(monde):
    m = monde
    assert _noms(m, m["p1"], options={"q": "Beta"}) == []
    assert _noms(m, m["p1"], options={"q": "Alpha"}) == ["Alpha"]


def test_le_filtre_de_l_app_ne_l_elargit_pas(monde):
    m = monde
    assert _noms(m, m["p1"], options={"filter": {"partner_id": "P-2"}}) == []
    assert _noms(m, m["p1"], options={"filter": {"name": "Beta"}}) == []
    assert _noms(m, m["p1"], options={"filter": {"partner_id": "P-1"}}) == ["Alpha"]


# ── Un périmètre qui ne se résout pas : rien ─────────────────────────────────────

def test_share_sans_valeur_posee_rien_et_aucun_ajout(monde):
    m = monde                                   # p3 : rôle partner, aucun périmètre posé
    assert _noms(m, m["p3"]) == []
    groupes = _appel(m, m["p3"], action="read_aggregate", table="fiches")["groups"]
    assert sum(g.get("count", 0) for g in groupes) == 0
    e = _refus(_consentir, m, m["p3"], action="add_row", table="fiches",
               values={"name": "Delta"})
    assert e.code == "row_scope_unresolved"
    # Sans rien voir, il n'écrit pas même la ligne que personne ne tient.
    g = m["lignes"]["Gamma"]
    e = _refus(_consentir, m, m["p3"], action="write_cell", table="fiches",
               row_id=g["_id"], field="status", value="won",
               expected_revision=g["_revision"])
    assert e.code == "row_not_found"


def test_un_role_sans_lignes_et_un_partage_sans_perimetre_voit_tout(monde):
    """Le contrôle positif : ce que le rôle et le partage ne bornent pas reste ouvert."""
    m = monde
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["p3"],
          role="free")
    assert _noms(m, m["p3"]) == ["Alpha", "Beta", "Gamma"]
    # Un périmètre posé seul (sans `$share` au rôle) borne aussi.
    _apps(m, m["auteur"], op="set_scope", principal_type="user", principal_id=m["p3"],
          scope={"fiches": {"partner_id": "P-2"}})
    assert _noms(m, m["p3"]) == ["Beta"]


# ── Écrire, ajouter ──────────────────────────────────────────────────────────────

def test_ecrire_la_fiche_du_voisin_est_refuse_comme_une_ligne_absente(monde):
    m = monde
    b = m["lignes"]["Beta"]
    corps = dict(action="write_cell", table="fiches", row_id=b["_id"], field="status",
                 value="won", expected_revision=b["_revision"])
    e = _refus(_consentir, m, m["p1"], **corps)
    assert (e.status, e.code) == (404, "row_not_found")
    # Une révision fausse ne révèle rien non plus : le même 404, pas un 409.
    e = _refus(_consentir, m, m["p1"], **{**corps, "expected_revision": "x"})
    assert (e.status, e.code) == (404, "row_not_found")
    # Et l'appel, même avec un jeton obtenu sur sa propre fiche, est rejugé.
    a = m["lignes"]["Alpha"]
    accord = _consentir(m, m["p1"], **{**corps, "row_id": a["_id"],
                                       "expected_revision": a["_revision"]})
    e = _refus(_appel, m, m["p1"], consent=accord["consent"], **corps)
    assert e.code == "row_not_found"


def test_ecrire_sa_fiche_passe(monde):
    m = monde
    a = m["lignes"]["Alpha"]
    out = _geste(m, m["p1"], action="write_cell", table="fiches", row_id=a["_id"],
                 field="status", value="won", expected_revision=a["_revision"])
    assert out["row"]["status"] == "won"


def test_l_ajout_nait_dans_le_perimetre(monde):
    m = monde
    accord = _consentir(m, m["p1"], action="add_row", table="fiches",
                        values={"name": "Delta", "partner_id": "P-2"})
    # Le résumé montre ce qui sera ÉCRIT : la valeur du périmètre, pas celle de l'app.
    assert accord["summary"]["content"]["row"]["partner_id"] == "P-1"
    out = _geste(m, m["p1"], action="add_row", table="fiches",
                 values={"name": "Delta", "partner_id": "P-2"})
    assert out["row"]["partner_id"] == "P-1"
    assert _noms(m, m["p1"]) == ["Alpha", "Delta"]
    assert _noms(m, m["p2"]) == ["Beta"]
    # Sans le champ du périmètre (absent de `add`) : posé quand même.
    out = _geste(m, m["p2"], action="add_row", table="fiches", values={"name": "Eta"})
    assert out["row"]["partner_id"] == "P-2"


def test_l_ajout_par_l_adresse_pose_l_adresse(monde):
    m = monde
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["p1"],
          role="contact")
    _apps(m, m["auteur"], op="set_scope", principal_type="user", principal_id=m["p1"])
    with _verifie(m["p1"]):
        out = _geste(m, m["p1"], action="add_row", table="fiches", values={"name": "Zeta"})
        assert out["row"]["contact_email"] == f"{m['p1']}@example.test"
        assert _noms(m, m["p1"]) == ["Alpha", "Zeta"]


# ── L'éditeur, l'essai d'un rôle, le run d'un agent ──────────────────────────────

def test_l_editeur_n_est_pas_borne_et_essaie_un_role(monde):
    m = monde
    assert _noms(m, m["auteur"]) == ["Alpha", "Beta", "Gamma"]
    # `as_role` partner : aucun partage retenu, `$share` ne trouve rien.
    assert _noms(m, m["auteur"], as_role="partner") == []
    # `as_role` contact : SA propre adresse vérifiée — aucune fiche ne la porte.
    with _verifie(m["auteur"]):
        assert _noms(m, m["auteur"], as_role="contact") == []
        assert _noms(m, m["auteur"], as_role="free") == ["Alpha", "Beta", "Gamma"]


def test_l_agent_n_herite_pas_d_un_tableau_au_perimetre_de_lignes(monde, monkeypatch):
    from types import SimpleNamespace

    from oto_mcp import db, verrou_org
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    monkeypatch.setattr(db.apps, "app_of_run", lambda job_id, sub: m["app"])
    for p in ("p1", "p3"):
        monkeypatch.setattr(verrou_org, "travail_de_la_requete",
                            lambda p=p: SimpleNamespace(job_id=1, sub=m[p]))
        assert C._droit_du_run(m[p], str(m["ns_id"])) is None
    # Le contrôle positif : un rôle sans lignes, un partage sans périmètre → hérité.
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["p3"],
          role="free")
    assert C._droit_du_run(m["p3"], str(m["ns_id"])) == "read"


# ── Poser un périmètre ───────────────────────────────────────────────────────────

def test_op_roles_rend_le_perimetre_par_nom_de_tableau(monde):
    m = monde
    grants = {g["principal_id"]: g for g in _apps(m, m["auteur"], op="roles")["grants"]}
    assert grants[m["p1"]]["scope"] == {"fiches": {"partner_id": "P-1"}}
    assert grants[m["p3"]]["scope"] is None


@pytest.mark.parametrize("scope, code", [
    ({"fiches": {"nope": "x"}}, "invalid_scope"),
    ({"fiches": {"since": "2026-01-01"}}, "invalid_scope"),
    ({"fiches": {"partner_id": ""}}, "invalid_scope"),
    ({"fiches": {"partner_id": None}}, "invalid_scope"),
    ({"autre": {"partner_id": "P-9"}}, "invalid_scope"),
])
def test_un_perimetre_mal_forme_est_refuse(monde, scope, code):
    m = monde
    e = _refus(_apps, m, m["auteur"], op="set_scope", principal_type="user",
               principal_id=m["p3"], scope=scope)
    assert e.code == code
    assert _noms(m, m["p3"]) == []


def test_seul_qui_partage_le_tableau_pose_un_perimetre(monde):
    from oto_mcp import db
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities.resources_v2 import ResourceInputV2
    m = monde
    g = f"g_{uuid.uuid4().hex[:6]}"
    db.upsert_user(g, email=f"{g}@example.test")
    m["orgs"][g] = m["orgs"][m["p3"]]
    R._resources(_ctx(m, m["auteur"]), ResourceInputV2(
        op="share", resource_type="app", resource_id=str(m["app"]), audience="person",
        email=f"{g}@example.test", role="manager"))
    # Gérant de l'APP, sans droit de partage sur le tableau : ni poser, ni retirer.
    for scope in ({"fiches": {"partner_id": "P-2"}}, None):
        e = _refus(_apps, m, g, op="set_scope", principal_type="user", principal_id=m["p1"],
                   scope=scope)
        assert e.code == "forbidden"
    assert _noms(m, m["p1"]) == ["Alpha"]


def test_un_perimetre_se_pose_sur_un_partage(monde):
    from oto_mcp import db
    m = monde
    x = f"x_{uuid.uuid4().hex[:6]}"
    db.upsert_user(x, email=f"{x}@example.test")
    e = _refus(_apps, m, m["auteur"], op="set_scope", principal_type="user", principal_id=x,
               scope={"fiches": {"partner_id": "P-1"}})
    assert e.code == "not_shared"


def test_le_perimetre_part_avec_le_partage(monde):
    from oto_mcp import ownership
    m = monde
    ownership.revoke("app", str(m["app"]), "user", m["p1"])
    _refus(_lire, m, m["p1"])


# ── Le manifeste ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rows, attendu", [
    ({"autre": {"partner_id": "$share"}}, "declares no such table"),
    ({"fiches": {"partner_id": "P-1"}}, "$email"),
    ({"fiches": {"status": "$share"}}, "may set this field"),
    ({"fiches": "x"}, "must map"),
    ({"fiches": {"_id": "$share"}}, "isn't a column"),
])
def test_un_perimetre_de_role_mal_forme_est_refuse(rows, attendu):
    from oto_mcp.capabilities.apps_manifeste import forme
    _, problems = forme({**MANIFESTE, "tables": {"fiches": {
        **MANIFESTE["tables"]["fiches"], "table": "1"}},
        "roles": {"partner": {"rows": rows}}})
    assert any(attendu in p for p in problems), problems


def test_un_role_ne_borne_que_les_tableaux_qu_il_ouvre():
    from oto_mcp.capabilities.apps_manifeste import forme
    _, problems = forme({
        "tables": {"fiches": {"table": "1"}, "autres": {"table": "2"}},
        "roles": {"partner": {"tables": {"autres": True},
                              "rows": {"fiches": {"partner_id": "$share"}}}},
        "default_role": "partner"})
    assert any("doesn't open this table" in p for p in problems), problems


def test_le_champ_du_perimetre_se_juge_contre_le_schema(monde):
    from oto_mcp.capabilities.apps_manifeste import auteur, forme
    m = monde
    for champ, attendu in (("nope", "doesn't declare"), ("since", "type date")):
        clean, problems = forme({**MANIFESTE, "tables": {"fiches": {
            **MANIFESTE["tables"]["fiches"], "table": str(m["ns_id"])}},
            "roles": {"partner": {"rows": {"fiches": {champ: "$share"}}}}})
        assert problems == []
        problems = auteur(m["auteur"], m["org"], clean)
        assert any(attendu in p for p in problems), problems


# ── La clé métier : unique sur tout le tableau, elle ne se choisit pas d'un périmètre ──

def _cle(m, champ):
    """La clé métier posée APRÈS l'écriture du manifeste : jugée sur le schéma vivant."""
    from oto_mcp.datastore.core import make_store
    make_store(m["auteur"]).set_schema(str(m["ns_id"]), {**SCHEMA, "key": champ})


def test_un_perimetre_ne_choisit_pas_la_cle_meme_libre(monde):
    """La clé du voisin ET une clé libre reçoivent le MÊME refus : rien ne dit laquelle
    est prise."""
    m = monde
    _cle(m, "name")
    for nom in ("Beta", "Inconnu-42"):
        e = _refus(_consentir, m, m["p1"], action="add_row", table="fiches",
                   values={"name": nom, "status": "open"})
        assert (e.status, e.code) == (403, "row_scope_key")
    # Sans la clé, l'ajout passe : la ligne naît sans clé, dans son périmètre.
    out = _geste(m, m["p1"], action="add_row", table="fiches", values={"status": "open"})
    assert out["row"]["partner_id"] == "P-1"


def test_un_perimetre_ne_reecrit_pas_la_cle(monde, monkeypatch):
    """`status` ne peut pas être une clé ici (trois lignes `open`) : le schéma vivant est
    remplacé par sa lecture, seule source que le garde consulte."""
    from oto_mcp.capabilities import apps_appel as C
    m = monde
    monkeypatch.setattr(C, "_cle_du_tableau", lambda ns: "status")
    r = m["lignes"]["Alpha"]
    e = _refus(_consentir, m, m["p1"], action="write_cell", table="fiches", row_id=r["_id"],
               field="status", value="won", expected_revision=r["_revision"])
    assert (e.status, e.code) == (403, "row_scope_key")


def test_le_refus_d_une_cle_deja_portee_ne_nomme_pas_la_ligne(monde):
    """Un lecteur SANS périmètre (rôle `free`) qui ajoute une clé déjà portée : le 409 ne
    livre ni l'identifiant ni les valeurs de la ligne en place."""
    m = monde
    _cle(m, "name")
    _apps(m, m["auteur"], op="set_role", principal_type="user", principal_id=m["p3"],
          role="free")
    e = _refus(_geste, m, m["p3"], action="add_row", table="fiches",
               values={"name": "Beta", "status": "open"})
    assert (e.status, e.code) == (409, "row_conflict")
    assert m["lignes"]["Beta"]["_id"] not in (e.message or "")
    assert not e.details


def test_le_manifeste_refuse_un_role_a_perimetre_qui_ajoute_la_cle(monde):
    m = monde
    _cle(m, "name")
    vu = _apps(m, m["auteur"], op="get")["app"]
    e = _refus(_apps, m, m["auteur"], op="update", manifest=vu["manifest"],
               expected_rev=vu["rev"])
    assert e.code == "invalid_manifest" and "unique key" in e.message


def test_op_roles_dit_a_l_ecran_ce_que_chaque_role_borne(monde):
    m = monde
    out = _apps(m, m["auteur"], op="roles")
    par_role = {r["key"]: r for r in out["roles"]}
    assert par_role["partner"]["rows"] == {"fiches": {"partner_id": "$share"}}
    assert par_role["contact"]["rows"] == {"fiches": {"contact_email": "$email"}}
    assert par_role["free"]["rows"] is None
    assert out["tables"] == {"fiches": str(m["ns_id"])}
