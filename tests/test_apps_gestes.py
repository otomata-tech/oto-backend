"""`me.app.call` — les gestes d'une app qui ÉCRIVENT ou LANCENT (oto-backend#1386).

Chaque geste exige un consentement du lecteur (`me.app.consent`) : à usage unique,
pour CE geste (app, lecteur, action, arguments), jugé comme l'appel. Les runs d'un
agent déclaré se lisent (statut) ; ce qu'il a conclu, seulement pour les runs du
lecteur. On remplace le stockage des consentements par un dictionnaire qui tient le
même contrat que le SQL (brûlé une fois, mêmes clés) : ce qu'on vérifie, c'est le
jugement.
"""
import pytest

from oto_mcp import db, geste, ownership
from oto_mcp.capabilities import _acces_agent
from oto_mcp.capabilities import apps_appel as C
from oto_mcp.capabilities import runner_triggers as T
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.datastore import rows as R

LECTEUR = "lecteur"
AUTRE = "collegue"
AUTEUR = "auteur"
ORG = 42
CTX = ResolvedCtx(sub=LECTEUR, org_id=43)
MANIFEST = {
    "tables": {"leads": {"table": "12", "fields": ["name", "status"],
                         "set": {"status": ["ok", "no"]}, "add": ["name"]}},
    "agents": {"enrich": {"id": 9, "run": True, "input": True},
               "watch": {"id": 10, "run": False, "input": False}},
}
APP = {"id": 5, "project_id": 7, "title": "Leads", "manifest": MANIFEST,
       "thumbnail": None, "updated_by": AUTEUR, "archived_at": None}


@pytest.fixture
def s(monkeypatch):
    rec = {"update": [], "append": [], "run_now": [], "consents": {}, "pending": 0,
           "niveaux": {(AUTEUR, 9): "editor", (LECTEUR, 9): "editor",
                       (AUTEUR, 10): "viewer", (LECTEUR, 10): "viewer"},
           "jobs": [], "results": {}}
    droits = {(LECTEUR, "project"): True, (AUTEUR, "12", "read"): True,
              (AUTEUR, "12", "write"): True}

    def can_access(sub, rtype, rid, want="read"):
        if rtype == "project":
            return droits.get((sub, "project"), False)
        return droits.get((sub, rid, want), False)

    monkeypatch.setattr(ownership, "can_access", can_access)
    monkeypatch.setattr(db.apps, "get_for_call", lambda i: dict(APP) if i == 5 else None)
    monkeypatch.setattr(C, "org_du_projet", lambda pid: ORG)
    monkeypatch.setattr(C, "tableau_de_l_org", lambda ns, org: org == ORG)
    monkeypatch.setattr(R, "_update_row", lambda ctx, inp: rec["update"].append((ctx.sub, inp)) or {
        "_id": inp.row_id, "_revision": "2", "name": "A", "status": inp.patch["status"], "email": "x"})
    monkeypatch.setattr(R, "_append_row", lambda ctx, inp: rec["append"].append((ctx.sub, inp)) or {
        "_id": "r9", "_revision": "1", **inp.row})
    monkeypatch.setattr(db, "get_trigger", lambda tid, org: {"id": tid, "org_id": org} if org == ORG else None)
    monkeypatch.setattr(_acces_agent, "niveau", lambda sub, org, t: rec["niveaux"].get((sub, int(t["id"]))))
    monkeypatch.setattr(db, "file_du_declencheur", lambda tid, org: {"pending": rec["pending"]})

    def run_now(ctx, inp):
        rec["run_now"].append((ctx, inp))
        return {"job": {"id": 77}}
    monkeypatch.setattr(T, "_triggers_sync", run_now)

    def issue(app_id, sub, action, fp, h, ttl):
        rec["consents"][h] = {"app_id": app_id, "sub": sub, "action": action, "fp": fp,
                              "used": False, "job": None, "geste": None}

    def burn(app_id, sub, action, fp, h, geste_id):
        c = rec["consents"].get(h)
        if not c or c["used"] or (c["app_id"], c["sub"], c["action"], c["fp"]) != (app_id, sub, action, fp):
            return None
        c["used"], c["geste"] = True, geste_id
        return list(rec["consents"]).index(h) + 1

    monkeypatch.setattr(db.apps, "consent_issue", issue)
    monkeypatch.setattr(db.apps, "consent_burn", burn)
    monkeypatch.setattr(db.apps, "consent_job",
                        lambda cid, jid: list(rec["consents"].values())[cid - 1].update(job=jid))
    monkeypatch.setattr(db.apps, "agent_jobs", lambda org, tid, limit: rec["jobs"][:limit])
    monkeypatch.setattr(db.apps, "run_results",
                        lambda ids: {i: rec["results"][i] for i in ids if i in rec["results"]})
    monkeypatch.setattr(db.apps, "job_of_org",
                        lambda org, jid: next((j for j in rec["jobs"] if j["id"] == jid), None))
    rec["droits"] = droits
    return rec


