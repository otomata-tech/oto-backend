"""La clé de modèle de l'org part avec le travail RÉSERVÉ — et rien d'autre.

Décidé le 02/09/2026 : la clé de modèle vit avec les autres secrets de
connecteurs de l'org, et le worker — qui fait partie du backend — a le droit de
la lire. Ce droit s'exerce à la réservation, une fois, avec le travail : le
runner n'interroge jamais le coffre, sans quoi il pourrait lire autre chose que
ce travail-ci.

D'où la garde que ces bancs tiennent : **elle porte sur le TYPE du dépôt**. Un
worker nomme le dépôt qu'il sait consommer ; s'il pouvait nommer n'importe quel
connecteur, réserver un travail suffirait à faire sortir le secret Folk ou
Salesforce de l'org. Seuls les connecteurs `kind="credential"` — porter une clé
est leur seule raison d'être, aucun outil derrière — sont servis.
"""
from __future__ import annotations

import pytest

from oto_mcp import providers
from oto_mcp.capabilities import runner_jobs as RJ


@pytest.fixture(autouse=True)
def _cle_de_modele_non_exigee(monkeypatch):
    """Ce fichier ne parle pas de la garde de clé de modèle — elle a son propre banc
    (`test_cle_de_modele_exigee.py`). Le réglage est lu ÉTEINT, comme sur toute
    plateforme qui ne l'a pas allumé : sans cette doublure, la lecture irait
    chercher la vraie base et chaque banc tomberait sur une raison qui n'est pas
    la sienne."""
    monkeypatch.setattr("oto_mcp.db.connector_settings.get_connector_setting",
                        lambda *a, **k: None)


_WORKER = "svc-runner-worker"
_MEMBRE = "un-membre-ordinaire"


def _servi(job, depot, appelant=_WORKER):
    """Le travail tel que servi au CLAIM, par défaut réservé par un worker.

    ⚠️ Depuis le 09/09/2026, « être un worker » n'est plus une marque posée sur
    un compte (lue par une option), c'est ce que la règle d'autorisation a
    établi en vérifiant un secret de machine déclaré en base. Le banc le
    modélise tel quel — `worker=` est un fait reçu, et `_MEMBRE` ne l'est
    jamais, quoi qu'il porte."""
    return RJ._avec_cle(job, depot, appelant, worker=(appelant == _WORKER))


@pytest.fixture
def _coffre(monkeypatch):
    """Un coffre qui note CE QU'ON LUI DEMANDE — l'entité autant que le dépôt."""
    from oto_mcp import credentials_store
    demandes = []

    def _get(entity_type, entity_id, connector, account=""):
        demandes.append((entity_type, entity_id, connector))
        return {"anthropic": "sk-de-l-org", "folk": "secret-folk-de-l-org"}.get(connector)

    def _has(entity_type, entity_id, connector, account=None):
        return connector in ("anthropic", "folk")

    monkeypatch.setattr(credentials_store, "get_credential", _get)
    monkeypatch.setattr(credentials_store, "has_credential", _has)
    return demandes


# ── ce que le registre déclare vraiment ───────────────────────────────────────

def test_les_depots_de_cle_sont_d_un_type_a_part_et_ne_portent_aucun_outil():
    """Sans le type distinct, la garde ci-dessous n'aurait rien à quoi se tenir."""
    for nom in ("anthropic", "mistral"):
        c = providers.connector_for_provider(nom)
        assert c and c.kind == "credential", f"{nom} n'est plus un dépôt de clé"
        assert not c.namespaces, f"{nom} expose des outils — ce n'est plus un dépôt"


def test_un_depot_de_cle_est_mono_compte_et_c_est_la_ou_on_le_lit():
    """La dérivation rendrait `multi` (c'est une api_key), et l'écran proposerait
    d'en poser une deuxième que rien ne saurait choisir : `_cle_de_modele` lit le
    compte unique. Une clé déposée sous un nom de compte ne serait jamais lue —
    l'org paierait sur la clé de la plateforme en croyant payer sur la sienne."""
    for nom in ("anthropic", "mistral"):
        c = providers.connector_for_provider(nom)
        assert not c.auth_multi_account, f"{nom} redevenu multi-compte"
        assert c.auth["cardinality"] == "single"


