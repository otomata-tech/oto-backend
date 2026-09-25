"""Un webhook authentifié PAR SIGNATURE (Standard Webhooks) — et son porteur ÉTEINT.

Beaucoup de plateformes ne savent pas poser un en-tête `Authorization` : elles SIGNENT
leurs livraisons avec un secret qu'elles génèrent (Granola, Svix, Resend, Clerk…).
Un agent choisit son mode ; ce fichier tient les propriétés qui rendent ce mode sûr :

1. **La vérification est celle de la spécification**, prouvée contre son vecteur de
   référence publié — pas seulement cohérente avec elle-même.
2. **Le mode signature ÉTEINT le porteur** : un `otoh_` fuité n'ouvre plus la porte.
3. **Aucun oracle** : signature fausse, id inconnu, mauvais mode — le même 404. Seul
   un horodatage périmé sur une signature VALIDE est nommé.
4. **Une retentative ne refait pas de déroulé** : la source retente pendant des jours,
   le même `webhook-id` accepté une fois ne l'est pas deux.
5. **Le secret se pose, il ne se relit jamais**, et un réglage inerte est refusé.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time

import pytest

from oto_mcp import runner_hook
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 77
#: Le vecteur de référence de la spécification (standardwebhooks.com / Svix).
SPEC_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SPEC_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
SPEC_TS = "1614265330"
SPEC_CORPS = b'{"test": 2432232314}'
SPEC_SIG = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="

SECRET = "whsec_" + base64.b64encode(b"une cle de signature de test").decode()


def _signer(secret: str, msg_id: str, ts: str, corps: bytes) -> str:
    cle = base64.b64decode(secret[len("whsec_"):])
    mac = hmac.new(cle, f"{msg_id}.{ts}.".encode() + corps, hashlib.sha256).digest()
    return "v1," + base64.b64encode(mac).decode()


def _recue(corps=b'{"note_id": "not_1"}', msg_id="evt_1", ts=None, secret=SECRET,
           signatures=None):
    ts = ts or str(int(time.time()))
    return runner_hook.SignatureRecue(
        msg_id, ts, signatures or _signer(secret, msg_id, ts, corps), corps)


# ── 1. la VÉRIFICATION ────────────────────────────────────────────────────────

def test_le_vecteur_de_reference_de_la_SPECIFICATION_passe():
    """⚠️ LE banc du lot : prouvé contre la valeur publiée, pas contre notre propre
    signeur — un signeur et un vérifieur faux de la même façon s'accorderaient."""
    r = runner_hook.SignatureRecue(SPEC_ID, SPEC_TS, SPEC_SIG, SPEC_CORPS)
    assert runner_hook.verifier_signature(SPEC_SECRET, r,
                                          maintenant=int(SPEC_TS)) == "ok"


def test_un_corps_RE_SERIALISE_ne_passe_pas():
    """La signature porte sur les octets REÇUS : un espace de moins suffit."""
    r = runner_hook.SignatureRecue(SPEC_ID, SPEC_TS, SPEC_SIG, b'{"test":2432232314}')
    assert runner_hook.verifier_signature(SPEC_SECRET, r,
                                          maintenant=int(SPEC_TS)) == "invalid"


@pytest.mark.parametrize("fausse", [
    lambda r: runner_hook.SignatureRecue(r.msg_id, r.horodatage, r.signatures,
                                         r.brut + b" "),
    lambda r: runner_hook.SignatureRecue("evt_autre", r.horodatage, r.signatures, r.brut),
    lambda r: runner_hook.SignatureRecue(r.msg_id, str(int(r.horodatage) + 1),
                                         r.signatures, r.brut),
    lambda r: runner_hook.SignatureRecue(r.msg_id, r.horodatage,
                                         r.signatures.replace("v1,", "v2,"), r.brut),
    lambda r: runner_hook.SignatureRecue(r.msg_id, "pas-un-nombre", r.signatures, r.brut),
])
def test_toute_alteration_rend_INVALID(fausse):
    assert runner_hook.verifier_signature(SECRET, fausse(_recue())) == "invalid"


def test_un_AUTRE_secret_rend_invalid():
    autre = "whsec_" + base64.b64encode(b"autre cle").decode()
    assert runner_hook.verifier_signature(autre, _recue()) == "invalid"


