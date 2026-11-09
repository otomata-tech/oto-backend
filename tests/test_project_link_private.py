"""`op=link make_private=true` rend privé ce qu'un projet PRIVÉ lie (`_liens_prives`).

Le défaut constaté : un projet possédé par une personne et partagé à deux collègues
liait des procédures et des tableaux possédés par l'org. Un troisième membre de l'org,
sans aucun partage du projet, les lisait tous par le catalogue de l'org. Le projet
était privé ; ce qu'il liait ne l'était pas, et rien ne le disait.

Sur demande seulement : sans `make_private`, `op=link` ne change aucune propriété. Le
transfert et les partages tiennent dans une transaction (`db.transaction_d_ecriture`).

Contre un vrai PostgreSQL (boot `init_db`) et par le chemin SERVI (`me.project`, autz
déclarée puis handler) : le transfert, les partages et la sonde de slug sont du SQL.
Chaque test pose ses propres cibles.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from oto_mcp.capabilities._types import RawCtx

ADMIN, COLLEGUE, TIERS, AUTRE = "u-admin", "u-collegue", "u-tiers", "u-autre"


@pytest.fixture(scope="module")
def monde(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_prive_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    dbconn._pool = None
    try:
        from oto_mcp import db, org_store
        from oto_mcp.db import init_db
        init_db()
        org = org_store.create_org("Acme", created_by=ADMIN)
        org_store.add_org_member(org, ADMIN, "org_admin")
        for m in (COLLEGUE, TIERS, AUTRE):
            org_store.add_org_member(org, m, "org_member")
        org_store.set_active_org(ADMIN, org)
        yield {"org": org}
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


def _projet(monde, *, partage_org: bool = False) -> int:
    from oto_mcp import db, ownership
    pid = int(db.create_project("user", ADMIN, "Ventes " + uuid.uuid4().hex[:6],
                                created_by=ADMIN, context_org_id=monde["org"]))
    ownership.grant("project", str(pid), "user", COLLEGUE, role="editor", granted_by=ADMIN)
    if partage_org:
        ownership.grant("project", str(pid), "org", str(monde["org"]), role="viewer",
                        granted_by=ADMIN)
    return pid


def _procedure(monde) -> tuple[str, str]:
    from oto_mcp import org_store
    slug = "proc-" + uuid.uuid4().hex[:8]
    org_store.set_instruction("org", monde["org"], slug, "1. Une étape",
                              title=slug, set_by=ADMIN)
    return str(org_store.get_instruction("org", monde["org"], slug)["id"]), slug


def _tableau(monde) -> str:
    from oto_mcp import db
    return str(db.create_datastore("org", str(monde["org"]),
                                   "t-" + uuid.uuid4().hex[:8]))


def _link(pid: int, target_type: str, ref: str, sub: str = ADMIN, **args) -> dict:
    """`op=link` par le chemin servi ; `make_private=True` sauf `make_private=None`."""
    args.setdefault("make_private", True)
    if args["make_private"] is None:
        del args["make_private"]
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "me.project")
    inp = cap.Input(op="link", project_id=pid, target_type=target_type,
                    target_ref=ref, **args)
    return cap.handler(cap.authz(RawCtx(sub=sub), inp), inp)


def _lit(sub: str, rtype: str, ref: str) -> bool:
    from oto_mcp import ownership
    return ownership.can_access(sub, rtype, ref, "read")


PROC, TAB = "doctrine", "datastore_namespace"


# ── Par défaut, op=link ne change aucune propriété ───────────────────────────

@pytest.mark.parametrize("cible", ["procedure", "tableau"])
def test_sans_make_private_le_link_ne_touche_a_rien(monde, cible):
    from oto_mcp import ownership
    pid = _projet(monde)
    ref = _procedure(monde)[0] if cible == "procedure" else _tableau(monde)
    rtype = PROC if cible == "procedure" else TAB

    out = _link(pid, cible, ref, make_private=None)

    assert out["link_status"] == "created"
    assert "privacy" not in out
    assert ownership.owner_of(rtype, ref) == ("org", str(monde["org"]))
    assert _lit(TIERS, rtype, ref), "un membre de l'org a perdu la cible sans l'avoir demandé"
    assert ownership.list_grants(rtype, ref) == []


def test_make_private_false_ne_touche_a_rien(monde):
    from oto_mcp import ownership
    pid = _projet(monde)
    ref, _ = _procedure(monde)
    out = _link(pid, "procedure", ref, make_private=False)
    assert "privacy" not in out
    assert ownership.owner_of(PROC, ref)[0] == "org"


def test_make_private_sur_un_connecteur_est_refuse(monde):
    from oto_mcp import db
    from oto_mcp.capabilities._types import AuthzDenied
    pid = _projet(monde)
    with pytest.raises(AuthzDenied) as e:
        _link(pid, "connecteur", "hunter")
    assert e.value.code == "make_private_not_applicable"
    assert db.list_project_links(pid) == []


# ── make_private=true : le défaut, corrigé ───────────────────────────────────

def test_une_procedure_d_org_liee_a_un_projet_prive_devient_privee(monde):
    from oto_mcp import ownership
    pid = _projet(monde)
    ref, _ = _procedure(monde)
    assert _lit(TIERS, PROC, ref), "prémisse : un membre lit une procédure d'org"

    out = _link(pid, "procedure", ref)

    assert out["privacy"]["made_private"] is True
    assert ownership.owner_of(PROC, ref) == ("user", ADMIN)
    assert not _lit(TIERS, PROC, ref), "un membre hors du projet la lit encore"
    assert _lit(COLLEGUE, PROC, ref), "le bénéficiaire du projet l'a perdue"
    assert out["privacy"]["shared_with"] == [
        {"principal_type": "user", "principal_id": COLLEGUE, "role": "viewer"}]


def test_le_beneficiaire_lit_la_procedure_par_le_chemin_servi(monde):
    """Pas seulement le seam : `oto_procedure op=get guide_id=…`, comme un agent ou le
    front, sert le corps au bénéficiaire du projet et le refuse au membre hors projet."""
    from oto_mcp.capabilities.registry import CAPABILITIES
    from oto_mcp import org_store
    pid = _projet(monde)
    ref, slug = _procedure(monde)
    _link(pid, "procedure", ref)
    cap = next(c for c in CAPABILITIES if c.key == "org.guide.get")
    for m in (COLLEGUE, TIERS):
        org_store.set_active_org(m, monde["org"])
    inp = cap.Input(guide_id=int(ref))
    lu = asyncio.run(cap.handler(cap.authz(RawCtx(sub=COLLEGUE), inp), inp))
    assert "1. Une étape" in str(lu)
    with pytest.raises(Exception, match=f"No guide #{ref}"):
        asyncio.run(cap.handler(cap.authz(RawCtx(sub=TIERS), inp), inp))


def test_un_tableau_d_org_lie_a_un_projet_prive_devient_prive(monde):
    from oto_mcp import db, ownership
    pid = _projet(monde)
    ref = _tableau(monde)

    out = _link(pid, "tableau", ref)

    assert out["privacy"]["made_private"] is True
    assert ownership.owner_of(TAB, ref) == ("user", ADMIN)
    assert db.get_datastore_by_id(int(ref))["context_org_id"] == monde["org"]
    assert not _lit(TIERS, TAB, ref)
    assert ownership.can_access(COLLEGUE, TAB, ref, "write"), "l'éditeur du projet n'écrit plus"


def test_relier_un_lien_existant_applique_la_regle(monde):
    """Le chemin de rattrapage d'un projet d'avant la règle : le même geste, rejoué."""
    from oto_mcp import db, ownership
    pid = _projet(monde)
    ref, _ = _procedure(monde)
    db.add_project_link(pid, "procedure", ref)   # posé avant la règle

    out = _link(pid, "procedure", ref)

    assert out["link_status"] == "unchanged"
    assert out["privacy"]["made_private"] is True
    assert ownership.owner_of(PROC, ref) == ("user", ADMIN)


# ── Ce qui reste à l'org, et le dit ──────────────────────────────────────────

def test_un_projet_partage_a_l_org_n_est_pas_prive(monde):
    from oto_mcp import ownership
    pid = _projet(monde, partage_org=True)
    ref, _ = _procedure(monde)
    out = _link(pid, "procedure", ref)
    assert out["privacy"] == {"made_private": False, "reason": "project_not_private"}
    assert ownership.owner_of(PROC, ref)[0] == "org"


def test_un_projet_d_org_ne_touche_a_rien(monde):
    from oto_mcp import db, ownership
    pid = int(db.create_project("org", str(monde["org"]), "Base", created_by=ADMIN))
    ref, _ = _procedure(monde)
    out = _link(pid, "procedure", ref)
    assert out["privacy"]["reason"] == "project_not_private"
    assert ownership.owner_of(PROC, ref)[0] == "org"


def test_une_cible_deja_personnelle_n_est_pas_touchee(monde):
    from oto_mcp import db, ownership
    pid = _projet(monde)
    ref = str(db.create_datastore("user", ADMIN, "perso-" + uuid.uuid4().hex[:8]))
    out = _link(pid, "tableau", ref)
    assert out["privacy"] == {"made_private": False, "reason": "not_org_owned"}


def test_une_cible_liee_par_un_autre_projet_reste_a_l_org(monde):
    from oto_mcp import db, ownership
    autre = int(db.create_project("org", str(monde["org"]), "Autre", created_by=ADMIN))
    ref, _ = _procedure(monde)
    db.add_project_link(autre, "procedure", ref)
    out = _link(_projet(monde), "procedure", ref)
    assert out["privacy"]["reason"] == "linked_by_other_projects"
    assert ownership.owner_of(PROC, ref)[0] == "org"


def test_sans_etre_admin_de_l_org_la_cible_reste_a_l_org(monde):
    """Le collègue ÉCRIT le projet, mais sortir une ressource de l'org est un transfert."""
    from oto_mcp import ownership
    pid = _projet(monde)
    ref, _ = _procedure(monde)
    out = _link(pid, "procedure", ref, sub=COLLEGUE)
    assert out["privacy"]["reason"] == "needs_org_admin"
    assert ownership.owner_of(PROC, ref)[0] == "org"


