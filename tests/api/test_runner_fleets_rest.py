"""Les flottes SUR LA ROUTE SERVIE — la réponse est sérialisée, pas inspectée.

⚠️ **Ce fichier existe à cause de ce que son absence a laissé passer.** Le lot qui a
créé cette capacité ne la faisait jamais passer par HTTP : ses tests lisaient des
champs de modèle et attrapaient des refus levés en appelant le handler à la main.
Deux défauts en sont sortis, de la MÊME famille — une valeur que PostgreSQL rend en
`Decimal`, que rien ne normalise, et qui fait un 500 à la sérialisation :

1. `max_cost_usd NUMERIC` — trouvé en relecture ;
2. `SUM(...)::bigint`, qui rend `numeric` en PostgreSQL — **passé sous le correctif
   du premier**, parce que la garde ajoutée balayait des NOMS (`usd|cost|euro`) et
   que le champ s'appelle `usage_tokens`.

> **Le correctif visait l'axe sur lequel le défaut s'était présenté (un nom), pas
> l'axe sur lequel il vit (un type qui ne se sérialise pas).** Un balayage de noms
> élargi à quatre modèles donnait l'impression d'avoir appris la leçon ; la seule
> occurrence vivante est passée dessous.

**La seule garde qui ne peut pas se tromper d'axe : sérialiser une vraie réponse.**
`TestClient` rend le JSON par la même pile que la production — si un type ne passe
pas, le test rougit, quel que soit le nom du champ.

Les refus sont rejoués ici aussi, parce qu'une déclaration `Capability.errors` sans
rejeu est décorative : elle promet un statut que le serveur ne rend peut-être pas.
"""
from __future__ import annotations

import uuid

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient


ROUTE = "/api/me/runner/fleets"


class _Claims:
    def __init__(self, sub: str):
        self.claims = {"sub": sub, "email": f"{sub}@fleets.invalid", "name": sub}


class _Verifier:
    async def verify_token(self, token: str):
        return _Claims(token)


def _h(sub: str) -> dict:
    return {"Authorization": f"Bearer {sub}"}


@pytest.fixture(scope="module")
def live(pg_dsn):
    import os

    psycopg = pytest.importorskip("psycopg")
    from oto_mcp.db import _conn as dbconn

    nom = "oto_fleets_rest_" + uuid.uuid4().hex[:8]
    root = psycopg.connect(pg_dsn, autocommit=True)
    root.execute(f'CREATE DATABASE "{nom}"')
    dsn = pg_dsn.rsplit("/", 1)[0] + "/" + nom
    url_avant, pool_avant = os.environ.get("DATABASE_URL"), dbconn._pool
    os.environ["DATABASE_URL"] = dsn
    dbconn._pool = None
    try:
        from oto_mcp.db import init_db
        init_db()
        yield dsn
    finally:
        if dbconn._pool is not None:
            dbconn._pool.close()
        dbconn._pool = pool_avant
        if url_avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = url_avant
        root.execute(f'DROP DATABASE IF EXISTS "{nom}" WITH (FORCE)')
        root.close()


@pytest.fixture(scope="module")
def client(live):
    from oto_mcp.api import routes as api_routes
    return TestClient(Starlette(routes=api_routes.make_routes(_Verifier(),
                                                              mcp_instance=None)))


@pytest.fixture(scope="module")
def org(live):
    from oto_mcp import db, org_store
    membre = "usr_fleets_membre"
    db.upsert_user(membre, email=f"{membre}@fleets.invalid", name=membre)
    oid = org_store.create_org("Org des flottes", created_by=membre)
    org_store.add_org_member(oid, membre, "org_admin")
    org_store.set_active_org(membre, oid)
    # La cible se résout À LA DÉCLARATION (#1067) : elle existe dans la portée.
    ns = db.create_datastore("org", str(oid), "un-tableau")
    return {"id": oid, "membre": membre, "ns": ns}


#: Le modèle des passages de ce fichier : servi par le worker de module.
MODELE_SERVI = "mistral-small-2603"


@pytest.fixture(scope="module", autouse=True)
def _un_worker_de_plateforme_sonde(live, org):
    """oto-runner#13 (17/09/2026) : `launch` refuse désormais si aucun worker n'est
    joignable (`no_runner_armed`) — ce fichier teste le CYCLE d'une flotte, pas
    cette précondition (couverte par `tests/test_runner_fleets_sans_worker.py`), donc
    un worker de plateforme réellement présent est posé une fois pour tout le
    module. Un worker de plateforme sert TOUTES les orgs (`db.runner_arme`), donc
    une seule ligne suffit ; elle sert la famille `mistral` (`MODELE_SERVI`) — le
    dernier banc pose sa propre présence Anthropic explicitement.

    Retiré à la sortie, comme tout worker de banc (patron de
    `test_modele_propose_accepte_rest.py`) : la table est globale, et une ligne
    laissée à `NOW()` ferait lire « un runner est là » à qui suit."""
    from oto_mcp.db._conn import _connect
    sub = "worker:banc-flottes-rest"
    with _connect() as c:
        c.execute("INSERT INTO runner_platform_workers (worker_sub) "
                  "VALUES (%s) ON CONFLICT (worker_sub) "
                  "DO UPDATE SET last_seen_at = NOW()", (sub,))
        # Un agent hébergé déclare son modèle (24/09/2026) : ce worker sert la famille
        # `mistral`, celle des passages de ce fichier (`MODELE_SERVI`). Anthropic n'est
        # PAS servie ici : le dernier banc en fait son sujet.
        c.execute("INSERT INTO runner_platform_depots (worker_sub, depot) "
                  "VALUES (%s, 'mistral') ON CONFLICT DO NOTHING", (sub,))
        c.commit()
    try:
        yield sub
    finally:
        with _connect() as c:
            c.execute("DELETE FROM runner_platform_depots WHERE worker_sub = %s", (sub,))
            c.execute("DELETE FROM runner_platform_workers WHERE worker_sub = %s", (sub,))
            c.commit()


@pytest.fixture(scope="module")
def flotte(client, org):
    """Une flotte déclarée PAR LA ROUTE — pas insérée en base à la main."""
    r = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "passage d'essai", "procedure": "enrichissement",
        "tools": ["oto_kb"], "namespace": "un-tableau", "row_filter": {"lot": "a"},
        # Un modèle du CATALOGUE : depuis le 12/09/2026 il part avec les travaux,
        # donc une chaîne libre est refusée à la déclaration.
        "model": "claude-sonnet-5",
        "max_rows": 10, "max_tokens": 1_000_000, "max_tokens_per_row": 50_000})
    assert r.status_code == 200, r.text
    return r.json()["fleet"]


