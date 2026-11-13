"""Les limites de dépense du jour (`docs/limites-de-depense.md`) : le cœur APPLIQUE la liste
de blocages que pousse le commerce, il ne compte rien.

1. la liste en mémoire (`access/limite_du_jour`) : qui est bloqué, jusqu'à quand, et
   fail-open quand elle est illisible ;
2. la face de service (`service.org.spend_blocks.*`) : ce qu'elle refuse à la saisie ;
3. les trois portes — l'outil (le résolveur), le tour d'un agent hébergé (à la réservation
   du travail et à chaque tour), le tour de l'agent intégré ;
4. sur une vraie base : la liste se remplace d'un bloc, et la personne d'un appel remonte
   au commerce (mouvements de crédits, relevé de service) — jamais à la lentille membre.
"""
from __future__ import annotations

import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from oto_mcp import db
from oto_mcp.access import limite_du_jour
from oto_mcp.capabilities import registry
from oto_mcp.capabilities import service_credits
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.db import spend_blocks
from oto_mcp.mcp_errors import McpError

MAINTENANT = datetime.now(timezone.utc)
DEMAIN = (MAINTENANT + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)


def _bloc(org_id, sub="", scope="org", group_id=None, until=DEMAIN):
    return {"org_id": org_id, "sub": sub, "scope": scope, "group_id": group_id,
            "until": until}


@pytest.fixture
def liste(monkeypatch):
    """La liste que rendrait la base ; `etat["panne"]` la rend illisible."""
    etat = {"blocs": [], "panne": None, "lectures": 0}

    def actifs():
        etat["lectures"] += 1
        if etat["panne"]:
            raise etat["panne"]
        return list(etat["blocs"])
    monkeypatch.setattr(spend_blocks, "actifs", actifs)
    monkeypatch.setattr(limite_du_jour, "_cache", {"par_org": {}, "lu_a": None})
    return etat


# ── 1. la liste en mémoire ───────────────────────────────────────────────────

def test_une_personne_bloquee_l_est_dans_son_org_seulement(liste):
    liste["blocs"] = [_bloc(7, "u-1", "member")]
    assert limite_du_jour.blocage("u-1", 7)["scope"] == "member"
    assert limite_du_jour.blocage("u-2", 7) is None, "un collègue n'est pas bloqué"
    assert limite_du_jour.blocage("u-1", 8) is None, "ses autres orgs restent ouvertes"
    assert limite_du_jour.blocage("u-1", None) is None


def test_un_blocage_d_org_touche_chaque_membre(liste):
    liste["blocs"] = [_bloc(7)]
    for sub in ("u-1", "u-2", None):
        assert limite_du_jour.blocage(sub, 7)["scope"] == "org"


def test_le_blocage_de_la_personne_passe_avant_celui_de_l_org(liste):
    liste["blocs"] = [_bloc(7), _bloc(7, "u-1", "group", group_id=3)]
    assert limite_du_jour.blocage("u-1", 7)["scope"] == "group"
    assert limite_du_jour.blocage("u-2", 7)["scope"] == "org"


def test_un_blocage_fini_ne_bloque_plus_meme_garde_en_memoire(liste):
    """La journée finie se rouvre seule, sans attendre que le commerce retire la ligne."""
    liste["blocs"] = [_bloc(7, "u-1", "member", until=MAINTENANT - timedelta(seconds=1))]
    assert limite_du_jour.blocage("u-1", 7) is None


def test_la_liste_est_lue_une_fois_par_fenetre(liste):
    liste["blocs"] = [_bloc(7)]
    for _ in range(5):
        limite_du_jour.blocage("u-1", 7)
    assert liste["lectures"] == 1
    limite_du_jour.invalider()
    limite_du_jour.blocage("u-1", 7)
    assert liste["lectures"] == 2


def test_une_liste_jamais_lue_et_illisible_ne_bloque_personne(liste):
    """FAIL-OPEN, à l'inverse de la suspension : une limite est un garde-fou du client."""
    liste["panne"] = RuntimeError("base injoignable")
    assert limite_du_jour.blocage("u-1", 7) is None


def test_une_relecture_illisible_garde_la_derniere_liste(liste):
    liste["blocs"] = [_bloc(7, "u-1", "member")]
    assert limite_du_jour.blocage("u-1", 7) is not None
    limite_du_jour.invalider()
    liste["panne"] = RuntimeError("base injoignable")
    assert limite_du_jour.blocage("u-1", 7) is not None


