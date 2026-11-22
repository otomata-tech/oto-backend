"""Le TEXTE d'une péremption de cycle nomme la VRAIE cause (27/09/2026,
OTO-130) — pas une garantie de comportement (la RÈGLE d'expiration ne
bouge pas), un diagnostic. Avant ce lot, `perimer_travaux_du_declencheur`
disait toujours « aucun agent ne dessert cette organisation », même quand un
worker de la famille existait et que seule la connexion personnelle du
propriétaire (ou un pool en pause) manquait — deux diagnostics qui n'envoient
pas au même geste : l'un dit « préviens l'exploitant », l'autre dit
« reconnecte-toi, ça repart tout seul ».

Ce fichier tient `_abonnement.raison_de_peremption` contre une vraie base pour
les lectures d'abonnement/pool, et vérifie que le texte calculé est bien celui
qui atterrit dans `last_error` (`perimer_travaux_du_declencheur`)."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

_F = "claude_subscription"


@pytest.fixture(scope="module")
def live(pg_dsn):
    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    name = "oto_raison_perime_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{name}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + name
    avant_url, avant_pool = os.environ.get("DATABASE_URL"), dbconn._pool
    avant_key = os.environ.get("OTO_MCP_MASTER_KEY")
    os.environ["DATABASE_URL"] = dsn
    os.environ["OTO_MCP_MASTER_KEY"] = "6" * 64
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = avant_pool
        for cle, valeur in (("DATABASE_URL", avant_url),
                            ("OTO_MCP_MASTER_KEY", avant_key)):
            if valeur is None:
                os.environ.pop(cle, None)
            else:
                os.environ[cle] = valeur
        root.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        root.close()


def _personne(sub):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute("INSERT INTO users (sub) VALUES (%s) ON CONFLICT DO NOTHING", (sub,))
    return sub


def _org(nom, *membres, mode=None):
    from oto_mcp import org_store
    from oto_mcp.db import org_subscription_pool as P
    admin = _personne(f"{nom}-admin")
    oid = org_store.create_org(f"Org {nom}", created_by=admin)
    org_store.add_org_member(oid, admin, "org_admin")
    for m in membres:
        org_store.add_org_member(oid, _personne(m), "org_member")
    if mode:
        P.poser_mode(oid, _F, mode, admin)
    return oid


def _abonne(sub, *, statut="connected", reset=None, pret_a=()):
    from oto_mcp.db import org_subscription_pool as P
    from oto_mcp.db import user_subscriptions as US
    _personne(sub)
    US.upsert_sandbox(sub, _F, f"sandbox-{sub}")
    US.marquer_statut(sub, _F, statut, limit_reset_at=reset, ok=statut == "connected")
    if pret_a:
        P.poser_prets(sub, _F, pret_a)
    return sub


def _futur(heures=1):
    return datetime.now(timezone.utc) + timedelta(hours=heures)


def _servie(*familles):
    return {"armed": True, "families": list(familles)}


def _raison(*a, **kw):
    from oto_mcp.capabilities import _abonnement
    return _abonnement.raison_de_peremption(*a, **kw)


# ── famille API déclarée : servie ou pas ────────────────────────────────────
#    (aucun modèle déclaré n'arrive plus ici : le déclencheur refuse la livraison,
#    `test_declencheur_sans_modele.py`)

def test_famille_api_non_servie(live):
    r = _raison("qui@x", 1, "anthropic", _servie("mistral"))
    assert "Aucun agent ne dessert cette organisation." not in r
    assert "Aucun worker ne sert la famille `anthropic`" in r


def test_famille_api_servie(live):
    r = _raison("qui@x", 1, "anthropic", _servie("anthropic"))
    assert "Aucun agent ne dessert cette organisation." not in r
    assert "Aucun worker" not in r


# ── abonnement personnel : la vraie cause, pas le texte générique ──────────

def test_abonnement_famille_non_servie_du_tout(live):
    oid = _org("r1", "r1-dem")
    _abonne("r1-dem", statut="needs_login")
    r = _raison("r1-dem", oid, _F, _servie())
    assert r.endswith(f"Aucun worker ne sert la famille `{_F}` (aucun worker vu)."), r


def test_abonnement_doit_se_reconnecter(live):
    oid = _org("r2", "r2-dem")
    _abonne("r2-dem", statut="needs_login")
    r = _raison("r2-dem", oid, _F, _servie(_F))
    assert "Aucun agent ne dessert cette organisation." not in r
    assert "doit se reconnecter" in r


def test_abonnement_deconnecte(live):
    oid = _org("r3", "r3-dem")
    _abonne("r3-dem", statut="disconnected")
    r = _raison("r3-dem", oid, _F, _servie(_F))
    assert "doit se reconnecter" in r


def test_abonnement_plafond_futur(live):
    oid = _org("r4", "r4-dem")
    _abonne("r4-dem", statut="paused_limit", reset=_futur())
    r = _raison("r4-dem", oid, _F, _servie(_F))
    assert "Aucun agent ne dessert cette organisation." not in r
    assert "plafond de consommation" in r


def test_abonnement_connecte_mais_pas_ouvert_ici(live):
    """Connecté, jamais ouvert dans CETTE org : la réservation fait attendre le
    travail (`_ABONNEMENT_OUVERT_ICI`). Ce cas portait le texte générique, qui
    envoyait chercher un agent absent quand il fallait ouvrir l'abonnement."""
    oid = _org("r5", "r5-dem")
    _abonne("r5-dem", statut="connected")
    r = _raison("r5-dem", oid, _F, _servie(_F))
    assert "Aucun agent ne dessert cette organisation." not in r
    assert "pas ouvert dans cette organisation" in r, r


