"""Flottes du runner — la cible figée, le périmètre orphelin, et le vide qui se dit.

Trois familles, et chacune vient d'un incident payé pendant la mise au point du
chantier, pas d'une précaution abstraite :

1. **La cible ne se modifie pas.** Rediriger un passage en vol vers un autre
   tableau est exactement le geste que la déclaration existe pour empêcher — et il
   compte double depuis que les bancs d'essai et la production d'un client ne
   vivent plus dans la même org.
2. **Un périmètre sans tableau est refusé**, et le refus le NOMME : un `row_filter`
   seul donne l'illusion d'un passage borné alors qu'il n'a aucune cible à opposer.
3. **Un passage sans travail le DIT.** Des compteurs à zéro se lisent « rien ne
   s'est passé » ; `aucun_travail_rattache` distingue le vide constaté du vide
   supposé — le défaut qui a coûté le plus cher sur ce chantier.
"""
from __future__ import annotations

import inspect

import pytest

from oto_mcp.capabilities import runner_fleets as RF
from oto_mcp.capabilities._types import AuthzDenied, ResolvedCtx
from oto_mcp.capabilities.registry import CAPABILITIES


@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    """Ce fichier ne parle pas de la garde de clé de modèle — elle a son propre banc
    (`test_cle_de_modele_exigee.py`). Le réglage est lu ÉTEINT, comme sur toute
    plateforme qui ne l'a pas allumé : sans cette doublure, la lecture irait
    chercher la vraie base et chaque banc tomberait sur une raison qui n'est pas
    la sienne."""
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)
    # Ni routée vers la ferme : sa garde de pose a son banc (`test_cle_de_modele_exigee`).
    monkeypatch.setattr("oto_mcp.capabilities._cle_exigee.org_routee_ferme",
                        lambda org_id: False)



@pytest.fixture(autouse=True)
def _un_worker_par_defaut(monkeypatch):
    """`launch` refuse d'armer un passage sans worker vivant (oto-runner#13,
    17/09/2026) — ce fichier ne parle pas de cette absence par défaut, elle a
    son propre banc plus bas (`test_runner_fleets_sans_worker.py`). Sans cette
    doublure, chaque `launch` irait chercher la vraie base."""
    monkeypatch.setattr(RF.db, "runner_arme", lambda org: {
        "armed": True, "workers": 1, "last_seen": "2026-09-17 08:00:00",
        "families": ["anthropic"]})


def _ctx(sub="alexis", org_id=2):
    return ResolvedCtx(sub=sub, org_id=org_id)


#: Un agent hébergé déclare son modèle (24/09/2026) — ce fichier ne parle pas de
#: cette règle, elle a son propre banc (`test_agent_heberge_modele_obligatoire.py`).
MODELE = "claude-sonnet-5"


def _appel(ctx, **kw):
    if kw.get("op") == "create":
        kw.setdefault("model", MODELE)
    return RF._fleets(ctx, RF.FleetInput(**kw))


# ── la cible est figée à la déclaration ───────────────────────────────────────

def test_update_refuse_de_deplacer_la_cible_et_dit_pourquoi():
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="update", fleet_id=1, namespace="un-autre-tableau")
    assert e.value.code == "target_is_frozen"
    assert "autre automatisation" in e.value.message


def test_update_refuse_aussi_de_deplacer_le_seul_perimetre():
    """Le périmètre est la moitié de la cible : le restreindre en vol change ce que
    le passage touche, sans changer le tableau — donc sans que ça se voie."""
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="update", fleet_id=1, row_filter={"lot": "autre"})
    assert e.value.code == "target_is_frozen"


def test_la_cible_ne_figure_pas_parmi_les_champs_modifiables():
    """La garde du haut refuse ; celle-ci vérifie qu'aucun autre chemin ne passe."""
    from oto_mcp import db
    assert "namespace" not in db.CHAMPS_MODIFIABLES
    assert "row_filter" not in db.CHAMPS_MODIFIABLES


# ── un périmètre sans tableau est refusé ──────────────────────────────────────

def test_un_perimetre_sans_tableau_est_refuse_et_le_refus_le_nomme():
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="create", label="essai", procedure="p", tools=["oto_kb"],
               row_filter={"lot": "essai-1"})
    assert e.value.code == "target_incomplete"
    assert "namespace" in e.value.message