def test_le_refus_d_outil_est_nomme_et_dit_quand_ca_reprend(liste):
    liste["blocs"] = [_bloc(7, "u-1", "member")]
    with pytest.raises(McpError) as e:
        limite_du_jour.exiger("u-1", 7)
    data = e.value.error.data
    assert data["code"] == "daily_spend_limit" and data["scope"] == "member"
    assert data["retryable"] is False
    assert datetime.fromisoformat(data["until"]) == DEMAIN
    assert "personal daily credit limit" in e.value.error.message
    assert "still work" in e.value.error.message, "ses propres clés restent servies"
    limite_du_jour.exiger("u-2", 7)  # le contrefactuel : un collègue passe


# ── 2. la face de service ───────────────────────────────────────────────────

def _cap(cle):
    return next(c for c in registry.CAPABILITIES if c.key == cle)


def _poser(blocks, org_id=7):
    cap = _cap("service.org.spend_blocks.put")
    ctx = ResolvedCtx(sub="service:m2m-commerce", org_id=None, role="service:commerce")
    return cap.handler(ctx, cap.Input(org_id=org_id, blocks=blocks))


@pytest.fixture
def sans_base(monkeypatch):
    posees = {}
    monkeypatch.setattr(service_credits, "_org_ou_404", lambda org_id: None)

    def remplacer(org_id, lignes):
        posees[org_id] = lignes
        return [{**l, "org_id": org_id} for l in lignes]
    monkeypatch.setattr(service_credits.db_spend_blocks, "remplacer", remplacer)
    return posees


def test_les_capacites_sont_des_tuyaux_de_service():
    for cle in ("service.org.spend_blocks.put", "service.org.spend_blocks.get",
                "service.org.usage.calls"):
        cap = _cap(cle)
        assert cap.mcp is None and cap.rest.path.startswith("/api/service/")


def test_une_liste_se_pose_et_dit_ce_qui_est_actif(sans_base):
    r = _poser([{"scope": "org", "until": DEMAIN.isoformat()},
                {"sub": "u-1", "scope": "group", "group_id": 3,
                 "until": DEMAIN.isoformat()}])
    assert [(b["sub"], b["scope"], b["active"]) for b in r["blocks"]] == [
        (None, "org", True), ("u-1", "group", True)]
    assert [l["sub"] for l in sans_base[7]] == ["", "u-1"], "l'org = sub vide en base"


@pytest.mark.parametrize("bloc, code", [
    ({"sub": "u-1", "scope": "org"}, "invalid_block"),
    ({"scope": "member"}, "invalid_block"),
    ({"sub": "u-1", "scope": "group"}, "invalid_block"),
    ({"sub": "u-1", "scope": "member", "group_id": 3}, "invalid_block"),
])
def test_portee_et_personne_doivent_s_accorder(sans_base, bloc, code):
    with pytest.raises(AuthzDenied) as e:
        _poser([{**bloc, "until": DEMAIN.isoformat()}])
    assert e.value.code == code


def test_une_personne_n_a_qu_un_blocage(sans_base):
    with pytest.raises(AuthzDenied) as e:
        _poser([{"sub": "u-1", "scope": "member", "until": DEMAIN.isoformat()},
                {"sub": "u-1", "scope": "group", "group_id": 3,
                 "until": DEMAIN.isoformat()}])
    assert e.value.code == "duplicate_block"


def test_un_blocage_du_jour_ne_dure_pas_des_mois(sans_base):
    with pytest.raises(AuthzDenied) as e:
        _poser([{"scope": "org", "until": (MAINTENANT + timedelta(days=3)).isoformat()}])
    assert e.value.code == "until_too_far"
    with pytest.raises(AuthzDenied) as e:
        _poser([{"scope": "org", "until": "2026-11-14T00:00:00"}])
    assert e.value.code == "until_without_timezone"
    assert 7 not in sans_base, "rien n'est posé sur un refus"


# ── 3. les trois portes ─────────────────────────────────────────────────────

def test_le_resolveur_passe_par_la_porte_sur_chaque_cle_refacturee():
    """Le résolveur s'ouvre avec dix gardes et un coffre : on vérifie le CÂBLAGE, comme
    `test_credits_appel` le fait pour la réservation — sur la clé de plateforme ET sur
    celle du tenant primaire, chaque réservation passe par la porte du jour."""
    src = (pathlib.Path(limite_du_jour.__file__).parent / "resolve.py").read_text()
    assert "credits.reserver(" not in src.replace("limite_du_jour.reserver(", "")
    for rang in ("tenant", "platform"):
        assert (f'limite_du_jour.reserver(sub, heritage.org_des_cles(sub, active_org), '
                f'"{rang}")') in src


