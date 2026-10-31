"""BRANCHER une source sur un agent webhook — `oto_trigger op=connect_source`.

Ce que ce fichier tient (la base est doublée ; l'écriture conditionnelle elle-même se
prouve en SQL dans `test_declencheur_webhook_db.py`) :

1. **Le porteur ne sort jamais** : ni dans la réponse, ni dans un journal — il ne
   va qu'à la source, comme `secret` de son webhook.
2. **Une inscription refusée ne change rien** : celles déjà faites sont retirées,
   la base n'est pas écrite, l'agent garde sa porte — adresse comprise.
3. **Un échec APRÈS l'inscription se compense** : la base qui casse, ou la ligne qui
   a changé, retire chez la source ce qui vient d'y être inscrit ; un retrait qui
   résiste est une erreur NOMMÉE, journalisée avec l'identifiant distant.
4. **Rebrancher = la rotation** : porteur neuf, inscriptions neuves, les anciennes
   retirées chez la source.
5. **Un agent branché garde sa porte** : `rotate_secret`, `rotate_address` et le
   changement de mode sont refusés (`source_connected`).
6. **Débrancher et supprimer retirent chez la source**, et rendent ce qui résiste.
7. **Allo SIGNE** : un endpoint pour tous les topics, son secret `whsec_` chiffré
   pour la ligne en mode `standard_webhooks` — ni rendu, ni journalisé ; une réponse
   sans secret utilisable retire l'endpoint et n'écrit rien.
8. **Le compte nommé** (`account`, deux espaces Lemlist) est épinglé à la résolution
   de la clé, retenu avec chaque inscription, et réutilisé pour la retirer.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from oto_mcp import runner_hook
from oto_mcp.capabilities import _source_du_hook as S
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 77


class _Lemlist:
    """Le client de la source : enregistre ce qu'on lui demande."""

    def __init__(self):
        self.ajouts, self.retraits = [], []
        self.refuse_ajout = None
        self.refuse_retrait: set = set()
        self.sans_id = False

    def add_webhook(self, url, *, type=None, secret=None, campaign_id=None,
                    is_first=None, zap_id=None):
        if self.refuse_ajout == type:
            # Le message d'une erreur amont peut recopier la requête — porteur compris.
            raise RuntimeError(f"upstream 400 for secret={secret}")
        self.ajouts.append({"url": url, "type": type, "secret": secret,
                            "campaign_id": campaign_id, "is_first": is_first})
        return {} if self.sans_id else {"_id": f"hoo_{len(self.ajouts)}"}

    def delete_webhook(self, hook_id):
        if hook_id in self.refuse_retrait:
            raise RuntimeError("upstream 500")
        self.retraits.append(hook_id)
        return {}