def test_une_flotte_sans_cible_du_tout_reste_possible():
    """Toutes les flottes n'écrivent pas dans un tableau — seule l'incohérence
    « périmètre sans cible » est refusée, pas l'absence de cible."""
    inp = RF.FleetInput(op="create", label="veille", procedure="p", tools=["oto_kb"])
    assert inp.namespace is None and inp.row_filter is None


def test_create_exige_le_nom_la_procedure_et_les_outils():
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="create", label="essai")
    assert e.value.code == "missing_fields"
    assert "procedure" in e.value.message and "tools" in e.value.message


# ── le vide se déclare ────────────────────────────────────────────────────────

def test_l_etat_declare_le_vide_au_lieu_de_rendre_des_zeros():
    """`aucun_travail_rattache` est un champ REQUIS du contrat : un lecteur ne peut
    pas confondre « aucun travail » avec « des travaux tous à zéro »."""
    champs = RF.FleetState.model_fields
    assert champs["no_jobs_attached"].is_required()
    assert champs["jobs_total"].is_required()
    # les compteurs de détail, eux, ont le droit d'être absents
    assert not champs["pending"].is_required()


def test_l_etat_porte_la_file_que_voit_l_ordonnanceur_et_pourquoi_elle_manque(monkeypatch):
    """Décision du 14/09/2026 : le superviseur lit ce que l'ordonnanceur voit. Sans
    le motif, un `null` ne dirait pas si la campagne ne vise rien ou si le compte a
    échoué."""
    from oto_mcp import db
    flotte = {"id": 7, "org_id": 2, "sub": "alexis", "namespace": "t",
              "row_filter": {"passe": "2"}}
    monkeypatch.setattr(db, "fleet_state", lambda fid, org: {
        "fleet": dict(flotte), "state": {"jobs_total": 3, "no_jobs_attached": False}})
    # Les lignes ont leur banc, sur une vraie base (`test_etat_de_flotte_lignes_db.py`).
    monkeypatch.setattr(RF._lignes_de_campagne, "pour_le_superviseur",
                        lambda f: {"rows": None, "rows_unavailable": "no_table"})
    for comptes, attendu in (({7: 12}, (12, None)), ({7: None}, (None, "no_table")),
                             ({}, (None, "count_failed"))):
        monkeypatch.setattr(RF._lignes_reservables, "lignes_reservables",
                            lambda _c, comptes=comptes: comptes)
        rendu = _appel(_ctx(), op="state", fleet_id=7)
        etat = RF.FleetOut.model_validate(rendu).state
        assert (etat.reservable_rows, etat.reservable_rows_unavailable) == attendu


def test_l_issue_des_travaux_est_declaree_au_contrat_et_facultative():
    champs = RF.FleetState.model_fields
    for nom in ("empty_jobs", "stopped_after_write", "reservation_unmeasured",
                "usage_unknown", "reservable_rows", "reservable_rows_unavailable"):
        assert nom in champs and not champs[nom].is_required(), nom


# ── ce que l'agent lit des outils : déclaré, validé, figé (oto#241) ──────────

def _creer(monkeypatch, **kw):
    from oto_mcp import db
    vu = {}
    monkeypatch.setattr(db, "create_fleet", lambda *a, **k: vu.update(k) or {"id": 1})
    _appel(_ctx(), op="create", label="essai", procedure="p",
           tools=["data_claim_next", "data_rows", "data_write"], **kw)
    return vu


def test_descriptions_outils_valide_part_a_la_creation(monkeypatch):
    reglage = {"defaut": 1024, "entieres": ["data_write", "data_claim_next", "data_rows"]}
    assert _creer(monkeypatch, descriptions_outils=reglage)["descriptions_outils"] == reglage


def test_sans_descriptions_outils_rien_ne_part(monkeypatch):
    assert _creer(monkeypatch)["descriptions_outils"] is None


@pytest.mark.parametrize("fautif", [
    {"defaut": "1024"},                            # un texte n'est pas un entier
    {"defaut": 0},                                 # borne nulle
    {"defaut": True},                              # un booléen n'est pas un entier
    {"entieres": "data_rows"},                     # pas une liste
    {"defaut": 1024, "coupees": ["data_rows"]},    # clé inconnue
    {"entieres": ["serper_scrape"]},               # outil hors de `tools`
])
def test_descriptions_outils_fautif_est_refuse_et_nomme(monkeypatch, fautif):
    with pytest.raises(AuthzDenied) as e:
        _creer(monkeypatch, descriptions_outils=fautif)
    assert e.value.code == "invalid_descriptions_outils"


