"""`upsert_user` n'écrit la ligne `users` que si une colonne posée CHANGE — sur SQL réel.

Appelée à chaque requête REST authentifiée et à chaque message MCP, elle réécrivait la
ligne à chaque fois : un verrou de ligne et un COMMIT par requête, et les appels
parallèles d'un même compte se sérialisaient sur sa ligne. La preuve d'une non-écriture
est la version de la ligne (`xmin`), qu'un UPDATE change même à valeurs égales ; celle
d'un non-verrou, un appel qui aboutit pendant qu'une autre transaction tient la ligne.

Les gardes de naissance (résurrection d'un compte en pause par la chaîne d'alias,
tenant désactivé) sont tenues par `test_account_suspension_db_live.py` et
`test_tenant_desactivation_live.py`, inchangés ; la langue par
`test_upsert_user_locale_live_701.py`.
"""
from __future__ import annotations

import os
import threading
import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    """Le schéma réel sur la base neuve du module (patron de
    `test_account_suspension_db_live.py::live`)."""
    avant = {cle: os.environ.get(cle)
             for cle in ("DATABASE_URL", "OTO_MCP_MASTER_KEY")}
    os.environ["DATABASE_URL"] = pg_module_dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "4" * 64
    try:
        from oto_mcp.db import init_db
        init_db()
        yield pg_module_dsn
    finally:
        for cle, valeur in avant.items():
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur


def _ligne(sub: str) -> dict:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return dict(conn.execute(
            "SELECT xmin::text AS version, email, name, locale, updated_at "
            "FROM users WHERE sub = %s", (sub,)).fetchone())


def _neuf() -> str:
    return f"u-{uuid.uuid4().hex[:8]}"


def test_un_second_appel_sans_changement_n_ecrit_pas(live):
    from oto_mcp.db.users import upsert_user
    sub = _neuf()
    upsert_user(sub, email=f"{sub}@acme.test", name="Julie", locale="fr")
    avant = _ligne(sub)
    upsert_user(sub, email=f"{sub}@acme.test", name="Julie", locale="en")
    upsert_user(sub)          # les sites d'appel qui ne passent rien
    assert _ligne(sub) == avant


def test_un_changement_d_adresse_s_ecrit(live):
    """Le contrefactuel : sans lui, un upsert qui n'écrirait plus jamais rien serait
    vert ci-dessus."""
    from oto_mcp.db.users import upsert_user
    sub = _neuf()
    upsert_user(sub, email=f"{sub}@acme.test", name="Julie")
    avant = _ligne(sub)
    upsert_user(sub, email=f"{sub}@globex.test", name="Julie")
    apres = _ligne(sub)
    assert apres["email"] == f"{sub}@globex.test"
    assert apres["version"] != avant["version"]
    assert apres["updated_at"] >= avant["updated_at"]


def test_un_nom_absent_n_efface_rien_et_un_nom_neuf_s_ecrit(live):
    from oto_mcp.db.users import upsert_user
    sub = _neuf()
    upsert_user(sub, email=f"{sub}@acme.test", name="Julie")
    upsert_user(sub, email=f"{sub}@acme.test", name=None)
    assert _ligne(sub)["name"] == "Julie"
    upsert_user(sub, email=f"{sub}@acme.test", name="Léa")
    assert _ligne(sub)["name"] == "Léa"


def test_une_premiere_langue_s_ecrit_sur_une_ligne_qui_n_en_a_pas(live):
    from oto_mcp.db.users import upsert_user
    sub = _neuf()
    upsert_user(sub, email=f"{sub}@acme.test")
    upsert_user(sub, email=f"{sub}@acme.test", locale="fr")
    assert _ligne(sub)["locale"] == "fr"


def test_sans_changement_l_appel_ne_prend_pas_le_verrou_de_la_ligne(live):
    """Une autre transaction tient la ligne (`FOR UPDATE`) : l'appel sans changement
    aboutit quand même. Un `ON CONFLICT DO UPDATE … WHERE` faux attendrait ici —
    PostgreSQL y verrouille la ligne en conflit même quand la condition est fausse."""
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db.users import upsert_user
    sub = _neuf()
    upsert_user(sub, email=f"{sub}@acme.test", name="Julie")
    fini = threading.Event()
    with psycopg.connect(live) as tiers:
        tiers.execute("SELECT 1 FROM users WHERE sub = %s FOR UPDATE", (sub,))

        def _appel():
            upsert_user(sub, email=f"{sub}@acme.test", name="Julie")
            fini.set()
        fil = threading.Thread(target=_appel, daemon=True)
        fil.start()
        aboutit = fini.wait(5)
        tiers.rollback()
    fil.join(10)
    assert aboutit, "l'appel sans changement a attendu le verrou de la ligne"


def test_une_naissance_rend_toujours_ses_effets(live):
    """Le gate `inserted` tient : la première présentation crée l'org maison, la
    seconde ne refait rien."""
    from oto_mcp import org_store
    from oto_mcp.db.users import upsert_user
    sub = _neuf()
    upsert_user(sub, email=f"{sub}@acme.test")
    orgs = org_store.list_orgs_for_user(sub)
    assert orgs
    upsert_user(sub, email=f"{sub}@acme.test")
    assert org_store.list_orgs_for_user(sub) == orgs