def test_la_porte_refuse_avant_de_reserver(liste, monkeypatch):
    from oto_mcp.access import credits
    vu = []
    monkeypatch.setattr(credits, "reserver", lambda *a: vu.append(a))
    limite_du_jour.reserver("u-1", 7, "platform")
    assert vu == [("u-1", 7, "platform")]
    liste["blocs"] = [_bloc(7, "u-1", "member")]
    limite_du_jour.invalider()
    with pytest.raises(McpError):
        limite_du_jour.reserver("u-1", 7, "platform")
    assert len(vu) == 1, "un refus ne laisse aucune réservation"

class _Ctx:
    sub = "worker-1"


@pytest.fixture
def tour(monkeypatch, liste):
    from oto_mcp.capabilities import runner_jobs
    monkeypatch.setattr(runner_jobs.db, "tentative_en_cours", lambda job, sub: {
        "org_id": 7, "sub": "u-1", "attempts": 1, "run_id": "run-1",
        "model": "claude-sonnet-5", "cle_partagee": {"tentative": 1, "mode": "platform",
                                                      "depot": "anthropic"}})
    monkeypatch.setattr(runner_jobs.tours, "reserver_tour",
                        lambda *a, **k: {"held": True, "reserved": 10})
    return runner_jobs


def test_le_tour_d_un_agent_compte_pour_son_auteur(tour, liste):
    assert tour._reserver_un_tour(_Ctx(), 5, 0, 1000)["held"] is True
    liste["blocs"] = [_bloc(7, "u-1", "member")]
    limite_du_jour.invalider()
    with pytest.raises(AuthzDenied) as e:
        tour._reserver_un_tour(_Ctx(), 5, 1, 1000)
    assert (e.value.status, e.value.code) == (429, "daily_spend_limit")


def test_le_tour_d_un_agent_sur_la_cle_de_l_org_n_est_jamais_bloque(tour, liste,
                                                                  monkeypatch):
    liste["blocs"] = [_bloc(7)]
    monkeypatch.setattr(tour.db, "tentative_en_cours", lambda job, sub: {
        "org_id": 7, "sub": "u-1", "attempts": 1, "cle_partagee": None})
    assert tour._reserver_un_tour(_Ctx(), 5, 0, 1000) == {"ok": True, "held": False}


def test_un_travail_d_un_auteur_bloque_ne_part_pas():
    """À la réservation du travail, sur la clé refacturée seulement, avant le plancher."""
    from oto_mcp.capabilities import runner_jobs
    src = pathlib.Path(runner_jobs.__file__).read_text()
    branche = src[src.index("payeur = budget.rang_refacture(partagee.mode"):
                  src.index("if payeur is not None and not db.estampiller_cle_partagee(")]
    garde = 'limite_du_jour.refus(job.get("sub"), job["org_id"])'
    assert "if payeur is not None and (" in branche and garde in branche
    assert branche.index(garde) < branche.index("tours.sous_le_plancher(")
    assert "_refuser_sans_cle(job, appelant, _limite_du_jour(job, bloc))" in branche


def test_la_raison_d_un_travail_arrete_dit_quand_relancer():
    from oto_mcp.capabilities import runner_jobs
    raison = runner_jobs._limite_du_jour({"id": 1}, _bloc(7, "u-1", "member"))
    assert raison.startswith("daily_spend_limit : ")
    assert DEMAIN.strftime("%Y-%m-%d %H:%M UTC") in raison


def test_l_agent_integre_est_refuse_sur_la_cle_refacturee_seulement(liste):
    from oto_mcp.capabilities.copilot import cle as copilot_cle
    from oto_mcp.capabilities.copilot.cle import Cle
    liste["blocs"] = [_bloc(7, "u-1", "member")]
    with pytest.raises(copilot_cle.SansCle) as e:
        copilot_cle.reserver(Cle("sk", None, "platform"), "copilot:t:000001", "u-1", 7,
                             "claude-sonnet-5", 1000)
    assert e.value.code == "daily_spend_limit" and e.value.statut == 429
    # La clé de l'org (aucun payeur refacturé) n'est jamais refusée.
    copilot_cle.reserver(Cle("sk", None, None), "copilot:t:000002", "u-1", 7,
                         "claude-sonnet-5", 1000)


# ── 4. sur une vraie base ───────────────────────────────────────────────────

def _org() -> int:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute("INSERT INTO orgs (name) VALUES (%s) RETURNING id",
                            (f"org-{uuid.uuid4().hex[:8]}",)).fetchone()["id"]


