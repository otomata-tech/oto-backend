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
    from oto_mcp.tools.lecture import LECTURE
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: SUB)
    appels: list[dict] = []
    m = FastMCP("t-recettes")

    @m.tool(annotations=LECTURE)
    def acme_people(company: str = "", page: int = 0, size: int = 3) -> dict:
        lot = PERSONNES[page * size:(page + 1) * size]
        appels.append({"company": company, "page": page, "size": size,
                       "ids": [p["id"] for p in lot]})
        session_org.note_call_trace(quantity=len(lot))
        return {"content": lot, "last": (page + 1) * size >= len(PERSONNES)}

    @m.tool(annotations=LECTURE)
    def acme_pricey(company: str = "", page: int = 0, size: int = 3) -> dict:
        # Facture 10 par page, quel que soit le nombre d'éléments rendus.
        lot = PERSONNES[page * size:(page + 1) * size]
        appels.append({"company": company, "page": page, "size": size,
                       "ids": [p["id"] for p in lot]})
        session_org.note_call_trace(quantity=10)
        return {"content": lot, "last": (page + 1) * size >= len(PERSONNES)}

    @m.tool(annotations=LECTURE)
    def acme_free(company: str = "", page: int = 0, size: int = 3) -> dict:
        # Ne déclare aucune quantité facturée.
        lot = PERSONNES[page * size:(page + 1) * size]
        return {"content": lot, "last": (page + 1) * size >= len(PERSONNES)}

    @m.tool()
    def acme_send(company: str = "", page: int = 0, size: int = 3) -> dict:
        # NON déclaré en lecture : une recette ne doit jamais l'appeler.
        appels.append({"company": company, "page": page, "size": size, "ids": []})
        return {"content": [], "last": True}

    @m.tool(annotations=LECTURE)
    def acme_broken(page: int = 0) -> dict:
        raise RuntimeError("upstream exploded")
    return m, appels


def _table(schema: dict | None = SCHEMA) -> str:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = str(db.create_datastore("user", SUB, f"recettes-{uuid.uuid4().hex[:6]}"))

    if schema is not None:
        make_store(SUB).set_schema(ns, schema)
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


def test_le_plafond_ne_relit_jamais_un_element_deja_lu(serveur):
    """En pagination par numéro, réduire la dernière page décalait la fenêtre : la page
    1 de taille 1 rendait l'élément 1, déjà lu et déjà payé, et sautait les suivants.
    La page n'est plus coupée : l'exécution s'arrête avant elle."""
    m, appels = serveur
    ns = _table()
    recu = _executer(m, _corps(limits={"max_units": 4}), ns)
    lus = [i for a in appels for i in a["ids"]]
    assert len(lus) == len(set(lus)), f"élément relu et repayé : {lus}"
    assert recu["stopped"] == "spend_cap" and recu["units"] == 3
    assert [a["size"] for a in appels] == [3]
    assert {l["contact_key"] for l in _lignes(ns)} == {
        f"acme_co::https://www.linkedin.com/in/p{i}" for i in (0, 1)}


def test_le_plafond_compte_ce_que_l_outil_facture(serveur):
    """10 facturés par page : le plafond de 15 s'arrête après 2 pages, quel que soit
    le nombre d'éléments rendus."""
    m, appels = serveur
    ns = _table()
    recu = _executer(m, _corps(tool="acme_pricey", limits={"max_units": 15}), ns)
    assert recu["stopped"] == "spend_cap" and recu["units"] == 20
    assert recu["units_basis"] == "billed" and recu["pages"] == 2


def test_sans_quantite_facturee_le_plafond_compte_les_unites_de_la_recette(serveur):
    m, _ = serveur
    ns = _table()
    recu = _executer(m, _corps(tool="acme_free"), ns)
    assert recu["done"] and recu["units"] == 7 and recu["units_basis"] == "declared"


