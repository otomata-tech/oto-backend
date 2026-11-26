"""`agent_access` — à qui une colonne est servie (oto#83).

Le fait mesuré : une colonne de suivi commercial d'un client, déclarée modifiable, dont
la description dit « Où en est VOTRE démarche auprès de cette entreprise. À vous de le
renseigner ». Servie telle quelle à un agent, cette phrase s'adresse à LUI — et un agent
a posé un statut de clôture sur un prospect avant tout contact, sans aucune source.

Ce banc figes les quatre choses qui rendent le cran réel plutôt que décoratif :

1. **le prédicat vient de la FACE**, et la face est posée par le middleware sur le vrai
   chemin d'appel — pas par une constante de test ;
2. **la ligne servie** perd la colonne, ses couches et ses alias plats — et la BASE ne
   perd rien ;
3. **l'écriture est refusée**, et le refus NOMME une destination réelle du tableau ;
4. **le réglage ne se rouvre pas sans TITRE** — sans quoi il suffirait de poser
   `agent_access: "write"` puis d'écrire. Depuis oto#93, c'est le titre (possède ou
   gouverne, le palier du forçage de `readonly`) qui décide, plus la face.

⚠️ Chaque épreuve porte son contrôle négatif : la même chose hors face agent doit
passer. Un banc qui ne mesure que le refus serait vert avec une garde qui refuse TOUT.
"""
from __future__ import annotations

from _origine_servie import AVEC_ORIGINE, row_to_dict_avec_origine  # noqa: E402

import uuid

import pytest

from oto_mcp import session_org
from oto_mcp.datastore import acces_agent as aga
from oto_mcp.datastore import schema as dsv2


@pytest.fixture
def face_agent():
    """Pose la face MCP le temps de l'épreuve — ce que fait `CallContextMiddleware`."""
    jeton = session_org.set_call_face(session_org.FACE_MCP)
    try:
        yield
    finally:
        session_org.reset_call_face(jeton)


SCHEMA = {
    "key": "ref",
    "claimable": {"etat": "a_faire"},
    "fields": [
        {"key": "ref", "type": "text"},
        {"key": "etat", "type": "text"},
        {"key": "note", "type": "text"},
        {"key": "source", "type": "text", "readonly": True},
        {"key": "suivi_commercial", "type": "text", "agent_access": "none"},
        {"key": "score_client", "type": "text", "agent_access": "read"},
    ],
}


# ── 1. Le prédicat : la face, et rien d'autre ────────────────────────────────


def test_hors_appel_d_outil_rien_n_est_un_agent():
    """Le défaut est FAUX. C'est le bon côté de l'erreur : un masquage qui s'appliquerait
    par accident retirerait au propriétaire la colonne de son propre écran."""
    assert aga.appel_d_agent() is False
    assert session_org.current_call_face() is None


def test_la_face_agent_arme_le_predicat(face_agent):
    assert aga.appel_d_agent() is True


def test_le_middleware_pose_la_face_sur_le_vrai_chemin_et_la_retire(monkeypatch):
    """LE maillon porteur. Sans lui, tout le reste de ce banc mesure une constante que
    le test a posée lui-même : le prédicat serait vrai en épreuve et faux en production.

    On joue `CallContextMiddleware.on_call_tool` — le middleware que `_build_mcp` monte
    sur CHAQUE instance MCP, l'anonyme comprise — et on lit la face DEPUIS l'intérieur
    du handler, là où le store la lit."""
    import asyncio

    from oto_mcp.middleware import call_context as mw

    monkeypatch.setattr(mw.guide_run, "active_run_id", _sans_run)

    class _Msg:
        name = "data_write"
        arguments: dict = {}

    class _Ctx:
        message = _Msg()

    vu = {}

    async def call_next(_ctx):
        vu["dedans"] = session_org.current_call_face()
        vu["agent"] = aga.appel_d_agent()
        return "ok"

    m = mw.CallContextMiddleware()
    assert asyncio.run(m.on_call_tool(_Ctx(), call_next)) == "ok"
    assert vu == {"dedans": session_org.FACE_MCP, "agent": True}
    # …et la variable est RENDUE : une face qui fuit d'un appel au suivant masquerait
    # des colonnes à une requête REST servie par la même boucle.
    assert session_org.current_call_face() is None


