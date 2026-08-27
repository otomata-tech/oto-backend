"""La messagerie hébergée côté membre, en capacité : mêmes chemins, MÊMES REPLIS.

Les quatre routes `/api/me/unipile*` ont quitté `api_routes_connectors.py` pour
`capabilities/unipile_me.py` (27/08). Le webhook, lui, RESTE écrit à la main : Unipile
l'appelle sans en-tête d'auth, et l'adaptateur REST authentifie toujours.

Quatre comportements qu'une migration naïve « nettoie » par erreur :

1. **Le refus 409/502 sert sa PROSE dans `error`**, pas son code machine. C'est laid et
   c'est ce qui est servi : le front affiche `error` tel quel, et pour ces deux-là le
   message est actionnable (« ce compte est déjà connecté ailleurs »). Les autres codes
   exposent bien leur jeton.
2. **Le `connect` a DEUX formes de succès** : `{url}` d'ordinaire, `{adopted, channel,
   account_name}` **sans url** quand le compte a été rattaché sans wizard.
3. **Le `GET` réconcilie avant de répondre**, et l'échec de cette réconciliation ne doit
   JAMAIS faire échouer le statut (le webhook v2 n'est pas livré, ce self-heal est ce
   qui fait apparaître un compte fraîchement connecté).
4. **Le `DELETE` est par-ORG et par-CANAL**, canal mis en majuscules, `linkedin` par
   défaut — y compris sur `?channel=` vide.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp import unipile_connect
from oto_mcp.capabilities import unipile_me as um
from oto_mcp.tools import unipile as tunipile

_STATUT = {
    "subscribed": True, "mode": "platform", "byo": False,
    "channels": {"linkedin": {"connected": True, "account_id": "a1",
                              "account_name": "Zoé", "connected_at": "2026-08-01"}},
    "elsewhere": {"whatsapp": {"account_id": "a2", "account_name": "Zoé", "org_id": 12}},
}


@pytest.fixture()
def socle(monkeypatch):
    vus: list = []

    async def _hosted(sub, channel, force=False, premium=None, app=None):
        vus.append(("hosted", sub, channel, force, premium, app))
        return {"url": "https://unipile/auth/xyz", "channel": channel}

    def _reconcile(sub):
        vus.append(("reconcile", sub))
        return {"bound": True, "accounts": [{"account_id": "a1"}]}

    monkeypatch.setattr(unipile_connect, "hosted_auth_url", _hosted)
    monkeypatch.setattr(unipile_connect, "reconcile_pending", _reconcile)
    monkeypatch.setattr(tunipile, "status_for", lambda sub: dict(_STATUT))
    monkeypatch.setattr(um.access, "current_org", lambda sub: 35)
    monkeypatch.setattr(um.db, "clear_unipile_account",
                        lambda sub, org, prov: vus.append(("clear", sub, org, prov)))
    return vus


def _refus(monkeypatch, status, code, message):
    async def _boum(*a, **kw):
        raise unipile_connect.ConnectRefused(status, code, message)
    monkeypatch.setattr(unipile_connect, "hosted_auth_url", _boum)


# --- Connecter --------------------------------------------------------------

def test_connect_rend_l_url_de_consentement(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.unipile.connect")
    assert (code, out) == (200, {"url": "https://unipile/auth/xyz"})


def test_les_options_du_wizard_arrivent_au_seam(monkeypatch, socle):
    """`premium` est normalisé (trim + minuscules) : sans lui, le produit LinkedIn n'est
    pas activé à la connexion et ses APIs répondent 403. `app` fait revenir l'utilisateur
    chez SON front, pas chez oto-dashboard."""
    stub_authz(monkeypatch)
    call("me.unipile.connect", body={"channel": "whatsapp", "premium": " Recruiter ",
                                     "force": True, "app": "tulina"})
    assert socle[0] == ("hosted", "u-1", "whatsapp", True, "recruiter", "tulina")


def test_sans_corps_le_canal_par_defaut_est_linkedin(monkeypatch, socle):
    stub_authz(monkeypatch)
    call("me.unipile.connect")
    assert socle[0] == ("hosted", "u-1", "linkedin", False, None, None)


def test_l_adoption_rend_un_corps_SANS_url(monkeypatch, socle):
    """Deuxième forme de succès : le compte était déjà connecté sous cette identité dans
    une autre org, il vient d'être rattaché ici. Il n'y a AUCUN consentement à donner —
    un front qui attend `url` ouvrirait une fenêtre vide."""
    stub_authz(monkeypatch)

    async def _adopte(*a, **kw):
        return {"adopted": True, "channel": "linkedin", "account_name": "Zoé"}

    monkeypatch.setattr(unipile_connect, "hosted_auth_url", _adopte)
    code, out = call("me.unipile.connect")
    assert code == 200
    assert out == {"adopted": True, "channel": "linkedin", "account_name": "Zoé"}
    assert "url" not in out


@pytest.mark.parametrize("status", [409, 502])
def test_les_refus_409_et_502_servent_leur_MESSAGE_dans_error(monkeypatch, socle, status):
    """⚠️ Forme historique, volontairement conservée : pour ces deux-là, `error` porte de
    la PROSE et non le jeton machine. Le front l'affiche tel quel, et le message est ce
    qui est actionnable. « Normaliser » enverrait `already_linked` à l'écran."""
    stub_authz(monkeypatch)
    _refus(monkeypatch, status, "already_linked", "Ce compte est déjà connecté ailleurs.")
    code, out = call("me.unipile.connect")
    assert code == status
    # `detail` est None : la vraie fabrique `_json_error` l'OMET alors, le corps servi
    # est donc `{"error": "<prose>"}` seul (le helper de test, lui, garde la clé).
    assert out["error"] == "Ce compte est déjà connecté ailleurs."
    assert out["detail"] is None


