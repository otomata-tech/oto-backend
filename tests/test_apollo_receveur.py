"""Le receveur des téléphones Apollo — `apollo_receiver.py`, `api/receveurs.py`.

Ce que ce banc fige, sur une VRAIE base (le DDL servi, `live`) et la vraie route :

1. **Le jeton EST l'autorisation.** Un jeton jamais émis, abandonné ou expiré rend
   404 et n'écrit rien ; le jeton n'est stocké qu'en empreinte.
2. **Une livraison se garde une fois.** Apollo réessaie : la seconde livraison
   répond 200 `duplicate`, et le premier corps reste — rien n'est écrasé.
3. **Le corps est borné AVANT d'être lu en entier** (413), et un corps illisible
   se refuse (400).
4. **La lecture passe par oto d'abord**, Apollo en repli : un reveal livré se lit sans
   appeler Apollo ; un reveal pas encore livré sonde Apollo. La forme servie ne
   change pas (`result.webhook_result.people[].phone_numbers[]`).
5. **Lit qui a accès au connecteur, par la clé qui a payé** : le lecteur résout sa clé
   Apollo avant toute lecture ; une autre clé (autre org, autre compte sans org) ne lit
   rien, et sans accès au connecteur la lecture est refusée.
6. **`webhook_url` n'est plus au schéma mais reste accepté** — à travers la vraie
   validation de FastMCP, pas en appelant la fonction nue — et la réponse le dit.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from unittest.mock import MagicMock

import pytest

_SUB, _ORG, _AUTRE_ORG = "sub-receveur", 7101, 7102
_RID = "-8351464734221602674"
_CORPS = {
    "request_id": -8351464734221602674,
    "webhook_status": "success",
    "webhook_result": {"people": [
        {"id": "ap-1", "phone_numbers": [
            {"sanitized_number": "+33600000001", "type_cd": "work_direct",
             "dnc_status_cd": "not_on_dnc"},
            {"sanitized_number": "+33600000002", "type_cd": "mobile",
             "dnc_status_cd": "not_on_dnc"}]}]},
}


def _cle(mode: str, entite: str | None):
    from oto_mcp.access.resolved_credential import ResolvedCredential
    return ResolvedCredential(provider="apollo", secret="k", is_platform=False,
                              mode=mode, entity_type=mode if entite else None,
                              entity_id=entite)


#: La clé Apollo de l'org du commanditaire : celle qui paie ses reveals.
_CLE = _cle("org", str(_ORG))


@pytest.fixture
def commanditaire(live, monkeypatch):
    """L'appelant d'un reveal : un compte, une org, l'adresse publique de l'instance."""
    from oto_mcp import access
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.acme.test")
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: _SUB)
    monkeypatch.setattr(access, "current_org", lambda sub: _ORG)


@pytest.fixture
def http():
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.testclient import TestClient

    from oto_mcp.api import receveurs
    app = Starlette(routes=receveurs.make_routes(lambda r: Response(status_code=204)))
    return TestClient(app)


def _chemin(url: str) -> str:
    return url.split("mcp.acme.test", 1)[1]


def _ligne(jeton: str) -> dict | None:
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM apollo_phone_reveals WHERE token_hash = %s",
            (hashlib.sha256(jeton.encode()).hexdigest(),)).fetchone()


# ── 1. le jeton est l'autorisation ──────────────────────────────────────────

def test_un_jeton_inconnu_rend_404_et_n_ecrit_rien(commanditaire, http):
    r = http.post("/api/receivers/apollo/phones/jamais-emis", json=_CORPS)
    assert r.status_code == 404
    assert r.json()["error"] == "receiver_not_found"


def test_le_jeton_n_est_stocke_qu_en_empreinte(commanditaire):
    from oto_mcp import apollo_receiver
    from oto_mcp.db._conn import _connect
    jeton, url = apollo_receiver.commander(_CLE)
    assert url.endswith(f"/api/receivers/apollo/phones/{jeton}")
    assert len(jeton) >= 40, "256 bits d'aléa, pas un identifiant devinable"
    with _connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM apollo_phone_reveals "
                         "WHERE token_hash = %s", (jeton,)).fetchone()["n"]
    assert n == 0, "le jeton en clair ne doit être nulle part en base"
    assert _ligne(jeton)["sub"] == _SUB and _ligne(jeton)["org_id"] == _ORG


def test_une_commande_abandonnee_ne_recoit_plus(commanditaire, http):
    from oto_mcp import apollo_receiver
    jeton, url = apollo_receiver.commander(_CLE)
    apollo_receiver.abandonner(jeton)
    assert http.post(_chemin(url), json=_CORPS).status_code == 404


def test_une_commande_expiree_ne_recoit_plus_et_se_purge(commanditaire, http):
    from oto_mcp import apollo_receiver
    from oto_mcp.db import apollo_reveals
    from oto_mcp.db._conn import _connect
    jeton, url = apollo_receiver.commander(_CLE)
    with _connect() as conn:
        conn.execute("UPDATE apollo_phone_reveals SET expires_at = NOW() - "
                     "INTERVAL '1 second' WHERE token_hash = %s",
                     (apollo_receiver.empreinte(jeton),))
    assert http.post(_chemin(url), json=_CORPS).status_code == 404
    assert apollo_reveals.compter_purgeables() >= 1
    apollo_reveals.purger()
    assert _ligne(jeton) is None


# ── 2. une livraison se garde une fois ──────────────────────────────────────

def test_la_livraison_est_gardee_puis_la_retentative_n_ecrase_rien(commanditaire, http):
    from oto_mcp import apollo_receiver
    jeton, url = apollo_receiver.commander(_CLE)
    apollo_receiver.lier(jeton, _RID)

    r1 = http.post(_chemin(url), json=_CORPS)
    assert r1.status_code == 200 and r1.json() == {"received": True, "duplicate": False}

    autre = {**_CORPS, "webhook_status": "retry"}
    r2 = http.post(_chemin(url), json=autre)
    assert r2.status_code == 200 and r2.json() == {"received": True, "duplicate": True}

    ligne = _ligne(jeton)
    assert ligne["deliveries"] == 2
    assert ligne["payload"]["webhook_status"] == "success", "le PREMIER corps reste"


def test_un_post_arrive_avant_la_fin_de_l_appel_porte_son_request_id(commanditaire, http):
    """Apollo peut livrer avant de nous avoir rendu la main : l'id du corps complète
    la commande, et l'appel qui le rattache ensuite ne la contredit pas."""
    from oto_mcp import apollo_receiver
    jeton, url = apollo_receiver.commander(_CLE)
    corps = {**_CORPS, "request_id": 5150}
    assert http.post(_chemin(url), json=corps).status_code == 200
    assert _ligne(jeton)["request_id"] == "5150"
    apollo_receiver.lier(jeton, 5150)
    assert apollo_receiver.resultat_recu("5150", _CLE)["webhook_status"] == "success"