# ── la réponse se SÉRIALISE — la garde qui ne peut pas se tromper d'axe ───────

def test_l_etat_d_une_flotte_vierge_se_serialise(client, org, flotte):
    """Le défaut vivait ICI, et il touchait 100 % des appels.

    `COALESCE(SUM(...), 0)` rend un `Decimal` **même sans un seul job** : la lecture
    phare de la capacité — celle qui devait rendre un opérateur autonome — répondait
    500 sur toute flotte, y compris vierge. Aucun test de modèle ne pouvait le voir.
    """
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "state", "fleet_id": flotte["id"]})
    assert r.status_code == 200, r.text
    etat = r.json()["state"]
    assert etat["no_jobs_attached"] is True
    assert etat["jobs_total"] == 0
    # les compteurs sont des ENTIERS servis, pas des chaînes ni des décimaux
    assert isinstance(etat["usage_tokens"], int)
    assert not isinstance(etat["usage_tokens"], bool)


def test_l_etat_se_serialise_aussi_avec_des_travaux_rattaches(client, org, flotte):
    """Le chemin où `SUM` rend vraiment une somme — l'autre moitié du défaut."""
    from oto_mcp.db._conn import _connect
    with _connect() as c:
        for jetons in (1200, 3400):
            c.execute(
                "INSERT INTO runner_jobs (org_id, kind, fleet_id, status, result) "
                "VALUES (%s, 'start', %s, 'done', %s::jsonb)",
                (org["id"], flotte["id"], '{"usage_tokens": %d}' % jetons))
        c.commit()
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "state", "fleet_id": flotte["id"]})
    assert r.status_code == 200, r.text
    etat = r.json()["state"]
    assert etat["jobs_total"] == 2 and etat["no_jobs_attached"] is False
    assert etat["usage_tokens"] == 4600
    assert isinstance(etat["usage_tokens"], int)


def test_la_flotte_elle_meme_se_serialise_sur_list_et_get(client, org, flotte):
    """`list` ramène la flotte dans le lot des autres : un type qui ne passe pas y
    rend TOUTE la liste illisible, pas seulement la flotte fautive."""
    for corps in ({"op": "list"}, {"op": "get", "fleet_id": flotte["id"]}):
        r = client.post(ROUTE, headers=_h(org["membre"]), json=corps)
        assert r.status_code == 200, f"{corps} → {r.text}"
    lot = client.post(ROUTE, headers=_h(org["membre"]),
                      json={"op": "list"}).json()["fleets"]
    assert any(f["id"] == flotte["id"] for f in lot)


# ── op=list rend la CARTE, get la déclaration — sur la route (13/09/2026) ─────

def test_list_rend_la_carte_et_get_la_declaration_sur_la_route(client, org, flotte):
    """La projection et l'empreinte rejouées là où elles se sérialisent : une clé
    rendue par le handler peut très bien ne pas atteindre le client."""
    import hashlib
    from oto_mcp.capabilities.runner_fleets import FleetCard
    h = _h(org["membre"])
    r = client.post(ROUTE, headers=h, json={"op": "list"})
    assert r.status_code == 200, r.text
    corps = r.json()
    carte = next(f for f in corps["fleets"] if f["id"] == flotte["id"])
    assert set(carte) == set(FleetCard.model_fields)
    get = client.post(ROUTE, headers=h,
                      json={"op": "get", "fleet_id": flotte["id"]}).json()["fleet"]
    assert get["input"] and get["tools"] == ["oto_kb"], "get rend la déclaration entière"
    assert carte["input_sha256"] == hashlib.sha256(get["input"].encode("utf-8")).hexdigest()
    assert {"input", "tools"} <= set(corps["projection"]["omitted"])
    assert "op=get" in corps["projection"]["hint"]
    assert "rows_at_launch" not in carte and "rows_at_launch" not in get


def test_armer_n_ecrit_ni_ne_sert_plus_le_denominateur(client, org):
    """La colonne reste en base, sans écrivain ni lecteur : une valeur posée avant le
    retrait SURVIT à l'armement (ni recompte ni remise à NULL) et n'est pas rendue."""
    from oto_mcp.db._conn import _connect
    h = _h(org["membre"])
    fid = client.post(ROUTE, headers=h, json={
        "op": "create", "model": MODELE_SERVI, "label": "sans-denominateur", "procedure": "p",
        "tools": ["oto_kb"], "namespace": "un-tableau"}).json()["fleet"]["id"]
    with _connect() as c:
        c.execute("UPDATE runner_fleets SET rows_at_launch = 12 WHERE id = %s", (fid,))
        c.commit()
    r = client.post(ROUTE, headers=h, json={"op": "launch", "fleet_id": fid})
    assert r.status_code == 200, r.text
    assert r.json()["fleet"]["status"] == "armed"
    assert "rows_at_launch" not in r.json()["fleet"]
    with _connect() as c:
        reste = c.execute("SELECT rows_at_launch FROM runner_fleets WHERE id = %s",
                          (fid,)).fetchone()
    assert reste["rows_at_launch"] == 12


# ── les refus DÉCLARÉS, rejoués sur la route servie ──────────────────────────

def _refus(client, org, corps: dict) -> tuple[int, str]:
    r = client.post(ROUTE, headers=_h(org["membre"]), json=corps)
    return r.status_code, (r.json() or {}).get("error", "")


def test_la_cible_figee_est_refusee_sur_la_route(client, org, flotte):
    assert _refus(client, org, {"op": "update", "fleet_id": flotte["id"],
                                "namespace": "ailleurs"}) == (400, "target_is_frozen")
    assert _refus(client, org, {"op": "update", "fleet_id": flotte["id"],
                                "row_filter": {"lot": "b"}}) == (400, "target_is_frozen")


def test_le_contexte_fige_est_refuse_sur_la_route(client, org, flotte):
    assert _refus(client, org, {"op": "update", "fleet_id": flotte["id"],
                                "model": "autre"}) == (400, "context_is_frozen")


def test_l_etat_ne_se_pose_pas_par_update_sur_la_route(client, org, flotte):
    assert _refus(client, org, {"op": "update", "fleet_id": flotte["id"],
                                "status": "stopped"}) == (400, "status_not_settable")


