"""Un mot de passe changé chez Logto ferme ce qu'il ouvrait — `POST /api/logto/webhook`.

On joue la VRAIE route (Starlette) sur des corps signés comme Logto les signe (HMAC-SHA256
du corps brut, digest hexadécimal dans `logto-signature-sha-256`) ; la base et la
Management API sont simulées. Un cas par refus, un cas par événement, et la fermeture
Logto vérifiée appel par appel (route, paramètre `revokeGrantsTarget=all`).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from oto_mcp import tenancy
from oto_mcp.api import logto_webhook
from oto_mcp.auth import annuaire, revocation_mot_de_passe as rmp

CLE = "cle-de-signature-de-test"
SUB = "u-logto-1"


def _maintenant() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _signe(corps: bytes, cle: str = CLE) -> str:
    return hmac.new(cle.encode(), corps, hashlib.sha256).hexdigest()


def _reinit_pwd() -> dict:
    return {"hookId": "h1", "event": "PostResetPassword", "createdAt": _maintenant(),
            "interactionEvent": "ForgotPassword", "userId": SUB,
            "user": {"id": SUB}}


def _pwd_par_management() -> dict:
    return {"hookId": "h1", "event": "User.Data.Updated", "createdAt": _maintenant(),
            "path": f"/users/{SUB}/password", "method": "PATCH", "status": 200,
            "matchedRoute": "/users/:userId/password", "params": {"userId": SUB},
            "data": {"id": SUB}}


@pytest.fixture
def monde(monkeypatch):
    """La base et Logto simulés ; `vus` relève chaque geste dans l'ordre."""
    vus: list = []
    monkeypatch.setenv("OTO_LOGTO_WEBHOOK_SIGNING_KEY", CLE)
    jetons = [{"id": 1, "parent_id": None}, {"id": 2, "parent_id": 1},
              {"id": 3, "parent_id": None}]
    monkeypatch.setattr(rmp.db, "list_api_tokens",
                        lambda sub, kinds=("user",), **kw:
                        vus.append(("list", sub, kinds)) or list(jetons))
    monkeypatch.setattr(rmp.db, "revoke_api_token",
                        lambda sub, tid, revoked_by, reason, parent_id=None:
                        vus.append(("revoke", sub, tid, revoked_by, reason)) or True)
    monkeypatch.setattr(rmp.annuaire, "fermer_sessions",
                        lambda sub: vus.append(("logto", sub))
                        or {"sessions": 2, "grants": 1})
    return vus


def _client() -> TestClient:
    return TestClient(Starlette(routes=logto_webhook.make_routes()),
                      raise_server_exceptions=False)


def _poste(evt, *, signature: "str | None | bool" = True, brut: bytes | None = None):
    corps = brut if brut is not None else json.dumps(evt).encode()
    entetes = {"content-type": "application/json"}
    if signature is True:
        entetes[rmp.ENTETE_SIGNATURE] = _signe(corps)
    elif signature:
        entetes[rmp.ENTETE_SIGNATURE] = signature
    return _client().post(logto_webhook.PATH, content=corps, headers=entetes)


# --- La signature --------------------------------------------------------------

def test_signature_valide_reinitialisation_revoque_et_ferme(monde):
    r = _poste(_reinit_pwd())
    assert r.status_code == 200, r.text
    assert r.json() == {"ignored": False, "event": "PostResetPassword",
                        "revoked_tokens": 3, "closed_sessions": 2, "revoked_grants": 1}
    assert ("logto", SUB) in monde


def test_signature_fausse_refusee_sans_rien_toucher(monde):
    r = _poste(_reinit_pwd(), signature=_signe(b"autre corps"))
    assert r.status_code == 401 and r.json()["error"] == "invalid_signature"
    assert monde == []


def test_signature_d_une_autre_cle_refusee(monde):
    corps = json.dumps(_reinit_pwd()).encode()
    r = _poste(None, brut=corps, signature=_signe(corps, "autre-cle"))
    assert r.status_code == 401 and r.json()["error"] == "invalid_signature"
    assert monde == []


def test_signature_absente_refusee(monde):
    r = _poste(_reinit_pwd(), signature=None)
    assert r.status_code == 401 and r.json()["error"] == "invalid_signature"
    assert monde == []


