"""Ce que le pont du bac à sable a le droit d'appeler (#1202, lot 1) : un outil de
connecteur DÉCLARÉ EN LECTURE, sous les gardes d'`oto_call` — et rien d'autre. Une
recette-code y ajoute `data_rows` et `data_write` (lot 2) : des appels ORDINAIRES de ces
outils, gardés et journalisés comme les autres.

Sur un vrai store (`live`) : `meta.executer_cible` lit la base (axes, org du run). Le
dernier banc joue aussi le vrai bac à sable, et passe sans lui en le disant."""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Optional

import pytest

SUB = "sub-recettes-pont"


@pytest.fixture(scope="module")
def compte(live):
    from oto_mcp import db
    db.upsert_user(SUB, email=f"{SUB}@acme.test", name=SUB)
    return SUB


@pytest.fixture
def serveur(compte, monkeypatch):
    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.auth import hooks
    from oto_mcp.tools.lecture import LECTURE
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: SUB)
    monkeypatch.setattr(hooks, "current_user_sub_from_token", lambda: SUB)
    appels: list[str] = []
    m = FastMCP("t-recettes-pont")

    @m.tool(annotations=LECTURE)
    def acme_people(company: str = "") -> dict:
        appels.append("acme_people")
        return {"content": [{"name": f"{company} {i}"} for i in range(2)]}

    @m.tool()
    def acme_post(text: str = "") -> dict:
        appels.append("acme_post")
        return {"posted": True}

    @m.tool(annotations=LECTURE)
    def acme_comptes() -> dict:
        """Un connecteur à DEUX comptes, sans défaut : la résolution lève comme
        `access.resolve._pick_account` — ambigu sans `_account`, introuvable pour un nom
        qu'il n'a pas. Synchrone, comme un outil de connecteur : il joue dans un fil."""
        from mcp.types import INVALID_PARAMS, ErrorData

        from oto_mcp import session_org
        appels.append("acme_comptes")
        nomme = session_org.current_call_account()
        if nomme is None:
            raise access.CompteAmbigu(ErrorData(
                code=INVALID_PARAMS, message="Multiple comptes `acme` (`un`, `deux`), with "
                "no single default — pass `_account=\"<name>\"` on this call."))
        if nomme not in ("un", "deux"):
            raise access.CompteIntrouvable(ErrorData(
                code=INVALID_PARAMS, message=f"Compte `{nomme}` not found for `acme`."),
                nomme)
        return {"compte": nomme}

    @m.tool(annotations=LECTURE)
    async def acme_lent(marque: str = "") -> dict:
        appels.append("acme_lent")
        await asyncio.sleep(5)
        return {"fini": marque}
    return m, appels


def _appeler(m, outil, arguments):
    from oto_mcp.recipes import pont
    return asyncio.run(pont.appeleur_en_lecture(m, SUB)(outil, arguments))


def test_un_outil_en_lecture_est_appele_et_son_resultat_rendu(serveur):
    m, appels = serveur
    reponse = _appeler(m, "acme_people", {"company": "acme"})
    assert reponse["ok"] and reponse["result"]["content"][0]["name"] == "acme 0"
    assert appels == ["acme_people"]


def test_un_outil_qui_ecrit_est_refuse_avant_tout_appel(serveur):
    m, appels = serveur
    reponse = _appeler(m, "acme_post", {"text": "x"})
    assert reponse == {"ok": False, "code": "recipe_tool_not_read_only",
                       "message": reponse["message"]}
    assert appels == []


def test_un_outil_inconnu_ou_de_plateforme_est_refuse(serveur):
    m, appels = serveur
    assert _appeler(m, "acme_nope", {})["code"] == "unknown_tool"
    assert _appeler(m, "data_write", {})["code"] == "tool_not_allowed"
    assert appels == []


def test_des_arguments_refuses_par_l_outil_ne_levent_pas(serveur):
    m, appels = serveur
    reponse = _appeler(m, "acme_people", {"company": {"pas": "une chaîne"}})
    assert reponse["ok"] is False and reponse["code"] in ("call_refused", "invalid_input")


