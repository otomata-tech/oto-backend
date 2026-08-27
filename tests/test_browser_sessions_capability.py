"""La session navigateur, en capacité : mêmes chemins, MÊME ORDRE DE REFUS.

Les deux routes `/api/me/connectors/{name}/session/{start,finalize}` ont quitté
`api_routes_credentials.py` (module supprimé) pour `capabilities/browser_sessions.py`
(27/08). C'est la voie par laquelle on pose un credential par login HUMAIN.

Ce que ce fichier garde, et qu'une migration naïve casse :

- **L'ordre des refus de `finalize`.** `no_org_context` (400) → `not_org_shareable`
  (400) → `forbidden` (403). Une règle d'autz déclarée qui trancherait EN AMONT rendrait
  403 là où la route rend 400 depuis toujours — d'où `SUB_ONLY` + escalade au handler,
  comme `me_credentials._clear`. Les six branches sont exercées une par une.
- **`connected: false` est une 200.** « Pas encore logué » n'est pas un échec : la
  session vit, rien n'a été écrit, l'appelant réessaie. En faire un 4xx ferait afficher
  une erreur là où l'utilisateur est simplement en train de taper son mot de passe.
- **`context_id`/`session_id` rendent `missing_params`**, pas le `invalid_input` de
  pydantic : ils sont donc déclarés facultatifs À DESSEIN (cf. le module).
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp import browser_session
from oto_mcp.capabilities import browser_sessions as bs


@pytest.fixture()
def socle(monkeypatch):
    """Le substrat navigateur et les paliers de droits, en mémoire."""
    vus: list = []
    monkeypatch.setattr(browser_session, "is_session_connector", lambda n: True)

    def _start(sub, name, login_url=None):
        vus.append(("start", sub, name, login_url))
        return {"live_view_url": "https://live/x", "context_id": "ctx-1",
                "session_id": "ses-1"}

    async def _finalize(sub, connector, context_id, session_id, *, scope="member",
                        group_id=None, account="", force=False):
        vus.append(("finalize", sub, connector, context_id, session_id, scope,
                    group_id, account, force))
        return True

    monkeypatch.setattr(browser_session, "start", _start)
    monkeypatch.setattr(browser_session, "finalize", _finalize)
    monkeypatch.setattr(bs.access, "current_org", lambda sub: 35)
    monkeypatch.setattr(bs.access, "current_group", lambda sub: 7)
    monkeypatch.setattr(bs.connectors, "is_org_shareable", lambda n: True)
    monkeypatch.setattr(bs.roles, "is_org_admin", lambda sub, oid: True)
    monkeypatch.setattr(bs.roles, "can_admin_group", lambda sub, gid: True)
    return vus


def _start(query=b"", nom="brevo"):
    return call("me.browser_session.start", path_params={"name": nom}, query=query)


def _finalize(corps, nom="brevo"):
    return call("me.browser_session.finalize", path_params={"name": nom}, body=corps)


_OK = {"context_id": "ctx-1", "session_id": "ses-1"}


# --- Ouvrir la fenêtre ------------------------------------------------------

def test_start_rend_la_live_view_et_le_couple_a_rejouer(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = _start()
    assert code == 200, out
    assert out == {"live_view_url": "https://live/x", "context_id": "ctx-1",
                   "session_id": "ses-1"}
    assert set(out) == set(bs.SessionOpened.model_fields)


def test_le_site_vient_de_l_appel_pour_le_connecteur_generique(monkeypatch, socle):
    """`?url=` n'existe que pour le connecteur générique, dont le site n'est pas
    enregistré. Vide ⇒ None ⇒ la `login_url` du connecteur, comportement inchangé."""
    stub_authz(monkeypatch)
    _start(query=b"url=https%3A%2F%2Fsite%2Flogin", nom="browser")
    assert socle[0][3] == "https://site/login"
    socle.clear()
    _start(query=b"url=")
    assert socle[0][3] is None


def test_un_connecteur_qui_ne_se_connecte_pas_ainsi_est_un_404(monkeypatch, socle):
    stub_authz(monkeypatch)
    monkeypatch.setattr(browser_session, "is_session_connector", lambda n: False)
    assert _start()[0] == 404
    assert _finalize(_OK)[0] == 404


def test_un_substrat_muet_est_un_503_qui_dit_quoi_faire(monkeypatch, socle):
    """Le détail est RENDU : « réessaie » est actionnable, `browserbase_unavailable`
    tout seul ne l'est pas."""
    stub_authz(monkeypatch)

    def _boum(sub, name, login_url=None):
        raise browser_session.SessionError("connexion au navigateur distant impossible.")

    monkeypatch.setattr(browser_session, "start", _boum)
    code, out = _start()
    assert code == 503 and out["error"] == "browserbase_unavailable"
    assert out["detail"] == "connexion au navigateur distant impossible."


# --- Finaliser --------------------------------------------------------------

