"""Le credential personnel, en capacité : mêmes chemins, mêmes refus, MÊME fil.

Les trois routes `/api/settings/api-keys/{provider}` ont quitté `api/routes.py` pour
`capabilities/me_credentials.py` (27/08). C'est la surface par laquelle tout le monde
branche ses clés : une migration qui « promet d'être invisible » ne se prouve qu'en
lisant ce qui part sur le fil, via la vraie chaîne de l'adaptateur REST.

Le point qui ne survit PAS à une migration naïve : le corps du POST est **plat** et
ses clés sont celles du connecteur (`{"key": "…"}`, `{"bot_token": …}`). Sans le cran
`body_field`, la garde « champ inconnu » les refuserait toutes — un 400 sur chaque
pose de clé. C'est le premier test ci-dessous, et il vaut pour tous les connecteurs.
"""
from __future__ import annotations

import pytest

from _datastore_rest import call, stub_authz

from oto_mcp.capabilities import me_credentials as mc
from oto_mcp import credentials_store


@pytest.fixture()
def vault(monkeypatch):
    """Coffre en mémoire : on lit ce qui aurait été écrit, sans DB ni chiffrement."""
    written: list[dict] = []

    def _set_credential(entity_type, entity_id, connector, secret, set_by=None,
                        account="", meta=None):
        written.append({"entity_type": entity_type, "entity_id": entity_id,
                        "connector": connector, "account": account, "meta": meta})

    monkeypatch.setattr(mc.access, "current_org", lambda sub: 35)
    monkeypatch.setattr(mc.db, "upsert_user", lambda sub: None)
    monkeypatch.setattr(mc.credentials_store, "set_credential", _set_credential)
    monkeypatch.setattr(mc.credentials_store, "guard_account_write",
                        lambda *a, **k: None)
    monkeypatch.setattr(mc.credentials_store, "get_credential_with_meta",
                        lambda *a, **k: None)
    from oto_mcp.connectors import verify as connector_verify
    monkeypatch.setattr(connector_verify, "supports", lambda p: False)
    return written


def _post(body, provider="serper"):
    return call("me.credential.set", path_params={"provider": provider}, body=body)


# --- Le contrat du fil ------------------------------------------------------

def test_le_corps_plat_du_connecteur_passe(monkeypatch, vault):
    """Les clés du corps sont celles du connecteur, pas des champs déclarés par un
    modèle : la garde de champ inconnu ne doit PAS les voir passer pour des erreurs."""
    stub_authz(monkeypatch)
    code, out = _post({"key": "K-42"})
    assert code == 200, out
    assert out == {"ok": True, "provider": "serper", "org_id": 35, "account": "",
                   "verified": False, "pending_action": None}
    assert vault[0]["account"] == ""
    assert vault[0]["entity_type"] == credentials_store.MEMBER


def test_un_compte_nomme_arrive_au_coffre(monkeypatch, vault):
    stub_authz(monkeypatch)
    code, out = _post({"key": "K-42", "account": "client-x"})
    assert code == 200, out
    assert out["account"] == "client-x"
    assert vault[0]["account"] == "client-x"


def test_le_nom_de_compte_ne_finit_pas_dans_le_secret(monkeypatch, vault):
    """`account` voyage AVEC les champs sur le fil : s'il était packé avec eux, il
    deviendrait un pseudo-champ du credential."""
    stub_authz(monkeypatch)
    packed: dict = {}
    monkeypatch.setattr(mc.credentials_store, "pack_secret",
                        lambda provider, fields: packed.update(fields) or "SECRET")
    _post({"key": "K-42", "account": "client-x"})
    assert packed == {"key": "K-42"}


# --- Les refus --------------------------------------------------------------

def test_champ_requis_vide_nomme_le_champ(monkeypatch, vault):
    stub_authz(monkeypatch)
    code, out = _post({"key": ""})
    assert code == 400 and out["error"] == "missing_credentials"
    assert "API key" in (out["detail"] or "")


