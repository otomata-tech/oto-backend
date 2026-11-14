"""Un webhook signé par LINEAR — le schéma générique `hmac_body` et son préréglage `linear`.

Linear signe le corps BRUT (`Linear-Signature` = HMAC-SHA256 hexadécimal, clé = le
secret du webhook), met l'horodatage d'envoi DANS le corps (`webhookTimestamp`, ms) et
identifie chaque livraison (`Linear-Delivery`). https://linear.app/developers/webhooks

Ce fichier tient :

1. **La vérification est celle de Linear**, prouvée contre un vecteur calculé HORS de
   notre code (`openssl dgst -sha256 -hmac`), pas contre notre propre signeur.
2. **Un rejeu au-delà de 60 s est refusé**, et nommé seulement pour une signature VALIDE.
3. **Chacun sa porte** : une signature Linear n'ouvre pas un agent `standard_webhooks`,
   ni l'inverse ; le porteur n'ouvre pas un agent `linear` ; même 404 partout.
4. **Une retentative du même `Linear-Delivery` ne refait pas de déroulé.**
5. **Linear reçoit 200**, pas 202 : il compte tout autre statut comme un échec. Les
   autres préréglages gardent 202.
6. **`linear` ne se pose pas à la main** : son secret vient de `connect_source`.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest

from oto_mcp import runner_hook
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

pytestmark = pytest.mark.usefixtures("agents_sans_partages", "procedures_connues")

ORG = 77
LINEAR = runner_hook.PREREGLAGES[runner_hook.LINEAR]

#: Le vecteur de référence, calculé HORS de notre code :
#:   printf '%s' '<CORPS_REF>' > corps.json
#:   openssl dgst -sha256 -hmac 'lin_wh_test_secret_0123456789' corps.json
SECRET_REF = "lin_wh_test_secret_0123456789"
CORPS_REF = (b'{"action":"update","type":"Issue","data":{"id":"iss_1",'
             b'"identifier":"ENG-42"},"webhookTimestamp":1761998400000}')
SIG_REF = "541707caffe497128d61f30b77a7c386d8b230ac99f3abee87c37d4f3fa507c8"
MAINTENANT_REF = 1761998400.0

SECRET = "un-secret-de-signature-linear-de-test"


def _signer(secret: str, brut: bytes) -> str:
    return hmac.new(secret.encode(), brut, hashlib.sha256).hexdigest()


def _corps(ts_ms: int | None = None, **extra) -> tuple[dict, bytes]:
    import time
    corps = {"action": "create", "type": "Comment", "data": {"id": "com_1"}, **extra}
    if ts_ms is not False:
        corps["webhookTimestamp"] = ts_ms if ts_ms is not None else int(time.time() * 1000)
    return corps, json.dumps(corps).encode()


def _recue(brut: bytes, secret: str = SECRET, delivery: str | None = "dlv_1",
           signature: str | None = None) -> runner_hook.SignatureDuCorps:
    return runner_hook.SignatureDuCorps(runner_hook.LINEAR,
                                        signature or _signer(secret, brut),
                                        delivery, brut)


# ── 1. la VÉRIFICATION ────────────────────────────────────────────────────────

def test_le_vecteur_calcule_par_OPENSSL_passe():
    """⚠️ LE banc du lot : un signeur et un vérifieur faux de la même façon
    s'accorderaient ; openssl, lui, ne connaît pas notre code."""
    corps = json.loads(CORPS_REF)
    r = runner_hook.SignatureDuCorps(runner_hook.LINEAR, SIG_REF, "dlv", CORPS_REF)
    assert runner_hook.verifier_signature_du_corps(
        SECRET_REF, LINEAR, r, corps, maintenant=MAINTENANT_REF) == "ok"


def test_un_corps_RE_SERIALISE_ne_passe_pas():
    corps = json.loads(CORPS_REF)
    brut = json.dumps(corps).encode()   # espaces après « : » et « , »
    assert brut != CORPS_REF
    r = runner_hook.SignatureDuCorps(runner_hook.LINEAR, SIG_REF, "dlv", brut)
    assert runner_hook.verifier_signature_du_corps(
        SECRET_REF, LINEAR, r, corps, maintenant=MAINTENANT_REF) == "invalid"