def test_descriptions_outils_ne_se_modifie_pas():
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="update", fleet_id=1, descriptions_outils={"defaut": 2048})
    assert e.value.code == "context_is_frozen"


def test_retirer_de_tools_un_outil_que_le_reglage_nomme_est_refuse(monkeypatch):
    from oto_mcp import db
    monkeypatch.setattr(db, "get_fleet", lambda fid, org: {
        "id": fid, "sub": "alexis",
        "descriptions_outils": {"entieres": ["data_rows", "data_write"]}})
    monkeypatch.setattr(db, "update_fleet", lambda fid, org, champs: {"id": fid, **champs})

    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="update", fleet_id=1, tools=["data_write", "data_claim_next"])
    assert e.value.code == "invalid_descriptions_outils"
    assert "data_rows" in e.value.message

    rendu = _appel(_ctx(), op="update", fleet_id=1,
                   tools=["data_rows", "data_write", "fr_get"])
    assert rendu["fleet"]["tools"] == ["data_rows", "data_write", "fr_get"]


def test_aucun_modele_servi_ne_porte_de_monnaie():
    """Les tarifs changent et diffèrent par fournisseur : une valeur monétaire figée
    en base devient fausse sans que rien ne le dise. Et un NUMERIC servi tel quel
    n'est même pas sérialisable en JSON — la flotte serait illisible dès qu'elle
    porte une borne.

    ⚠️ Ce test a d'abord porté sur `FleetState` SEUL, qui était propre : il passait
    au vert pendant que `Fleet`, l'autre modèle du même fichier, servait un
    `max_cost_usd`. Un test qui vise le bon principe et le mauvais objet est vert et
    inutile. Il balaie maintenant TOUS les modèles servis."""
    monnaie = ("usd", "cost", "euro", "eur", "price", "prix")
    for modele in (RF.Fleet, RF.FleetCard, RF.FleetState, RF.FleetInput, RF.FleetOut):
        fautifs = [c for c in modele.model_fields if any(m in c for m in monnaie)]
        assert not fautifs, f"{modele.__name__} porte de la monnaie : {fautifs}"
    assert "usage_tokens" in RF.FleetState.model_fields
    assert "max_tokens" in RF.Fleet.model_fields


def test_le_contexte_dexecution_est_fige_comme_la_cible():
    """Changer le modèle en vol rend FAUSSE l'attribution des lignes déjà écrites
    sous le passage — exactement l'argument qui gèle la cible."""
    for champ, valeur in (("provider", "openai"), ("model", "un-autre-modele")):
        with pytest.raises(AuthzDenied) as e:
            _appel(_ctx(), op="update", fleet_id=1, **{champ: valeur})
        assert e.value.code == "context_is_frozen"
    from oto_mcp import db
    assert "provider" not in db.CHAMPS_MODIFIABLES
    assert "model" not in db.CHAMPS_MODIFIABLES


def test_status_ne_se_pose_pas_par_update_et_le_refus_le_dit():
    """`status` figure dans l'entrée parce qu'il FILTRE `list`. Le laisser tomber en
    silence rendrait 200 avec la flotte inchangée — et c'est précisément ce qu'un
    agent privé de `stop` tenterait, en lisant un succès dans la réponse."""
    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="update", fleet_id=1, status="stopped")
    assert e.value.code == "status_not_settable"
    from oto_mcp import db
    assert "status" not in db.CHAMPS_MODIFIABLES


# ── la capacité est bien servie, et sur les deux faces ────────────────────────

def test_la_capacite_est_servie_sur_les_deux_faces():
    cap = [c for c in CAPABILITIES if c.key == "runner.fleets"]
    assert len(cap) == 1, "la capacité `runner.fleets` doit être enregistrée une fois"
    cap = cap[0]
    assert cap.mcp == "oto_fleet", (
        "une flotte est de la CONFIG utilisateur : elle se pose et se lit en "
        "conversation, contrairement à la file de jobs qui est worker-only")
    assert cap.rest.path == "/api/me/runner/fleets"


