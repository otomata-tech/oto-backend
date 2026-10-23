"""Les PRÉRÉGLAGES d'un webhook — `body_secret` (Lemlist, cal.com…) et `none`.

Une source prouve qui elle est d'une des quelques manières génériques ; un
fournisseur n'est qu'un préréglage (`runner_hook.PREREGLAGES`). Ce fichier tient
les propriétés des deux schémas ajoutés le 06/10/2026 :

1. **`body_secret`** (ex-`lemlist`, renommé le 20/10/2026) : la source ne sait pas poser d'en-tête, elle renvoie
   notre `otoh_` dans son corps. Il est vérifié, puis RETIRÉ avant que l'agent ne
   voie le corps ; une retentative du même `_id` ne refait pas de déroulé.
2. **Chacun sa porte** : le porteur en en-tête n'ouvre pas un agent qui l'attend
   dans le corps, ni l'inverse.
3. **`none`** : aucun credential, choix explicite — seulement par l'adresse privée,
   toujours avec un plafond journalier, et l'agent sait que sa donnée n'est pas
   authentifiée.
"""
from __future__ import annotations

import asyncio
import typing

import pytest

from oto_mcp import runner_hook
from oto_mcp.capabilities import runner_triggers as RT
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx

ORG = 77
JETON = "otoh_le_bon_jeton"


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        raise AssertionError(f"SQL direct inattendu : {sql!r}")


@pytest.fixture
def porte(monkeypatch):
    """Un agent webhook ; `trigger_par_secret` rejoue la garde SQL (haché ET mode)."""
    vu = {"livraisons": [], "acceptees": 0, "deja": None, "sans_preuve_appels": 0}
    t = {"id": 5, "org_id": ORG, "sub": "alexis", "procedure": "lemlist-replies",
         "tools": ["a"], "input": "go", "enabled": True, "kind": "webhook",
         "payload_mode": "inline", "payload_fields": None, "max_per_hour": None,
         "fraicheur_s": None, "model": "claude-sonnet-5", "project_id": None, "max_steps": None,
         "label": None, "hook_auth": "body_secret", "max_per_day": None,
         "hook_slug": "h_privee"}
    vu["trigger"] = t

    def _par_secret(i, h, hook_auth="bearer"):
        vu.setdefault("recherches", []).append((h, hook_auth))
        ok = (i == 5 and h == runner_hook.hacher(JETON)
              and t["hook_auth"] == hook_auth)
        return dict(t) if ok else None

    def _sans_preuve(i):
        vu["sans_preuve_appels"] += 1
        ok = i == 5 and t["hook_auth"] == "none" and t["hook_slug"]
        return dict(t) if ok else None

    monkeypatch.setattr(runner_hook.db, "trigger_par_secret", _par_secret)
    monkeypatch.setattr(runner_hook.db, "trigger_sans_preuve", _sans_preuve)
    monkeypatch.setattr(runner_hook.db, "_connect", lambda: _Conn())
    monkeypatch.setattr(runner_hook.db, "verrouiller_le_declencheur", lambda c, i: None)
    monkeypatch.setattr(runner_hook.db, "retard_de_lissage", lambda c, t_, d, f: 0)
    monkeypatch.setattr(runner_hook.db, "acceptees_sur_24h",
                        lambda c, i: (vu["acceptees"], 3600))
    monkeypatch.setattr(runner_hook.db, "livraison_acceptee",
                        lambda c, t_, e: vu["deja"] if vu["deja"] and
                        vu["deja"]["external_id"] == e else None)
    monkeypatch.setattr(
        runner_hook.db, "enregistrer",
        lambda c, t_, o, outcome, job_id=None, source=None, due_at=None,
        external_id=None: vu["livraisons"].append((outcome, external_id)) or 1)
    monkeypatch.setattr(runner_hook.db, "enqueue_job",
                        lambda org, kind, **kw: vu.update(enfile=kw) or {"id": 900})
    return vu


def _lemlist(secret=JETON, _id="act_1"):
    return {"_id": _id, "type": "emailsReplied", "leadEmail": "a@b.com",
            "secret": secret}


# ── 0. le REGISTRE ────────────────────────────────────────────────────────────

def test_chaque_preregle_nomme_un_schema_CONNU():
    # `slack` : jugé par la route des événements Slack (`slack_events.py`), jamais
    # par `/api/hooks/{id}`.
    connus = {runner_hook.BEARER, runner_hook.STANDARD_WEBHOOKS,
              runner_hook.BODY_SECRET, runner_hook.NONE, runner_hook.SLACK}
    for p in runner_hook.PREREGLAGES.values():
        assert p.schema in connus, p
        if p.schema == runner_hook.BODY_SECRET:
            assert p.champ_secret, f"{p.nom} : un body_secret sans champ n'ouvre rien"


