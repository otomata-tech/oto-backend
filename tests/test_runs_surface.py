"""Ce que l'écran « Runs » demande à la file et aux passages, et que ni l'une ni
les autres n'écrivaient.

Deux manques, tous deux découverts en dessinant l'écran plutôt qu'en lisant le
code — et tous deux du même genre : une donnée qui n'est vraie qu'à un instant
et que personne ne notait.

1. **Le DÉNOMINATEUR d'un passage.** « 1 240 lignes faites » ne se lit pas sans
   « sur 2 000 ». Le compte des lignes visées n'est vrai qu'à l'armement : dès la
   première ligne traitée, les agents le font baisser. Le reconstituer après coup
   est impossible — et diviser par `max_rows` (un PLAFOND) a déjà produit un coût
   par ligne faux d'un facteur 46 sur un passage de démo.
2. **D'où vient un travail.** La file mélange trois origines et ne les
   distinguait qu'implicitement. Trier côté client ne marche PAS : la file est
   paginée sur `id DESC` et un passage de 2 000 lignes remplit la première page à
   lui seul, donc « programmé » rendrait VIDE sur une org qui joue un digest
   chaque matin. Un écran qui ment sur une absence est pire que pas d'écran.
"""
from __future__ import annotations

import pytest

from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities import runner_jobs as RJ
from oto_mcp.capabilities._types import ResolvedCtx
from oto_mcp.db import runner_jobs as JOBS


@pytest.fixture(autouse=True)
def _compte_beta(monkeypatch):
    """Les passages sont une surface BÊTA (#818) : sans l'option, la capacité
    refuse avant d'atteindre le verbe. Ces tests parlent du dénominateur et de
    l'origine d'un travail, pas de la porte — ils l'ouvrent."""
    monkeypatch.setattr(RF.access, "has_option", lambda sub, option, *, org=None: True)


def _ctx(sub="alexis", org_id=2):
    return ResolvedCtx(sub=sub, org_id=org_id)


# ── le dénominateur ───────────────────────────────────────────────────────────