def test_lancer_et_arreter_sont_servis_avec_des_PLANCHERS_DIFFÉRENTS():
    """La décision du 01/09/2026, et elle remplace celle que ce fichier gravait.

    Ces tests disaient « ni lancer ni arrêter ne sont servis ici — ils entreront
    ensemble, quand la question de qui a le droit sera tranchée ». Elle l'est, et
    la réponse **casse la symétrie** que je supposais :

        lancer   effets externes IRRÉVERSIBLES (argent dépensé, lignes écrites
                 chez un tiers)          ⟹ plancher ADMIN
        arrêter  une interruption et un travail à reprendre
                 ⟹ tout MEMBRE — un passage qui part en vrille doit pouvoir
                   être stoppé par la première personne qui le voit

    ⚠️ Ils entrent bien par la même porte, mais **ils n'engagent pas la même
    chose** : la garde suit le VERBE, pas l'objet. Attendre un admin pendant
    qu'une flotte dépense est le mauvais échange.
    """
    ops = set(RF.FleetInput.model_fields["op"].annotation.__args__)
    assert {"list", "get", "state", "launch", "stop"} <= ops
    # le plancher d'`ORG_MEMBER` reste celui de la capacité : c'est `launch` qui
    # exige davantage, DANS le handler — le vérifier ici garde la trace que la
    # différence est voulue et non un oubli d'autz.
    src = inspect.getsource(RF._fleets)
    assert "is_org_admin" in src, "`launch` doit exiger l'admin"
    assert src.index("op == \"launch\"") < src.index("is_org_admin"), (
        "la vérification admin appartient à la branche `launch`, pas à toute la "
        "capacité — sinon `stop` deviendrait admin par effet de bord")


def test_un_deroule_ne_lance_pas_et_n_arrete_pas_LE_SIEN():
    """Les deux gardes anti-agent, et elles DIFFÈRENT parce que le coût diffère.

    ⚠️ Un agent qui se RELANCE lui-même dépense en boucle ; un agent qui arrête
    de trop coûte une reprise. D'où : aucun déroulé ne lance, mais un déroulé peut
    arrêter une AUTRE flotte de son org — c'est même le cas utile, un opérateur
    qui pilote par la conversation. **Fermer le verbe à tout le monde pour le
    seul cas dangereux ferait payer le prix sur tous les usages légitimes.**
    """
    src = inspect.getsource(RF._fleets)
    assert "not_from_a_run" in src, "un déroulé ne lance pas"
    assert "not_your_own_fleet" in src, "un déroulé n'arrête pas celle qui l'exécute"
    from oto_mcp import db
    assert callable(db.run_appartient_a_flotte), (
        "la garde repose sur un PRÉDICAT — « ce déroulé tourne-t-il pour CETTE "
        "flotte ? » — et non sur la seule présence d'un run")


def test_lancer_et_arreter_posent_une_INTENTION_jamais_un_fait():
    """⚠️ Le défaut que ce lot a failli commettre, après une journée passée sur
    « trois états, jamais deux » — la leçon ne s'est pas reconnue sur cet objet.

    `launch` pose `armed` (on a DEMANDÉ), pas `running` (un ordonnanceur l'a
    PRISE). `stop` pose `stopping` (l'ordre est posé), pas `stopped` (la boucle a
    accusé réception). Entre l'appel et la lecture, **le passage continue de
    dépenser** : annoncer un arrêt qui n'a pas eu lieu est pire qu'annoncer un
    lancement qui ne part pas.
    """
    src = inspect.getsource(RF._fleets)
    assert "db.armer(" in src and "'running'" not in src.split("op == \"launch\"")[1][:900]
    assert "db.demander_arret(" in src


def test_une_flotte_est_org_scopee():
    with pytest.raises(AuthzDenied) as e:
        _appel(ResolvedCtx(sub="alexis", org_id=None), op="list")
    assert e.value.code == "org_required"


def test_la_description_servie_dit_que_launch_ARME_et_ne_demarre_rien():
    """⚠️ Ce test existe parce que le NOM DU VERBE induit en erreur tout seul.

    « launch » invite à écrire « lance ». Trois personnes l'ont annoncé de travers
    le 02/09/2026 — dont un message de tag **immuable**, qui restera faux dans
    l'historique. Et une description d'outil est relue à CHAQUE appel par un
    modèle qui, lui aussi, lira « launch » et conclura « démarre ».

    Le texte le plus proche du geste gagne : la correction appartient à la
    description servie, pas à une doc à côté que personne ne relit au moment
    d'agir."""
    cap = [c for c in CAPABILITIES if c.key == "runner.fleets"][0]
    d = cap.description
    assert "ARMS" in d and "does NOT start any process" in d, (
        "la description doit dire ce que le geste FAIT, pas répéter son nom")
    assert "`armed`, never `running`" in d
    assert "REQUESTS the stop" in d and "SPENDING" in d, (
        "le versant `stop` compte autant : entre l'ordre et sa lecture, le "
        "passage continue de dépenser — l'annoncer arrêté est le plus coûteux "
        "des deux mensonges")