def test_plusieurs_signatures_UNE_suffit():
    """Une rotation côté source envoie l'ancienne ET la nouvelle."""
    bonne = _recue()
    r = runner_hook.SignatureRecue(bonne.msg_id, bonne.horodatage,
                                   f"v1,AAAA {bonne.signatures}", bonne.brut)
    assert runner_hook.verifier_signature(SECRET, r) == "ok"


def test_un_horodatage_hors_fenetre_rend_STALE_dans_les_deux_sens():
    r = _recue(ts=str(int(time.time()) - 301))
    assert runner_hook.verifier_signature(SECRET, r) == "stale"
    r = _recue(ts=str(int(time.time()) + 301))
    assert runner_hook.verifier_signature(SECRET, r) == "stale"
    assert runner_hook.verifier_signature(
        SECRET, _recue(ts=str(int(time.time()) - 299))) == "ok"


def test_STALE_n_est_rendu_que_pour_une_signature_VALIDE():
    """Nommer « périmé » sur une signature fausse dirait à un inconnu qu'il n'a
    raté que l'horloge."""
    r = _recue(ts=str(int(time.time()) - 3600))
    r = runner_hook.SignatureRecue(r.msg_id, r.horodatage, "v1,AAAA", r.brut)
    assert runner_hook.verifier_signature(SECRET, r) == "invalid"


@pytest.mark.parametrize("secret", ["", "otoh_abc", "whsec_", "whsec_***pas du base64",
                                    "sk_live_abc"])
def test_un_secret_qui_n_a_pas_la_forme_whsec_est_refuse(secret):
    assert runner_hook.cle_de_signature(secret) is None


def test_un_identifiant_LONG_se_verifie_entier_et_se_stocke_borne(signe):
    """L'id est SIGNÉ : le tronquer avant la vérification refuserait une
    livraison légitime. Il n'est borné qu'au stockage."""
    long_id = "evt_" + "x" * 400
    r = _recue(msg_id=long_id)
    assert runner_hook.signature_des_entetes(
        {"webhook-id": long_id, "webhook-timestamp": r.horodatage,
         "webhook-signature": r.signatures}, r.brut).msg_id == long_id
    runner_hook.declencher(5, None, {}, "x", signature=r)
    assert signe["livraisons"][0][2] == long_id[:256]


def test_un_id_inconnu_coute_LE_MEME_calcul_qu_une_vraie_verification(signe, monkeypatch):
    """Pas d'oracle par la durée : le refus d'un id inconnu vérifie quand même."""
    appels = []
    vrai = runner_hook.verifier_signature
    monkeypatch.setattr(runner_hook, "verifier_signature",
                        lambda *a, **k: appels.append(1) or vrai(*a, **k))
    with pytest.raises(runner_hook.HookRefus):
        runner_hook.declencher(6, None, {}, "x", signature=_recue())
    assert appels == [1]


def test_les_entetes_vont_par_TROIS_ou_pas_du_tout():
    assert runner_hook.signature_des_entetes(
        {"webhook-id": "a", "webhook-timestamp": "1"}, b"") is None
    s = runner_hook.signature_des_entetes(
        {"webhook-id": "a", "webhook-timestamp": "1", "webhook-signature": "v1,x"}, b"{}")
    assert s is not None and s.brut == b"{}"


# ── 2. la ROUTE : porteur éteint, aucun oracle, rejeu, doublon ─────────────────

class _Conn:
    def __init__(self, vu):
        self.vu = vu

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        raise AssertionError(f"SQL direct inattendu : {sql!r}")


