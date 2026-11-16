"""`GET /api/me/connectors` : les clés des AUTRES orgs du membre se lisent en un lot.

`access.reachable_instances_map` annote le catalogue des instances « à portée » (une
clé posée dans une autre org dont on est membre). Elle lisait les clés de chaque autre
org une par une : une requête par org. Mesuré en production sur un compte membre
d'une quarantaine d'orgs : 470 à 690 ms pour une lecture isolée de la route, ~1 s
pendant le chargement d'une page ; sur le jeu de `_jeu_connecteurs_1148.py` grossi de
40 orgs, 94 requêtes par appel au lieu de 50, et ~440 ms au lieu de ~210 à 3 ms
l'aller-retour.

Ce que ce fichier tient :
1. `credentials_store.list_credentials_many` rend, entité par entité, EXACTEMENT ce
   que `list_credentials` rend (colonnes, `meta` filtré, ordre, multi-compte) ;
2. la carte est la MÊME que celle que donnaient les lectures entité par entité ;
3. le nombre de requêtes de la route ne suit plus le nombre d'orgs du membre.
"""
from __future__ import annotations

import json
import os

import pytest

import _jeu_connecteurs_1148 as jeu_1148


@pytest.fixture(scope="module")
def jeu(live):
    import _mcp_app
    _mcp_app.static_mcp()
    avant = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["OTO_MCP_MASTER_KEY"] = "7" * 64
    try:
        j = jeu_1148.peupler()
        _orgs_en_plus(j, 0, 6)
        yield j
    finally:
        if avant is None:
            os.environ.pop("OTO_MCP_MASTER_KEY", None)
        else:
            os.environ["OTO_MCP_MASTER_KEY"] = avant


def _orgs_en_plus(j, debut: int, n: int) -> list[int]:
    """`n` orgs de plus dont le membre fait partie, chacune avec des clés d'org — dont
    une multi-compte, et un `meta` qui porte un satellite secret que la liste filtre."""
    from oto_mcp import org_store
    partage = jeu_1148._eligibles("org")
    poses = []
    for i in range(debut, debut + n):
        o = org_store.create_org(f"org-lot-{i}", created_by=j.sub)
        org_store.add_org_member(o, j.sub, "org_member")
        for k in range(3):
            jeu_1148._cle("org", o, partage[(i * 3 + k) % len(partage)])
        jeu_1148._cle("org", o, partage[i % len(partage)], account="second")
        jeu_1148._cle("org", o, "google", account=f"lot{i}@example.invalid",
                      meta={"scopes": "openid", "connected_by": j.sub,
                            "access_token": "jeton-factice"})
        poses.append(o)
    return poses


def _compter(f):
    import psycopg
    from unittest import mock
    vues = []
    reel = psycopg.Cursor.execute

    def _ex(self, query, *a, **k):
        vues.append(query)
        return reel(self, query, *a, **k)
    with mock.patch.object(psycopg.Cursor, "execute", _ex):
        out = f()
    return out, len(vues)


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def test_le_lot_rend_ce_que_rend_la_lecture_entite_par_entite(jeu):
    from oto_mcp import credentials_store as cs, group_store, org_store
    orgs = [str(o["org_id"]) for o in org_store.list_orgs_for_user(jeu.sub)]
    equipes = [str(g["id"]) for g in group_store.list_groups(jeu.org)]
    for et, ids in (("org", orgs + ["999999"]), ("group", equipes)):
        lot = cs.list_credentials_many(et, ids)
        attendu = {i: cs.list_credentials(et, i) for i in ids if cs.list_credentials(et, i)}
        assert _json(dict(sorted(lot.items()))) == _json(dict(sorted(attendu.items()))), et
    lot = cs.list_credentials_many("org", orgs)
    assert sum(len(v) for v in lot.values()) > 20, "le jeu ne pose presque rien"
    assert any(r["account"] == "second" for v in lot.values() for r in v), "multi-compte absent"
    assert not any("access_token" in (r["meta"] or {}) for v in lot.values() for r in v)
    assert cs.list_credentials_many("org", []) == {}


def _carte_entite_par_entite(sub, org):
    """La carte telle que la lisait le code d'avant : une lecture par équipe, une par
    org (`list_group_secrets`, `list_org_secrets`). L'oracle de la comparaison."""
    from oto_mcp import group_store, org_store, providers, roles
    from oto_mcp.access import cascade
    out: dict = {}
    groups = list(group_store.list_groups_for_user(sub, org)) if org is not None else []
    if org is not None and roles.is_org_admin(sub, org):
        known = {g["group_id"] for g in groups}
        groups += [{"group_id": g["id"], "name": g["name"]}
                   for g in group_store.list_groups(org) if g["id"] not in known]
    vus = set()
    for g in groups:
        if g["group_id"] in vus:
            continue
        vus.add(g["group_id"])
        for s in group_store.list_group_secrets(g["group_id"]):
            if s.get("provider") in cascade.ORG_SHAREABLE_PROVIDERS:
                out.setdefault(s["provider"], []).append(
                    {"kind": "group", "id": g["group_id"], "name": g["name"]})
    for o in org_store.list_orgs_for_user(sub):
        if o["org_id"] == org:
            continue
        for s in org_store.list_org_secrets(o["org_id"]):
            if s.get("provider") in cascade.ORG_SHAREABLE_PROVIDERS:
                out.setdefault(s["provider"], []).append(
                    {"kind": "org", "id": o["org_id"], "name": o.get("name") or f"org {o['org_id']}"})
    for c in providers._REGISTRY_LIST:
        if c.credential_of and c.credential_of in out:
            out[c.name] = list(out[c.credential_of])
    return out


