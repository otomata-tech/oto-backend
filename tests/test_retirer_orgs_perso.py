"""`scripts/retirer_orgs_perso.py` : les orgs perso héritées perdent leur étiquette, les
vides s'archivent, rien ne se perd (05/11/2026, plus d'org perso).

Base réelle : le geste est du SQL sur des tables vivantes.
"""
from __future__ import annotations

import uuid

import pytest


def _perso_heritee(db, org_store, sub: str) -> int:
    """Une org perso comme les bases d'avant le lot en portent : créée par `sub`, seul
    membre, étiquetée `personal_of`, sans la marque `auto_created`."""
    db.upsert_user(sub, email=f"{sub}@t.invalid", name=sub)
    oid = org_store.home_org(sub)
    with db._connect() as c:
        c.execute("UPDATE orgs SET personal_of = %s, auto_created = FALSE WHERE id = %s",
                  (sub, oid))
    return oid


@pytest.fixture(scope="module")
def monde(live):
    from oto_mcp import db, org_store
    u = uuid.uuid4().hex[:6]
    vide, pleine, seule = f"usr_rop_vide_{u}", f"usr_rop_pleine_{u}", f"usr_rop_seule_{u}"
    p_vide = _perso_heritee(db, org_store, vide)
    p_pleine = _perso_heritee(db, org_store, pleine)
    p_seule = _perso_heritee(db, org_store, seule)
    # `vide` et `pleine` ont une autre org ; `seule` n'a que sa perso.
    for sub in (vide, pleine):
        autre = org_store.create_org(f"Boîte {sub}", created_by=sub)
        org_store.add_org_member(autre, sub, "org_admin")
    projet = int(db.create_project("user", pleine, f"rop-{u}", created_by=pleine))
    return {"vide": (vide, p_vide), "pleine": (pleine, p_pleine), "seule": (seule, p_seule),
            "projet": projet}


def _org(db, oid):
    with db._connect() as c:
        return c.execute("SELECT personal_of, auto_created, archived_at FROM orgs "
                         "WHERE id = %s", (oid,)).fetchone()


def _compteurs(sortie: str) -> dict[str, int]:
    return {k: int(v) for k, v in (ligne.split(": ") for ligne in sortie.splitlines()
                                   if ": " in ligne and ligne.split(": ")[1].isdigit())}


def test_a_blanc_rien_n_est_ecrit_et_seuls_des_nombres_sortent(monde, capsys):
    from oto_mcp import db
    from scripts import retirer_orgs_perso as R
    assert R.main([]) == 0
    sortie = capsys.readouterr().out
    c = _compteurs(sortie)
    assert c["orgs_perso_vivantes"] == 3 and c["orgs_perso_marquees"] == 0
    assert c["orgs_perso_peuplees"] == 0
    # `pleine` a un projet, `seule` n'a pas d'autre org : seule `vide` est archivable.
    with db._connect() as conn:
        vides = set(R._vides(conn))
    assert monde["vide"][1] in vides
    assert not vides & {monde["pleine"][1], monde["seule"][1]}
    assert c["orgs_perso_vides_archivables"] == 1
    assert c["projets_a_ranger"] == 1
    # Rien de nominatif : ni compte, ni nom d'org.
    for sub, _oid in (monde["vide"], monde["pleine"], monde["seule"]):
        assert sub not in sortie
    assert "Boîte" not in sortie and "«" not in sortie
    assert _org(db, monde["vide"][1])["personal_of"] == monde["vide"][0]
    assert not _org(db, monde["vide"][1])["auto_created"]


def test_a_blanc_la_connexion_est_en_lecture_seule_et_bornee(monkeypatch):
    """Sans base : à blanc, le script borne ses requêtes et passe en lecture seule
    AVANT la première lecture, et n'ouvre aucune autre connexion."""
    from contextlib import contextmanager

    from scripts import retirer_orgs_perso as R
    vu, ouvertures = [], []

    class _C:
        def execute(self, sql, params=None):
            vu.append(" ".join(sql.split()))
            rows = [] if "SELECT o.id FROM orgs o" in sql else [{"n": 0}]
            return type("R", (), {"fetchone": lambda s: rows[0] if rows else None,
                                  "fetchall": lambda s: rows})()

    @contextmanager
    def _ouvrir():
        ouvertures.append(1)
        yield _C()

    monkeypatch.setattr(R, "_connect_autocommit", _ouvrir)
    assert R.main([]) == 0
    assert len(ouvertures) == 1
    premiere_lecture = next(i for i, s in enumerate(vu) if s.startswith("SELECT"))
    assert {"SET statement_timeout = '15s'", "SET lock_timeout = '5s'",
            "SET default_transaction_read_only = on"} <= set(vu[:premiere_lecture])
    assert not any(s.startswith(("UPDATE", "INSERT", "DELETE")) for s in vu)