async def _sans_run(_ctx):
    return None


# ── 2. Ce qui est SERVI ──────────────────────────────────────────────────────


LIGNE = {
    "row_id": "r-1", "created_at": "t0", "updated_at": "t1",
    "data": {
        "ref": "ACME",
        "etat": "a_faire",
        "suivi_commercial": {"valeur": "gagné", "origine": "à contacter"},
        "score_client": "A",
    },
}


def _projeter():
    from oto_mcp.datastore.core import DatastorePg
    return row_to_dict_avec_origine(dict(LIGNE), SCHEMA)


def test_la_ligne_servie_a_un_humain_porte_tout():
    """Contrôle négatif — c'est lui qui donne son sens au suivant."""
    out = _projeter()
    assert out["suivi_commercial"] == "gagné"
    assert out["suivi_commercial.origine"] == "à contacter"
    assert out["score_client"] == "A"


def test_la_ligne_servie_a_un_agent_perd_la_colonne_ET_ses_couches(face_agent):
    out = _projeter()
    assert "suivi_commercial" not in out
    # La couche à plat est fabriquée DEPUIS la colonne : si elle survivait, le masquage
    # servirait la valeur d'avant sous un autre nom, ce qui est pire que de la servir.
    assert "suivi_commercial.origine" not in out
    # Ce qui n'est pas fermé reste servi — sinon le cran couperait le tableau.
    assert out["ref"] == "ACME"
    assert out["etat"] == "a_faire"
    # `read` = servie. C'est tout le point d'avoir trois valeurs et pas un booléen.
    assert out["score_client"] == "A"


def test_le_schema_servi_a_un_agent_perd_la_colonne_et_le_perimetre(face_agent):
    servi = aga.schema_servi(SCHEMA)
    cles = [f["key"] for f in servi["fields"]]
    assert "suivi_commercial" not in cles
    assert "score_client" in cles          # `read` reste DÉCLARÉE, elle est lisible
    assert cles == ["ref", "etat", "note", "source", "score_client"]
    # `claimable` nomme des colonnes : le laisser intact rendrait la colonne masquée
    # par la porte de derrière. ⚠️ Au PREMIER NIVEAU, cette clé n'est lue par aucun
    # moteur — l'assertion ci-dessous ne vaut donc que pour ce que la fonction copie.
    # Le vrai périmètre est éprouvé par les deux bancs suivants.
    assert servi["claimable"] == {"etat": "a_faire"}
    # Le schéma STOCKÉ n'a pas bougé — masquer est une opération de SORTIE.
    assert len(SCHEMA["fields"]) == 6


# ── La porte de derrière, à l'adresse où le moteur lit VRAIMENT (07/09/2026) ──
#
# ⚠️ **Le banc du dessus a gardé ce rinçage sans jamais l'exercer.** Sa fixture pose
# `claimable` au PREMIER NIVEAU du schéma — une adresse que rien ne lit : le moteur
# interroge `fields[role="status"].lifecycle.claimable`. Et le périmètre y nomme
# `etat`, une colonne NON masquée. L'assertion était donc satisfaite par la simple
# copie du schéma : en retirant entièrement la branche de rinçage, trente-deux bancs
# sur trente-deux restaient VERTS.
#
# C'est le pire état d'un banc — vert en n'exerçant rien — et il gardait une porte
# réellement ouverte : mesuré, une colonne `agent_access: "none"` ressortait dans le
# périmètre servi à l'agent, avec son nom ET la valeur sur laquelle elle est filtrée.

PERIMETRE_REEL = {
    "fields": [
        {"key": "statut", "type": "enum", "role": "status",
         "options": ["froid", "chaud"],
         "lifecycle": {"states": ["froid", "chaud"], "terminal": ["chaud"],
                       "claimable": {"suivi_commercial": "chaud",
                                     "statut": "froid"}}},
        {"key": "suivi_commercial", "type": "text", "agent_access": "none"},
    ],
}