# ── ouverte à toute org : plus de porte bêta (24/09/2026) ────────────────────

def test_sans_option_beta_la_capacite_SERT(monkeypatch):
    """Les agents hébergés s'ouvrent à toute org : ce qui borne la dépense est le
    modèle obligatoire et la clé de modèle de l'org, plus l'option `beta` : un
    compte sans elle est servi."""
    from oto_mcp import access, db
    monkeypatch.setattr(access, "has_option", lambda *a, **k: False)
    monkeypatch.setattr(db, "list_fleets", lambda *a, **k: [])
    assert "fleets" in _appel(_ctx(), op="list")


def test_l_org_manquante_reste_le_premier_refus():
    """`org_required` reste le premier refus : les automatisations sont org-scopées."""
    with pytest.raises(AuthzDenied) as e:
        _appel(ResolvedCtx(sub="alexis", org_id=None), op="list")
    assert e.value.code == "org_required"


# ── La borne par ligne : POSABLE, et son produit dit à l'armement ───────────
# ⚠️ Arbitrage d'Alexis, 09/09/2026, verbatim : « Une borne doit pouvoir être
# posée, si pas de borne, tant pis pour le moment (ou plutôt : pour le moment ça
# s'arrêtera à la fenêtre de contexte du LLM). »
#
# Le serveur ne REFUSE donc rien : ni l'absence, ni une valeur haute. J'avais
# livré un plafond à 200 000 — il aurait cassé 86 déclarations existantes à
# 1,5 M, dont une de production. Une garde que personne n'a demandée, posée sur
# une distribution mesurée, reste une garde posée de son chef.
#
# Ce qui reste : un champ qui borne quand il est posé, rien quand il ne l'est
# pas, et le PRODUIT montré au moment où l'on engage — c'est la seule partie qui
# aurait vraiment servi, celle qui aurait affiché les 150 millions à celui qui
# armait.


def _creation(**kw):
    base = dict(op="create", label="passage", procedure="p", tools=["data_rows"],
                max_tokens_per_row=120_000)
    base.update(kw)
    return base


def test_creer_sans_borne_par_ligne_est_permis(monkeypatch):
    from oto_mcp import db
    monkeypatch.setattr(db, "create_fleet", lambda *a, **k: {"id": 1})
    _appel(_ctx(), **_creation(max_tokens_per_row=None))


def test_une_borne_HAUTE_est_permise(monkeypatch):
    """Le bord qui compte après l'arbitrage : 1,5 M passe. Refuser aurait cassé
    des déclarations vivantes, dont une de production."""
    from oto_mcp import db
    monkeypatch.setattr(db, "create_fleet", lambda *a, **k: {"id": 1})
    _appel(_ctx(), **_creation(max_tokens_per_row=1_500_000))