def test_l_API_de_pose_sert_EXACTEMENT_le_registre():
    """Un préréglage ajouté au registre mais pas à la pose serait inatteignable ;
    l'inverse, une valeur posable que la route ne sait pas juger."""
    servis = typing.get_args(RT.HookAuthInput.model_fields["hook_auth"].annotation)
    # L'ancien nom n'est admis par le schéma que pour être refusé en le nommant.
    assert set(servis) - {runner_hook.ANCIEN_NOM_BODY_SECRET} == \
        set(runner_hook.HOOK_AUTHS)


def test_l_ANCIEN_nom_lemlist_n_est_plus_un_prereglage():
    """Ni alias ni traduction : les lignes stockées sont réécrites par la révision
    `0071_hook_auth_body_secret`, et rien ne lit plus l'ancien nom."""
    assert runner_hook.ANCIEN_NOM_BODY_SECRET not in runner_hook.PREREGLAGES
    assert not hasattr(runner_hook, "canonique") and not hasattr(runner_hook, "ALIAS")


def test_un_mode_inconnu_se_lit_PORTEUR():
    assert runner_hook.prereglage("jamais_vu").nom == runner_hook.BEARER
    assert runner_hook.prereglage(None).nom == runner_hook.BEARER


# ── 1. `body_secret` : le secret dans le CORPS ──────────────────────────────────────

def test_lemlist_le_bon_secret_dans_le_corps_ENFILE(porte):
    out = runner_hook.declencher(5, None, _lemlist(), "lemlist",
                                 par_adresse_privee=True)
    assert out["job_id"] == 900
    assert porte["livraisons"] == [(runner_hook.db.QUEUED, "act_1")]


def test_lemlist_le_secret_est_RETIRE_avant_l_agent(porte):
    """⚠️ En `inline` le corps devient l'entrée de l'agent : le jeton y finirait
    dans un transcript, et dans `deliveries with_input`."""
    runner_hook.declencher(5, None, _lemlist(), "lemlist", par_adresse_privee=True)
    entree = porte["enfile"]["payload"]["input"]
    assert JETON not in entree and '"secret"' not in entree
    assert "a@b.com" in entree, "le reste du corps voyage"


def test_lemlist_un_secret_FAUX_rend_le_404_commun_sans_journal(porte):
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, None, _lemlist(secret="otoh_faux"), "x",
                               par_adresse_privee=True)
    assert (e.value.statut, e.value.message) == (404, runner_hook.HOOK_INCONNU)
    assert porte["livraisons"] == []


def test_lemlist_un_secret_SANS_prefixe_n_est_pas_regarde(porte):
    """Comme l'en-tête : une clé d'API ou un autre jeton collé là n'est même pas
    haché — un credential ne voyage pas vers une surface qui n'est pas la sienne."""
    with pytest.raises(runner_hook.HookRefus):
        runner_hook.declencher(5, None, _lemlist(secret="sk_live_quelconque"), "x",
                               par_adresse_privee=True)
    assert not porte.get("recherches"), "jamais cherché en base"


def test_lemlist_une_RETENTATIVE_du_meme_id_ne_refait_pas_de_deroule(porte):
    porte["deja"] = {"id": 1, "job_id": 900, "external_id": "act_1"}
    out = runner_hook.declencher(5, None, _lemlist(), "lemlist",
                                 par_adresse_privee=True)
    assert out["duplicate"] is True and "enfile" not in porte


def test_lemlist_par_l_id_NUMERIQUE_d_un_agent_a_adresse_privee_est_refuse(porte):
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, None, _lemlist(), "lemlist")
    assert e.value.statut == 404


# ── 2. chacun SA porte ────────────────────────────────────────────────────────

def test_une_ligne_encore_LEMLIST_ne_s_ouvre_plus(porte):
    """Une ligne que la révision `0071_hook_auth_body_secret` n'aurait pas réécrite
    reste FERMÉE (le même 404 qu'un agent inconnu) : aucun préréglage ne porte plus
    l'ancien nom, rien ne l'ouvre en le traduisant."""
    porte["trigger"]["hook_auth"] = "lemlist"
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, None, _lemlist(), "lemlist", par_adresse_privee=True)
    assert e.value.statut == 404