@pytest.fixture
def base(monkeypatch):
    monkeypatch.setenv("OTO_MCP_PUBLIC_URL", "https://mcp.example")
    monkeypatch.setattr(RT.db, "comptage_livraisons",
                        lambda t, o: {"recues_24h": 0, "refusees_24h": 0,
                                      "derniere": None})
    monkeypatch.setattr(RT.db, "file_du_declencheur",
                        lambda t, o: {"pending": 0, "held": 0})
    monkeypatch.setattr(RT.db, "comptage_perime",
                        lambda o, t: {"expired_count": 0, "expired_since": None,
                                      "expired_last": None})
    ligne = {"id": 5, "org_id": ORG, "sub": "owner-sub", "kind": "webhook",
             "procedure": "veille", "tools": ["a"], "enabled": True,
             "payload_mode": "inline", "payload_fields": None,
             "model": "claude-sonnet-5", "hook_auth": "bearer",
             "signing_secret_set": False, "hook_slug": "h_privee", "max_per_day": 20,
             "hook_sources": None}
    etat = {"ligne": ligne, "hash": "h0", "client": _Lemlist(), "supprime": False,
            "panne": None, "concurrent": False}

    def _brancher(tid, org, *, hook_auth, secret_hash, hook_slug, sources,
                  sources_avant, signing_secret_enc=None):
        if etat["panne"]:
            raise etat["panne"]
        if etat["concurrent"] or (ligne["hook_sources"] or []) != (sources_avant or []):
            return False
        ligne.update(hook_auth=hook_auth, hook_sources=sources, hook_slug=hook_slug,
                     signing_secret_set=signing_secret_enc is not None)
        etat["hash"] = secret_hash
        etat["secret_enc"] = signing_secret_enc
        return True

    def _poser_sources(tid, org, sources, *, sources_avant):
        if etat["concurrent"] or (ligne["hook_sources"] or []) != (sources_avant or []):
            return False
        ligne.update(hook_sources=sources or None)
        return True

    def _interdit(*a, **k):
        raise AssertionError("connect_source n'écrit qu'en UNE transaction")

    monkeypatch.setattr(RT.db, "get_trigger", lambda t, o: dict(ligne))
    monkeypatch.setattr(RT.db, "brancher_la_source", _brancher)
    monkeypatch.setattr(RT.db, "poser_sources_de_hook", _poser_sources)
    monkeypatch.setattr(RT.db, "poser_adresse_de_hook",
                        lambda t, o, slug: ligne.update(hook_slug=slug) or True)
    monkeypatch.setattr(RT.db, "poser_secret_de_hook",
                        lambda t, o, h: etat.update(hash=h) or True)
    monkeypatch.setattr(RT.db, "poser_auth_de_hook",
                        lambda t, o, m, secret_enc=None, effacer_le_secret=False:
                        ligne.update(hook_auth=m) or True)
    monkeypatch.setattr(RT.db, "delete_trigger",
                        lambda t, o: etat.update(supprime=True) or True)
    monkeypatch.setattr(S, "_client",
                        lambda ctx, source, account=None: etat["client"])
    etat["interdit"] = _interdit
    return etat


def _ctx(sub="owner-sub"):
    return ResolvedCtx(sub=sub, org_id=ORG)


def _appel(**kw):
    return asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(trigger_id=5, **kw)))


def _brancher(**kw):
    kw.setdefault("events", ["emailsReplied", "linkedinReplied"])
    return _appel(op="connect_source", source="lemlist", **kw)


def _porte_intacte(base, slug="h_privee"):
    assert base["ligne"]["hook_auth"] == "bearer" and base["hash"] == "h0"
    assert base["ligne"]["hook_slug"] == slug
    assert not base["ligne"]["hook_sources"]


# ── 1. le porteur ne sort jamais ──────────────────────────────────────────────

def test_brancher_inscrit_chez_la_source_avec_le_porteur_de_l_agent(base, monkeypatch):
    monkeypatch.setattr(RT.db, "poser_adresse_de_hook", base["interdit"])
    monkeypatch.setattr(RT.db, "poser_secret_de_hook", base["interdit"])
    monkeypatch.setattr(RT.db, "poser_auth_de_hook", base["interdit"])
    out = _brancher(campaign_id="cam_1")
    c = base["client"]
    assert [a["type"] for a in c.ajouts] == ["emailsReplied", "linkedinReplied"]
    porteur = c.ajouts[0]["secret"]
    assert porteur.startswith(runner_hook.HOOK_SECRET_PREFIX)
    assert base["hash"] == runner_hook.hacher(porteur), "la source détient LE porteur"
    assert {a["secret"] for a in c.ajouts} == {porteur}
    assert c.ajouts[0]["url"] == "https://mcp.example/api/hooks/h_privee"
    assert c.ajouts[0]["campaign_id"] == "cam_1"
    assert base["ligne"]["hook_auth"] == runner_hook.BODY_SECRET
    assert [x["remote_id"] for x in out["connected"]] == ["hoo_1", "hoo_2"]
    assert out["trigger"]["hook_sources"] == base["ligne"]["hook_sources"]
    assert out["remote_cleanup_failed"] == []