# ── la remise ─────────────────────────────────────────────────────────────────

def test_le_travail_reserve_emporte_la_cle_deposee_par_son_org(_coffre):
    job = _servi({"id": 1, "org_id": 42}, "anthropic")
    assert job["model_key"] == "sk-de-l-org"
    assert _coffre == [("org", "42", "anthropic")]


def test_sans_depot_nomme_aucune_cle_ne_part(_coffre):
    assert "model_key" not in _servi({"id": 1, "org_id": 42}, None)
    assert _coffre == [], "le coffre n'est même pas interrogé"


def test_une_org_qui_n_a_rien_depose_ne_recoit_pas_de_cle(_coffre):
    assert "model_key" not in _servi({"id": 1, "org_id": 42}, "mistral")


# ── la garde : le type, pas le nom ────────────────────────────────────────────

def test_un_connecteur_ordinaire_ne_se_laisse_pas_tirer_par_un_worker(_coffre):
    """`folk` a bien un secret dans cette org — et il ne sort pas. Réserver un
    travail ne doit jamais devenir un moyen de lire le coffre."""
    job = _servi({"id": 1, "org_id": 42}, "folk")
    assert "model_key" not in job
    assert _coffre == [], "le coffre ne doit pas même être interrogé"


def test_un_depot_inconnu_ne_fait_pas_tomber_la_reservation(_coffre):
    assert "model_key" not in _servi({"id": 1, "org_id": 42}, "n-existe-pas")


def test_aucun_connecteur_a_outils_ne_porte_le_type_depot():
    """La garde se tient par CLASSE : le jour où un dépôt de clé gagnerait des
    outils, ou un connecteur ordinaire le type `credential`, la liste
    d'autorisation cesserait d'être une liste d'autorisation."""
    coupables = [c.name for c in providers.REGISTRY.values()
                 if c.kind == "credential" and c.namespaces]
    assert not coupables


# ── ce qui prime sur la remise ────────────────────────────────────────────────

def test_un_travail_refuse_pour_identite_ne_recoit_pas_de_cle(_coffre):
    """Il est déjà marqué échoué : lui remettre une clé serait armer un travail
    qui ne doit pas tourner."""
    job = _servi(
        {"id": 1, "org_id": 42, "delegation_refusee": "compte supprimé"}, "anthropic")
    assert "model_key" not in job
    assert _coffre == []


def test_un_travail_sans_org_ne_recoit_pas_de_cle(_coffre):
    assert "model_key" not in _servi({"id": 1, "org_id": None}, "anthropic")


# ── le contrat servi le dit ───────────────────────────────────────────────────

def test_le_contrat_dit_que_la_cle_appartient_a_l_org_et_ne_se_journalise_pas():
    d = RJ.Job.model_fields["model_key"].description
    assert "op=claim only" in d and "never written" in d


def test_la_cle_ne_sort_que_de_la_reservation_jamais_d_une_lecture():
    """`list` et `get` servent les mêmes travaux à toute l'org : si la clé y
    passait, la lire ne demanderait plus d'en réserver un. Elle n'est écrite
    nulle part en base — elle n'existe que dans la réponse au claim."""
    import inspect
    src = inspect.getsource(RJ._jobs)
    appels = [l.strip() for l in src.splitlines() if "_avec_cle(" in l]
    assert len(appels) == 1 and 'op == "claim"' in src
    branche = src.split('if inp.op == "claim":')[1].split("if inp.op ==")[0]
    assert "_avec_cle(" in branche


# ── ce que le journal peut en voir : rien ─────────────────────────────────────

MOTS_DE_RESULTAT = ("result", "response", "output", "reponse", "resultat")
TYPES_QUI_NE_PORTENT_PAS_DE_TEXTE = ("integer", "bigint", "smallint", "numeric",
                                     "boolean", "double precision", "real")