def test_un_request_id_qui_revient_ne_fait_jamais_echouer_la_livraison(commanditaire,
                                                                       http):
    """Trouvé par ce banc : sous un index UNIQUE, la seconde commande au même
    identifiant faisait répondre 500 au POST — qu'Apollo réessaie sans fin. La
    livraison se garde, et la lecture prend la commande livrée la plus récente."""
    from oto_mcp import apollo_receiver
    j1, u1 = apollo_receiver.commander(_CLE)
    apollo_receiver.lier(j1, 6160)
    j2, u2 = apollo_receiver.commander(_CLE)
    apollo_receiver.lier(j2, 6160)
    assert http.post(_chemin(u1), json={**_CORPS, "request_id": 6160,
                                        "webhook_status": "premier"}).status_code == 200
    assert http.post(_chemin(u2), json={**_CORPS, "request_id": 6160,
                                        "webhook_status": "second"}).status_code == 200
    assert apollo_receiver.resultat_recu("6160", _CLE)["webhook_status"] == "second"


# ── 3. le corps ─────────────────────────────────────────────────────────────

def test_un_corps_trop_gros_est_refuse_sans_etre_garde(commanditaire, http, monkeypatch):
    from oto_mcp import apollo_receiver
    monkeypatch.setattr(apollo_receiver, "CORPS_MAX", 64)
    jeton, url = apollo_receiver.commander(_CLE)
    r = http.post(_chemin(url), content=json.dumps(_CORPS).encode(),
                  headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert _ligne(jeton)["payload"] is None


def test_un_corps_trop_gros_EN_FLUX_est_refuse_aussi(commanditaire, http, monkeypatch):
    """Sans `content-length` : la borne se juge en lisant, morceau par morceau."""
    from oto_mcp import apollo_receiver
    monkeypatch.setattr(apollo_receiver, "CORPS_MAX", 64)
    jeton, url = apollo_receiver.commander(_CLE)

    def morceaux():
        for _ in range(10):
            yield b"x" * 32

    r = http.post(_chemin(url), content=morceaux())
    assert r.status_code == 413
    assert _ligne(jeton)["payload"] is None


def test_un_corps_illisible_est_refuse(commanditaire, http):
    from oto_mcp import apollo_receiver
    jeton, url = apollo_receiver.commander(_CLE)
    r = http.post(_chemin(url), content=b"pas du json",
                  headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert _ligne(jeton)["deliveries"] == 0


# ── 4-5. la lecture : oto d'abord, Apollo en repli, l'org dans la clé ───────

def _outil_resultat(monkeypatch, poll_return, cle=None):
    import oto.tools.apollo.client as apollo_client
    from fastmcp import FastMCP

    from oto_mcp import access
    from oto_mcp.tools import apollo as apollo_tool
    client = MagicMock()
    client.poll_webhook_result.return_value = poll_return
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: cle or _CLE)
    monkeypatch.setattr(apollo_client, "ApolloClient", lambda **kw: client)
    m = FastMCP("t")
    apollo_tool.register(m)
    return asyncio.run(m.get_tool("apollo_reveal_phone_result")).fn, client


def test_un_reveal_livre_se_lit_chez_oto_sans_appeler_apollo(commanditaire, http,
                                                             monkeypatch):
    from oto_mcp import apollo_receiver
    jeton, url = apollo_receiver.commander(_CLE)
    apollo_receiver.lier(jeton, _RID)
    http.post(_chemin(url), json=_CORPS)

    fn, client = _outil_resultat(monkeypatch, {"done": False})
    out = fn(request_id=_RID)
    assert not client.poll_webhook_result.called, "livré chez oto : Apollo n'est pas sondé"
    assert out["done"] is True
    nums = out["result"]["webhook_result"]["people"][0]["phone_numbers"]
    assert nums[1]["sanitized_number"] == "+33600000002"
    assert nums[1]["dnc_status_cd"] == "not_on_dnc"
    assert out["result"]["request_id"] == _RID, "l'écho sort en chaîne, comme avant"


def test_un_reveal_pas_encore_livre_sonde_apollo(commanditaire, monkeypatch):
    from oto_mcp import apollo_receiver
    jeton, _ = apollo_receiver.commander(_CLE)
    apollo_receiver.lier(jeton, "718432950164203900")
    fn, client = _outil_resultat(monkeypatch, {"done": False, "retry_after_seconds": 12})
    out = fn(request_id="718432950164203900")
    assert client.poll_webhook_result.called, "rien de livré : le repli Apollo répond"
    assert out == {"done": False, "retry_after_seconds": 12,
                   "next_step": out["next_step"]}


def test_une_autre_cle_ne_lit_pas_le_reveal(commanditaire, http):
    """Autre org : sa clé n'est pas celle qui a payé, le reveal ne se lit pas."""
    from oto_mcp import apollo_receiver
    jeton, url = apollo_receiver.commander(_CLE)
    apollo_receiver.lier(jeton, "4242")
    http.post(_chemin(url), json={**_CORPS, "request_id": 4242})
    assert apollo_receiver.resultat_recu("4242", _CLE) is not None
    assert apollo_receiver.resultat_recu("4242", _cle("org", str(_AUTRE_ORG))) is None


def test_deux_comptes_sans_org_ne_se_lisent_pas(commanditaire, http):
    """Sans org, chacun sur sa propre clé : rien n'est partagé par défaut d'org."""
    from oto_mcp import apollo_receiver
    a, b = _cle("user", "sub-a"), _cle("user", "sub-b")
    jeton, url = apollo_receiver.commander(a)
    apollo_receiver.lier(jeton, "4343")
    http.post(_chemin(url), json={**_CORPS, "request_id": 4343})
    assert apollo_receiver.resultat_recu("4343", a) is not None
    assert apollo_receiver.resultat_recu("4343", b) is None


def test_un_membre_qui_resout_la_cle_de_l_org_lit_le_reveal(commanditaire, http,
                                                            monkeypatch):
    """Un collègue de l'org, qui atteint Apollo par la clé de l'org, lit le reveal."""
    from oto_mcp import access, apollo_receiver
    jeton, url = apollo_receiver.commander(_CLE)
    apollo_receiver.lier(jeton, "4444")
    http.post(_chemin(url), json={**_CORPS, "request_id": 4444})
    monkeypatch.setattr(access, "current_user_sub_or_raise", lambda: "sub-collegue")
    fn, client = _outil_resultat(monkeypatch, {"done": False},
                                 cle=_cle("org", str(_ORG)))
    out = fn(request_id="4444")
    assert out["done"] is True and not client.poll_webhook_result.called


def test_sans_acces_au_connecteur_la_lecture_est_refusee_avant_la_table(
        commanditaire, http, monkeypatch):
    """Sans clé Apollo résolue, rien n'est lu — ni chez oto, ni chez Apollo."""
    import oto.tools.apollo.client as apollo_client
    from fastmcp import FastMCP

    from oto_mcp import access, apollo_receiver
    from mcp.types import ErrorData

    from oto_mcp.mcp_errors import McpError
    from oto_mcp.tools import apollo as apollo_tool
    jeton, url = apollo_receiver.commander(_CLE)
    apollo_receiver.lier(jeton, "4545")
    http.post(_chemin(url), json={**_CORPS, "request_id": 4545})
    lus: list = []
    monkeypatch.setattr(apollo_receiver, "resultat_recu",
                        lambda rid, cle: lus.append(rid))

    def _refus(*a, **k):
        raise McpError(ErrorData(code=-32602, message="No `apollo` credential configured for you"))
    monkeypatch.setattr(access, "resolve_credential", _refus)
    monkeypatch.setattr(apollo_client, "ApolloClient", lambda **kw: MagicMock())
    m = FastMCP("t")
    apollo_tool.register(m)
    with pytest.raises(McpError):
        asyncio.run(m.get_tool("apollo_reveal_phone_result")).fn(request_id="4545")
    assert lus == [], "la table n'est pas lue sans accès au connecteur"


def test_l_id_rendu_par_l_appel_fait_foi_sur_celui_du_post(commanditaire, http):
    """Le POST arrive avant la fin de l'appel, avec l'id abîmé par le float64 : l'id
    rendu par l'appel le remplace, et le reveal se lit par l'id exact."""
    from oto_mcp import apollo_receiver
    jeton, url = apollo_receiver.commander(_CLE)
    abime = {**_CORPS, "request_id": -8351464734221603000.0}
    assert http.post(_chemin(url), json=abime).status_code == 200
    apollo_receiver.lier(jeton, _RID)
    assert _ligne(jeton)["request_id"] == _RID
    assert apollo_receiver.resultat_recu(_RID, _CLE) is not None


def test_un_jeton_mal_forme_est_refuse_sans_lire_le_corps(commanditaire, http,
                                                           monkeypatch):
    """Un jeton qui n'a pas la forme émise rend 404 AVANT la lecture du corps : un
    corps au-delà du plafond ne donne pas 413."""
    from oto_mcp import apollo_receiver
    monkeypatch.setattr(apollo_receiver, "CORPS_MAX", 8)
    r = http.post("/api/receivers/apollo/phones/pas-un-jeton", content=b"x" * 64)
    assert r.status_code == 404


def test_un_people_de_premier_niveau_est_range_sous_webhook_result():
    """La seule forme de repli reconnue — nommée, pour que le chemin servi tienne."""
    from oto_mcp import apollo_receiver
    env = apollo_receiver.enveloppe({"request_id": 1, "people": [{"id": "x"}]})
    assert env == {"request_id": 1, "webhook_result": {"people": [{"id": "x"}]}}
    assert apollo_receiver.enveloppe(_CORPS) is _CORPS


# ── 6. `webhook_url` : hors du schéma, toujours accepté ─────────────────────

def test_webhook_url_n_est_plus_au_schema_mais_reste_accepte(monkeypatch):
    """Par un vrai client MCP : c'est la validation de FastMCP qui refusait un
    argument inconnu, et c'est elle qu'on traverse. Le jour où `exclude_args`
    disparaît de FastMCP, ce test rougit."""
    import oto.tools.apollo.client as apollo_client
    from fastmcp import Client, FastMCP

    from oto_mcp import access, apollo_receiver
    from oto_mcp.tools import apollo as apollo_tool

    client = MagicMock()
    client.match_person.return_value = {"person": {"id": "p1"}, "request_id": 11}
    client.bulk_match_people.return_value = {"matches": [{"id": "p1"}], "request_id": 12}
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: MagicMock(key="k"))
    monkeypatch.setattr(apollo_client, "ApolloClient", lambda **kw: client)
    monkeypatch.setattr(apollo_receiver, "commander",
                        lambda cle: ("j", "https://mcp.acme.test/api/receivers/apollo/phones/j"))
    monkeypatch.setattr(apollo_receiver, "lier", lambda j, rid: None)
    m = FastMCP("t")
    apollo_tool.register(m)

    async def scenario():
        async with Client(m) as c:
            outils = {t.name: t for t in await c.list_tools()}
            for nom in ("apollo_reveal_phone", "apollo_bulk_match"):
                assert "webhook_url" not in outils[nom].inputSchema["properties"], nom
                assert "webhook_url" not in json.dumps(outils[nom].description), nom
            a = await c.call_tool("apollo_reveal_phone", {
                "person_id": "p1", "webhook_url": "https://hooks.acme.test/x"})
            b = await c.call_tool("apollo_bulk_match", {
                "people": [{"id": "p1"}], "reveal_phone_number": True,
                "webhook_url": "https://hooks.acme.test/x"})
            sans = await c.call_tool("apollo_reveal_phone", {"person_id": "p1"})
            return a.structured_content, b.structured_content, sans.structured_content

    a, b, sans = asyncio.run(scenario())
    assert "webhook_url" in a["deprecation"] and "webhook_url" in b["deprecation"]
    assert "deprecation" not in sans
    for appel in (client.match_person, client.bulk_match_people):
        assert appel.call_args.kwargs["webhook_url"].startswith(
            "https://mcp.acme.test/api/receivers/apollo/phones/"), (
            "Apollo reçoit l'URL d'oto, jamais celle de l'appelant")