def test_la_signature_porte_sur_les_octets_recus_pas_sur_le_json(monde):
    """Un espace de plus dans le corps signé ne change rien au JSON — et invalide la
    signature calculée sur l'autre forme : on juge les OCTETS."""
    evt = _reinit_pwd()
    signe_sur = json.dumps(evt).encode()
    envoye = json.dumps(evt, indent=1).encode()
    r = _poste(None, brut=envoye, signature=_signe(signe_sur))
    assert r.status_code == 401
    assert monde == []


def test_cle_absente_refuse_tout_meme_signe(monde, monkeypatch):
    monkeypatch.delenv("OTO_LOGTO_WEBHOOK_SIGNING_KEY")
    r = _poste(_reinit_pwd())
    assert r.status_code == 503 and r.json()["error"] == "logto_webhook_not_configured"
    assert monde == []


def test_cle_vide_vaut_absente(monde, monkeypatch):
    monkeypatch.setenv("OTO_LOGTO_WEBHOOK_SIGNING_KEY", "  ")
    r = _poste(_reinit_pwd(), signature=_signe(json.dumps(_reinit_pwd()).encode(), "  "))
    assert r.status_code == 503
    assert monde == []


# --- Le corps ------------------------------------------------------------------

def test_corps_illisible_mais_signe_refuse_nommement(monde):
    r = _poste(None, brut=b"{pas du json")
    assert r.status_code == 400 and r.json()["error"] == "invalid_event"
    assert monde == []


def test_corps_illisible_non_signe_dit_la_signature_d_abord(monde):
    r = _poste(None, brut=b"{pas du json", signature=None)
    assert r.status_code == 401


def test_livraison_ancienne_refusee(monde):
    evt = {**_reinit_pwd(), "createdAt": "2020-01-01T00:00:00.000Z"}
    r = _poste(evt)
    assert r.status_code == 400 and r.json()["error"] == "stale_event"
    assert monde == []


def test_livraison_sans_date_refusee(monde):
    evt = _reinit_pwd()
    del evt["createdAt"]
    r = _poste(evt)
    assert r.status_code == 400 and r.json()["error"] == "invalid_event"


# --- Les événements ------------------------------------------------------------

def test_changement_par_la_management_api_revoque(monde):
    r = _poste(_pwd_par_management())
    assert r.status_code == 200 and r.json()["revoked_tokens"] == 3, r.text
    assert ("logto", SUB) in monde


@pytest.mark.parametrize("evt", [
    # Une autre route de management : profil, données personnalisées, échéance du
    # mot de passe — pas un mot de passe changé.
    {"event": "User.Data.Updated", "method": "PATCH", "matchedRoute": "/users/:userId",
     "params": {"userId": SUB}, "data": {"id": SUB}},
    {"event": "User.Data.Updated", "method": "PATCH",
     "matchedRoute": "/users/:userId/password/expiration", "params": {"userId": SUB}},
    # L'Account API et l'Experience API : la livraison ne porte que l'entité, sans
    # route — un changement d'email ou de MFA y ressemble trait pour trait.
    {"event": "User.Data.Updated", "data": {"id": SUB}},
    {"event": "User.Created", "data": {"id": SUB}},
    {"event": "PostSignIn", "userId": SUB},
])
def test_evenement_qui_ne_dit_pas_un_mot_de_passe_change_est_ignore(monde, evt):
    r = _poste({**evt, "hookId": "h1", "createdAt": _maintenant()})
    assert r.status_code == 200 and r.json() == {"ignored": True, "event": evt["event"]}
    assert monde == [], "un événement ignoré ne touche ni la base ni Logto"


# --- La révocation des jetons --------------------------------------------------

def test_revocation_passe_par_le_geste_unique_avec_sa_trace(monde):
    _poste(_reinit_pwd())
    assert monde[0] == ("list", SUB, ("user", "delegation"))
    revoques = [e for e in monde if e[0] == "revoke"]
    assert [e[2] for e in revoques] == [2, 1, 3], "l'enfant avant son émetteur"
    assert {e[3] for e in revoques} == {rmp.REVOQUE_PAR}
    assert all("mot de passe" in e[4] for e in revoques)
    # Les jetons d'abord : si Logto tombe, ils sont déjà coupés.
    assert monde.index(("logto", SUB)) > monde.index(revoques[-1])


