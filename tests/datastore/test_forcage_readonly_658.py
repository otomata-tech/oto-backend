"""Forcer une colonne verrouillée, sur l'appel et sous palier (#658).

`readonly: true` (#606) protège la valeur remise par le client contre son écrasement
par un agent. Le cran était fermé à TOUT LE MONDE : celui à qui la donnée appartient ne
pouvait plus la corriger, et la seule sortie nommée était le schéma — lever, écrire,
remettre.

⚠️ **C'est cette manœuvre-là qu'on ferme, pas celle qu'on offre.** Mesurée sur l'autre
verrou en forme d'ÉTAT (`key_required`, #668) : le 01/09/2026 un agent refusé la
retrouve seul et la rejoue deux fois ; le lendemain un autre passage ne la retrouve pas
et s'arrête. Une exécution interrompue entre « lever » et « remettre » laisse le verrou
OUVERT sans aucun signal. Le forçage vaut donc pour cet appel et rien d'autre.

Ce que ce fichier éprouve, et qui doit ROUGIR si l'une des trois pièces tombe :

1. **le palier**, contre le VRAI `ownership` — propriétaire OK, gouvernant OK, tiers à
   qui le tableau a été partagé EN ÉCRITURE refusé. Ce dernier est le cas qui compte :
   il PEUT écrire (la prémisse est affirmée), et il ne force pas ;
2. **le geste** — sans le paramètre, l'écriture reste refusée pour tout le monde, y
   compris le propriétaire ;
3. **le refus qui nomme le geste** — les deux variantes disent qui peut forcer ;
4. **la trace** — ce qui atterrit vraiment dans le journal, sur les DEUX faces.
"""
from __future__ import annotations

import pytest

from oto_mcp import ownership, session_org
from oto_mcp.datastore import core as dsm
from oto_mcp.datastore import forcage as fcg
from oto_mcp.datastore.errors import RowValidationError

FIELDS = [{"key": "siren", "type": "text"},
          {"key": "adresse", "type": "text", "readonly": True},
          {"key": "libre", "type": "text"}]
SCHEMA = {"key": "siren", "fields": FIELDS}
LIGNE = {"siren": "552081317", "adresse": "1 rue A"}

NS_ID = 7
ORG_PROPRIETAIRE = 42


# ══ 1. Le palier, contre le VRAI `ownership` ═════════════════════════════════
#
# On ne stubbe PAS `ownership.owns` / `can_govern` ici : ce sont eux qu'on éprouve.
# Seules les primitives qu'ils interrogent sont simulées (propriétaire du tableau,
# appartenance, rôles, grants) — sinon le test ne prouverait que le câblage de deux
# booléens, et le jour où le palier se relâcherait il resterait vert.

@pytest.fixture()
def acteurs(monkeypatch):
    """Un tableau possédé par l'org 42, et quatre acteurs distincts.

    `membre`      — membre de l'org PROPRIÉTAIRE, sans rien d'autre : il possède.
    `gerant`      — hors de l'org, grant `role='manager'` : il gouverne sans posséder.
    `org_admin`   — admin de l'org propriétaire : il gouverne ET possède.
    `partenaire`  — hors de l'org, grant `permission='write'` : il ÉCRIT, point.
    """
    from oto_mcp import group_store, org_store, roles

    monkeypatch.setattr(dsm.db, "get_datastore_by_id",
                        lambda ns_id: {"id": ns_id, "datastore": "viviers",
                                       "owner_type": "org",
                                       "owner_id": str(ORG_PROPRIETAIRE),
                                       "schema": SCHEMA})
    monkeypatch.setattr(roles, "is_org_member",
                        lambda sub, org: org == ORG_PROPRIETAIRE
                        and sub in ("membre", "org_admin"))
    monkeypatch.setattr(roles, "is_org_admin",
                        lambda sub, org: org == ORG_PROPRIETAIRE and sub == "org_admin")
    monkeypatch.setattr(roles, "is_platform_admin", lambda sub: False)
    # Chaque acteur n'a QUE son principal user : aucun grant n'arrive par une org ou
    # une équipe de côté, donc ce que le test attribue est exactement ce qui joue.
    monkeypatch.setattr(org_store, "list_orgs_for_user", lambda sub: [])
    monkeypatch.setattr(group_store, "list_groups_for_user",
                        lambda sub, org_id=None: [])
    grants = {"gerant": {"permission": "write", "role": "manager"},
              "partenaire": {"permission": "write", "role": None}}
    monkeypatch.setattr(
        dsm.db, "get_resource_grant",
        lambda rt, rid, ptype, pid: grants.get(pid) if ptype == "user" else None)
    return None