def test_de_bout_en_bout_le_code_lit_un_outil_et_ne_peut_pas_en_ecrire(serveur):
    from oto_mcp.functions import executor, pont as pont_bac
    from oto_mcp.recipes import pont
    if not os.environ.get(executor.ENV_DIR):
        if os.environ.get("CI"):
            pytest.fail(f"{executor.ENV_DIR} absente")
        pytest.skip(f"{executor.ENV_DIR} absente : pas de bac à sable sur ce poste")
    m, appels = serveur
    sortie = asyncio.run(pont_bac.executer_avec_pont(
        sources={"r.py": (
            "async def run(oto, entree):\n"
            "    lu = await oto.call('acme_people', {'company': 'acme'})\n"
            "    try:\n"
            "        await oto.call('acme_post', {'text': 'x'})\n"
            "        ecrit = 'oui'\n"
            "    except oto.ToolError as e:\n"
            "        ecrit = e.code\n"
            "    return {'lus': len(lu['content']), 'ecrit': ecrit}\n")},
        entrypoint="r:run", appeler=pont.appeleur_en_lecture(m, SUB)))
    assert sortie["result"] == {"lus": 2, "ecrit": "recipe_tool_not_read_only"}
    assert appels == ["acme_people"]


# ── recette-code : les tableaux passent par le chemin ordinaire des outils ───────────

SCHEMA = {"key": "slug", "fields": [{"key": "slug", "type": "text"},
                                    {"key": "name", "type": "text"}]}


@pytest.fixture
def tableaux(serveur, monkeypatch):
    """Le serveur, plus les VRAIS outils `data_*` ; l'acteur lu là où ils le lisent."""
    from oto_mcp import access, call_axes
    from oto_mcp.tools import datastore as outils_tableaux
    m, _ = serveur
    outils_tableaux.register(m)
    monkeypatch.setattr(access, "current_user_sub_from_token", lambda: SUB)
    monkeypatch.setattr(call_axes, "current_user_sub_from_token", lambda: SUB)
    return m


def _table() -> str:
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = f"recettes-pont-{uuid.uuid4().hex[:6]}"
    db.create_datastore("user", SUB, ns)
    make_store(SUB).set_schema(ns, SCHEMA)
    return ns


def _lignes(ns: str) -> list:
    from oto_mcp.datastore.core import make_store
    return make_store(SUB).cursor_rows(ns, limit=100)["rows"]


def _journal(outil: str, ns: str) -> list:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT ok FROM tool_calls WHERE sub = %s AND tool = %s "
            "AND args->>'datastore' = %s", (SUB, outil, ns)).fetchall()


def _de_recette(m, ns, outil, arguments):
    from oto_mcp.recipes import pont
    return asyncio.run(pont.appeleur_de_recette(m, SUB, datastore=ns, ecrire=True)(
        outil, arguments))


def test_data_write_du_pont_laisse_sa_ligne_dans_le_journal(tableaux):
    ns = _table()
    reponse = _de_recette(tableaux, ns, "data_write",
                          {"rows": [{"slug": "a", "name": "A"}], "key": "slug"})
    assert reponse["ok"] and reponse["result"]["inserted"] == 1
    assert [r["slug"] for r in _lignes(ns)] == ["a"]
    assert [r["ok"] for r in _journal("data_write", ns)] == [True]


def test_data_write_du_pont_est_refuse_quand_la_garde_de_l_outil_refuse(tableaux,
                                                                        monkeypatch):
    # Une org suspendue : la garde des outils de plateforme (`activation_gate`) refuse
    # `data_write`. Un accès direct au store, à côté d'`executer_cible`, ne la voyait pas.
    from oto_mcp import org_suspension
    monkeypatch.setattr(org_suspension, "refus", lambda org: "This organization is "
                                                             "suspended.")
    ns = _table()
    reponse = _de_recette(tableaux, ns, "data_write",
                          {"rows": [{"slug": "a", "name": "A"}], "key": "slug"})
    assert reponse["ok"] is False and reponse["code"] == "call_refused"
    assert org_suspension.CODE in reponse["message"]
    assert _lignes(ns) == []


def test_data_rows_du_pont_est_un_appel_journalise_et_son_refus_aussi(tableaux):
    from oto_mcp.datastore.core import make_store
    ns = _table()
    make_store(SUB).write_rows(ns, [{"slug": "a", "name": "A"}], key="slug")
    page = _de_recette(tableaux, ns, "data_rows", {"datastore": ns, "limit": 10})
    assert page["ok"] and [r["slug"] for r in page["result"]["rows"]] == ["a"]
    assert [r["ok"] for r in _journal("data_rows", ns)] == [True]
    absent = f"absent-{uuid.uuid4().hex[:6]}"
    refus = _de_recette(tableaux, ns, "data_rows", {"datastore": absent})
    assert refus["ok"] is False
    assert [r["ok"] for r in _journal("data_rows", absent)] == [False]


