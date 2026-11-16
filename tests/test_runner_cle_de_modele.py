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

import re

import pytest

from oto_mcp import providers
from oto_mcp.capabilities import runner_jobs as RJ


@pytest.fixture(autouse=True)
def _aucune_cle_partagee(monkeypatch):
    """Aucune clé partagée (plateforme ou tenant) n'est posée ni accordée ici : sans cette
    doublure, la garde d'argent irait la résoudre dans le vrai coffre. Le cas où elle
    l'est a son banc (`test_cle_partagee_anthropic.py`)."""
    monkeypatch.setattr("oto_mcp.capabilities._cle_exigee.cle_partagee",
                        lambda org_id, fournisseur, sub=None, strict=False: None)
    monkeypatch.setattr("oto_mcp.capabilities._cle_exigee.cle_de_plateforme_posee",
                        lambda fournisseur: False)



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
    # Un travail porte la famille de son modèle (24/09/2026) : sans elle il est
    # arrêté avant toute question de clé. Ce banc parle de la REMISE de la clé.
    if "payload" not in job:
        job = {**job, "payload": {"model_family": depot or "anthropic"}}
    return RJ._avec_cle(job, depot, appelant, worker=(appelant == _WORKER))


class _Demandes(list):
    """Les lectures du coffre, dans l'ordre — et le `meta` que chaque ligne rendra."""
    meta: dict


@pytest.fixture
def _coffre(monkeypatch):
    """Un coffre qui note CE QU'ON LUI DEMANDE — l'entité autant que le dépôt. Il se lit
    comme la remise le lit : la ligne entière, secret et `meta` (`_coffre.meta[dépôt]`)."""
    from oto_mcp import credentials_store
    demandes = _Demandes()
    demandes.meta = {}

    def _get(entity_type, entity_id, connector, account=""):
        demandes.append((entity_type, entity_id, connector))
        secret = {"anthropic": "sk-de-l-org", "folk": "secret-folk-de-l-org"}.get(connector)
        return {"secret": secret, "meta": dict(demandes.meta.get(connector, {}))} if secret else None

    def _has(entity_type, entity_id, connector, account=None):
        return connector in ("anthropic", "folk")

    monkeypatch.setattr(credentials_store, "get_credential_with_meta", _get)
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
# Un TEXT dont la BASE ferme le vocabulaire (#644, `result_shape`) ne porte pas de texte
# libre non plus : une contrainte `CHECK (<col> ~ '^…$')` ancrée, sans joker ni
# répétition ouverte, refuse toute valeur hors de la liste — clé de modèle et extrait
# de réponse compris. La propriété est lue dans le DDL, pas accordée à un nom.
_CHECK_ANCRE = re.compile(r"check \((\w+) ~ '\^([^']*)\$'\)$")
_JOKERS = (".", "*", "+", "?", "\\", "[^", ",}")


def _vocabulaire_ferme(colonne: str, type_: str) -> bool:
    m = _CHECK_ANCRE.search(type_)
    return (type_.startswith("text") and m is not None and m.group(1) == colonne
            and not any(j in m.group(2) for j in _JOKERS))


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
                       for t in TYPES_QUI_NE_PORTENT_PAS_DE_TEXTE)
            and not _vocabulaire_ferme(c, types.get(c, "?"))]


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
    from oto_mcp.db import usage
    from oto_mcp.db.schema.usage import USAGE

    # L'INSERT unique du journal, que `insert_tool_call` et `inserer_tour_au_cout`
    # partagent : une colonne posée là l'est pour les deux écrivains.
    fautives = _colonnes_de_resultat_non_numeriques(usage._INSERT_TOOL_CALL, USAGE)
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