def test_la_commande_est_abandonnee_quand_apollo_ne_trouve_personne(monkeypatch):
    import oto.tools.apollo.client as apollo_client
    from fastmcp import FastMCP

    from oto_mcp import access, apollo_receiver
    from oto_mcp.tools import apollo as apollo_tool
    client = MagicMock()
    client.match_person.return_value = None
    gestes: list = []
    monkeypatch.setattr(access, "resolve_credential", lambda *a, **k: MagicMock(key="k"))
    monkeypatch.setattr(apollo_client, "ApolloClient", lambda **kw: client)
    monkeypatch.setattr(apollo_receiver, "commander", lambda cle: ("j", "https://x.test/r/j"))
    monkeypatch.setattr(apollo_receiver, "abandonner", lambda j: gestes.append(("abandon", j)))
    monkeypatch.setattr(apollo_receiver, "lier", lambda j, rid: gestes.append(("lier", j)))
    m = FastMCP("t")
    apollo_tool.register(m)
    out = asyncio.run(m.get_tool("apollo_reveal_phone")).fn(person_id="p1")
    assert out["matched"] is False
    assert gestes == [("abandon", "j")]


# ── 7. écrire les numéros dans le tableau, rendre des comptes ───────────────

def _tableau(lignes: list[dict]) -> tuple[str, list[str]]:
    import uuid

    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = str(db.create_datastore("user", _SUB, "prospects-" + uuid.uuid4().hex[:6]))
    st = make_store(_SUB)
    # Les colonnes des lignes et celles où les numéros s'écrivent, déclarées.
    colonnes = sorted({k for r in lignes for k in r} | {"phone", "tel"})
    st.set_schema(ns, {"fields": [{"key": k} for k in colonnes]})
    return ns, [str(st.append_row(ns, dict(r))["_id"]) for r in lignes]


