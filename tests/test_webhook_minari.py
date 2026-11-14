"""Un webhook signé par MINARI — le schéma `hmac_body`, préréglage `minari`.

Minari signe le corps BRUT (`X-Webhook-Signature` = HMAC-SHA256 hexadécimal, clé = le
secret qu'il affiche), sans horodatage ni identifiant de livraison en en-tête, et n'a
pas d'API pour inscrire un webhook : son secret se COLLE.
https://api.minari.ai/docs/openapi.json (« Webhooks »)

Ce fichier tient :

1. **La vérification est celle de Minari**, prouvée contre un vecteur calculé HORS de
   notre code (`openssl dgst -sha256 -hmac`).
2. **Sans horodatage, une signature valide passe** (rien à juger), et **un même
   `call.call_id` ne refait pas de déroulé** : l'identifiant vient du corps SIGNÉ.
3. **Chacun sa porte** : une signature Minari n'ouvre ni un agent `linear` ni un agent
   au porteur ; même 404 partout. Minari reçoit 202.
4. **La pose** : `minari` se pose à la main AVEC son secret ; sans secret, refusé —
   y compris depuis un agent `linear` débranché, dont le secret n'est pas celui de
   Minari ; le porteur collé par erreur est refusé.
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

pytestmark = pytest.mark.usefixtures("agents_sans_partages")

ORG = 77
MINARI = runner_hook.PREREGLAGES[runner_hook.MINARI]

#: Le vecteur de référence, calculé HORS de notre code :
#:   printf '%s' '<CORPS_REF>' > corps.json
#:   openssl dgst -sha256 -hmac 'minari_test_secret_0123456789' corps.json
SECRET_REF = "minari_test_secret_0123456789"
CORPS_REF = (b'{"event":"call.completed","call":{"call_id":'
             b'"00000000-0000-4000-8000-000000000001","status":"connected",'
             b'"duration_seconds":42,"summary":null},"prospect":null,"user":null,'
             b'"list":null}')
SIG_REF = "054a58843cf5acca5a7f960e9e1a00ee35c92cb3672bf0e8677172d2e60870ec"

SECRET = "un-secret-de-signature-minari-de-test"


def _signer(secret: str, brut: bytes) -> str:
    return hmac.new(secret.encode(), brut, hashlib.sha256).hexdigest()


def _corps(call_id: str = "call-1", event: str = "call.completed") -> tuple[dict, bytes]:
    corps = {"event": event, "call": {"call_id": call_id, "status": "connected"}}
    return corps, json.dumps(corps, separators=(",", ":")).encode()


def _recue(brut: bytes, secret: str = SECRET,
           signature: str | None = None) -> runner_hook.SignatureDuCorps:
    return runner_hook.SignatureDuCorps(runner_hook.MINARI,
                                        signature or _signer(secret, brut), None, brut)


# ── 1. la VÉRIFICATION ────────────────────────────────────────────────────────

def test_le_vecteur_calcule_par_OPENSSL_passe():
    r = runner_hook.SignatureDuCorps(runner_hook.MINARI, SIG_REF, None, CORPS_REF)
    assert runner_hook.verifier_signature_du_corps(
        SECRET_REF, MINARI, r, json.loads(CORPS_REF)) == "ok"


def test_un_corps_RE_SERIALISE_ne_passe_pas():
    corps = json.loads(CORPS_REF)
    brut = json.dumps(corps).encode()
    assert brut != CORPS_REF
    r = runner_hook.SignatureDuCorps(runner_hook.MINARI, SIG_REF, None, brut)
    assert runner_hook.verifier_signature_du_corps(
        SECRET_REF, MINARI, r, corps) == "invalid"


@pytest.mark.parametrize("signature", [SIG_REF[:-2] + "00", "sha256=" + SIG_REF, ""])
def test_toute_alteration_de_l_en_tete_rend_INVALID(signature):
    r = runner_hook.SignatureDuCorps(runner_hook.MINARI, signature, None, CORPS_REF)
    assert runner_hook.verifier_signature_du_corps(
        SECRET_REF, MINARI, r, json.loads(CORPS_REF)) == "invalid"


def test_SANS_horodatage_une_signature_valide_passe_a_toute_heure():
    """Minari n'en envoie pas : le préréglage n'en déclare pas, rien n'est jugé."""
    assert MINARI.chemin_horodatage is None
    r = runner_hook.SignatureDuCorps(runner_hook.MINARI, SIG_REF, None, CORPS_REF)
    assert runner_hook.verifier_signature_du_corps(
        SECRET_REF, MINARI, r, json.loads(CORPS_REF), maintenant=0.0) == "ok"


