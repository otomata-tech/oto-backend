"""oto#247 — deux connexions simultanées sur la clé partagée, contre un VRAI PostgreSQL.

La réconciliation prenait « le plus récent compte vivant » parmi les comptes créés après
le pending : sur la clé plateforme, partagée entre orgs, deux personnes qui connectent
dans la même fenêtre rendent leurs deux comptes candidats pour chacune, et un compte de
messagerie a été rattaché à la mauvaise personne.

Ce banc rejoue la scène avec de vrais pendings et de vraies liaisons (la concurrence
se LIT dans `unipile_pending` — la stubber ne prouverait que le stub) ; seul le
fournisseur est simulé :

1. deux comptes éligibles, aucune preuve ⟹ refus nommé des deux côtés, rien d'écrit ;
2. l'`account_id` du retour ⟹ le bon rattachement, et la scène se dénoue pour l'autre ;
3. un seul compte neuf, sans indice ⟹ refus, que quelqu'un d'autre attende ou non :
   la demande dit qui a DEMANDÉ le lien, pas qui l'a parcouru (lien transféré).
"""
from __future__ import annotations

import pytest

A, B = "usr_247_a", "usr_247_b"
ACC_A, ACC_B = "acc_247_a", "acc_247_b"
NEUF = "2099-01-01 00:00:00+00"     # toujours APRÈS les pendings du banc


def _exec(sql, params=()):
    from oto_mcp.db._conn import _connect

    with _connect() as conn:
        conn.execute(sql, params)


def _rows(sql, params=()):
    from oto_mcp.db._conn import _connect

    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


@pytest.fixture
def scene(live, monkeypatch):
    """Deux personnes, deux orgs, la même clé plateforme — et chacune un lien demandé."""
    from oto_mcp import access, db
    import oto.tools.unipile as core_unipile

    _exec("DELETE FROM unipile_accounts")
    _exec("DELETE FROM unipile_pending")
    org_a = _rows("INSERT INTO orgs (name) VALUES ('Org A') RETURNING id")[0]["id"]
    org_b = _rows("INSERT INTO orgs (name) VALUES ('Org B') RETURNING id")[0]["id"]
    db.create_unipile_pending("nonce_247_a", A, org_a, "LINKEDIN", platform_seat=True)
    db.create_unipile_pending("nonce_247_b", B, org_b, "LINKEDIN", platform_seat=True)

    comptes: list[dict] = []

    class _Cred:
        is_platform, key, config = True, "clef", {}

    class _Client:
        def list_accounts(self):
            return list(comptes)

        def account_alive(self, _aid):
            return True

    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: _Cred())
    monkeypatch.setattr(core_unipile, "make_unipile_client", lambda **k: _Client())
    return {"org_a": org_a, "org_b": org_b, "comptes": comptes}


def _compte(aid: str, nom: str) -> dict:
    return {"id": aid, "provider": "linkedin", "created_at": NEUF, "name": nom}


def _liaisons() -> list[tuple]:
    return [(r["sub"], r["account_id"]) for r in _rows(
        "SELECT sub, account_id FROM unipile_accounts ORDER BY sub")]


def _pendings() -> list[str]:
    return [r["sub"] for r in _rows("SELECT sub FROM unipile_pending ORDER BY sub")]


def test_deux_eligibles_sans_preuve_refus_des_deux_cotes(scene):
    from oto_mcp import unipile_connect

    scene["comptes"] += [_compte(ACC_A, "Homonyme"), _compte(ACC_B, "Homonyme")]
    for sub in (A, B):
        out = unipile_connect.reconcile_pending(sub)
        assert out["bound"] is False and out["reason"] == "ambiguous_candidates"
    assert _liaisons() == [], "un compte a été rattaché sans preuve parmi deux candidats"
    assert _pendings() == [A, B], "un refus ne consomme pas la demande"


def test_l_account_id_lie_le_bon_et_denoue_la_scene(scene):
    from oto_mcp import unipile_connect

    scene["comptes"] += [_compte(ACC_A, "Homonyme"), _compte(ACC_B, "Homonyme")]
    out = unipile_connect.reconcile_pending(A, account_id=ACC_A)
    assert out["bound"] is True and out["accounts"][0]["account_id"] == ACC_A
    assert out["accounts"][0]["org_id"] == scene["org_a"]
    # A a consommé sa demande, ACC_A est pris : pour B il ne reste qu'un candidat —
    # qui ne prouve toujours rien sans l'indice de SA page de retour.
    out = unipile_connect.reconcile_pending(B)
    assert out["bound"] is False and out["reason"] == "proof_required"
    out = unipile_connect.reconcile_pending(B, account_id=ACC_B)
    assert out["bound"] is True and out["accounts"][0]["account_id"] == ACC_B
    assert _liaisons() == [(A, ACC_A), (B, ACC_B)]
    assert _pendings() == []


def test_un_seul_compte_neuf_mais_l_autre_attend_refus(scene):
    """B a fini, A pas encore (ou a abandonné) : le seul compte neuf est celui de B.
    Le lier à A était exactement le rattachement à la mauvaise personne."""
    from oto_mcp import unipile_connect

    scene["comptes"].append(_compte(ACC_B, "Personne B"))
    out = unipile_connect.reconcile_pending(A)
    assert out["bound"] is False and out["reason"] == "proof_required"
    assert _liaisons() == []
    # B, porteur de son indice, lie le sien.
    assert unipile_connect.reconcile_pending(B, account_id=ACC_B)["bound"]
    assert _liaisons() == [(B, ACC_B)]


def test_un_lien_transfere_ne_rattache_pas_le_compte_de_qui_l_a_parcouru(scene):
    """A demande un lien et l'envoie à quelqu'un, qui le parcourt avec SON compte. Seul
    candidat, personne d'autre n'attend : avant, il était rattaché à A. Sans l'indice de
    la page de retour (arrivée dans le navigateur de l'autre, pas chez A), rien."""
    from oto_mcp import unipile_connect

    _exec("DELETE FROM unipile_pending WHERE sub = %s", (B,))
    scene["comptes"].append(_compte(ACC_B, "Celle qui a parcouru le lien"))
    out = unipile_connect.reconcile_pending(A)
    assert out["bound"] is False and out["reason"] == "proof_required"
    assert _liaisons() == []
    assert _pendings() == [A], "un refus ne consomme pas la demande"
