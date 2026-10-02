"""Le moteur des recettes sur un vrai store : un faux outil de connecteur paginé, des
lignes écrites comme toute écriture d'agent, les existantes laissées intactes, le
plafond de dépense, la reprise, et chaque page journalisée SOUS LE NOM DE L'OUTIL."""
from __future__ import annotations

import asyncio
import uuid

import pytest

SUB = "sub-recettes"
SCHEMA = {"key": "contact_key", "fields": [
    {"key": "contact_key", "type": "text"}, {"key": "linkedin_url", "type": "url"},
    {"key": "title", "type": "text"}, {"key": "company", "type": "text"},
    {"key": "status", "type": "text"}]}
# 7 personnes chez « Acme Co » : 3 pages de 3 ; la 5e n'a pas de profil LinkedIn.
PERSONNES = [{"id": f"p{i}", "profile": {"title": f"Role {i}"},
              "link": {"linkedin": None if i == 5 else f"https://www.linkedin.com/in/p{i}"},
              "location": {"country": "Spain" if i != 2 else "France"}} for i in range(7)]


def _sql(requete: str, *params):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        cur = conn.execute(requete, params or None)
        return cur.fetchall() if cur.description else None


@pytest.fixture(scope="module")
def compte(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@acme.test", name=SUB)
    return SUB


@pytest.fixture
def serveur(compte, monkeypatch):
    """Une instance FastMCP qui porte un faux outil paginé `acme_people`."""
    from fastmcp import FastMCP

    from oto_mcp import access, session_org
    from oto_mcp.auth import hooks
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: SUB)
    appels: list[dict] = []
    m = FastMCP("t-recettes")

    @m.tool()
    def acme_people(company: str = "", page: int = 0, size: int = 3) -> dict:
        appels.append({"company": company, "page": page, "size": size})
        lot = PERSONNES[page * size:(page + 1) * size]
        session_org.note_call_trace(quantity=len(lot))
        return {"content": lot, "last": (page + 1) * size >= len(PERSONNES)}

    @m.tool()
    def acme_broken(page: int = 0) -> dict:
        raise RuntimeError("upstream exploded")
    return m, appels


def _table() -> str:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = f"recettes-{uuid.uuid4().hex[:6]}"
    db.create_datastore("user", SUB, ns)
    make_store(SUB).set_schema(ns, SCHEMA)
    return ns


def _corps(**surcharge) -> dict:
    from oto_mcp.recipes import contrat
    corps = {"tool": "acme_people", "arguments": {"company": "{{params.company}}"},
             "params": {"company": {"required": True}},
             "source": {"items": "content", "pagination": {
                 "type": "page", "param": "page", "start": 0, "size": 3,
                 "size_param": "size", "last": "last"}},
             "where": [{"path": "location.country", "op": "eq", "value": "spain"}],
             "map": {"linkedin_url": "link.linkedin", "title": "profile.title",
                     "country": "location.country"},
             "values": {"company": "{{params.company}}", "status": "sourced"},
             "key": {"column": "contact_key",
                     "template": "{{params.company|slug}}::{{item.link.linkedin}}"},
             "limits": {"max_units": 100}}
    corps.update(surcharge)
    return contrat.valider(corps)


def _executer(m, corps, ns, **kw):
    from oto_mcp.recipes import moteur
    return asyncio.run(moteur.executer(corps, {"company": "Acme Co"}, fastmcp=m, sub=SUB,
                                       datastore=ns, **kw))


def _lignes(ns: str) -> list[dict]:
    from oto_mcp.datastore.core import make_store
    return make_store(SUB).cursor_rows(ns, limit=50)["rows"]


def test_une_recette_ecrit_les_pages_filtre_et_cree_ses_colonnes(serveur):
    m, appels = serveur
    ns = _table()
    recu = _executer(m, _corps(), ns)
    assert recu["done"] and recu["pages"] == 3 and recu["items_seen"] == 7
    assert recu["skipped_where"] == 1 and recu["skipped_no_key"] == 1
    assert recu["written"] == 5 and recu["created_columns"] == ["country"]
    assert [a["page"] for a in appels] == [0, 1, 2]
    lignes = _lignes(ns)
    assert {l["contact_key"] for l in lignes} == {
        f"acme_co::https://www.linkedin.com/in/p{i}" for i in (0, 1, 3, 4, 6)}
    assert all(l["status"] == "sourced" and l["company"] == "Acme Co" for l in lignes)