def test_le_perimetre_de_reservation_perd_la_colonne_masquee(face_agent):
    """L'assertion qui manquait, à l'adresse que le moteur lit."""
    servi = aga.schema_servi(PERIMETRE_REEL)
    ancre = [f for f in servi["fields"] if f["key"] == "statut"][0]

    assert ancre["lifecycle"]["claimable"] == {"statut": "froid"}, (
        "la colonne masquée ne doit pas ressortir par le périmètre")
    # ⚠️ Et le reste du cycle de vie survit : rincer n'est pas amputer.
    assert ancre["lifecycle"]["states"] == ["froid", "chaud"]
    assert ancre["lifecycle"]["terminal"] == ["chaud"]


def test_rincer_le_perimetre_ne_MUTE_pas_le_schema_stocke(face_agent):
    """La contre-épreuve qui borne le correctif. Le validateur, lui, lit le schéma
    ENTIER — le muter au passage lui retirerait la colonne masquée, et l'écriture d'un
    agent y passerait alors comme un champ hors schéma : le contraire exact du but."""
    import copy
    avant = copy.deepcopy(PERIMETRE_REEL)

    aga.schema_servi(PERIMETRE_REEL)

    assert PERIMETRE_REEL == avant, "masquer est une opération de SORTIE, jamais d'entrée"


def test_le_schema_servi_hors_face_agent_est_le_meme_objet():
    assert aga.schema_servi(SCHEMA) is SCHEMA


def test_la_cle_metier_ne_se_masque_jamais():
    """Même déclarée telle par un schéma ANTÉRIEUR au cran, où `agent_access` n'était
    qu'une clé transportée que rien ne lisait. Sans cette règle, le déploiement rendrait
    d'un coup un tableau illisible et inécrivable pour tous ses agents."""
    legacy = {"key": "ref", "fields": [{"key": "ref", "agent_access": "none"}]}
    assert aga.masquees(legacy) == set()
    assert aga.fermees(legacy) == set()


# ── 3. Ce qui est REFUSÉ, et où porter l'intention ───────────────────────────


def _refus(payload, avant=None, *, agent=True):
    return dsv2.reserved_refusals(SCHEMA, payload, avant, agent=agent)[0]


def test_un_agent_ne_pose_rien_sur_une_colonne_masquee():
    """Aucune exemption pour l'identique : la colonne ne lui est pas servie, il ne peut
    donc pas la tenir d'une lecture — s'il la nomme, il l'a inventée."""
    for payload in ({"suivi_commercial": "gagné"},
                    {"suivi_commercial": None},
                    {"suivi_commercial": {"valeur": "gagné"}},
                    {"suivi_commercial": {"comment": "vu au tel"}}):
        errs = _refus(payload, {"suivi_commercial": "gagné"})
        assert len(errs) == 1, payload
        assert "`suivi_commercial` is not served to you" in errs[0]


def test_une_colonne_en_lecture_refuse_la_valeur_qui_CHANGE():
    errs = _refus({"score_client": "C"}, {"score_client": "A"})
    assert len(errs) == 1
    assert "served to you for READING, not for writing" in errs[0]


def test_une_colonne_en_lecture_accepte_l_identique_et_le_commentaire():
    """Le geste dominant du terrain réémet la fiche ENTIÈRE, colonnes lues comprises.
    Refuser l'identique aurait arrêté une campagne (#623) — et une colonne `read` EST
    servie, donc elle revient forcément dans ce que l'agent réécrit."""
    assert _refus({"score_client": "A"}, {"score_client": "A"}) == []
    assert _refus({"score_client": {"comment": "vu au tel"}},
                  {"score_client": "A"}) == []


def test_une_colonne_en_lecture_est_fermee_des_la_CREATION():
    """Différence assumée avec `readonly`, qui laisse passer une création : `readonly`
    protège une valeur remise par le client, qu'une création n'écrase pas ; ici c'est la
    DESTINATION qui n'est pas à l'agent, et elle ne l'est pas plus sur une ligne neuve."""
    assert len(_refus({"score_client": "A"}, None)) == 1