def test_corps_vide_refuse(monkeypatch, vault):
    stub_authz(monkeypatch)
    code, out = _post({})
    assert code == 400 and out["error"] == "missing_credentials"


def test_connecteur_inconnu(monkeypatch, vault):
    stub_authz(monkeypatch)
    code, out = _post({"key": "K"}, provider="pas-au-registre")
    assert code == 404 and out["error"] == "unknown_provider"


def test_compte_nomme_sur_un_connecteur_mono_est_refuse(monkeypatch, vault):
    """La garde de pose du coffre (#409) remonte telle quelle en 400 nommé — unipile
    est mono-compte par construction (identité cross-org)."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(
        mc.credentials_store, "guard_account_write",
        lambda *a, **k: (_ for _ in ()).throw(
            credentials_store.SingleAccountConnector("`unipile` ne gère qu'un compte")))
    code, out = _post({"key": "K", "account": "deux"}, provider="unipile")
    assert code == 400 and out["error"] == "single_account_connector"
    assert "unipile" in (out["detail"] or "")


def test_pose_anonyme_la_ou_des_comptes_nommes_existent(monkeypatch, vault):
    stub_authz(monkeypatch)
    monkeypatch.setattr(
        mc.credentials_store, "guard_account_write",
        lambda *a, **k: (_ for _ in ()).throw(
            credentials_store.NamedAccountRequired("précise `account`")))
    code, out = _post({"key": "K"})
    assert code == 409 and out["error"] == "account_required"


def test_sans_org_de_contexte(monkeypatch, vault):
    stub_authz(monkeypatch)
    monkeypatch.setattr(mc.access, "current_org", lambda sub: None)
    code, out = _post({"key": "K"})
    assert code == 400 and out["error"] == "no_org_context"


# --- Lecture et retrait -----------------------------------------------------

def test_lecture_sans_credential(monkeypatch, vault):
    stub_authz(monkeypatch)   # la fixture `vault` rend déjà un coffre vide
    code, out = call("me.credential.get", path_params={"provider": "serper"})
    assert code == 404 and out["error"] == "not_configured"


def test_retrait_d_un_compte_nomme(monkeypatch, vault):
    stub_authz(monkeypatch)
    cleared: list = []
    monkeypatch.setattr(mc.credentials_store, "clear_credential",
                        lambda et, eid, prov, account="": cleared.append((et, prov, account)))
    code, out = call("me.credential.clear", path_params={"provider": "serper"},
                     query=b"account=client-x")
    assert code == 200, out
    # `warning` s'ajoute depuis oto#59, TOUJOURS présent et à `None` quand aucun agent
    # programmé ne dépend de la clé retirée : la clé constante distingue « rien à
    # signaler » d'un serveur trop vieux pour le savoir. Le retrait, lui, ne change pas.
    assert out == {"ok": True, "provider": "serper", "account": "client-x",
                   "scope": "member", "warning": None}
    assert cleared[0][2] == "client-x"


def test_retrait_au_palier_org_exige_l_admin(monkeypatch, vault):
    stub_authz(monkeypatch)
    monkeypatch.setattr("oto_mcp.detenteurs.est_membre", lambda *a: False)  # oto#108 : sans annuaire
    monkeypatch.setattr(mc.roles, "is_org_admin", lambda sub, org: False)
    code, out = call("me.credential.clear", path_params={"provider": "serper"},
                     query=b"scope=org")
    assert code == 403 and out["error"] == "forbidden"


# --- démarquage (oto#25 lot b3) — « nouvelle clé posée » ---------------------

def test_reposer_une_cle_ne_reporte_jamais_une_sante_precedente(monkeypatch, vault):
    """`credentials_store.set_credential` REMPLACE tout le `meta` (jamais un merge,
    cf. son docstring — un incident vécu le 03/08 dépend déjà de cette garantie) :
    reposer une clé ici envoie TOUJOURS `meta=None` (non vérifié) ou
    `{"verified_at": …}` (vérifié), jamais un `meta` qui reporterait un `health_ko`
    d'avant. C'est ce qui fait qu'une reconnexion démarque une ligne rejetée, sans
    geste dédié — figé ici pour qu'une régression future (un `meta` qui se mettrait
    à lire l'ancien avant d'écrire) rougisse."""
    stub_authz(monkeypatch)
    code, out = _post({"key": "K-NOUVELLE"}, provider="serper")
    assert code == 200, out
    meta = vault[0]["meta"]
    assert meta is None or "health_ko" not in meta, (
        f"un `meta` qui reporterait `health_ko` empêcherait toute reconnexion de "
        f"démarquer une ligne rejetée : {meta!r}")


# --- Le refus dit CE QUI CLOCHE, et OÙ aller (oto#…, 08/09/2026) -------------
#
# `_credentialable` rendait `None` pour QUATRE raisons distinctes et ses trois
# appelants traduisaient les quatre en un seul « 404 Connecteur inconnu ». Le
# 08/09/2026, deux sessions ont lu ce message sur `api` — org-partageable, jamais
# `byo_user`, donc refusé au palier membre qui est le DÉFAUT — et en ont déduit que
# la production tournait une version antérieure au connecteur. L'une est partie
# chercher un arbitrage de déploiement qui n'existait pas.
#
# `api` a depuis gagné le palier membre (`byo_user`, 13/11/2026) : ces tests prennent
# un connecteur qui reste org seul, `ORG_SEUL`, et le vérifient.
#
# Un refus qui nomme la mauvaise cause envoie chercher au mauvais endroit : c'est
# plus cher qu'un refus tout court. Ces tests exigent donc DEUX choses de chaque
# refus — le bon code, et la DESTINATION (le palier, la route ou le porteur qui,
# lui, accepterait). Et la destination nommée est ENCHAÎNÉE : on rejoue par où le
# message envoie, et on prouve que ça passe.

ORG_SEUL = "resend"


def test_le_connecteur_d_exemple_reste_org_seul():
    from oto_mcp import providers
    assert providers.REGISTRY[ORG_SEUL].auth_modes == {"byo_org"}


def test_le_palier_membre_sur_un_connecteur_org_ne_dit_pas_inconnu(monkeypatch, vault):
    """`ORG_SEUL` est au registre, avec sa clé : il n'est pas inconnu. Ce qui cloche
    est le PALIER — il n'est pas `byo_user`."""
    stub_authz(monkeypatch)
    code, out = call("me.credential.get", path_params={"provider": ORG_SEUL})
    assert out["error"] != "unknown_provider", (
        f"`{ORG_SEUL}` EST au registre : dire « connecteur inconnu » envoie chercher un "
        "déploiement manquant. C'est le voyage qu'ont fait deux sessions le 08/09.")
    assert code == 400 and out["error"] == "wrong_credential_scope"