def test_une_ligne_existante_n_est_pas_touchee_par_defaut(serveur):
    from oto_mcp.datastore.core import make_store
    m, _ = serveur
    ns = _table()
    _executer(m, _corps(), ns)
    store = make_store(SUB)
    cible = next(l for l in _lignes(ns) if l["contact_key"].endswith("/p0"))
    store.update_row(ns, cible["_id"], {"status": "validated", "title": "kept"})
    recu = _executer(m, _corps(), ns)
    assert recu["written"] == 0 and recu["existing_left_untouched"] == 5
    apres = next(l for l in _lignes(ns) if l["_id"] == cible["_id"])
    assert apres["status"] == "validated" and apres["title"] == "kept"
    # `update` réécrit les colonnes de la correspondance, jamais les valeurs fixes.
    recu = _executer(m, _corps(on_existing="update"), ns)
    assert recu["updated"] == 5
    apres = next(l for l in _lignes(ns) if l["_id"] == cible["_id"])
    assert apres["title"] == "Role 0" and apres["status"] == "validated"


def test_le_plafond_de_depense_arrete_et_reduit_la_page(serveur):
    m, appels = serveur
    ns = _table()
    recu = _executer(m, _corps(limits={"max_units": 4}), ns)
    assert recu["stopped"] == "spend_cap" and recu["units"] == 4
    assert [a["size"] for a in appels] == [3, 1]


def test_le_budget_d_horloge_rend_une_reprise_qui_continue(serveur):
    m, appels = serveur
    ns = _table()
    recu = _executer(m, _corps(), ns, budget_s=0.0)
    assert recu["stopped"] == "time_budget" and recu["pages"] == 1 and recu["resume"]
    suite = _executer(m, _corps(), ns, reprise=recu["resume"])
    assert suite["done"] and suite["pages"] == 3
    assert [a["page"] for a in appels] == [0, 1, 2]
    assert len(_lignes(ns)) == 5


def test_chaque_page_est_journalisee_et_facturee_sous_le_nom_de_l_outil(serveur):
    m, _ = serveur
    ns = _table()
    _executer(m, _corps(), ns)
    rows = _sql("SELECT tool, quantity FROM tool_calls WHERE sub = %s "
                "AND tool = 'acme_people' AND args->>'company' = 'Acme Co' "
                "ORDER BY id DESC LIMIT 3", SUB)
    assert len(rows) == 3
    assert sorted(r["quantity"] for r in rows) == [1, 3, 3]


def test_un_outil_en_echec_arrete_avec_son_code_sans_rien_ecrire(serveur):
    m, _ = serveur
    ns = _table()
    recu = _executer(m, _corps(tool="acme_broken", arguments={}), ns)
    assert recu["stopped"] and recu["stopped"] != "spend_cap" and recu["written"] == 0
    assert "exploded" not in (recu.get("error") or "")


def test_l_epreuve_appelle_une_page_n_ecrit_rien_et_rend_le_remplissage(serveur):
    m, _ = serveur
    ns = _table()
    recu = _executer(m, _corps(), ns, ecrire=False, pages_max=1)
    assert recu["pages"] == 1 and recu["written"] == 0 and _lignes(ns) == []
    assert recu["fill"] == {"linkedin_url": 1.0, "title": 1.0, "country": 1.0}


def test_une_cle_declaree_differente_est_refusee_avant_tout_appel(serveur):
    from oto_mcp.recipes import moteur
    m, appels = serveur
    ns = _table()
    corps = _corps(key={"column": "linkedin_url"})
    with pytest.raises(moteur.RecetteRefusee) as e:
        _executer(m, corps, ns)
    assert e.value.code == "key_mismatch" and appels == []


def test_l_echantillon_rend_la_forme_sans_valeur(serveur):
    from oto_mcp.recipes import moteur
    m, _ = serveur
    forme = asyncio.run(moteur.echantillon(m, SUB, "acme_people", {"size": 3}, "content"))
    chemins = {p["path"]: p for p in forme["paths"]}
    assert forme["items"] == 3 and chemins["link.linkedin"]["filled"] == 3
    assert "values" not in str(forme)