def test_le_porteur_n_est_NI_rendu_NI_journalise(base, caplog):
    caplog.set_level(logging.DEBUG)
    out = _brancher()
    porteur = base["client"].ajouts[0]["secret"]
    assert porteur not in repr(out) and not out.get("hook_secret")
    assert porteur not in caplog.text


def test_un_agent_SANS_adresse_privee_en_recoit_une_avec_le_branchement(base):
    base["ligne"]["hook_slug"] = None
    _brancher()
    slug = base["ligne"]["hook_slug"]
    assert slug.startswith(runner_hook.ADRESSE_PREFIX)
    assert base["client"].ajouts[0]["url"].endswith("/api/hooks/" + slug)


def test_sans_adresse_publique_rien_n_est_inscrit(base, monkeypatch):
    """Une URL relative inscrite chez un tiers ne répondrait jamais."""
    monkeypatch.delenv("OTO_MCP_PUBLIC_URL")
    with pytest.raises(AuthzDenied) as e:
        _brancher()
    assert e.value.code == "public_url_unset" and not base["client"].ajouts
    _porte_intacte(base)


@pytest.mark.parametrize("kw, code", [
    ({"events": []}, "events_required"),
    ({"events": ["  "]}, "events_required"),
    ({"events": [f"e{i}" for i in range(11)]}, "too_many_events"),
])
def test_les_evenements_sont_EXIGES_et_bornes(base, kw, code):
    with pytest.raises(AuthzDenied) as e:
        _brancher(**kw)
    assert e.value.code == code and not base["client"].ajouts


def test_sans_source_le_branchement_est_refuse(base):
    with pytest.raises(AuthzDenied) as e:
        _appel(op="connect_source", events=["emailsReplied"])
    assert e.value.code == "unknown_source"


def test_un_agent_PROGRAMME_ne_se_branche_pas(base):
    base["ligne"]["kind"] = "schedule"
    with pytest.raises(AuthzDenied) as e:
        _brancher()
    assert e.value.code == "trigger_not_found"


def test_brancher_l_agent_d_un_AUTRE_est_refuse(base, monkeypatch):
    monkeypatch.setattr(RT.roles, "is_org_admin", lambda s, o: False)
    monkeypatch.setattr(RT._acces_agent, "niveau", lambda s, o, a: "editor")
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(RT._triggers(_ctx("other-sub"), RT.TriggerInput(
            op="connect_source", trigger_id=5, source="lemlist",
            events=["emailsReplied"])))
    assert e.value.code == "trigger_owner_or_admin_required"
    assert not base["client"].ajouts


def test_la_cle_est_celle_de_l_APPELANT_jamais_une_cle_partagee(monkeypatch):
    """`want="byo"` : un rang partagé inscrirait le webhook sur le compte d'un autre."""
    from oto_mcp import access
    vu = {}

    def _resoudre(provider, want="auto", sub=None, **kw):
        vu.update(provider=provider, want=want, sub=sub, **kw)
        return SimpleNamespace(key="cle-de-test")

    monkeypatch.setattr(access, "resolve_credential", _resoudre)
    client = S._client(_ctx(), "lemlist")
    assert vu == {"provider": "lemlist", "want": "byo", "sub": "owner-sub",
                  "check_usage": False}
    assert type(client).__name__ == "LemlistClient"


def test_sans_cle_le_branchement_est_refuse_par_son_nom(monkeypatch):
    from mcp.types import ErrorData, INVALID_PARAMS

    from oto_mcp import access
    from oto_mcp.mcp_errors import McpError

    def _sans(*a, **k):
        raise McpError(ErrorData(code=INVALID_PARAMS, message="no key"))

    monkeypatch.setattr(access, "resolve_credential", _sans)
    with pytest.raises(AuthzDenied) as e:
        S._client(_ctx(), "lemlist")
    assert e.value.code == "credential_unavailable"


# ── 2. une inscription refusée ne change rien ─────────────────────────────────