def test_un_champ_non_modifiable_ne_rend_pas_200_sans_effet(client, org, flotte):
    """`procedure` est le champ le plus lourd de la configuration — ce que la flotte
    EXÉCUTE. Il n'était ni refusé ni appliqué : 200, et rien ne changeait.

    ⚠️ La garde appartient au SEAM, pas au champ : tout champ d'entrée ni structurel
    ni modifiable doit aboutir ou être refusé. Écrite champ par champ, elle oublie
    exactement ceux auxquels on n'a pas pensé — et ceux qu'on ajoutera."""
    for champ, valeur in (("procedure", "UNE-AUTRE"), ("project_id", 4242)):
        statut, code = _refus(client, org, {"op": "update", "fleet_id": flotte["id"],
                                            champ: valeur})
        assert (statut, code) == (400, "field_not_settable"), f"{champ} → {statut}"
    # et la procédure n'a pas bougé
    f = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "get", "fleet_id": flotte["id"]}).json()["fleet"]
    assert f["procedure"] == "enrichissement"


def test_la_cible_est_gardee_par_son_identifiant_sur_la_route(flotte, org):
    assert flotte["namespace"] == str(org["ns"])


def test_une_cible_introuvable_ou_ambigue_est_refusee_sur_la_route(client, org):
    """#1067 : la cible se résout une fois, à la déclaration, dans la portée du
    déclarant — rien qui y réponde, ou deux tableaux au même rang, est refusé."""
    from oto_mcp import db, group_store
    base = {"op": "create", "model": MODELE_SERVI, "label": "x", "procedure": "p", "tools": ["oto_kb"]}
    assert _refus(client, org, {**base, "namespace": "nulle-part"}
                  ) == (404, "datastore_not_found")
    for i in (1, 2):
        g = group_store.create_group(org["id"], f"equipe-doublon-{i}")
        group_store.add_group_member(g, org["membre"])
        db.create_datastore("group", str(g), "doublon")
    assert _refus(client, org, {**base, "namespace": "doublon"}
                  ) == (409, "datastore_ambigu")


def test_un_perimetre_sans_tableau_est_refuse_sur_la_route(client, org):
    assert _refus(client, org, {"op": "create", "model": MODELE_SERVI, "label": "x", "procedure": "p",
                                "tools": ["oto_kb"], "row_filter": {"lot": "a"}}
                  ) == (400, "target_incomplete")


def test_create_sans_les_champs_requis_est_refuse_sur_la_route(client, org):
    assert _refus(client, org, {"op": "create", "model": MODELE_SERVI, "label": "x"}
                  ) == (400, "missing_fields")


def test_une_flotte_inconnue_rend_404_sur_la_route(client, org):
    assert _refus(client, org, {"op": "get", "fleet_id": 999_999_999}
                  ) == (404, "fleet_not_found")


def test_une_flotte_d_une_autre_org_est_invisible(client, org, flotte):
    """L'isolation par org se vérifie sur la route, pas sur le handler : c'est là
    que `org_id` est résolu depuis le porteur."""
    from oto_mcp import db, org_store
    autre = "usr_fleets_etranger"
    db.upsert_user(autre, email=f"{autre}@fleets.invalid", name=autre)
    oid = org_store.create_org("Une autre org", created_by=autre)
    org_store.add_org_member(oid, autre, "org_admin")
    org_store.set_active_org(autre, oid)
    r = client.post(ROUTE, headers=_h(autre),
                    json={"op": "get", "fleet_id": flotte["id"]})
    assert (r.status_code, r.json().get("error")) == (404, "fleet_not_found")


# ── les résidus de la 3ᵉ passe : le seam vaut pour TOUTE opération ────────────

def test_create_refuse_ce_qu_il_n_applique_pas(client, org):
    """`create status="running"` rendait 200 avec une flotte `draft` et le champ
    avalé — mot pour mot le geste que le refus d'`update` prédit.

    ⚠️ La garde était écrite dans la branche `update` SEULE. **Une garde écrite
    dans une branche ne garde que cette branche** : c'est le même défaut que celui
    qu'elle corrigeait, déplacé d'un verbe."""
    base = {"op": "create", "model": MODELE_SERVI, "label": "x", "procedure": "p", "tools": ["oto_kb"]}
    assert _refus(client, org, {**base, "status": "running"}
                  ) == (400, "status_not_settable")
    assert _refus(client, org, {**base, "fleet_id": 1}
                  ) == (400, "field_not_settable")


def test_une_borne_absurde_est_refusee_des_DEUX_cotes(client, org, flotte):
    """Une borne se compte, donc elle vaut au moins 1. `max_rows=-5`, `workers=0`
    passaient à la création ET à la retouche — une borne absurde acceptée est une
    panne différée, découverte au lancement plutôt qu'à la déclaration."""
    base = {"op": "create", "model": MODELE_SERVI, "label": "x", "procedure": "p", "tools": ["oto_kb"]}
    for champ, valeur in (("max_rows", -5), ("workers", 0), ("max_tokens", -1),
                          ("max_tokens_per_row", 0), ("max_steps", -2),
                          ("max_consecutive_failures", 0)):
        assert _refus(client, org, {**base, champ: valeur}) == (400, "invalid_bound"), champ
        assert _refus(client, org, {"op": "update", "fleet_id": flotte["id"],
                                    champ: valeur}) == (400, "invalid_bound"), champ


def test_update_ne_peut_pas_annuler_ce_que_create_exige(client, org, flotte):
    """`create tools=[]` était refusé et `update tools=[]` vidait l'allowlist.
    Une garde qui ne tient qu'à l'entrée laisse la sortie ouverte."""
    assert _refus(client, org, {"op": "update", "fleet_id": flotte["id"], "tools": []}
                  ) == (400, "missing_fields")
    f = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "get", "fleet_id": flotte["id"]}).json()["fleet"]
    assert f["tools"] == ["oto_kb"]


# ── LA BORNE PAR LIGNE, REJOUÉE SUR LA ROUTE ─────────────────────────────────
#
# ⚠️ Arbitrage d'Alexis, 09/09/2026, verbatim : « Une borne doit pouvoir être
# posée, si pas de borne, tant pis pour le moment (ou plutôt : pour le moment ça
# s'arrêtera à la fenêtre de contexte du LLM). »
#
# Le serveur ne refuse donc plus rien ici : ni l'absence de borne, ni une valeur
# haute. Les trois bancs qui rejouaient ces refus SUR LA ROUTE tombent avec eux —
# garder un banc sur une garde retirée la rétablirait par la porte du test, et
# c'est le test qui suit le code, jamais l'inverse.
#
# Ce qui reste à éprouver, et ce n'est pas rien : le champ est POSABLE, et une
# borne posée est SERVIE. `budget_max_tokens`, rendu à l'armement, vaut
# `max_rows × max_tokens_per_row` — le seul chiffre qui réponde à « combien ce
# passage peut coûter », montré au moment où l'on engage la dépense. Sans borne
# il vaut `null`, ce qui le DIT au lieu de fabriquer une protection.
#
# ⚠️ Le plancher générique tient toujours, lui : `max_tokens_per_row=0` est refusé
# comme toute borne absurde (`test_une_borne_absurde_est_refusee_des_DEUX_cotes`).


