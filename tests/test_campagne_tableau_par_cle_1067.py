"""#1067 — une automatisation garde son tableau par son IDENTIFIANT, résolu à la déclaration.

Le dernier pont de #365 : une automatisation (`runner_fleets`) gardait le NOM de son
tableau et le résolvait à chaque lecture, dans la portée de son déclarant. Un homonyme
apparu ensuite — un « vivier » perso devant celui de l'org — captait le compte de
l'ordonnanceur, l'état servi au superviseur et la file de l'agent, sans erreur.

Sur PostgreSQL réel, par la capacité elle-même (`oto_fleet`), l'ordonnanceur
(`lignes_reservables`) et la production d'un travail (`_produire_pour_une_campagne`) :

- un homonyme apparu APRÈS la déclaration ne détourne rien ;
- un nom que deux tableaux portent au même rang est refusé à la déclaration ;
- une automatisation d'avant, qui ne garde qu'un nom, est lue sans rien écrire, puis
  fixée au premier travail — et refusée, jamais tranchée, quand l'ancienne règle et
  celle de la déclaration ne désignent pas le même tableau.
"""
from __future__ import annotations

import json
import uuid

import pytest

from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

EN_FILE = {"statut": "a_enrichir"}
SCHEMA = {"fields": [
    {"key": "nom"},
    {"key": "statut", "role": "status",
     "lifecycle": {"states": ["a_enrichir", "enrichi"],
                   "transitions": {"a_enrichir": ["enrichi"]},
                   "terminal": ["enrichi"]}},
]}


@pytest.fixture(autouse=True)
def _socle(monkeypatch):
    from oto_mcp.capabilities import _lignes_reservables
    from oto_mcp.capabilities import runner_fleets as RF
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    monkeypatch.setattr(_lignes_reservables, "_CACHE", {})
    monkeypatch.setattr(_lignes_reservables, "_SIGNALE", {})


@pytest.fixture
def monde(live):
    """Un déclarant, son org, deux équipes de cette org dont il est membre."""
    from oto_mcp import db, group_store, org_store
    u = uuid.uuid4().hex[:8]
    moi = f"decl_{u}"
    db.upsert_user(moi, email=f"{moi}@example.test")
    org = org_store.create_org(f"org_{u}", created_by=moi)
    org_store.add_org_member(org, moi)
    org_store.set_active_org(moi, org)
    equipes = [group_store.create_group(org, f"g{i}_{u}") for i in (1, 2)]
    for e in equipes:
        group_store.add_group_member(e, moi)
    return {"moi": moi, "org": org, "equipes": equipes, "u": u}


def _tableau(owner: tuple, nom: str, lignes: int) -> int:
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    ns_id = db.create_datastore(owner[0], str(owner[1]), nom)
    db.set_datastore_schema(ns_id, SCHEMA)
    with _connect() as c:
        for i in range(lignes):
            c.execute("INSERT INTO datastore_rows (ns_id, row_id, data) VALUES (%s, %s, %s)",
                      (ns_id, f"l{i}", json.dumps({"nom": str(i), "statut": "a_enrichir"})))
    return ns_id


def _fleets(m, **kw) -> dict:
    from oto_mcp.capabilities import runner_fleets as RF
    if kw.get("op") == "create":
        kw.setdefault("model", "claude-sonnet-5")
    return RF._fleets(ResolvedCtx(sub=m["moi"], org_id=m["org"]), RF.FleetInput(**kw))


def _declarer(m, namespace: str) -> dict:
    return _fleets(m, op="create", label="essai", procedure="p",
                   tools=["data_claim_next"], namespace=namespace,
                   row_filter=EN_FILE)["fleet"]


def _compte(f) -> object:
    from oto_mcp import db
    from oto_mcp.capabilities._lignes_reservables import lignes_reservables
    fraiche = db.get_fleet(int(f["id"]), int(f["org_id"]))
    return lignes_reservables([fraiche], horloge=lambda: 1000.0).get(int(f["id"]), "absente")


def _etat(m, f) -> dict:
    return _fleets(m, op="state", fleet_id=int(f["id"]))["state"]


def _produire(m) -> dict:
    """Arme puis fait produire UN travail à l'org — rend sa charge utile."""
    from oto_mcp import db
    from oto_mcp.capabilities import runner_jobs as RJ
    with db._connect() as c:
        c.execute("UPDATE runner_fleets SET status = 'armed' WHERE org_id = %s", (m["org"],))
    assert RJ._produire_pour_une_campagne(m["org"], 60) is None
    with db._connect() as c:
        row = c.execute("SELECT payload FROM runner_jobs WHERE org_id = %s "
                        "ORDER BY id DESC LIMIT 1", (m["org"],)).fetchone()
    assert row is not None, "aucun travail produit"
    return row["payload"]


# ── à la déclaration ─────────────────────────────────────────────────────────