def test_data_write_du_pont_n_ecrit_que_dans_le_tableau_de_l_execution(tableaux):
    ns, autre = _table(), _table()
    reponse = _de_recette(tableaux, ns, "data_write",
                          {"datastore": autre, "rows": [{"slug": "x"}], "key": "slug"})
    assert reponse["code"] == "write_outside_target"
    assert _lignes(autre) == [] and _journal("data_write", autre) == []


# ── lot 3 : les écritures chez un tiers, par la liste fermée ──────────────────────────

@pytest.fixture
def crm(serveur, monkeypatch):
    """Un faux CRM inscrit dans la liste fermée : `search` lit, `create` écrit."""
    from oto_mcp.recipes import ecritures as ec
    m, appels = serveur
    monkeypatch.setitem(ec.ECRITURES, "acme_crm", ec.Ecriture(
        frozenset({"search"}), frozenset({"create"})))

    @m.tool()
    def acme_crm(op: str = "search", name: str = "", items: Optional[list] = None,
                 ids: Optional[list] = None) -> dict:
        lot = items if items is not None else ids
        appels.append(f"acme_crm:{op}" + (f"x{len(lot)}" if lot is not None else ""))
        return {"op": op, "id": f"rec-{name}"}
    return m, appels


def _ecrire(m, outil, arguments, *, ecrire=True, declarees=("acme_crm:create",),
            max_ecritures=5, compteur=None):
    from oto_mcp.recipes import pont
    return asyncio.run(pont.appeleur_de_recette(
        m, SUB, datastore=None, ecrire=ecrire, ecritures=frozenset(declarees),
        max_ecritures=max_ecritures, compteur=compteur)(outil, arguments))


def test_une_op_qui_lit_passe_sans_declaration_meme_en_epreuve(crm):
    m, appels = crm
    reponse = _ecrire(m, "acme_crm", {"op": "search", "name": "a"}, ecrire=False,
                      declarees=())
    assert reponse["ok"] and appels == ["acme_crm:search"]


def test_une_ecriture_non_declaree_n_appelle_rien(crm):
    m, appels = crm
    assert _ecrire(m, "acme_crm", {"op": "create"}, declarees=())["code"] == \
        "write_not_declared"
    assert appels == []


def test_une_ecriture_declaree_n_est_jamais_jouee_en_epreuve(crm):
    m, appels = crm
    assert _ecrire(m, "acme_crm", {"op": "create"}, ecrire=False)["code"] == "write_in_test"
    assert appels == []


def test_une_ecriture_declaree_est_appelee_comptee_et_journalisee(crm):
    from oto_mcp.db._conn import _connect
    from oto_mcp.recipes import pont
    m, appels = crm
    compteur = pont.compteur_neuf()
    reponse = _ecrire(m, "acme_crm", {"op": "create", "name": "a"}, compteur=compteur)
    assert reponse["ok"] and reponse["result"]["id"] == "rec-a"
    assert appels == ["acme_crm:create"]
    assert compteur == {"writes": {"acme_crm:create": 1}, "total": 1, "envois": 0,
                        "stopped": None}
    with _connect() as conn:
        lignes = conn.execute("SELECT ok FROM tool_calls WHERE sub = %s AND tool = %s "
                              "AND args->>'op' = 'create'", (SUB, "acme_crm")).fetchall()
    assert [r["ok"] for r in lignes][-1:] == [True]


def test_une_op_absente_ou_destructrice_est_refusee(crm):
    m, appels = crm
    assert _ecrire(m, "acme_crm", {"name": "a"})["code"] == "write_op_not_allowed"
    assert _ecrire(m, "acme_crm", {"op": "delete"})["code"] == "write_op_not_allowed"
    assert appels == []


def test_une_ecriture_reste_dans_l_org_du_run(crm):
    m, appels = crm
    for axe in ("_org", "_group", "_project"):
        assert _ecrire(m, "acme_crm", {"op": "create", axe: 1})["code"] == \
            "write_outside_run_org"
    assert appels == []


