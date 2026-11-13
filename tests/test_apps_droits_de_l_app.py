"""Une app partagée se CLIQUE avec le rôle Use, et sert ce qu'elle déclare avec les
droits de son AUTEUR quand il pourrait le partager lui-même (13/11/2026).

- View (`viewer`) voit l'app, sans geste : 403 `app_view_only` au consentement, à
  l'appel ; Use (`contributor`), Edit, Manage cliquent.
- Un tableau que l'auteur peut partager (`can_govern`) se lit et s'écrit sous l'auteur,
  pour un lecteur qui n'y a pas accès ; sinon la règle d'avant (auteur ∩ lecteur).
- Un agent dont l'auteur est propriétaire ou admin se lance AU NOM DE L'AUTEUR ; le
  lecteur qui a cliqué en lit seul l'erreur et le résultat (`app_consents.job_id`).

Mêmes bancs que `test_apps_gestes.py` (fixtures `s`, `hote`), réutilisés tels quels.
"""
import pytest

from oto_mcp.capabilities import apps_appel as C
from oto_mcp.capabilities._types import AuthzDenied

from tests.test_apps_gestes import (AUTEUR, AUTRE, CTX, ECRIRE, LECTEUR, ORG, call,  # noqa: F401
                                    consent, hote, refus, s)

LIRE = dict(action="read_rows", table="leads")


@pytest.fixture
def lecture(s, monkeypatch):
    from oto_mcp.capabilities.datastore import rows as R
    s["list"] = []
    monkeypatch.setattr(R, "_list_rows", lambda ctx, inp: s["list"].append(ctx) or {
        "rows": [{"_id": "r1", "_revision": "1", "name": "A", "status": "ok"}], "total": 1})
    return s


def view_seulement(s):
    """Le lecteur LIT l'app (partage `viewer`) sans pouvoir la modifier."""
    d = s["droits"]
    from oto_mcp import ownership
    lit = ownership.can_access

    def can_access(sub, rtype, rid, want="read"):
        if rtype == "app" and want == "write":
            return d.get((sub, "app", "write"), False)
        return lit(sub, rtype, rid, want)
    return can_access


# ── Qui clique : View voit, Use clique ────────────────────────────────────────

@pytest.mark.parametrize("geste_", [ECRIRE, dict(action="add_row", table="leads", values={"name": "B"}),
                                    dict(action="start_agent", agent="enrich")])
def test_view_ne_clique_pas(s, monkeypatch, geste_):
    from oto_mcp import ownership
    monkeypatch.setattr(ownership, "can_access", view_seulement(s))
    assert refus(consent, **geste_).code == "app_view_only"
    e = refus(call, consent="x", **geste_)
    assert (e.status, e.code) == (403, "app_view_only")
    assert s["update"] == s["append"] == s["run_now"] == []


def test_view_lit_toujours(lecture, monkeypatch):
    from oto_mcp import ownership
    monkeypatch.setattr(ownership, "can_access", view_seulement(lecture))
    assert call(**LIRE)["total"] == 1


def test_use_clique_sans_modifier_l_app(s, monkeypatch):
    from oto_mcp import ownership
    monkeypatch.setattr(ownership, "can_access", view_seulement(s))
    s["droits"][(LECTEUR, "app_role")] = "contributor"
    assert C.peut_cliquer(LECTEUR, 5)
    call(consent=consent(**ECRIRE), **ECRIRE)
    assert len(s["update"]) == 1


def test_le_depot_exige_use(s, monkeypatch):
    from oto_mcp import ownership
    monkeypatch.setattr(ownership, "can_access", view_seulement(s))
    with pytest.raises(AuthzDenied) as e:
        C.deposer(CTX, 5, "x", "a.csv", "text/csv", b"a,b\n")
    assert e.value.code == "app_view_only"


# ── Les tableaux : sous l'auteur quand il peut les partager ──────────────────