def test_un_vocabulaire_FERME_par_la_base_passe_un_vocabulaire_ouvert_tombe():
    """#644 : `result_shape` est un TEXT, mais la base n'y accepte que `empty`,
    `non_empty` ou `refused(<identifiant>)`. Une contrainte qui laisserait passer un
    texte libre — joker, répétition ouverte, classe niée, ou posée sur une AUTRE
    colonne — ne ferme rien, et la garde tombe comme avant."""
    insert = ("INSERT INTO tool_calls (tool, result_a, result_b, result_c, result_d, "
              "result_e)\nVALUES (%s, %s, %s, %s, %s, %s)")
    ddl = ("CREATE TABLE IF NOT EXISTS tool_calls (\n"
           "    tool TEXT NOT NULL,\n"
           "    result_a TEXT CONSTRAINT k CHECK (result_a ~ "
           "'^(empty|refused[(][a-z][a-z_]{0,39}[)])$'),\n"
           "    result_b TEXT CHECK (result_b ~ '^.*$'),\n"
           "    result_c TEXT CHECK (result_c ~ '^[a-z]+$'),\n"
           "    result_d TEXT CHECK (tool ~ '^(a|b)$'),\n"
           "    result_e TEXT CHECK (result_e ~ '^[^x]{0,9}$')\n);")
    assert _colonnes_de_resultat_non_numeriques(insert, ddl) == [
        "result_b", "result_c", "result_d", "result_e"]


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


# ── le workspace d'une clé d'organisation (14/09/2026) ────────────────────────
# Une clé Anthropic créée pour toute l'organisation fait refuser chaque requête qui ne
# nomme pas son workspace (en-tête `anthropic-workspace-id`). Il se dépose avec la clé,
# dans `meta` de la même ligne, et part avec elle — par la même lecture.

def test_le_workspace_depose_part_a_cote_de_la_cle(_coffre):
    _coffre.meta["anthropic"] = {"workspace_id": "wrkspc_01banc"}
    job = _servi({"id": 1, "org_id": 42}, "anthropic")
    assert (job["model_key"], job["model_workspace"]) == ("sk-de-l-org", "wrkspc_01banc")
    assert _coffre == [("org", "42", "anthropic")], "une seule lecture, la même ligne"


def test_sans_workspace_depose_aucun_champ_ne_part(_coffre):
    assert "model_workspace" not in _servi({"id": 1, "org_id": 42}, "anthropic")


def test_seul_un_champ_declare_sort_de_meta(_coffre):
    """`meta` porte aussi des satellites de service : aucun ne part au worker."""
    _coffre.meta["anthropic"] = {"health_reason": "sonde KO", "verified_at": "2026-09-14"}
    job = _servi({"id": 1, "org_id": 42}, "anthropic")
    assert "model_workspace" not in job and "health_reason" not in str(job)


def test_un_membre_ordinaire_ne_recoit_pas_le_workspace_non_plus(_coffre):
    _coffre.meta["anthropic"] = {"workspace_id": "wrkspc_01banc"}
    job = _servi({"id": 1, "org_id": 42}, "anthropic", appelant=_MEMBRE)
    assert "model_workspace" not in job and "model_key" not in job


def test_le_workspace_n_entre_dans_aucune_ligne_de_journal(_coffre, caplog):
    """Exigé à la mise en production : l'absence se LIT dans le journal réellement écrit
    — la trace de remise ET le refus d'un membre existent, et aucun ne porte le numéro."""
    _coffre.meta["anthropic"] = {"workspace_id": "wrkspc_01banc"}
    with caplog.at_level("DEBUG"):
        servi = _servi({"id": 3, "org_id": 42}, "anthropic")
        _servi({"id": 4, "org_id": 42}, "anthropic", appelant=_MEMBRE)
    assert servi["model_workspace"] == "wrkspc_01banc"
    assert "remise" in caplog.text and "REFUSÉE" in caplog.text
    assert "wrkspc_01banc" not in caplog.text and "sk-de-l-org" not in caplog.text


def test_le_contrat_dit_que_le_workspace_ne_sort_que_du_claim():
    d = RJ.Job.model_fields["model_workspace"].description
    assert "op=claim only" in d and "never written to a log" in d
