"""Deux protections OPTIONNELLES d'un agent déclenché, posées par son utilisateur.

1. **Le plafond journalier** (`max_per_day`) — la borne de dépense d'un credential
   fuité. Le lissage (`max_per_hour`) RETARDE et la file n'a pas de fond : sans
   plafond, un porteur ou un secret de signature qui fuit empile des déroulés
   payants sans fin. Au-delà du plafond : 429, aucun travail.
2. **L'adresse privée** (`private_address`) — `h_` + 128 bits à la place de l'id
   numérique, qui se parcourt (`/api/hooks/1`, `/2`…). Posée, l'id numérique
   cesse d'ouvrir. Ce n'est PAS un credential : la preuve reste exigée derrière.

Les deux sont ABSENTS par défaut : un agent existant ne change pas de comportement.
"""
from __future__ import annotations

import asyncio

import pytest

from oto_mcp import runner_hook
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 77


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        raise AssertionError(f"SQL direct inattendu : {sql!r}")


@pytest.fixture
def porte(monkeypatch):
    """Un agent au porteur ; la base doublée pour ce que `declencher` écrit."""
    vu = {"livraisons": [], "acceptees": 0, "sortie": 3600}
    t = {"id": 5, "org_id": ORG, "sub": "alexis", "procedure": "veille",
         "tools": ["a"], "input": "go", "enabled": True, "kind": "webhook",
         "payload_mode": "ignore", "payload_fields": None, "max_per_hour": None,
         "fraicheur_s": None, "model": None, "project_id": None, "max_steps": None,
         "label": None, "hook_auth": "bearer", "max_per_day": None, "hook_slug": None}
    vu["trigger"] = t
    monkeypatch.setattr(runner_hook.db, "trigger_par_secret", lambda i, h: dict(t))
    monkeypatch.setattr(runner_hook.db, "_connect", lambda: _Conn())
    monkeypatch.setattr(runner_hook.db, "verrouiller_le_declencheur", lambda c, i: None)
    monkeypatch.setattr(runner_hook.db, "retard_de_lissage", lambda c, t_, d, f: 0)
    monkeypatch.setattr(runner_hook.db, "acceptees_sur_24h",
                        lambda c, i: (vu["acceptees"], vu["sortie"]))
    monkeypatch.setattr(
        runner_hook.db, "enregistrer",
        lambda c, t_, o, outcome, job_id=None, source=None, due_at=None,
        external_id=None: vu["livraisons"].append(outcome) or 1)
    monkeypatch.setattr(runner_hook.db, "enqueue_job",
                        lambda org, kind, **kw: vu.update(enfile=kw) or {"id": 900})
    return vu


# ── 1. le PLAFOND ─────────────────────────────────────────────────────────────

def test_sans_plafond_on_ne_compte_meme_pas(porte, monkeypatch):
    """Le défaut ne coûte rien : aucune lecture de plus sur le chemin chaud."""
    monkeypatch.setattr(runner_hook.db, "acceptees_sur_24h",
                        lambda c, i: pytest.fail("compté sans plafond déclaré"))
    assert runner_hook.declencher(5, "otoh_x", {}, "src")["job_id"] == 900


def test_sous_le_plafond_le_travail_part(porte):
    porte["trigger"]["max_per_day"] = 3
    porte["acceptees"] = 2
    assert runner_hook.declencher(5, "otoh_x", {}, "src")["job_id"] == 900
    assert porte["livraisons"] == [runner_hook.db.QUEUED]


def test_au_plafond_on_REFUSE_sans_travail_et_on_le_journalise(porte):
    """⚠️ LE banc : au-delà, un REFUS — pas un retard. Retarder, c'est payer quand
    même, plus tard ; la file n'a pas de fond."""
    porte["trigger"]["max_per_day"] = 3
    porte["acceptees"] = 3
    porte["sortie"] = 1800
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, "otoh_x", {}, "src")
    assert (e.value.statut, e.value.code) == (429, "hook_daily_cap")
    assert e.value.retry_after == 1800, "la source sait QUAND une place se libère"
    assert "enfile" not in porte
    assert porte["livraisons"] == [runner_hook.db.REFUSE_DAILY_CAP]


