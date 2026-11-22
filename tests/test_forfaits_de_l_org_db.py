"""Ce qui fait tourner les agents d'une org : l'ÉTAT de chaque forfait et ce qui l'attend,
lus par l'admin d'org (`GET /api/orgs/{id}/model-subscriptions/{family}`, 22/11/2026),
EN BASE.

Ce que ces bancs tiennent :

1. l'état d'un PRÊTEUR (`ok`, `signed_out`, `at_limit`) et `ok` ⇔ `usable_now` ;
2. l'état d'un PORTEUR d'agents de la voie personnelle (`ok`, `none`, `not_open`,
   `signed_out`, `at_limit`), avec sa préséance — et que l'écran dit ce que la FILE fait :
   les travaux réservés sont exactement ceux des forfaits `ok` ;
3. les travaux en attente : par porteur sur la voie personnelle, UNE fois pour le pool ;
4. une seule ligne pour qui prête ET porte, son état selon le mode ;
5. un MEMBRE lit la forme d'avant : `lenders` à `sub` + `usable_now`, `plans: []`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

_F = "claude_subscription"


def _nom():
    return "f" + uuid.uuid4().hex[:6]


def _personne(sub):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (sub,))
    return sub


def _org(nom, *membres, mode=None):
    """Une org, son admin (`<nom>-admin`), ses membres, et son mode (None = rien réglé)."""
    from oto_mcp import org_store
    from oto_mcp.db import org_subscription_pool as P
    admin = _personne(f"{nom}-admin")
    oid = org_store.create_org(f"Org {nom}", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    for m in membres:
        org_store.add_org_member(oid, _personne(m), "org_member")
    if mode:
        P.poser_mode(oid, _F, mode, admin)
    return oid


def _dans(heures: float) -> str:
    """Un instant à `heures` de l'horloge de la base — en TEXTE UTC sans fuseau, comme la
    fabrique de lignes du pool le rend (`db._conn`)."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute("SELECT NOW() + make_interval(secs => %s) AS t",
                            (heures * 3600,)).fetchone()["t"]


def _abonne(sub, *, statut="connected", reset=None, pret_a=(), ouvert_dans=()):
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    _personne(sub)
    US.upsert_sandbox(sub, _F, f"sandbox-{sub}")
    US.marquer_statut(sub, _F, statut, limit_reset_at=reset, ok=statut == "connected")
    if pret_a:
        P.poser_prets(sub, _F, pret_a)
    if ouvert_dans:
        US.poser_orgs_servies(sub, _F, ouvert_dans)
    return sub


def _agent(org, sub, *, enabled=True):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO runner_triggers (org_id, sub, procedure, tools, cron, model, "
                     "enabled) VALUES (%s, %s, 'p', '[]', '0 8 * * 1', 'sub:sonnet', %s)",
                     (org, sub, enabled))


def _travail(org, sub, **kw):
    from oto_mcp import db
    return db.enqueue_job(org, "start", sub=sub,
                          payload={"procedure": "p", "model": "sub:sonnet",
                                   "model_family": _F}, **kw)["id"]


def _lire(org, sub):
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.orgs import model_subscriptions as MS
    return MS._get_plafond(ResolvedCtx(sub=sub, org_id=org),
                           MS.GetOrgPlafondInput(org_id=org, family=_F))


def _par_sub(lignes):
    return {l["sub"]: l for l in lignes}


def _reserver_tout(org):
    """Ce que la FILE réserve, jusqu'à ce qu'elle ne réserve plus rien : les demandeurs."""
    from oto_mcp import db
    pris = []
    while (job := db.claim_next_job(org, "w-ecran", lease_seconds=60, depot=_F,
                                    famille_seule=True)) is not None:
        pris.append(job["sub"])
    return sorted(pris)


# ── 1. le prêteur ───────────────────────────────────────────────────────────