def test_auteur_qui_partage_le_tableau_le_sert_au_lecteur_sans_acces(lecture):
    lecture["droits"][(AUTEUR, "12", "govern")] = True
    call(**LIRE)
    (ctx,), = [lecture["list"]]
    assert (ctx.sub, ctx.org_id) == (AUTEUR, ORG)


def test_auteur_qui_ne_peut_pas_partager_laisse_le_lecteur_lire_comme_lui(lecture):
    call(**LIRE)
    (ctx,), = [lecture["list"]]
    assert ctx.sub == LECTEUR


def test_l_ecriture_par_l_app_passe_sous_l_auteur_et_le_dit(s):
    s["droits"][(AUTEUR, "12", "govern")] = True
    resume = C._consent(CTX, C.AppCallInput(app_id=5, **ECRIRE))["summary"]
    assert "through this app" in resume["text"] and ", as you" not in resume["text"]
    assert call(consent=consent(**ECRIRE), **ECRIRE)["through_app"] is True
    (sub, _), = s["update"]
    assert sub == AUTEUR


def test_le_partage_ne_sert_pas_ce_que_l_auteur_ne_peut_plus_ecrire(s):
    s["droits"][(AUTEUR, "12", "govern")] = True
    s["droits"][(AUTEUR, "12", "write")] = False
    assert refus(consent, **ECRIRE).code == "app_author_lost_access"


# ── Les agents : au nom de l'auteur quand il peut les partager ───────────────

def test_agent_de_l_auteur_lance_a_son_nom_pour_un_lecteur_qui_ne_le_voit_pas(s):
    s["niveaux"][(AUTEUR, 9)] = "owner"
    s["niveaux"][(LECTEUR, 9)] = None
    args = dict(action="start_agent", agent="enrich")
    resume = C._consent(CTX, C.AppCallInput(app_id=5, **args))["summary"]
    assert "name of the app's author" in resume["text"]
    assert call(consent=consent(**args), **args) == {"job_id": 77}
    (ctx, _), = s["run_now"]
    assert (ctx.sub, ctx.org_id) == (AUTEUR, ORG)


def test_un_invite_du_dehors_avec_use_lance_l_agent_de_l_auteur(s):
    """L'app ne partage pas l'agent, elle le lance : un invité du dehors aussi."""
    s["niveaux"][(AUTEUR, 9)] = "owner"
    s["niveaux"][(LECTEUR, 9)] = None
    s["droits"][(LECTEUR, "membre")] = False
    args = dict(action="start_agent", agent="enrich")
    assert call(consent=consent(**args), **args) == {"job_id": 77}
    (ctx, _), = s["run_now"]
    assert ctx.sub == AUTEUR


def test_agent_que_l_auteur_ne_peut_pas_partager_reste_au_lecteur(s):
    s["niveaux"][(LECTEUR, 9)] = None   # l'auteur n'est qu'éditeur : la règle d'avant
    assert refus(consent, action="start_agent", agent="enrich").code == "unknown_agent"


def test_le_run_lance_par_le_clic_est_au_lecteur_meme_au_nom_de_l_auteur(s):
    s["niveaux"][(AUTEUR, 10)] = "owner"
    s["niveaux"][(LECTEUR, 10)] = None
    s["jobs"] = [{"id": 2, "status": "failed", "run_id": "ra", "sub": AUTEUR, "trigger_id": "10",
                  "last_error": "theirs"},
                 {"id": 1, "status": "failed", "run_id": "rb", "sub": AUTEUR, "trigger_id": "10",
                  "last_error": "mine"}]
    s["results"] = {"ra": {"outcome": "done", "note": "x"}, "rb": {"outcome": "done", "note": "ok"}}
    s["lances"] = {(LECTEUR, 1)}
    runs = call(action="agent_runs", agent="watch")["runs"]
    assert [(r["job_id"], r["mine"], r["error"], r["note"]) for r in runs] == [
        (2, False, None, None), (1, True, "mine", "ok")]
    assert call(action="run_result", job_id=1)["run"]["note"] == "ok"
    assert refus(call, action="run_result", job_id=2).code == "unknown_run"
