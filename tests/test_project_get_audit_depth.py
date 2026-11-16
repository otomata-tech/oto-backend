"""La profondeur de l'audit d'`oto_project op=get`, contre un vrai PostgreSQL.

1. **`audit="links"` rend les MÊMES liens morts que l'audit complet** — procédures
   comprises, puisque l'écran qui ouvre un projet s'en sert pour cacher les procédures
   mortes de ses comptes —, sans les trois autres listes, et le dit (`checked`).
2. **L'audit complet n'a pas changé** : mêmes quatre listes, et le connecteur dont la
   clé ne vit que chez une équipe est toujours signalé.
3. **Le nombre de requêtes d'`op=get` ne suit ni le nombre d'équipes de l'org ni le
   nombre de connecteurs liés.** Il les suivait : une lecture par équipe et par
   connecteur faisait plus de la moitié des requêtes d'un projet de 86 liens au banc.
"""
from __future__ import annotations

import os
import uuid

import pytest

from oto_mcp.capabilities._types import RawCtx

_SUB = "u-admin-audit"


def _appel(op: str, **args) -> dict:
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "me.project")
    inp = cap.Input(op=op, **args)
    return cap.handler(cap.authz(RawCtx(sub=_SUB), inp), inp)


@pytest.fixture(scope="module")
def monde(live):
    """Un projet d'ORG : deux procédures vivantes, une procédure morte, un tableau vivant
    et un mort, un connecteur hors registre, `zoho` dont la clé n'existe que chez une
    équipe, `attio` qu'aucune clé ne sert ; un run qui a déroulé l'une des procédures."""
    from oto_mcp import db, group_store, org_store
    org = org_store.create_org("Acme", created_by=_SUB)
    org_store.add_org_member(org, _SUB, "org_admin")
    org_store.set_active_org(_SUB, org)
    equipe = group_store.create_group(org, "Ventes", created_by=_SUB)
    # Une équipe SANS la clé : `groups_holding` lit alors aussi les prêts (sa 2e requête).
    group_store.create_group(org, "Support", created_by=_SUB)
    with pytest.MonkeyPatch.context() as mp:   # la pose chiffre ; la lecture d'audit non
        mp.setenv("OTO_MCP_MASTER_KEY", os.urandom(32).hex())
        group_store.set_group_secret(equipe, "zoho", "cle-equipe", set_by=_SUB)
    projet = db.create_project("org", str(org), "Données", created_by=_SUB)
    slugs = []
    for _ in range(2):
        slug = "proc-" + uuid.uuid4().hex[:8]
        org_store.set_instruction("org", org, slug, "1. Une étape", title=slug, set_by=_SUB)
        slugs.append(slug)
        _appel("link", project_id=projet, target_type="procedure",
               target_ref=str(org_store.get_instruction("org", org, slug)["id"]))
    ds = db.create_datastore("org", str(org), "vivant_" + uuid.uuid4().hex[:6])
    _appel("link", project_id=projet, target_type="tableau", target_ref=str(ds))
    _appel("link", project_id=projet, target_type="connecteur", target_ref="zoho")
    # Un connecteur qu'aucune équipe ne détient : c'est lui qui interroge TOUTES les
    # équipes (`zoho`, détenu par la première, arrêtait la recherche avant).
    _appel("link", project_id=projet, target_type="connecteur", target_ref="attio")
    # Les liens morts se posent sous la garde d'`op=link` : directement en base.
    db.add_project_link(projet, "procedure", "987654321")
    db.add_project_link(projet, "tableau", "987654322")
    db.add_project_link(projet, "connecteur", "connecteur_inexistant")
    run_id = uuid.uuid4().hex
    db.insert_run(run_id, sub=_SUB, org_id=org, label="x", project_id=projet)
    db.insert_tool_call({"server": "oto", "sub": _SUB, "tool": "run_start",
                         "args": {"doctrine": slugs[0]}, "ok": True, "duration_ms": 1,
                         "run_id": run_id})
    return {"org": org, "projet": projet, "slugs": slugs}


def _morts(audit: dict) -> set:
    return {(d["target_type"], str(d["target_ref"])) for d in audit["dead_links"]}


def test_audit_links_rend_les_memes_liens_morts(monde):
    complet = _appel("get", project_id=monde["projet"])["audit"]
    liens = _appel("get", project_id=monde["projet"], audit="links")["audit"]
    assert _morts(liens) == _morts(complet) == {
        ("procedure", "987654321"), ("tableau", "987654322"),
        ("connecteur", "connecteur_inexistant")}
    assert liens["dead_links"] == complet["dead_links"]   # mêmes raisons, même ordre
    # Les trois autres listes ne sont pas servies vides : elles sont ABSENTES, et la
    # réponse nomme ce qui a été vérifié.
    assert sorted(liens) == ["checked", "dead_links"]
    assert liens["checked"] == ["dead_links"]


def test_audit_complet_inchange(monde):
    for audit in (_appel("get", project_id=monde["projet"])["audit"],
                  _appel("get", project_id=monde["projet"], audit="full")["audit"]):
        assert sorted(audit) == ["dead_links", "inert_procedures", "unbound_slots",
                                 "unresolvable_connectors"]
        assert [u["target_ref"] for u in audit["unresolvable_connectors"]] == ["zoho"]
        assert audit["inert_procedures"] == [monde["slugs"][1]]


def test_audit_hors_op_get_refuse(monde):
    from oto_mcp.capabilities._types import AuthzDenied
    with pytest.raises(AuthzDenied) as e:
        _appel("list", audit="links")
    assert e.value.code == "unsupported_audit"


def test_la_route_par_url_transmet_la_profondeur(monde):
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "me.project_read")
    inp = cap.Input(project_id=monde["projet"], audit="links")
    out = cap.handler(cap.authz(RawCtx(sub=_SUB), inp), inp)
    assert out["audit"]["checked"] == ["dead_links"]


def _requetes(monkeypatch, **args) -> int:
    import psycopg
    n = {"v": 0}
    orig = psycopg.Cursor.execute

    def compte(self, *a, **k):
        n["v"] += 1
        return orig(self, *a, **k)
    monkeypatch.setattr(psycopg.Cursor, "execute", compte)
    try:
        _appel("get", project_id=args.pop("projet"), **args)
    finally:
        monkeypatch.setattr(psycopg.Cursor, "execute", orig)
    return n["v"]


def test_les_requetes_ne_suivent_ni_les_equipes_ni_les_connecteurs(monde, monkeypatch):
    from oto_mcp import db, group_store
    _appel("get", project_id=monde["projet"])   # chaud
    avant = _requetes(monkeypatch, projet=monde["projet"])
    avant_liens = _requetes(monkeypatch, projet=monde["projet"], audit="links")
    for i in range(5):
        group_store.create_group(monde["org"], f"Équipe {i}", created_by=_SUB)
    # Le même connecteur lié sous une autre identité ne se juge qu'une fois.
    db.add_project_link(monde["projet"], "connecteur", "attio", identity_ref="autre@x")
    apres = _requetes(monkeypatch, projet=monde["projet"])
    apres_liens = _requetes(monkeypatch, projet=monde["projet"], audit="links")
    # Avant ce lot : deux requêtes de plus par équipe et par connecteur non résolu.
    assert apres == avant, (avant, apres)
    assert apres_liens == avant_liens, (avant_liens, apres_liens)
    # Et la profondeur `links` ne lit rien de plus que les liens : l'audit complet en
    # coûte davantage.
    assert apres_liens < apres