def test_le_porteur_en_EN_TETE_n_ouvre_pas_un_agent_body_secret(porte):
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, JETON, {}, "curl", par_adresse_privee=True)
    assert e.value.statut == 404


def test_le_secret_dans_le_CORPS_n_ouvre_pas_un_agent_au_porteur(porte):
    porte["trigger"]["hook_auth"] = "bearer"
    with pytest.raises(runner_hook.HookRefus):
        runner_hook.declencher(5, None, _lemlist(), "x", par_adresse_privee=True)
    # …alors que le même jeton, en en-tête, l'ouvre.
    assert runner_hook.declencher(5, JETON, {}, "x", par_adresse_privee=True)["job_id"]


def test_un_corps_qui_porte_un_secret_vers_un_agent_au_porteur_le_GARDE(porte):
    """Hors préréglage `body_secret`, un champ `secret` est une donnée comme une
    autre : rien n'est retiré d'un corps qui n'a pas servi de preuve."""
    porte["trigger"]["hook_auth"] = "bearer"
    runner_hook.declencher(5, JETON, {"secret": "valeur_metier"}, "x",
                           par_adresse_privee=True)
    assert "valeur_metier" in porte["enfile"]["payload"]["input"]


# ── 3. `none` : aucune preuve, par choix ──────────────────────────────────────

def test_none_s_ouvre_SANS_credential_par_l_adresse_privee(porte):
    porte["trigger"].update(hook_auth="none", max_per_day=50)
    out = runner_hook.declencher(5, None, {"x": 1}, "src", par_adresse_privee=True)
    assert out["job_id"] == 900


def test_none_ne_s_ouvre_JAMAIS_par_l_id_numerique(porte):
    """Un id se parcourt : sans preuve, seule l'adresse de 128 bits est un secret."""
    porte["trigger"].update(hook_auth="none", max_per_day=50)
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, None, {}, "src", par_adresse_privee=False)
    assert e.value.statut == 404
    assert porte["sans_preuve_appels"] == 0, "même pas cherché"


def test_none_garde_un_PLAFOND_meme_si_la_ligne_a_perdu_le_sien(porte):
    porte["trigger"].update(hook_auth="none", max_per_day=None)
    porte["acceptees"] = runner_hook.PLAFOND_SANS_PREUVE_DEFAUT
    with pytest.raises(runner_hook.HookRefus) as e:
        runner_hook.declencher(5, None, {}, "src", par_adresse_privee=True)
    assert (e.value.statut, e.value.code) == (429, "hook_daily_cap")


def test_none_l_agent_SAIT_que_sa_donnee_n_est_pas_authentifiee(porte):
    porte["trigger"].update(hook_auth="none", max_per_day=50)
    runner_hook.declencher(5, None, {"x": 1}, "src", par_adresse_privee=True)
    assert runner_hook._SANS_PREUVE in porte["enfile"]["payload"]["input"]


def test_un_agent_AUTHENTIFIE_ne_porte_pas_l_avertissement(porte):
    runner_hook.declencher(5, None, _lemlist(), "lemlist", par_adresse_privee=True)
    assert runner_hook._SANS_PREUVE not in porte["enfile"]["payload"]["input"]


# ── 4. la POSE ────────────────────────────────────────────────────────────────

def _ctx():
    return ResolvedCtx(sub="alexis", org_id=ORG)


def _appel(**kw):
    return asyncio.run(RT._triggers(_ctx(), RT.TriggerInput(**kw)))


def _auth(**kw):
    return asyncio.run(RT._hook_auth(_ctx(), RT.HookAuthInput(trigger_id=5, **kw)))


@pytest.fixture
def base(monkeypatch):
    monkeypatch.setenv("OTO_MCP_MASTER_KEY", "4" * 64)
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda portee, ident, fournisseur, cle: None)
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
             "signing_secret_set": False, "hook_slug": "h_privee", "max_per_day": 20}
    etat = {"ligne": ligne, "hash": "h0", "enc": "enc0"}

    def _poser_auth(tid, org, mode, secret_enc=None, effacer_le_secret=False):
        ligne["hook_auth"] = mode
        if effacer_le_secret:
            etat["enc"] = None
        elif secret_enc is not None:
            etat["enc"] = secret_enc
        ligne["signing_secret_set"] = etat["enc"] is not None
        return True

    monkeypatch.setattr(RT.db, "get_trigger", lambda t, o: dict(ligne))
    monkeypatch.setattr(RT.db, "update_trigger",
                        lambda t, o, champs, hors_abonnement_d_autrui=None:
                        ligne.update(champs) or dict(ligne))
    monkeypatch.setattr(RT.db, "poser_auth_de_hook", _poser_auth)
    monkeypatch.setattr(RT.db, "poser_secret_de_hook",
                        lambda t, o, h: etat.update(hash=h) or True)
    monkeypatch.setattr(RT.db, "poser_adresse_de_hook",
                        lambda t, o, slug: ligne.update(hook_slug=slug) or True)
    return etat