def test_declarer_sans_borne_par_ligne_est_PERMIS_sur_la_route(client, org):
    """L'absence de borne n'est plus un refus. Le passage n'a alors aucun plafond
    de jetons — assumé : l'arrêt reste la fenêtre de contexte du modèle."""
    r = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "sans-borne", "procedure": "p",
        "tools": ["oto_kb"]})
    assert r.status_code == 200, r.text
    assert r.json()["fleet"]["max_tokens_per_row"] is None


def test_une_borne_HAUTE_est_PERMISE_sur_la_route(client, org):
    """1,5 M par ligne : la valeur qu'un plafond serveur aurait refusée, et avec
    elle 86 déclarations vivantes, dont une de production."""
    r = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "borne-haute", "procedure": "p",
        "tools": ["oto_kb"], "max_tokens_per_row": 1_500_000})
    assert r.status_code == 200, r.text
    assert r.json()["fleet"]["max_tokens_per_row"] == 1_500_000


def test_la_borne_POSEE_est_SERVIE_a_l_armement(client, org):
    """⚠️ Ce qui reste de la question d'origine, et la seule part qui servait
    vraiment : le pire cas, rendu PAR LA ROUTE au moment où l'on arme. C'est ce
    nombre qui aurait affiché les 150 millions de jetons à celui qui armait.

    Il est rejoué ici parce qu'un champ calculé par le handler peut très bien ne
    jamais atteindre le client — c'est toute la raison d'être de ce fichier."""
    fid = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "pire-cas", "procedure": "p", "tools": ["oto_kb"],
        "max_rows": 100, "max_tokens_per_row": 50_000}).json()["fleet"]["id"]
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "launch", "fleet_id": fid})
    assert r.status_code == 200, r.text
    assert r.json()["budget_max_tokens"] == 5_000_000, (
        "`max_rows × max_tokens_per_row` : la dépense maximale du passage")


def test_sans_borne_l_armement_dit_NULL_et_arme_quand_meme(client, org):
    """Les deux moitiés du même choix : armer sans borne reste PERMIS, et le pire
    cas vaut `null`. Un nombre fabriqué ferait croire à une protection qui
    n'existe pas."""
    fid = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "arme-sans-borne", "procedure": "p",
        "tools": ["oto_kb"], "max_rows": 100}).json()["fleet"]["id"]
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "launch", "fleet_id": fid})
    assert r.status_code == 200, r.text
    assert r.json()["fleet"]["status"] == "armed"
    assert r.json()["budget_max_tokens"] is None


def test_une_flotte_HISTORIQUE_sans_borne_reste_lisible_et_S_ARME(client, org):
    """Le cas qui avait motivé une garde à l'armement — deux flottes déclarées
    avant le champ ne l'ont pas (mesuré le 09/09/2026 : 2 sur 101). Sans refus,
    elles se lisent ET s'arment, et leur pire cas est `null`, ce qui est la
    vérité.

    L'état est reproduit EN BASE : par la route, `update max_tokens_per_row=None`
    ne retire rien (un champ absent du corps n'est pas une remise à zéro)."""
    from oto_mcp.db import runner_fleets as dbf

    fid = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "historique", "procedure": "p",
        "tools": ["oto_kb"], "max_rows": 10,
        "max_tokens_per_row": 50_000}).json()["fleet"]["id"]
    dbf.update_fleet(fid, org["id"], {"max_tokens_per_row": None})

    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "get", "fleet_id": fid})
    assert r.status_code == 200, r.text
    assert r.json()["fleet"]["max_tokens_per_row"] is None

    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "launch", "fleet_id": fid})
    assert r.status_code == 200, r.text
    assert r.json()["fleet"]["status"] == "armed"
    assert r.json()["budget_max_tokens"] is None



# ── LANCER et ARRÊTER : deux verbes, deux planchers, deux gardes ─────────────
#
# ⚠️ Ils ne sont PAS symétriques, et c'est tout le point. Ils entrent par la même
# porte mais n'engagent pas la même chose :
#
#   lancer   de l'argent et des effets externes IRRÉVERSIBLES — des lignes
#            écrites chez un tiers ⟹ plancher ADMIN
#   arrêter  une interruption et un travail à reprendre ⟹ tout MEMBRE, parce
#            qu'un passage qui part en vrille doit pouvoir être stoppé par la
#            première personne qui le voit
#
# ⚠️ Et aucun des deux ne pose un FAIT : `launch` arme (on a demandé), `stop`
# demande l'arrêt (la boucle ne l'a pas lu). Une intention déclarée et un fait
# constaté ne partagent jamais une colonne.

@pytest.fixture(scope="module")
def flotte_a_piloter(client, org):
    r = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "pilotage", "procedure": "p", "tools": ["oto_kb"],
        "max_tokens_per_row": 50_000})
    assert r.status_code == 200, r.text
    return r.json()["fleet"]


def test_lancer_ARME_et_ne_pretend_pas_que_ca_tourne(client, org, flotte_a_piloter):
    """⚠️ LE point : `armed`, jamais `running`.

    `running` veut dire qu'un ordonnanceur l'a PRISE et donne signe. Poser
    `running` ici ferait lire « en cours » un passage que personne n'exécute —
    et une flotte armée que nul n'a réclamée doit se DIRE, pas se confondre."""
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "launch", "fleet_id": flotte_a_piloter["id"]})
    assert r.status_code == 200, r.text
    f = r.json()["fleet"]
    assert f["status"] == "armed", "une intention, pas un fait"
    assert f["armed_at"] and not f["started_at"]