def test_hors_face_agent_le_cran_est_INERTE():
    """Le contrôle qui protège l'écran du propriétaire. S'il tombe, le lot a fermé une
    colonne à son propre auteur."""
    assert _refus({"suivi_commercial": "gagné", "score_client": "C"},
                  {"suivi_commercial": "à contacter", "score_client": "A"},
                  agent=False) == []


def test_le_refus_NOMME_une_destination_reelle_du_tableau():
    """Un refus qui dit seulement « interdit » fait rejouer le même appel — mesuré dans
    la nuit du 05 au 06/09/2026 sur une autre garde : huit refus sur dix-huit rejouaient
    le geste, la reprise la plus rapide à neuf secondes."""
    msg = _refus({"suivi_commercial": "gagné"})[0]
    assert "`note`" in msg and "`etat`" in msg          # ouvertes à l'écriture
    assert "`source`" not in msg                         # `readonly` : pas une sortie
    assert "`score_client`" not in msg                   # fermée aussi
    assert "replaying this call will return the same refusal" in msg
    # Sur `read`, la couche EST la sortie — le refus doit la nommer.
    lecture = _refus({"score_client": "C"}, {"score_client": "A"})[0]
    assert "`score_client.comment`" in lecture


def test_le_refus_le_dit_quand_il_n_y_a_AUCUNE_destination():
    """Nommer une destination qui n'existe pas serait pire que n'en nommer aucune."""
    schema = {"fields": [{"key": "x", "agent_access": "none"}]}
    msg = dsv2.reserved_refusals(schema, {"x": "v"}, agent=True)[0][0]
    assert "No column of this table is open to you for writing" in msg


def test_enforced_annonce_le_cran():
    """`enforced` est la seule chose qu'un client puisse vérifier contre le serveur qui
    lui répond, plutôt que contre une documentation."""
    dsv2.reset_enforced_keys()
    try:
        assert "agent_access" in dsv2.enforced_keys()
    finally:
        dsv2.reset_enforced_keys()


# ── 4. La DÉCLARATION : ce qui se refuse à la pose ───────────────────────────


def test_une_valeur_inconnue_est_refusee_a_la_pose():
    """LE point du cran. Sur une garde, une faute de frappe silencieuse la DÉSARME —
    `agent_access: "non"` retomberait sur « write » et le propriétaire croirait sa
    colonne fermée. C'est mot pour mot la plaie de `read_only` écrit pour `readonly`,
    à ceci près qu'ici on peut la fermer."""
    errs = dsv2.validate_schema_def({"fields": [{"key": "x", "agent_access": "non"}]})
    assert any("valeur inconnue 'non'" in e and "'write', 'read', 'none'" in e
               for e in errs), errs


@pytest.mark.parametrize("v", ["write", "read", "none"])
def test_les_trois_valeurs_passent(v):
    assert dsv2.validate_schema_def({"fields": [{"key": "x", "agent_access": v}]}) == []


def test_le_cran_ne_se_pose_pas_sous_un_sous_record():
    errs = dsv2.validate_schema_def({"fields": [
        {"key": "o", "type": "object",
         "fields": [{"key": "y", "agent_access": "none"}]}]})
    assert any("is set only at the first level" in e and "agent_access" in e
               for e in errs), errs


def test_la_cle_metier_ne_se_ferme_pas():
    errs = dsv2.validate_schema_def(
        {"key": "ref", "fields": [{"key": "ref", "agent_access": "read"}]})
    assert any("est la clé métier" in e and "agent_access" in e for e in errs), errs


def test_le_vocabulaire_declare_la_cle():
    """La déclaration est SERVIE (`GET /api/datastore/schema/keys`) : une clé appliquée
    et non déclarée ferait mentir l'avertissement des clés non lues."""
    from oto_mcp.datastore import schema_keys as K
    assert "agent_access" in K.ADMISES["champ"]
    assert "agent_access" in K.LUES_PAR_LE_VALIDATEUR
    assert "agent_access" in K.COLONNE_SEULEMENT
    assert "agent_access" in dsv2.interpreted_keys()


