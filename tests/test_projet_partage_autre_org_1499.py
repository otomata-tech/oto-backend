"""Projet PARTAGÉ d'une autre org : aucun compte de l'org ni de l'équipe propriétaire
ne sert au bénéficiaire (#1499, D1/D2 — l'héritage de clés #480 et la clé personnelle
venue d'une autre org #172 sont retirés).

Le trou d'origine : `_project=` co-pose l'org propriétaire comme contexte d'appel, et le
barreau ORG de la cascade n'était gardé par aucune appartenance — un bénéficiaire hors
de l'org agissait sous ses clés d'org, sans que personne l'ait décidé.

La règle, depuis la bascule vers les règles de comptes : un compte ne sert qu'aux
principaux de SON org. Hors de l'org propriétaire, ni la clé d'org (D1), ni celle de
l'équipe propriétaire (D2), ni sa propre clé posée dans une AUTRE org (#172). **Iso sur
les trois types de bénéficiaire** (personne, équipe, org) : chaque test de comportement
est paramétré sur les trois, et c'est ce paramétrage qui prouve l'iso.

Exercé en SQL réel (walker, cascade, partage) : la faille vivait dans l'assemblage
— pose de l'axe, contexte, barreau — qu'aucun stub ne rejoue.
"""
from __future__ import annotations

import os
import uuid

import pytest

from oto_mcp import call_axes, session_org

CONNECTEUR = "lemlist"          # byo_user + byo_org, sans palier plateforme
TYPES = ("person", "team", "org")


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_cles480_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name

    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url),
                            ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


@pytest.fixture
def monde(live):
    """Org A (propriétaire du projet, détient une clé d'org, et une équipe T qui détient
    une clé d'équipe) ; org B, où vit le bénéficiaire P, avec une équipe G qui le
    contient. Le partageur est membre des deux (un partage d'équipe exige d'être membre
    de l'org de l'équipe)."""
    from oto_mcp import db, group_store, org_store
    u = uuid.uuid4().hex[:8]
    owner, benef = f"owner_{u}", f"benef_{u}"
    for s in (owner, benef):
        db.upsert_user(s, email=f"{s}@example.test")
    org_a = org_store.create_org(f"a_{u}", created_by=owner)
    org_b = org_store.create_org(f"b_{u}", created_by=benef)
    org_store.add_org_member(org_a, owner, "org_admin")
    org_store.add_org_member(org_b, owner)
    org_store.add_org_member(org_b, benef)
    team = group_store.create_group(org_b, f"g_{u}")
    group_store.add_group_member(team, benef)
    org_store.set_org_secret(org_a, CONNECTEUR, "CLE-DE-L-ORG-A", set_by=owner)
    pid = db.create_project("org", str(org_a), f"p_{u}", created_by=owner)
    equipe_a = group_store.create_group(org_a, f"t_{u}")
    group_store.add_group_member(equipe_a, owner)
    group_store.set_group_secret(equipe_a, CONNECTEUR, "CLE-DE-L-EQUIPE-T", set_by=owner)
    pid_t = db.create_project("group", str(equipe_a), f"pt_{u}", created_by=owner)
    return {"owner": owner, "benef": benef, "org_a": org_a, "org_b": org_b,
            "team": team, "pid": pid, "equipe_a": equipe_a, "pid_t": pid_t}


def _destinataire(m: dict, type_: str) -> dict:
    return {"person": {"email": f"{m['benef']}@example.test"},
            "team": {"group_id": m["team"]},
            "org": {"org_id": m["org_b"]}}[type_]


def _partager(m: dict, type_: str, projet: str = "pid"):
    from oto_mcp.capabilities import resources as R
    from oto_mcp.capabilities._types import ResolvedCtx
    return R._resources(ResolvedCtx(sub=m["owner"], org_id=m["org_a"]),
                        R.ResourceInput(op="share", resource_type="project",
                                        resource_id=str(m[projet]), role="editor",
                                        **_destinataire(m, type_)))


def _resoudre_dans_le_projet(m: dict, monkeypatch, projet: str = "pid"):
    """Ce que P résout, en appelant AVEC `_project=` — la vraie pose de l'axe."""
    import asyncio

    from oto_mcp import access
    monkeypatch.setattr(call_axes, "require_axis_sub", lambda axis: m["benef"])
    monkeypatch.setattr(session_org, "current_subdomain_candidate", lambda: None)
    axe = next(a for a in call_axes.AXES if a.param == "_project")

    async def _appel():
        # Pose et résolution dans le MÊME contexte, comme le middleware d'appel.
        undo = await axe.pin(m[projet])
        try:
            return access.resolve_credential(CONNECTEUR, "byo", sub=m["benef"],
                                             emit_on_failure=False)
        finally:
            for reset, tok in reversed(undo):
                reset(tok)
    return asyncio.run(_appel())


# ── D1/D2 : rouges sur le code d'avant, un par type ────────────────────────────