def test_logto_en_echec_rend_502_jetons_deja_coupes(monde, monkeypatch):
    def _tombe(sub):
        raise annuaire.FermetureInachevee("Logto injoignable")
    monkeypatch.setattr(rmp.annuaire, "fermer_sessions", _tombe)
    r = _poste(_reinit_pwd())
    assert r.status_code == 502 and r.json()["error"] == "logto_sessions_not_closed"
    assert len([e for e in monde if e[0] == "revoke"]) == 3


def test_panne_de_notre_cote_rend_500_nomme(monde, monkeypatch):
    def _base_tombee(sub, **kw):
        raise RuntimeError("base injoignable")
    monkeypatch.setattr(rmp.db, "list_api_tokens", _base_tombee)
    r = _poste(_reinit_pwd())
    assert r.status_code == 500 and r.json()["error"] == "logto_webhook_failed"


# --- La fermeture chez Logto (Management API simulée) --------------------------

class _Rep:
    def __init__(self, statut: int, corps: dict | None = None):
        self.status_code, self._corps = statut, corps or {}

    def json(self):
        return self._corps


@pytest.fixture
def logto(monkeypatch):
    import requests
    appels: list = []
    reponses: dict = {
        ("GET", f"/api/users/{SUB}/sessions"): _Rep(200, {"sessions": [
            {"payload": {"uid": "s1"}}, {"payload": {"uid": "s2"}}]}),
        ("DELETE", f"/api/users/{SUB}/sessions/s1"): _Rep(204),
        ("DELETE", f"/api/users/{SUB}/sessions/s2"): _Rep(404),   # échue entre-temps
        ("GET", f"/api/users/{SUB}/grants"): _Rep(200, {"grants": [{"id": "g1"}]}),
        ("DELETE", f"/api/users/{SUB}/grants/g1"): _Rep(204),
    }

    def _request(methode, url, headers=None, timeout=None, params=None):
        chemin = url.removeprefix("https://annuaire.test")
        appels.append((methode, chemin, params, headers.get("Authorization"), timeout))
        return reponses[(methode, chemin)]

    monkeypatch.setenv("LOGTO_ENDPOINT", "https://annuaire.test")
    monkeypatch.setattr(annuaire, "_mgmt_token", lambda directory=None: "JETON-M2M")
    monkeypatch.setattr(tenancy, "require_primary_tenant", lambda sub, action: None)
    monkeypatch.setattr(requests, "request", _request)
    return appels, reponses


def test_fermeture_logto_sessions_avec_grants_puis_grants_restants(logto):
    appels, _ = logto
    assert annuaire.fermer_sessions(SUB) == {"sessions": 1, "grants": 1}
    assert [(a[0], a[1], a[2]) for a in appels] == [
        ("GET", f"/api/users/{SUB}/sessions", None),
        ("DELETE", f"/api/users/{SUB}/sessions/s1", {"revokeGrantsTarget": "all"}),
        ("DELETE", f"/api/users/{SUB}/sessions/s2", {"revokeGrantsTarget": "all"}),
        ("GET", f"/api/users/{SUB}/grants", None),
        ("DELETE", f"/api/users/{SUB}/grants/g1", None),
    ]
    assert {a[3] for a in appels} == {"Bearer JETON-M2M"}
    assert all(a[4] and a[4] <= annuaire._DELAI_REQUETE_S for a in appels)


def test_fermeture_logto_un_refus_leve_nomme(logto):
    _, reponses = logto
    reponses[("DELETE", f"/api/users/{SUB}/sessions/s1")] = _Rep(500)
    with pytest.raises(annuaire.FermetureInachevee):
        annuaire.fermer_sessions(SUB)


def test_fermeture_logto_sans_credential_leve_nomme(logto, monkeypatch):
    def _absent(directory=None):
        raise RuntimeError("credential de management absent")
    monkeypatch.setattr(annuaire, "_mgmt_token", _absent)
    with pytest.raises(annuaire.FermetureInachevee):
        annuaire.fermer_sessions(SUB)


def test_fermeture_logto_borne_son_propre_delai(logto, monkeypatch):
    """Une borne à SON niveau : passé `_DELAI_FERMETURE_S`, la suite n'est pas tentée
    et l'échec se dit."""
    horloge = iter([0.0, 0.0, annuaire._DELAI_FERMETURE_S + 1] + [999.0] * 10)
    monkeypatch.setattr(annuaire.time, "monotonic", lambda: next(horloge))
    appels, _ = logto
    with pytest.raises(annuaire.FermetureInachevee, match="délai"):
        annuaire.fermer_sessions(SUB)
    assert len(appels) == 1