# ── 5. Le réglage ne se rouvre pas sans TITRE (oto#93) ───────────────────────


def _titre(tenu: bool):
    return lambda: tenu


def _jamais():
    raise AssertionError("le palier ne doit pas être interrogé quand rien ne bouge")


def test_hors_face_agent_le_proprietaire_fait_ce_qu_il_veut():
    assert aga.refus_de_schema(SCHEMA, {"fields": [{"key": "ref"}]},
                               geste="schema", a_titre=_titre(True)) is None
    assert aga.refus_de_schema(SCHEMA, SCHEMA, geste="patch", a_titre=_jamais) is None


def test_un_agent_ne_repose_pas_un_schema_dont_il_ne_voit_qu_une_part(face_agent):
    """`set_schema` REMPLACE. Le schéma qu'un agent relit ne porte pas les colonnes
    masquées : le reposer les efface, réglage compris. C'est le piège du `_id` relu
    puis réécrit, à l'échelle du format. Refus de FACE, titre ou pas : la face agent
    masque la colonne au propriétaire aussi."""
    msg = aga.refus_de_schema(SCHEMA, aga.schema_servi(SCHEMA), geste="schema",
                              a_titre=_titre(True))
    assert msg and "data_patch_schema" in msg


def test_un_patch_qui_TRANSPORTE_le_reglage_passe(face_agent):
    """Sans ce contrôle négatif, la garde arrêterait tout patch sur un tableau réglé —
    un cran qui ferme le tableau entier au lieu d'une colonne. Et le palier n'est pas
    interrogé : le chemin nominal ne paie aucune lecture d'ownership."""
    fusionne = {**SCHEMA, "fields": SCHEMA["fields"] + [{"key": "neuve"}]}
    assert aga.refus_de_schema(SCHEMA, fusionne, geste="patch", a_titre=_jamais) is None


_GESTES_SUR_LE_REGLAGE = [
    ([{"key": "suivi_commercial", "agent_access": "write"}], "changé"),
    ([{"key": "suivi_commercial"}], "retiré"),
    ([{"key": "ref", "agent_access": "none"}], "posé ailleurs"),
]


def _avec(apres):
    autres = [f for f in SCHEMA["fields"]
              if f["key"] not in {f2["key"] for f2 in apres}]
    return {**SCHEMA, "fields": autres + apres}


@pytest.mark.parametrize("face", [True, False], ids=["outil", "écran"])
@pytest.mark.parametrize("apres,quoi", _GESTES_SUR_LE_REGLAGE)
def test_sans_titre_on_ne_pose_ni_ne_change_ni_ne_retire_le_reglage(apres, quoi, face):
    """Qui n'a reçu qu'un accès en écriture partagé : refusé, PAR LES DEUX FACES —
    c'est le titre qui décide, plus la porte."""
    jeton = session_org.set_call_face(session_org.FACE_MCP) if face else None
    try:
        msg = aga.refus_de_schema(SCHEMA, _avec(apres), geste="patch",
                                  a_titre=_titre(False))
    finally:
        if jeton is not None:
            session_org.reset_call_face(jeton)
    assert msg and "agent_access" in msg and "GOUVERNE" in msg, quoi


@pytest.mark.parametrize("apres,quoi", _GESTES_SUR_LE_REGLAGE)
def test_le_TITRE_pose_le_reglage_par_l_outil(face_agent, apres, quoi):
    """oto#93 : le propriétaire, ou qui gouverne, qui passe par un outil n'est plus
    traité en exécutant — comme pour le forçage de `readonly`."""
    assert aga.refus_de_schema(SCHEMA, _avec(apres), geste="patch",
                               a_titre=_titre(True)) is None, quoi


# ── 6. Bout en bout, sur du vrai SQL ─────────────────────────────────────────


def _blob(ns_id: int, row_id: str) -> dict:
    """Ce que porte la BASE, jamais ce que le store a bien voulu rendre."""
    from oto_mcp.db._conn import _connect
    with _connect() as conn:
        r = conn.execute("SELECT data FROM datastore_rows WHERE ns_id=%s AND row_id=%s",
                         (ns_id, row_id)).fetchone()
    return dict((r or {}).get("data") or {})