def test_un_slug_deja_pris_chez_le_proprietaire_garde_la_procedure(monde):
    from oto_mcp import org_store, ownership
    pid = _projet(monde)
    ref, slug = _procedure(monde)
    org_store.set_instruction("user", ADMIN, slug, "ma copie", title=slug, set_by=ADMIN)
    out = _link(pid, "procedure", ref)
    assert out["privacy"]["reason"] == "slug_taken_at_owner"
    assert ownership.owner_of(PROC, ref)[0] == "org"


# ── Un agent qui lit la cible : la même règle, procédure ou tableau ──────────

def _agent(monde, porteur: str, **kw) -> dict:
    from oto_mcp import db
    return db.create_trigger(monde["org"], porteur, tz="Europe/Paris", tools=[],
                             label="agent-" + uuid.uuid4().hex[:6], **kw)


def test_un_agent_d_un_autre_porteur_garde_la_procedure_a_l_org(monde):
    """Un agent lit sa procédure chez SON porteur puis à l'org : déplacée chez le
    propriétaire du projet, celle-ci ne se résoudrait plus pour l'agent d'un autre."""
    from oto_mcp import ownership
    pid = _projet(monde)
    ref, slug = _procedure(monde)
    t = _agent(monde, AUTRE, procedure=slug)
    out = _link(pid, "procedure", ref)
    assert out["privacy"] == {"made_private": False, "reason": "agents_depend_on_it",
                              "agents": [{"id": t["id"], "label": t["label"]}]}
    assert ownership.owner_of(PROC, ref)[0] == "org"