def _store(sub):
    st = dsm.DatastorePg(sub)
    return st


@pytest.mark.parametrize("sub, attendu, pourquoi", [
    ("membre", True, "membre de l'org propriétaire : il POSSÈDE"),
    ("org_admin", True, "admin de l'org propriétaire : il possède ET gouverne"),
    ("gerant", True, "grant `manager` : il GOUVERNE sans posséder"),
    ("partenaire", False, "grant `write` seul : il écrit, il ne force pas"),
])
def test_le_palier_du_forcage(acteurs, sub, attendu, pourquoi):
    """Propriétaire OU gouvernant. L'un des deux suffit ; un tiers ne force rien."""
    assert _store(sub)._peut_forcer(NS_ID) is attendu, pourquoi


def test_le_tiers_refuse_PEUT_pourtant_ecrire(acteurs):
    """⚠️ La prémisse que le palier neutralise, AFFIRMÉE — sans elle le refus du
    partenaire se lirait « il n'avait pas le droit d'écrire », ce qui est faux et
    ferait passer le test pour une raison qui n'a rien à voir.

    C'est tout l'enjeu du cran : le verrou doit tenir contre quelqu'un qui a le droit
    d'écrire, sinon il ne protège de personne."""
    assert ownership.can_access("partenaire", "datastore_namespace",
                                str(NS_ID), "write") is True
    assert ownership.owns("partenaire", "datastore_namespace", str(NS_ID)) is False
    assert ownership.can_govern("partenaire", "datastore_namespace", str(NS_ID)) is False


def test_le_membre_possede_sans_gouverner_et_le_gerant_l_inverse(acteurs):
    """Les deux moitiés du palier sont bien DEUX : ni l'une ni l'autre seule ne
    couvrirait les deux acteurs légitimes."""
    assert ownership.owns("membre", "datastore_namespace", str(NS_ID)) is True
    assert ownership.can_govern("membre", "datastore_namespace", str(NS_ID)) is False
    assert ownership.owns("gerant", "datastore_namespace", str(NS_ID)) is False
    assert ownership.can_govern("gerant", "datastore_namespace", str(NS_ID)) is True


def test_agissant_org_force_sur_le_tableau_de_SON_org(acteurs):
    """Endpoint agissant-org (sub-less) : pas de gouvernance par cette porte, reste
    l'owner-match de l'org elle-même. Une autre org ne force pas."""
    assert dsm.DatastorePg(None, acting_org=ORG_PROPRIETAIRE)._peut_forcer(NS_ID) is True
    assert dsm.DatastorePg(None, acting_org=99)._peut_forcer(NS_ID) is False


# ══ 2. Le geste, de bout en bout ═════════════════════════════════════════════

def _fake_merge_locked(rows):
    def merge_locked(ns_id, row_id, apply_fn, updated_at, **k):
        if row_id not in rows:
            return None
        merged = apply_fn(dict(rows[row_id]))
        rows[row_id] = dict(merged)
        return ({"row_id": row_id, "created_at": "t0", "updated_at": updated_at,
                 "data": dict(merged)}, merged)
    return merge_locked


@pytest.fixture()
def banc(acteurs, monkeypatch):
    """Le tableau `viviers` d'UNE ligne, sur le palier réel — `store(<acteur>)` rend
    un store pour l'acteur voulu, et `etat["maj"]` dit si quelque chose a été écrit."""
    etat = {"lignes": {"r1": dict(LIGNE)}, "creees": [], "maj": []}

    def find(ns_id, key, kv):
        for rid, data in etat["lignes"].items():
            if key and str(data.get(key)) == str(kv):
                return rid
        return None

    def get_row(ns_id, rid):
        data = etat["lignes"].get(rid)
        return ({"row_id": rid, "created_at": "t", "updated_at": "t",
                 "data": dict(data)} if data is not None else None)

    def insert(ns_id, rid, data, *a, **k):
        etat["creees"].append(data)
        etat["lignes"][rid] = dict(data)
        return {"row_id": rid, "created_at": "t", "updated_at": "t", "data": data}

    monkeypatch.setattr(dsm.db, "datastore_find_row_id_by_key", find)
    monkeypatch.setattr(dsm.db, "datastore_get_row", get_row)
    monkeypatch.setattr(dsm.db, "datastore_insert_row", insert)
    monkeypatch.setattr(dsm.db, "datastore_active_lease", lambda ns_id, rid: None)
    fusion = _fake_merge_locked(etat["lignes"])

    def fusion_relevee(ns_id, rid, apply_fn, updated_at, **k):
        # Le patch par `id` écrit par la fusion sous verrou depuis le 12/09/2026 : une
        # fusion ABOUTIE est une écriture, un refus levé dans `apply_fn` n'en est pas une.
        sortie = fusion(ns_id, rid, apply_fn, updated_at, **k)
        if sortie is not None:
            etat["maj"].append(rid)
        return sortie

    monkeypatch.setattr(dsm.db, "datastore_merge_row_locked", fusion_relevee)

    def store(sub):
        st = dsm.DatastorePg(sub)
        monkeypatch.setattr(st, "_resolve", lambda ns, write=False: NS_ID)
        monkeypatch.setattr(st, "_assert_writable", lambda ns_id, rid: None)
        return st
    return store, etat