def test_ce_refus_de_palier_nomme_le_palier_qui_accepte(monkeypatch, vault):
    """Nommer la cause ne suffit pas : le refus doit dire OÙ ça se pose."""
    stub_authz(monkeypatch)
    _, out = call("me.credential.get", path_params={"provider": ORG_SEUL})
    detail = out["detail"] or ""
    assert ORG_SEUL in detail and "org" in detail, detail
    assert "scope=org" in detail, (
        f"le refus doit porter le geste à rejouer, pas seulement son diagnostic : {detail!r}")


def test_le_palier_nomme_par_le_refus_passe_vraiment(monkeypatch, vault):
    """La destination est ENCHAÎNÉE, pas seulement citée : on rejoue au palier que
    le message désigne et on vérifie qu'il franchit la garde d'éligibilité (il tombe
    alors sur le coffre vide, `not_configured` — pas sur un refus de connecteur)."""
    stub_authz(monkeypatch)
    monkeypatch.setattr(mc.roles, "is_org_admin", lambda sub, org: True)
    code, out = call("me.credential.get", path_params={"provider": ORG_SEUL},
                     query=b"scope=org")
    assert (code, out["error"]) == (404, "not_configured"), (
        f"le message envoie vers `scope=org` : ce palier doit passer la garde. {out!r}")


