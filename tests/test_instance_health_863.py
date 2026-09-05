"""Sonder la session d'un TIERS — et ne jamais confondre « je ne sais pas » avec
« déconnecté » (oto-backend#863).

Le manque, daté : une utilisatrice a relancé SIX FOIS la connexion d'un connecteur dont
la session avait toujours été valide (2026-09-03). Personne ne pouvait répondre « elle
est vivante » sans se connecter à la machine de production et écrire un script jetable
pendant qu'elle attendait.

Ces bancs tiennent les quatre bornes de conception, et deux d'entre elles sont des
distinctions que l'absence a déjà fait payer.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import browser_session
from oto_mcp.capabilities import instance_health as IH
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

CTX = ResolvedCtx(sub="u-support", org_id=2, role="super_admin")


def _appel(inp):
    out = IH._instance_health(CTX, inp)
    return asyncio.run(out) if asyncio.iscoroutine(out) else out


@pytest.fixture()
def instance(monkeypatch):
    """Une instance vivante d'un TIERS, et son contexte au coffre."""
    fiche = {"id": 7, "connector": "crunchbase", "owner_type": "user",
             "owner_id": "u-cliente", "account": "", "revoked_at": None}
    monkeypatch.setattr(IH.db.connector_instances, "instance_by_id",
                        lambda i, conn=None: dict(fiche) if int(i) == 7 else None)
    monkeypatch.setattr(IH.credentials_store, "get_credential",
                        lambda ot, oid, c, account="": "ctx_abc")
    return fiche


def test_une_session_vivante_est_dite_vivante(instance, monkeypatch):
    async def _sonde(connector, context_id, account=""):
        assert (connector, context_id) == ("crunchbase", "ctx_abc"), (
            "la sonde doit viser le contexte de l'INSTANCE, pas celui de l'appelant — "
            "c'est tout l'objet du lot")
        return browser_session.Verdict(True, "logged_in")
    monkeypatch.setattr(IH.browser_session, "sonder", _sonde)
    out = _appel(IH.InstanceHealthInput(instance_id=7))
    assert out["connected"] is True and out["reason"] == "logged_in"
    assert out["owner_id"] == "u-cliente", "le verdict nomme DE QUI est l'instance"


def test_une_sonde_MUETTE_ne_dit_pas_deconnecte(instance, monkeypatch):
    """⚠️ LA distinction qui a coûté une matinée. `ProbeUnavailable` veut dire « je ne
    sais pas » : l'endpoint sondé a disparu, la réponse a une forme inattendue. Rendre
    `connected: false` ferait dire au support « refais ton login » — un geste qui ne
    répare rien, sur un diagnostic faux, à quelqu'un qui est déjà bloqué.

    `connected: null` et `retry: false` : on ne sait pas, et surtout ne recommence
    pas."""
    async def _sonde(connector, context_id, account=""):
        raise browser_session.ProbeUnavailable("endpoint de sonde absent (404)")
    monkeypatch.setattr(IH.browser_session, "sonder", _sonde)
    out = _appel(IH.InstanceHealthInput(instance_id=7))
    assert out["connected"] is None, "« je ne sais pas » n'est pas « pas connecté »"
    assert out["reason"] == "probe_unavailable" and out["retry"] is False


def test_une_session_rejetee_dit_de_REFAIRE_le_login(instance, monkeypatch):
    """L'autre moitié : quand le refus vient bien de l'amont, `retry` doit inviter à
    recommencer. Les deux cas se ressemblent dans une réponse pauvre et appellent des
    conduites opposées."""
    async def _sonde(connector, context_id, account=""):
        return browser_session.Verdict(False, "session_expired", "cookie rejeté", retry=True)
    monkeypatch.setattr(IH.browser_session, "sonder", _sonde)
    out = _appel(IH.InstanceHealthInput(instance_id=7))
    assert out["connected"] is False and out["retry"] is True


def test_une_instance_ARCHIVEE_se_dit_sans_etre_sondee(monkeypatch):
    """Son propriétaire a justement révoqué ce contexte : y ouvrir une session serait
    se servir d'un accès repris. Et « retirée » n'est pas « n'a jamais existé » — le
    support a besoin des deux."""
    monkeypatch.setattr(IH.db.connector_instances, "instance_by_id",
                        lambda i, conn=None: {"id": 7, "connector": "crunchbase",
                                              "owner_type": "user", "owner_id": "u-c",
                                              "account": "", "revoked_at": "2026-09-01",
                                              "revoked_reason": "rotation"})
    appels = []
    monkeypatch.setattr(IH.browser_session, "sonder",
                        lambda *a, **k: appels.append(a) or None)
    out = _appel(IH.InstanceHealthInput(instance_id=7))
    assert out["reason"] == "instance_revoked" and out["retry"] is False
    assert appels == [], "une instance retirée ne se sonde pas"