def _table(sub="sub-a83"):
    from oto_mcp import db
    from oto_mcp.datastore.core import make_store
    ns = "t-" + uuid.uuid4().hex[:6]
    ns_id = db.create_datastore("user", sub, ns)
    ns = str(ns_id)
    st = make_store(sub)
    # La tête n'admet pas `claimable` (01/10/2026) : posé là, il n'était lu par aucun
    # moteur — c'est la fixture que le rinçage de `schema_servi` éprouve à part.
    st.set_schema(ns, {k: v for k, v in SCHEMA.items() if k != "claimable"})
    return st, ns, ns_id


def test_bout_en_bout_l_agent_ne_voit_ni_n_ecrit_la_colonne(live):
    from oto_mcp.datastore.errors import RowValidationError

    st, ns, ns_id = _table()
    ligne = st.append_row(ns, {"ref": "ACME", "suivi_commercial": "à contacter"})
    rid = ligne["_id"]
    # Une SECONDE ligne pour la file : le bail que pose `claim_next` refuserait ensuite
    # l'écriture, et on mesurerait ce refus-là au lieu du nôtre.
    st.append_row(ns, {"ref": "ZETA", "etat": "a_faire"})
    assert _blob(ns_id, rid)["suivi_commercial"] == "à contacter"

    jeton = session_org.set_call_face(session_org.FACE_MCP)
    try:
        # (a) la ligne lue ne la porte pas.
        assert "suivi_commercial" not in st.get_row(ns, rid)
        # (b) le schéma servi non plus…
        assert "suivi_commercial" not in {f["key"] for f in st.get_schema(ns)["fields"]}
        # …ni le catalogue, second chemin par lequel un schéma complet sort.
        entree = next(e for e in st.list_datastores() if e["id"] == ns_id)
        assert "suivi_commercial" not in {f["key"] for f in entree["schema"]["fields"]}
        # (c) l'écriture est refusée — sur les DEUX gestes qu'un agent enchaîne : le
        # patch par identifiant et le lot par clé métier.
        with pytest.raises(RowValidationError) as patch:
            st.update_row(ns, rid, {"suivi_commercial": "gagné"})
        assert "is not served to you" in str(patch.value)
        with pytest.raises(RowValidationError) as lot:
            st.write_rows(ns, [{"ref": "ACME", "suivi_commercial": "gagné"}], key="ref")
        assert "is not served to you" in str(lot.value)
        # (d) la RÉSERVATION en dernier — c'est elle qui pose un bail, et un bail
        # refuserait les écritures ci-dessus pour une tout autre raison que la nôtre.
        reservee = st.claim_next(ns, worker="w-1")
        assert reservee is not None and "suivi_commercial" not in reservee
    finally:
        session_org.reset_call_face(jeton)

    # ZÉRO perte : la valeur du client est intacte, et le refus n'a rien écrit.
    assert _blob(ns_id, rid)["suivi_commercial"] == "à contacter"


def test_bout_en_bout_l_ecran_du_proprietaire_ne_perd_RIEN(live, monkeypatch):
    """Le contrôle qui compte le plus : une colonne masquée n'est pas une colonne
    supprimée. La face REST — le dashboard, les fronts tiers, les scripts du client —
    la voit et l'écrit comme avant."""
    import _datastore_rest as R

    st, ns, ns_id = _table(sub="u-1")
    ligne = st.append_row(ns, {"ref": "ACME", "suivi_commercial": "à contacter"})
    R.stub_authz(monkeypatch, org_id=None)

    code, corps = R.call("me.datastore.get_schema", path_params={"datastore": ns})
    assert code == 200, corps
    assert "suivi_commercial" in {f["key"] for f in corps["schema"]["fields"]}

    code, corps = R.call("me.datastore.get_row",
                         path_params={"datastore": ns, "row_id": ligne["_id"]})
    assert code == 200, corps
    assert corps["suivi_commercial"] == "à contacter"

    code, corps = R.call("me.datastore.update_row",
                         path_params={"datastore": ns, "row_id": ligne["_id"]},
                         body={"suivi_commercial": "gagné"})
    assert code == 200, corps
    assert _blob(ns_id, ligne["_id"])["suivi_commercial"] == "gagné"