def _livre(http, rid: int, corps: dict) -> None:
    from oto_mcp import apollo_receiver
    jeton, url = apollo_receiver.commander(_CLE)
    apollo_receiver.lier(jeton, rid)
    assert http.post(_chemin(url), json={**corps, "request_id": rid}).status_code == 200


def test_les_numeros_vont_dans_la_ligne_nommee_et_seuls_des_comptes_reviennent(
        commanditaire, http, monkeypatch):
    from oto_mcp.datastore.core import make_store
    ns, ids = _tableau([{"nom_societe": "Acme"}])
    _livre(http, 7070, _CORPS)
    fn, client = _outil_resultat(monkeypatch, {"done": False})

    out = fn(request_id="7070", datastore=ns, row_id=ids[0])
    assert not client.poll_webhook_result.called
    assert out["done"] is True and "result" not in out
    assert out["written"]["rows_written"] == 1 and out["written"]["with_numbers"] == 1
    assert "+336" not in json.dumps(out), "aucun numéro ne revient à l'agent"
    ligne = make_store(_SUB).get_row(ns, ids[0])
    assert ligne["phone"] == "+33600000002", "le mobile passe avant la ligne directe"
    assert "mobile" in ligne["phone.comment"]
    assert "dnc: not_on_dnc" in ligne["phone.comment"], "l'opposition se lit avec le numéro"