def _colonnes_de_resultat_non_numeriques(insert: str, ddl: str) -> list[str]:
    """Les colonnes du journal qui évoquent une réponse ET pourraient en porter une.

    Une colonne dont le nom évoque un résultat n'est pas une fuite en soi — c'est ce
    qu'elle peut CONTENIR qui l'est. Un entier ne peut porter ni une clé de modèle ni
    un extrait de réponse ; un TEXT ou un JSONB le peut."""
    colonnes = [c.strip() for c in
                insert.split("INSERT INTO tool_calls")[1].split(")")[0]
                .strip().lstrip("(").split(",")]
    types = {}
    for ligne in ddl.splitlines():
        morceaux = ligne.strip().rstrip(",").split()
        if len(morceaux) >= 2 and not morceaux[0].upper() in ("CREATE", "PRIMARY", "--"):
            types[morceaux[0]] = " ".join(morceaux[1:]).lower()
    suspectes = [c for c in colonnes
                 if any(m in c.lower() for m in MOTS_DE_RESULTAT)]
    return [c for c in suspectes
            if not any(types.get(c, "?").startswith(t)
                       for t in TYPES_QUI_NE_PORTENT_PAS_DE_TEXTE)]


def test_le_journal_des_appels_ne_garde_aucune_reponse():
    """La clé part dans la RÉPONSE au claim, pas dans ses arguments — le masque
    de `tool_calls.args` (#558/#564) ne la couvre donc pas, et n'a pas à le
    faire : le journal ne stocke aucune réponse.

    ⚠️ **Ce banc a déjà servi**, et c'est pourquoi il a changé de forme. Il visait le
    MOT et il est tombé sur `result_size` (#340), une colonne qui compte les
    caractères servis sans en garder un seul. La question qu'il exige de reposer a
    donc été reposée, et la réponse est : mesurer n'est pas stocker.

    Il garde désormais ce qu'il protégeait vraiment — qu'aucune colonne de résultat ne
    puisse CONTENIR quoi que ce soit. Un `INTEGER` ne porte ni clé ni extrait ; un
    `TEXT` ou un `JSONB` le porterait, et le banc tombe alors comme avant. Fermer sur
    le mot laissait passer le vrai danger sous un nom neutre (`payload`, `body`) tout
    en refusant une mesure inoffensive."""
    import inspect

    from oto_mcp.db import usage
    from oto_mcp.db.schema.usage import USAGE

    fautives = _colonnes_de_resultat_non_numeriques(
        inspect.getsource(usage.insert_tool_call), USAGE)
    assert fautives == [], (
        f"`tool_calls` garde maintenant une réponse : {fautives}. Un travail réservé "
        "journalisé avec sa clé de modèle serait une fuite. Si la colonne ne fait que "
        "MESURER, donne-lui un type numérique ; si elle stocke, ne la pose pas.")


def test_la_garde_tombe_bien_sur_une_colonne_qui_STOCKERAIT():
    """⚠️ Une garde qui ne tombe jamais ne garde rien. On lui présente les deux cas :
    la mesure passe, le stockage est refusé — y compris sous le même préfixe."""
    insert = ("INSERT INTO tool_calls (tool, result_size, result_text)\n"
              "VALUES (%s, %s, %s)")
    ddl = ("CREATE TABLE IF NOT EXISTS tool_calls (\n"
           "    tool TEXT NOT NULL,\n"
           "    result_size INTEGER,\n"
           "    result_text TEXT\n);")
    assert _colonnes_de_resultat_non_numeriques(insert, ddl) == ["result_text"]


def test_la_remise_ne_modifie_pas_le_travail_d_origine(_coffre):
    """`_avec_cle` rend une COPIE : le dict du claim, lui, peut être relu,
    compté ou tracé ailleurs sans emporter le secret."""
    origine = {"id": 1, "org_id": 42}
    servi = _servi(origine, "anthropic")
    assert servi is not origine and "model_key" not in origine


# ── et la file n'est pas réservée aux workers ─────────────────────────────────