def test_le_retry_after_n_est_jamais_nul(porte):
    porte["trigger"]["max_per_day"] = 1
    porte["acceptees"] = 1
    porte["sortie"] = 0
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, "otoh_x", {}, "src")
    assert e.value.retry_after >= 60


# ── 2. l'ADRESSE privée ───────────────────────────────────────────────────────

def test_une_adresse_privee_a_128_bits_et_son_prefixe():
    a, b = runner_hook.nouvelle_adresse(), runner_hook.nouvelle_adresse()
    assert a.startswith("h_") and len(a) == 24 and a != b


def test_la_resolution_distingue_id_et_adresse(monkeypatch):
    monkeypatch.setattr(runner_hook.db, "trigger_id_par_adresse",
                        lambda s: 5 if s == "h_bonne" else None)
    assert runner_hook.resoudre_adresse("71") == (71, False)
    assert runner_hook.resoudre_adresse("h_bonne") == (5, True)
    assert runner_hook.resoudre_adresse("h_inconnue") == (None, True)
    assert runner_hook.resoudre_adresse("h_" + "x" * 100) == (None, True)
    assert runner_hook.resoudre_adresse("pas-un-id") == (None, False)


def test_un_agent_a_adresse_privee_n_ouvre_PLUS_par_son_id(porte):
    porte["trigger"]["hook_slug"] = "h_abc"
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, "otoh_x", {}, "src")
    assert e.value.statut == 404 and e.value.message == runner_hook.HOOK_INCONNU
    assert porte["livraisons"] == [], "rien ne dit qu'une adresse privée existe"


def test_par_son_adresse_privee_il_ouvre(porte):
    porte["trigger"]["hook_slug"] = "h_abc"
    out = runner_hook.declencher(5, "otoh_x", {}, "src", par_adresse_privee=True)
    assert out["job_id"] == 900


def test_l_adresse_privee_n_est_PAS_un_credential(porte, monkeypatch):
    """La bonne adresse sans la preuve n'ouvre rien."""
    porte["trigger"]["hook_slug"] = "h_abc"
    monkeypatch.setattr(runner_hook.db, "trigger_par_secret", lambda i, h: None)
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, "otoh_faux", {}, "src", par_adresse_privee=True)
    assert e.value.statut == 404


# ── 3. la POSE, par l'outil ───────────────────────────────────────────────────

def _ctx():
    return ResolvedCtx(sub="alexis", org_id=ORG)


def _appel(**kw):
    if kw.get("op") == "create":
        kw.setdefault("model", "claude-sonnet-5")
    return asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(**kw)))


@pytest.fixture
def base(monkeypatch):
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
    ligne = {"id": 5, "org_id": ORG, "sub": "alexis", "kind": "webhook",
             "procedure": "veille", "tools": ["a"], "enabled": True,
             "payload_mode": "ignore", "payload_fields": None,
             "model": "claude-sonnet-5", "hook_auth": "bearer",
             "max_per_day": None, "hook_slug": None}
    etat = {"ligne": ligne, "ecrit": []}

    def _create(org, sub, **kw):
        etat["cree"] = kw
        ligne.update(kw)
        return dict(ligne)

    def _update(t, o, champs, hors_abonnement_d_autrui=None):
        etat["ecrit"].append(dict(champs))
        ligne.update(champs)
        return dict(ligne)

    monkeypatch.setattr(RT.db, "create_trigger", _create)
    monkeypatch.setattr(RT.db, "get_trigger", lambda t, o: dict(ligne))
    monkeypatch.setattr(RT.db, "update_trigger", _update)
    monkeypatch.setattr(RT.db, "poser_secret_de_hook", lambda t, o, h: True)
    monkeypatch.setattr(RT.db, "poser_adresse_de_hook",
                        lambda t, o, slug: ligne.update(hook_slug=slug) or True)
    return etat