@pytest.mark.parametrize("sub", ["membre", "org_admin", "gerant"])
def test_le_palier_FORCE_et_la_valeur_change(banc, sub):
    """Propriétaire et gouvernant : le forçage passe, et il écrit vraiment."""
    store, etat = banc
    out = store(sub).update_row("viviers", "r1", {"adresse": "2 rue B"},
                                readonly_override=True)
    assert etat["lignes"]["r1"]["adresse"] == "2 rue B"
    assert out["adresse"] == "2 rue B"
    assert etat["maj"] == ["r1"]


def test_un_TIERS_qui_force_est_REFUSE_et_rien_n_est_ecrit(banc):
    """Le cas qui fait exister le verrou. Il a le droit d'écrire ; il n'a pas celui-là."""
    store, etat = banc
    with pytest.raises(RowValidationError) as exc:
        store("partenaire").update_row("viviers", "r1", {"adresse": "2 rue B"},
                                       readonly_override=True)
    assert etat["maj"] == [] and etat["lignes"]["r1"]["adresse"] == "1 rue A"
    assert "readonly_override" in str(exc.value)


@pytest.mark.parametrize("sub", ["membre", "org_admin", "gerant", "partenaire"])
def test_SANS_le_parametre_l_ecriture_reste_refusee_pour_TOUT_LE_MONDE(banc, sub):
    """Le défaut ne bouge pas d'un pouce : le propriétaire lui-même est refusé tant
    qu'il ne demande rien. C'est ce qui fait du forçage un ACTE, pas une exemption."""
    store, etat = banc
    with pytest.raises(RowValidationError, match="`adresse`"):
        store(sub).update_row("viviers", "r1", {"adresse": "2 rue B"})
    assert etat["maj"] == [] and etat["lignes"]["r1"]["adresse"] == "1 rue A"


def test_le_forcage_ne_deverrouille_QUE_ce_que_l_appel_ecrit(banc):
    """Il vaut pour cet appel — donc l'appel SUIVANT du même acteur est refusé.
    Rien n'a été laissé ouvert derrière lui : c'est toute la raison de la forme."""
    store, etat = banc
    st = store("membre")
    st.update_row("viviers", "r1", {"adresse": "2 rue B"}, readonly_override=True)
    with pytest.raises(RowValidationError, match="`adresse`"):
        store("membre").update_row("viviers", "r1", {"adresse": "3 rue C"})
    assert etat["lignes"]["r1"]["adresse"] == "2 rue B"


def test_le_forcage_passe_aussi_par_la_FUSION_et_par_le_LOT(banc):
    """Les deux autres chemins d'écriture qui rencontrent une ligne en place : la
    fusion sur clé métier (`data_write(row=…)` sans `id`) et le lot."""
    store, etat = banc
    store("membre").append_row("viviers", {"siren": "552081317", "adresse": "2 rue B"},
                               readonly_override=True)
    assert etat["lignes"]["r1"]["adresse"] == "2 rue B"
    store("membre")._write_rows_to_ns(
        NS_ID, [{"siren": "552081317", "adresse": "4 rue D"}], key="siren",
        readonly_override=True)
    assert etat["lignes"]["r1"]["adresse"] == "4 rue D"