def test_le_plafond_d_ecritures_arrete_les_suivantes(crm):
    from oto_mcp.recipes import pont
    m, appels = crm
    compteur = pont.compteur_neuf()
    assert _ecrire(m, "acme_crm", {"op": "create"}, max_ecritures=1, compteur=compteur)["ok"]
    refus = _ecrire(m, "acme_crm", {"op": "create"}, max_ecritures=1, compteur=compteur)
    assert refus["code"] == "budget_exhausted"
    assert compteur["stopped"] == "max_writes" and compteur["total"] == 1
    # Une lecture du même outil n'est pas une écriture : le plafond ne la touche pas.
    assert _ecrire(m, "acme_crm", {"op": "search"}, max_ecritures=1, compteur=compteur)["ok"]
    assert appels == ["acme_crm:create", "acme_crm:search"]


def test_un_lot_est_compte_fiche_par_fiche(crm):
    from oto_mcp.recipes import pont
    m, appels = crm
    compteur = pont.compteur_neuf()
    lot = [{"name": str(i)} for i in range(50)]
    assert _ecrire(m, "acme_crm", {"op": "create", "items": lot}, max_ecritures=100,
                   compteur=compteur)["ok"]
    assert _ecrire(m, "acme_crm", {"op": "create", "ids": ["a", "b"]}, max_ecritures=100,
                   compteur=compteur)["ok"]
    assert appels == ["acme_crm:createx50", "acme_crm:createx2"]
    assert compteur == {"writes": {"acme_crm:create": 52}, "total": 52, "envois": 0,
                        "stopped": None}


def test_un_lot_mal_forme_ou_trop_gros_n_appelle_rien(crm):
    m, appels = crm
    for lot in ([], "x", [{}] * 51):
        assert _ecrire(m, "acme_crm", {"op": "create", "items": lot},
                       max_ecritures=1000)["code"] == "invalid_call"
    assert appels == []


def test_un_lot_qui_ne_tient_pas_dans_le_reste_n_est_pas_joue(crm):
    from oto_mcp.recipes import pont
    m, appels = crm
    compteur = pont.compteur_neuf()
    assert _ecrire(m, "acme_crm", {"op": "create", "items": [{}] * 3}, max_ecritures=5,
                   compteur=compteur)["ok"]
    refus = _ecrire(m, "acme_crm", {"op": "create", "items": [{}] * 3}, max_ecritures=5,
                    compteur=compteur)
    assert refus["code"] == "budget_exhausted" and "2 write(s) left" in refus["message"]
    assert compteur["stopped"] == "max_writes" and compteur["total"] == 3
    # Redécoupé dans ce qui reste, il passe.
    assert _ecrire(m, "acme_crm", {"op": "create", "items": [{}] * 2}, max_ecritures=5,
                   compteur=compteur)["ok"]
    assert compteur["total"] == 5
    assert appels == ["acme_crm:createx3", "acme_crm:createx2"]


def test_des_ecritures_en_parallele_ne_depassent_pas_le_plafond(monkeypatch):
    """Le pont sert les appels en parallèle (27/10/2026) : trois écritures lancées
    ensemble sous `max_writes=2` — deux jouées, une refusée. Le reste se RÉSERVE avant le
    premier `await` ; jugé avant et compté après, les trois passaient. Sans base : seul
    le compte est éprouvé, l'outil et sa résolution sont des doublures qui cèdent la main."""
    from oto_mcp.recipes import ecritures as ec
    from oto_mcp.recipes import pont
    from oto_mcp.tools import meta
    monkeypatch.setitem(ec.ECRITURES, "acme_crm", ec.Ecriture(
        frozenset({"search"}), frozenset({"create"})))
    joues = []

    async def resoudre(fastmcp, outil):
        await asyncio.sleep(0)
        return object()

    async def executer(tool, sub, outil, arguments):
        await asyncio.sleep(0.01)
        joues.append(arguments["name"])
        return {"ok": True, "result": {}}

    monkeypatch.setattr(meta, "resoudre_outil", resoudre)
    monkeypatch.setattr(pont, "_executer", executer)
    compteur = pont.compteur_neuf()
    appeler = pont.appeleur_de_recette(
        None, SUB, datastore=None, ecrire=True, ecritures=frozenset({"acme_crm:create"}),
        max_ecritures=2, compteur=compteur)

    async def ensemble():
        return await asyncio.gather(*(appeler("acme_crm", {"op": "create", "name": n})
                                      for n in "abc"))

    reponses = asyncio.run(ensemble())
    assert sorted(r.get("code", "ok") for r in reponses) == ["budget_exhausted", "ok", "ok"]
    assert len(joues) == 2
    assert compteur["total"] == 2 and compteur["writes"] == {"acme_crm:create": 2}
    assert compteur["stopped"] == "max_writes"