def test_un_agent_du_proprietaire_ne_bloque_pas_la_procedure(monde):
    from oto_mcp import ownership
    pid = _projet(monde)
    ref, slug = _procedure(monde)
    _agent(monde, ADMIN, procedure=slug)
    out = _link(pid, "procedure", ref)
    assert out["privacy"]["made_private"] is True
    assert ownership.owner_of(PROC, ref) == ("user", ADMIN)


def test_un_agent_d_un_autre_porteur_garde_le_tableau_a_l_org(monde):
    """Le tableau est lu par l'agent qui travaille dans le projet (ses slots) : déplacé
    chez le propriétaire du projet, il serait perdu pour l'agent d'un autre porteur —
    refusé comme pour une procédure, et les agents sont nommés."""
    from oto_mcp import ownership
    pid = _projet(monde)
    ref = _tableau(monde)
    proc_ref, slug = _procedure(monde)
    t = _agent(monde, AUTRE, procedure=slug, project_id=pid)
    out = _link(pid, "tableau", ref)
    assert out["privacy"] == {"made_private": False, "reason": "agents_depend_on_it",
                              "agents": [{"id": t["id"], "label": t["label"]}]}
    assert ownership.owner_of(TAB, ref)[0] == "org"
    assert _lit(TIERS, TAB, ref)