@pytest.mark.parametrize("status", [400, 403])
def test_les_autres_refus_servent_leur_CODE(monkeypatch, socle, status):
    stub_authz(monkeypatch)
    _refus(monkeypatch, status, "option_required", "Souscris l'option messagerie.")
    code, out = call("me.unipile.connect")
    assert code == status
    assert out["error"] == "option_required" and out["detail"] is None


# --- Réconcilier ------------------------------------------------------------

def test_reconcile_lie_ce_qui_vient_d_etre_connecte(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.unipile.reconcile")
    assert (code, out) == (200, {"bound": True, "accounts": [{"account_id": "a1"}]})


def test_rien_a_lier_n_est_pas_une_panne(monkeypatch, socle):
    stub_authz(monkeypatch)
    monkeypatch.setattr(unipile_connect, "reconcile_pending",
                        lambda sub: {"bound": False, "accounts": []})
    code, out = call("me.unipile.reconcile")
    assert (code, out) == (200, {"bound": False, "accounts": []})


# --- Lire le statut ---------------------------------------------------------

def test_le_statut_reconcilie_avant_de_repondre(monkeypatch, socle):
    """Self-heal : le webhook hosted-auth v2 n'est pas livré, donc sans cette passe un
    compte fraîchement connecté n'apparaîtrait jamais. No-op sans pending."""
    stub_authz(monkeypatch)
    code, out = call("me.unipile.status")
    assert code == 200 and out == _STATUT
    assert ("reconcile", "u-1") in socle


def test_un_self_heal_en_echec_ne_casse_PAS_le_statut(monkeypatch, socle):
    """Best-effort, jamais fatal : le statut est lisible même quand l'amont est muet.
    L'inverse rendrait la page connecteurs inaccessible sur une panne Unipile."""
    stub_authz(monkeypatch)

    def _boum(sub):
        raise RuntimeError("amont muet")

    monkeypatch.setattr(unipile_connect, "reconcile_pending", _boum)
    code, out = call("me.unipile.status")
    assert code == 200 and out == _STATUT


def test_le_statut_declare_tout_ce_qu_il_sert(monkeypatch, socle):
    stub_authz(monkeypatch)
    _, out = call("me.unipile.status")
    assert set(out) == set(um.UnipileStatusView.model_fields)


# --- Délier -----------------------------------------------------------------

@pytest.mark.parametrize("query,attendu", [
    (b"", "LINKEDIN"),
    (b"channel=whatsapp", "WHATSAPP"),
    (b"channel=", "LINKEDIN"),          # vide ⇒ défaut, pas une chaîne vide
])
def test_le_canal_delie_est_mis_en_majuscules(monkeypatch, socle, query, attendu):
    stub_authz(monkeypatch)
    code, out = call("me.unipile.disconnect", query=query)
    assert (code, out) == (200, {"ok": True})
    assert socle[0] == ("clear", "u-1", 35, attendu)


def test_la_deconnexion_est_bornee_a_l_org_courante(monkeypatch, socle):
    """Anti-résurgence cross-org (ex-#221) : l'affichage ne montre que les bindings de
    l'org courante, donc ce qu'on voit doit être exactement ce qu'on délie."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(um.access, "current_org", lambda sub: 77)
    call("me.unipile.disconnect")
    assert socle[0][2] == 77


# --- Ce qui CHANGE pour un appelant -----------------------------------------

def test_un_champ_inconnu_est_desormais_refuse(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = call("me.unipile.connect", body={"chanel": "linkedin"})
    assert code == 400
    assert out["error"] == "unknown_fields" and "chanel" in out["detail"]
