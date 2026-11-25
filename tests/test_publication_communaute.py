"""Qui publie dans la communauté (bibliothèque publique), et sous quel nom.

Décision produit du 25/11/2026, qui remplace « publication réservée à la plateforme »
(14/09/2026) : tout **admin de l'org active** publie une procédure de cette org, signée
du NOM de l'org ; un **super_admin** plateforme publie toujours au nom d'Otomata. Un
simple membre ne publie pas. Ce qui reste ouvert ne change pas — ses procédures
personnelles pour tout compte, le fork d'une entrée pour tout membre d'org.

Ce que ces tests figent, sur la séquence SERVIE (validation → règle d'autz déclarée →
canal posé au seuil → handler, celle de `_rest_adapter` et de `_mcp_adapter`) :
- un membre non admin est refusé (403 `publication_reservee_aux_admins`), et rien
  n'est lu ni écrit ; un opérateur plateforme `admin` qui n'est pas admin de l'org
  active aussi ;
- l'org active est exigée AVANT le rôle (le rôle s'y lit), sur les deux faces ;
- un org_admin publie au nom de son org ; un opérateur `admin` qui est admin de l'org
  publie au nom de l'org, JAMAIS d'Otomata ; un super_admin publie au nom d'Otomata ;
- le contrôle automatique refuse avant toute écriture (400 `contenu_refuse`), en
  nommant le champ et la catégorie, jamais la valeur ;
- le droit ANNONCÉ (`capacite_autorise`, `platform_floor`) lit la même règle ;
- sur `oto_procedure`, la garde d'agent parle avant le handler pour qui peut publier,
  et un compte qui ne publiera pas reçoit le refus d'admin ;
- le fork est ouvert à tout membre de l'org active ; le palier suit le rôle, et
  l'installation est notée dans l'org active.

⚠️ Le rôle plateforme se pose sur `access.get_user_role`, la source commune des deux
prédicats (`is_super_admin`, `is_platform_operator`), jamais sur l'un d'eux.
"""
from __future__ import annotations

import asyncio
import dataclasses
import inspect
import types

import pytest

from oto_mcp import access, org_store, roles
from oto_mcp.capabilities import _authz
from oto_mcp.capabilities import guide_library as lib
from oto_mcp.capabilities._types import AuthzDenied, RawCtx, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES

ORG = 7
ROLES = {"compte-membre": "member", "compte-admin-org": "member",
         "compte-operateur": "admin", "compte-operateur-admin-org": "admin",
         "compte-super": "super_admin"}
ADMINS_D_ORG = {"compte-admin-org", "compte-operateur-admin-org"}
CODE = "publication_reservee_aux_admins"


@pytest.fixture
def banc(monkeypatch):
    """Règles, gates et handlers réels ; seuls les rôles, l'org active et le store
    sont posés. `roles.is_org_admin` suit `ADMINS_D_ORG` (un super_admin y escalade,
    comme dans le système)."""
    trace = types.SimpleNamespace(lu=[], ecrit=[], org_active=dict.fromkeys(ROLES, ORG))
    monkeypatch.setattr(access, "get_user_role", lambda sub: ROLES[sub])
    monkeypatch.setattr(access, "current_org", lambda sub: trace.org_active[sub])
    monkeypatch.setattr(roles, "is_org_admin",
                        lambda sub, org_id: sub in ADMINS_D_ORG or ROLES[sub] == "super_admin")

    def _get_instruction(otype, org_id, slug):
        trace.lu.append((otype, org_id, slug))
        return {"body_md": trace.__dict__.get("corps", "# corps"), "title": "T",
                "description": "d", "slots": []}

    def _publish_guide(**k):
        trace.ecrit.append(k)
        return {"id": 1, "slug": k["slug"], "version": 1, "visibility": k["visibility"]}

    monkeypatch.setattr(org_store, "get_instruction", _get_instruction)
    monkeypatch.setattr(org_store, "publish_guide", _publish_guide)
    monkeypatch.setattr(org_store, "get_org",
                        lambda org_id: {"id": org_id, "name": "Org de test"})
    monkeypatch.setattr(lib.db_communaute, "etat", lambda kind, i: {"state": "listed"})
    return trace


def _cap(cle):
    return next(c for c in CAPABILITIES if c.key == cle)


def _servir(cle, sub, canal, **champs):
    """Rejoue ce que font les deux adaptateurs : `Input` → règle d'autz DÉCLARÉE →
    canal posé au seuil → handler."""
    cap = _cap(cle)
    inp = cap.Input(**champs)
    ctx = dataclasses.replace(cap.authz(RawCtx(sub=sub), inp), channel=canal)
    out = cap.handler(ctx, inp)
    return asyncio.run(out) if inspect.isawaitable(out) else out