def test_un_homonyme_apparu_apres_la_declaration_ne_detourne_pas_l_automatisation(monde):
    """LA REPRODUCTION. Déclarée sur le « vivier » de l'org (2 lignes en file) ; puis
    le déclarant se fait un « vivier » perso (5 lignes), mieux classé chez lui. Le
    compte, l'état et le travail suivent le tableau déclaré, pas l'homonyme."""
    nom = f"vivier_{monde['u']}"
    ns_org = _tableau(("org", monde["org"]), nom, 2)
    f = _declarer(monde, str(ns_org))

    ns_perso = _tableau(("user", monde["moi"]), nom, 5)
    assert ns_perso != ns_org

    assert _compte(f) == 2, "l'ordonnanceur compte le tableau déclaré"
    assert _etat(monde, f)["rows"]["total"] == 2, "l'état ventile le tableau déclaré"
    payload = _produire(monde)
    assert payload["datastore_id"] == ns_org
    assert f"`{ns_org}`" in payload["input"] and nom not in payload["input"], \
        "la file de l'agent désigne le tableau par son identifiant"
    assert f["namespace"] == str(ns_org), "gardée par son identifiant"


def test_un_nom_a_la_declaration_est_refuse_et_rien_n_est_ecrit(monde):
    """Même partagé par deux tableaux, un nom ne se résout plus : refus déclaré avant
    toute résolution, qui dit de passer le numéro."""
    from oto_mcp import db
    nom = f"doublon_{monde['u']}"
    for e in monde["equipes"]:
        _tableau(("group", e), nom, 1)
    with pytest.raises(AuthzDenied) as e:
        _declarer(monde, nom)
    assert (e.value.status, e.value.code) == (400, "datastore_by_name")
    assert nom in e.value.message
    assert db.list_fleets(monde["org"]) == []


def test_un_tableau_introuvable_a_la_declaration_est_refuse(monde):
    with pytest.raises(AuthzDenied) as e:
        _declarer(monde, "999999999")
    assert (e.value.status, e.value.code) == (404, "datastore_not_found")


def test_une_declaration_par_identifiant_garde_cet_identifiant(monde):
    ns = _tableau(("org", monde["org"]), f"par_id_{monde['u']}", 1)
    assert _declarer(monde, str(ns))["namespace"] == str(ns)


def test_un_tableau_sorti_de_la_portee_du_declarant_ne_se_lit_plus(monde):
    """Lue par identifiant, la cible reste soumise à la portée du déclarant : un tableau
    d'une autre org ne se compte pas parce qu'on connaît son numéro."""
    from oto_mcp import db, org_store
    from oto_mcp.capabilities._lignes_reservables import tableau_vise
    ailleurs = org_store.create_org(f"ailleurs_{monde['u']}", created_by="tiers")
    ns = _tableau(("org", ailleurs), f"etranger_{monde['u']}", 1)
    f = db.create_fleet(monde["org"], monde["moi"], label="x", procedure="p",
                        tools=["data_claim_next"], namespace=str(ns))
    assert tableau_vise(f) is None
    assert _etat(monde, f)["rows_unavailable"] == "table_not_found"


# ── les automatisations d'avant, qui ne gardent qu'un nom ──────────────────────

def _heritee(m, nom: str) -> dict:
    """Une automatisation telle qu'avant #1067 : le NOM en base, la consigne composée
    par la plateforme sur ce nom."""
    from oto_mcp import db
    from oto_mcp.capabilities import _instruction
    return db.create_fleet(m["org"], m["moi"], label="avant", procedure="p",
                           tools=["data_claim_next"], namespace=nom, row_filter=EN_FILE,
                           input=_instruction.de_file("p", nom, EN_FILE))


def test_une_automatisation_d_avant_se_lit_sans_ecriture_puis_se_fixe_au_premier_travail(monde):
    from oto_mcp import db
    from oto_mcp.capabilities import _instruction
    nom = f"avant_{monde['u']}"
    ns_org = _tableau(("org", monde["org"]), nom, 3)
    f = _heritee(monde, nom)

    assert _compte(f) == 3
    assert _etat(monde, f)["rows"]["total"] == 3
    assert db.get_fleet(int(f["id"]), monde["org"])["namespace"] == nom, \
        "une LECTURE n'écrit pas"

    payload = _produire(monde)
    fixee = db.get_fleet(int(f["id"]), monde["org"])
    assert fixee["namespace"] == str(ns_org)
    assert fixee["input"] == _instruction.de_file("p", str(ns_org), EN_FILE)
    assert payload["datastore_id"] == ns_org and f"`{ns_org}`" in payload["input"]

    _tableau(("user", monde["moi"]), nom, 7)          # l'homonyme, après coup
    assert _compte(fixee) == 3


def test_une_automatisation_d_avant_dont_les_regles_divergent_est_refusee(monde):
    """Équipe et org portent le même nom : l'ancienne règle servait celui de l'org, la
    règle de la déclaration désigne celui de l'équipe. On ne devine pas lequel."""
    from oto_mcp import db
    nom = f"divergent_{monde['u']}"
    _tableau(("org", monde["org"]), nom, 1)
    _tableau(("group", monde["equipes"][0]), nom, 1)
    f = _heritee(monde, nom)

    assert _compte(f) == "absente", "non servie"
    e = _etat(monde, f)
    assert (e["rows"], e["rows_unavailable"]) == (None, "table_ambiguous")

    from oto_mcp.capabilities import _lignes_reservables
    with pytest.raises(_lignes_reservables.TableauAmbigu):
        _lignes_reservables.fixer_le_tableau(f)
    assert db.get_fleet(int(f["id"]), monde["org"])["namespace"] == nom