def test_une_inscription_REFUSEE_defait_les_autres_et_n_ecrit_rien(base, caplog):
    caplog.set_level(logging.DEBUG)
    base["client"].refuse_ajout = "linkedinReplied"
    with pytest.raises(AuthzDenied) as e:
        _brancher()
    assert e.value.code == "source_registration_failed"
    assert base["client"].retraits == ["hoo_1"], "la première est retirée"
    _porte_intacte(base)
    porteur = base["client"].ajouts[0]["secret"]
    assert "upstream 400" not in e.value.message, "jamais le message brut du tiers"
    assert porteur not in e.value.message and porteur not in caplog.text


def test_une_inscription_refusee_ne_pose_PAS_l_adresse_privee(base):
    """Poser l'adresse privée remplace l'adresse numérique — qu'une autre source
    utilise peut-être : un branchement refusé ne la touche pas."""
    base["ligne"]["hook_slug"] = None
    base["client"].refuse_ajout = "emailsReplied"
    with pytest.raises(AuthzDenied):
        _brancher()
    _porte_intacte(base, slug=None)


def test_une_inscription_SANS_identifiant_est_refusee(base):
    base["client"].sans_id = True
    with pytest.raises(AuthzDenied) as e:
        _brancher()
    assert e.value.code == "source_registration_failed"
    _porte_intacte(base)


def test_un_refus_dont_le_RETRAIT_resiste_est_nomme(base, caplog):
    base["client"].refuse_ajout = "linkedinReplied"
    base["client"].refuse_retrait = {"hoo_1"}
    with pytest.raises(AuthzDenied) as e:
        _brancher()
    assert e.value.code == "source_cleanup_failed" and "hoo_1" in e.value.message
    assert any(r.levelno == logging.ERROR and "hoo_1" in r.getMessage()
               for r in caplog.records)
    _porte_intacte(base)


# ── 3. un échec APRÈS l'inscription se compense ───────────────────────────────

def test_la_base_qui_CASSE_apres_l_inscription_retire_chez_la_source(base):
    base["panne"] = RuntimeError("connection lost")
    with pytest.raises(RuntimeError, match="connection lost"):
        _brancher()
    assert base["client"].retraits == ["hoo_1", "hoo_2"], "aucun webhook orphelin"
    _porte_intacte(base)


def test_la_compensation_qui_ECHOUE_est_nommee_et_journalisee(base, caplog):
    caplog.set_level(logging.DEBUG)
    base["panne"] = RuntimeError("connection lost")
    base["client"].refuse_retrait = {"hoo_2"}
    with pytest.raises(AuthzDenied) as e:
        _brancher()
    assert e.value.code == "source_cleanup_failed"
    assert "hoo_2" in e.value.message and "hoo_1" not in e.value.message
    assert isinstance(e.value.__context__, RuntimeError), "la cause reste chaînée"
    assert base["client"].retraits == ["hoo_1"]
    erreurs = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("hoo_2" in m for m in erreurs), "l'id distant est au journal"
    porteur = base["client"].ajouts[0]["secret"]
    assert porteur not in caplog.text and porteur not in e.value.message


def test_une_ligne_qui_a_CHANGE_pendant_le_branchement_est_compensee(base):
    """Un branchement concurrent (ou l'agent supprimé) : rien n'est écrasé, ce qui
    vient d'être inscrit est retiré."""
    base["concurrent"] = True
    with pytest.raises(AuthzDenied) as e:
        _brancher()
    assert e.value.code == "trigger_changed"
    assert base["client"].retraits == ["hoo_1", "hoo_2"]
    _porte_intacte(base)


# ── 4. rebrancher = la rotation ───────────────────────────────────────────────

def test_REBRANCHER_renouvelle_le_porteur_et_retire_les_anciennes(base):
    _brancher()
    premier = base["hash"]
    out = _brancher(events=["emailsReplied"])
    assert base["hash"] != premier
    assert base["client"].retraits == ["hoo_1", "hoo_2"]
    assert [x["remote_id"] for x in base["ligne"]["hook_sources"]] == ["hoo_3"]
    assert out["remote_cleanup_failed"] == []