def test_un_webhook_NAIT_avec_une_adresse_privee_et_sans_plafond(base):
    """Décidé le 25/09 : l'adresse aléatoire dès le premier jour, pas après une
    fuite. Le plafond, lui, reste un choix de l'utilisateur."""
    out = _appel(op="create", kind="webhook", procedure="veille", tools=["a"])
    assert base["cree"]["max_per_day"] is None
    assert out["trigger"]["private_address"] is True
    assert "/api/hooks/h_" in out["trigger"]["hook_url"]


def test_l_adresse_numerique_ne_se_CHOISIT_plus_a_la_creation(base):
    """Rien ne la justifie : un id se parcourt, et une source stocke une URL
    aléatoire aussi bien. Refusée par son nom, pas ignorée."""
    with pytest.raises(AuthzDenied) as e:
        _appel(op="create", kind="webhook", procedure="veille", tools=["a"],
               private_address=False)
    assert e.value.code == "numeric_address_retired"


def test_un_agent_PROGRAMME_ne_recoit_pas_d_adresse(base, monkeypatch):
    monkeypatch.setattr(RT.db, "poser_adresse_de_hook",
                        lambda *a: pytest.fail("adresse posée sur un agent programmé"))
    _appel(op="create", procedure="veille", tools=["a"], cron="0 9 * * *")


def test_creer_avec_plafond_et_adresse_privee(base):
    out = _appel(op="create", kind="webhook", procedure="veille", tools=["a"],
                 max_per_day=200, private_address=True)
    assert base["cree"]["max_per_day"] == 200
    assert out["trigger"]["private_address"] is True
    assert "/api/hooks/h_" in out["trigger"]["hook_url"]
    assert not out["trigger"]["hook_url"].endswith("/5")


def test_plafond_ZERO_le_retire_et_stocke_null(base):
    _appel(op="update", trigger_id=5, max_per_day=50)
    _appel(op="update", trigger_id=5, max_per_day=0)
    assert base["ecrit"][-1] == {"max_per_day": None}


def test_un_plafond_negatif_est_refuse(base):
    with pytest.raises(AuthzDenied) as e:
        _appel(op="update", trigger_id=5, max_per_day=-1)
    assert e.value.code == "invalid_bound"


def test_les_deux_reglages_sur_un_agent_PROGRAMME_sont_refuses(base):
    base["ligne"]["kind"] = "schedule"
    for kw in ({"max_per_day": 10}, {"private_address": True}):
        with pytest.raises(AuthzDenied) as e:
            _appel(op="update", trigger_id=5, **kw)
        assert e.value.code == "not_a_webhook"


def test_redemander_une_adresse_privee_ne_la_CHANGE_pas(base):
    """Remplacer l'adresse casse la source : c'est `rotate_address`, pas un
    `true` répété par un écran qui resynchronise."""
    _appel(op="update", trigger_id=5, private_address=True)
    premiere = base["ligne"]["hook_slug"]
    _appel(op="update", trigger_id=5, private_address=True)
    assert base["ligne"]["hook_slug"] == premiere


def test_rotate_address_la_REMPLACE(base):
    _appel(op="update", trigger_id=5, private_address=True)
    premiere = base["ligne"]["hook_slug"]
    out = _appel(op="rotate_address", trigger_id=5)
    assert base["ligne"]["hook_slug"] not in (None, premiere)
    assert base["ligne"]["hook_slug"] in out["trigger"]["hook_url"]


def test_un_ANCIEN_agent_passe_en_privee_SANS_retour(base):
    """Sens unique : un agent d'avant le 25/09 passe à l'adresse privée ; en
    revenir est refusé — ce serait défaire la protection."""
    assert base["ligne"]["hook_slug"] is None
    out = _appel(op="update", trigger_id=5, private_address=True)
    assert out["trigger"]["private_address"] is True
    with pytest.raises(AuthzDenied) as e:
        _appel(op="update", trigger_id=5, private_address=False)
    assert e.value.code == "numeric_address_retired"
    assert base["ligne"]["hook_slug"], "l'adresse privée est restée"