def test_une_ecriture_dont_l_outil_manque_rend_sa_reservation(monkeypatch):
    """Réservée avant la résolution de l'outil : un outil absent de l'instance n'a rien
    tenté, sa place est rendue et le reçu ne le compte pas."""
    from oto_mcp.recipes import ecritures as ec
    from oto_mcp.recipes import pont
    from oto_mcp.tools import meta
    monkeypatch.setitem(ec.ECRITURES, "acme_crm", ec.Ecriture(
        frozenset({"search"}), frozenset({"create"})))

    async def absent(fastmcp, outil):
        return None

    monkeypatch.setattr(meta, "resoudre_outil", absent)
    compteur = pont.compteur_neuf()
    reponse = asyncio.run(pont.appeleur_de_recette(
        None, SUB, datastore=None, ecrire=True, ecritures=frozenset({"acme_crm:create"}),
        max_ecritures=2, compteur=compteur)("acme_crm", {"op": "create", "name": "a"}))
    assert reponse["code"] == "unknown_tool"
    assert compteur == {"writes": {}, "total": 0, "envois": 0, "stopped": None}


def test_un_outil_hors_connecteur_reste_refuse(crm):
    """Un outil qui n'est d'aucun connecteur (ici, `acme` n'en est pas un) : la lecture
    le refuse en le nommant, comme avant la règle générale."""
    m, appels = crm
    assert _ecrire(m, "acme_post", {"text": "x"})["code"] == "recipe_tool_not_read_only"
    assert appels == []


@pytest.fixture
def connecteur(serveur, monkeypatch):
    """Un faux connecteur HORS de la liste forcée (28/10/2026) : la règle générale le
    classe par le verbe de son op — `list` lit, `create` écrit, `send` envoie,
    `delete` détruit."""
    from oto_mcp.recipes import ecritures as ec
    m, appels = serveur
    monkeypatch.setattr(ec, "_est_connecteur", lambda ns: ns == "acme")

    @m.tool()
    def acme_act(op: str, text: str = "", ids: Optional[list] = None) -> dict:
        appels.append(f"acme_act:{op}" + (f"x{len(ids)}" if ids is not None else ""))
        return {"op": op}

    @m.tool()
    def acme_post_message(text: str = "") -> dict:
        appels.append("acme_post_message")
        return {"ok": True}
    return m, appels


def _agir(m, outil, arguments, *, ecrire=True, declarees=(), max_ecritures=5,
          max_envois=None, compteur=None):
    from oto_mcp.recipes import pont
    return asyncio.run(pont.appeleur_de_recette(
        m, SUB, datastore=None, ecrire=ecrire, ecritures=frozenset(declarees),
        max_ecritures=max_ecritures, max_envois=max_envois,
        compteur=compteur)(outil, arguments))


def test_regle_generale_une_lecture_passe_sans_declaration_meme_en_epreuve(connecteur):
    m, appels = connecteur
    assert _agir(m, "acme_act", {"op": "list"}, ecrire=False)["ok"]
    assert appels == ["acme_act:list"]


def test_regle_generale_une_ecriture_declaree_est_appelee_et_comptee(connecteur):
    from oto_mcp.recipes import pont
    m, appels = connecteur
    compteur = pont.compteur_neuf()
    assert _agir(m, "acme_act", {"op": "create", "text": "x"},
                 declarees=("acme_act:create",), compteur=compteur)["ok"]
    assert appels == ["acme_act:create"]
    assert compteur["writes"] == {"acme_act:create": 1} and compteur["envois"] == 0


def test_regle_generale_une_ecriture_non_declaree_ou_en_epreuve_n_appelle_rien(connecteur):
    m, appels = connecteur
    assert _agir(m, "acme_act", {"op": "create"})["code"] == "write_not_declared"
    assert _agir(m, "acme_act", {"op": "create"}, ecrire=False,
                 declarees=("acme_act:create",))["code"] == "write_in_test"
    assert appels == []