def test_arreter_DEMANDE_et_ne_pretend_pas_que_c_est_fait(client, org, flotte_a_piloter):
    """⚠️ Le mensonge symétrique, et il est PIRE.

    Entre cet appel et la lecture par la boucle, le passage continue : il réserve,
    il appelle, il dépense. Annoncer `stopped` ferait croire qu'on a coupé une
    dépense qui continue — on part tranquille pendant que ça brûle."""
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "stop", "fleet_id": flotte_a_piloter["id"],
                          "reason": "ça part en vrille"})
    assert r.status_code == 200, r.text
    f = r.json()["fleet"]
    assert f["status"] == "stopping", "l'ordre est posé, la boucle ne l'a pas lu"
    assert f["stopping_at"] and not f["stopped_at"]
    assert f["stop_reason"] == "ça part en vrille", "la raison est ÉCRITE"


def test_l_ecart_entre_demande_et_effectif_est_le_diagnostic(client, org, flotte_a_piloter):
    """Un `stopping` qui ne devient jamais `stopped` désigne un ordonnanceur mort.
    Fondu dans un seul état, ce cas ressemblerait à un arrêt réussi.

    Aucun ordonnanceur ne tient cette campagne : c'est le sondage qui CONSTATE
    l'arrêt, quand plus aucune exécution ne tourne (21/09/2026 — l'accusé d'un
    ordonnanceur n'est plus reçu que de celui qui la tient)."""
    from oto_mcp import db
    assert flotte_a_piloter["id"] in db.accuser_arrets_effectifs(org["id"])
    f = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "get", "fleet_id": flotte_a_piloter["id"]}
                    ).json()["fleet"]
    assert f["status"] == "stopped" and f["stopped_at"]
    # et la raison posée à la demande SURVIT à l'accusé de réception
    assert f["stop_reason"] == "ça part en vrille"


def test_on_n_arme_pas_ce_qui_tourne_deja(client, org, flotte_a_piloter):
    """Relancer un passage en cours en ouvrirait un second sur la même cible."""
    client.post(ROUTE, headers=_h(org["membre"]),
                json={"op": "launch", "fleet_id": flotte_a_piloter["id"]})
    statut, code = _refus(client, org, {"op": "launch",
                                        "fleet_id": flotte_a_piloter["id"]})
    assert (statut, code) == (409, "not_launchable")


def test_sans_runner_joignable_launch_est_refuse_et_dit_ce_qui_reste(
        client, org, _un_worker_de_plateforme_sonde):
    """`no_runner_armed` est DÉCLARÉ sur `runner.fleets` : rejoué ici, sur la route.

    Le worker du module est vieilli au-delà de la fenêtre de présence le temps du
    banc — plus aucun runner joignable — puis rendu. Le refus doit dire QUOI
    FAIRE et ce qui RESTE ouvert : sinon on cherche un contournement."""
    from oto_mcp import db
    from oto_mcp.db._conn import _connect
    fid = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "sans-runner", "procedure": "p",
        "tools": ["oto_kb"]}).json()["fleet"]["id"]
    with _connect() as c:
        c.execute("UPDATE runner_platform_workers SET last_seen_at = NOW() - "
                  "make_interval(secs => %s) WHERE worker_sub = %s",
                  (db.ARME_FENETRE_S * 2, _un_worker_de_plateforme_sonde))
        c.commit()
    try:
        r = client.post(ROUTE, headers=_h(org["membre"]),
                        json={"op": "launch", "fleet_id": fid})
        assert (r.status_code, r.json().get("error")) == (400, "no_runner_armed"), r.text
        detail = r.json().get("detail", "")
        assert "rien n'exécute les automatisations" in detail and "worker" not in detail
        assert "s'arrête (`stop`)" in detail, "le refus nomme ce qui reste ouvert"
        f = client.post(ROUTE, headers=_h(org["membre"]),
                        json={"op": "get", "fleet_id": fid}).json()["fleet"]
        assert f["status"] == "draft", "un refus n'arme rien"
    finally:
        with _connect() as c:
            c.execute("UPDATE runner_platform_workers SET last_seen_at = NOW() "
                      "WHERE worker_sub = %s", (_un_worker_de_plateforme_sonde,))
            c.commit()


def test_on_n_arrete_pas_ce_qui_ne_tourne_pas(client, org):
    """Un refus qui NOMME l'état, plutôt qu'un 200 qui laisserait croire à un
    arrêt sur un passage jamais lancé."""
    r = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "jamais-lancee", "procedure": "p",
        "tools": ["oto_kb"],
        "max_tokens_per_row": 50_000})
    fid = r.json()["fleet"]["id"]
    assert _refus(client, org, {"op": "stop", "fleet_id": fid}) == (409, "not_stoppable")


@pytest.fixture(scope="module")
def simple_membre(client, org):
    """Un membre SANS le rôle admin — sans lui, le plancher n'est pas éprouvé :
    la fixture `org` crée son porteur en `org_admin`."""
    from oto_mcp import db, org_store
    sub = "usr_fleets_simple"
    db.upsert_user(sub, email=f"{sub}@fleets.invalid", name=sub)
    org_store.add_org_member(org["id"], sub, "org_member")
    org_store.set_active_org(sub, org["id"])
    return sub


def test_un_simple_membre_ne_LANCE_pas(client, org, simple_membre):
    """⚠️ Le plancher, éprouvé sur quelqu'un qui n'est PAS admin.

    Lancer engage une dépense et des écritures chez un tiers. Le refus dit aussi
    ce qui reste ouvert — un refus qui n'enseigne rien pousse à chercher un
    contournement."""
    r = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "plancher", "procedure": "p", "tools": ["oto_kb"],
        "max_tokens_per_row": 50_000})
    fid = r.json()["fleet"]["id"]
    rr = client.post(ROUTE, headers=_h(simple_membre),
                     json={"op": "launch", "fleet_id": fid})
    assert (rr.status_code, rr.json().get("error")) == (403, "org_admin_required")
    # La phrase du refus est servie sous `detail` (enveloppe REST), pas `message`.
    assert "ARRÊTER" in rr.json().get("detail", ""), (
        "un refus qui n'enseigne pas ce qui RESTE ouvert pousse à chercher un "
        "contournement — ici, que tout membre peut arrêter")


def test_un_simple_membre_ARRÊTE(client, org, simple_membre):
    """L'autre moitié, et c'est le choix de conception : un passage qui part en
    vrille doit pouvoir être stoppé par la première personne qui le voit, pas par
    celle qui a le bon rôle. Attendre un admin pendant qu'une flotte dépense est
    le mauvais échange."""
    r = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "arret-par-membre", "procedure": "p",
        "tools": ["oto_kb"],
        "max_tokens_per_row": 50_000})
    fid = r.json()["fleet"]["id"]
    assert client.post(ROUTE, headers=_h(org["membre"]),
                       json={"op": "launch", "fleet_id": fid}).status_code == 200
    rr = client.post(ROUTE, headers=_h(simple_membre),
                     json={"op": "stop", "fleet_id": fid, "reason": "vu passer"})
    assert rr.status_code == 200, rr.text
    assert rr.json()["fleet"]["status"] == "stopping"