@pytest.mark.parametrize("org_active", ["active", "aucune"])
def test_la_carte_est_la_meme_qu_entite_par_entite(jeu, org_active):
    from oto_mcp import access
    org = jeu.org if org_active == "active" else None
    carte = access.reachable_instances_map(jeu.sub, org)
    assert carte, "la carte est vide : la comparaison ne prouve rien"
    assert any(i["kind"] == "org" for v in carte.values() for i in v)
    if org is not None:
        assert any(i["kind"] == "group" for v in carte.values() for i in v)
    assert _json(carte) == _json(_carte_entite_par_entite(jeu.sub, org))


def test_le_nombre_de_requetes_ne_suit_pas_le_nombre_d_orgs(jeu):
    """Douze orgs de plus, chacune avec ses clés : pas une requête de plus, ni sur la
    carte, ni sur la route (verbeuse ou compacte). Rouge sur le code d'avant : +12."""
    from oto_mcp import access, db
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.connectors import selection as S
    ctx = ResolvedCtx(sub=jeu.sub, org_id=jeu.org)

    def _carte():
        with db.reuse_connection():
            return access.reachable_instances_map(jeu.sub, jeu.org)
    appels = {"carte": _carte,
              "verbeux": lambda: S._me(ctx, S.MyConnectorsInput(verbose=True)),
              "compact": lambda: S._me(ctx, S.MyConnectorsInput())}
    for f in appels.values():   # à chaud : un premier appel du processus lit plus
        f()
    avant = {nom: _compter(f)[1] for nom, f in appels.items()}
    carte_avant = _carte()
    _orgs_en_plus(jeu, 100, 12)
    apres = {nom: _compter(f)[1] for nom, f in appels.items()}
    assert apres == avant, f"le compte de requêtes suit les orgs du membre : {avant} → {apres}"
    assert sum(len(v) for v in _carte().values()) > sum(len(v) for v in carte_avant.values()), (
        "les orgs ajoutées n'apparaissent pas dans la carte : le banc ne mesure rien")


# ── `roster=true` : la liste qui ne peint que logos, libellés et états ─────────────
# Même jeu : un membre de plusieurs orgs et équipes, des clés à tous les paliers. Le
# mode saute les trois enrichissements propres au demandeur (présence de credential,
# instances à portée, aptitude du flux de connexion) et le DIT dans l'enveloppe.

_PROPRES_AU_DEMANDEUR = {"credential", "reachable_instances"}
_ENRICHISSEMENT_CONNECT = {"callback_url", "app_ready", "app_ready_scopes", "fixed",
                           "warnings", "app_owner", "ready_options"}


def _sans_enrichissement(row: dict) -> dict:
    out = {k: v for k, v in row.items() if k not in _PROPRES_AU_DEMANDEUR}
    if isinstance(out.get("connect"), dict):
        out["connect"] = {k: v for k, v in out["connect"].items()
                          if k not in _ENRICHISSEMENT_CONNECT}
    return out


@pytest.mark.parametrize("include", [None, ["card"]], ids=["compact", "card"])
def test_roster_rend_les_memes_lignes_moins_l_enrichissement(jeu, include):
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.connectors import selection as S
    ctx = ResolvedCtx(sub=jeu.sub, org_id=jeu.org)
    plein = S._me(ctx, S.MyConnectorsInput(include=include))
    leger = S._me(ctx, S.MyConnectorsInput(include=include, roster=True))
    assert any(_PROPRES_AU_DEMANDEUR & r.keys() for r in plein["connectors"]), (
        "le jeu n'a ni clé ni instance à portée : la comparaison ne prouve rien")
    assert not any(_PROPRES_AU_DEMANDEUR & r.keys() for r in leger["connectors"])
    assert _json([_sans_enrichissement(r) for r in leger["connectors"]]) == \
        _json([_sans_enrichissement(r) for r in plein["connectors"]])
    assert leger["credentials"] == "not_computed" and leger["reach"] == "not_computed"
    assert "reach" not in plein, "la lecture par défaut a changé de forme"


def test_roster_coute_peu_et_ne_suit_ni_les_orgs_ni_les_cles(jeu):
    from oto_mcp.capabilities._types import ResolvedCtx
    from oto_mcp.capabilities.connectors import selection as S
    ctx = ResolvedCtx(sub=jeu.sub, org_id=jeu.org)
    inp = S.MyConnectorsInput(include=["card"], roster=True)
    S._me(ctx, inp)
    _, avant = _compter(lambda: S._me(ctx, inp))
    _, plein = _compter(lambda: S._me(ctx, S.MyConnectorsInput(include=["card"])))
    assert avant <= 20 and avant * 2 < plein, (avant, plein)
    _orgs_en_plus(jeu, 200, 5)
    _, apres = _compter(lambda: S._me(ctx, inp))
    assert apres == avant, (avant, apres)


def test_roster_refuse_une_lecture_ciblee():
    from pydantic import ValidationError
    from oto_mcp.capabilities.connectors import selection as S
    with pytest.raises(ValidationError, match="roster"):
        S.MyConnectorsInput(roster=True, name="linkedin")