@pytest.mark.parametrize("op", ["delete", "merge", "bulk_update", "archive"])
def test_regle_generale_une_op_qui_detruit_est_refusee(connecteur, op):
    m, appels = connecteur
    reponse = _agir(m, "acme_act", {"op": op}, declarees=(f"acme_act:{op}",))
    assert reponse["code"] == "write_op_not_allowed"
    assert appels == []


def test_regle_generale_l_op_d_un_outil_multiplexe_est_litterale(connecteur):
    m, appels = connecteur
    assert _agir(m, "acme_act", {})["code"] == "write_op_not_allowed"
    assert appels == []


def test_regle_generale_un_outil_sans_op_se_classe_par_son_nom(connecteur):
    """`acme_post_message` : le verbe `post`, un envoi, déclaré sous son nom seul."""
    from oto_mcp.recipes import pont
    m, appels = connecteur
    compteur = pont.compteur_neuf()
    assert _agir(m, "acme_post_message", {"text": "x"},
                 declarees=("acme_post_message",), compteur=compteur)["ok"]
    assert appels == ["acme_post_message"] and compteur["envois"] == 1


def test_regle_generale_max_sends_borne_les_envois(connecteur):
    from oto_mcp.recipes import pont
    m, appels = connecteur
    compteur = pont.compteur_neuf()
    declarees = ("acme_act:send", "acme_act:create")
    assert _agir(m, "acme_act", {"op": "send", "ids": ["a", "b"]}, declarees=declarees,
                 max_envois=2, compteur=compteur)["ok"]
    refus = _agir(m, "acme_act", {"op": "send", "text": "x"}, declarees=declarees,
                  max_envois=2, compteur=compteur)
    assert refus["code"] == "budget_exhausted" and compteur["stopped"] == "max_sends"
    # Une écriture qui n'envoie pas reste permise : seul `max_writes` la borne.
    assert _agir(m, "acme_act", {"op": "create"}, declarees=declarees, max_envois=2,
                 compteur=compteur)["ok"]
    assert appels == ["acme_act:sendx2", "acme_act:create"]


def test_un_appel_annule_en_vol_n_est_ni_regle_ni_journalise_comme_un_succes(serveur,
                                                                          monkeypatch):
    """Le pont qui ferme (fin du code, délai) annule les appels encore en vol : l'appel
    annulé n'a pas abouti. `executer_cible` laissait `ok` vrai — réglé par
    `regler_appel` et journalisé réussi."""
    from oto_mcp import access
    from oto_mcp.db._conn import _connect
    from oto_mcp.recipes import pont
    m, appels = serveur
    regles = []
    monkeypatch.setattr(access, "regler_appel", lambda *a, **k: regles.append(a))
    marque = uuid.uuid4().hex

    async def jouer():
        tache = asyncio.create_task(
            pont.appeleur_en_lecture(m, SUB)("acme_lent", {"marque": marque}))
        while "acme_lent" not in appels:
            await asyncio.sleep(0.01)
        tache.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tache

    asyncio.run(jouer())
    assert regles == []
    with _connect() as conn:
        lignes = conn.execute(
            "SELECT ok FROM tool_calls WHERE sub = %s AND tool = 'acme_lent' "
            "AND args->>'marque' = %s", (SUB, marque)).fetchall()
    assert [r["ok"] for r in lignes] == [False]


def test_un_compte_a_nommer_est_un_refus_nomme_dans_une_recette(serveur, monkeypatch):
    """Deux comptes, aucun nommé : le code de la recette reçoit `account_required` (la
    CLASSE `CompteAmbigu`, plus un `invalid_input` générique) et le message dit le geste ;
    un nom absent, de même ; `_account=` d'un compte qui existe passe."""
    from oto_mcp import call_axes
    d_origine = call_axes.accepts_account_axis
    monkeypatch.setattr(call_axes, "accepts_account_axis",
                        lambda name: name == "acme_comptes" or d_origine(name))
    m, appels = serveur
    ambigu = _appeler(m, "acme_comptes", {})
    assert (ambigu["ok"], ambigu["code"]) == (False, "account_required")
    assert "_account=" in ambigu["message"]
    introuvable = _appeler(m, "acme_comptes", {"_account": "trois"})
    assert (introuvable["ok"], introuvable["code"]) == (False, "account_required")
    assert "_account=" in introuvable["message"]
    nomme = _appeler(m, "acme_comptes", {"_account": "deux"})
    assert nomme["ok"] and nomme["result"] == {"compte": "deux"}
    assert appels == ["acme_comptes"] * 3