def test_l_armement_MONTRE_le_pire_cas(monkeypatch):
    """`max_rows × max_tokens_per_row` = la dépense maximale du passage, dite au
    moment où on l'engage. Une borne invisible ne borne personne."""
    from oto_mcp import db, roles
    monkeypatch.setattr(roles, "is_org_admin", lambda *a, **k: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    monkeypatch.setattr(db, "get_fleet", lambda *a, **k: {
        "id": 1, "status": "draft", "procedure": "p", "input": "x", "model": MODELE})
    monkeypatch.setattr(db, "armer", lambda *a, **k: {
        "id": 1, "max_rows": 100, "max_tokens_per_row": 1_500_000})

    assert _appel(_ctx(), op="launch", fleet_id=1)["budget_max_tokens"] == 150_000_000


def test_sans_borne_le_pire_cas_est_NULL_et_non_un_nombre(monkeypatch):
    """Sans borne il n'y a pas de pire cas. `null` le dit ; un nombre fabriqué
    ferait croire à une protection qui n'existe pas."""
    from oto_mcp import db, roles
    monkeypatch.setattr(roles, "is_org_admin", lambda *a, **k: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    monkeypatch.setattr(db, "get_fleet", lambda *a, **k: {
        "id": 1, "status": "draft", "procedure": "p", "input": "x", "model": MODELE})
    monkeypatch.setattr(db, "armer", lambda *a, **k: {
        "id": 1, "max_rows": 100, "max_tokens_per_row": None})

    assert _appel(_ctx(), op="launch", fleet_id=1)["budget_max_tokens"] is None


def _armable_a_borne(monkeypatch, produits):
    from oto_mcp import db, roles
    monkeypatch.setattr(roles, "is_org_admin", lambda *a, **k: True)
    monkeypatch.setattr(RF, "_run_courant", lambda: None)
    monkeypatch.setattr(db, "get_fleet", lambda *a, **k: {
        "id": 1, "status": "done", "procedure": "p", "input": "x", "model": MODELE,
        "max_rows": 2, "stop_reason": "max_rows"})
    monkeypatch.setattr(db, "travaux_produits", lambda fid, org: produits)
    arme = {}
    monkeypatch.setattr(db, "armer", lambda *a, **k: arme.setdefault("f", {
        "id": 1, "max_rows": 2, "max_tokens_per_row": None}))
    return arme


def test_une_campagne_a_sa_borne_ne_se_rearme_pas_en_silence(monkeypatch):
    """Close `done` à sa borne, réarmée telle quelle, elle resterait `armed` sans
    que rien ne lui soit jamais produit : le compte ne repart pas de zéro. Le
    refus nomme les deux sorties, et n'écrit rien."""
    arme = _armable_a_borne(monkeypatch, produits=2)

    with pytest.raises(AuthzDenied) as e:
        _appel(_ctx(), op="launch", fleet_id=1)
    assert (e.value.status, e.value.code) == (409, "max_rows_reached")
    assert "op=update" in e.value.message
    assert "déclare une autre automatisation" in e.value.message
    assert arme == {}, "un refus n'arme rien"


def test_sous_sa_borne_la_campagne_se_rearme(monkeypatch):
    arme = _armable_a_borne(monkeypatch, produits=1)

    assert _appel(_ctx(), op="launch", fleet_id=1)["fleet"]["id"] == 1
    assert arme


# ── La température : déclarée par PASSAGE, jamais posée dans l'environnement ──
# Mesuré le 06/09/2026 : le même texte sur le même banc donnait 11 à 18 sur 18 au
# défaut du fournisseur, contre 14 à 16 à zéro. Une journée d'itérations a comparé
# des versions dont l'écart était entièrement dans ce bruit.
#
# ⚠️ La forme compte autant que le réglage. Une variable d'environnement
# s'appliquerait à TOUS les passages sans distinction et ne se lirait nulle part ;
# déclarée, elle se choisit passage par passage et se relit dans la campagne.

def test_la_temperature_se_declare_et_atteint_la_base(monkeypatch):
    from oto_mcp import db
    vu = {}
    monkeypatch.setattr(db, "create_fleet",
                        lambda *a, **k: vu.update(k) or {"id": 1})
    _appel(_ctx(), **_creation(temperature=0))
    assert vu["temperature"] == 0, (
        "zéro est une valeur, pas une absence — la confondre avec `None` rendrait "
        "le réglage le plus utile impossible à poser")


def test_sans_temperature_declaree_rien_nest_pose(monkeypatch):
    """L'autre bord : le fournisseur applique son défaut, comme avant. Poser une
    température devinée serait pire que ne rien poser — elle deviendrait un
    contexte d'exécution que personne n'a choisi."""
    from oto_mcp import db
    vu = {}
    monkeypatch.setattr(db, "create_fleet",
                        lambda *a, **k: vu.update(k) or {"id": 1})
    _appel(_ctx(), **_creation())
    assert vu["temperature"] is None


def test_la_temperature_ne_se_change_PAS_en_vol():
    """Même raison que `provider`/`model` : deux lignes du même passage écrites à
    deux températures ne sont pas comparables, et rien dans la donnée ne dirait
    laquelle vient de quel régime."""
    from oto_mcp.db.runner_fleets import CHAMPS_MODIFIABLES
    assert "temperature" not in CHAMPS_MODIFIABLES


# ── op=list rend la CARTE, get la déclaration (13/09/2026) ────────────────────
# Relevé par l'opérateur des campagnes : une vingtaine de passages, chacun rendu avec
# son instruction complète (~2 Ko) et son allowlist, dans une liste qui ne sert qu'à
# choisir quoi ouvrir. Le détail reste à `get`, inchangé.

_CARTE = {"id", "label", "status", "procedure", "namespace", "row_filter", "max_rows",
          "model", "stop_reason", "armed_at", "started_at", "stopping_at", "stopped_at",
          "created_at", "taken_by", "trigger_id", "input_sha256"}


def _ligne(**surcharges) -> dict:
    """Une flotte telle que la base la rend : EXACTEMENT les colonnes du SELECT servi.
    Une doublure qui porterait d'autres colonnes éprouverait la projection d'une ligne
    qui n'existe pas."""
    from oto_mcp.db import runner_fleets as dbf
    ligne = {
        "id": 41, "org_id": 2, "sub": "alexis", "label": "vague", "procedure": "p",
        "project_id": 9, "tools": ["data_rows", "data_write"],
        "input": "Lis la procédure `p` et applique-la.", "max_steps": 40,
        "namespace": "vivier", "row_filter": {"lot": "a"}, "provider": "mistral",
        "model": "mistral-large-2512", "temperature": 0.0, "descriptions_outils": None,
        "workers": 3, "max_rows": 100,
        "max_tokens": 1_000_000, "max_consecutive_failures": 5,
        "max_tokens_per_row": 50_000, "max_run_seconds": 900,
        "status": "running", "stop_reason": None,
        "armed_at": "2026-09-13 08:00:00", "started_at": "2026-09-13 08:00:05",
        "stopping_at": None, "heartbeat_at": None, "taken_by": None, "stopped_at": None,
        "created_at": "2026-09-12 17:00:00", "trigger_id": None}
    assert set(ligne) == {c.strip() for c in dbf._COLS.split(",")}, (
        "la doublure ne porte plus les colonnes du SELECT servi")
    ligne.update(surcharges)
    return ligne


def _lister(monkeypatch, lignes) -> dict:
    monkeypatch.setattr(RF.db, "list_fleets",
                        lambda org_id, statut=None: [dict(l) for l in lignes])
    return _appel(_ctx(), op="list")


def test_list_rend_la_CARTE_et_rien_d_autre(monkeypatch):
    (carte,) = _lister(monkeypatch, [_ligne()])["fleets"]
    assert set(carte) == _CARTE
    assert "input" not in carte and "tools" not in carte
    assert set(RF.FleetCard.model_fields) == _CARTE, (
        "le schéma servi de la carte et ce que le handler garde sont le même ensemble")


def test_l_empreinte_est_celle_du_texte_que_get_rend(monkeypatch):
    import hashlib
    ligne = _ligne()
    monkeypatch.setattr(RF.db, "get_fleet", lambda fid, oid: dict(ligne))
    get = _appel(_ctx(), op="get", fleet_id=41)["fleet"]
    (carte,) = _lister(monkeypatch, [ligne])["fleets"]
    assert carte["input_sha256"] == hashlib.sha256(get["input"].encode("utf-8")).hexdigest()


def test_deux_instructions_se_distinguent_et_l_absence_n_a_pas_d_empreinte(monkeypatch):
    """Même texte ⟹ même empreinte ; un caractère d'écart ⟹ une autre ; aucune
    instruction (`null`) ⟹ `null`."""
    cartes = _lister(monkeypatch, [_ligne(id=1, input="A"), _ligne(id=2, input="A"),
                                   _ligne(id=3, input="A."), _ligne(id=4, input=None)]
                     )["fleets"]
    e = {c["id"]: c["input_sha256"] for c in cartes}
    assert e[1] == e[2] != e[3]
    assert e[4] is None


def test_la_projection_NOMME_ce_que_la_carte_ecarte_et_ou_le_lire(monkeypatch):
    out = _lister(monkeypatch, [_ligne()])
    ecartes = set(_ligne()) - _CARTE
    assert set(out["projection"]["omitted"]) == ecartes
    assert {"input", "tools"} <= ecartes
    indice = out["projection"]["hint"]
    assert "op=get" in indice
    assert "fields" not in indice, (
        "l'indice par défaut du seam prescrit `fields=[\"*\"]`, que cette capacité "
        "n'accepte pas : un agent qui le suivrait se ferait refuser")


def test_get_rend_la_declaration_telle_quelle(monkeypatch):
    ligne = _ligne()
    monkeypatch.setattr(RF.db, "get_fleet", lambda fid, oid: dict(ligne))
    assert _appel(_ctx(), op="get", fleet_id=41) == {"fleet": ligne}


def test_une_liste_vide_ne_fabrique_pas_de_notice(monkeypatch):
    assert _lister(monkeypatch, []) == {"fleets": [], "projection": None}


# ── l'agent qui déclare le passage (23/10/2026) ───────────────────────────────

class _Verrou:
    def __init__(self, job_id):
        self.job_id = job_id


def _create_capture(monkeypatch, *, job_id, agents):
    """`create` jusqu'à l'écriture, l'écriture capturée : le travail du jeton est
    `job_id` (None = une personne), et `agents` dit, par (travail, org), l'agent que
    `db.agent_du_travail` résout."""
    monkeypatch.setattr(RF.verrou_org, "travail_de_la_requete",
                        lambda: None if job_id is None else _Verrou(job_id))
    lus = []
    monkeypatch.setattr(RF.db, "agent_du_travail",
                        lambda j, org: lus.append((j, org)) or agents.get((j, org)))
    monkeypatch.setattr(RF, "_cible_a_la_declaration", lambda ctx, ns: ns)
    vu = {}
    monkeypatch.setattr(RF.db, "create_fleet", lambda *a, **kw: vu.update(kw) or {"id": 1})
    _appel(_ctx(), op="create", label="cartes", procedure="pousser-les-appels",
           tools=["oto_kb"])
    return vu, lus


def test_un_passage_declare_par_un_agent_heberge_porte_cet_agent(monkeypatch):
    """Un agent déclare un passage d'une AUTRE procédure que la sienne : seul ce lien
    le range sous lui. L'agent se lit par le travail du JETON, dans l'org de l'appel."""
    vu, lus = _create_capture(monkeypatch, job_id=900, agents={(900, 2): 41})
    assert vu["trigger_id"] == 41
    assert lus == [(900, 2)]


def test_un_passage_declare_par_une_personne_ne_porte_aucun_agent(monkeypatch):
    vu, lus = _create_capture(monkeypatch, job_id=None, agents={})
    assert vu["trigger_id"] is None
    assert lus == [], "sans travail au jeton, rien ne se lit"


def test_un_travail_sans_agent_ne_porte_aucun_agent(monkeypatch):
    """Un travail de campagne, enfilé à la main, ou dont l'agent est d'une autre org ou
    supprimé : `agent_du_travail` ne le résout pas (son banc sur base :
    `test_agent_du_travail_db.py`)."""
    vu, _ = _create_capture(monkeypatch, job_id=901, agents={})
    assert vu["trigger_id"] is None


def test_un_agent_ne_se_designe_pas_par_un_argument():
    """Aucun champ d'entrée ne porte l'agent déclarant : il ne se lit qu'au jeton."""
    assert "trigger_id" not in RF.FleetInput.model_fields


def test_la_carte_de_list_sert_l_agent_declarant():
    assert "trigger_id" in RF.FleetCard.model_fields
    from oto_mcp import db
    assert "trigger_id" in db.runner_fleets._COLS


# ── le droit par objet : modifier est au déclarant et aux admins ──────────────

def _une_flotte_de(monkeypatch, proprietaire: str, *, admin: bool) -> list:
    from oto_mcp import db, roles
    ecrits: list = []
    monkeypatch.setattr(db, "get_fleet", lambda fid, org: {
        "id": fid, "sub": proprietaire, "tools": ["data_rows"]})
    monkeypatch.setattr(db, "update_fleet",
                        lambda fid, org, champs: ecrits.append(champs) or {"id": fid, **champs})
    monkeypatch.setattr(roles, "is_org_admin", lambda sub, org: admin)
    return ecrits


def test_un_membre_ne_modifie_pas_l_automatisation_d_un_autre(monkeypatch):
    ecrits = _une_flotte_de(monkeypatch, "collegue", admin=False)
    for champs in ({"label": "x"}, {"input": "autre consigne"},
                   {"tools": ["data_rows", "data_write"]}):
        with pytest.raises(AuthzDenied) as e:
            _appel(_ctx(), op="update", fleet_id=1, **champs)
        assert (e.value.status, e.value.code) == (403, "fleet_edit_forbidden")
    assert ecrits == [], "un refus n'écrit rien"


def test_le_declarant_modifie_la_sienne(monkeypatch):
    ecrits = _une_flotte_de(monkeypatch, "alexis", admin=False)
    assert _appel(_ctx(), op="update", fleet_id=1, label="x")["fleet"]["label"] == "x"
    assert ecrits == [{"label": "x"}]


def test_un_admin_de_l_org_modifie_celle_d_un_autre(monkeypatch):
    ecrits = _une_flotte_de(monkeypatch, "collegue", admin=True)
    assert _appel(_ctx(), op="update", fleet_id=1, label="x")["fleet"]["label"] == "x"
    assert ecrits == [{"label": "x"}]


def test_le_refus_de_droit_est_declare():
    cap = next(c for c in CAPABILITIES if c.key == "runner.fleets")
    assert (403, "fleet_edit_forbidden") in {(e.status, e.code) for e in cap.errors}
