"""La PORTE d'un webhook (revue de #1083, 26/09/2026) — deux défauts corrigés.

1. **Des en-têtes `webhook-*` ne décident pas du mode.** Un agent au porteur dont la
   source signe déjà pour son propre compte (Svix et d'autres posent ces en-têtes)
   était jugé « signé » et tombait en 404 dès le déploiement du mode signature. C'est
   l'AGENT qui porte le mode : au porteur, son porteur décide.
2. **Changer la porte est réservé au propriétaire ou à un admin d'org.** Qui tient la
   porte déclenche l'agent, et l'agent tourne SOUS son propriétaire : un collègue ne
   doit pas pouvoir se fabriquer un porteur (ni poser son `whsec_`) sur l'agent d'un
   autre.
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
PROPRIETAIRE = "alexis"
SECRET_SOURCE = "whsec_" + base64.b64encode(b"le secret de la source").decode()
PORTEUR = "otoh_le_bon_porteur"


def _signature(corps: bytes, msg_id: str = "msg_1") -> runner_hook.SignatureRecue:
    ts = str(int(time.time()))
    cle = base64.b64decode(SECRET_SOURCE[len("whsec_"):])
    mac = hmac.new(cle, f"{msg_id}.{ts}.".encode() + corps, hashlib.sha256).digest()
    return runner_hook.SignatureRecue(msg_id, ts, "v1," + base64.b64encode(mac).decode(),
                                      corps)


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        raise AssertionError(f"SQL direct inattendu : {sql!r}")


# ── 1. la ROUTE : l'agent porte le mode ───────────────────────────────────────

@pytest.fixture
def au_porteur(monkeypatch):
    """Un agent AU PORTEUR : le mode signature ne le trouve pas (garde SQL)."""
    vu = {"livraisons": []}
    t = {"id": 5, "org_id": ORG, "sub": PROPRIETAIRE, "procedure": "veille",
         "tools": ["a"], "input": "fais la veille", "enabled": True,
         "kind": "webhook", "payload_mode": "inline", "payload_fields": None,
         "max_per_hour": None, "fraicheur_s": None, "model": "claude-sonnet-5",
         "project_id": None, "max_steps": None, "label": None,
         "hook_auth": "bearer", "hook_slug": None, "max_per_day": None}
    bon = runner_hook.hacher(PORTEUR)
    monkeypatch.setattr(runner_hook.db, "trigger_signe", lambda i: None)
    monkeypatch.setattr(runner_hook.db, "trigger_par_secret",
                        lambda i, h: dict(t) if i == 5 and h == bon else None)
    monkeypatch.setattr(runner_hook.db, "_connect", lambda: _Conn())
    monkeypatch.setattr(runner_hook.db, "verrouiller_le_declencheur", lambda c, i: None)
    monkeypatch.setattr(runner_hook.db, "retard_de_lissage", lambda c, t_, d, f: 0)
    monkeypatch.setattr(runner_hook.db, "livraison_acceptee", lambda c, t_, e: None)
    monkeypatch.setattr(
        runner_hook.db, "enregistrer",
        lambda c, t_, o, outcome, job_id=None, source=None, due_at=None,
        external_id=None: vu["livraisons"].append((outcome, job_id, external_id)) or 1)
    monkeypatch.setattr(runner_hook.db, "enqueue_job",
                        lambda org, kind, **kw: vu.update(enfile=kw) or {"id": 900})
    return vu


def test_un_agent_AU_PORTEUR_dont_la_source_signe_deja_reste_ouvert(au_porteur):
    """⚠️ LE cas de la revue : Svix pose `webhook-*` pour son compte, le client a
    collé notre porteur. Avant le correctif : 404 définitif au déploiement."""
    corps = b'{"note_id": "n1"}'
    out = runner_hook.declencher(5, PORTEUR, {"note_id": "n1"}, "Svix-Webhooks/1.0",
                                 signature=_signature(corps))
    assert out["job_id"] == 900
    # Rien de la signature n'est retenu : elle ne concerne pas cet agent.
    assert au_porteur["livraisons"] == [(runner_hook.db.QUEUED, 900, None)]


def test_des_entetes_webhook_ne_remplacent_PAS_le_porteur(au_porteur):
    """Sans le bon porteur, les en-têtes signés d'une source n'ouvrent pas un agent
    au porteur — et le refus est le 404 commun."""
    for porteur in (None, "otoh_faux"):
        with pytest.raises(runner_hook.HookRefus) as e:
            runner_hook.declencher(5, porteur, {}, "Svix-Webhooks/1.0",
                                   signature=_signature(b"{}"))
        assert (e.value.statut, e.value.message) == (404, runner_hook.HOOK_INCONNU)
    assert au_porteur["livraisons"] == []


def test_un_agent_EN_SIGNATURE_refuse_toujours_le_porteur_seul(monkeypatch, au_porteur):
    """La contrepartie, inchangée : le mode signature éteint le porteur."""
    monkeypatch.setattr(runner_hook.db, "trigger_par_secret", lambda i, h: None)
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, PORTEUR, {}, "curl")
    assert e.value.statut == 404


# ── 2. les GESTES de porte : propriétaire ou admin ─────────────────────────────

@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    ligne = {"id": 5, "org_id": ORG, "sub": PROPRIETAIRE, "kind": "webhook",
             "procedure": "veille", "tools": ["a"], "enabled": True,
             "payload_mode": "ignore", "payload_fields": None,
             "model": "claude-sonnet-5", "hook_auth": "bearer",
             "signing_secret_set": False, "hook_slug": None}
    ecrit = []
    monkeypatch.setattr(RT.db, "get_trigger", lambda t, o: dict(ligne))
    monkeypatch.setattr(RT.db, "poser_secret_de_hook",
                        lambda t, o, h: ecrit.append("secret") or True)
    monkeypatch.setattr(RT.db, "poser_adresse_de_hook",
                        lambda t, o, s: ecrit.append("adresse") or True)
    monkeypatch.setattr(RT.db, "poser_auth_de_hook",
                        lambda *a, **k: ecrit.append("auth") or True)
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda t, o, champs, hors_abonnement_d_autrui=None:
                        ecrit.append("update") or dict(ligne, **champs))
    monkeypatch.setattr(RT.db, "runner_arme",
                        lambda org: {"armed": True, "workers": 1, "last_seen": None,
                                     "families": ["anthropic"]})
    monkeypatch.setattr(RT.db, "comptage_livraisons",
                        lambda t, o: {"recues_24h": 0, "refusees_24h": 0,
                                      "derniere": None})
    monkeypatch.setattr(RT.db, "file_du_declencheur",
                        lambda t, o: {"pending": 0, "held": 0})
    monkeypatch.setattr(RT.db, "comptage_perime",
                        lambda o, t: {"expired_count": 0, "expired_since": None,
                                      "expired_last": None})
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda portee, ident, fournisseur, cle: None)
    return ecrit


def _admin(monkeypatch, admins):
    monkeypatch.setattr(RT.roles, "is_org_admin", lambda sub, org: sub in admins)


_GESTES = {
    "rotate_secret": lambda sub: asyncio.run(RT._triggers(
        ResolvedCtx(sub=sub, org_id=ORG),
        RT.TriggerInput(op="rotate_secret", trigger_id=5))),
    "rotate_address": lambda sub: asyncio.run(RT._triggers(
        ResolvedCtx(sub=sub, org_id=ORG),
        RT.TriggerInput(op="rotate_address", trigger_id=5))),
    "private_address": lambda sub: asyncio.run(RT._triggers(
        ResolvedCtx(sub=sub, org_id=ORG),
        RT.TriggerInput(op="update", trigger_id=5, private_address=True))),
    "hook_auth": lambda sub: asyncio.run(RT._hook_auth(
        ResolvedCtx(sub=sub, org_id=ORG),
        RT.HookAuthInput(trigger_id=5, hook_auth="standard_webhooks",
                         signing_secret=SECRET_SOURCE))),
}


@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_un_COLLEGUE_ne_change_pas_la_porte_de_l_agent_d_un_autre(geste, agent,
                                                                   monkeypatch):
    _admin(monkeypatch, admins=set())
    # Un collègue à qui l'agent est PARTAGÉ en écriture : il le modifie, il ne
    # change pas sa porte. (Sans partage il ne le verrait pas — 404, banc du partage.)
    from oto_mcp.capabilities import _acces_agent
    monkeypatch.setattr(_acces_agent, "niveaux",
                        lambda sub, org, agents: {int(t["id"]): "editor" for t in agents})
    with pytest.raises(AuthzDenied) as e:
        _GESTES[geste]("un_collegue")
    assert (e.value.status, e.value.code) == (403, "trigger_owner_or_admin_required")
    assert agent == [], "un refus n'écrit rien"


@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_le_PROPRIETAIRE_change_sa_porte(geste, agent, monkeypatch):
    _admin(monkeypatch, admins=set())
    _GESTES[geste](PROPRIETAIRE)
    assert agent, "le geste du propriétaire s'écrit"


@pytest.mark.parametrize("geste", sorted(_GESTES))
def test_un_ADMIN_d_org_change_la_porte_d_un_agent_qui_n_est_pas_le_sien(
        geste, agent, monkeypatch):
    """Cohérent avec `take_over` : un admin referme une porte qui fuit sans avoir
    besoin du propriétaire."""
    _admin(monkeypatch, admins={"un_admin"})
    _GESTES[geste]("un_admin")
    assert agent


def test_le_refus_est_DECLARE_sur_les_deux_capacites():
    for cle in ("runner.triggers", "runner.trigger.hook_auth"):
        cap = next(c for c in RT.CAPABILITIES if c.key == cle)
        assert "trigger_owner_or_admin_required" in {e.code for e in cap.errors}