def test_un_ancien_webhook_qui_RESISTE_est_rendu(base):
    _brancher()
    base["client"].refuse_retrait = {"hoo_2"}
    out = _brancher()
    assert [x["remote_id"] for x in out["remote_cleanup_failed"]] == ["hoo_2"]


# ── 5. un agent branché garde sa porte ────────────────────────────────────────

@pytest.mark.parametrize("op", ["rotate_secret", "rotate_address"])
def test_un_agent_BRANCHE_refuse_les_rotations(base, op):
    _brancher()
    with pytest.raises(AuthzDenied) as e:
        _appel(op=op)
    assert e.value.code == "source_connected"


def test_un_agent_BRANCHE_refuse_le_changement_de_mode(base):
    _brancher()
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(RT._hook_auth(_ctx(), RT.HookAuthInput(trigger_id=5,
                                                          hook_auth="bearer")))
    assert e.value.code == "source_connected"


# ── 6. débrancher, supprimer ──────────────────────────────────────────────────

def test_DEBRANCHER_retire_chez_la_source_et_garde_le_mode(base):
    _brancher()
    out = _appel(op="disconnect_source")
    assert base["client"].retraits == ["hoo_1", "hoo_2"]
    assert not base["ligne"]["hook_sources"]
    assert base["ligne"]["hook_auth"] == runner_hook.BODY_SECRET
    assert [x["remote_id"] for x in out["disconnected"]] == ["hoo_1", "hoo_2"]
    _appel(op="rotate_secret")   # débranché : la porte se change de nouveau


def test_debrancher_GARDE_ce_qui_resiste_pour_le_retirer_plus_tard(base):
    _brancher()
    base["client"].refuse_retrait = {"hoo_1"}
    out = _appel(op="disconnect_source")
    assert [x["remote_id"] for x in base["ligne"]["hook_sources"]] == ["hoo_1"]
    assert [x["remote_id"] for x in out["remote_cleanup_failed"]] == ["hoo_1"]


def test_debrancher_une_ligne_qui_a_CHANGE_ne_l_ecrase_pas(base):
    _brancher()
    avant = list(base["ligne"]["hook_sources"])
    base["concurrent"] = True
    with pytest.raises(AuthzDenied) as e:
        _appel(op="disconnect_source")
    assert e.value.code == "trigger_changed"
    assert base["ligne"]["hook_sources"] == avant


def test_SUPPRIMER_un_agent_branche_retire_chez_la_source(base):
    _brancher()
    out = _appel(op="delete")
    assert base["supprime"] and base["client"].retraits == ["hoo_1", "hoo_2"]
    assert out["ok"] is True and out["remote_cleanup_failed"] == []


def test_supprimer_un_agent_JAMAIS_branche_garde_son_recu(base):
    assert _appel(op="delete") == {"ok": True}
    assert base["supprime"] and not base["client"].retraits


def test_supprimer_SANS_cle_de_la_source_supprime_quand_meme_et_le_dit(base, monkeypatch):
    _brancher()

    def _sans_cle(ctx, source, account=None):
        raise AuthzDenied(400, "credential_unavailable", "no key")

    monkeypatch.setattr(S, "_client", _sans_cle)
    out = _appel(op="delete")
    assert base["supprime"]
    assert {x["error"] for x in out["remote_cleanup_failed"]} == {"credential_unavailable"}


# ── 8. le compte nommé ────────────────────────────────────────────────────────

def test_le_compte_NOMME_est_epingle_a_la_resolution_puis_relache(monkeypatch):
    """Deux espaces Lemlist : `account` vise l'un d'eux comme `_account=` le ferait."""
    from oto_mcp import access, session_org
    vu = {}

    def _resoudre(provider, want="auto", sub=None, **kw):
        vu["compte"] = session_org.current_call_account()
        return SimpleNamespace(key="cle-de-test")

    monkeypatch.setattr(access, "resolve_credential", _resoudre)
    S._client(_ctx(), "lemlist", "Lemlist Team 2")
    assert vu["compte"] == "Lemlist Team 2"
    assert session_org.current_call_account() is None
    S._client(_ctx(), "lemlist")
    assert vu["compte"] is None