def test_un_lot_se_rapproche_par_l_id_apollo_de_chaque_personne(commanditaire, http,
                                                                monkeypatch):
    from oto_mcp.datastore.core import make_store
    ns, ids = _tableau([{"apollo_id": "ap-1"}, {"apollo_id": "ap-2"},
                        {"apollo_id": "ap-3"}])
    corps = {"webhook_result": {"people": [
        {"id": "ap-1", "phone_numbers": [{"sanitized_number": "+33700000001",
                                          "type_cd": "mobile"}]},
        {"id": "ap-2", "phone_numbers": []},
        {"id": "ap-9", "phone_numbers": [{"sanitized_number": "+33700000009"}]}]}}
    _livre(http, 7171, corps)
    fn, _ = _outil_resultat(monkeypatch, {"done": False})

    w = fn(request_id="7171", datastore=ns, match_column="apollo_id",
           phone_column="tel")["written"]
    assert w == {**w, "people": 3, "with_numbers": 2, "rows_written": 1,
                 "people_without_rows": 1, "errors": []}
    st = make_store(_SUB)
    assert st.get_row(ns, ids[0])["tel"] == "+33700000001"
    assert st.get_row(ns, ids[1]).get("tel") is None, "sans numéro : rien d'écrit"


def test_une_seule_ligne_ne_recoit_pas_un_lot(commanditaire, http, monkeypatch):
    from oto_mcp.mcp_errors import McpError
    ns, ids = _tableau([{"x": 1}])
    corps = {"webhook_result": {"people": [{"id": "a"}, {"id": "b"}]}}
    _livre(http, 7272, corps)
    fn, _ = _outil_resultat(monkeypatch, {"done": False})
    with pytest.raises(McpError) as e:
        fn(request_id="7272", datastore=ns, row_id=ids[0])
    assert e.value.error.data["code"] == "apollo_phone_target"