@pytest.fixture
def signe(monkeypatch):
    """Un agent en mode signature ; le chiffrement réel, avec une clé de test."""
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    vu = {"livraisons": [], "deja": None}
    t = {"id": 5, "org_id": ORG, "sub": "alexis", "procedure": "veille",
         "tools": ["a"], "input": "fais la veille", "enabled": True,
         "kind": "webhook", "payload_mode": "inline", "payload_fields": None,
         "max_per_hour": None, "fraicheur_s": None, "model": None,
         "project_id": None, "max_steps": None, "label": None,
         "hook_auth": "standard_webhooks",
         "hook_signing_secret_enc": runner_hook.chiffrer_secret_de_signature(5, SECRET)}
    vu["trigger"] = t
    monkeypatch.setattr(runner_hook.db, "trigger_signe",
                        lambda i: dict(t) if i == 5 else None)
    # Le porteur, lui, ne trouve RIEN : la garde `hook_auth = 'bearer'` est en SQL.
    monkeypatch.setattr(runner_hook.db, "trigger_par_secret", lambda i, h: None)
    monkeypatch.setattr(runner_hook.db, "_connect", lambda: _Conn(vu))
    monkeypatch.setattr(runner_hook.db, "verrouiller_le_declencheur", lambda c, i: None)
    monkeypatch.setattr(runner_hook.db, "retard_de_lissage", lambda c, t_, d, f: 0)
    monkeypatch.setattr(runner_hook.db, "livraison_acceptee",
                        lambda c, t_, e: vu["deja"] if vu["deja"] and
                        vu["deja"]["external_id"] == e else None)
    monkeypatch.setattr(
        runner_hook.db, "enregistrer",
        lambda c, t_, o, outcome, job_id=None, source=None, due_at=None,
        external_id=None: vu["livraisons"].append((outcome, job_id, external_id)) or 1)
    monkeypatch.setattr(runner_hook.db, "enqueue_job",
                        lambda org, kind, **kw: vu.update(enfile=kw) or {"id": 900})
    return vu


def test_une_signature_valide_ENFILE_et_garde_l_identifiant(signe):
    r = _recue()
    out = runner_hook.declencher(5, None, {"note_id": "not_1"}, "Granola",
                                 signature=r)
    assert out["job_id"] == 900 and not out.get("duplicate")
    assert signe["livraisons"] == [(runner_hook.db.QUEUED, 900, "evt_1")]
    assert "not_1" in signe["enfile"]["payload"]["input"]


def test_le_PORTEUR_est_refuse_sur_un_agent_en_mode_signature(signe):
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, "otoh_le_bon_d_avant", {}, "curl")
    assert e.value.statut == 404 and e.value.message == runner_hook.HOOK_INCONNU


def test_signature_fausse_et_id_inconnu_rendent_LE_MEME_refus_et_ne_s_ecrivent_pas(signe):
    faux = _recue(secret="whsec_" + base64.b64encode(b"intrus").decode())
    refus = []
    for tid, sig in ((5, faux), (6, _recue())):
        with pytest.raises(runner_hook.HookRefus) as e:
            runner_hook.declencher(tid, None, {}, "x", signature=sig)
        refus.append((e.value.statut, e.value.code, e.value.message))
    assert refus[0] == refus[1] == (404, "hook_not_found", runner_hook.HOOK_INCONNU)
    assert signe["livraisons"] == [], "un inconnu ne remplit pas le journal d'autrui"


def test_un_REJEU_hors_fenetre_est_nomme_et_journalise(signe):
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, None, {}, "Granola",
                               signature=_recue(ts=str(int(time.time()) - 3600)))
    assert (e.value.statut, e.value.code) == (400, "hook_stale_timestamp")
    assert signe["livraisons"] == [(runner_hook.db.REFUSE_STALE, None, "evt_1")]
    assert "enfile" not in signe


def test_une_RETENTATIVE_du_meme_identifiant_ne_refait_pas_de_deroule(signe):
    """⚠️ Granola retente quatre jours sur un délai d'attente — y compris quand
    notre écriture avait abouti. Chaque retentative serait sinon un déroulé."""
    signe["deja"] = {"id": 1, "job_id": 900, "external_id": "evt_1"}
    out = runner_hook.declencher(5, None, {}, "Granola", signature=_recue())
    assert out["duplicate"] is True and out["job_id"] == 900
    assert "enfile" not in signe and signe["livraisons"] == []


def test_un_AUTRE_identifiant_n_est_pas_un_doublon(signe):
    signe["deja"] = {"id": 1, "job_id": 900, "external_id": "evt_1"}
    out = runner_hook.declencher(5, None, {}, "Granola",
                                 signature=_recue(msg_id="evt_2"))
    assert not out.get("duplicate") and signe["enfile"]


def test_un_agent_en_PAUSE_repond_409_a_une_source_qui_a_prouve_qui_elle_est(signe):
    signe["trigger"]["enabled"] = False
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, None, {}, "Granola", signature=_recue())
    assert e.value.statut == 409
    assert signe["livraisons"] == [(runner_hook.db.REFUSE_PAUSED, None, "evt_1")]