def test_le_retrait_nomme_le_palier_lui_aussi(monkeypatch, vault):
    """`_clear` recopiait l'éligibilité au lieu de la partager : le même mensonge y
    vivait en double."""
    stub_authz(monkeypatch)
    code, out = call("me.credential.clear", path_params={"provider": ORG_SEUL})
    assert code == 400 and out["error"] == "wrong_credential_scope"
    assert "scope=org" in (out["detail"] or "")


def test_la_pose_envoie_vers_une_route_qui_existe(monkeypatch, vault):
    """La pose n'a PAS de `scope` (ADR 0033 : elle écrit toujours au palier membre) —
    lui dire « rejoue avec scope=org » serait un remède inatteignable depuis sa face.
    Elle doit nommer la surface qui, elle, pose une clé d'org."""
    stub_authz(monkeypatch)
    code, out = _post({"base_url": "https://x"}, provider=ORG_SEUL)
    assert code == 400 and out["error"] == "wrong_credential_scope"
    detail = out["detail"] or ""
    routes = {b.path for c in mc.CAPABILITIES for b in c.rest_bindings()}
    citees = [r for r in routes if r in detail]
    assert citees, (
        f"le refus doit citer une route SERVIE, pas un chemin plausible : {detail!r}")


def test_un_connecteur_delegue_nomme_son_porteur(monkeypatch, vault):
    """`whatsapp` n'a pas de clé à lui : elle se pose sur `unipile`. Le registre le
    sait (`credential_of`) — le refus le disait « inconnu »."""
    stub_authz(monkeypatch)
    code, out = _post({"key": "K"}, provider="whatsapp")
    assert code == 400 and out["error"] == "credential_delegated"
    assert "unipile" in (out["detail"] or "")


def test_un_connecteur_sans_formulaire_dit_son_flux(monkeypatch, vault):
    """`crunchbase` est `byo_user` et bien au registre, mais sans aucun champ de
    saisie : il se connecte par session navigateur. « Inconnu » est faux."""
    stub_authz(monkeypatch)
    code, out = call("me.credential.get", path_params={"provider": "crunchbase"})
    assert code == 400 and out["error"] == "no_credential_form"
    assert "crunchbase" in (out["detail"] or "")


def test_AUCUN_connecteur_du_registre_ne_se_fait_dire_inconnu(monkeypatch, vault):
    """Le garde-fou du MOTIF, pas du cas : on ÉNUMÈRE le registre entier plutôt que
    de rejouer les trois noms qui ont mordu. `unknown_provider` doit rester réservé à
    un nom absent du registre — sinon la surface renvoie chercher un déploiement."""
    stub_authz(monkeypatch)
    from oto_mcp import providers
    coupables = []
    for nom in sorted(providers.REGISTRY):
        _, out = call("me.credential.get", path_params={"provider": nom})
        if out.get("error") == "unknown_provider":
            coupables.append(nom)
    assert coupables == [], (
        f"{len(coupables)} connecteurs du registre se font dire « inconnu » : {coupables}")


def test_un_nom_absent_du_registre_reste_un_404_inconnu(monkeypatch, vault):
    """Le pendant : à force de nommer les causes, `unknown_provider` doit rester
    LEVÉ pour celle qui le mérite."""
    stub_authz(monkeypatch)
    code, out = call("me.credential.get", path_params={"provider": "pas-au-registre"})
    assert code == 404 and out["error"] == "unknown_provider"