def consent(ctx=CTX, **kw) -> str:
    return C._consent(ctx, C.AppCallInput(app_id=5, **kw))["consent"]


def call(ctx=CTX, **kw):
    return C._call(ctx, C.AppCallInput(app_id=5, **kw))


def refus(fn, *a, **kw) -> AuthzDenied:
    with pytest.raises(AuthzDenied) as e:
        fn(*a, **kw)
    return e.value


ECRIRE = dict(action="write_cell", table="leads", row_id="r1", field="status", value="ok",
              expected_revision="1")


# ── Le consentement ────────────────────────────────────────────────────────

@pytest.mark.parametrize("geste_", [ECRIRE, dict(action="add_row", table="leads", values={"name": "B"}),
                                    dict(action="start_agent", agent="enrich")])
def test_sans_consentement_rien_n_est_fait(s, geste_):
    e = refus(call, **geste_)
    assert (e.status, e.code) == (403, "consent_required")
    assert s["update"] == s["append"] == s["run_now"] == []


def test_le_consentement_vaut_une_fois(s):
    jeton = consent(**ECRIRE)
    with geste.portee(geste.CONSOLE, LECTEUR, "g-1"):
        out = call(consent=jeton, **ECRIRE)
    assert out["row"] == {"_id": "r1", "_revision": "2", "name": "A", "status": "ok"}
    (sub, inp), = s["update"]
    assert sub == LECTEUR and inp.expected_revision == "1"
    # Le journal : le geste d'écriture est rattaché au consentement.
    assert next(iter(s["consents"].values()))["geste"] == "g-1"
    assert refus(call, consent=jeton, **ECRIRE).code == "consent_invalid"
    assert len(s["update"]) == 1


def test_le_consentement_ne_vaut_que_pour_ses_arguments(s):
    jeton = consent(**ECRIRE)
    assert refus(call, consent=jeton, **{**ECRIRE, "value": "no"}).code == "consent_invalid"
    assert refus(call, consent=jeton, **{**ECRIRE, "row_id": "r2"}).code == "consent_invalid"
    assert s["update"] == []


def test_le_consentement_d_un_autre_lecteur_ne_sert_pas(s):
    s["droits"][(AUTRE, "project")] = True
    jeton = consent(**ECRIRE)
    assert refus(call, ResolvedCtx(sub=AUTRE, org_id=ORG), consent=jeton, **ECRIRE).code == "consent_invalid"


def test_un_jeton_invente_ne_sert_pas(s):
    assert refus(call, consent="x" * 43, **ECRIRE).code == "consent_invalid"


def test_le_consentement_juge_comme_l_appel(s):
    """Un geste que l'appel refuserait n'obtient pas de jeton."""
    assert refus(consent, **{**ECRIRE, "value": "peut-etre"}).code == "undeclared_value"
    assert refus(consent, **{**ECRIRE, "field": "name"}).code == "undeclared_write"
    assert refus(consent, **{**ECRIRE, "expected_revision": None}).code == "revision_required"
    s["droits"][(AUTEUR, "12", "write")] = False
    assert refus(consent, **ECRIRE).code == "app_author_lost_access"
    assert refus(consent, action="read_rows", table="leads").code == "consent_not_needed"
    assert s["consents"] == {}