def _publier_rest(sub):
    return _servir("library.publish", sub, "rest", slug="veille-concurrence")


def _publier_mcp(sub):
    return _servir("org.procedure.console", sub, "mcp", op="publish",
                   slug="veille-concurrence", visibility="public")


@pytest.mark.parametrize("sub", ["compte-membre", "compte-operateur"])
def test_hors_admin_de_l_org_la_publication_est_refusee_sans_rien_toucher(banc, sub):
    with pytest.raises(AuthzDenied) as e:
        _publier_rest(sub)
    assert (e.value.status, e.value.code) == (403, CODE)
    assert banc.lu == [], "la procédure source ne doit même pas être lue"
    assert banc.ecrit == [], "rien ne doit être publié"


def test_le_refus_dit_qui_publie_et_ce_qui_reste_ouvert(banc):
    with pytest.raises(AuthzDenied) as e:
        _publier_rest("compte-membre")
    m = e.value.message
    assert "réservé aux admins de l'org" in m, "qui publie"
    assert "tes procédures personnelles" in m, "ce qui reste ouvert à tout compte"
    assert "forker une entrée" in m, "le fork reste promis"


@pytest.mark.parametrize("servir", [_publier_rest, _publier_mcp], ids=["rest", "mcp"])
@pytest.mark.parametrize("sub", ["compte-membre", "compte-admin-org", "compte-super"])
def test_l_org_active_est_exigee_avant_le_role(banc, servir, sub):
    """Le rôle se lit DANS l'org active : sans elle, le refus dit d'en choisir une —
    être admin d'une autre org est une porte ouverte."""
    banc.org_active[sub] = None
    with pytest.raises(AuthzDenied) as e:
        servir(sub)
    assert (e.value.status, e.value.code) == (400, "no_active_org")
    assert banc.lu == [] and banc.ecrit == []


@pytest.mark.parametrize("sub", ["compte-admin-org", "compte-operateur-admin-org"])
def test_un_admin_d_org_publie_au_nom_de_son_org(banc, sub):
    out = _publier_rest(sub)
    assert out["published"] is True and out["slug"] == "veille-concurrence"
    assert (out["author_kind"], out["author_display"]) == ("org", "Org de test")
    assert out["moderation_state"] == "listed"
    (ecrit,) = banc.ecrit
    assert (ecrit["author_kind"], ecrit["author_org_id"], ecrit["author_display"]) \
        == ("org", ORG, "Org de test"), "un opérateur `admin` ne signe pas Otomata"
    assert (ecrit["source_org_id"], ecrit["published_by"]) == (ORG, sub)


def test_un_super_admin_publie_au_nom_d_otomata(banc):
    out = _publier_rest("compte-super")
    assert out["published"] is True
    assert banc.lu == [("org", ORG, "veille-concurrence")], \
        "le corps se lit dans l'org active"
    (ecrit,) = banc.ecrit
    assert (ecrit["author_kind"], ecrit["author_org_id"], ecrit["author_display"]) \
        == ("otomata", None, "Otomata")


def test_le_controle_automatique_refuse_avant_toute_ecriture(banc):
    banc.corps = ("# Relance\n\nÉcrire à jeanne.dupont@client-reel.fr ou au "
                  "06 12 34 56 78, clé sk-proj-abcdefghijklmnopqrstuvwx.")
    with pytest.raises(AuthzDenied) as e:
        _publier_rest("compte-admin-org")
    assert (e.value.status, e.value.code) == (400, "contenu_refuse")
    assert banc.ecrit == [], "rien n'est publié"
    kinds = {(f["field"], f["kind"]) for f in e.value.details["found"]}
    assert kinds == {("body_md", "email"), ("body_md", "phone"), ("body_md", "secret")}
    for secret in ("jeanne.dupont", "client-reel", "06 12", "sk-proj"):
        assert secret not in e.value.message, "le refus ne recopie jamais la valeur"


def test_des_slots_refuses_par_la_publication_rendent_un_400_qui_dit_pourquoi(
        banc, monkeypatch):
    def _refuse(**k):
        raise org_store.LibrarySlotsInvalid("`slots[0].schema` invalide : `help` …")
    monkeypatch.setattr(org_store, "publish_guide", _refuse)
    with pytest.raises(AuthzDenied) as e:
        _publier_rest("compte-admin-org")
    assert (e.value.status, e.value.code) == (400, "invalid_slots")
    assert "slots[0].schema" in e.value.message