def test_un_membre_ordinaire_recoit_son_travail_SANS_la_cle(_coffre):
    """Le défaut du 04/09 : la capacité est `ORG_MEMBER`, et rien dans le
    protocole ne distingue un worker d'un membre — même genre de jeton, même
    route. Sans cette garde, `enqueue` puis `claim provider=anthropic` rendait
    la clé de l'org en clair à n'importe lequel de ses membres."""
    job = _servi({"id": 1, "org_id": 42}, "anthropic", appelant=_MEMBRE)
    assert "model_key" not in job
    assert job["id"] == 1, "il reçoit son travail — c'est la CLÉ qu'on lui retire"


def test_le_refus_est_muet_pour_l_appelant_et_ecrit_pour_nous(_coffre, caplog):
    """Un refus explicite apprendrait qu'il y a une clé à obtenir. Mais un membre
    qui nomme un dépôt RÉELLEMENT POSÉ cherche quelque chose : ça, ça se
    journalise."""
    with caplog.at_level("WARNING"):
        job = _servi({"id": 2, "org_id": 42}, "anthropic", appelant=_MEMBRE)
    assert "error" not in job and "detail" not in job
    assert "REFUSÉE" in caplog.text and _MEMBRE in caplog.text
    assert "sk-de-l-org" not in caplog.text, "jamais la clé dans un journal"


def test_sans_depot_pose_le_refus_ne_dit_RIEN(_coffre, caplog):
    """⚠️ Le journal ne décrit un événement que s'il y a quelque chose à refuser.
    Sans dépôt, le travail serait parti sans clé de toute façon — et les workers
    eux-mêmes, qui nomment leur dépôt à chaque réservation, écriraient des
    milliers de lignes par jour tant que la marque n'est pas posée. Une sonde qui
    fabrique son propre signal fait cesser de lire le journal."""
    with caplog.at_level("WARNING"):
        _servi({"id": 5, "org_id": 42}, "mistral", appelant=_MEMBRE)
    assert caplog.text == ""


def test_vingt_reservations_sans_depot_ne_laissent_aucune_ligne(_coffre, caplog):
    """Le volume, mesuré plutôt qu'espéré : c'est le régime réel de la production
    tant que la marque n'est pas posée."""
    with caplog.at_level("WARNING"):
        for i in range(20):
            _servi({"id": 100 + i, "org_id": 42}, "mistral", appelant=_MEMBRE)
    assert caplog.text.count("REFUSÉE") == 0

    caplog.clear()
    with caplog.at_level("WARNING"):
        for i in range(20):
            _servi({"id": 200 + i, "org_id": 42}, "anthropic", appelant=_MEMBRE)
    assert caplog.text.count("REFUSÉE") == 20, (
        "quand il y a une clé, chaque tentative se voit — c'est le signal qu'on "
        "veut garder")


def test_etre_worker_n_est_PAS_une_marque_de_compte():
    """⚠️ Le piège que cette garde a porté deux jours : « ce compte est un de nos
    workers » — une option posée sur un `users.sub`. Le compte marqué était un
    compte personnel, et la flotte sondait son org active. Depuis le 09/09/2026
    la garde ne consulte AUCUNE marque : le fait vient de la règle d'autorisation,
    qui lit ce que l'authentification REST a posé après avoir vérifié un secret
    de machine en base. Rien ici ne regarde un compte, et c'est le point."""
    import inspect
    from oto_mcp.capabilities import _authz
    src = inspect.getsource(RJ._avec_cle)
    assert "has_option" not in src, "aucune marque de compte, ni d'org"
    assert "worker" in inspect.signature(RJ._avec_cle).parameters
    regle = inspect.getsource(_authz.WORKER_OR_ORG_MEMBER)
    assert "platform_worker.current()" in regle, (
        "le fait est LU de ce que l'auth a posé, jamais déduit d'un sub ou d'une option")

def test_la_remise_a_un_worker_laisse_une_trace_sans_la_cle(_coffre, caplog):
    with caplog.at_level("INFO"):
        job = _servi({"id": 3, "org_id": 42}, "anthropic")
    assert job["model_key"] == "sk-de-l-org"
    assert "remise" in caplog.text and "org 42" in caplog.text
    assert "sk-de-l-org" not in caplog.text