# ── le droit par objet : modifier est au déclarant et aux admins de l'org ─────

def _declarer(client, sub: str, label: str) -> int:
    r = client.post(ROUTE, headers=_h(sub), json={
        "op": "create", "model": MODELE_SERVI, "label": label, "procedure": "p",
        "tools": ["oto_kb"], "max_tokens_per_row": 50_000})
    assert r.status_code == 200, r.text
    return r.json()["fleet"]["id"]


def test_un_simple_membre_ne_MODIFIE_pas_celle_d_un_autre(client, org, simple_membre):
    """Refusé sur la consigne comme sur les outils, et rien n'est écrit. La lire reste
    ouvert."""
    fid = _declarer(client, org["membre"], "a-l-admin")
    for champs in ({"label": "autre"}, {"input": "autre consigne"},
                   {"tools": ["oto_kb", "data_write"]}):
        rr = client.post(ROUTE, headers=_h(simple_membre),
                         json={"op": "update", "fleet_id": fid, **champs})
        assert (rr.status_code, rr.json().get("error")) == (403, "fleet_edit_forbidden")
    lu = client.post(ROUTE, headers=_h(simple_membre), json={"op": "get", "fleet_id": fid})
    assert lu.status_code == 200, lu.text
    assert (lu.json()["fleet"]["label"], lu.json()["fleet"]["tools"]) == ("a-l-admin", ["oto_kb"])


def test_le_declarant_modifie_la_sienne_et_l_admin_celle_d_un_autre(
        client, org, simple_membre):
    fid = _declarer(client, simple_membre, "au-membre")
    r = client.post(ROUTE, headers=_h(simple_membre),
                    json={"op": "update", "fleet_id": fid, "label": "par-le-declarant"})
    assert r.status_code == 200, r.text
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "update", "fleet_id": fid, "label": "par-l-admin"})
    assert r.status_code == 200, r.text
    assert r.json()["fleet"]["label"] == "par-l-admin"


def test_lancer_reste_aux_admins_meme_pour_le_declarant(client, org, simple_membre):
    """Le plancher de `launch` est plus haut que le droit par objet : il suit ce que le
    geste ENGAGE. Le déclarant non admin ne lance pas la sienne ; l'admin la lance."""
    fid = _declarer(client, simple_membre, "a-lancer")
    rr = client.post(ROUTE, headers=_h(simple_membre), json={"op": "launch", "fleet_id": fid})
    assert (rr.status_code, rr.json().get("error")) == (403, "org_admin_required")
    rr = client.post(ROUTE, headers=_h(org["membre"]), json={"op": "launch", "fleet_id": fid})
    assert rr.status_code == 200, rr.text


# ── LE CYCLE COMPLET : l'intention devient un fait, et par qui ───────────────
#
# ⚠️ C'est ce cycle qui rend `op=stop` RÉEL. Sans lui, l'arrêt est une écriture
# que personne ne lit — et `stopping` resterait éternellement `stopping`, ce qui
# est exactement le symptôme qu'on veut pouvoir DIAGNOSTIQUER.

@pytest.fixture(scope="module")
def flotte_cycle(client, org):
    r = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "cycle", "procedure": "p", "tools": ["oto_kb"],
        "max_tokens_per_row": 50_000})
    return r.json()["fleet"]


def test_le_cycle_armee_prise_arret_demande_arret_accuse(client, org, flotte_cycle):
    """Les quatre pas, dans l'ordre, et chacun par qui a le droit de le poser."""
    fid = flotte_cycle["id"]
    h = _h(org["membre"])

    armee = client.post(ROUTE, headers=h, json={"op": "launch", "fleet_id": fid}).json()["fleet"]
    assert armee["status"] == "armed" and not armee["started_at"]

    moi = "box-a/oto-fleet-cycle"
    prise = client.post(ROUTE, headers=h, json={"op": "take", "fleet_id": fid,
                                                "taken_by": moi}).json()["fleet"]
    assert prise["status"] == "running" and prise["started_at"], (
        "c'est l'ordonnanceur qui pose le FAIT `running`, en prenant la flotte")
    assert prise["taken_by"] == moi, "et il dit QUI la tient"

    demande = client.post(ROUTE, headers=h, json={
        "op": "stop", "fleet_id": fid, "reason": "budget"}).json()["fleet"]
    assert demande["status"] == "stopping", "l'ordre est posé, pas encore exécuté"

    # l'ordonnanceur LIT l'ordre en battant — c'est ce qui rend `stop` réel
    beat = client.post(ROUTE, headers=h, json={"op": "beat", "fleet_id": fid,
                                               "taken_by": moi}).json()
    assert beat["stop_requested"] is True

    acc = client.post(ROUTE, headers=h, json={"op": "ack_stop", "fleet_id": fid,
                                              "taken_by": moi}).json()["fleet"]
    assert acc["status"] == "stopped" and acc["stopped_at"]
    assert acc["stop_reason"] == "budget", "la raison de la DEMANDE survit à l'accusé"


def test_un_battement_sans_ordre_ne_dit_pas_qu_il_faut_s_arreter(client, org):
    """Le cas nominal doit être aussi net que le cas d'arrêt : un ordonnanceur qui
    lirait « arrête-toi » par défaut s'éteindrait en boucle."""
    fid = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "battement", "procedure": "p",
        "tools": ["oto_kb"],
        "max_tokens_per_row": 50_000}).json()["fleet"]["id"]
    client.post(ROUTE, headers=_h(org["membre"]), json={"op": "launch", "fleet_id": fid})
    _prendre(client, org, fid, "box-a/oto-fleet-battement")
    beat = client.post(ROUTE, headers=_h(org["membre"]),
                       json={"op": "beat", "fleet_id": fid,
                             "taken_by": "box-a/oto-fleet-battement"}).json()
    assert beat["stop_requested"] is False and beat["beat_taken"] is True