@pytest.mark.parametrize("signature", [
    SIG_REF[:-2] + "00",            # un octet faux
    SIG_REF[:-1],                   # hexadécimal impair
    "zz" + SIG_REF[2:],             # pas de l'hexadécimal
    "sha256=" + SIG_REF,            # la forme GitHub, pas celle de Linear
    "",
])
def test_toute_alteration_de_l_en_tete_rend_INVALID(signature):
    r = runner_hook.SignatureDuCorps(runner_hook.LINEAR, signature, "dlv", CORPS_REF)
    assert runner_hook.verifier_signature_du_corps(
        SECRET_REF, LINEAR, r, json.loads(CORPS_REF),
        maintenant=MAINTENANT_REF) == "invalid"


def test_l_hexadecimal_en_MAJUSCULES_est_le_meme_condense():
    r = runner_hook.SignatureDuCorps(runner_hook.LINEAR, SIG_REF.upper(), "dlv",
                                     CORPS_REF)
    assert runner_hook.verifier_signature_du_corps(
        SECRET_REF, LINEAR, r, json.loads(CORPS_REF),
        maintenant=MAINTENANT_REF) == "ok"


@pytest.mark.parametrize("secret", ["un autre secret", ""])
def test_un_AUTRE_secret_rend_invalid(secret):
    r = runner_hook.SignatureDuCorps(runner_hook.LINEAR, SIG_REF, "dlv", CORPS_REF)
    assert runner_hook.verifier_signature_du_corps(
        secret, LINEAR, r, json.loads(CORPS_REF),
        maintenant=MAINTENANT_REF) == "invalid"


@pytest.mark.parametrize("ecart_s", [61, -61, 3600])
def test_un_horodatage_hors_de_la_MINUTE_rend_STALE_dans_les_deux_sens(ecart_s):
    r = runner_hook.SignatureDuCorps(runner_hook.LINEAR, SIG_REF, "dlv", CORPS_REF)
    assert runner_hook.verifier_signature_du_corps(
        SECRET_REF, LINEAR, r, json.loads(CORPS_REF),
        maintenant=MAINTENANT_REF + ecart_s) == "stale"
    assert runner_hook.verifier_signature_du_corps(
        SECRET_REF, LINEAR, r, json.loads(CORPS_REF),
        maintenant=MAINTENANT_REF + (59 if ecart_s > 0 else -59)) == "ok"


def test_un_corps_signe_SANS_horodatage_est_STALE():
    """Rien ne dirait sa fraîcheur : il se rejouerait indéfiniment."""
    corps, brut = _corps(ts_ms=False)
    assert runner_hook.verifier_signature_du_corps(
        SECRET, LINEAR, _recue(brut), corps) == "stale"


@pytest.mark.parametrize("ts", [float("nan"), float("inf"), float("-inf"),
                                {"ms": 1}, "demain"])
def test_un_horodatage_NON_FINI_ou_qui_n_est_pas_un_nombre_est_STALE(ts):
    """`abs(maintenant - nan) > tolérance` est faux : NaN passerait la fenêtre."""
    corps, brut = _corps(ts_ms=ts)
    assert runner_hook.verifier_signature_du_corps(
        SECRET, LINEAR, _recue(brut), corps) == "stale"


def test_STALE_n_est_rendu_que_pour_une_signature_VALIDE():
    corps, brut = _corps(ts_ms=1)
    assert runner_hook.verifier_signature_du_corps(
        SECRET, LINEAR, _recue(brut, secret="intrus"), corps) == "invalid"


# ── 2. la ROUTE relève la signature de chaque préréglage hmac_body ───────────

def test_les_entetes_Linear_sont_releves_avec_l_identifiant_de_livraison():
    from starlette.datastructures import Headers
    h = Headers({"Linear-Signature": " abc ", "Linear-Delivery": "dlv_9",
                 "Linear-Event": "Issue"})
    trouvees = runner_hook.signatures_du_corps(h, b"{}")
    assert trouvees == {runner_hook.LINEAR: runner_hook.SignatureDuCorps(
        runner_hook.LINEAR, "abc", "dlv_9", b"{}")}