def _ouvert_ici(sub, oid):
    from oto_mcp.db import user_subscriptions as US
    US.poser_orgs_servies(sub, _F, [oid])


def test_abonnement_servable_et_ouvert_ici_dit_un_travail_a_la_fois(live):
    """Tout est en place : il ne reste que la sérialisation (un travail à la fois par
    abonnement). Le dire, au lieu d'« aucun agent » — un worker de la famille vit."""
    oid = _org("r5b", "r5b-dem")
    _abonne("r5b-dem", statut="connected")
    _ouvert_ici("r5b-dem", oid)
    r = _raison("r5b-dem", oid, _F, _servie(_F))
    assert "Aucun agent ne dessert cette organisation." not in r
    assert "un travail à la" in r and "fois" in r, r


def test_abonnement_plafond_echu_suit_l_attente_ordinaire(live):
    oid = _org("r5c", "r5c-dem")
    _abonne("r5c-dem", statut="paused_limit",
            reset=datetime.now(timezone.utc) - timedelta(hours=1))
    _ouvert_ici("r5c-dem", oid)
    r = _raison("r5c-dem", oid, _F, _servie(_F))
    assert "plafond" not in r and "Aucun agent ne dessert" not in r, r
    assert "un travail à la" in r, r


def test_abonnement_sans_proprietaire(live):
    r = _raison(None, 1, _F, _servie(_F))
    assert "Aucun agent ne dessert cette organisation." not in r
    assert "pas de propriétaire" in r, r


# ── pool ─────────────────────────────────────────────────────────────────────

def test_pool_vide(live):
    oid = _org("r6", "r6-dem", mode="pool")
    r = _raison("r6-dem", oid, _F, _servie(_F))
    assert "pool" in r and "vide" in r


def test_pool_en_pause(live):
    oid = _org("r7", "r7-dem", "r7-preteur", mode="pool")
    _abonne("r7-preteur", statut="paused_limit", reset=_futur(), pret_a=[oid])
    r = _raison("r7-dem", oid, _F, _servie(_F))
    assert "pool" in r and "pause" in r



def test_pool_avec_un_preteur_servable_dit_qu_ils_etaient_occupes(live):
    oid = _org("r7c", "r7c-dem", "r7c-preteur", mode="pool")
    _abonne("r7c-preteur", statut="connected", pret_a=[oid])
    r = _raison("r7c-dem", oid, _F, _servie(_F))
    assert "Aucun agent ne dessert cette organisation." not in r
    assert "prêteurs servables" in r, r