def test_un_TIERS_est_refuse_sur_la_fusion_et_sur_le_lot_aussi(banc):
    """Le palier ne dépend pas de la porte empruntée."""
    store, etat = banc
    with pytest.raises(RowValidationError, match="`adresse`"):
        store("partenaire").append_row(
            "viviers", {"siren": "552081317", "adresse": "2 rue B"},
            readonly_override=True)
    with pytest.raises(RowValidationError, match="`adresse`"):
        store("partenaire")._write_rows_to_ns(
            NS_ID, [{"siren": "552081317", "adresse": "2 rue B"}], key="siren",
            readonly_override=True)
    assert etat["lignes"]["r1"]["adresse"] == "1 rue A"


def test_les_colonnes_ORDINAIRES_ne_changent_pas_de_regime(banc):
    """Le forçage ne desserre rien d'autre : une colonne libre s'écrit comme avant,
    avec ou sans lui, et le relevé de forçage reste vide."""
    store, etat = banc
    st = store("membre")
    st.update_row("viviers", "r1", {"libre": "x"}, readonly_override=True)
    assert etat["lignes"]["r1"]["libre"] == "x" and st.off_forced == []


def test_un_forcage_demande_sur_un_tableau_SANS_colonne_verrouillee_ne_coute_rien(
        banc, monkeypatch):
    """Court-circuit : pas de colonne `readonly` ⟹ pas une seule lecture d'ownership.
    C'est ce qui garantit que la présence du paramètre n'ajoute rien au chemin chaud."""
    store, _ = banc
    st = store("membre")
    appels = []
    monkeypatch.setattr(st, "_peut_forcer",
                        lambda ns_id: appels.append(ns_id) or True)
    assert st._forcage_readonly(NS_ID, {"fields": [{"key": "libre"}]}, True) is not None
    assert appels == []
    st._forcage_readonly(NS_ID, SCHEMA, True)
    assert appels == [NS_ID]


def test_sans_demande_le_palier_n_est_JAMAIS_interroge(banc, monkeypatch):
    """Zéro SQL de plus sur le chemin nominal — la propriété qui rend le cran
    déployable sur un tronc qui a gelé 13 minutes le jour même."""
    store, _ = banc
    st = store("membre")
    def _boom(ns_id):
        raise AssertionError("le palier a été lu alors que personne ne demandait rien")
    monkeypatch.setattr(st, "_peut_forcer", _boom)
    with pytest.raises(RowValidationError):
        st.update_row("viviers", "r1", {"adresse": "2 rue B"})


# ══ 3. Le refus nomme le geste ═══════════════════════════════════════════════

def test_le_refus_SANS_parametre_dit_comment_forcer_et_a_qui_c_est_ouvert(banc):
    """Un refus qui dit seulement « colonne verrouillée » renvoie l'appelant chercher
    une manœuvre — c'est exactement ce qui a produit le contournement de
    `key_required` (#668). Celui-ci nomme le paramètre, le palier, et la portée."""
    store, _ = banc
    with pytest.raises(RowValidationError) as exc:
        store("membre").update_row("viviers", "r1", {"adresse": "2 rue B"})
    msg = str(exc.value)
    assert "`adresse`" in msg and "readonly" in msg
    assert "readonly_override=true" in msg              # le geste, nommé
    assert "PROPRIÉTAIRE" in msg and "GOUVERNE" in msg  # à qui il est ouvert
    assert "cet appel" in msg                           # et sa portée
    assert "`adresse.comment`" in msg                   # où va la divergence
    assert exc.value.details == {"expected_column": "adresse.comment"}


def test_le_refus_du_TIERS_dit_qui_peut_et_ne_le_renvoie_pas_au_parametre(banc):
    """Il l'a déjà passé. Lui redire « passe-le » l'enverrait chercher une manœuvre
    pour l'obtenir — le défaut qu'on ferme. Le refus nomme qui peut, et la sortie
    praticable pour lui : la couche `comment`, ou demander au propriétaire."""
    store, _ = banc
    with pytest.raises(RowValidationError) as exc:
        store("partenaire").update_row("viviers", "r1", {"adresse": "2 rue B"},
                                       readonly_override=True)
    msg = str(exc.value)
    assert "readonly_override" in msg and "readonly_override=true" not in msg
    assert "PROPRIÉTAIRE" in msg and "GOUVERNE" in msg
    assert "partagé" in msg                             # pourquoi lui, non
    assert "`adresse.comment`" in msg


# ══ 4. La trace — ce qui atterrit VRAIMENT dans le journal ═══════════════════