def test_la_liste_se_remplace_d_un_bloc_et_finit_seule(live):
    org = _org()
    spend_blocks.remplacer(org, [
        {"sub": "u-1", "scope": "member", "group_id": None, "until": DEMAIN},
        {"sub": "u-2", "scope": "member", "group_id": None,
         "until": MAINTENANT - timedelta(minutes=1)}])
    assert {b["sub"] for b in spend_blocks.de_l_org(org)} == {"u-1", "u-2"}
    actifs = [b for b in spend_blocks.actifs() if b["org_id"] == org]
    assert [b["sub"] for b in actifs] == ["u-1"], "un blocage fini n'est plus actif"
    assert actifs[0]["until"] == DEMAIN and actifs[0]["until"].tzinfo is not None
    spend_blocks.remplacer(org, [{"sub": "", "scope": "org", "group_id": None,
                                  "until": DEMAIN}])
    assert [(b["sub"], b["scope"]) for b in spend_blocks.de_l_org(org)] == [("", "org")]
    spend_blocks.remplacer(org, [])
    assert spend_blocks.de_l_org(org) == []


def test_la_base_refuse_une_portee_incoherente(live):
    import psycopg
    org = _org()
    with pytest.raises(psycopg.errors.CheckViolation):
        spend_blocks.remplacer(org, [{"sub": "", "scope": "member", "group_id": None,
                                      "until": DEMAIN}])


def test_le_mouvement_d_un_appel_dit_qui_l_a_fait(live):
    from oto_mcp.db import credits as db_credits
    org = _org()
    payeur = f"org:{org}"
    db_credits.verser(payeur, 1000, "pack", f"k-{uuid.uuid4().hex}")
    uid = f"copilot:{uuid.uuid4().hex}:000001"
    db_credits.reserver(call_uid=uid, payer=payeur, connector="anthropic",
                        function="messages:claude-sonnet-5", unit="output_tokens",
                        scale_version=1, quantity=100, reserved=10, deadline_s=600,
                        forcer=True, acting_sub="u-qui-agit")
    db_credits.regler(uid, montant=7, quantite=80, meta={})
    # Le flux ne sert que sous l'horizon des transactions OUVERTES du serveur (une suite
    # parallèle en tient) : on relit jusqu'à ce qu'il ait passé notre règlement.
    import time
    for _ in range(100):
        lu = db_credits.page_de_mouvements(0, 0, 1000)
        mien = [m for m in lu["mouvements"] if m["call_uid"] == uid]
        if mien:
            break
        time.sleep(0.1)
    assert [(m["kind"], m["payer"], m["sub"]) for m in mien] == [
        ("settle", payeur, "u-qui-agit")]
    versement = [m for m in lu["mouvements"] if m["payer"] == payeur
                 and m["kind"] == "deposit"]
    assert versement and versement[0]["sub"] is None, "un versement n'a pas d'auteur"


def test_le_releve_de_service_nomme_la_personne_pas_celui_des_membres(live):
    org = _org()
    sub = f"u-{uuid.uuid4().hex[:8]}"
    db.upsert_user(sub, email=f"{sub}@exemple.test")
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO tool_calls (sub, org_id, tool, ok, key_mode, call_uid) "
                     "VALUES (%s, %s, 'serper_search', TRUE, 'platform', %s)",
                     (sub, org, uuid.uuid4().hex))
    service = db.list_billable_calls_for_org(org, "serper_search", avec_personne=True)
    assert [c["sub"] for c in service["calls"]] == [sub]
    membre = db.list_billable_calls_for_org(org, "serper_search")
    assert membre["total"] == 1 and "sub" not in membre["calls"][0]


def test_le_commerce_lit_chaque_equipe_avec_tous_ses_membres(live):
    """Le pot commun d'une équipe : tous ses membres, quelle que soit leur équipe active."""
    from oto_mcp import group_store
    org = _org()
    g = group_store.create_group(org, "Ventes")
    for sub in ("u-a", "u-b"):
        db.upsert_user(sub, email=f"{sub}@exemple.test")
        group_store.add_group_member(g, sub)
    cap = _cap("service.org.groups")
    ctx = ResolvedCtx(sub="service:m2m-commerce", org_id=None, role="service:commerce")
    lu = cap.handler(ctx, cap.Input(org_id=org))
    assert [(x["name"], sorted(x["members"])) for x in lu["groups"]] == [
        ("Ventes", ["u-a", "u-b"])]