def test_ajout_borne_aux_colonnes_declarees(s):
    assert refus(consent, action="add_row", table="leads", values={"email": "x"}).code == "undeclared_write"
    assert refus(consent, action="add_row", table="leads", values={}).code == "empty_row"
    args = dict(action="add_row", table="leads", values={"name": "B"})
    out = call(consent=consent(**args), **args)
    assert out["row"]["name"] == "B" and s["append"][0][1].datastore == "12"


# ── Lancer un agent ────────────────────────────────────────────────────────

def test_lancer_dans_l_org_du_projet_au_nom_du_lecteur(s):
    args = dict(action="start_agent", agent="enrich", input="acme.com")
    out = call(consent=consent(**args), **args)
    assert out == {"job_id": 77}
    (ctx, inp), = s["run_now"]
    assert (ctx.sub, ctx.org_id, inp.op, inp.trigger_id) == (LECTEUR, ORG, "run_now", 9)
    assert "acme.com" in inp.handoff and "app 5" in inp.handoff
    assert next(iter(s["consents"].values()))["job"] == 77


def test_un_run_d_app_avec_entree_est_dedoublonne(s):
    """`run_now` ne déduplique pas un run qui a un handoff ; un run d'app, si."""
    s["pending"] = 1
    args = dict(action="start_agent", agent="enrich", input="acme.com")
    e = refus(call, consent=consent(**args), **args)
    assert (e.status, e.code) == (409, "run_already_queued")
    assert s["run_now"] == []


def test_agent_non_lancable_ou_auteur_lecteur_seul(s):
    assert refus(consent, action="start_agent", agent="watch").code == "agent_not_runnable"
    s["niveaux"][(AUTEUR, 9)] = "viewer"
    assert refus(consent, action="start_agent", agent="enrich").code == "app_author_lost_access"
    s["niveaux"][(LECTEUR, 9)] = None
    assert refus(consent, action="start_agent", agent="enrich").code == "unknown_agent"
    assert refus(consent, action="start_agent", agent="nope").code == "undeclared_agent"


def test_entree_non_permise_ou_trop_longue(s):
    assert refus(consent, action="start_agent", agent="enrich", input="x" * 301).code == "agent_input_too_long"


# ── Les runs ───────────────────────────────────────────────────────────────

def test_les_runs_disent_le_statut_et_ne_concluent_que_pour_le_lecteur(s):
    s["jobs"] = [{"id": 2, "status": "done", "run_id": "ra", "sub": AUTRE, "trigger_id": "10"},
                 {"id": 1, "status": "done", "run_id": "rb", "sub": LECTEUR, "trigger_id": "10"}]
    s["results"] = {"ra": {"outcome": "done", "note": "secret"}, "rb": {"outcome": "done", "note": "mine"}}
    runs = call(action="agent_runs", agent="watch")["runs"]
    assert [(r["job_id"], r["status"], r["mine"], r["note"]) for r in runs] == [
        (2, "done", False, None), (1, "done", True, "mine")]


def test_run_result_seulement_pour_un_run_du_lecteur(s):
    s["jobs"] = [{"id": 2, "status": "done", "run_id": "ra", "sub": AUTRE, "trigger_id": "10"},
                 {"id": 1, "status": "done", "run_id": "rb", "sub": LECTEUR, "trigger_id": "10"},
                 {"id": 3, "status": "done", "run_id": "rc", "sub": LECTEUR, "trigger_id": "99"}]
    s["results"] = {"rb": {"outcome": "done", "note": "mine"}}
    assert call(action="run_result", job_id=1)["run"]["note"] == "mine"
    assert refus(call, action="run_result", job_id=2).code == "unknown_run"
    assert refus(call, action="run_result", job_id=3).code == "unknown_run"


def test_les_runs_d_un_agent_que_l_auteur_ne_voit_plus(s):
    s["niveaux"][(AUTEUR, 10)] = None
    assert refus(call, action="agent_runs", agent="watch").code == "app_author_lost_access"