def test_le_forcage_est_verse_au_releve_d_appel_MCP(banc):
    """Face MCP : la substitution part par `note_call_trace`, le seam que le sink du
    journal relit. Ligne, colonne, valeur remplacée — le « qui » est déjà stampé par
    le journal (`sub`, `org_id`), on ne le répète pas."""
    store, _ = banc
    holder: dict = {}
    token = session_org.set_call_trace(holder)
    try:
        store("membre").update_row("viviers", "r1", {"adresse": "2 rue B"},
                                   readonly_override=True)
    finally:
        session_org.reset_call_trace(token)
    assert holder["readonly_forced"] == [
        {"row": "r1", "col": "adresse", "was": "1 rue A", "now": "2 rue B"}]


def test_le_releve_MCP_passe_l_allowlist_et_atteint_les_args_de_la_ligne():
    """⚠️ Le relevé est FILTRÉ à l'écriture : une clé absente de `_TRACED_ARGS` est
    silencieusement jetée. Sans cette assertion, tout le reste du forçage pourrait
    marcher et la trace ne jamais exister. On reproduit ensuite la ligne du sink —
    c'est elle qui décide ce qui entre dans `args`."""
    from oto_mcp import server
    assert "readonly_forced" in server._TRACED_ARGS
    trace = {"readonly_forced": [{"row": "r1", "col": "adresse", "was": "1 rue A"}],
             "ns_id": 7, "interne": "jamais journalisé"}
    args = {**{"datastore": "viviers"},
            **{k: v for k, v in trace.items() if k in server._TRACED_ARGS}}
    assert args["readonly_forced"] == trace["readonly_forced"]
    assert "interne" not in args


def test_un_geste_REFUSE_ne_journalise_aucun_forcage(banc):
    """Le relevé suit l'écriture, pas l'intention : chercher dans le journal une
    valeur qui n'a pas bougé est la pire chose qu'on puisse faire lire à quelqu'un."""
    store, _ = banc
    holder: dict = {}
    token = session_org.set_call_trace(holder)
    try:
        with pytest.raises(RowValidationError):
            store("partenaire").update_row("viviers", "r1", {"adresse": "2 rue B"},
                                           readonly_override=True)
    finally:
        session_org.reset_call_trace(token)
    assert holder == {}


def test_le_forcage_atteint_la_ligne_REST_en_VRAIE_liste(banc, monkeypatch):
    """Face REST : `datastore_journal.record` → `calllog.log_rest_call` → la table.

    ⚠️ La clé ne passe PAS par `args` : `truncated_args` y stringifie tout ce qui
    n'est pas scalaire et coupe à 300 caractères — la liste reviendrait en `\"[{'row':
    …\"`, illisible colonne par colonne. Elle rejoint la ligne comme `fields`, après
    la troncature. C'est cette propriété-là que le test fige."""
    # ⚠️ `calllog._insert_rest` importe `db` et `access` À L'APPEL : patcher
    # `calllog.db` n'existe pas, c'est le module d'origine qu'il faut prendre.
    from oto_mcp import access, db
    from oto_mcp.datastore import journal as dj

    store, _ = banc
    st = store("membre")
    st.update_row("viviers", "r1", {"adresse": "2 rue B"}, readonly_override=True)
    assert st.off_forced, "le store n'a rien relevé — la face REST n'aurait rien à dire"

    lignes: list = []
    monkeypatch.setattr(db, "insert_tool_call", lignes.append)
    monkeypatch.setattr(access, "current_org", lambda sub: 35)
    dj.record(dj.TOOL_WRITE, sub="membre",
              ctx=dj.NsContext(ns_id=NS_ID, name="viviers"), row_id="r1",
              fields=["adresse"], forced=st.off_forced)
    assert len(lignes) == 1
    forced = lignes[0]["args"]["readonly_forced"]
    assert isinstance(forced, list) and isinstance(forced[0], dict)
    assert forced == [{"row": "r1", "col": "adresse",
                       "was": "1 rue A", "now": "2 rue B"}]


def test_sans_forcage_la_ligne_REST_n_a_PAS_la_cle(banc, monkeypatch):
    """Absente, pas à `[]` : un forçage se cherche par la PRÉSENCE de la clé, et une
    clé toujours là ferait relire chaque écriture ordinaire comme un candidat."""
    from oto_mcp import access, db
    from oto_mcp.datastore import journal as dj

    lignes: list = []
    monkeypatch.setattr(db, "insert_tool_call", lignes.append)
    monkeypatch.setattr(access, "current_org", lambda sub: 35)
    dj.record(dj.TOOL_WRITE, sub="membre",
              ctx=dj.NsContext(ns_id=NS_ID, name="viviers"), row_id="r1",
              fields=["libre"], forced=[])
    assert "readonly_forced" not in lignes[0]["args"]