def test_l_etat_d_un_PRETEUR_et_ok_si_et_seulement_si_usable_now(live):
    n = _nom()
    oid = _org(n, *(f"{n}-{x}" for x in "abcde"))
    _abonne(f"{n}-a", pret_a=[oid])
    _abonne(f"{n}-b", statut="needs_login", pret_a=[oid])
    _abonne(f"{n}-c", statut="disconnected", pret_a=[oid])
    reprise = _dans(2)
    _abonne(f"{n}-d", statut="paused_limit", reset=reprise, pret_a=[oid])
    _abonne(f"{n}-e", statut="paused_limit", reset=_dans(-1), pret_a=[oid])

    lu = _lire(oid, f"{n}-admin")
    preteurs = _par_sub(lu["lenders"])
    assert {s[-1]: p["state"] for s, p in preteurs.items()} == {
        "a": "ok", "b": "signed_out", "c": "signed_out", "d": "at_limit",
        "e": "ok"}, "un plafond échu ne freine plus : la file le sert"
    for p in preteurs.values():
        assert (p["state"] == "ok") is p["usable_now"], p
    d = preteurs[f"{n}-d"]
    assert d["reset_at"].endswith("+00:00"), "un instant ISO AVEC son fuseau"
    assert datetime.fromisoformat(d["reset_at"]) == \
        datetime.fromisoformat(reprise).replace(tzinfo=timezone.utc)
    assert all(p["reset_at"] is None for s, p in preteurs.items() if s != f"{n}-d")
    assert preteurs[f"{n}-a"]["last_ok_at"].endswith("+00:00")
    assert preteurs[f"{n}-b"]["last_ok_at"] is None, "jamais servi : rien d'inventé"
    assert lu["pool_size"] == 2
    # Les prêteurs sont aussi les lignes de `plans`, au même état, sans travail.
    plans = _par_sub(lu["plans"])
    assert {s: (p["lent"], p["state"], p["waiting_jobs"]) for s, p in plans.items()} == {
        s: (True, p["state"], 0) for s, p in preteurs.items()}


# ── 2. le porteur d'agents, voie personnelle ─────────────────────────────────

def _porteurs(n, oid):
    """Un porteur par état ; chacun a un agent ALLUMÉ dans l'org."""
    gens = {
        "ok": dict(ouvert_dans=[oid]),
        "ferme": dict(),                                         # pas ouvert ici
        "dehors": dict(statut="needs_login", ouvert_dans=[oid]),
        "dehors_ferme": dict(statut="disconnected"),
        "plafond": dict(statut="paused_limit", reset=_dans(3), ouvert_dans=[oid]),
        "plafond_ferme": dict(statut="paused_limit", reset=_dans(3)),
    }
    for qui, kw in gens.items():
        _abonne(f"{n}-{qui}", **kw)
        _agent(oid, f"{n}-{qui}")
    _agent(oid, f"{n}-sans")                                     # aucun abonnement
    _agent(oid, f"{n}-eteint", enabled=False)                    # agent éteint, rien en file
    return {**{q: f"{n}-{q}" for q in gens}, "sans": f"{n}-sans"}


ATTENDUS = {"ok": "ok", "ferme": "not_open", "dehors": "signed_out",
            "dehors_ferme": "signed_out", "plafond": "at_limit",
            "plafond_ferme": "at_limit", "sans": "none"}


def test_l_etat_d_un_PORTEUR_d_agents_et_sa_preseance(live):
    n = _nom()
    membres = [*(f"{n}-{q}" for q in ATTENDUS), f"{n}-eteint"]
    oid = _org(n, *membres, mode="personnel")
    gens = _porteurs(n, oid)

    lu = _lire(oid, f"{n}-admin")
    plans = _par_sub(lu["plans"])
    assert {q: plans[s]["state"] for q, s in gens.items()} == ATTENDUS
    assert f"{n}-eteint" not in plans, "un agent éteint sans travail ne fait rien tourner"
    assert all(not p["lent"] and p["waiting_jobs"] == 0 for p in plans.values())
    assert plans[gens["plafond"]]["reset_at"].endswith("+00:00")
    assert plans[gens["dehors"]]["reset_at"] is None
    assert plans[gens["sans"]]["last_ok_at"] is None
    assert lu["pool_waiting_jobs"] == 0 and lu["lenders"] == []


def test_l_ecran_dit_ce_que_la_FILE_fait(live):
    """Un travail en file par porteur : l'écran les compte chacun sur son forfait, et la
    réservation prend EXACTEMENT ceux des forfaits `ok` — les autres attendent."""
    n = _nom()
    membres = [*(f"{n}-{q}" for q in ATTENDUS), f"{n}-eteint"]
    oid = _org(n, *membres, mode="personnel")
    gens = _porteurs(n, oid)
    for s in gens.values():
        _travail(oid, s)

    avant = _par_sub(_lire(oid, f"{n}-admin")["plans"])
    assert {q: avant[s]["waiting_jobs"] for q, s in gens.items()} == {q: 1 for q in gens}

    assert _reserver_tout(oid) == sorted(
        s for q, s in gens.items() if avant[s]["state"] == "ok")

    apres = _par_sub(_lire(oid, f"{n}-admin")["plans"])
    assert {q: apres[s]["waiting_jobs"] for q, s in gens.items()} == {
        q: 0 if ATTENDUS[q] == "ok" else 1 for q in gens}


# ── 3. ce qui attend : par porteur, ou UNE fois pour le pool ─────────────────