def test_sans_en_tete_de_signature_rien_n_est_releve():
    from starlette.datastructures import Headers
    assert runner_hook.signatures_du_corps(
        Headers({"Linear-Delivery": "dlv_9", "webhook-id": "x"}), b"{}") == {}


# ── 3-5. le DÉCLENCHEMENT ─────────────────────────────────────────────────────

class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        raise AssertionError(f"SQL direct inattendu : {sql!r}")


@pytest.fixture
def linear(monkeypatch):
    """Un agent `linear` ; `trigger_signe` rejoue la garde SQL (id ET préréglage)."""
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    vu = {"livraisons": [], "deja": None, "signe": []}
    t = {"id": 5, "org_id": ORG, "sub": "alexis", "procedure": "usine-a-connecteurs",
         "tools": ["a"], "input": "traite le ticket", "enabled": True,
         "kind": "webhook", "payload_mode": "inline", "payload_fields": None,
         "max_per_hour": None, "fraicheur_s": None, "model": "claude-sonnet-5",
         "project_id": None, "max_steps": None, "label": None,
         "hook_auth": runner_hook.LINEAR, "hook_slug": None,
         "hook_signing_secret_enc": runner_hook.chiffrer_secret_de_signature(5, SECRET)}
    vu["trigger"] = t

    def _signe(i, hook_auth="standard_webhooks"):
        vu["signe"].append((i, hook_auth))
        return dict(t) if i == 5 and t["hook_auth"] == hook_auth else None

    monkeypatch.setattr(runner_hook.db, "trigger_signe", _signe)
    monkeypatch.setattr(runner_hook.db, "trigger_par_secret",
                        lambda i, h, hook_auth="bearer": None)
    monkeypatch.setattr(runner_hook.db, "trigger_sans_preuve", lambda i: None)
    monkeypatch.setattr(runner_hook.db, "_connect", lambda: _Conn())
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


def _declencher(tid, corps, brut, recue=None, **kw):
    recue = recue or _recue(brut)
    return runner_hook.declencher(tid, kw.pop("secret", None), corps, "Linear-Webhook",
                                  signatures_corps={runner_hook.LINEAR: recue}, **kw)


def test_une_signature_valide_ENFILE_garde_la_livraison_et_rend_200(linear):
    corps, brut = _corps()
    out = _declencher(5, corps, brut)
    assert out["job_id"] == 900 and not out.get("duplicate")
    assert out.statut_http == 200
    assert linear["livraisons"] == [(runner_hook.db.QUEUED, 900, "dlv_1")]
    assert "com_1" in linear["enfile"]["payload"]["input"]
    assert linear["signe"] == [(5, runner_hook.LINEAR)], "la garde SQL nomme le préréglage"


def test_une_RETENTATIVE_du_meme_Linear_Delivery_ne_refait_pas_de_deroule(linear):
    linear["deja"] = {"id": 1, "job_id": 900, "external_id": "dlv_1"}
    corps, brut = _corps()
    out = _declencher(5, corps, brut)
    assert out["duplicate"] is True and out.statut_http == 200
    assert "enfile" not in linear and linear["livraisons"] == []


def test_un_REJEU_hors_fenetre_est_nomme_et_journalise(linear):
    import time
    corps, brut = _corps(ts_ms=int((time.time() - 120) * 1000))
    with pytest.raises(runner_hook.HookRefus) as e:
        _declencher(5, corps, brut)
    assert (e.value.statut, e.value.code) == (400, "hook_stale_timestamp")
    assert "webhookTimestamp" in e.value.message and "60 s" in e.value.message
    assert linear["livraisons"] == [(runner_hook.db.REFUSE_STALE, None, "dlv_1")]
    assert "enfile" not in linear