def test_l_agent_d_un_autre_projet_ne_bloque_pas_le_tableau(monde):
    from oto_mcp import db, ownership
    pid = _projet(monde)
    ailleurs = int(db.create_project("org", str(monde["org"]), "Ailleurs " +
                                     uuid.uuid4().hex[:6], created_by=ADMIN))
    ref = _tableau(monde)
    _, slug = _procedure(monde)
    _agent(monde, AUTRE, procedure=slug, project_id=ailleurs)
    out = _link(pid, "tableau", ref)
    assert out["privacy"]["made_private"] is True
    assert ownership.owner_of(TAB, ref) == ("user", ADMIN)


def test_un_agent_eteint_ne_bloque_pas_le_tableau(monde):
    from oto_mcp import ownership
    pid = _projet(monde)
    ref = _tableau(monde)
    _, slug = _procedure(monde)
    _agent(monde, AUTRE, procedure=slug, project_id=pid, enabled=False)
    out = _link(pid, "tableau", ref)
    assert out["privacy"]["made_private"] is True
    assert ownership.owner_of(TAB, ref) == ("user", ADMIN)


def test_projet_d_equipe_tout_agent_garde_le_tableau(monde):
    """Chez une équipe, palier qu'un agent ne résout jamais : même l'agent du lieur."""
    from oto_mcp import db, group_store, ownership
    g = group_store.create_group(monde["org"], "eq-" + uuid.uuid4().hex[:6])
    group_store.add_group_member(g, ADMIN, "group_admin")
    pid = int(db.create_project("group", str(g), "Equipe " + uuid.uuid4().hex[:6],
                                created_by=ADMIN))
    ref = _tableau(monde)
    _, slug = _procedure(monde)
    t = _agent(monde, ADMIN, procedure=slug, project_id=pid)
    out = _link(pid, "tableau", ref)
    assert out["privacy"]["reason"] == "agents_depend_on_it"
    assert [a["id"] for a in out["privacy"]["agents"]] == [t["id"]]
    assert ownership.owner_of(TAB, ref)[0] == "org"


# ── Transfert et partages : une seule transaction ────────────────────────────

class _Panne(RuntimeError):
    pass


@pytest.mark.parametrize("cible", ["procedure", "tableau"])
def test_un_echec_au_milieu_ne_laisse_rien(monde, monkeypatch, cible):
    """La panne tombe à la DERNIÈRE écriture de la règle (son journal) : le transfert, le
    rangement du tableau et les partages sont déjà posés dans la transaction. Rien ne
    doit en rester."""
    from oto_mcp import db, ownership
    pid = _projet(monde)
    ref = _procedure(monde)[0] if cible == "procedure" else _tableau(monde)
    rtype = PROC if cible == "procedure" else TAB
    avant = db.get_datastore_by_id(int(ref)) if cible == "tableau" else None
    journal = db.log_project_activity
    vus = []

    def _panne(project_id, sub, action, detail=None, *a, **kw):
        if action != "project.link_private":
            return journal(project_id, sub, action, detail, *a, **kw)
        vus.append((ownership.owner_of(rtype, ref),
                    [(g["principal_type"], g["principal_id"])
                     for g in ownership.list_grants(rtype, ref)]))
        raise _Panne("panne au milieu")

    monkeypatch.setattr(db, "log_project_activity", _panne)
    with pytest.raises(_Panne):
        _link(pid, cible, ref)

    assert vus == [(("user", ADMIN), [("user", COLLEGUE)])], \
        "la panne n'est pas tombée après le transfert et les partages"
    assert ownership.owner_of(rtype, ref) == ("org", str(monde["org"]))
    assert ownership.list_grants(rtype, ref) == []
    assert _lit(TIERS, rtype, ref)
    if avant is not None:
        assert db.get_datastore_by_id(int(ref))["context_org_id"] == avant["context_org_id"]


def test_transaction_une_erreur_attrapee_n_annule_que_sa_part(monde):
    """Un `_connect()` du bloc est un point de sauvegarde : une fonction qui attrape sa
    propre erreur (un journal « best-effort ») n'emporte pas les autres écritures."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp import db, ownership
    ref = _tableau(monde)
    with db.transaction_d_ecriture():
        ownership.grant(TAB, ref, "user", TIERS, role="viewer", granted_by=ADMIN)
        try:
            with db._connect() as conn:
                conn.execute("SELECT 1/0")
        except psycopg.errors.DivisionByZero:
            pass
    assert [(g["principal_type"], g["principal_id"])
            for g in ownership.list_grants(TAB, ref)] == [("user", TIERS)]


def test_transaction_refusee_dans_une_portee_de_lecture(monde):
    from oto_mcp import db
    with db.reuse_connection():
        with pytest.raises(db.TransactionDansUneLecture):
            with db.transaction_d_ecriture():
                pass