def test_passer_en_body_secret_emet_un_porteur_NEUF_et_efface_la_signature(base):
    out = _auth(hook_auth="body_secret")
    assert out["hook_secret"].startswith("otoh_")
    assert base["hash"] == runner_hook.hacher(out["hook_secret"])
    assert base["enc"] is None and out["trigger"]["hook_auth"] == "body_secret"


def test_poser_l_ANCIEN_nom_lemlist_est_REFUSE_en_le_nommant(base):
    """Refusé, jamais traduit en silence : le refus nomme le nom d'aujourd'hui, et
    la ligne ne bouge pas."""
    avant = dict(base["ligne"])
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="lemlist")
    assert e.value.code == "hook_auth_renamed" and e.value.status == 400
    assert "body_secret" in e.value.message
    assert base["ligne"] == avant and base["hash"] == "h0"


def test_none_SANS_adresse_privee_en_RECOIT_une(base):
    """Pas de pré-requis : la pose émet l'adresse, et l'id numérique cesse d'ouvrir."""
    base["ligne"]["hook_slug"] = None
    out = _auth(hook_auth="none")
    assert base["ligne"]["hook_slug"].startswith(runner_hook.ADRESSE_PREFIX)
    assert out["hook_url"].endswith(base["ligne"]["hook_slug"])


def test_none_SANS_plafond_journalier_est_refuse(base):
    base["ligne"]["max_per_day"] = None
    with pytest.raises(AuthzDenied) as e:
        _auth(hook_auth="none")
    assert e.value.code == "daily_cap_required"


def test_passer_en_none_EFFACE_porteur_et_signature_et_ne_rend_rien(base):
    out = _auth(hook_auth="none")
    assert out["trigger"]["hook_auth"] == "none" and not out.get("hook_secret")
    assert base["hash"] is None and base["enc"] is None, \
        "un credential dormant se réveillerait au prochain changement de mode"


def test_le_plafond_d_un_agent_none_ne_se_RETIRE_pas(base):
    _auth(hook_auth="none")
    with pytest.raises(AuthzDenied) as e:
        _appel(op="update", trigger_id=5, max_per_day=0)
    assert e.value.code == "daily_cap_required"
    _appel(op="update", trigger_id=5, max_per_day=40)   # le BAISSER/CHANGER, oui
    assert base["ligne"]["max_per_day"] == 40


def test_la_rotation_du_porteur_marche_sur_body_secret_PAS_sur_none(base):
    _auth(hook_auth="body_secret")
    out = _appel(op="rotate_secret", trigger_id=5)
    assert out["hook_secret"].startswith("otoh_")
    _auth(hook_auth="none")
    with pytest.raises(AuthzDenied):
        _appel(op="rotate_secret", trigger_id=5)


# ── 5. SANS preuve, l'adresse EST le credential : servie masquée, révélée une fois

def test_none_l_adresse_est_MASQUEE_a_toute_lecture(base):
    """`_avec_hook` est le passage UNIQUE de toute lecture servie (get, list,
    create, update, rotations) : masquer là, c'est masquer partout."""
    _auth(hook_auth="none")
    slug = base["ligne"]["hook_slug"]
    servi = RT._avec_hook(ORG, dict(base["ligne"]))
    assert slug not in servi["hook_url"], "ni MCP ni un agent partagé ne lisent le credential"
    assert servi["hook_url"].endswith(RT.ADRESSE_MASQUEE)


def test_passer_en_none_EMET_une_adresse_NEUVE_rendue_une_fois(base):
    """L'adresse d'avant a été servie en clair tant qu'elle n'ouvrait rien : elle
    ne peut pas devenir le credential."""
    avant = base["ligne"]["hook_slug"]
    out = _auth(hook_auth="none")
    neuve = base["ligne"]["hook_slug"]
    assert neuve != avant and neuve.startswith(runner_hook.ADRESSE_PREFIX)
    assert out["hook_url"].endswith("/api/hooks/" + neuve)
    assert neuve not in out["trigger"]["hook_url"]