def test_armer_compte_les_lignes_visees_et_les_passe_a_la_transition(monkeypatch):
    """Le compte est lu AU MOMENT d'armer, avec le filtre figé de la flotte."""
    vus = {}
    monkeypatch.setattr(RF.db, "get_fleet", lambda fid, oid: {
        "id": fid, "namespace": "prospects", "row_filter": {"statut": "a_traiter"}})

    class _Store:
        def count_rows(self, ns, *, filter=None):
            vus["ns"], vus["filter"] = ns, filter
            return 2000

    monkeypatch.setattr("oto_mcp.datastore.core.make_store", lambda sub: _Store())
    monkeypatch.setattr(RF.db, "armer", lambda fid, oid, rows_at_launch=None:
                        vus.setdefault("rows", rows_at_launch) or {"id": fid, "status": "armed"})
    monkeypatch.setattr("oto_mcp.roles.is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)

    RF._fleets(_ctx(), RF.FleetInput(op="launch", fleet_id=7))
    assert vus["ns"] == "prospects"
    assert vus["filter"] == {"statut": "a_traiter"}
    assert vus["rows"] == 2000


def test_une_table_illisible_n_empeche_PAS_d_armer(monkeypatch):
    """Fail-OPEN : le passage part avec un dénominateur inconnu (`None`), jamais
    zéro — un zéro se lirait « la table est vide », ce qui est une autre affirmation.
    Refuser ici transformerait un défaut d'affichage en panne de lancement."""
    monkeypatch.setattr(RF.db, "get_fleet", lambda fid, oid: {
        "id": fid, "namespace": "table-supprimee", "row_filter": None})

    class _Store:
        def count_rows(self, ns, *, filter=None):
            raise RuntimeError("namespace inconnu")

    monkeypatch.setattr("oto_mcp.datastore.core.make_store", lambda sub: _Store())
    recu = {}
    monkeypatch.setattr(RF.db, "armer", lambda fid, oid, rows_at_launch=None:
                        recu.update(rows=rows_at_launch) or {"id": fid, "status": "armed"})
    monkeypatch.setattr("oto_mcp.roles.is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)

    out = RF._fleets(_ctx(), RF.FleetInput(op="launch", fleet_id=7))
    assert out["fleet"]["status"] == "armed"
    assert recu["rows"] is None


def test_une_flotte_sans_cible_arme_sans_denominateur(monkeypatch):
    monkeypatch.setattr(RF.db, "get_fleet", lambda fid, oid: {"id": fid, "namespace": None})
    recu = {}
    monkeypatch.setattr(RF.db, "armer", lambda fid, oid, rows_at_launch=None:
                        recu.update(rows=rows_at_launch) or {"id": fid, "status": "armed"})
    monkeypatch.setattr("oto_mcp.roles.is_org_admin", lambda sub, org: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    RF._fleets(_ctx(), RF.FleetInput(op="launch", fleet_id=7))
    assert recu["rows"] is None


def test_le_denominateur_est_SERVI_et_n_est_pas_une_borne():
    """`rows_at_launch` (un compte) et `max_rows` (un plafond) coexistent : les
    confondre est précisément le défaut qu'on corrige."""
    champs = RF.Fleet.model_fields
    assert "rows_at_launch" in champs and "max_rows" in champs
    assert champs["rows_at_launch"].default is None
    assert "rows_at_launch" not in RF.db.CHAMPS_MODIFIABLES, (
        "le dénominateur se pose en armant, jamais par une retouche de configuration")


# ── l'origine d'un travail ────────────────────────────────────────────────────

def test_les_trois_origines_partitionnent_la_file():
    """Tout travail tombe dans exactement une origine — sinon un onglet en perd."""
    assert set(JOBS._SOURCES) == {"batch", "scheduled", "manual"}
    assert JOBS._SOURCES["batch"] == "fleet_id IS NOT NULL"
    for s in ("scheduled", "manual"):
        assert "fleet_id IS NULL" in JOBS._SOURCES[s]
    assert "IS NOT NULL" in JOBS._SOURCES["scheduled"]
    assert "IS NULL" in JOBS._SOURCES["manual"].split("payload->>'trigger_id'")[1]


@pytest.mark.parametrize("source", ["batch", "scheduled", "manual"])
def test_le_filtre_dorigine_entre_dans_le_WHERE_commun(source):
    ou, params = JOBS._filtre_de_file(196, None, source)
    assert JOBS._SOURCES[source] in ou
    assert params == [196]


def test_une_origine_inconnue_est_REFUSEE_et_ne_rend_pas_toute_la_file():
    """Un filtre inconnu doit lever, pas se taire : ignorer silencieusement une
    valeur inattendue rendrait la file ENTIÈRE sous une étiquette qui promet un
    sous-ensemble."""
    with pytest.raises(ValueError):
        JOBS._filtre_de_file(196, None, "batches")


def test_la_page_et_son_total_partagent_le_MEME_filtre():
    """Servir un filtre à la page sans l'appliquer au compte redonne un total qui
    décrit une autre population que les lignes affichées."""
    import inspect
    for fn in (JOBS.list_jobs, JOBS.count_jobs):
        p = inspect.signature(fn).parameters
        assert "source" in p and "fleet_id" in p, fn.__name__


def test_le_filtre_dorigine_se_compose_avec_le_statut_et_la_flotte():
    ou, params = JOBS._filtre_de_file(196, "failed", "scheduled", fleet_id=None)
    assert "status = %s" in ou and JOBS._SOURCES["scheduled"] in ou
    assert params == [196, "failed"]
    ou, params = JOBS._filtre_de_file(196, None, None, fleet_id=12)
    assert "fleet_id = %s" in ou and params == [196, 12]


def test_la_source_est_declaree_sur_le_contrat_servi():
    champ = RJ.JobsInput.model_fields["source"]
    assert champ.default is None
    assert "scheduled" in str(champ.annotation)


def test_le_filtre_par_declencheur_lit_le_payload_et_entre_dans_le_total():
    """`trigger_id` n'a pas de colonne (le tick le pose dans le payload) ;
    demandé par Alexis au même titre que `fleet_id` : un historique trié côté
    client donne un total qui ne sert pas de dénominateur."""
    ou, params = JOBS._filtre_de_file(196, None, None, fleet_id=None, trigger_id=14)
    assert "payload->>'trigger_id'" in ou and params == [196, "14"]
    import inspect
    for fn in (JOBS.list_jobs, JOBS.count_jobs):
        assert "trigger_id" in inspect.signature(fn).parameters, fn.__name__
    assert "trigger_id" in RJ.JobsInput.model_fields


def test_le_filtre_par_declencheur_ne_CASTE_pas_le_payload():
    """⚠️ Le filtre a d'abord été écrit `(payload->>'trigger_id')::bigint = %s`.

    `payload` est un JSON libre : il suffit d'UNE ligne de l'org dont `trigger_id` n'est
    pas un nombre pour que le cast fasse échouer la requête ENTIÈRE — pas seulement
    cette ligne-là. Le filtre deviendrait une panne, sur des données qu'aucun de nos
    écrivains ne produit aujourd'hui mais que rien n'empêche d'exister : un payload est
    précisément l'endroit où l'on met ce qu'on n'a pas modélisé.

    La forme sûre vivait déjà deux fonctions plus haut dans le même fichier
    (`perimer_travaux_du_declencheur`, `comptage_perime`) : même clé, même lecture. Ce
    banc tient les TROIS d'accord, parce que c'est la divergence qui a produit le
    défaut — pas l'ignorance de la bonne forme."""
    import inspect
    ou, params = JOBS._filtre_de_file(196, None, None, fleet_id=None, trigger_id=14)
    assert "::bigint" not in ou, (
        "un cast sur une clé de payload libre : une seule ligne non numérique dans "
        "l'org fait tomber la requête entière")
    assert params == [196, "14"], "la comparaison est textuelle des deux côtés"
    # Et les trois lectures de cette clé restent d'accord.
    src = inspect.getsource(JOBS)
    assert src.count("payload->>'trigger_id')::bigint") == 0
    assert src.count("payload->>'trigger_id' = %s") >= 3