# ── la BASE (attendu ou pas) porte toujours l'ouverture, sans « aucun agent » ni
#    « aucun modèle » : c'est ce que le texte GARDE de la version d'avant ────

def test_le_socle_est_toujours_present(live):
    for r in (
        _raison("x", 1, "mistral", _servie("anthropic")),
        _raison("x", 1, "anthropic", _servie("anthropic")),
    ):
        assert r.startswith(
            "occurrence non prise dans son cycle : le déclencheur a enfilé la "
            "suivante.")


# ── intégration : le texte calculé atterrit bien dans `last_error` ─────────

def test_le_texte_calcule_atterrit_dans_last_error(live):
    from oto_mcp import db

    oid = _org("r8", "r8-dem")
    _abonne("r8-dem", statut="paused_limit", reset=_futur())
    job = db.enqueue_job(oid, "start", sub="r8-dem",
                         payload={"procedure": "p", "model": "sub:sonnet",
                                  "model_family": _F, "trigger_id": 999})

    raison = _raison("r8-dem", oid, _F, _servie(_F))
    n = db.perimer_travaux_du_declencheur(999, oid, raison=raison)
    assert n == 1

    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        row = conn.execute("SELECT status, last_error FROM runner_jobs WHERE id = %s",
                           (job["id"],)).fetchone()
    assert row["status"] == "expired"
    assert row["last_error"] == raison
    assert "plafond de consommation" in row["last_error"]



def test_pool_de_preteurs_a_reconnecter_n_est_pas_un_plafond(live):
    """Un pool dont TOUS les prêteurs doivent se reconnecter n'est pas « en pause
    (plafond) » — c'est la confusion même que ce diagnostic devait lever."""
    oid = _org("r7b", "r7b-dem", "r7b-a", "r7b-b", mode="pool")
    _abonne("r7b-a", statut="needs_login", pret_a=[oid])
    _abonne("r7b-b", statut="needs_login", pret_a=[oid])
    r = _raison("r7b-dem", oid, _F, _servie(_F))
    assert "plafond" not in r
    assert "reconnect" in r


# ── pool AU PLAFOND : jusqu'à quand, et pourquoi le repli ne l'a pas pris (21/10/2026)

def _pool_au_plafond(nom):
    """Deux prêteurs au plafond, l'un repart dans 2 h, l'autre dans 5 h : la reprise la
    plus PROCHE est celle de 2 h."""
    oid = _org(nom, f"{nom}-dem", f"{nom}-a", f"{nom}-b", mode="pool")
    proche = _futur(2)
    _abonne(f"{nom}-a", statut="paused_limit", reset=proche, pret_a=[oid])
    _abonne(f"{nom}-b", statut="paused_limit", reset=_futur(5), pret_a=[oid])
    return oid, f"{proche:%d/%m/%Y %H:%M} UTC"


def test_pool_au_plafond_repli_ferme_dit_jusqu_a_quand(live):
    oid, reprise = _pool_au_plafond("r9")
    r = _raison("r9-dem", oid, _F, _servie(_F), modele="sub:sonnet")
    assert f"abonnements au plafond jusqu'au {reprise}" in r, r
    assert r.endswith("repli sur la clé d'org fermé."), r


def test_pool_au_plafond_repli_ouvert_sans_cle_d_org(live):
    from oto_mcp.db import org_subscription_pool as P
    oid, reprise = _pool_au_plafond("r10")
    P.set_repli_api(oid, _F, True, "banc")
    r = _raison("r10-dem", oid, _F, _servie(_F), modele="sub:sonnet")
    assert f"jusqu'au {reprise}" in r, r
    assert "repli sur la clé d'org ouvert mais aucune clé `anthropic` déposée" in r, r


def test_pool_au_plafond_repli_ouvert_avec_cle_garde_le_texte_d_avant(live):
    from oto_mcp import org_store
    from oto_mcp.db import org_subscription_pool as P
    oid, _ = _pool_au_plafond("r11")
    P.set_repli_api(oid, _F, True, "banc")
    org_store.set_org_secret(oid, "anthropic", "sk-ant-api03-banc", set_by="banc")
    r = _raison("r11-dem", oid, _F, _servie(_F), modele="sub:sonnet")
    assert r.endswith("était en pause (plafond de consommation atteint) au moment où "
                      "la suivante est arrivée."), r