def test_appliquer_et_archiver(monde):
    from oto_mcp import db, org_store
    from scripts import retirer_orgs_perso as R
    assert R.main(["--appliquer", "--archiver"]) == 0
    vide, pleine, seule = (_org(db, monde[k][1]) for k in ("vide", "pleine", "seule"))
    assert vide["archived_at"] is not None
    assert pleine["archived_at"] is None and seule["archived_at"] is None
    assert all(o["personal_of"] is None for o in (vide, pleine, seule))
    assert pleine["auto_created"] and seule["auto_created"]
    # Le projet perso sans org de création est rangé là où il se listait.
    assert int(db.get_project_by_id(monde["projet"])["context_org_id"]) == monde["pleine"][1]
    # Personne n'est laissé sans org active.
    for k in ("vide", "pleine", "seule"):
        assert org_store.home_org(monde[k][0]) is not None
    # Idempotent : un second passage ne trouve plus rien.
    assert R.main(["--appliquer", "--archiver"]) == 0
    with db._connect() as c:
        assert c.execute("SELECT count(*) AS n FROM orgs WHERE personal_of IS NOT NULL"
                         ).fetchone()["n"] == 0


def test_archiver_seul_dit_en_clair_qu_il_ne_trouve_rien(monkeypatch, capsys):
    """Sans base : `--archiver` sur une liste vide le dit, au lieu de passer en silence."""
    from contextlib import contextmanager

    from scripts import retirer_orgs_perso as R

    class _C:
        def execute(self, sql, params=None):
            rows = [] if "SELECT o.id FROM orgs o" in sql else [{"n": 0}]
            return type("R", (), {"fetchone": lambda s: rows[0] if rows else None,
                                  "fetchall": lambda s: rows})()

    @contextmanager
    def _ouvrir():
        yield _C()

    monkeypatch.setattr(R, "_connect_autocommit", _ouvrir)
    assert R.main(["--archiver"]) == 0
    assert "--archiver : aucune org vide à archiver." in capsys.readouterr().out


@pytest.fixture(scope="module")
def deux_passages(live):
    """Le geste joué en deux temps : `--appliquer` seul a déjà vidé l'étiquette."""
    from oto_mcp import db, org_store
    u = uuid.uuid4().hex[:6]
    vide, pleine, seule = f"usr_rop2_vide_{u}", f"usr_rop2_pleine_{u}", f"usr_rop2_seule_{u}"
    orgs = {k: _perso_heritee(db, org_store, sub)
            for k, sub in (("vide", vide), ("pleine", pleine), ("seule", seule))}
    for sub in (vide, pleine):
        autre = org_store.create_org(f"Boîte {sub}", created_by=sub)
        org_store.add_org_member(autre, sub, "org_admin")
    db.create_project("user", pleine, f"rop2-{u}", created_by=pleine)
    return orgs


def test_archiver_apres_un_appliquer_seul_retrouve_les_ex_orgs_perso(deux_passages):
    from oto_mcp import db
    from scripts import retirer_orgs_perso as R
    assert R.main(["--appliquer"]) == 0
    for oid in deux_passages.values():
        o = _org(db, oid)
        assert o["personal_of"] is None and o["auto_created"] and o["archived_at"] is None
    # À blanc, sans l'étiquette, le critère retrouve la même org vide, et elle seule.
    with db._connect() as conn:
        vides = set(R._vides(conn))
    assert deux_passages["vide"] in vides
    assert not vides & {deux_passages["pleine"], deux_passages["seule"]}
    assert R.main(["--archiver"]) == 0
    assert _org(db, deux_passages["vide"])["archived_at"] is None   # à blanc : rien écrit
    assert R.main(["--appliquer", "--archiver"]) == 0
    assert _org(db, deux_passages["vide"])["archived_at"] is not None
    assert _org(db, deux_passages["pleine"])["archived_at"] is None
    assert _org(db, deux_passages["seule"])["archived_at"] is None