def test_deux_ordonnanceurs_ne_prennent_pas_la_meme_flotte(client, org):
    """⚠️ Le second doit l'APPRENDRE, pas partir en croyant l'avoir prise —
    sinon le passage double et son état ne dit la vérité pour aucun des deux.

    Depuis le 21/09/2026 le refus dit POURQUOI — un autre la TIENT — sans dire qui :
    `held_by_other` enseigne le geste (ne pas partir), la lecture dit le reste."""
    fid = _campagne_armee(client, org, "concurrence")
    assert _prendre(client, org, fid, "box-a/oto-fleet-a").status_code == 200
    r = _prendre(client, org, fid, "box-b/oto-fleet-b")
    assert (r.status_code, r.json().get("error")) == (409, "held_by_other"), r.text
    assert "box-a" not in r.text, "le refus ne nomme pas le preneur en place"
    f = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "get", "fleet_id": fid}).json()["fleet"]
    assert f["taken_by"] == "box-a/oto-fleet-a", "le refusé n'a rien écrit"


def test_on_ne_prend_qu_une_campagne_armee_ou_en_cours(client, org):
    """`not_takeable` rejoué : une campagne `draft` n'est demandée par personne."""
    fid = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "jamais-armee", "procedure": "p",
        "tools": ["oto_kb"],
        "max_tokens_per_row": 50_000}).json()["fleet"]["id"]
    assert _refus(client, org, {"op": "take", "fleet_id": fid,
                                "taken_by": "box-a/oto-fleet-draft"}
                  ) == (409, "not_takeable")


def test_on_n_accuse_pas_un_arret_qui_n_a_pas_ete_demande(client, org):
    """Un accusé sans demande effacerait la distinction : `stopped` ne voudrait
    plus dire « l'ordonnanceur a obéi » mais « quelqu'un a écrit stopped »."""
    fid = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "model": MODELE_SERVI, "label": "sans-demande", "procedure": "p",
        "tools": ["oto_kb"],
        "max_tokens_per_row": 50_000}).json()["fleet"]["id"]
    assert _refus(client, org, {"op": "ack_stop", "fleet_id": fid,
                                "taken_by": "box-a/oto-fleet-x"}
                  ) == (409, "nothing_to_acknowledge")


# ── QUI TIENT une campagne (21/09/2026) ──────────────────────────────────────
#
# `take` notait l'état sans noter l'auteur : un ordonnanceur qui redémarrait ne
# savait pas s'il reprenait SA campagne ou s'il en voyait une qu'un autre tenait,
# et oto-runner supposait une reprise — deux ordonnanceurs pouvaient conduire la
# même campagne. Le preneur est désormais écrit, lu, et comparé à chaque geste.

def _prendre(client, org, fid: int, preneur: str):
    return client.post(ROUTE, headers=_h(org["membre"]),
                       json={"op": "take", "fleet_id": fid, "taken_by": preneur})


def _campagne_armee(client, org, label: str) -> int:
    h = _h(org["membre"])
    fid = client.post(ROUTE, headers=h, json={
        "op": "create", "model": MODELE_SERVI, "label": label, "procedure": "p", "tools": ["oto_kb"],
        "max_tokens_per_row": 50_000}).json()["fleet"]["id"]
    assert client.post(ROUTE, headers=h,
                       json={"op": "launch", "fleet_id": fid}).status_code == 200
    return fid


def test_take_pose_le_preneur_et_get_comme_list_le_rendent(client, org):
    """Ce qu'un ordonnanceur qui redémarre LIT pour décider : sur la route, sérialisé."""
    fid = _campagne_armee(client, org, "preneur-lu")
    r = _prendre(client, org, fid, "box-a/oto-fleet-lu")
    assert r.status_code == 200, r.text
    assert r.json()["fleet"]["taken_by"] == "box-a/oto-fleet-lu"
    h = _h(org["membre"])
    get = client.post(ROUTE, headers=h, json={"op": "get", "fleet_id": fid}).json()["fleet"]
    assert get["taken_by"] == "box-a/oto-fleet-lu"
    carte = next(c for c in client.post(ROUTE, headers=h, json={"op": "list"}
                                        ).json()["fleets"] if c["id"] == fid)
    assert carte["taken_by"] == "box-a/oto-fleet-lu"


def test_la_reprise_par_le_MEME_preneur_reussit_et_ne_redemarre_rien(client, org):
    """L'ordonnanceur a redémarré : il reprend SA campagne, sans refus à contourner,
    et le passage ne recommence pas pour autant."""
    fid = _campagne_armee(client, org, "reprise")
    premiere = _prendre(client, org, fid, "box-a/oto-fleet-reprise").json()["fleet"]
    r = _prendre(client, org, fid, "box-a/oto-fleet-reprise")
    assert r.status_code == 200, r.text
    f = r.json()["fleet"]
    assert f["status"] == "running" and f["taken_by"] == "box-a/oto-fleet-reprise"
    assert f["started_at"] == premiere["started_at"], "une reprise ne redémarre pas"


def test_un_geste_d_ordonnanceur_sans_preneur_est_refuse(client, org):
    """Un geste sans auteur est ce que ce lot existe pour empêcher — et le refus
    n'écrit rien."""
    fid = _campagne_armee(client, org, "sans-preneur")
    for op in ("take", "beat", "ack_stop"):
        assert _refus(client, org, {"op": op, "fleet_id": fid}) == (400, "missing_fields"), op
        assert _refus(client, org, {"op": op, "fleet_id": fid, "taken_by": "  "}
                      ) == (400, "missing_fields"), op
    f = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "get", "fleet_id": fid}).json()["fleet"]
    assert f["status"] == "armed" and f["taken_by"] is None


def test_un_AUTRE_ne_bat_ni_n_accuse_a_la_place_du_preneur(client, org):
    """Un battement d'autrui ferait passer pour vivant un preneur mort ; un accusé
    d'autrui annoncerait un arrêt que le preneur n'a pas exécuté."""
    h = _h(org["membre"])
    fid = _campagne_armee(client, org, "battu-par-un-autre")
    assert _prendre(client, org, fid, "box-a/oto-fleet-tenue").status_code == 200
    assert _refus(client, org, {"op": "beat", "fleet_id": fid,
                                "taken_by": "box-b/oto-fleet-intrus"}
                  ) == (409, "not_the_holder")
    beat = client.post(ROUTE, headers=h, json={
        "op": "beat", "fleet_id": fid, "taken_by": "box-a/oto-fleet-tenue"}).json()
    assert beat["beat_taken"] is True
    assert client.post(ROUTE, headers=h, json={
        "op": "stop", "fleet_id": fid, "reason": "fin"}).status_code == 200
    assert _refus(client, org, {"op": "ack_stop", "fleet_id": fid,
                                "taken_by": "box-b/oto-fleet-intrus"}
                  ) == (409, "not_the_holder")
    acc = client.post(ROUTE, headers=h, json={
        "op": "ack_stop", "fleet_id": fid, "taken_by": "box-a/oto-fleet-tenue"})
    assert acc.status_code == 200, acc.text
    assert acc.json()["fleet"]["status"] == "stopped"