def test_l_en_tete_Minari_est_releve_sans_identifiant():
    from starlette.datastructures import Headers
    trouvees = runner_hook.signatures_du_corps(
        Headers({"X-Webhook-Signature": " abc "}), b"{}")
    assert trouvees == {runner_hook.MINARI: runner_hook.SignatureDuCorps(
        runner_hook.MINARI, "abc", None, b"{}")}


def test_minari_est_un_secret_COLLE_linear_non():
    assert MINARI.secret_colle is True
    assert runner_hook.PREREGLAGES[runner_hook.LINEAR].secret_colle is False


# ── 2-3. le DÉCLENCHEMENT ─────────────────────────────────────────────────────

class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        raise AssertionError(f"SQL direct inattendu : {sql!r}")


@pytest.fixture
def minari(monkeypatch):
    """Un agent `minari` ; `trigger_signe` rejoue la garde SQL (id ET préréglage)."""
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    vu = {"livraisons": [], "deja": None, "signe": []}
    t = {"id": 5, "org_id": ORG, "sub": "alexis", "procedure": "apres-appel",
         "tools": ["a"], "input": "traite l'appel", "enabled": True,
         "kind": "webhook", "payload_mode": "inline", "payload_fields": None,
         "max_per_hour": None, "fraicheur_s": None, "model": "claude-sonnet-5",
         "project_id": None, "max_steps": None, "label": None,
         "hook_auth": runner_hook.MINARI, "hook_slug": None,
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
    return runner_hook.declencher(tid, kw.pop("secret", None), corps, "Minari",
                                  signatures_corps={runner_hook.MINARI: recue}, **kw)


def test_une_signature_valide_ENFILE_retient_le_call_id_et_rend_202(minari):
    corps, brut = _corps()
    out = _declencher(5, corps, brut)
    assert out["job_id"] == 900 and not out.get("duplicate")
    assert out.statut_http == 202
    assert minari["livraisons"] == [(runner_hook.db.QUEUED, 900, "call-1")]
    assert minari["signe"] == [(5, runner_hook.MINARI)], "la garde SQL nomme le préréglage"


def test_un_REJEU_du_meme_call_id_ne_refait_pas_de_deroule(minari):
    minari["deja"] = {"id": 1, "job_id": 900, "external_id": "call-1"}
    corps, brut = _corps()
    out = _declencher(5, corps, brut)
    assert out["duplicate"] is True
    assert "enfile" not in minari and minari["livraisons"] == []


def test_un_evenement_SANS_call_id_au_chemin_passe_sans_identifiant(minari):
    """`call.meeting_booked` porte `call_id` à la racine : pas d'identifiant retenu
    (rejouable, borné par `max_per_day`) — jamais l'identifiant d'un autre appel."""
    corps = {"event": "call.meeting_booked", "call_id": "call-1", "meeting_booked": True}
    brut = json.dumps(corps, separators=(",", ":")).encode()
    out = _declencher(5, corps, brut)
    assert out["job_id"] == 900
    assert minari["livraisons"] == [(runner_hook.db.QUEUED, 900, None)]


def test_signature_fausse_et_id_inconnu_rendent_LE_MEME_refus_et_ne_s_ecrivent_pas(
        minari):
    corps, brut = _corps()
    refus = []
    for tid, recue in ((5, _recue(brut, secret="intrus")), (6, _recue(brut))):
        with pytest.raises(runner_hook.HookRefus) as e:
            _declencher(tid, corps, brut, recue)
        refus.append((e.value.statut, e.value.code, e.value.message))
    assert refus[0] == refus[1] == (404, "hook_not_found", runner_hook.HOOK_INCONNU)
    assert minari["livraisons"] == []


def test_une_signature_Minari_n_ouvre_pas_un_agent_LINEAR(minari):
    minari["trigger"]["hook_auth"] = runner_hook.LINEAR
    corps, brut = _corps()
    with pytest.raises(runner_hook.HookRefus) as e:
        _declencher(5, corps, brut)
    assert e.value.statut == 404 and minari["livraisons"] == []


def test_le_PORTEUR_n_ouvre_pas_un_agent_minari(minari):
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, "otoh_un_porteur", {"a": 1}, "curl")
    assert e.value.statut == 404