def test_la_raison_d_une_peremption_atterrit_aussi_dans_attempt_errors(live):
    """Le MÊME texte dans `last_error` et dans `attempt_errors` : `oto_trigger` (ses
    livraisons) et l'écran lisent les tentatives, et y trouvaient `[]`."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    oid, _ = _pool_au_plafond("r12")
    job = db.enqueue_job(oid, "start", sub="r12-dem",
                         payload={"procedure": "p", "model": "sub:sonnet",
                                  "model_family": _F, "trigger_id": 998})
    raison = _raison("r12-dem", oid, _F, _servie(_F), modele="sub:sonnet")
    assert db.perimer_travaux_du_declencheur(998, oid, raison=raison) == 1
    with _connect() as conn:
        row = conn.execute("SELECT status, last_error, attempt_errors FROM runner_jobs "
                           "WHERE id = %s", (job["id"],)).fetchone()
    assert row["status"] == "expired" and row["last_error"] == raison
    (t,) = row["attempt_errors"]
    assert t["error"] == raison and t["attempt"] == 0 and t["at"], t


# ── famille NON servie : la raison la nomme, avec la dernière présence (21/10/2026) ──
#    Les présences sont PORTÉES (`<famille>@org:<id>`) : chaque banc ne voit que les
#    siennes, quelle que soit la base partagée par le module.

def _presence(depot, quand="NOW()"):
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO runner_platform_depots (worker_sub, depot, last_seen_at) "
            f"VALUES (%s, %s, {quand})", (f"w-{uuid.uuid4().hex[:6]}", depot))


def test_famille_jamais_vue_le_dit(live):
    oid = _org("f1")
    r = _raison("f1-admin", oid, "mistral", _servie("anthropic"))
    assert r.endswith("Aucun worker ne sert la famille `mistral` (aucun worker vu)."), r


def test_famille_vue_autrefois_donne_sa_derniere_presence(live):
    oid = _org("f2")
    _presence(f"mistral@org:{oid}", "'2026-10-18 09:12:00+00'")
    _presence(f"mistral@org:{oid}", "'2026-10-11 07:00:00+00'")
    from oto_mcp import db
    etat = db.runner_arme(oid)
    assert "mistral" not in etat["families"], etat
    r = _raison("f2-admin", oid, "mistral", etat)
    assert r.endswith("Aucun worker ne sert la famille `mistral` "
                      "(dernier vu le 18/10/2026 09:12 UTC)."), r


def test_la_presence_portee_d_une_autre_org_ne_compte_pas(live):
    oid, autre = _org("f3"), _org("f3b")
    _presence(f"mistral@org:{autre}", "'2026-10-18 09:12:00+00'")
    r = _raison("f3-admin", oid, "mistral", _servie())
    assert r.endswith("(aucun worker vu)."), r


def test_une_presence_de_ferme_qui_ne_sert_pas_l_org_ne_compte_pas(live):
    """La règle de `families` (ferme comprise), pas une lecture brute de la table : un
    worker de ferme ne sert pas `anthropic` à une org non routée sans clé déposée."""
    oid = _org("f4")
    _presence(f"anthropic#ferme@org:{oid}", "'2026-10-18 09:12:00+00'")
    r = _raison("f4-admin", oid, "anthropic", _servie())
    assert r.endswith("Aucun worker ne sert la famille `anthropic` (aucun worker vu)."), r


def test_une_famille_vivante_est_servie_et_la_raison_ne_l_accuse_pas(live):
    oid = _org("f5")
    _presence(f"mistral@org:{oid}")
    from oto_mcp import db
    etat = db.runner_arme(oid)
    assert "mistral" in etat["families"], etat
    assert "Aucun worker" not in _raison("f5-admin", oid, "mistral", etat)


def test_la_famille_non_servie_atterrit_dans_last_error_et_attempt_errors(live):
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    oid = _org("f6")
    _presence(f"mistral@org:{oid}", "'2026-10-18 09:12:00+00'")
    job = db.enqueue_job(oid, "start", sub="f6-admin",
                         payload={"procedure": "p", "model": "mistral-large-2512",
                                  "model_family": "mistral", "trigger_id": 997})
    raison = _raison("f6-admin", oid, "mistral", db.runner_arme(oid))
    assert db.perimer_travaux_du_declencheur(997, oid, raison=raison) == 1
    with _connect() as conn:
        row = conn.execute("SELECT status, last_error, attempt_errors FROM runner_jobs "
                           "WHERE id = %s", (job["id"],)).fetchone()
    assert row["status"] == "expired" and row["last_error"] == raison
    assert "famille `mistral` (dernier vu le 18/10/2026 09:12 UTC)" in raison
    (t,) = row["attempt_errors"]
    assert t["error"] == raison and t["attempt"] == 0, t


# ── le repli sur la clé de PLATEFORME dans le diagnostic (19/11/2026) ────────

@pytest.mark.parametrize("declare", [None, "0"])
def test_pool_au_plafond_instance_qui_ne_permet_pas_garde_le_texte_d_avant(
        live, monkeypatch, declare):
    """Une instance qui ne permet pas le repli sur la clé de plateforme ne le nomme
    pas : le texte reste celui d'avant, mot pour mot — même si l'org l'a réglé."""
    from oto_mcp.db import org_subscription_pool as P
    if declare is None:
        monkeypatch.delenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", raising=False)
    else:
        monkeypatch.setenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", declare)
    nom = f"r13{declare}"
    oid, reprise = _pool_au_plafond(nom)
    P.set_repli_api(oid, _F, True, "banc")
    P.set_repli_cle_plateforme(oid, _F, True, "banc")
    r = _raison(f"{nom}-dem", oid, _F, _servie(_F), modele="sub:sonnet")
    assert r.endswith(f"jusqu'au {reprise}, repli sur la clé d'org ouvert mais aucune "
                      "clé `anthropic` déposée par l'organisation."), r