def test_redemander_none_RENOUVELLE_l_adresse(base):
    premiere = _auth(hook_auth="none")["hook_url"]
    seconde = _auth(hook_auth="none")["hook_url"]
    assert seconde and seconde != premiere


def test_rotate_address_PAR_L_OUTIL_est_refuse_sur_none(base):
    _auth(hook_auth="none")
    with pytest.raises(AuthzDenied) as e:
        _appel(op="rotate_address", trigger_id=5)
    assert e.value.code == "no_auth_address"


def test_un_agent_AUTHENTIFIE_sert_son_adresse_en_clair(base):
    assert RT._avec_hook(ORG, dict(base["ligne"]))["hook_url"].endswith("/api/hooks/h_privee")
    assert not _auth(hook_auth="body_secret").get("hook_url")


# ── 6. Sentry : ni l'adresse ni le corps d'un webhook ne partent ──────────────

def test_sentry_ne_recoit_ni_l_ADRESSE_ni_le_CORPS_d_un_webhook():
    from oto_mcp import sentry_setup
    ev = {"request": {"url": "https://mcp.example/api/hooks/h_credentielsanspreuve",
                      "data": {"secret": JETON}, "query_string": "a=1"}}
    sentry_setup._before_send_transaction(ev, {})
    assert "h_credentielsanspreuve" not in repr(ev) and JETON not in repr(ev)
    assert ev["request"]["url"] == "https://mcp.example/api/hooks/[redacted]"


def test_sentry_les_autres_routes_gardent_leur_requete():
    from oto_mcp import sentry_setup
    ev = {"request": {"url": "https://mcp.example/api/me", "data": {"x": 1}}}
    sentry_setup._before_send_transaction(ev, {})
    assert ev["request"] == {"url": "https://mcp.example/api/me", "data": {"x": 1}}


# ── 7. `hook_slug` ne sort JAMAIS brut (un Output DÉCRIT, il ne filtre pas) ────

def test_avec_hook_ne_sert_pas_HOOK_SLUG(base):
    _auth(hook_auth="none")
    servi = RT._avec_hook(ORG, dict(base["ligne"]))
    assert "hook_slug" not in servi
    assert base["ligne"]["hook_slug"] not in repr(servi)


def test_ni_oto_trigger_ni_hook_auth_ne_servent_HOOK_SLUG(base, monkeypatch):
    """Les SORTIES des deux capacités, quel que soit le chemin : même une réponse
    qui ne passerait pas par `_avec_hook` ne laisse pas filer l'adresse."""
    _auth(hook_auth="none")
    slug = base["ligne"]["hook_slug"]
    rep = RT._sans_adresse_brute({"trigger": dict(base["ligne"]),
                                  "triggers": [dict(base["ligne"])]})
    assert slug not in repr(rep)
    out = _auth(hook_auth="none")   # sortie réelle de la capacité hook-auth
    assert "hook_slug" not in out["trigger"]
    nouvelle = base["ligne"]["hook_slug"]
    assert out["trigger"]["hook_url"].endswith(RT.ADRESSE_MASQUEE)
    assert out["hook_url"].endswith(nouvelle), "révélée une fois, ici seulement"
    maj = _appel(op="update", trigger_id=5, max_per_day=30)   # sortie réelle d'oto_trigger
    assert nouvelle not in repr(maj)


def test_none_l_agent_est_AVERTI_meme_en_mode_ignore(porte):
    porte["trigger"].update(hook_auth="none", max_per_day=50, payload_mode="ignore")
    runner_hook.declencher(5, None, {"x": 1}, "src", par_adresse_privee=True)
    entree = porte["enfile"]["payload"]["input"]
    assert runner_hook._SANS_PREUVE in entree and '"x"' not in entree


def test_passer_en_none_n_ouvre_JAMAIS_l_ancienne_adresse_sur_un_echec(base, monkeypatch):
    """Trois écritures, trois transactions : si l'adresse neuve n'a pas pu s'écrire,
    l'agent reste dans son mode d'avant — jamais `none` sur l'adresse déjà servie."""
    def _panne(t, o, slug):
        raise RuntimeError("base injoignable")
    monkeypatch.setattr(RT.db, "poser_adresse_de_hook", _panne)
    with pytest.raises(RuntimeError):
        _auth(hook_auth="none")
    assert base["ligne"]["hook_auth"] == "bearer"
    assert base["ligne"]["hook_slug"] == "h_privee"