def test_la_ROUTE_accepte_les_octets_recus_et_rend_202(minari):
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    from oto_mcp.api import hooks

    corps, brut = _corps(call_id="call-7")
    app = Starlette(routes=hooks.make_routes(lambda r: None))
    r = TestClient(app).post("/api/hooks/5", content=brut, headers={
        "X-Webhook-Signature": _signer(SECRET, brut),
        "content-type": "application/json"})
    assert r.status_code == 202, r.text
    assert r.json()["job_id"] == 900
    assert minari["livraisons"] == [(runner_hook.db.QUEUED, 900, "call-7")]

    r = TestClient(app).post("/api/hooks/5", content=json.dumps(corps).encode(),
                             headers={"X-Webhook-Signature": _signer(SECRET, brut)})
    assert r.status_code == 404


# ── 4. la POSE ────────────────────────────────────────────────────────────────

@pytest.fixture
def pose(monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    ligne = {"id": 5, "org_id": ORG, "sub": "alexis", "kind": "webhook",
             "procedure": "veille", "tools": ["a"], "enabled": True,
             "payload_mode": "ignore", "payload_fields": None, "model": "claude-sonnet-5",
             "hook_auth": "bearer", "signing_secret_set": False, "hook_sources": None,
             # Exigé pour `minari` (rejouable) : voir test_webhook_prereglages.
             "max_per_day": 20}
    ecrit = []
    monkeypatch.setattr(RT.db, "get_trigger", lambda t, o: dict(ligne))
    monkeypatch.setattr(RT.db, "poser_auth_de_hook",
                        lambda *a, **k: ecrit.append(("auth", a, k)) or True)
    monkeypatch.setattr(RT.db, "poser_secret_de_hook",
                        lambda *a, **k: ecrit.append(("porteur", a, k)) or True)
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


def test_minari_se_POSE_avec_son_secret_chiffre_et_le_porteur_s_efface(pose):
    out = _auth(hook_auth="minari", signing_secret=SECRET)
    assert out["hook_secret"] is None
    auth = [e for e in pose["ecrit"] if e[0] == "auth"]
    assert len(auth) == 1 and auth[0][1][2] == "minari"
    enveloppe = auth[0][2]["secret_enc"]
    assert enveloppe and SECRET not in enveloppe
    assert runner_hook._dechiffrer_secret_de_signature(5, enveloppe) == SECRET
    assert ("porteur", (5, ORG, None), {}) in pose["ecrit"]


def test_un_secret_whsec_est_pris_TEL_QUEL(pose):
    """Ses octets UTF-8 sont la clé HMAC, comme le reste : aucune forme n'est exigée."""
    _auth(hook_auth="minari", signing_secret="whsec_abc")
    assert any(e[0] == "auth" for e in pose["ecrit"])


def test_minari_SANS_secret_est_refuse(pose):
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="minari")
    assert e.value.code == "missing_signing_secret" and pose["ecrit"] == []


def test_d_un_agent_linear_DEBRANCHE_vers_minari_un_secret_neuf_est_exige(pose):
    """Même schéma `hmac_body`, mais le secret posé est celui de Linear."""
    pose["ligne"].update(hook_auth="linear", signing_secret_set=True)
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="minari")
    assert e.value.code == "missing_signing_secret" and pose["ecrit"] == []


def test_un_agent_DEJA_minari_garde_son_secret_sans_en_recoller(pose):
    pose["ligne"].update(hook_auth="minari", signing_secret_set=True)
    _auth(hook_auth="minari")
    auth = [e for e in pose["ecrit"] if e[0] == "auth"]
    assert auth and auth[0][2]["secret_enc"] is None
    assert not any(e[0] == "porteur" for e in pose["ecrit"])


@pytest.mark.parametrize("secret", ["", " avec-espaces ", "deux\nlignes",
                                    "otoh_notre_porteur", "x" * 513])
def test_un_secret_colle_INVRAISEMBLABLE_est_refuse(pose, secret):
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="minari", signing_secret=secret)
    assert e.value.code == "invalid_signing_secret" and pose["ecrit"] == []


def test_standard_webhooks_exige_toujours_un_whsec(pose):
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="standard_webhooks", signing_secret=SECRET)
    assert e.value.code == "invalid_signing_secret"


def test_linear_ne_se_pose_TOUJOURS_pas_a_la_main(pose):
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="linear", signing_secret=SECRET)
    assert e.value.code == "hook_auth_needs_connect_source"


def test_d_un_agent_minari_on_revient_au_porteur(pose):
    pose["ligne"].update(hook_auth="minari", signing_secret_set=True)
    out = _auth(hook_auth="bearer")
    assert out["hook_secret"].startswith(runner_hook.HOOK_SECRET_PREFIX)