def test_pool_au_plafond_repli_plateforme_ferme_le_dit(live, monkeypatch):
    """L'instance le permet, l'org ne l'a pas ouvert : le diagnostic nomme le réglage
    qui aurait servi le travail."""
    from oto_mcp.db import org_subscription_pool as P
    monkeypatch.setenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", "1")
    oid, reprise = _pool_au_plafond("r14")
    P.set_repli_api(oid, _F, True, "banc")
    r = _raison("r14-dem", oid, _F, _servie(_F), modele="sub:sonnet")
    assert r.endswith("aucune clé `anthropic` déposée par l'organisation, et repli sur "
                      "la clé de plateforme fermé."), r
    assert f"jusqu'au {reprise}" in r


def test_pool_au_plafond_repli_plateforme_ouvert_dit_ce_qui_l_a_retenu(live, monkeypatch):
    from oto_mcp.db import org_subscription_pool as P
    monkeypatch.setenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", "yes")
    oid, reprise = _pool_au_plafond("r15")
    P.set_repli_api(oid, _F, True, "banc")
    P.set_repli_cle_plateforme(oid, _F, True, "banc")
    r = _raison("r15-dem", oid, _F, _servie(_F), modele="sub:sonnet")
    assert f"jusqu'au {reprise}" in r
    assert "repli sur la clé de plateforme ouvert" in r, r
    assert "un seul travail replié à la fois par forfait" in r
    assert "accordée à l'organisation" in r and "crédits" in r


def test_pool_au_plafond_repli_api_ferme_ne_parle_pas_de_la_plateforme(live, monkeypatch):
    """`repli_api` fermé : rien ne replie, quel que soit le payeur choisi."""
    from oto_mcp.db import org_subscription_pool as P
    monkeypatch.setenv("OTO_REPLI_CLE_PLATEFORME_PERMIS", "1")
    monkeypatch.delenv("OTO_REPLI_API_PAR_DEFAUT", raising=False)
    oid, _ = _pool_au_plafond("r16")
    P.set_repli_cle_plateforme(oid, _F, True, "banc")
    r = _raison("r16-dem", oid, _F, _servie(_F), modele="sub:sonnet")
    assert r.endswith("repli sur la clé d'org fermé."), r