def test_plusieurs_comptes_SANS_account_dit_comment_le_nommer(monkeypatch):
    from mcp.types import ErrorData, INVALID_PARAMS

    from oto_mcp import access
    from oto_mcp.mcp_errors import McpError

    def _ambigu(*a, **k):
        raise McpError(ErrorData(code=INVALID_PARAMS, message=(
            "Multiple comptes `lemlist` — pass `_account=\"<name>\"` on this call.")))

    monkeypatch.setattr(access, "resolve_credential", _ambigu)
    with pytest.raises(AuthzDenied) as e:
        S._client(_ctx(), "lemlist")
    assert e.value.code == "credential_unavailable"
    assert "`account`" in e.value.message


class _ParCompte:
    """`_client` doublé : un client Lemlist par compte, pour voir lequel retire quoi."""

    def __init__(self):
        self.clients: dict = {}

    def __call__(self, ctx, source, account=None):
        return self.clients.setdefault(account, _Lemlist())


def test_brancher_avec_un_compte_le_RETIENT_et_debrancher_le_reutilise(base, monkeypatch):
    par_compte = _ParCompte()
    monkeypatch.setattr(S, "_client", par_compte)
    out = _brancher(account="Lemlist Team 2")
    assert {x["account"] for x in out["connected"]} == {"Lemlist Team 2"}
    assert {x["account"] for x in base["ligne"]["hook_sources"]} == {"Lemlist Team 2"}
    assert len(par_compte.clients["Lemlist Team 2"].ajouts) == 2
    _appel(op="disconnect_source")
    assert par_compte.clients["Lemlist Team 2"].retraits == ["hoo_1", "hoo_2"]
    assert None not in par_compte.clients, "jamais la clé par défaut pour retirer"


def test_REBRANCHER_sur_un_autre_compte_retire_les_anciennes_avec_LEUR_cle(base, monkeypatch):
    par_compte = _ParCompte()
    monkeypatch.setattr(S, "_client", par_compte)
    _brancher(account="Lemlist Team 1", events=["emailsReplied"])
    out = _brancher(account="Lemlist Team 2", events=["emailsReplied"])
    assert par_compte.clients["Lemlist Team 1"].retraits == ["hoo_1"]
    assert par_compte.clients["Lemlist Team 2"].retraits == []
    assert out["remote_cleanup_failed"] == []
    assert {x["account"] for x in base["ligne"]["hook_sources"]} == {"Lemlist Team 2"}


def test_sans_account_les_inscriptions_restent_COMME_AVANT(base):
    out = _brancher()
    assert all("account" not in x for x in out["connected"])


# ── 7. Allo : la source signe ─────────────────────────────────────────────────

_WHSEC = "whsec_" + "QUxMT19TSUdOSU5HX1NFQ1JFVF9URVNUXzMyQg=="


class _Allo:
    """Le client Allo : son transport rend l'endpoint créé AVEC son secret."""

    def __init__(self, secret=_WHSEC, sans_id=False, refuse=False):
        self.creations, self.retraits = [], []
        self.secret, self.sans_id, self.refuse = secret, sans_id, refuse
        self.refuse_retrait: set = set()

    def _request(self, method, path, body=None, **kw):
        assert (method, path) == ("POST", "/v2/api/webhooks")
        if self.refuse:
            raise RuntimeError(f"upstream 403 for {body}")
        self.creations.append(body)
        data = {"url": body["url"], "topics": body["topics"], "enabled": True}
        if not self.sans_id:
            data["id"] = f"ep_{len(self.creations)}"
        if self.secret is not None:
            data["signing_secret"] = self.secret
        return {"data": data}

    def delete_webhook(self, hook_id):
        if hook_id in self.refuse_retrait:
            raise RuntimeError("upstream 500")
        self.retraits.append(hook_id)
        return {}