def test_signature_fausse_et_id_inconnu_rendent_LE_MEME_refus_et_ne_s_ecrivent_pas(
        linear):
    corps, brut = _corps()
    refus = []
    for tid, recue in ((5, _recue(brut, secret="intrus")), (6, _recue(brut))):
        with pytest.raises(runner_hook.HookRefus) as e:
            _declencher(tid, corps, brut, recue)
        refus.append((e.value.statut, e.value.code, e.value.message))
    assert refus[0] == refus[1] == (404, "hook_not_found", runner_hook.HOOK_INCONNU)
    assert linear["livraisons"] == []


def test_un_id_inconnu_coute_LE_MEME_calcul_qu_une_vraie_verification(linear,
                                                                      monkeypatch):
    appels = []
    vraie = runner_hook.verifier_signature_du_corps
    monkeypatch.setattr(runner_hook, "verifier_signature_du_corps",
                        lambda *a, **k: appels.append(a[0]) or vraie(*a, **k))
    corps, brut = _corps()
    with pytest.raises(runner_hook.HookRefus):
        _declencher(6, corps, brut)
    assert appels == [runner_hook._SECRET_LEURRE]


def test_le_PORTEUR_n_ouvre_pas_un_agent_linear(linear):
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, "otoh_un_porteur", {"a": 1}, "curl")
    assert e.value.statut == 404


def test_des_entetes_STANDARD_WEBHOOKS_n_ouvrent_pas_un_agent_linear(linear):
    corps, brut = _corps()
    sw = runner_hook.SignatureRecue("msg_1", "1", "v1,abc", brut)
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, None, corps, "x", signature=sw)
    assert e.value.statut == 404 and linear["livraisons"] == []


def test_une_signature_Linear_n_ouvre_pas_un_agent_STANDARD_WEBHOOKS(linear):
    """La garde SQL filtre le préréglage : le secret d'un agent `standard_webhooks`
    n'est même pas lu pour une signature Linear."""
    linear["trigger"]["hook_auth"] = runner_hook.STANDARD_WEBHOOKS
    corps, brut = _corps()
    with pytest.raises(runner_hook.HookRefus) as e:
        _declencher(5, corps, brut)
    assert e.value.statut == 404 and linear["livraisons"] == []


def test_un_agent_linear_en_PAUSE_repond_409_et_journalise(linear):
    linear["trigger"]["enabled"] = False
    corps, brut = _corps()
    with pytest.raises(runner_hook.HookRefus) as e:
        _declencher(5, corps, brut)
    assert e.value.statut == 409
    assert linear["livraisons"] == [(runner_hook.db.REFUSE_PAUSED, None, "dlv_1")]


def test_un_agent_avec_adresse_privee_n_ouvre_pas_par_son_id(linear):
    linear["trigger"]["hook_slug"] = "h_privee"
    corps, brut = _corps()
    with pytest.raises(runner_hook.HookRefus) as e:
        _declencher(5, corps, brut)
    assert e.value.statut == 404
    assert _declencher(5, corps, brut, par_adresse_privee=True)["job_id"] == 900


def test_les_AUTRES_preregages_gardent_202(linear):
    linear["trigger"]["hook_auth"] = runner_hook.STANDARD_WEBHOOKS
    import base64
    import time
    whsec = "whsec_" + base64.b64encode(b"cle de test").decode()
    linear["trigger"]["hook_signing_secret_enc"] = \
        runner_hook.chiffrer_secret_de_signature(5, whsec)
    ts = str(int(time.time()))
    brut = b'{"a": 1}'
    mac = hmac.new(b"cle de test", f"m1.{ts}.".encode() + brut, hashlib.sha256).digest()
    sw = runner_hook.SignatureRecue("m1", ts, "v1," + base64.b64encode(mac).decode(),
                                    brut)
    out = runner_hook.declencher(5, None, {"a": 1}, "x", signature=sw)
    assert out["job_id"] == 900 and out.statut_http == 202