def test_le_secret_chiffre_est_LIE_a_sa_ligne(monkeypatch):
    """Un chiffré recopié vers un autre déclencheur ne se déchiffre pas."""
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    env = runner_hook.chiffrer_secret_de_signature(5, SECRET)
    assert runner_hook._dechiffrer_secret_de_signature(5, env) == SECRET
    with pytest.raises(Exception):
        runner_hook._dechiffrer_secret_de_signature(6, env)


def test_la_route_verifie_les_OCTETS_recus(monkeypatch):
    """Bout en bout par la route : ce qui est signé est le corps tel qu'envoyé."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from oto_mcp.api import hooks

    vu = {}

    def _declencher(tid, secret, corps, source, *, signature=None,
                    par_adresse_privee=False):
        vu.update(sig=signature, secret=secret, corps=corps)
        return {"ok": True, "job_id": 1}

    monkeypatch.setattr(hooks.runner_hook, "declencher", _declencher)
    app = Starlette(routes=hooks.make_routes(lambda r: None))
    corps = b'{ "note_id" :"not_1" }'
    ts = str(int(time.time()))
    r = TestClient(app).post("/api/hooks/5", content=corps, headers={
        "webhook-id": "evt_9", "webhook-timestamp": ts,
        "webhook-signature": _signer(SECRET, "evt_9", ts, corps),
        "content-type": "application/json"})
    assert r.status_code == 202
    assert vu["sig"].brut == corps and vu["sig"].msg_id == "evt_9"
    assert runner_hook.verifier_signature(SECRET, vu["sig"]) == "ok"
    assert vu["corps"] == {"note_id": "not_1"}


# ── 3. la POSE : secret en écriture seule, réglages inertes refusés ────────────

def _ctx():
    return ResolvedCtx(sub="alexis", org_id=ORG)


def _appel(**kw):
    if kw.get("op") == "create":
        kw.setdefault("model", "claude-sonnet-5")
    return asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(**kw)))


@pytest.fixture(autouse=True)
def _org_servie(monkeypatch):
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda portee, ident, fournisseur, cle: None)
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1,
                                     "last_seen": None, "families": ["anthropic"]})
    monkeypatch.setattr(RT.db, "triggers_for_procedure", lambda o, p: [])
    monkeypatch.setattr(RT.db, "comptage_livraisons",
                        lambda t, o: {"recues_24h": 0, "refusees_24h": 0,
                                      "derniere": None})
    monkeypatch.setattr(RT.db, "file_du_declencheur",
                        lambda t, o: {"pending": 0, "held": 0})
    monkeypatch.setattr(RT.db, "comptage_perime",
                        lambda o, t: {"expired_count": 0, "expired_since": None,
                                      "expired_last": None})


@pytest.fixture
def base(monkeypatch):
    """Une « base » en mémoire pour les écritures du mode d'authentification."""
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    ligne = {"id": 5, "org_id": ORG, "sub": "alexis", "kind": "webhook",
             "procedure": "veille", "tools": ["a"], "enabled": True,
             "payload_mode": "ignore", "payload_fields": None, "model": "claude-sonnet-5",
             "hook_auth": "bearer", "signing_secret_set": False}
    etat = {"ligne": ligne, "hash": "h0", "enc": None}

    def _create(org, sub, **kw):
        ligne.update(kw)
        return dict(ligne)

    def _poser_auth(tid, org, mode, secret_enc=None, effacer_le_secret=False):
        ligne["hook_auth"] = mode
        if effacer_le_secret:
            etat["enc"] = None
        elif secret_enc is not None:
            etat["enc"] = secret_enc
        ligne["signing_secret_set"] = etat["enc"] is not None
        return True

    monkeypatch.setattr(RT.db, "create_trigger", _create)
    monkeypatch.setattr(RT.db, "get_trigger", lambda t, o: dict(ligne))
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda t, o, champs, hors_abonnement_d_autrui=None:
                        ligne.update(champs) or dict(ligne))
    monkeypatch.setattr(RT.db, "poser_auth_de_hook", _poser_auth)
    monkeypatch.setattr(RT.db, "poser_secret_de_hook",
                        lambda t, o, h: etat.update(hash=h) or True)
    return etat