def test_pas_encore_logue_est_une_200_pas_une_erreur(monkeypatch, socle):
    """La session vit toujours, rien n'a été écrit au coffre, l'appelant réessaie. En
    faire un 4xx afficherait une erreur à quelqu'un qui tape simplement son mot de
    passe."""
    stub_authz(monkeypatch)

    async def _pas_encore(*a, **kw):
        return False

    monkeypatch.setattr(browser_session, "finalize", _pas_encore)
    code, out = _finalize(_OK)
    assert code == 200
    assert out == {"connected": False, "scope": "member", "account": ""}


@pytest.mark.parametrize("corps", [{}, {"session_id": "s"}, {"context_id": "c"}])
def test_le_couple_manquant_rend_missing_params(monkeypatch, socle, corps):
    """PAS le `invalid_input` de pydantic : ces deux champs sont déclarés facultatifs
    à dessein, pour que le code servi ne change pas."""
    stub_authz(monkeypatch)
    code, out = _finalize(corps)
    assert code == 400 and out["error"] == "missing_params"


def test_un_scope_inconnu_est_refuse(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = _finalize(dict(_OK, scope="planete"))
    assert code == 400 and out["error"] == "invalid_scope"


@pytest.mark.parametrize("scope,patch,attendu,code", [
    ("org",   ("current_org", None),        "no_org_context",    400),
    ("org",   ("is_org_shareable", False),  "not_org_shareable", 400),
    ("org",   ("is_org_admin", False),      "forbidden",         403),
    ("group", ("current_group", None),      "no_group_context",  400),
    ("group", ("can_admin_group", False),   "forbidden",         403),
])
def test_l_ordre_des_refus_du_scope_partage(monkeypatch, socle, scope, patch,
                                            attendu, code):
    """⚠️ L'ORDRE est observable et il est le contrat : contexte manquant (400) AVANT
    partageabilité (400) AVANT droit d'admin (403). C'est pourquoi l'escalade vit dans
    le handler et non dans une règle d'autz déclarée, qui trancherait trop tôt."""
    stub_authz(monkeypatch)
    champ, valeur = patch
    cible = bs.access if champ.startswith("current") else (
        bs.connectors if champ == "is_org_shareable" else bs.roles)
    monkeypatch.setattr(cible, champ, (lambda *a, **k: valeur))
    got, out = _finalize(dict(_OK, scope=scope))
    assert (got, out["error"]) == (code, attendu)


@pytest.mark.parametrize("scope", ["member", "org", "group"])
def test_les_trois_paliers_persistent_et_rendent_leur_scope(monkeypatch, socle, scope):
    stub_authz(monkeypatch)
    code, out = _finalize(dict(_OK, scope=scope))
    assert code == 200 and out["scope"] == scope
    assert socle[0][5] == scope
    # `group_id` n'est transmis QUE pour le palier équipe — le coffre en dépend.
    assert socle[0][6] == (7 if scope == "group" else None)


def test_account_et_force_arrivent_au_seam(monkeypatch, socle):
    """Le connecteur générique pose UNE ligne de coffre PAR SITE (`account` = le host),
    et `force` contourne un verify qui répondrait « pas logué » à tort."""
    stub_authz(monkeypatch)
    code, out = _finalize(dict(_OK, account=" app.site.com ", force=True), nom="browser")
    assert code == 200 and out["account"] == "app.site.com"
    assert socle[0][7] == "app.site.com" and socle[0][8] is True


def test_un_verify_impossible_est_un_502_qui_porte_son_message(monkeypatch, socle):
    stub_authz(monkeypatch)

    async def _boum(*a, **kw):
        raise browser_session.SessionError("vérification impossible — réessaie.")

    monkeypatch.setattr(browser_session, "finalize", _boum)
    code, out = _finalize(_OK)
    assert code == 502 and out["error"] == "session_verify_failed"
    assert out["detail"] == "vérification impossible — réessaie."


# --- Ce qui CHANGE pour un appelant -----------------------------------------

def test_un_corps_malforme_rend_missing_params_et_non_invalid_json(monkeypatch, socle):
    """**Écart visible, assumé.** L'adaptateur avale un corps JSON illisible (il le
    traite comme absent) ; la route rendait `400 invalid_json`, elle rend désormais
    `400 missing_params` — même statut, code différent. Aucun consommateur n'envoie de
    JSON malformé, et le refus reste actionnable."""
    stub_authz(monkeypatch)
    code, out = call("me.browser_session.finalize", path_params={"name": "brevo"},
                     body="{pas du json")
    assert code == 400 and out["error"] == "missing_params"


def test_un_corps_non_objet_ne_fait_plus_de_500(monkeypatch, socle):
    """**Écart visible, et c'est une correction.** `(body or {}).get(...)` sur une LISTE
    levait `AttributeError` → 500. L'adaptateur ne fusionne qu'un objet, donc le corps
    est vu comme absent → `400 missing_params`. Un corps malformé ne doit pas rendre
    une erreur serveur."""
    stub_authz(monkeypatch)
    code, out = _finalize([1, 2])
    assert code == 400 and out["error"] == "missing_params"


def test_un_champ_inconnu_est_desormais_refuse(monkeypatch, socle):
    stub_authz(monkeypatch)
    code, out = _finalize(dict(_OK, contextId="ctx-1"))
    assert code == 400
    assert out["error"] == "unknown_fields" and "contextId" in out["detail"]