def test_la_ROUTE_rend_200_a_Linear_sur_les_octets_recus(linear):
    """Bout en bout : en-têtes Linear, corps brut signé, statut 200."""
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from oto_mcp.api import hooks

    corps, _ = _corps()
    brut = json.dumps(corps, separators=(",", ":")).encode()   # la forme envoyée
    app = Starlette(routes=hooks.make_routes(lambda r: None))
    r = TestClient(app).post("/api/hooks/5", content=brut, headers={
        "Linear-Signature": _signer(SECRET, brut), "Linear-Delivery": "dlv_7",
        "Linear-Event": "Comment", "User-Agent": "Linear-Webhook",
        "content-type": "application/json; charset=utf-8"})
    assert r.status_code == 200, r.text
    assert r.json()["job_id"] == 900
    assert linear["livraisons"] == [(runner_hook.db.QUEUED, 900, "dlv_7")]

    # Le même corps re-sérialisé par un intermédiaire : refusé comme un inconnu.
    r = TestClient(app).post("/api/hooks/5", content=json.dumps(corps).encode(),
                             headers={"Linear-Signature": _signer(SECRET, brut),
                                      "Linear-Delivery": "dlv_8"})
    assert r.status_code == 404


# ── 6. la POSE ────────────────────────────────────────────────────────────────

@pytest.fixture
def pose(monkeypatch):
    ligne = {"id": 5, "org_id": ORG, "sub": "alexis", "kind": "webhook",
             "procedure": "veille", "tools": ["a"], "enabled": True,
             "payload_mode": "ignore", "payload_fields": None, "model": "claude-sonnet-5",
             "hook_auth": "bearer", "signing_secret_set": False, "hook_sources": None}
    ecrit = []
    monkeypatch.setattr(RT.db, "get_trigger", lambda t, o: dict(ligne))
    monkeypatch.setattr(RT.db, "poser_auth_de_hook",
                        lambda *a, **k: ecrit.append(a) or True)
    monkeypatch.setattr(RT.db, "poser_secret_de_hook",
                        lambda *a, **k: ecrit.append(a) or True)
    monkeypatch.setattr(RT.db, "comptage_livraisons",
                        lambda t, o: {"recues_24h": 0, "refusees_24h": 0,
                                      "derniere": None})
    monkeypatch.setattr(RT.db, "file_du_declencheur",
                        lambda t, o: {"pending": 0, "held": 0})
    monkeypatch.setattr(RT.db, "derniers_travaux", lambda org, ids: {int(i): {} for i in ids})
    monkeypatch.setattr(RT.db, "comptage_perime",
                        lambda o, t: {"expired_count": 0, "expired_since": None,
                                      "expired_last": None})
    return {"ligne": ligne, "ecrit": ecrit}


def _auth(**kw):
    return asyncio.run(RT._hook_auth(ResolvedCtx(sub="alexis", org_id=ORG),
                                     RT.HookAuthInput(trigger_id=5, **kw)))


def test_linear_ne_se_POSE_pas_a_la_main(pose):
    """Accepté, il tomberait dans la branche du porteur : un agent signé sans secret."""
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="linear")
    assert e.value.code == "hook_auth_needs_connect_source"
    assert pose["ecrit"] == []


def test_d_un_agent_linear_DEBRANCHE_vers_standard_webhooks_un_whsec_est_exige(pose):
    """Le secret posé est celui de Linear : il n'a pas la forme `whsec_`."""
    pose["ligne"].update(hook_auth="linear", signing_secret_set=True)
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="standard_webhooks")
    assert e.value.code == "missing_signing_secret" and pose["ecrit"] == []


def test_d_un_agent_linear_DEBRANCHE_on_revient_au_porteur(pose):
    pose["ligne"].update(hook_auth="linear", signing_secret_set=True)
    out = _auth(hook_auth="bearer")
    assert out["hook_secret"].startswith(runner_hook.HOOK_SECRET_PREFIX)


def test_un_agent_linear_refuse_la_rotation_de_porteur(pose, monkeypatch):
    pose["ligne"].update(hook_auth="linear", signing_secret_set=True)
    monkeypatch.setattr(RT.db, "comptage_livraisons",
                        lambda t, o: {"recues_24h": 0, "refusees_24h": 0,
                                      "derniere": None})
    with pytest.raises(AuthzDenied) as e:
        asyncio.run(RT._triggers(ResolvedCtx(sub="alexis", org_id=ORG),
                                 RT.TriggerInput(op="rotate_secret", trigger_id=5)))
    assert e.value.code == "signature_mode"