def test_max_pages_borne_chaque_appel_et_la_reprise_avance(serveur):
    """La reprise reportait le compteur de pages : rejouée, elle s'arrêtait aussitôt
    sur `max_pages`, sans aucune page, et rendait le même jeton."""
    m, appels = serveur
    ns = _table()
    corps = _corps(limits={"max_units": 100, "max_pages": 1})
    recu = _executer(m, corps, ns)
    assert recu["stopped"] == "max_pages" and recu["pages"] == 1 and recu["resume"]
    suite = _executer(m, corps, ns, reprise=recu["resume"])
    assert suite["pages"] == 1 and suite["resume"] != recu["resume"]
    fin = _executer(m, corps, ns, reprise=suite["resume"])
    assert fin["done"] and [a["page"] for a in appels] == [0, 1, 2]
    assert len(_lignes(ns)) == 5


def test_le_budget_d_horloge_rend_une_reprise_qui_continue(serveur):
    m, appels = serveur
    ns = _table()
    recu = _executer(m, _corps(), ns, budget_s=0.0)
    assert recu["stopped"] == "time_budget" and recu["pages"] == 1 and recu["resume"]
    suite = _executer(m, _corps(), ns, reprise=recu["resume"])
    # `pages` compte CET appel : la reprise fait les deux pages restantes.
    assert suite["done"] and suite["pages"] == 2
    assert [a["page"] for a in appels] == [0, 1, 2]
    assert len(_lignes(ns)) == 5


def test_un_outil_non_declare_en_lecture_est_refuse_avant_tout_effet(serveur):
    from oto_mcp.datastore.core import make_store
    from oto_mcp.recipes import moteur
    m, appels = serveur
    ns = _table()
    with pytest.raises(moteur.RecetteRefusee) as e:
        _executer(m, _corps(tool="acme_send"), ns)
    assert e.value.code == "recipe_tool_not_read_only" and appels == []
    # Ni colonne créée, ni clé touchée : le refus précède l'ouverture du tableau.
    champs = {f["key"] for f in make_store(SUB).get_schema(ns)["fields"]}
    assert "country" not in champs
    with pytest.raises(moteur.RecetteRefusee) as e:
        asyncio.run(moteur.echantillon(m, SUB, "acme_send", {}, "content"))
    assert e.value.code == "recipe_tool_not_read_only" and appels == []


def test_un_tableau_sans_cle_recoit_celle_de_la_recette(serveur):
    from oto_mcp.datastore.core import make_store
    m, _ = serveur
    ns = _table({k: v for k, v in SCHEMA.items() if k != "key"})
    recu = _executer(m, _corps(), ns)
    assert recu["written"] == 5
    assert make_store(SUB).declared_key(ns) == "contact_key"
    # Rejouée : l'unicité tient, rien n'est réécrit.
    assert _executer(m, _corps(), ns)["written"] == 0 and len(_lignes(ns)) == 5


def test_un_tableau_libre_recoit_la_cle_de_la_recette(serveur):
    from oto_mcp.datastore.core import make_store
    m, _ = serveur
    ns = _table(None)
    recu = _executer(m, _corps(), ns)
    assert recu["written"] == 5 and make_store(SUB).declared_key(ns) == "contact_key"


def test_des_doublons_existants_empechent_de_declarer_la_cle(serveur):
    from oto_mcp.datastore.core import make_store
    from oto_mcp.recipes import moteur
    m, appels = serveur
    ns = _table({k: v for k, v in SCHEMA.items() if k != "key"})
    make_store(SUB).write_rows(ns, [{"contact_key": "dup"}, {"contact_key": "dup"}])
    with pytest.raises(moteur.RecetteRefusee) as e:
        _executer(m, _corps(), ns)
    assert e.value.code == "key_not_declarable" and appels == []


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


def test_une_valeur_prise_comme_cle_ne_remonte_pas():
    """Un résultat indexé par des valeurs (un e-mail en clé) ne rend pas ces valeurs,
    ni dans la forme d'un échantillon, ni dans le refus `items_not_found`."""
    from oto_mcp.recipes import moteur
    forme: dict = {}
    moteur._forme({"by_email": {"jane@acme.test": {"title": "CEO"}}}, "", forme, 0)
    assert "jane@acme.test" not in str(forme) and "by_email.*.title" in forme
    assert moteur._cles_sures(["content", "jane@acme.test", "Acme Co"]) == ["content"]