def test_le_droit_annonce_lit_la_meme_regle(banc):
    """Un drapeau de droit servi à un écran se calcule par `capacite_autorise`, qui
    exécute la règle DÉCLARÉE (#695)."""
    for cle, champs in (("library.publish", {}),
                        ("org.procedure.console", {"op": "publish"})):
        assert not _authz.capacite_autorise(cle, "compte-membre", **champs), cle
        assert not _authz.capacite_autorise(cle, "compte-operateur", **champs), cle
        assert _authz.capacite_autorise(cle, "compte-admin-org", **champs), cle
        assert _authz.capacite_autorise(cle, "compte-super", **champs), cle
    # Plus aucun plancher plateforme : un org_admin publie.
    assert _authz.platform_floor(_cap("library.publish").authz) is None
    assert _authz.platform_floor(_cap("org.procedure.console").authz) is None


def test_le_filet_du_handler_leve_le_meme_refus(banc):
    ctx = ResolvedCtx(sub="compte-membre", org_id=ORG, role="member")
    with pytest.raises(AuthzDenied) as e:
        lib._publish(ctx, lib.PublishInput(slug="veille-concurrence"))
    assert (e.value.status, e.value.code) == (403, CODE)
    assert e.value.message == _authz._refus_publication_bibliotheque().message
    assert banc.lu == [] and banc.ecrit == []


def test_la_console_garde_l_ordre_des_refus(banc):
    """Face agent : pour qui peut publier, la garde d'agent parle avant le handler.
    Pour un compte qui ne publiera pas, la règle d'admin parle d'abord."""
    for sub in ("compte-admin-org", "compte-super"):
        with pytest.raises(AuthzDenied) as e:
            _publier_mcp(sub)
        assert e.value.code == "publication_reservee_a_l_humain", sub
    for sub in ("compte-membre", "compte-operateur"):
        with pytest.raises(AuthzDenied) as e:
            _publier_mcp(sub)
        assert e.value.code == CODE, sub
    assert banc.lu == [] and banc.ecrit == []


def _fork_stub(monkeypatch, banc):
    monkeypatch.setattr(org_store, "get_library_entry",
                        lambda **k: {"id": 3, "body_md": "# corps",
                                     "moderation_state": "listed"})

    def _fork(**k):
        banc.ecrit.append(k)
        return {"owner_type": k["owner_type"], "owner_id": k["owner_id"],
                "slug": "veille-concurrence", "version": 1, "guide_id": 21,
                "forked_from": 3, "source_title": "T"}
    monkeypatch.setattr(org_store, "fork_library_entry", _fork)


def test_le_fork_d_un_org_admin_cree_une_procedure_d_org(banc, monkeypatch):
    _fork_stub(monkeypatch, banc)
    out = _servir("library.fork", "compte-admin-org", "rest", slug="veille-concurrence")
    assert out["forked"] is True and out["org_id"] == ORG and out["scope"] == "org"
    assert (banc.ecrit[-1]["owner_type"], banc.ecrit[-1]["owner_id"]) == ("org", ORG)
    assert banc.ecrit[-1]["install_org_id"] == ORG, "l'installation est notée dans l'org"


def test_le_fork_d_un_simple_membre_cree_une_procedure_perso(banc, monkeypatch):
    """Ajouter un process de la communauté n'est pas réservé aux admins, et ce qu'un
    membre crée lui appartient (décision du 06/10/2026) — l'installation compte pour
    l'org active."""
    _fork_stub(monkeypatch, banc)
    out = _servir("library.fork", "compte-membre", "rest", slug="veille-concurrence")
    assert out["forked"] is True and out["org_id"] == ORG and out["scope"] == "user"
    assert (banc.ecrit[-1]["owner_type"],
            banc.ecrit[-1]["owner_id"]) == ("user", "compte-membre")
    assert banc.ecrit[-1]["install_org_id"] == ORG


def test_le_slug_apps_est_reserve(banc):
    """`apps` est l'adresse de la page des apps de la communauté (site, application) :
    aucune procédure ne le porte. Même refus qu'un nom pris, rien d'écrit."""
    with pytest.raises(AuthzDenied) as e:
        _servir("library.publish", "compte-admin-org", "rest", slug="veille-concurrence",
                public_slug="Apps")
    assert (e.value.status, e.value.code) == (409, "slug_taken")
    assert banc.ecrit == []