def _auth(**kw):
    return asyncio.run(RT._hook_auth(_ctx(), RT.HookAuthInput(trigger_id=5, **kw)))


def test_la_pose_du_secret_n_a_PAS_de_face_MCP():
    """⚠️ La règle du dépôt : un secret brut ne passe jamais en argument d'outil —
    il finirait dans le contexte d'un modèle. `oto_trigger` ne doit même pas
    connaître le champ."""
    cap = next(c for c in RT.CAPABILITIES if c.key == "runner.trigger.hook_auth")
    assert cap.mcp is None
    assert not {"hook_auth", "signing_secret", "hook_signing_secret"} & set(
        RT.TriggerInput.model_fields)


def test_passer_en_signature_pose_le_secret_CHIFFRE_et_EFFACE_le_porteur(base):
    out = _auth(hook_auth="standard_webhooks", signing_secret=SECRET)
    assert out["trigger"]["hook_auth"] == "standard_webhooks"
    assert out["trigger"]["signing_secret_set"] is True
    assert not out.get("hook_secret")
    assert SECRET not in repr(out), "le secret ne se relit JAMAIS"
    assert base["enc"] and SECRET not in base["enc"], "stocké CHIFFRÉ"
    assert base["hash"] is None, "un otoh_ fuité ne doit pas se réveiller plus tard"


def test_le_mode_signature_SANS_secret_est_refuse(base):
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="standard_webhooks")
    assert e.value.code == "missing_signing_secret"


def test_un_secret_avec_le_PORTEUR_serait_inerte_donc_refuse(base):
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="bearer", signing_secret=SECRET)
    assert e.value.code == "not_signature_mode"


@pytest.mark.parametrize("faux", ["otoh_abc", "sk_live_x", "whsec_***"])
def test_un_secret_qui_n_est_pas_un_whsec_est_refuse(base, faux):
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="standard_webhooks", signing_secret=faux)
    assert e.value.code == "invalid_signing_secret"
    assert base["enc"] is None, "rien d'écrit sur un refus"


def test_un_agent_PROGRAMME_n_a_pas_de_porte(base):
    base["ligne"]["kind"] = "schedule"
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="standard_webhooks", signing_secret=SECRET)
    assert e.value.code == "trigger_not_found"


def test_revenir_au_porteur_EFFACE_le_secret_et_emet_un_porteur_NEUF(base):
    _auth(hook_auth="standard_webhooks", signing_secret=SECRET)
    out = _auth(hook_auth="bearer")
    assert out["hook_secret"].startswith("otoh_")
    assert base["enc"] is None and out["trigger"]["signing_secret_set"] is False
    assert base["hash"] == runner_hook.hacher(out["hook_secret"])


def test_rester_au_porteur_n_emet_RIEN(base):
    out = _auth(hook_auth="bearer")
    assert not out.get("hook_secret") and base["hash"] == "h0"


def test_remplacer_le_secret_de_signature_ne_touche_pas_au_mode(base):
    _auth(hook_auth="standard_webhooks", signing_secret=SECRET)
    premier = base["enc"]
    autre = "whsec_" + base64.b64encode(b"cle tournee").decode()
    out = _auth(hook_auth="standard_webhooks", signing_secret=autre)
    assert base["enc"] != premier and out["trigger"]["hook_auth"] == "standard_webhooks"


def test_garder_la_signature_sans_recoller_le_secret_le_CONSERVE(base):
    _auth(hook_auth="standard_webhooks", signing_secret=SECRET)
    premier = base["enc"]
    _auth(hook_auth="standard_webhooks")
    assert base["enc"] == premier


def test_une_rotation_de_PORTEUR_sur_un_agent_en_signature_est_refusee(base):
    _auth(hook_auth="standard_webhooks", signing_secret=SECRET)
    with pytest.raises(AuthzDenied) as e:
        _appel(op="rotate_secret", trigger_id=5)
    assert e.value.code == "signature_mode"


def test_un_agent_au_porteur_se_lit_bearer_par_DEFAUT(base):
    out = _appel(op="get", trigger_id=5)
    assert out["trigger"]["hook_auth"] == "bearer"
    assert out["trigger"]["signing_secret_set"] is False