def test_bout_en_bout_un_agent_ne_rouvre_pas_le_reglage(live, monkeypatch):
    """L'épreuve de chute l'a exigée : les épreuves unitaires de `refus_de_schema`
    restaient VERTES quand on retirait la garde de `set_schema` — elles prouvaient que
    la fonction décide bien, pas qu'on l'appelle. C'est ici que le CÂBLAGE se mesure.

    Sans lui, le cran serait décoratif : un agent pose `agent_access: "write"`, écrit,
    et rien n'a jamais eu à être refermé."""
    st, ns, ns_id = _table()
    # Un EXÉCUTANT : il écrit, sans posséder ni gouverner (oto#93). Le palier réel est
    # éprouvé par `test_forcage_readonly_658` ; ici se mesure le CÂBLAGE.
    monkeypatch.setattr(st, "_peut_forcer", lambda ns_id: False)

    jeton = session_org.set_call_face(session_org.FACE_MCP)
    try:
        # (a) reposer le schéma qu'il PEUT lire effacerait la colonne masquée.
        with pytest.raises(ValueError) as pose:
            st.set_schema(ns, st.get_schema(ns))
        assert "data_patch_schema" in str(pose.value)
        # (b) rouvrir la colonne par un patch.
        with pytest.raises(ValueError) as rouvre:
            st.patch_schema(ns, fields=[{"key": "suivi_commercial",
                                         "agent_access": "write"}])
        assert "agent_access" in str(rouvre.value)
        # (c) la retirer du schéma, ce qui la rendrait ensuite purgeable.
        with pytest.raises(ValueError) as retire:
            st.patch_schema(ns, remove=["suivi_commercial"])
        assert "agent_access" in str(retire.value)
        # (d) CONTRÔLE NÉGATIF — un patch ordinaire passe. Le cran ferme une colonne,
        # pas le tableau : s'il fermait le format entier, on l'aurait su par un agent
        # qui ne peut plus déclarer la colonne qu'il vient de découvrir.
        st.patch_schema(ns, fields=[{"key": "note", "type": "text", "max_length": 40}])
    finally:
        session_org.reset_call_face(jeton)

    # Le réglage est INTACT…
    stocke = st.get_schema(ns)
    assert aga.acces_declare(stocke, "suivi_commercial") == "none"
    # …et le même exécutant est refusé depuis l'ÉCRAN aussi : le titre, pas la face.
    with pytest.raises(ValueError) as ecran:
        st.patch_schema(ns, fields=[{"key": "suivi_commercial",
                                     "agent_access": "write"}])
    assert "agent_access" in str(ecran.value)


def test_bout_en_bout_le_PROPRIETAIRE_regle_l_acces_par_l_outil(live):
    """oto#93 : le propriétaire du tableau, sur la face agent, pose, change et retire
    le réglage — avec son VRAI titre (ownership en base), sans doublure."""
    st, ns, ns_id = _table()
    jeton = session_org.set_call_face(session_org.FACE_MCP)
    try:
        st.patch_schema(ns, fields=[{"key": "note", "type": "text",
                                     "agent_access": "read"}])
        st.patch_schema(ns, fields=[{"key": "suivi_commercial",
                                     "agent_access": "write"}])
        # Le refus de FACE reste : reposer un schéma entier effacerait ce qu'il ne voit
        # pas — la face agent masque la colonne `none` au propriétaire aussi.
        with pytest.raises(ValueError) as pose:
            st.set_schema(ns, st.get_schema(ns))
        assert "data_patch_schema" in str(pose.value)
    finally:
        session_org.reset_call_face(jeton)
    stocke = st.get_schema(ns)
    assert aga.acces_declare(stocke, "note") == "read"
    assert aga.acces_declare(stocke, "suivi_commercial") == "write"
