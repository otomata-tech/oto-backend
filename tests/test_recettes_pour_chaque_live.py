"""`for_each`, les correspondances avec un tableau et la dérive, sur un vrai store : les
lignes d'un tableau PARENT déclenchent l'appel, reçoivent leur état, et une exécution
suivante ne les repaie pas."""
from __future__ import annotations

import asyncio
import uuid

import pytest

SUB = "sub-recettes-fe"
# Trois sociétés : Acme a 5 personnes, Globex aucune, Initech 2.
PERSONNES = {
    "acme": [{"id": f"a{i}", "name": f"A {i}", "site": "https://www.acme.test/team",
              "emails": [f"a{i}@acme.test"]} for i in range(5)],
    "globex": [],
    "initech": [{"id": f"i{i}", "name": f"I {i}", "site": "initech.test"} for i in range(2)],
}
CIBLE = {"key": "person_key", "fields": [
    {"key": "person_key", "type": "text"}, {"key": "name", "type": "text"},
    {"key": "company", "type": "text"}]}


@pytest.fixture(scope="module")
def compte(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@acme.test", name=SUB)
    return SUB


@pytest.fixture
def serveur(compte, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access, session_org
    from oto_mcp.auth import hooks
    from oto_mcp.tools.lecture import LECTURE
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: SUB)
    appels: list[dict] = []
    m = FastMCP("t-recettes-fe")

    @m.tool(annotations=LECTURE)
    def acme_people_of(company: str = "", page: int = 0, size: int = 2) -> dict:
        tous = PERSONNES.get(company, [])
        lot = tous[page * size:(page + 1) * size]
        appels.append({"company": company, "page": page})
        session_org.note_call_trace(quantity=len(lot))
        return {"content": lot, "last": (page + 1) * size >= len(tous)}

    @m.tool(annotations=LECTURE)
    def acme_picky(company: str = "", page: int = 0, size: int = 2) -> dict:
        # Refuse les sociétés qu'il ne connaît pas : un échec qui tient à la LIGNE.
        appels.append({"company": company, "page": page})
        if company not in PERSONNES:
            from mcp.types import INVALID_PARAMS, ErrorData

            from oto_mcp.mcp_errors import McpError
            raise McpError(ErrorData(code=INVALID_PARAMS, message="unknown company"))
        tous = PERSONNES[company]
        return {"content": tous[page * size:(page + 1) * size],
                "last": (page + 1) * size >= len(tous)}

    @m.tool(annotations=LECTURE)
    def acme_shape(company: str = "", page: int = 0, size: int = 20) -> dict:
        # Douze personnes par société ; chez Acme, sans `name` : la forme a changé.
        appels.append({"company": company, "page": page})
        base = [{"id": f"{company}{i}"} for i in range(12)]
        if company != "acme":
            base = [{**p, "name": f"N {p['id']}"} for p in base]
        return {"content": base, "last": True}

    @m.tool(annotations=LECTURE)
    def acme_flat(page: int = 0, size: int = 12) -> dict:
        # Douze éléments sans `name` : la forme a changé.
        appels.append({"company": None, "page": page})
        return {"content": [{"id": f"f{i}"} for i in range(12)], "last": True}
    return m, appels


def _tableau(schema: dict, lignes: list[dict] = (), nom: str = "") -> int:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    nom = nom or f"fe-{uuid.uuid4().hex[:6]}"
    ns_id = db.create_datastore("user", SUB, nom)
    store = make_store(SUB)
    store.set_schema(nom, schema)
    if lignes:
        store.write_rows(nom, list(lignes))
    return ns_id


def _societes() -> int:
    return _tableau({"key": "slug", "fields": [{"key": "slug", "type": "text"},
                                               {"key": "people_status", "type": "text"}]},
                    [{"slug": s} for s in ("acme", "globex", "initech")])


def _corps(parents: int, **surcharge) -> dict:
    from oto_mcp.recipes import contrat
    corps = {"tool": "acme_people_of", "arguments": {"company": "{{row.slug}}"},
             "for_each": {"datastore": parents, "status_column": "people_status"},
             "source": {"items": "content", "pagination": {
                 "type": "page", "param": "page", "size": 2, "size_param": "size",
                 "last": "last"}},
             "map": {"name": "name"},
             "values": {"company": "{{row.slug}}"},
             "key": {"column": "person_key", "template": "{{row.slug}}::{{item.id}}"},
             "limits": {"max_units": 100}}
    corps.update(surcharge)
    return contrat.valider(corps)


def _executer(m, corps, ns, **kw):
    from oto_mcp.recipes import moteur
    return asyncio.run(moteur.executer(corps, {}, fastmcp=m, sub=SUB, datastore=ns, **kw))


def _lignes(ns) -> list[dict]:
    from oto_mcp.datastore.core import make_store
    return make_store(SUB).cursor_rows(str(ns), limit=100)["rows"]


def _etats(parents) -> dict:
    return {l["slug"]: l.get("people_status") for l in _lignes(parents)}


def test_chaque_ligne_parente_declenche_l_appel_puis_recoit_son_etat(serveur):
    m, appels = serveur
    parents, cible = _societes(), _tableau(CIBLE)
    recu = _executer(m, _corps(parents), cible)
    assert recu["done"] and recu["written"] == 7
    assert recu["parents"] == {"done": 2, "empty": 1, "failed": 0}
    assert recu["skipped_empty_input"] == 0
    assert _etats(parents) == {"acme": "done", "globex": "empty", "initech": "done"}
    assert {l["company"] for l in _lignes(cible)} == {"acme", "initech"}
    # Une exécution suivante ne repaie aucune ligne faite.
    avant = len(appels)
    recu = _executer(m, _corps(parents), cible)
    assert recu["done"] and recu["parents"] == {"done": 0, "empty": 0, "failed": 0}
    assert len(appels) == avant


def test_max_items_per_row_borne_chaque_parent(serveur):
    m, _ = serveur
    parents, cible = _societes(), _tableau(CIBLE)
    corps = _corps(parents, for_each={"datastore": parents, "status_column": "people_status",
                                      "max_items_per_row": 3})
    recu = _executer(m, corps, cible)
    assert recu["done"] and recu["written"] == 5  # 3 chez Acme, 2 chez Initech
    assert _etats(parents)["acme"] == "done"


def test_un_plafond_au_milieu_d_un_parent_le_laisse_en_attente_et_la_reprise_le_finit(serveur):
    m, appels = serveur
    parents, cible = _societes(), _tableau(CIBLE)
    corps = _corps(parents, limits={"max_units": 100, "max_pages": 2})
    recu = _executer(m, corps, cible)
    assert recu["stopped"] == "max_pages" and recu["resume"]
    assert _etats(parents)["acme"] is None  # coupé : toujours en attente
    suite = _executer(m, corps, cible, reprise=recu["resume"])
    # La reprise repart à la page 2 d'Acme, pas à la première.
    assert appels[2] == {"company": "acme", "page": 2}
    while suite.get("resume"):
        suite = _executer(m, corps, cible, reprise=suite["resume"])
    assert suite["done"] and len(_lignes(cible)) == 7
    assert _etats(parents) == {"acme": "done", "globex": "empty", "initech": "done"}


def test_l_epreuve_essaie_plusieurs_parents_et_n_ecrit_rien(serveur):
    """La première ligne parente peut ne rien rendre : l'épreuve passe à la suivante,
    et ni la cible ni l'état des parents ne sont écrits."""
    m, _ = serveur
    parents = _tableau({"key": "slug", "fields": [{"key": "slug", "type": "text"},
                                                  {"key": "people_status", "type": "text"}]},
                       [{"slug": "globex"}, {"slug": "acme"}])
    cible = _tableau(CIBLE)
    recu = _executer(m, _corps(parents), cible, ecrire=False, pages_max=1)
    assert recu["rows_built"] == 2 and recu["stopped"] is None
    assert recu["fill"] == {"name": 1.0}
    assert _lignes(cible) == [] and set(_etats(parents).values()) == {None}


def test_le_tableau_parent_ne_peut_pas_etre_la_cible(serveur):
    from oto_mcp.recipes import moteur
    m, appels = serveur
    parents = _societes()
    with pytest.raises(moteur.RecetteRefusee) as e:
        _executer(m, _corps(parents), parents)
    assert e.value.code == "for_each_same_table" and appels == []


def test_not_in_table_ecarte_par_valeur_normalisee(serveur):
    m, _ = serveur
    parents, cible = _societes(), _tableau(CIBLE)
    exclus = _tableau({"fields": [{"key": "domain", "type": "text"}]},
                      [{"domain": "ACME.test"}])
    corps = _corps(parents, where=[{"path": "site", "op": "not_in_table", "table": exclus,
                                    "column": "domain", "normalize": "domain"}])
    recu = _executer(m, corps, cible)
    assert recu["skipped_where"] == 5 and recu["written"] == 2
    assert {l["company"] for l in _lignes(cible)} == {"initech"}


def test_une_colonne_pleine_a_la_publication_qui_revient_vide_arrete_l_execution(serveur):
    from oto_mcp.recipes import contrat
    m, _ = serveur
    cible = _tableau(CIBLE)
    corps = contrat.valider({"tool": "acme_flat", "source": {"items": "content"},
                             "map": {"name": "name"},
                             "key": {"column": "person_key", "template": "{{item.id}}"},
                             "limits": {"max_units": 100}})
    recu = _executer(m, corps, cible, temoin={"fill": {"name": 1.0}, "rows_built": 10})
    assert recu["stopped"] == "mapping_drift" and recu["drifted_columns"] == ["name"]
    assert _lignes(cible) == []
    # Une colonne clairsemée à la publication n'est pas surveillée.
    recu = _executer(m, corps, cible, temoin={"fill": {"name": 0.4}, "rows_built": 10})
    assert recu["done"] and recu["written"] == 12


def test_le_contrat_de_for_each():
    from oto_mcp.recipes import contrat
    base = {"tool": "acme_people_of", "source": {"items": "content"},
            "map": {"name": "name"}, "key": {"column": "k", "template": "{{row.slug}}"},
            "limits": {"max_units": 10}}
    with pytest.raises(contrat.RecetteInvalide) as e:
        contrat.valider(base)
    assert any("unknown scope(s) ['row']" in p for p in e.value.problemes)
    with pytest.raises(contrat.RecetteInvalide) as e:
        contrat.valider({**base, "for_each": {"datastore": "companies"}})
    assert any("for_each.datastore" in p for p in e.value.problemes)
    assert any("for_each.status_column" in p for p in e.value.problemes)
    ok = contrat.valider({**base, "for_each": {"datastore": 12, "status_column": "st"}})
    # Pas de borne de parents imposée : absente, `max_parents` reste absente.
    assert "max_parents" not in ok["for_each"]
    with pytest.raises(contrat.RecetteInvalide) as e:
        contrat.valider({**base, "for_each": {"datastore": 12, "status_column": "st",
                                              "max_parents": 0}})
    assert any("max_parents" in p for p in e.value.problemes)
    assert contrat.valider({**base, "for_each": {"datastore": 12, "status_column": "st",
                                                 "max_parents": 5000}})["for_each"][
        "max_parents"] == 5000
    with pytest.raises(contrat.RecetteInvalide) as e:
        contrat.valider({**base, "key": {"column": "k", "template": "{{item.id}}"},
                         "where": [{"path": "d", "op": "not_in_table", "table": 3},
                                   {"path": "d", "op": "eq", "value": "x",
                                    "normalize": "rot13"}]})
    assert len(e.value.problemes) == 2


def _parents(slugs) -> int:
    return _tableau({"key": "slug", "fields": [
        {"key": "slug", "type": "text"}, {"key": "tier", "type": "text"},
        {"key": "people_status", "type": "text"}]},
        [{"slug": sl, "tier": t} if sl else {"tier": t} for sl, t in slugs])


def test_le_filtre_et_les_entrees_vides_ecartent_des_parents_sans_appel(serveur):
    m, appels = serveur
    parents = _parents([("acme", "a"), ("initech", "b"), (None, "a")])
    cible = _tableau(CIBLE)
    corps = _corps(parents, for_each={"datastore": parents, "status_column": "people_status",
                                      "filter": {"tier": "a"}})
    recu = _executer(m, corps, cible)
    assert recu["done"] and recu["parents"]["done"] == 1
    # Initech est hors filtre ; la ligne sans `slug` n'est pas sélectionnée.
    assert {a["company"] for a in appels} == {"acme"}
    etats = {(l.get("slug"), l["tier"]): l.get("people_status") for l in _lignes(parents)}
    assert etats == {("acme", "a"): "done", ("initech", "b"): None, (None, "a"): None}
    # La ligne sans `slug` attend encore : le reçu le dit (Initech est hors filtre, pas
    # comptée).
    assert recu["skipped_empty_input"] == 1


def test_un_filtre_refuse_l_est_avant_tout_appel(serveur):
    from oto_mcp.recipes import moteur
    m, appels = serveur
    parents, cible = _societes(), _tableau(CIBLE)
    corps = _corps(parents, for_each={"datastore": parents, "status_column": "people_status",
                                      "filter": {"slug": {"regex": "a.*"}}})
    with pytest.raises(moteur.RecetteRefusee) as e:
        _executer(m, corps, cible)
    assert e.value.code == "invalid_filter" and appels == []


def test_une_ligne_que_l_outil_refuse_est_marquee_et_l_execution_continue(serveur):
    m, _ = serveur
    parents = _parents([("acme", "a"), ("umbrella", "a"), ("initech", "a")])
    cible = _tableau(CIBLE)
    recu = _executer(m, _corps(parents, tool="acme_picky"), cible)
    assert recu["done"] and recu["stopped"] is None
    assert recu["parents"] == {"done": 2, "empty": 0, "failed": 1}
    assert _etats(parents) == {"acme": "done", "umbrella": "failed:invalid_input",
                               "initech": "done"}


def test_une_serie_d_echecs_identiques_arrete_en_marquant_et_nommant_la_serie(serveur):
    m, appels = serveur
    parents = _parents([(f"x{i}", "a") for i in range(5)])
    cible = _tableau(CIBLE)
    recu = _executer(m, _corps(parents, tool="acme_picky"), cible)
    assert recu["stopped"] == "repeated_failure" and len(appels) == 3
    etats = {l["_id"]: l.get("people_status") for l in _lignes(parents)}
    marques = {i for i, e in etats.items() if e == "failed:invalid_input"}
    assert len(marques) == 3 and set(map(str, recu["failed_rows"])) == set(map(str, marques))
    # Une exécution suivante ne bute plus sur les mêmes.
    avant = len(appels)
    _executer(m, _corps(parents, tool="acme_picky"), cible)
    assert {a["company"] for a in appels[avant:]} <= {"x3", "x4"}


def test_des_societes_inconnues_d_affilee_sont_marquees_sans_arreter(serveur, monkeypatch):
    """Un 404 dit « cette ligne n'existe pas chez lui », rien de systémique : trois
    d'affilée ne doivent pas bloquer chaque exécution sur les trois mêmes lignes."""
    from oto_mcp.recipes import moteur
    m, _ = serveur
    vrai = moteur._appeler

    async def introuvable(outil, sub, nom, args):
        issue = await vrai(outil, sub, nom, args)
        if args.get("company", "").startswith("x"):
            issue.ok, issue.code, issue.retryable = False, "not_found", False
        return issue
    monkeypatch.setattr(moteur, "_appeler", introuvable)
    parents = _parents([(f"x{i}", "a") for i in range(4)] + [("acme", "a")])
    recu = _executer(m, _corps(parents), _tableau(CIBLE))
    assert recu["done"] and recu["parents"]["failed"] == 4 and recu["parents"]["done"] == 1


def test_une_entree_qui_se_normalise_en_rien_n_appelle_pas(serveur):
    m, appels = serveur
    parents = _parents([("n/a", "a")])
    corps = _corps(parents, arguments={"company": "{{row.slug|digits}}"})
    recu = _executer(m, corps, _tableau(CIBLE))
    assert appels == [] and _etats(parents)["n/a"] == "failed:invalid_input"


def test_un_jeton_d_une_autre_recette_ou_fabrique_est_refuse(serveur):
    import base64
    import json

    from oto_mcp.recipes import moteur
    m, _ = serveur
    parents, cible = _societes(), _tableau(CIBLE)
    corps = _corps(parents, limits={"max_units": 100, "max_pages": 1})
    recu = _executer(m, corps, cible)
    autre = _corps(parents, limits={"max_units": 99, "max_pages": 1})
    with pytest.raises(moteur.RecetteRefusee):
        _executer(m, autre, cible, reprise=recu["resume"])
    etat = json.loads(base64.urlsafe_b64decode(recu["resume"]))
    etat["u"] = -10_000
    forge = base64.urlsafe_b64encode(json.dumps(etat).encode()).decode()
    assert moteur.lire_reprise(forge, corps)["u"] == 0


def _lents_et_introuvables(monkeypatch, secondes: float):
    import time

    from oto_mcp.recipes import moteur
    from oto_mcp.tools.meta import IssueCible
    vus: list = []

    async def lent(outil, sub, nom, args):
        vus.append(args)
        time.sleep(secondes)
        return IssueCible(ok=False, code="not_found", message="no such company")
    monkeypatch.setattr(moteur, "_appeler", lent)
    return vus


def test_des_appels_rates_comptent_dans_l_horloge(serveur, monkeypatch):
    """Dix parents qui échouent vite, aucune page réussie : le budget d'horloge
    s'applique quand même, et la reprise est rendue."""
    m, _ = serveur
    vus = _lents_et_introuvables(monkeypatch, 0.3)
    parents = _parents([(f"x{i}", "a") for i in range(10)])
    recu = _executer(m, _corps(parents), _tableau(CIBLE), budget_s=0.5)
    assert recu["stopped"] == "time_budget" and recu["resume"]
    assert len(vus) < 10


def test_des_appels_rates_comptent_dans_la_depense(serveur, monkeypatch):
    m, _ = serveur
    vus = _lents_et_introuvables(monkeypatch, 0)
    parents = _parents([(f"x{i}", "a") for i in range(10)])
    corps = _corps(parents, units="calls", limits={"max_units": 2})
    recu = _executer(m, corps, _tableau(CIBLE))
    assert recu["stopped"] == "spend_cap" and len(vus) == 2 and recu["units"] == 2


def test_le_parent_ne_peut_pas_etre_la_cible_meme_par_son_nom(serveur):
    from oto_mcp.recipes import moteur
    m, appels = serveur
    nom = f"fe-{uuid.uuid4().hex[:6]}"
    parents = _tableau({"key": "slug", "fields": [{"key": "slug", "type": "text"}]},
                       [{"slug": "acme"}], nom=nom)
    with pytest.raises(moteur.RecetteRefusee) as e:
        _executer(m, _corps(parents), nom)
    assert e.value.code == "for_each_same_table" and appels == []


def test_une_epreuve_courte_ne_surveille_rien():
    from oto_mcp.recipes import moteur
    assert moteur.surveillees({"fill": {"name": 1.0}, "rows_built": 1}) == []
    assert moteur.surveillees({"fill": {"name": 1.0}, "rows_built": 10}) == ["name"]


def test_la_derive_d_un_parent_le_marque_et_les_suivants_passent(serveur):
    m, _ = serveur
    parents = _parents([("acme", "a"), ("initech", "a"), ("globex", "a")])
    corps = _corps(parents, tool="acme_shape",
                   source={"items": "content", "pagination": {"type": "none"}})
    recu = _executer(m, corps, _tableau(CIBLE), temoin={"fill": {"name": 1.0},
                                                       "rows_built": 12})
    assert recu["done"] and recu["drifted_columns"] == ["name"]
    assert _etats(parents) == {"acme": "failed:mapping_drift", "initech": "done",
                               "globex": "done"}


def test_une_liste_la_ou_la_recette_attend_une_valeur_arrete_nommee(serveur):
    m, _ = serveur
    parents, cible = _societes(), _tableau(CIBLE)
    corps = _corps(parents, where=[{"path": "emails", "op": "eq", "value": "x",
                                    "normalize": "email"}])
    recu = _executer(m, corps, cible)
    assert recu["stopped"] == "non_scalar_value" and _lignes(cible) == []


def test_un_filtre_refuse_ne_declare_pas_la_colonne_d_etat(serveur):
    from oto_mcp.datastore.core import make_store
    from oto_mcp.recipes import moteur
    m, appels = serveur
    nom = f"fe-{uuid.uuid4().hex[:6]}"
    parents = _tableau({"key": "slug", "fields": [{"key": "slug", "type": "text"}]},
                       [{"slug": "acme"}], nom=nom)
    corps = _corps(parents, for_each={"datastore": parents, "status_column": "people_status",
                                      "filter": {"slug": {"regex": "a.*"}}})
    with pytest.raises(moteur.RecetteRefusee) as e:
        _executer(m, corps, _tableau(CIBLE))
    assert e.value.code == "invalid_filter" and appels == []
    cles = {f["key"] for f in make_store(SUB).get_schema(nom).get("fields") or []}
    assert "people_status" not in cles


def test_max_items_per_row_reduit_la_taille_demandee(serveur, monkeypatch):
    from oto_mcp.recipes import moteur
    m, _ = serveur
    vrai, tailles = moteur._appeler, []

    async def note(outil, sub, nom, args):
        tailles.append(args.get("size"))
        return await vrai(outil, sub, nom, args)
    monkeypatch.setattr(moteur, "_appeler", note)
    parents = _parents([("acme", "a")])
    corps = _corps(parents, for_each={"datastore": parents, "status_column": "people_status",
                                      "max_items_per_row": 1})
    recu = _executer(m, corps, _tableau(CIBLE))
    assert recu["written"] == 1 and tailles == [1]


def test_une_lecture_bornee_est_coupee_et_nommee(live):
    from oto_mcp.db import lecture_bornee
    from oto_mcp.db._conn import _connect
    with pytest.raises(lecture_bornee.LectureTropLongue):
        with lecture_bornee.lectures_bornees("banc", 50):
            with _connect() as c:
                c.execute("SELECT pg_sleep(1)")
    # Hors de la portée, rien ne reste posé sur la connexion rendue au pool.
    with _connect() as c:
        assert c.execute("SHOW statement_timeout").fetchone()["statement_timeout"] in ("0",
                                                                                        "0ms")