def test_le_lot_releve_CHAQUE_ligne_forcee(banc):
    """Un lot force autant de lignes qu'il en porte, et le journal doit pouvoir dire
    LAQUELLE. Un relevé qui ne garderait que la dernière serait pire qu'aucun."""
    store, etat = banc
    etat["lignes"]["r2"] = {"siren": "389256712", "adresse": "9 rue Z"}
    holder: dict = {}
    token = session_org.set_call_trace(holder)
    try:
        store("membre")._write_rows_to_ns(
            NS_ID, [{"siren": "552081317", "adresse": "2 rue B"},
                    {"siren": "389256712", "adresse": "8 rue Y"}],
            key="siren", readonly_override=True)
    finally:
        session_org.reset_call_trace(token)
    assert [(e["row"], e["was"], e["now"]) for e in holder["readonly_forced"]] == [
        ("r1", "1 rue A", "2 rue B"), ("r2", "9 rue Z", "8 rue Y")]


# ══ 5. La description SERVIE annonce le paramètre ════════════════════════════
#
# Une capacité qu'aucun texte n'annonce n'existe pas pour un agent : il retombera sur
# la manœuvre qu'on cherche à supprimer. Le contrôle porte sur le texte SERVI, celui
# qui est relu à chaque appel — pas sur `docs/`, que personne ne lit.

def test_data_write_ANNONCE_le_forcage_et_a_qui_il_est_ouvert():
    from oto_mcp.tools import datastore as tools_ds
    doc = tools_ds.register.__doc__ or ""
    for outil in _docstrings_du_module(tools_ds):
        if "data_write" in outil[0]:
            doc = outil[1]
            break
    assert "readonly_override" in doc
    assert "OWNER" in doc and "GOVERNS" in doc
    assert "this one call" in doc or "this call" in doc


def test_data_patch_schema_qui_POSE_le_verrou_nomme_la_sortie():
    """Le cran se documente des DEUX côtés : celui qui le pose doit dire que
    verrouiller une colonne ne veut pas dire que plus personne ne pourra la corriger."""
    from oto_mcp.capabilities.datastore import columns as caps
    desc = next(c.description for c in caps.CAPABILITIES
                if c.key == "me.datastore.patch_schema")
    assert "readonly_override=true" in desc and "OWNER" in desc


def test_les_deux_capacites_REST_d_ecriture_l_annoncent_aussi():
    from oto_mcp.capabilities.datastore import rows as caps
    for cle in ("me.datastore.append_row", "me.datastore.update_row"):
        cap = next(c for c in caps.CAPABILITIES if c.key == cle)
        assert "readonly_override" in cap.description, cle
        assert "readonly_override" in cap.Input.model_fields, cle


def _docstrings_du_module(module):
    """Les `(source, docstring)` des fonctions imbriquées dans `register` — la face
    MCP les déclare dans un closure, donc elles ne sont pas des attributs du module."""
    import ast
    import inspect
    arbre = ast.parse(inspect.getsource(module))
    return [(getattr(n, "name", ""), ast.get_docstring(n) or "")
            for n in ast.walk(arbre)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


# ══ 6. Le relevé lui-même ════════════════════════════════════════════════════

def test_le_releve_est_borne_en_nombre_et_en_longueur():
    """Un lot de huit mille lignes ne doit pas produire une ligne de journal de huit
    mille entrées, ni y recopier une fiche entière."""
    f = fcg.Forcage(demande=True, autorise=True)
    for i in range(fcg.MAX_RELEVE + 10):
        f.relever(f"col{i}", "x" * 500, "y")
        f.rattacher(f"r{i}")
    assert len(f.forcees) == fcg.MAX_RELEVE
    assert len(f.forcees[0]["was"]) == fcg.MAX_VALEUR


def test_un_apply_rejoue_ne_compte_pas_deux_fois():
    """`datastore_merge_row_locked` documente que son `_apply` peut être rejoué : deux
    entrées pour un seul remplacement se liraient comme deux forçages."""
    f = fcg.Forcage(demande=True, autorise=True)
    f.relever("adresse", "1 rue A", "2 rue B")
    f.relever("adresse", "1 rue A", "2 rue B")
    f.rattacher("r1")
    assert f.releve() == [{"row": "r1", "col": "adresse",
                           "was": "1 rue A", "now": "2 rue B"}]