def test_une_campagne_demarree_par_le_sondage_se_prend_une_seule_fois(client, org):
    """Le sondage des workers démarre une campagne sans ordonnanceur (`taken_by`
    NULL) : le premier qui la prend la tient, le suivant l'apprend."""
    from oto_mcp import db
    fid = _campagne_armee(client, org, "demarree-par-le-sondage")
    db.marquer_demarree(fid)
    f = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "get", "fleet_id": fid}).json()["fleet"]
    assert f["status"] == "running" and f["taken_by"] is None
    assert _prendre(client, org, fid, "box-a/oto-fleet-sonde").status_code == 200
    r = _prendre(client, org, fid, "box-b/oto-fleet-sonde")
    assert (r.status_code, r.json().get("error")) == (409, "held_by_other"), r.text


def test_rearmer_libere_la_campagne_et_l_ancien_preneur_l_apprend(client, org):
    """Le chemin quand le preneur est mort : arrêter, laisser constater l'arrêt,
    réarmer. Le réarmement libère ; l'ancien preneur, s'il revit, l'apprend à son
    battement au lieu de conduire un passage qui n'est plus le sien."""
    from oto_mcp import db
    h = _h(org["membre"])
    fid = _campagne_armee(client, org, "preneur-mort")
    assert _prendre(client, org, fid, "box-a/oto-fleet-mort").status_code == 200
    client.post(ROUTE, headers=h, json={"op": "stop", "fleet_id": fid,
                                        "reason": "preneur mort"})
    assert fid in db.accuser_arrets_effectifs(org["id"])
    rearmee = client.post(ROUTE, headers=h, json={"op": "launch", "fleet_id": fid})
    assert rearmee.status_code == 200, rearmee.text
    assert rearmee.json()["fleet"]["taken_by"] is None
    assert _prendre(client, org, fid, "box-b/oto-fleet-relais").status_code == 200
    assert _refus(client, org, {"op": "beat", "fleet_id": fid,
                                "taken_by": "box-a/oto-fleet-mort"}
                  ) == (409, "not_the_holder")


# ── ouverte à toute org : plus de porte bêta (24/09/2026) ─────────────────────

@pytest.fixture(scope="module")
def org_sans_beta(live):
    from oto_mcp import db, org_store
    membre = "usr_fleets_pas_beta"
    db.upsert_user(membre, email=f"{membre}@fleets.invalid", name=membre)
    oid = org_store.create_org("Org sans bêta", created_by=membre)
    org_store.add_org_member(oid, membre, "org_admin")
    org_store.set_active_org(membre, oid)
    return {"id": oid, "membre": membre}


def test_sans_option_beta_la_route_SERT(client, org_sans_beta):
    """Les agents hébergés s'ouvrent à toute org (24/09/2026) : un admin d'org sans
    l'option `beta` liste et déclare. Ce qui borne la dépense est le modèle
    obligatoire et la clé de modèle de l'org, plus une population choisie."""
    h = _h(org_sans_beta["membre"])
    r = client.post(ROUTE, headers=h, json={"op": "list"})
    assert r.status_code == 200, r.text
    r = client.post(ROUTE, headers=h, json={
        "op": "create", "model": MODELE_SERVI, "label": "x", "procedure": "p",
        "tools": ["oto_kb"]})
    assert r.status_code == 200, r.text


def test_api_me_orgs_dit_par_org_si_le_compte_est_beta(client, org, org_sans_beta):
    """Le front lit l'org de l'URL : le fait doit être PAR ORG, sur la liste — pas
    global sur /api/me. Deux comptes, deux orgs, deux réponses. (Le champ ne décide
    plus des Agents, ouverts à toute org, mais des autres surfaces bêta.)"""
    from oto_mcp import db
    db.set_option_comp("org", str(org["id"]), "beta", granted_by="test")
    r = client.get("/api/me/orgs", headers=_h(org["membre"]))
    assert r.status_code == 200, r.text
    assert {o["id"]: o["beta"] for o in r.json()["orgs"]}[org["id"]] is True
    r = client.get("/api/me/orgs", headers=_h(org_sans_beta["membre"]))
    assert r.status_code == 200, r.text
    assert {o["id"]: o["beta"] for o in r.json()["orgs"]}[org_sans_beta["id"]] is False


# ── LE MODÈLE d'un passage : il part avec ses travaux (12/09/2026) ────────────
#
# Rejoués sur la route parce qu'ils sont DÉCLARÉS. En fin de fichier à dessein :
# le dernier banc pose une présence Anthropic dans la base du module, et aucun
# banc au-dessus ne doit en dépendre.

def test_un_modele_hors_catalogue_est_refuse_a_la_declaration(client, org):
    base = {"op": "create", "model": MODELE_SERVI, "label": "x", "procedure": "p", "tools": ["oto_kb"]}
    assert _refus(client, org, {**base, "model": "un-modele-libre"}
                  ) == (400, "invalid_model")
    assert _refus(client, org, {**base, "provider": "openai"}
                  ) == (400, "invalid_model")


def test_armer_un_passage_dont_personne_ne_sert_le_modele_est_refuse(client, org):
    """Sans ce refus, le passage passerait `running` au premier travail produit —
    que le claim filtre — et n'avancerait plus jamais, sans une erreur."""
    from oto_mcp.db._conn import _connect
    fid = client.post(ROUTE, headers=_h(org["membre"]), json={
        "op": "create", "label": "opus", "procedure": "p", "tools": ["oto_kb"],
        "model": "claude-opus-5"}).json()["fleet"]["id"]
    assert _refus(client, org, {"op": "launch", "fleet_id": fid}
                  ) == (400, "model_not_served")

    # Un worker de plateforme sert désormais la famille : l'armement passe.
    with _connect() as c:
        c.execute("INSERT INTO runner_platform_depots (worker_sub, depot) "
                  "VALUES ('worker:banc-flottes', 'anthropic')")
        c.commit()
    r = client.post(ROUTE, headers=_h(org["membre"]),
                    json={"op": "launch", "fleet_id": fid})
    assert r.status_code == 200, r.text
    assert r.json()["fleet"]["status"] == "armed"