def test_un_credential_illisible_ne_dit_pas_deconnecte(monkeypatch):
    """Le coffre ne rend rien : soit rien n'a jamais été posé, soit la ligne ne se
    déchiffre plus (clé maîtresse périmée). Dire « déconnecté » ferait refaire un login
    qui ne réparerait pas une corruption — on rend `null` et on nomme la piste."""
    monkeypatch.setattr(IH.db.connector_instances, "instance_by_id",
                        lambda i, conn=None: {"id": 7, "connector": "crunchbase",
                                              "owner_type": "user", "owner_id": "u-c",
                                              "account": "", "revoked_at": None})
    monkeypatch.setattr(IH.credentials_store, "get_credential",
                        lambda *a, **k: None)
    out = _appel(IH.InstanceHealthInput(instance_id=7))
    assert out["connected"] is None and out["reason"] == "no_credential"
    assert "vault_health" in out["detail"], "le refus nomme la piste suivante"


def test_une_instance_inconnue_est_un_404(monkeypatch):
    monkeypatch.setattr(IH.db.connector_instances, "instance_by_id",
                        lambda i, conn=None: None)
    with pytest.raises(AuthzDenied) as e:
        _appel(IH.InstanceHealthInput(instance_id=99))
    assert e.value.status == 404 and e.value.code == "unknown_instance"


# ── Les bornes de conception, tenues par le code et non par la promesse ──────

def test_la_capacite_est_reservee_a_la_PLATEFORME():
    """Sonder l'accès d'un tiers n'est pas un geste d'org : c'est du support
    plateforme. Un `ORG_ADMIN` pourrait sinon sonder les instances de ses membres."""
    from oto_mcp.capabilities._authz import PLATFORM_ADMIN
    from oto_mcp.capabilities.registry import CAPABILITIES
    cap = next(c for c in CAPABILITIES if c.key == "admin.instance_health")
    assert cap.authz is PLATFORM_ADMIN


def test_la_sonde_n_execute_QUE_ce_que_le_connecteur_declare():
    """⚠️ La borne qui justifie tout le reste. `sonder` prend un CONNECTEUR et lit sa
    sonde dans le registre ; il n'accepte ni URL, ni requête, ni sélecteur venu de
    l'appelant. Un « agir en tant que » générique convertirait une porte qui exige un
    accès à la machine de production en une porte qu'un jeton d'admin ouvre à distance.

    Ce banc lit la SIGNATURE et le corps : si un paramètre de requête apparaissait, il
    tomberait — c'est le seul endroit où cet élargissement se verrait avant d'être
    servi."""
    import inspect
    p = inspect.signature(browser_session.sonder).parameters
    assert set(p) == {"connector", "context_id", "account"}, (
        "un paramètre de plus sur la sonde = un pas vers « exécute ce que je veux "
        "avec le credential de quelqu'un d'autre »")
    src = inspect.getsource(browser_session.sonder)
    assert "_REGISTRY.get(connector)" in src, "la sonde vient du connecteur, pas de l'appel"


def test_la_session_ouverte_est_TOUJOURS_refermee():
    """Une session laissée ouverte sur le contexte d'un tiers serait exactement l'accès
    persistant que ce lot refuse de créer. Le `finally` le garantit même quand la sonde
    lève — c'est le cas où l'on oublie."""
    import inspect
    src = inspect.getsource(browser_session.sonder)
    assert "finally:" in src and "release_session" in src
    assert src.index("finally:") < src.index("release_session")


def test_aucune_donnee_metier_ne_peut_remonter():
    """La sortie déclarée ne porte que le verdict et de quoi le lire. Un champ libre y
    ferait entrer ce que la sonde a vu — et la sonde regarde une page authentifiée."""
    champs = set(IH.InstanceHealth.model_fields)
    assert champs == {"instance_id", "connector", "owner_type", "owner_id", "account",
                      "connected", "reason", "detail", "retry"}