@pytest.fixture
def allo(base, monkeypatch):
    base["client"] = _Allo()
    monkeypatch.setattr(runner_hook, "chiffrer_secret_de_signature",
                        lambda tid, secret: f"enc[{tid}]:{secret}")
    return base


def _brancher_allo(**kw):
    kw.setdefault("events", ["call.completed"])
    return _appel(op="connect_source", source="allo", **kw)


def test_allo_UN_endpoint_tous_les_topics_et_le_mode_signature(allo, monkeypatch):
    monkeypatch.setattr(RT.db, "poser_auth_de_hook", allo["interdit"])
    out = _brancher_allo(events=["call.completed", "summary.updated"])
    c = allo["client"]
    assert c.creations == [{"url": "https://mcp.example/api/hooks/h_privee",
                            "topics": ["call.completed", "summary.updated"],
                            "description": "oto agent 5"}]
    assert allo["ligne"]["hook_auth"] == runner_hook.STANDARD_WEBHOOKS
    assert allo["secret_enc"] == f"enc[5]:{_WHSEC}", "le secret d'ALLO, chiffré pour SA ligne"
    assert out["connected"] == [{"source": "allo", "remote_id": "ep_1",
                                 "event": "call.completed, summary.updated",
                                 "campaign_id": None}]
    assert out["trigger"]["signing_secret_set"] is True


def test_allo_le_secret_n_est_NI_rendu_NI_journalise(allo, caplog):
    caplog.set_level(logging.DEBUG)
    out = _brancher_allo()
    assert _WHSEC not in repr(out) and not out.get("hook_secret")
    assert _WHSEC not in caplog.text


@pytest.mark.parametrize("kw, code", [
    ({"events": ["call.finished"]}, "unknown_event"),
    ({"campaign_id": "cam_1"}, "option_not_supported"),
    ({"is_first": True}, "option_not_supported"),
])
def test_allo_refuse_avant_tout_appel(allo, kw, code):
    with pytest.raises(AuthzDenied) as e:
        _brancher_allo(**kw)
    assert e.value.code == code and not allo["client"].creations
    _porte_intacte(allo)


def test_allo_qui_REFUSE_n_ecrit_rien_et_ne_recopie_pas_la_requete(allo, caplog):
    allo["client"].refuse = True
    with pytest.raises(AuthzDenied) as e:
        _brancher_allo()
    assert e.value.code == "source_registration_failed"
    assert "mcp.example" not in str(e.value) and "mcp.example" not in caplog.text
    _porte_intacte(allo)


@pytest.mark.parametrize("secret", [None, "", "otoh_pas_un_secret", "whsec_%%%"])
def test_allo_SANS_secret_utilisable_retire_l_endpoint(allo, secret):
    allo["client"].secret = secret
    with pytest.raises(AuthzDenied) as e:
        _brancher_allo()
    assert e.value.code == "source_registration_failed"
    assert allo["client"].retraits == ["ep_1"]
    _porte_intacte(allo)


def test_allo_SANS_identifiant_est_refuse(allo):
    allo["client"].sans_id = True
    with pytest.raises(AuthzDenied) as e:
        _brancher_allo()
    assert e.value.code == "source_registration_failed"
    _porte_intacte(allo)


def test_allo_la_base_qui_CASSE_retire_l_endpoint(allo):
    allo["panne"] = RuntimeError("db down")
    with pytest.raises(RuntimeError):
        _brancher_allo()
    assert allo["client"].retraits == ["ep_1"]


def test_allo_REBRANCHER_cree_un_endpoint_neuf_et_retire_l_ancien(allo):
    _brancher_allo()
    _brancher_allo()
    assert [x["remote_id"] for x in allo["ligne"]["hook_sources"]] == ["ep_2"]
    assert allo["client"].retraits == ["ep_1"]


def test_allo_DEBRANCHER_retire_l_endpoint(allo):
    _brancher_allo()
    out = _appel(op="disconnect_source")
    assert allo["client"].retraits == ["ep_1"]
    assert out["disconnected"][0]["remote_id"] == "ep_1"
    assert not allo["ligne"]["hook_sources"]