def _refus(m: dict, monkeypatch, projet: str = "pid"):
    from oto_mcp.mcp_errors import McpError
    with pytest.raises(McpError) as e:
        rc = _resoudre_dans_le_projet(m, monkeypatch, projet)
        pytest.fail(f"clé servie au bénéficiaire : mode={rc.mode} "
                    f"entité={rc.entity_type}:{rc.entity_id}")
    return e.value.error.message


@pytest.mark.parametrize("type_", TYPES)
def test_le_beneficiaire_hors_org_n_atteint_pas_la_cle_d_org_du_proprietaire(
        monde, monkeypatch, type_):
    """D1 : la clé de l'org A ne sert pas au bénéficiaire, qui n'en est pas membre — et
    le refus ne propose plus de « prêt » qui n'existe plus."""
    _partager(monde, type_)
    assert "inherit" not in _refus(monde, monkeypatch)


@pytest.mark.parametrize("type_", TYPES)
def test_ni_la_cle_de_l_equipe_proprietaire(monde, monkeypatch, type_):
    """D2 : un projet de l'équipe T, partagé — ni la clé de T, ni celle de l'org A."""
    _partager(monde, type_, projet="pid_t")
    _refus(monde, monkeypatch, projet="pid_t")


def test_membre_de_l_org_mais_pas_de_l_equipe_l_equipe_ne_sert_pas(monde, monkeypatch):
    """D2 pour un membre de l'org A hors de l'équipe T : la clé d'org lui sert, celle de
    T jamais (le groupe n'est pas co-posé, et seule l'équipe ACTIVE est lue)."""
    from oto_mcp import org_store
    org_store.add_org_member(monde["org_a"], monde["benef"])
    _partager(monde, "person", projet="pid_t")
    rc = _resoudre_dans_le_projet(monde, monkeypatch, projet="pid_t")
    assert (rc.mode, rc.entity_id) == ("org", str(monde["org_a"]))


@pytest.mark.parametrize("type_", TYPES)
def test_sa_cle_d_une_autre_org_le_sert_a_l_etape_5(monde, monkeypatch, type_):
    """#172 à l'étape 5 : dans le projet de l'org A, où rien de local ne le sert, SA clé
    posée chez lui (org B) sert sans nom, et la trace de l'appel dit qu'elle est la
    sienne dans l'org B (l'écho : `your_account_in_org`). Le partage du projet, lui, ne
    lui donne aucune clé de l'org A ni de l'équipe propriétaire."""
    from oto_mcp import db
    db.set_member_api_key(monde["benef"], monde["org_b"], CONNECTEUR, "SA-CLE")
    _partager(monde, type_)
    trace: dict = {}
    tok = session_org.set_call_trace(trace)
    try:
        rc = _resoudre_dans_le_projet(monde, monkeypatch)
    finally:
        session_org.reset_call_trace(tok)
    assert (rc.mode, rc.entity_type, rc.entity_id) == (
        "user", "member", f"{monde['org_b']}:{monde['benef']}")
    assert trace["compte_d_une_autre_org"]["own"] is True
    assert trace["compte_d_une_autre_org"]["org"].startswith("b_")


def test_le_membre_de_l_org_n_est_pas_touche(monde, monkeypatch):
    """Garde-fou : un membre de l'org propriétaire résout la clé d'org comme avant."""
    from oto_mcp import org_store
    org_store.add_org_member(monde["org_a"], monde["benef"])
    rc = _resoudre_dans_le_projet(monde, monkeypatch)
    assert (rc.mode, rc.entity_id) == ("org", str(monde["org_a"]))


# ── Une clé personnelle SUSPENDUE refuse, elle ne passe plus la main ───────────

def test_sa_cle_suspendue_refuse_account_suspended_au_lieu_de_la_cle_d_org(
        monde, monkeypatch):
    """Membre de l'org A, sa clé personnelle y est suspendue : l'appel est refusé par
    son nom (`account_suspended`), jamais servi par la clé d'org en dessous."""
    from oto_mcp import access, credentials_store, db, org_store
    from oto_mcp.access import niveaux
    from oto_mcp.mcp_errors import McpError
    m = monde
    org_store.add_org_member(m["org_a"], m["benef"])
    db.set_member_api_key(m["benef"], m["org_a"], CONNECTEUR, "SA-CLE")
    assert credentials_store.update_meta(
        credentials_store.MEMBER, credentials_store.member_id(m["org_a"], m["benef"]),
        CONNECTEUR, "", {"suspended": "true"})
    monkeypatch.setattr(access, "current_org", lambda sub: m["org_a"])
    monkeypatch.setattr(access, "current_group", lambda sub: None)
    with pytest.raises(McpError) as e:
        rc = access.resolve_credential(CONNECTEUR, "byo", sub=m["benef"],
                                       emit_on_failure=False)
        pytest.fail(f"clé servie malgré la suspension : {rc.mode}:{rc.entity_id}")
    assert (e.value.error.data or {}).get("code") == niveaux.CODE_COMPTE_SUSPENDU