def test_en_POOL_les_travaux_sont_comptes_une_fois_et_les_porteurs_absents(live):
    n = _nom()
    oid = _org(n, f"{n}-l1", f"{n}-l2", f"{n}-o", mode="pool")
    _abonne(f"{n}-l1", statut="needs_login", pret_a=[oid])
    _abonne(f"{n}-l2", statut="needs_login", pret_a=[oid])
    _abonne(f"{n}-o", ouvert_dans=[oid])          # son forfait ne sert pas en pool
    _agent(oid, f"{n}-o")
    for _ in range(3):
        _travail(oid, f"{n}-o")
    _travail(oid, f"{n}-o", delai_s=3600)          # pas encore dû : n'attend personne

    lu = _lire(oid, f"{n}-admin")
    assert lu["pool_waiting_jobs"] == 3
    assert {p["sub"]: (p["lent"], p["state"], p["waiting_jobs"]) for p in lu["plans"]} == {
        f"{n}-l1": (True, "signed_out", 0), f"{n}-l2": (True, "signed_out", 0)}
    assert _reserver_tout(oid) == [], "tous les prêteurs à reconnecter : tout attend"


def test_sans_mode_REGLE_la_voie_se_decide_par_travail_comme_a_la_reservation(live):
    """Le pool complète : le porteur dont l'abonnement sert garde ses travaux ; ceux d'un
    porteur à reconnecter ou sans abonnement partent au pool (un prêteur sert). Un
    travail en file d'un agent ÉTEINT compte aussi, sur son porteur."""
    n = _nom()
    oid = _org(n, f"{n}-l", f"{n}-a", f"{n}-b", f"{n}-c", f"{n}-d")
    _abonne(f"{n}-l", pret_a=[oid])
    _abonne(f"{n}-a", ouvert_dans=[oid])
    _abonne(f"{n}-b", statut="needs_login", ouvert_dans=[oid])
    _abonne(f"{n}-d", ouvert_dans=[oid])
    for s in ("a", "b", "c"):
        _agent(oid, f"{n}-{s}")
        _travail(oid, f"{n}-{s}")
    _agent(oid, f"{n}-d", enabled=False)
    _travail(oid, f"{n}-d")

    lu = _lire(oid, f"{n}-admin")
    assert {p["sub"][-1]: (p["lent"], p["state"], p["waiting_jobs"]) for p in lu["plans"]} == {
        "l": (True, "ok", 0), "a": (False, "ok", 1), "b": (False, "signed_out", 0),
        "c": (False, "none", 0), "d": (False, "ok", 1)}
    assert lu["pool_waiting_jobs"] == 2, "b et c, une fois, sur le pool"


# ── 4. qui prête ET porte : une ligne ───────────────────────────────────────

@pytest.mark.parametrize("mode, etat, sur_lui, sur_le_pool", [
    # Rien réglé : son travail part au pool (pas ouvert ici, mais il prête et sert).
    (None, "ok", 0, 1),
    # Personnel réglé : le prêt ne sert rien, sa voie personnelle n'est pas ouverte.
    ("personnel", "not_open", 1, 0),
])
def test_qui_prete_ET_porte_n_a_qu_une_ligne(live, mode, etat, sur_lui, sur_le_pool):
    n = _nom()
    oid = _org(n, f"{n}-x", mode=mode)
    _abonne(f"{n}-x", pret_a=[oid])                # prêté, jamais ouvert en personnel
    _agent(oid, f"{n}-x")
    _travail(oid, f"{n}-x")

    lu = _lire(oid, f"{n}-admin")
    (ligne,) = lu["plans"]
    assert (ligne["sub"], ligne["lent"], ligne["state"], ligne["waiting_jobs"]) == \
        (f"{n}-x", True, etat, sur_lui)
    assert lu["pool_waiting_jobs"] == sur_le_pool


# ── 5. un membre : rien de neuf ─────────────────────────────────────────────

def test_un_MEMBRE_lit_la_forme_d_avant(live):
    n = _nom()
    oid = _org(n, f"{n}-l", f"{n}-o")
    reprise = _dans(5)
    _abonne(f"{n}-l", statut="paused_limit", reset=reprise, pret_a=[oid])
    _abonne(f"{n}-o", statut="needs_login", ouvert_dans=[oid])
    _agent(oid, f"{n}-o")
    _travail(oid, f"{n}-o")

    membre = _lire(oid, f"{n}-o")
    admin = _lire(oid, f"{n}-admin")
    assert set(membre) == {"org_id", "family", "limit_pct", "default", "updated_at",
                           "updated_by", "mode", "mode_chosen", "pool_size", "lenders",
                           "api_fallback", "plans"}
    assert membre["plans"] == []
    assert membre["lenders"] == [{"sub": f"{n}-l", "usable_now": False}]
    assert reprise[:16] not in str(membre) and "T" + reprise[11:16] not in str(membre), \
        "aucune échéance ne sort"
    # Tout le reste est ce que l'admin lit aussi.
    communs = set(membre) - {"lenders", "plans"}
    assert {k: membre[k] for k in communs} == {k: admin[k] for k in communs}
    assert admin["plans"] and admin["pool_waiting_jobs"] == 0
